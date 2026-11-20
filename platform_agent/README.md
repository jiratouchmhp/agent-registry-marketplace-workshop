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
GCP_LOCATION="us-central1" \
python -m platform_agent.adk_orchestrator \
  --mode rest \
  --skill-query "cost anomaly" \
  --source-agent-urn "urn:agent:projects-123456789012:projects:123456789012:locations:us-central1:agentregistry:services:finops-advisor-agent"
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
3. Build the live ADK `LlmAgent`:
   ```bash
   export GOOGLE_CLOUD_PROJECT="acme-agent-governance"
   export GOOGLE_CLOUD_LOCATION="us-central1"
   export AGENT_REGISTRY_ENDPOINT="https://agentregistry.googleapis.com/v1"
   python -m platform_agent.adk_orchestrator --mode adk
   ```

---

## Critical Agent Registry `v1` Engineering Gotchas

1. **Skill ID `private-` auto-prefixing**: Creating `skillId=cost-anomaly-triage` produces `projects/{p}/locations/{l}/skills/private-cost-anomaly-triage`. Subsequent `GET`/`PATCH`/`DELETE` calls must use `private-cost-anomaly-triage` (see `PlatformAgentConsumer.private_skill_name()`).
2. **`?alt=media` HTTP 302 redirect header stripping**: `GetSkillRevision?alt=media` redirects to a short-lived GCS signed URL (or `/scotty-signed/{token}` in the local workshop `mock_registry`). `PlatformAgentConsumer.download_skill_archive()` follows redirects explicitly with `follow_redirects=False` and strips all `x-goog-*` headers (`x-goog-user-project`, `x-goog-api-client`) and `Authorization` on every redirect hop—both cross-origin and same-origin—so signed-URL validation always succeeds.
3. **Search API asymmetry**: `skills:search` supports `searchType: "SEMANTIC" | "KEYWORD"` plus `filter="state=ACTIVE AND defaultRevisionFindingCount=0"`. `agents:search` and `mcpServers:search` are keyword/prefix only (`{"searchString": "..."}`, no `searchType`).
4. **Project scoping for Standalone Skills**: Standalone `Skill` resources are strictly project-scoped and do not roll up across folder/org management boundaries—platform agents must target the project where approved skills are published.
5. **KMS Encryption vs. KMS Signing (Fail-Closed)**: Native `SkillEncryptionSpec` (CMEK in `us`/`eu`) provides encryption at rest only; cryptographic provenance signing uses a Cloud KMS `EC_SIGN_P256_SHA256` asymmetric key verified by `PlatformAgentConsumer.fetch_and_extract_skill(..., verify_kms_signature=True)`. Setting `REQUIRE_KMS_SIGNATURE=true` without `KMS_PUBLIC_KEY_PEM_PATH` fails closed with `SkillSignatureError`.
