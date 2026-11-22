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
GCP_LOCATION="asia-southeast3" \
REQUIRE_KMS_SIGNATURE="false" \
.venv/bin/python -m platform_agent.adk_orchestrator \
  --mode rest \
  --skill-query "cost anomaly" \
  --mcp-query "finops" \
  --agent-query "finops" \
  --source-agent-urn "urn:agent:projects-123456789012:projects:123456789012:locations:asia-southeast3:agentregistry:services:finops-advisor-agent"
```

Examine the JSON output:
- `skills`: `private-cost-anomaly-triage` matched via `POST .../skills:search` (`searchType: "SEMANTIC"`) and extracted safely in memory after `?alt=media` 302 redirect header stripping and `sha256Hash` comparison.
- `mcp_servers`: `finops-mcp-server` matched via `POST .../mcpServers:search` (keyword/prefix only; remember `mcpServers:search` and `agents:search` do **not** accept `searchType`!).
- `agents`: `finops-advisor-agent` matched via `POST .../agents:search`.

### Step 3: Run Live ADK Mode (Mode B: Live GCP Mode)

On a workstation or Vertex AI Agent Engine runtime with `google-adk >= 1.29.0`, remember the **verified live regional split**: `AgentRegistry` discovers regional `McpServer` and `Agent` Services in **Bangkok (`asia-southeast3`)** (or **Singapore `asia-southeast1`**), while `GCPSkillRegistry` / `PlatformAgentConsumer` reads standalone **`Skills`** from **`global`** (`AGENT_REGISTRY_SKILLS_LOCATION=global`, or `us`/`eu` for CMEK) and automatically filters to enterprise `private-*` skills:

```bash
# Install ADK with A2A, Agent Identity, and MCP extras (see platform_agent/requirements.txt)
.venv/bin/pip install -r platform_agent/requirements.txt

# Export BOTH GCP_* and GOOGLE_CLOUD_* vars (adk_orchestrator.py gives precedence to
# GCP_PROJECT_ID, GCP_LOCATION, AGENT_REGISTRY_SKILLS_LOCATION, and AGENT_REGISTRY_BASE_URL
# over GOOGLE_CLOUD_* / AGENT_REGISTRY_ENDPOINT so any local-mock env vars are overridden!)
export GCP_PROJECT_ID="<your-gcp-project-id>"
export GCP_LOCATION="asia-southeast3"
export AGENT_REGISTRY_SKILLS_LOCATION="global"
export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"
export GOOGLE_CLOUD_PROJECT="${GCP_PROJECT_ID}"
export GOOGLE_CLOUD_LOCATION="${GCP_LOCATION}"
export AGENT_REGISTRY_ENDPOINT="${AGENT_REGISTRY_BASE_URL}"

# Construct the live ADK LlmAgent wired to regional AgentRegistry + global GCPSkillRegistry + SkillToolset
.venv/bin/python -m platform_agent.adk_orchestrator \
  --mode adk \
  --project-id "${GCP_PROJECT_ID}" \
  --location "${GCP_LOCATION}" \
  --skills-location "${AGENT_REGISTRY_SKILLS_LOCATION}" \
  --base-url "${AGENT_REGISTRY_BASE_URL}"
```

> **Remember the Project-Scoping & Regional Split Rules:** Standalone `Skill` resources in live Agent Registry `v1` are strictly project-scoped (they do **not** roll up across folder/organization management boundaries) and live in **`global`** (APAC default) or **`us`/`eu`** (CMEK). Point `GCPSkillRegistry(project_id=..., location="global")` to your governed catalog project while keeping `AgentRegistry(project_id=..., location="asia-southeast3")` regional.

---

## Part 2: GitLab Authoring SoT vs. Agent Registry Runtime SoT

Review [starter-marketplace/.gitlab-ci.yml](../../starter-marketplace/.gitlab-ci.yml). Notice how the **Dual-Role Split** separates code review from runtime publication across three CI stages:

| GitLab CI Stage | Job(s) | Trigger | What It Enforces |
| :--- | :--- | :--- | :--- |
| **1. `validate`** | `validate:schemas`, `validate:antigravity-cli` | MR, `main`, Tags | Validates every plugin under `plugins/` against all 5 IDE schemas + runs `agy plugin validate --json` with the multi-tool `jq` warning filter. |
| **2. `package-and-sign`** | `package-and-sign` | MR, `main`, Tags | Runs `python -m tools.plugin_sync sync --dry-run` to build deterministic per-skill ZIPs, inject `source_repo` & `git_commit`, and sign with Cloud KMS (`--kms-key-version`) or PEM (`--sign-pem`). |
| **3. `register-registry`** | `register:draft-mr` | Merge Requests | Uploads skills with `--target-state TARGET_STATE_DRAFT --scanning-policy ENABLED_BLOCKING`. Skills are **quarantined** while security scans run—developers never see unreviewed MR changes. |
| **3. `register-registry`** | `register:active-release` | Semver Tags (`v*.*.*`) or manual on `main` | Promotes clean, signed revisions to `--target-state TARGET_STATE_ACTIVE`. |

### Pull-Based Option 2 (`ingest --source-type git`) vs. Push-Based GitLab CI (`.gitlab-ci.yml`)

These two mechanisms complement each other cleanly across a team's adoption lifecycle:
- **Pull-based Option 2 (`python -m tools.plugin_sync ingest --source-type git` / `make demo-sync-repo` / Terraform `skills_source_mode = "git"`):** Ideal for Day-0 onboarding of an existing centralized GitLab or GitHub skills/plugins repository, Terraform environment bootstrapping, or scheduled catalog ingestion across repositories that have not yet adopted `.gitlab-ci.yml`. It auto-detects `agent-plugins.org` multi-plugin repos, standalone `skills/*/SKILL.md` repos, and hybrid layouts while recording exact sanitized `source_repo` and 40-char `git_commit` provenance.
- **Push-based GitLab CI ([starter-marketplace/.gitlab-ci.yml](../../starter-marketplace/.gitlab-ci.yml)):** Target-state production governance triggered directly on Merge Requests and semver tags—enforcing 5-IDE schema + `agy plugin validate` gates, `TARGET_STATE_DRAFT` MR quarantine, keyless GCP Workload Identity Federation OIDC exchange, and Cloud KMS ECDSA P-256 signing before any release enters `TARGET_STATE_ACTIVE`.

### Migrating `starter-marketplace/.gitlab-ci.yml` to a Standalone GitLab Repository

[starter-marketplace/.gitlab-ci.yml](../../starter-marketplace/.gitlab-ci.yml) runs cleanly both inside this workshop monorepo and when copied into a standalone enterprise GitLab repository rooted at `starter-marketplace/`. Configure these four CI/CD variables when migrating to a standalone GitLab project:

| GitLab CI Variable | Purpose | Example Value |
| :--- | :--- | :--- |
| `WORKSHOP_TOOLS_PACKAGE` | Pip spec, Artifact Registry Python wheel URL, or `git+https://` URL installing `schemas` and `tools.plugin_sync` when `pyproject.toml`/`tools/` are not in the standalone repo | `git+https://github.com/jiratouchmhp/agent-registry-marketplace-workshop.git@main` (or private Artifact Registry wheel) |
| `PLUGINS_DIR` | Override the directory containing plugin folders; leave empty to auto-detect `plugins` (standalone repo) or `starter-marketplace/plugins` (monorepo) | `plugins` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full GCP Workload Identity Provider resource path used for keyless GitLab OIDC (`GCP_OIDC_TOKEN`) → STS token exchange (zero static JSON keys!) | `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/gitlab-pool/providers/gitlab-oidc` |
| `GCP_SERVICE_ACCOUNT_EMAIL` | Least-privilege CI publisher service account impersonated via `iamcredentials.googleapis.com` (`roles/agentregistry.editor` + `roles/cloudkms.signerVerifier`) | `agent-registry-ci-publisher@<PROJECT_ID>.iam.gserviceaccount.com` |

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
  --location asia-southeast3 \
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
  --location asia-southeast3 \
  --target-state TARGET_STATE_ACTIVE \
  --scanning-policy ENABLED_BLOCKING \
  --source-repo https://gitlab.acme.example.com/platform/starter-marketplace \
  --git-commit "v1.0.0-release-sha" \
  --skip-mcp-probe

# 5. Verify Middleware now serves gcp-finops-toolkit immediately!
curl -fsSL http://127.0.0.1:8080/v1/catalog | .venv/bin/python -m json.tool
```

> **Continuing Locally to Workstream B (Labs 06–08)?** Because step 1 called `POST /_mock/reset` and only re-synced `gcp-finops-toolkit`, `local-ops-toolkit` and `finops-billing-api` were cleared from local `mock_registry` memory. Run `make demo-sync-local` to restore the full multi-plugin catalog before continuing locally.

---

## Part 3: Workstream A Live GCP Validation & Cleanup Checkpoint

If you deployed Workstream A to Google Cloud in Lab 03 (**Bangkok `asia-southeast3`** primary and optional **Singapore `asia-southeast1`** secondary), run the automated end-to-end live verification and — if you are stopping after Workstream A or want to avoid idle cloud charges — tear down all workshop Cloud Run, Agent Registry, and Artifact Registry resources cleanly:

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"

# 1. Validate live Cloud Run services, Agent Registry v1 skills/services, and 4-IDE feeds
#    (ENABLE_SECONDARY defaults to false for Bangkok-only; pass ENABLE_SECONDARY=true if Singapore was deployed)
make gcp-validate GCP_PROJECT_ID="${GCP_PROJECT_ID}"
# Multi-region Bangkok + Singapore validation (includes live hub_sync failover drill):
# make gcp-validate GCP_PROJECT_ID="${GCP_PROJECT_ID}" ENABLE_SECONDARY=true
# Direct script equivalent:
# scripts/validate_live_gcp.sh --project "${GCP_PROJECT_ID}" --primary-region asia-southeast3 --enable-secondary

# 2. Tear down Live GCP Workstream A resources when finished (skip if continuing straight to live Workstream B!)
# Track 2 (gcloud + Cloud Build):
make gcp-destroy GCP_PROJECT_ID="${GCP_PROJECT_ID}"
# Multi-region Bangkok + Singapore teardown:
# make gcp-destroy GCP_PROJECT_ID="${GCP_PROJECT_ID}" ENABLE_SECONDARY=true
# Direct script equivalent (add --delete-source-deploy-repo if you tested --deploy-mode source in a dedicated sandbox):
# scripts/destroy_gcp.sh --project "${GCP_PROJECT_ID}" --primary-region asia-southeast3 --enable-secondary

# Track 3 (Terraform): set force_destroy_staging_bucket = true in sandbox .tfvars if objects exist
# terraform -chdir=terraform/core destroy
```

> **Note on Deleting Skills with Revisions & Build Side Effects:**
> - `gcloud agent-registry skills delete` does **not** expose a `--force` flag. `scripts/destroy_gcp.sh` automatically calls REST `DELETE https://agentregistry.googleapis.com/v1/projects/{p}/locations/${AGENT_REGISTRY_SKILLS_LOCATION:-global}/skills/{id}?force=true` with `Authorization` and `x-goog-user-project` headers so skills with revisions in `global` (or `us`/`eu`) are removed cleanly.
> - If you tested `scripts/deploy_gcp.sh --deploy-mode source`, Cloud Run auto-creates an Artifact Registry repository named `cloud-run-source-deploy` in each deployed region. Pass `--delete-source-deploy-repo` to `scripts/destroy_gcp.sh` **only** in a dedicated single-purpose sandbox (never in a shared project!).
> - Regional Cloud Build user-owned staging buckets (`--default-buckets-behavior=regional-user-owned-bucket`) are retained by default to avoid disrupting other builds in the project; inspect with `gcloud storage buckets list --project="${GCP_PROJECT_ID}" --limit=50 --quiet`.

> **APAC Regional Split Reminder (Transitioning to Workstream B):** As detailed in the [Attendee Quickstart](../attendee-quickstart.md), **Bangkok (`asia-southeast3`)** primary and **Singapore (`asia-southeast1`)** secondary host Cloud Run, Cloud Build, Artifact Registry, Agent Registry **`Services`** (`McpServer`, `Agent`, `Endpoint`) & `Bindings`, and Cloud KMS `EC_SIGN_P256_SHA256` asymmetric signing (Lab 07), while standalone **`Skills`** live in **`global`** (or `us`/`eu` for native CMEK `SkillEncryptionSpec`). For Lab 08 in APAC, place `AuthProvider` in **Singapore (`asia-southeast1`)** (`auth_provider_location = "asia-southeast1"`) and keep `enable_agent_gateway = false` unless deploying Agent Gateway + IAP v2 UAP in `us-central1`/`europe-west1`.

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
| `401 CREDENTIALS_MISSING` downloading `?alt=media` in Live GCP Mode | Stripped `Authorization` on live Agent Registry's same-origin `302` → `/download/v1/...?alt=media` redirect | `platform_agent/rest_consumer.py` preserves `Authorization` strictly on same-origin `/download/...` redirects without signed-URL tokens while stripping `Authorization` and `x-goog-*` on cross-origin and signed-URL redirects. |
| Live `skills:search` or `list_skills` returns 404/empty in `asia-southeast3` | Standalone Skills are hosted in `global` (or `us`/`eu`), not single regions | Pass `--skills-location global` (or export `AGENT_REGISTRY_SKILLS_LOCATION=global`); `platform_agent` auto-defaults to `global` on `googleapis.com` and filters strictly to `private-*` enterprise skills. |

---

**Workstream A Complete!** Continue to the standalone Security & Governance Track: [Lab 06: Governance, Scanning & Skill Lifecycle →](06-governance-scanning-lifecycle.md)
