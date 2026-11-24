# Lab 08: Access Control, Agent Gateway IAP & Audit Logging

**Workstream:** B (Security & Governance — Standalone, no Workstream A Terraform state required)  
**Duration:** 40 minutes  
**Prerequisites:** Python `.venv` verified (`make test`).

> **Running Workstream B Standalone**  
> This lab needs zero Workstream A state (`terraform/core/` does not need to be deployed):  
> - **Local Mock Mode (Offline):** Start `make run-mock` (`:8090`) to test `bindings:fetchAvailable` IAM filtering, dry-run IAP v2 UAP rules with `apply-uap.sh --dry-run`, and run the client-side `policy_guard.py` hook directly.  
> - **Live GCP Mode (Optional):** Apply [`terraform/security_governance/agent_gateway_iap.tf`](../../terraform/security_governance/agent_gateway_iap.tf) independently (`make tf-sec-plan`) by copying [`terraform/security_governance/terraform.tfvars.example`](../../terraform/security_governance/terraform.tfvars.example) to `terraform.tfvars` and filling in `project_id`, `middleware_sa_email`, `mcp_server_resource_id`, `mcp_server_urn`, `a2a_agent_resource_id`, and `a2a_agent_urn`.

## Files You'll Work With

No `starter/` coding stub in this lab — you'll run and inspect the repo files below directly.

| Role | File | What You'll Do |
| :--- | :--- | :--- |
| **Inspect** | [`security-governance/iap-uap/mcp-tool-annotations-uap.json`](../../security-governance/iap-uap/mcp-tool-annotations-uap.json) | Read the IAP v2 UAP CEL rules allowing read-only tools and blocking destructive ones. |
| **Run** | [`security-governance/iap-uap/apply-uap.sh`](../../security-governance/iap-uap/apply-uap.sh) | Preview (`--dry-run`) or apply IAM v3 AccessPolicy and PolicyBinding commands. |
| **Inspect** | [`security-governance/hooks/hooks.json`](../../security-governance/hooks/hooks.json) | Check how Antigravity wires the client-side `policy-guard` hook. |
| **Run / Inspect** | [`security-governance/hooks/scripts/policy_guard.py`](../../security-governance/hooks/scripts/policy_guard.py) | Test `PreToolUse` blocking and redacted `PostToolUse` JSONL logging locally. |
| **Inspect** | [`security-governance/audit/compliance-queries.md`](../../security-governance/audit/compliance-queries.md) | Review Cloud Logging filters and BigQuery Log Analytics SQL queries. |
| **Inspect (Live GCP)** | [`auth_bindings.tf`](../../terraform/security_governance/auth_bindings.tf), [`agent_gateway_iap.tf`](../../terraform/security_governance/agent_gateway_iap.tf), [`audit_logging.tf`](../../terraform/security_governance/audit_logging.tf) | Terraform definitions under `terraform/security_governance/` for `AuthProvider`, `Binding`, IAP UAP, and audit logging. |
| **Configure (Live GCP)** | [`terraform/security_governance/terraform.tfvars.example`](../../terraform/security_governance/terraform.tfvars.example) | Starting template for Workstream B Terraform variables (`cp` to `terraform.tfvars` if running standalone). |
| **Run (Live GCP)** | [`scripts/destroy_gcp.sh`](../../scripts/destroy_gcp.sh) | Cleanly remove Workstream A GCP resources and audit zero leftovers (`make gcp-destroy`). |

## Learning Objectives

By the end of this lab, you will:
1. Map the 4 Agent Registry IAM roles and keep runtime agents limited to read-only access.
2. Prove that `bindings:fetchAvailable` hides bindings when a caller lacks `roles/agentidentity.user` on the `AuthProvider`.
3. Preview IAP v2 UAP CEL rules with `apply-uap.sh --dry-run` and test the client-side `policy_guard.py` hook locally.
4. Review Cloud Audit Logging (`ADMIN_READ` and WORM bucket settings), compliance queries, and clean GCP teardown.

---

## Step 1: Review the 4 Agent Registry IAM Roles

Never grant `editor` or `admin` to runtime agents or the Polyglot Middleware. Split permissions across these four predefined roles:

| Role | What It Allows | Approved Principals |
| :--- | :--- | :--- |
| `roles/agentregistry.viewer` | Read-only discovery (`list`/`get`/`search`) and active skill downloads (`?alt=media`) | Polyglot Middleware SA, Platform Agents (ADK), developers |
| `roles/agentregistry.user` | `viewer` + standalone `Skill` CRUD only | Skill authors (`skill-author` SA) |
| `roles/agentregistry.editor` | `viewer` + `Service` and `Skill` CRUD | CI/CD Publisher SA only (never runtime agents) |
| `roles/agentregistry.admin` | Full control + `Binding`, `Finding`, per-`Skill` IAM, `SkillEncryptionSpec` | Platform Security Admins only (never runtime agents) |

## Step 2: Test `Binding` and `bindings:fetchAvailable` IAM Filtering

In [`terraform/security_governance/auth_bindings.tf`](../../terraform/security_governance/auth_bindings.tf), an `AuthProvider` and `Binding` link the FinOps A2A Agent to the FinOps MCP Server using read-only OAuth scopes (`cloud-billing.readonly`, `bigquery.readonly`). At runtime, `GET .../bindings:fetchAvailable` drops any binding whose `AuthProvider` the caller cannot access via `roles/agentidentity.user`.

Make sure `make run-mock` is running on `:8090`, then test authorized vs. unauthorized callers with `X-Mock-Allowed-Auth-Providers`:

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

# 2. Authorized caller (holds roles/agentidentity.user on finops-mcp-oauth) -> returns 1 binding
curl -fsSL \
  -H "X-Mock-Allowed-Auth-Providers: finops-mcp-oauth" \
  "${BASE}/bindings:fetchAvailable?sourceIdentifier=${AGENT_URN}&targetIdentifier=${MCP_URN}" \
  | .venv/bin/python -m json.tool

# 3. Unauthorized caller -> returns bindings: []
curl -fsSL \
  -H "X-Mock-Allowed-Auth-Providers: other-auth-provider" \
  "${BASE}/bindings:fetchAvailable?sourceIdentifier=${AGENT_URN}&targetIdentifier=${MCP_URN}" \
  | .venv/bin/python -m json.tool
```

You should see 1 binding returned for `finops-mcp-oauth` and an empty list for `other-auth-provider`.

## Step 3: Preview IAP v2 UAP CEL Rules Locally

Open [`security-governance/iap-uap/mcp-tool-annotations-uap.json`](../../security-governance/iap-uap/mcp-tool-annotations-uap.json). Two CEL rules on `iap.googleapis.com/resources.egressViaIAP` enforce tool-level safety at the Agent Gateway boundary:

- **ALLOW rule** (permits `get_billing_summary` and `analyze_bq_query`):
  ```cel
  destination.is_registered == true &&
  destination.agent_registry.resource_type == 'MCP_SERVER' &&
  destination.agent_registry.mcp_server.tool.annotations.read_only_hint == true
  ```
- **DENY rule** (blocks `delete_idle_dataset`):
  ```cel
  destination.agent_registry.mcp_server.tool.annotations.destructive_hint == true
  ```

Validate the policy JSON and preview the exact `gcloud` commands offline with `--dry-run` (live Agent Gateway runs in `us-central1` or `europe-west1`):

```bash
bash security-governance/iap-uap/apply-uap.sh \
  --project acme-agent-governance \
  --location us-central1 \
  --gateway-name agent-marketplace-gateway \
  --dry-run
```

You should see the JSON pass validation and the exact IAM v3 `access-policies` and `policy-bindings` commands printed without touching GCP.

## Step 4: Test the Client-Side Policy Guard Hook

Open [`security-governance/hooks/hooks.json`](../../security-governance/hooks/hooks.json) (named group `"policy-guard"`, relative `./scripts/policy_guard.py`, mode `0755`). This hook adds a client-side guardrail on top of server-side IAP policies.

Test `PreToolUse` decisions and redacted `PostToolUse` logging locally:

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

# 4. PostToolUse redacted audit log (records arg_keys only, never secret values)
echo '{"conversation_id": "conv-demo-1", "agent_name": "finops-advisor", "tool_call": {"name": "get_billing_summary", "args": {"project_id": "acme-agent-governance", "token": "SECRET"}}, "result": "ok"}' \
  | POLICY_GUARD_AUDIT_LOG=/tmp/policy-guard-demo.jsonl \
    .venv/bin/python security-governance/hooks/scripts/policy_guard.py --event PostToolUse

cat /tmp/policy-guard-demo.jsonl
```

You should see `{}` for step 1, `"decision": "deny"` for steps 2 and 3, and `"arg_keys": ["project_id", "token"]` in `/tmp/policy-guard-demo.jsonl` with zero trace of `"SECRET"`.

## Step 5: Inspect Audit Logging Config and Compliance Queries

Open [`terraform/security_governance/audit_logging.tf`](../../terraform/security_governance/audit_logging.tf):
1. It enables `ADMIN_READ` Data Access logs on both `agentregistry.googleapis.com` and `apphubendpoint.googleapis.com` (Admin Activity is already on by default).
2. It creates a regional Cloud Logging bucket (`agent-governance-audit` in `asia-southeast3`, `enable_analytics = true`) with a sink routing `cloudaudit.googleapis.com/activity`, `cloudaudit.googleapis.com/data_access`, and `networkservices.googleapis.com/gateway_requests`.

> **WORM Lock Warning (`lock_audit_bucket`):** Once `lock_audit_bucket = true` is applied, the bucket retention period cannot be shortened and the bucket cannot be deleted until all log entries expire. Keep `lock_audit_bucket = false` in workshop sandboxes so `terraform destroy` works, and set `lock_audit_bucket = true` in production.

Now open [`security-governance/audit/compliance-queries.md`](../../security-governance/audit/compliance-queries.md) to review the Cloud Logging filters and BigQuery Log Analytics SQL queries for:
- Scanning policy downgrades (`scanningConfig.policy = "DISABLED"`)
- Denied IAP `egressViaIAP` calls and unregistered egress attempts (`unregisteredResource`)
- IAP UAP `DRY_RUN` readiness breakdown before flipping to enforced mode
- Cloud KMS `AsymmetricSign` and `GetPublicKey` audit trails

---

## Going Live on GCP (Optional)

**Region note:** In `terraform/security_governance/terraform.tfvars`, set `location = "asia-southeast3"` (Bangkok primary) and `auth_provider_location = "asia-southeast1"` (Singapore fallback for Agent Identity). Keep `enable_agent_gateway = false` (US/EU only) and `lock_audit_bucket = false` in workshop sandboxes. Standalone `Skills` and per-`Skill` IAM stay in `global` (see [Attendee Quickstart](../attendee-quickstart.md)).

### 1. Grant Per-`Skill` Resource-Level IAM

Restrict a sensitive private skill (in `global`, or `us`/`eu` for CMEK) to a specific service account:

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

### 2. Apply Workstream B Terraform and Optional Live IAP UAP

If you haven't already created `terraform/security_governance/terraform.tfvars` in [Lab 07](07-kms-encryption-and-signing.md), copy the template first and fill in your required variables (`project_id`, `middleware_sa_email`, `mcp_server_resource_id`, `mcp_server_urn`, `a2a_agent_resource_id`, `a2a_agent_urn`):

```bash
cp terraform/security_governance/terraform.tfvars.example \
   terraform/security_governance/terraform.tfvars
```

Then plan and apply the Workstream B stack:

```bash
export TF_VAR_auth_provider_client_secret="<your-oauth-client-secret>"
make tf-sec-plan
terraform -chdir=terraform/security_governance apply
```

If you also evaluate live Agent Gateway in `us-central1` (with `enable_agent_gateway = true`), apply the IAM v3 UAP policy:

```bash
bash security-governance/iap-uap/apply-uap.sh \
  --project "<your-gcp-project-id>" \
  --location "us-central1" \
  --gateway-name "agent-marketplace-gateway" \
  --principal "principal://agents.global.proj-<PROJECT_NUMBER>.system.id.goog/resources/aiplatform/projects/<PROJECT_ID>/locations/us-central1/reasoningEngines/finops-advisor-agent"
```

### 3. Clean Teardown

Remove workshop GCP resources when finished so no billable Cloud Run services, Artifact Registry images, or test skills remain:

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"

# 1. Destroy Workstream B Terraform stack (clean when lock_audit_bucket = false;
#    Cloud KMS key versions schedule for destruction rather than instant deletion)
terraform -chdir=terraform/security_governance destroy

# 2. Destroy Workstream A resources (pick the track you used in Lab 03)
# Track 2 (gcloud + Cloud Build):
make gcp-destroy GCP_PROJECT_ID="${GCP_PROJECT_ID}"
# Direct script equivalent:
# scripts/destroy_gcp.sh --project "${GCP_PROJECT_ID}" --region asia-southeast3 --skills-location global

# Track 3 (Terraform Core — set force_destroy_staging_bucket = true in sandbox .tfvars if objects exist):
# terraform -chdir=terraform/core destroy

# 3. Verify zero workshop Cloud Run services remain in Bangkok (asia-southeast3)
gcloud run services list --project="${GCP_PROJECT_ID}" --region="asia-southeast3" --limit=50 --quiet
```

> **Teardown Tip:** Because `gcloud agent-registry skills delete` has no `--force` flag, remove any leftover skill with revisions via REST `DELETE "https://agentregistry.googleapis.com/v1/projects/${GCP_PROJECT_ID}/locations/${AGENT_REGISTRY_SKILLS_LOCATION:-global}/skills/private-<id>?force=true"` (with `Authorization` and `x-goog-user-project` headers). On projects with IAM policy version 3 conditional bindings, pass `--condition=None` to `gcloud projects remove-iam-policy-binding`. `scripts/destroy_gcp.sh` automates both.

---

## Verification Checkpoint

- [ ] Proved `bindings:fetchAvailable` returns the binding for an authorized `AuthProvider` caller and `[]` for an unauthorized caller.
- [ ] Validated `mcp-tool-annotations-uap.json` with `apply-uap.sh --dry-run`.
- [ ] Confirmed `policy_guard.py` allows read-only MCP calls, denies `destructiveHint: true` and `terraform destroy`, and writes redacted `PostToolUse` JSONL entries.
- [ ] Reviewed [`security-governance/audit/compliance-queries.md`](../../security-governance/audit/compliance-queries.md) and ran:
  ```bash
  .venv/bin/pytest -v tests/test_workstream_b_security.py
  ```

## Troubleshooting

| Symptom | Why It Happens | How to Fix |
| :--- | :--- | :--- |
| `bindings:fetchAvailable` returns all bindings locally | `X-Mock-Allowed-Auth-Providers` header was omitted | Pass `-H "X-Mock-Allowed-Auth-Providers: <provider-id>"` to simulate IAM filtering in `mock_registry`. |
| `gcloud beta iam access-policies create` rejects the principal | Passed a short email instead of an IAM v3 Workload or Agent Identity URI | Use the full IAM v3 principal format (`principal://agents.global.proj-<NUM>.system.id.goog/...` or `principal://iam.googleapis.com/...`). |
| Missing `apphubendpoint.googleapis.com` discovery audit records | Enabled `ADMIN_READ` only on `agentregistry.googleapis.com` | Enable `ADMIN_READ` Data Access on both `agentregistry.googleapis.com` and `apphubendpoint.googleapis.com` (see `audit_logging.tf`). |
| `gcloud agent-registry skills add-iam-policy-binding` returns 404 or times out in `asia-southeast3` | Standalone `Skills` live in `global` (or `us`/`eu` for CMEK) | Pass `--location=global` for per-`Skill` IAM while keeping `Bindings` in `asia-southeast3` and `AuthProvider` in `asia-southeast1`. |
| `gcloud projects remove-iam-policy-binding` fails or prompts on teardown | Project uses IAM policy version 3 with conditional bindings | Pass `--condition=None` to `add-iam-policy-binding` and `remove-iam-policy-binding` (automated in `scripts/deploy_gcp.sh` and `scripts/destroy_gcp.sh`). |

---

**Congratulations!** You finished both Workstream A and Workstream B of the Enterprise Agent Marketplace Workshop. Return to the [Workshop Home](../../README.md) or review the [Customer Use Case 3 Executive Brief](../customer-usecase-3-brief.md).
