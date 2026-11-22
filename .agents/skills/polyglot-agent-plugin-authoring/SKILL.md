---
name: polyglot-agent-plugin-authoring
description: >-
  Authors, upgrades, and validates multi-IDE plugins (Agent Plugins 1.0, Google
  Antigravity, Claude Code, VS Code / GitHub Copilot, OpenAI Codex) and
  standalone agentskills.io skills across workshop and production repositories.
  Use when scaffolding or editing plugin.json, .antigravity-plugin/plugin.json,
  .claude-plugin/plugin.json, .codex-plugin/plugin.json, mcp.json, .mcp.json,
  mcp_config.json, SKILL.md, rules/*.md, hooks.json, executable scripts,
  subagent specs, or A2A v1.0 agent-card.json files, fixing Antigravity
  pre-publish failures, or running schema and agy CLI validation. Don't use for
  workshop navigation (agent-marketplace-workshop-guide), Agent Registry v1
  ingestion or lifecycle (agent-registry-v1-ingest-and-lifecycle), middleware
  delivery or hub_sync (marketplace-middleware-and-ide-delivery), or KMS/IAP
  security hardening (agent-registry-security-and-prod-hardening).
---

# Polyglot Agent Plugin & Skill Authoring

Author once and ship cleanly across **Agent Plugins 1.0**, **Google Antigravity**, **Claude Code**, **VS Code / GitHub Copilot**, and **OpenAI Codex**—or author lightweight standalone **`agentskills.io`** skills—across **workshop** (`starter-marketplace/`, `starter/lab01_plugin_skeleton/`, `local-skills/`), **local development**, and **production enterprise repositories**.

## Quick Decision Tree

```text
What are you authoring or validating?
├── Pure instructions, runbooks, or checklists (no custom MCP/A2A/hooks/logo needed)
│   └── Author a standalone agentskills.io skill:
│       - Workshop Option 1: `local-skills/<skill-name>/SKILL.md`
│       - Production repo:   `skills/<skill-name>/SKILL.md`
│       (Polyglot Middleware synthesizes all 5 IDE manifests automatically on egress.)
├── Full plugin bundling Skills + MCP servers + A2A agents + rules + hooks + branding
│   └── Author a 5-IDE Polyglot plugin bundle:
│       - Workshop reference: `starter-marketplace/plugins/gcp-finops-toolkit/`
│       - Lab 01 exercise:    `starter/lab01_plugin_skeleton/`
│       - Production repo:    `plugins/<plugin-name>/`
│       ├── Need side-by-side 5-target schemas & minimal manifest templates?
│       │   └── Read references/five-ide-schemas-and-coexistence.md
│       └── Debugging Antigravity Pre-Publish rules, hooks.json, or agy CI filtering?
│           └── Read references/antigravity-prepublish-and-ci-validation.md
└── Task is outside plugin/skill authoring?
    ├── Workshop progression, Make targets, or Lab 00-08 checkpoints -> agent-marketplace-workshop-guide
    ├── Decomposing/uploading to Agent Registry v1 or GitLab CI sync -> agent-registry-v1-ingest-and-lifecycle
    ├── Middleware runtime, /mcp gateway, hub_sync, or ADK consumers -> marketplace-middleware-and-ide-delivery
    └── KMS CMEK/signing, IAP UAPs, AuthProvider/Bindings, audit logs -> agent-registry-security-and-prod-hardening
```

## Critical Gotchas ("Do This, Not That")

| # | Do This | Not That | Why |
| :--- | :--- | :--- | :--- |
| **1** | Place Antigravity-specific fields in `.antigravity-plugin/plugin.json` (`$schema: "https://antigravity.google/schemas/v1/plugin.json"`, `additionalProperties: false`), use lowercase kebab-case `name` without brand prefixes (`antigravity`, `gemini`, `claude`, `cursor` on `name`/`displayName`), local 1:1 square `logo` (`assets/logo.svg`), `https://` `author.url`, and `<= 3` `suggestedPrompts`. | Put Antigravity keys (`logo`, `displayName`, `suggestedPrompts`) in root `plugin.json`, add unknown/deprecated keys (`source`, `sourcePath`, `source_path`), or use remote/rectangular logos. | `.antigravity-plugin/plugin.json` shadows root `plugin.json` in Antigravity so root `plugin.json` stays pure Agent Plugins 1.0 (`additionalProperties: false`); Antigravity rejects unknown/deprecated keys, branded prefixes, >3 prompts, and non-square or remote logos. |
| **2** | Write `mcp_config.json` for Antigravity alongside `mcp.json` / `.mcp.json`; use `url` + `oauth` or `authProviderType: "google_credentials"` without `variables[]`; sanitize server keys (`^[a-zA-Z0-9_-]{1,20}$`, no dots); never declare `PLUGIN_ROOT` or `PLUGIN_DATA` in `env`. | Expect Antigravity to load `mcp.json`/`.mcp.json`, put `variables` at JSON root, use dotted server keys (`finops.mcp.server`), or set reserved env keys. | Antigravity ignores `mcp.json` and `.mcp.json`; omitting `variables[]` lets plugins install **enabled immediately**; root `variables` and reserved env keys fail validation; dots/long keys break Antigravity's `mcp_<server>_<tool>` prefixing. |
| **3** | Keep remote `streamable-http` / `sse` `url` and `headers` in root `mcp.json` free of `${ENV_VAR}` placeholders (isolate `${HUB_AUTH_TOKEN}` to Claude's `.mcp.json` if needed). | Put `"Authorization": "Bearer ${TOKEN}"` or `"https://${HOST}/mcp"` in Agent Plugins 1.0 `mcp.json`. | Agent Plugins 1.0 (`schemas/agent_plugin_mcp_v1.schema.json` + `validate_agent_plugin_bundle`) forbids `${ENV_VAR}` expansion in remote `url`/`headers` to prevent static token leakage; use OAuth, Google Credentials, or Agent Registry `Binding`. |
| **4** | Name every skill manifest strictly uppercase `SKILL.md` with frontmatter `name == <parent-dir>` (`^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`, no `--`), `description` 1-1024 chars, scalar `map<string, string>` `metadata`, compressed ZIP `<= 1 MB` (`<= 500 KB` inline target), uncompressed `<= 10 MB` (`<= 1 MB`/file), depth `<= 8`, zero symlinks. | Use lowercase `skill.md`, nested duplicate `SKILL.md`, mismatched folder/frontmatter names, YAML lists/objects inside `metadata`, or symlinks. | Both Antigravity and Agent Registry `v1` (`tools.plugin_sync.sync.validate_skill_zip`) hard-reject case/name mismatches, symlinks, oversize ZIPs, and non-string metadata (Agent Registry `v1` has no generic `labels` map, so provenance relies on string `metadata`). |
| **5** | Put always-on rules in `rules/AGENTS.md` and scoped rules in `rules/<name>.md` using `trigger: always_on\|model_decision\|glob\|manual` with `globs` as a **comma-separated string** (`globs: "**/*.sql, **/*.tf"`). | Rely on root `AGENTS.md` for Antigravity or write `globs: ["**/*.sql", "**/*.tf"]` as a YAML list. | Antigravity ignores root `AGENTS.md`, and a YAML array for `globs` fails both `validate_antigravity_bundle` and Antigravity rule parsing. |
| **6** | Wrap `hooks.json` in a named top-level group (`"policy-guard": {"enabled": true, ...}`), use `matcher`+`hooks` wrappers ONLY for `PreToolUse`/`PostToolUse` and flat handler arrays for `Stop`/`SessionStart`/`PreInvocation`/`PostInvocation`, invoke `./scripts/...` relative paths ONLY, `chmod 0755 scripts/*`, and return exit `0` + JSON stdout on allow/structured decisions (or non-zero + stderr message, e.g. exit `2`, to block). | Put bare `PreToolUse`/`Stop`/`hooks` at top level, wrap `Stop` in `matcher`/`hooks`, use `${PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_ROOT}` / bare `scripts/`, or commit `0644` scripts. | Antigravity executes hooks relative to plugin root without env-var expansion, rejects bare event roots and mismatched event wrappers, and fails to run non-executable scripts (`0644`). |
| **7** | Author A2A v1.0 cards at `agents/<slug>/agent-card.json` `<= 10 KB` (`10,240` bytes compact JSON) with `name`, non-empty `supportedInterfaces` using **unique URLs per binding** (`/a2a` for `JSONRPC`, `/a2a/http` for `HTTP+JSON`), A2A v1.0 `capabilities` (`streaming`, `pushNotifications` only), `defaultInputModes`, `defaultOutputModes`, and `skills[]`. | Exceed 10 KB, include legacy A2A v0.x `capabilities.stateTransitionHistory`, assign duplicate URLs across `supportedInterfaces`, or duplicate endpoints in top-level Service `interfaces` when registering `A2A_AGENT_CARD`. | Live Agent Registry `v1` caps `agentSpec.content` at 10 KB and returns `400 INVALID_ARGUMENT` on non-empty top-level `interfaces`, unknown field `stateTransitionHistory`, or `duplicate interface url found in agent card` (enforced locally in `mock_registry` and `tools.plugin_sync`). |

## Quick-Start Patterns

### 1. Scaffold & Validate a Standalone `agentskills.io` Skill

- **Local Workshop Mode:** create under `local-skills/<skill-name>/` (see bundled `cloud-run-debugger`, `terraform-iam-auditor`, `incident-postmortem-writer`).
- **Live GCP / Production Mode:** create under `skills/<skill-name>/` in your team's Git repository (ingested via `tools.plugin_sync ingest --source-type git`).

```bash
# 1) When creating a new skill, scaffold a kebab-case folder with strictly uppercase SKILL.md:
#    mkdir -p "local-skills/<skill-name>/references"   # Production: "skills/<skill-name>/references"
#    (Ensure frontmatter `name` matches folder basename and `metadata` values are scalar strings.)

# 2) Validate frontmatter + deterministic Agent Registry ZIP caps (<= 1 MB, zero symlinks):
SKILL_DIR="${SKILL_DIR:-local-skills/cloud-run-debugger}" \
.venv/bin/python - << 'EOF'
import os
from pathlib import Path
from schemas.validators import validate_skill_md
from tools.plugin_sync.sync import build_skill_zip

skill_dir = Path(os.environ["SKILL_DIR"])
res = validate_skill_md(skill_dir / "SKILL.md", expected_name=skill_dir.name)
assert res.valid, res.errors
pkg = build_skill_zip(skill_dir, {"plugin_name": "local-ops-toolkit", "plugin_version": "1.0.0"})
print(f"OK: {skill_dir.name} zip_bytes={len(pkg.zip_bytes)} sha256={pkg.sha256_hex[:12]}...")
EOF
```

### 2. Scaffold or Upgrade a 5-IDE Polyglot Plugin

- **Local Workshop Mode:** inspect `starter-marketplace/plugins/gcp-finops-toolkit/` (uses local `http://127.0.0.1:8091/mcp` and `http://127.0.0.1:8092/a2a`) or upgrade `starter/lab01_plugin_skeleton/`.
- **Live GCP / Production Mode:** place under `plugins/<plugin-name>/` and replace localhost URLs in `mcp.json`, `.mcp.json`, `mcp_config.json`, and `agents/<slug>/agent-card.json` with HTTPS Cloud Run / Agent Gateway URLs (e.g., `https://<service>.<domain>/mcp`).

Required coexistence layout (full minimal templates in [references/five-ide-schemas-and-coexistence.md](references/five-ide-schemas-and-coexistence.md)):

```text
<plugin-name>/
├── plugin.json                             # Agent Plugins 1.0 + Copilot + Codex extensions["com.openai"]
├── .antigravity-plugin/plugin.json         # Antigravity v1 (shadows root plugin.json)
├── .claude-plugin/plugin.json              # Claude Code manifest
├── .codex-plugin/plugin.json               # OpenAI Codex manifest (`name` must match root plugin.json)
├── mcp.json                                # Agent Plugins 1.0 & Copilot (NO ${ENV_VAR} in url/headers)
├── .mcp.json                               # Claude Code (`${VAR}` allowed in headers)
├── mcp_config.json                         # Antigravity (`authProviderType: "google_credentials"`)
├── assets/logo.svg                         # 1:1 square icon for Antigravity
├── skills/<skill-name>/SKILL.md            # Shared agentskills.io skills (all 5 targets)
├── agents/<agent>.md                       # Antigravity & Claude Code subagent
├── agents/<agent>/agent-card.json          # A2A v1.0 Agent Card (<= 10 KB)
├── com.github.copilot/agents/<agent>.agent.md # GitHub Copilot custom agent
├── rules/AGENTS.md & rules/<rule>.md       # Antigravity rules (comma-separated `globs` string)
├── hooks.json                              # Named top-level group + `./scripts/...` relative paths
└── scripts/<hook>.sh                       # chmod 0755 executable helper scripts
```

### 3. Run All 5 Python Schema Validators (`schemas.validators`)

```bash
# Workshop default: PLUGIN_DIR="starter-marketplace/plugins/gcp-finops-toolkit"
# Production repo:  PLUGIN_DIR="plugins/<your-plugin-name>"
PLUGIN_DIR="${PLUGIN_DIR:-starter-marketplace/plugins/gcp-finops-toolkit}" \
.venv/bin/python - << 'EOF'
import os, sys
from schemas.validators import (
    validate_agent_plugin_bundle,
    validate_antigravity_bundle,
    validate_claude_plugin_bundle,
    validate_copilot_plugin_bundle,
    validate_codex_plugin_bundle,
)

plugin_dir = os.environ["PLUGIN_DIR"]
failed = False
for label, fn in [
    ("Agent Plugins 1.0", validate_agent_plugin_bundle),
    ("Google Antigravity", validate_antigravity_bundle),
    ("Claude Code", validate_claude_plugin_bundle),
    ("VS Code / Copilot", validate_copilot_plugin_bundle),
    ("OpenAI Codex", validate_codex_plugin_bundle),
]:
    res = fn(plugin_dir)
    print(f"{label:20s} valid={res.valid} errors={res.errors} warnings={len(res.warnings)}")
    failed = failed or not res.valid
sys.exit(1 if failed else 0)
EOF
```

### 4. Run Official `agy plugin validate --json` Multi-Tool `jq` CI Filter

Filters out ONLY expected cross-IDE coexistence warnings (`.mcp.json`, `mcp.json`, root `AGENTS.md`, and shadowed root `plugin.json`) while failing on every error or unexpected warning (identical to `starter-marketplace/.gitlab-ci.yml`):

```bash
PLUGIN_DIR="${PLUGIN_DIR:-starter-marketplace/plugins/gcp-finops-toolkit}"
agy_rc=0
report="$(agy plugin validate --json "${PLUGIN_DIR}")" || agy_rc=$?
echo "${report}" | jq .
echo "${report}" | jq -e '
  [
    (.findings // [])[]
    | select(
        .severity == "error"
        or (
          .severity == "warning"
          and (.file != ".mcp.json")
          and (.file != "mcp.json")
          and (.file != "AGENTS.md")
          and ((.file == "plugin.json" and (.message | contains("takes precedence"))) | not)
        )
      )
  ] | length == 0
'
test "${agy_rc}" -eq 0
```

*(If `agy` is not installed locally, run `.venv/bin/pytest -v tests/test_schemas.py` and keep `validate:antigravity-cli` active in CI runners with `agy` + `jq`.)*

## Verification Checklist

Run these checks before opening a PR/MR or moving to Agent Registry ingestion:

1. **All 5 IDE bundle validators pass (`valid=True`, `errors=[]`)** on the target plugin directory (Pattern 3 above).
2. **Standalone `SKILL.md` & ZIP archive checks pass** (`validate_skill_md` + `build_skill_zip` <= 1 MB compressed, 0 symlinks, depth <= 8).
3. **Executable permissions verified:** `ls -l "${PLUGIN_DIR:-starter-marketplace/plugins/gcp-finops-toolkit}/scripts"` shows `-rwxr-xr-x` (`0755`) on every hook/helper script (`validate_antigravity_bundle` enforces `(mode & 0o755) == 0o755` portably across Linux/macOS while excluding `.md`/`.txt`/`.json`/`.yaml`/`.yml`/`.pyc`, dotfiles, and `__pycache__`).
4. **A2A Agent Card verified <= 10 KB (strictly offline):** dry-run decomposition passes without live HTTP MCP probing or spec-size errors:
   ```bash
   .venv/bin/python -m tools.plugin_sync sync \
     --plugin-dir "${PLUGIN_DIR:-starter-marketplace/plugins/gcp-finops-toolkit}" \
     --project "${GCP_PROJECT_ID:-acme-agent-governance}" \
     --location "${GCP_LOCATION:-asia-southeast3}" \
     --skip-mcp-probe --dry-run
   ```
5. **Repo schema regression suite & starter check pass (zero Docker):** `.venv/bin/pytest -v tests/test_schemas.py` and `make starter-check` report 0 failures.
6. **Antigravity CLI CI gate passes (when `agy` is available):** Pattern 4 exits `0` with `true` from `jq -e`.

## Reference Guides

- [Five-IDE Schemas and Coexistence](references/five-ide-schemas-and-coexistence.md) — Side-by-side 5-target compatibility table, standalone vs. plugin layout matrix, marketplace catalog rules, and exact minimal valid JSON/YAML templates for every manifest file.
- [Antigravity Pre-Publish Checklist and CI Validation](references/antigravity-prepublish-and-ci-validation.md) — All 7 Antigravity Pre-Publish checks, exact `v1/plugin.json` allowed properties table, complete `hooks.json` structure & script contract, `agy plugin validate --json` CLI & GitLab CI `jq` filter, and Python verification recipes.
