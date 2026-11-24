# Lab 04: 1-Command Multi-IDE Developer Setup (`hub_sync` & Bootstrap `/mcp`)

**Workstream:** A (Core Platform & Middleware)  
**Duration:** 40 minutes  
**Prerequisites:** Completed [Lab 03](03-build-deploy-middleware.md) with the Middleware running on `http://127.0.0.1:8080` (or deployed on Cloud Run).

---

## Files You'll Work With

| Role | File | What You'll Do |
| :--- | :--- | :--- |
| **Edit** | [starter/lab04_hub_sync_adapter_exercise.py](../../starter/lab04_hub_sync_adapter_exercise.py) | Implement the 2 `# WORKSHOP-TODO(lab-04)` functions (`atomic_write_json`, `safe_merge_antigravity_mcp_config`). |
| **Run** | `make demo-hub-sync` ([tools/hub_sync/cli.py](../../tools/hub_sync/cli.py)) | Sync verified plugins and MCP configs across all 4 IDEs in one command. |
| **Inspect (Generated Output)** | `/tmp/acme-agent-hub-demo-home/` | Check the generated files for Claude Code, VS Code/Copilot, Codex, and Antigravity without touching your real `$HOME`. |
| **Reference Solution** | [tools/hub_sync/hydration.py](../../tools/hub_sync/hydration.py), [tools/hub_sync/adapters.py](../../tools/hub_sync/adapters.py) | Peek here if you get stuck on atomic JSON writes or ownership-tracked config merging. |

---

## Learning Objectives

By the end of this lab, you will:
1. Implement atomic JSON writes and ownership-tracked MCP config merging in `starter/lab04_hub_sync_adapter_exercise.py`.
2. Sync verified plugins, marketplaces, and MCP configs across Claude Code, VS Code/Copilot, OpenAI Codex, and Google Antigravity with `python -m tools.hub_sync sync`.
3. Prove that personal MCP servers and `disabledTools` overrides survive full re-syncs untouched.
4. Wire the Middleware's `/mcp` gateway directly with `--bootstrap-mcp-only` when you want zero plugin files on disk.

> **Why we use a local sync CLI (`tools.hub_sync`):**
> - **Antigravity remote URL limit:** `agy plugin install <url>` only accepts `github.com` URLs today, so it cannot pull directly from corporate GitLab or Cloud Run. `tools.hub_sync` verifies the ZIP SHA-256, unpacks it locally, and installs from disk.
> - **Claude Code local directory marketplace:** Claude Code forbids relative `./plugins/...` paths in HTTP marketplaces, but allows them in local directory marketplaces. `tools.hub_sync` writes `.acme-agent-hub/.claude-plugin/marketplace.json` and registers it in `~/.claude/settings.json`.
> - **Never clobber personal dotfiles:** Every write uses a same-directory temporary file, `os.fsync`, and `os.replace`. Ownership is recorded in `.acme-agent-hub/.sync-manifest.json` so personal entries are never overwritten or deleted.

Steps 1 through 5 run in **Local Mode** against `http://127.0.0.1:8080` and write under `/tmp` so your real `$HOME` stays clean. Ready to point at Cloud Run? Jump to the Going Live on GCP section at the bottom after finishing locally.

---

## Step 1: Preview What `tools.hub_sync` Will Write (`--dry-run`)

Start with a dry run into an isolated demo home directory so you can see the full plan before any files touch disk:

```bash
export DEMO_HOME="/tmp/acme-agent-hub-demo-home"
rm -rf "${DEMO_HOME}"

.venv/bin/python -m tools.hub_sync sync \
  --middleware-url http://127.0.0.1:8080 \
  --home "${DEMO_HOME}" \
  --tools all \
  --dry-run
```

**You should see:** `[DRY-RUN] Acme Agent Hub Sync Complete`, listing `gcp-finops-toolkit`, its SHA-256 prefix, and the verification commands for all 4 IDEs.

---

## Step 2: Code Atomic Writes and Safe Config Merging

Open [starter/lab04_hub_sync_adapter_exercise.py](../../starter/lab04_hub_sync_adapter_exercise.py) and run its offline self-check to see the starting state (`0/3 checks passed`):

```bash
.venv/bin/python starter/lab04_hub_sync_adapter_exercise.py
```

Fill in the 2 `# WORKSHOP-TODO(lab-04)` functions:

1. **`atomic_write_json(path, payload)`**
   - Hint: Create `path.parent` with `mkdir(parents=True, exist_ok=True)`. Create a temp file in `path.parent` (same filesystem) with `tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)`. Write pretty JSON with a trailing newline, call `fh.flush()` and `os.fsync(fh.fileno())`, then call `os.replace(tmp_name, path)`. Unlink the temp file in an `except` block if anything fails before `os.replace`.
   - Stuck? Peek at `atomic_write_bytes()` and `atomic_write_json()` in [tools/hub_sync/hydration.py](../../tools/hub_sync/hydration.py).
2. **`safe_merge_antigravity_mcp_config(existing_doc, desired_servers, previous_owned_keys, *, catalog_complete)`**
   - Hint: Deep-copy `existing_doc` and read its `mcpServers` dict. For each key in `desired_servers`, skip it if the key already exists and is not in `previous_owned_keys` (that server belongs to the user). For owned or new keys, preserve any `PRESERVED_USER_FIELDS` (`disabled`, `disabledTools`) already set on the existing entry. Delete retired keys (`previous_owned_keys - set(desired_servers)`) only when `catalog_complete` is `True`; otherwise keep them in `new_owned` so a partial outage never wipes working configs.
   - Stuck? Peek at `merge_mcp_servers_dict()` in [tools/hub_sync/hydration.py](../../tools/hub_sync/hydration.py) and `wire_antigravity()` in [tools/hub_sync/adapters.py](../../tools/hub_sync/adapters.py).

Re-run the self-check and verify all Labs 01–04 starter exercises together:

```bash
.venv/bin/python starter/lab04_hub_sync_adapter_exercise.py
make starter-check
```

**You should see:** `[DONE] Progress: 3/3 checks passed` for Lab 04, followed by a clean `make starter-check` run.

---

## Step 3: Run `make demo-hub-sync` and Inspect All 4 IDEs

Now run the reference CLI to install plugins and configure all 4 IDEs inside `/tmp/acme-agent-hub-demo-home`:

```bash
make demo-hub-sync
```

If you also want to generate VS Code's `~/.config/Code/User/settings.json` inside `${DEMO_HOME}`, pass `--apply-vscode`:

```bash
.venv/bin/python -m tools.hub_sync sync \
  --middleware-url http://127.0.0.1:8080 \
  --home "${DEMO_HOME}" \
  --tools all \
  --apply-vscode \
  --no-exec
```

Check the sync status in both plain text and JSON:

```bash
.venv/bin/python -m tools.hub_sync status --home "${DEMO_HOME}"
.venv/bin/python -m tools.hub_sync status --home "${DEMO_HOME}" --json
```

> **Note on `${DEMO_HOME}` vs. `$HOME`:** `make demo-hub-sync` writes into `/tmp/acme-agent-hub-demo-home` with `--no-exec` so your personal dotfiles stay untouched. The optional CLI checks below (`claude plugin list`, `codex mcp list`, `agy plugin list --json`) read `$HOME`. Either sync to your real `$HOME` first (without `--home` or `--no-exec`) or prefix the command with `HOME="${DEMO_HOME}"`.

Inspect the generated files for each IDE:

### 1. Claude Code
```bash
cat "${DEMO_HOME}/.claude/settings.json"
cat "${DEMO_HOME}/.acme-agent-hub/.claude-plugin/marketplace.json"
```
- Registers `extraKnownMarketplaces["acme-enterprise-marketplace"]` pointing to `${DEMO_HOME}/.acme-agent-hub` (`source: "directory"`).
- Enables `gcp-finops-toolkit@acme-enterprise-marketplace` in `enabledPlugins`.
- Optional CLI check: `claude plugin list`.

### 2. VS Code & GitHub Copilot
```bash
cat "${DEMO_HOME}/.acme-agent-hub/vscode-settings-snippet.json"
cat "${DEMO_HOME}/.acme-agent-hub/marketplace.json"
cat "${DEMO_HOME}/.acme-agent-hub/.github/plugin/marketplace.json"
```
- Sets `"chat.plugins.enabled": true`, `"chat.plugins.marketplaces": ["file:///..."]`, and `"chat.pluginLocations"`.
- Optional Copilot CLI check:
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
- Symlinks `~/.agents/plugins/plugins/gcp-finops-toolkit` to `~/.acme-agent-hub/plugins/gcp-finops-toolkit`.
- Merges `[mcp_servers.finops-mcp-server]` and `[mcp_servers.agent-hub-gw]` into `~/.codex/config.toml`.
- Optional CLI check: `codex mcp list`.

### 4. Google Antigravity
```bash
cat "${DEMO_HOME}/.acme-agent-hub/antigravity-marketplace.json"
cat "${DEMO_HOME}/.gemini/config/mcp_config.json"
ls -la "${DEMO_HOME}/.gemini/config/plugins/gcp-finops-toolkit/"
```
- When `agy` is on `PATH` and `--no-exec` is omitted, `tools.hub_sync` runs `agy plugin install <hub>/plugins/gcp-finops-toolkit` automatically.
- **Naming note (`finops-advisor` vs. `finops-advisor-agent`):** In the source Git repo (`starter-marketplace/plugins/gcp-finops-toolkit/agents/finops-advisor.md`), the local subagent is named `finops-advisor`. When the Middleware builds the governed ZIP from Agent Registry, it derives the slug from the registered A2A Service ID (`finops-advisor-agent`), so synced bundles expose `agy --agent finops-advisor-agent`.
- Optional CLI check:
  ```bash
  agy plugin list --json
  agy --agent finops-advisor-agent
  ```

---

## Step 4: Prove Safe Config Merging

Add a personal MCP server (`my-personal-mcp`) and disable one tool on the enterprise server inside `${DEMO_HOME}/.gemini/config/mcp_config.json`, then re-run `make demo-hub-sync`:

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

# Re-sync and confirm both personal changes survived
make demo-hub-sync
cat /tmp/acme-agent-hub-demo-home/.gemini/config/mcp_config.json
```

**You should see:** Both `my-personal-mcp` and `disabledTools: ["delete_idle_dataset"]` stay intact after the re-sync.

---

## Step 5: Try Zero-Disk `--bootstrap-mcp-only` Mode

For locked-down workstations or fast onboarding, you can wire only the Middleware's `/mcp` gateway without extracting any plugin folders to disk:

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

**You should see:** Zero plugin folders under `${BOOTSTRAP_HOME}/.acme-agent-hub/plugins/`, and each IDE wired to `http://127.0.0.1:8080/mcp`:
- **Google Antigravity:** Merges the `/mcp` gateway into `${BOOTSTRAP_HOME}/.gemini/config/mcp_config.json`.
- **OpenAI Codex:** Merges `[mcp_servers.acme-agent-hub]` (and catalog MCP servers) into `${BOOTSTRAP_HOME}/.codex/config.toml`.
- **Claude Code:** Writes `${BOOTSTRAP_HOME}/.acme-agent-hub/claude-bootstrap-mcp.json` (and prints `claude mcp add --transport http ...`) without changing plugin entries in `settings.json`.
- **VS Code / GitHub Copilot:** Writes `${BOOTSTRAP_HOME}/.acme-agent-hub/vscode-mcp-snippet.json` alongside `vscode-settings-snippet.json`.

---

## Going Live on GCP (Optional — Track 2 & Track 3)

> **Region placement:** Point `--middleware-url` at your Bangkok (`asia-southeast3`) Cloud Run Middleware. It serves `global` standalone `Skills` (`private-*` auto-filtered) alongside your Bangkok `Services` (see [Attendee Quickstart](../attendee-quickstart.md)).

Fetch your Cloud Run Middleware URL using whichever track you deployed in Lab 03:

**Track 2 (`gcloud`):**

```bash
export GCP_PROJECT_ID="<your-gcp-project-id>"
export HUB_MIDDLEWARE_URL="$(gcloud run services describe polyglot-middleware \
  --project="${GCP_PROJECT_ID}" --region="asia-southeast3" \
  --format="value(status.url)" --quiet)"
```

**Track 3 (Terraform):**

```bash
export HUB_MIDDLEWARE_URL="$(terraform -chdir=terraform/core output -raw middleware_url)"
```

Then export an identity token (required when `allow_unauthenticated_workshop_demo = false`) and sync your real `$HOME` (omit `--no-exec` so `agy plugin install` runs automatically when `agy` is installed):

```bash
export HUB_AUTH_TOKEN="$(gcloud auth print-identity-token --quiet)"

.venv/bin/python -m tools.hub_sync sync \
  --middleware-url "${HUB_MIDDLEWARE_URL}" \
  --tools all

.venv/bin/python -m tools.hub_sync status
```

---

## Verification Checkpoint

- [ ] `.venv/bin/python starter/lab04_hub_sync_adapter_exercise.py` reports `3/3` checks passed (`[DONE]`).
- [ ] `make demo-hub-sync` completes cleanly and lists `gcp-finops-toolkit` as `[OK]` with a 64-character SHA-256 digest.
- [ ] All 4 IDE config locations (`claude`, `copilot`, `codex`, `antigravity`) report `(wired, present)` in `status`.
- [ ] Personal `my-personal-mcp` and `disabledTools: ["delete_idle_dataset"]` survived a full re-sync.
- [ ] `.venv/bin/pytest -v tests/test_track1c_hub_sync.py` passes.

---

## Troubleshooting

| Symptom | Why It Happens | How to Fix |
| :--- | :--- | :--- |
| `ERROR: Failed to fetch catalog from http://127.0.0.1:8080/v1/catalog` | Middleware is not running or the URL is wrong. | Start `make run-middleware` in Terminal 3 and confirm `/health` (or `/healthz` locally) returns `200`. |
| `agy plugin install` fails on a remote HTTPS URL | Antigravity remote URL installs currently accept only `github.com` URLs. | Sync locally first with `tools.hub_sync sync` and install from the local folder (`~/.acme-agent-hub/plugins/<name>`). |
| `401` or `403` against Cloud Run Middleware in Live GCP Mode | Cloud Run requires IAM invoker auth (`allow_unauthenticated_workshop_demo = false`). | Export `HUB_AUTH_TOKEN="$(gcloud auth print-identity-token --quiet)"` and confirm you hold `roles/run.invoker`. |
| `404` HTML page probing `${HUB_MIDDLEWARE_URL}/healthz` | Google Frontend (GFE) reserves `/healthz` on live `*.run.app` URLs. | Probe **`${HUB_MIDDLEWARE_URL}/health`** (or `/v1/health`) with `Authorization: Bearer ${HUB_AUTH_TOKEN}` instead. |

---

**Next Lab:** [Lab 05: Platform Agents (ADK) & GitLab Dual-SoT CI/CD →](05-platform-agents-gitlab-sot.md)
