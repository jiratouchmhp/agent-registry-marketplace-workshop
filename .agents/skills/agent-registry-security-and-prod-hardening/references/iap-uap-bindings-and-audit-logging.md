# IAM, Auth Manager Bindings, Agent Gateway IAP v2 UAP & WORM Audit Logging

This reference encodes the exact identity boundaries, Terraform resources (`terraform/security_governance/` + `terraform/core/iam.tf`), IAP v2 UAP CEL rules (`security-governance/iap-uap/`), client-side Antigravity `hooks.json` guard (`security-governance/hooks/`), and forensic audit queries (`security-governance/audit/compliance-queries.md`).

---

## 1. The 4 Identifier Layers (Never Mix Layers!)

Mixing identifier formats across APIs is the #1 source of `INVALID_ARGUMENT` and silent policy misses.

| Layer | Format & Verified Example (`WORKSHOP_SPEC.json`) | Valid Surfaces | FORBIDDEN Surfaces |
|---|---|---|---|
| **1. URN** | `urn:mcp:projects-123456789012:projects:123456789012:locations:asia-southeast3:agentregistry:services:finops-mcp-server`<br>`urn:agent:...`, `urn:endpoint:...`, `urn:skill:projects-123456789012:locations:global:private-cost-anomaly-triage` | Discovery/search results, `google_agent_registry_binding` `source.identifier` & `target.identifier`, `bindings:fetchAvailable` query params | **NEVER** in IAM bindings, IAP IAM resources, PolicyBindings, or Gateway configs |
| **2. Resource Name** | `projects/acme-agent-governance/locations/asia-southeast3/services/finops-mcp-server`<br>`projects/acme-agent-governance/locations/global/skills/private-cost-anomaly-triage`<br>`//networkservices.googleapis.com/projects/acme-agent-governance/locations/us-central1/agentGateways/agent-marketplace-gateway` | REST/gcloud CRUD, per-`Skill` IAM (live Skills in `global` / `us` / `eu`), `google_iap_agent_registry_*_iam_member` (short ID = `basename(registry_resource)`), IAM PolicyBinding `--target-resource` | Do not pass full Resource Names into `Binding` `source`/`target` (those require URNs) |
| **3. Agent / Workload Principal** | `principal://agents.global.proj-123456789012.system.id.goog/resources/aiplatform/projects/acme-agent-governance/locations/asia-southeast3/reasoningEngines/finops-advisor-agent`<br>`principalSet://...`, `serviceAccount:<email>` | IAM `member`, `roles/iap.egressor`, IAP v2 UAP `principals[]` | **NEVER pass `spiffe://...`** (IAM/UAP returns `INVALID_ARGUMENT`!) |
| **4. Runtime Reference** | Cloud Run `https://sample-mcp-server-<num>.asia-southeast3.run.app/mcp`, Vertex AI Reasoning Engine resource, App Hub backing workload | Backend routing, `Service.interfaces[].url`, `A2A_AGENT_CARD` `supportedInterfaces[]`, App Hub endpoint resolution (`apphubendpoint.googleapis.com`) | Never substitute raw URLs for Resource Names in IAM or URNs in `Binding` |

---

## 2. Least-Privilege IAM & Per-`Skill` Resource Sharing

### Persona Matrix (Verified Against `terraform/core/iam.tf` & `terraform/security_governance/kms.tf`)

| Identity | Service Account / Group | Agent Registry Role | Cloud KMS Role | Other Required Roles |
|---|---|---|---|---|
| **Polyglot Middleware** | `agent-marketplace-middleware@<PROJECT>.iam.gserviceaccount.com` | **`roles/agentregistry.viewer`** (ONLY) | **`roles/cloudkms.publicKeyViewer`** (ONLY) | `roles/iap.egressor` (if egressing via Gateway) |
| **Platform Agents (ADK)** | Dedicated agent workload SA / Agent Identity | **`roles/agentregistry.viewer`** (ONLY) | `roles/cloudkms.publicKeyViewer` (if verifying skills directly) | `roles/agentidentity.user`, `roles/iap.egressor` |
| **Standalone Skill Author** | `agent-registry-skill-author@<PROJECT>.iam.gserviceaccount.com` | `roles/agentregistry.user` (viewer + Skill CRUD only) | *(none)* | `roles/storage.objectUser` on skill staging bucket |
| **CI/CD Publisher** | `agent-registry-ci-publisher@<PROJECT>.iam.gserviceaccount.com` | `roles/agentregistry.editor` (Services & Skills CRUD) | `roles/cloudkms.signerVerifier` | `roles/artifactregistry.writer`, `roles/storage.objectUser` on staging bucket |
| **Agent Registry P4SA** | `service-<PROJECT_NUMBER>@gcp-sa-agentregistry.iam.gserviceaccount.com` | *(Google-managed)* | `roles/cloudkms.cryptoKeyEncrypterDecrypter` (on CMEK key) | `roles/storage.objectViewer` on skill staging bucket |
| **Sample / Target Workloads** | `finops-sample-workloads@<PROJECT>.iam.gserviceaccount.com` | *(none — zero registry IAM)* | *(none)* | Only their own domain APIs (never default Compute SA) |
| **Platform Security Admins** | Human SecOps group | `roles/agentregistry.admin` (Bindings, Findings, Skill IAM, CMEK) | KMS Admin | `roles/orgpolicy.policyAdmin` (Org level), `roles/iap.admin` |

### Per-`Skill` Resource-Level IAM

Use resource-level IAM on individual `Skill` resources to restrict sensitive playbooks without granting project-wide access (always use the auto-prefixed `private-<skill-id>` name and live Skills location `global` — or `us`/`eu` for CMEK):

```bash
# Grant viewer access on one sensitive skill
gcloud agent-registry skills add-iam-policy-binding private-cost-anomaly-triage \
  --project="${GCP_PROJECT_ID}" --location="${AGENT_REGISTRY_SKILLS_LOCATION:-global}" \
  --member="serviceAccount:finops-agent@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/agentregistry.viewer" --quiet

# Audit who can read a skill
gcloud agent-registry skills get-iam-policy private-cost-anomaly-triage \
  --project="${GCP_PROJECT_ID}" --location="${AGENT_REGISTRY_SKILLS_LOCATION:-global}" --quiet
```

---

## 3. Auth Manager (`AuthProvider`) & Agent Registry `Binding`

Configured in `terraform/security_governance/auth_bindings.tf` using GA `hashicorp/google >= 7.39.0`:

```hcl
resource "google_agent_identity_auth_provider" "finops_oauth" {
  project          = var.project_id
  location         = var.auth_provider_location # Default: "asia-southeast1" (Singapore GA for Agent Identity in APAC)
  auth_provider_id = var.auth_provider_id
  allowed_scopes   = var.auth_provider_scopes
  blocked_scopes = [
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/admin.directory.group",
  ]
  auth_provider_type_params {
    three_legged_oauth {
      client_id            = var.auth_provider_client_id
      client_secret        = var.auth_provider_client_secret # Inject via TF_VAR_auth_provider_client_secret
      authorization_url    = var.auth_provider_authorization_url
      token_url            = var.auth_provider_token_url
      enable_pkce          = true
      default_continue_uri = var.auth_provider_continue_uri
    }
  }
}

resource "google_agent_registry_binding" "finops_agent_to_mcp" {
  project         = var.project_id
  location        = var.location
  binding_id      = var.binding_id
  deletion_policy = "DELETE"
  source { identifier = var.a2a_agent_urn }
  target { identifier = var.mcp_server_urn }
  auth_provider_binding { # REQUIRED in GA Terraform provider
    auth_provider = google_agent_identity_auth_provider.finops_oauth.name
    scopes        = var.auth_provider_scopes
    continue_uri  = var.auth_provider_continue_uri
  }
}
```

### `FetchAvailableBindings` Active IAM Filtering

- `GET /v1/projects/{p}/locations/{l}/bindings:fetchAvailable?sourceIdentifier={urn}&targetIdentifier={urn}` **actively filters out** any binding whose `AuthProvider` the calling principal does **not** hold `roles/agentidentity.user` on.
- **Local Mock verification (`mock_registry/app.py`)**: pass `-H "X-Mock-Allowed-Auth-Providers: finops-mcp-oauth"` (authorized → returns binding) vs. `-H "X-Mock-Allowed-Auth-Providers: other-auth-provider"` (unauthorized → returns `{"bindings": []}`).

---

## 4. Network Services Agent Gateway + IAP v2 UAP (CEL Governance)

### Terraform Infrastructure (`terraform/security_governance/agent_gateway_iap.tf`)

1. **`google_network_services_agent_gateway.marketplace`** *(guarded by `count = var.enable_agent_gateway ? 1 : 0`, default `false` in APAC because Agent Gateway on Cloud Run is currently GA only in `us-central1` and `europe-west1`)*:
   - `location = var.agent_gateway_location` (`"us-central1"` or `"europe-west1"`)
   - `google_managed { governed_access_path = "CLIENT_TO_AGENT" }`
   - `registries = ["//agentregistry.googleapis.com/projects/${var.project_id}/locations/${var.location}"]`
2. **IAP Egressor IAM Bindings**:
   - `google_iap_agent_registry_mcp_server_iam_member` (`mcp_server_id = var.mcp_server_resource_id`, `role = "roles/iap.egressor"`)
   - `google_iap_agent_registry_agent_iam_member` (`agent_id = var.a2a_agent_resource_id`, `role = "roles/iap.egressor"`)
   - Pass the projected **short ID** (`basename(mcp_registry_resource)`), never the full URN!

### Verified IAP v2 UAP CEL Attributes (`iap.googleapis.com/resources.egressViaIAP`)

| CEL Attribute | Purpose |
|---|---|
| `destination.is_registered` | `true` if destination is registered in a linked Agent Registry |
| `destination.agent_registry.resource_type` | `'MCP_SERVER'`, `'AGENT'`, or `'ENDPOINT'` |
| `destination.agent_registry.mcp_server.name` | Registered MCP server resource name |
| `destination.agent_registry.mcp_server.method` | JSON-RPC method (e.g., `tools/call`, `tools/list`) |
| `destination.agent_registry.mcp_server.tool.name` | Invoked MCP tool name |
| `destination.agent_registry.mcp_server.tool.annotations.read_only_hint` | `bool` — from registered `TOOL_SPEC` |
| `destination.agent_registry.mcp_server.tool.annotations.destructive_hint` | `bool` — from registered `TOOL_SPEC` |
| `destination.agent_registry.mcp_server.tool.annotations.idempotent_hint` | `bool` — from registered `TOOL_SPEC` |

**Verified Policy Rules (`security-governance/iap-uap/mcp-tool-annotations-uap.json`):**
- **ALLOW**: `destination.is_registered == true && destination.agent_registry.resource_type == 'MCP_SERVER' && destination.agent_registry.mcp_server.tool.annotations.read_only_hint == true`
- **DENY**: `destination.agent_registry.mcp_server.tool.annotations.destructive_hint == true`

### Safe Rollout: `DRY_RUN` → Enforce Without Hitting the Validator Trap

1. Validate locally with `bash security-governance/iap-uap/apply-uap.sh --dry-run` and server-side with `--validate-only`.
2. Apply the AccessPolicy and bind to `//networkservices.googleapis.com/projects/${PROJECT_ID}/locations/${LOCATION}/agentGateways/${GATEWAY_NAME}` (`--bind-scope gateway`, the default in `apply-uap.sh`).
3. Start IAP enforcement metadata in `"DRY_RUN"` (`iamEnforcementMode: "DRY_RUN"`) and run **BigQuery Query 5.3** below for 7–14 days to inspect every `would_deny_calls` entry.
4. **CRITICAL ENFORCEMENT RULE**: To switch from `DRY_RUN` to enforced mode, **REMOVE the `iamEnforcementMode` key** from IAP `metadata` — **NEVER set `"ENFORCED"`** (the IAP validator rejects any explicit value other than `"DRY_RUN"`). Preserve all sibling `metadata` keys when patching with `updateMask=metadata`.
5. **Never blanket-deny `destination.is_registered == false`** across an entire gateway/project unless telemetry endpoints (`telemetry.mtls.googleapis.com`) are explicitly exempted.

---

## 5. Client-Side Defense-in-Depth: Antigravity `hooks.json` Guard

`security-governance/hooks/hooks.json` complements server-side Agent Gateway policies inside developer IDEs:
- Uses a named top-level group (`"policy-guard"`), `PreToolUse` and `PostToolUse` matcher arrays (`"matcher": ".*"`), and relative script path `"./scripts/policy_guard.py"` (mode `0755`, stdlib-only Python — **never** `${PLUGIN_ROOT}`).
- **`PreToolUse`** fails closed on malformed JSON and blocks:
  1. Tools annotated `destructiveHint: true` / `destructive_hint: true`.
  2. Destructive tool names matching `delete|destroy|drop|purge|wipe|truncate|terminate|decommission|revoke|remove|rm|kill`.
  3. 8 dangerous shell patterns in `run_command`/`bash`/`shell`/`terminal`/`exec`: `rm -rf`/`rm -fr`, `terraform destroy`, `gcloud ... delete`, `DROP TABLE|DATABASE|SCHEMA|DATASET`, `bq rm`, `gsutil`/`gcloud storage rm`, `kubectl delete`.
  4. Explicit human override: `POLICY_GUARD_APPROVE_DESTRUCTIVE=true`.
- **`PostToolUse`** writes redacted JSONL entries (`arg_keys` only — never raw secret values) to `POLICY_GUARD_AUDIT_LOG` (default `<tmp>/agent-policy-guard-audit.jsonl`).

---

## 6. Immutable WORM Audit Logging & Verified Compliance Queries

Provisioned by `terraform/security_governance/audit_logging.tf`:
- `google_project_iam_audit_config` enables **`ADMIN_READ`** Data Access on **BOTH** `agentregistry.googleapis.com` **and** `apphubendpoint.googleapis.com`.
- `google_logging_project_bucket_config.agent_governance_audit` creates bucket `agent-governance-audit` in `var.location` (`asia-southeast3`) with **`locked = var.lock_audit_bucket`** (`false` default for clean workshop teardown; **set `lock_audit_bucket = true` in production** — irreversible once locked!), `enable_analytics = true`, and `retention_days = var.audit_retention_days` (default `90`, valid `1..3650`).
- `google_logging_project_sink.agent_governance_audit` (`unique_writer_identity = true`, granted `roles/logging.bucketWriter`) routes:
  - `cloudaudit.googleapis.com/activity` (`agentregistry`, `apphubendpoint`, `agentidentity`, `iap`, `networkservices`, `cloudkms`)
  - `cloudaudit.googleapis.com/data_access` (`agentregistry`, `apphubendpoint`, `iap`, `cloudkms`)
  - `networkservices.googleapis.com/gateway_requests`

### Verified Cloud Logging Filters (`security-governance/audit/compliance-queries.md`)

**1. Scanning policy downgrade alert (`scanningConfig.policy = "DISABLED"`):**
```text
log_id("cloudaudit.googleapis.com/activity")
protoPayload.serviceName="agentregistry.googleapis.com"
protoPayload.methodName=~"(CreateSkill|UpdateSkill)"
protoPayload.request.skill.scanningConfig.policy="DISABLED"
```

**2. `ADMIN_READ` discovery & App Hub workload resolution:**
```text
log_id("cloudaudit.googleapis.com/data_access")
(protoPayload.serviceName="agentregistry.googleapis.com" OR protoPayload.serviceName="apphubendpoint.googleapis.com")
```

**3. Denied IAP `egressViaIAP` calls & shadow unregistered egress:**
```text
# Denied tool or agent calls
protoPayload.serviceName="iap.googleapis.com"
protoPayload.authorizationInfo.permission="iap.resources.egressViaIAP"
protoPayload.authorizationInfo.granted=false

# Unregistered shadow MCP / shadow agent destination attempts
protoPayload.serviceName="iap.googleapis.com"
protoPayload.authorizationInfo.permission="iap.resources.egressViaIAP"
labels."iap.googleapis.com/audited_resource_name"="unregisteredResource"
```

**4. Cloud KMS signing & CMEK audit trail:**
```text
log_id("cloudaudit.googleapis.com/data_access")
protoPayload.serviceName="cloudkms.googleapis.com"
protoPayload.methodName=~"(AsymmetricSign|GetPublicKey|Decrypt|Encrypt)"
```

### BigQuery Log Analytics SQL — IAP UAP `DRY_RUN` Readiness (Query 5.3)

Run against `` `PROJECT_ID.REGION.agent-governance-audit._AllLogs` `` before removing `iamEnforcementMode`:

```sql
SELECT
  proto_payload.audit_log.authentication_info.principal_subject AS agent_principal,
  auth.resource AS destination_resource,
  JSON_VALUE(proto_payload.audit_log.metadata, '$.mcp_attributes.base_protocol_method') AS is_base_protocol,
  COUNTIF(auth.granted = TRUE) AS would_allow_calls,
  COUNTIF(auth.granted = FALSE) AS would_deny_calls
FROM
  `PROJECT_ID.REGION.agent-governance-audit._AllLogs`,
  UNNEST(proto_payload.audit_log.authorization_info) AS auth
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
  AND proto_payload.audit_log.service_name = 'iap.googleapis.com'
  AND auth.permission = 'iap.resources.egressViaIAP'
  AND JSON_VALUE(proto_payload.audit_log.metadata, '$.iamEnforcementMode') = 'DRY_RUN'
GROUP BY 1, 2, 3
ORDER BY would_deny_calls DESC;
```
