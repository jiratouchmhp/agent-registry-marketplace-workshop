# Lab 01: Author a Polyglot Agent Plugin

**Workstream:** A (Core Platform & Middleware)  
**Duration:** 35 minutes  
**Prerequisites:** Completed [Lab 00](00-environment-setup.md).

---

## Files You'll Work With

In this workshop, `starter/` holds the files you edit, while `starter-marketplace/` is the finished reference plugin repo you can peek at anytime.

| Role | File | What You'll Do |
| :--- | :--- | :--- |
| **Edit (Starter Skeleton)** | [starter/lab01_plugin_skeleton/plugin.json](../../starter/lab01_plugin_skeleton/plugin.json)<br>[starter/lab01_plugin_skeleton/skills/hello-finops/SKILL.md](../../starter/lab01_plugin_skeleton/skills/hello-finops/SKILL.md) | Inspect these 2 starter files, then add 6 Antigravity coexistence files inside `starter/lab01_plugin_skeleton/`. |
| **Inspect (Reference Plugin)** | [starter-marketplace/plugins/gcp-finops-toolkit/](../../starter-marketplace/plugins/gcp-finops-toolkit/plugin.json) | Tour a finished 5-IDE plugin (`Agent Plugins 1.0`, Antigravity, Claude Code, VS Code/Copilot, Codex). Peek here if you get stuck. |
| **Inspect (Standalone Skills)** | [local-skills/](../../local-skills/README.md) | Review 3 standalone `SKILL.md` skills (`cloud-run-debugger`, `terraform-iam-auditor`, `incident-postmortem-writer`). |
| **Run** | `make starter-check` ([scripts/check_starter.py](../../scripts/check_starter.py))<br>[schemas/validators.py](../../schemas/validators.py) | Track your Lab 01 checklist (`0/6` -> `6/6`) and validate bundles across all 5 IDE schemas. |

---

## Learning Objectives

By the end of this lab, you will:
1. Read a finished 5-IDE plugin bundle and understand what each manifest file does.
2. Upgrade `starter/lab01_plugin_skeleton/` with all 6 Google Antigravity coexistence files until `make starter-check` hits `6/6`.
3. Know when a standalone `SKILL.md` in `local-skills/` is simpler than authoring a full plugin.
4. Run the multi-tool `agy plugin validate --json` filter used in CI.

---

## Step 1: Tour the Reference Plugin and Run All 5 Validators

Start by looking at the finished reference plugin in `starter-marketplace/plugins/gcp-finops-toolkit/`. One `skills/` folder serves all 5 tools, while thin manifest files satisfy each IDE's schema rules.

Run:

```bash
ls -la starter-marketplace/plugins/gcp-finops-toolkit/
```

Here is what each file controls:

| File / Folder | Target IDE(s) | Key Rule to Watch |
| :--- | :--- | :--- |
| [plugin.json](../../starter-marketplace/plugins/gcp-finops-toolkit/plugin.json) | **Agent Plugins 1.0**, VS Code/Copilot, Codex | Uses `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`. VS Code and Copilot read this file first. |
| [.antigravity-plugin/plugin.json](../../starter-marketplace/plugins/gcp-finops-toolkit/.antigravity-plugin/plugin.json) | **Google Antigravity** | Overrides root `plugin.json`. Uses strict `additionalProperties: false`, bans branded name prefixes (`antigravity`, `gemini`, `claude`, `cursor`), and requires a 1:1 `logo` plus up to 3 `suggestedPrompts`. |
| [.claude-plugin/plugin.json](../../starter-marketplace/plugins/gcp-finops-toolkit/.claude-plugin/plugin.json) & [.mcp.json](../../starter-marketplace/plugins/gcp-finops-toolkit/.mcp.json) | **Claude Code** | Standard Claude plugin manifest and `streamable-http` MCP server entry. |
| [.codex-plugin/plugin.json](../../starter-marketplace/plugins/gcp-finops-toolkit/.codex-plugin/plugin.json) | **OpenAI Codex** | Paired with `extensions["com.openai"]` in root `plugin.json`. |
| [mcp.json](../../starter-marketplace/plugins/gcp-finops-toolkit/mcp.json) | **Agent Plugins 1.0**, VS Code/Copilot | Never put `${ENV_VAR}` placeholders inside remote `url` or `headers`. |
| [mcp_config.json](../../starter-marketplace/plugins/gcp-finops-toolkit/mcp_config.json) | **Google Antigravity** | Antigravity ignores `mcp.json` and `.mcp.json`. Use `authProviderType: "google_credentials"`, a short key (`<= 20` chars, no dots), and no `variables[]` so the plugin installs enabled right away. |
| [skills/cost-anomaly-triage/SKILL.md](../../starter-marketplace/plugins/gcp-finops-toolkit/skills/cost-anomaly-triage/SKILL.md)<br>[skills/bq-query-optimizer/SKILL.md](../../starter-marketplace/plugins/gcp-finops-toolkit/skills/bq-query-optimizer/SKILL.md) | **All 5 targets** (`agentskills.io`) | Filename must be uppercase `SKILL.md`. YAML `name` must match the folder name, and `metadata` accepts flat string key/value pairs only. |
| [rules/AGENTS.md](../../starter-marketplace/plugins/gcp-finops-toolkit/rules/AGENTS.md)<br>[rules/finops-guardrails.md](../../starter-marketplace/plugins/gcp-finops-toolkit/rules/finops-guardrails.md) | **Google Antigravity** | Root `AGENTS.md` is ignored. For `trigger: glob`, write `globs` as a comma-separated string (`"**/*.sql, **/*.tf"`), never a YAML list. |
| [hooks.json](../../starter-marketplace/plugins/gcp-finops-toolkit/hooks.json)<br>[scripts/finops-guard.sh](../../starter-marketplace/plugins/gcp-finops-toolkit/scripts/finops-guard.sh) | **Google Antigravity** | Put hooks inside a named top-level group (`"policy-guard"`). Use relative paths (`./scripts/...`), never `${PLUGIN_ROOT}`, and set mode `0755`. |
| [agents/finops-advisor.md](../../starter-marketplace/plugins/gcp-finops-toolkit/agents/finops-advisor.md)<br>[com.github.copilot/agents/finops-advisor.agent.md](../../starter-marketplace/plugins/gcp-finops-toolkit/com.github.copilot/agents/finops-advisor.agent.md)<br>[agents/finops-advisor/agent-card.json](../../starter-marketplace/plugins/gcp-finops-toolkit/agents/finops-advisor/agent-card.json) | **Antigravity, Claude, Copilot, A2A v1.0** | Native subagent definitions plus an A2A v1.0 card (`<= 10 KB`, no legacy `stateTransitionHistory`, unique URLs `/a2a` and `/a2a/http`). Registered as `finops-advisor-agent` in Lab 02. |

Now run all 5 schema validators from [schemas/validators.py](../../schemas/validators.py) against `gcp-finops-toolkit`:

```bash
.venv/bin/python - << 'EOF'
from schemas.validators import (
    validate_agent_plugin_bundle,
    validate_antigravity_bundle,
    validate_claude_plugin_bundle,
    validate_codex_plugin_bundle,
    validate_copilot_plugin_bundle,
)

plugin_dir = "starter-marketplace/plugins/gcp-finops-toolkit"
for label, fn in [
    ("Agent Plugins 1.0", validate_agent_plugin_bundle),
    ("Google Antigravity", validate_antigravity_bundle),
    ("Claude Code", validate_claude_plugin_bundle),
    ("VS Code / Copilot", validate_copilot_plugin_bundle),
    ("OpenAI Codex", validate_codex_plugin_bundle),
]:
    res = fn(plugin_dir)
    print(f"{label:20s} valid={res.valid} errors={res.errors} warnings={len(res.warnings)}")
EOF
```

You should see `valid=True` and `errors=[]` on all 5 lines.

---

## Step 2: Run `make starter-check` and Inspect the Skeleton (`0/6`)

Open the two files already in `starter/lab01_plugin_skeleton/`:
- [starter/lab01_plugin_skeleton/plugin.json](../../starter/lab01_plugin_skeleton/plugin.json)
- [starter/lab01_plugin_skeleton/skills/hello-finops/SKILL.md](../../starter/lab01_plugin_skeleton/skills/hello-finops/SKILL.md)

Run the starter checker to see your baseline:

```bash
make starter-check
```

Look at the `[Lab 01]` block in the output. You should see `Agent Plugins 1.0` pass, while `Antigravity v1` flags the missing `logo` and the checklist starts at `0/6`:

```text
[Lab 01] starter/lab01_plugin_skeleton
  [PASS] Agent Plugins 1.0 bundle validation passed
  [TODO] Antigravity v1 schema validator items remaining (1):
         - Manifest is missing required local 'logo' (e.g. 'assets/logo.svg', 1:1 square)
  [INFO] Lab 01 Antigravity Coexistence Checklist (0/6 present):
         [TODO] .antigravity-plugin/plugin.json (Antigravity v1 manifest with logo & displayName)
         [TODO] assets/logo.svg (1:1 square local SVG logo)
         [TODO] mcp_config.json (Antigravity MCP config with authProviderType: google_credentials)
         [TODO] rules/AGENTS.md + glob rule (e.g. rules/finops-guardrails.md with comma-separated globs)
         [TODO] hooks.json (named top-level group using relative ./scripts/... paths)
         [TODO] scripts/*.sh with executable permissions (chmod 0755)
```

Here is what `starter/lab01_plugin_skeleton/` looks like now versus after Step 3:

```text
Before (0/6)                                  After (6/6)
starter/lab01_plugin_skeleton/                starter/lab01_plugin_skeleton/
├── plugin.json                               ├── plugin.json
└── skills/                                   ├── .antigravity-plugin/
    └── hello-finops/                         │   └── plugin.json              (3.1)
        └── SKILL.md                          ├── assets/
                                              │   └── logo.svg                 (3.2)
                                              ├── mcp_config.json              (3.3)
                                              ├── rules/
                                              │   ├── AGENTS.md                (3.4)
                                              │   └── finops-guardrails.md     (3.4)
                                              ├── hooks.json                   (3.5)
                                              ├── scripts/
                                              │   └── finops-guard.sh (0755)   (3.6)
                                              └── skills/
                                                  └── hello-finops/
                                                      └── SKILL.md
```

> **Tip (Keeping `git status` clean):** Editing `starter/lab01_plugin_skeleton/` directly lets `make starter-check` grade your work automatically. If you prefer to keep the repo untouched, run `cp -r starter/lab01_plugin_skeleton /tmp/my-finops-plugin`, apply Steps 3.1–3.6 inside `/tmp/my-finops-plugin`, and validate that folder with the Python snippet at the end of Step 3.

---

## Step 3: Upgrade `starter/lab01_plugin_skeleton/` File by File

Work through these 6 files in order. Stuck on a field or format? Compare with the matching file in `starter-marketplace/plugins/gcp-finops-toolkit/`.

### Step 3.1: Create `.antigravity-plugin/plugin.json`

Antigravity enforces `additionalProperties: false` ([schemas/antigravity_plugin_v1.schema.json](../../schemas/antigravity_plugin_v1.schema.json)), so any unrecognized key fails validation. Keep `author.url` as `https://`, avoid branded name prefixes, and point `logo` at `assets/logo.svg`.

Run:

```bash
mkdir -p starter/lab01_plugin_skeleton/.antigravity-plugin
cat << 'EOF' > starter/lab01_plugin_skeleton/.antigravity-plugin/plugin.json
{
  "$schema": "https://antigravity.google/schemas/v1/plugin.json",
  "name": "hello-finops-plugin",
  "displayName": "Hello FinOps Plugin",
  "description": "Starter FinOps plugin for BigQuery cost estimation and safe billing guardrails.",
  "version": "1.0.0",
  "logo": "assets/logo.svg",
  "suggestedPrompts": [
    "Estimate bytes scanned for my BigQuery SQL query",
    "Check my Terraform BigQuery table config for missing partition filters",
    "Summarize top Cloud Billing cost drivers before proposing changes"
  ],
  "author": {
    "name": "Acme Platform Security",
    "email": "platform-security@acme.example.com",
    "url": "https://acme.example.com/platform"
  },
  "license": "Apache-2.0",
  "keywords": [
    "finops",
    "bigquery",
    "workshop"
  ]
}
EOF
```

*Stuck? Peek at [starter-marketplace/plugins/gcp-finops-toolkit/.antigravity-plugin/plugin.json](../../starter-marketplace/plugins/gcp-finops-toolkit/.antigravity-plugin/plugin.json).*

### Step 3.2: Add a 1:1 Square `assets/logo.svg`

Antigravity expects a local square icon at the path declared in `logo`. Copy the reference SVG into your starter skeleton:

```bash
mkdir -p starter/lab01_plugin_skeleton/assets
cp starter-marketplace/plugins/gcp-finops-toolkit/assets/logo.svg \
  starter/lab01_plugin_skeleton/assets/logo.svg
```

### Step 3.3: Create `mcp_config.json`

Antigravity loads `mcp_config.json` and ignores `mcp.json` or `.mcp.json`. Use a sanitized server key (`<= 20` chars, letters/digits/hyphens/underscores only), set `authProviderType: "google_credentials"`, and omit `variables[]` so users don't get stuck with a disabled plugin on install.

Run:

```bash
cat << 'EOF' > starter/lab01_plugin_skeleton/mcp_config.json
{
  "mcpServers": {
    "finops-mcp-server": {
      "url": "http://127.0.0.1:8091/mcp",
      "authProviderType": "google_credentials"
    }
  }
}
EOF
```

*Stuck? Peek at [starter-marketplace/plugins/gcp-finops-toolkit/mcp_config.json](../../starter-marketplace/plugins/gcp-finops-toolkit/mcp_config.json).*

### Step 3.4: Create `rules/AGENTS.md` and `rules/finops-guardrails.md`

Always-on instructions belong in `rules/AGENTS.md`. File-scoped rules use `trigger: glob` with `globs` written as one comma-separated string (`"**/*.sql, **/*.tf"`), never a YAML list.

Run:

```bash
mkdir -p starter/lab01_plugin_skeleton/rules

cat << 'EOF' > starter/lab01_plugin_skeleton/rules/AGENTS.md
# Hello FinOps Always-On Rules

- Estimate BigQuery bytes scanned before executing any query.
- Never invoke destructive billing or dataset tools without explicit user confirmation.
EOF

cat << 'EOF' > starter/lab01_plugin_skeleton/rules/finops-guardrails.md
---
trigger: glob
globs: "**/*.sql, **/*.tf"
---

# FinOps SQL and Terraform Guardrails

- Avoid `SELECT *` on partitioned BigQuery tables; select only needed columns.
- Require partition filters on partitioned tables and set `deletion_protection = true` on production datasets.
EOF
```

*Stuck? Peek at [rules/AGENTS.md](../../starter-marketplace/plugins/gcp-finops-toolkit/rules/AGENTS.md) and [rules/finops-guardrails.md](../../starter-marketplace/plugins/gcp-finops-toolkit/rules/finops-guardrails.md).*

### Step 3.5: Create `hooks.json`

Client hooks run as an extra safety layer before tool calls. Wrap hook events inside a named top-level group (`"policy-guard"`) and use a relative script path (`./scripts/finops-guard.sh`), never `${PLUGIN_ROOT}`.

Run:

```bash
cat << 'EOF' > starter/lab01_plugin_skeleton/hooks.json
{
  "policy-guard": {
    "enabled": true,
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
    ]
  }
}
EOF
```

*Stuck? Peek at [starter-marketplace/plugins/gcp-finops-toolkit/hooks.json](../../starter-marketplace/plugins/gcp-finops-toolkit/hooks.json).*

### Step 3.6: Create `scripts/finops-guard.sh` and Set `chmod 0755`

Create the executable hook script referenced by `hooks.json` and mark it executable (`0755`):

```bash
mkdir -p starter/lab01_plugin_skeleton/scripts
cat << 'EOF' > starter/lab01_plugin_skeleton/scripts/finops-guard.sh
#!/usr/bin/env bash
set -euo pipefail

if [[ "${FINOPS_APPROVE_DESTRUCTIVE:-false}" != "true" ]]; then
  echo "Blocked destructive FinOps tool call: set FINOPS_APPROVE_DESTRUCTIVE=true after review." >&2
  exit 2
fi
EOF
chmod 0755 starter/lab01_plugin_skeleton/scripts/finops-guard.sh
```

*Stuck? Peek at [starter-marketplace/plugins/gcp-finops-toolkit/scripts/finops-guard.sh](../../starter-marketplace/plugins/gcp-finops-toolkit/scripts/finops-guard.sh).*

### Verify Your Upgraded Skeleton (`6/6`)

Run `make starter-check`:

```bash
make starter-check
```

You should see both validators pass and `6/6 present` on the Lab 01 checklist:

```text
[Lab 01] starter/lab01_plugin_skeleton
  [PASS] Agent Plugins 1.0 bundle validation passed
  [PASS] Google Antigravity v1 schema validator passed
  [INFO] Lab 01 Antigravity Coexistence Checklist (6/6 present):
         [PASS] .antigravity-plugin/plugin.json (Antigravity v1 manifest with logo & displayName)
         [PASS] assets/logo.svg (1:1 square local SVG logo)
         [PASS] mcp_config.json (Antigravity MCP config with authProviderType: google_credentials)
         [PASS] rules/AGENTS.md + glob rule (e.g. rules/finops-guardrails.md with comma-separated globs)
         [PASS] hooks.json (named top-level group using relative ./scripts/... paths)
         [PASS] scripts/*.sh with executable permissions (chmod 0755)
```

If you edited a copy in `/tmp/my-finops-plugin` instead, validate it directly:

```bash
.venv/bin/python - << 'EOF'
from schemas import validate_agent_plugin_bundle, validate_antigravity_bundle

target = "/tmp/my-finops-plugin"
for label, fn in [
    ("Agent Plugins 1.0", validate_agent_plugin_bundle),
    ("Antigravity v1   ", validate_antigravity_bundle),
]:
    res = fn(target)
    print(f"{label}: valid={res.valid} errors={res.errors}")
EOF
```

---

## Step 4: Quick Look at Standalone `local-skills/`

You do not always need a full plugin. When you only want a runbook, checklist, or prompt workflow (no MCP servers, A2A agents, rules, or hooks), a single `<skill-name>/SKILL.md` folder is enough.

This workshop includes 3 ready-to-sync standalone skills under [local-skills/](../../local-skills/README.md):

| Standalone Skill | Files | What It Does |
| :--- | :--- | :--- |
| **`cloud-run-debugger`** | [SKILL.md](../../local-skills/cloud-run-debugger/SKILL.md) + [references/triage-runbook.md](../../local-skills/cloud-run-debugger/references/triage-runbook.md) | Read-only triage for Cloud Run startup failures, probe timeouts, IAM invoker denials, and log spikes. |
| **`terraform-iam-auditor`** | [SKILL.md](../../local-skills/terraform-iam-auditor/SKILL.md) + [references/least-privilege-checklist.md](../../local-skills/terraform-iam-auditor/references/least-privilege-checklist.md) | Flags overly broad IAM bindings (`roles/owner`, `roles/editor`, `allUsers`, or admin roles on agents). |
| **`incident-postmortem-writer`** | [SKILL.md](../../local-skills/incident-postmortem-writer/SKILL.md) | Writes blameless SRE postmortems with UTC timelines, Five Whys analysis, and action items. |

### Standalone Skill vs. Full Plugin

| Format | Pick This When... | How Lab 02 & Lab 03 Handle It |
| :--- | :--- | :--- |
| **Standalone Skill** (`local-skills/<name>/`) | You only need markdown instructions and optional `references/` or `scripts/`. | `tools.plugin_sync ingest` groups them under `--plugin-name` (default `local-ops-toolkit`), and the Middleware generates all 5 IDE manifests automatically. |
| **Full Plugin** (`starter-marketplace/plugins/<name>/`) | You need MCP servers, A2A subagents, glob rules, client hooks, or custom branding. | `tools.plugin_sync` splits skills and MCP/A2A services into Agent Registry, and the Middleware reassembles the plugin ZIP per IDE. |

Validate all 3 standalone skills now:

```bash
.venv/bin/python - << 'EOF'
from pathlib import Path
from schemas.validators import validate_skill_md

for skill_dir in sorted(p for p in Path("local-skills").iterdir() if p.is_dir()):
    res = validate_skill_md(skill_dir / "SKILL.md", expected_name=skill_dir.name)
    print(f"{skill_dir.name:28s} valid={res.valid} errors={res.errors}")
EOF
```

All 3 should report `valid=True` with `errors=[]`.

---

## Step 5 (Optional / CI): Run `agy plugin validate --json` with the Multi-Tool Filter

When you validate a multi-IDE plugin with Google's `agy` CLI, it logs expected warnings for sibling IDE files (`.mcp.json`, `mcp.json`, root `AGENTS.md`, and shadowed root `plugin.json`). Use the `jq` filter below to ignore only those expected coexistence warnings while still failing on any real error.

### Local Workstation Check

Run this locally (if `agy` is not installed on your laptop, it automatically runs the schema test suite instead):

```bash
if command -v agy >/dev/null 2>&1; then
  agy_rc=0
  report="$(agy plugin validate --json starter-marketplace/plugins/gcp-finops-toolkit)" || agy_rc=$?
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
else
  echo "agy CLI not installed on this workstation; running schema test suite instead:"
  .venv/bin/pytest -v tests/test_schemas.py
fi
```

### Live CI Pipeline Gate (GitLab CI)

In [starter-marketplace/.gitlab-ci.yml](../../starter-marketplace/.gitlab-ci.yml), Stage 1 runs both `validate:schemas` (the Python 5-IDE validators) and `validate:antigravity-cli` (the `agy plugin validate --json` + `jq` filter above) on every Merge Request, `main` push, and release tag before anything reaches Live Google Cloud Agent Registry.

---

## Verification Checkpoint

- [ ] All 5 schema validators pass on `starter-marketplace/plugins/gcp-finops-toolkit/`.
- [ ] `make starter-check` shows `6/6 present` and `[PASS]` for both `Agent Plugins 1.0` and `Google Antigravity v1` on your updated skeleton.
- [ ] All 3 standalone skills in `local-skills/` report `valid=True`.
- [ ] `.venv/bin/pytest -v tests/test_schemas.py` passes completely.

---

## Troubleshooting

| Symptom | Why It Happens | How to Fix |
| :--- | :--- | :--- |
| `additionalProperties` error in `.antigravity-plugin/plugin.json` | An unsupported key (like `category` or `mcpServers`) is inside `.antigravity-plugin/plugin.json`. | Check [schemas/antigravity_plugin_v1.schema.json](../../schemas/antigravity_plugin_v1.schema.json) and remove unrecognized keys. |
| `globs must be a comma-separated string` | `globs` was written as a YAML list (`["**/*.sql", "**/*.tf"]`) inside `rules/*.md`. | Change it to a single string: `globs: "**/*.sql, **/*.tf"`. |
| `hooks.json` rejected for `${PLUGIN_ROOT}` | A hook command uses `${PLUGIN_ROOT}/scripts/...`. | Replace it with a relative path (`./scripts/...`) and run `chmod 0755 scripts/*.sh`. |
| `${ENV_VAR}` error in `mcp.json` | Remote `url` or `headers` in Agent Plugins 1.0 `mcp.json` contains `${VAR}`. | Remove `${VAR}` from `mcp.json`; use OAuth, `google_credentials`, or an Agent Registry `Binding`. |
| Live Agent Registry rejects `agent-card.json` (`unknown field "stateTransitionHistory"` or `duplicate interface url`) | The A2A card uses legacy v0.x `stateTransitionHistory` or reuses `/a2a` for both protocol bindings. | Keep only A2A v1.0 `capabilities` (`streaming`, `pushNotifications`) and split bindings onto `/a2a` (`JSONRPC`) and `/a2a/http` (`HTTP+JSON`). |

---

**Next Lab:** [Lab 02: Register Assets in Google Cloud Agent Registry →](02-register-agent-registry.md)
