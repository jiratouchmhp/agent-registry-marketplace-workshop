# Agent Registry (`v1` GA) API, `gcloud`, Terraform Schemas, Lifecycle & Quotas

Deep technical reference for **Google Cloud Agent Registry (`v1` GA)** (`agentregistry.googleapis.com`), covering REST endpoints, `tools.plugin_sync` CLI options, `gcloud agent-registry` commands, GA `hashicorp/google >= 7.39.0` Terraform schemas, the `Skill` lifecycle state machine, `ENABLED_BLOCKING` scanning & `Finding` inspection, and system quotas/limits.

---

## 1. Endpoints, Locations & Core Invariants

- **API Service:** `agentregistry.googleapis.com`
- **REST Base:** `https://agentregistry.googleapis.com/v1` (Local Workshop Mock: `http://127.0.0.1:8090/v1`, resettable via `POST http://127.0.0.1:8090/_mock/reset`)
- **Per-Product Per-Project Service Agent (P4SA):** `service-${PROJECT_NUMBER}@gcp-sa-agentregistry.iam.gserviceaccount.com`
  - Required for importing `gcsSource` skill ZIPs (`roles/storage.objectViewer` on the GCS staging bucket) and CMEK at-rest encryption (`roles/cloudkms.cryptoKeyEncrypterDecrypter`).
- **Locations:** `global` (mandatory for auto-discovered Google/GCP remote MCP servers), multi-regions `us` and `eu`, plus regional locations (e.g., `us-central1`, `europe-west1`).
- **No Generic `labels` Map:** `Service`, `Agent`, `McpServer`, `Endpoint`, `Binding`, and `Skill` reject any `labels` field with `400 INVALID_ARGUMENT`. Store plugin/provenance attributes exclusively in `SKILL.md` YAML `frontmatter.metadata` (`map<string, string>` scalar strings only).

### Canonical `SKILL.md` `frontmatter.metadata` Provenance Keys

Injected deterministically by `tools/plugin_sync/sync.py` and `tools/plugin_sync/sources.py`:

| Metadata Key | Type | Purpose |
|---|---|---|
| `plugin_name` | `string` | Logical kebab-case plugin bundle name (`^[a-z0-9]+(-[a-z0-9]+)*$`, no disallowed brand prefixes) used by Polyglot Middleware to re-assemble IDE bundles |
| `plugin_display_name` | `string` | Human-readable bundle display name |
| `plugin_version` | `string` | Semver version string (default `1.0.0`) |
| `source_repo` | `string` | Sanitized Git repository URL (all embedded `user:token@` credentials stripped) or `local://<dir>` |
| `git_commit` | `string` | Exact 40-char `HEAD` SHA when cloned/detected from Git (else `unknown`) |
| `mcp_service_id` | `string` | Linked Agent Registry `McpServer` Service ID (e.g., `finops-mcp-server`) |
| `agent_service_id` | `string` | Linked Agent Registry `Agent` Service ID (e.g., `finops-advisor-agent`) |
| `kms_signature` | `string` | Base64 DER ECDSA P-256 signature over the zlib-independent canonical skill payload digest (`compute_canonical_skill_digest`) |
| `kms_key_version` | `string` | Cloud KMS CryptoKeyVersion resource name or logical PEM label (`local-ecdsa-p256-pem`) |

---

## 2. Agent Registry `v1` REST API Reference

Base parent path: `PARENT="projects/{project}/locations/{location}"`.

### A. Writable `Service` Resources (Manual Registration Control Plane)

Creating a `Service` returns a Long-Running Operation (`operations/{opId}`) and projects a read-only `Agent`, `McpServer`, or `Endpoint` whose full resource name is exposed in `service.registryResource`.

| Operation | Method & Path | Key Rules |
|---|---|---|
| Create Service | `POST /v1/{PARENT}/services?serviceId={id}` | Returns LRO. `serviceId`: `^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$` (4–63 chars in live GCP). Must specify **exactly one** of `agentSpec`, `mcpServerSpec`, `endpointSpec` (**spec `type` is immutable**). |
| List / Get | `GET /v1/{PARENT}/services` \| `GET /v1/{PARENT}/services/{id}` | Returns `registryResource` pointing to the projected read-only resource. |
| Update Service | `PATCH /v1/{PARENT}/services/{id}?updateMask=...` | MCP mask: `displayName,description,interfaces,mcpServerSpec.content`. A2A mask: `displayName,description,agentSpec.content`. Cannot change spec `type`. |
| Delete Service | `DELETE /v1/{PARENT}/services/{id}` | Returns LRO; removes Service and its projected read-only resource. |
| Poll LRO | `GET /v1/{PARENT}/operations/{opId}` | Poll until `done == true`; check `error` before reading `response`. |

#### `oneof spec` Rules (`MAX_SERVICE_SPEC_BYTES = 10 KB`)

| Spec Block | Allowed `type` | Top-Level `interfaces` | `content` Contract |
|---|---|---|---|
| `mcpServerSpec` | `"TOOL_SPEC"` \| `"NO_SPEC"` | Required (`url`, `protocolBinding`: `"JSONRPC"` \| `"HTTP_JSON"` \| `"GRPC"`) | For `TOOL_SPEC`: `{"tools": [{name, description, inputSchema, annotations: {title, readOnlyHint, destructiveHint, idempotentHint, openWorldHint}}]}` (≤ 10 KB). Empty for `NO_SPEC`. |
| `agentSpec` | `"A2A_AGENT_CARD"` \| `"NO_SPEC"` | **MUST be empty/omitted** for `A2A_AGENT_CARD`; required for `NO_SPEC` | For `A2A_AGENT_CARD`: A2A v1.0 Agent Card JSON with `name`, `supportedInterfaces` (`url`, `protocolBinding`, `protocolVersion`), `capabilities`, `defaultInputModes`, `defaultOutputModes`, `skills[]` (≤ 10 KB). |
| `endpointSpec` | `"NO_SPEC"` | Required (`url`, `protocolBinding`) | Must be empty. Use `endpointSpec` (not `agentSpec: NO_SPEC`) for non-agent REST/SaaS APIs so governance policies classify them as `Endpoint`. |

### B. Read-Only Discovery & Search Endpoints

| Resource | List / Get | Search Endpoint & Request Body |
|---|---|---|
| **Agents** | `GET /v1/{PARENT}/agents`, `GET .../agents/{id}` | `POST /v1/{PARENT}/agents:search` — `{"searchString": "..."}` (**keyword/prefix only — NO `searchType`!**) |
| **MCP Servers** | `GET /v1/{PARENT}/mcpServers`, `GET .../mcpServers/{id}` | `POST /v1/{PARENT}/mcpServers:search` — `{"searchString": "..."}` (**keyword/prefix only — NO `searchType`!**) |
| **Endpoints** | `GET /v1/{PARENT}/endpoints`, `GET .../endpoints/{id}` | **No `:search` endpoint** — use AIP-160 `?filter=...` on `GET .../endpoints`. |
| **Skills** | `GET /v1/{PARENT}/skills`, `GET .../skills/private-{id}` | `POST /v1/{PARENT}/skills:search` — `{"searchString": "...", "searchType": "KEYWORD"\|"SEMANTIC", "filter": "state=ACTIVE AND defaultRevisionFindingCount=0"}` |
| **Publishers** | `GET /v1/{PARENT}/publishers` | Read-only verification tiers: `FIRST_PARTY` (`google-`), `THIRD_PARTY`, `PRIVATE` (`private-`). |
| **Findings** | `GET /v1/{PARENT}/findings?filter=resource="..."` | Requires `roles/agentregistry.admin`. |

### C. Standalone `Skill` & Immutable `SkillRevision` Endpoints

Standalone skills are top-level resources (no `Service` wrapper). Creating `?skillId=cost-anomaly-triage` creates `projects/{p}/locations/{l}/skills/private-cost-anomaly-triage` (`urn:skill:projects-{NUM}:locations:{LOC}:private-cost-anomaly-triage`).

| Operation | Method & Path | Request Body / Notes |
|---|---|---|
| Create Skill | `POST /v1/{PARENT}/skills?skillId={rawId}` | `{"displayName", "description", "type": "SIMPLE", "targetState", "scanningConfig": {"policy": "ENABLED_BLOCKING"}, "initialRevision": {"archiveUploadSource": {"archiveContent": "<base64-zip>"}}}` (or `"gcsSource": {"uri": "gs://bucket/skill.zip"}`). Returns LRO. |
| Create Revision | `POST /v1/{PARENT}/skills/private-{rawId}/revisions?skillRevisionId={revId}` | `{"archiveUploadSource": {"archiveContent": "<base64-zip>"}}` or `{"gcsSource": {"uri": "gs://..."}}`. Immutable snapshot with server-computed `sha256Hash`, `sizeBytes`, `scanResult`. |
| Describe / List Revisions | `GET /v1/{PARENT}/skills/private-{rawId}/revisions[/{revId}]` | Check `scanResult.scanState` and `scanResult.findings[]` before promoting. |
| Update / Promote / Rollback | `PATCH /v1/{PARENT}/skills/private-{rawId}?updateMask=defaultRevision,targetState` | Set `defaultRevision` to full revision name `projects/{p}/locations/{l}/skills/private-{id}/revisions/{revId}` for atomic promotion or rollback. |
| Download ZIP (`?alt=media`) | `GET /v1/{PARENT}/skills/private-{rawId}/revisions/{revId}?alt=media` | Returns **HTTP 302** to a short-lived GCS signed URL. Clients MUST follow redirects **and strip `Authorization` + all `x-goog-*` headers** on cross-origin redirect (`curl -L` works; naive `httpx` forwarding headers gets HTTP 403). |
| Delete Skill | `DELETE /v1/{PARENT}/skills/private-{rawId}?force=true` | `?force=true` is **required** whenever one or more revisions exist. |

### D. `agentskills.io` ZIP Package Rules (Validated Pre-Upload by `tools/plugin_sync/sync.py`)

- `Skill.type` MUST be `"SIMPLE"`.
- Strictly **one** uppercase `SKILL.md` at archive root (`SKILL.md`) or inside a single top-level directory (`<skill-name>/SKILL.md` with no stray root files or extra top-level dirs). Lowercase `skill.md` is rejected.
- `SKILL.md` YAML frontmatter: required `name` (`^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`, no `--`, must match folder name), required `description` (1–1,024 chars), optional `metadata` (`map<string, string>` scalar strings only).
- **Archive limits:** Compressed ZIP **≤ 1 MB** (recommend **≤ 500 KB** for inline `archiveUploadSource`; use `gcsSource` up to 1 MB), uncompressed total **≤ 10 MB**, individual file **≤ 1 MB**, max nesting depth **8**, zero symlinks, zero `..` traversal, zero absolute or backslash paths.

---

## 3. `tools.plugin_sync` CLI Reference (`tools/plugin_sync/cli.py`)

Invoke via `.venv/bin/python -m tools.plugin_sync <command>`.

| Subcommand | Purpose | Key Flags & Env Vars |
|---|---|---|
| `ingest` | Unified Option 1 (`local`) & Option 2 (`git`) auto-discovery & sync across all 5 repo layouts | `--source-type [local\|git]` (auto-inferred), `--path <dir>`, `--repo-url <url>`, `--ref <branch\|tag\|sha>`, `--subdir <rel-path>`, `--git-token-env <ENV_NAME>`, `--plugin-name`, `--plugin-display-name`, `--plugin-version` (default `1.0.0`), `--per-skill-plugin`, `--only-skills`, `--registry-url` (`AGENT_REGISTRY_BASE_URL`), `--project` (`GCP_PROJECT_ID`), `--location` (`GCP_LOCATION`, default `us-central1`), `--target-state` (default `TARGET_STATE_ACTIVE`), `--scanning-policy` (default `ENABLED_BLOCKING`), `--source-repo`, `--git-commit`, `--sign-pem`, `--kms-key-version`, `--auth-token` (`AGENT_REGISTRY_AUTH_TOKEN`), `--output-dir`, `--skip-mcp-probe`, `--dry-run` |
| `sync` | Sync a single Agent Plugins 1.0 directory (skills + MCP/A2A services) | `--plugin-dir <dir>` (required), plus the same registry, target-state, scanning-policy, provenance, signing, `--output-dir`, `--skip-mcp-probe`, and `--dry-run` flags |
| `verify` | Verify embedded ECDSA P-256 `kms_signature` in a packaged skill ZIP | `--skill-zip <path>` (required), `--public-key-pem <path>` (required) |

> **Auth Resolution Order (`_resolve_bearer_token`):** `--auth-token` → `AGENT_REGISTRY_AUTH_TOKEN` → `GOOGLE_OAUTH_ACCESS_TOKEN` → `gcloud auth print-access-token` (automatically called only for `googleapis.com` URLs).

---

## 4. Verified `gcloud agent-registry` Commands

```bash
# Enable API & create P4SA if needed
gcloud services enable agentregistry.googleapis.com --project="${GCP_PROJECT_ID}"
gcloud beta services identity create --service=agentregistry.googleapis.com --project="${GCP_PROJECT_ID}"

# Register Services manually (spec flags are mutually exclusive; <= 10 KB spec content)
gcloud agent-registry services create finops-advisor-agent \
  --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" \
  --display-name="GCP FinOps Advisor" \
  --agent-spec-type=a2a-agent-card \
  --agent-spec-content=starter-marketplace/plugins/gcp-finops-toolkit/agents/finops-advisor/agent-card.json

gcloud agent-registry services create finops-billing-api \
  --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" \
  --display-name="FinOps Cloud Billing API Endpoint" \
  --endpoint-spec-type=no-spec \
  --interfaces="url=https://cloudbilling.googleapis.com/v1,protocolBinding=http-json"

# Inspect writable Service & projected read-only resources
gcloud agent-registry services describe finops-advisor-agent --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
gcloud agent-registry agents list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
gcloud agent-registry mcp-servers list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
gcloud agent-registry endpoints list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" --filter='displayName:"FinOps"'

# Search (note: --search-type is ONLY valid on `skills search`)
gcloud agent-registry agents search --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" --search-string="finops"
gcloud agent-registry mcp-servers search --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" --search-string="billing"
gcloud agent-registry skills search --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" \
  --query="cost anomaly triage" --search-type=semantic

# Standalone Skill create (local ZIP or --gcs-source-uri="gs://BUCKET/skill.zip"), revision, promote, download, IAM, delete
gcloud agent-registry skills create cost-anomaly-triage \
  --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" \
  --display-name="Cost Anomaly Triage" --description="Diagnose GCP Cloud Billing spend spikes." \
  --target-state=disabled --scanning-policy=enabled-blocking --payload="/tmp/cost-anomaly-triage.zip"

# Use private- prefix on ALL post-create calls!
gcloud agent-registry skills describe private-cost-anomaly-triage --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
gcloud agent-registry skills revisions create v2 \
  --skill=private-cost-anomaly-triage --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" \
  --payload="/tmp/cost-anomaly-triage-v2.zip"
gcloud agent-registry skills revisions describe v2 \
  --skill=private-cost-anomaly-triage --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
gcloud agent-registry skills update private-cost-anomaly-triage \
  --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" \
  --default-revision="projects/${GCP_PROJECT_ID}/locations/${GCP_LOCATION}/skills/private-cost-anomaly-triage/revisions/v2" \
  --target-state=ACTIVE
gcloud agent-registry skills revisions download v2 \
  --skill=private-cost-anomaly-triage --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" \
  --destination="/tmp/extracted" --allow-overwrite

# Per-Skill IAM & Findings triage (both require roles/agentregistry.admin)
gcloud agent-registry skills add-iam-policy-binding private-cost-anomaly-triage \
  --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" \
  --member="serviceAccount:agent-marketplace-middleware@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/agentregistry.viewer"
gcloud agent-registry findings list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" \
  --filter="resource=\"projects/${GCP_PROJECT_ID}/locations/${GCP_LOCATION}/skills/private-cost-anomaly-triage/revisions/v2\""
gcloud agent-registry findings describe FINDING_ID --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" --format=json

# Delete skill & all revisions
gcloud agent-registry skills delete private-cost-anomaly-triage --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
```

---

## 5. GA Terraform Schemas (`hashicorp/google >= 7.39.0`)

Verified against `terraform/core/versions.tf`, `terraform/core/agent_registry_services.tf`, `terraform/core/skills_bootstrap.tf`, `terraform/core/variables.tf`, and `terraform/core/outputs.tf` (no `google-beta` provider needed).

### A. `google_agent_registry_service` + Direct Discovery Data Sources

```hcl
# 1. MCP Server Service (TOOL_SPEC + JSONRPC interface)
resource "google_agent_registry_service" "finops_mcp" {
  project         = var.project_id
  location        = var.location
  service_id      = "finops-mcp-server"
  display_name    = "FinOps Cost & BigQuery MCP Server"
  description     = "Governed MCP server with explicit tool annotations."
  deletion_policy = "DELETE" # DELETE | PREVENT | ABANDON

  interfaces {
    url              = "${google_cloud_run_v2_service.sample_mcp_server.uri}/mcp"
    protocol_binding = "JSONRPC" # JSONRPC | HTTP_JSON | GRPC
  }

  mcp_server_spec {
    type    = "TOOL_SPEC"
    content = jsonencode({ tools = [/* ... <= 10 KB ... */] })
  }
}

# 2. A2A Agent Service (A2A_AGENT_CARD — MUST OMIT top-level `interfaces`!)
resource "google_agent_registry_service" "finops_a2a_agent" {
  project         = var.project_id
  location        = var.location
  service_id      = "finops-advisor-agent"
  display_name    = "GCP FinOps Advisor"
  description     = "Enterprise A2A v1.0 FinOps specialist."
  deletion_policy = "DELETE"

  agent_spec {
    type = "A2A_AGENT_CARD"
    content = jsonencode({
      name                = "GCP FinOps Advisor"
      version             = "1.0.0"
      supportedInterfaces = [{ url = "${google_cloud_run_v2_service.sample_a2a_agent.uri}/a2a", protocolBinding = "JSONRPC", protocolVersion = "1.0" }]
      capabilities        = { streaming = false, pushNotifications = false, stateTransitionHistory = true }
      defaultInputModes   = ["text/plain", "application/json"]
      defaultOutputModes  = ["text/plain", "application/json"]
      skills              = [{ id = "cost-anomaly-triage", name = "Cost Anomaly Triage", description = "..." }]
    })
  }
}

# 3. Direct lookup via basename(registry_resource) — zero sleep/filter race conditions!
data "google_agent_registry_mcp_server" "finops_mcp" {
  project       = var.project_id
  location      = var.location
  mcp_server_id = basename(google_agent_registry_service.finops_mcp.registry_resource)
}

data "google_agent_registry_agent" "finops_a2a_agent" {
  project  = var.project_id
  location = var.location
  agent_id = basename(google_agent_registry_service.finops_a2a_agent.registry_resource)
}

data "google_agent_registry_endpoint" "finops_endpoint" {
  project     = var.project_id
  location    = var.location
  endpoint_id = basename(google_agent_registry_service.finops_endpoint.registry_resource)
}
```

### B. Standalone Skills Bootstrap (`terraform/core/skills_bootstrap.tf`)

`hashicorp/google >= 7.39.0` does **not** yet expose a native `google_agent_registry_skill` resource. `terraform/core/skills_bootstrap.tf` uses `terraform_data.bootstrap_starter_skills` with `triggers_replace` content hashes (in `local` mode) and a `lifecycle.precondition` requiring non-empty `skills_repo_url` when `skills_source_mode == "git"`.

| Terraform Variable (`terraform/core/variables.tf`) | Default | Purpose & Validation |
|---|---|---|
| `enable_skills_bootstrap` | `true` | Set `false` when GitLab CI exclusively publishes skills |
| `skills_source_mode` | `"local"` | `"local"` (Option 1) or `"git"` (Option 2) |
| `starter_plugin_dir` | `"../../starter-marketplace/plugins/gcp-finops-toolkit"` | Starter plugin ingested in `local` mode |
| `enable_local_standalone_skills` | `true` | Also ingests `local_skills_dir` in `local` mode |
| `local_skills_dir` | `"../../local-skills"` | Standalone `agentskills.io` folder (`local` mode) |
| `local_skills_plugin_name` | `"local-ops-toolkit"` | Kebab-case bundle name (`^[a-z0-9]+(-[a-z0-9]+)*$`, ≤ 64 chars) |
| `skills_repo_url` | `""` | GitLab/GitHub URL for `git` mode (required by precondition) |
| `skills_repo_ref` | `"main"` | Non-empty branch, tag, or commit SHA |
| `skills_repo_subdir` | `""` | Empty or relative path without `..` or leading `/` |
| `skills_repo_token_env` | `""` | Env var **NAME** matching `^[A-Za-z_][A-Za-z0-9_]*$` (NEVER a raw token!) |
| `skills_repo_plugin_name` | `"central-repo-skills"` | Default bundle name for standalone skills discovered in `git` mode |
| `skills_per_skill_plugin` | `false` | Passes `--per-skill-plugin` to emit 1 logical plugin per standalone skill |
| `skills_bootstrap_command` | `""` | Optional custom shell command override |

> **Ownership Guarantee:** `skills_bootstrap.tf` **always** passes `--only-skills --skip-mcp-probe` to `tools.plugin_sync ingest`, so `agent_registry_services.tf` remains the sole owner of `google_agent_registry_service` Cloud Run endpoints. `terraform/core/iam.tf` also provisions `google_storage_bucket.skill_staging` and grants `roles/storage.objectViewer` to the Agent Registry P4SA for `gcsSource` imports.

---

## 6. Skill Lifecycle State Machine & Automated Scanning

### Lifecycle State Transitions

```text
  [Create Skill]
        │
        ▼
  STATE_DRAFT ────(0 findings + promote)────► STATE_ACTIVE ◄────(re-enable)────► STATE_DISABLED
  (quarantined;                               (served &                          (emergency kill switch;
   not served)                                 downloadable)                      blocked, reversible)
                                                    │
                                                    ▼ (deprecate)
                                             STATE_DEPRECATED
                                             (downloadable with warnings)
                                                    │
                                                    ▼ (retire — ONLY allowed from DEPRECATED!)
                                           STATE_DECOMMISSIONED
                                           (TERMINAL & IRREVERSIBLE)
```

Key lifecycle rules:
- **Auto-quarantine:** Creating a skill with `TARGET_STATE_ACTIVE` and `ENABLED_BLOCKING` starts in `STATE_DRAFT` and auto-promotes to `STATE_ACTIVE` **only** if scanning succeeds with `0` findings while still in `STATE_DRAFT` pointing to `initialRevision`.
- **Without `initialRevision`:** Create only accepts `TARGET_STATE_DRAFT` or `TARGET_STATE_DISABLED`.
- **Deleting `defaultRevision`:** Clears the pointer and transitions an `ACTIVE` skill to `DISABLED` — always `PATCH` `defaultRevision` to a known-good revision first to avoid downtime.
- **Terminal state guardrail:** Jumping directly from `DRAFT`, `ACTIVE`, or `DISABLED` to `TARGET_STATE_DECOMMISSIONED` is rejected (`400`); once `STATE_DECOMMISSIONED`, no further state transitions are permitted.

### Scanning Policy Matrix (`scanningConfig.policy`)

| Operation | `ENABLED_BLOCKING` *(Recommended)* | `ENABLED_NONBLOCKING` *(Audit)* | `DISABLED` |
|---|---|---|---|
| Create with `initialRevision` + `TARGET_STATE_ACTIVE` | Quarantines in `STATE_DRAFT`; auto-activates only on `scanState=SUCCEEDED` with 0 findings | Transitions to `STATE_ACTIVE` after LRO | Transitions to `STATE_ACTIVE` after LRO |
| Download revision (`?alt=media`) | Fails (`400 FAILED_PRECONDITION`) if findings > 0 or scan incomplete | Allowed with warning | Allowed |
| Promote `defaultRevision` | Fails (`400 FAILED_PRECONDITION`) if findings > 0 or scan pending/failed | Allowed with warning | Allowed |
| Switch policy → `ENABLED_BLOCKING` | Fails (`FAILED_PRECONDITION`) if current `defaultRevision` has findings or incomplete scan | Allowed unless blocked by Org Policy | Allowed unless blocked by Org Policy |

- **Non-retroactive:** Changing `scanningConfig.policy` applies **only to newly uploaded revisions**.
- **`scanResult.scanState` values:** `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `DISABLED`, `UNSUPPORTED_FORMAT`.
- **`Finding` fields:** `name`, `uid`, `resource`, `severity` (`SEVERITY_CRITICAL` | `SEVERITY_HIGH` | `SEVERITY_MEDIUM` | `SEVERITY_LOW`), `findingClass` (`VULNERABILITY` | `SECRET`), `files[{path, lineNumber}]` (`0` = whole file), `detail`. Listing/describing `Finding` requires `roles/agentregistry.admin`.
- **Org Policy backstop:** Deploy `security-governance/org-policy/require-skill-scanning.yaml` (or strict `require-skill-scanning-blocking.yaml`) — see `agent-registry-security-and-prod-hardening`.

---

## 7. Governance Boundaries, IAM Roles & Quotas/Limits

### Cross-Project Management Boundaries & Project Scoping
- Resource Manager **Management Boundaries** aggregate *auto-discovered* agents and MCP servers from up to **10,000** member projects into the management project.
- **Critical Scoping Rule:** Manually registered `Service` resources, `Binding` resources, and custom standalone `Skill` / `SkillRevision` / `Publisher` / `Finding` resources are **strictly scoped to the project where created** and do **NOT** roll up from member projects. Create enterprise catalog skills directly in the designated hub/governance project.

### Predefined Agent Registry IAM Roles (`terraform/core/iam.tf`)

| Role | Scope | Intended Principal |
|---|---|---|
| `roles/agentregistry.viewer` | Read-only discovery, search, and active skill download | Polyglot Middleware SA (`agent-marketplace-middleware`) and runtime ADK agents — **never grant higher** |
| `roles/agentregistry.user` | Viewer + standalone `Skill` / `SkillRevision` CRUD & scan config | Standalone skill authors (`agent-registry-skill-author`) |
| `roles/agentregistry.editor` | Viewer + `Service` & `Skill` CRUD | CI/CD publisher SA (`agent-registry-ci-publisher`) |
| `roles/agentregistry.admin` | Full control + `Binding`, `Finding` inspection, per-`Skill` IAM, `SkillEncryptionSpec` | Platform security admins only |

### Official Agent Registry (`v1`) Quotas & System Limits

| Category | Metric / Limit | Default / Hard Limit |
|---|---|---|
| **Allocation Quotas** | Agents / MCP servers / Endpoints / Bindings / A2A skills per project | **100** each (global & per region) |
| **Allocation Quotas** | Standalone skills per project per location | **100** |
| **Allocation Quotas** | Skill revisions per project per location (shared across all skills in location) | **500** |
| **Rate Quotas** | Aggregate API requests (global + regional) | **12,000/min** (200 QPS) |
| **Rate Quotas** | Global OR per-region API requests | **1,200/min** (20 QPS) |
| **Rate Quotas** | Skill & revision CUD (`create` / `update` / `delete`) | **120/min** (2 QPS) per location |
| **Rate Quotas** | Skill & revision read (`get` / `list` / `search`) | **1,200/min** (20 QPS) per location |
| **System Limits** | `displayName` / `description` max length | **63 chars** (Services/Bindings), **128 chars** (Skills) / **2,048 chars** |
| **System Limits** | `agentSpec.content` or `mcpServerSpec.content` size | **10 KB** |
| **System Limits** | Compressed skill ZIP / Scanned uncompressed total / Individual file / Nesting depth | **1 MB** (≤ 500 KB inline recommended) / **10 MB** / **1 MB** / **8** |
| **System Limits** | A2A skills or MCP tools per service / Page size / Boundary member projects | **100** / **100** / **10,000** |
