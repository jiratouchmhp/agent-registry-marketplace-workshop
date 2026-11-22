# Dual-Source Ingestion Matrix & Customer Use Case 3 Alignment

This repository supports **two first-class ingestion paths** into Google Cloud Agent Registry (`v1` GA)—plus a clean graduation path to **push-based GitLab CI**. Every path feeds the exact same governed catalog, Polyglot Middleware (`:8080`), 4-IDE installer (`tools.hub_sync`), and ADK platform agents (`platform_agent/adk_orchestrator.py`).

---

## 1. Option 1 vs. Option 2 vs. Target-State GitLab CI

| Dimension | Option 1: Local Skills Path *(Default)* | Option 2: Centralized Git Repo (Pull Clone & Ingest) | Target State: Push-Based GitLab CI (`.gitlab-ci.yml`) |
| :--- | :--- | :--- | :--- |
| **Best For** | Classroom workshops, offline/air-gapped demos, rapid local iteration with zero external Git tokens | Customer Bring-Your-Own-Repo onboarding (GitLab / GitHub) without restructuring repos or editing upstream CI first | Production enterprise governance triggered on every MR and semver release tag (`v*.*.*`) |
| **Source Content** | Pre-prepared standalone skills in `local-skills/` (`cloud-run-debugger`, `terraform-iam-auditor`, `incident-postmortem-writer` -> `local-ops-toolkit`) **+** full `agent-plugins.org` bundle in `starter-marketplace/plugins/gcp-finops-toolkit/` | Any GitLab, GitHub, SSH, `file://`, or local Git repository containing plugins, standalone skills, or both | Authoring GitLab repository (`starter-marketplace/.gitlab-ci.yml`) |
| **Make Target** | `make demo-sync-local` *(or `make demo-sync` for `gcp-finops-toolkit` only)* | `make demo-sync-repo` *(override `SKILLS_REPO_*` vars)* | Automated by GitLab Runner stages (`validate` -> `package-and-sign` -> `register-registry`) |
| **CLI Command** | `.venv/bin/python -m tools.plugin_sync ingest --source-type local --path <dir> ...` | `.venv/bin/python -m tools.plugin_sync ingest --source-type git --repo-url <url> [--ref main] [--subdir <rel>] [--git-token-env GITLAB_TOKEN] ...` | Stage 3 runs `tools.plugin_sync sync` (`TARGET_STATE_DRAFT` on MR; `TARGET_STATE_ACTIVE` on release tag) |
| **Terraform Mode (`terraform/core/`)** | `skills_source_mode = "local"` (`enable_local_standalone_skills = true`, `local_skills_dir = "../../local-skills"`) | `skills_source_mode = "git"` (`skills_repo_url`, `skills_repo_ref`, `skills_repo_subdir`, `skills_repo_token_env`) | Set `enable_skills_bootstrap = false` so GitLab CI is the sole Skill publisher |
| **Provenance Recorded (`SKILL.md` `frontmatter.metadata`)** | Sanitized local `remote.origin.url` (or `local://<dir>`) + local `HEAD` SHA | Sanitized `source_repo` (embedded credentials stripped) + exact 40-char `git_commit` SHA from cloned `HEAD` | `$CI_PROJECT_URL` + `$CI_COMMIT_SHA` + Cloud KMS `EC_SIGN_P256_SHA256` signature |
| **Auth / Credentials** | None for source; ADC or `AGENT_REGISTRY_AUTH_TOKEN` for Live GCP | `--git-token-env <ENV_VAR_NAME>` injected via origin-scoped ephemeral `GIT_CONFIG_*` `http.<origin>/.extraHeader` (never in URLs/logs/`.tfvars`) | Keyless GCP Workload Identity Federation (`GCP_OIDC_TOKEN` -> `GCP_WORKLOAD_IDENTITY_PROVIDER` -> `GCP_SERVICE_ACCOUNT_EMAIL`) |

---

## 2. Auto-Detected Repository Layouts & Built-In Security Controls

Both Option 1 (`--source-type local`) and Option 2 (`--source-type git`) share `tools/plugin_sync/sources.py::discover_ingest_targets()`, which automatically classifies **5 repository layouts**:

| Layout Name | Directory Pattern Detected | Behavior on Ingest |
| :--- | :--- | :--- |
| `single_plugin` | `<target>/plugin.json` | Decomposes `skills/` into standalone `private-*` Skills + MCP/A2A `Service` resources (unless `--only-skills`). |
| `single_skill` | `<target>/SKILL.md` | Packages single skill; defaults `plugin_name` to sanitized skill directory name unless `--plugin-name` is set. |
| `multi_plugin` | `<target>/plugins/*/plugin.json` or `<target>/*/plugin.json` | Builds one `SyncPlan` per discovered plugin directory. |
| `standalone_skills` | `<target>/skills/*/SKILL.md` or `<target>/*/SKILL.md` | Groups all standalone skills under `--plugin-name` (default `local-ops-toolkit` / `central-repo-skills`), OR emits 1 bundle per skill with `--per-skill-plugin`. |
| `hybrid` | Both plugin directories and standalone skill directories under `<target>` | Runs plugin plans for every plugin AND standalone skill plan(s) for remaining skill directories in one `ingest` call. |

**Security guardrails enforced automatically in `tools/plugin_sync/sources.py`:**
- Shallow clone (`git clone --depth 1`) into an auto-cleaned `TemporaryDirectory`, with `-c protocol.ext.allow=never`, `-c core.hooksPath=/dev/null`, `GIT_TERMINAL_PROMPT=0`, and `ext::` URL blocking.
- Strict `--subdir` containment: rejects absolute paths, backslashes, NUL bytes, `..` traversal, and symlinks escaping the source root.
- Uppercase `SKILL.md` enforcement: gives an explicit diagnostic hint if lowercase `skill.md` is found.
- Brand-prefix sanitization: auto-prefixes derived plugin names starting with `antigravity`, `gemini`, `claude`, or `cursor` with `enterprise-` to satisfy Antigravity schema rules.

---

## 3. Exact `Makefile` Variables & CLI Flags Reference

### `Makefile` Dual-Source Variables & Defaults

| Variable | Default in `Makefile` | Notes |
| :--- | :--- | :--- |
| `PLUGIN_DIR` | `starter-marketplace/plugins/gcp-finops-toolkit` | Used by `demo-sync` and `demo-sync-local`. |
| `LOCAL_SKILLS_DIR` | `local-skills` | Used by `demo-sync-local` (Option 1B). |
| `LOCAL_SKILLS_PLUGIN_NAME` | `local-ops-toolkit` | Logical IDE plugin name grouping `local-skills/`. |
| `SKILLS_REPO_URL` | `https://github.com/jiratouchmhp/agent-registry-marketplace-workshop.git` | Target repo URL for `demo-sync-repo` (Option 2). |
| `SKILLS_REPO_REF` | `main` | Branch, tag, or commit SHA for `demo-sync-repo`. |
| `SKILLS_REPO_SUBDIR` | `local-skills` | **Override with `SKILLS_REPO_SUBDIR=""`** to auto-detect from a custom repo's root! |
| `SKILLS_REPO_PLUGIN_NAME` | `central-repo-skills` | Logical IDE plugin name for standalone skills discovered in Option 2. |
| `SKILLS_REPO_TOKEN_ENV` | *(empty)* | Name of env var holding a private Git token (e.g., `GITLAB_TOKEN`, `GITHUB_TOKEN`). |
| `SIGNING_KEY_PEM_PATH` | *(empty)* | Optional local ECDSA P-256 private key PEM passed as `--sign-pem` (Lab 07). |

### `python -m tools.plugin_sync ingest` Verified CLI Flags

| Flag | Applies To | Description / Default |
| :--- | :---: | :--- |
| `--source-type [local\|git]` | Both | Source mode (auto-inferred from `--path` vs. `--repo-url` if omitted). |
| `--path <dir>` | Option 1 | Local directory containing standalone skills and/or plugins. |
| `--repo-url <url-or-local-git>` | Option 2 | Git HTTPS/SSH/`file://` URL or local Git repo path to shallow-clone. |
| `--ref <branch\|tag\|sha>` | Option 2 | Branch, tag, or commit SHA to check out (supports fallback fetch for commit SHAs). |
| `--subdir <rel-path>` | Both | Relative POSIX subdirectory inside source root (traversal/symlink escapes blocked). |
| `--plugin-name`, `--plugin-display-name`, `--plugin-version` | Both | Bundle metadata for standalone skills (`--plugin-version` defaults to `1.0.0`). |
| `--per-skill-plugin` | Both | Emit 1 separate IDE plugin per standalone skill instead of grouping under `--plugin-name`. |
| `--git-token-env <VAR_NAME>` | Option 2 | Env var NAME holding private Git token (never pass the secret value directly). |
| `--only-skills` | Both | Ingest `Skill` resources only; skip MCP/A2A `Service` registration (**mandatory when `terraform/core` owns Services**). |
| `--registry-url`, `--project`, `--location`, `--skills-location`, `--auth-token` | Both | Env fallbacks: `AGENT_REGISTRY_BASE_URL`, `GCP_PROJECT_ID`, `GCP_LOCATION` (Services: `asia-southeast3`/`asia-southeast1`), `AGENT_REGISTRY_SKILLS_LOCATION` (live Skills: `global` default, or `us`/`eu` for CMEK), `AGENT_REGISTRY_AUTH_TOKEN`. |
| `--target-state`, `--scanning-policy`, `--wait-for-scan` | Both | Defaults: `TARGET_STATE_ACTIVE`, `ENABLED_BLOCKING`; pass `--wait-for-scan` on live GCP so initial `STATE_DRAFT` → `STATE_ACTIVE` AND idempotent re-deploy `SkillRevision` scans (`wait_for_revision_scan_completion()`) finish before promoting `defaultRevision` (`x-goog-user-project` is auto-attached for `googleapis.com`). |
| `--source-repo`, `--git-commit` | Both | Optional explicit provenance overrides injected into `SKILL.md` `frontmatter.metadata`. |
| `--sign-pem`, `--kms-key-version` | Both | Local ECDSA P-256 PEM or Cloud KMS `projects/.../cryptoKeyVersions/1` signing. |
| `--output-dir <dir>`, `--skip-mcp-probe`, `--dry-run` | Both | Write packaged ZIPs + `sync-plan.json` + `ingest-summary.json`; skip `:8091` probe; preview with zero API mutations. |

---

## 4. Terraform Bootstrap Configuration (`terraform/core/skills_bootstrap.tf`)

`terraform/core/skills_bootstrap.tf` uses `terraform_data.bootstrap_starter_skills` (because `hashicorp/google >= 7.39.0` has native GA resources for `google_agent_registry_service` and `google_agent_registry_binding`, but not yet `Skill`). It **always passes `--only-skills --skip-mcp-probe --wait-for-scan`** so `terraform/core/agent_registry_services.tf` stays the sole Terraform owner of Cloud Run Services and live scans settle cleanly.

### Option 1 (`terraform/core/terraform.tfvars` — Default)
```hcl
location                       = "asia-southeast3"
secondary_location             = "asia-southeast1"
skills_location                = "global" # APAC default (or "us"/"eu" for CMEK)
enable_skills_bootstrap        = true
skills_source_mode             = "local"
starter_plugin_dir             = "../../starter-marketplace/plugins/gcp-finops-toolkit"
enable_local_standalone_skills = true
local_skills_dir               = "../../local-skills"
local_skills_plugin_name       = "local-ops-toolkit"
skills_per_skill_plugin        = false
```

### Option 2 (`terraform/core/terraform.tfvars` — Centralized GitLab/GitHub Repo)
```hcl
enable_skills_bootstrap = true
skills_source_mode      = "git"
skills_repo_url         = "https://gitlab.example.com/ai-platform/agent-marketplace.git"
skills_repo_ref         = "main"
skills_repo_subdir      = "plugins"          # Set "" to auto-detect from repository root
skills_repo_token_env   = "GITLAB_TOKEN"     # Env var NAME only — NEVER put raw tokens in .tfvars!
skills_repo_plugin_name = "central-repo-skills"
skills_per_skill_plugin = false
```
*(In CI-owned production environments, set `enable_skills_bootstrap = false` and publish solely via GitLab CI.)*

---

## 5. Mapping to Customer Use Case 3's Four Enterprise Questions

Full architect brief: `docs/customer-usecase-3-brief.md`.

| Customer Question | Verified Architectural Answer in This Repo | Labs & Primary Sibling Skill |
| :--- | :--- | :--- |
| **Q1: Packaging Standard (`agent-plugins.org` vs. `agentskills.io`)** | **Decompose-on-Ingest, Re-Assemble-on-Egress:** Author full bundles in `agent-plugins.org` 1.0 (or lightweight standalone `agentskills.io` dirs in `local-skills/` / Git repos) -> `tools.plugin_sync ingest` splits into `agentskills.io` ZIPs (`private-*`, <= 1 MB, scalar `frontmatter.metadata`) + `Service` registrations -> Middleware synthesizes deterministic Polyglot 5-IDE ZIPs on egress. | Labs 01, 02, 03<br>`polyglot-agent-plugin-authoring`<br>`agent-registry-v1-ingest-and-lifecycle` |
| **Q2: Developer (4 IDEs) & Platform Agent (ADK) UX** | **Developers:** Polyglot Middleware (`/v1/marketplace/{claude,copilot,codex,antigravity}.json`, `/v1/plugins/*.zip`, `/mcp` gateway bridging `?alt=media` 302 header stripping) + 1-command `tools.hub_sync` (working around Antigravity's `github.com`-only remote URL limit and Claude HTTP relative-source rule).<br>**Platform Agents:** Direct `agentregistry.googleapis.com/v1` via ADK `AgentRegistry` + `GCPSkillRegistry` + `SkillToolset` + `GcpAuthProvider`. | Labs 03, 04, 05<br>`marketplace-middleware-and-ide-delivery` |
| **Q3: Security, Governance, KMS, Access Control & Audit** | **5 Defense-in-Depth Planes:** (1) `TARGET_STATE_DRAFT` quarantine + `ENABLED_BLOCKING` scan + Org Policy; (2) Native CMEK (`SkillEncryptionSpec`, encryption at rest in `us`/`eu` w/ zero-skills precondition) **+** Cloud KMS Asymmetric Signing (`EC_SIGN_P256_SHA256`) verified by Middleware; (3) 4 IAM roles (`viewer` only for middleware/agents) + per-`Skill` IAM + `AuthProvider`/`Binding`; (4) Agent Gateway + IAP v2 UAP CEL (`read_only_hint` ALLOW / `destructive_hint` DENY) + optional Antigravity `hooks.json`; (5) `ADMIN_READ` Data Access on `agentregistry` + `apphubendpoint` -> locked WORM Cloud Logging bucket. | Labs 06, 07, 08<br>`agent-registry-security-and-prod-hardening` |
| **Q4: GitLab vs. Agent Registry Source of Truth** | **Dual-Role Split + Progressive Adoption Ramp:** GitLab is the **Authoring SoT** (PR/MR review, history, 5-IDE schema + `agy plugin validate` gates); Agent Registry `v1` is the **Governed Runtime Catalog SoT** (immutable revisions, blocking scans, instant `STATE_DISABLED` kill switch, runtime discovery & audit). Adopt progressively: **Option 1 (Local)** -> **Option 2 (Pull Clone & Ingest)** -> **Push-Based GitLab CI (`.gitlab-ci.yml`)**. | Labs 02, 05<br>`agent-registry-v1-ingest-and-lifecycle` |
