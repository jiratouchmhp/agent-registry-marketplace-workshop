# Customer Use Case 3: Enterprise Agent Marketplace Executive & Architect Brief

This brief directly answers the **four core architectural questions** for building a unified **Enterprise Agent Marketplace** on **Google Cloud Agent Registry (`v1` GA)** serving both **developer coding IDEs/CLIs** (Claude Code, OpenAI Codex, VS Code / GitHub Copilot, Google Antigravity) and **autonomous platform agents** (Vertex AI Agent Engine / Cloud Run with Google ADK).

Every recommendation below is backed by **verified GA API schemas** and **runnable code, Terraform, and hands-on labs** in this repository.

---

## Executive Summary Matrix

| Question | Core Decision | Verified Implementation in This Repo | Deep-Dive Lab |
| :--- | :--- | :--- | :--- |
| **Q1: Packaging Standard (`agent-plugins.org` vs. `agentskills.io`)** | **Author in `agent-plugins.org` 1.0 (full plugins) OR standalone `agentskills.io` (`local-skills/` or existing Git repos) → Auto-detect & decompose on ingest (`tools.plugin_sync ingest`) → Re-assemble Polyglot 5-IDE ZIPs on egress.** | [tools/plugin_sync/sources.py](../tools/plugin_sync/sources.py), [tools/plugin_sync/sync.py](../tools/plugin_sync/sync.py), [middleware/app/polyglot_builder.py](../middleware/app/polyglot_builder.py), [schemas/validators.py](../schemas/validators.py) | [Lab 01](labs/01-author-agent-plugin.md), [Lab 02](labs/02-register-agent-registry.md), [Lab 03](labs/03-build-deploy-middleware.md) |
| **Q2: Developer & Platform Agent UX** | **Developers:** 1-command `tools.hub_sync` + Polyglot Middleware `/v1/marketplace/{tool}.json` + Streamable HTTP `/mcp` gateway.<br>**Platform Agents:** Direct `agentregistry.googleapis.com/v1` via ADK `AgentRegistry` + `GCPSkillRegistry` + `SkillToolset`. | [tools/hub_sync/cli.py](../tools/hub_sync/cli.py), [middleware/app/mcp_gateway.py](../middleware/app/mcp_gateway.py), [platform_agent/adk_orchestrator.py](../platform_agent/adk_orchestrator.py) | [Lab 04](labs/04-ide-developer-experience.md), [Lab 05](labs/05-platform-agents-gitlab-sot.md) |
| **Q3: Security, Governance & Audit** | **Defense-in-depth across 5 planes:** `DRAFT` quarantine + `ENABLED_BLOCKING` scan + Org Policy; Cloud KMS CMEK (at rest) **+** Asymmetric `EC_SIGN_P256_SHA256` signing (supply chain); 4 IAM roles + Skill IAM + `Binding`; Agent Gateway + IAP v2 UAP CEL; WORM Audit Logs. | [terraform/security_governance/](../terraform/security_governance/kms.tf), [security-governance/kms/sign_and_verify.py](../security-governance/kms/sign_and_verify.py), [security-governance/iap-uap/mcp-tool-annotations-uap.json](../security-governance/iap-uap/mcp-tool-annotations-uap.json), [security-governance/audit/compliance-queries.md](../security-governance/audit/compliance-queries.md) | [Lab 06](labs/06-governance-scanning-lifecycle.md), [Lab 07](labs/07-kms-encryption-and-signing.md), [Lab 08](labs/08-access-control-and-audit.md) |
| **Q4: GitLab vs. Agent Registry Source of Truth** | **Dual-Role Split with Progressive Onboarding:** Start via **Option 1 (Local Skills)** or **Option 2 (Pull Clone & Ingest of an existing GitLab/GitHub repo)**, then graduate to push-based GitLab CI (`.gitlab-ci.yml`) as Authoring SoT while Google Cloud Agent Registry (`v1`) stays the Governed Runtime SoT. | [starter-marketplace/.gitlab-ci.yml](../starter-marketplace/.gitlab-ci.yml), [tools/plugin_sync/cli.py](../tools/plugin_sync/cli.py), [tools/plugin_sync/sources.py](../tools/plugin_sync/sources.py) | [Lab 02](labs/02-register-agent-registry.md), [Lab 05](labs/05-platform-agents-gitlab-sot.md) |

---

## Q1: `agent-plugins.org` vs. `agentskills.io` — Which Standard Should We Adopt?

### Short Answer
Adopt **both** in a clean **Decompose-on-Ingest, Re-Assemble-on-Egress** architecture:
1. **Authoring & IDE Distribution Format**: **Agent Plugins 1.0** (`https://agent-plugins.org/`) enriched with multi-tool coexistence files for Google Antigravity, Claude Code, VS Code/Copilot, and OpenAI Codex.
2. **Google Cloud Agent Registry Storage Format**: **`agentskills.io`** for standalone `Skill` resources (`Skill.type = "SIMPLE"`), paired with native `Service` registrations for `McpServer` (`TOOL_SPEC`), `Agent` (`A2A_AGENT_CARD`), and `Endpoint` (`NO_SPEC`).

### Why Agent Registry Requires Decomposition
Google Cloud Agent Registry (`v1` GA) does **not** accept multi-skill `agent-plugins.org` plugin ZIPs as a single `Skill` object. Every `SkillRevision` upload strictly enforces:
- **Strictly ONE uppercase `SKILL.md`** at archive root (`SKILL.md`) or inside a single top-level directory (`<dir>/SKILL.md`).
- **Size & safety limits**: Compressed ZIP ≤ **1 MB** (≤ 500 KB recommended for inline `archiveUploadSource`; up to 1 MB via `gcsSource`), uncompressed total ≤ **10 MB**, individual file ≤ **1 MB**, max nesting depth **8**, zero symlinks, zero `..` traversal or absolute paths.
- **No generic `labels` map** on `Skill`, `Service`, `Agent`, `McpServer`, `Endpoint`, or `Binding` in `v1`. Instead, `SKILL.md` YAML frontmatter supports `metadata: map<string, string>` (scalar string values only).
- **Automatic `private-` ID prefix & verified regional split**: Uploading `skillId=cost-anomaly-triage` into a live Skills location (`global` default for APAC, or `us`/`eu` for CMEK) creates `projects/{p}/locations/global/skills/private-cost-anomaly-triage` under the `PRIVATE` publisher (`urn:skill:projects-{NUM}:locations:global:private-cost-anomaly-triage`), while regional `Services` (`McpServer`, `Agent`, `Endpoint`) run in Bangkok (`asia-southeast3`) primary and Singapore (`asia-southeast1`) secondary/DR. Middleware (`INCLUDE_PUBLIC_SKILLS=false`) automatically filters to `private-*` skills so built-in Google first-party skills do not pollute enterprise catalogs.

### How Decomposition & Polyglot Re-Assembly Work Together

| Stage | Artifact / Resource | Linkage Mechanism | Code Reference |
| :--- | :--- | :--- | :--- |
| **1. Author (Option 1 Local or Option 2 Git Repo)** | Full `agent-plugins.org` bundles (`starter-marketplace/plugins/gcp-finops-toolkit/`), lightweight standalone `agentskills.io` directories ([local-skills/](../local-skills/README.md)), or an existing centralized GitLab/GitHub repo | `tools.plugin_sync ingest` auto-detects `single_plugin`, `multi_plugin`, `single_skill`, `standalone_skills`, and `hybrid` layouts without forcing repo restructuring | [starter-marketplace/plugins/gcp-finops-toolkit/plugin.json](../starter-marketplace/plugins/gcp-finops-toolkit/plugin.json), [local-skills/README.md](../local-skills/README.md) |
| **2. Ingest (`plugin_sync`)** | Splits plugins into standalone `agentskills.io` ZIPs (`private-*`) + `McpServer`/`A2A_AGENT_CARD` `Service` resources, and groups standalone skills under `--plugin-name` (or `--per-skill-plugin`) | Injects scalar string keys into each `SKILL.md` `frontmatter.metadata`: `plugin_name`, `plugin_display_name`, `plugin_version`, `source_repo` (credentials stripped), `git_commit` (exact 40-char HEAD SHA), `mcp_service_id`, `agent_service_id`, `kms_signature`, `kms_key_version` | [tools/plugin_sync/sync.py](../tools/plugin_sync/sync.py), [tools/plugin_sync/sources.py](../tools/plugin_sync/sources.py) |
| **3. Egress (Middleware)** | Queries `STATE_ACTIVE` skills with `findings=0`, downloads `?alt=media`, verifies SHA-256 + KMS ECDSA P-256 signature, groups by `metadata.plugin_name`, and builds a deterministic **Polyglot Single-Bundle ZIP** | Single ZIP passes validators for Agent Plugins 1.0, Antigravity, Claude Code, Copilot, and Codex simultaneously — regardless of whether skills were ingested from Option 1 local directories or Option 2 cloned Git repositories | [middleware/app/polyglot_builder.py](../middleware/app/polyglot_builder.py) |

> **Result:** Skill authors get modular, reusable `agentskills.io` units (independently scannable, versionable, and discoverable by ADK platform agents via `skills:search`), while IDE developers install cohesive plugins bundling skills + MCP servers + subagents + guardrails.

---

## Q2: Developer UX (4 IDEs) & Platform Agent UX (ADK)

### Why Not Point Developer IDEs Directly at `https://agentregistry.googleapis.com/mcp`?
Google Cloud Agent Registry exposes a Preview Remote MCP server at `https://agentregistry.googleapis.com/mcp` (Streamable HTTP, OAuth 2.0 Bearer only, requiring `roles/mcp.toolUser` + an Agent Registry role). However:
1. It exposes **metadata discovery tools only**—it has **NO tool to download skill ZIP bytes** (`?alt=media`).
2. It does **not** emit IDE-specific marketplace JSON feeds or Polyglot plugin ZIPs.
3. Calling `?alt=media` directly returns an **HTTP 302 redirect** that requires nuanced header handling: live Agent Registry redirects to a same-origin authenticated Scotty endpoint (`https://agentregistry.googleapis.com/download/v1/...`, where stripping `Authorization` causes `401 CREDENTIALS_MISSING`), whereas cross-origin or signed-URL redirects (`storage.googleapis.com` / `/scotty-signed/` / `X-Goog-Signature`) return `403` if `Authorization` or `x-goog-*` headers leak.

Our **Polyglot Marketplace Middleware** ([middleware/app/main.py](../middleware/app/main.py)) bridges all three gaps cleanly, offering both **4 IDE-native marketplace feeds + 1-command `hub_sync` hydration** AND a **governed Streamable HTTP `/mcp` gateway** (`search_catalog`, `load_verified_skill`, `list_governed_mcp_servers`, `call_remote_a2a_agent`).

### Developer IDE / CLI Support Matrix

| IDE / CLI Target | Plugin Manifest & MCP File | Marketplace Feed / Local Path | Onboarding UX & Verified Engineering Realities |
| :--- | :--- | :--- | :--- |
| **Google Antigravity** | `.antigravity-plugin/plugin.json` (takes precedence over root `plugin.json`; strict schema `additionalProperties: false`), `mcp_config.json` (ignores `.mcp.json`/`mcp.json`), `assets/logo.svg` (1:1), `rules/` (comma-separated `globs` string), `hooks.json` (`./scripts/...` relative paths, `0755`) | Middleware: `/v1/marketplace/antigravity.json` (archive mode w/ `sha256`)<br>Local: `~/.gemini/config/plugins/<name>` & `~/.gemini/config/mcp_config.json` | **`github.com`-only remote URL reality:** `agy plugin install <url>` only accepts `github.com` remote URLs—enterprise GitLab / Cloud Run URLs MUST be hydrated locally first by `tools.hub_sync`, which copies atomically and runs `agy plugin install <local-dir>`. CI runs `agy plugin validate --json` with the multi-tool `jq` warning filter (see [Lab 01](labs/01-author-agent-plugin.md)). |
| **Claude Code** | `.claude-plugin/plugin.json`, `.mcp.json` (`type: "streamable-http"`), `agents/<name>.md`, `skills/` | Middleware: `/v1/marketplace/claude.json`<br>Local: `<hub>/.claude-plugin/marketplace.json` wired into `~/.claude/settings.json` (`extraKnownMarketplaces` + `enabledPlugins`) | HTTP URL marketplaces forbid relative `./` plugin sources; local directory & Git marketplaces allow `./plugins/<name>`. `hub_sync` writes a local directory marketplace and registers it in `~/.claude/settings.json` while preserving user toggles. |
| **VS Code & GitHub Copilot** | Root `plugin.json` (`agent-plugins.org` 1.0 `$schema` takes priority #1), root `mcp.json`, `com.github.copilot/agents/<name>.agent.md`, `skills/` | Middleware: `/v1/marketplace/copilot.json`<br>Local: `<hub>/marketplace.json` + `<hub>/.github/plugin/marketplace.json` + `<hub>/vscode-settings-snippet.json` | `hub_sync` generates a copy-pasteable `vscode-settings-snippet.json` (`chat.plugins.enabled`, `chat.plugins.marketplaces`, `chat.pluginLocations`), optionally applies it via `--apply-vscode`, and prints `copilot plugin marketplace add` / `install` commands. |
| **OpenAI Codex** | Root `plugin.json` (`extensions["com.openai"]`) + `.codex-plugin/plugin.json`, `~/.codex/agents/<name>.toml`, `~/.codex/config.toml` (`[mcp_servers.<key>]`) | Middleware: `/v1/marketplace/codex.json`<br>Local: `~/.agents/plugins/marketplace.json` + symlinks under `~/.agents/plugins/plugins/<name>` | `hub_sync` merges `[mcp_servers.<key>]` into `~/.codex/config.toml` without clobbering user entries and materializes `.toml` agents and plugin symlinks atomically. |

### Zero-Friction Developer Onboarding & Multi-Region Failover (1 Command)
A developer onboards all 4 IDEs in one idempotent, ownership-tracked command — optionally supplying `--fallback-middleware-url` for seamless **Bangkok (`asia-southeast3`) → Singapore (`asia-southeast1`)** failover:

```bash
python -m tools.hub_sync sync \
  --middleware-url https://<bangkok-middleware-url> \
  --fallback-middleware-url https://<singapore-middleware-url> \
  --tools all
```

Or zero-disk **Bootstrap MCP-Only Mode** (wires only the `/mcp` gateway into IDEs so agents fetch verified skills on demand via `load_verified_skill`):

```bash
python -m tools.hub_sync sync --middleware-url https://<middleware-url> --bootstrap-mcp-only
```

### Platform Agent UX (Google ADK on Vertex AI Agent Engine / Cloud Run)
Headless platform agents do **not** need IDE plugin wrappers—they connect **directly to `agentregistry.googleapis.com/v1`** using `google-adk >= 1.29.0` ([platform_agent/adk_orchestrator.py](../platform_agent/adk_orchestrator.py)):
1. Register `CredentialManager.register_auth_provider(GcpAuthProvider())` so ADK automatically resolves Agent Registry `Binding` credentials.
2. Instantiate `AgentRegistry(project_id, location)` (e.g., Bangkok `asia-southeast3` or Singapore `asia-southeast1`) to discover and materialize regional `get_mcp_toolset(...)` and `get_remote_a2a_agent(...)`.
3. Instantiate `SkillToolset(registry=GCPSkillRegistry(project_id, location="global"))` (or `us`/`eu` when CMEK is enabled) to run `POST .../skills:search` (`searchType: "SEMANTIC"`, `filter: "state=ACTIVE AND defaultRevisionFindingCount=0"`, filtering to `private-*` enterprise skills) and load verified `SKILL.md` bundles via `?alt=media`.
4. Note the **project scoping** boundary: Standalone `Skill` resources are strictly project-scoped (they do not roll up across folder/org boundaries), so platform agents point to the governed hub project.

---

## Q3: Governance, Security, KMS, Access Control & Audit

### 1. Approval Workflow & Skill Lifecycle Governance
- **Quarantine by default**: Merge Requests publish skills with `targetState = TARGET_STATE_DRAFT` (`STATE_DRAFT`) so reviewers and security teams can inspect scans before developers ever see the skill.
- **Automated blocking scans**: Every skill sets `scanningConfig.policy = "ENABLED_BLOCKING"`. Even if created with `TARGET_STATE_ACTIVE`, Agent Registry keeps the skill quarantined until scanning completes with **0 findings**. Any `SECRET` (e.g., `AKIA*`, `AIza*`, `ghp_*`, private keys) or `VULNERABILITY` (e.g., prompt injection) blocks download and `defaultRevision` promotion.
- **Finding triage**: Security engineers (`roles/agentregistry.admin`) inspect findings via `GET /v1/projects/{p}/locations/{l}/findings?filter=resource="..."`.
- **Org Policy backstop**: Deploy the Custom Organization Policy Constraint ([security-governance/org-policy/require-skill-scanning.yaml](../security-governance/org-policy/require-skill-scanning.yaml) or strict allow-only [require-skill-scanning-blocking.yaml](../security-governance/org-policy/require-skill-scanning-blocking.yaml)) targeting `agentregistry.googleapis.com/Skill` on `CREATE`/`UPDATE` so no team can disable scanning.
- **Immutable revisions & safe deprecation**: Every upload creates an immutable `SkillRevision` with server-computed `sha256Hash`. Rollback is an atomic `PATCH ...?updateMask=defaultRevision`. Lifecycle follows `STATE_DRAFT` → `STATE_ACTIVE` → `STATE_DEPRECATED` (downloadable with warnings) → `STATE_DECOMMISSIONED` (**terminal and irreversible**, permitted **only** after `DEPRECATED`). `STATE_DISABLED` provides an instant reversible emergency kill switch.
- **Publisher trust tiers**: Read-only `GET .../publishers` distinguishes `FIRST_PARTY` (`google-`), `THIRD_PARTY`, and `PRIVATE` (`private-`) publishers.

### 2. Cloud KMS: Native CMEK vs. Cryptographic Artifact Signing
A critical distinction often conflated in architecture reviews:

| Capability | Native Agent Registry CMEK (`SkillEncryptionSpec`, Preview) | Custom Cloud KMS Asymmetric Signing (CI + Middleware) |
| :--- | :--- | :--- |
| **Security Purpose** | **Encryption at rest ONLY** (key sovereignty / crypto-shredding of stored Skill packages). Does **not** sign skills. | **Supply-chain provenance & tamper detection** (proves an artifact was built/approved by trusted CI and unmodified). |
| **KMS Key Type** | Symmetric `ENCRYPT_DECRYPT` (`GOOGLE_SYMMETRIC_ENCRYPTION`), 90-day rotation | Asymmetric `ASYMMETRIC_SIGN` (`EC_SIGN_P256_SHA256`) |
| **Location Rules** | **Multi-region ONLY**: Skills in `us` → KMS key in `us`; Skills in `eu` → KMS key in `europe`. *(For APAC deployments with standalone Skills in `global` and regional compute/Services in `asia-southeast3`/`asia-southeast1`, rely on Google-managed encryption at rest plus regional Asymmetric Signing.)* | Regional — natively supported in **Bangkok (`asia-southeast3`)** and **Singapore (`asia-southeast1`)** matching CI/Middleware region |
| **Critical Precondition** | **Zero-skills precondition:** `PATCH .../skillEncryptionSpec` fails with `FAILED_PRECONDITION` if **any** Skill already exists in that multi-region! | Can be enabled/rotated anytime via `kms_key_version` |
| **Least-Privilege IAM** | Agent Registry P4SA (`service-${PROJECT_NUMBER}@gcp-sa-agentregistry.iam.gserviceaccount.com`) gets `roles/cloudkms.cryptoKeyEncrypterDecrypter` | CI Publisher SA gets `roles/cloudkms.signerVerifier`; Middleware SA gets **ONLY** `roles/cloudkms.publicKeyViewer` |
| **Repo Implementation** | [terraform/security_governance/kms.tf](../terraform/security_governance/kms.tf), [skill_encryption_spec.tf](../terraform/security_governance/skill_encryption_spec.tf) | [security-governance/kms/sign_and_verify.py](../security-governance/kms/sign_and_verify.py), [middleware/app/security_verifier.py](../middleware/app/security_verifier.py) |

### 3. RBAC, Delegated Auth Bindings, Runtime Tool Governance & Client Hooks
- **4 Agent Registry IAM Roles** ([terraform/core/iam.tf](../terraform/core/iam.tf)):
  - `roles/agentregistry.viewer` — Read-only discovery & active skill download (**assign ONLY this to Middleware and runtime Platform Agents**).
  - `roles/agentregistry.user` — Viewer + standalone `Skill` CRUD (skill authors).
  - `roles/agentregistry.editor` — Viewer + `Service` & `Skill` CRUD (CI/CD publisher SA only).
  - `roles/agentregistry.admin` — Full control + `Binding`, `Finding`, per-`Skill` IAM (`skills add-iam-policy-binding`), and `SkillEncryptionSpec` (platform security admins only).
  - **Rule:** *Never grant `editor` or `admin` to runtime agents or serving middleware.*
- **Delegated Tool Credentials (`AuthProvider` + `Binding`)**: Provision `google_agent_identity_auth_provider` + `google_agent_registry_binding` ([terraform/security_governance/auth_bindings.tf](../terraform/security_governance/auth_bindings.tf)). In APAC deployments, host the `AuthProvider` in Singapore (`auth_provider_location = "asia-southeast1"`) while regional Services/Bindings run in Bangkok (`asia-southeast3`) and standalone Skills live in `global`. At runtime, `GET .../bindings:fetchAvailable` automatically filters out bindings whose `AuthProvider` the calling agent lacks `roles/agentidentity.user` on.
- **Runtime Tool-Annotation Enforcement (Agent Gateway + IAP v2 UAP)**: Attach a Google-managed `google_network_services_agent_gateway` (`CLIENT_TO_AGENT`, currently available in `us-central1` / `europe-west1`; toggle via `enable_agent_gateway = false` in APAC-only workshops) + `roles/iap.egressor` ([terraform/security_governance/agent_gateway_iap.tf](../terraform/security_governance/agent_gateway_iap.tf)) and enforce IAM v3 Unified Access Policies ([security-governance/iap-uap/mcp-tool-annotations-uap.json](../security-governance/iap-uap/mcp-tool-annotations-uap.json)) on `iap.googleapis.com/resources.egressViaIAP`:
  - **ALLOW** when `destination.is_registered == true && destination.agent_registry.resource_type == 'MCP_SERVER' && destination.agent_registry.mcp_server.tool.annotations.read_only_hint == true`
  - **DENY** when `destination.agent_registry.mcp_server.tool.annotations.destructive_hint == true`
- **Client-Side Defense-in-Depth (Optional)**: Antigravity `hooks.json` + [security-governance/hooks/scripts/policy_guard.py](../security-governance/hooks/scripts/policy_guard.py) blocks destructive MCP tools and dangerous shell commands locally in `PreToolUse` and logs redacted telemetry in `PostToolUse`.

### 4. Immutable Audit Logging & Compliance
Configured in [terraform/security_governance/audit_logging.tf](../terraform/security_governance/audit_logging.tf) with ready-to-run Cloud Logging & BigQuery Log Analytics SQL queries in [security-governance/audit/compliance-queries.md](../security-governance/audit/compliance-queries.md):
- Enable `ADMIN_READ` Data Access logs on **both** `agentregistry.googleapis.com` **and** `apphubendpoint.googleapis.com` (Admin Activity is always on).
- Route `cloudaudit.googleapis.com/activity`, `cloudaudit.googleapis.com/data_access`, and `networkservices.googleapis.com/gateway_requests` into a regional Cloud Logging bucket (`asia-southeast3`, `enable_analytics = true`), keeping `lock_audit_bucket = false` in ephemeral sandboxes and setting `lock_audit_bucket = true` in production for irreversible WORM compliance.

---

## Q4: GitLab vs. Google Cloud Agent Registry — Which Is the Source of Truth?

### Recommendation: Dual-Role Split (Never Either/Or)

| Dimension | GitLab Repository (**Authoring SoT**) | Google Cloud Agent Registry `v1` (**Governed Runtime Catalog SoT**) |
| :--- | :--- | :--- |
| **Primary Role** | Source code authoring, PR/MR peer review, Git history, multi-IDE plugin repo layout (`starter-marketplace/`) | Immutable published revisions, automated vulnerability/secret scanning, lifecycle state enforcement, runtime discovery, IAM/IAP enforcement, audit trail |
| **Consumers** | Human authors, code reviewers, GitLab CI runners | Cloud Run Polyglot Middleware, `tools.hub_sync`, ADK Platform Agents, Agent Gateway + IAP v2 UAP |
| **What Happens on Merge Request** | Developers open MR → CI Stage 1 runs 5-IDE schema validation + `agy plugin validate --json` (`jq` filter) → Stage 2 packages & signs ZIPs | CI Stage 3 (`register:draft-mr`) uploads Skills in **`TARGET_STATE_DRAFT`** with `ENABLED_BLOCKING` so findings block merge before exposure |
| **What Happens on Release Tag (`vX.Y.Z`)** | Maintainers approve & tag release on `main` | CI Stage 3 (`register:active-release`) signs with Cloud KMS `EC_SIGN_P256_SHA256` and promotes clean revisions to **`TARGET_STATE_ACTIVE`** |
| **Incident / Emergency Kill Switch** | Revert commit in GitLab for next release | **Immediate runtime containment** in Agent Registry (`PATCH ... targetState=TARGET_STATE_DISABLED` or roll back `defaultRevision`)—takes effect across Middleware and ADK agents instantly without waiting for GitLab CI |

### Progressive Onboarding Path: Option 1 → Option 2 → Push-Based GitLab CI

Teams rarely migrate every skill repository on Day 1. This workshop provides a smooth three-stage adoption ramp on the exact same Agent Registry (`v1`) control plane:

1. **Option 1 — Local Skills Path (`make demo-sync-local` / Terraform `skills_source_mode = "local"`):** Zero external Git setup. Ingests pre-prepared standalone skills from [local-skills/](../local-skills/README.md) alongside `starter-marketplace/plugins/gcp-finops-toolkit/` directly from disk.
2. **Option 2 — Centralized Git Repo Pull-Based Clone & Ingest (`make demo-sync-repo` / `tools.plugin_sync ingest --source-type git` / Terraform `skills_source_mode = "git"`):** Shallow-clones an existing customer GitLab or GitHub repository (`--repo-url`, `--ref`, optional `--subdir`, and `--git-token-env` via ephemeral `http.<origin>/.extraHeader`), auto-detects `agent-plugins.org` multi-plugin repos, standalone `skills/*/SKILL.md` repos, or hybrid repos, stamps exact 40-char `git_commit` provenance, and publishes into Agent Registry without changing upstream CI first.
3. **Target State — Push-Based GitLab CI (`.gitlab-ci.yml`):** Automated 3-stage pipeline inside the authoring repository enforcing 5-IDE schema + `agy plugin validate` gates, `TARGET_STATE_DRAFT` MR quarantine, keyless GCP Workload Identity Federation, and Cloud KMS ECDSA P-256 signing on semver release tags.

See the complete 3-stage pipeline in [starter-marketplace/.gitlab-ci.yml](../starter-marketplace/.gitlab-ci.yml) and walk through both developer and platform-agent consumption end-to-end in [Lab 02](labs/02-register-agent-registry.md) and [Lab 05: Platform Agents & GitLab Dual-SoT](labs/05-platform-agents-gitlab-sot.md).
