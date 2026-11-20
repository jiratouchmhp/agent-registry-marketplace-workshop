# Lab 02: Decompose & Register Assets in Google Cloud Agent Registry (`v1`)

**Workstream:** A — Core Platform & Middleware  
**Duration:** 35 minutes  
**Prerequisites:** Completed [Lab 00](00-environment-setup.md) and [Lab 01](01-author-agent-plugin.md).

---

## Learning Objectives

By the end of this lab, you will:
1. Understand how `tools.plugin_sync` decomposes an `agent-plugins.org` bundle into Google Cloud Agent Registry `v1` primitives:
   - Standalone **`Skill`** resources (`agentskills.io` ZIP ≤ 1 MB, strictly one uppercase `SKILL.md`, `private-` auto-prefixing).
   - **`Service`** resources projecting read-only **`McpServer`** (`TOOL_SPEC`), **`Agent`** (`A2A_AGENT_CARD` with **empty** top-level `interfaces`), and **`Endpoint`** (`NO_SPEC`).
2. Preview and execute `python -m tools.plugin_sync sync` in both **Local Mock Mode** and **Live GCP Mode** (plus equivalent `gcloud` & Terraform registrations).
3. Complete the optional hands-on coding exercise in [starter/lab02_plugin_sync_exercise.py](../../starter/lab02_plugin_sync_exercise.py).

---

## Key Agent Registry `v1` Invariants (Read Before Running)

1. **No generic `labels` map** exists on `Service`, `Agent`, `McpServer`, `Endpoint`, `Binding`, or `Skill` in `v1`. Plugin lineage is stored inside `SKILL.md` YAML `frontmatter.metadata` (`map<string, string>` scalar strings only): `plugin_name`, `plugin_display_name`, `plugin_version`, `source_repo`, `git_commit`, `mcp_service_id`, `agent_service_id`, `kms_signature`, `kms_key_version`.
2. **Automatic `private-` Skill ID prefix**: Calling `POST /v1/projects/{p}/locations/{l}/skills?skillId=cost-anomaly-triage` creates `projects/{p}/locations/{l}/skills/private-cost-anomaly-triage`. Every subsequent `GET`, `PATCH`, `DELETE`, and `/revisions` call **must** use `private-cost-anomaly-triage`!
3. **`A2A_AGENT_CARD` top-level `interfaces` rule**: When `agentSpec.type == "A2A_AGENT_CARD"`, the Service's top-level `interfaces` array **must be empty**—endpoints are declared exclusively inside `agentSpec.content.supportedInterfaces` (≤ 10 KB).
4. **Immutable `oneof spec` type**: Once a `Service` is created as `mcpServerSpec`, `agentSpec`, or `endpointSpec`, its spec type cannot be mutated via `PATCH`.

---

## Step 1: Dry-Run Decomposition & Inspect the Sync Plan

Run `tools.plugin_sync` in `--dry-run` mode to package deterministic per-skill ZIPs and inspect the exact `v1` payloads without mutating any API:

```bash
.venv/bin/python -m tools.plugin_sync sync \
  --plugin-dir starter-marketplace/plugins/gcp-finops-toolkit \
  --project acme-agent-governance \
  --location us-central1 \
  --target-state TARGET_STATE_ACTIVE \
  --scanning-policy ENABLED_BLOCKING \
  --output-dir /tmp/finops-sync-dryrun \
  --skip-mcp-probe \
  --dry-run
```

Inspect the generated deterministic skill ZIPs and `sync-plan.json`:

```bash
ls -la /tmp/finops-sync-dryrun/
.venv/bin/python -m zipfile -l /tmp/finops-sync-dryrun/cost-anomaly-triage.zip
```

Notice each archive contains **strictly one uppercase `SKILL.md`** and stays well under the 500 KB inline upload guideline (1 MB hard limit).

---

## Step 2: Register Skills, MCP Server, A2A Agent & Endpoint

### Mode A: Local Mock Mode

1. Ensure the Mock Registry (`:8090`) and Sample FinOps Workloads (`:8091` MCP server + `:8092` A2A agent) are running in two terminals:
   ```bash
   # Terminal 1
   make run-mock

   # Terminal 2
   make run-samples
   ```

2. Run `make demo-sync` (which calls `.venv/bin/python -m tools.plugin_sync sync` against `http://127.0.0.1:8090/v1`, live-probing `http://127.0.0.1:8091/mcp` for `tools/list` annotations!):
   ```bash
   make demo-sync
   ```

3. Also register the `finops-billing-api` **Endpoint** Service (`NO_SPEC` + `HTTP_JSON`) directly via REST `v1`:
   ```bash
   curl -fsSL -X POST \
     "http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/us-central1/services?serviceId=finops-billing-api" \
     -H "Content-Type: application/json" \
     -d '{
       "displayName": "FinOps Cloud Billing API Endpoint",
       "description": "External Cloud Billing REST API endpoint registered for governed agent egress policies.",
       "interfaces": [
         {"url": "https://cloudbilling.googleapis.com/v1", "protocolBinding": "HTTP_JSON"}
       ],
       "endpointSpec": {"type": "NO_SPEC"}
     }' | .venv/bin/python -m json.tool
   ```

4. Verify all projected read-only discovery resources and standalone `private-` skills:
   ```bash
   BASE="http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/us-central1"

   # Standalone Skills (observe 'private-' prefix!)
   curl -fsSL "${BASE}/skills" | .venv/bin/python -m json.tool

   # Projected read-only MCP Server (with readOnlyHint & destructiveHint annotations)
   curl -fsSL "${BASE}/mcpServers" | .venv/bin/python -m json.tool

   # Projected read-only A2A Agent (interfaces projected from supportedInterfaces)
   curl -fsSL "${BASE}/agents" | .venv/bin/python -m json.tool

   # Projected read-only Endpoint
   curl -fsSL "${BASE}/endpoints" | .venv/bin/python -m json.tool
   ```

5. Test the `A2A_AGENT_CARD` top-level `interfaces` guardrail (should return `400 INVALID_ARGUMENT`):
   ```bash
   curl -i -s -X POST \
     "${BASE}/services?serviceId=bad-a2a-test" \
     -H "Content-Type: application/json" \
     -d '{
       "displayName": "Invalid A2A Service",
       "interfaces": [{"url": "http://127.0.0.1:8092/a2a", "protocolBinding": "JSONRPC"}],
       "agentSpec": {"type": "A2A_AGENT_CARD", "content": "{\"supportedInterfaces\": [{\"url\": \"http://127.0.0.1:8092/a2a\"}]}"}
     }'
   ```

### Mode B: Live GCP Mode (`v1` REST, `gcloud`, and Terraform)

1. **Via `tools.plugin_sync` (REST `v1`):**
   ```bash
   export GCP_PROJECT_ID="<your-gcp-project-id>"
   export GCP_LOCATION="us-central1"
   export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"
   export AGENT_REGISTRY_AUTH_TOKEN="$(gcloud auth print-access-token)"

   .venv/bin/python -m tools.plugin_sync sync \
     --plugin-dir starter-marketplace/plugins/gcp-finops-toolkit \
     --registry-url "${AGENT_REGISTRY_BASE_URL}" \
     --project "${GCP_PROJECT_ID}" \
     --location "${GCP_LOCATION}" \
     --target-state TARGET_STATE_ACTIVE \
     --scanning-policy ENABLED_BLOCKING \
     --skip-mcp-probe
   ```

2. **Via Terraform (`terraform/core/`):**
   [terraform/core/agent_registry_services.tf](../../terraform/core/agent_registry_services.tf) manages the three GA `google_agent_registry_service` resources (`finops-mcp-server`, `finops-advisor-agent`, `finops-billing-api`) and reads back their projected IDs & URNs via `data.google_agent_registry_mcp_server`, `data.google_agent_registry_agent`, and `data.google_agent_registry_endpoint` using `basename(...registry_resource)`. Standalone Skills are bootstrapped cleanly via [terraform/core/skills_bootstrap.tf](../../terraform/core/skills_bootstrap.tf) (`terraform_data`) without overwriting Terraform-managed Services.

3. **Inspect live resources via `gcloud`:**
   ```bash
   gcloud agent-registry skills list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
   gcloud agent-registry mcp-servers list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
   gcloud agent-registry agents list --project="${GCP_PROJECT_ID}" --location="${GCP_LOCATION}"
   ```

---

## Step 3: Hands-On Coding Exercise (Optional Deep Dive)

Practice implementing `canonical_private_skill_id`, `enrich_skill_frontmatter`, `build_single_skill_zip`, and `register_skill_revision` in [starter/lab02_plugin_sync_exercise.py](../../starter/lab02_plugin_sync_exercise.py):

```bash
.venv/bin/python starter/lab02_plugin_sync_exercise.py
```

Fill in the `# WORKSHOP-TODO(lab-02)` blocks and re-run until all built-in self-checks print `[PASS]`. Compare against [tools/plugin_sync/sync.py](../../tools/plugin_sync/sync.py).

---

## Verification Checkpoint

- [ ] `GET .../skills` lists both `private-cost-anomaly-triage` and `private-bq-query-optimizer` in `STATE_ACTIVE` with `defaultRevisionFindingCount: 0`.
- [ ] `GET .../mcpServers` lists `finops-mcp-server` with 3 tools (`get_billing_summary`, `analyze_bq_query`, `delete_idle_dataset`) and their MCP annotations.
- [ ] `GET .../agents` lists `finops-advisor-agent` with projected `interfaces` from `supportedInterfaces`.
- [ ] `.venv/bin/pytest -v tests/test_track1a_sync_and_samples.py` passes completely.

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `404 NOT_FOUND` when calling `GET .../skills/cost-anomaly-triage` | Missing `private-` auto-prefix on private publisher skills | Always address the skill as `skills/private-cost-anomaly-triage` after creation. |
| `400 INVALID_ARGUMENT: Generic 'labels' map is not supported` | Attempted to pass a `labels` object to `Service` or `Skill` | Move custom string attributes into `SKILL.md` `frontmatter.metadata` (`map<string, string>`). |
| `400 INVALID_ARGUMENT: Top-level 'interfaces' MUST be empty` | Passed both top-level `interfaces` and `agentSpec.type = "A2A_AGENT_CARD"` | Omit top-level `interfaces`; declare endpoints inside `agentSpec.content.supportedInterfaces`. |

---

**Next Lab:** [Lab 03: Build & Deploy the Polyglot Marketplace Middleware →](03-build-deploy-middleware.md)
