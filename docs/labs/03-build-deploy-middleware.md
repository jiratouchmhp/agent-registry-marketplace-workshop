# Lab 03: Build and Deploy the Polyglot Marketplace Middleware

**Workstream:** A (Core Platform & Middleware)  
**Duration:** 40 minutes  
**Prerequisites:** Completed [Lab 02](02-register-agent-registry.md) with `gcp-finops-toolkit` synced into the registry.

---

## Files You'll Work With

| Role | File | What You'll Do |
| :--- | :--- | :--- |
| **Edit** | [starter/lab03_middleware_redirect_exercise.py](../../starter/lab03_middleware_redirect_exercise.py) | Implement the 3 `# WORKSHOP-TODO(lab-03)` functions (`strip_headers_for_cross_origin_redirect`, `download_skill_revision_media`, `assemble_polyglot_plugin_zip`). |
| **Inspect / Run** | [middleware/app/main.py](../../middleware/app/main.py), [middleware/app/mcp_gateway.py](../../middleware/app/mcp_gateway.py), `make run-middleware` | Start the local Middleware on port `8080` and test the catalog, IDE feeds, ZIP bundle, and `/mcp` gateway. |
| **Live GCP Deploy (Optional)** | `cloudbuild/*.yaml` ([middleware.yaml](../../cloudbuild/middleware.yaml), [sample-mcp-server.yaml](../../cloudbuild/sample-mcp-server.yaml), [sample-a2a-agent.yaml](../../cloudbuild/sample-a2a-agent.yaml)), [scripts/deploy_gcp.sh](../../scripts/deploy_gcp.sh), [terraform/core/cloud_run.tf](../../terraform/core/cloud_run.tf) | Build images remotely via Cloud Build and deploy to Cloud Run in Bangkok (`asia-southeast3`). |
| **Reference Solution** | [middleware/app/registry_client.py](../../middleware/app/registry_client.py), [middleware/app/polyglot_builder.py](../../middleware/app/polyglot_builder.py) | Peek here if you get stuck on redirect header stripping or ZIP assembly. |

---

## Learning Objectives

By the end of this lab, you will:
1. Reproduce the `?alt=media` HTTP 302 redirect header trap against the local mock registry.
2. Implement redirect header stripping, SHA-256 verification, and 5-IDE ZIP assembly in `starter/lab03_middleware_redirect_exercise.py`.
3. Query the local Middleware's `/v1/catalog`, 4 IDE marketplace feeds, single-bundle ZIP, and Streamable HTTP `/mcp` gateway.
4. *(Optional)* Deploy the full Workstream A stack to Cloud Run in Bangkok (`asia-southeast3`) with zero local Docker.

> Steps 1 through 4 run in **Local Mode** against `127.0.0.1`. Ready to deploy to Google Cloud? Finish the local flow first, then continue to the Going Live on GCP section at the bottom.

---

## Step 1: See the `?alt=media` HTTP 302 Redirect Trap

When you download a `SkillRevision` archive from Agent Registry:

```text
GET /v1/projects/{p}/locations/{l}/skills/private-{id}/revisions/{rev}?alt=media
```

Agent Registry returns **HTTP 302 Found** pointing to a signed download URL. If your HTTP client blindly forwards `Authorization` or any `x-goog-*` header (like `x-goog-user-project`) onto that signed URL, storage rejects the request with **HTTP 403**.

On live GCP, Agent Registry first redirects to the same-origin endpoint `https://agentregistry.googleapis.com/download/v1/.../revisions/{rev}?alt=media`. That `/download/v1/` hop still expects `Authorization`, so stripping auth unconditionally causes `401 CREDENTIALS_MISSING` (see [middleware/app/registry_client.py](../../middleware/app/registry_client.py)).

Make sure your local stack (`make run-local-stack`, or `make run-mock` + `make run-samples`) is running and you synced the sample plugin with `make demo-sync-local` in Lab 02. Then run:

```bash
BASE="http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/asia-southeast3"

# 1. Fetch the skill's promoted defaultRevision name
DEFAULT_REV="$(curl -fsSL "${BASE}/skills/private-cost-anomaly-triage" \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["defaultRevision"])')"
echo "Default Revision: ${DEFAULT_REV}"

# 2. Request ?alt=media without following redirects to read the 302 Location header
SIGNED_LOC="$(curl -sS -D - -o /dev/null \
  -H "Authorization: Bearer simulated-token" \
  -H "x-goog-user-project: acme-agent-governance" \
  "http://127.0.0.1:8090/v1/${DEFAULT_REV}?alt=media" \
  | awk 'tolower($1)=="location:" {print $2}' | tr -d '\r')"
echo "Redirect Location: ${SIGNED_LOC}"

# 3. Bad client: forwards x-goog-user-project onto the signed URL -> HTTP 403
curl -sS -o /dev/null -w "Leaked header HTTP status: %{http_code}\n" \
  -H "x-goog-user-project: acme-agent-governance" \
  "${SIGNED_LOC}"

# 4. Good client: strips Authorization and x-goog-* headers -> HTTP 200
curl -sS -o /tmp/downloaded-skill.zip -w "Clean redirect HTTP status: %{http_code}\n" \
  "${SIGNED_LOC}"
```

**You should see:** `Leaked header HTTP status: 403`, followed by `Clean redirect HTTP status: 200`.

---

## Step 2: Code the Redirect Handler and Polyglot ZIP Builder

Now implement the same redirect logic and multi-IDE bundler yourself. Open [starter/lab03_middleware_redirect_exercise.py](../../starter/lab03_middleware_redirect_exercise.py) and run its offline self-check to see the starting state (`0/3 checks passed`):

```bash
.venv/bin/python starter/lab03_middleware_redirect_exercise.py
```

Fill in the 3 `# WORKSHOP-TODO(lab-03)` functions in order:

1. **`strip_headers_for_cross_origin_redirect(headers, source_url, target_url)`**
   - Hint: Parse both URLs with `urlparse()` and compare `(scheme.lower(), netloc.lower())`. Return a copy of `headers` if they match. If the origin changed, drop `authorization` and any key starting with `x-goog-` (case-insensitive).
   - Stuck? Peek at `strip_google_redirect_headers()` in [middleware/app/registry_client.py](../../middleware/app/registry_client.py).
2. **`download_skill_revision_media(client, *, media_url, auth_token, quota_project, expected_sha256, max_redirects=5)`**
   - Hint: Send `Authorization`, `x-goog-user-project`, and `x-goog-api-client` with `follow_redirects=False`. Loop while status is in `(301, 302, 303, 307, 308)`, resolve `Location` via `urljoin()`, clean headers with `strip_headers_for_cross_origin_redirect()`, and stop after `max_redirects`. Once you get HTTP `200`, check `hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256.strip().lower())`.
   - Stuck? Peek at `AgentRegistryClient.download_skill_revision_media()` in [middleware/app/registry_client.py](../../middleware/app/registry_client.py).
3. **`assemble_polyglot_plugin_zip(*, plugin_name, display_name, version, description, mcp_server_key, mcp_server_url, skill_markdowns)`**
   - Hint: Write all required files into an in-memory `zipfile.ZipFile(io.BytesIO(), "w")`: `plugin.json`, `.antigravity-plugin/plugin.json`, `assets/logo.svg` (1:1 square `viewBox`), `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `mcp.json` (no `${ENV_VAR}` placeholders), `.mcp.json`, `mcp_config.json` (with `"authProviderType": "google_credentials"`), and `skills/<name>/SKILL.md`. Return `(zip_bytes, hashlib.sha256(zip_bytes).hexdigest())`.
   - Stuck? Peek at `_build_single_bundle()` in [middleware/app/polyglot_builder.py](../../middleware/app/polyglot_builder.py).

Re-run the self-check until every check passes:

```bash
.venv/bin/python starter/lab03_middleware_redirect_exercise.py
```

**You should see:** `[DONE] Progress: 3/3 checks passed`.

---

## Step 3: Start and Query the Local Middleware

> Already running `make run-local-stack` from Lab 00? Skip `make run-middleware` below. Port `8080` is already live.

If you are using separate terminals, start the Middleware in Terminal 3 (`REQUIRE_KMS_SIGNATURE=false` for Workstream A; you will turn on KMS signature checks in [Lab 07](07-kms-encryption-and-signing.md)):

```bash
# Terminal 3
make run-middleware
```

In Terminal 4, query the health endpoint, unified catalog, and all 4 IDE marketplace feeds:

```bash
# 1. Health check (/healthz works locally; use /health on live Cloud Run)
curl -fsSL http://127.0.0.1:8080/healthz | .venv/bin/python -m json.tool

# 2. Unified catalog (verified skills, plugins, MCP servers, A2A agents, rejectedSkills)
curl -fsSL http://127.0.0.1:8080/v1/catalog | .venv/bin/python -m json.tool

# 3. All 4 IDE-specific marketplace JSON feeds
curl -fsSL http://127.0.0.1:8080/v1/marketplace/claude.json | .venv/bin/python -m json.tool
curl -fsSL http://127.0.0.1:8080/v1/marketplace/copilot.json | .venv/bin/python -m json.tool
curl -fsSL http://127.0.0.1:8080/v1/marketplace/codex.json | .venv/bin/python -m json.tool
curl -fsSL http://127.0.0.1:8080/v1/marketplace/antigravity.json | .venv/bin/python -m json.tool
```

Download and inspect the single-bundle Polyglot ZIP:

```bash
curl -fsSL -D /tmp/bundle-headers.txt \
  http://127.0.0.1:8080/v1/plugins/gcp-finops-toolkit.zip \
  -o /tmp/gcp-finops-toolkit-polyglot.zip

grep -i "x-bundle-sha256" /tmp/bundle-headers.txt
.venv/bin/python -m zipfile -l /tmp/gcp-finops-toolkit-polyglot.zip
```

**You should see:** A 64-character `x-bundle-sha256` header and one deterministic ZIP packing manifests for all 5 targets:
- **Agent Plugins 1.0 / Copilot / Codex:** `plugin.json` (with `extensions["com.openai"]`) and `mcp.json`
- **Google Antigravity:** `.antigravity-plugin/plugin.json`, `mcp_config.json`, `assets/logo.svg`, `rules/AGENTS.md`, `rules/finops-guardrails.md`
- **Claude Code:** `.claude-plugin/plugin.json`, `.mcp.json`, `agents/finops-advisor-agent.md`
- **OpenAI Codex:** `.codex-plugin/plugin.json`
- **GitHub Copilot Agents:** `com.github.copilot/agents/finops-advisor-agent.agent.md`
- **Verified Skills:** `skills/cost-anomaly-triage/SKILL.md` and `skills/bq-query-optimizer/SKILL.md`

---

## Step 4: Call the Streamable HTTP `/mcp` Gateway

Google Cloud's preview Remote MCP server (`https://agentregistry.googleapis.com/mcp`) returns metadata only and cannot download `?alt=media` skill bytes. Our Middleware's `POST /mcp` endpoint ([middleware/app/mcp_gateway.py](../../middleware/app/mcp_gateway.py)) fills that gap.

Run all 4 JSON-RPC calls against `http://127.0.0.1:8080/mcp`:

```bash
# 1. MCP Initialize handshake
curl -fsSL -X POST http://127.0.0.1:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}' \
  | .venv/bin/python -m json.tool

# 2. List tools (check the readOnlyHint / destructiveHint annotations)
curl -fsSL -X POST http://127.0.0.1:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | .venv/bin/python -m json.tool

# 3. Call load_verified_skill (downloads via ?alt=media, verifies SHA-256 & scan gate, returns SKILL.md)
curl -fsSL -X POST http://127.0.0.1:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "load_verified_skill",
      "arguments": {"skill_name": "cost-anomaly-triage"}
    }
  }' | .venv/bin/python -m json.tool

# 4. Proxy a task to the governed FinOps A2A Agent via call_remote_a2a_agent
curl -fsSL -X POST http://127.0.0.1:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 4,
    "method": "tools/call",
    "params": {
      "name": "call_remote_a2a_agent",
      "arguments": {
        "agent_name": "finops-advisor-agent",
        "task": "Optimize SELECT * FROM analytics.events for lower BigQuery cost"
      }
    }
  }' | .venv/bin/python -m json.tool
```

**You should see:** `load_verified_skill` returns the verified `SKILL.md` text and SHA-256 digest, and `call_remote_a2a_agent` returns BigQuery cost-optimization recommendations from the sample A2A agent.

---

## Going Live on GCP (Optional — Zero Local Docker)

> **Region placement:** Cloud Run, Cloud Build, Artifact Registry, and Agent Registry `Services` deploy to Bangkok (`asia-southeast3`), while standalone `Skills` live in `global`. The Middleware filters to `private-*` skills (`INCLUDE_PUBLIC_SKILLS=false`) and caps concurrent verification with `asyncio.Semaphore(8)`. Singapore (`asia-southeast1`) is only used in Workstream B (`auth_provider_location = "asia-southeast1"` for Agent Identity); see [Attendee Quickstart](../attendee-quickstart.md) for the full table.

Two quick live-GCP notes before you deploy:
- **Zero local Docker & OIDC auth:** Images build remotely in Cloud Build via `cloudbuild/*.yaml` (`logging: CLOUD_LOGGING_ONLY`, `--default-buckets-behavior=regional-user-owned-bucket`; root `.gcloudignore` and `.dockerignore` strip local bloat). Both tracks keep `sample-a2a-agent` private by default; Track 2 (`scripts/deploy_gcp.sh`) automatically grants `roles/run.invoker` to `agent-marketplace-middleware` on `sample-a2a-agent`, so [middleware/app/mcp_gateway.py](../../middleware/app/mcp_gateway.py) fetches a cached Cloud Run OIDC Identity Token automatically on `call_remote_a2a_agent`. On Track 3 (`terraform/core/`), either grant `roles/run.invoker` to `middleware_sa` (`agent-marketplace-middleware`) on `sample-a2a-agent` or set `allow_unauthenticated_workshop_demo = true` in an isolated sandbox.
- **Health probes:** Google Frontend (GFE) intercepts external `https://*.run.app/healthz` requests with HTTP 404. Always probe `/health` (or `/v1/health`) on live Cloud Run URLs; `/healthz` stays active for local dev and internal probes.

<details>
<summary><strong>Track 2: Zero-Docker, Zero-Terraform (<code>scripts/deploy_gcp.sh</code> / <code>make gcp-deploy</code>)</strong></summary>

Deploy APIs, Artifact Registry, Cloud Build images, Cloud Run services, Bangkok Agent Registry `Services`, and scanned `global` skills in one command:

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"

scripts/deploy_gcp.sh \
  --project "${GCP_PROJECT_ID}" \
  --region asia-southeast3 \
  --skills-location global

# Or use the Makefile shortcut (defaults: asia-southeast3 + global):
# make gcp-deploy GCP_PROJECT_ID="${GCP_PROJECT_ID}"
```

*(Tip: `samples/mcp-server` and `samples/a2a-agent` have self-contained root `Dockerfile`s, so you can also iterate on them directly with `gcloud run deploy sample-mcp-server --source samples/mcp-server --project "${GCP_PROJECT_ID}" --region asia-southeast3 --port 8091 --no-allow-unauthenticated --quiet`. `polyglot-middleware` needs the repo root so `schemas/` is included, which is why `cloudbuild/middleware.yaml` builds from `.`.)*

Verify the deployed Cloud Run services and probe `/health`:

```bash
gcloud run services list \
  --project="${GCP_PROJECT_ID}" \
  --region="asia-southeast3" \
  --limit=50 \
  --quiet

export MIDDLEWARE_URL="$(gcloud run services describe polyglot-middleware \
  --project="${GCP_PROJECT_ID}" \
  --region="asia-southeast3" \
  --format="value(status.url)" \
  --quiet)"

curl -fsSL -H "Authorization: Bearer $(gcloud auth print-identity-token --quiet)" \
  "${MIDDLEWARE_URL}/health" | .venv/bin/python -m json.tool
```

</details>

<details>
<summary><strong>Track 3: Two-Stage Cloud Build + Terraform (<code>scripts/build_images_cloudbuild.sh</code> + <code>terraform/core/</code>)</strong></summary>

**Prerequisite:** Copy [`terraform/core/terraform.tfvars.example`](../../terraform/core/terraform.tfvars.example) to `terraform/core/terraform.tfvars` (if you have not already in Lab 00) and replace `acme-agent-governance` with `${GCP_PROJECT_ID}` in `project_id`, `middleware_image`, `sample_mcp_image`, and `sample_a2a_image`.

Always run **Stage 1 (enable APIs & Artifact Registry, then build images remotely)** before **Stage 2 (`terraform apply`)** so Cloud Run creation never hits missing-image errors on a fresh project.

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"
export GCP_LOCATION="asia-southeast3"

# Stage 1A: Enable APIs and create the Bangkok Artifact Registry repository
terraform -chdir=terraform/core init
terraform -chdir=terraform/core apply \
  -target=google_project_service.core \
  -target=google_artifact_registry_repository.workshop_images

# Stage 1B: Build and push all 3 images remotely via cloudbuild/*.yaml (zero local Docker)
scripts/build_images_cloudbuild.sh \
  --project "${GCP_PROJECT_ID}" \
  --region "${GCP_LOCATION}" \
  --repo "agent-marketplace" \
  --tag "1.0.0"

# Or build a single image config directly:
# gcloud builds submit \
#   --project="${GCP_PROJECT_ID}" \
#   --region="${GCP_LOCATION}" \
#   --default-buckets-behavior=regional-user-owned-bucket \
#   --config=cloudbuild/middleware.yaml \
#   --substitutions=_IMAGE_URI="${GCP_LOCATION}-docker.pkg.dev/${GCP_PROJECT_ID}/agent-marketplace/polyglot-middleware:1.0.0" \
#   . --quiet
```

Now run **Stage 2** to deploy Cloud Run ([terraform/core/cloud_run.tf](../../terraform/core/cloud_run.tf), where `middleware_sa` gets `roles/agentregistry.viewer` only), register Bangkok `Services`, and bootstrap scanned `global` skills:

```bash
make tf-core-plan
terraform -chdir=terraform/core apply

export MIDDLEWARE_URL="$(terraform -chdir=terraform/core output -raw middleware_url)"
curl -fsSL -H "Authorization: Bearer $(gcloud auth print-identity-token --quiet)" \
  "${MIDDLEWARE_URL}/health" | .venv/bin/python -m json.tool
```

</details>

---

## Verification Checkpoint

- [ ] Reproduced `403` (leaked header) vs. `200` (clean redirect) on `?alt=media`.
- [ ] `.venv/bin/python starter/lab03_middleware_redirect_exercise.py` reports `3/3` checks passed (`[DONE]`).
- [ ] All 4 `/v1/marketplace/{claude,copilot,codex,antigravity}.json` feeds list `gcp-finops-toolkit`, and `/v1/marketplace/antigravity.json` and `/v1/catalog` report the same `sha256` as the `X-Bundle-Sha256` header on `/v1/plugins/gcp-finops-toolkit.zip`.
- [ ] `load_verified_skill` and `call_remote_a2a_agent` succeed on `POST /mcp`.
- [ ] `.venv/bin/pytest -v tests/test_track1b_middleware.py` passes.

---

## Troubleshooting

| Symptom | Why It Happens | How to Fix |
| :--- | :--- | :--- |
| `/v1/catalog` shows `plugins: []` and entries in `rejectedSkills` | Either `REQUIRE_KMS_SIGNATURE=true` has no public key PEM, or skills are in `STATE_DRAFT` / have findings. | Start local Middleware with `make run-middleware` (`REQUIRE_KMS_SIGNATURE=false`) and re-sync skills with `TARGET_STATE_ACTIVE`. |
| `502 upstream_registry_unreachable` | `mock_registry` (`:8090`) is stopped or `AGENT_REGISTRY_BASE_URL` points to the wrong port. | Run `curl http://127.0.0.1:8090/healthz` and confirm it returns `200 OK`. |
| `call_remote_a2a_agent` returns connection refused | `samples/a2a-agent` (`:8092`) is not running. | Start `make run-samples` in Terminal 2. |
| External `https://*.run.app/healthz` returns Google HTML `404` | Cloud Run Google Frontend (GFE) reserves `/healthz` before requests reach your container. | Probe **`/health`** (or `/v1/health`) on live Cloud Run URLs. Keep `/healthz` for local `127.0.0.1` checks. |
| `401 CREDENTIALS_MISSING` on `?alt=media` in live GCP | Client stripped `Authorization` on the same-origin `302` to `/download/v1/...?alt=media`. | Keep `Authorization` on same-origin `/download/v1/` hops without signed-URL tokens; strip auth and `x-goog-*` headers on cross-origin or signed URLs. |
| Live `agents[]` lists Workspace Agent first or fails to parse `card` | Live Agent Registry sorts Google's built-in Workspace Agent first and nests `card.content.supportedInterfaces`. | Pass `finops-advisor-agent` explicitly by name; [middleware/app/mcp_gateway.py](../../middleware/app/mcp_gateway.py) parses both `card` shapes. |
| Built-in Google skills clutter `/v1/catalog` or catalog builds feel slow | `INCLUDE_PUBLIC_SKILLS=true` includes first-party Google skills, or per-skill downloads run sequentially. | Keep `INCLUDE_PUBLIC_SKILLS=false` (default) to filter to `private-*` skills; `CatalogService.build_snapshot()` bounds concurrency with `asyncio.Semaphore(8)`. |

---

**Next Lab:** [Lab 04: 1-Command Multi-IDE Developer Setup →](04-ide-developer-experience.md)
