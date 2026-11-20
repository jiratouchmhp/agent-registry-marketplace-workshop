---
name: agent-marketplace-workshop-guide
description: >-
  Guides developers and facilitators through the Enterprise Agent Marketplace
  Workshop (Workstream A Labs 00-05 and standalone Workstream B Labs 06-08),
  environment pre-flight, Local Mock vs. Live GCP routing, Option 1 Local
  Skills vs. Option 2 Centralized Git Repo selection, guided starter/
  exercises, and lab troubleshooting. Use when onboarding to this repository,
  choosing a workstream or execution mode, running the 4-terminal local stack
  or Makefile demo targets, coaching through starter/ # WORKSHOP-TODO exercises,
  verifying lab checkpoints, or mapping Customer Use Case 3 questions to labs.
  Don't use for deep 5-IDE plugin authoring (polyglot-agent-plugin-authoring),
  Agent Registry v1 API/CLI/GitLab CI lifecycle details
  (agent-registry-v1-ingest-and-lifecycle), middleware/hub_sync/ADK runtime
  engineering (marketplace-middleware-and-ide-delivery), or production
  KMS/IAP/IAM/WORM hardening (agent-registry-security-and-prod-hardening).
---

# Enterprise Agent Marketplace Workshop Guide

Navigate, facilitate, and execute the **Enterprise Agent Marketplace Workshop** backed by **Google Cloud Agent Registry (`v1` GA)**—from offline classroom smoke tests and guided `starter/` coding exercises to active repository development and live Google Cloud Terraform rollouts.

## Quick Decision Tree

```text
What is your goal in this repository?
├── First time here / pre-flight check / running the end-to-end demo locally
│   └── Run Pre-Flight (`make test` + `make validate-hcl`) -> 4-Terminal Local Mock Mode below
├── Choosing a workshop track
│   ├── Full platform, multi-IDE marketplace, middleware & ADK build (~3.5 hrs)
│   │   └── Workstream A: Labs 00 -> 05 (`docs/labs/00-*.md` .. `05-*.md`)
│   │       -> See references/lab-checkpoints-and-troubleshooting.md
│   └── Security, scanning, KMS CMEK/signing, IAM/IAP UAP & WORM audit only (~2 hrs)
│       └── Workstream B (Standalone — zero `terraform/core` dependency): Labs 06 -> 08
│           -> See references/lab-checkpoints-and-troubleshooting.md
├── Choosing how to ingest skills into Agent Registry (Lab 02 & Terraform bootstrap)
│   ├── Classroom / offline / zero external Git tokens (default)
│   │   └── Option 1: Local Skills (`local-skills/` + `starter-marketplace/`) via `make demo-sync-local`
│   │       -> See references/dual-source-ingestion-matrix.md
│   └── Bring-your-own centralized GitLab / GitHub repo without restructuring first
│       └── Option 2: Git Shallow-Clone & Ingest via `make demo-sync-repo` (`--source-type git`)
│           -> See references/dual-source-ingestion-matrix.md
├── Practicing hands-on coding exercises (`starter/`)
│   └── Labs 01-04 `# WORKSHOP-TODO` exercises -> Follow the Guided `starter/` Protocol below
├── Deploying to a real Google Cloud project (Live GCP Mode)
│   ├── Workstream A core stack -> `terraform/core/` (`make tf-core-plan`)
│   └── Workstream B security stack -> `terraform/security_governance/` (`make tf-sec-plan`)
└── Deep technical implementation outside workshop navigation?
    ├── 5-IDE plugin schemas, Antigravity rules/hooks/`agy plugin validate` -> `polyglot-agent-plugin-authoring`
    ├── Agent Registry `v1` CRUD, `private-`, `Service` specs, GitLab CI -> `agent-registry-v1-ingest-and-lifecycle`
    ├── Middleware `?alt=media` 302, `/mcp`, `hub_sync`, ADK orchestrator -> `marketplace-middleware-and-ide-delivery`
    └── Production IAM, KMS CMEK vs. signing, IAP v2 UAP, WORM audit -> `agent-registry-security-and-prod-hardening`
```

## Critical Gotchas ("Do This, Not That")

| # | Do This | Not That | Why |
| :---: | :--- | :--- | :--- |
| 1 | Run `.venv/bin/python`, `.venv/bin/pytest`, or `make <target>` from the repository root. | Invoking system `python`/`pytest` or running commands inside subdirectories. | Avoids `ModuleNotFoundError` and stdlib `calendar` shadowing; `Makefile` pins `PYTHON ?= .venv/bin/python` and `PYTEST ?= .venv/bin/pytest`. |
| 2 | Coach developers through `starter/` `# WORKSHOP-TODO` exercises with progressive hints and built-in self-checks first. | Immediately pasting complete solutions or editing `starter/` files without being asked. | `starter/lab01_plugin_skeleton/` and `starter/lab02..04_*_exercise.py` ship with built-in validators and `__main__` self-check harnesses meant for hands-on learning; reveal full answers only when requested. |
| 3 | Verify each lab's **Verification Checkpoint** (and its targeted `pytest` module) before advancing. | Skipping ahead to the next lab when an endpoint returns empty results or `rejectedSkills`. | Labs 01–05 build incrementally on live local state (`:8090`–`:8092`, `:8080`) or Terraform outputs; catching a stopped sample server or `STATE_DRAFT` skill early prevents cascading confusion. |
| 4 | Run **Workstream B (Labs 06–08)** standalone against `make run-mock` or directly via `terraform/security_governance/`. | Assuming Workstream A (`terraform/core`) must be applied first. | Workstream B has **zero runtime or state dependency** on `terraform/core/`; `terraform/security_governance/terraform.tfvars` accepts any existing project's SA emails and URNs. |
| 5 | Pass `SKILLS_REPO_SUBDIR=""` when running `make demo-sync-repo` on custom repos that do not store skills under `local-skills/`. | Leaving `SKILLS_REPO_SUBDIR` unset on a custom repo. | `Makefile` defaults `SKILLS_REPO_SUBDIR ?= local-skills`; leaving it set on a repo with plugins/skills at the root or `plugins/` fails with `Subdirectory 'local-skills' does not exist`. |
| 6 | Pass `--only-skills --skip-mcp-probe` when invoking `tools.plugin_sync ingest` against a Live GCP project managed by `terraform/core/`. | Running `ingest` without `--only-skills` alongside `terraform/core/agent_registry_services.tf`. | `agent_registry_services.tf` owns Cloud Run `google_agent_registry_service` resources; without `--only-skills`, local `127.0.0.1` URLs from `starter-marketplace/` can overwrite live Cloud Run endpoints (`skills_bootstrap.tf` passes both flags automatically). |
| 7 | Pass `--git-token-env <ENV_VAR_NAME>` (or `SKILLS_REPO_TOKEN_ENV` / `skills_repo_token_env`) for private Git repos. | Putting raw GitLab/GitHub tokens in URLs, CLI arguments, or `.tfvars` files. | `tools/plugin_sync/sources.py` injects the token via ephemeral, origin-scoped `GIT_CONFIG_*` `http.<origin>/.extraHeader` and scrubs credentials from `source_repo` metadata and error logs. |
| 8 | Keep `REQUIRE_KMS_SIGNATURE=false` during Workstream A local labs, and reset mock state via `POST http://127.0.0.1:8090/_mock/reset` between drills. | Enabling `REQUIRE_KMS_SIGNATURE=true` without a public key PEM, or reusing poisoned Lab 06 skills in later steps. | Middleware fails closed when `REQUIRE_KMS_SIGNATURE=true` lacks `KMS_PUBLIC_KEY_PEM_PATH`/`KMS_SIGNING_KEY_VERSION` (empty `plugins[]`); `mock_registry` holds in-memory state (`409 ALREADY_EXISTS` on duplicate creates). |
| 9 | Keep workspace skills in `.agents/skills/` distinct from catalog content in `local-skills/` and `starter-marketplace/`. | Uploading `.agents/skills/` into Agent Registry during labs or confusing them with sample skills. | `.agents/skills/` assists the developer inside Antigravity; `local-skills/` (`local-ops-toolkit`) and `starter-marketplace/` (`gcp-finops-toolkit`) are the sample assets ingested into Agent Registry. |

## Quick-Start Patterns

### 1. Environment Pre-Flight (Run First in Both Modes)

```bash
# Confirm repository root and Python 3.12+ virtualenv
.venv/bin/python --version

# Bootstrap .venv only if missing on a fresh workstation
# python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e .

# Run full offline unit/schema/integration suite and Terraform HCL formatting check
make test
make validate-hcl
```

### 2. Local Workshop Mode (4-Terminal Offline Stack — Zero GCP Needed)

Canonical ports from `WORKSHOP_SPEC.json`: Mock Registry `:8090`, Sample MCP Server `:8091`, Sample A2A Agent `:8092`, Polyglot Middleware `:8080`.

```bash
# Terminal 1 — Mock Google Cloud Agent Registry v1 API (:8090)
make run-mock

# Terminal 2 — Sample FinOps MCP Server (:8091) + A2A v1.0 Agent (:8092)
make run-samples

# Terminal 3 — Polyglot Marketplace Middleware (:8080, REQUIRE_KMS_SIGNATURE=false)
make run-middleware

# Terminal 4 — Ingest catalog + hydrate all 4 IDEs into isolated /tmp/acme-agent-hub-demo-home
# Option 1 (Offline default): gcp-finops-toolkit + 3 standalone local-skills/ (local-ops-toolkit)
make demo-sync-local
# Or Option 1 (starter plugin only): make demo-sync
# Or Option 2 (Centralized Git repo clone & ingest; set SKILLS_REPO_SUBDIR="" for root auto-detect):
# make demo-sync-repo

make demo-hub-sync
```

Inspect live endpoints immediately:

```bash
curl -fsSL http://127.0.0.1:8090/healthz | .venv/bin/python -m json.tool
curl -fsSL http://127.0.0.1:8080/v1/catalog | .venv/bin/python -m json.tool
curl -fsSL http://127.0.0.1:8080/v1/marketplace/antigravity.json | .venv/bin/python -m json.tool
```

### 3. Guided `starter/` Exercise Coaching Protocol (Labs 01–04)

When a developer is working through `starter/` exercises:
1. Point them to the exact `# WORKSHOP-TODO(lab-XX)` target and run the built-in offline check first.
2. Give conceptual hints and point to relevant spec constraints before showing code; only reveal or compare with reference implementations (`starter-marketplace/`, `tools/plugin_sync/`, `middleware/`, `tools/hub_sync/`) when explicitly asked.

```bash
# Lab 01: Validate starter/lab01_plugin_skeleton against Agent Plugins 1.0 + Antigravity v1
.venv/bin/python - << 'EOF'
from schemas import validate_agent_plugin_bundle, validate_antigravity_bundle
print("Agent Plugins 1.0:", validate_agent_plugin_bundle("starter/lab01_plugin_skeleton"))
print("Antigravity v1   :", validate_antigravity_bundle("starter/lab01_plugin_skeleton"))
EOF

# Labs 02-04: Run built-in __main__ self-check harnesses
.venv/bin/python starter/lab02_plugin_sync_exercise.py
.venv/bin/python starter/lab03_middleware_redirect_exercise.py
.venv/bin/python starter/lab04_hub_sync_adapter_exercise.py
```

### 4. Live GCP Production Mode Handoff

Never run `terraform apply` or mutate live `https://agentregistry.googleapis.com/v1` without previewing (`--dry-run` / `plan`) and confirming target `${GCP_PROJECT_ID}` and `${GCP_LOCATION}`:

```bash
# Authenticate and target live Google Cloud
export GCP_PROJECT_ID="<your-gcp-project-id>"
export GCP_LOCATION="us-central1"
export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"
gcloud auth login && gcloud auth application-default login
gcloud config set project "${GCP_PROJECT_ID}"

# Workstream A — Core infrastructure (keep allow_unauthenticated_workshop_demo = false in shared/prod)
cp terraform/core/terraform.tfvars.example terraform/core/terraform.tfvars
# Edit terraform/core/terraform.tfvars (project_id, container images, skills_source_mode = "local"|"git")
make tf-core-plan
# terraform -chdir=terraform/core apply

# Workstream B — Standalone security & governance (no terraform/core state dependency)
cp terraform/security_governance/terraform.tfvars.example terraform/security_governance/terraform.tfvars
export TF_VAR_auth_provider_client_secret="<your-oauth-client-secret>"
make tf-sec-plan
# terraform -chdir=terraform/security_governance apply
```

For deep production hardening (CMEK `us`/`eu` zero-skills precondition, Cloud KMS `EC_SIGN_P256_SHA256`, IAP v2 UAP `DRY_RUN` → enforcement, WORM audit logs, GitLab OIDC/WIF), hand off to **`agent-registry-security-and-prod-hardening`**.

## Verification Checklist

Run these checks to confirm workshop readiness before starting or after completing a workstream:

- [ ] **Pre-flight unit & schema tests pass:** `make test` exits `0` with zero failures.
- [ ] **Terraform HCL formatting clean:** `make validate-hcl` passes across `terraform/core/` and `terraform/security_governance/`.
- [ ] **End-to-end pipeline verified offline:** `make test-e2e` exits `0`.
- [ ] **Local mock stack healthy (Local Mode):**
  - `curl -fsSL http://127.0.0.1:8090/healthz` returns `{"status": "ok", "service": "mock-agent-registry"}`.
  - `curl -fsSL http://127.0.0.1:8080/healthz` returns `"status": "ok"`.
  - `curl -fsSL http://127.0.0.1:8080/v1/catalog` lists `gcp-finops-toolkit` (and `local-ops-toolkit` after `make demo-sync-local` or `central-repo-skills` after `make demo-sync-repo`).
- [ ] **All 4 IDEs hydrated in demo home:** `.venv/bin/python -m tools.hub_sync status --home /tmp/acme-agent-hub-demo-home` reports `claude`, `copilot`, `codex`, and `antigravity` as `(wired, present)` with `[OK]` plugin digests.
- [ ] **Workstream B standalone suite passes:** `.venv/bin/pytest -v tests/test_workstream_b_security.py` exits `0`.

## Reference Guides

Load only the specific reference needed for the current question:

| Reference File | Load When You Need... |
| :--- | :--- |
| [references/lab-checkpoints-and-troubleshooting.md](references/lab-checkpoints-and-troubleshooting.md) | Complete Lab 00–08 map (durations, key files, exact verification commands, expected outputs), `starter/` exercise index, and fast error-to-fix troubleshooting matrix across Workstreams A & B. |
| [references/dual-source-ingestion-matrix.md](references/dual-source-ingestion-matrix.md) | Option 1 (Local Skills) vs. Option 2 (Centralized Git Repo Clone & Ingest) vs. push-based GitLab CI comparison, auto-detected layouts, exact `Makefile` & CLI flags, Terraform `skills_source_mode` config, and Customer Use Case 3 Q1–Q4 mapping. |
