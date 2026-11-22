# Guided Workshop Starter Exercises (Labs 01–04)

This directory contains hands-on starter skeletons and `# WORKSHOP-TODO(lab-XX)` coding exercises so attendees can build each piece of the Enterprise Agent Marketplace step-by-step while comparing against the complete reference implementations in the repository.

> **100% Zero-Docker & Offline Friendly:** Every starter exercise and self-check harness in this directory runs purely with `.venv/bin/python` — **no Docker daemon, no Terraform, and no GCP account required**.

## One-Command Verification Harness

Run all starter self-checks in a single command (or verify that the reference solutions pass every starter exercise check end-to-end):

```bash
# Check your current progress across Labs 01-04 (shows [PASS] vs [TODO])
make starter-check

# Verify the built-in reference solutions against every starter harness
.venv/bin/python scripts/check_starter.py --verify-reference-solutions
```

## Exercise-to-Solution Map

| Lab | Starter File(s) | Concept Practiced | Reference Solution |
| --- | --- | --- | --- |
| **Lab 01** | [lab01_plugin_skeleton/plugin.json](lab01_plugin_skeleton/plugin.json)<br>[lab01_plugin_skeleton/skills/hello-finops/SKILL.md](lab01_plugin_skeleton/skills/hello-finops/SKILL.md) | Authoring a Polyglot **Agent Plugins 1.0** + **Google Antigravity** + **Claude Code** + **Copilot** + **Codex** plugin bundle passing all 7 Antigravity Pre-Publish checks. | `starter-marketplace/plugins/gcp-finops-toolkit/` |
| **Lab 02** | [lab02_plugin_sync_exercise.py](lab02_plugin_sync_exercise.py) | Decomposing an `agent-plugins.org` bundle into single-`SKILL.md` `agentskills.io` ZIPs (≤ 1 MB, scalar `metadata` map) and registering standalone `Skill` resources via `v1` REST API with `private-{skillId}` auto-prefixing. | `tools/plugin_sync/` |
| **Lab 03** | [lab03_middleware_redirect_exercise.py](lab03_middleware_redirect_exercise.py) | Downloading `?alt=media` skill archives across the HTTP 302 GCS signed-URL redirect (stripping `Authorization` and `x-goog-*` headers), verifying SHA-256 + KMS ECDSA P-256 signatures, and assembling Polyglot 5-IDE ZIP bundles. | `middleware/` |
| **Lab 04** | [lab04_hub_sync_adapter_exercise.py](lab04_hub_sync_adapter_exercise.py) | Safe ownership-tracked hydration (`tempfile` + `os.replace` atomic writes, preserving user-authored MCP entries and `disabled`/`disabledTools` overrides, pruning only on clean full sync). | `tools/hub_sync/` |

## How to Work Through the Exercises

1. **Activate or invoke the workshop virtual environment**:
   ```bash
   .venv/bin/python --version
   ```
2. **Lab 01 — Validate your plugin skeleton**:
   ```bash
   .venv/bin/python - << 'EOF'
   from schemas import validate_agent_plugin_bundle, validate_antigravity_bundle
   print("Agent Plugins 1.0:", validate_agent_plugin_bundle("starter/lab01_plugin_skeleton"))
   print("Antigravity v1   :", validate_antigravity_bundle("starter/lab01_plugin_skeleton"))
   EOF
   ```
   Out of the box, `validate_agent_plugin_bundle` passes while `validate_antigravity_bundle` flags the missing Antigravity `logo` requirement and `make starter-check` displays the full 6-item Lab 01 coexistence checklist (`.antigravity-plugin/plugin.json`, 1:1 `assets/logo.svg`, `mcp_config.json`, `rules/AGENTS.md` + `rules/finops-guardrails.md`, `hooks.json`, and `0755` `scripts/*.sh`)!
3. **Labs 02–04 — Complete `# WORKSHOP-TODO(lab-XX)` blocks and run the built-in self-checks**:
   ```bash
   .venv/bin/python starter/lab02_plugin_sync_exercise.py
   .venv/bin/python starter/lab03_middleware_redirect_exercise.py
   .venv/bin/python starter/lab04_hub_sync_adapter_exercise.py
   ```
   Each file ships with a runnable `__main__` self-check harness that tests every `# WORKSHOP-TODO` function independently, reports `Progress: X/N checks passed`, and prints `[DONE]` once all checks pass.
