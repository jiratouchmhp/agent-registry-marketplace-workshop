# Lab 04: 1-Command Multi-IDE Developer Onboarding (`hub_sync` & Bootstrap `/mcp`)

**Workstream:** A — Core Platform & Middleware  
**Duration:** 40 minutes  
**Prerequisites:** Completed [Lab 03](03-build-deploy-middleware.md) with Middleware running on `http://127.0.0.1:8080` (or deployed on Cloud Run).

---

## Learning Objectives

By the end of this lab, you will:
1. Run `python -m tools.hub_sync sync` to hydrate verified enterprise plugins, marketplace catalogs, and MCP configs across **all 4 developer IDEs/CLIs** in one idempotent command:
   - **Claude Code**
   - **VS Code / GitHub Copilot**
   - **OpenAI Codex**
   - **Google Antigravity** (including `agy plugin install`, `agy plugin list --json`, and `agy --agent`)
2. Test **ownership-tracked safe merging**: prove that personal user MCP servers and `disabled` / `disabledTools` toggles survive re-syncs untouched, and that removed enterprise plugins are pruned *only* on clean full syncs.
3. Run zero-disk **Bootstrap MCP-Only Mode** (`--bootstrap-mcp-only`) so developers can discover and load verified skills dynamically through the Middleware `/mcp` gateway without extracting plugin files locally.
4. Practice atomic writes and safe config merging in [starter/lab04_hub_sync_adapter_exercise.py](../../starter/lab04_hub_sync_adapter_exercise.py).

---

## Why `hub_sync` Exists (Enterprise Realities)

1. **Google Antigravity `github.com`-only remote URL constraint:** `agy plugin install <url>` currently only supports `github.com` remote URLs—corporate GitLab instances and internal Cloud Run URLs cannot be passed directly. `tools.hub_sync` solves this cleanly by verifying the bundle's SHA-256, extracting it atomically into `~/.acme-agent-hub/plugins/<name>`, copying to `~/.gemini/config/plugins/<name>`, and invoking `agy plugin install <local-dir>` when `agy` is installed.
2. **Claude Code HTTP vs. Directory marketplaces:** HTTP URL marketplaces forbid relative `./plugins/...` sources; local directory marketplaces allow them. `hub_sync` builds `~/.acme-agent-hub/.claude-plugin/marketplace.json` and registers it in `~/.claude/settings.json`.
3. **Never clobber developer dotfiles:** Every write uses `tempfile.NamedTemporaryFile` (same directory) → `os.fsync` → `os.replace`, and `.acme-agent-hub/.sync-manifest.json` tracks exact ownership so user-authored entries are never overwritten or deleted.

---

## Step 1: Preview & Execute 1-Command Multi-IDE Hydration

Use an isolated demo home directory (`/tmp/acme-agent-hub-demo-home`) first so you can inspect every generated IDE file safely without touching your personal `~/.claude` or `~/.gemini` configs.

### Mode A: Local Mock Mode

1. Preview with `--dry-run` first:
   ```bash
   export DEMO_HOME="/tmp/acme-agent-hub-demo-home"
   rm -rf "${DEMO_HOME}"

   .venv/bin/python -m tools.hub_sync sync \
     --middleware-url http://127.0.0.1:8080 \
     --home "${DEMO_HOME}" \
     --tools all \
     --dry-run
   ```

2. Run the live local hydration via `make demo-hub-sync` (or directly with `--apply-vscode` to also generate `~/.config/Code/User/settings.json`):
   ```bash
   make demo-hub-sync
   ```
   Or with VS Code settings merge enabled:
   ```bash
   .venv/bin/python -m tools.hub_sync sync \
     --middleware-url http://127.0.0.1:8080 \
     --home "${DEMO_HOME}" \
     --tools all \
     --apply-vscode \
     --no-exec
   ```

3. Check hub status in human-readable and machine-readable JSON formats:
   ```bash
   .venv/bin/python -m tools.hub_sync status --home "${DEMO_HOME}"
   .venv/bin/python -m tools.hub_sync status --home "${DEMO_HOME}" --json
   ```

### Mode B: Live GCP Mode (Bangkok `asia-southeast3` Cloud Run Middleware Reading `global` Skills)

Point `--middleware-url` at your **Bangkok (`asia-southeast3`)** Cloud Run Middleware and supply an identity token via `HUB_AUTH_TOKEN` (required when `allow_unauthenticated_workshop_demo = false`). Your Bangkok Middleware reads standalone `Skills` from **`global`** (`private-*` auto-filtered) while linking your Bangkok (`asia-southeast3`) `McpServer` and `Agent` Services seamlessly.

#### Load Middleware URL (Track 2 — Zero-Terraform `scripts/deploy_gcp.sh`)

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"

# Option 1: Read directly from the deploy manifest written by scripts/deploy_gcp.sh
eval "$(.venv/bin/python - "/tmp/workshop-gcp-deploy-${GCP_PROJECT_ID}.json" << 'EOF'
import json, shlex, sys
from pathlib import Path
p = Path(sys.argv[1])
if p.is_file():
    doc = json.loads(p.read_text(encoding="utf-8"))
    regions = doc.get("regions", {})
    pri = regions.get("asia-southeast3", {}).get("cloud_run", {}).get("polyglot_middleware", {}).get("url", "")
    print(f"export HUB_MIDDLEWARE_URL={shlex.quote(pri)}")
EOF
)"

# Option 2: Or query Cloud Run directly via gcloud (works even if /tmp manifest was cleared)
export HUB_MIDDLEWARE_URL="${HUB_MIDDLEWARE_URL:-$(gcloud run services describe polyglot-middleware \
  --project="${GCP_PROJECT_ID}" --region="asia-southeast3" --format="value(status.url)" --quiet)}"
```

#### Load Middleware URL (Track 3 — Full Terraform IaC)

```bash
export HUB_MIDDLEWARE_URL="$(terraform -chdir=terraform/core output -raw middleware_url)"
```

#### Hydrate All 4 IDEs from Bangkok Cloud Run Middleware

```bash
export HUB_AUTH_TOKEN="$(gcloud auth print-identity-token --quiet)"

# Hydrate all 4 IDEs in your real developer HOME (omit --no-exec to run `agy plugin install`)
.venv/bin/python -m tools.hub_sync sync \
  --middleware-url "${HUB_MIDDLEWARE_URL}" \
  --tools all

.venv/bin/python -m tools.hub_sync status
```

---

## Step 2: Inspect Generated Configurations Across All 4 IDEs

Inspect every IDE surface generated under `${DEMO_HOME}`:

> **Note on `${DEMO_HOME}` vs. Real `$HOME` for Live CLI Commands:** Step 1 Mode A hydrated files into the isolated `${DEMO_HOME}` (`/tmp/acme-agent-hub-demo-home`) with `--no-exec` so your personal dotfiles stay untouched. The optional live CLI verification commands below (`claude plugin list`, `codex mcp list`, `agy plugin list --json`, `agy --agent finops-advisor-agent`) read `$HOME`, so run them either after syncing to your real `$HOME` (without `--home`/`--no-exec`) or by prefixing the command with `HOME="${DEMO_HOME}"` (for example, `HOME="${DEMO_HOME}" codex mcp list`).

### 1. Claude Code
```bash
cat "${DEMO_HOME}/.claude/settings.json"
cat "${DEMO_HOME}/.acme-agent-hub/.claude-plugin/marketplace.json"
```
- `extraKnownMarketplaces["acme-enterprise-marketplace"]` points to `${DEMO_HOME}/.acme-agent-hub` (`source: "directory"`).
- `enabledPlugins["gcp-finops-toolkit@acme-enterprise-marketplace"]` is set to `true`.
- Verify in Claude Code CLI: `claude plugin list`.

### 2. VS Code & GitHub Copilot
```bash
cat "${DEMO_HOME}/.acme-agent-hub/vscode-settings-snippet.json"
cat "${DEMO_HOME}/.acme-agent-hub/marketplace.json"
cat "${DEMO_HOME}/.acme-agent-hub/.github/plugin/marketplace.json"
```
- Sets `"chat.plugins.enabled": true`, `"chat.plugins.marketplaces": ["file:///..."]`, and `"chat.pluginLocations"`.
- Verify in GitHub Copilot CLI:
  ```bash
  copilot plugin marketplace add "${DEMO_HOME}/.acme-agent-hub"
  copilot plugin install gcp-finops-toolkit@acme-enterprise-marketplace
  ```

### 3. OpenAI Codex
```bash
cat "${DEMO_HOME}/.agents/plugins/marketplace.json"
ls -la "${DEMO_HOME}/.agents/plugins/plugins/"
cat "${DEMO_HOME}/.codex/config.toml"
ls -la "${DEMO_HOME}/.codex/agents/"
```
- Local plugin symlink points from `~/.agents/plugins/plugins/gcp-finops-toolkit` to `~/.acme-agent-hub/plugins/gcp-finops-toolkit`.
- `~/.codex/config.toml` merges `[mcp_servers.finops-mcp-server]` and `[mcp_servers.agent-hub-gw]`.
- Verify in Codex CLI: `codex mcp list`.

### 4. Google Antigravity
```bash
cat "${DEMO_HOME}/.acme-agent-hub/antigravity-marketplace.json"
cat "${DEMO_HOME}/.gemini/config/mcp_config.json"
ls -la "${DEMO_HOME}/.gemini/config/plugins/gcp-finops-toolkit/"
```
- When `agy` is on `PATH` (and `--no-exec` is omitted), `hub_sync` runs `agy plugin install <hub>/plugins/gcp-finops-toolkit` automatically.
- **Naming note (`finops-advisor` vs. `finops-advisor-agent`):** In the source Git repo (`starter-marketplace/plugins/gcp-finops-toolkit/agents/finops-advisor.md`), the local subagent is authored as `finops-advisor`. When the Polyglot Middleware synthesizes the governed plugin ZIP from Google Cloud Agent Registry, it derives the agent slug from the registered Agent Registry A2A `Service` ID (`finops-advisor-agent`), so hydrated bundles expose `agy --agent finops-advisor-agent`.
- Verify in Antigravity CLI:
  ```bash
  agy plugin list --json
  agy --agent finops-advisor-agent
  ```

---

## Step 3: Prove Safe Ownership-Tracked Merging

Inject a **personal user-authored MCP server** AND disable a single tool on the managed server inside `${DEMO_HOME}/.gemini/config/mcp_config.json`, then re-run `hub_sync`:

```bash
.venv/bin/python - << 'EOF'
import json
from pathlib import Path

cfg_path = Path("/tmp/acme-agent-hub-demo-home/.gemini/config/mcp_config.json")
doc = json.loads(cfg_path.read_text(encoding="utf-8"))

# 1. Add a personal unmanaged MCP server
doc["mcpServers"]["my-personal-mcp"] = {"url": "http://127.0.0.1:9999/mcp"}

# 2. Override disabledTools on the enterprise-managed finops-mcp-server
if "finops-mcp-server" in doc["mcpServers"]:
    doc["mcpServers"]["finops-mcp-server"]["disabledTools"] = ["delete_idle_dataset"]

cfg_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
EOF

# Re-sync and verify BOTH the personal server and disabledTools override survived!
make demo-hub-sync
cat /tmp/acme-agent-hub-demo-home/.gemini/config/mcp_config.json
```

---

## Step 4: Test Zero-Disk Bootstrap MCP-Only Mode

For locked-down developer environments or ultra-fast onboarding, wire **only** the Middleware's `/mcp` gateway without downloading any plugin ZIPs:

```bash
export BOOTSTRAP_HOME="/tmp/acme-agent-hub-bootstrap-home"
rm -rf "${BOOTSTRAP_HOME}"

.venv/bin/python -m tools.hub_sync sync \
  --middleware-url http://127.0.0.1:8080 \
  --home "${BOOTSTRAP_HOME}" \
  --tools all \
  --bootstrap-mcp-only \
  --no-exec

cat "${BOOTSTRAP_HOME}/.gemini/config/mcp_config.json"
cat "${BOOTSTRAP_HOME}/.codex/config.toml"
cat "${BOOTSTRAP_HOME}/.acme-agent-hub/claude-bootstrap-mcp.json"
cat "${BOOTSTRAP_HOME}/.acme-agent-hub/vscode-mcp-snippet.json"
```

Exact per-IDE bootstrap outputs (with zero plugin directories extracted on disk):
- **Google Antigravity:** Merges the `/mcp` gateway directly into `${BOOTSTRAP_HOME}/.gemini/config/mcp_config.json`.
- **OpenAI Codex:** Merges `[mcp_servers.acme-agent-hub]` (and catalog MCP servers) directly into `${BOOTSTRAP_HOME}/.codex/config.toml`.
- **Claude Code:** Emits `${BOOTSTRAP_HOME}/.acme-agent-hub/claude-bootstrap-mcp.json` (plus prints `claude mcp add --transport http ...`) while leaving `${BOOTSTRAP_HOME}/.claude/settings.json` plugin entries untouched.
- **VS Code / GitHub Copilot:** Emits `${BOOTSTRAP_HOME}/.acme-agent-hub/vscode-mcp-snippet.json` (drop-in `.vscode/mcp.json` `"servers"` snippet) alongside `${BOOTSTRAP_HOME}/.acme-agent-hub/vscode-settings-snippet.json`.

Developers now have direct access to `search_catalog`, `load_verified_skill`, `list_governed_mcp_servers`, and `call_remote_a2a_agent` via `http://127.0.0.1:8080/mcp` with zero plugin directories on disk.

---

## Step 5: Hands-On Coding Exercise (Optional Deep Dive)

Complete the `# WORKSHOP-TODO(lab-04)` blocks in [starter/lab04_hub_sync_adapter_exercise.py](../../starter/lab04_hub_sync_adapter_exercise.py):

```bash
.venv/bin/python starter/lab04_hub_sync_adapter_exercise.py
# Or verify all Labs 01-04 starter exercises together:
make starter-check
```

---

## Verification Checkpoint

- [ ] `make demo-hub-sync` completes cleanly and lists `gcp-finops-toolkit` as `[OK]` with a 64-char SHA-256 digest.
- [ ] All 4 IDE config locations (`claude`, `copilot`, `codex`, `antigravity`) report `(wired, present)` in `status`.
- [ ] Personal `my-personal-mcp` and `disabledTools: ["delete_idle_dataset"]` survived a full re-sync untouched.
- [ ] `.venv/bin/pytest -v tests/test_track1c_hub_sync.py` passes completely.

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `ERROR: Failed to fetch catalog from http://127.0.0.1:8080/v1/catalog` | Middleware is not running or wrong URL | Start `make run-middleware` in Terminal 3 and confirm `/health` (or `/healthz` locally) returns `200`. |
| `agy plugin install` fails on a remote HTTPS URL | Antigravity remote URL installs currently only accept `github.com` URLs | Always hydrate locally first with `tools.hub_sync sync` and pass the local directory path (`~/.acme-agent-hub/plugins/<name>`). |
| `401/403` against Cloud Run Middleware in Live GCP Mode | Cloud Run requires IAM invoker auth (`allow_unauthenticated_workshop_demo = false`) | Export `HUB_AUTH_TOKEN="$(gcloud auth print-identity-token --quiet)"` and grant yourself `roles/run.invoker`. |
| `404` HTML page probing `${HUB_MIDDLEWARE_URL}/healthz` before running `hub_sync` | Google Frontend (GFE) reserves `/healthz` on live `*.run.app` URLs | Probe `${HUB_MIDDLEWARE_URL}/health` (or `/v1/health`) with `Authorization: Bearer ${HUB_AUTH_TOKEN}` instead. |

---

**Next Lab:** [Lab 05: Platform Agents (ADK) & GitLab Dual-SoT CI/CD →](05-platform-agents-gitlab-sot.md)
