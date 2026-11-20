# Production Go-Live Readiness Gate Checklist

Execute every gate below before promoting the Enterprise Agent Marketplace (`terraform/core/`, `terraform/security_governance/`, `middleware/`, and CI/CD pipelines) from a workshop/sandbox environment to live production. Every item has an explicit pass/fail check.

---

## Gate 1: Infrastructure, Terraform State & Cloud Run (`terraform/core/`)

- [ ] **No unauthenticated Cloud Run demo access**: `allow_unauthenticated_workshop_demo = false` in `terraform/core/terraform.tfvars`.
  - *Check:* `gcloud run services get-iam-policy polyglot-middleware --project="${GCP_PROJECT_ID}" --region="${GCP_LOCATION}"` returns **zero** `allUsers` or `allAuthenticatedUsers` bindings (repeat for `sample-mcp-server` and `sample-a2a-agent`).
- [ ] **Deletion protection enabled**: `deletion_protection = true` in `terraform/core/terraform.tfvars` for all Cloud Run v2 services.
- [ ] **Immutable pinned container digests**: `middleware_image`, `sample_mcp_image`, and `sample_a2a_image` pin `@sha256:...` digests in Artifact Registry (never `:latest`).
- [ ] **Restricted ingress & availability**: Change `ingress = "INGRESS_TRAFFIC_ALL"` in `terraform/core/cloud_run.tf` to `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` (fronted by an HTTPS Load Balancer + IAP / Cloud Armor) or enforce Cloud Run IAM (`roles/run.invoker`) with OIDC ID tokens; raise `min_instance_count >= 1` for latency-sensitive middleware.
- [ ] **Remote Terraform state**: Configure a versioned, CMEK-encrypted GCS backend for both `terraform/core/` and `terraform/security_governance/` (never commit local `.tfstate` or `terraform.tfvars`).
- [ ] **KMS destruction guard**: Set `lifecycle { prevent_destroy = true }` on `google_kms_crypto_key.skill_cmek` and `google_kms_crypto_key.skill_artifact_signer` in `terraform/security_governance/kms.tf`.
- [ ] **HCL formatting & tests green**: `make validate-hcl` and `.venv/bin/pytest -v tests/test_workstream_b_security.py` both exit `0`.

---

## Gate 2: Least-Privilege IAM & Identity Separation

- [ ] **Middleware SA strictly read-only**: `agent-marketplace-middleware@${GCP_PROJECT_ID}.iam.gserviceaccount.com` holds **only** `roles/agentregistry.viewer` (project level) and `roles/cloudkms.publicKeyViewer` (CryptoKey resource level).
  - *Check:*
    ```bash
    # 1. Project-level IAM check
    gcloud projects get-iam-policy "${GCP_PROJECT_ID}" \
      --flatten="bindings[].members" \
      --filter="bindings.members:agent-marketplace-middleware@" \
      --format="table(bindings.role)"
    # FAIL if roles/agentregistry.editor, roles/agentregistry.admin, or roles/cloudkms.signerVerifier appears!

    # 2. CryptoKey resource-level IAM check (terraform/security_governance/kms.tf binds KMS roles here)
    gcloud kms keys get-iam-policy skill-artifact-ecdsa-p256 \
      --keyring="agent-registry-skill-signing" \
      --location="${GCP_LOCATION}" \
      --project="${GCP_PROJECT_ID}"
    # Confirm middleware SA has ONLY roles/cloudkms.publicKeyViewer (NEVER roles/cloudkms.signerVerifier).
    ```
- [ ] **CI Publisher SA has zero admin privileges**: `agent-registry-ci-publisher@...` holds `roles/agentregistry.editor` + `roles/cloudkms.signerVerifier` + `roles/artifactregistry.writer` + bucket-level `roles/storage.objectUser` (never `roles/agentregistry.admin`).
- [ ] **Runtime MCP/A2A workloads isolated**: Backing workloads run as dedicated SAs (e.g., `finops-sample-workloads@...`) with **zero** Agent Registry IAM and never fall back to the default Compute Engine SA.
- [ ] **Platform Agents (ADK) least privilege**: Runtime agents hold only `roles/agentregistry.viewer`, `roles/agentidentity.user`, and `roles/iap.egressor` (never `editor`/`admin`).
- [ ] **Per-`Skill` resource IAM verified**: Restricted skills grant `roles/agentregistry.viewer` only to approved teams/SAs (`gcloud agent-registry skills get-iam-policy private-<id> --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"`).

---

## Gate 3: Skill Scanning, Lifecycle & Organization Policy

- [ ] **Custom Org Policy enforced**: `custom.requireAgentRegistrySkillScanning` (denies `DISABLED`) or strict allow-only `custom.requireBlockingSkillScanning` (`security-governance/org-policy/require-skill-scanning-blocking.yaml`, requires `ENABLED_BLOCKING`) is enforced at Org/Folder/Project level.
  - *Check:*
    ```bash
    gcloud org-policies describe custom.requireAgentRegistrySkillScanning \
      --project="${GCP_PROJECT_ID}" --effective
    # Must show: enforce: true
    ```
- [ ] **Zero active skills with findings**: Every `STATE_ACTIVE` skill has `scanResult.scanState == "SUCCEEDED"`, `defaultRevisionFindingCount == 0`, and valid `source_repo` + 40-char `git_commit` metadata.
- [ ] **Clear ownership boundary**: Set `enable_skills_bootstrap = false` in `terraform/core/terraform.tfvars` once GitLab CI owns skill releases (or keep `--only-skills` so Terraform `agent_registry_services.tf` remains sole owner of `google_agent_registry_service` resources).

---

## Gate 4: Cloud KMS (CMEK at Rest + Asymmetric Signing)

- [ ] **CMEK configured before first skill (if required by compliance)**:
  - Key ring in `us` (for `us` skills) or **`europe`** (for `eu` skills); P4SA (`service-<NUM>@gcp-sa-agentregistry.iam.gserviceaccount.com`) holds `roles/cloudkms.cryptoKeyEncrypterDecrypter`.
  - `SkillEncryptionSpec` attached cleanly while location held zero skills (`enable_cmek_spec = true`).
- [ ] **Fail-closed signature verification active**: `require_kms_signature = true` (`REQUIRE_KMS_SIGNATURE=true`) on `polyglot-middleware`, configured with `KMS_SIGNING_KEY_VERSION` or a mounted trusted `KMS_PUBLIC_KEY_PEM_PATH`.
  - *Check:* `curl -fsSL "${MIDDLEWARE_URL}/healthz"` returns `"requireKmsSignature": true`.
- [ ] **Negative tamper test passed**: Uploading an unsigned skill or modifying 1 byte of a signed skill results in rejection inside `GET /v1/catalog` (`rejectedSkills[]`) and fails `sign_and_verify.py verify` with non-zero exit code.

---

## Gate 5: Runtime Gateway, Auth Manager Bindings & IAP v2 UAP

- [ ] **AuthProvider hardened**: `google_agent_identity_auth_provider` sets `enable_pkce = true`, minimal `allowed_scopes`, explicit `blocked_scopes` (blocking directory admin scopes), and reads `client_secret` from Secret Manager / `TF_VAR_auth_provider_client_secret` (never committed `.tfvars`).
- [ ] **Binding IAM filtering verified**: `google_agent_registry_binding` includes required `auth_provider_binding`; verified that `bindings:fetchAvailable` returns the binding only for callers with `roles/agentidentity.user`.
- [ ] **Agent Gateway attached**: `google_network_services_agent_gateway` runs `CLIENT_TO_AGENT` with `registries` pointing to `//agentregistry.googleapis.com/projects/${GCP_PROJECT_ID}/locations/${GCP_LOCATION}`, and `roles/iap.egressor` is bound to short projected resource IDs (`mcp_server_resource_id`, `a2a_agent_resource_id`).
- [ ] **IAP v2 UAP baked in `DRY_RUN`, then enforced**:
  - `apply-uap.sh --validate-only` passed with `principal://` callers (zero URNs or `spiffe://`).
  - Ran BigQuery Query 5.3 (`DRY_RUN` readiness) for 7–14 days confirming `would_deny_calls == 0` for all legitimate read-only tools.
  - Promoted to enforced by **removing** `iamEnforcementMode` from IAP `metadata` (preserving sibling keys; never setting `"ENFORCED"`).
- [ ] **Client-side IDE guard verified (optional defense-in-depth)**: `security-governance/hooks/hooks.json` + executable (`0755`) `scripts/policy_guard.py` bundled and tested on `PreToolUse` & `PostToolUse`.

---

## Gate 6: CI/CD Keyless OIDC & Supply-Chain Hygiene

- [ ] **Zero static credentials anywhere**: No GCP JSON key files, no raw Git tokens in `.tfvars` or URLs, no `auth_provider_client_secret` literals in repo files.
  - *Check:*
    ```bash
    # Audits existing terraform.tfvars files (ignores # and // comment lines; exits non-zero on any match)
    bash -euo pipefail -c '
      pattern="(allow_unauthenticated_workshop_demo[[:space:]]*=[[:space:]]*true|deletion_protection[[:space:]]*=[[:space:]]*false|require_kms_signature[[:space:]]*=[[:space:]]*false|auth_provider_client_secret[[:space:]]*=|glpat-|ghp_|AKIA|PRIVATE KEY)"
      checked=0; failed=0
      for f in terraform/core/terraform.tfvars terraform/security_governance/terraform.tfvars; do
        [ -f "$f" ] || continue
        checked=$((checked + 1))
        if nl -ba "$f" | grep -Ev "^[[:space:]]*[0-9]+[[:space:]]*(#|//)" | grep -E "$pattern"; then
          echo "FAIL: unsafe setting or secret found in $f" >&2
          failed=1
        fi
      done
      [ "$checked" -gt 0 ] || { echo "FAIL: no terraform.tfvars files found to audit" >&2; exit 1; }
      [ "$failed" -eq 0 ] || exit 1
      echo "TFVARS_AUDIT_CLEAN ($checked file(s) checked)"
    '
    ```
- [ ] **Keyless GitLab OIDC → GCP WIF active**: `starter-marketplace/.gitlab-ci.yml` configured with `GCP_WORKLOAD_IDENTITY_PROVIDER` and `GCP_SERVICE_ACCOUNT_EMAIL`, with WIF attribute condition restricting impersonation to the approved GitLab project and protected refs/tags.
- [ ] **MR quarantine & release gates enforced**: Merge Requests execute `register:draft-mr` (`TARGET_STATE_DRAFT`); only protected semver tags (`v*.*.*`) or manual approval on protected `main` run `register:active-release` (`TARGET_STATE_ACTIVE`).
- [ ] **Option 2 pull ingestion safe (if used)**: `skills_repo_token_env` / `--git-token-env` passes only an env var *name*, never a raw secret.

---

## Gate 7: WORM Audit Logging & SIEM Alerting

- [ ] **Dual `ADMIN_READ` Data Access enabled**: Verified on **both** `agentregistry.googleapis.com` and `apphubendpoint.googleapis.com`.
  - *Check:* `gcloud projects get-iam-policy "${GCP_PROJECT_ID}" --format=json | jq '.auditConfigs[] | select(.service=="agentregistry.googleapis.com" or .service=="apphubendpoint.googleapis.com")'`.
- [ ] **Locked WORM bucket active**: `agent-governance-audit` has `locked: true`, `analyticsEnabled: true`, and compliance-approved `retentionDays` (`gcloud logging buckets describe agent-governance-audit --location="${GCP_LOCATION}" --project="${GCP_PROJECT_ID}"`).
- [ ] **Log sink healthy**: `agent-governance-audit-sink` `writerIdentity` holds `roles/logging.bucketWriter` and test events appear in BigQuery Log Analytics (`_AllLogs`).
- [ ] **Log-based alerts wired to SIEM / On-Call**:
  1. Attempted scan downgrade (`scanningConfig.policy="DISABLED"`).
  2. Denied `iap.resources.egressViaIAP` calls (`granted=false`) & `unregisteredResource` shadow egress.
  3. Any `cloudkms.googleapis.com` `AsymmetricSign` by a principal other than `agent-registry-ci-publisher@...`.
  4. Any `SetIamPolicy`, `UpdateSkillEncryptionSpec`, or `DeleteBinding` mutation on `agentregistry.googleapis.com`.

---

## Gate 8: Incident Response & Rollback Runbook

| Incident Scenario | Immediate Containment (< 2 min) | Recovery & Forensics |
|---|---|---|
| **Buggy or compromised `SkillRevision`** | **Option A (Atomic Rollback):** `PATCH .../skills/private-<id>?updateMask=defaultRevision` pointing to last-known-good immutable revision.<br>**Option B (Kill Switch):** `PATCH .../skills/private-<id>?updateMask=targetState` with `{"targetState": "TARGET_STATE_DISABLED"}` (reversible — **never** jump straight to terminal `TARGET_STATE_DECOMMISSIONED` during triage!). | Inspect `Finding` records (`GET .../findings`), diff `git_commit` provenance, verify middleware `/v1/catalog` drops the skill, publish clean signed revision, then re-activate. |
| **Compromised Cloud KMS signing key version** | Disable compromised `CryptoKeyVersion` (`gcloud kms keys versions disable ...`) and set `REQUIRE_KMS_SIGNATURE=true` against a freshly created key version (`cryptoKeyVersions/N+1`). | Audit WORM `cloudkms.googleapis.com` `AsymmetricSign` logs for unauthorized signatures; re-sign all trusted skills from verified Git commits; destroy old version post-forensics. |
| **IAP v2 UAP blocking legitimate MCP tools** | Restore `iamEnforcementMode: "DRY_RUN"` on the IAP metadata (or detach the PolicyBinding via `gcloud iam policy-bindings delete`) to stop user impact. | Run BigQuery Query 5.1 & 5.3 on `agent-governance-audit._AllLogs` to identify the mismatched CEL attribute or missing `readOnlyHint`; fix `TOOL_SPEC` or UAP JSON; validate and re-enforce. |
| **Rogue MCP server or compromised agent** | Revoke `roles/iap.egressor` on the projected `McpServer`/`Agent`, delete the `Binding`, and delete the `Service` (`DELETE .../services/{id}`). | Query WORM logs (`cloudaudit.googleapis.com/data_access` + `networkservices.googleapis.com/gateway_requests`) filtered by `principal_subject` to scope blast radius. |
