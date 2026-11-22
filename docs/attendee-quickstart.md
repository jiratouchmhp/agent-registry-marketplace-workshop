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
│       2. `scripts/deploy_gcp.sh --project "$GCP_PROJECT_ID" --primary-region asia-southeast3 [--enable-secondary]`
│          (or `make gcp-deploy GCP_PROJECT_ID=<ID> [ENABLE_SECONDARY=true]`)
│       3. `scripts/validate_live_gcp.sh` (or `make gcp-validate GCP_PROJECT_ID=<ID> [ENABLE_SECONDARY=true]`)
│       4. `scripts/destroy_gcp.sh`       (or `make gcp-destroy GCP_PROJECT_ID=<ID> [ENABLE_SECONDARY=true]` — always clean up!)
│
└── Have a GCP project + `gcloud` + Terraform >= 1.6.0 (Zero local Docker)?
    └── Track 3: Live GCP — Full IaC (Stage 1 Cloud Build -> Stage 2 Terraform)
        1. `make preflight`
        2. Stage 1 (Build images remotely first — avoids chicken-and-egg Terraform failures!):
           `scripts/build_images_cloudbuild.sh --project "$GCP_PROJECT_ID" --region asia-southeast3`
        3. Stage 2 (Provision Workstream A core stack — Bangkok primary + optional Singapore secondary):
           `make tf-core-plan && terraform -chdir=terraform/core apply`
        4. Optional Workstream B (Standalone Security & Governance — zero `terraform/core` state required):
           `make tf-sec-plan && terraform -chdir=terraform/security_governance apply`
```

### Track Comparison Matrix

| Track | Required Tools | GCP Account? | Local Docker? | Terraform? | Primary Commands |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Track 1: Offline Local Mode** | Python 3.12+ (`.venv`), `curl`, `make` | No | No | No | `make preflight`, `make smoke-local`, `make run-local-stack`, `make starter-check` |
| **Track 2: Live GCP (`gcloud` + Cloud Build)** | Track 1 + `gcloud` CLI | Yes | **No** (Cloud Build) | **No** | `scripts/deploy_gcp.sh --project <ID> --primary-region asia-southeast3 [--enable-secondary]`, `scripts/validate_live_gcp.sh`, `scripts/destroy_gcp.sh` |
| **Track 3: Live GCP (Full IaC)** | Track 2 + Terraform `>= 1.6.0` (`hashicorp/google >= 7.39.0`) | Yes | **No** (Cloud Build) | Yes | Stage 1: `scripts/build_images_cloudbuild.sh` → Stage 2: `terraform -chdir=terraform/core apply` |

> **Important Ordering Rule for Track 3 (Terraform):** Always run **Stage 1 (`scripts/build_images_cloudbuild.sh`) BEFORE Stage 2 (`terraform apply`)**. Deploying `terraform/core` on a fresh project before container images exist in Artifact Registry causes Cloud Run v2 creation to fail. [Lab 00](labs/00-environment-setup.md) handles authentication & pre-flight only; container builds and Cloud Run deployment happen in [Lab 03](labs/03-build-deploy-middleware.md).

---

## 2. Verified APAC Regional Availability Matrix (Bangkok + Singapore + `global` Skills)

Live deployments follow a **verified regional split**:
- **Bangkok (`asia-southeast3`, Primary) & Singapore (`asia-southeast1`, Secondary/DR):** Cloud Run v2, Cloud Build, Artifact Registry, Cloud Storage, Cloud KMS Asymmetric Signing (`EC_SIGN_P256_SHA256`), Cloud Logging Audit Bucket, and **Agent Registry `Services` (`McpServer`, `Agent`, `Endpoint`) & `Bindings`**.
- **`global` (Default for APAC — or `us` / `eu` for CMEK):** **Agent Registry Standalone `Skills` (`Skill`, `SkillRevision`, `Findings`)**, ingested once and shared seamlessly by both Bangkok primary and Singapore secondary Middleware (filtering automatically to private `private-*` skills so built-in Google first-party skills do not pollute enterprise catalogs).

| Capability / Component | Workstream | Verified Live Location(s) | Notes & Terraform / CLI Toggles |
| :--- | :---: | :--- | :--- |
| **Agent Registry `Services` & Discovery** (`McpServer`, `Agent`, `Endpoint`, `Bindings`) | A & B | **Bangkok (`asia-southeast3`)** Primary<br>**Singapore (`asia-southeast1`)** Secondary / DR | Default `GCP_LOCATION=asia-southeast3`, `GCP_SECONDARY_LOCATION=asia-southeast1`. Note: A2A v1.0 cards omit legacy `stateTransitionHistory` and split `JSONRPC` (`/a2a`) vs. `HTTP+JSON` (`/a2a/http`) onto unique URLs. |
| **Agent Registry Standalone `Skills`** (`Skill`, `SkillRevision`, `Findings`) | A & B | **`global`** *(default for APAC)*<br>or **`us` / `eu`** *(required for CMEK)* | Single-region locations (`asia-southeast3`/`asia-southeast1`) do not host standalone `Skills`. Set `AGENT_REGISTRY_SKILLS_LOCATION=global` / `skills_location = "global"` (`--skills-location global`). Middleware (`INCLUDE_PUBLIC_SKILLS=false`) filters strictly to `private-*` skills; pass `--wait-for-scan` so async `ENABLED_BLOCKING` scans finish before promoting `defaultRevision`. |
| **Cloud Run v2** (`polyglot-middleware`, `sample-mcp-server`, `sample-a2a-agent`) | A | **Bangkok (`asia-southeast3`)** Primary<br>**Singapore (`asia-southeast1`)** Secondary / DR | Deploy secondary region via `--enable-secondary` (Track 2) or `enable_secondary_region = true` (Track 3); test failover with `tools.hub_sync --fallback-middleware-url`. Probe **`/health`** (or `/v1/health`) externally because Google Frontend reserves `/healthz`. |
| **Artifact Registry & Cloud Build** (`cloudbuild/*.yaml`) | A | **Bangkok (`asia-southeast3`)** & **Singapore (`asia-southeast1`)** | Regional builds use `--region=asia-southeast3` (and `asia-southeast1` when secondary is enabled) with `--default-buckets-behavior=regional-user-owned-bucket`. |
| **Cloud Storage** (Skill Staging Bucket) | A | **Bangkok (`asia-southeast3`)** & **Singapore (`asia-southeast1`)** | P4SA granted `roles/storage.objectViewer` for `gcsSource` skill imports. |
| **Cloud KMS Asymmetric Signing** (`EC_SIGN_P256_SHA256`) | B (Lab 07) | **Bangkok (`asia-southeast3`)** & **Singapore (`asia-southeast1`)** | Regional key ring in `asia-southeast3` (or `asia-southeast1`) for CI artifact signing & Middleware fail-closed tamper rejection. |
| **Native Skill CMEK** (`SkillEncryptionSpec`, Preview) | B (Lab 07) | **`us` / `eu` multi-regions only** | Encryption at rest only (KMS in `us` for `us` skills, `europe` for `eu` skills; strict zero-skills precondition). For APAC (`global` Skills), use Google-managed encryption at rest + regional Cloud KMS `EC_SIGN_P256_SHA256` signing in `asia-southeast3`/`asia-southeast1`. |
| **Agent Identity `AuthProvider`** (`google_agent_identity_auth_provider`) | B (Lab 08) | **Singapore (`asia-southeast1`)** *(APAC GA)* | Bangkok (`asia-southeast3`) does not host Agent Identity yet. Set `auth_provider_location = "asia-southeast1"` in `terraform/security_governance/terraform.tfvars`. |
| **Network Services Agent Gateway + IAP v2 UAP** | B (Lab 08) | **`us-central1` / `europe-west1` only** | Keep `enable_agent_gateway = false` for APAC-only workshops; deploy with `agent_gateway_location = "us-central1"` or `"europe-west1"` when testing live Agent Gateway + IAP v2 UAP. |
| **Cloud Logging Audit Bucket** (`agent-governance-audit`) | B (Lab 08) | **Bangkok (`asia-southeast3`)** & **Singapore (`asia-southeast1`)** | Keep `lock_audit_bucket = false` in workshop sandboxes (clean teardown); set `lock_audit_bucket = true` in production for irreversible WORM retention. |

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
| `make gcp-build` | Build & push all 3 container images remotely via Cloud Build (`scripts/build_images_cloudbuild.sh`, zero local Docker; add `ENABLE_SECONDARY=true` for Singapore) |
| `make gcp-deploy` | Track 2 zero-Docker, zero-Terraform live GCP deploy to Bangkok `asia-southeast3` (add `ENABLE_SECONDARY=true` for Singapore secondary/DR) |
| `make gcp-validate` | Validate live GCP Cloud Run endpoints, Agent Registry `v1` resources, and 4-IDE feeds (`scripts/validate_live_gcp.sh`; add `ENABLE_SECONDARY=true` for failover test) |
| `make gcp-destroy` | Cleanly delete workshop Cloud Run services, Agent Registry skills/services, and staging resources (`scripts/destroy_gcp.sh`; add `ENABLE_SECONDARY=true` if secondary was deployed) |
| `make tf-core-plan` / `make tf-sec-plan` | Preview Workstream A (`terraform/core/`) or standalone Workstream B (`terraform/security_governance/`) plans |

---

## 5. Lab Navigation

- **Workstream A (Core Platform, Middleware & Multi-IDE UX):** [Lab 00](labs/00-environment-setup.md) → [Lab 01](labs/01-author-agent-plugin.md) → [Lab 02](labs/02-register-agent-registry.md) → [Lab 03](labs/03-build-deploy-middleware.md) → [Lab 04](labs/04-ide-developer-experience.md) → [Lab 05](labs/05-platform-agents-gitlab-sot.md)
- **Workstream B (Standalone Security & Governance — Zero `terraform/core` State Needed):** [Lab 06](labs/06-governance-scanning-lifecycle.md) → [Lab 07](labs/07-kms-encryption-and-signing.md) → [Lab 08](labs/08-access-control-and-audit.md)
- **Deep Dives & Exercises:** [Executive Brief](customer-usecase-3-brief.md) · [Technical Architecture](architecture.md) · [Guided Starter Exercises](../starter/README.md) · [Workshop Home](../README.md)
