# Lab 07: Cloud KMS Encryption (CMEK) vs. Artifact Signing

**Workstream:** B (Security & Governance — Standalone, no Workstream A Terraform state required)  
**Duration:** 40 minutes  
**Prerequisites:** Python `.venv` verified (`make test`).

> **Running Workstream B Standalone**  
> This lab needs zero Workstream A state (`terraform/core/` does not need to be deployed):  
> - **Local Mock Mode (Offline):** Start `make run-mock` (`:8090`) and `make run-middleware` (`:8080`) with local ECDSA P-256 PEM keys (the same algorithm Cloud KMS uses for `EC_SIGN_P256_SHA256`).  
> - **Live GCP Mode (Optional):** Deploy [`terraform/security_governance/kms.tf`](../../terraform/security_governance/kms.tf) directly against any GCP project (`make tf-sec-plan`) using [`terraform/security_governance/terraform.tfvars.example`](../../terraform/security_governance/terraform.tfvars.example).

## Files You'll Work With

No `starter/` coding stub in this lab — you'll run and inspect the repo files below directly.

| Role | File | What You'll Do |
| :--- | :--- | :--- |
| **Run / Inspect** | [`security-governance/kms/sign_and_verify.py`](../../security-governance/kms/sign_and_verify.py) | Generate ECDSA P-256 keys, sign skill ZIPs, verify signatures, and fetch Cloud KMS public keys. |
| **Run** | [`tools/plugin_sync/cli.py`](../../tools/plugin_sync/cli.py) | Package unsigned skill ZIPs locally (`--dry-run`) and sign/ingest skills with Cloud KMS (`--kms-key-version`). |
| **Inspect** | [`middleware/app/security_verifier.py`](../../middleware/app/security_verifier.py) | Read how Middleware verifies `sha256Hash` and `kms_signature` before serving any skill. |
| **Inspect (Live GCP)** | [`terraform/security_governance/kms.tf`](../../terraform/security_governance/kms.tf) | Review both KMS key rings: multi-region CMEK (`ENCRYPT_DECRYPT`) and regional signing (`EC_SIGN_P256_SHA256`). |
| **Inspect (Live GCP)** | [`terraform/security_governance/skill_encryption_spec.tf`](../../terraform/security_governance/skill_encryption_spec.tf) | Review the zero-skills pre-flight check before enabling `SkillEncryptionSpec`. |
| **Configure (Live GCP)** | [`terraform/security_governance/terraform.tfvars.example`](../../terraform/security_governance/terraform.tfvars.example) | Starting template for Workstream B Terraform variables. |

## Learning Objectives

By the end of this lab, you will:
1. Know the difference between native CMEK (encryption at rest in `us`/`eu`) and Cloud KMS asymmetric signing (tamper protection in CI and Middleware).
2. Generate an ECDSA P-256 keypair, sign a skill ZIP, verify it, and watch verification fail after a 1-line edit.
3. Enable `REQUIRE_KMS_SIGNATURE=true` on Middleware and prove signed plugins pass while an unsigned upload gets rejected.

---

## CMEK vs. Asymmetric Signing

These two KMS controls solve completely different problems. Keep them separate:

| Dimension | Native CMEK (`SkillEncryptionSpec`, Preview) | Asymmetric Signing (CI + Middleware) |
| :--- | :--- | :--- |
| **What it protects against** | Key ownership and crypto-shredding of stored Skill ZIPs at rest | Compromised registry creds, rogue uploads bypassing CI, or tampered ZIPs |
| **KMS key type** | `ENCRYPT_DECRYPT` (`GOOGLE_SYMMETRIC_ENCRYPTION`, 90-day rotation) | `ASYMMETRIC_SIGN` (`EC_SIGN_P256_SHA256`) |
| **Where it runs** | Multi-region only: `us` skills need a `us` key; `eu` skills need an `europe` key | Regional in Bangkok (`asia-southeast3`), signing skills in `global`, `us`, or `eu` |
| **Precondition** | Fails with `FAILED_PRECONDITION` if any Skill exists in that multi-region | None (enable or rotate keys anytime) |
| **IAM roles** | Agent Registry P4SA gets `roles/cloudkms.cryptoKeyEncrypterDecrypter` | CI SA gets `roles/cloudkms.signerVerifier`; Middleware SA gets `roles/cloudkms.publicKeyViewer` only |

In APAC, standalone `Skills` live in `global` with Google-managed encryption at rest, while regional asymmetric signing in `asia-southeast3` protects supply-chain integrity.

## Step 1: Sign a Skill ZIP and Test Tamper Detection

Start locally with ECDSA P-256 PEM keys. The signature format is identical to Cloud KMS `EC_SIGN_P256_SHA256`.

1. **Generate a local ECDSA P-256 keypair:**

   ```bash
   .venv/bin/python security-governance/kms/sign_and_verify.py keygen \
     --private-out /tmp/kms-demo/skill-signer-private.pem \
     --public-out /tmp/kms-demo/skill-signer-public.pem
   ```

2. **Build an unsigned skill ZIP, sign it, and verify it:**

   ```bash
   .venv/bin/python -m tools.plugin_sync sync \
     --plugin-dir starter-marketplace/plugins/gcp-finops-toolkit \
     --project acme-agent-governance \
     --location asia-southeast3 \
     --output-dir /tmp/kms-demo/unsigned \
     --skip-mcp-probe \
     --dry-run

   # Sign the skill payload and embed kms_signature + kms_key_version in SKILL.md metadata
   .venv/bin/python security-governance/kms/sign_and_verify.py sign \
     --input /tmp/kms-demo/unsigned/cost-anomaly-triage.zip \
     --output /tmp/kms-demo/cost-anomaly-triage-signed.zip \
     --private-key /tmp/kms-demo/skill-signer-private.pem \
     --attestation-out /tmp/kms-demo/cost-anomaly-triage.attestation.json

   # Verify SHA-256 and ECDSA P-256 signature
   .venv/bin/python security-governance/kms/sign_and_verify.py verify \
     --input /tmp/kms-demo/cost-anomaly-triage-signed.zip \
     --public-key /tmp/kms-demo/skill-signer-public.pem
   ```

   You should see `verified: true` in the JSON output.

3. **Tamper with 1 line and watch verification fail:**

   Append a single line to `SKILL.md` inside the signed ZIP without re-signing, then re-run `verify`:

   ```bash
   .venv/bin/python - << 'EOF'
   import zipfile
   from pathlib import Path

   src = Path("/tmp/kms-demo/cost-anomaly-triage-signed.zip")
   dst = Path("/tmp/kms-demo/cost-anomaly-triage-tampered.zip")
   with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zout:
       for info in zin.infolist():
           data = zin.read(info)
           if info.filename.endswith("SKILL.md"):
               data += b"\n# Malicious tampered instruction appended post-signing\n"
           zout.writestr(info, data)
   EOF

   # Must exit non-zero with verified=false
   .venv/bin/python security-governance/kms/sign_and_verify.py verify \
     --input /tmp/kms-demo/cost-anomaly-triage-tampered.zip \
     --public-key /tmp/kms-demo/skill-signer-public.pem || echo "Tampered ZIP rejected as expected!"
   ```

## Step 2: Enforce `REQUIRE_KMS_SIGNATURE=true` in Middleware

Now prove end-to-end that [`middleware/app/security_verifier.py`](../../middleware/app/security_verifier.py) serves signed skills and rejects unsigned uploads.

1. **Start `mock_registry` and restart Middleware with signature enforcement:**

   ```bash
   # Terminal 1: Start mock_registry on :8090 if not already running (stays in the foreground)
   make run-mock

   # Terminal 2: Start (or restart) Middleware with strict KMS verification enabled
   REQUIRE_KMS_SIGNATURE=true \
   KMS_PUBLIC_KEY_PEM_PATH=/tmp/kms-demo/skill-signer-public.pem \
   make run-middleware
   ```

2. **Reset `mock_registry`, sync a signed plugin, upload an unsigned rogue skill, and query `/v1/catalog`:**

   *(If `make run-samples` is not running on `:8091`, `make demo-sync` logs a harmless MCP probe warning and falls back to the static FinOps tool spec.)*

   ```bash
   # Terminal 3:
   # 1. Reset mock_registry to a clean slate
   curl -fsSL -X POST http://127.0.0.1:8090/_mock/reset

   # 2. Sync gcp-finops-toolkit signed with your trusted private key
   SIGNING_KEY_PEM_PATH=/tmp/kms-demo/skill-signer-private.pem make demo-sync

   # 3. Upload an unsigned rogue skill directly to mock_registry (simulating a CI bypass)
   .venv/bin/python - << 'EOF'
   import base64, io, zipfile, httpx

   buf = io.BytesIO()
   with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
       zf.writestr("rogue-unsigned-skill/SKILL.md", "---\nname: rogue-unsigned-skill\ndescription: Unsigned direct upload.\n---\n# Rogue Skill\n")

   resp = httpx.post(
       "http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/asia-southeast3/skills",
       params={"skillId": "rogue-unsigned-skill"},
       json={
           "displayName": "Rogue Unsigned Skill",
           "type": "SIMPLE",
           "targetState": "TARGET_STATE_ACTIVE",
           "scanningConfig": {"policy": "ENABLED_BLOCKING"},
           "initialRevision": {"archiveUploadSource": {"archiveContent": base64.b64encode(buf.getvalue()).decode("ascii")}},
       },
   )
   print("Uploaded rogue skill:", resp.status_code)
   EOF

   # 4. Query Middleware /v1/catalog
   curl -fsSL http://127.0.0.1:8080/v1/catalog | .venv/bin/python -m json.tool
   ```

   You should see `gcp-finops-toolkit` under `plugins` (with its skills under `skills` showing `kmsVerified: true`), while `private-rogue-unsigned-skill` is blocked under `rejectedSkills`.

## Step 3: Inspect `kms.tf` and `skill_encryption_spec.tf`

Open both Terraform files under `terraform/security_governance/` to see how live GCP provisions these controls:

- [`terraform/security_governance/kms.tf`](../../terraform/security_governance/kms.tf): Creates two separate key rings:
  - `agent-registry-skill-cmek` in `var.cmek_location` (`"us"` or `"europe"`), granting `roles/cloudkms.cryptoKeyEncrypterDecrypter` to the Agent Registry P4SA.
  - `agent-registry-skill-signing` in `var.location` (`"asia-southeast3"`), creating `skill-artifact-ecdsa-p256` (`ASYMMETRIC_SIGN`, `EC_SIGN_P256_SHA256`).
- [`terraform/security_governance/skill_encryption_spec.tf`](../../terraform/security_governance/skill_encryption_spec.tf): Keeps `enable_cmek_spec = false` by default. When enabled, it runs a pre-flight check (`GET .../locations/{us|eu}/skills?pageSize=1`) to confirm zero skills exist before calling `PATCH .../v1alpha/.../skillEncryptionSpec?updateMask=cryptoKeyName`.

---

## Going Live on GCP (Optional)

**Region note:** For APAC, keep `location = "asia-southeast3"` (Bangkok signing key) and `auth_provider_location = "asia-southeast1"` (Singapore Agent Identity fallback). Leave `enable_cmek_spec = false` (standalone `Skills` live in `global`), `enable_agent_gateway = false`, and `lock_audit_bucket = false` in workshop sandboxes. See [Attendee Quickstart](../attendee-quickstart.md) for the full regional table.

### 1. Provision Cloud KMS via `terraform/security_governance/`

```bash
cp terraform/security_governance/terraform.tfvars.example \
   terraform/security_governance/terraform.tfvars
# Edit project_id, middleware_sa_email, ci_publisher_sa_email, and URNs.
# Verify these sandbox defaults:
#   location               = "asia-southeast3"
#   auth_provider_location = "asia-southeast1"
#   enable_cmek_spec       = false
#   enable_agent_gateway   = false
#   lock_audit_bucket      = false

export TF_VAR_auth_provider_client_secret="workshop-placeholder-secret"
make tf-sec-plan
terraform -chdir=terraform/security_governance apply

terraform -chdir=terraform/security_governance output cmek_crypto_key_id
terraform -chdir=terraform/security_governance output signing_crypto_key_id
```

### 2. Sign and Sync with Cloud KMS `:asymmetricSign`

```bash
export KMS_SIGNING_KEY_VERSION="$(terraform -chdir=terraform/security_governance output -raw signing_crypto_key_id)/cryptoKeyVersions/1"

# 1. Fetch the trusted public key PEM (needs only roles/cloudkms.publicKeyViewer)
.venv/bin/python security-governance/kms/sign_and_verify.py fetch-public-key \
  --kms-key-version "${KMS_SIGNING_KEY_VERSION}" \
  --output /tmp/kms-demo/cloud-kms-public.pem

# 2. Sign and sync standalone Skills into global using Cloud KMS :asymmetricSign (needs roles/cloudkms.signerVerifier)
.venv/bin/python -m tools.plugin_sync ingest \
  --source-type local \
  --path starter-marketplace/plugins/gcp-finops-toolkit \
  --registry-url "https://agentregistry.googleapis.com/v1" \
  --project "<your-gcp-project-id>" \
  --location "asia-southeast3" \
  --skills-location "${AGENT_REGISTRY_SKILLS_LOCATION:-global}" \
  --kms-key-version "${KMS_SIGNING_KEY_VERSION}" \
  --wait-for-scan \
  --only-skills \
  --skip-mcp-probe
```

---

## Verification Checkpoint

- [ ] Explained why native CMEK (`us`/`eu` + zero-skills check) covers encryption at rest only, while Cloud KMS `EC_SIGN_P256_SHA256` signing blocks tampering.
- [ ] Verified a signed skill ZIP passes `sign_and_verify.py verify` and fails closed after a 1-line edit.
- [ ] Confirmed Middleware with `REQUIRE_KMS_SIGNATURE=true` serves signed `gcp-finops-toolkit` (with `kmsVerified: true` on its verified skills) and rejects `private-rogue-unsigned-skill`.
- [ ] `.venv/bin/pytest -v tests/test_workstream_b_security.py -k kms` passes.

## Troubleshooting

| Symptom | Why It Happens | How to Fix |
| :--- | :--- | :--- |
| `FAILED_PRECONDITION` enabling `SkillEncryptionSpec` | At least one Skill already exists in the target `us` or `eu` location | Delete all skills in that multi-region (`?force=true`) before updating `SkillEncryptionSpec`, or configure CMEK on a fresh location first. |
| `KMS key location mismatch` for EU skills | Created the CMEK key ring in `eu` instead of `europe` | Cloud KMS uses `europe` for Agent Registry skills in `eu` (see `var.cmek_location` in [`terraform/security_governance/variables.tf`](../../terraform/security_governance/variables.tf)). |
| Middleware rejects all skills (`Failed to load trusted KMS public key`) | `REQUIRE_KMS_SIGNATURE=true` is enabled without `KMS_PUBLIC_KEY_PEM_PATH`, `KMS_PUBLIC_KEY_PEM`, or `KMS_SIGNING_KEY_VERSION` | Provide the trusted ECDSA P-256 public key PEM locally, or set `KMS_SIGNING_KEY_VERSION` on Cloud Run so Middleware reads `/publicKey` via `roles/cloudkms.publicKeyViewer`. |
| `400 FAILED_PRECONDITION` promoting a re-signed revision under `ENABLED_BLOCKING` | Patched `defaultRevision` before the async live scan (~10s) completed | Always pass `--wait-for-scan` so `tools.plugin_sync` waits for `scanState == "SUCCEEDED"` before updating `defaultRevision`. |

---

**Next Lab:** [Lab 08: Access Control, Agent Gateway IAP & Audit Logging →](08-access-control-and-audit.md)
