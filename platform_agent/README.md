# Enterprise Platform Agent (Google ADK & Agent Registry `v1`)

This package demonstrates the **platform agents** side of enterprise Agent Marketplace consumption: how headless agents running on **Vertex AI Agent Engine** or **Cloud Run** consume approved **A2A Agents**, **MCP Servers**, **Auth Manager Bindings**, and **Standalone Skills** *directly* from **Google Cloud Agent Registry (`v1`)**—without needing an IDE plugin marketplace wrapper.

> **Why direct Agent Registry consumption for platform agents?**
> Developer IDEs (Antigravity, Claude Code, VS Code/Copilot, Codex) require IDE-specific plugin manifests and marketplace JSON catalogs projected by the Hub Middleware. Platform agents built on **Google ADK (`google-adk>=1.29.0`)** connect natively to `agentregistry.googleapis.com/v1` for real-time discovery, Auth Manager credential exchange, and on-demand `?alt=media` skill loading.

---

## Architecture

```text
+-------------------------------------------------------------------------+
| Enterprise Platform Agent (Vertex AI Agent Engine / Cloud Run)          |
|   ADK LlmAgent + CredentialManager.register_auth_provider(              |
|                    GcpAuthProvider())                                   |
|                                                                         |
|   ├── AgentRegistry (google.adk.integrations.agent_registry)            |
|   │     ├── list_agents / search_agents  ──> get_remote_a2a_agent()     |
|   │     ├── list_mcp_servers / search_mcp_servers ──> get_mcp_toolset() |
|   │     └── bindings:fetchAvailable  ──> Auto-resolves AuthProvider     |
|   │                                                                     |
|   └── SkillToolset(registry=GCPSkillRegistry(...))                      |
|         ├── POST .../skills:search (SEMANTIC + state=ACTIVE             |
|         │     AND defaultRevisionFindingCount=0)                        |
|         └── 2-step GetSkill -> GetSkillRevision?alt=media (HTTP 302)    |
|               └── Strips x-goog-* & Authorization on cross-origin GCS   |
|                   signed URL + verifies sha256Hash (+ optional KMS sig) |
+-------------------------------------------------------------------------+
                                   │
                    Runtime IAM: roles/agentregistry.viewer
                               + roles/agentidentity.user
                    Egress Policy: Agent Gateway + IAP v2 UAP
```

---

## Modules

- [adk_orchestrator.py](./adk_orchestrator.py):
  - **Live ADK Mode (`mode="adk"`)**: Registers `GcpAuthProvider()` on `CredentialManager`, wires `AgentRegistry` (`list_agents`, `search_agents`, `get_remote_a2a_agent`, `list_mcp_servers`, `search_mcp_servers`, `get_mcp_toolset`) and `GCPSkillRegistry` + `SkillToolset` into an ADK `LlmAgent`.
  - **Local HTTP / Workshop Mode (`mode="rest"`)**: Gracefully guards optional `google.adk` imports so the orchestrator can be inspected (`build_adk_agent_config()`), unit-tested, and run locally against `mock-registry` (`http://127.0.0.1:8090/v1`) or live `agentregistry.googleapis.com/v1`.
- [rest_consumer.py](./rest_consumer.py):
  - `PlatformAgentConsumer`: Concrete async `httpx` client implementing `skills:search`, 2-step `GetSkill` -> `GetSkillRevision` -> `GetSkillRevision?alt=media` (302 cross-origin header stripping + `sha256Hash` + optional Cloud KMS `EC_SIGN_P256_SHA256` signature verification + safe `agentskills.io` ZIP extraction), `mcpServers:search`, `agents:search`, and `bindings:fetchAvailable`.

---

## Running in Local HTTP / Workshop Mode (`mode="rest"`)

No `google-adk` installation or GCP credentials are required for local workshop runs against `mock-registry`:

```bash
# Inspect the declarative ADK & governance configuration
python -m platform_agent.adk_orchestrator --mode adk --print-config

# Discover approved skills, MCP servers, A2A agents, and bindings from local mock-registry
AGENT_REGISTRY_BASE_URL="http://127.0.0.1:8090/v1" \
GCP_PROJECT_ID="acme-agent-governance" \
GCP_LOCATION="asia-southeast3" \
python -m platform_agent.adk_orchestrator \
  --mode rest \
  --skill-query "cost anomaly" \
  --source-agent-urn "urn:agent:projects-123456789012:projects:123456789012:locations:asia-southeast3:agentregistry:services:finops-advisor-agent"
```

---

## Running in Live ADK Mode (`mode="adk"`)

1. Install Google ADK with A2A, Agent Identity, and MCP extras:
   ```bash
   pip install -r platform_agent/requirements.txt
   ```
2. Grant the runtime service account least-privilege IAM roles (never grant `roles/agentregistry.editor` or `roles/agentregistry.admin` to runtime agents):
   - `roles/agentregistry.viewer` (read-only discovery & `?alt=media` download of active skills)
   - `roles/agentidentity.user` (credential retrieval from bound Auth Manager `AuthProvider` resources)
   - `roles/iap.egressor` (when routing through Agent Gateway + IAP v2 UAP)
3. Build the live ADK `LlmAgent` (regional `Services` in Bangkok `asia-southeast3` or Singapore `asia-southeast1`, shared standalone `Skills` in `global` — or `us`/`eu` for CMEK):
   ```bash
   export GCP_PROJECT_ID="acme-agent-governance"
   export GCP_LOCATION="asia-southeast3"
   export AGENT_REGISTRY_SKILLS_LOCATION="global"
   export AGENT_REGISTRY_BASE_URL="https://agentregistry.googleapis.com/v1"
   export GOOGLE_CLOUD_PROJECT="${GCP_PROJECT_ID}"
   export GOOGLE_CLOUD_LOCATION="${GCP_LOCATION}"
   export AGENT_REGISTRY_ENDPOINT="${AGENT_REGISTRY_BASE_URL}"
   python -m platform_agent.adk_orchestrator --mode adk --skills-location "${AGENT_REGISTRY_SKILLS_LOCATION}"
   ```

---

## Critical Agent Registry `v1` Engineering Gotchas

1. **Skill ID `private-` auto-prefixing & verified live regional split**: On live Google Cloud Agent Registry `v1`, standalone `Skills` live in **`global`** (default for APAC — or `us`/`eu` for CMEK) while `Services` (`McpServer`, `Agent`, `Endpoint`) run regionally in **Bangkok (`asia-southeast3`)** and **Singapore (`asia-southeast1`)**. Creating `skillId=cost-anomaly-triage` produces `projects/{p}/locations/global/skills/private-cost-anomaly-triage`; `PlatformAgentConsumer` automatically defaults `skills_location="global"` for `googleapis.com` and filters to `private-*` enterprise skills (`only_private_skills=True`) so Google first-party skills do not pollute discovery.
2. **`?alt=media` HTTP 302 redirect header handling**: Live `GetSkillRevision?alt=media` redirects (`302`) to a same-origin authenticated Scotty endpoint (`https://agentregistry.googleapis.com/download/v1/...`), where stripping `Authorization` causes `401 CREDENTIALS_MISSING`, whereas cross-origin or signed-URL redirects (`storage.googleapis.com`, `X-Goog-Signature`, or mock `/scotty-signed/{token}`) return `403` if `Authorization` or `x-goog-*` headers leak. `PlatformAgentConsumer.download_skill_archive()` follows redirects explicitly (`follow_redirects=False`), preserving auth **strictly** on same-origin `/download/...` redirects without signed-URL tokens while stripping `Authorization` and all `x-goog-*` headers on every cross-origin or signed-URL hop.
3. **Search API asymmetry**: `skills:search` supports `searchType: "SEMANTIC" | "KEYWORD"` plus `filter="state=ACTIVE AND defaultRevisionFindingCount=0"`. `agents:search` and `mcpServers:search` are keyword/prefix only (`{"searchString": "..."}`, no `searchType`).
4. **Project scoping for Standalone Skills**: Standalone `Skill` resources are strictly project-scoped and do not roll up across folder/org management boundaries—platform agents must target the project where approved skills are published.
5. **KMS Encryption vs. KMS Signing (Fail-Closed)**: Native `SkillEncryptionSpec` (CMEK in `us`/`eu`) provides encryption at rest only; cryptographic provenance signing uses a Cloud KMS `EC_SIGN_P256_SHA256` asymmetric key verified by `PlatformAgentConsumer.fetch_and_extract_skill(..., verify_kms_signature=True)`. Setting `REQUIRE_KMS_SIGNATURE=true` without `KMS_PUBLIC_KEY_PEM_PATH` fails closed with `SkillSignatureError`.
