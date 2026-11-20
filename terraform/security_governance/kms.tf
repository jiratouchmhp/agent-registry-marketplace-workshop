# Cloud KMS Key Rings, Crypto Keys, and Least-Privilege IAM Bindings
#
# CRITICAL ARCHITECTURAL DISTINCTION:
#   1. Native Agent Registry CMEK (`google_kms_crypto_key.skill_cmek`):
#      - Strictly ENCRYPTION AT REST (`ENCRYPT_DECRYPT`) for Skill packages in
#        multi-regions `us` or `eu`.
#      - Location mapping rule:
#          * Skills in `us` -> Cloud KMS location MUST be `us`
#          * Skills in `eu` -> Cloud KMS location MUST be `europe`
#      - Granted to the Agent Registry P4SA (`service-<PROJECT_NUMBER>@gcp-sa-agentregistry.iam.gserviceaccount.com`)
#        with `roles/cloudkms.cryptoKeyEncrypterDecrypter`.
#
#   2. Artifact Cryptographic Signing (`google_kms_crypto_key.skill_artifact_signer`):
#      - Agent Registry does NOT natively sign skills.
#      - Custom CI/CD + Middleware supply-chain integrity uses an ASYMMETRIC_SIGN
#        ECDSA P-256 key (`EC_SIGN_P256_SHA256`).
#      - CI publisher SA gets `roles/cloudkms.signerVerifier` to sign builds.
#      - Runtime middleware SA gets ONLY `roles/cloudkms.publicKeyViewer` to
#        fetch the public key and verify signatures (it can NEVER sign).

# -----------------------------------------------------------------------------
# 1. CMEK Key (Encryption at Rest for Agent Registry Skills)
# -----------------------------------------------------------------------------

resource "google_kms_key_ring" "agent_registry_cmek" {
  project  = var.project_id
  name     = "agent-registry-skill-cmek"
  location = var.cmek_location

  depends_on = [google_project_service.security_governance["cloudkms.googleapis.com"]]
}

resource "google_kms_crypto_key" "skill_cmek" {
  name            = "agent-registry-skills-at-rest"
  key_ring        = google_kms_key_ring.agent_registry_cmek.id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "7776000s" # 90 days

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = "SOFTWARE"
  }

  # Set prevent_destroy = true in production environments.
  lifecycle {
    prevent_destroy = false
  }
}

# Grant the Agent Registry P4SA encrypter/decrypter access on the CMEK key
# BEFORE updating SkillEncryptionSpec.
resource "google_kms_crypto_key_iam_member" "agent_registry_p4sa_cmek" {
  crypto_key_id = google_kms_crypto_key.skill_cmek.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${local.agent_registry_p4sa}"

  depends_on = [google_project_service.security_governance["agentregistry.googleapis.com"]]
}

# -----------------------------------------------------------------------------
# 2. Asymmetric Signing Key (Cryptographic Integrity for Skill Artifacts)
# -----------------------------------------------------------------------------

resource "google_kms_key_ring" "skill_signing" {
  project  = var.project_id
  name     = "agent-registry-skill-signing"
  location = var.location

  depends_on = [google_project_service.security_governance["cloudkms.googleapis.com"]]
}

resource "google_kms_crypto_key" "skill_artifact_signer" {
  name     = "skill-artifact-ecdsa-p256"
  key_ring = google_kms_key_ring.skill_signing.id
  purpose  = "ASYMMETRIC_SIGN"

  version_template {
    algorithm        = "EC_SIGN_P256_SHA256"
    protection_level = "SOFTWARE"
  }

  # Set prevent_destroy = true in production environments.
  lifecycle {
    prevent_destroy = false
  }
}

# CI/CD publisher identity: signs skill digests during release pipelines.
resource "google_kms_crypto_key_iam_member" "ci_publisher_signer" {
  crypto_key_id = google_kms_crypto_key.skill_artifact_signer.id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${local.ci_publisher_sa_email}"
}

# Runtime polyglot-middleware identity: strictly read-only public key access.
resource "google_kms_crypto_key_iam_member" "middleware_public_key_viewer" {
  crypto_key_id = google_kms_crypto_key.skill_artifact_signer.id
  role          = "roles/cloudkms.publicKeyViewer"
  member        = "serviceAccount:${var.middleware_sa_email}"
}
