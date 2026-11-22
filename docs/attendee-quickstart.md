# Attendee Quickstart: Decision Tree, APAC Regional Matrix & Cheat Sheet

Choose your track based on what is installed on your laptop — **zero local Docker is required for any track** (all live GCP container builds run remotely in Google Cloud Build).

---

## 1. Choose Your Track (Laptop Environment Decision Tree)

```text
Start here -> Run `make preflight` from the repository root
├── Do you want to run 100% offline (No GCP project, No Docker, No Terraform)?
│   └── Track 1: Offline Local Mode (Recommended first 5 minutes for everyone)
│       1. `make preflight`
│       2. `make smoke-local`             # 1-command automated end-to-end smoke check
│       3. `make run-local-stack`         # Or 4 terminals: run-mock, run-samples, run-middleware, demo-*
│       4. `make starter-check`           # Hands-on Labs 01-04 # WORKSHOP-TODO exercises
│
├── Have a GCP project + `gcloud`, but NO Docker & NO Terraform?
│   └── Track 2: Live GCP — Zero-Docker, Zero-Terraform (`gcloud` + Cloud Build)
│       1. `make preflight`
│       2. `scripts/deploy_gcp.sh --project "$GCP_PROJECT_ID" --region asia-southeast3 --skills-location global`
│          (or `make gcp-deploy GCP_PROJECT_ID=<ID>`)
│       3. `scripts/validate_live_gcp.sh --project "$GCP_PROJECT_ID" --region asia-southeast3 --skills-location global`
│          (or `make gcp-validate GCP_PROJECT_ID=<ID>`)
│       4. `scripts/destroy_gcp.sh --project "$GCP_PROJECT_ID" --region asia-southeast3 --skills-location global`
│          (or `make gcp-destroy GCP_PROJECT_ID=<ID>` — always clean up!)
│
└── Have a GCP project + `gcloud` + Terraform >= 1.6.0 (Zero local Docker)?
    └── Track 3: Live GCP — Full IaC (Stage 1 Cloud Build -> Stage 2 Terraform)
        1. `make preflight`
        2. Stage 1 (Build images remotely first — avoids chicken-and-egg Terraform failures!):
           `scripts/build_images_cloudbuild.sh --project "$GCP_PROJECT_ID" --region asia-southeast3`
        3. Stage 2 (Provision single Workstream A core stack in Bangkok `asia-southeast3` + `global` Skills):
           `make tf-core-plan && terraform -chdir=terraform/core apply`
        4. Optional Workstream B (Standalone Security & Governance — zero `terraform/core` state required):
           `make tf-sec-plan && terraform -chdir=terraform/security_governance apply`
```

### Track Comparison Matrix

| Track | Required Tools | GCP Account? | Local Docker? | Terraform? | Primary Commands |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Track 1: Offline Local Mode** | Python 3.12+ (`.venv`), `curl`, `make` | No | No | No | `make preflight`, `make smoke-local`, `make run-local-stack`, `make starter-check` |
| **Track 2: Live GCP (`gcloud` + Cloud Build)** | Track 1 + `gcloud` CLI | Yes | **No** (Cloud Build) | **No** | `scripts/deploy_gcp.sh --project <ID> --region asia-southeast3 --skills-location global`, `scripts/validate_live_gcp.sh`, `scripts/destroy_gcp.sh` |
| **Track 3: Live GCP (Full IaC)** | Track 2 + Terraform `>= 1.6.0` (`hashicorp/google >= 7.39.0`) | Yes | **No** (Cloud Build) | Yes | Stage 1: `scripts/build_images_cloudbuild.sh` → Stage 2: `terraform -chdir=terraform/core apply` |

> **Important Ordering Rule for Track 3 (Terraform):** Always run **Stage 1 (`scripts/build_images_cloudbuild.sh`) BEFORE Stage 2 (`terraform apply`)**. Deploying `terraform/core` on a fresh project before container images exist in Artifact Registry causes Cloud Run v2 creation to fail. [Lab 00](labs/00-environment-setup.md) handles authentication & pre-flight only; container builds and Cloud Run deployment happen in [Lab 03](labs/03-build-deploy-middleware.md).

---

## 2. Verified APAC Regional Placement Matrix (Bangkok Primary + Service-Specific Fallbacks)

Live deployments run a **single, clean deployment anchored in Bangkok (`asia-southeast3`)** — with **no duplicate multi-region stacks** — falling back to Singapore or Global/Multi-Region **only** where a specific Google Cloud service or API requires it:
- **Bangkok (`asia-southeast3`) — Primary Region:** Cloud Run v2 (`sample-mcp-server`, `sample-a2a-agent`, `polyglot-middleware`), Cloud Build (`--region=asia-southeast3`), Artifact Registry (`asia-southeast3-docker.pkg.dev`), Agent Registry `Services` (`finops-mcp-server`, `finops-advisor-agent`, `finops-billing-api` → `McpServer`, `Agent`, `Endpoint`) & `Bindings`, Cloud Storage skill staging bucket, Cloud KMS Asymmetric Signing (`EC_SIGN_P256_SHA256`), and Cloud Logging audit bucket.
- **Singapore (`asia-southeast1`) — Service-Specific Fallback ONLY:** Workstream B **Agent Identity Auth Manager (`agentidentity.googleapis.com` → `google_agent_identity_auth_provider`)** uses `auth_provider_location = "asia-southeast1"` because Agent Identity is GA in Singapore and not yet available in Bangkok.
- **Global / Multi-Region (API-Required):** Agent Registry Standalone **`Skills`** (`global` default, or `us`/`eu` for CMEK — single-region `/skills` endpoints time out; automatically filtered to `private-*` skills), auto-registered Google/GCP remote MCP servers (`global`), optional native Skill CMEK (`us`/`eu` multi-regions only), and optional Network Services Agent Gateway (`us-central1`/`europe-west1` only; `enable_agent_gateway = false` by default for APAC).

| Capability / Component | Workstream | Placement Tier | Verified Live Location | Notes & Terraform / CLI Toggles |
| :--- | :---: | :--- | :--- | :--- |
| **Cloud Run v2** (`sample-mcp-server`, `sample-a2a-agent`, `polyglot-middleware`) | A | Primary | **Bangkok (`asia-southeast3`)** | Default `GCP_LOCATION=asia-southeast3` / `--region asia-southeast3`. Probe **`/health`** (or `/v1/health`) externally because Google Frontend reserves `/healthz`. |
| **Cloud Build & Artifact Registry** (`cloudbuild/*.yaml`) | A | Primary | **Bangkok (`asia-southeast3`)** | Regional builds use `--region=asia-southeast3` with `--default-buckets-behavior=regional-user-owned-bucket` and `asia-southeast3-docker.pkg.dev`. |
| **Agent Registry `Services` & `Bindings`** (`finops-mcp-server`, `finops-advisor-agent`, `finops-billing-api` → `McpServer`, `Agent`, `Endpoint`) | A & B | Primary | **Bangkok (`asia-southeast3`)** | Default `location = "asia-southeast3"`. Note: A2A v1.0 cards omit legacy `stateTransitionHistory` and split `JSONRPC` (`/a2a`) vs. `HTTP+JSON` (`/a2a/http`) onto unique URLs. |
| **Cloud Storage** (Skill Staging Bucket) | A | Primary | **Bangkok (`asia-southeast3`)** | P4SA granted `roles/storage.objectViewer` for `gcsSource` skill imports. |
| **Cloud KMS Asymmetric Signing** (`EC_SIGN_P256_SHA256`) | B (Lab 07) | Primary | **Bangkok (`asia-southeast3`)** | Regional key ring in `asia-southeast3` for CI artifact signing & Middleware fail-closed tamper rejection. |
| **Cloud Logging Audit Bucket** (`agent-governance-audit`) | B (Lab 08) | Primary | **Bangkok (`asia-southeast3`)** | Keep `lock_audit_bucket = false` in workshop sandboxes (clean teardown); set `lock_audit_bucket = true` in production for irreversible WORM retention. |
| **Agent Identity Auth Manager (`AuthProvider`)** (`agentidentity.googleapis.com` → `google_agent_identity_auth_provider`) | B (Lab 08) | **Service-Specific Fallback** | **Singapore (`asia-southeast1`)** | Agent Identity is GA in Singapore and not yet available in Bangkok. Set `auth_provider_location = "asia-southeast1"` in `terraform/security_governance/terraform.tfvars`. |
| **Agent Registry Standalone `Skills`** (`Skill`, `SkillRevision`, `Findings`) | A & B | **API-Mandated Global / Multi-Region** | **`global`** *(default)* or **`us` / `eu`** *(CMEK)* | API supports `global`, `us`, `eu` only (single-region `/skills` endpoints time out). Set `AGENT_REGISTRY_SKILLS_LOCATION=global` / `skills_location = "global"` (`--skills-location global`). Middleware (`INCLUDE_PUBLIC_SKILLS=false`) filters strictly to `private-*` skills; pass `--wait-for-scan` so async `ENABLED_BLOCKING` scans finish before promoting `defaultRevision`. |
| **Auto-Registered Google/GCP Remote MCP Servers** | A | **API-Mandated Global** | **`global`** | Google-managed remote MCP servers auto-register in `locations/global`. |
| **Native Skill CMEK** (`SkillEncryptionSpec`, Preview) | B (Lab 07) | **API-Mandated Multi-Region** | **`us` / `eu` multi-regions only** | Encryption at rest only (KMS in `us` for `us` skills, `europe` for `eu` skills; strict zero-skills precondition). For APAC (`global` Skills), use Google-managed encryption at rest + regional Cloud KMS `EC_SIGN_P256_SHA256` signing in `asia-southeast3`. |
| **Network Services Agent Gateway + IAP v2 UAP** | B (Lab 08) | **API-Mandated Regional Preview/GA** | **`us-central1` / `europe-west1` only** | Keep `enable_agent_gateway = false` by default for APAC workshops; deploy with `agent_gateway_location = "us-central1"` or `"europe-west1"` when testing live Agent Gateway + IAP v2 UAP. |

---

## 3. Quick Port Map & Local Endpoints

| Service | Local Port | Health / Primary URL | Start Target |
| :--- | :---: | :--- | :--- |
| **Mock Agent Registry `v1`** | `8090` | `http://127.0.0.1:8090/healthz` (`/v1/...`, `POST /_mock/reset`) | `make run-mock` |
| **Sample FinOps MCP Server** | `8091` | `http://127.0.0.1:8091/mcp` (`/health`, `/healthz`) | `make run-samples` |
| **Sample FinOps A2A v1.0 Agent** | `8092` | `http://127.0.0.1:8092/a2a` (`/a2a/http`, `/.well-known/agent-card.json`, `/health`) | `make run-samples` |
| **Polyglot Marketplace Middleware** | `8080` | `http://127.0.0.1:8080/v1/catalog` (`/health`, `/v1/health`, `/v1/marketplace/{tool}.json`, `/mcp`) | `make run-middleware` |

> **Live Cloud Run Health Probe Tip:** Google Frontend (GFE) intercepts external `https://*.run.app/healthz` requests with an HTML `404` before reaching the container. Always probe **`/health`** (or `/v1/health` on Middleware) on live Cloud Run URLs; `/healthz` remains active for local `127.0.0.1` testing and internal Cloud Run probes.

---

## 4. 1-Command `Makefile` Cheat Sheet

| Target | Purpose |
| :--- | :--- |
| `make preflight` | Verify `.venv`, Python 3.12+, local port readiness (`8080/8090/8091/8092`), `curl`/`jq`/`gcloud` (PATH, auth, ADC, `agent-registry` CLI), and optional Terraform `>= 1.6.0` (`scripts/preflight_check.sh`) |
| `make test` | Run the full unit, schema, middleware, `hub_sync`, platform agent, docs, and security test suite |
| `make test-e2e` | Run live-ASGI end-to-end pipeline tests |
| `make validate-hcl` | Verify Terraform HCL formatting across `terraform/` *(Track 3 Terraform IaC; skips cleanly if `terraform`/`hclfmt` are absent unless `STRICT_HCL=true`)* |
| `make starter-check` | Run all Lab 01–04 `# WORKSHOP-TODO` self-checks (`scripts/check_starter.py`) |
| `make smoke-local` | Run a 1-command automated offline smoke test across Mock Registry, Samples, Middleware, Sync, and `hub_sync` |
| `make run-local-stack` | Launch the full local offline stack in one command (alternative to opening 4 separate terminals) |
| `make run-mock` / `make run-samples` / `make run-middleware` | Start individual local servers in separate terminals (`:8090`, `:8091`/`:8092`, `:8080`) |
| `make demo-sync-local` | Option 1 (Default): Ingest `gcp-finops-toolkit` + `local-skills/` (`local-ops-toolkit`) into Agent Registry |
| `make demo-sync-repo` | Option 2: Shallow-clone & ingest a centralized GitLab/GitHub repo (`SKILLS_REPO_URL`, `SKILLS_REPO_SUBDIR`) |
| `make demo-sync` | Legacy single-plugin sync for `gcp-finops-toolkit` only |
| `make demo-hub-sync` | Hydrate `/tmp/acme-agent-hub-demo-home` for Claude Code, Copilot/VS Code, Codex, and Antigravity |
| `make gcp-build` | Build & push all 3 container images remotely via Cloud Build to Bangkok `asia-southeast3` (`scripts/build_images_cloudbuild.sh`, zero local Docker) |
| `make gcp-deploy` | Track 2 zero-Docker, zero-Terraform live GCP deploy to Bangkok `asia-southeast3` + `global` Skills (`scripts/deploy_gcp.sh`) |
| `make gcp-validate` | Validate live GCP Cloud Run endpoints (`/health`), Bangkok Agent Registry `v1` Services + `global` Skills, and 4-IDE feeds (`scripts/validate_live_gcp.sh`) |
| `make gcp-destroy` | Cleanly delete workshop Cloud Run services, Agent Registry skills/services, and staging resources in Bangkok `asia-southeast3` + `global` (`scripts/destroy_gcp.sh`) |
| `make tf-core-plan` / `make tf-sec-plan` | Preview Workstream A (`terraform/core/`) or standalone Workstream B (`terraform/security_governance/`) plans |

---

## 5. Lab Navigation

- **Workstream A (Core Platform, Middleware & Multi-IDE UX):** [Lab 00](labs/00-environment-setup.md) → [Lab 01](labs/01-author-agent-plugin.md) → [Lab 02](labs/02-register-agent-registry.md) → [Lab 03](labs/03-build-deploy-middleware.md) → [Lab 04](labs/04-ide-developer-experience.md) → [Lab 05](labs/05-platform-agents-gitlab-sot.md)
- **Workstream B (Standalone Security & Governance — Zero `terraform/core` State Needed):** [Lab 06](labs/06-governance-scanning-lifecycle.md) → [Lab 07](labs/07-kms-encryption-and-signing.md) → [Lab 08](labs/08-access-control-and-audit.md)
- **Deep Dives & Exercises:** [Executive Brief](customer-usecase-3-brief.md) · [Technical Architecture](architecture.md) · [Guided Starter Exercises](../starter/README.md) · [Workshop Home](../README.md)
