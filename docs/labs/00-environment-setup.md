# Lab 00: Environment Setup & Deployment Readiness

**Workstream:** A — Core Platform & Middleware  
**Duration:** 20 minutes  
**Prerequisites:** Python 3.12+, `curl`, `jq` (recommended), `make`. For Live GCP Mode: Google Cloud project with Billing enabled and `gcloud` CLI (Track 2 — zero Docker, zero Terraform), plus optional Terraform `>= 1.6.0` (`hashicorp/google >= 7.39.0`) for Track 3. **Zero local Docker is required.**

> **New to this workshop?** Read the 1-page **[Attendee Quickstart & APAC Regional Matrix](../attendee-quickstart.md)** first to pick your laptop track (**Track 1:** Offline Local, **Track 2:** Live GCP `gcloud` + Cloud Build, or **Track 3:** Live GCP Full Terraform IaC).

---

## Learning Objectives

By the end of this lab, you will:
1. Verify your Python virtual environment (`.venv`), run `make preflight`, and pass the full local unit and schema test suites (plus optional Terraform HCL formatting for Track 3).
2. Run a 1-command **Local Mock Mode** end-to-end smoke test (`make smoke-local`), and learn how to launch the offline stack either via `make run-local-stack` (single terminal) or across 4 dedicated terminals.
3. *(Optional — Live GCP Mode)* Authenticate `gcloud`, verify the **Bangkok (`asia-southeast3`)** primary + **Singapore (`asia-southeast1`)** secondary + **`global` Skills** regional split environment settings, and complete pre-flight checks — leaving API enablement, container builds, and Cloud Run deployment for [Lab 03](03-build-deploy-middleware.md) so fresh projects never hit chicken-and-egg Terraform failures.

---

## Step 1: Verify Your Local Workspace, Virtual Environment & Pre-Flight

Run all commands from the repository root (`agent-registry-marketplace-workshop/`):

```bash
# Confirm Python 3.12+ inside .venv
.venv/bin/python --version

# Inspect canonical ports, APAC region defaults, resource names, and URNs
cat WORKSHOP_SPEC.json
```

If `.venv` has not been created yet on a fresh machine, create it and install dependencies from [pyproject.toml](../../pyproject.toml):

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

Run the workshop pre-flight checker and full offline unit/schema test suite (zero Docker and zero Terraform required):

```bash
make preflight
make test
```

*(Track 3 Terraform IaC attendees can also verify HCL formatting with `make validate-hcl`.)*

---

## Step 2: Choose Your Execution Mode

Every lab in Workstream A and Workstream B supports **two modes** (with **zero local Docker**):
- **Local Mock Mode (Track 1 — Default):** Runs completely offline against `mock_registry` on `http://127.0.0.1:8090/v1`, faithfully simulating `private-` skill auto-prefixing, `ENABLED_BLOCKING` secret/injection scanning, `A2A_AGENT_CARD` validation, `bindings:fetchAvailable` IAM filtering, and `?alt=media` HTTP 302 signed-URL header enforcement.
- **Live GCP Mode (Track 2 `gcloud` + Cloud Build, or Track 3 Terraform IaC):** Targets `https://agentregistry.googleapis.com/v1` using the **verified live APAC regional split**: Cloud Run, Cloud Build, Artifact Registry, and Agent Registry **`Services`** (`McpServer`, `Agent`, `Endpoint`) run regionally in **Bangkok (`asia-southeast3`)** primary and optional **Singapore (`asia-southeast1`)** secondary/DR, while standalone **`Skills`** live once in **`global`** (default `AGENT_REGISTRY_SKILLS_LOCATION=global`, or `us`/`eu` for CMEK) and are shared seamlessly by both regions.

### Mode A: Local Mock Mode Smoke Test

#### Option A1 — 1-Command Automated Smoke Check (Fastest)

Run the self-contained end-to-end local smoke check (starts the local services ephemerally, syncs skills, verifies `/v1/catalog` and 4-IDE hydration, and shuts down cleanly):

```bash
make smoke-local
```

#### Option A2 — Persistent Local Stack (1-Terminal or 4-Terminal)

When you want servers running continuously across Labs 01–05, either launch all three local servers in a single terminal:

```bash
make run-local-stack
```

Or open dedicated terminals so you can watch each server's logs individually:

```bash
# Terminal 1 — Start the Mock Agent Registry v1 server on port 8090
make run-mock
```

In another terminal, verify the mock registry health endpoint and inspect the built-in Publisher tiers (using Bangkok `asia-southeast3` defaults):

```bash
export AGENT_REGISTRY_BASE_URL="http://127.0.0.1:8090/v1"
export GCP_PROJECT_ID="acme-agent-governance"
export GCP_LOCATION="asia-southeast3"

curl -fsSL http://127.0.0.1:8090/healthz | .venv/bin/python -m json.tool
curl -fsSL "${AGENT_REGISTRY_BASE_URL}/projects/${GCP_PROJECT_ID}/locations/${GCP_LOCATION}/publishers" \
  | .venv/bin/python -m json.tool
```

Verify that three publishers are returned: `FIRST_PARTY` (`google-`), `THIRD_PARTY` (`partner-`), and `PRIVATE` (`private-`).

### Mode B: Live GCP Mode — Authentication & Pre-Flight ONLY

> **Live GCP Journey Across Labs 00 → 03:** On a fresh GCP project, Cloud Run v2 requires container images to exist in Artifact Registry **before** `terraform -chdir=terraform/core apply` runs. Complete **authentication and pre-flight ONLY** here in Lab 00, author and test your plugins/skills locally in **[Lab 01](01-author-agent-plugin.md)** and **[Lab 02](02-register-agent-registry.md)**, and let **[Lab 03](03-build-deploy-middleware.md)** enable APIs, build images remotely via Cloud Build (zero local Docker!), deploy Cloud Run to Bangkok (`asia-southeast3`) + optional Singapore (`asia-southeast1`), register Services, and ingest Skills cleanly (via either **Track 2** `scripts/deploy_gcp.sh` or **Track 3** `scripts/build_images_cloudbuild.sh` + `terraform/core`).

1. Authenticate `gcloud` and set your **Bangkok (`asia-southeast3`)** primary, **Singapore (`asia-southeast1`)** secondary, and **`global` Skills** environment variables:
   ```bash
   export GCP_PROJECT_ID="<your-gcp-project-id>"
   export GCP_LOCATION="asia-southeast3"
   export GCP_SECONDARY_LOCATION="asia-southeast1"
   export AGENT_REGISTRY_SKILLS_LOCATION="global"
   export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"

   gcloud auth login --quiet
   gcloud auth application-default login --quiet
   gcloud config set project "${GCP_PROJECT_ID}" --quiet
   gcloud auth application-default set-quota-project "${GCP_PROJECT_ID}" --quiet
   ```

2. Verify billing, active credentials, and regional access via pre-flight:
   ```bash
   gcloud projects describe "${GCP_PROJECT_ID}" --project="${GCP_PROJECT_ID}" --quiet \
     --format="value(projectId,projectNumber,lifecycleState)"
   gcloud services list --enabled --project="${GCP_PROJECT_ID}" --limit=50 --quiet
   make preflight
   ```

3. *(Optional for Track 3 Terraform users)* Verify HCL formatting and copy [terraform/core/terraform.tfvars.example](../../terraform/core/terraform.tfvars.example) to `terraform/core/terraform.tfvars` now (fill in `project_id`, `location = "asia-southeast3"`, `secondary_location = "asia-southeast1"`, and `skills_location = "global"` — **do not run full `terraform apply` until Stage 1 Cloud Build completes in [Lab 03](03-build-deploy-middleware.md)**):
   ```bash
   make validate-hcl
   cp terraform/core/terraform.tfvars.example terraform/core/terraform.tfvars
   ```

---

## Verification Checkpoint

Before moving to Lab 01, confirm all checks pass:

- [ ] `make preflight` passes with Python 3.12+ inside `.venv` and local ports `8080/8090/8091/8092` ready.
- [ ] `make test` completes with zero failures.
- [ ] *(Track 3 Terraform only)* `make validate-hcl` passes cleanly on both `terraform/core/` and `terraform/security_governance/`.
- [ ] *(Track 1 Local Mode)* `make smoke-local` passes, or `curl -fsSL http://127.0.0.1:8090/healthz` returns `{"status": "ok", "service": "mock-agent-registry"}`.
- [ ] *(Track 2 / Track 3 Live GCP Mode)* `gcloud projects describe "${GCP_PROJECT_ID}" --project="${GCP_PROJECT_ID}" --quiet` succeeds and `GCP_LOCATION=asia-southeast3`, `GCP_SECONDARY_LOCATION=asia-southeast1`, and `AGENT_REGISTRY_SKILLS_LOCATION=global` are exported.

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `ModuleNotFoundError` or stdlib `calendar` shadow error | Running `python` outside the repository root or using system Python instead of `.venv` | `cd` into the repository root and always invoke `.venv/bin/python` / `.venv/bin/pytest` (or `make` targets). |
| `Address already in use` on port `8090`, `8091`, `8092`, or `8080` | A previous workshop server process is still listening | Run `lsof -i :8090` (or the conflicting port) and stop the old process before retrying. |
| `gcloud: command not found` even though `make preflight` found `$HOME/.local/bin/gcloud` | `gcloud` is installed under `$HOME/.local/bin`, which is not in your shell `$PATH` | Run `export PATH="$HOME/.local/bin:$PATH"` in your terminal (and add it to `~/.bashrc` or `~/.zshrc`) so bare `gcloud` commands across Labs 00–08 work directly. |
| `terraform: command not found` | Terraform CLI is not installed locally | Use **Track 1 (Offline Local Mode)** or **Track 2 (Live GCP `scripts/deploy_gcp.sh` — zero Docker & zero Terraform)**. `make validate-hcl` is only needed for **Track 3 (Terraform IaC)** and skips cleanly unless `STRICT_HCL=true` is set. |
| `Container image not found` during `terraform apply` | Attempted full `terraform/core apply` before building images | Wait until [Lab 03](03-build-deploy-middleware.md) Stage 1 (`scripts/build_images_cloudbuild.sh`) before applying Cloud Run resources. |
| External `https://*.run.app/healthz` returns Google 404 HTML page | Google Frontend (GFE) reserves `/healthz` on Cloud Run custom/default URLs | Always probe `/health` (or `/v1/health` on Middleware) on live Cloud Run URLs; `/healthz` works locally on `127.0.0.1` and for internal Cloud Run probes. |

---

**Next Lab:** [Lab 01: Author a Polyglot Agent Plugin →](01-author-agent-plugin.md)
