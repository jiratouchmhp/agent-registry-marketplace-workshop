# Lab 00: Environment Setup & Deployment Readiness

**Workstream:** A — Core Platform & Middleware  
**Duration:** 20 minutes  
**Prerequisites:** Python 3.12+, `curl`, `jq` (recommended), `make`. For Live GCP Mode: Google Cloud project with Billing enabled, `gcloud` CLI, Docker or Cloud Build, and Terraform `>= 1.6.0` (`hashicorp/google >= 7.39.0`).

---

## Learning Objectives

By the end of this lab, you will:
1. Verify your Python virtual environment (`.venv`) and pass the full local unit, schema, and HCL validation suites.
2. Run a 2-minute **Local Mock Mode** smoke test using [mock_registry/app.py](../../mock_registry/app.py) (`:8090`), sample FinOps MCP/A2A services (`:8091`, `:8092`), and the Polyglot Middleware (`:8080`).
3. *(Optional — Live GCP Mode)* Configure and preview [terraform/core/](../../terraform/core/variables.tf) to provision Agent Registry `v1`, Cloud Run services, Artifact Registry, GCS skill staging, and least-privilege IAM.

---

## Step 1: Verify Your Local Workspace & Virtual Environment

Run all commands from the repository root (`agent-registry-marketplace-workshop/`):

```bash
# Confirm Python 3.12+ inside .venv
.venv/bin/python --version

# Inspect canonical ports, resource names, and URNs
cat WORKSHOP_SPEC.json
```

If `.venv` has not been created yet on a fresh machine, create it and install dependencies from [pyproject.toml](../../pyproject.toml):

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

Run the workshop test suite and Terraform HCL formatting check:

```bash
make test
make validate-hcl
```

---

## Step 2: Choose Your Execution Mode

Every lab in Workstream A and Workstream B supports **two modes**:
- **Local Mock Mode (Default):** Runs completely offline against `mock_registry` on `http://127.0.0.1:8090/v1`, faithfully simulating `private-` skill auto-prefixing, `ENABLED_BLOCKING` secret/injection scanning, `A2A_AGENT_CARD` validation, `bindings:fetchAvailable` IAM filtering, and `?alt=media` HTTP 302 signed-URL header enforcement.
- **Live GCP Mode:** Targets `https://agentregistry.googleapis.com/v1` in your Google Cloud project using `gcloud`, REST `v1`, and GA Terraform (`hashicorp/google >= 7.39.0`).

### Mode A: Local Mock Mode Smoke Test

Open two terminals (or run `make run-mock` in the background):

```bash
# Terminal 1 — Start the Mock Agent Registry v1 server on port 8090
make run-mock
```

In Terminal 2, verify the mock registry health endpoint and inspect the built-in Publisher tiers:

```bash
export AGENT_REGISTRY_BASE_URL="http://127.0.0.1:8090/v1"
export GCP_PROJECT_ID="acme-agent-governance"
export GCP_LOCATION="us-central1"

curl -fsSL http://127.0.0.1:8090/healthz | .venv/bin/python -m json.tool
curl -fsSL "${AGENT_REGISTRY_BASE_URL}/projects/${GCP_PROJECT_ID}/locations/${GCP_LOCATION}/publishers" \
  | .venv/bin/python -m json.tool
```

Verify that three publishers are returned: `FIRST_PARTY` (`google-`), `THIRD_PARTY` (`partner-`), and `PRIVATE` (`private-`).

### Mode B: Live GCP Mode (`terraform/core`)

1. Authenticate `gcloud` and set your project environment variables:
   ```bash
   export GCP_PROJECT_ID="<your-gcp-project-id>"
   export GCP_LOCATION="us-central1"
   export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"

   gcloud auth login
   gcloud auth application-default login
   gcloud config set project "${GCP_PROJECT_ID}"
   ```

2. Create `terraform/core/terraform.tfvars` from [terraform/core/terraform.tfvars.example](../../terraform/core/terraform.tfvars.example):
   ```bash
   cp terraform/core/terraform.tfvars.example terraform/core/terraform.tfvars
   ```
   Edit `terraform/core/terraform.tfvars` and set:
   - `project_id = "<your-gcp-project-id>"`
   - `location = "us-central1"`
   - `allow_unauthenticated_workshop_demo = false` *(keep `false` in shared projects; set `true` only in isolated sandboxes if you want unauthenticated IDE access to Cloud Run demo endpoints)*
   - `enable_skills_bootstrap = true` *(bootstraps standalone skills via `terraform_data` because `hashicorp/google >= 7.39.0` has native resources for `Service` and `Binding` but not `Skill` yet)*

3. Preview the core infrastructure plan (ensure container images are built and pushed to Artifact Registry first, or follow [Lab 03](03-build-deploy-middleware.md) for container builds):
   ```bash
   make tf-core-plan
   ```

4. Apply Workstream A core infrastructure when ready:
   ```bash
   terraform -chdir=terraform/core apply
   terraform -chdir=terraform/core output
   ```

Key outputs exported by [terraform/core/outputs.tf](../../terraform/core/outputs.tf):
- `agent_registry_p4sa` (`service-<PROJECT_NUMBER>@gcp-sa-agentregistry.iam.gserviceaccount.com`)
- `middleware_url` & `middleware_mcp_url`
- `middleware_sa_email` (`roles/agentregistry.viewer` ONLY)
- `ci_publisher_sa_email` (`roles/agentregistry.editor`)
- `mcp_registry_resource`, `agent_registry_resource`, `endpoint_registry_resource`
- `mcp_server_urn`, `a2a_agent_urn`, `endpoint_urn`

---

## Verification Checkpoint

Before moving to Lab 01, confirm all checks pass:

- [ ] `make test` completes with zero failures.
- [ ] `make validate-hcl` passes cleanly on both `terraform/core/` and `terraform/security_governance/`.
- [ ] *(Local Mode)* `curl -fsSL http://127.0.0.1:8090/healthz` returns `{"status": "ok", "service": "mock-agent-registry"}`.
- [ ] *(Live GCP Mode)* `terraform -chdir=terraform/core output middleware_url` returns a valid `https://...run.app` URL.

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `ModuleNotFoundError` or stdlib `calendar` shadow error | Running `python` outside the repository root or using system Python instead of `.venv` | `cd` into the repository root and always invoke `.venv/bin/python` / `.venv/bin/pytest` (or `make` targets). |
| `Address already in use` on port `8090`, `8091`, `8092`, or `8080` | A previous workshop server process is still listening | Run `lsof -i :8090` (or the conflicting port) and stop the old process before retrying. |
| `terraform: command not found` | Terraform CLI is not installed locally | Run in **Local Mock Mode** (`make run-mock`, `make validate-hcl` works via `hclfmt`) or install Terraform `>= 1.6.0`. |
| `FAILED_PRECONDITION` or API not enabled in Live GCP Mode | `agentregistry.googleapis.com` is still propagating after enablement | Wait 60 seconds after enabling APIs in [terraform/core/apis.tf](../../terraform/core/apis.tf) and re-run `terraform apply`. |

---

**Next Lab:** [Lab 01: Author a Polyglot Agent Plugin →](01-author-agent-plugin.md)
