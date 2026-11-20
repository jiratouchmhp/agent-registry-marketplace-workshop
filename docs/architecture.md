# Technical Architecture: Governed Polyglot Enterprise Agent Marketplace

This document details the engineering architecture of the **Enterprise Agent Marketplace Workshop** built on **Google Cloud Agent Registry (`v1` GA)**. All ports, resource names, URNs, and schemas match [WORKSHOP_SPEC.json](../WORKSHOP_SPEC.json).

---

## 1. End-to-End System Topology

```mermaid
flowchart TB
    subgraph DevAuthor["Authoring Plane (GitLab SoT)"]
        REPO["starter-marketplace/plugins/gcp-finops-toolkit/<br/>plugin.json + .antigravity-plugin/ + skills/ + agents/ + rules/"]
        PIPE[".gitlab-ci.yml (3 Stages)<br/>1. validate (5-IDE schemas + agy jq filter)<br/>2. package-and-sign (plugin_sync + KMS ECDSA P-256)<br/>3. register-registry (DRAFT on MR / ACTIVE on tag)"]
        REPO --> PIPE
    end

    subgraph GCPControl["Control & Governance Plane (Google Cloud)"]
        AR["Google Cloud Agent Registry v1<br/>(Local: mock_registry :8090)<br/>• Skills (private-*) + Immutable SkillRevisions<br/>• Services -> McpServer / Agent / Endpoint<br/>• Bindings + Findings + Publishers"]
        KMS_CMEK["Cloud KMS Symmetric Key (us/europe)<br/>SkillEncryptionSpec (Encryption at Rest)"]
        KMS_SIGN["Cloud KMS Asymmetric Key (EC_SIGN_P256_SHA256)<br/>CI: signerVerifier | Middleware: publicKeyViewer"]
        ORGPOL["Org Policy Custom Constraint<br/>Deny scanningConfig.policy == DISABLED"]
        AUDIT["Locked WORM Cloud Logging Bucket<br/>Admin Activity + ADMIN_READ + Gateway Requests"]
        AR --- KMS_CMEK
        AR --- ORGPOL
        AR --> AUDIT
    end

    subgraph RuntimeServe["Serving & Distribution Plane"]
        MW["Polyglot Middleware (Cloud Run :8080)<br/>• GET /v1/catalog<br/>• GET /v1/marketplace/{claude,copilot,codex,antigravity}.json<br/>• GET /v1/plugins/{name}.zip<br/>• POST /mcp (Streamable HTTP JSON-RPC 2.0)"]
        MCP_S["FinOps Sample MCP Server (:8091)<br/>get_billing_summary (readOnly)<br/>analyze_bq_query (readOnly)<br/>delete_idle_dataset (destructive)"]
        A2A_S["FinOps Sample A2A v1.0 Agent (:8092)<br/>/.well-known/agent-card.json + /a2a"]
        GW["Network Services Agent Gateway<br/>+ IAP v2 UAP (CEL tool annotations)"]
    end

    subgraph Consumers["Consumption Plane"]
        HUB["tools.hub_sync (1-Command CLI)<br/>Ownership-Tracked Hydration"]
        IDES["Developer IDEs / CLIs<br/>Claude Code | Copilot/VS Code | Codex | Antigravity"]
        ADK["Platform Agents (Google ADK)<br/>AgentRegistry + GCPSkillRegistry + SkillToolset"]
    end

    PIPE -->|"Sign canonical digest"| KMS_SIGN
    PIPE -->|"REST v1 (Service + Skill)"| AR
    AR -->|"?alt=media (302 -> GCS signed URL)"| MW
    KMS_SIGN -->|"Public Key PEM"| MW
    MW --> HUB --> IDES
    AR -->|"Direct v1 REST / ADK"| ADK
    ADK --> GW --> MCP_S
    ADK --> GW --> A2A_S
    MW -->|"call_remote_a2a_agent"| A2A_S
    GW --> AUDIT
```

### Component Ports & Defaults (`WORKSHOP_SPEC.json`)

| Service | Module / Directory | Local Port | Key Endpoints |
| :--- | :--- | :---: | :--- |
| **Mock Agent Registry (`v1`)** | [mock_registry/app.py](../mock_registry/app.py) | `8090` | `/v1/projects/{p}/locations/{l}/{services,agents,mcpServers,endpoints,skills,bindings,findings,publishers}`, `/scotty-signed/{token}`, `/_mock/reset` |
| **Sample FinOps MCP Server** | [samples/mcp-server/app.py](../samples/mcp-server/app.py) | `8091` | `/healthz`, `POST /mcp` (`get_billing_summary`, `analyze_bq_query`, `delete_idle_dataset`) |
| **Sample FinOps A2A Agent** | [samples/a2a-agent/app.py](../samples/a2a-agent/app.py) | `8092` | `/healthz`, `GET /.well-known/agent-card.json`, `POST /a2a`, `GET /a2a/tasks/{task_id}` |
| **Polyglot Marketplace Middleware** | [middleware/app/main.py](../middleware/app/main.py) | `8080` | `/healthz`, `/v1/catalog`, `/v1/marketplace/{claude,copilot,codex,antigravity}.json`, `/v1/plugins/{plugin_name}.zip`, `POST /mcp` |

---

## 2. Polyglot Single-Bundle ZIP Layout

When [middleware/app/polyglot_builder.py](../middleware/app/polyglot_builder.py) synthesizes `/v1/plugins/{plugin_name}.zip`, it groups all approved `STATE_ACTIVE` (and optionally `STATE_DEPRECATED`) skills sharing `frontmatter.metadata.plugin_name`, resolves linked `mcp_service_id` and `agent_service_id` resources from Agent Registry, and builds a **deterministic single ZIP** (sorted paths, fixed timestamp, `0644`/`0755` Unix permissions, strict zip-slip/symlink rejection) that passes validators for **all five targets simultaneously**:

```mermaid
graph TD
    ZIP["gcp-finops-toolkit.zip (Deterministic Polyglot Bundle)"]

    ZIP --> AP["Agent Plugins 1.0 & Copilot Root<br/>• plugin.json ($schema 1.0.0 + extensions['com.openai'])<br/>• mcp.json (streamable-http, NO ${ENV} expansion)<br/>• com.github.copilot/agents/finops-advisor-agent.agent.md"]
    ZIP --> AG["Google Antigravity (Precedence Manifest)<br/>• .antigravity-plugin/plugin.json (additionalProperties: false)<br/>• mcp_config.json (authProviderType: google_credentials, no variables[])<br/>• assets/logo.svg (1:1 square icon)<br/>• rules/AGENTS.md + rules/finops-guardrails.md (comma-sep globs)"]
    ZIP --> CC["Claude Code<br/>• .claude-plugin/plugin.json<br/>• .mcp.json (streamable-http)<br/>• agents/finops-advisor-agent.md"]
    ZIP --> CX["OpenAI Codex<br/>• .codex-plugin/plugin.json<br/>• plugin.json extensions['com.openai']"]
    ZIP --> SK["Shared Verified Skills (agentskills.io)<br/>• skills/cost-anomaly-triage/SKILL.md (+ references/)<br/>• skills/bq-query-optimizer/SKILL.md<br/>(folder name == frontmatter.name)"]
```

### Critical Cross-IDE Coexistence Invariants
1. **No brand prefix violations**: Antigravity rejects plugin names starting with `antigravity`, `gemini`, `claude`, or `cursor` (`sanitize_kebab_name()`).
2. **Sanitized MCP server keys**: Keys match `^[a-zA-Z0-9_-]{1,20}$` (dots replaced with hyphens) so `<plugin-name>_<server>` / `mcp_<server>_<tool>` validates cleanly (`sanitize_antigravity_mcp_key()`).
3. **Immediate enablement in Antigravity**: `mcp_config.json` uses `authProviderType: "google_credentials"` without `variables[]`, ensuring plugins install enabled immediately without manual variable prompts.
4. **Comma-separated `globs` string**: `rules/finops-guardrails.md` declares `globs: "**/*.sql, **/*.tf, **/*.yaml"` as a single string—never a YAML list.
5. **No `${ENV}` expansion in `mcp.json`**: Agent Plugins 1.0 forbids `${VAR}` interpolation inside remote `url` or `headers`.

---

## 3. Critical `?alt=media` HTTP 302 Redirect & Verification Pipeline

Downloading a `SkillRevision` archive from Agent Registry `v1` is a **two-hop cross-origin flow** that breaks naive HTTP clients (`follow_redirects=True` forwarding Google headers to GCS signed URLs causes HTTP 403/400 rejection). Both [middleware/app/registry_client.py](../middleware/app/registry_client.py) and [platform_agent/rest_consumer.py](../platform_agent/rest_consumer.py) implement explicit redirect handling and fail-closed cryptographic verification:

```mermaid
sequenceDiagram
    autonumber
    participant Client as Middleware / ADK Consumer
    participant AR as Agent Registry v1<br/>(agentregistry.googleapis.com)
    participant GCS as Signed GCS Endpoint<br/>(storage.googleapis.com / /scotty-signed)
    participant Verifier as SecurityVerifier<br/>(SHA-256 + KMS P-256)

    Client->>AR: GET /v1/.../skills/private-cost-anomaly-triage/revisions/rev1<br/>Headers: Authorization: Bearer ..., x-goog-user-project, x-goog-api-client
    AR-->>Client: 200 OK {sha256Hash, scanResult: {scanState: "SUCCEEDED", findings: []}}

    Client->>Verifier: check_metadata_policy(skill, revision)
    Note over Verifier: Gate 1: state in (STATE_ACTIVE, STATE_DEPRECATED)<br/>Gate 2: scanState in (SUCCEEDED, DISABLED) & findings == 0

    Client->>AR: GET /v1/.../revisions/rev1?alt=media (follow_redirects=False)<br/>Headers: Authorization, x-goog-user-project, x-goog-api-client
    AR-->>Client: 302 Found -> Location: https://storage.googleapis.com/...signed-url...

    Note over Client: CRITICAL: Strip Authorization AND ALL x-goog-* headers<br/>before issuing the redirect hop!

    Client->>GCS: GET <signed-url> (clean headers — NO Authorization, NO x-goog-*)
    GCS-->>Client: 200 OK (application/zip bytes)

    Client->>Verifier: verify_skill_package(skill, revision, zip_bytes)
    Note over Verifier: Gate 3: hmac.compare_digest(sha256(zip_bytes), revision.sha256Hash)<br/>Gate 4 (when REQUIRE_KMS_SIGNATURE=true):<br/>Verify SKILL.md metadata.kms_signature (ECDSA P-256) over canonical payload
    Verifier-->>Client: VerificationResult(approved=True, kms_verified=True)
```

> **Local Workshop Fidelity:** [mock_registry/app.py](../mock_registry/app.py) simulates this exact behavior via `/scotty-signed/{token}`, returning **HTTP 403** if any `x-goog-*` or `Authorization` header leaks onto the redirect target!

---

## 4. Safe Ownership-Tracked Workstation Hydration (`tools.hub_sync`)

Corporate developer laptops hold customized personal IDE configurations. [tools/hub_sync/hydration.py](../tools/hub_sync/hydration.py) and [tools/hub_sync/adapters.py](../tools/hub_sync/adapters.py) enforce five strict safety invariants:

```mermaid
flowchart LR
    CAT["1. Fetch /v1/catalog<br/>& Download All ZIPs in Memory"] --> VAL["2. Pre-Flight Verify<br/>• SHA-256 matches catalog<br/>• No zip-slip / symlinks / '..'<br/>• Valid identifiers"]
    VAL -->|"Any failure -> ABORT<br/>(disk untouched)"| ABORT["Exit 1 — Existing<br/>IDE Configs Preserved"]
    VAL -->|"All valid"| EXTRACT["3. Atomic Extract<br/>~/.acme-agent-hub/plugins/<name><br/>+ Marketplace Indexes"]
    EXTRACT --> MERGE["4. Ownership-Tracked IDE Merge<br/>(.sync-manifest.json tracks managed keys)<br/>• Preserve user-authored servers/plugins<br/>• Preserve user disabled / disabledTools<br/>• Prune removed hub entries ONLY on clean full sync"]
    MERGE --> ATOMIC["5. Atomic Writes<br/>NamedTemporaryFile (same dir)<br/>-> fsync -> os.replace"]
```

### Files Managed per IDE Target (`--home <HOME>`)

| Target | Hub Index / Config Files Written | User-Safety Guarantee |
| :--- | :--- | :--- |
| **Shared Hub** | `<HOME>/.acme-agent-hub/.sync-manifest.json`, `<HOME>/.acme-agent-hub/plugins/<name>/` | Atomic directory swap; pruning disabled on partial failure or `--bootstrap-mcp-only` |
| **Claude Code** | `<HOME>/.acme-agent-hub/.claude-plugin/marketplace.json`, `<HOME>/.claude/settings.json` | Merges `extraKnownMarketplaces` & `enabledPlugins`; preserves pre-existing keys and user `false` toggles |
| **VS Code / Copilot** | `<HOME>/.acme-agent-hub/marketplace.json`, `.github/plugin/marketplace.json`, `vscode-settings-snippet.json`, optional `~/.config/Code/User/settings.json` (`--apply-vscode`) | Preserves existing `chat.plugins.marketplaces` and user `chat.pluginLocations` entries |
| **OpenAI Codex** | `~/.agents/plugins/marketplace.json`, `~/.agents/plugins/plugins/<name>` (symlinks), `~/.codex/agents/<agent>.toml`, `~/.codex/config.toml` | Preserves user-authored `[mcp_servers.*]`, user agent `.toml` files, and user plugin directories |
| **Google Antigravity** | `<HOME>/.acme-agent-hub/antigravity-marketplace.json`, `~/.gemini/config/mcp_config.json`, `~/.gemini/config/plugins/<name>`, optional `agy plugin install` (skipped with `--no-exec`) | Preserves user-authored MCP servers and user `disabled`/`disabledTools` overrides; skips pre-existing unmanaged plugin folders |

---

## 5. Security Defense-in-Depth Planes (Workstream B)

```mermaid
flowchart TB
    P1["Plane 1: Authoring & Supply Chain<br/>• 5-IDE schema validation + agy jq filter in GitLab CI<br/>• Cloud KMS Asymmetric Signing (EC_SIGN_P256_SHA256)<br/>• CI SA: roles/cloudkms.signerVerifier"]
    P2["Plane 2: Registry Admission & Storage<br/>• TARGET_STATE_DRAFT quarantine on MR<br/>• ENABLED_BLOCKING scanner (SECRET & VULNERABILITY Findings)<br/>• Org Policy Custom Constraint denying DISABLED scanning<br/>• Native CMEK (SkillEncryptionSpec in us/eu, zero-skills precondition)"]
    P3["Plane 3: Egress Verification & Least Privilege<br/>• Middleware & Platform Agents: roles/agentregistry.viewer ONLY<br/>• Middleware KMS access: roles/cloudkms.publicKeyViewer ONLY<br/>• SHA-256 + ECDSA P-256 verification before serving or loading"]
    P4["Plane 4: Runtime Tool & Credential Governance<br/>• Agent Identity AuthProvider + Agent Registry Binding (fetchAvailable IAM filter)<br/>• Network Services Agent Gateway (CLIENT_TO_AGENT) + roles/iap.egressor<br/>• IAP v2 UAP CEL: read_only_hint ALLOW / destructive_hint DENY<br/>• Optional client-side Antigravity hooks.json PreToolUse/PostToolUse guard"]
    P5["Plane 5: Immutable Forensic Audit<br/>• Admin Activity + ADMIN_READ Data Access (agentregistry & apphubendpoint)<br/>• Agent Gateway request logs + IAP egressViaIAP decisions + KMS logs<br/>• Locked WORM Cloud Logging Bucket + BigQuery Log Analytics SQL"]

    P1 --> P2 --> P3 --> P4 --> P5
```

### Canonical Separation of Identities

| Identity | Terraform Module | Agent Registry Role | Cloud KMS Role | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **Polyglot Middleware SA** (`agent-marketplace-middleware`) | `terraform/core` + `security_governance` | `roles/agentregistry.viewer` | `roles/cloudkms.publicKeyViewer` | Reads active skills/services, verifies signatures, serves IDE feeds |
| **CI Publisher SA** (`agent-registry-ci-publisher`) | `terraform/core` + `security_governance` | `roles/agentregistry.editor` | `roles/cloudkms.signerVerifier` | Uploads Services & Skills from GitLab CI, signs skill digests |
| **Standalone Skill Author SA** (`agent-registry-skill-author`) | `terraform/core` | `roles/agentregistry.user` | *(none)* | Standalone skill CRUD only (cannot mutate `Service` registrations) |
| **Agent Registry P4SA** (`service-<NUM>@gcp-sa-agentregistry.iam.gserviceaccount.com`) | `terraform/core` + `security_governance` | *(Google-managed)* | `roles/cloudkms.cryptoKeyEncrypterDecrypter` | Imports GCS-staged skills (`roles/storage.objectViewer`) & encrypts skills at rest via CMEK |
| **Security Admin** (Human / SecOps group) | Organization / Project | `roles/agentregistry.admin` | KMS Admin | Manages `Bindings`, `Findings`, per-`Skill` IAM, `SkillEncryptionSpec`, Org Policies |
| **Sample Workloads SA** (`finops-sample-workloads`) | `terraform/core` | *(none — zero Agent Registry IAM)* | *(none)* | Runs sample MCP server & A2A agent with zero registry privileges |
