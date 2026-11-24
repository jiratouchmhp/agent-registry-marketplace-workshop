# Lab 02: Decompose & Register Assets in Google Cloud Agent Registry (`v1`)

**Workstream:** A (Core Platform & Middleware)  
**Duration:** 35 minutes  
**Prerequisites:** Completed [Lab 00](00-environment-setup.md) and [Lab 01](01-author-agent-plugin.md).

---

## Files You'll Work With

| Role | File | What You'll Do |
| :--- | :--- | :--- |
| **Edit (Starter Exercise)** | [starter/lab02_plugin_sync_exercise.py](../../starter/lab02_plugin_sync_exercise.py) | Complete 4 `# WORKSHOP-TODO(lab-02)` functions (`canonical_private_skill_id`, `enrich_skill_frontmatter`, `build_single_skill_zip`, `register_skill_revision`). |
| **Input** | [starter/lab01_plugin_skeleton/skills/hello-finops/SKILL.md](../../starter/lab01_plugin_skeleton/skills/hello-finops/SKILL.md)<br>[starter-marketplace/plugins/gcp-finops-toolkit/](../../starter-marketplace/plugins/gcp-finops-toolkit/plugin.json)<br>[local-skills/](../../local-skills/README.md) | Sample skill and plugin folders packaged and uploaded into Agent Registry `v1`. |
| **Run** | `make demo-sync-local` ([tools/plugin_sync/cli.py](../../tools/plugin_sync/cli.py)) | Sync the reference plugin and standalone local skills into the local mock registry. |
| **Reference Solution** | [tools/plugin_sync/sync.py](../../tools/plugin_sync/sync.py)<br>[tools/plugin_sync/sources.py](../../tools/plugin_sync/sources.py) | Peek here if you get stuck on any TODO function or want to inspect the full CLI logic. |

---

## Learning Objectives

By the end of this lab, you will:
1. Preview how `tools.plugin_sync` splits an `agent-plugins.org` plugin into single-skill ZIPs and Agent Registry `Service` payloads.
2. Implement the 4 core packaging and registration functions in `starter/lab02_plugin_sync_exercise.py` and pass `4/4 [DONE]`.
3. Register the reference plugin, standalone `local-skills/`, and an external `Endpoint` against the local Mock Registry (`:8090`).
4. Verify the projected `/skills`, `/mcpServers`, `/agents`, and `/endpoints` views and trigger the A2A validation guardrail.

---

## Before You Start: 4 Agent Registry `v1` Rules

Keep these 4 API rules in mind while coding in Step 2:

1. **Automatic `private-` prefix on Skill IDs:** Calling `POST .../skills?skillId=cost-anomaly-triage` creates `skills/private-cost-anomaly-triage`. Every later `GET`, `PATCH`, `DELETE`, and `/revisions` call must use `private-cost-anomaly-triage`.
2. **No `labels` map in `v1`:** Agent Registry `v1` resources do not have a generic `labels` map. Store plugin metadata (`plugin_name`, `plugin_version`, `source_repo`, `git_commit`, `mcp_service_id`, `agent_service_id`) as flat `map<string, string>` entries inside `SKILL.md` `frontmatter.metadata`.
3. **Strict A2A v1.0 Agent Cards:** Leave top-level `interfaces` empty on A2A `Service` payloads and put endpoints inside `agentSpec.content.supportedInterfaces` (`<= 10 KB`). Omit legacy `stateTransitionHistory`, and give each binding a unique URL (`/a2a` for `JSONRPC`, `/a2a/http` for `HTTP+JSON`).
4. **Immutable `oneof spec`:** Once you create a `Service` as `mcpServerSpec`, `agentSpec`, or `endpointSpec`, you cannot switch its spec type with `PATCH`.

> **Local vs. Live GCP note:** Steps 1–4 run completely offline against `http://127.0.0.1:8090`. On Live GCP, regional `Services` run in Bangkok (`asia-southeast3`) while standalone `Skills` live in `global` (covered in [Going Live on GCP](#going-live-on-gcp-optional--track-2--track-3) below and the [Attendee Quickstart](../attendee-quickstart.md)).

---

## Step 1: Preview What `tools.plugin_sync` Builds (`--dry-run`)

Before writing code, run `tools.plugin_sync` in `--dry-run` mode on `gcp-finops-toolkit`. This writes per-skill ZIPs and the full `sync-plan.json` to `/tmp/finops-sync-dryrun/` without touching any API.

Run:

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

Inspect the generated files and peek inside one skill archive:

```bash
ls -la /tmp/finops-sync-dryrun/
.venv/bin/python -m zipfile -l /tmp/finops-sync-dryrun/cost-anomaly-triage.zip
```

You should see one ZIP per skill (`cost-anomaly-triage.zip`, `bq-query-optimizer.zip`) plus `sync-plan.json`. Each archive holds one uppercase `SKILL.md` under `<skill-name>/SKILL.md` and stays well below the 500 KB inline upload guideline (1 MB hard cap).

---

## Step 2: Hands-On Coding — Complete `starter/lab02_plugin_sync_exercise.py`

Open [starter/lab02_plugin_sync_exercise.py](../../starter/lab02_plugin_sync_exercise.py). First run its built-in self-check to confirm you start at `0/4`:

```bash
.venv/bin/python starter/lab02_plugin_sync_exercise.py
```

You should see 4 `[TODO]` lines and `Progress: 0/4 checks passed`.

Now implement the 4 `# WORKSHOP-TODO(lab-02)` functions in order, re-running the script after each one:

| # | Function | What to Implement | Stuck? Peek Here |
| :---: | :--- | :--- | :--- |
| 1 | `canonical_private_skill_id(raw_skill_id)` | Strip whitespace, raise `ValueError` if empty, and prepend `"private-"` only when not already present (never `private-private-...`). | `canonical_private_skill_id` in [tools/plugin_sync/sync.py](../../tools/plugin_sync/sync.py) |
| 2 | `enrich_skill_frontmatter(skill_md_text, plugin_metadata)` | Parse YAML with `parse_frontmatter()`, reject list/dict values in `metadata`, merge `plugin_metadata` as plain strings, and serialize `---\n<yaml>\n---\n\n<body>`. | `_serialize_frontmatter_markdown` & `build_skill_zip` in [tools/plugin_sync/sync.py](../../tools/plugin_sync/sync.py) |
| 3 | `build_single_skill_zip(skill_dir, plugin_metadata)` | Validate `SKILL.md` via `validate_skill_md()`, enrich it, zip `<skill-name>/...` while rejecting symlinks, `..` paths, and ZIPs over `MAX_SKILL_ZIP_BYTES`, then return `SkillArchiveArtifact`. | `build_skill_zip` in [tools/plugin_sync/sync.py](../../tools/plugin_sync/sync.py) |
| 4 | `register_skill_revision(...)` | `GET .../skills/{canonical_skill_id}` first. On `404`, `POST .../skills?skillId={skill_id}` with base64 `initialRevision`; on `200`, `POST .../revisions?skillRevisionId={revision_id}` and `PATCH` `defaultRevision`. | `apply_sync_plan` in [tools/plugin_sync/sync.py](../../tools/plugin_sync/sync.py) |

Re-run the self-check once all 4 functions are filled in:

```bash
.venv/bin/python starter/lab02_plugin_sync_exercise.py
```

You should see all 4 checks pass:

```text
Lab 02 Self-Check
  [PASS] canonical_private_skill_id
  [PASS] enrich_skill_frontmatter
  [PASS] build_single_skill_zip (sha256=...)
  [PASS] register_skill_revision (create + new revision promotion)
  [DONE] Progress: 4/4 checks passed — all Lab 02 checks passed!
```

*(You can also run `make starter-check` anytime to see your progress across Labs 01–04 together.)*

---

## Step 3: Sync to the Local Mock Registry (`make demo-sync-local`)

> **Already running `make run-local-stack` from Lab 00?** Your local servers (`:8090`, `:8091`, `:8092`, `:8080`) are already up. Jump straight to `make demo-sync-local` below.

If you are running servers in separate terminals instead, start the Mock Registry (`:8090`) and sample FinOps workloads (`:8091` MCP + `:8092` A2A) first:

```bash
# Terminal 1
make run-mock

# Terminal 2
make run-samples
```

Now run Option 1 (Local Skills default) to ingest both `starter-marketplace/plugins/gcp-finops-toolkit` and all 3 standalone skills in `local-skills/` (grouped as `local-ops-toolkit`). It also live-probes `http://127.0.0.1:8091/mcp` for MCP tool annotations:

```bash
make demo-sync-local
```

*(Need to sync only `gcp-finops-toolkit`? `make demo-sync` still works as a shortcut.)*

Next, register the `finops-billing-api` Endpoint `Service` (`NO_SPEC` + `HTTP_JSON`) directly via REST `v1`:

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

---

## Step 4: Inspect Projected Resources & Test the Bad A2A Guardrail

Query the read-only discovery collections on your local Mock Registry:

```bash
BASE="http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/asia-southeast3"

# 1. Standalone Skills (check the 'private-' prefix across all 5 skills)
curl -fsSL "${BASE}/skills" | .venv/bin/python -m json.tool

# 2. Projected MCP Server (check readOnlyHint & destructiveHint annotations)
curl -fsSL "${BASE}/mcpServers" | .venv/bin/python -m json.tool

# 3. Projected A2A Agent (interfaces projected from supportedInterfaces)
curl -fsSL "${BASE}/agents" | .venv/bin/python -m json.tool

# 4. Projected Endpoint
curl -fsSL "${BASE}/endpoints" | .venv/bin/python -m json.tool
```

You should see:
- **5 `private-` skills** in `STATE_ACTIVE` (`private-cost-anomaly-triage`, `private-bq-query-optimizer`, `private-cloud-run-debugger`, `private-terraform-iam-auditor`, `private-incident-postmortem-writer`).
- **`finops-mcp-server`** exposing 3 tools (`get_billing_summary`, `analyze_bq_query`, `delete_idle_dataset`).
- **`finops-advisor-agent`** with projected `/a2a` (`JSONRPC`) and `/a2a/http` (`HTTP+JSON`) interfaces.
- **`finops-billing-api`** under `/endpoints`.

Finally, send a deliberately invalid A2A `Service` that includes top-level `interfaces` to prove the API guardrail rejects it with `400 INVALID_ARGUMENT`:

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

You should get an `HTTP/1.1 400 Bad Request` response explaining that top-level `interfaces` must be empty for `A2A_AGENT_CARD`.

---

## Optional: Option 2 — Clone & Ingest a Git Repo

`python -m tools.plugin_sync ingest` ([tools/plugin_sync/sources.py](../../tools/plugin_sync/sources.py)) supports both `--source-type local` and `--source-type git`. It auto-detects 5 folder layouts (`single_plugin`, `multi_plugin`, `single_skill`, `standalone_skills`, `hybrid`), shallow-clones remote repos into a temporary directory, strips embedded URL credentials before recording `source_repo`, and stamps the 40-char `HEAD` commit SHA in `git_commit`.

<details>
<summary><strong>Offline local Git repo demo (no internet required)</strong></summary>

Create a temporary local Git repo at `/tmp/central-skills-repo` and ingest it with `--source-type git`:

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

Check the JSON output for `"source_type": "git"`, `"layout": "standalone_skills"`, and a 40-character `"git_commit"` SHA matching `git -C /tmp/central-skills-repo rev-parse HEAD`.

</details>

<details>
<summary><strong>Ingest a GitHub or private GitLab repository (<code>make demo-sync-repo</code> &amp; <code>--git-token-env</code>)</strong></summary>

1. **Shortcut command** (defaults to this workshop's `local-skills/` folder on GitHub):
   ```bash
   make demo-sync-repo
   ```
   Override variables for any customer repo (set `SKILLS_REPO_SUBDIR=""` to detect from the repo root):
   ```bash
   export GITLAB_TOKEN="<your-read-repository-token>"
   make demo-sync-repo \
     SKILLS_REPO_URL="https://gitlab.example.com/ai-platform/agent-marketplace.git" \
     SKILLS_REPO_REF="main" \
     SKILLS_REPO_SUBDIR="plugins" \
     SKILLS_REPO_TOKEN_ENV="GITLAB_TOKEN" \
     SKILLS_REPO_PLUGIN_NAME="central-repo-skills"
   ```

2. **Explicit CLI command** (never pass raw tokens as CLI arguments; pass the environment variable name via `--git-token-env`):
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

</details>

<details>
<summary><strong>Explicit Option 1 CLI commands (<code>--source-type local</code>)</strong></summary>

Run `ingest --source-type local` directly when you want custom flags like `--per-skill-plugin`, `--only-skills`, or `--sign-pem`:

```bash
# Ingest the full starter plugin (skills + MCP Server + A2A Agent)
.venv/bin/python -m tools.plugin_sync ingest \
  --source-type local \
  --path starter-marketplace/plugins/gcp-finops-toolkit \
  --registry-url http://127.0.0.1:8090/v1 \
  --project acme-agent-governance \
  --location asia-southeast3 \
  --target-state TARGET_STATE_ACTIVE \
  --scanning-policy ENABLED_BLOCKING

# Ingest all 3 standalone skills in local-skills/ under local-ops-toolkit
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

</details>

---

## Going Live on GCP (Optional — Track 2 & Track 3)

**APAC region split:** On Live Google Cloud Agent Registry `v1`, regional `Services` (`McpServer`, `Agent`, `Endpoint`) run in Bangkok (`asia-southeast3`), while standalone `Skills` live in `global` (`--skills-location global`, or `us`/`eu` for CMEK; single-region `/skills` endpoints time out). Singapore (`asia-southeast1`) is only used for Workstream B Agent Identity `AuthProvider` (see [Attendee Quickstart](../attendee-quickstart.md)).

**Fresh-project order:** If you are following Track 2 or Track 3 end to end, finish Steps 1–4 locally first, then run [Lab 03](03-build-deploy-middleware.md) to enable APIs, deploy Cloud Run, and register Services and Skills together. Expand the blocks below if you want to test live skills right now or configure Terraform's `skills_source_mode`.

Key live behaviors to remember:
- **`--wait-for-scan`:** Live `ENABLED_BLOCKING` runs an async LLM scan (~10s). Always pass `--wait-for-scan` so `wait_for_revision_scan_completion()` waits for `scanState` before patching `defaultRevision` (otherwise re-deploys fail with `400 FAILED_PRECONDITION`).
- **`private-` auto-filtering:** The Middleware defaults to `INCLUDE_PUBLIC_SKILLS=false` so Google's built-in first-party skills in `global` never mix into your `private-*` catalog.
- **`--only-skills`:** Pass `--only-skills` when Terraform manages your Cloud Run `Service` URLs so local `127.0.0.1` plugin manifests do not overwrite them.

<details>
<summary><strong>Track 2: Live CLI ingest (<code>--skills-location global --wait-for-scan</code>)</strong></summary>

Enable the Agent Registry API on a fresh project first, then upload standalone skills to `global`:

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"
export GCP_LOCATION="asia-southeast3"
export AGENT_REGISTRY_SKILLS_LOCATION="global"
export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"

gcloud services enable agentregistry.googleapis.com --project="${GCP_PROJECT_ID}" --quiet
export AGENT_REGISTRY_AUTH_TOKEN="$(gcloud auth print-access-token --quiet)"

# Option 1 (Local skills -> global; tools.plugin_sync sends x-goog-user-project automatically)
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

# OR Option 2 (Centralized GitLab / GitHub repo)
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

</details>

<details>
<summary><strong>Track 3: Terraform <code>skills_source_mode</code> (<code>terraform/core/skills_bootstrap.tf</code>)</strong></summary>

[terraform/core/agent_registry_services.tf](../../terraform/core/agent_registry_services.tf) manages the 3 regional `google_agent_registry_service` resources in Bangkok (`var.location = "asia-southeast3"`). Standalone skills are uploaded into `var.skills_location` (`"global"`) by [terraform/core/skills_bootstrap.tf](../../terraform/core/skills_bootstrap.tf), which always passes `--only-skills --skip-mcp-probe --wait-for-scan`.

Configure your mode in `terraform/core/terraform.tfvars` (see [variables.tf](../../terraform/core/variables.tf) and [terraform.tfvars.example](../../terraform/core/terraform.tfvars.example)):

```hcl
location                       = "asia-southeast3"
skills_location                = "global" # "global" (APAC default), "us", or "eu" (CMEK)

# Option 1 (Default): Local starter plugin + standalone local-skills/
skills_source_mode             = "local"
starter_plugin_dir             = "../../starter-marketplace/plugins/gcp-finops-toolkit"
enable_local_standalone_skills = true
local_skills_dir               = "../../local-skills"
local_skills_plugin_name       = "local-ops-toolkit"
skills_per_skill_plugin        = false

# Option 2 (Alternative): Clone & ingest a Git repo
# (Never put raw tokens in .tfvars — set skills_repo_token_env to the env var name)
# skills_source_mode      = "git"
# skills_repo_url         = "https://gitlab.example.com/ai-platform/agent-marketplace.git"
# skills_repo_ref         = "main"
# skills_repo_subdir      = "plugins"
# skills_repo_token_env   = "GITLAB_TOKEN"
# skills_repo_plugin_name = "central-repo-skills"
# skills_per_skill_plugin = false
```

</details>

<details>
<summary><strong>Inspect live resources with <code>gcloud agent-registry</code></strong></summary>

```bash
# Standalone Skills live in global (or us/eu for CMEK):
gcloud agent-registry skills list --project="${GCP_PROJECT_ID}" --location="${AGENT_REGISTRY_SKILLS_LOCATION:-global}" --limit=50 --quiet

# Projected MCP Servers & A2A Agents live in Bangkok (asia-southeast3):
gcloud agent-registry mcp-servers list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" --limit=50 --quiet
gcloud agent-registry agents list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}" --limit=50 --quiet
```

</details>

---

## Verification Checkpoint

- [ ] `.venv/bin/python starter/lab02_plugin_sync_exercise.py` prints `4/4` `[DONE]`.
- [ ] `GET .../skills` lists `private-cost-anomaly-triage` and `private-bq-query-optimizer`, plus the 3 `local-skills/` (`private-cloud-run-debugger`, `private-terraform-iam-auditor`, `private-incident-postmortem-writer`), in `STATE_ACTIVE` with `defaultRevisionFindingCount: 0`.
- [ ] `GET .../mcpServers` lists `finops-mcp-server` with 3 tools (`get_billing_summary`, `analyze_bq_query`, `delete_idle_dataset`), and `GET .../agents` lists `finops-advisor-agent`.
- [ ] `.venv/bin/pytest -v tests/test_track1a_sync_and_samples.py tests/test_dual_skill_sources.py` passes completely.

---

## Troubleshooting

| Symptom | Why It Happens | How to Fix |
| :--- | :--- | :--- |
| `403 SERVICE_DISABLED` calling `https://agentregistry.googleapis.com/v1` | `agentregistry.googleapis.com` is not enabled on the project yet. | Run `gcloud services enable agentregistry.googleapis.com --project="${GCP_PROJECT_ID}" --quiet`, or finish locally and deploy via [Lab 03](03-build-deploy-middleware.md). |
| `404 NOT_FOUND` on `GET .../skills/cost-anomaly-triage` | Private skills automatically get a `private-` prefix after creation. | Address the resource as `skills/private-cost-anomaly-triage`. |
| `400 INVALID_ARGUMENT: Generic 'labels' map is not supported` | Sent a top-level `labels` object to a `Service` or `Skill`. | Move flat string attributes into `SKILL.md` `frontmatter.metadata` (`map<string, string>`). |
| `400 INVALID_ARGUMENT: Top-level 'interfaces' MUST be empty` | Sent both top-level `interfaces` and `agentSpec.type = "A2A_AGENT_CARD"`. | Omit top-level `interfaces`; declare endpoints inside `agentSpec.content.supportedInterfaces`. |
| `400 INVALID_ARGUMENT: unknown field "stateTransitionHistory"` | Included legacy A2A v0.x `capabilities.stateTransitionHistory`. | Remove `stateTransitionHistory`; keep only `streaming` and `pushNotifications`. |
| `400 INVALID_ARGUMENT: duplicate interface url found in agent card` | Two bindings in `supportedInterfaces` share `/a2a`. | Use `/a2a` for `JSONRPC` and `/a2a/http` for `HTTP+JSON`. |
| `400 FAILED_PRECONDITION: pending scan results` | Re-deployed a skill under `ENABLED_BLOCKING` and patched `defaultRevision` before the async scan (~10s) finished. | Always pass `--wait-for-scan` so `tools.plugin_sync` waits for scan completion before promotion. |
| Live `/skills` create or list times out in `asia-southeast3` | Live standalone `Skills` only exist in `global`, `us`, or `eu`. | Pass `--skills-location global` (or set `skills_location = "global"`) while keeping `Services` in `asia-southeast3`. |

---

**Next Lab:** [Lab 03: Build & Deploy the Polyglot Marketplace Middleware →](03-build-deploy-middleware.md)
