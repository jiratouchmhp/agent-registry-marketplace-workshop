# Optional Agent Registry SkillEncryptionSpec (CMEK) Attachment
#
# Why this uses `terraform_data` and defaults to `enable_cmek_spec = false`:
#   1. `hashicorp/google` (>= 7.39.0) does not yet provide a native resource for
#      `SkillEncryptionSpec` (Preview).
#   2. ZERO-SKILLS PRECONDITION (FAILED_PRECONDITION):
#      Agent Registry allows setting or changing `SkillEncryptionSpec` for a
#      multi-region (`us` or `eu`) ONLY when ZERO Skills exist in that location.
#      If ANY Skill has already been registered in that location, the API
#      immediately rejects the update with `FAILED_PRECONDITION`.
#   3. LOCATION MAPPING:
#      - Skills in `us` require a Cloud KMS key in location `us`.
#      - Skills in `eu` require a Cloud KMS key in location `europe`.
#
# Enable with `enable_cmek_spec = true` ONLY on a clean location before creating
# any skills (for example, before bootstrapping skills in `us` or `eu`).

resource "terraform_data" "skill_encryption_spec" {
  count = var.enable_cmek_spec ? 1 : 0

  triggers_replace = {
    project_id          = var.project_id
    cmek_location       = var.cmek_location
    skill_cmek_location = local.skill_cmek_location
    crypto_key_id       = google_kms_crypto_key.skill_cmek.id
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-euo", "pipefail", "-c"]

    environment = {
      GCP_PROJECT_ID      = var.project_id
      KMS_LOCATION        = var.cmek_location
      SKILL_CMEK_LOCATION = local.skill_cmek_location
      KMS_CRYPTO_KEY_ID   = google_kms_crypto_key.skill_cmek.id
    }

    command = <<-EOT
      echo "[PRE-FLIGHT] Checking zero-skills precondition for location '$SKILL_CMEK_LOCATION' (KMS location '$KMS_LOCATION')..."
      echo "WARNING: Agent Registry rejects SkillEncryptionSpec updates with FAILED_PRECONDITION if ANY Skill exists in '$SKILL_CMEK_LOCATION'."

      TOKEN="$(gcloud auth print-access-token)"
      EXISTING_SKILLS="$(curl -fsSL \
        -H "Authorization: Bearer $TOKEN" \
        "https://agentregistry.googleapis.com/v1/projects/$GCP_PROJECT_ID/locations/$SKILL_CMEK_LOCATION/skills?pageSize=1")"

      if echo "$EXISTING_SKILLS" | grep -q '"skills"'; then
        echo "ERROR (FAILED_PRECONDITION): One or more Skills already exist in projects/$GCP_PROJECT_ID/locations/$SKILL_CMEK_LOCATION." >&2
        echo "Delete all existing Skills in '$SKILL_CMEK_LOCATION' (with ?force=true) before enabling CMEK SkillEncryptionSpec, or keep enable_cmek_spec = false." >&2
        exit 1
      fi

      # Prefer gcloud CLI if the alpha agent-registry skill-encryption-spec group is installed;
      # otherwise fall back to the verified v1alpha REST PATCH endpoint.
      if gcloud alpha agent-registry skill-encryption-spec update --help >/dev/null 2>&1; then
        gcloud alpha agent-registry skill-encryption-spec update \
          --project="$GCP_PROJECT_ID" \
          --location="$SKILL_CMEK_LOCATION" \
          --kms-key="$KMS_CRYPTO_KEY_ID"
      else
        curl -fsSL -X PATCH \
          -H "Authorization: Bearer $TOKEN" \
          -H "Content-Type: application/json" \
          -d "{\"cryptoKeyName\": \"$KMS_CRYPTO_KEY_ID\"}" \
          "https://agentregistry.googleapis.com/v1alpha/projects/$GCP_PROJECT_ID/locations/$SKILL_CMEK_LOCATION/skillEncryptionSpec?updateMask=cryptoKeyName"
      fi
    EOT
  }

  depends_on = [
    google_project_service.security_governance["agentregistry.googleapis.com"],
    google_kms_crypto_key_iam_member.agent_registry_p4sa_cmek,
  ]
}
