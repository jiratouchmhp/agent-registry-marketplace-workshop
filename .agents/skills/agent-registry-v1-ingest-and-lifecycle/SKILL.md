---
name: agent-registry-v1-ingest-and-lifecycle
description: >-
  Decomposes, registers, syncs, and manages standalone Skills, MCP Servers, A2A
  v1.0 Agents, and Endpoints on Google Cloud Agent Registry (v1 GA) via
  tools.plugin_sync (Option 1 local directories or Option 2 centralized Git
  repos), gcloud agent-registry, terraform/core (google_agent_registry_service
  and skills_source_mode), and push-based GitLab CI Dual-SoT pipelines. Use when
  ingesting local or Git skills/plugins, registering Services, triaging
  ENABLED_BLOCKING scan Findings, promoting or rolling back SkillRevisions,
  transitioning skill lifecycle states (DRAFT, ACTIVE, DISABLED, DEPRECATED,
  DECOMMISSIONED), or migrating .gitlab-ci.yml. Don't use for end-to-end
  workshop navigation (agent-marketplace-workshop-guide), authoring 5-IDE
  plugins (polyglot-agent-plugin-authoring), serving Middleware/hub_sync/ADK
  consumption (marketplace-middleware-and-ide-delivery), or KMS/IAP/Org
  Policy/Audit hardening (agent-registry-security-and-prod-hardening).
---

# Agent Registry (`v1` GA) Ingest, Registration & Skill Lifecycle

Decompose, register, sync, and govern standalone **Skills**, **MCP Servers** (`TOOL_SPEC`), **A2A v1.0 Agents** (`A2A_AGENT_CARD`), and **Endpoints** (`NO_SPEC`) on **Google Cloud Agent Registry (`v1` GA)** across local workshop labs (`mock_registry` on `:8090`), active repository development, and production CI/Terraform rollouts.

## Quick Decision Tree

```text
What are you ingesting or managing on Agent Registry (v1)?
├── Option 1: Local standalone skills (`local-skills/`) or local plugin bundle (`starter-marketplace/plugins/...`)
│   └── Run `python -m tools.plugin_sync ingest --source-type local --path <dir> --dry-run` (or `make demo-sync-local`)
│       -> See references/gitlab-ci-and-git-ingest-patterns.md
├── Option 2: Centralized GitLab / GitHub repository (pull-based shallow clone, auto-detects all 5 layouts)
│   └── Run `python -m tools.plugin_sync ingest --source-type git --repo-url <url> [--ref ...] [--subdir ...] [--git-token-env ENV] --dry-run`
│       (or `make demo-sync-repo` — pass `SKILLS_REPO_SUBDIR=""` to auto-detect from repo root)
│       -> See references/gitlab-ci-and-git-ingest-patterns.md
├── Infrastructure-as-Code provisioning (`terraform/core/`)
│   ├── MCP / A2A / Endpoint Services -> `google_agent_registry_service` (`terraform/core/agent_registry_services.tf`)
│   └── Standalone Skills bootstrap -> `skills_source_mode = "local" | "git"` (`terraform/core/skills_bootstrap.tf`, always passes `--only-skills`)
│       -> See references/agent-registry-v1-api-and-schemas.md
├── Push-based GitLab CI Dual-SoT pipeline (`starter-marketplace/.gitlab-ci.yml`)
│   └── MR uploads `TARGET_STATE_DRAFT` (quarantined) -> semver tag `v*.*.*` promotes `TARGET_STATE_ACTIVE`
│       -> See references/gitlab-ci-and-git-ingest-patterns.md
├── Skill revision promotion, rollback, `ENABLED_BLOCKING` Finding triage, or lifecycle transitions
│   └── Use `gcloud agent-registry skills ...` / `findings list` / REST `v1` (`private-<skill-id>`)
│       -> See references/agent-registry-v1-api-and-schemas.md
└── Different domain? Switch to the matching sibling skill:
    ├── End-to-end lab roadmap / local-vs-live setup -> `agent-marketplace-workshop-guide`
    ├── Authoring/validating 5-IDE manifests & `SKILL.md` -> `polyglot-agent-plugin-authoring`
    ├── Middleware `/v1/*` & `/mcp`, `hub_sync`, ADK consumption -> `marketplace-middleware-and-ide-delivery`
    └── KMS CMEK/signing, IAP UAP, AuthProvider/Binding, Org Policy, Audit -> `agent-registry-security-and-prod-hardening`
```

## Critical Gotchas ("Do This, Not That")

| # | Do This | Not That | Why |
|---|---|---|---|
| **1** | Run `--dry-run` first against live GCP and ask for **explicit user confirmation** before promoting to `TARGET_STATE_ACTIVE` or transitioning to `TARGET_STATE_DECOMMISSIONED`. | Blindly mutating `agentregistry.googleapis.com/v1` or auto-decommissioning skills. | Dry-run validates ZIP limits (≤ 1 MB), 10 KB specs, and metadata locally; `TARGET_STATE_ACTIVE` exposes skills to developers/agents, and `DECOMMISSIONED` is permanent. |
| **2** | Address every created skill as **`private-<skill-id>`** on `GET`, `PATCH`, `DELETE`, IAM, and `/revisions` calls. | Calling `GET .../skills/<raw-skill-id>` after `POST .../skills?skillId=<id>`. | Agent Registry auto-assigns user skills to the `PRIVATE` publisher and prepends `private-`; omitting it returns `404 NOT_FOUND`. |
| **3** | Store provenance (`plugin_name`, `plugin_display_name`, `plugin_version`, `source_repo`, `git_commit`, `mcp_service_id`, `agent_service_id`, `kms_signature`, `kms_key_version`) in `SKILL.md` `frontmatter.metadata` (`map<string, string>`). | Sending a generic `labels` map on `Service`, `Skill`, `Agent`, `McpServer`, `Endpoint`, or `Binding`. | Agent Registry `v1` has **no** generic `labels` field and rejects it with `400 INVALID_ARGUMENT`. |
| **4** | Omit top-level `interfaces` when `agentSpec.type == "A2A_AGENT_CARD"` (put URLs in `agentSpec.content.supportedInterfaces`, ≤ 10 KB). | Setting both top-level `interfaces` and `A2A_AGENT_CARD`, or patching a Service's `oneof spec` type. | Agent Registry rejects non-empty top-level `interfaces` on `A2A_AGENT_CARD` with `400 INVALID_ARGUMENT`, and `oneof spec` `type` is immutable after creation. |
| **5** | Pass `--only-skills` (and `--skip-mcp-probe`) whenever `terraform/core/agent_registry_services.tf` owns Cloud Run `google_agent_registry_service` resources. | Running full `tools.plugin_sync sync` / `ingest` without `--only-skills` against a Terraform-managed live project. | Local `127.0.0.1` URLs in `starter-marketplace/` will clobber live Cloud Run Service endpoints (`skills_bootstrap.tf` enforces `--only-skills` automatically). |
| **6** | Pass private Git tokens **only** via `--git-token-env ENV_VAR_NAME` (or `skills_repo_token_env = "GITLAB_TOKEN"` in `.tfvars`). | Embedding raw tokens in `--repo-url`, CLI arguments, or `.tfvars`. | `--git-token-env` injects an origin-scoped ephemeral `GIT_CONFIG_*` `http.<origin>/.extraHeader`, keeps `.git/config` and logs clean, and strips URL credentials before stamping `source_repo`. |
| **7** | Follow `DEPRECATED` → `DECOMMISSIONED`, append `?force=true` on REST skill deletes with revisions, and create standalone skills in the target catalog project. | Jumping straight from `ACTIVE`/`DISABLED` to `DECOMMISSIONED`, calling bare `DELETE .../skills/private-<id>`, or expecting cross-project skill rollup. | `DECOMMISSIONED` is terminal/irreversible and only permitted from `DEPRECATED`; REST delete fails if revisions exist without `?force=true`; custom standalone skills do **not** roll up across management boundaries. |
| **8** | Pass `searchType` (`KEYWORD` \| `SEMANTIC`) + `filter: "state=ACTIVE AND defaultRevisionFindingCount=0"` **only** on `skills:search`. | Passing `searchType` to `agents:search` or `mcpServers:search`, or calling `endpoints:search`. | `agents:search` and `mcpServers:search` accept `{"searchString": "..."}` only; `endpoints` has no `:search` endpoint (use AIP-160 `?filter=...`). |

## Quick-Start Patterns

### 1. Option 1 — Local Skills & Starter Plugin Ingest (`--dry-run` → Local Mock vs. Live GCP)

```bash
# Always dry-run first to inspect deterministic ZIPs, metadata, and sync-plan.json
.venv/bin/python -m tools.plugin_sync ingest \
  --source-type local \
  --path starter-marketplace/plugins/gcp-finops-toolkit \
  --project acme-agent-governance --location us-central1 \
  --output-dir /tmp/finops-dryrun --skip-mcp-probe --dry-run

# Local Workshop Mode (requires `make run-mock` on :8090 and `make run-samples` on :8091/:8092)
make demo-sync-local

# Live GCP Production Mode (pass --only-skills so Terraform keeps owning Cloud Run Services!)
export GCP_PROJECT_ID="${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
export GCP_LOCATION="${GCP_LOCATION:-us-central1}"
export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"
export AGENT_REGISTRY_AUTH_TOKEN="$(gcloud auth print-access-token)"
.venv/bin/python -m tools.plugin_sync ingest \
  --source-type local --path local-skills --plugin-name local-ops-toolkit \
  --registry-url "${AGENT_REGISTRY_BASE_URL}" --project "${GCP_PROJECT_ID}" --location "${GCP_LOCATION}" \
  --target-state TARGET_STATE_DRAFT --scanning-policy ENABLED_BLOCKING \
  --only-skills --skip-mcp-probe
```

### 2. Option 2 — Centralized Git Repository Clone & Ingest (Auto-Detects All 5 Layouts)

```bash
# Dry-run any public or private GitLab/GitHub repo (pass token env var NAME, never the token value)
export GITLAB_TOKEN="<read-repository-token>"
.venv/bin/python -m tools.plugin_sync ingest \
  --source-type git \
  --repo-url "https://gitlab.example.com/ai-platform/agent-marketplace.git" \
  --ref main --subdir plugins --git-token-env GITLAB_TOKEN \
  --plugin-name central-repo-skills \
  --project "${GCP_PROJECT_ID:-acme-agent-governance}" --location "${GCP_LOCATION:-us-central1}" \
  --only-skills --skip-mcp-probe --dry-run

# Local Workshop Mode shortcut (set SKILLS_REPO_SUBDIR="" to auto-detect from repo root)
make demo-sync-repo \
  SKILLS_REPO_URL="https://gitlab.example.com/ai-platform/agent-marketplace.git" \
  SKILLS_REPO_REF="main" SKILLS_REPO_SUBDIR="" SKILLS_REPO_TOKEN_ENV="GITLAB_TOKEN"
```

### 3. Terraform Core (`terraform/core/`) Dual-Source Bootstrap & Services

Configure `terraform/core/terraform.tfvars` (see `terraform/core/terraform.tfvars.example`), then preview with `make tf-core-plan`:

```hcl
# Option 1 (default): starter plugin + local-skills/  |  Option 2: skills_source_mode = "git"
skills_source_mode             = "local"          # or "git"
enable_skills_bootstrap        = true             # false if GitLab CI exclusively publishes skills
enable_local_standalone_skills = true             # local mode only
local_skills_plugin_name       = "local-ops-toolkit"
# For skills_source_mode = "git": set skills_repo_url, skills_repo_ref, skills_repo_subdir,
# skills_repo_token_env = "GITLAB_TOKEN" (env var NAME only!), skills_repo_plugin_name
```

`terraform/core/agent_registry_services.tf` owns `google_agent_registry_service` (`finops-mcp-server`, `finops-advisor-agent`, `finops-billing-api`) and resolves projected IDs via `basename(...registry_resource)`; `terraform/core/skills_bootstrap.tf` always passes `--only-skills --skip-mcp-probe`.

### 4. `gcloud` Revision Promotion, Rollback & `Finding` Triage (Live GCP)

```bash
# 1. Inspect scan state and Findings (Finding list/describe requires roles/agentregistry.admin)
gcloud agent-registry skills revisions describe REV_ID \
  --skill=private-cost-anomaly-triage --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
gcloud agent-registry findings list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" \
  --filter="resource=\"projects/${GCP_PROJECT_ID}/locations/${GCP_LOCATION}/skills/private-cost-anomaly-triage/revisions/REV_ID\""

# 2. Atomic promotion or rollback via --default-revision (confirm with user before activating!)
gcloud agent-registry skills update private-cost-anomaly-triage \
  --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" \
  --default-revision="projects/${GCP_PROJECT_ID}/locations/${GCP_LOCATION}/skills/private-cost-anomaly-triage/revisions/REV_ID" \
  --target-state=ACTIVE

# 3. Emergency kill switch (reversible) vs. controlled retirement (DEPRECATED -> terminal DECOMMISSIONED)
gcloud agent-registry skills update private-cost-anomaly-triage \
  --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" --target-state=DISABLED
```

### 5. Push-Based GitLab CI Dual-SoT (`starter-marketplace/.gitlab-ci.yml`)

GitLab owns authoring/review; Agent Registry owns immutable runtime state. `starter-marketplace/.gitlab-ci.yml` runs 3 stages (`validate` → `package-and-sign` → `register-registry`), uploading `TARGET_STATE_DRAFT` on Merge Requests (`register:draft-mr`) and promoting `TARGET_STATE_ACTIVE` on semver tags `^v[0-9]+\.[0-9]+\.[0-9]+` (`register:active-release`) using keyless GitLab OIDC → GCP WIF (`GCP_OIDC_TOKEN`). Simulate locally against `mock_registry` (`:8090`):

```bash
curl -fsSL -X POST http://127.0.0.1:8090/_mock/reset
.venv/bin/python -m tools.plugin_sync sync \
  --plugin-dir starter-marketplace/plugins/gcp-finops-toolkit \
  --registry-url http://127.0.0.1:8090/v1 --project acme-agent-governance --location us-central1 \
  --target-state TARGET_STATE_DRAFT --scanning-policy ENABLED_BLOCKING --skip-mcp-probe
# Promote to ACTIVE after review/scan confirmation by re-running with --target-state TARGET_STATE_ACTIVE
```

## Verification Checklist

Run from repository root and confirm every check passes before declaring completion:

```bash
# 1. Automated sync, dual-source (all 5 layouts + Git security), and Terraform Core tests
.venv/bin/pytest -v tests/test_track1a_sync_and_samples.py tests/test_dual_skill_sources.py tests/test_track1e_terraform_core.py
make validate-hcl

# 2. Local Mock Mode verification (when mock_registry is running on :8090)
BASE="http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/us-central1"
curl -fsSL "${BASE}/skills" | .venv/bin/python -m json.tool      # private-* skills, state=STATE_ACTIVE, defaultRevisionFindingCount=0
curl -fsSL "${BASE}/mcpServers" | .venv/bin/python -m json.tool  # finops-mcp-server with 3 annotated tools
curl -fsSL "${BASE}/agents" | .venv/bin/python -m json.tool      # finops-advisor-agent with projected interfaces

# 3. Live GCP Mode verification
gcloud agent-registry skills list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
gcloud agent-registry mcp-servers list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
gcloud agent-registry agents list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
```

## Reference Guides

- [references/agent-registry-v1-api-and-schemas.md](references/agent-registry-v1-api-and-schemas.md) — Complete `v1` REST API endpoints, `tools.plugin_sync` CLI flags, `gcloud agent-registry` workflows, GA `hashicorp/google >= 7.39.0` Terraform schemas, Skill lifecycle state machine, `ENABLED_BLOCKING` scanning & `Finding` triage, IAM roles, and quotas/limits.
- [references/gitlab-ci-and-git-ingest-patterns.md](references/gitlab-ci-and-git-ingest-patterns.md) — Dual-Role SoT architecture, all 5 `discover_ingest_targets` repository layouts, `--subdir` containment & Git token security, `--per-skill-plugin` vs. `--plugin-name` bundling, and standalone GitLab CI migration variables (`WORKSHOP_TOOLS_PACKAGE`, `PLUGINS_DIR`, `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT_EMAIL`).
