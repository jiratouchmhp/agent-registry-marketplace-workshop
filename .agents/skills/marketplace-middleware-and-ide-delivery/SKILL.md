---
name: marketplace-middleware-and-ide-delivery
description: >-
  Builds, debugs, deploys to Cloud Run, and operates the Polyglot Marketplace
  Middleware (/v1/catalog, /v1/marketplace/{tool}.json, /v1/plugins/*.zip,
  /mcp), 1-command tools.hub_sync multi-IDE workstation hydration (Claude Code,
  Copilot/VS Code, Codex, Antigravity), and Google ADK / REST Platform Agent
  consumers. Use when modifying middleware/app/, deploying polyglot-middleware
  via Cloud Build and terraform/core/cloud_run.tf, debugging ?alt=media 302
  redirects or /mcp tools (load_verified_skill), running tools.hub_sync
  sync/status or --bootstrap-mcp-only, or wiring platform_agent ADK consumers
  (AgentRegistry, GCPSkillRegistry, SkillToolset, GcpAuthProvider). Don't use
  for workshop routing (agent-marketplace-workshop-guide), authoring 5-IDE
  plugins (polyglot-agent-plugin-authoring), ingesting to Agent Registry v1
  (agent-registry-v1-ingest-and-lifecycle), or KMS, IAP UAP, and audit hardening
  (agent-registry-security-and-prod-hardening).
---

# Marketplace Middleware, Multi-IDE Delivery & ADK Consumption

Build, debug, deploy, and operate the serving and consumption planes of the Enterprise Agent Marketplace: the Cloud Run **Polyglot Marketplace Middleware** (`middleware/app/`), the 1-command **`tools.hub_sync`** 4-IDE workstation installer, and direct **Google ADK / REST `v1`** headless Platform Agents (`platform_agent/`) across local workshop sandboxes, active repository development, and live GCP production.

## Quick Decision Tree

```text
What are you trying to do?
├── Modify or debug Polyglot Middleware endpoints, ?alt=media downloads, Polyglot ZIPs, or /mcp
│   └── Inspect middleware/app/{main,config,registry_client,polyglot_builder,mcp_gateway,security_verifier}.py
│       -> See references/middleware-runtime-and-mcp-gateway.md
├── Build & deploy polyglot-middleware (and sample MCP/A2A workloads) to Cloud Run in Bangkok (asia-southeast3 — Zero Local Docker!)
│   ├── No-Terraform track -> `scripts/deploy_gcp.sh --project <ID> --region asia-southeast3 --skills-location global` (`make gcp-deploy`)
│   └── Full IaC track -> Stage 1 `scripts/build_images_cloudbuild.sh` (`cloudbuild/*.yaml`) -> Stage 2 `terraform/core/cloud_run.tf`
│       -> See references/middleware-runtime-and-mcp-gateway.md
├── Roll out governed plugins or /mcp gateway to developer workstations (4 IDEs)
│   ├── Full local Polyglot hydration from Bangkok Cloud Run Middleware
│   │   └── Run `python -m tools.hub_sync sync --middleware-url <bkk-middleware-url> --tools all` + `status`
│   └── Zero-disk MCP-only onboarding (dynamic load_verified_skill without local ZIPs)
│       └── Run `python -m tools.hub_sync sync --bootstrap-mcp-only`
│       -> See references/hub-sync-and-adk-consumption.md
├── Wire headless Platform Agents directly to Agent Registry v1 (Vertex AI Agent Engine / Cloud Run)
│   ├── Local / CI / no-ADK verification -> `python -m platform_agent.adk_orchestrator --mode rest`
│   └── Live Google ADK LlmAgent -> `AgentRegistry` + `GCPSkillRegistry` + `SkillToolset` + `GcpAuthProvider`
│       -> See references/hub-sync-and-adk-consumption.md
└── Outside middleware, IDE delivery, or ADK consumption?
    ├── Workshop navigation / lab progression -> `agent-marketplace-workshop-guide`
    ├── Authoring or validating 5-IDE plugins -> `polyglot-agent-plugin-authoring`
    ├── Registering skills & services in v1   -> `agent-registry-v1-ingest-and-lifecycle`
    └── KMS CMEK/signing, IAP UAP, WORM audit -> `agent-registry-security-and-prod-hardening`
```

---

## Critical Gotchas ("Do This, Not That")

| # | Do This | Not That | Why |
|---|---|---|---|
| **1** | Follow `?alt=media` 3xx redirects manually (`follow_redirects=False`): **preserve `Authorization` strictly on live same-origin `/download/...` redirects without signed-URL tokens**, and **strip `Authorization` and all `x-goog-*` headers** on all cross-origin and signed-URL (`/scotty-signed/`, `X-Goog-Signature`) hops. | Rely on `httpx(..., follow_redirects=True)` forwarding headers everywhere, OR unconditionally strip `Authorization` on live same-origin `/download/v1/` redirects. | Live Agent Registry redirects `?alt=media` (`302`) to authenticated same-origin `https://agentregistry.googleapis.com/download/v1/...` (stripping `Authorization` causes `401 CREDENTIALS_MISSING`), while cross-origin/signed GCS URLs (and local mock `/scotty-signed/{token}`) return **HTTP 403** if `Authorization` or `x-goog-*` headers leak. |
| **2** | Keep client `HUB_AUTH_TOKEN` separate from upstream Agent Registry OAuth2 (`AGENT_REGISTRY_AUTH_TOKEN` or automatic GCE/Cloud Run metadata server → `gcloud` ADC fallback). | Pass `HUB_AUTH_TOKEN` as the upstream Agent Registry Bearer token. | `HUB_AUTH_TOKEN` is the developer's Cloud Run invoker ID token (`gcloud auth print-identity-token --quiet`). `middleware/app/registry_client.py` acquires upstream GCP OAuth2 independently as `middleware_sa` (`roles/agentregistry.viewer` only). |
| **3** | Serve `load_verified_skill` via our Middleware `POST /mcp` gateway (`middleware/app/mcp_gateway.py`). | Point developer IDEs exclusively at `https://agentregistry.googleapis.com/mcp`. | Google Cloud's built-in Remote MCP server returns skill *metadata only* and cannot download `?alt=media` ZIP bytes. Middleware `/mcp` fetches, verifies `scanState`/`findings==0`/SHA-256/KMS P-256, and returns attested `SKILL.md` instructions. |
| **4** | Distribute plugins to GitLab enterprises via Middleware HTTP ZIPs + `tools.hub_sync` local Polyglot hydration + `/mcp`. | Build a Virtual Git Smart-HTTP server alone as the sole distribution mechanism. | Antigravity `agy plugin install <url>` hardcodes `github.com` remote URLs only, VS Code Web & Codex Enterprise Admin require GitHub, and Claude Code HTTP marketplaces forbid relative `./` sources. Local Polyglot hydration works across all 4 IDEs with zero GitHub dependency. |
| **5** | Preserve ownership-tracked `.sync-manifest.json` atomic merges in `tools.hub_sync` and prune **only** on clean full syncs. | Overwrite IDE dotfiles wholesale or prune entries after partial fetches / `--bootstrap-mcp-only`. | `tools/hub_sync/hydration.py` validates every ZIP in memory before touching disk, writes via `NamedTemporaryFile` → `fsync` → `os.replace`, preserves user-authored MCP servers and `disabled`/`disabledTools` toggles, and skips pruning on errors or bootstrap mode. |
| **6** | Install `google-adk[a2a,agent-identity,mcp]>=1.29.0`, call `CredentialManager.register_auth_provider(GcpAuthProvider())`, set `GCP_PROJECT_ID`/`AGENT_REGISTRY_BASE_URL` explicitly, and omit `searchType` on `agents:search`/`mcpServers:search`. | Install bare `google-adk`, skip `GcpAuthProvider`, assume `GOOGLE_CLOUD_*` overrides `GCP_*`, or pass `searchType` to non-skill searches. | Missing extras raise `ImportError`; skipping `GcpAuthProvider()` breaks `Binding` auto-resolution; `adk_orchestrator.py` prioritizes `GCP_PROJECT_ID`/`GCP_LOCATION`/`AGENT_REGISTRY_BASE_URL` over `GOOGLE_CLOUD_*`/`AGENT_REGISTRY_ENDPOINT`; and only `skills:search` accepts `searchType`. |
| **7** | Build images remotely using the checked-in `cloudbuild/*.yaml` configs (`scripts/build_images_cloudbuild.sh` or `scripts/deploy_gcp.sh`) with `--region=asia-southeast3` and `--default-buckets-behavior=regional-user-owned-bucket`. | Run `gcloud builds submit --tag ... middleware/`, write throwaway `/tmp/cloudbuild-*.yaml` heredocs, pass a nonexistent `--file` flag, or require local Docker. | `middleware/Dockerfile` copies both `schemas/` and `middleware/` from repo root; checked-in `cloudbuild/{middleware,sample-mcp-server,sample-a2a-agent}.yaml` enforce `CLOUD_LOGGING_ONLY` + `_IMAGE_URI` with zero local Docker. |
| **8** | Keep the verified Bangkok-first placement (`GCP_LOCATION=asia-southeast3` for Cloud Run + Agent Registry `Services`, `AGENT_REGISTRY_SKILLS_LOCATION=global`, `INCLUDE_PUBLIC_SKILLS=false` filtering strictly to `private-*`), probe **`/health`** (or `/v1/health`) externally on Cloud Run, and target `finops-advisor-agent` explicitly on live `agents[]`. | Deploy duplicate multi-region Cloud Run stacks, point live standalone `Skills` at single-region `asia-southeast3`, probe `/healthz` externally on `https://*.run.app`, or assume `agents[0]` is your custom A2A agent. | Google Frontend (GFE) intercepts external `/healthz` with HTML `404`; single-region `/skills` endpoints time out; live Agent Registry lists Google Workspace Agent ahead of `finops-advisor-agent` and nests `card.content.supportedInterfaces` (`mcp_gateway.py` handles both); `CatalogService.build_snapshot()` bounds concurrency with `Semaphore(8)`. |

---

## Quick-Start Patterns

### 1. Run & Verify Middleware (Local Workshop vs. Live Cloud Run)

**Local Workshop Mode** (requires `make run-mock` and `make run-samples` running, plus `make demo-sync` completed from Lab 02):

```bash
# Terminal 3: starts uvicorn middleware.app.main:app on 127.0.0.1:8080 (REQUIRE_KMS_SIGNATURE=false)
make run-middleware

# Verify health, catalog, 4 IDE marketplace feeds, and deterministic Polyglot ZIP header
curl -fsSL http://127.0.0.1:8080/healthz | .venv/bin/python -m json.tool
curl -fsSL http://127.0.0.1:8080/v1/catalog | .venv/bin/python -m json.tool
for tool in claude copilot codex antigravity; do
  curl -fsSL "http://127.0.0.1:8080/v1/marketplace/${tool}.json" | .venv/bin/python -m json.tool > /dev/null
done
curl -fsSL -D - http://127.0.0.1:8080/v1/plugins/gcp-finops-toolkit.zip -o /tmp/gcp-finops-toolkit.zip \
  | grep -i "x-bundle-sha256"
```

**Live GCP Production Mode — Zero Local Docker (Bangkok `asia-southeast3` Primary + `global` Skills):**

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"
export GCP_LOCATION="asia-southeast3"

# Track 2 (Zero-Terraform): deploy single Bangkok stack + global Skills in one command
# scripts/deploy_gcp.sh --project "${GCP_PROJECT_ID}" --region asia-southeast3 --skills-location global
# make gcp-validate GCP_PROJECT_ID="${GCP_PROJECT_ID}"
# make gcp-destroy  GCP_PROJECT_ID="${GCP_PROJECT_ID}"

# Track 3 (Terraform + Zero-Docker Cloud Build):
# Stage 1 — Build all 3 images remotely via checked-in cloudbuild/*.yaml (before terraform apply!)
scripts/build_images_cloudbuild.sh \
  --project "${GCP_PROJECT_ID}" --region "${GCP_LOCATION}" --repo "agent-marketplace" --tag "1.0.0"

# Stage 2 — Deploy Cloud Run workloads, Agent Registry services, and scanned skills
export TF_VAR_middleware_image="${GCP_LOCATION}-docker.pkg.dev/${GCP_PROJECT_ID}/agent-marketplace/polyglot-middleware:1.0.0"
make tf-core-plan && terraform -chdir=terraform/core apply

export HUB_MIDDLEWARE_URL="$(terraform -chdir=terraform/core output -raw middleware_url)"
curl -fsSL -H "Authorization: Bearer $(gcloud auth print-identity-token --quiet)" \
  "${HUB_MIDDLEWARE_URL}/health" | .venv/bin/python -m json.tool
```

### 2. Exercise Streamable HTTP `/mcp` Gateway

```bash
MCP_URL="${HUB_MIDDLEWARE_URL:-http://127.0.0.1:8080}/mcp"
# For IAM-protected Cloud Run (allow_unauthenticated_workshop_demo = false), set:
#   export HUB_AUTH_TOKEN="$(gcloud auth print-identity-token --quiet)"
AUTH_HDR=(); if [ -n "${HUB_AUTH_TOKEN:-}" ]; then AUTH_HDR=(-H "Authorization: Bearer ${HUB_AUTH_TOKEN}"); fi

# 1. Handshake (-i prints the Mcp-Session-Id response header)
curl -fsSL -i -X POST "${MCP_URL}" "${AUTH_HDR[@]}" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}'

# 2. List tools & safety annotations (search_catalog, load_verified_skill, list_governed_mcp_servers, call_remote_a2a_agent)
curl -fsSL -X POST "${MCP_URL}" "${AUTH_HDR[@]}" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# 3. Download & verify a skill via ?alt=media + SHA-256 (+ KMS P-256 when enabled)
curl -fsSL -X POST "${MCP_URL}" "${AUTH_HDR[@]}" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"load_verified_skill","arguments":{"skill_name":"cost-anomaly-triage"}}}'
```

### 3. Hydrate Developer IDEs via `tools.hub_sync`

**Local Workshop Mode** (isolated demo HOME; omit `--dry-run` to apply):

```bash
# Preview, then hydrate all 4 IDEs into /tmp/acme-agent-hub-demo-home
.venv/bin/python -m tools.hub_sync sync \
  --middleware-url http://127.0.0.1:8080 --home /tmp/acme-agent-hub-demo-home --tools all --dry-run
make demo-hub-sync

# Zero-disk Bootstrap MCP-Only Mode (wires /mcp gateway without downloading plugin ZIPs)
.venv/bin/python -m tools.hub_sync sync \
  --middleware-url http://127.0.0.1:8080 --home /tmp/acme-agent-hub-bootstrap-home --tools all --bootstrap-mcp-only --no-exec
```

**Live GCP Production Mode** (authenticated Bangkok Cloud Run Middleware into real developer `$HOME`):

```bash
export HUB_MIDDLEWARE_URL="$(terraform -chdir=terraform/core output -raw middleware_url)"
export HUB_AUTH_TOKEN="$(gcloud auth print-identity-token --quiet)"
.venv/bin/python -m tools.hub_sync sync \
  --middleware-url "${HUB_MIDDLEWARE_URL}" \
  --tools all
.venv/bin/python -m tools.hub_sync status --json
```

### 4. Run Headless Platform Agents (`platform_agent.adk_orchestrator`)

```bash
# Inspect declarative ADK + governance wiring (offline, zero google-adk dependency)
.venv/bin/python -m platform_agent.adk_orchestrator --mode adk --print-config

# Local Workshop Mode (--mode rest against mock_registry :8090)
AGENT_REGISTRY_BASE_URL="http://127.0.0.1:8090/v1" GCP_PROJECT_ID="acme-agent-governance" \
GCP_LOCATION="asia-southeast3" REQUIRE_KMS_SIGNATURE="false" \
.venv/bin/python -m platform_agent.adk_orchestrator \
  --mode rest --skill-query "cost anomaly" --mcp-query "finops" --agent-query "finops"

# Live GCP Production Mode (--mode adk; requires pip install -r platform_agent/requirements.txt)
# Verified live split: regional Services (asia-southeast3) + shared standalone Skills (global)
export GCP_PROJECT_ID="<your-gcp-project-id>" GCP_LOCATION="asia-southeast3"
export AGENT_REGISTRY_SKILLS_LOCATION="global"
export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"
export GOOGLE_CLOUD_PROJECT="${GCP_PROJECT_ID}" GOOGLE_CLOUD_LOCATION="${GCP_LOCATION}"
export AGENT_REGISTRY_ENDPOINT="${AGENT_REGISTRY_BASE_URL}"
.venv/bin/python -m platform_agent.adk_orchestrator \
  --mode adk --project-id "${GCP_PROJECT_ID}" --location "${GCP_LOCATION}" \
  --skills-location "${AGENT_REGISTRY_SKILLS_LOCATION}" --base-url "${AGENT_REGISTRY_BASE_URL}"
```

---

## Verification Checklist

Run these checks before merging middleware, `hub_sync`, or `platform_agent` changes or shipping to Cloud Run:

1. **Unit & contract test suites (Middleware, `hub_sync`, Platform Agent):**
   ```bash
   .venv/bin/pytest -v tests/test_track1b_middleware.py tests/test_track1c_hub_sync.py tests/test_track1d_platform_agent.py
   ```
   *Pass criteria:* All tests pass (covers `?alt=media` 302 stripping, Polyglot 5-IDE ZIP validation, `/mcp` JSON-RPC 2.0, ownership-safe dotfile merging, zip-slip defense, and ADK/REST consumer flows).
2. **End-to-end live ASGI pipeline suite:**
   ```bash
   make test-e2e
   ```
   *Pass criteria:* Full ingest → mock registry → live Middleware → `hub_sync` → `platform_agent` tests pass with 0 failures.
3. **Live runtime smoke verification (local or Cloud Run):**
   - `GET /health` (or `/v1/health` on live Cloud Run; `/healthz` locally on `127.0.0.1`) returns `{"status": "ok", ...}` with expected `projectId`, `location`/`region`, `skillsLocation` (`global` on live GCP), and `requireKmsSignature`.
   - `GET /v1/catalog` lists expected plugins with 64-hex `sha256` and zero unexpected `rejectedSkills`.
   - `POST /mcp` `load_verified_skill` returns `isError: false` with `instructions` and `sha256`.
   - `.venv/bin/python -m tools.hub_sync status --home "${HUB_DEMO_HOME:-/tmp/acme-agent-hub-demo-home}" --json` (omit `--home` for live `$HOME`) reports `synced: true`, all plugins `installed_on_disk: true`, and all 4 IDE configs `wired: true` and `exists: true`.

---

## Reference Guides

Load only the reference file needed for your current task:

- [references/middleware-runtime-and-mcp-gateway.md](references/middleware-runtime-and-mcp-gateway.md) — Exact `config.py` environment variables, all 8 HTTP endpoints, `?alt=media` 302 redirect & 4-gate verification pipeline, deterministic Polyglot 5-IDE ZIP file map, `/mcp` JSON-RPC 2.0 tool schemas, and Cloud Run build/deploy specs.
- [references/hub-sync-and-adk-consumption.md](references/hub-sync-and-adk-consumption.md) — Exact 4-IDE workstation wiring paths, `tools.hub_sync` CLI flags & bootstrap mode, `.sync-manifest.json` ownership & safety invariants, and Google ADK (`AgentRegistry`, `GCPSkillRegistry`, `SkillToolset`, `GcpAuthProvider`) + REST `v1` consumer patterns.
