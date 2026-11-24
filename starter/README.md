# Guided Starter Exercises (`starter/`)

This folder holds your hands-on coding exercises for **Labs 01–04**. Every exercise runs offline with `.venv/bin/python` (no Docker, Terraform, or GCP account required).

## Know Your Three Content Folders

- **`starter/`** *(this folder)*: Incomplete skeletons and `# WORKSHOP-TODO` Python files you edit.
- **[`starter-marketplace/`](../starter-marketplace/plugins/gcp-finops-toolkit/plugin.json)**: Working reference plugin repo (`gcp-finops-toolkit` + `.gitlab-ci.yml`). Peek here if you get stuck.
- **[`local-skills/`](../local-skills/README.md)**: Ready-to-sync standalone skills (`cloud-run-debugger`, `terraform-iam-auditor`, `incident-postmortem-writer`).

---

## Check Your Progress in One Command

Run this from the repository root at any time to see what is still `[TODO]` vs. `[PASS]`:

```bash
# Check your current edits across Labs 01-04
make starter-check

# Confirm the reference solutions pass every starter check (runs in a temp folder; never edits your files)
.venv/bin/python scripts/check_starter.py --verify-reference-solutions
```

Before you start editing, `make starter-check` shows `0/6` on Lab 01, `0/4` on Lab 02, `0/3` on Lab 03, and `0/3` on Lab 04.

---

## Labs 01–04: Exact Files, Functions & Reference Solutions

| Lab | Starter File(s) You Edit | `# WORKSHOP-TODO` Functions / Deliverables | Self-Check Command | Reference Solution |
| :--- | :--- | :--- | :--- | :--- |
| **[Lab 01](../docs/labs/01-author-agent-plugin.md)** | [`lab01_plugin_skeleton/plugin.json`](lab01_plugin_skeleton/plugin.json)<br>[`lab01_plugin_skeleton/skills/hello-finops/SKILL.md`](lab01_plugin_skeleton/skills/hello-finops/SKILL.md) | Add 6 Antigravity coexistence files inside `starter/lab01_plugin_skeleton/`: `.antigravity-plugin/plugin.json`, `assets/logo.svg`, `mcp_config.json`, `rules/AGENTS.md` + `rules/finops-guardrails.md`, `hooks.json`, and `chmod 0755` `scripts/finops-guard.sh` (`6/6`). | `make starter-check` | [`starter-marketplace/plugins/gcp-finops-toolkit/`](../starter-marketplace/plugins/gcp-finops-toolkit/plugin.json) |
| **[Lab 02](../docs/labs/02-register-agent-registry.md)** | [`lab02_plugin_sync_exercise.py`](lab02_plugin_sync_exercise.py) | Implement 4 `# WORKSHOP-TODO(lab-02)` functions (`4/4 [DONE]`):<br>• `canonical_private_skill_id`<br>• `enrich_skill_frontmatter`<br>• `build_single_skill_zip`<br>• `register_skill_revision` | `.venv/bin/python starter/lab02_plugin_sync_exercise.py` | [`tools/plugin_sync/sync.py`](../tools/plugin_sync/sync.py)<br>[`tools/plugin_sync/sources.py`](../tools/plugin_sync/sources.py) |
| **[Lab 03](../docs/labs/03-build-deploy-middleware.md)** | [`lab03_middleware_redirect_exercise.py`](lab03_middleware_redirect_exercise.py) | Implement 3 `# WORKSHOP-TODO(lab-03)` functions (`3/3 [DONE]`):<br>• `strip_headers_for_cross_origin_redirect`<br>• `download_skill_revision_media`<br>• `assemble_polyglot_plugin_zip` | `.venv/bin/python starter/lab03_middleware_redirect_exercise.py` | [`middleware/app/registry_client.py`](../middleware/app/registry_client.py)<br>[`middleware/app/polyglot_builder.py`](../middleware/app/polyglot_builder.py) |
| **[Lab 04](../docs/labs/04-ide-developer-experience.md)** | [`lab04_hub_sync_adapter_exercise.py`](lab04_hub_sync_adapter_exercise.py) | Implement 2 `# WORKSHOP-TODO(lab-04)` functions (`3/3 [DONE]` checks):<br>• `atomic_write_json`<br>• `safe_merge_antigravity_mcp_config` | `.venv/bin/python starter/lab04_hub_sync_adapter_exercise.py` | [`tools/hub_sync/hydration.py`](../tools/hub_sync/hydration.py)<br>[`tools/hub_sync/adapters.py`](../tools/hub_sync/adapters.py) |

### Quick Commands While You Work

**Check Lab 01 schema validation directly:**

```bash
.venv/bin/python - << 'EOF'
from schemas import validate_agent_plugin_bundle, validate_antigravity_bundle
print("Agent Plugins 1.0:", validate_agent_plugin_bundle("starter/lab01_plugin_skeleton"))
print("Antigravity v1   :", validate_antigravity_bundle("starter/lab01_plugin_skeleton"))
EOF
```

Out of the box, `validate_agent_plugin_bundle` passes while `validate_antigravity_bundle` flags the missing `logo` field. Follow [Lab 01](../docs/labs/01-author-agent-plugin.md) to add all 6 files until `make starter-check` reports `6/6`.

**Run Labs 02–04 individually as you edit each file:**

```bash
.venv/bin/python starter/lab02_plugin_sync_exercise.py
.venv/bin/python starter/lab03_middleware_redirect_exercise.py
.venv/bin/python starter/lab04_hub_sync_adapter_exercise.py
```

Each script tests one `# WORKSHOP-TODO` at a time, prints `Progress: X/N checks passed`, and shows `[DONE]` once every check passes.

---

## What About Labs 05–08?

Labs 05–08 have **no `starter/` coding stubs**. Instead, you run and inspect working scripts, policies, and Terraform configs already in the repo:

| Lab | Files You Run & Inspect | What You Practice |
| :--- | :--- | :--- |
| **[Lab 05](../docs/labs/05-platform-agents-gitlab-sot.md)** | [`platform_agent/adk_orchestrator.py`](../platform_agent/adk_orchestrator.py), [`platform_agent/rest_consumer.py`](../platform_agent/rest_consumer.py), [`starter-marketplace/.gitlab-ci.yml`](../starter-marketplace/.gitlab-ci.yml), [`scripts/validate_live_gcp.sh`](../scripts/validate_live_gcp.sh), [`scripts/destroy_gcp.sh`](../scripts/destroy_gcp.sh) | Querying Agent Registry from ADK/REST agents, simulating GitLab MR (`DRAFT`) vs. release tag (`ACTIVE`), and live GCP validation & teardown. |
| **[Lab 06](../docs/labs/06-governance-scanning-lifecycle.md)** | [`mock_registry/app.py`](../mock_registry/app.py), [`security-governance/org-policy/require-skill-scanning.yaml`](../security-governance/org-policy/require-skill-scanning.yaml), [`security-governance/org-policy/require-skill-scanning-blocking.yaml`](../security-governance/org-policy/require-skill-scanning-blocking.yaml), [`terraform/security_governance/org_policy.tf`](../terraform/security_governance/org_policy.tf) | Triggering `ENABLED_BLOCKING` findings, promoting and rolling back `defaultRevision`, decommissioning skills, and enforcing Org Policy. |
| **[Lab 07](../docs/labs/07-kms-encryption-and-signing.md)** | [`security-governance/kms/sign_and_verify.py`](../security-governance/kms/sign_and_verify.py), [`middleware/app/security_verifier.py`](../middleware/app/security_verifier.py), [`terraform/security_governance/kms.tf`](../terraform/security_governance/kms.tf), [`terraform/security_governance/skill_encryption_spec.tf`](../terraform/security_governance/skill_encryption_spec.tf) | Generating ECDSA P-256 keys, signing and verifying skill ZIPs, rejecting tampered uploads in Middleware, and reviewing CMEK vs. signing. |
| **[Lab 08](../docs/labs/08-access-control-and-audit.md)** | [`security-governance/iap-uap/mcp-tool-annotations-uap.json`](../security-governance/iap-uap/mcp-tool-annotations-uap.json), [`security-governance/iap-uap/apply-uap.sh`](../security-governance/iap-uap/apply-uap.sh), [`security-governance/hooks/hooks.json`](../security-governance/hooks/hooks.json), [`security-governance/hooks/scripts/policy_guard.py`](../security-governance/hooks/scripts/policy_guard.py), [`security-governance/audit/compliance-queries.md`](../security-governance/audit/compliance-queries.md), [`terraform/security_governance/auth_bindings.tf`](../terraform/security_governance/auth_bindings.tf), [`terraform/security_governance/agent_gateway_iap.tf`](../terraform/security_governance/agent_gateway_iap.tf), [`terraform/security_governance/audit_logging.tf`](../terraform/security_governance/audit_logging.tf) | Testing IAM roles, `AuthProvider` + `Binding`, IAP v2 UAP CEL rules, client-side hook guards, and WORM Cloud Audit queries. |
