# Lab 02: Decompose & Register Assets in Google Cloud Agent Registry (`v1`)

**Workstream:** A — Core Platform & Middleware  
**Duration:** 35 minutes  
**Prerequisites:** Completed [Lab 00](00-environment-setup.md) and [Lab 01](01-author-agent-plugin.md).

---

## Learning Objectives

By the end of this lab, you will:
1. Understand how `tools.plugin_sync` decomposes an `agent-plugins.org` bundle—or ingests standalone `agentskills.io` directories—into Google Cloud Agent Registry `v1` primitives:
   - Standalone **`Skill`** resources (`agentskills.io` ZIP ≤ 1 MB, strictly one uppercase `SKILL.md`, `private-` auto-prefixing).
   - **`Service`** resources projecting read-only **`McpServer`** (`TOOL_SPEC`), **`Agent`** (`A2A_AGENT_CARD` with **empty** top-level `interfaces`), and **`Endpoint`** (`NO_SPEC`).
2. Execute **both first-class skill ingestion paths** via `python -m tools.plugin_sync ingest`:
   - **Option 1 — Local Skills Path**: Ingest [local-skills/](../../local-skills/README.md) (`local-ops-toolkit`) + `starter-marketplace/plugins/gcp-finops-toolkit` directly from disk (`make demo-sync-local`).
   - **Option 2 — Centralized Git Repository Path**: Shallow-clone and ingest a centralized GitLab/GitHub (or offline local test) repository (`make demo-sync-repo` / `--source-type git`) with automatic layout detection and exact 40-char `git_commit` provenance.
3. Configure Terraform's dual-source `skills_source_mode = "local" | "git"` bootstrapper in [terraform/core/skills_bootstrap.tf](../../terraform/core/skills_bootstrap.tf) and complete the optional coding exercise in [starter/lab02_plugin_sync_exercise.py](../../starter/lab02_plugin_sync_exercise.py).

---

## Key Agent Registry `v1` Invariants (Read Before Running)

1. **Verified Bangkok-first regional placement (`Services` vs. standalone `Skills`):** On live Google Cloud Agent Registry `v1`, all regional **`Services`** (`McpServer`, `Agent`, `Endpoint`) run in a single **Bangkok (`asia-southeast3`)** primary deployment, while standalone **`Skills`** (`Skill`, `SkillRevision`, `Findings`) are hosted **only** in **`global`** (default for APAC — `AGENT_REGISTRY_SKILLS_LOCATION=global` / `--skills-location global`) or **`us` / `eu`** (required for CMEK; single-region `/skills` endpoints time out), and auto-registered Google/GCP remote MCP servers live in **`global`**. *(Singapore `asia-southeast1` is reserved exclusively as the service-specific fallback for Workstream B Agent Identity `AuthProvider`, which is not yet available in Bangkok.)* Middleware (`INCLUDE_PUBLIC_SKILLS=false`) automatically filters strictly to `private-*` skills so Google's built-in first-party skills do not pollute enterprise catalogs. *(Local `mock_registry` accepts regional `asia-southeast3` for backward-compatible offline drills.)*
2. **No generic `labels` map** exists on `Service`, `Agent`, `McpServer`, `Endpoint`, `Binding`, or `Skill` in `v1`. Plugin lineage is stored inside `SKILL.md` YAML `frontmatter.metadata` (`map<string, string>` scalar strings only): `plugin_name`, `plugin_display_name`, `plugin_version`, `source_repo`, `git_commit`, `mcp_service_id`, `agent_service_id`, `kms_signature`, `kms_key_version`.
3. **Automatic `private-` Skill ID prefix**: Calling `POST /v1/projects/{p}/locations/{skills_loc}/skills?skillId=cost-anomaly-triage` creates `projects/{p}/locations/{skills_loc}/skills/private-cost-anomaly-triage`. Every subsequent `GET`, `PATCH`, `DELETE`, and `/revisions` call **must** use `private-cost-anomaly-triage`!
4. **A2A v1.0 `A2A_AGENT_CARD` strict validation rules**:
   - Top-level `interfaces` on the `Service` **must be empty**—endpoints are declared exclusively inside `agentSpec.content.supportedInterfaces` (≤ 10 KB).
   - **No legacy A2A v0.x `capabilities.stateTransitionHistory`**: Live Agent Registry rejects it with `400 INVALID_ARGUMENT` (`proto unmarshal failed: unknown field "stateTransitionHistory"`). Use only A2A v1.0 capabilities (`streaming`, `pushNotifications`).
   - **Unique `supportedInterfaces` URLs**: Live Agent Registry rejects duplicate URLs across bindings with `400 INVALID_ARGUMENT` (`duplicate interface url found in agent card`). Split `JSONRPC` onto `/a2a` and `HTTP+JSON` onto `/a2a/http` (both served by `samples/a2a-agent/app.py`, and enforced locally in `mock_registry/app.py` and `tools/plugin_sync/sync.py`).
5. **Immutable `oneof spec` type**: Once a `Service` is created as `mcpServerSpec`, `agentSpec`, or `endpointSpec`, its spec type cannot be mutated via `PATCH`.

---

## Step 1: Dry-Run Decomposition & Inspect the Sync Plan

Run `tools.plugin_sync` in `--dry-run` mode to package deterministic per-skill ZIPs and inspect the exact `v1` payloads without mutating any API:

```bash
.venv/bin/python -m tools.plugin_sync sync \
  --plugin-dir starter-marketplace/plugins/gcp-finops-toolkit \
  --project acme-agent-governance \
  --location asia-southeast3 \
  --target-state TARGET_STATE_ACTIVE \
  --scanning-policy ENABLED_BLOCKING \
  --output-dir /tmp/finops-sync-dryrun \
  --skip-mcp-probe \
  --dry-run
```

Inspect the generated deterministic skill ZIPs and `sync-plan.json`:

```bash
ls -la /tmp/finops-sync-dryrun/
.venv/bin/python -m zipfile -l /tmp/finops-sync-dryrun/cost-anomaly-triage.zip
```

Notice each archive contains **strictly one uppercase `SKILL.md`** and stays well under the 500 KB inline upload guideline (1 MB hard limit).

---

## Step 2: Register Skills, MCP Server, A2A Agent & Endpoint (Choose Option 1 or Option 2)

> **Already running `make run-local-stack` from Lab 00?** Skip starting individual servers below (ports `:8090`, `:8091`, `:8092`, and `:8080` are already active) and run your sync commands directly in a second terminal.

If you are using the 4-terminal interactive layout instead, ensure the Mock Registry (`:8090`) and Sample FinOps Workloads (`:8091` MCP server + `:8092` A2A agent) are running in two terminals before running **Mode A (Local Mock Mode)**:

```bash
# Terminal 1
make run-mock

# Terminal 2
make run-samples
```

> **Unified `ingest` Auto-Discovery:** `python -m tools.plugin_sync ingest` ([tools/plugin_sync/sources.py](../../tools/plugin_sync/sources.py)) automatically detects all 5 layouts—`single_plugin`, `multi_plugin`, `single_skill`, `standalone_skills`, and `hybrid`—for both **Option 1 (`--source-type local`)** and **Option 2 (`--source-type git`)**, while legacy `python -m tools.plugin_sync sync` and `make demo-sync` remain 100% backward compatible.

---

### Option 1: Local Skills Path (`local-skills/` + `starter-marketplace/`)

Use Option 1 for zero-friction classroom labs and offline environments—no GitLab/GitHub credentials required.

1. **One-command shortcut** (ingests both `starter-marketplace/plugins/gcp-finops-toolkit` AND `local-skills/` as `local-ops-toolkit` into Agent Registry, live-probing `http://127.0.0.1:8091/mcp` for `tools/list` annotations):
   ```bash
   make demo-sync-local
   ```
   *(To sync only `gcp-finops-toolkit` via the legacy `sync` subcommand, `make demo-sync` continues to work unchanged.)*

2. **Explicit `tools.plugin_sync ingest --source-type local` commands** (useful for inspecting individual options such as `--plugin-name`, `--per-skill-plugin`, `--only-skills`, or `--sign-pem`):
   ```bash
   # Option 1A: Ingest the full starter plugin (skills + MCP Server + A2A Agent)
   .venv/bin/python -m tools.plugin_sync ingest \
     --source-type local \
     --path starter-marketplace/plugins/gcp-finops-toolkit \
     --registry-url http://127.0.0.1:8090/v1 \
     --project acme-agent-governance \
     --location asia-southeast3 \
     --target-state TARGET_STATE_ACTIVE \
     --scanning-policy ENABLED_BLOCKING

   # Option 1B: Ingest all 3 pre-prepared standalone skills in local-skills/ as local-ops-toolkit
   .venv/bin/python -m tools.plugin_sync ingest \
     --source-type local \
     --path local-skills \
     --plugin-name local-ops-toolkit \
     --registry-url http://127.0.0.1:8090/v1 \
     --project acme-agent-governance \
     --location asia-southeast3 \
     --target-state TARGET_STATE_ACTIVE \
     --scanning-policy ENABLED_BLOCKING
   ```
   *(Tip: Pass `--per-skill-plugin` to emit 1 separate IDE plugin per standalone skill instead of grouping them under `local-ops-toolkit`, or `--only-skills` to skip `Service` registration.)*

---

### Option 2: Centralized Git Repository Path (Clone & Ingest)

Use Option 2 when onboarding an existing centralized GitLab or GitHub skills/plugins repository. `tools.plugin_sync ingest --source-type git` runs a shallow clone (`git clone --depth 1`) into an auto-cleaned temporary directory, strips embedded URL credentials before recording `frontmatter.metadata.source_repo`, stamps the exact 40-char `HEAD` commit SHA in `git_commit`, enforces `--subdir` containment (rejecting `..` and symlink escapes), and ingests every discovered plugin and/or standalone skill.

#### Option 2A: Self-Contained Offline Local Git Repo Demo (Zero Internet Required)

Prove the full Git clone-and-ingest workflow completely offline against a temporary local Git repository:

```bash
rm -rf /tmp/central-skills-repo
mkdir -p /tmp/central-skills-repo/skills/central-security-checker
cat << 'EOF' > /tmp/central-skills-repo/skills/central-security-checker/SKILL.md
---
name: central-security-checker
description: Check service configurations for hardcoded secrets and overly permissive IAM bindings.
license: Apache-2.0
metadata:
  domain: "security"
---

# Central Security Checker

Verify that all workloads use Workload Identity Federation and read-only Agent Registry roles.
EOF

git -C /tmp/central-skills-repo init -b main
git -C /tmp/central-skills-repo config user.name "Workshop Author"
git -C /tmp/central-skills-repo config user.email "author@acme.example.com"
git -C /tmp/central-skills-repo add .
git -C /tmp/central-skills-repo commit -m "Add central-security-checker skill"

# Clone & ingest the local Git repository via Option 2 (--source-type git)
.venv/bin/python -m tools.plugin_sync ingest \
  --source-type git \
  --repo-url /tmp/central-skills-repo \
  --ref main \
  --subdir skills \
  --plugin-name central-sec-toolkit \
  --registry-url http://127.0.0.1:8090/v1 \
  --project acme-agent-governance \
  --location asia-southeast3 \
  --target-state TARGET_STATE_ACTIVE \
  --scanning-policy ENABLED_BLOCKING
```

Inspect the JSON output: notice `"source_type": "git"`, `"layout": "standalone_skills"`, and a real 40-character `"git_commit"` SHA matching `git -C /tmp/central-skills-repo rev-parse HEAD`.

#### Option 2B: Live GitHub / Private GitLab Repository Examples

1. **One-command shortcut** (defaults to `SKILLS_REPO_URL=https://github.com/jiratouchmhp/agent-registry-marketplace-workshop.git`, `SKILLS_REPO_REF=main`, `SKILLS_REPO_SUBDIR=local-skills`, `SKILLS_REPO_PLUGIN_NAME=central-repo-skills`):
   ```bash
   make demo-sync-repo
   ```
   Override variables on the command line for any customer GitLab or GitHub repository (if your repository has plugins or skills at the root instead of `local-skills/`, pass `SKILLS_REPO_SUBDIR=""` to auto-detect from the repository root):
   ```bash
   export GITLAB_TOKEN="<your-read-repository-token>"
   make demo-sync-repo \
     SKILLS_REPO_URL="https://gitlab.example.com/ai-platform/agent-marketplace.git" \
     SKILLS_REPO_REF="main" \
     SKILLS_REPO_SUBDIR="plugins" \
     SKILLS_REPO_TOKEN_ENV="GITLAB_TOKEN" \
     SKILLS_REPO_PLUGIN_NAME="central-repo-skills"
   ```

2. **Explicit `tools.plugin_sync ingest --source-type git` command** for a private GitLab repository (never pass raw tokens on the command line—pass the **environment variable name** via `--git-token-env`, which is injected via an origin-scoped ephemeral `http.<origin>/.extraHeader`):
   ```bash
   export GITLAB_TOKEN="<your-read-repository-token>"

   .venv/bin/python -m tools.plugin_sync ingest \
     --source-type git \
     --repo-url https://gitlab.example.com/ai-platform/agent-marketplace.git \
     --ref main \
     --subdir plugins \
     --git-token-env GITLAB_TOKEN \
     --plugin-name central-repo-skills \
     --registry-url http://127.0.0.1:8090/v1 \
     --project acme-agent-governance \
     --location asia-southeast3 \
     --target-state TARGET_STATE_ACTIVE \
     --scanning-policy ENABLED_BLOCKING
   ```

---

### Register the Endpoint Service & Verify Projected Discovery Resources (Local Mock Mode)

1. Also register the `finops-billing-api` **Endpoint** Service (`NO_SPEC` + `HTTP_JSON`) directly via REST `v1`:
   ```bash
   curl -fsSL -X POST \
     "http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/asia-southeast3/services?serviceId=finops-billing-api" \
     -H "Content-Type: application/json" \
     -d '{
       "displayName": "FinOps Cloud Billing API Endpoint",
       "description": "External Cloud Billing REST API endpoint registered for governed agent egress policies.",
       "interfaces": [
         {"url": "https://cloudbilling.googleapis.com/v1", "protocolBinding": "HTTP_JSON"}
       ],
       "endpointSpec": {"type": "NO_SPEC"}
     }' | .venv/bin/python -m json.tool
   ```

2. Verify all projected read-only discovery resources and standalone `private-` skills:
   ```bash
   BASE="http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/asia-southeast3"

   # Standalone Skills (observe 'private-' prefix across Option 1 and Option 2 skills!)
   curl -fsSL "${BASE}/skills" | .venv/bin/python -m json.tool

   # Projected read-only MCP Server (with readOnlyHint & destructiveHint annotations)
   curl -fsSL "${BASE}/mcpServers" | .venv/bin/python -m json.tool

   # Projected read-only A2A Agent (interfaces projected from supportedInterfaces)
   curl -fsSL "${BASE}/agents" | .venv/bin/python -m json.tool

   # Projected read-only Endpoint
   curl -fsSL "${BASE}/endpoints" | .venv/bin/python -m json.tool
   ```

3. Test the `A2A_AGENT_CARD` top-level `interfaces` guardrail (should return `400 INVALID_ARGUMENT`):
   ```bash
   curl -i -s -X POST \
     "${BASE}/services?serviceId=bad-a2a-test" \
     -H "Content-Type: application/json" \
     -d '{
       "displayName": "Invalid A2A Service",
       "interfaces": [{"url": "http://127.0.0.1:8092/a2a", "protocolBinding": "JSONRPC"}],
       "agentSpec": {"type": "A2A_AGENT_CARD", "content": "{\"supportedInterfaces\": [{\"url\": \"http://127.0.0.1:8092/a2a\"}]}"}
     }'
   ```

### Mode B: Live GCP Mode (`v1` REST, `gcloud`, and Terraform `skills_source_mode`)

> **Track 2 & Track 3 Fresh-Project Ordering (Choose Your Path):** Because [Lab 00](00-environment-setup.md) stopped at authentication and pre-flight (without enabling APIs or deploying Cloud Run yet), choose one of these two options:
> 1. **Recommended (End-to-End Track 2 / Track 3 Flow):** Complete Lab 02 in **Mode A (Local Mock Mode)** above, then move to **[Lab 03](03-build-deploy-middleware.md)** where `scripts/deploy_gcp.sh` (Track 2) or `terraform/core` (Track 3) enables all required APIs, deploys Cloud Run services, registers Agent Registry Services (`finops-mcp-server`, `finops-advisor-agent`, `finops-billing-api`), and ingests Skills with `--wait-for-scan` in one atomic pass.
> 2. **Run Lab 02 Live Standalone Right Now:** Enable the Agent Registry API first so you do not hit `403 SERVICE_DISABLED`:
>    ```bash
>    gcloud services enable agentregistry.googleapis.com --project="${GCP_PROJECT_ID}" --quiet
>    ```
>    Then run `tools.plugin_sync ingest --only-skills --skills-location global --wait-for-scan` below and verify `gcloud agent-registry skills list --project="${GCP_PROJECT_ID}" --location="${AGENT_REGISTRY_SKILLS_LOCATION:-global}" --limit=50 --quiet`. Expect regional `mcp-servers list` and `agents list` (`--location="${GCP_LOCATION}"`) to populate once Cloud Run services are deployed and registered in [Lab 03](03-build-deploy-middleware.md).

1. **Via `tools.plugin_sync ingest` (REST `v1`) with `--skills-location global` and `--wait-for-scan`:**
   > **Live Async LLM Scanning (`--wait-for-scan`), Re-Deploy Race Protection & Quota Project Header:** On live `https://agentregistry.googleapis.com/v1`, `ENABLED_BLOCKING` runs an asynchronous LLM security scan (~10 seconds) after upload. On **initial create**, newly created skills briefly remain in `STATE_DRAFT` before auto-promoting to `STATE_ACTIVE`. On **idempotent re-deploy** of an existing skill, creating a new `SkillRevision` and immediately `PATCH`ing `defaultRevision` fails with `400 FAILED_PRECONDITION` (`cannot set default revision to a revision with pending scan results when scanning policy is ENABLED_BLOCKING`). Always pass **`--wait-for-scan`** so `tools/plugin_sync/sync.py` (`wait_for_revision_scan_completion()`) polls the new revision's `scanState` to completion before promoting `defaultRevision`. `tools.plugin_sync` also automatically sends `x-goog-user-project: <project>` on all `googleapis.com` requests.
   >
   > **Terraform Service Ownership Note:** If `terraform/core` manages your Cloud Run `google_agent_registry_service` resources (`finops-mcp-server`, `finops-advisor-agent`), pass `--only-skills` when invoking `tools.plugin_sync ingest` directly so local `127.0.0.1` development URLs in `starter-marketplace/` do not overwrite your Cloud Run Service endpoints (the Terraform bootstrapper passes `--only-skills --skills-location global --wait-for-scan` automatically).

   ```bash
   export GCP_PROJECT_ID="<your-gcp-project-id>"
   export GCP_LOCATION="asia-southeast3"
   export AGENT_REGISTRY_SKILLS_LOCATION="global"
   export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"
   export AGENT_REGISTRY_AUTH_TOKEN="$(gcloud auth print-access-token --quiet)"

   # Option 1 (Local Skills + Starter Plugin — standalone Skills go to global; add --only-skills if Terraform owns Services)
   .venv/bin/python -m tools.plugin_sync ingest \
     --source-type local \
     --path local-skills \
     --plugin-name local-ops-toolkit \
     --registry-url "${AGENT_REGISTRY_BASE_URL}" \
     --project "${GCP_PROJECT_ID}" \
     --location "${GCP_LOCATION}" \
     --skills-location "${AGENT_REGISTRY_SKILLS_LOCATION}" \
     --target-state TARGET_STATE_ACTIVE \
     --scanning-policy ENABLED_BLOCKING \
     --wait-for-scan \
     --only-skills \
     --skip-mcp-probe

   # OR Option 2 (Centralized GitLab / GitHub Repo)
   export GITLAB_TOKEN="<your-read-repository-token>"
   .venv/bin/python -m tools.plugin_sync ingest \
     --source-type git \
     --repo-url https://gitlab.example.com/ai-platform/agent-marketplace.git \
     --ref main \
     --subdir plugins \
     --git-token-env GITLAB_TOKEN \
     --registry-url "${AGENT_REGISTRY_BASE_URL}" \
     --project "${GCP_PROJECT_ID}" \
     --location "${GCP_LOCATION}" \
     --skills-location "${AGENT_REGISTRY_SKILLS_LOCATION}" \
     --target-state TARGET_STATE_ACTIVE \
     --scanning-policy ENABLED_BLOCKING \
     --wait-for-scan \
     --only-skills \
     --skip-mcp-probe
   ```

2. **Via Terraform (`terraform/core/`) with `skills_location = "global"` and `skills_source_mode = "local" | "git"`:**
   [terraform/core/agent_registry_services.tf](../../terraform/core/agent_registry_services.tf) manages the three GA regional `google_agent_registry_service` resources (`finops-mcp-server`, `finops-advisor-agent`, `finops-billing-api` in `var.location`, default Bangkok `asia-southeast3`) and reads back their projected IDs & URNs via `data.google_agent_registry_mcp_server`, `data.google_agent_registry_agent`, and `data.google_agent_registry_endpoint` using `basename(...registry_resource)`.

   Standalone Skills are bootstrapped cleanly into `var.skills_location` (`"global"` default, or `"us"`/`"eu"` for CMEK) via [terraform/core/skills_bootstrap.tf](../../terraform/core/skills_bootstrap.tf) (`terraform_data`), which **always passes `--only-skills --skip-mcp-probe --wait-for-scan`** so Terraform-managed `google_agent_registry_service` resources are never overwritten and async scans settle cleanly. Choose your mode in `terraform/core/terraform.tfvars` (see [terraform/core/variables.tf](../../terraform/core/variables.tf) and [terraform/core/terraform.tfvars.example](../../terraform/core/terraform.tfvars.example)):

   ```hcl
   # Verified Bangkok-first regional placement: Bangkok Services (asia-southeast3) + global Skills
   location                       = "asia-southeast3"
   skills_location                = "global" # "global" (APAC default), "us", or "eu" (CMEK)

   # Option 1 (Default): Local starter plugin + pre-prepared standalone local-skills/
   skills_source_mode             = "local"
   starter_plugin_dir             = "../../starter-marketplace/plugins/gcp-finops-toolkit"
   enable_local_standalone_skills = true
   local_skills_dir               = "../../local-skills"
   local_skills_plugin_name       = "local-ops-toolkit"
   skills_per_skill_plugin        = false

   # Option 2 (Alternative): Clone & ingest a centralized GitLab or GitHub repo
   # (NEVER place raw tokens in .tfvars — set skills_repo_token_env to the env var NAME!)
   # skills_source_mode      = "git"
   # skills_repo_url         = "https://gitlab.example.com/ai-platform/agent-marketplace.git"
   # skills_repo_ref         = "main"
   # skills_repo_subdir      = "plugins"
   # skills_repo_token_env   = "GITLAB_TOKEN"
   # skills_repo_plugin_name = "central-repo-skills"
   # skills_per_skill_plugin = false
   ```

3. **Inspect live resources via `gcloud` (note `global` for Skills vs. `asia-southeast3` for Services):**
   ```bash
   # Standalone Skills live in global (or us/eu for CMEK):
   gcloud agent-registry skills list --project="${GCP_PROJECT_ID}" --location="${AGENT_REGISTRY_SKILLS_LOCATION:-global}" --limit=50 --quiet
   # Projected MCP Servers & A2A Agents live in Bangkok primary (asia-southeast3):
   gcloud agent-registry mcp-servers list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" --limit=50 --quiet
   gcloud agent-registry agents list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" --limit=50 --quiet
   ```

---

## Step 3: Hands-On Coding Exercise (Optional Deep Dive)

Practice implementing `canonical_private_skill_id`, `enrich_skill_frontmatter`, `build_single_skill_zip`, and `register_skill_revision` in [starter/lab02_plugin_sync_exercise.py](../../starter/lab02_plugin_sync_exercise.py):

```bash
.venv/bin/python starter/lab02_plugin_sync_exercise.py
# Or verify all starter exercises together:
make starter-check
```

Fill in the `# WORKSHOP-TODO(lab-02)` blocks and re-run until all built-in self-checks print `[PASS]`. Compare against [tools/plugin_sync/sync.py](../../tools/plugin_sync/sync.py).

---

## Verification Checkpoint

- [ ] `GET .../skills` lists `private-cost-anomaly-triage` and `private-bq-query-optimizer` (starter plugin) plus `private-cloud-run-debugger`, `private-terraform-iam-auditor`, and `private-incident-postmortem-writer` (Option 1 `local-skills/`) and/or `private-central-security-checker` (Option 2 cloned Git repo) in `STATE_ACTIVE` with `defaultRevisionFindingCount: 0`.
- [ ] `GET .../mcpServers` lists `finops-mcp-server` with 3 tools (`get_billing_summary`, `analyze_bq_query`, `delete_idle_dataset`) and their MCP annotations.
- [ ] `GET .../agents` lists `finops-advisor-agent` with projected `interfaces` from `supportedInterfaces`.
- [ ] `.venv/bin/pytest -v tests/test_track1a_sync_and_samples.py tests/test_dual_skill_sources.py` passes completely.

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `403 SERVICE_DISABLED` calling `https://agentregistry.googleapis.com/v1` | `agentregistry.googleapis.com` has not been enabled on the fresh project yet | Either complete Lab 02 in Local Mock Mode and let `scripts/deploy_gcp.sh` / `terraform/core` in [Lab 03](03-build-deploy-middleware.md) enable all APIs automatically, or run `gcloud services enable agentregistry.googleapis.com --project="${GCP_PROJECT_ID}" --quiet` first. |
| `404 NOT_FOUND` when calling `GET .../skills/cost-anomaly-triage` | Missing `private-` auto-prefix on private publisher skills | Always address the skill as `skills/private-cost-anomaly-triage` after creation. |
| `400 INVALID_ARGUMENT: Generic 'labels' map is not supported` | Attempted to pass a `labels` object to `Service` or `Skill` | Move custom string attributes into `SKILL.md` `frontmatter.metadata` (`map<string, string>`). |
| `400 INVALID_ARGUMENT: Top-level 'interfaces' MUST be empty` | Passed both top-level `interfaces` and `agentSpec.type = "A2A_AGENT_CARD"` | Omit top-level `interfaces`; declare endpoints inside `agentSpec.content.supportedInterfaces`. |
| `400 INVALID_ARGUMENT: proto unmarshal failed: unknown field "stateTransitionHistory"` | Included legacy A2A v0.x `capabilities.stateTransitionHistory` in `agent-card.json` or Terraform `agent_spec.content` | Remove `stateTransitionHistory`; keep only A2A v1.0 `capabilities` (`streaming`, `pushNotifications`). |
| `400 INVALID_ARGUMENT: duplicate interface url found in agent card` | Declared two `supportedInterfaces` bindings (e.g. `JSONRPC` and `HTTP+JSON`) with the same `/a2a` URL | Split each protocol binding onto a distinct path (`/a2a` for `JSONRPC`, `/a2a/http` for `HTTP+JSON`). |
| `400 FAILED_PRECONDITION: cannot set default revision to a revision with pending scan results` | Re-deployed an existing skill under `ENABLED_BLOCKING` and patched `defaultRevision` before the new `SkillRevision`'s async scan (~10s) finished | Always pass `--wait-for-scan` to `tools.plugin_sync` so `wait_for_revision_scan_completion()` polls `scanState` to completion before promoting `defaultRevision`. |
| Live `skills` create/list times out or fails in `asia-southeast3` | Standalone `Skill` resources are hosted only in `global`, `us`, or `eu` on live Agent Registry `v1` (single-region `/skills` endpoints time out) | Pass `--skills-location global` (or set `AGENT_REGISTRY_SKILLS_LOCATION=global` / `skills_location = "global"`) while keeping Services in Bangkok (`asia-southeast3`). |

---

**Next Lab:** [Lab 03: Build & Deploy the Polyglot Marketplace Middleware →](03-build-deploy-middleware.md)
