# Attendee Quickstart & Cheat Sheet

Pick your track based on what is installed on your laptop. No local Docker is required for any track because live GCP builds run remotely in Google Cloud Build.

> **Know your 3 content folders:**
> - **`starter/`** = Your hands-on exercises (skeletons and `# WORKSHOP-TODO` files you edit in Labs 01–04). See [starter/README.md](../starter/README.md).
> - **`starter-marketplace/`** = Working reference plugin repo (`gcp-finops-toolkit` + `.gitlab-ci.yml`). Peek here if you get stuck.
> - **`local-skills/`** = Ready-to-sync standalone skills (`cloud-run-debugger`, `terraform-iam-auditor`, `incident-postmortem-writer`).

---

## 1. Choose Your Track

```text
Start here -> Run `make preflight` from the repo root
├── Want to run 100% offline (no GCP project, no Docker, no Terraform)?
│   └── Track 1: Offline Local Mode (Recommended first 5 minutes for everyone)
│       1. make preflight
│       2. make smoke-local             # 1-command automated end-to-end check
│       3. make run-local-stack         # Starts :8090, :8091, :8092, :8080 in one terminal
│       4. make starter-check           # Check Labs 01-04 # WORKSHOP-TODO progress
│
├── Have a GCP project + gcloud CLI, but NO Docker and NO Terraform?
│   └── Track 2: Live GCP (gcloud + Cloud Build)
│       1. make preflight
│       2. make gcp-deploy GCP_PROJECT_ID="<ID>"
│          (runs scripts/deploy_gcp.sh --region asia-southeast3 --skills-location global)
│       3. make gcp-validate GCP_PROJECT_ID="<ID>"
│       4. make gcp-destroy GCP_PROJECT_ID="<ID>"   # Always clean up when finished
│
└── Have a GCP project + gcloud CLI + Terraform >= 1.6.0?
    └── Track 3: Live GCP (Full IaC: Stage 1 APIs/AR + Cloud Build -> Stage 2 Terraform)
        1. make preflight
        2. Copy terraform/core/terraform.tfvars.example -> terraform/core/terraform.tfvars
           (replace acme-agent-governance in project_id, middleware_image, sample_mcp_image, sample_a2a_image)
        3. Stage 1A (enable APIs + create Artifact Registry repo in Terraform state):
           terraform -chdir=terraform/core init && terraform -chdir=terraform/core apply \
             -target=google_project_service.core -target=google_artifact_registry_repository.workshop_images
        4. Stage 1B (build images via Cloud Build before full apply):
           scripts/build_images_cloudbuild.sh --project "$GCP_PROJECT_ID" --region asia-southeast3
        5. Stage 2 (Workstream A core stack in Bangkok asia-southeast3 + global Skills):
           make tf-core-plan && terraform -chdir=terraform/core apply
        6. Optional Workstream B (Standalone Security & Governance, no core state needed):
           make tf-sec-plan && terraform -chdir=terraform/security_governance apply
```

| Track | Required Tools | GCP Account? | Local Docker? | Terraform? | Main Commands |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Track 1: Offline Local** | Python 3.12+ (`.venv`), `curl`, `make` | No | No | No | `make preflight`, `make smoke-local`, `make run-local-stack`, `make starter-check` |
| **Track 2: Live GCP (`gcloud`)** | Track 1 + `gcloud` CLI | Yes | No | No | `make gcp-deploy`, `make gcp-validate`, `make gcp-destroy` |
| **Track 3: Live GCP (Terraform)** | Track 2 + Terraform `>= 1.6.0` (`hashicorp/google >= 7.39.0`) | Yes | No | Yes | Stage 1A: targeted `terraform apply` (APIs + AR), Stage 1B: `scripts/build_images_cloudbuild.sh`, Stage 2: `terraform -chdir=terraform/core apply` |

> **Track 3 ordering rule:** Copy and update `terraform/core/terraform.tfvars` (including `project_id` and the three image URIs), run **Stage 1A** (targeted `terraform apply` for APIs and `google_artifact_registry_repository.workshop_images`) and **Stage 1B** (`scripts/build_images_cloudbuild.sh`), then run **Stage 2** (full `terraform apply`). Cloud Build needs those APIs enabled, Terraform should own the Artifact Registry repo, and Cloud Run v2 creation fails on a fresh project if the images are not pushed yet. [Lab 00](labs/00-environment-setup.md) only handles login and preflight; container builds and deploys happen in [Lab 03](labs/03-build-deploy-middleware.md).

---

## 2. APAC Region Guide

This is the single canonical region reference for all labs. Live deployments run one clean stack in **Bangkok (`asia-southeast3`)** and use Singapore or global/multi-region only where a Google Cloud API requires it.

| Component | Workstream | Placement | Live Location | Key Flags & Live Gotchas |
| :--- | :---: | :--- | :--- | :--- |
| **Cloud Run v2** (`sample-mcp-server`, `sample-a2a-agent`, `polyglot-middleware`) | A | Primary | `asia-southeast3` (Bangkok) | Default `GCP_LOCATION=asia-southeast3`. Probe `/health` (or `/v1/health`) externally because Google Frontend reserves `/healthz`. |
| **Cloud Build & Artifact Registry** (`cloudbuild/*.yaml`) | A | Primary | `asia-southeast3` (Bangkok) | Uses `--region=asia-southeast3`, `--default-buckets-behavior=regional-user-owned-bucket`, and `asia-southeast3-docker.pkg.dev`. |
| **Agent Registry `Services` & `Bindings`** (`finops-mcp-server`, `finops-advisor-agent`, `finops-billing-api`) | A & B | Primary | `asia-southeast3` (Bangkok) | Default `location = "asia-southeast3"`. A2A v1.0 cards must omit `stateTransitionHistory` and use distinct URLs for `JSONRPC` (`/a2a`) and `HTTP+JSON` (`/a2a/http`). |
| **Cloud Storage** (Skill staging bucket) | A | Primary | `asia-southeast3` (Bangkok) | Grant the Agent Registry P4SA `roles/storage.objectViewer` for `gcsSource` skill imports. |
| **Cloud KMS Asymmetric Signing** (`EC_SIGN_P256_SHA256`) | B (Lab 07) | Primary | `asia-southeast3` (Bangkok) | Regional key ring for CI signing and Middleware fail-closed tamper checks. |
| **Cloud Logging Audit Bucket** (`agent-governance-audit`) | B (Lab 08) | Primary | `asia-southeast3` (Bangkok) | Keep `lock_audit_bucket = false` in workshop sandboxes for clean teardown; set `true` in production for WORM retention. |
| **Agent Identity `AuthProvider`** (`agentidentity.googleapis.com`) | B (Lab 08) | Service fallback | `asia-southeast1` (Singapore) | Agent Identity is GA in Singapore and not yet available in Bangkok. Set `auth_provider_location = "asia-southeast1"` in `terraform/security_governance/terraform.tfvars`. |
| **Agent Registry Standalone `Skills`** (`Skill`, `SkillRevision`, `Findings`) | A & B | API-required | `global` *(default)* or `us` / `eu` | Single-region `/skills` endpoints time out. Set `AGENT_REGISTRY_SKILLS_LOCATION=global` / `--skills-location global`. Middleware (`INCLUDE_PUBLIC_SKILLS=false`) filters to `private-` IDs only. Pass `--wait-for-scan` so async `ENABLED_BLOCKING` scans finish before promoting `defaultRevision`. |
| **Auto-Registered Google/GCP Remote MCP Servers** | A | API-required | `global` | Google-managed remote MCP servers register automatically under `locations/global`. |
| **Native Skill CMEK** (`SkillEncryptionSpec`, Preview) | B (Lab 07) | API-required | `us` / `eu` only | Encryption at rest only (`us` KMS for `us` skills, `europe` KMS for `eu` skills; requires zero existing skills). For APAC (`global` Skills), use Google-managed encryption plus Bangkok KMS signing. |
| **Network Services Agent Gateway + IAP v2 UAP** | B (Lab 08) | API-required | `us-central1` / `europe-west1` only | Keep `enable_agent_gateway = false` by default in APAC workshops. Set `agent_gateway_location = "us-central1"` or `"europe-west1"` if testing live Agent Gateway. |

---

## 3. Local Ports & Health Endpoints

| Service | Port | Local URLs | Start Command |
| :--- | :---: | :--- | :--- |
| **Mock Agent Registry `v1`** | `8090` | `http://127.0.0.1:8090/healthz` (`/v1/...`, `POST /_mock/reset`) | `make run-mock` |
| **Sample FinOps MCP Server** | `8091` | `http://127.0.0.1:8091/mcp` (`/health`, `/healthz`) | `make run-samples` |
| **Sample FinOps A2A v1.0 Agent** | `8092` | `http://127.0.0.1:8092/a2a` (`/a2a/http`, `/.well-known/agent-card.json`, `/health`) | `make run-samples` |
| **Polyglot Middleware** | `8080` | `http://127.0.0.1:8080/v1/catalog` (`/health`, `/v1/health`, `/v1/marketplace/{tool}.json`, `/mcp`) | `make run-middleware` |

> **Live Cloud Run health check tip:** Google Frontend intercepts external `https://*.run.app/healthz` calls and returns an HTML `404`. Always curl `/health` (or `/v1/health` on Middleware) against live Cloud Run URLs. `/healthz` still works locally on `127.0.0.1` and for internal Cloud Run probes.

---

## 4. `Makefile` Cheat Sheet

| Command | What It Does |
| :--- | :--- |
| `make preflight` | Checks `.venv`, Python 3.12+, local ports (`8080/8090/8091/8092`), `curl`, `jq`, `gcloud` (PATH, auth, ADC, `agent-registry`), and optional Terraform `>= 1.6.0`. |
| `make test` | Runs the full unit, schema, middleware, `hub_sync`, platform agent, docs, and security test suite. |
| `make test-e2e` | Runs live-ASGI end-to-end pipeline tests. |
| `make validate-hcl` | Checks Terraform HCL formatting across `terraform/` (skips if `terraform`/`hclfmt` are missing unless `STRICT_HCL=true`). |
| `make starter-check` | Runs all Lab 01–04 `# WORKSHOP-TODO` self-checks (`scripts/check_starter.py`). |
| `make smoke-local` | Runs a self-contained offline smoke test across Mock Registry, Samples, Middleware, Sync, and `hub_sync`. |
| `make run-local-stack` | Starts all local services (`:8090`, `:8091`, `:8092`, `:8080`) in one terminal. |
| `make run-mock` / `make run-samples` / `make run-middleware` | Starts each local server in its own terminal if you want separate logs. |
| `make demo-sync-local` | **Option 1 (Default):** Uploads `gcp-finops-toolkit` and `local-skills/` (`local-ops-toolkit`) to Agent Registry. |
| `make demo-sync-repo` | **Option 2:** Shallow-clones and uploads a Git repo (`SKILLS_REPO_URL`, `SKILLS_REPO_SUBDIR`). |
| `make demo-sync` | Uploads `gcp-finops-toolkit` only. |
| `make demo-hub-sync` | Syncs IDE configs into `/tmp/acme-agent-hub-demo-home` for Claude Code, Copilot, Codex, and Antigravity. |
| `make gcp-build` | Builds and pushes all 3 container images via Cloud Build in `asia-southeast3`. |
| `make gcp-deploy` | **Track 2:** Deploys Cloud Run + Services in `asia-southeast3` and Skills in `global` (`scripts/deploy_gcp.sh`). |
| `make gcp-validate` | Verifies live Cloud Run `/health`, Bangkok Services, `global` Skills, and 4-IDE feeds (`scripts/validate_live_gcp.sh`). |
| `make gcp-destroy` | Deletes workshop Cloud Run services, Agent Registry resources, and staging buckets (`scripts/destroy_gcp.sh`). |
| `make tf-core-plan` / `make tf-sec-plan` | Runs `terraform init` and `plan` for `terraform/core/` or standalone `terraform/security_governance/`. |

---

## 5. Lab Navigation

- **Workstream A (Core Platform, Middleware & IDE UX):** [Lab 00](labs/00-environment-setup.md) → [Lab 01](labs/01-author-agent-plugin.md) → [Lab 02](labs/02-register-agent-registry.md) → [Lab 03](labs/03-build-deploy-middleware.md) → [Lab 04](labs/04-ide-developer-experience.md) → [Lab 05](labs/05-platform-agents-gitlab-sot.md)
- **Workstream B (Standalone Security & Governance):** [Lab 06](labs/06-governance-scanning-lifecycle.md) → [Lab 07](labs/07-kms-encryption-and-signing.md) → [Lab 08](labs/08-access-control-and-audit.md)
- **Reference Docs:** [Guided Starter Exercises](../starter/README.md) · [Executive Brief](customer-usecase-3-brief.md) · [Technical Architecture](architecture.md) · [Workshop Home](../README.md)
