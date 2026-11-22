# Lab 03: Build & Deploy the Polyglot Marketplace Middleware

**Workstream:** A — Core Platform & Middleware  
**Duration:** 40 minutes  
**Prerequisites:** Completed [Lab 02](02-register-agent-registry.md) with `gcp-finops-toolkit` synced into the registry.

---

## Learning Objectives

By the end of this lab, you will:
1. Inspect and run the **Polyglot Marketplace Middleware** ([middleware/app/main.py](../../middleware/app/main.py)) exposing `/v1/catalog`, 4 IDE marketplace feeds (`/v1/marketplace/{claude,copilot,codex,antigravity}.json`), deterministic Polyglot 5-IDE ZIP bundles (`/v1/plugins/{plugin_name}.zip`), and a Streamable HTTP `/mcp` gateway.
2. Prove why Agent Registry's `?alt=media` **HTTP 302 redirect** requires explicit policy-based header handling—preserving `Authorization` strictly on live same-origin `/download/v1/` authenticated redirects (preventing `401 CREDENTIALS_MISSING`) while stripping `Authorization` and all `x-goog-*` headers (`x-goog-user-project`, `x-goog-api-client`) on cross-origin and signed-URL redirects (preventing `403`)—and verify how [middleware/app/registry_client.py](../../middleware/app/registry_client.py) handles both cleanly.
3. Invoke the Middleware's Streamable HTTP `/mcp` tools (`search_catalog`, `load_verified_skill`, `list_governed_mcp_servers`, `call_remote_a2a_agent`) directly.
4. Build and deploy the container images to **Cloud Run** in **Bangkok (`asia-southeast3`)** primary and optional **Singapore (`asia-southeast1`)** secondary/DR — both sharing **`global`** standalone `Skills` (`private-*` auto-filtered) alongside regional `Services` — with **zero local Docker** using checked-in `cloudbuild/*.yaml` configs and either `scripts/deploy_gcp.sh` (**Track 2:** Zero-Docker, Zero-Terraform) or `scripts/build_images_cloudbuild.sh` + [terraform/core/cloud_run.tf](../../terraform/core/cloud_run.tf) (**Track 3:** Full Terraform IaC).

---

## Step 1: Observe the `?alt=media` HTTP 302 Header-Stripping Gotcha

When fetching a `SkillRevision` archive from Agent Registry:
```text
GET /v1/projects/{p}/locations/{l}/skills/private-{id}/revisions/{rev}?alt=media
```
Agent Registry returns **HTTP 302 Found** with two distinct real-world redirect targets:
1. **Cross-origin or signed-URL redirect** (e.g., short-lived GCS signed URL with `X-Goog-Signature`, or simulated in `mock_registry` at `/scotty-signed/{token}`): Forwarding `Authorization` or any `x-goog-*` header causes **HTTP 403**!
2. **Live same-origin Scotty download redirect**: On live GCP, `GET https://agentregistry.googleapis.com/v1/.../revisions/{rev}?alt=media` returns HTTP 302 to `https://agentregistry.googleapis.com/download/v1/.../revisions/{rev}?alt=media` (same origin, authenticated download endpoint with no signed-URL query params). Unconditionally stripping `Authorization` on that hop causes **HTTP 401 `CREDENTIALS_MISSING`**!

Both [middleware/app/registry_client.py](../../middleware/app/registry_client.py) and [platform_agent/rest_consumer.py](../../platform_agent/rest_consumer.py) preserve auth **strictly** on same-origin `/download/...` redirects without signed-URL tokens while always stripping `Authorization` and all `x-goog-*` headers on cross-origin and signed-URL (`/scotty-signed/`, `X-Goog-Signature`, `Signature`, `GoogleAccessId`, `X-Goog-Credential`) redirects.

### Mode A: Prove It Live Against `mock_registry`

With `make run-mock`, `make run-samples`, and `make demo-sync` already completed from Lab 02:

```bash
BASE="http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/asia-southeast3"

# 1. Fetch the skill's promoted defaultRevision name
DEFAULT_REV="$(curl -fsSL "${BASE}/skills/private-cost-anomaly-triage" \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["defaultRevision"])')"
echo "Default Revision: ${DEFAULT_REV}"

# 2. Request ?alt=media WITHOUT following redirects to inspect the 302 Location header
SIGNED_LOC="$(curl -sS -D - -o /dev/null \
  -H "Authorization: Bearer simulated-token" \
  -H "x-goog-user-project: acme-agent-governance" \
  "http://127.0.0.1:8090/v1/${DEFAULT_REV}?alt=media" \
  | awk 'tolower($1)=="location:" {print $2}' | tr -d '\r')"
echo "Redirect Location: ${SIGNED_LOC}"

# 3. BAD client: leaks x-goog-user-project onto the signed URL -> HTTP 403!
curl -sS -o /dev/null -w "Leaked header HTTP status: %{http_code}\n" \
  -H "x-goog-user-project: acme-agent-governance" \
  "${SIGNED_LOC}"

# 4. GOOD client: strips Authorization & x-goog-* headers -> HTTP 200!
curl -sS -o /tmp/downloaded-skill.zip -w "Clean redirect HTTP status: %{http_code}\n" \
  "${SIGNED_LOC}"
```

Observe how `Leaked header HTTP status: 403` flips to `Clean redirect HTTP status: 200` when Google headers are stripped cleanly!

---

## Step 2: Start and Exercise the Polyglot Middleware

### Mode A: Local Mock Mode

> **Already running `make run-local-stack` from Lab 00?** Skip `make run-middleware` below — Polyglot Middleware is already listening on `http://127.0.0.1:8080`.

If you are using the 4-terminal interactive layout, start the middleware in Terminal 3 on port `8080` (`REQUIRE_KMS_SIGNATURE=false` in Workstream A; you will enable strict KMS signature enforcement in [Lab 07](07-kms-encryption-and-signing.md)):

```bash
# Terminal 3
make run-middleware
```

In Terminal 4, query every Middleware endpoint:

```bash
# 1. Health check
curl -fsSL http://127.0.0.1:8080/healthz | .venv/bin/python -m json.tool

# 2. Unified governed catalog (shows verified skills, plugins, MCP servers, A2A agents, and rejectedSkills)
curl -fsSL http://127.0.0.1:8080/v1/catalog | .venv/bin/python -m json.tool

# 3. All 4 IDE-specific marketplace JSON feeds
curl -fsSL http://127.0.0.1:8080/v1/marketplace/claude.json | .venv/bin/python -m json.tool
curl -fsSL http://127.0.0.1:8080/v1/marketplace/copilot.json | .venv/bin/python -m json.tool
curl -fsSL http://127.0.0.1:8080/v1/marketplace/codex.json | .venv/bin/python -m json.tool
curl -fsSL http://127.0.0.1:8080/v1/marketplace/antigravity.json | .venv/bin/python -m json.tool
```

Download and inspect the synthesized **Polyglot 5-IDE Single-Bundle ZIP**:

```bash
curl -fsSL -D /tmp/bundle-headers.txt \
  http://127.0.0.1:8080/v1/plugins/gcp-finops-toolkit.zip \
  -o /tmp/gcp-finops-toolkit-polyglot.zip

grep -i "x-bundle-sha256" /tmp/bundle-headers.txt
.venv/bin/python -m zipfile -l /tmp/gcp-finops-toolkit-polyglot.zip
```

Notice the single deterministic ZIP contains manifests for **all 5 targets** simultaneously:
- `plugin.json` & `mcp.json` (Agent Plugins 1.0 + VS Code/Copilot + Codex `extensions["com.openai"]`)
- `.antigravity-plugin/plugin.json`, `mcp_config.json`, `assets/logo.svg`, `rules/AGENTS.md`, `rules/finops-guardrails.md`
- `.claude-plugin/plugin.json`, `.mcp.json`, `agents/finops-advisor-agent.md`
- `.codex-plugin/plugin.json`
- `com.github.copilot/agents/finops-advisor-agent.agent.md`
- `skills/cost-anomaly-triage/SKILL.md` & `skills/bq-query-optimizer/SKILL.md`

---

## Step 3: Exercise the Streamable HTTP `/mcp` Gateway

Remember: Google Cloud's preview Remote MCP server (`https://agentregistry.googleapis.com/mcp`) returns metadata only and **cannot** download `?alt=media` skill bytes. Our Middleware's `POST /mcp` endpoint ([middleware/app/mcp_gateway.py](../../middleware/app/mcp_gateway.py)) fills that gap cleanly:

```bash
# 1. MCP Initialize handshake
curl -fsSL -X POST http://127.0.0.1:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}' \
  | .venv/bin/python -m json.tool

# 2. List tools (observe MCP tool annotations!)
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

---

## Step 4: Deploy to Cloud Run — Zero Local Docker (Mode B: Live GCP Mode)

Every container build in this workshop runs remotely in **Google Cloud Build** using the checked-in `cloudbuild/*.yaml` configs ([cloudbuild/middleware.yaml](../../cloudbuild/middleware.yaml), [cloudbuild/sample-mcp-server.yaml](../../cloudbuild/sample-mcp-server.yaml), [cloudbuild/sample-a2a-agent.yaml](../../cloudbuild/sample-a2a-agent.yaml)) with `logging: CLOUD_LOGGING_ONLY` and `--default-buckets-behavior=regional-user-owned-bucket` — **no Docker Desktop or local Docker daemon required**. Checked-in `.gcloudignore` and `.dockerignore` files at the repository root exclude `.venv/`, `.git/`, `terraform/`, `docs/`, and `tests/` so Cloud Build uploads stay fast and lean.

> **Cloud Run Service-to-Service OIDC Auth:** Both Track 2 and Track 3 deploy `sample-a2a-agent` with `--no-allow-unauthenticated` and grant `roles/run.invoker` to the least-privilege `agent-marketplace-middleware` service account. When `POST /mcp` executes `call_remote_a2a_agent` against an IAM-protected `https://*.run.app` A2A endpoint, [middleware/app/mcp_gateway.py](../../middleware/app/mcp_gateway.py) automatically fetches and caches a Cloud Run OIDC Identity Token for the target service origin from the GCE metadata server (or local ADC fallback) and attaches `Authorization: Bearer <id_token>`.

> **Verified Live APAC Regional Split (Bangkok Primary + Singapore Secondary + `global` Skills):**
> - **Bangkok (`asia-southeast3`, Primary) & Singapore (`asia-southeast1`, Secondary/DR):** Cloud Run v2, Cloud Build, Artifact Registry, Cloud Storage, Agent Registry **`Services`** (`McpServer`, `Agent`, `Endpoint`), and Cloud KMS `EC_SIGN_P256_SHA256` asymmetric signing.
> - **`global` (Default for APAC — or `us`/`eu` for CMEK):** Agent Registry standalone **`Skills`** (`AGENT_REGISTRY_SKILLS_LOCATION=global`, `var.skills_location = "global"`), ingested once and shared seamlessly by both Bangkok primary and Singapore secondary Middleware! Both Middleware (`INCLUDE_PUBLIC_SKILLS=false`) and `platform_agent` automatically filter strictly to enterprise `private-*` skills so Google's built-in first-party skills do not pollute enterprise catalogs, and `CatalogService.build_snapshot()` bounds concurrent skill verification (`asyncio.Semaphore(8)`) for responsive catalog builds even with 20+ private skills.
> - **Workstream B APAC notes:** `AuthProvider` runs in **Singapore (`asia-southeast1`)**, native Skill CMEK (`SkillEncryptionSpec`) is `us`/`eu` multi-regions only, and Agent Gateway + IAP v2 UAP (`enable_agent_gateway = false`) is `us-central1`/`europe-west1` (see the full matrix in [Attendee Quickstart](../attendee-quickstart.md)).

Choose either **Track 2 (Zero-Docker, Zero-Terraform)** or **Track 3 (Full Terraform IaC + Zero-Docker Cloud Build)**:

### Track 2: Zero-Docker, Zero-Terraform Deploy (`scripts/deploy_gcp.sh` / `gcloud` + Cloud Build)

Deploy the complete Workstream A stack (APIs, Artifact Registry, Cloud Build images, Cloud Run services, regional Agent Registry `Services` in each deployed region, and one-time `--skills-location global --wait-for-scan` Skill sync) to **Bangkok (`asia-southeast3`)** primary and optional **Singapore (`asia-southeast1`)** secondary/DR in a single command:

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"

# Deploy Bangkok primary + Singapore secondary/DR sharing global Skills (omit --enable-secondary for Bangkok-only)
scripts/deploy_gcp.sh \
  --project "${GCP_PROJECT_ID}" \
  --primary-region asia-southeast3 \
  --skills-location global \
  --enable-secondary

# Or via the Makefile shortcut (ENABLE_SECONDARY defaults to false for Bangkok-only; AGENT_REGISTRY_SKILLS_LOCATION defaults to global):
# make gcp-deploy GCP_PROJECT_ID="${GCP_PROJECT_ID}"
# make gcp-deploy GCP_PROJECT_ID="${GCP_PROJECT_ID}" ENABLE_SECONDARY=true
```

*(Tip for rapid single-service iteration on `samples/` without Terraform: because [samples/mcp-server/](../../samples/mcp-server/Dockerfile) and [samples/a2a-agent/](../../samples/a2a-agent/Dockerfile) are self-contained directories with root `Dockerfile`s, you can also deploy them directly via `gcloud run deploy sample-mcp-server --source samples/mcp-server --project "${GCP_PROJECT_ID}" --region asia-southeast3 --port 8091 --no-allow-unauthenticated --quiet` — note that `polyglot-middleware` needs the repository root context so `schemas/` is included, which is why `cloudbuild/middleware.yaml` builds from `.`!)*

Verify the deployed Cloud Run services and run an authenticated `/health` check against Bangkok (and Singapore if `--enable-secondary` / `ENABLE_SECONDARY=true` was used). *(Note: Google Frontend intercepts `/healthz` on external `*.run.app` requests with HTTP 404, so always probe `/health` on live Cloud Run URLs; `/healthz` remains active for local dev and internal probes.)*

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

# Optional: verify Singapore secondary/DR middleware when ENABLE_SECONDARY=true
export SECONDARY_MIDDLEWARE_URL="$(gcloud run services describe polyglot-middleware \
  --project="${GCP_PROJECT_ID}" \
  --region="asia-southeast1" \
  --format="value(status.url)" \
  --quiet 2>/dev/null || true)"
if [[ -n "${SECONDARY_MIDDLEWARE_URL}" ]]; then
  curl -fsSL -H "Authorization: Bearer $(gcloud auth print-identity-token --quiet)" \
    "${SECONDARY_MIDDLEWARE_URL}/health" | .venv/bin/python -m json.tool
fi
```

### Track 3: Two-Stage Terraform + Zero-Docker Cloud Build (`terraform/core/`)

Always execute **Stage 1 (Build images remotely)** before **Stage 2 (`terraform apply`)** so Cloud Run v2 creation never hits missing-image errors on a fresh project:

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"
export GCP_LOCATION="asia-southeast3"
export GCP_SECONDARY_LOCATION="asia-southeast1"

# Stage 1A — Enable APIs & create the Artifact Registry Docker repository first
terraform -chdir=terraform/core init
terraform -chdir=terraform/core apply \
  -target=google_project_service.core \
  -target=google_artifact_registry_repository.workshop_images

# Stage 1B — Build & push all 3 images remotely via checked-in cloudbuild/*.yaml (ZERO local Docker!)
scripts/build_images_cloudbuild.sh \
  --project "${GCP_PROJECT_ID}" \
  --region "${GCP_LOCATION}" \
  --repo "agent-marketplace" \
  --tag "1.0.0"

# (Optional) Build images for Singapore secondary/DR when enable_secondary_region = true:
# terraform -chdir=terraform/core apply -target=google_artifact_registry_repository.workshop_images_secondary
# scripts/build_images_cloudbuild.sh \
#   --project "${GCP_PROJECT_ID}" \
#   --region "${GCP_SECONDARY_LOCATION}" \
#   --repo "agent-marketplace" \
#   --tag "1.0.0"

# Or invoke any single checked-in Cloud Build config directly:
# gcloud builds submit \
#   --project="${GCP_PROJECT_ID}" \
#   --region="${GCP_LOCATION}" \
#   --default-buckets-behavior=regional-user-owned-bucket \
#   --config=cloudbuild/middleware.yaml \
#   --substitutions=_IMAGE_URI="${GCP_LOCATION}-docker.pkg.dev/${GCP_PROJECT_ID}/agent-marketplace/polyglot-middleware:1.0.0" \
#   . --quiet
```

Now run **Stage 2** to deploy Cloud Run workloads ([terraform/core/cloud_run.tf](../../terraform/core/cloud_run.tf) — least-privilege `middleware_sa` gets `roles/agentregistry.viewer` ONLY), register Agent Registry services, and bootstrap scanned skills (`--wait-for-scan`):

```bash
make tf-core-plan
terraform -chdir=terraform/core apply

export MIDDLEWARE_URL="$(terraform -chdir=terraform/core output -raw middleware_url)"
curl -fsSL -H "Authorization: Bearer $(gcloud auth print-identity-token --quiet)" \
  "${MIDDLEWARE_URL}/health" | .venv/bin/python -m json.tool
```

If you enabled `enable_secondary_region = true` in `terraform/core/terraform.tfvars`, inspect both regional middleware URLs for Lab 04 failover testing:

```bash
terraform -chdir=terraform/core output -raw middleware_url
terraform -chdir=terraform/core output -raw secondary_middleware_url
```

---

## Step 5: Hands-On Coding Exercise (Optional Deep Dive)

Complete the `# WORKSHOP-TODO(lab-03)` blocks in [starter/lab03_middleware_redirect_exercise.py](../../starter/lab03_middleware_redirect_exercise.py) to implement cross-origin header stripping, SHA-256-verified media downloads, and Polyglot ZIP assembly:

```bash
.venv/bin/python starter/lab03_middleware_redirect_exercise.py
# Or run all starter self-checks together:
make starter-check
```

---

## Verification Checkpoint

- [ ] Proved `403` vs `200` on `?alt=media` 302 redirect header stripping.
- [ ] All 4 `/v1/marketplace/{claude,copilot,codex,antigravity}.json` feeds return `gcp-finops-toolkit` with a matching `sha256`.
- [ ] `load_verified_skill` and `call_remote_a2a_agent` succeed on `POST /mcp`.
- [ ] `.venv/bin/pytest -v tests/test_track1b_middleware.py` passes completely.

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `/v1/catalog` shows `plugins: []` and entries in `rejectedSkills` | Either `REQUIRE_KMS_SIGNATURE=true` without a public key PEM, or skills are in `STATE_DRAFT` / have security findings | For Workstream A local testing, start middleware with `make run-middleware` (`REQUIRE_KMS_SIGNATURE=false`) and ensure skills were synced with `TARGET_STATE_ACTIVE`. |
| `502 upstream_registry_unreachable` | `mock_registry` (`:8090`) is not running or `AGENT_REGISTRY_BASE_URL` points to the wrong port | Verify `curl http://127.0.0.1:8090/healthz` returns `200 OK`. |
| `call_remote_a2a_agent` returns connection refused | `samples/a2a-agent` (`:8092`) is not running | Start `make run-samples` in Terminal 2. |
| External `https://*.run.app/healthz` returns Google HTML `404` | Cloud Run Google Frontend (GFE) reserves `/healthz` externally before requests reach the container | Probe **`/health`** (or `/v1/health` on Middleware) on live Cloud Run URLs; `/healthz` remains active for local `127.0.0.1` and internal probes. |
| `401 CREDENTIALS_MISSING` downloading `?alt=media` on live GCP | Client unconditionally stripped `Authorization` on Agent Registry's same-origin `302` → `/download/v1/...?alt=media` redirect | Preserve `Authorization` strictly on same-origin `/download/...` redirects without signed-URL tokens, while stripping `Authorization` and all `x-goog-*` headers on cross-origin or signed-URL (`/scotty-signed/`, `X-Goog-Signature`) redirects. |
| Live `agents[]` lists Workspace Agent first or `call_remote_a2a_agent` fails to parse `card` | Live Agent Registry orders Google's built-in Workspace Agent ahead of custom agents and nests `card.content.supportedInterfaces` | Target `finops-advisor-agent` explicitly by name/displayName; `middleware/app/mcp_gateway.py` resolves both `card.supportedInterfaces` and `card.content.supportedInterfaces`. |
| Built-in Google skills cluttering catalog or slow `/v1/catalog` with 20+ skills | `INCLUDE_PUBLIC_SKILLS=true` enabled or sequential per-skill verification | Keep `INCLUDE_PUBLIC_SKILLS=false` (default) to serve strictly `private-*` enterprise skills; `CatalogService.build_snapshot()` bounds concurrent verification via `asyncio.Semaphore(8)`. |

---

**Next Lab:** [Lab 04: 1-Command Multi-IDE Developer Experience →](04-ide-developer-experience.md)
