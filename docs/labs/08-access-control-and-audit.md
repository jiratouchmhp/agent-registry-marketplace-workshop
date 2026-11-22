# Lab 08: Access Control, Agent Gateway IAP v2 UAP & Immutable Audit Logging

**Workstream:** B — Security & Governance *(Standalone Runnable — No Workstream A Terraform State Required)*  
**Duration:** 40 minutes  
**Prerequisites:** Python `.venv` verified (`make test`).

> **Running Workstream B Standalone**  
> This lab requires **zero Workstream A state** (`terraform/core/` does not need to be deployed):  
> - **Local Mock Mode (Offline):** Start `make run-mock` (`:8090`) to exercise `bindings:fetchAvailable` IAM filtering, validate IAP v2 UAP rules with `apply-uap.sh --dry-run`, and test the client-side `policy_guard.py` hook directly.  
> - **Standalone Live GCP Mode:** Apply [terraform/security_governance/](../../terraform/security_governance/agent_gateway_iap.tf) independently (`make tf-sec-plan`) against any existing Google Cloud Agent Registry project—just populate `mcp_server_resource_id`, `mcp_server_urn`, `a2a_agent_resource_id`, and `a2a_agent_urn` in `terraform.tfvars` without touching `terraform/core/`.

---

## Learning Objectives

By the end of this lab, you will:
1. Apply the **4 Agent Registry IAM roles** (`viewer`, `user`, `editor`, `admin`) and **per-`Skill` resource-level IAM**—enforcing the golden rule: *never grant `editor` or `admin` to runtime agents or serving middleware*.
2. Configure **Agent Identity `AuthProvider` + Agent Registry `Binding`** and demonstrate how `GET .../bindings:fetchAvailable` actively filters out bindings whose `AuthProvider` the caller lacks `roles/agentidentity.user` on.
3. Enforce **tool-level runtime governance** using **Network Services Agent Gateway + IAP v2 IAM Unified Access Policies (UAP)** with CEL expressions (`read_only_hint == true` ALLOW, `destructive_hint == true` DENY).
4. Test the optional client-side **Antigravity `hooks.json` Policy Guard** ([security-governance/hooks/scripts/policy_guard.py](../../security-governance/hooks/scripts/policy_guard.py)) blocking destructive MCP tools and dangerous shell commands.
5. Configure **immutable WORM Cloud Audit Logging** (`agentregistry.googleapis.com` + `apphubendpoint.googleapis.com` `ADMIN_READ` Data Access + Agent Gateway request logs) and review the compliance queries in [security-governance/audit/compliance-queries.md](../../security-governance/audit/compliance-queries.md).

---

## Step 1: Least-Privilege IAM & Per-`Skill` Resource IAM

### The 4 Predefined Agent Registry IAM Roles

| Role | Capabilities | Approved Principals |
| :--- | :--- | :--- |
| `roles/agentregistry.viewer` | Read-only discovery (`list`/`get`/`search`) & active skill download (`?alt=media`) | **Polyglot Middleware SA**, **Platform Agents (ADK)**, developers |
| `roles/agentregistry.user` | `viewer` + standalone `Skill` CRUD only | Skill authors (`skill-author` SA) |
| `roles/agentregistry.editor` | `viewer` + `Service` & `Skill` CRUD | **CI/CD Publisher SA only** (never runtime agents!) |
| `roles/agentregistry.admin` | Full control + `Binding`, `Finding`, per-`Skill` IAM, `SkillEncryptionSpec` | Platform Security Admins only (never runtime agents!) |

### Live GCP Mode — Grant Per-`Skill` Resource-Level IAM

Restrict a sensitive private skill (e.g. `private-cost-anomaly-triage` in `global` — or `us`/`eu` if using CMEK) to a specific team or agent service account:

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"
export AGENT_REGISTRY_SKILLS_LOCATION="global"

gcloud agent-registry skills add-iam-policy-binding private-cost-anomaly-triage \
  --project="${GCP_PROJECT_ID}" \
  --location="${AGENT_REGISTRY_SKILLS_LOCATION}" \
  --member="serviceAccount:agent-marketplace-middleware@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/agentregistry.viewer" \
  --quiet
```

---

## Step 2: `AuthProvider` + `Binding` + `FetchAvailableBindings` IAM Filtering

In [terraform/security_governance/auth_bindings.tf](../../terraform/security_governance/auth_bindings.tf), `google_agent_identity_auth_provider` + `google_agent_registry_binding` bind the FinOps A2A Agent (`source.identifier`) to the FinOps MCP Server (`target.identifier`) with scoped OAuth (`cloud-billing.readonly`, `bigquery.readonly`) and explicitly blocked directory admin scopes.

> **Workstream B APAC Regional Split & Toggles (`terraform/security_governance/terraform.tfvars`):**
> - **`location = "asia-southeast3"`** (Bangkok primary for regional Agent Registry `Services`/`Bindings`, IAP egressor IAM, Cloud KMS `EC_SIGN_P256_SHA256` asymmetric signing, and regional Cloud Logging bucket; optional secondary/DR compute & `Services` in Singapore `asia-southeast1`).
> - **Standalone `Skills` location (`global`, or `us`/`eu` for CMEK):** Skills and per-`Skill` IAM live in **`global`** for APAC (`AGENT_REGISTRY_SKILLS_LOCATION=global`, `skills_location = "global"` in `terraform/core/`), shared across Bangkok and Singapore.
> - **`auth_provider_location = "asia-southeast1"`** (Singapore — verified APAC GA region for `agentidentity.googleapis.com`; Bangkok `asia-southeast3` does not host Agent Identity yet).
> - **`enable_agent_gateway = false`** (Network Services Agent Gateway on Cloud Run is currently GA only in `us-central1` and `europe-west1`; set `enable_agent_gateway = true` and `agent_gateway_location = "us-central1"` only when evaluating live Agent Gateway in US/EU).
> - **`lock_audit_bucket = false`** in ephemeral workshop sandboxes so `terraform destroy` works cleanly (**set `lock_audit_bucket = true` in production** for irreversible WORM log retention!).

At runtime, calling `GET .../bindings:fetchAvailable` **actively filters out** any binding whose `AuthProvider` the caller lacks `roles/agentidentity.user` on.

### Mode A: Local Mock Mode — Demonstrate IAM-Filtered `bindings:fetchAvailable`

Ensure `make run-mock` is running on `:8090`, then create a Binding and test authorized vs. unauthorized callers using `X-Mock-Allowed-Auth-Providers`:

```bash
BASE="http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/asia-southeast3"
AGENT_URN="urn:agent:projects-123456789012:projects:123456789012:locations:asia-southeast3:agentregistry:services:finops-advisor-agent"
MCP_URN="urn:mcp:projects-123456789012:projects:123456789012:locations:asia-southeast3:agentregistry:services:finops-mcp-server"
AUTH_PROV="projects/acme-agent-governance/locations/asia-southeast1/authProviders/finops-mcp-oauth"

# 1. Create the Agent Registry Binding
curl -fsSL -X POST "${BASE}/bindings?bindingId=finops-agent-to-mcp-binding" \
  -H "Content-Type: application/json" \
  -d "{
    \"displayName\": \"FinOps Advisor Agent -> FinOps MCP Server\",
    \"source\": {\"identifier\": \"${AGENT_URN}\"},
    \"target\": {\"identifier\": \"${MCP_URN}\"},
    \"authProviderBinding\": {
      \"authProvider\": \"${AUTH_PROV}\",
      \"scopes\": [
        \"https://www.googleapis.com/auth/cloud-billing.readonly\",
        \"https://www.googleapis.com/auth/bigquery.readonly\"
      ],
      \"continueUri\": \"https://console.cloud.google.com/agent-registry/auth/callback\"
    }
  }" | .venv/bin/python -m json.tool

# 2. Authorized caller (holds roles/agentidentity.user on finops-mcp-oauth) -> returns 1 binding!
curl -fsSL \
  -H "X-Mock-Allowed-Auth-Providers: finops-mcp-oauth" \
  "${BASE}/bindings:fetchAvailable?sourceIdentifier=${AGENT_URN}&targetIdentifier=${MCP_URN}" \
  | .venv/bin/python -m json.tool

# 3. Unauthorized caller (lacks roles/agentidentity.user on finops-mcp-oauth) -> returns bindings: []!
curl -fsSL \
  -H "X-Mock-Allowed-Auth-Providers: other-auth-provider" \
  "${BASE}/bindings:fetchAvailable?sourceIdentifier=${AGENT_URN}&targetIdentifier=${MCP_URN}" \
  | .venv/bin/python -m json.tool
```

### Mode B: Live GCP Mode (`terraform/security_governance`)

Apply [terraform/security_governance/auth_bindings.tf](../../terraform/security_governance/auth_bindings.tf) and [agent_gateway_iap.tf](../../terraform/security_governance/agent_gateway_iap.tf):

```bash
export TF_VAR_auth_provider_client_secret="<your-oauth-client-secret>"
make tf-sec-plan
terraform -chdir=terraform/security_governance apply
```

---

## Step 3: Agent Gateway + IAP v2 UAP Tool-Annotation Enforcement (CEL)

Inspect [security-governance/iap-uap/mcp-tool-annotations-uap.json](../../security-governance/iap-uap/mcp-tool-annotations-uap.json):

- **ALLOW Rule** (`iap.googleapis.com/resources.egressViaIAP`):
  ```cel
  destination.is_registered == true &&
  destination.agent_registry.resource_type == 'MCP_SERVER' &&
  destination.agent_registry.mcp_server.tool.annotations.read_only_hint == true
  ```
  → Permits `get_billing_summary` and `analyze_bq_query`.
- **DENY Rule** (`iap.googleapis.com/resources.egressViaIAP`):
  ```cel
  destination.agent_registry.mcp_server.tool.annotations.destructive_hint == true
  ```
  → Explicitly blocks `delete_idle_dataset` at the Agent Gateway network boundary!

### Mode A: Validate Rules & Preview Commands (`--dry-run`, Zero GCP Needed)

*(Note: Network Services Agent Gateway is currently GA in `us-central1` and `europe-west1`; keep `enable_agent_gateway = false` in APAC-only Terraform deployments and use `us-central1` or `europe-west1` when evaluating live Agent Gateway + IAP v2 UAP.)*

```bash
bash security-governance/iap-uap/apply-uap.sh \
  --project acme-agent-governance \
  --location us-central1 \
  --gateway-name agent-marketplace-gateway \
  --dry-run
```

### Mode B: Apply IAM v3 AccessPolicy & PolicyBinding in Live GCP

```bash
bash security-governance/iap-uap/apply-uap.sh \
  --project "<your-gcp-project-id>" \
  --location "us-central1" \
  --gateway-name "agent-marketplace-gateway" \
  --principal "principal://agents.global.proj-<PROJECT_NUMBER>.system.id.goog/resources/aiplatform/projects/<PROJECT_ID>/locations/us-central1/reasoningEngines/finops-advisor-agent"
```

---

## Step 4: Test Client-Side Antigravity `hooks.json` Policy Guard (Defense-in-Depth)

Inspect [security-governance/hooks/hooks.json](../../security-governance/hooks/hooks.json) (uses named group `"policy-guard"` and relative `./scripts/policy_guard.py` path with mode `0755`). Test both `PreToolUse` and `PostToolUse` locally:

```bash
# 1. Read-only MCP tool -> ALLOWED ({})
echo '{"tool_call": {"name": "mcp_finops-mcp-server_get_billing_summary", "annotations": {"readOnlyHint": true, "destructiveHint": false}}}' \
  | .venv/bin/python security-governance/hooks/scripts/policy_guard.py --event PreToolUse

# 2. Destructive MCP tool -> DENIED ({"decision": "deny", "reason": ...})
echo '{"tool_call": {"name": "mcp_finops-mcp-server_delete_idle_dataset", "annotations": {"readOnlyHint": false, "destructiveHint": true}}}' \
  | .venv/bin/python security-governance/hooks/scripts/policy_guard.py --event PreToolUse

# 3. Dangerous shell command -> DENIED
echo '{"tool_call": {"name": "run_command", "args": {"CommandLine": "terraform destroy -auto-approve"}}}' \
  | .venv/bin/python security-governance/hooks/scripts/policy_guard.py --event PreToolUse

# 4. PostToolUse redacted audit log (arg values never logged, only arg_keys!)
echo '{"conversation_id": "conv-demo-1", "agent_name": "finops-advisor", "tool_call": {"name": "get_billing_summary", "args": {"project_id": "acme-agent-governance", "token": "SECRET"}}, "result": "ok"}' \
  | POLICY_GUARD_AUDIT_LOG=/tmp/policy-guard-demo.jsonl \
    .venv/bin/python security-governance/hooks/scripts/policy_guard.py --event PostToolUse

cat /tmp/policy-guard-demo.jsonl
```

Verify `/tmp/policy-guard-demo.jsonl` records `"arg_keys": ["project_id", "token"]` without ever writing the secret value `"SECRET"`.

---

## Step 5: Immutable WORM Audit Logging & Compliance Queries

Inspect [terraform/security_governance/audit_logging.tf](../../terraform/security_governance/audit_logging.tf):
1. Enables `ADMIN_READ` Data Access audit logs on **both** `agentregistry.googleapis.com` **and** `apphubendpoint.googleapis.com` (Admin Activity is always on).
2. Creates a regional Cloud Logging bucket (`agent-governance-audit` in `asia-southeast3`, `enable_analytics = true`) controlled by `var.lock_audit_bucket` (**`false` in ephemeral workshop sandboxes** so `terraform destroy` cleans up; **`true` in production** for irreversible WORM retention) with a dedicated sink routing:
   - `cloudaudit.googleapis.com/activity` (`agentregistry`, `apphubendpoint`, `agentidentity`, `iap`, `networkservices`, `cloudkms`)
   - `cloudaudit.googleapis.com/data_access` (`agentregistry`, `apphubendpoint`, `iap`, `cloudkms`)
   - `networkservices.googleapis.com/gateway_requests`
3. Open [security-governance/audit/compliance-queries.md](../../security-governance/audit/compliance-queries.md) for production-ready **Cloud Logging** filters and **BigQuery Log Analytics** SQL queries covering:
   - Scanning policy downgrade alerts (`scanningConfig.policy = "DISABLED"`)
   - Denied IAP `egressViaIAP` calls & shadow unregistered egress attempts (`unregisteredResource`)
   - IAP UAP `DRY_RUN` readiness breakdown before flipping to enforced mode
   - Cloud KMS `AsymmetricSign` / `GetPublicKey` audit trail

> **Irreversibility Warning:** Once `lock_audit_bucket = true` (`locked = true`) is applied on `google_logging_project_bucket_config`, the bucket's retention policy cannot be shortened and the bucket cannot be deleted until all log entries expire. Keep `lock_audit_bucket = false` in workshop sandboxes and always flip `lock_audit_bucket = true` in production!

---

## Step 6: Clean Teardown (Live GCP Workshop Cleanup)

Always tear down ephemeral workshop resources once you finish so no billable Cloud Run services, Artifact Registry images, or Agent Registry test skills remain running:

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"

# 1. Destroy Workstream B Terraform stack (clean when lock_audit_bucket = false)
#    Note: Cloud KMS CryptoKeyVersions schedule for destruction rather than instant deletion.
terraform -chdir=terraform/security_governance destroy

# 2. Destroy Workstream A resources (choose the Track you used in Lab 03):
# Track 2 (Zero-Terraform gcloud + Cloud Build):
make gcp-destroy GCP_PROJECT_ID="${GCP_PROJECT_ID}"
# Or with Singapore secondary cleanup:
# scripts/destroy_gcp.sh --project "${GCP_PROJECT_ID}" --primary-region asia-southeast3 --enable-secondary

# Track 3 (Terraform Core — set force_destroy_staging_bucket = true in sandbox .tfvars if objects exist):
# terraform -chdir=terraform/core destroy

# 3. Verify zero workshop Cloud Run services remain in Bangkok and Singapore
gcloud run services list --project="${GCP_PROJECT_ID}" --region="asia-southeast3" --limit=50 --quiet
gcloud run services list --project="${GCP_PROJECT_ID}" --region="asia-southeast1" --limit=50 --quiet
```

> **Tip for Deleting Leftover Skills with Revisions:** Because `gcloud agent-registry skills delete` does **not** accept `--force`, delete any remaining test skill with revisions via REST `DELETE "https://agentregistry.googleapis.com/v1/projects/${GCP_PROJECT_ID}/locations/${AGENT_REGISTRY_SKILLS_LOCATION:-global}/skills/private-<id>?force=true"` with `Authorization: Bearer $(gcloud auth print-access-token --quiet)` and `x-goog-user-project: ${GCP_PROJECT_ID}` headers (handled automatically by `scripts/destroy_gcp.sh`).

---

## Verification Checkpoint

- [ ] Proved `bindings:fetchAvailable` returns the binding for an authorized `AuthProvider` caller and returns `[]` for an unauthorized caller.
- [ ] Validated `mcp-tool-annotations-uap.json` via `apply-uap.sh --dry-run`.
- [ ] Confirmed `policy_guard.py` allows read-only MCP calls, denies `destructiveHint: true` and `terraform destroy`, and writes redacted `PostToolUse` JSONL entries.
- [ ] Reviewed [security-governance/audit/compliance-queries.md](../../security-governance/audit/compliance-queries.md) and ran the full Workstream B suite:
  ```bash
  .venv/bin/pytest -v tests/test_workstream_b_security.py
  ```

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `bindings:fetchAvailable` returns all bindings when testing locally | `X-Mock-Allowed-Auth-Providers` header omitted | Pass `-H "X-Mock-Allowed-Auth-Providers: <provider-id>"` to simulate IAM filtering in `mock_registry`. |
| `gcloud beta iam access-policies create` fails with invalid principal format | Used a short email instead of IAM v3 Workload / Agent Identity principal URI | Format principals per IAP v2 UAP syntax (e.g. `principal://agents.global.proj-<NUM>.system.id.goog/...` or `principal://iam.googleapis.com/...`). |
| Missing `apphubendpoint.googleapis.com` discovery audit records | Only enabled `ADMIN_READ` on `agentregistry.googleapis.com` | Enable `ADMIN_READ` Data Access on **both** `agentregistry.googleapis.com` and `apphubendpoint.googleapis.com` (configured in `audit_logging.tf`). |
| `gcloud agent-registry skills add-iam-policy-binding` returns `404` in `asia-southeast3` | Standalone Skills are hosted in `global` (or `us`/`eu` for CMEK) on live Agent Registry `v1` | Pass `--location=global` (matching `AGENT_REGISTRY_SKILLS_LOCATION`) for per-`Skill` IAM while keeping `Bindings` and IAP egressor IAM in `asia-southeast3` and `AuthProvider` in `asia-southeast1`. |

---

**Congratulations!** You have completed both **Workstream A** and **Workstream B** of the Enterprise Agent Marketplace Workshop. Return to the [Workshop Home](../../README.md) or review the [Customer Use Case 3 Executive Brief](../customer-usecase-3-brief.md).
