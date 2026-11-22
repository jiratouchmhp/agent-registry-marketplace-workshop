---
name: agent-registry-security-and-prod-hardening
description: >-
  Hardens Google Cloud Agent Registry, Polyglot Middleware, Terraform stacks,
  Cloud KMS (CMEK and asymmetric signing), IAM/Bindings, Agent Gateway IAP v2
  UAP, Org Policies, and WORM Cloud Audit Logs for Workstream B and production
  go-live. Use when running Labs 06-08, configuring terraform/security_governance
  or hardening terraform/core, enforcing ENABLED_BLOCKING scanning and Custom
  Org Policies, setting up CMEK (SkillEncryptionSpec) vs EC_SIGN_P256_SHA256
  signing, managing least-privilege IAM, per-Skill sharing, AuthProvider
  Bindings, IAP v2 UAP CEL rules, Antigravity hooks.json guards, WORM audit
  queries, or pre-production audits. Don't use for workshop navigation
  (agent-marketplace-workshop-guide), 5-IDE plugin authoring
  (polyglot-agent-plugin-authoring), routine v1 ingest/sync
  (agent-registry-v1-ingest-and-lifecycle), or middleware dev, hub_sync, and
  ADK wiring (marketplace-middleware-and-ide-delivery).
---

# Agent Registry Security & Production Hardening

Secure and harden **Google Cloud Agent Registry (`v1`)**, the **Polyglot Marketplace Middleware**, and **runtime MCP/A2A traffic** across **Workstream B (Labs 06–08)**, **active repository development**, and **live GCP production go-live**.

Workstream B (`terraform/security_governance/` + `security-governance/`) runs cleanly either **layered on top of `terraform/core/`** or **standalone** against any existing Agent Registry project.

---

## Quick Decision Tree

```text
What security or production hardening task are you tackling?
├── Automated skill scanning, Finding triage, lifecycle quarantine, or Org Policies (Lab 06)
│   ├── Test ENABLED_BLOCKING, SECRET Findings, atomic rollback, DEPRECATED -> DECOMMISSIONED
│   │   └── Follow docs/labs/06-governance-scanning-lifecycle.md (local :8090 or live GCP)
│   └── Block scanningConfig.policy == "DISABLED" org-wide
│       └── Apply security-governance/org-policy/require-skill-scanning*.yaml
│           or set enable_org_policy = true + org_id in terraform/security_governance/org_policy.tf
├── Cloud KMS encryption at rest (CMEK) vs. cryptographic skill signing (Lab 07)
│   ├── Encryption at rest (SkillEncryptionSpec, us/eu only, zero-skills precondition)
│   └── Supply-chain provenance (EC_SIGN_P256_SHA256 + REQUIRE_KMS_SIGNATURE=true)
│       -> See references/kms-cmek-and-asymmetric-signing.md
├── Least-privilege IAM, per-Skill sharing, AuthProvider Bindings, Agent Gateway + IAP v2 UAP,
│   Antigravity hooks.json policy guard, or WORM Cloud Audit Logs / SIEM queries (Lab 08)
│   └── Apply terraform/security_governance/{auth_bindings,agent_gateway_iap,audit_logging}.tf,
│       security-governance/iap-uap/apply-uap.sh, and security-governance/audit/compliance-queries.md
│       -> See references/iap-uap-bindings-and-audit-logging.md
├── Auditing a deployment for Production Go-Live (tfvars, Cloud Run, WIF OIDC, rollback runbook)
│   └── Run every gate in references/production-go-live-checklist.md
└── Outside Security & Prod Hardening?
    ├── Workshop setup, mode routing, or starter TODOs -> agent-marketplace-workshop-guide
    ├── 5-IDE plugin / SKILL.md manifest authoring     -> polyglot-agent-plugin-authoring
    ├── Skill/Service ingestion & GitLab sync setup    -> agent-registry-v1-ingest-and-lifecycle
    └── Middleware endpoints, hub_sync, or ADK wiring  -> marketplace-middleware-and-ide-delivery
```

---

## Critical Gotchas ("Do This, Not That")

| # | Do This | Not That | Why |
|---|---|---|---|
| **1** | Set `allow_unauthenticated_workshop_demo = false`, `deletion_protection = true`, and `require_kms_signature = true` in production `terraform/core/terraform.tfvars` (with pinned `@sha256:...` images). | Enabling `allow_unauthenticated_workshop_demo = true` or leaving workshop defaults (`deletion_protection = false`, `require_kms_signature = false`) in production. | `allow_unauthenticated_workshop_demo` defaults to `false` (secure by default), but flipping it `true` binds `allUsers` to `roles/run.invoker` on all three Cloud Run services; `require_kms_signature = false` lets unsigned rogue uploads bypass CI provenance checks. |
| **2** | Grant **strictly `roles/agentregistry.viewer`** to the Polyglot Middleware SA (`agent-marketplace-middleware`) and runtime ADK agents (plus `roles/cloudkms.publicKeyViewer` only on the signing key). | Granting `roles/agentregistry.editor`, `roles/agentregistry.admin`, or `roles/cloudkms.signerVerifier` to runtime agents or serving middleware. | An `editor`/`admin` runtime identity can tamper with registered MCP `readOnlyHint`/`destructiveHint` annotations, enroll rogue MCP servers, or forge skill signatures if prompt-injected or compromised. |
| **3** | Treat **CMEK (`SkillEncryptionSpec`)** (symmetric `ENCRYPT_DECRYPT` at rest, strictly `us`→`us` or `eu`→`europe` KMS, **zero-skills precondition**, P4SA needs `roles/cloudkms.cryptoKeyEncrypterDecrypter`) separately from **Asymmetric Signing (`EC_SIGN_P256_SHA256`)** (CI signs canonical digest; middleware verifies via `KMS_SIGNING_KEY_VERSION` or `KMS_PUBLIC_KEY_PEM_PATH`). | Assuming CMEK signs skill artifacts, creating an EU CMEK key in `eu` instead of `europe`, or enabling `enable_cmek_spec = true` on a location with existing skills. | Agent Registry does **not** natively sign skills; CMEK only encrypts at rest. `SkillEncryptionSpec` rejects `eu` KMS location names (must be `europe`) and returns `FAILED_PRECONDITION` if even one skill exists in the target multi-region. |
| **4** | Always include `auth_provider_binding { auth_provider, scopes, continue_uri }` on `google_agent_registry_binding`, and grant calling agents `roles/agentidentity.user` on the referenced `AuthProvider`. | Omitting `auth_provider_binding` in Terraform or forgetting `roles/agentidentity.user` when debugging empty `bindings:fetchAvailable` responses. | `auth_provider_binding` is **Required** in `hashicorp/google >= 7.39.0`. At runtime, `GET .../bindings:fetchAvailable` silently filters out any binding whose `AuthProvider` the caller lacks `roles/agentidentity.user` on. |
| **5** | Use **Resource Names** (`projects/...`, `//networkservices.googleapis.com/...`) for IAM/IAP/Gateway targets, **`principal://` / `principalSet://`** for agent callers, and enable IAP UAP enforcement by **removing** `iamEnforcementMode`. | Putting URNs (`urn:...`) or raw `spiffe://` URIs in IAM/IAP/Gateway policies, or setting `iamEnforcementMode = "ENFORCED"`. | URNs are valid only for discovery and `Binding` source/target; `spiffe://` returns `INVALID_ARGUMENT`; the IAP API validator rejects any explicit `iamEnforcementMode` value other than `"DRY_RUN"`. |
| **6** | Explicitly enable `ADMIN_READ` Data Access logs on **both** `agentregistry.googleapis.com` **and** `apphubendpoint.googleapis.com`, and set `lock_audit_bucket = true` in production (`false` only in ephemeral sandboxes for clean teardown). | Relying on Admin Activity logs alone, enabling `ADMIN_READ` only on `agentregistry.googleapis.com`, or locking a sandbox bucket before teardown. | Data Access (`ADMIN_READ`) is disabled by default; without both services you lose forensic visibility into skill downloads, discovery queries, and App Hub resolution. `lock_audit_bucket = true` (`locked = true`) is **irreversible** once applied. |
| **7** | Follow the verified live APAC regional split: regional `Services`/`Bindings`/Audit Bucket & KMS Asymmetric Signing (`EC_SIGN_P256_SHA256`) in **Bangkok (`asia-southeast3`)** (and optional Singapore `asia-southeast1`), standalone `Skills` & per-`Skill` IAM in **`global`** (or `us`/`eu` for CMEK), `auth_provider_location = "asia-southeast1"`, `enable_agent_gateway = false` (currently GA in `us-central1`/`europe-west1`), and inject secrets via `TF_VAR_auth_provider_client_secret` / `--git-token-env` / keyless GitLab OIDC WIF. | Putting live standalone `Skills` or `auth_provider_location` in `asia-southeast3`, enabling Agent Gateway in unsupported APAC regions, or committing OAuth client secrets, Git tokens, or static GCP JSON keys into `.tfvars`. | Live Agent Registry hosts standalone `Skills` only in `global`/`us`/`eu`; `agentidentity.googleapis.com` is GA in Singapore (`asia-southeast1`), not Bangkok (`asia-southeast3`); Network Services Agent Gateway is GA in `us-central1`/`europe-west1`; keyless WIF eliminates static credential rotation risk. |

---

## Quick-Start Patterns

### 1. Provision Standalone `terraform/security_governance` (Live GCP Mode)

```bash
cp terraform/security_governance/terraform.tfvars.example \
   terraform/security_governance/terraform.tfvars
# APAC defaults: location = "asia-southeast3" (Bangkok), auth_provider_location = "asia-southeast1" (Singapore),
# enable_agent_gateway = false (GA in us-central1/europe-west1), lock_audit_bucket = false (set true in prod!).
# Populate project_id, middleware_sa_email, ci_publisher_sa_email, mcp_server_resource_id/urn, a2a_agent_resource_id/urn.
# Keep enable_cmek_spec = false unless the target `us` or `eu` location has ZERO skills.

export TF_VAR_auth_provider_client_secret="<oauth-client-secret-from-secret-manager>"
make tf-sec-plan
terraform -chdir=terraform/security_governance apply
```

### 2. Sign & Verify Skills (Local Workshop Mode vs. Live GCP Cloud KMS Mode)

**Local Workshop Mode (Offline ECDSA P-256 PEM — identical cryptography to Cloud KMS):**
```bash
.venv/bin/python security-governance/kms/sign_and_verify.py keygen \
  --private-out /tmp/kms-demo/private.pem --public-out /tmp/kms-demo/public.pem

# Package, sign canonically, and sync to mock registry (:8090)
SIGNING_KEY_PEM_PATH=/tmp/kms-demo/private.pem make demo-sync

# Start Middleware with fail-closed signature verification
REQUIRE_KMS_SIGNATURE=true KMS_PUBLIC_KEY_PEM_PATH=/tmp/kms-demo/public.pem make run-middleware
```

**Live GCP Production Mode (Cloud KMS `:asymmetricSign` & `/publicKey`):**
```bash
export KMS_SIGNING_KEY_VERSION="$(terraform -chdir=terraform/security_governance output -raw signing_crypto_key_id)/cryptoKeyVersions/1"

# 1. Dry-run first (packages & signs locally without mutating live Agent Registry)
#    Use `ingest --only-skills --skills-location global` when terraform/core/agent_registry_services.tf owns Services
#    (`sync` does not expose `--only-skills`; live standalone Skills live in global, or us/eu for CMEK).
.venv/bin/python -m tools.plugin_sync ingest \
  --source-type local --path starter-marketplace/plugins/gcp-finops-toolkit \
  --registry-url "https://agentregistry.googleapis.com/v1" \
  --project "${GCP_PROJECT_ID}" --location "${GCP_LOCATION:-asia-southeast3}" \
  --skills-location "${AGENT_REGISTRY_SKILLS_LOCATION:-global}" \
  --target-state TARGET_STATE_DRAFT --scanning-policy ENABLED_BLOCKING \
  --kms-key-version "${KMS_SIGNING_KEY_VERSION}" --wait-for-scan --only-skills --skip-mcp-probe --dry-run

# 2. Remove `--dry-run` to upload signed DRAFT skills (or use `sync` when CI owns Services too)
# 3. Export public PEM (or configure KMS_SIGNING_KEY_VERSION directly on Cloud Run middleware):
.venv/bin/python security-governance/kms/sign_and_verify.py fetch-public-key \
  --kms-key-version "${KMS_SIGNING_KEY_VERSION}" --output /tmp/kms-public.pem
```

### 3. Validate & Apply Agent Gateway IAP v2 UAP (`--dry-run` → `--validate-only` → Enforce)

```bash
# Local Workshop / Pre-flight dry-run (validates JSON & prints exact gcloud commands)
bash security-governance/iap-uap/apply-uap.sh \
  --project "${GCP_PROJECT_ID:-acme-agent-governance}" \
  --location "${GCP_LOCATION:-us-central1}" \
  --gateway-name agent-marketplace-gateway --dry-run

# Live GCP: validate server-side first, then apply (start IAP metadata in DRY_RUN before enforcing!)
bash security-governance/iap-uap/apply-uap.sh \
  --project "${GCP_PROJECT_ID}" --location "${GCP_LOCATION}" \
  --gateway-name agent-marketplace-gateway \
  --principal "principal://agents.global.proj-${GCP_PROJECT_NUMBER}.system.id.goog/resources/aiplatform/projects/${GCP_PROJECT_ID}/locations/${GCP_LOCATION}/reasoningEngines/finops-advisor-agent" \
  --validate-only
```

### 4. Grant Per-`Skill` Resource-Level IAM (Live GCP Mode)

```bash
# Always use the auto-prefixed `private-<skill-id>` name AND live Skills location (`global`, or `us`/`eu` for CMEK)!
gcloud agent-registry skills add-iam-policy-binding private-cost-anomaly-triage \
  --project="${GCP_PROJECT_ID}" --location="${AGENT_REGISTRY_SKILLS_LOCATION:-global}" \
  --member="serviceAccount:agent-marketplace-middleware@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/agentregistry.viewer" --quiet
```

### 5. Pre-Production `.tfvars` & Org Policy Audit

```bash
# Audit existing production .tfvars files (ignores # and // comments; exits non-zero on any match)
bash -euo pipefail -c '
  pattern="(allow_unauthenticated_workshop_demo[[:space:]]*=[[:space:]]*true|deletion_protection[[:space:]]*=[[:space:]]*false|require_kms_signature[[:space:]]*=[[:space:]]*false|lock_audit_bucket[[:space:]]*=[[:space:]]*false|auth_provider_client_secret[[:space:]]*=|glpat-|ghp_|AKIA|PRIVATE KEY)"
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

# Verify effective Org Policy enforcement on a live GCP project
gcloud org-policies describe custom.requireAgentRegistrySkillScanning \
  --project="${GCP_PROJECT_ID}" --effective --quiet
```

---

## Verification Checklist

Run these checks before merging security changes or promoting to production:

```bash
# 1. Full Workstream B security test suite (Terraform invariants, KMS signing/tamper, UAP, hooks, audit)
.venv/bin/pytest -v tests/test_workstream_b_security.py

# 2. Terraform HCL formatting across both core and security_governance stacks
make validate-hcl

# 3. IAP UAP rules validation (dry-run, zero GCP calls)
bash security-governance/iap-uap/apply-uap.sh --dry-run

# 4. Antigravity PreToolUse policy guard blocks destructive MCP tool (expect {"decision": "deny", ...})
echo '{"tool_call": {"name": "mcp_finops-mcp-server_delete_idle_dataset", "annotations": {"destructiveHint": true}}}' \
  | .venv/bin/python security-governance/hooks/scripts/policy_guard.py --event PreToolUse
```

Expected pass criteria:
- `pytest` exits `0` with all Workstream B tests passing.
- `make validate-hcl` exits `0` with zero formatting diffs.
- `apply-uap.sh --dry-run` validates both `ALLOW` (`read_only_hint`) and `DENY` (`destructive_hint`) rules targeting `iap.googleapis.com/resources.egressViaIAP`.
- `policy_guard.py` returns `"decision": "deny"` for destructive tools and `{}` for `readOnlyHint: true` tools.

---

## Reference Guides

Load **only** the specific reference guide needed for your current task:

| Reference File | Deep-Dive Scope |
|---|---|
| [references/kms-cmek-and-asymmetric-signing.md](references/kms-cmek-and-asymmetric-signing.md) | CMEK (`us`/`eu`→`europe`, zero-skills guard, P4SA grant, `SkillEncryptionSpec` REST/gcloud/Terraform) vs. Asymmetric Signing (`EC_SIGN_P256_SHA256`, canonical skill digest algorithm, `KMS_SIGNING_KEY_VERSION` runtime lookup, key rotation) |
| [references/iap-uap-bindings-and-audit-logging.md](references/iap-uap-bindings-and-audit-logging.md) | 4-layer identifier matrix, 4 IAM roles persona table, `AuthProvider` + `Binding` + `FetchAvailableBindings`, Agent Gateway + IAP v2 UAP CEL (`DRY_RUN` → Enforce), Antigravity `hooks.json` guard, and verified Cloud Logging / BigQuery compliance queries |
| [references/production-go-live-checklist.md](references/production-go-live-checklist.md) | Actionable 8-domain Pre-Production Readiness Gate (Cloud Run/Terraform, IAM, Org Policy/Scanning, KMS, Gateway/UAP, Keyless GitLab OIDC/WIF, WORM Audit/SIEM, and Incident Rollback Runbook) |
