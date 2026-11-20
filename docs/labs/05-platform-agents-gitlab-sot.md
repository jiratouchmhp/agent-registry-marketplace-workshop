# Lab 05: Platform Agents (Google ADK) & GitLab Dual-Role Source of Truth

**Workstream:** A — Core Platform & Middleware  
**Duration:** 40 minutes  
**Prerequisites:** Completed [Lab 02](02-register-agent-registry.md) (mock or live Agent Registry populated with `gcp-finops-toolkit` skills & services).

---

## Learning Objectives

By the end of this lab, you will:
1. Run the dual-mode **Platform Agent Orchestrator** ([platform_agent/adk_orchestrator.py](../../platform_agent/adk_orchestrator.py)) that connects **directly** to Google Cloud Agent Registry `v1`—bypassing IDE marketplace wrappers—using:
   - `AgentRegistry` (`list_agents`, `search_agents`, `get_remote_a2a_agent`, `list_mcp_servers`, `search_mcp_servers`, `get_mcp_toolset`)
   - `GCPSkillRegistry` + `SkillToolset` (`skills:search` with `SEMANTIC` + `state=ACTIVE AND defaultRevisionFindingCount=0` + `?alt=media` 302 header-stripping download)
   - `CredentialManager.register_auth_provider(GcpAuthProvider())` for automatic `Binding` credential resolution
2. Inspect and walk through the **GitLab Authoring SoT → Agent Registry Runtime SoT** dual-role split in [starter-marketplace/.gitlab-ci.yml](../../starter-marketplace/.gitlab-ci.yml).
3. Simulate the exact 3-stage GitLab CI workflow locally (MR `TARGET_STATE_DRAFT` upload vs. release tag `TARGET_STATE_ACTIVE` promotion).

---

## Part 1: Direct Platform Agent Consumption (ADK & REST `v1`)

Developer IDEs require plugin ZIPs and marketplace feeds from the Polyglot Middleware. Headless **Platform Agents** running on **Vertex AI Agent Engine** or **Cloud Run** consume Agent Registry `v1` **directly** with least-privilege runtime roles (`roles/agentregistry.viewer` + `roles/agentidentity.user` + `roles/iap.egressor`—**never** `editor` or `admin`).

### Step 1: Inspect the Declarative ADK Wiring

Print the declarative ADK configuration (works offline with zero `google-adk` dependency):

```bash
.venv/bin/python -m platform_agent.adk_orchestrator --mode adk --print-config
```

Verify the printed JSON confirms:
- `auth_manager.registration_call`: `"CredentialManager.register_auth_provider(GcpAuthProvider())"`
- `auth_manager.required_iam_roles`: `["roles/agentregistry.viewer", "roles/agentidentity.user"]` (IAP runtime egress is recorded under `governance.egress_policy`: `"Agent Gateway + IAP v2 UAP (iap.googleapis.com/resources.egressViaIAP)"`)
- `governance.skill_filter` and `integrations.skill_registry.filter`: `"state=ACTIVE AND defaultRevisionFindingCount=0"`
- `governance.disallowed_runtime_roles`: `["roles/agentregistry.editor", "roles/agentregistry.admin"]`

### Step 2: Run Live Capability Discovery (Mode A: Local Mock Mode)

With `make run-mock` (`:8090`) and `make demo-sync` active, run `PlatformAgentOrchestrator` in `rest` mode to execute semantic skill search, 2-step `?alt=media` 302 download + SHA-256 verification, keyword MCP/A2A discovery, and `bindings:fetchAvailable`:

```bash
AGENT_REGISTRY_BASE_URL="http://127.0.0.1:8090/v1" \
GCP_PROJECT_ID="acme-agent-governance" \
GCP_LOCATION="us-central1" \
REQUIRE_KMS_SIGNATURE="false" \
.venv/bin/python -m platform_agent.adk_orchestrator \
  --mode rest \
  --skill-query "cost anomaly" \
  --mcp-query "finops" \
  --agent-query "finops" \
  --source-agent-urn "urn:agent:projects-123456789012:projects:123456789012:locations:us-central1:agentregistry:services:finops-advisor-agent"
```

Examine the JSON output:
- `skills`: `private-cost-anomaly-triage` matched via `POST .../skills:search` (`searchType: "SEMANTIC"`) and extracted safely in memory after `?alt=media` 302 redirect header stripping and `sha256Hash` comparison.
- `mcp_servers`: `finops-mcp-server` matched via `POST .../mcpServers:search` (keyword/prefix only; remember `mcpServers:search` and `agents:search` do **not** accept `searchType`!).
- `agents`: `finops-advisor-agent` matched via `POST .../agents:search`.

### Step 3: Run Live ADK Mode (Mode B: Live GCP Mode)

On a workstation or Vertex AI Agent Engine runtime with `google-adk >= 1.29.0`:

```bash
# Install ADK with A2A, Agent Identity, and MCP extras (see platform_agent/requirements.txt)
.venv/bin/pip install -r platform_agent/requirements.txt

# Export BOTH GCP_* and GOOGLE_CLOUD_* vars (adk_orchestrator.py gives precedence to
# GCP_PROJECT_ID, GCP_LOCATION, and AGENT_REGISTRY_BASE_URL over GOOGLE_CLOUD_* /
# AGENT_REGISTRY_ENDPOINT so any local-mock env vars from earlier labs must be overridden!)
export GCP_PROJECT_ID="<your-gcp-project-id>"
export GCP_LOCATION="us-central1"
export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"
export GOOGLE_CLOUD_PROJECT="${GCP_PROJECT_ID}"
export GOOGLE_CLOUD_LOCATION="${GCP_LOCATION}"
export AGENT_REGISTRY_ENDPOINT="${AGENT_REGISTRY_BASE_URL}"

# Construct the live ADK LlmAgent wired to AgentRegistry + GCPSkillRegistry + SkillToolset
.venv/bin/python -m platform_agent.adk_orchestrator \
  --mode adk \
  --project-id "${GCP_PROJECT_ID}" \
  --location "${GCP_LOCATION}" \
  --base-url "${AGENT_REGISTRY_BASE_URL}"
```

> **Remember the Project-Scoping Rule:** Standalone `Skill` resources in Agent Registry `v1` are strictly scoped to the project where they were created and do **not** roll up across folder/organization management boundaries. Point `GCPSkillRegistry(project_id=...)` to the governed catalog project hosting your approved skills.

---

## Part 2: GitLab Authoring SoT vs. Agent Registry Runtime SoT

Review [starter-marketplace/.gitlab-ci.yml](../../starter-marketplace/.gitlab-ci.yml). Notice how the **Dual-Role Split** separates code review from runtime publication across three CI stages:

| GitLab CI Stage | Job(s) | Trigger | What It Enforces |
| :--- | :--- | :--- | :--- |
| **1. `validate`** | `validate:schemas`, `validate:antigravity-cli` | MR, `main`, Tags | Validates every plugin under `plugins/` against all 5 IDE schemas + runs `agy plugin validate --json` with the multi-tool `jq` warning filter. |
| **2. `package-and-sign`** | `package-and-sign` | MR, `main`, Tags | Runs `python -m tools.plugin_sync sync --dry-run` to build deterministic per-skill ZIPs, inject `source_repo` & `git_commit`, and sign with Cloud KMS (`--kms-key-version`) or PEM (`--sign-pem`). |
| **3. `register-registry`** | `register:draft-mr` | Merge Requests | Uploads skills with `--target-state TARGET_STATE_DRAFT --scanning-policy ENABLED_BLOCKING`. Skills are **quarantined** while security scans run—developers never see unreviewed MR changes. |
| **3. `register-registry`** | `register:active-release` | Semver Tags (`v*.*.*`) or manual on `main` | Promotes clean, signed revisions to `--target-state TARGET_STATE_ACTIVE`. |

### Step 4: Simulate the MR (`DRAFT`) → Release Tag (`ACTIVE`) Promotion Locally

Test the exact commands from [starter-marketplace/.gitlab-ci.yml](../../starter-marketplace/.gitlab-ci.yml) against `mock_registry` (or live GCP by setting `AGENT_REGISTRY_BASE_URL` and `AGENT_REGISTRY_AUTH_TOKEN`):

```bash
# 1. Reset mock registry to a clean slate (Local Mock Mode only)
curl -fsSL -X POST http://127.0.0.1:8090/_mock/reset

# 2. Simulate Merge Request job (register:draft-mr) -> uploads in TARGET_STATE_DRAFT
.venv/bin/python -m tools.plugin_sync sync \
  --plugin-dir starter-marketplace/plugins/gcp-finops-toolkit \
  --registry-url http://127.0.0.1:8090/v1 \
  --project acme-agent-governance \
  --location us-central1 \
  --target-state TARGET_STATE_DRAFT \
  --scanning-policy ENABLED_BLOCKING \
  --source-repo https://gitlab.acme.example.com/platform/starter-marketplace \
  --git-commit "mr-preview-sha1234" \
  --skip-mcp-probe

# 3. Verify Middleware hides DRAFT skills from developers!
curl -fsSL http://127.0.0.1:8080/v1/catalog | .venv/bin/python -m json.tool
# Observe: plugins=[] and both skills appear in rejectedSkills (STATE_DRAFT)!

# 4. Simulate Tagged Release job (register:active-release) -> promotes to TARGET_STATE_ACTIVE
.venv/bin/python -m tools.plugin_sync sync \
  --plugin-dir starter-marketplace/plugins/gcp-finops-toolkit \
  --registry-url http://127.0.0.1:8090/v1 \
  --project acme-agent-governance \
  --location us-central1 \
  --target-state TARGET_STATE_ACTIVE \
  --scanning-policy ENABLED_BLOCKING \
  --source-repo https://gitlab.acme.example.com/platform/starter-marketplace \
  --git-commit "v1.0.0-release-sha" \
  --skip-mcp-probe

# 5. Verify Middleware now serves gcp-finops-toolkit immediately!
curl -fsSL http://127.0.0.1:8080/v1/catalog | .venv/bin/python -m json.tool
```

---

## Verification Checkpoint

- [ ] `python -m platform_agent.adk_orchestrator --mode adk --print-config` prints valid ADK wiring.
- [ ] `platform_agent.adk_orchestrator --mode rest` discovers and extracts `private-cost-anomaly-triage` after `?alt=media` 302 redirect header stripping.
- [ ] Confirmed `TARGET_STATE_DRAFT` skills from MR uploads are quarantined and hidden from `/v1/catalog` until promoted to `TARGET_STATE_ACTIVE`.
- [ ] `.venv/bin/pytest -v tests/test_track1d_platform_agent.py` and `make test-e2e` pass completely.

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `SkillSignatureError` when running `adk_orchestrator --mode rest` | `REQUIRE_KMS_SIGNATURE` defaults to `"false"` in `adk_orchestrator.py` and `rest_consumer.py`; `SkillSignatureError` is raised when `REQUIRE_KMS_SIGNATURE=true` is enabled in the environment without `KMS_PUBLIC_KEY_PEM_PATH` (fail-closed) or when a downloaded skill ZIP is unsigned/tampered | Unset `REQUIRE_KMS_SIGNATURE` (or set `REQUIRE_KMS_SIGNATURE=false`) for unsigned Workstream A runs, or supply `KMS_PUBLIC_KEY_PEM_PATH` pointing to the trusted ECDSA P-256 public key PEM after completing [Lab 07](07-kms-encryption-and-signing.md). |
| `RuntimeError: Live ADK mode requires google-adk>=1.29.0` | Running `--mode adk` without installing optional `platform_agent/requirements.txt` | Install `pip install -r platform_agent/requirements.txt` or use `--mode rest` / `--print-config`. |
| `400 INVALID_ARGUMENT` on `agents:search` | Passing `searchType` to `agents:search` or `mcpServers:search` | Only `skills:search` accepts `searchType` (`KEYWORD`\|`SEMANTIC`); `agents:search` and `mcpServers:search` accept `{"searchString": "..."}` only. |

---

**Workstream A Complete!** Continue to the standalone Security & Governance Track: [Lab 06: Governance, Scanning & Skill Lifecycle →](06-governance-scanning-lifecycle.md)
