# Cloud KMS: Native CMEK (`SkillEncryptionSpec`) vs. Asymmetric Signing (`EC_SIGN_P256_SHA256`)

Google Cloud Agent Registry supports **Customer-Managed Encryption Keys (CMEK)** for storage encryption at rest, but **does not natively cryptographically sign skill artifacts**. Production deployments combine **both** mechanisms using separate Cloud KMS key rings, key purposes, and least-privilege IAM roles.

---

## 1. Architectural Comparison Matrix

| Dimension | Native CMEK (`SkillEncryptionSpec`, Preview) | Custom Asymmetric Signing (CI + Middleware) |
|---|---|---|
| **Security Guarantee** | Encryption at rest, customer key sovereignty, crypto-shredding of stored Skill ZIPs | Supply-chain provenance & tamper detection (blocks compromised registry creds or out-of-band uploads bypassing CI) |
| **Cloud KMS Key Ring** | `agent-registry-skill-cmek` (`terraform/security_governance/kms.tf`) | `agent-registry-skill-signing` (`terraform/security_governance/kms.tf`) |
| **Cloud KMS Key** | `agent-registry-skills-at-rest` | `skill-artifact-ecdsa-p256` |
| **Purpose & Algorithm** | `ENCRYPT_DECRYPT` (`GOOGLE_SYMMETRIC_ENCRYPTION`, 90-day `rotation_period = "7776000s"`) | `ASYMMETRIC_SIGN` (`EC_SIGN_P256_SHA256`) |
| **Location Constraint** | **Multi-region ONLY**: Skills in `us` → KMS **`us`**; Skills in `eu` → KMS **`europe`** *(APAC deployments host standalone Skills in `global` with Google-managed encryption at rest + regional Asymmetric Signing)* | Regional — natively supported in **Bangkok (`asia-southeast3`)** and **Singapore (`asia-southeast1`)**, signing skills stored in `global`, `us`, or `eu` |
| **Precondition** | **Strict ZERO-SKILLS precondition** — fails with `FAILED_PRECONDITION` if any skill exists in `us` or `eu` | None — enable or rotate at any time |
| **IAM Principals** | Agent Registry P4SA (`service-<NUM>@gcp-sa-agentregistry.iam.gserviceaccount.com`) → `roles/cloudkms.cryptoKeyEncrypterDecrypter` | CI Publisher SA (`agent-registry-ci-publisher`) → `roles/cloudkms.signerVerifier`<br>Middleware SA (`agent-marketplace-middleware`) → **`roles/cloudkms.publicKeyViewer` ONLY** |
| **Terraform Resource** | `google_kms_crypto_key.skill_cmek` + `terraform_data.skill_encryption_spec` (`enable_cmek_spec = true`) | `google_kms_crypto_key.skill_artifact_signer` + IAM bindings in `kms.tf` |

---

## 2. Native CMEK (`SkillEncryptionSpec`) Configuration

### Location Mapping & Zero-Skills Precondition

1. **Multi-region only**: Agent Registry `SkillEncryptionSpec` applies strictly to multi-region skill locations **`us`** and **`eu`** (not single regions like `us-central1`).
2. **EU → `europe` mismatch trap**: Skills in Agent Registry location `eu` **must** use a Cloud KMS key ring in location **`europe`** (`terraform/security_governance/variables.tf` enforces `contains(["us", "europe"], var.cmek_location)`, while `terraform/security_governance/versions.tf` maps `"europe" -> "eu"` in `local.skill_cmek_location`).
3. **Zero-skills precondition**: `PATCH .../skillEncryptionSpec` fails immediately with `FAILED_PRECONDITION` if **even a single Skill** exists in that multi-region location.
4. **P4SA IAM grant first**: Grant `roles/cloudkms.cryptoKeyEncrypterDecrypter` to `service-${PROJECT_NUMBER}@gcp-sa-agentregistry.iam.gserviceaccount.com` **before** calling `skill-encryption-spec update`.

### Pre-Flight Verification & Attachment (Terraform, `gcloud`, REST)

**Step 1 — Verify zero existing skills in the target multi-region (`us` or `eu`):**
```bash
export GCP_PROJECT_ID="<your-project-id>"
export SKILL_CMEK_LOCATION="us"   # "us" (KMS: us) or "eu" (KMS: europe)
TOKEN="$(gcloud auth print-access-token --quiet)"

curl -fsSL -H "Authorization: Bearer ${TOKEN}" \
  -H "x-goog-user-project: ${GCP_PROJECT_ID}" \
  "https://agentregistry.googleapis.com/v1/projects/${GCP_PROJECT_ID}/locations/${SKILL_CMEK_LOCATION}/skills?pageSize=1"
# Must return {} (no "skills" array). If skills exist, delete each with ?force=true first.
```

**Step 2 — Attach via Terraform (`terraform/security_governance/skill_encryption_spec.tf`):**
```hcl
# In terraform/security_governance/terraform.tfvars (ONLY on a clean us/eu location)
cmek_location    = "us"   # or "europe" for eu skills
enable_cmek_spec = true
```
`terraform_data.skill_encryption_spec` runs the zero-skills pre-flight check automatically before mutating `SkillEncryptionSpec`.

**Step 3 — Equivalent `gcloud` / REST `v1alpha` commands:**
```bash
export KMS_CRYPTO_KEY_ID="$(terraform -chdir=terraform/security_governance output -raw cmek_crypto_key_id)"

# Preferred CLI (when gcloud alpha agent-registry skill-encryption-spec is installed):
gcloud alpha agent-registry skill-encryption-spec update \
  --project="${GCP_PROJECT_ID}" \
  --location="${SKILL_CMEK_LOCATION}" \
  --kms-key="${KMS_CRYPTO_KEY_ID}" \
  --quiet

# Direct REST fallback:
curl -fsSL -X PATCH \
  -H "Authorization: Bearer $(gcloud auth print-access-token --quiet)" \
  -H "x-goog-user-project: ${GCP_PROJECT_ID}" \
  -H "Content-Type: application/json" \
  -d "{\"cryptoKeyName\": \"${KMS_CRYPTO_KEY_ID}\"}" \
  "https://agentregistry.googleapis.com/v1alpha/projects/${GCP_PROJECT_ID}/locations/${SKILL_CMEK_LOCATION}/skillEncryptionSpec?updateMask=cryptoKeyName"
```

> **Production KMS Lifecycle:** In `terraform/security_governance/kms.tf`, workshop defaults set `lifecycle { prevent_destroy = false }` for easy teardown. Change to `prevent_destroy = true` on both `google_kms_crypto_key.skill_cmek` and `google_kms_crypto_key.skill_artifact_signer` (and optionally set `protection_level = "HSM"` if FIPS 140-2 Level 3 is required) before production apply.

---

## 3. Cloud KMS Asymmetric Signing (`EC_SIGN_P256_SHA256`)

### Canonical Skill Payload & Digest Algorithm

To embed `kms_signature` and `kms_key_version` inside `SKILL.md` `frontmatter.metadata` **without circular hashing** and remain **independent of ZIP timestamps and zlib compression levels**, three components share the exact same canonicalization algorithm byte-for-byte:
- `middleware/app/security_verifier.py` (`compute_canonical_skill_payload`, `compute_canonical_skill_digest`)
- `tools/plugin_sync/sync.py` (`compute_canonical_skill_payload`, `compute_canonical_skill_digest`)
- `security-governance/kms/sign_and_verify.py` (`compute_canonical_skill_payload`, `compute_canonical_skill_digest`)

**Exact byte stream specification:**
1. Header prefix: `b"agent-registry-skill-v1\x00"`
2. Iterate all non-directory entries in the ZIP sorted lexicographically by `info.filename`.
3. If the single `SKILL.md` sits inside a single top-level directory (`<skill-dir>/SKILL.md`), strip `<skill-dir>/` from every entry's `rel_name`.
4. Compute each entry's `payload`:
   - For `SKILL.md`: parse YAML frontmatter + body; remove `kms_signature` and `kms_key_version` from `frontmatter["metadata"]` (drop `metadata` key if empty); serialize frontmatter as compact sorted JSON `json.dumps(cleaned_fm, sort_keys=True, separators=(",", ":"))`; normalize CRLF→LF and strip leading/trailing whitespace on `body`; encode `f"{canonical_fm}\n---\n{normalized_body}\n"` as UTF-8.
   - For all other files (`references/*`, `scripts/*`, assets): use raw uncompressed bytes bit-for-bit.
5. Append framed record: `rel_name_utf8 + b"\x00" + str(len(payload)).encode("ascii") + b"\x00" + payload + b"\x00"`.
6. Compute 32-byte digest: `digest = hashlib.sha256(canonical_payload).digest()`.
7. Sign `digest` with ECDSA P-256 (`SECP256R1`) + SHA-256:
   - **Live Cloud KMS**: `POST https://cloudkms.googleapis.com/v1/{kms_key_version}:asymmetricSign` with `{"digest": {"sha256": "<base64(digest)>"}}` → returns base64 DER signature.
   - **Local PEM**: `private_key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))` → base64 DER signature.
8. Inject `kms_signature` and `kms_key_version` into `SKILL.md` `frontmatter.metadata` and write the deterministic final ZIP.

> **Compatibility fallbacks in `middleware/app/security_verifier.py`:** `verify_ecdsa_p256_signature()` checks the canonical payload first, then falls back to raw ZIP bytes, `sha256Hash` hex string, and `compute_unsigned_deterministic_zip_digest()` (prehashed deterministic ZIP with `date_time=(1980, 1, 1, 0, 0, 0)` and `compresslevel=9`).

---

## 4. Runtime Middleware Verification & Public Key Resolution

`middleware/app/security_verifier.py` (`SecurityVerifier.verify_skill_package()`) enforces four fail-closed gates on every skill before serving `/v1/catalog`, `/v1/plugins/{name}.zip`, or `/mcp` (`load_verified_skill`):

1. **Lifecycle gate**: `skill.state == "STATE_ACTIVE"` (or `"STATE_DEPRECATED"` with warning when `ALLOW_DEPRECATED_SKILLS=true`). `STATE_DRAFT`, `STATE_DISABLED`, and `STATE_DECOMMISSIONED` are rejected.
2. **Scan gate**: `revision.scanResult.scanState in {"SUCCEEDED", "DISABLED"}` AND `len(findings) == 0` AND `defaultRevisionFindingCount == 0`.
3. **Archive integrity**: constant-time `hmac.compare_digest(sha256(zip_bytes), revision.sha256Hash)`.
4. **Cryptographic provenance** (when `REQUIRE_KMS_SIGNATURE=true`, the default in `MiddlewareSettings`):
   - Extracts `frontmatter.metadata.kms_signature` (rejects immediately if missing).
   - Resolves the trusted ECDSA P-256 public key in strict precedence order (`load_kms_public_key()`):
     1. `KMS_PUBLIC_KEY_PEM` (inline PEM string env var)
     2. `KMS_PUBLIC_KEY_PEM_PATH` (file path mounted in container; wired via `var.kms_public_key_pem_path` in `terraform/core/cloud_run.tf`)
     3. `KMS_SIGNING_KEY_VERSION` (fetches `GET https://cloudkms.googleapis.com/v1/{keyVersion}/publicKey` using GCE/Cloud Run metadata server OAuth2 token, validates `algorithm == "EC_SIGN_P256_SHA256"`, and caches the key in `SecurityVerifier._cached_kms_public_key`)
   - Fails closed if none of the three is set or if signature verification fails.

### Wiring Dynamic `KMS_SIGNING_KEY_VERSION` on Cloud Run

`terraform/core/cloud_run.tf` ships with `REQUIRE_KMS_SIGNATURE` and `KMS_PUBLIC_KEY_PEM_PATH`. In production, either mount the public key PEM via Secret Manager / container config (`kms_public_key_pem_path`) OR inject `KMS_SIGNING_KEY_VERSION` directly so the middleware fetches `/publicKey` via `roles/cloudkms.publicKeyViewer`:

```bash
export KMS_SIGNING_KEY_VERSION="$(terraform -chdir=terraform/security_governance output -raw signing_crypto_key_id)/cryptoKeyVersions/1"

gcloud run services update polyglot-middleware \
  --project="${GCP_PROJECT_ID}" --region="${GCP_LOCATION:-asia-southeast3}" \
  --update-env-vars="REQUIRE_KMS_SIGNATURE=true,KMS_SIGNING_KEY_VERSION=${KMS_SIGNING_KEY_VERSION}" --quiet
```

> **Terraform Drift Warning:** An out-of-band `gcloud run services update --update-env-vars=...` change will be overwritten on the next `terraform -chdir=terraform/core apply` unless you also add a `KMS_SIGNING_KEY_VERSION` `env` block (and variable) to `terraform/core/cloud_run.tf`.

---

## 5. CLI Operations (`sign_and_verify.py` & `tools.plugin_sync`)

```bash
# 1. Sign an existing skill ZIP with live Cloud KMS + write detached JSON attestation
.venv/bin/python security-governance/kms/sign_and_verify.py sign \
  --input /tmp/skill-unsigned.zip \
  --output /tmp/skill-signed.zip \
  --kms-key-version "${KMS_SIGNING_KEY_VERSION}" \
  --attestation-out /tmp/skill.attestation.json

# 2. Verify ZIP against BOTH SkillRevision.sha256Hash AND Cloud KMS /publicKey
.venv/bin/python security-governance/kms/sign_and_verify.py verify \
  --input /tmp/skill-signed.zip \
  --kms-key-version "${KMS_SIGNING_KEY_VERSION}" \
  --expected-sha256 "<64-char-hex-from-SkillRevision.sha256Hash>"

# 3. Verify embedded signature locally via tools.plugin_sync
.venv/bin/python -m tools.plugin_sync verify \
  --skill-zip /tmp/skill-signed.zip \
  --public-key-pem /tmp/kms-public.pem
```

---

## 6. Asymmetric Signing Key Rotation Runbook

> **Important implementation detail:** `SecurityVerifier` currently validates signatures against **one** configured public key (`KMS_PUBLIC_KEY_PEM`, `KMS_PUBLIC_KEY_PEM_PATH`, or `KMS_SIGNING_KEY_VERSION`). Plan cutovers carefully so active skills are not temporarily rejected during rotation.

1. **Create a new key version** (`cryptoKeyVersions/2` — inherits `EC_SIGN_P256_SHA256` from the parent CryptoKey's `version_template` in `terraform/security_governance/kms.tf`; `versions create` does not accept `--algorithm`):
   ```bash
   gcloud kms keys versions create \
     --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION:-asia-southeast3}" \
     --keyring="agent-registry-skill-signing" \
     --key="skill-artifact-ecdsa-p256" --quiet
   ```
2. **Stage re-signed revisions**: Run CI (`tools.plugin_sync ingest --only-skills --skills-location "${AGENT_REGISTRY_SKILLS_LOCATION:-global}" --wait-for-scan`) with `--kms-key-version .../cryptoKeyVersions/2` and `--target-state TARGET_STATE_DRAFT` for all active skills; verify every new revision reaches `scanState == SUCCEEDED` with `0` findings (avoiding `400 FAILED_PRECONDITION` pending-scan rejection under `ENABLED_BLOCKING`).
3. **Atomic cutover (small catalog)** — or **dual-key overlap (large fleet)**:
   - *Small catalog*: Promote all v2 revisions (`PATCH ...?updateMask=defaultRevision,targetState`) and immediately update middleware `KMS_SIGNING_KEY_VERSION` (or mounted PEM) to `cryptoKeyVersions/2`.
   - *Zero-downtime large fleet*: Extend `SecurityVerifier` to accept a map of trusted `kms_key_version -> public_key` (v1 + v2) during the rollout window, re-sign and promote all skills to v2, then remove v1 from the allowlist.
4. **Retire old key version**: Confirm `0` active skills report `kmsKeyVersion` ending in `/1` on `GET /v1/catalog`, then disable `cryptoKeyVersions/1` (`gcloud kms keys versions disable 1 ...`) and schedule destruction after your audit window.
