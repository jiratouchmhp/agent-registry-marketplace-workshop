# Antigravity Pre-Publish Checklist & CI Validation Reference

Deep reference for all **7 Antigravity Pre-Publish checks**, the exact `v1/plugin.json` JSON Schema contract, `hooks.json` syntax, the official `agy plugin validate --json` multi-tool `jq` CI filter, and Python verification recipes (`schemas/validators.py` + `tools/plugin_sync/sync.py`).

---

## 1. All 7 Antigravity Pre-Publish Checks

Every check below is enforced programmatically by `schemas.validators.validate_antigravity_bundle(plugin_dir)` and tested in `tests/test_schemas.py`:

| Check # | Domain | Exact Enforcement Rules | Common Failure & Fix |
| :--- | :--- | :--- | :--- |
| **1** | **Manifest Schema & Naming** | `.antigravity-plugin/plugin.json` (preferred in multi-tool repos) or root `plugin.json` must pass `schemas/antigravity_plugin_v1.schema.json` (`additionalProperties: false`). `name` must match `^[a-z0-9]+(-[a-z0-9]+)*$` (<= 64 chars). Neither `name` nor `displayName`/`display_name` may start with `antigravity`, `gemini`, `claude`, or `cursor` (case-insensitive). Deprecated keys `source`, `sourcePath`, `source_path` are rejected. `author.url` (when present) must start with `https://`. `suggestedPrompts` allows max 3 non-blank strings. | Adding `"category"` or `"mcpServers"` to `.antigravity-plugin/plugin.json` triggers an `additionalProperties` error; keep Codex `category` inside root `plugin.json` `extensions["com.openai"]` and MCP servers in `mcp_config.json`. |
| **2** | **Skills Parity & Archive Caps** | Every directory under `skills/` (and any extra path listed in manifest `skills`) must contain strictly uppercase `SKILL.md` (never `skill.md`) with frontmatter `name == <folder-name>` (`^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`, no `--`), `description` 1-1024 chars, and `metadata` strictly `map<string, string>`. When packaged via `tools.plugin_sync.sync.build_skill_zip`, ZIP must be `<= 1 MB` compressed (`<= 500 KB` inline target), `<= 10 MB` uncompressed total, `<= 1 MB` per file, depth `<= 8`, zero symlinks, zero `..`/absolute paths. | Putting a list (`tags: [a, b]`) inside `frontmatter.metadata` fails `validate_skill_md`; flatten to a comma-separated string (`tags: "a,b"`). |
| **3** | **Rules & Comma-Separated `globs`** | Place always-on instructions in `rules/AGENTS.md` (root `AGENTS.md` emits a warning because Antigravity ignores it). For `rules/*.md` files with YAML frontmatter: `trigger` must be one of `always_on`, `model_decision`, `glob`, `manual`. When `globs` is present (mandatory if `trigger: glob`), it MUST be a non-empty comma-separated **string** (e.g., `"**/*.sql, **/*.tf"`), NEVER a YAML list. | Writing `globs:\n  - "**/*.sql"` fails validation; rewrite as `globs: "**/*.sql, **/*.tf"`. |
| **4** | **`mcp_config.json` Rules** | Antigravity loads ONLY `mcp_config.json` (having `mcp.json`/`.mcp.json` without `mcp_config.json` emits a warning). Top-level `variables` is forbidden (declare inside `mcpServers.<server>.variables` as a list of `{name: "..."}` objects — or omit `variables[]` entirely with `oauth`/`authProviderType: "google_credentials"` so the plugin installs **enabled immediately**). Server keys must match `^[a-zA-Z0-9_-]{1,64}$` (no dots; `> 20` chars emits a warning for tool prefixing). Each server must define `url`, `serverUrl`, or `command`. If `authProviderType` is set, it must be `"google_credentials"`. `env` must NEVER declare reserved `PLUGIN_ROOT` or `PLUGIN_DATA`. | Using dotted key `acme.finops.mcp` or declaring `"PLUGIN_ROOT": "."` in `env` fails validation; sanitize keys to `finops-mcp-server` (`<= 20` chars) and let Antigravity inject `PLUGIN_ROOT`/`PLUGIN_DATA` automatically. |
| **5** | **`hooks.json` Structure & Paths** | Root must be an object of one or more **named hook groups** (never bare event names like `PreToolUse`, `Stop`, `PreInvocation`, `PostInvocation`, `SessionStart`, `hooks` at top level). `PreToolUse`/`PostToolUse` require `[{"matcher": "...", "hooks": [...]}]`; `Stop`/`SessionStart`/`PreInvocation`/`PostInvocation` require flat handler lists `[{"type": "command"\|"prompt", ...}]`. Command hooks MUST use `./scripts/...` relative paths that exist inside the plugin (never `${PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}`, bare `scripts/`, or `/abs/path`). | Wrapping `Stop` in `{"matcher": ".*", "hooks": [...]}` or writing `"${PLUGIN_ROOT}/scripts/guard.sh"` fails validation; use flat array + `"./scripts/guard.sh"`. |
| **6** | **Local 1:1 Square Logo** | `logo` must be a local relative path inside the plugin (e.g., `"assets/logo.svg"` or `"assets/logo.png"`, never an external `http(s)://` URL in `validate_antigravity_bundle`). File must exist, be non-empty, valid XML (SVG) or valid PNG header, and have a strict **1:1 square aspect ratio** (`viewBox` width == height for SVG; IHDR width == height for PNG). | Providing a `120x60` SVG `viewBox` or `64x32` PNG fails validation; redraw/export with square dimensions (e.g., `128x128`). |
| **7** | **Executable `0755` Scripts** | Every script file under `scripts/` (excluding `.md`, `.txt`, `.json`, `.yaml`, `.yml`, `.pyc`, dotfiles, and `__pycache__`) must have Unix mode bits satisfying `(mode & 0o755) == 0o755`. | Committing a shell/Python hook script with `0644` fails validation; run `chmod 0755 scripts/*.sh` and commit the executable bit (`git update-index --chmod=+x`). |

---

## 2. Exact `https://antigravity.google/schemas/v1/plugin.json` Allowed Properties

Verified verbatim against `schemas/antigravity_plugin_v1.schema.json` (`Draft 2020-12`, `required: ["name"]`, `additionalProperties: false`):

| Property | Type / Constraint | Notes |
| :--- | :--- | :--- |
| `$schema` | `string` | Set to `"https://antigravity.google/schemas/v1/plugin.json"`. |
| `name` *(required)* | `string` (`^[a-zA-Z0-9_-]+$` in JSON schema; **policy enforces lowercase kebab-case `^[a-z0-9]+(-[a-z0-9]+)*$`, `<= 64` chars, no brand prefixes**) | Must match root `plugin.json` `name`. |
| `displayName` | `string` | Human-readable label shown in Antigravity UI; no brand prefixes. |
| `display_name` | `string` | Alias of `displayName` (`displayName` wins if both set). Prefer `displayName`. |
| `description` | `string` | Short plugin summary shown in the plugin list. |
| `version` | `string` | Recommended SemVer (e.g., `"1.0.0"`; missing triggers a warning). |
| `disabled` | `boolean` (default `false`) | Whether plugin starts disabled. Omit or `false` for active installs. |
| `logo` | `string` (`.png`, `.svg`, `.jpg`, `.jpeg`, `.webp` relative path) | `validate_antigravity_bundle` requires a local relative 1:1 square file inside the plugin (e.g., `"assets/logo.svg"`). |
| `suggestedPrompts` | `array<string>` (`pattern: \S`, `maxItems: 3`) | Up to 3 starter prompts shown in Antigravity. |
| `suggested_prompts` | `array<string>` (`pattern: \S`, `maxItems: 3`) | Alias used only when `suggestedPrompts` is empty. Prefer `suggestedPrompts`. |
| `skills` | `string` or `array<string>` (`pattern: \S`) | Extra relative skill directories loaded in addition to `skills/`. |
| `author` | `string` or `{name, email, url}` (`additionalProperties: false`) | When object includes `url`, must start with `https://`. |
| `homepage` | `string` | Documentation or landing page URL. |
| `repository` | `string` | Source repository URL. |
| `license` | `string` | SPDX identifier, e.g., `"Apache-2.0"`. |
| `keywords` | `array<string>` | Search keywords. |
| `extensions` | `object` (`additionalProperties: {type: "object"}`) | Reverse-domain client-specific settings map. |
| `source`, `sourcePath`, `source_path` | **DEPRECATED — DO NOT SET** | Present in upstream schema as deprecated legacy keys; **explicitly rejected by `validate_antigravity_bundle`**. |

---

## 3. Exact `hooks.json` Contract & Script Exit Protocol

### Valid Complete `hooks.json` Structure

- **Top-level keys:** Arbitrary named hook group IDs (e.g., `"policy-guard"`). Bare event names (`PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `Notification`, `SessionStart`, `PreInvocation`, `PostInvocation`, `UserPromptSubmit`, `hooks`) at top level are **rejected**.
- **Group metadata:** Optional boolean `enabled` and string `description`.
- **Matcher-wrapped events (`PreToolUse`, `PostToolUse`):** Array of `{"matcher": "<regex>", "hooks": [ <handler>, ... ]}`.
- **Flat-array events (`Stop`, `SessionStart`, `PreInvocation`, `PostInvocation`):** Array of `<handler>` objects directly (**no** `matcher` or nested `hooks` wrapper).
- **Handler object:**
  - `type`: `"command"` (with non-empty `command`, NO `prompt`) OR `"prompt"` (with non-empty `prompt`, NO `command`).
  - `timeout` *(optional)*: positive integer > 0 (booleans rejected).
  - `command` paths: relative `./scripts/...` ONLY; no `${PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}`, bare `scripts/`, or `/abs/path`; target file must exist inside plugin root and have mode `0755`.

```json
{
  "policy-guard": {
    "enabled": true,
    "description": "Block unapproved destructive MCP tools and audit session completion.",
    "PreToolUse": [
      {
        "matcher": "mcp_finops-mcp-server_delete_.*",
        "hooks": [
          {
            "type": "command",
            "command": "./scripts/finops-guard.sh",
            "timeout": 10
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "mcp_finops-mcp-server_.*",
        "hooks": [
          {
            "type": "prompt",
            "prompt": "Verify output cites project IDs and never exposes raw credentials."
          }
        ]
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "./scripts/finops-guard.sh --audit",
        "timeout": 15
      }
    ],
    "SessionStart": [
      {
        "type": "prompt",
        "prompt": "Prefer read-only diagnostics before proposing infrastructure mutations."
      }
    ]
  }
}
```

### Hook Script Exit Code & JSON Stdout Contract (`scripts/*.sh`, mode `0755`)

- Exit `0` to allow execution; when returning structured decision/context feedback to the host runtime, write valid JSON to `stdout` (keep logs/diagnostics off `stdout` or route them to `stderr`).
- Exit non-zero (for example, `exit 2` with a clear explanation on `stderr`, matching `starter-marketplace/plugins/gcp-finops-toolkit/scripts/finops-guard.sh`) to block a disallowed tool invocation:

```bash
#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--audit" ]]; then
  printf '{"status":"ok","event":"audit"}\n'
  exit 0
fi

if [[ "${FINOPS_APPROVE_DESTRUCTIVE:-false}" != "true" ]]; then
  echo "Blocked destructive tool call: set FINOPS_APPROVE_DESTRUCTIVE=true after human review." >&2
  exit 2
fi

printf '{"decision":"allow"}\n'
exit 0
```

---

## 4. Official `agy plugin validate --json` Multi-Tool `jq` Filter

In a 5-IDE polyglot bundle, `agy plugin validate --json` intentionally warns about sibling-IDE files (`.mcp.json`, `mcp.json`, root `AGENTS.md`) and that `.antigravity-plugin/plugin.json` takes precedence over root `plugin.json`. The filter below fails CI on **any error** and **any unexpected warning**.

### Local Workstation Verification (Single Plugin)

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
if [[ "${agy_rc}" -ne 0 ]]; then
  echo "agy plugin validate exited with status ${agy_rc}" >&2
  exit "${agy_rc}"
fi
```

### Production GitLab CI Stage 1 Gate (Verified against `starter-marketplace/.gitlab-ci.yml`)

```yaml
validate:antigravity-cli:
  stage: validate
  script:
    - |
      set -euo pipefail
      if [[ -n "${PLUGINS_DIR:-}" ]]; then
        RESOLVED_PLUGINS_DIR="${PLUGINS_DIR}"
      elif [[ -d "plugins" ]]; then
        RESOLVED_PLUGINS_DIR="plugins"
      elif [[ -d "starter-marketplace/plugins" ]]; then
        RESOLVED_PLUGINS_DIR="starter-marketplace/plugins"
      else
        echo "ERROR: Set PLUGINS_DIR explicitly." >&2
        exit 1
      fi
      command -v agy >/dev/null 2>&1 || { echo "agy CLI not found on runner" >&2; exit 1; }
      for plugin_dir in "${RESOLVED_PLUGINS_DIR}"/*/; do
        agy_rc=0
        report="$(agy plugin validate --json "${plugin_dir}")" || agy_rc=$?
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
      done
```

---

## 5. Complete Python Verification Recipes (`schemas.validators` + `tools.plugin_sync`)

### Recipe A: Validate All Plugins Under a Directory (Monorepo or Standalone Repo)

```bash
PLUGINS_DIR="${PLUGINS_DIR:-starter-marketplace/plugins}" \
.venv/bin/python - << 'EOF'
import os, sys
from pathlib import Path
from schemas.validators import (
    validate_agent_plugin_bundle,
    validate_antigravity_bundle,
    validate_claude_plugin_bundle,
    validate_copilot_plugin_bundle,
    validate_codex_plugin_bundle,
)
from tools.plugin_sync.sync import build_sync_plan

plugins_root = Path(os.environ["PLUGINS_DIR"])
failed = False
for plugin_dir in sorted(p for p in plugins_root.iterdir() if p.is_dir()):
    for name, res in [
        ("agent-plugins-1.0", validate_agent_plugin_bundle(plugin_dir)),
        ("antigravity", validate_antigravity_bundle(plugin_dir)),
        ("claude-code", validate_claude_plugin_bundle(plugin_dir)),
        ("copilot", validate_copilot_plugin_bundle(plugin_dir)),
        ("codex", validate_codex_plugin_bundle(plugin_dir)),
    ]:
        if not res.valid:
            failed = True
            print(f"[FAIL] {plugin_dir} ({name}): {res.errors}")
        else:
            print(f"[PASS] {plugin_dir} ({name}) warnings={len(res.warnings)}")
    # Also verify skill ZIP caps (<=1 MB) and A2A agent-card.json (<=10 KB) offline:
    plan = build_sync_plan(
        plugin_dir,
        project="validation-check",
        location="asia-southeast3",
        probe_mcp=False,
    )
    print(
        f"[PLAN] {plugin_dir.name}: skills={len(plan.skills)} "
        f"mcp={len(plan.mcp_services)} a2a={len(plan.agent_services)}"
    )
sys.exit(1 if failed else 0)
EOF
```

### Recipe B: Validate Standalone Skills (`local-skills/` or Customer `skills/`)

```bash
SKILLS_DIR="${SKILLS_DIR:-local-skills}" \
.venv/bin/python - << 'EOF'
import os, sys
from pathlib import Path
from schemas.validators import validate_skill_md
from tools.plugin_sync.sync import build_skill_zip

skills_root = Path(os.environ["SKILLS_DIR"])
failed = False
for skill_dir in sorted(p for p in skills_root.iterdir() if p.is_dir()):
    res = validate_skill_md(skill_dir / "SKILL.md", expected_name=skill_dir.name)
    if not res.valid:
        failed = True
        print(f"[FAIL] {skill_dir.name}: {res.errors}")
        continue
    pkg = build_skill_zip(skill_dir, {"plugin_name": "standalone-check", "plugin_version": "1.0.0"})
    print(f"[PASS] {skill_dir.name}: zip_bytes={len(pkg.zip_bytes)} warnings={pkg.warnings}")
sys.exit(1 if failed else 0)
EOF
```
