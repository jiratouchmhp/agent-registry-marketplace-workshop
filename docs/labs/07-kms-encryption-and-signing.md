# Lab 07: Cloud KMS Encryption (CMEK) vs. Cryptographic Artifact Signing

**Workstream:** B — Security & Governance *(Standalone Runnable — No Workstream A Terraform State Required)*  
**Duration:** 40 minutes  
**Prerequisites:** Python `.venv` verified (`make test`).

> **Running Workstream B Standalone**  
> This lab requires **zero Workstream A state** (`terraform/core/` does not need to be deployed):  
> - **Local Mock Mode (Offline):** Start `make run-mock` (`:8090`) and `make run-middleware` (`:8080`) using local ECDSA P-256 PEM keys (cryptographically identical to Cloud KMS `EC_SIGN_P256_SHA256`).  
> - **Standalone Live GCP Mode:** Provision [terraform/security_governance/](../../terraform/security_governance/kms.tf) directly against any existing Google Cloud Agent Registry project (`make tf-sec-plan`) using [terraform/security_governance/terraform.tfvars.example](../../terraform/security_governance/terraform.tfvars.example)—no `terraform/core/` outputs or state required.

---

## Learning Objectives

By the end of this lab, you will:
1. Articulate the critical architectural boundary between:
   - **Native Agent Registry CMEK (`SkillEncryptionSpec`, Preview)**: **Encryption at rest ONLY** in multi-regions `us` or `eu` (with a strict **zero-skills precondition**).
   - **Custom Cloud KMS Asymmetric Signing (`EC_SIGN_P256_SHA256`)**: **Supply-chain provenance & tamper resistance** enforced between CI/CD and the Polyglot Middleware.
2. Generate an ECDSA P-256 keypair (or provision Cloud KMS keys via `terraform/security_governance/`), sign skill archives with [security-governance/kms/sign_and_verify.py](../../security-governance/kms/sign_and_verify.py) and `python -m tools.plugin_sync`, and verify both `SkillRevision.sha256Hash` and `frontmatter.metadata.kms_signature`.
3. Enable `REQUIRE_KMS_SIGNATURE=true` on the Polyglot Middleware and **prove** that signed skills are served while unsigned or tampered skills are immediately rejected!

---

## Architecture Comparison: CMEK vs. Asymmetric Signing

| Attribute | Native CMEK (`SkillEncryptionSpec`) | Cryptographic Artifact Signing (CI + Middleware) |
| :--- | :--- | :--- |
| **What It Protects Against** | Storage key sovereignty & crypto-shredding of stored Skill archives in Google Cloud | Compromised registry credentials, rogue uploads bypassing CI, or payload tampering in transit/storage |
| **Cloud KMS Key Purpose & Algorithm** | `ENCRYPT_DECRYPT` (`GOOGLE_SYMMETRIC_ENCRYPTION`, 90-day rotation) | `ASYMMETRIC_SIGN` (`EC_SIGN_P256_SHA256`) |
| **Mandatory Location Rule** | **Multi-region ONLY:** Skills in `us` → KMS key in **`us`**; Skills in `eu` → KMS key in **`europe`**! | Regional (e.g. `us-central1`) |
| **Zero-Skills Precondition** | `PATCH .../skillEncryptionSpec` **fails with `FAILED_PRECONDITION`** if **any** Skill already exists in that multi-region | None — can be enabled or key-rotated anytime |
| **Least-Privilege IAM** | P4SA (`service-<NUM>@gcp-sa-agentregistry.iam.gserviceaccount.com`) → `roles/cloudkms.cryptoKeyEncrypterDecrypter` | CI Publisher SA → `roles/cloudkms.signerVerifier`<br>Middleware SA → **`roles/cloudkms.publicKeyViewer` ONLY** |

---

## Part 1: Native CMEK (`SkillEncryptionSpec`) & the Zero-Skills Precondition

Inspect [terraform/security_governance/kms.tf](../../terraform/security_governance/kms.tf) and [terraform/security_governance/skill_encryption_spec.tf](../../terraform/security_governance/skill_encryption_spec.tf):

1. Notice how `kms.tf` provisions **two separate key rings**:
   - `agent-registry-skill-cmek` in `var.cmek_location` (`"us"` or `"europe"`) with key `agent-registry-skills-at-rest` (`ENCRYPT_DECRYPT`) granted to the Agent Registry P4SA (`roles/cloudkms.cryptoKeyEncrypterDecrypter`).
   - `agent-registry-skill-signing` in `var.location` (`"us-central1"`) with key `skill-artifact-ecdsa-p256` (`ASYMMETRIC_SIGN`, `EC_SIGN_P256_SHA256`).
2. Notice how `skill_encryption_spec.tf` defaults `enable_cmek_spec = false` and runs a **pre-flight zero-skills check** (`GET .../locations/{us|eu}/skills?pageSize=1`) before invoking `gcloud alpha agent-registry skill-encryption-spec update` (or `PATCH .../v1alpha/.../skillEncryptionSpec?updateMask=cryptoKeyName`), preventing unexpected `FAILED_PRECONDITION` errors on populated locations.

### Live GCP Mode — Provision KMS & Optionally Attach CMEK

```bash
cp terraform/security_governance/terraform.tfvars.example \
   terraform/security_governance/terraform.tfvars
# Edit project_id, middleware_sa_email, ci_publisher_sa_email, and URNs.
# Keep enable_cmek_spec = false unless your target `us` or `eu` location has ZERO existing skills!

export TF_VAR_auth_provider_client_secret="workshop-placeholder-secret"
make tf-sec-plan
terraform -chdir=terraform/security_governance apply

# Export the provisioned Cloud KMS key IDs
terraform -chdir=terraform/security_governance output cmek_crypto_key_id
terraform -chdir=terraform/security_governance output signing_crypto_key_id
```

---

## Part 2: Cryptographic Skill Signing & Tamper Detection

### Mode A: Local Offline Mode (ECDSA P-256 PEM — Identical Cryptography to Cloud KMS)

1. Generate a workshop ECDSA P-256 keypair with [security-governance/kms/sign_and_verify.py](../../security-governance/kms/sign_and_verify.py):
   ```bash
   .venv/bin/python security-governance/kms/sign_and_verify.py keygen \
     --private-out /tmp/kms-demo/skill-signer-private.pem \
     --public-out /tmp/kms-demo/skill-signer-public.pem
   ```

2. Build an unsigned skill ZIP with `tools.plugin_sync --dry-run`, sign it with `sign_and_verify.py sign`, and verify it:
   ```bash
   .venv/bin/python -m tools.plugin_sync sync \
     --plugin-dir starter-marketplace/plugins/gcp-finops-toolkit \
     --project acme-agent-governance \
     --location us-central1 \
     --output-dir /tmp/kms-demo/unsigned \
     --skip-mcp-probe \
     --dry-run

   # Sign the canonical skill payload & embed kms_signature + kms_key_version into SKILL.md metadata
   .venv/bin/python security-governance/kms/sign_and_verify.py sign \
     --input /tmp/kms-demo/unsigned/cost-anomaly-triage.zip \
     --output /tmp/kms-demo/cost-anomaly-triage-signed.zip \
     --private-key /tmp/kms-demo/skill-signer-private.pem \
     --attestation-out /tmp/kms-demo/cost-anomaly-triage.attestation.json

   # Verify both SHA-256 and ECDSA P-256 signature -> verified=true
   .venv/bin/python security-governance/kms/sign_and_verify.py verify \
     --input /tmp/kms-demo/cost-anomaly-triage-signed.zip \
     --public-key /tmp/kms-demo/skill-signer-public.pem
   ```

3. **Tamper test:** Modify a single line in `SKILL.md` inside the signed ZIP without re-signing, and prove verification fails closed:
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

   # Must exit non-zero with verified=false!
   .venv/bin/python security-governance/kms/sign_and_verify.py verify \
     --input /tmp/kms-demo/cost-anomaly-triage-tampered.zip \
     --public-key /tmp/kms-demo/skill-signer-public.pem || echo "Tampered ZIP rejected as expected!"
   ```

### Mode B: Live Cloud KMS Mode (`:asymmetricSign` & `/publicKey`)

With `signing_crypto_key_id` provisioned by `terraform/security_governance/`:

```bash
export KMS_SIGNING_KEY_VERSION="$(terraform -chdir=terraform/security_governance output -raw signing_crypto_key_id)/cryptoKeyVersions/1"

# 1. Fetch the trusted public key PEM (requires only roles/cloudkms.publicKeyViewer)
.venv/bin/python security-governance/kms/sign_and_verify.py fetch-public-key \
  --kms-key-version "${KMS_SIGNING_KEY_VERSION}" \
  --output /tmp/kms-demo/cloud-kms-public.pem

# 2. Sign & sync directly from CI using Cloud KMS :asymmetricSign (requires roles/cloudkms.signerVerifier)
.venv/bin/python -m tools.plugin_sync sync \
  --plugin-dir starter-marketplace/plugins/gcp-finops-toolkit \
  --registry-url "https://agentregistry.googleapis.com/v1" \
  --project "<your-gcp-project-id>" \
  --location "us-central1" \
  --kms-key-version "${KMS_SIGNING_KEY_VERSION}" \
  --skip-mcp-probe
```

---

## Part 3: Enforce `REQUIRE_KMS_SIGNATURE=true` in the Polyglot Middleware

Prove end-to-end that the Middleware ([middleware/app/security_verifier.py](../../middleware/app/security_verifier.py)) serves KMS-signed skills and blocks unsigned/rogue skills:

```bash
# Terminal 1: Ensure mock_registry is running & clean
# (Run `make run-mock` if not already running)
curl -fsSL -X POST http://127.0.0.1:8090/_mock/reset

# Terminal 2: Start Middleware WITH strict KMS signature verification enabled!
REQUIRE_KMS_SIGNATURE=true \
KMS_PUBLIC_KEY_PEM_PATH=/tmp/kms-demo/skill-signer-public.pem \
make run-middleware
```

In Terminal 3:

> **Standalone Note:** If you did not start `make run-samples` (`:8091`), `make demo-sync` will log an expected MCP live-probe warning and cleanly fall back to the static FinOps `TOOL_SPEC`—everything in this signing verification demo still succeeds.

```bash
# 1. Sync gcp-finops-toolkit SIGNED with our trusted private key
SIGNING_KEY_PEM_PATH=/tmp/kms-demo/skill-signer-private.pem make demo-sync

# 2. Upload an UNSIGNED rogue skill directly to mock_registry (simulating bypassing CI)
.venv/bin/python - << 'EOF'
import base64, io, zipfile, httpx

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("rogue-unsigned-skill/SKILL.md", "---\nname: rogue-unsigned-skill\ndescription: Unsigned direct upload.\n---\n# Rogue Skill\n")

resp = httpx.post(
    "http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/us-central1/skills",
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

# 3. Query Middleware /v1/catalog:
#    - gcp-finops-toolkit skills have kmsVerified=true and are SERVED
#    - private-rogue-unsigned-skill is REJECTED in rejectedSkills!
curl -fsSL http://127.0.0.1:8080/v1/catalog | .venv/bin/python -m json.tool
```

---

## Verification Checkpoint

- [ ] Explained why native CMEK (`us`/`eu` + zero-skills precondition) is encryption at rest only and requires separate Cloud KMS `EC_SIGN_P256_SHA256` asymmetric signing for integrity/provenance.
- [ ] Verified a signed skill ZIP passes `sign_and_verify.py verify`, while modifying 1 byte fails closed.
- [ ] Confirmed Middleware with `REQUIRE_KMS_SIGNATURE=true` serves signed `gcp-finops-toolkit` (`kmsVerified: true`) and rejects `private-rogue-unsigned-skill`.
- [ ] `.venv/bin/pytest -v tests/test_workstream_b_security.py -k kms` passes completely.

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `FAILED_PRECONDITION` when enabling `SkillEncryptionSpec` | One or more Skills already exist in the target `us` or `eu` location | Delete all skills in that multi-region (with `?force=true`) before updating `SkillEncryptionSpec`, or provision CMEK on a fresh project/location first. |
| `KMS key location mismatch` for EU skills | Created CMEK key ring in `eu` instead of `europe` | Cloud KMS uses **`europe`** for Agent Registry skills in **`eu`** (see `var.cmek_location` validation in [terraform/security_governance/variables.tf](../../terraform/security_governance/variables.tf)). |
| Middleware rejects ALL skills with `Failed to load trusted KMS public key` | `REQUIRE_KMS_SIGNATURE=true` without `KMS_PUBLIC_KEY_PEM_PATH`, `KMS_PUBLIC_KEY_PEM`, or `KMS_SIGNING_KEY_VERSION` | Always provide the trusted ECDSA P-256 public key PEM (or set `KMS_SIGNING_KEY_VERSION` on Cloud Run so Middleware fetches `/publicKey` directly via `roles/cloudkms.publicKeyViewer`) when `REQUIRE_KMS_SIGNATURE=true` (fail-closed by design). |

---

**Next Lab:** [Lab 08: Access Control, Agent Gateway IAP & Immutable Audit →](08-access-control-and-audit.md)
