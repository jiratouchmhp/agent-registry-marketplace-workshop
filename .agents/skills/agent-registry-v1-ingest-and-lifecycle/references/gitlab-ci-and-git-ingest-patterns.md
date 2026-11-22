# GitLab CI Dual-SoT & Dual-Source Git Ingest Patterns

Operational guide to the **Dual-Role Source of Truth** architecture (GitLab Authoring SoT + Google Cloud Agent Registry `v1` Runtime Catalog SoT), all 5 repository layouts auto-detected by `tools/plugin_sync/sources.py`, `--subdir` containment & private Git token security, standalone skill bundling flags, and migrating `starter-marketplace/.gitlab-ci.yml` to a standalone enterprise GitLab repository.

---

## 1. Dual-Role Source of Truth & Progressive Adoption Ramp

Never treat GitLab and Agent Registry as competing Sources of Truth — assign each the responsibility it excels at:

| Dimension | GitLab Repository (**Authoring SoT**) | Google Cloud Agent Registry `v1` (**Governed Runtime Catalog SoT**) |
|---|---|---|
| **Owns** | Source code, Markdown/manifest authoring, MR peer review, Git history, multi-IDE repo layouts | Immutable `SkillRevision` ZIPs + SHA-256 hashes, automated `ENABLED_BLOCKING` scanning, lifecycle states (`DRAFT` → `ACTIVE` → `DEPRECATED` → `DECOMMISSIONED`), IAM/IAP enforcement, Cloud Audit Logs |
| **Consumers** | Human authors, reviewers, GitLab CI runners | Cloud Run Polyglot Middleware, `tools.hub_sync`, ADK Platform Agents, Agent Gateway + IAP v2 UAP |
| **Emergency Containment** | Revert commit in Git for next release | Instant runtime kill switch (`PATCH ... targetState=TARGET_STATE_DISABLED`) or atomic `defaultRevision` rollback — no CI rebuild wait |

### Three-Stage Adoption Ramp on the Same Agent Registry Control Plane

1. **Stage 1 — Option 1 Local Skills Path (`--source-type local` / `make demo-sync-local` / `skills_source_mode = "local"`):** Zero external Git dependencies. Ingests `starter-marketplace/plugins/gcp-finops-toolkit` and/or pre-prepared standalone skills (`local-skills/`: `cloud-run-debugger`, `terraform-iam-auditor`, `incident-postmortem-writer`) directly from disk.
2. **Stage 2 — Option 2 Pull-Based Centralized Git Clone & Ingest (`--source-type git` / `make demo-sync-repo` / `skills_source_mode = "git"`):** Day-0 onboarding for existing GitLab/GitHub repositories without modifying their CI yet. Performs a shallow clone (`git clone --depth 1`), auto-detects layout, strips URL credentials, stamps the exact 40-char `HEAD` SHA into `frontmatter.metadata.git_commit`, and publishes to Agent Registry.
3. **Stage 3 — Target-State Push-Based GitLab CI (`starter-marketplace/.gitlab-ci.yml`):** Automated 3-stage governance pipeline triggered on Merge Requests (`TARGET_STATE_DRAFT` quarantine) and semver tags (`TARGET_STATE_ACTIVE` release) with keyless GitLab OIDC → GCP Workload Identity Federation and Cloud KMS signing.

---

## 2. All 5 `discover_ingest_targets` Repository Layouts (`tools/plugin_sync/sources.py`)

`discover_ingest_targets(root_path, subdir=...)` resolves `target_root` and evaluates layouts in strict precedence order:

| Priority | Layout Name | Directory Pattern Matched at `target_root` | Plans Emitted by `build_ingest_plans()` |
|---|---|---|---|
| **1** | `single_plugin` | `target_root/plugin.json` exists | 1 `SyncPlan` via `build_sync_plan()` (skills + MCP/A2A services unless `--only-skills`) |
| **2** | `single_skill` | `target_root/SKILL.md` exists | 1 `SyncPlan` via `build_standalone_skills_plans()`; default bundle name derives from the skill folder name unless `--plugin-name` is passed |
| **3** | `multi_plugin` | `target_root/plugins/*/plugin.json` and/or `target_root/*/plugin.json` (and no standalone skills found) | 1 `SyncPlan` per discovered plugin directory (sorted deterministically by resolved path) |
| **4** | `standalone_skills` | `target_root/skills/*/SKILL.md` and/or `target_root/*/SKILL.md` (and no plugins found) | 1 grouped `SyncPlan` under `--plugin-name` (default), OR *N* individual `SyncPlan`s when `--per-skill-plugin` is set |
| **5** | `hybrid` | Both plugin directories (`plugins/*/plugin.json` or `*/plugin.json`) **and** standalone skill directories (`skills/*/SKILL.md` or `*/SKILL.md`) | 1 `SyncPlan` per plugin directory **plus** 1 grouped (or *N* per-skill) `SyncPlan`(s) for the standalone skills |

### Example Trees for Every Layout

```text
# 1. single_plugin              # 2. single_skill                # 3. multi_plugin
my-plugin/                      cloud-run-debugger/              marketplace-repo/
├── plugin.json                 ├── SKILL.md                     └── plugins/
├── mcp.json                    └── references/                      ├── gcp-finops-toolkit/plugin.json
└── skills/                                                          └── secops-toolkit/plugin.json
    └── cost-anomaly-triage/SKILL.md

# 4. standalone_skills          # 5. hybrid
central-skills-repo/            enterprise-hub-repo/
└── skills/ (or directly *)     ├── plugins/
    ├── cloud-run-debugger/     │   └── gcp-finops-toolkit/plugin.json
    │   └── SKILL.md            └── skills/
    └── terraform-iam-auditor/      └── incident-postmortem-writer/
        └── SKILL.md                    └── SKILL.md
```

> **Lowercase Hint:** If no `plugin.json` or uppercase `SKILL.md` is found, `discover_ingest_targets()` specifically inspects for lowercase `skill.md` and raises a clear `PluginSyncError` reminding authors that Agent Registry requires strictly uppercase `SKILL.md`.

---

## 3. `--subdir` Containment & Git Clone Security Guardrails

Implemented in `clone_git_repository()` and `discover_ingest_targets()` (`tools/plugin_sync/sources.py`):

### A. `--subdir` Containment & Symlink Protection
- Rejects NUL bytes (`\x00`), backslashes (`\`), leading `/`, absolute paths, and any `..` path traversal segment.
- Resolves `(root_resolved / subdir).resolve()` and verifies `resolved.relative_to(root_resolved)` — blocking any symlink escape outside the source root.
- `_assert_contained_non_symlink()` verifies that discovered `plugin.json`, `mcp.json`, `mcp-tool-spec.json`, `skills/`, `agents/`, `agents/*/agent-card.json`, and `SKILL.md` paths are **not** symlinks and stay strictly contained inside `root_resolved`.
- Every packaged skill also passes `build_skill_zip()` + `validate_skill_zip()` (no symlinks inside ZIP, max nesting depth 8, ≤ 1 MB compressed, ≤ 10 MB uncompressed).

### B. Safe Shallow Cloning & Private Token Hygiene
- **Allowed URL schemes:** `https`, `http`, `ssh`, `file`, `git`, SCP-style SSH (`git@host:org/repo.git`), and local Git directory paths. Blocks `ext::` remotes, URLs/refs starting with `-`, and control characters (`CR`/`LF`/`NUL`).
- **Hardened `git` invocation:** Always runs with `-c protocol.ext.allow=never -c core.hooksPath=/dev/null`, `GIT_TERMINAL_PROMPT=0`, and a 60s timeout inside an auto-cleaned `tempfile.TemporaryDirectory(prefix="plugin-sync-clone-")`.
- **Ref resolution:** Tries `git clone --depth 1 [--branch <ref>]` first; if `<ref>` is a commit SHA or non-branch ref, falls back cleanly to `git init` → `git fetch --depth 1 origin <ref>` → detached `FETCH_HEAD` checkout (and full clone + detached checkout only if the server disallows shallow SHA fetch). Always verifies a 40-char `HEAD` SHA via `git rev-parse HEAD`.
- **Zero-leak private token handling (`--git-token-env ENV_VAR_NAME`):**
  - Reads `os.environ[ENV_VAR_NAME]` and passes `Authorization: Basic base64("oauth2:<token>")` **only** through ephemeral `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_N=http.<scheme>://<hostport>/.extraHeader` / `GIT_CONFIG_VALUE_N` environment variables.
  - Scoped strictly to the repository origin so cross-origin redirects cannot leak the token.
  - Never written to `.git/config`, CLI arguments, or disk; `_scrub_git_error()` masks raw tokens, base64 credentials, and URL userinfo from stderr/stdout on failure.
  - `sanitize_repo_url()` strips embedded `user:pass@` or `token@` before writing `frontmatter.metadata.source_repo` (preserving `git@host:org/repo.git`).

---

## 4. Standalone Skill Bundling: `--plugin-name` vs. `--per-skill-plugin`

Standalone `agentskills.io` directories have no `plugin.json`. `tools.plugin_sync ingest` attaches `plugin_name` and `plugin_display_name` metadata so the Polyglot Marketplace Middleware knows how to assemble IDE plugin bundles on egress:

| Mode | CLI Flag | Behavior | Best For |
|---|---|---|---|
| **Grouped Bundle** *(default)* | `--plugin-name <kebab-name>` `[--plugin-display-name "..."]` `[--plugin-version "1.0.0"]` | Groups all discovered standalone skills into one logical IDE plugin bundle (e.g., `local-ops-toolkit` containing all 3 `local-skills/`). | Cohesive domain toolkits (FinOps, Cloud Ops, SecOps) so developers install 1 bundle instead of many tiny plugins. |
| **One Plugin per Skill** | `--per-skill-plugin` | Emits a separate `SyncPlan` and logical `plugin_name` (sanitized from each skill's directory name) for every standalone skill. | Independent, loosely coupled skills installed individually across teams. |

### Default Name Derivation & Antigravity Safety
- If `--plugin-name` is omitted:
  - `single_skill` layout derives from the skill folder name.
  - `standalone_skills` / `hybrid` layout derives from `target_root.name` (local mode) or the sanitized repo name (git mode). Folders named `local-skills` / `local_skills` automatically map to `local-ops-toolkit`.
- `sanitize_kebab_name()` enforces `^[a-z0-9]+(-[a-z0-9]+)*$` (≤ 64 chars) and auto-prefixes `enterprise-` if a derived name starts with a disallowed Antigravity brand prefix (`antigravity`, `gemini`, `claude`, `cursor`).

### Quick Recipes (`Makefile` & Offline Local Git Test)

```bash
# 1. Default Makefile Git ingest (clones public workshop repo's `local-skills/` as `central-repo-skills`)
make demo-sync-repo

# 2. Override Makefile variables for a customer GitLab repo (use SKILLS_REPO_SUBDIR="" for repo root)
export GITLAB_TOKEN="<read-repository-token>"
make demo-sync-repo \
  SKILLS_REPO_URL="https://gitlab.example.com/ai-platform/agent-marketplace.git" \
  SKILLS_REPO_REF="main" \
  SKILLS_REPO_SUBDIR="" \
  SKILLS_REPO_TOKEN_ENV="GITLAB_TOKEN" \
  SKILLS_REPO_PLUGIN_NAME="enterprise-platform-skills"

# 3. Emit 1 plugin per standalone skill directly from local-skills/ (skipping Services)
.venv/bin/python -m tools.plugin_sync ingest \
  --source-type local --path local-skills --per-skill-plugin --only-skills \
  --registry-url http://127.0.0.1:8090/v1 --project acme-agent-governance --location asia-southeast3

# 4. Fully offline Option 2 Git clone-and-ingest smoke test (no internet or token needed)
.venv/bin/python -m tools.plugin_sync ingest \
  --source-type git --repo-url /tmp/central-skills-repo --ref main --subdir skills \
  --plugin-name central-sec-toolkit --only-skills --dry-run \
  --project acme-agent-governance --location asia-southeast3
```

---

## 5. Push-Based GitLab CI Pipeline (`starter-marketplace/.gitlab-ci.yml`)

### 3-Stage Governance Workflow

| Stage | Job | Trigger Rules | Key Enforcement |
|---|---|---|---|
| **1. `validate`** | `validate:schemas` | MR, `main`, Tags | Validates every plugin under `${RESOLVED_PLUGINS_DIR}/*` against all 5 targets (`agent-plugins-1.0`, `antigravity`, `claude-code`, `copilot`, `codex`) via `schemas.validators`. |
| **1. `validate`** | `validate:antigravity-cli` | MR, `main`, Tags | Runs `agy plugin validate --json` with the multi-tool `jq` filter (fails on any error or unexpected warning, ignoring expected multi-tool warnings for `.mcp.json`, `mcp.json`, `AGENTS.md`, and shadowed root `plugin.json`). |
| **2. `package-and-sign`** | `package-and-sign` | MR, `main`, Tags (`needs: validate:*`) | Runs `tools.plugin_sync sync --dry-run --output-dir dist/<plugin>` with `--kms-key-version` (or `--sign-pem`), attaching `CI_PROJECT_URL` + `CI_COMMIT_SHA` provenance and saving `dist/` artifacts for 14 days. |
| **3. `register-registry`** | `register:draft-mr` | `merge_request_event` | Publishes with `--target-state TARGET_STATE_DRAFT --scanning-policy ENABLED_BLOCKING`. Quarantines MR changes while security scans run; Middleware hides `STATE_DRAFT` skills from IDEs. |
| **3. `register-registry`** | `register:active-release` | Tag `^v[0-9]+\.[0-9]+\.[0-9]+` (auto) or `main` (`when: manual`) | Promotes clean, signed revisions to `--target-state TARGET_STATE_ACTIVE`. |

### Keyless GitLab OIDC → GCP Workload Identity Federation (Zero Static Keys)

`.ensure_gcp_registry_auth` exchanges GitLab's short-lived `id_tokens.GCP_OIDC_TOKEN` (`aud: "https://iam.googleapis.com/${GCP_WORKLOAD_IDENTITY_PROVIDER}"`) in pure Python `httpx` (no `gcloud` binary required inside `python:3.12-slim`):
1. `POST https://sts.googleapis.com/v1/token` (`grantType: urn:ietf:params:oauth:grant-type:token-exchange`, `subjectTokenType: urn:ietf:params:oauth:token-type:jwt`) → federated token.
2. `POST https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${GCP_SERVICE_ACCOUNT_EMAIL}:generateAccessToken` (`scope: ["https://www.googleapis.com/auth/cloud-platform"]`) → exported as `AGENT_REGISTRY_AUTH_TOKEN`.

Fallback order if not using WIF: `AGENT_REGISTRY_AUTH_TOKEN` → `GOOGLE_OAUTH_ACCESS_TOKEN`.

### Migrating `starter-marketplace/.gitlab-ci.yml` to a Standalone GitLab Repo

`.gitlab-ci.yml` supports **both** the workshop monorepo (`starter-marketplace/plugins/*` + local `pyproject.toml`) and a standalone GitLab repository rooted at `starter-marketplace/` (`plugins/*`). Configure these CI/CD variables in your standalone GitLab project:

| GitLab CI/CD Variable | Required in Standalone Repo? | Purpose & Example Value |
|---|---|---|
| `WORKSHOP_TOOLS_PACKAGE` | **Yes** *(unless pre-installed in runner image)* | Pip spec, private Artifact Registry wheel URL, or `git+https://` URL installing `schemas` and `tools.plugin_sync` (e.g., `git+https://github.com/jiratouchmhp/agent-registry-marketplace-workshop.git@main` or internal Artifact Registry wheel) |
| `PLUGINS_DIR` | Optional *(auto-detected)* | Leave empty to auto-detect `plugins` (standalone) or `starter-marketplace/plugins` (monorepo); set explicitly for custom folder paths (e.g., `catalog/plugins`) |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | **Yes** *(live GCP keyless auth)* | Full WIF provider path: `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/<POOL>/providers/<PROVIDER>` |
| `GCP_SERVICE_ACCOUNT_EMAIL` | **Yes** *(live GCP keyless auth)* | Least-privilege CI publisher SA (`roles/agentregistry.editor` + `roles/cloudkms.signerVerifier`): `agent-registry-ci-publisher@<PROJECT_ID>.iam.gserviceaccount.com` |
| `GCP_PROJECT_ID` / `GCP_LOCATION` / `AGENT_REGISTRY_SKILLS_LOCATION` | **Yes** *(override defaults)* | Target catalog project ID, regional Services location (`asia-southeast3`), and standalone Skills location (`global` default for APAC, or `us`/`eu` for CMEK) |
| `KMS_SIGNING_KEY_VERSION` | Recommended *(production signing)* | Cloud KMS `EC_SIGN_P256_SHA256` CryptoKeyVersion resource name (`projects/.../cryptoKeyVersions/1`); overrides `SIGNING_KEY_PEM_PATH` |
| `AGENT_REGISTRY_BASE_URL` / `SCANNING_POLICY` | Optional | Defaults: `https://agentregistry.googleapis.com/v1` and `ENABLED_BLOCKING` |

> **Ownership & Re-Deploy Scan Tip for Production CI:** When Terraform (`terraform/core/agent_registry_services.tf`) owns Cloud Run `google_agent_registry_service` registrations, replace `tools.plugin_sync sync` in `.gitlab-ci.yml` with `tools.plugin_sync ingest --source-type local --path "${plugin_dir}" --skills-location "${AGENT_REGISTRY_SKILLS_LOCATION:-global}" --wait-for-scan --only-skills --skip-mcp-probe` (and set `enable_skills_bootstrap = false` in `terraform/core/terraform.tfvars`) so Terraform exclusively manages regional `Service` endpoints while GitLab CI exclusively manages `global` (or `us`/`eu`) `Skill` revisions without hitting `ENABLED_BLOCKING` pending-scan promotion errors.
