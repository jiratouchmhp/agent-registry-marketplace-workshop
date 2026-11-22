# Five-IDE Schemas & Multi-Tool Coexistence Reference

Use this guide when authoring or auditing plugin manifests so a single repository bundle validates simultaneously across **Agent Plugins 1.0**, **Google Antigravity**, **Claude Code**, **VS Code / GitHub Copilot**, and **OpenAI Codex**.

---

## 1. Side-by-Side 5-Target Compatibility Matrix

| Capability | Agent Plugins 1.0 (`agent-plugins.org`) | Google Antigravity (`antigravity.google`) | Claude Code (`code.claude.com`) | VS Code & GitHub Copilot | OpenAI Codex |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Plugin Manifest** | Root `plugin.json` (`$schema: "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"`, `additionalProperties: false`) | `.antigravity-plugin/plugin.json` (takes precedence over root `plugin.json`; `$schema: "https://antigravity.google/schemas/v1/plugin.json"`, `additionalProperties: false`) | `.claude-plugin/plugin.json` (`name`, `version`, `description`, `author`) | Root `plugin.json` (Agent Plugins 1.0 takes priority #1) | Root `plugin.json` (`extensions["com.openai"]`) + `.codex-plugin/plugin.json` (`name` must match root `plugin.json`) |
| **MCP Config** | Root `mcp.json` (`$schema: "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"`; **forbids `${ENV_VAR}`** in remote `url`/`headers`) | Root `mcp_config.json` (ignores `mcp.json` & `.mcp.json`; prefers `url` + `oauth` or `authProviderType: "google_credentials"`, no root `variables`) | Root `.mcp.json` (`type`: `streamable-http` / `http` / `sse` / `stdio`; **`${ENV_VAR}` allowed** in `headers`) | Root `mcp.json` | Workstation `~/.codex/config.toml` (`[mcp_servers.<key>]` with `url`, `bearer_token_env_var`) |
| **Skills (`agentskills.io`)** | `skills/<name>/SKILL.md` | `skills/<name>/SKILL.md` (+ optional `manifest.skills` relative dirs) | `skills/<name>/SKILL.md` | `skills/<name>/SKILL.md` | `skills/<name>/SKILL.md` |
| **Agents / Subagents** | Optional A2A v1.0 `agents/<slug>/agent-card.json` (<= 10 KB for Agent Registry) | `agents/<name>.md` (YAML frontmatter: `name`, `description`, `mainAgent`, `subagent`, `model`, `commandExecutionPolicy`, `tools`, `inheritMcp`) | `agents/<name>.md` (YAML frontmatter: `name`, `description`, `tools`, `model`) | `com.github.copilot/agents/<name>.agent.md` (MUST end with `.agent.md`; frontmatter `name`, `description`, `tools`) | Workstation `~/.codex/agents/<name>.toml` (`name`, `description`, `instructions`) |
| **Rules / Instructions** | *(client-specific)* | `rules/AGENTS.md` (always-on) & `rules/<rule>.md` (`trigger`, comma-separated string `globs`); **root `AGENTS.md` ignored** | `CLAUDE.md` / plugin skills | Workspace instructions / skills | `AGENTS.md` / skills |
| **Hooks** | *(client-specific)* | Root `hooks.json` (named group, relative `./scripts/...`, `0755` scripts) | Plugin hooks | *(client-specific)* | *(client-specific)* |
| **Branding Icon** | *(optional)* | Required local 1:1 square `logo` (e.g., `assets/logo.svg` or square PNG) + `<= 3` `suggestedPrompts` | *(optional)* | *(optional)* | *(optional)* |
| **Marketplace Catalog** | *(served per IDE)* | `marketplace.json` (`name`, `description`, `plugins[].source.type: "archive"` with `https://...zip` + 64-hex `sha256`, or `"local"`) | `.claude-plugin/marketplace.json` (`owner`, `metadata.pluginRoot`, `plugins[]`; **no `./` relative sources over HTTP**) | Root `marketplace.json` & `.github/plugin/marketplace.json` (`owner`, `metadata.pluginRoot`, `plugins[]`) | `~/.agents/plugins/marketplace.json` (`interface.displayName`, `source.source: "local"`, `policy`, `category`) |
| **Python Validator** | `validate_agent_plugin_bundle` | `validate_antigravity_bundle` | `validate_claude_plugin_bundle` | `validate_copilot_plugin_bundle` | `validate_codex_plugin_bundle` |

---

## 2. Standalone Skill vs. Full 5-IDE Plugin & Auto-Detected Repo Layouts

`tools.plugin_sync ingest` (`discover_ingest_targets` in `tools/plugin_sync/sources.py` and `WORKSHOP_SPEC.json` `skill_sources.git.supported_layouts`) supports 5 repository layouts:

| Layout | Directory Pattern | When to Choose |
| :--- | :--- | :--- |
| **`single_skill`** | `<path>/SKILL.md` | Authoring or ingesting one standalone runbook skill (e.g., `local-skills/cloud-run-debugger`). |
| **`standalone_skills`** | `<path>/skills/<skill>/SKILL.md` or `<path>/<skill>/SKILL.md` | Teams publishing pure Markdown/script skills with zero `plugin.json` boilerplate (e.g., `local-skills/` or customer `skills/`). Middleware synthesizes all 5 IDE manifests automatically on egress. |
| **`single_plugin`** | `<path>/plugin.json` + `skills/` (+ MCP/A2A/rules/hooks) | Dedicated repository for one governed multi-IDE plugin (e.g., `starter-marketplace/plugins/gcp-finops-toolkit`). |
| **`multi_plugin`** | `<path>/plugins/<plugin>/plugin.json` or `<path>/<plugin>/plugin.json` | Central enterprise marketplace monorepo (e.g., `starter-marketplace/` or `starter-marketplace/plugins/`). |
| **`hybrid`** | Mix of plugin folders (`plugins/*/plugin.json` or `*/plugin.json`) and standalone skill folders (`skills/*/SKILL.md` or `*/SKILL.md`) | Enterprise monorepos combining full MCP/A2A toolkits with standalone team runbooks. |

---

## 3. Minimal Valid Manifest Templates (All 5 Targets)

All examples below pass `schemas/validators.py` (Draft 2020-12 JSON Schema + policy checks). Replace `https://mcp.example.com/mcp` and `https://a2a.example.com/a2a` with `http://127.0.0.1:8091/mcp` and `http://127.0.0.1:8092/a2a` for Local Workshop Mode.

### 3.1 Root `plugin.json` (Agent Plugins 1.0 + VS Code / Copilot + Codex Extension)

Validated against `schemas/agent_plugin_v1.schema.json` (`additionalProperties: false`, required: `$schema`, `name`). `name` must match `^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$` (1-64 chars; prefer pure kebab-case without dots so it matches Antigravity).

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "gcp-finops-toolkit",
  "version": "1.0.0",
  "description": "Enterprise GCP FinOps toolkit for billing triage, BigQuery optimization, and governed MCP/A2A tools.",
  "author": {
    "name": "Platform Engineering",
    "email": "platform-security@example.com",
    "url": "https://platform.example.com"
  },
  "homepage": "https://platform.example.com/plugins/gcp-finops-toolkit",
  "repository": "https://gitlab.example.com/platform/agent-plugins",
  "license": "Apache-2.0",
  "keywords": ["gcp", "finops", "bigquery"],
  "extensions": {
    "com.openai": {
      "category": "Cloud Operations"
    },
    "com.acme.agentregistry": {
      "displayName": "GCP FinOps Toolkit",
      "mcpServiceId": "finops-mcp-server",
      "agentServiceId": "finops-advisor-agent",
      "endpointServiceId": "finops-billing-api"
    }
  }
}
```

### 3.2 `.antigravity-plugin/plugin.json` (Google Antigravity v1)

Validated against `schemas/antigravity_plugin_v1.schema.json` (`additionalProperties: false`). Shadows root `plugin.json` in Antigravity.

```json
{
  "$schema": "https://antigravity.google/schemas/v1/plugin.json",
  "name": "gcp-finops-toolkit",
  "displayName": "GCP FinOps Toolkit",
  "description": "Enterprise GCP FinOps toolkit for Cloud Billing triage and BigQuery SQL optimization.",
  "version": "1.0.0",
  "logo": "assets/logo.svg",
  "suggestedPrompts": [
    "Triage yesterday's Cloud Billing cost anomaly across GCP projects",
    "Analyze my BigQuery SQL query to reduce bytes scanned and cost",
    "Summarize the top GCP SKUs driving spend in the current billing period"
  ],
  "author": {
    "name": "Platform Engineering",
    "email": "platform-security@example.com",
    "url": "https://platform.example.com"
  },
  "homepage": "https://platform.example.com/plugins/gcp-finops-toolkit",
  "repository": "https://gitlab.example.com/platform/agent-plugins",
  "license": "Apache-2.0",
  "keywords": ["gcp", "finops", "bigquery"]
}
```

### 3.3 `.claude-plugin/plugin.json` & `.codex-plugin/plugin.json`

Both require a non-empty `name` (and `.codex-plugin/plugin.json` `name` MUST equal root `plugin.json` `name`):

```json
{
  "name": "gcp-finops-toolkit",
  "version": "1.0.0",
  "description": "Enterprise GCP FinOps toolkit for billing triage, BigQuery optimization, and governed MCP tools.",
  "author": {
    "name": "Platform Engineering",
    "email": "platform-security@example.com",
    "url": "https://platform.example.com"
  }
}
```

### 3.4 MCP Configurations (`mcp.json` vs. `.mcp.json` vs. `mcp_config.json`)

**Root `mcp.json` (Agent Plugins 1.0 & VS Code / Copilot)** — validated by `schemas/agent_plugin_mcp_v1.schema.json`.
- `type` must be `"streamable-http"`, `"sse"`, or `"stdio"`.
- Remote `streamable-http` / `sse` entries **MUST NOT** contain `${ENV_VAR}` in `url` or `headers`.
- `stdio` entries accept `command`, `args`, `env` (forbidding `PLUGIN_ROOT` and `PLUGIN_DATA`), and `cwd` (must start with `./`, `${PLUGIN_ROOT}`, or `${PLUGIN_DATA}`).

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "finops-mcp-server": {
      "type": "streamable-http",
      "url": "https://mcp.example.com/mcp"
    }
  }
}
```

**Root `.mcp.json` (Claude Code)** — supports `streamable-http`, `http`, `sse`, or `stdio`, and permits `${ENV_VAR}` header expansion:

```json
{
  "mcpServers": {
    "finops-mcp-server": {
      "type": "streamable-http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${HUB_AUTH_TOKEN}"
      }
    }
  }
}
```

**Root `mcp_config.json` (Google Antigravity)** — Antigravity ignores `mcp.json` and `.mcp.json`. Omit `variables[]` and use `authProviderType: "google_credentials"` (or `oauth`) so the plugin installs **enabled immediately**. Keep server keys `^[a-zA-Z0-9_-]{1,20}$` (no dots):

```json
{
  "mcpServers": {
    "finops-mcp-server": {
      "url": "https://mcp.example.com/mcp",
      "authProviderType": "google_credentials"
    }
  }
}
```

### 3.5 Shared `skills/<skill-name>/SKILL.md` (`agentskills.io`)

- Filename MUST be strictly uppercase `SKILL.md` (exactly one per skill folder).
- `name` MUST equal parent directory name (`^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`, no `--`).
- `metadata` values MUST be scalar strings (`map<string, string>`). Canonical keys from `WORKSHOP_SPEC.json`: `plugin_name`, `plugin_display_name`, `plugin_version`, `source_repo`, `git_commit`, `mcp_service_id`, `agent_service_id`, `kms_signature`, `kms_key_version`.

```yaml
---
name: cost-anomaly-triage
description: Investigate unexpected Google Cloud Billing cost spikes safely using read-only FinOps telemetry and structured checklists.
license: Apache-2.0
compatibility: Requires finops-mcp-server read-only billing telemetry tools.
metadata:
  plugin_name: "gcp-finops-toolkit"
  plugin_display_name: "GCP FinOps Toolkit"
  plugin_version: "1.0.0"
  mcp_service_id: "finops-mcp-server"
  agent_service_id: "finops-advisor-agent"
---

# Cost Anomaly Triage

Follow read-only telemetry checks before proposing any resource mutation.
```

### 3.6 Multi-IDE Subagents & A2A v1.0 Agent Card

**`agents/finops-advisor.md` (Antigravity & Claude Code):**

```yaml
---
name: finops-advisor
description: Read-only GCP FinOps specialist for Cloud Billing anomaly triage and BigQuery SQL optimization.
mainAgent: true
subagent: true
model: inherit
commandExecutionPolicy: sandbox
inheritMcp: true
tools:
  - get_billing_summary
  - analyze_bq_query
---

# FinOps Advisor Subagent

Collect factual cost telemetry first and never invoke destructive tools without explicit approval.
```

**`com.github.copilot/agents/finops-advisor.agent.md` (GitHub Copilot — MUST end in `.agent.md`):**

```yaml
---
name: finops-advisor
description: Read-only GCP FinOps specialist for Cloud Billing anomaly triage and BigQuery SQL optimization.
tools:
  - finops-mcp-server/get_billing_summary
  - finops-mcp-server/analyze_bq_query
---

# FinOps Advisor Custom Agent

Analyze billing anomalies and BigQuery SQL plans using read-only MCP telemetry.
```

**`agents/finops-advisor/agent-card.json` (A2A v1.0 Agent Card — compact JSON `<= 10,240` bytes; MUST omit legacy A2A v0.x `capabilities.stateTransitionHistory` and assign distinct URLs per `supportedInterfaces` entry — e.g. `/a2a` for `JSONRPC` and `/a2a/http` for `HTTP+JSON` — or live Agent Registry rejects with `400 INVALID_ARGUMENT`):**

```json
{
  "name": "GCP FinOps Advisor",
  "description": "Enterprise A2A v1.0 FinOps specialist for billing anomalies and BigQuery optimization.",
  "version": "1.0.0",
  "provider": {
    "organization": "Platform Engineering",
    "url": "https://platform.example.com"
  },
  "supportedInterfaces": [
    {
      "url": "https://a2a.example.com/a2a",
      "protocolBinding": "JSONRPC",
      "protocolVersion": "1.0"
    },
    {
      "url": "https://a2a.example.com/a2a/http",
      "protocolBinding": "HTTP+JSON",
      "protocolVersion": "1.0"
    }
  ],
  "capabilities": {
    "streaming": false,
    "pushNotifications": false
  },
  "defaultInputModes": ["text/plain", "application/json"],
  "defaultOutputModes": ["text/plain", "application/json"],
  "skills": [
    {
      "id": "cost-anomaly-triage",
      "name": "Cost Anomaly Triage",
      "description": "Diagnose unexpected Cloud Billing spend spikes by service, SKU, and project labels.",
      "tags": ["finops", "billing", "gcp"],
      "examples": ["Why did BigQuery spend jump yesterday?"]
    }
  ]
}
```

### 3.7 Antigravity Rules & 1:1 Square SVG Logo

**`rules/AGENTS.md`** (always-on; do NOT rely on root `AGENTS.md`): plain Markdown instructions without frontmatter.

**`rules/finops-guardrails.md`** (`globs` MUST be a comma-separated string, NEVER a YAML list):

```yaml
---
trigger: glob
globs: "**/*.sql, **/*.tf"
---

# FinOps SQL & Terraform Guardrails

- Never use `SELECT *` on partitioned BigQuery tables; require an explicit partition filter.
- Set `require_partition_filter = true` and attribution labels on billable Terraform resources.
```

**`assets/logo.svg`** (1:1 square `viewBox` and `width`/`height`):

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128" role="img" aria-label="Plugin Icon">
  <rect width="128" height="128" rx="24" fill="#1A73E8"/>
  <circle cx="64" cy="64" r="32" fill="#FFFFFF"/>
</svg>
```

---

## 4. Marketplace Catalog Validators Reference (`schemas/validators.py`)

When generating or checking IDE marketplace feeds (usually synthesized by `middleware/` or hydrated by `tools.hub_sync`), verify with these 4 catalog validators (each accepts `path: str | Path | dict[str, Any]`):

| Validator Function | Target IDE | Key Enforced Rules |
| :--- | :--- | :--- |
| `validate_claude_marketplace(path, *, is_remote_http=False)` | Claude Code | Required `name`, `owner.name`, unique `plugins[].name`, valid `source`. When `is_remote_http=True`, relative `./` strings and `local`/`directory` source dicts are **forbidden** (must be `http(s)://` URL or `url`/`git-subdir`/`archive`/`git`/`github`/`npm`/`pip`). |
| `validate_copilot_marketplace(path, *, is_remote_http=False)` | VS Code / Copilot | Same `owner` & `source` rules as Claude, plus optional string `category` and list-of-strings `tags`. |
| `validate_codex_marketplace(path)` | OpenAI Codex | Required `name`, `interface.displayName`, `plugins[].source.source` in `{"local", "url", "archive", "git"}`, `policy.installation` in `{"AVAILABLE", "INSTALLED_BY_DEFAULT", "NOT_AVAILABLE"}`, `policy.authentication` in `{"ON_INSTALL", "ON_USE"}`, non-empty `category`. |
| `validate_antigravity_marketplace(path)` | Google Antigravity | Required `name`, `description`; kebab-case non-branded `plugins[].name`/`displayName`; supports `source.type` in `{"archive", "local", "git", "github"}` (`"archive"` requires `https://` URL ending in `.zip` — `http://` allowed only for `localhost`/`127.0.0.1`/`::1` — and 64-char lowercase hex `sha256`; `"local"` requires non-empty `path`; `"git"`/`"github"` require `url` or `repo`). |
