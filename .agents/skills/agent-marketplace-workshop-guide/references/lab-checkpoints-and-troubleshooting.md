# Lab Checkpoints (Labs 00–08) & Fast Troubleshooting Matrix

Use this reference to navigate Workstream A (Labs 00–05, ~3.5 hours) and standalone Workstream B (Labs 06–08, ~2 hours), verify each lab's exact pass criteria, coach `starter/` exercises, and resolve common workshop issues rapidly.

---

## 1. Canonical Workshop Defaults (`WORKSHOP_SPEC.json`)

| Setting | Local Mock Default | Live GCP Production Placeholder |
| :--- | :--- | :--- |
| `AGENT_REGISTRY_BASE_URL` | `http://127.0.0.1:8090/v1` | `https://agentregistry.googleapis.com/v1` |
| `GCP_PROJECT_ID` | `acme-agent-governance` (project number `123456789012`) | `${GCP_PROJECT_ID}` |
| `GCP_LOCATION` | `us-central1` (CMEK multi-region: `us`) | `${GCP_LOCATION}` (CMEK: `us` or `europe` for `eu` skills) |
| `MIDDLEWARE_PUBLIC_URL` / `HUB_MIDDLEWARE_URL` | `http://127.0.0.1:8080` | `https://<cloud-run-middleware-url>` |
| Sample MCP Server / A2A Agent Ports | `:8091/mcp` / `:8092/a2a` (`make run-samples`) | Cloud Run URLs from `terraform/core` |
| Isolated Demo Home | `HUB_DEMO_HOME=/tmp/acme-agent-hub-demo-home` | `${HOME}` (after verifying `--dry-run`) |
| Mock Registry State Reset | `curl -fsSL -X POST http://127.0.0.1:8090/_mock/reset` | N/A (delete test skills with `?force=true`) |

---

## 2. Workstream A: Core Platform, Middleware & Developer UX (Labs 00–05)

| Lab | Duration | Key Files & Artifacts | Exact Verification Commands | Expected Pass Criteria |
| :---: | :---: | :--- | :--- | :--- |
| **00** | 20 min | `WORKSHOP_SPEC.json`, `Makefile`, `mock_registry/app.py`, `terraform/core/` | `make test && make validate-hcl`<br>Local: `curl -fsSL http://127.0.0.1:8090/healthz`<br>Live: `terraform -chdir=terraform/core output middleware_url` | All pytest tests & HCL formatting pass; `/healthz` returns `{"status": "ok", "service": "mock-agent-registry"}`; `/publishers` lists `FIRST_PARTY`, `THIRD_PARTY`, `PRIVATE`. |
| **01** | 35 min | `starter-marketplace/plugins/gcp-finops-toolkit/`, `local-skills/`, `starter/lab01_plugin_skeleton/`, `schemas/validators.py` | `.venv/bin/pytest -v tests/test_schemas.py`<br>Optional CLI: `agy plugin validate --json starter-marketplace/plugins/gcp-finops-toolkit` (+ multi-tool `jq` filter) | All 5 IDE validators report `valid=True, errors=[]` on `gcp-finops-toolkit`; all 3 `local-skills/*/SKILL.md` pass `validate_skill_md`; upgraded `starter/lab01_plugin_skeleton/` passes `validate_agent_plugin_bundle` & `validate_antigravity_bundle`. |
| **02** | 35 min | `tools/plugin_sync/cli.py`, `tools/plugin_sync/sources.py`, `tools/plugin_sync/sync.py`, `terraform/core/skills_bootstrap.tf`, `starter/lab02_plugin_sync_exercise.py` | `make demo-sync-local` (or `make demo-sync-repo`)<br>`curl -fsSL http://127.0.0.1:8090/v1/projects/acme-agent-governance/locations/us-central1/skills`<br>`.venv/bin/pytest -v tests/test_track1a_sync_and_samples.py tests/test_dual_skill_sources.py` | `skills` lists `private-cost-anomaly-triage`, `private-bq-query-optimizer`, plus Option 1 `private-cloud-run-debugger`, `private-terraform-iam-auditor`, `private-incident-postmortem-writer` (or Option 2 skills) in `STATE_ACTIVE` (`defaultRevisionFindingCount: 0`); `mcpServers` lists `finops-mcp-server` (3 tools); `agents` lists `finops-advisor-agent`. |
| **03** | 40 min | `middleware/app/main.py`, `middleware/app/registry_client.py`, `middleware/app/polyglot_builder.py`, `middleware/app/mcp_gateway.py`, `terraform/core/cloud_run.tf`, `starter/lab03_middleware_redirect_exercise.py` | `curl -fsSL http://127.0.0.1:8080/v1/catalog`<br>`curl -fsSL http://127.0.0.1:8080/v1/marketplace/{claude,copilot,codex,antigravity}.json`<br>`POST http://127.0.0.1:8080/mcp` (`load_verified_skill`, `call_remote_a2a_agent`)<br>`.venv/bin/pytest -v tests/test_track1b_middleware.py` | `?alt=media` 302 redirect returns `403` if `x-goog-user-project` leaks and `200` when headers are stripped; all 4 marketplace feeds return `gcp-finops-toolkit` with matching 64-char `sha256`; `/mcp` tools succeed. |
| **04** | 40 min | `tools/hub_sync/cli.py`, `tools/hub_sync/adapters.py`, `tools/hub_sync/hydration.py`, `starter/lab04_hub_sync_adapter_exercise.py` | `make demo-hub-sync`<br>`.venv/bin/python -m tools.hub_sync status --home /tmp/acme-agent-hub-demo-home --json`<br>`.venv/bin/pytest -v tests/test_track1c_hub_sync.py` | `gcp-finops-toolkit` shows `[OK]` with 64-char `sha256`; all 4 IDEs (`claude`, `copilot`, `codex`, `antigravity`) report `(wired, present)`; personal `my-personal-mcp` and `disabledTools` survive re-sync; `--bootstrap-mcp-only` wires `/mcp` with zero plugin directories on disk. |
| **05** | 40 min | `platform_agent/adk_orchestrator.py`, `platform_agent/rest_consumer.py`, `starter-marketplace/.gitlab-ci.yml` | `.venv/bin/python -m platform_agent.adk_orchestrator --mode adk --print-config`<br>`.venv/bin/python -m platform_agent.adk_orchestrator --mode rest --skill-query "cost anomaly" --mcp-query "finops" --agent-query "finops"`<br>`.venv/bin/pytest -v tests/test_track1d_platform_agent.py && make test-e2e` | `--print-config` confirms `GcpAuthProvider`, `roles/agentregistry.viewer` + `roles/agentidentity.user`, and `state=ACTIVE AND defaultRevisionFindingCount=0`; `--mode rest` loads `private-cost-anomaly-triage`; MR `TARGET_STATE_DRAFT` skills stay hidden in `rejectedSkills` until `TARGET_STATE_ACTIVE` promotion. |

---

## 3. Workstream B: Standalone Security & Governance Track (Labs 06–08)

> **Standalone Guarantee:** Workstream B requires **zero `terraform/core/` state**. Run offline against `make run-mock` (`:8090`, plus `make run-middleware` on `:8080` for Lab 07 Part 3), or apply `terraform/security_governance/` directly against any existing Google Cloud Agent Registry project.

| Lab | Duration | Key Files & Artifacts | Exact Verification Commands | Expected Pass Criteria |
| :---: | :---: | :--- | :--- | :--- |
| **06** | 40 min | `mock_registry/app.py`, `security-governance/org-policy/require-skill-scanning.yaml`, `require-skill-scanning-blocking.yaml`, `terraform/security_governance/org_policy.tf` | Upload poisoned skill (`AKIA...` + prompt injection) with `ENABLED_BLOCKING` -> query `GET .../findings?filter=resource="..."` -> attempt `?alt=media` download & `rev-1` promotion -> upload clean `rev-2`/`rev-3`, promote & roll back `defaultRevision` -> test `DISABLED`, `DEPRECATED` -> terminal `DECOMMISSIONED` | Poisoned upload auto-quarantines as `STATE_DRAFT` with `2` findings (`SECRET` + `VULNERABILITY`); `rev-1` download & promotion return `400 FAILED_PRECONDITION`; clean `rev-2`/`rev-3` roll back atomically; re-activating `DECOMMISSIONED` is rejected. |
| **07** | 40 min | `terraform/security_governance/kms.tf`, `skill_encryption_spec.tf`, `security-governance/kms/sign_and_verify.py`, `middleware/app/security_verifier.py` | `.venv/bin/python security-governance/kms/sign_and_verify.py keygen/sign/verify`<br>Start middleware with `REQUIRE_KMS_SIGNATURE=true KMS_PUBLIC_KEY_PEM_PATH=... make run-middleware`<br>`SIGNING_KEY_PEM_PATH=... make demo-sync` + upload unsigned rogue skill<br>`.venv/bin/pytest -v tests/test_workstream_b_security.py -k kms` | Signed ZIP passes `verify`; 1-byte tamper fails closed (non-zero exit); Middleware serves signed `gcp-finops-toolkit` (`kmsVerified: true`) and rejects unsigned `private-rogue-unsigned-skill` into `rejectedSkills`. |
| **08** | 40 min | `terraform/security_governance/auth_bindings.tf`, `agent_gateway_iap.tf`, `audit_logging.tf`, `security-governance/iap-uap/mcp-tool-annotations-uap.json`, `apply-uap.sh`, `security-governance/hooks/scripts/policy_guard.py`, `security-governance/audit/compliance-queries.md` | Test `GET .../bindings:fetchAvailable` with `-H "X-Mock-Allowed-Auth-Providers: ..."`<br>`bash security-governance/iap-uap/apply-uap.sh --project acme-agent-governance --location us-central1 --gateway-name agent-marketplace-gateway --dry-run`<br>Pipe `PreToolUse`/`PostToolUse` JSON into `policy_guard.py`<br>`.venv/bin/pytest -v tests/test_workstream_b_security.py` | Authorized caller receives 1 binding while unauthorized receives `[]`; `apply-uap.sh --dry-run` validates CEL rules; `policy_guard.py` allows read-only MCP tools, denies `destructiveHint: true` and `terraform destroy`, and logs redacted `arg_keys` only. |

---

## 4. Guided `starter/` Exercise Index (Labs 01–04)

| Lab | Starter Path | `# WORKSHOP-TODO` Functions / Targets | Built-In Self-Check Command | Reference Solution |
| :---: | :--- | :--- | :--- | :--- |
| **01** | `starter/lab01_plugin_skeleton/plugin.json`<br>`starter/lab01_plugin_skeleton/skills/hello-finops/SKILL.md` | Add `.antigravity-plugin/plugin.json`, 1:1 `assets/logo.svg`, `mcp_config.json`, `mcp.json`, `.claude-plugin/plugin.json`, `.mcp.json`, `.codex-plugin/plugin.json`, `rules/`, `hooks.json` + `0755` script | `.venv/bin/python -c 'from schemas import validate_agent_plugin_bundle, validate_antigravity_bundle; print(validate_agent_plugin_bundle("starter/lab01_plugin_skeleton")); print(validate_antigravity_bundle("starter/lab01_plugin_skeleton"))'` | `starter-marketplace/plugins/gcp-finops-toolkit/` |
| **02** | `starter/lab02_plugin_sync_exercise.py` | `canonical_private_skill_id()`, `enrich_skill_frontmatter()`, `build_single_skill_zip()`, `register_skill_revision()` | `.venv/bin/python starter/lab02_plugin_sync_exercise.py` | `tools/plugin_sync/sync.py` |
| **03** | `starter/lab03_middleware_redirect_exercise.py` | `strip_headers_for_cross_origin_redirect()`, `download_skill_revision_media()`, `assemble_polyglot_plugin_zip()` | `.venv/bin/python starter/lab03_middleware_redirect_exercise.py` | `middleware/app/registry_client.py`, `middleware/app/polyglot_builder.py` |
| **04** | `starter/lab04_hub_sync_adapter_exercise.py` | `atomic_write_json()`, `safe_merge_antigravity_mcp_config()` | `.venv/bin/python starter/lab04_hub_sync_adapter_exercise.py` | `tools/hub_sync/hydration.py`, `tools/hub_sync/adapters.py` |

---

## 5. Fast Troubleshooting Matrix (Verified Across Labs 00–08)

| Lab(s) | Exact Error / Symptom | Root Cause | Verified Fix |
| :---: | :--- | :--- | :--- |
| **00–08** | `ModuleNotFoundError` or stdlib `calendar` shadow error | Running system `python` or executing outside repository root | `cd` to repo root and invoke `.venv/bin/python`, `.venv/bin/pytest`, or `make <target>`. |
| **00, 03** | `Address already in use` on `:8090`, `:8091`, `:8092`, or `:8080` | Previous workshop server still running | Inspect with `lsof -i :<port>` and stop the stale process. |
| **00** | `terraform: command not found` | Terraform CLI not installed locally | Use Local Mock Mode (`make run-mock`; `make validate-hcl` falls back to `hclfmt`) or install Terraform `>= 1.6.0`. |
| **00** | `FAILED_PRECONDITION` / API not enabled during `terraform apply` | `agentregistry.googleapis.com` propagation lag after enablement | Wait ~60 seconds after `apis.tf` enables APIs and re-run `terraform apply`. |
| **01** | `additionalProperties` error in `.antigravity-plugin/plugin.json` | Unsupported field (e.g., `category`, `mcpServers`) in Antigravity manifest | Restrict keys to `schemas/antigravity_plugin_v1.schema.json`. |
| **01** | `globs must be a comma-separated string` | Wrote `globs: ["**/*.sql", "**/*.tf"]` as YAML array in `rules/*.md` | Change to scalar string `globs: "**/*.sql, **/*.tf"`. |
| **01** | `hooks.json` rejected for `${PLUGIN_ROOT}` or non-executable script | Used `${PLUGIN_ROOT}/scripts/...` or missing `0755` mode | Use relative `./scripts/...` paths and `chmod 0755 scripts/*.sh`. |
| **01** | `${ENV_VAR}` error in root `mcp.json` | Used `${API_TOKEN}` in remote `url`/`headers` of Agent Plugins 1.0 `mcp.json` | Remove `${VAR}` expansion; use OAuth / `google_credentials` or Agent Registry `Binding`. |
| **02** | `404 NOT_FOUND` on `GET .../skills/cost-anomaly-triage` | Missing `private-` auto-prefix | Address created private skills as `skills/private-<skill-id>` on all post-create calls. |
| **02** | `400 INVALID_ARGUMENT: Generic 'labels' map is not supported` | Passed `labels` object to `Service` or `Skill` | Store custom string provenance in `SKILL.md` `frontmatter.metadata` (`map<string, string>`). |
| **02** | `400 INVALID_ARGUMENT: Top-level 'interfaces' MUST be empty` | Sent top-level `interfaces` with `agentSpec.type = "A2A_AGENT_CARD"` | Omit top-level `interfaces`; put endpoints inside `agentSpec.content.supportedInterfaces`. |
| **02** | `Subdirectory 'local-skills' does not exist` on `make demo-sync-repo` | Default `SKILLS_REPO_SUBDIR ?= local-skills` doesn't exist in target repo | Override `SKILLS_REPO_SUBDIR=""` (auto-detect from repo root) or set exact subdir (`plugins`, `skills`). |
| **02** | `No plugins ('plugin.json') or standalone skills ('SKILL.md') discovered` | Lowercase `skill.md` or wrong `--subdir` | Rename to strictly uppercase `SKILL.md` and verify `--subdir` path. |
| **03** | HTTP `403` following `?alt=media` 302 redirect | Client forwarded `Authorization` or `x-goog-*` headers to GCS signed URL | Strip `Authorization` and all `x-goog-*` headers on cross-origin redirect hops. |
| **03, 07** | `/v1/catalog` shows `plugins: []` with entries in `rejectedSkills` | `REQUIRE_KMS_SIGNATURE=true` without a trusted public key, or skills are `STATE_DRAFT` / have findings | In Workstream A, start via `make run-middleware` (`REQUIRE_KMS_SIGNATURE=false`) and sync with `TARGET_STATE_ACTIVE`; in Lab 07, supply `KMS_PUBLIC_KEY_PEM_PATH` (or `KMS_SIGNING_KEY_VERSION` on Cloud Run). |
| **03** | `502 upstream_registry_unreachable` or `call_remote_a2a_agent` connection refused | `mock_registry` (`:8090`) or `samples` (`:8091`/`:8092`) not running | Start `make run-mock` (Terminal 1) and `make run-samples` (Terminal 2). |
| **04** | `agy plugin install` fails on HTTPS URL | Antigravity remote installs only accept `github.com` URLs | Hydrate locally via `python -m tools.hub_sync sync` and pass local directory `~/.acme-agent-hub/plugins/<name>`. |
| **04** | `401`/`403` against Cloud Run Middleware in Live GCP Mode | `allow_unauthenticated_workshop_demo = false` requires IAM invoker auth | Export `HUB_AUTH_TOKEN="$(gcloud auth print-identity-token)"` and grant `roles/run.invoker`. |
| **05** | `SkillSignatureError` in `adk_orchestrator --mode rest` | `REQUIRE_KMS_SIGNATURE=true` set in shell without matching public key PEM or signed ZIPs | Unset `REQUIRE_KMS_SIGNATURE` (or set `false`) for unsigned Workstream A runs, or pass `KMS_PUBLIC_KEY_PEM_PATH` after Lab 07. |
| **05** | `400 INVALID_ARGUMENT` on `agents:search` or `mcpServers:search` | Passed `searchType` field | Only `skills:search` accepts `searchType`; `agents:search` and `mcpServers:search` accept `{"searchString": "..."}` only. |
| **05** | `RuntimeError: Live ADK mode requires google-adk>=1.29.0` | Optional `platform_agent/requirements.txt` not installed | Install `.venv/bin/pip install -r platform_agent/requirements.txt`, or use `--mode rest` / `--print-config`. |
| **06** | `409 ALREADY_EXISTS` re-running Lab 06 Step 1 locally | `private-leaky-finops-skill` already in mock memory | Call `DELETE .../skills/private-leaky-finops-skill?force=true` or `POST http://127.0.0.1:8090/_mock/reset`. |
| **06** | `403 PERMISSION_DENIED` on `/findings` or `orgpolicy.customConstraints.create` | Missing `roles/agentregistry.admin` (for findings) or org-level `roles/orgpolicy.policyAdmin` | Grant `roles/agentregistry.admin` to security reviewers only; keep `enable_org_policy = false` if you lack org-level admin. |
| **07** | `FAILED_PRECONDITION` attaching `SkillEncryptionSpec` or EU KMS location mismatch | Existing skills already present in `us`/`eu` multi-region, or created KMS key ring in `eu` instead of `europe` | Delete all skills in that multi-region (`?force=true`) before enabling CMEK (zero-skills precondition); use KMS location `europe` for Agent Registry skills in `eu`. |
| **08** | `bindings:fetchAvailable` returns all bindings locally, or UAP rejects principal, or missing `apphubendpoint` audit logs | Omitted `X-Mock-Allowed-Auth-Providers`; used raw email instead of `principal://...`; only enabled Data Access on `agentregistry.googleapis.com` | Pass `-H "X-Mock-Allowed-Auth-Providers: <id>"` locally; use `principal://...` URI (never email or `spiffe://`); enable `ADMIN_READ` Data Access on **both** `agentregistry.googleapis.com` and `apphubendpoint.googleapis.com`. |
