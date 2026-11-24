# Lab 00: Environment Setup & Deployment Readiness

**Workstream:** A (Core Platform & Middleware)  
**Duration:** 20 minutes  
**Prerequisites:** Python 3.12+, `curl`, `make`, and `jq` (recommended). Live GCP adds `gcloud` CLI (Track 2) and optional Terraform `>= 1.6.0` (Track 3). No local Docker is required.

> **New here?** Skim the 1-page [Attendee Quickstart](../attendee-quickstart.md) to pick your track (Track 1 Local, Track 2 Live GCP with `gcloud` + Cloud Build, or Track 3 Live GCP with Terraform).
>
> **Know your 3 content folders:** `starter/` holds your hands-on `# WORKSHOP-TODO` exercises for Labs 01–04, `starter-marketplace/` holds the working reference plugin (`gcp-finops-toolkit` + `.gitlab-ci.yml`), and `local-skills/` holds 3 ready-to-sync standalone skills (`cloud-run-debugger`, `terraform-iam-auditor`, `incident-postmortem-writer`).

---

## Files You'll Work With

*No `starter/` files to edit in Lab 00.* You will verify your local environment, run the offline smoke test, and preview what lives inside `starter/`.

| Role | File | What You'll Do |
| :--- | :--- | :--- |
| **Inspect** | [`WORKSHOP_SPEC.json`](../../WORKSHOP_SPEC.json) | Review default local ports (`8090`, `8091`, `8092`, `8080`) and APAC region defaults. |
| **Run** | [`Makefile`](../../Makefile) & [`scripts/preflight_check.sh`](../../scripts/preflight_check.sh) | Run `make preflight` and `make test` to verify `.venv` and local tooling. |
| **Run** | [`scripts/local_smoke_test.sh`](../../scripts/local_smoke_test.sh) & [`scripts/run_local_stack.sh`](../../scripts/run_local_stack.sh) | Run `make smoke-local`, then start the persistent local stack with `make run-local-stack`. |
| **Preview** | [`starter/README.md`](../../starter/README.md) | Run `make starter-check` to see the Labs 01–04 exercises you'll complete next. |

---

## Learning Objectives

By the end of this lab, you will:
- Verify Python 3.12+ inside `.venv`, pass `make preflight`, and pass `make test`.
- Run the offline smoke check (`make smoke-local`) and preview all Labs 01–04 exercises (`make starter-check`).
- Start the local mock stack (`make run-local-stack`) and confirm the mock Agent Registry responds on `:8090`.
- *(Optional, Track 2 & Track 3)* Authenticate `gcloud` and confirm project access before building images in [Lab 03](03-build-deploy-middleware.md).

---

## Step 1: Verify Python, Preflight, and Tests

Run every command from the repository root (`agent-registry-marketplace-workshop/`).

First, confirm `.venv` works and inspect the default ports and resource names:

```bash
.venv/bin/python --version
cat WORKSHOP_SPEC.json
```

If `.venv` does not exist yet on your laptop, create it and install packages from [`pyproject.toml`](../../pyproject.toml):

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

Now run the preflight check and the offline test suite:

```bash
make preflight
make test
```

**You should see:**
- `make preflight` prints `[OK]` for `python3`, `.venv`, `git`, `curl`, and local ports `8080/8090/8091/8092`.
- `make test` finishes with zero failures.

---

## Step 2: Run the Local Smoke Test and Preview `starter/`

Run the 1-command offline smoke test. It boots the local mock registry, sample MCP/A2A servers, and Middleware on isolated ports, uploads sample skills, installs configs for all 4 IDEs into a temp folder, and shuts everything down:

```bash
make smoke-local
```

Next, preview the hands-on exercises waiting for you in [`starter/`](../../starter/README.md):

```bash
make starter-check
```

**You should see:**
- `make smoke-local` passes end-to-end with zero errors.
- `make starter-check` prints `0/6 present` for Lab 01 (`starter/lab01_plugin_skeleton`), `0/4` for Lab 02, `0/3` for Lab 03, and `0/3` for Lab 04. Those are the exact files you will build in Labs 01–04.

---

## Step 3: Start the Persistent Local Stack

Labs 01–05 run against a local Mock Agent Registry (`:8090`), sample FinOps MCP/A2A servers (`:8091` / `:8092`), and Polyglot Middleware (`:8080`). Start all of them in one terminal:

```bash
make run-local-stack
```

Leave that terminal running. In a second terminal, verify the mock registry health endpoint and list the built-in publisher tiers:

```bash
export AGENT_REGISTRY_BASE_URL="http://127.0.0.1:8090/v1"
export GCP_PROJECT_ID="acme-agent-governance"
export GCP_LOCATION="asia-southeast3"

curl -fsSL http://127.0.0.1:8090/healthz | .venv/bin/python -m json.tool
curl -fsSL "${AGENT_REGISTRY_BASE_URL}/projects/${GCP_PROJECT_ID}/locations/${GCP_LOCATION}/publishers" \
  | .venv/bin/python -m json.tool
```

**You should see:**
- `/healthz` returns `{"status": "ok", "service": "mock-agent-registry"}`.
- `/publishers` returns three publisher tiers: `FIRST_PARTY` (`google-`), `THIRD_PARTY` (`partner-`), and `PRIVATE` (`private-`).

<details>
<summary>Prefer 4 separate terminals instead of <code>make run-local-stack</code>?</summary>

Open three terminals and run one server per window so you can watch each log stream on its own:

```bash
# Terminal 1: Mock Agent Registry v1 (:8090)
make run-mock

# Terminal 2: FinOps MCP server (:8091) and A2A v1.0 agent (:8092)
make run-samples

# Terminal 3: Polyglot Middleware (:8080)
make run-middleware
```

Use Terminal 4 for your `curl`, `tools.plugin_sync`, and `tools.hub_sync` commands.

</details>

---

## Going Live on GCP (Optional — Track 2 & Track 3)

Skip this section if you are running **Track 1 (Local Mode)**. You can complete every lab offline first and deploy to Google Cloud later.

> **Region note:** Live GCP runs Services, Cloud Run, and Cloud Build in Bangkok (`asia-southeast3`), standalone Skills in `global`, and Workstream B Agent Identity `AuthProvider` in Singapore (`auth_provider_location = "asia-southeast1"`). See the full table in [Attendee Quickstart](../attendee-quickstart.md#2-apac-region-guide).

Complete **authentication and preflight only** here in Lab 00. Wait until [Lab 03](03-build-deploy-middleware.md) to enable APIs, build images via Cloud Build, and deploy Cloud Run (that avoids fresh-project Terraform failures when images do not exist yet).

1. Sign in with `gcloud` and set your project and APAC region variables (leave `AGENT_REGISTRY_BASE_URL` and `AGENT_REGISTRY_SKILLS_LOCATION` unset for now; you will set them in the live-GCP sections of [Lab 02](02-register-agent-registry.md) and [Lab 03](03-build-deploy-middleware.md) so local `make` commands still target `127.0.0.1:8090`):

   ```bash
   export GCP_PROJECT_ID="<your-gcp-project-id>"
   export GCP_LOCATION="asia-southeast3"

   gcloud auth login --quiet
   gcloud auth application-default login --quiet
   gcloud config set project "${GCP_PROJECT_ID}" --quiet
   gcloud auth application-default set-quota-project "${GCP_PROJECT_ID}" --quiet
   ```

2. Confirm your project is active and re-run preflight:

   ```bash
   gcloud projects describe "${GCP_PROJECT_ID}" --project="${GCP_PROJECT_ID}" --quiet \
     --format="value(projectId,projectNumber,lifecycleState)"
   gcloud services list --enabled --project="${GCP_PROJECT_ID}" --limit=50 --quiet
   make preflight
   ```

> **Continuing in Local Mode next?** If you plan to run Local Mode `make` targets (`make demo-sync-local`, `make run-middleware`) in Labs 02–04 from this same shell, use a separate terminal or reset your shell back to the local mock defaults:
>
> ```bash
> unset AGENT_REGISTRY_BASE_URL AGENT_REGISTRY_SKILLS_LOCATION
> export GCP_PROJECT_ID="acme-agent-governance"
> ```

<details>
<summary>Track 3 only: prepare Terraform variables now (do not apply yet)</summary>

Verify HCL formatting and copy [`terraform/core/terraform.tfvars.example`](../../terraform/core/terraform.tfvars.example) to `terraform/core/terraform.tfvars`:

```bash
make validate-hcl
cp terraform/core/terraform.tfvars.example terraform/core/terraform.tfvars
```

Edit `terraform/core/terraform.tfvars` to replace `acme-agent-governance` with your own GCP project ID in `project_id`, `middleware_image`, `sample_mcp_image`, and `sample_a2a_image` (keeping the `:1.0.0` tag to match `scripts/build_images_cloudbuild.sh`), along with `location = "asia-southeast3"` and `skills_location = "global"`. Do not run `terraform apply` yet; you will run Stage 1 (enable APIs + Artifact Registry, then Cloud Build) first in [Lab 03](03-build-deploy-middleware.md).

</details>

---

## Verification Checkpoint

Before moving to Lab 01, confirm:

- [ ] `make preflight` passes with Python 3.12+ in `.venv` and ports `8080/8090/8091/8092` ready.
- [ ] `make test` passes with zero failures.
- [ ] `make smoke-local` passes and `make starter-check` lists Labs 01–04 cleanly.
- [ ] `http://127.0.0.1:8090/healthz` returns `{"status": "ok", "service": "mock-agent-registry"}`.
- [ ] *(Optional, Live GCP)* `gcloud projects describe "${GCP_PROJECT_ID}"` and `make preflight` succeed (with `GCP_LOCATION=asia-southeast3`).
- [ ] *(Optional, Track 3)* `make validate-hcl` passes on `terraform/core/` and `terraform/security_governance/`.

---

## Troubleshooting

| Symptom | Why It Happens | How to Fix |
| :--- | :--- | :--- |
| `ModuleNotFoundError` or stdlib import collision | Running system `python` or running outside the repo root | `cd` into the repository root and always use `.venv/bin/python`, `.venv/bin/pytest`, or `make` targets. |
| `Address already in use` on `8080`, `8090`, `8091`, or `8092` | An earlier workshop server is still running | Run `lsof -i :8090` (or the conflicting port) and stop that process first. |
| `gcloud: command not found` even though `make preflight` saw `$HOME/.local/bin/gcloud` | `$HOME/.local/bin` is missing from your shell `$PATH` | Run `export PATH="$HOME/.local/bin:$PATH"` and add it to `~/.bashrc` or `~/.zshrc`. |
| `terraform: command not found` | Terraform CLI is not installed locally | Use Track 1 (Local) or Track 2 (`make gcp-deploy`). `make validate-hcl` skips cleanly unless `STRICT_HCL=true`. |
| `Container image not found` during `terraform apply` | Ran `terraform/core apply` before building container images | Wait until [Lab 03](03-build-deploy-middleware.md) and run Stage 1 (`scripts/build_images_cloudbuild.sh`) first. |
| Live `https://*.run.app/healthz` returns an HTML `404` | Google Frontend reserves `/healthz` on external Cloud Run URLs | Curl `/health` (or `/v1/health` on Middleware) for live Cloud Run URLs. Keep `/healthz` for local `127.0.0.1` checks. |

---

**Next Lab:** [Lab 01: Author a Polyglot Agent Plugin →](01-author-agent-plugin.md)
