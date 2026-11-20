# Middleware Runtime, Polyglot Bundles, `/mcp` Gateway & Cloud Run Deployment

Authoritative reference for `middleware/app/` (`main.py`, `config.py`, `registry_client.py`, `security_verifier.py`, `polyglot_builder.py`, `mcp_gateway.py`), `middleware/Dockerfile`, and `terraform/core/cloud_run.tf`.

---

## 1. Environment Variables (`middleware/app/config.py`)

`MiddlewareSettings.from_env()` reads 18 environment variables. Note the critical default difference between code and `Makefile`: `MiddlewareSettings` defaults `REQUIRE_KMS_SIGNATURE` to **`true`** (fail-closed), whereas `Makefile`'s `make run-middleware` sets `REQUIRE_KMS_SIGNATURE ?= false` for local Workstream A labs.

| Environment Variable | Code Default (`config.py`) | Local Workshop (`make run-middleware`) | Live GCP Production (Cloud Run) | Purpose |
|---|---|---|---|---|
| `AGENT_REGISTRY_BASE_URL` | `http://127.0.0.1:8090/v1` | `http://127.0.0.1:8090/v1` | `https://agentregistry.googleapis.com/v1` | Upstream Agent Registry `v1` REST base URL |
| `GCP_PROJECT_ID` | `acme-agent-governance` | `acme-agent-governance` | `${var.project_id}` | Catalog GCP project hosting standalone skills & services |
| `GCP_LOCATION` | `us-central1` | `us-central1` | `${var.location}` | Agent Registry region (e.g. `us-central1`, `us`, `eu`) |
| `MIDDLEWARE_PUBLIC_URL` | `http://127.0.0.1:8080` | `http://127.0.0.1:8080` | `https://polyglot-middleware-<NUM>.<LOC>.run.app` (or custom domain) | Public base URL embedded in marketplace JSONs, `bundleUrl`, and `agent-hub-gw` `/mcp` entry |
| `REQUIRE_KMS_SIGNATURE` | `true` | `false` (enable in Lab 07) | `true` (`var.require_kms_signature = true`) | Fail-closed Cloud KMS `EC_SIGN_P256_SHA256` verification gate |
| `KMS_PUBLIC_KEY_PEM_PATH` | `""` | Path to local public PEM | Container path to mounted PEM (or leave empty when using `KMS_SIGNING_KEY_VERSION`) | File path to ECDSA P-256 public key PEM |
| `KMS_PUBLIC_KEY_PEM` | `""` | *(unset)* | Optional inline PEM string (e.g. Secret Manager env) | Highest-precedence ECDSA P-256 public key PEM |
| `KMS_SIGNING_KEY_VERSION` | `""` | *(unset)* | `projects/{p}/locations/{l}/keyRings/{r}/cryptoKeys/{k}/cryptoKeyVersions/{v}` | Live Cloud KMS `/publicKey` fetch (cached in memory; requires `roles/cloudkms.publicKeyViewer` on `middleware_sa`) |
| `AGENT_REGISTRY_AUTH_TOKEN` | `""` | *(unset)* | *(unset — use metadata server)* | Optional explicit upstream OAuth2 token; falls back to `GOOGLE_OAUTH_ACCESS_TOKEN` |
| `HUB_AUTH_TOKEN` | `""` | *(unset)* | *(client-side only; never forwarded upstream)* | Stored on `MiddlewareSettings` but decoupled from `AgentRegistryClient` upstream OAuth2 |
| `GCP_QUOTA_PROJECT` | `${GCP_PROJECT_ID}` | `acme-agent-governance` | `${var.project_id}` | Sent as `x-goog-user-project` on Agent Registry API requests (stripped on 302 redirects) |
| `ALLOW_DEPRECATED_SKILLS` | `true` | `true` | `true` (serves with warnings) or `false` (strict `STATE_ACTIVE` only) | Whether `STATE_DEPRECATED` skills remain downloadable with warnings |
| `MARKETPLACE_NAME` | `acme-enterprise-marketplace` | default | Org marketplace slug | Marketplace identifier across all 4 IDE feeds |
| `MARKETPLACE_DISPLAY_NAME` | `Acme Enterprise Agent Marketplace` | default | Org marketplace display name | Human-readable display name (Codex `interface.displayName`, `/v1/catalog`) |
| `MARKETPLACE_DESCRIPTION` | `Governed enterprise plugins, skills, MCP servers, and A2A agents backed by Google Cloud Agent Registry.` | default | Org marketplace description | Shared description in feeds and `/v1/catalog` |
| `MARKETPLACE_OWNER_NAME` | `Acme Platform Security` | default | Platform team name | `owner.name` and bundle `author.name` |
| `MARKETPLACE_OWNER_EMAIL` | `platform-security@acme.example.com` | default | Platform team email | `owner.email` and bundle `author.email` |
| `MARKETPLACE_OWNER_URL` | `https://acme.example.com/platform` | default | `https://...` (non-HTTPS falls back to default) | Bundle `author.url` (Antigravity requires `https://`) |

**Public key load precedence in `security_verifier.py` (`load_kms_public_key`):**
1. `KMS_PUBLIC_KEY_PEM` (inline PEM)
2. `KMS_PUBLIC_KEY_PEM_PATH` (file on disk)
3. `KMS_SIGNING_KEY_VERSION` (`GET https://cloudkms.googleapis.com/v1/{keyVersion}/publicKey`, validating `algorithm == "EC_SIGN_P256_SHA256"`, cached on `SecurityVerifier`)
4. If `REQUIRE_KMS_SIGNATURE=true` and all three are empty → fails closed and rejects every skill into `rejectedSkills`.

---

## 2. All 8 HTTP Endpoints (`middleware/app/main.py`)

| # | Method & Path | Response Type / Headers | Payload Summary & Behavior |
|---|---|---|---|
| 1 | `GET /healthz` | `200 application/json` | `{"status": "ok", "service": "agent-registry-marketplace-middleware", "projectId", "location", "requireKmsSignature"}` — Cloud Run startup probe target |
| 2 | `GET /v1/catalog` | `200 application/json` | Live governed snapshot: `marketplace`, `plugins[]` (`name`, `displayName`, `version`, `description`, `sha256`, `bundleUrl`, `download_url`, `skills`, `mcpServers`, `agents`, `warnings`, `keywords`), `skills[]` (verified + `kmsVerified`, `kmsKeyVersion`, `frontmatter`), `rejectedSkills[]`, `mcpServers[]`, `agents[]` |
| 3 | `GET /v1/marketplace/claude.json` | `200 application/json` | Claude Code catalog (`name`, `owner`, `metadata`, `plugins[].source = {"source": "archive", "url": "<public>/v1/plugins/<name>.zip"}`) |
| 4 | `GET /v1/marketplace/copilot.json` | `200 application/json` | VS Code / Copilot catalog (`name`, `owner`, `metadata`, `plugins[]` with `category`, `tags`, `source.source = "archive"`) |
| 5 | `GET /v1/marketplace/codex.json` | `200 application/json` | OpenAI Codex catalog (`name`, `interface.displayName`, `plugins[]` with `source.source = "archive"`, `policy: {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}`, `category`) |
| 6 | `GET /v1/marketplace/antigravity.json` | `200 application/json` | Antigravity archive-mode catalog (`name`, `description`, `plugins[]` with `displayName`, `source: {"type": "archive", "url": "...", "sha256": "<64-hex>"}`) |
| 7 | `GET /v1/plugins/{plugin_name}.zip` | `200 application/zip`, `Content-Disposition`, `X-Bundle-Sha256`, `Cache-Control: no-store` | Deterministic 5-IDE Polyglot ZIP bundle. Returns `404` if plugin is missing or all its skills were rejected by policy. |
| 8 | `POST /mcp` | `200 application/json` (or `202` for `notifications/*`, `400` on JSON parse error); sets `Mcp-Session-Id` on `initialize` | Streamable HTTP JSON-RPC 2.0 MCP Gateway (`2025-03-26`). |

**Upstream error handling:** `RegistryClientError` returns `502 {"error": "upstream_registry_error", "message", "upstreamStatus"}`; `httpx.HTTPError` returns `502 {"error": "upstream_registry_unreachable", "message"}`.

---

## 3. Upstream OAuth2, `?alt=media` 302 Redirect & 4-Gate Verification

### Upstream Token Acquisition (`registry_client.py`)
`AgentRegistryClient.get_access_token()` never uses `HUB_AUTH_TOKEN`:
1. Explicit `AGENT_REGISTRY_AUTH_TOKEN` (or `GOOGLE_OAUTH_ACCESS_TOKEN`) if set.
2. When targeting `googleapis.com`: fetches OAuth2 token from GCE/Cloud Run metadata server (`http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token` with `Metadata-Flavor: Google`), cached with a 60-second refresh skew (`TOKEN_REFRESH_SKEW_SECONDS = 60.0`) under an `asyncio.Lock`.
3. Local workstation fallback for live `googleapis.com`: shells out via `asyncio.to_thread` to `gcloud auth application-default print-access-token`, then `gcloud auth print-access-token` (cached 300s).
4. Against local non-`googleapis.com` endpoints (such as `mock_registry` `:8090`), sends `Authorization: Bearer adc-simulated-token` plus `x-goog-user-project` and `x-goog-api-client` so redirect header stripping is actively exercised in tests and local labs.

### `?alt=media` HTTP 302 Redirect Flow
1. `GET /v1/projects/{p}/locations/{l}/skills/private-{id}/revisions/{rev}?alt=media` is sent with `follow_redirects=False`, `Accept: application/zip, application/octet-stream, */*`, `Authorization`, `x-goog-user-project`, and `x-goog-api-client`.
2. On any `301`/`302`/`303`/`307`/`308` response (up to `max_redirects=5`), `download_skill_revision_media()` resolves `Location` via `urljoin` and calls `strip_google_redirect_headers()` — removing `Authorization` and every `x-goog-*` header — before issuing the next hop.
3. Verify the 403-vs-200 behavior directly against local `mock_registry`:
   ```bash
   BASE="http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/us-central1"
   DEFAULT_REV="$(curl -fsSL "${BASE}/skills/private-cost-anomaly-triage" \
     | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["defaultRevision"])')"
   SIGNED_LOC="$(curl -sS -D - -o /dev/null \
     -H "Authorization: Bearer simulated-token" -H "x-goog-user-project: acme-agent-governance" \
     "http://127.0.0.1:8090/v1/${DEFAULT_REV}?alt=media" \
     | awk 'tolower($1)=="location:" {print $2}' | tr -d '\r')"

   # Leaked header -> 403; clean request -> 200 (SIGNED_LOC is already a full URL)
   curl -sS -o /dev/null -w "Leaked: %{http_code}\n" -H "x-goog-user-project: acme-agent-governance" "${SIGNED_LOC}"
   curl -sS -o /tmp/downloaded-skill.zip -w "Clean:  %{http_code}\n" "${SIGNED_LOC}"
   ```

### 4-Gate `SecurityVerifier` Pipeline (`security_verifier.py`)
1. **Lifecycle Gate:** Allows `STATE_ACTIVE` (and `STATE_DEPRECATED` with a warning only when `ALLOW_DEPRECATED_SKILLS=true`). Rejects `STATE_DRAFT`, `STATE_DISABLED`, `STATE_DECOMMISSIONED`, or missing `defaultRevision`.
2. **Scan Gate:** Requires `revision.scanResult.scanState` in `{"SUCCEEDED", "DISABLED"}`, `len(scanResult.findings) == 0`, and `skill.defaultRevisionFindingCount == 0`.
3. **Archive Integrity Gate:** Constant-time `hmac.compare_digest` verifying `sha256(zip_bytes)` matches `revision.sha256Hash` (stripping optional `sha256:` prefix) and enforces strictly one uppercase `SKILL.md`.
4. **Cloud KMS Signature Gate** (when `REQUIRE_KMS_SIGNATURE=true`): Reads `frontmatter.metadata.kms_signature` (base64 ASN.1 DER) and verifies ECDSA P-256 (`SECP256R1`) SHA-256 against canonical `agent-registry-skill-v1` payload (non-circular: strips `kms_signature` & `kms_key_version`), raw ZIP bytes, hex SHA-256 string, or `plugin_sync` prehashed deterministic unsigned ZIP digest.

---

## 4. Deterministic Polyglot 5-IDE Bundle File Map (`polyglot_builder.py`)

`build_plugin_bundles()` groups verified skills by `frontmatter.metadata.plugin_name` (sanitized via `sanitize_kebab_name()` — lowercase kebab-case, prepends `enterprise-` if starting with disallowed brand prefixes `antigravity`, `gemini`, `claude`, `cursor`), resolves comma-separated `metadata.mcp_service_id` and `metadata.agent_service_id` against Agent Registry `mcpServers`/`agents`, and writes a deterministic ZIP (`FIXED_ZIP_TIMESTAMP = (2025, 1, 1, 0, 0, 0)`, sorted paths, Unix `0644`/`0755`, strict rejection of symlinks, `..`, backslashes, NUL bytes, and absolute paths).

| Path Inside `{plugin_name}.zip` | Target IDE(s) | Key Synthesized Contents / Invariants |
|---|---|---|
| `plugin.json` | Agent Plugins 1.0, VS Code/Copilot, Codex | `$schema: 1.0.0/plugin.schema.json`, `extensions["com.openai"]`, `extensions["com.google.agentregistry"].verifiedSkills[]` |
| `.antigravity-plugin/plugin.json` | Google Antigravity | Strict `v1/plugin.json` (`additionalProperties: false`), `displayName`, `logo: "assets/logo.svg"`, `<= 3` `suggestedPrompts`, `https://` `author.url` |
| `.claude-plugin/plugin.json` | Claude Code | `name`, `version`, `description`, `author` |
| `.codex-plugin/plugin.json` | OpenAI Codex | `name`, `version`, `description`, `author` |
| `assets/logo.svg` | Google Antigravity | Built-in 128×128 (1:1 square) SVG shield icon |
| `mcp.json` | Agent Plugins 1.0, VS Code/Copilot | `type: "streamable-http"`, static `url` (**no `${ENV}` expansion**), sanitized keys (`<= 20` chars, no dots) + always includes `agent-hub-gw` (`${MIDDLEWARE_PUBLIC_URL}/mcp`) |
| `.mcp.json` | Claude Code | Same sanitized `mcpServers` map (`type: "streamable-http"`) |
| `mcp_config.json` | Google Antigravity | `url` + `authProviderType: "google_credentials"`, **no `variables[]`** so plugin installs enabled immediately |
| `skills/<skill-name>/SKILL.md` (+ `references/`, `scripts/`) | All 5 targets | Extracted from verified Skill ZIPs; preserves `0755` executable bits on `scripts/*` |
| `agents/<agent-slug>.md` | Antigravity, Claude Code | YAML frontmatter (`mainAgent: true`, `subagent: true`, `model: "inherit"`, `commandExecutionPolicy: "sandbox"`, `inheritMcp: true`). *Note:* slug comes from registered Agent Registry `Service` ID (e.g. `finops-advisor-agent`, or `<plugin>-advisor` fallback if no A2A agent is linked). |
| `com.github.copilot/agents/<agent-slug>.agent.md` | VS Code / GitHub Copilot | YAML frontmatter (`name`, `description`, deduplicated `tools[]` combining gateway + linked MCP tools) |
| `rules/AGENTS.md` | Google Antigravity | Always-on governance guardrails |
| `rules/finops-guardrails.md` (or `rules/<plugin>-guardrails.md`) | Google Antigravity | `trigger: glob` with a **comma-separated string** (never a YAML list): `"**/*.sql, **/*.tf, **/*.yaml"` for finops bundles, or `"**/*.yaml, **/*.json, **/*.tf"` for non-finops bundles |
| `README.md` | Human / docs | Summary of verified skills (`revision`, `sha256`, `kms_verified`), linked MCP servers, and A2A agents |

---

## 5. Streamable HTTP `/mcp` JSON-RPC 2.0 Reference (`mcp_gateway.py`)

- **Protocol Version:** `2025-03-26` (echoes client `protocolVersion` if supplied).
- **Server Info:** `{"name": "gcp-agent-registry-middleware-mcp", "version": "1.0.0"}`, `capabilities: {"tools": {"listChanged": false}}`.
- **Methods:** `initialize` (returns `Mcp-Session-Id` header), `ping`, `notifications/*` (`202 Accepted` empty body), `tools/list`, `tools/call`.
- **Tool Result Envelope:** `{"content": [{"type": "text", "text": "<json>"}], "structuredContent": {...}, "isError": bool}`.
- **Error Codes:** `-32700` parse error (HTTP 400), `-32600` invalid request, `-32601` unknown method/tool, `-32602` invalid params.

| Tool Name | Input Schema (`additionalProperties: false`) | MCP Safety Annotations | Behavior |
|---|---|---|---|
| `search_catalog` | `query` (`string`, default `""`), `kind` (`"all"\|"skill"\|"mcp_server"\|"agent"`, default `"all"`) | `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`, `openWorldHint: false` | Case-insensitive search over live verified `/v1/catalog` snapshot (`totalMatches`, `results.{skills,mcpServers,agents}`). |
| `load_verified_skill` | `skill_name` (`string`, **required** — accepts short name, `private-<name>`, or full resource name) | `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`, `openWorldHint: false` | Fetches skill + `defaultRevision`, enforces all 4 `SecurityVerifier` gates, downloads via `?alt=media` 302 redirect, returns `frontmatter`, `instructions`, `files[]`, `sha256`, `kmsVerified`, `warnings`. |
| `list_governed_mcp_servers` | `{}` | `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`, `openWorldHint: false` | Lists registered MCP servers from Agent Registry with `interfaces` and `tools` annotations. |
| `call_remote_a2a_agent` | `agent_name` (`string`, **required** — short ID, `displayName`, URN, or full name), `task` (`string`, **required**) | `readOnlyHint: false`, `destructiveHint: false`, `idempotentHint: false`, `openWorldHint: true` | Resolves endpoint from `interfaces[0].url` or `card.supportedInterfaces[0].url` and posts JSON-RPC `message/send`. |

---

## 6. Cloud Run Container Build & Deploy (`middleware/Dockerfile` + `terraform/core/cloud_run.tf`)

`middleware/Dockerfile` runs `python:3.12-slim` as non-root `appuser` (UID `10001`), copies `schemas/` and `middleware/` from the **repository root**, installs `middleware/requirements.txt`, and launches `uvicorn middleware.app.main:app --host 0.0.0.0 --port ${PORT:-8080}`.

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"
export GCP_LOCATION="us-central1"
export AR_PREFIX="${GCP_LOCATION}-docker.pkg.dev/${GCP_PROJECT_ID}/agent-marketplace"

# 1. Build polyglot-middleware from repo root via Cloud Build --config (NOT --file)
cat <<EOF > /tmp/cloudbuild-middleware.yaml
steps:
  - name: "gcr.io/cloud-builders/docker"
    args: ["build", "-f", "middleware/Dockerfile", "-t", "${AR_PREFIX}/polyglot-middleware:1.0.0", "."]
images:
  - "${AR_PREFIX}/polyglot-middleware:1.0.0"
EOF
gcloud builds submit --project="${GCP_PROJECT_ID}" --config=/tmp/cloudbuild-middleware.yaml .

# 2. Build sample FinOps MCP server & A2A agent (each has a root Dockerfile)
gcloud builds submit --project="${GCP_PROJECT_ID}" --tag="${AR_PREFIX}/sample-mcp-server:1.0.0" samples/mcp-server
gcloud builds submit --project="${GCP_PROJECT_ID}" --tag="${AR_PREFIX}/sample-a2a-agent:1.0.0" samples/a2a-agent
```

**Key `terraform/core/cloud_run.tf` & `variables.tf` settings for `polyglot-middleware`:**
- **Service account:** `agent-marketplace-middleware@${var.project_id}.iam.gserviceaccount.com` with **strictly `roles/agentregistry.viewer`** (plus `roles/cloudkms.publicKeyViewer` added by Workstream B when KMS signing is enabled).
- **Container & scaling:** port `8080`, `min_instance_count = 0`, `max_instance_count = 5`, `cpu = "1"`, `memory = "512Mi"`, HTTP startup probe on `GET /healthz`.
- **Production variables (`terraform/core/variables.tf`):**
  - `middleware_image`: defaults to `${ar_image_prefix}/polyglot-middleware:latest` in `cloud_run.tf`; when deploying a tagged build (e.g. `:1.0.0`) or immutable `@sha256:...` digest in production, set `TF_VAR_middleware_image` or `middleware_image` in `terraform/core/terraform.tfvars` before `terraform apply`.
  - `middleware_public_url`: custom domain / HTTPS LB URL (defaults to `https://polyglot-middleware-${project_number}.${location}.run.app`).
  - `allow_unauthenticated_workshop_demo = false` (default secure — requires `roles/run.invoker` + `Authorization: Bearer $(gcloud auth print-identity-token)`).
  - `deletion_protection = true` in production (`false` default for ephemeral workshops).
  - `require_kms_signature = true` + `kms_public_key_pem_path` (wired directly in `terraform/core/cloud_run.tf`). Note: `KMS_SIGNING_KEY_VERSION` is a runtime container env var with no Terraform variable in `terraform/core/variables.tf` yet — to fetch the public key directly from Cloud KMS `/publicKey`, either add an `env` block for `KMS_SIGNING_KEY_VERSION` in `terraform/core/cloud_run.tf` or run `gcloud run services update polyglot-middleware --project="${GCP_PROJECT_ID}" --region="${GCP_LOCATION}" --update-env-vars="KMS_SIGNING_KEY_VERSION=projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>/cryptoKeyVersions/<v>"`.
- **Terraform outputs (`terraform/core/outputs.tf`):** `middleware_url`, `middleware_mcp_url`, `middleware_sa_email`, `sample_mcp_server_url`, `sample_a2a_agent_url`, `artifact_registry_repository_url`.
