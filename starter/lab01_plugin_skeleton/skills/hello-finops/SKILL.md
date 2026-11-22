---
name: hello-finops
description: Starter Agent Skill for estimating BigQuery query cost before execution.
license: Apache-2.0
metadata:
  plugin_name: "hello-finops-plugin"
  plugin_display_name: "Hello FinOps Plugin"
  plugin_version: "0.1.0"
  source_repo: "https://github.com/acme/agent-plugins"
  git_commit: "0000000"
  mcp_service_id: "finops-mcp-server"
  agent_service_id: "finops-advisor-agent"
---

# Hello FinOps Skill

Use this starter skill to practice authoring an `agentskills.io`-compliant `SKILL.md`
inside a Polyglot multi-IDE plugin bundle.

<!-- WORKSHOP-TODO(lab-01):
1. Keep frontmatter `name` strictly equal to this folder name (`hello-finops`).
2. Keep every `metadata` value a scalar string (`map<string, string>` required by
   Google Cloud Agent Registry v1 Skill ingestion).
3. Add the remaining Polyglot files in `starter/lab01_plugin_skeleton/`:
   - `.antigravity-plugin/plugin.json` (with `logo: "assets/logo.svg"`, `displayName`,
     up to 3 `suggestedPrompts`, no branded prefixes, `https://` `author.url`)
   - `assets/logo.svg` (1:1 square `viewBox="0 0 64 64"`)
   - `mcp_config.json` (Antigravity MCP config with sanitized server key `finops-mcp-server`,
     `authProviderType: "google_credentials"`, no root `variables`, no `PLUGIN_ROOT` in `env`)
   - `mcp.json` (Agent Plugins 1.0 MCP config with NO `${ENV_VAR}` expansion in remote url/headers)
   - `.claude-plugin/plugin.json` and `.mcp.json` (Claude Code)
   - `.codex-plugin/plugin.json` (OpenAI Codex)
   - `rules/AGENTS.md` and `rules/finops-guardrails.md` (`trigger: glob`, comma-separated `globs: "**/*.sql, **/*.tf"`)
   - `hooks.json` (named top-level group using relative `./scripts/...` paths) + `chmod 0755 scripts/*.sh`
4. Verify your bundle passes `schemas.validate_antigravity_bundle("starter/lab01_plugin_skeleton")`!
-->

## Workflow

1. Inspect the user's SQL query or billing anomaly request.
2. Estimate scanned bytes using a read-only dry-run tool call before running any query.
3. Recommend partitioning or clustering filters whenever estimated bytes exceed 1 GiB.
