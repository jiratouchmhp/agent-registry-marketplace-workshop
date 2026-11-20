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

Restrict a sensitive private skill (e.g. `private-cost-anomaly-triage`) to a specific team or agent service account:

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"
export GCP_LOCATION="us-central1"

gcloud agent-registry skills add-iam-policy-binding private-cost-anomaly-triage \
  --project="${GCP_PROJECT_ID}" \
  --location="${GCP_LOCATION}" \
  --member="serviceAccount:agent-marketplace-middleware@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/agentregistry.viewer"
```

---

## Step 2: `AuthProvider` + `Binding` + `FetchAvailableBindings` IAM Filtering

In [terraform/security_governance/auth_bindings.tf](../../terraform/security_governance/auth_bindings.tf), `google_agent_identity_auth_provider` + `google_agent_registry_binding` bind the FinOps A2A Agent (`source.identifier`) to the FinOps MCP Server (`target.identifier`) with scoped OAuth (`cloud-billing.readonly`, `bigquery.readonly`) and explicitly blocked directory admin scopes.

At runtime, calling `GET .../bindings:fetchAvailable` **actively filters out** any binding whose `AuthProvider` the caller lacks `roles/agentidentity.user` on.

### Mode A: Local Mock Mode — Demonstrate IAM-Filtered `bindings:fetchAvailable`

Ensure `make run-mock` is running on `:8090`, then create a Binding and test authorized vs. unauthorized callers using `X-Mock-Allowed-Auth-Providers`:

```bash
BASE="http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/us-central1"
AGENT_URN="urn:agent:projects-123456789012:projects:123456789012:locations:us-central1:agentregistry:services:finops-advisor-agent"
MCP_URN="urn:mcp:projects-123456789012:projects:123456789012:locations:us-central1:agentregistry:services:finops-mcp-server"
AUTH_PROV="projects/acme-agent-governance/locations/us-central1/authProviders/finops-mcp-oauth"

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
2. Creates a **locked WORM Cloud Logging bucket** (`agent-governance-audit`, `locked = true`, `enable_analytics = true`) with a dedicated sink routing:
   - `cloudaudit.googleapis.com/activity` (`agentregistry`, `apphubendpoint`, `agentidentity`, `iap`, `networkservices`, `cloudkms`)
   - `cloudaudit.googleapis.com/data_access` (`agentregistry`, `apphubendpoint`, `iap`, `cloudkms`)
   - `networkservices.googleapis.com/gateway_requests`
3. Open [security-governance/audit/compliance-queries.md](../../security-governance/audit/compliance-queries.md) for production-ready **Cloud Logging** filters and **BigQuery Log Analytics** SQL queries covering:
   - Scanning policy downgrade alerts (`scanningConfig.policy = "DISABLED"`)
   - Denied IAP `egressViaIAP` calls & shadow unregistered egress attempts (`unregisteredResource`)
   - IAP UAP `DRY_RUN` readiness breakdown before flipping to enforced mode
   - Cloud KMS `AsymmetricSign` / `GetPublicKey` audit trail

> **Irreversibility Warning:** Once `locked = true` is applied on `google_logging_project_bucket_config`, the bucket's retention policy cannot be shortened and the bucket cannot be deleted until all log entries expire.

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

---

**Congratulations!** You have completed both **Workstream A** and **Workstream B** of the Enterprise Agent Marketplace Workshop. Return to the [Workshop Home](../../README.md) or review the [Customer Use Case 3 Executive Brief](../customer-usecase-3-brief.md).
