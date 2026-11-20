# Lab 03: Build & Deploy the Polyglot Marketplace Middleware

**Workstream:** A — Core Platform & Middleware  
**Duration:** 40 minutes  
**Prerequisites:** Completed [Lab 02](02-register-agent-registry.md) with `gcp-finops-toolkit` synced into the registry.

---

## Learning Objectives

By the end of this lab, you will:
1. Inspect and run the **Polyglot Marketplace Middleware** ([middleware/app/main.py](../../middleware/app/main.py)) exposing `/v1/catalog`, 4 IDE marketplace feeds (`/v1/marketplace/{claude,copilot,codex,antigravity}.json`), deterministic Polyglot 5-IDE ZIP bundles (`/v1/plugins/{plugin_name}.zip`), and a Streamable HTTP `/mcp` gateway.
2. Prove why Agent Registry's `?alt=media` **HTTP 302 signed-URL redirect** requires stripping `Authorization` and all `x-goog-*` headers (`x-goog-user-project`, `x-goog-api-client`) on cross-origin hops—and verify how [middleware/app/registry_client.py](../../middleware/app/registry_client.py) handles it cleanly.
3. Invoke the Middleware's Streamable HTTP `/mcp` tools (`search_catalog`, `load_verified_skill`, `list_governed_mcp_servers`, `call_remote_a2a_agent`) directly.
4. Build and deploy the container images to **Cloud Run** via **Artifact Registry** and [terraform/core/cloud_run.tf](../../terraform/core/cloud_run.tf) (Live GCP Mode).

---

## Step 1: Observe the `?alt=media` HTTP 302 Header-Stripping Gotcha

When fetching a `SkillRevision` archive from Agent Registry:
```text
GET /v1/projects/{p}/locations/{l}/skills/private-{id}/revisions/{rev}?alt=media
```
Agent Registry returns **HTTP 302 Found** pointing to a short-lived GCS signed URL (simulated in `mock_registry` at `/scotty-signed/{token}`). If an HTTP client forwards `Authorization` or `x-goog-*` headers across that redirect, **GCS rejects the request with HTTP 403**!

### Mode A: Prove It Live Against `mock_registry`

With `make run-mock`, `make run-samples`, and `make demo-sync` already completed from Lab 02:

```bash
BASE="http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/us-central1"

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

In a third terminal, start the middleware on port `8080` (`REQUIRE_KMS_SIGNATURE=false` in Workstream A; you will enable strict KMS signature enforcement in [Lab 07](07-kms-encryption-and-signing.md)):

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

## Step 4: Deploy to Cloud Run (Mode B: Live GCP Mode)

Build and push the three container images ([middleware/Dockerfile](../../middleware/Dockerfile), [samples/mcp-server/Dockerfile](../../samples/mcp-server/Dockerfile), [samples/a2a-agent/Dockerfile](../../samples/a2a-agent/Dockerfile)) to Artifact Registry:

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"
export GCP_LOCATION="us-central1"
export AR_PREFIX="${GCP_LOCATION}-docker.pkg.dev/${GCP_PROJECT_ID}/agent-marketplace"

# Build & push polyglot-middleware via Cloud Build --config
# (uses repo root as build context so both schemas/ and middleware/ are copied;
# `gcloud builds submit --tag` only reads a root Dockerfile and has no --file flag)
cat <<EOF > /tmp/cloudbuild-middleware.yaml
steps:
  - name: "gcr.io/cloud-builders/docker"
    args:
      - "build"
      - "-f"
      - "middleware/Dockerfile"
      - "-t"
      - "${AR_PREFIX}/polyglot-middleware:1.0.0"
      - "."
images:
  - "${AR_PREFIX}/polyglot-middleware:1.0.0"
EOF

gcloud builds submit \
  --project="${GCP_PROJECT_ID}" \
  --config=/tmp/cloudbuild-middleware.yaml .

# Local Docker alternative (if Docker is installed & authenticated via `gcloud auth configure-docker`):
#   docker build -f middleware/Dockerfile -t "${AR_PREFIX}/polyglot-middleware:1.0.0" .
#   docker push "${AR_PREFIX}/polyglot-middleware:1.0.0"

# Build & push sample FinOps MCP server & A2A agent (each directory has a root Dockerfile)
gcloud builds submit --project="${GCP_PROJECT_ID}" \
  --tag="${AR_PREFIX}/sample-mcp-server:1.0.0" samples/mcp-server
gcloud builds submit --project="${GCP_PROJECT_ID}" \
  --tag="${AR_PREFIX}/sample-a2a-agent:1.0.0" samples/a2a-agent
```

Deploy via [terraform/core/cloud_run.tf](../../terraform/core/cloud_run.tf) (least-privilege `middleware_sa` gets `roles/agentregistry.viewer` ONLY):

```bash
make tf-core-plan
terraform -chdir=terraform/core apply

export MIDDLEWARE_URL="$(terraform -chdir=terraform/core output -raw middleware_url)"
curl -fsSL -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "${MIDDLEWARE_URL}/healthz" | .venv/bin/python -m json.tool
```

---

## Step 5: Hands-On Coding Exercise (Optional Deep Dive)

Complete the `# WORKSHOP-TODO(lab-03)` blocks in [starter/lab03_middleware_redirect_exercise.py](../../starter/lab03_middleware_redirect_exercise.py) to implement cross-origin header stripping, SHA-256-verified media downloads, and Polyglot ZIP assembly:

```bash
.venv/bin/python starter/lab03_middleware_redirect_exercise.py
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

---

**Next Lab:** [Lab 04: 1-Command Multi-IDE Developer Experience →](04-ide-developer-experience.md)
