# Terraform GCP IAM Least-Privilege Audit Checklist

Apply every section below during a `terraform-iam-auditor` review.

## 1. Critical Findings (Block Merge)

- [ ] **Primitive Admin/Write Roles**: `roles/owner` or `roles/editor` granted to any service account, user, or group in `google_project_iam_*`, `google_folder_iam_*`, or `google_organization_iam_*`.
- [ ] **Autonomous Agent Over-Privilege on Agent Registry**:
  - `roles/agentregistry.editor` or `roles/agentregistry.admin` assigned to an autonomous agent runtime service account, LLM/ADK workload identity, or general developer/IDE user group.
  - **Why**: Allows an autonomous agent or compromised prompt session to register rogue MCP servers/A2A agents, alter `defaultRevision`, weaken `scanningConfig`, or create unauthorized Bindings.
  - **Fix**: Downgrade runtime agent identities to `roles/agentregistry.viewer` (or skill-level `roles/agentregistry.viewer`) paired with IAP Unified Access Policy controls (`roles/iap.egressor`). Reserve `roles/agentregistry.editor` for CI/CD release pipelines and `roles/agentregistry.admin` for platform security administrators.
- [ ] **Public Principals**: `allUsers` or `allAuthenticatedUsers` granted access to Cloud Storage buckets, BigQuery datasets, Cloud Run services, Secret Manager secrets, or KMS keys without documented public-endpoint justification.
- [ ] **Static Service Account Keys**: `google_service_account_key` created in Terraform state (leaks private key material into state files and disables short-lived token governance). Replace with Workload Identity Federation (`google_iam_workload_identity_pool*`) or GKE/Cloud Run workload service accounts.
- [ ] **Unrestricted Service Account Impersonation**: Broad project-level `roles/iam.serviceAccountTokenCreator` or `roles/iam.serviceAccountUser` instead of resource-level `google_service_account_iam_member` on the single target service account.

## 2. High Findings (Require Remediation or Written Exception)

- [ ] **Authoritative IAM Clobbering**: Use of `google_project_iam_policy` or `google_project_iam_binding` (which overwrite existing members, including Google-managed P4SAs) instead of additive `google_project_iam_member`.
- [ ] **Project-Wide Secret or KMS Access**: `roles/secretmanager.secretAccessor` or `roles/cloudkms.cryptoKeyEncrypterDecrypter` / `roles/cloudkms.signerVerifier` granted at project level instead of `google_secret_manager_secret_iam_member` or `google_kms_crypto_key_iam_member`.
- [ ] **Missing Separation of Duties on KMS Signing**: Same identity holding both `roles/cloudkms.signerVerifier` (or `roles/cloudkms.admin`) and unreviewed write access to skill/source repositories.
- [ ] **Wildcard IAP Agent Registry Egress**: `google_iap_agent_registry_iam_member` granting project-wide `roles/iap.egressor` to all MCP servers/agents without CEL conditions restricting destructive tools (`destination.agent_registry.mcp_server.tool.annotations.destructive_hint == false`).

## 3. Agent Registry IAM Role Matrix

| Role | Intended Principal | Prohibited Principal |
| :--- | :--- | :--- |
| `roles/agentregistry.viewer` | Autonomous agents, Middleware readers, developer IDE sync clients | *(Allowed for read-only discovery)* |
| `roles/agentregistry.user` | Scoped skill-authoring automation (standalone Skills only, no Services/Bindings) | Autonomous runtime agents |
| `roles/agentregistry.editor` | Reviewed CI/CD release pipelines (via WIF) managing Services & Skills | **Autonomous agents**, interactive developer groups |
| `roles/agentregistry.admin` | Platform security administrators managing Findings, Bindings, Skill IAM, CMEK | **Autonomous agents**, application runtimes, general developers |

## 4. Medium Findings (Hardening Recommendations)

- [ ] Prefer resource-level `google_iap_agent_registry_mcp_server_iam_member` / `google_iap_agent_registry_agent_iam_member` over broad project-level `google_iap_agent_registry_iam_member`.
- [ ] Attach `description` and `condition` blocks to elevated break-glass or temporary bindings with explicit `request.time < timestamp(...)` expiration.
- [ ] Confirm Data Access audit logging (`ADMIN_READ`, `DATA_READ`, `DATA_WRITE`) is enabled via `google_project_iam_audit_config` for `agentregistry.googleapis.com` and `cloudkms.googleapis.com`.
- [ ] Ensure every workload uses a dedicated service account rather than the Compute Engine or App Engine default service account.
