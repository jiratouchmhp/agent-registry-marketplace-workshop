# Lab 01: Author a Polyglot Agent Plugin (`agent-plugins.org` 1.0 + Antigravity Coexistence)

**Workstream:** A — Core Platform & Middleware  
**Duration:** 35 minutes  
**Prerequisites:** Completed [Lab 00](00-environment-setup.md).

---

## Learning Objectives

By the end of this lab, you will:
1. Understand why an enterprise plugin should be authored once in **Agent Plugins 1.0** (`https://agent-plugins.org/`) with **multi-tool coexistence** files for **Google Antigravity**, **Claude Code**, **VS Code / GitHub Copilot**, and **OpenAI Codex**—and when a lightweight standalone **`agentskills.io`** skill (like those in [local-skills/](../../local-skills/README.md)) is the better fit.
2. Inspect the complete reference plugin [starter-marketplace/plugins/gcp-finops-toolkit/](../../starter-marketplace/plugins/gcp-finops-toolkit/plugin.json) and see how it satisfies all **7 Antigravity Pre-Publish rules**.
3. Complete the hands-on exercise in [starter/lab01_plugin_skeleton/](../../starter/lab01_plugin_skeleton/plugin.json) until it passes both `validate_agent_plugin_bundle` and `validate_antigravity_bundle`.
4. Run the official multi-tool `agy plugin validate --json` CI `jq` filter that tolerates expected cross-IDE files without masking real errors.

---

## Step 1: Anatomy of a 5-Target Polyglot Plugin

Inspect the reference plugin directory `starter-marketplace/plugins/gcp-finops-toolkit/`:

```bash
ls -la starter-marketplace/plugins/gcp-finops-toolkit/
```

Each file serves a specific IDE/standard while sharing one unified `skills/` tree:

| File / Folder | Consumer Target(s) | Verified Engineering Rule |
| :--- | :--- | :--- |
| [plugin.json](../../starter-marketplace/plugins/gcp-finops-toolkit/plugin.json) | **Agent Plugins 1.0**, **VS Code / Copilot**, **OpenAI Codex** (`extensions["com.openai"]`) | `$schema: "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"`; root `plugin.json` takes priority #1 in VS Code/Copilot. |
| [.antigravity-plugin/plugin.json](../../starter-marketplace/plugins/gcp-finops-toolkit/.antigravity-plugin/plugin.json) | **Google Antigravity** (takes precedence over root `plugin.json`) | Strict `additionalProperties: false`; `name` must NOT start with `antigravity`, `gemini`, `claude`, or `cursor`; requires local 1:1 `logo: "assets/logo.svg"` and up to 3 `suggestedPrompts`. |
| [.claude-plugin/plugin.json](../../starter-marketplace/plugins/gcp-finops-toolkit/.claude-plugin/plugin.json) & [.mcp.json](../../starter-marketplace/plugins/gcp-finops-toolkit/.mcp.json) | **Claude Code** | Standard Claude plugin metadata and `streamable-http` MCP server entry. |
| [.codex-plugin/plugin.json](../../starter-marketplace/plugins/gcp-finops-toolkit/.codex-plugin/plugin.json) | **OpenAI Codex** | Paired with `extensions["com.openai"]` in root `plugin.json`. |
| [mcp.json](../../starter-marketplace/plugins/gcp-finops-toolkit/mcp.json) | **Agent Plugins 1.0**, **VS Code / Copilot** | `$schema: "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"`; **NO `${ENV_VAR}` expansion** allowed in remote `url` or `headers`! |
| [mcp_config.json](../../starter-marketplace/plugins/gcp-finops-toolkit/mcp_config.json) | **Google Antigravity** (ignores `mcp.json` & `.mcp.json`) | Uses `authProviderType: "google_credentials"` without `variables[]` so the plugin installs **enabled immediately**; server key sanitized (`^[a-zA-Z0-9_-]{1,20}$`, no dots). |
| [skills/cost-anomaly-triage/SKILL.md](../../starter-marketplace/plugins/gcp-finops-toolkit/skills/cost-anomaly-triage/SKILL.md) & [skills/bq-query-optimizer/SKILL.md](../../starter-marketplace/plugins/gcp-finops-toolkit/skills/bq-query-optimizer/SKILL.md) | **All 5 targets** (`agentskills.io`) | Uppercase `SKILL.md`; YAML `name` MUST equal parent directory name; `metadata` holds scalar `map<string, string>` provenance links (`plugin_name`, `mcp_service_id`, `agent_service_id`). |
| [rules/AGENTS.md](../../starter-marketplace/plugins/gcp-finops-toolkit/rules/AGENTS.md) & [rules/finops-guardrails.md](../../starter-marketplace/plugins/gcp-finops-toolkit/rules/finops-guardrails.md) | **Google Antigravity** (root `AGENTS.md` is ignored) | `rules/finops-guardrails.md` uses `trigger: glob` with **comma-separated string** `globs: "**/*.sql, **/*.tf"` (NEVER a YAML array!). |
| [hooks.json](../../starter-marketplace/plugins/gcp-finops-toolkit/hooks.json) & [scripts/finops-guard.sh](../../starter-marketplace/plugins/gcp-finops-toolkit/scripts/finops-guard.sh) | **Google Antigravity** | Named top-level group (`"policy-guard"`), **relative paths (`./scripts/finops-guard.sh`) ONLY** (never `${PLUGIN_ROOT}`), script permissions `0755`. |
| [agents/finops-advisor.md](../../starter-marketplace/plugins/gcp-finops-toolkit/agents/finops-advisor.md), [com.github.copilot/agents/finops-advisor.agent.md](../../starter-marketplace/plugins/gcp-finops-toolkit/com.github.copilot/agents/finops-advisor.agent.md), [agents/finops-advisor/agent-card.json](../../starter-marketplace/plugins/gcp-finops-toolkit/agents/finops-advisor/agent-card.json) | **Antigravity / Claude Code**, **GitHub Copilot**, and **A2A v1.0** | Native subagent definitions (`name: finops-advisor` in the local authored repo) plus A2A v1.0 Agent Card (≤ 10 KB, no legacy v0.x `capabilities.stateTransitionHistory`, and distinct `supportedInterfaces` URLs: `JSONRPC` on `/a2a`, `HTTP+JSON` on `/a2a/http`) registered in Agent Registry under Service ID `finops-advisor-agent` in Lab 02 (which the Middleware uses as the hydrated agent slug in Lab 04). |

---

## Step 1B: Standalone `agentskills.io` Skills (`local-skills/`) vs. Full `agent-plugins.org` Plugins

Not every team needs MCP servers, A2A subagents, rules, and hooks on Day 1. For **Option 1 (Local Skills Path)**, this workshop ships **three pre-prepared, zero-manifest standalone skills** in [local-skills/](../../local-skills/README.md) (`<skill-name>/SKILL.md` with no `plugin.json` required):

| Standalone Skill | Manifest & References | Purpose |
| :--- | :--- | :--- |
| **`cloud-run-debugger`** | [SKILL.md](../../local-skills/cloud-run-debugger/SKILL.md) + [references/triage-runbook.md](../../local-skills/cloud-run-debugger/references/triage-runbook.md) | Structured read-only triage for Cloud Run revision startup failures, probe timeouts, IAM invoker denials, and Cloud Logging error spikes. |
| **`terraform-iam-auditor`** | [SKILL.md](../../local-skills/terraform-iam-auditor/SKILL.md) + [references/least-privilege-checklist.md](../../local-skills/terraform-iam-auditor/references/least-privilege-checklist.md) | Least-privilege audit of Terraform GCP IAM bindings (flags `roles/owner`/`roles/editor`, `allUsers`, and granting `roles/agentregistry.editor`/`admin` to autonomous agents). |
| **`incident-postmortem-writer`** | [SKILL.md](../../local-skills/incident-postmortem-writer/SKILL.md) | Blameless SRE postmortem authoring workflow with UTC timelines, Five Whys root-cause analysis, and tracked action items. |

### When to Author a Standalone Skill vs. a Full Polyglot Plugin

| Authoring Format | Choose This When... | Required Files | How Middleware Serves It |
| :--- | :--- | :--- | :--- |
| **Standalone `agentskills.io` Skill** (`local-skills/<name>/`) | You want pure instructional runbooks, checklists, or prompt templates with minimal boilerplate (e.g., SRE runbooks, code-review guides, Terraform auditors) | Only `<skill-name>/SKILL.md` (frontmatter `name == <skill-name>`, uppercase `SKILL.md`) + optional `references/` or `scripts/` | `tools.plugin_sync ingest` groups standalone skills under `--plugin-name` (default `local-ops-toolkit`, or `--per-skill-plugin`), and the Polyglot Middleware automatically synthesizes all 5 IDE manifests on egress! |
| **Full `agent-plugins.org` 1.0 Plugin** (`starter-marketplace/plugins/<name>/`) | You need to bundle skills together with **governed MCP servers** (`mcp.json` / `mcp_config.json`), **A2A subagents** (`agents/`), **Antigravity glob rules** (`rules/`), **client hooks** (`hooks.json`), or custom branding (`assets/logo.svg`) | Root `plugin.json`, `.antigravity-plugin/plugin.json`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, MCP configs, `skills/` | `tools.plugin_sync` decomposes skills + MCP/A2A `Service` resources into Agent Registry, and Middleware re-assembles the linked plugin ZIP across all 4 IDE feeds. |

Validate all 3 standalone local skills immediately:

```bash
.venv/bin/python - << 'EOF'
from pathlib import Path
from schemas.validators import validate_skill_md

for skill_dir in sorted(p for p in Path("local-skills").iterdir() if p.is_dir()):
    res = validate_skill_md(skill_dir / "SKILL.md", expected_name=skill_dir.name)
    print(f"{skill_dir.name:28s} valid={res.valid} errors={res.errors}")
EOF
```

---

## Step 2: Validate the Reference Plugin Across All 5 Schemas

Run the repository's schema validators ([schemas/validators.py](../../schemas/validators.py)) against `gcp-finops-toolkit`:

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

All five validators should report `valid=True` with `errors=[]`.

---

## Step 3: Hands-On Exercise — Upgrade `starter/lab01_plugin_skeleton/`

Inspect the minimal starter skeleton in [starter/lab01_plugin_skeleton/plugin.json](../../starter/lab01_plugin_skeleton/plugin.json) and [starter/lab01_plugin_skeleton/skills/hello-finops/SKILL.md](../../starter/lab01_plugin_skeleton/skills/hello-finops/SKILL.md).

Run the validator against the skeleton to see what passes vs. what Antigravity requires:

```bash
.venv/bin/python - << 'EOF'
from schemas import validate_agent_plugin_bundle, validate_antigravity_bundle

for label, fn in [
    ("Agent Plugins 1.0", validate_agent_plugin_bundle),
    ("Antigravity v1   ", validate_antigravity_bundle),
]:
    res = fn("starter/lab01_plugin_skeleton")
    print(f"{label}: valid={res.valid}")
    for err in res.errors:
        print(f"  ERROR: {err}")
    for warn in res.warnings:
        print(f"  WARN : {warn}")
EOF
```

Now upgrade `starter/lab01_plugin_skeleton/` (or a temporary copy in `/tmp/my-finops-plugin`) by adding:
1. `.antigravity-plugin/plugin.json` matching [schemas/antigravity_plugin_v1.schema.json](../../schemas/antigravity_plugin_v1.schema.json) (`$schema`, `name`, `displayName`, `version`, `description`, `logo: "assets/logo.svg"`, `suggestedPrompts`, `author` with `https://` `url`).
2. A 1:1 square `assets/logo.svg` (you can copy [assets/logo.svg](../../starter-marketplace/plugins/gcp-finops-toolkit/assets/logo.svg)).
3. `mcp_config.json` with `authProviderType: "google_credentials"` and a sanitized server key (≤ 20 chars, no dots, no `variables[]`).
4. `rules/AGENTS.md` and `rules/finops-guardrails.md` using a **comma-separated string** for `globs`.
5. `hooks.json` with a named top-level group pointing to `./scripts/<script>.sh` with mode `chmod 0755`.

Re-run the Python validator snippet above (or run `make starter-check`, which verifies all Labs 01–04 exercises completely offline with zero Docker) until **both** `validate_agent_plugin_bundle` and `validate_antigravity_bundle` return `valid=True`!

---

## Step 4: Run the Official Antigravity Multi-Tool CI Filter

When validating a multi-tool plugin with the Google Antigravity CLI (`agy plugin validate --json`), Antigravity emits expected informational warnings for sibling-IDE files (`.mcp.json`, `mcp.json`, root `AGENTS.md`, and shadowed root `plugin.json`).

### Local Mode / CI Runner (when `agy` CLI is installed)

This is the exact filter enforced in [starter-marketplace/.gitlab-ci.yml](../../starter-marketplace/.gitlab-ci.yml):

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

### Live GCP / GitLab CI Mode

In GitLab CI ([starter-marketplace/.gitlab-ci.yml](../../starter-marketplace/.gitlab-ci.yml)), Stage 1 runs both `validate:schemas` (Python 5-IDE schema gate) and `validate:antigravity-cli` (the `agy plugin validate --json` + `jq` filter above) on every Merge Request, `main` commit, and release tag before any artifact can be packaged or uploaded to Agent Registry.

---

## Verification Checkpoint

- [ ] All 5 IDE validators pass on `starter-marketplace/plugins/gcp-finops-toolkit/`.
- [ ] `.venv/bin/pytest -v tests/test_schemas.py` passes (covering all 7 Antigravity pitfalls and all 4 marketplace catalog schemas).
- [ ] Your updated plugin skeleton passes both `validate_agent_plugin_bundle` and `validate_antigravity_bundle`.

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `additionalProperties` error in `.antigravity-plugin/plugin.json` | Added an unsupported key (e.g., `category` or `mcpServers`) inside `.antigravity-plugin/plugin.json` | Check [schemas/antigravity_plugin_v1.schema.json](../../schemas/antigravity_plugin_v1.schema.json) for the exact allowed keys. |
| `globs must be a comma-separated string` | Wrote `globs: ["**/*.sql", "**/*.tf"]` as a YAML list in `rules/*.md` | Change to `globs: "**/*.sql, **/*.tf"`. |
| `hooks.json` rejected for `${PLUGIN_ROOT}` | Used `${PLUGIN_ROOT}/scripts/...` in a hook command | Replace with a relative path `./scripts/...` and run `chmod 0755 scripts/*.sh`. |
| `${ENV_VAR}` error in `mcp.json` | Used `${API_TOKEN}` in remote `url` or `headers` of Agent Plugins 1.0 `mcp.json` | Agent Plugins 1.0 forbids `${VAR}` expansion in remote `url`/`headers`; rely on OAuth / Google Credentials or Agent Registry `Binding`. |
| Live Agent Registry rejects `agent-card.json` with `unknown field "stateTransitionHistory"` or `duplicate interface url` | Included legacy A2A v0.x `capabilities.stateTransitionHistory` or pointed both `JSONRPC` and `HTTP+JSON` at `/a2a` | Keep only A2A v1.0 `capabilities` (`streaming`, `pushNotifications`) and assign each protocol binding a unique URL (`/a2a` for `JSONRPC`, `/a2a/http` for `HTTP+JSON`). |

---

**Next Lab:** [Lab 02: Register Assets in Google Cloud Agent Registry →](02-register-agent-registry.md)
