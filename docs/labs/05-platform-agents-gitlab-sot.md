# Lab 05: Platform Agents (Google ADK) & GitLab Source of Truth

**Workstream:** A — Core Platform & Middleware  
**Duration:** 40 minutes  
**Prerequisites:** Completed [Lab 03](03-build-deploy-middleware.md) with `make run-mock` (`:8090`) and `make run-middleware` (`:8080`) running locally.

## Files You'll Work With

No `starter/` coding stub in this lab — you'll run and inspect the repo files below directly.

| Role | File | What You'll Do |
| :--- | :--- | :--- |
| **Run / Inspect** | [`platform_agent/adk_orchestrator.py`](../../platform_agent/adk_orchestrator.py) | Print the offline ADK wiring and run live local capability discovery. |
| **Inspect** | [`platform_agent/rest_consumer.py`](../../platform_agent/rest_consumer.py) | Read how direct REST calls handle `skills:search`, `?alt=media` redirects, and `bindings:fetchAvailable`. |
| **Inspect** | [`starter-marketplace/.gitlab-ci.yml`](../../starter-marketplace/.gitlab-ci.yml) | Review the 3-stage GitLab CI pipeline separating MR drafts from tagged releases. |
| **Run** | [`tools/plugin_sync/cli.py`](../../tools/plugin_sync/cli.py), [`starter-marketplace/plugins/gcp-finops-toolkit/`](../../starter-marketplace/plugins/gcp-finops-toolkit/plugin.json) | Simulate the GitLab CI MR draft (`TARGET_STATE_DRAFT`) and release (`TARGET_STATE_ACTIVE`) sync commands locally. |
| **Install (Live GCP)** | [`platform_agent/requirements.txt`](../../platform_agent/requirements.txt) | Optional `google-adk` dependencies for running `--mode adk` against live GCP. |
| **Run (Live GCP)** | [`scripts/validate_live_gcp.sh`](../../scripts/validate_live_gcp.sh) | Run end-to-end checks against a live Workstream A GCP deployment (`make gcp-validate`). |
| **Run (Live GCP)** | [`scripts/destroy_gcp.sh`](../../scripts/destroy_gcp.sh) | Cleanly remove Workstream A GCP resources (`make gcp-destroy`). |

## Learning Objectives

By the end of this lab, you will:
1. Verify how a headless Platform Agent connects directly to Agent Registry `v1` using read-only runtime roles.
2. Run local semantic skill search, safe `?alt=media` downloads, and MCP/A2A discovery against port `8090`.
3. Walk through `starter-marketplace/.gitlab-ci.yml` and simulate MR draft quarantine vs. release promotion locally.
4. *(Optional)* Run live Google ADK mode, validate a live Workstream A GCP deployment, and tear it down cleanly.

---

Developer IDEs need plugin ZIPs and marketplace feeds from the Polyglot Middleware. Headless Platform Agents on Vertex AI Agent Engine or Cloud Run skip that wrapper and call Agent Registry `v1` directly. They run with least-privilege roles (`roles/agentregistry.viewer`, `roles/agentidentity.user`, and `roles/iap.egressor`), never `editor` or `admin`.

## Step 1: Print the ADK Wiring Offline

Start by inspecting the declarative ADK configuration in [`platform_agent/adk_orchestrator.py`](../../platform_agent/adk_orchestrator.py). This runs completely offline without installing `google-adk`.

Run:

```bash
.venv/bin/python -m platform_agent.adk_orchestrator --mode adk --print-config
```

You should see JSON confirming these guardrails:
- `auth_manager.registration_call`: `"CredentialManager.register_auth_provider(GcpAuthProvider())"`
- `auth_manager.required_iam_roles`: `["roles/agentregistry.viewer", "roles/agentidentity.user"]` (IAP runtime egress is listed under `governance.egress_policy`)
- `governance.skill_filter` and `integrations.skill_registry.filter`: `"state=ACTIVE AND defaultRevisionFindingCount=0"`
- `governance.disallowed_runtime_roles`: `["roles/agentregistry.editor", "roles/agentregistry.admin"]`

## Step 2: Run Live Local Discovery Against Port 8090

Make sure `make run-mock` (`:8090`) is running and populated (`make demo-sync-local` or `make demo-sync`). Now run `PlatformAgentOrchestrator` in `rest` mode to query skills, MCP servers, A2A agents, and auth bindings directly.

Run:

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

You should see JSON results across three resource types:
- **Skills:** `private-cost-anomaly-triage` matched via `POST .../skills:search` (`searchType: "SEMANTIC"`), downloaded via `?alt=media` with 302 redirect header stripping, and checked against `sha256Hash`.
- **MCP Servers:** `finops-mcp-server` matched via `POST .../mcpServers:search`.
- **Agents:** `finops-advisor-agent` matched via `POST .../agents:search`.

Note that `mcpServers:search` and `agents:search` accept `{"searchString": "..."}` only; passing `searchType` triggers an HTTP 400 error. Open [`platform_agent/rest_consumer.py`](../../platform_agent/rest_consumer.py) if you want to read the two-hop redirect and hash-check implementation.

## Step 3: Walk Through GitLab CI and Simulate MR Draft to Release Promotion

Open [`starter-marketplace/.gitlab-ci.yml`](../../starter-marketplace/.gitlab-ci.yml). GitLab handles code review and version history, while Agent Registry serves approved runtime artifacts. Three CI stages enforce that boundary:

| Stage | Job(s) | Trigger | What It Does |
| :--- | :--- | :--- | :--- |
| **1. `validate`** | `validate:schemas`, `validate:antigravity-cli` | MR, `main`, tags | Validates every plugin under `plugins/` against all 5 IDE schemas and runs `agy plugin validate --json` with the `jq` filter. |
| **2. `package-and-sign`** | `package-and-sign` | MR, `main`, tags | Runs `python -m tools.plugin_sync sync --dry-run`, records `source_repo` and `git_commit`, and signs ZIPs (`--kms-key-version` or `--sign-pem`). |
| **3. `register-registry`** | `register:draft-mr` | Merge Requests | Uploads with `--target-state TARGET_STATE_DRAFT --scanning-policy ENABLED_BLOCKING` so unreviewed MR changes stay hidden from developers. |
| **3. `register-registry`** | `register:active-release` | Semver tags (`v*.*.*`) or manual on `main` | Promotes clean, signed revisions to `--target-state TARGET_STATE_ACTIVE`. |

Make sure `make run-mock` (`:8090`) and `make run-middleware` (`:8080`) are both running, then run the exact MR draft and release tag commands locally:

```bash
# 1. Reset mock_registry to a clean slate
curl -fsSL -X POST http://127.0.0.1:8090/_mock/reset

# 2. Simulate the MR job (register:draft-mr) -> uploads in TARGET_STATE_DRAFT
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

# 3. Confirm Middleware hides DRAFT skills from developers
curl -fsSL http://127.0.0.1:8080/v1/catalog | .venv/bin/python -m json.tool
# You should see plugins: [] and both skills under rejectedSkills (STATE_DRAFT).

# 4. Simulate the release tag job (register:active-release) -> promotes to TARGET_STATE_ACTIVE
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

# 5. Confirm Middleware now serves gcp-finops-toolkit
curl -fsSL http://127.0.0.1:8080/v1/catalog | .venv/bin/python -m json.tool
```

> **Continuing locally to Labs 06–08?** Step 3 reset `mock_registry` and only re-synced `gcp-finops-toolkit`, which cleared `local-ops-toolkit` and `finops-billing-api`. Run `make demo-sync-local` afterward if you want the full local catalog back.

---

## Going Live on GCP (Optional)

**Region note:** On live GCP, `AgentRegistry` reads regional `Services` (`McpServer`, `Agent`, `Endpoint`) from Bangkok (`asia-southeast3`), while `GCPSkillRegistry` reads standalone `Skills` from `global` (filtered to `private-*` skills). In Workstream B later, Agent Identity `AuthProvider` uses Singapore (`auth_provider_location = "asia-southeast1"`). See the full APAC matrix in [Attendee Quickstart](../attendee-quickstart.md).

### Live Google ADK Mode (`--mode adk`)

On a workstation or Vertex AI Agent Engine runtime with `google-adk >= 1.29.0`, run `adk_orchestrator.py` against live Agent Registry `v1`. Standalone `Skill` resources are project-scoped and do not roll up across folders or organizations, so point `GCPSkillRegistry` directly at your catalog project.

```bash
# Install ADK with A2A, Agent Identity, and MCP extras
.venv/bin/pip install -r platform_agent/requirements.txt

# Export both GCP_* and GOOGLE_CLOUD_* vars (adk_orchestrator.py gives GCP_* precedence
# so any local mock settings in your shell are overridden)
export GCP_PROJECT_ID="<your-gcp-project-id>"
export GCP_LOCATION="asia-southeast3"
export AGENT_REGISTRY_SKILLS_LOCATION="global"
export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"
export GOOGLE_CLOUD_PROJECT="${GCP_PROJECT_ID}"
export GOOGLE_CLOUD_LOCATION="${GCP_LOCATION}"
export AGENT_REGISTRY_ENDPOINT="${AGENT_REGISTRY_BASE_URL}"

# Build the live ADK LlmAgent wired to Bangkok Services + global Skills
.venv/bin/python -m platform_agent.adk_orchestrator \
  --mode adk \
  --project-id "${GCP_PROJECT_ID}" \
  --location "${GCP_LOCATION}" \
  --skills-location "${AGENT_REGISTRY_SKILLS_LOCATION}" \
  --base-url "${AGENT_REGISTRY_BASE_URL}"
```

### Workstream A Live GCP Validation & Teardown

If you deployed Workstream A to Google Cloud in Lab 03, run the end-to-end validation script. Tear down the resources afterward unless you are heading straight into live Workstream B.

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"

# 1. Validate live Cloud Run services (/health), Bangkok Services, global Skills, and 4-IDE feeds
make gcp-validate GCP_PROJECT_ID="${GCP_PROJECT_ID}"
# Direct script equivalent:
# scripts/validate_live_gcp.sh --project "${GCP_PROJECT_ID}" --region asia-southeast3 --skills-location global

# 2. Tear down Workstream A resources when done
# Track 2 (gcloud + Cloud Build):
make gcp-destroy GCP_PROJECT_ID="${GCP_PROJECT_ID}"
# Direct script equivalent:
# scripts/destroy_gcp.sh --project "${GCP_PROJECT_ID}" --region asia-southeast3 --skills-location global

# Track 3 (Terraform): set force_destroy_staging_bucket = true in sandbox .tfvars if objects exist
# terraform -chdir=terraform/core destroy
```

A few live teardown details handled by `scripts/destroy_gcp.sh`:
- **Deleting skills with revisions:** `gcloud agent-registry skills delete` has no `--force` flag. The script calls REST `DELETE .../locations/${AGENT_REGISTRY_SKILLS_LOCATION:-global}/skills/{id}?force=true` with `Authorization` and `x-goog-user-project` headers instead.
- **IAM policy version 3:** Projects with conditional IAM bindings require `--condition=None` on `gcloud projects remove-iam-policy-binding`. The script passes `--condition=None` and cleans up `deleted:serviceAccount:<email>?uid=...` tombstones.
- **Source-deploy repo and build buckets:** Pass `--delete-source-deploy-repo` only in a dedicated sandbox if you tested `--deploy-mode source`. Regional Cloud Build staging buckets stay intact by default (`gcloud storage buckets list --project="${GCP_PROJECT_ID}" --limit=50 --quiet`).

<details>
<summary>Reference: Pull-Based Git Ingest vs. Push-Based GitLab CI &amp; Standalone Migration Variables</summary>

Two ingestion patterns complement each other as teams adopt Agent Registry:
- **Pull-based Option 2 (`python -m tools.plugin_sync ingest --source-type git` / `make demo-sync-repo` / `skills_source_mode = "git"`):** Good for onboarding an existing GitLab or GitHub skill repo before teams add `.gitlab-ci.yml`. It auto-detects multi-plugin, standalone `skills/*/SKILL.md`, and hybrid layouts while recording `source_repo` and `git_commit`.
- **Push-based GitLab CI ([`starter-marketplace/.gitlab-ci.yml`](../../starter-marketplace/.gitlab-ci.yml)):** Good for ongoing governance on Merge Requests and semver tags, combining 5-IDE schema validation, `TARGET_STATE_DRAFT` quarantine, keyless GCP OIDC, and Cloud KMS signing.

`starter-marketplace/.gitlab-ci.yml` works both inside this monorepo and when copied into a standalone GitLab repo. Set these four CI/CD variables when migrating to a standalone GitLab project:

| GitLab CI Variable | Purpose | Example Value |
| :--- | :--- | :--- |
| `WORKSHOP_TOOLS_PACKAGE` | Pip spec, Artifact Registry wheel, or `git+https://` URL installing `schemas` and `tools.plugin_sync` | `git+https://github.com/jiratouchmhp/agent-registry-marketplace-workshop.git@main` |
| `PLUGINS_DIR` | Override the plugin folder path (blank auto-detects `plugins` or `starter-marketplace/plugins`) | `plugins` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full Workload Identity Provider path for keyless GitLab OIDC to STS token exchange | `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/gitlab-pool/providers/gitlab-oidc` |
| `GCP_SERVICE_ACCOUNT_EMAIL` | Least-privilege CI publisher SA (`roles/agentregistry.editor` + `roles/cloudkms.signerVerifier`) | `agent-registry-ci-publisher@<PROJECT_ID>.iam.gserviceaccount.com` |

</details>

---

## Verification Checkpoint

- [ ] `.venv/bin/python -m platform_agent.adk_orchestrator --mode adk --print-config` prints valid ADK wiring and least-privilege roles.
- [ ] `platform_agent.adk_orchestrator --mode rest` discovers and extracts `private-cost-anomaly-triage` from `:8090`.
- [ ] Confirmed MR uploads in `TARGET_STATE_DRAFT` stay hidden from `/v1/catalog` until promoted to `TARGET_STATE_ACTIVE`.
- [ ] `.venv/bin/pytest -v tests/test_track1d_platform_agent.py` and `make test-e2e` pass.

## Troubleshooting

| Symptom | Why It Happens | How to Fix |
| :--- | :--- | :--- |
| `SkillSignatureError` in `--mode rest` | `REQUIRE_KMS_SIGNATURE=true` is set in your shell without a trusted public key, or the skill ZIP is unsigned | Unset `REQUIRE_KMS_SIGNATURE` (or set `REQUIRE_KMS_SIGNATURE=false`) for unsigned Workstream A runs, or set `KMS_PUBLIC_KEY_PEM_PATH` after [Lab 07](07-kms-encryption-and-signing.md). |
| `RuntimeError: Live ADK mode requires google-adk>=1.29.0` | Running `--mode adk` without installing optional ADK dependencies | Run `.venv/bin/pip install -r platform_agent/requirements.txt`, or stick to `--mode rest` and `--print-config`. |
| `400 INVALID_ARGUMENT` on `agents:search` or `mcpServers:search` | Sent `searchType` to an endpoint that does not accept it | Only `skills:search` accepts `searchType` (`KEYWORD` or `SEMANTIC`). Send `{"searchString": "..."}` only to `agents:search` and `mcpServers:search`. |
| `401 CREDENTIALS_MISSING` on `?alt=media` in Live GCP | `Authorization` header was stripped on the same-origin `302` redirect to `/download/v1/...` | [`platform_agent/rest_consumer.py`](../../platform_agent/rest_consumer.py) keeps `Authorization` on same-origin `/download/...` redirects without signed-URL tokens and strips `Authorization` and `x-goog-*` on cross-origin or signed redirects. |
| Live `skills:search` returns 404 or times out in `asia-southeast3` | Standalone `Skills` live in `global` (or `us`/`eu`), not single regions | Pass `--skills-location global` or export `AGENT_REGISTRY_SKILLS_LOCATION="global"`. |
| `gcloud projects remove-iam-policy-binding` fails or prompts on teardown | Project uses IAM policy version 3 with conditional bindings | Pass `--condition=None` on `add-iam-policy-binding` and `remove-iam-policy-binding` (already automated in `scripts/destroy_gcp.sh`). |

---

**Workstream A Complete!** Continue to the standalone Security & Governance track: [Lab 06: Governance, Automated Scanning & Skill Lifecycle →](06-governance-scanning-lifecycle.md)
