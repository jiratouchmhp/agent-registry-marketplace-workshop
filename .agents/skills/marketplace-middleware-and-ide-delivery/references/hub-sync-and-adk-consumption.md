# Multi-IDE `hub_sync` Hydration & Direct Platform Agent (ADK / REST `v1`) Consumption

Authoritative reference for `tools/hub_sync/` (`cli.py`, `hydration.py`, `adapters.py`) and `platform_agent/` (`adk_orchestrator.py`, `rest_consumer.py`, `requirements.txt`).

---

## 1. Why Local Polyglot Hydration + `/mcp` Beats Virtual Git Smart-HTTP Alone

| Enterprise IDE / CLI | Hard Platform Constraint | How `tools.hub_sync` + Middleware Solves It |
|---|---|---|
| **Google Antigravity** | `agy plugin install <url>` only accepts `github.com` remote URLs; corporate GitLab & internal Cloud Run URLs fail. | Downloads & verifies SHA-256 of `/v1/plugins/{name}.zip`, extracts to `~/.acme-agent-hub/plugins/<name>`, copies atomically to `~/.gemini/config/plugins/<name>`, and runs `agy plugin install <local-dir>`. |
| **Claude Code** | HTTP URL marketplaces forbid relative `./` plugin sources; local directory & Git marketplaces allow `./plugins/<name>`. | Writes local `<hub>/.claude-plugin/marketplace.json` with `./plugins/<name>` sources and registers `"source": "directory"` in `~/.claude/settings.json`. |
| **VS Code / GitHub Copilot** | VS Code Web & GitHub-managed marketplaces assume GitHub repos. | Generates `<hub>/marketplace.json`, `<hub>/.github/plugin/marketplace.json`, and `<hub>/vscode-settings-snippet.json` using `file://` URIs and local `chat.pluginLocations` (plus Copilot CLI local marketplace commands). |
| **OpenAI Codex** | Codex Enterprise Admin ties remote distribution to GitHub; local marketplaces expect `~/.agents/plugins/marketplace.json` + `~/.codex/agents/*.toml` + `~/.codex/config.toml`. | Hydrates local symlinks under `~/.agents/plugins/plugins/<name>`, renders verified TOML agents, and performs a section-preserving merge into `~/.codex/config.toml`. |

---

## 2. `python -m tools.hub_sync` CLI Reference (`tools/hub_sync/cli.py`)

### Subcommand `sync`
```bash
.venv/bin/python -m tools.hub_sync sync [OPTIONS]
```

| Option | Env / Default | Purpose |
|---|---|---|
| `--middleware-url` | `$HUB_MIDDLEWARE_URL` or `http://127.0.0.1:8080` | Base URL of the primary Polyglot Marketplace Middleware (e.g. Bangkok `asia-southeast3`) |
| `--fallback-middleware-url` | `$HUB_FALLBACK_MIDDLEWARE_URL` or `None` | Optional secondary Middleware URL (e.g. Singapore `asia-southeast1`) used automatically if primary is unreachable |
| `--home` | `~` | Target developer home directory (use `/tmp/acme-agent-hub-demo-home` for safe workshop testing) |
| `--hub-dir` | `<home>/.acme-agent-hub` | Override local hub storage directory |
| `--tools` | `all` | `all` or comma-separated subset of `claude,copilot,codex,antigravity` |
| `--token` | `$HUB_AUTH_TOKEN` | Optional Bearer token for Cloud Run IAM / Middleware auth (stripped automatically on cross-origin redirects) |
| `--bootstrap-mcp-only` | `false` | Wire `/mcp` gateway configs only — skips plugin ZIP downloads, disk extraction, and plugin pruning |
| `--dry-run` | `false` | Fetches & verifies catalog/ZIPs in memory and previews planned changes without writing files |
| `--apply-vscode` | `false` | Also merges `chat.plugins.*` into `<home>/.config/Code/User/settings.json` (by default only writes snippet) |
| `--no-exec` | `false` | Skips invoking `agy plugin install <local-dir>` even when `agy` is on `PATH` |
| `--marketplace-name` | from `/v1/catalog` | Override enterprise marketplace identifier |

### Subcommand `status`
```bash
.venv/bin/python -m tools.hub_sync status [--home <DIR>] [--hub-dir <DIR>] [--json]
```
Displays hub directory, middleware URL, marketplace name, last sync timestamp, wired tools, installed plugins with versions, `installed_on_disk` check, 64-hex SHA-256 digests, and presence of all 4 IDE config files.

### 5-Phase Atomic Execution Flow (`sync_hub`)
1. **In-Memory Pre-Flight:** Fetches `GET /v1/catalog`, downloads every plugin ZIP (verifying SHA-256 against catalog `sha256` or `X-Content-Sha256`/`X-Bundle-Sha256`), and runs `validate_zip_bytes()` in memory. Any error aborts with exit code `1` before touching disk.
2. **Atomic Extraction & Pruning:** Extracts each plugin via `safe_extract_zip()` (staged temp dir → `os.replace` with rollback) into `<hub>/plugins/<name>` preserving `0755` script permissions. Prunes previously owned plugin folders **only** when `catalog_complete and not bootstrap_mcp_only`.
3. **Top-Level Marketplace Indexes:** Writes 5 catalog JSONs under `<hub>/`: `marketplace.json`, `.github/plugin/marketplace.json`, `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json`, `antigravity-marketplace.json`.
4. **IDE Adapter Wiring:** Aggregates sanitized MCP server configs (`sanitize_mcp_key()` `<= 20` chars, deterministic 4-hex hash suffix on truncation) and runs selected adapters (`wire_claude`, `wire_copilot`, `wire_codex`, `wire_antigravity`).
5. **Manifest Persistence:** Atomically writes `<hub>/.sync-manifest.json` (`NamedTemporaryFile` same-dir → `fsync` → `os.replace`).

---

## 3. Exact 4-IDE Workstation Wiring Paths (`tools/hub_sync/adapters.py`)

| IDE / CLI | Full Hydration Paths Written | Bootstrap MCP-Only (`--bootstrap-mcp-only`) | Verification Commands |
|---|---|---|---|
| **Claude Code** | `~/.claude/settings.json` (`extraKnownMarketplaces.<mkt>` with `source: "directory"`, `enabledPlugins["<plugin>@<mkt>"] = true`), `<hub>/.claude-plugin/marketplace.json` | Writes `<hub>/claude-bootstrap-mcp.json` (`type: "streamable-http"`, `Authorization: Bearer ${HUB_AUTH_TOKEN}`) & prints `claude mcp add --transport http <key> <url>`; preserves existing `enabledPlugins`. | `claude plugin list` |
| **VS Code / Copilot** | `<hub>/marketplace.json`, `<hub>/.github/plugin/marketplace.json`, `<hub>/vscode-settings-snippet.json` (`chat.plugins.enabled`, `chat.plugins.marketplaces: ["file:///..."]`, `chat.pluginLocations`); merges `~/.config/Code/User/settings.json` when `--apply-vscode` is passed | Writes `<hub>/vscode-mcp-snippet.json` (`{"servers": {"<key>": {"type": "http", "url": "..."}}}`) alongside settings snippet. | `copilot plugin marketplace add <hub>`<br>`copilot plugin install <plugin>@<mkt>` |
| **OpenAI Codex** | `~/.agents/plugins/marketplace.json` (`source: {source: "local", path: "./plugins/<name>"}`, `policy: {installation: "AVAILABLE", authentication: "ON_INSTALL"}`), symlinks `~/.agents/plugins/plugins/<name>` → `<hub>/plugins/<name>`, `~/.codex/agents/<agent>.toml` (`tomllib`-verified), section-preserving merge into `~/.codex/config.toml` (`[mcp_servers.<key>]`, `bearer_token_env_var = "HUB_AUTH_TOKEN"`) | Merges `[mcp_servers.acme-agent-hub]` (targeting `<middleware>/mcp`) and catalog MCP servers into `~/.codex/config.toml`; leaves existing plugins & agents untouched. | `codex mcp list` |
| **Google Antigravity** | `<hub>/antigravity-marketplace.json` (archive mode w/ `sha256` for `https://` or localhost `http://` `.zip`), `~/.gemini/config/mcp_config.json` (`url`, `authProviderType: "google_credentials"`, strips reserved `PLUGIN_ROOT`/`PLUGIN_DATA`), atomic mirror to `~/.gemini/config/plugins/<name>`, runs `agy plugin install <hub>/plugins/<name>` (unless `--no-exec`) | Merges `acme-agent-hub` (`<middleware>/mcp`) and catalog MCP servers into `~/.gemini/config/mcp_config.json`; skips plugin folder copy & `agy plugin install`. | `agy plugin list --json`<br>`agy --agent <agent-slug>` |

---

## 4. `.sync-manifest.json` Schema & Ownership Invariants (`tools/hub_sync/hydration.py`)

Saved atomically at `<home>/.acme-agent-hub/.sync-manifest.json` (`schema_version: "1.0.0"`):

```json
{
  "schema_version": "1.0.0",
  "middleware_url": "http://127.0.0.1:8080",
  "marketplace_name": "acme-enterprise-marketplace",
  "last_synced_at": "2026-11-20T00:00:00+00:00",
  "last_sync_complete": true,
  "tools": ["antigravity", "claude", "codex", "copilot"],
  "owned_plugins": {
    "gcp-finops-toolkit": {
      "version": "1.0.0",
      "display_name": "GCP FinOps Toolkit",
      "sha256": "<64-hex>",
      "path": "<home>/.acme-agent-hub/plugins/gcp-finops-toolkit",
      "antigravity_plugin_path": "<home>/.gemini/config/plugins/gcp-finops-toolkit",
      "skills": ["bq-query-optimizer", "cost-anomaly-triage"],
      "mcp_servers": ["agent-hub-gw", "finops-mcp-server"],
      "agents": ["finops-advisor-agent"]
    }
  },
  "owned_mcp_servers": ["agent-hub-gw", "finops-mcp-server"],
  "owned_codex_mcp_servers": ["agent-hub-gw", "finops-mcp-server"],
  "owned_codex_plugins": ["gcp-finops-toolkit"],
  "owned_codex_agents": ["finops-advisor-agent"],
  "owned_claude_plugins": ["gcp-finops-toolkit@acme-enterprise-marketplace"],
  "owned_antigravity_plugins": ["gcp-finops-toolkit"],
  "owned_vscode_plugin_locations": [],
  "owned_skill_dirs": []
}
```

**Strict Safety Guarantees:**
1. **User-authored entries are untouchable:** Any MCP server key, Claude `enabledPlugins` entry, VS Code `chat.pluginLocations` path, Codex plugin/agent file, or Antigravity plugin folder already present on disk and **not** in the previous manifest's `owned_*` list is skipped with a warning — never overwritten, claimed, or pruned.
2. **User preference preservation on managed MCP entries:** `merge_mcp_servers_dict()` preserves all 7 keys in `PRESERVED_MCP_USER_KEYS` across re-syncs: `disabled`, `disabledTools`, `disabled_tools`, `enabled`, `enabled_tools`, `autoApprove`, `alwaysAllow`. Claude Code also preserves user `false` toggles on previously owned `enabledPlugins` keys.
3. **Pruning gate:** Removed plugins, MCP keys, and Codex agents are pruned **only** when `allow_prune` is `True` (full catalog + ZIP downloads succeeded cleanly, and not `--bootstrap-mcp-only`).
4. **ZIP & path security:** `validate_zip_bytes()` blocks symlinks, special device/FIFO entries, `..`, backslashes, absolute/drive paths, NUL bytes, depth `> 16`, uncompressed payloads `> 50 MiB`, and unsafe `frontmatter.name` identifiers in agent markdown files. All path joins pass through `ensure_contained_path()`.

---

## 5. Direct Platform Agent Consumption (`platform_agent/`)

Headless Platform Agents on **Vertex AI Agent Engine** or **Cloud Run** bypass the IDE Middleware and talk directly to `agentregistry.googleapis.com/v1` using least-privilege runtime IAM:
- **Required:** `roles/agentregistry.viewer`, `roles/agentidentity.user` (for `bindings:fetchAvailable`), `roles/iap.egressor` (when routing via Agent Gateway + IAP v2 UAP).
- **Forbidden:** Never grant `roles/agentregistry.editor` or `roles/agentregistry.admin` to runtime agents.

### Environment Variables & Precedence (`adk_orchestrator.py` & `rest_consumer.py`)

| Setting | Env Var Precedence (Highest → Lowest) | Default |
|---|---|---|
| Project ID | `GCP_PROJECT_ID` → `GOOGLE_CLOUD_PROJECT` | `acme-agent-governance` |
| Location (Services) | `GCP_LOCATION` → `GOOGLE_CLOUD_LOCATION` | `asia-southeast3` |
| Skills Location | `--skills-location` → `AGENT_REGISTRY_SKILLS_LOCATION` (auto-defaults to `global` on `googleapis.com` when `GCP_LOCATION` is regional) | `global` (live) / `asia-southeast3` (local mock) |
| Private-Only Skill Filter | `only_private_skills=True` (default in `OrchestratorConfig` / `PlatformAgentConsumer`) | `true` (filters out built-in Google 1P skills) |
| Registry Base URL | `AGENT_REGISTRY_BASE_URL` (also temporarily sets `AGENT_REGISTRY_ENDPOINT` during `GCPSkillRegistry(...)` init) | `https://agentregistry.googleapis.com/v1` |
| Mode | `--mode` CLI flag → `PLATFORM_AGENT_MODE` (`adk` \| `rest`) | `rest` |
| Model | `PLATFORM_AGENT_MODEL` | `gemini-2.5-flash` |
| Access Token | `AGENT_REGISTRY_ACCESS_TOKEN` → `AGENT_REGISTRY_AUTH_TOKEN` → `GOOGLE_OAUTH_ACCESS_TOKEN` → GCE metadata server → `gcloud` ADC | *(auto)* |
| Source Agent URN | `--source-agent-urn` → `PLATFORM_AGENT_SOURCE_URN` | `None` |
| OAuth Continue URI | `OAUTH_CONTINUE_URI` (requires `google-adk>=2.8.0` when non-empty) | `None` |
| KMS Verification | `REQUIRE_KMS_SIGNATURE` (`true`/`1`/`yes`), `KMS_PUBLIC_KEY_PEM_PATH` | `false`, `None` |

### CLI Usage (`python -m platform_agent.adk_orchestrator`)
```bash
.venv/bin/python -m platform_agent.adk_orchestrator \
  [--mode rest|adk] [--project-id ID] [--location LOC] [--skills-location LOC] [--base-url URL] \
  [--skill-query Q] [--mcp-query Q] [--agent-query Q] \
  [--source-agent-urn URN] [--no-download-skills] [--print-config]
```

### Production Google ADK Snippet (`google-adk[a2a,agent-identity,mcp]>=1.29.0`)

Install dependencies first via `pip install -r platform_agent/requirements.txt`:

```python
import os
from google.adk.agents.llm_agent import LlmAgent
from google.adk.auth.credential_manager import CredentialManager
from google.adk.integrations.agent_identity import GcpAuthProvider
from google.adk.integrations.agent_registry import AgentRegistry
from google.adk.integrations.skill_registry import GCPSkillRegistry
from google.adk.tools.skill_toolset import SkillToolset

# 1. Register GcpAuthProvider so ADK auto-resolves Agent Registry Auth Manager Bindings
CredentialManager.register_auth_provider(GcpAuthProvider())

project_id = os.environ["GCP_PROJECT_ID"]   # Catalog project (skills are strictly project-scoped!)
location = os.environ.get("GCP_LOCATION", "asia-southeast3")  # Regional Services (Bangkok / Singapore)
skills_location = os.environ.get("AGENT_REGISTRY_SKILLS_LOCATION", "global")  # Standalone Skills (global / us / eu)

# 2. Discover & materialize governed regional MCP Toolsets and Remote A2A sub-agents
registry = AgentRegistry(project_id=project_id, location=location)
mcp_names = [s["name"] for s in registry.search_mcp_servers(search_string="finops").get("mcpServers", [])]
mcp_toolsets = [registry.get_mcp_toolset(name) for name in mcp_names]

agent_names = [a["name"] for a in registry.search_agents(search_string="finops").get("agents", [])]
remote_agents = [registry.get_remote_a2a_agent(name) for name in agent_names]

# 3. Dynamic semantic skill discovery & verified ?alt=media ZIP loading (global / us / eu)
skill_registry = GCPSkillRegistry(project_id=project_id, location=skills_location)
skill_toolset = SkillToolset(registry=skill_registry)

# 4. Assemble root LlmAgent (or use platform_agent.adk_orchestrator.build_live_adk_agent)
root_agent = LlmAgent(
    name="enterprise_platform_orchestrator",
    model="gemini-2.5-flash",
    instruction=(
        "Discover approved skills via SkillToolset before executing specialized runbooks, "
        "invoke governed MCP tools, and delegate domain tasks to registered A2A sub-agents."
    ),
    tools=[skill_toolset, *mcp_toolsets],
    sub_agents=remote_agents,
)
```

### `PlatformAgentConsumer` REST `v1` Contract (`platform_agent/rest_consumer.py`)
- **Search asymmetry:** `skills:search` accepts `searchType: "SEMANTIC" | "KEYWORD"` and `filter: "state=ACTIVE AND defaultRevisionFindingCount=0"`. `mcpServers:search` and `agents:search` accept `{"searchString": "..."}` only (never `searchType`).
- **2-Step Skill Fetch (`fetch_and_extract_skill`):** `GET .../locations/{skills_loc}/skills/{private-id}` (filtering strictly to `private-*` when `only_private_skills=True`) → checks `SERVABLE_SKILL_STATES` & `defaultRevision` → `GET .../{defaultRevision}` → enforces `findings == []` & `scanState` in `SUCCEEDED`/`DISABLED` → `GET .../{defaultRevision}?alt=media` (`follow_redirects=False` loop preserving `Authorization` strictly on live same-origin `/download/...` redirects without signed-URL tokens while stripping `Authorization` and all `x-goog-*` headers on cross-origin and signed-URL `/scotty-signed/` / `X-Goog-Signature` hops) → verifies `sha256Hash` → enforces `agentskills.io` ZIP limits (`<= 1 MiB` compressed, `<= 10 MiB` total uncompressed, `<= 1 MiB`/file, depth `<= 8`, strictly one uppercase `SKILL.md`, zero symlinks) → optionally verifies Cloud KMS `EC_SIGN_P256_SHA256` signature (supports raw SHA-256 digest, hex digest, `agent-registry-skill-v1` canonical ZIP digest, and structured canonical digest; fails closed with `SkillSignatureError` if enabled without `KMS_PUBLIC_KEY_PEM_PATH`).
- **Binding Resolution (`resolve_auth_binding`):** Calls `GET .../bindings:fetchAvailable?sourceIdentifier=...&targetIdentifier=...`, prefers bindings scoped explicitly to `source_identifier` over wildcard bindings, and selects the entry with the latest RFC-3339 `updateTime`.
