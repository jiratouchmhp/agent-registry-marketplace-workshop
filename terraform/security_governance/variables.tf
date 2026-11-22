# Regional Architecture:
# Aim for Bangkok (asia-southeast3) for all supported services (KMS asymmetric
# signing key, Agent Registry Bindings, IAP egressor IAM bindings, Cloud Logging
# audit bucket); use Singapore (asia-southeast1) only for services not yet
# available in Bangkok (Agent Identity AuthProvider).

variable "project_id" {
  description = "Google Cloud project ID hosting Agent Registry and Workstream B security & governance resources."
  type        = string

  validation {
    condition     = length(trimspace(var.project_id)) > 0
    error_message = "project_id must be a non-empty Google Cloud project ID."
  }
}

variable "location" {
  description = "Primary Google Cloud region for all supported Workstream B services: KMS asymmetric signing key, Agent Registry Bindings, IAP egressor IAM bindings, and regional Cloud Logging audit bucket (default: asia-southeast3, Bangkok)."
  type        = string
  default     = "asia-southeast3"

  validation {
    condition     = length(trimspace(var.location)) > 0
    error_message = "location must be a non-empty Google Cloud region (for example, asia-southeast3)."
  }
}

variable "auth_provider_location" {
  description = "Google Cloud region for the Agent Identity AuthProvider (agentidentity.googleapis.com). Defaults to asia-southeast1 (Singapore) as a service-specific fallback ONLY because Agent Identity is GA in Singapore and not yet available in Bangkok (asia-southeast3)."
  type        = string
  default     = "asia-southeast1"

  validation {
    condition     = length(trimspace(var.auth_provider_location)) > 0
    error_message = "auth_provider_location must be a non-empty Google Cloud region supported by Agent Identity (for example, asia-southeast1)."
  }
}

variable "enable_agent_gateway" {
  description = "When true, provisions the optional Google-managed Network Services Agent Gateway. Defaults to false for APAC because Network Services Agent Gateway on Cloud Run is currently GA only in us-central1 and europe-west1."
  type        = bool
  default     = false
}

variable "agent_gateway_location" {
  description = "Google Cloud region for the optional Network Services Agent Gateway when enable_agent_gateway is true (currently GA in us-central1 and europe-west1)."
  type        = string
  default     = "us-central1"

  validation {
    condition     = length(trimspace(var.agent_gateway_location)) > 0
    error_message = "agent_gateway_location must be a non-empty Google Cloud region supported by Network Services Agent Gateway (for example, us-central1 or europe-west1)."
  }
}

variable "cmek_location" {
  description = "Cloud KMS multi-region location for the Agent Registry Skill CMEK key ring. Must be \"us\" (for Skills in location \"us\") or \"europe\" (REQUIRED for Skills in location \"eu\")."
  type        = string
  default     = "us"

  validation {
    condition     = contains(["us", "europe"], var.cmek_location)
    error_message = "cmek_location must be \"us\" (for Skills in us) or \"europe\" (required for Skills in eu)."
  }
}

variable "middleware_sa_email" {
  description = "Email of the least-privilege polyglot-middleware service account (from terraform/core output middleware_sa_email, or an existing viewer SA). Granted roles/cloudkms.publicKeyViewer on the asymmetric signing key."
  type        = string

  validation {
    condition     = length(trimspace(var.middleware_sa_email)) > 0
    error_message = "middleware_sa_email must be a non-empty service account email."
  }
}

variable "ci_publisher_sa_email" {
  description = "Email of the CI/CD publisher service account granted roles/cloudkms.signerVerifier on the asymmetric artifact signing key. Defaults to agent-registry-ci-publisher@<project_id>.iam.gserviceaccount.com if omitted."
  type        = string
  default     = ""
}

variable "mcp_server_resource_id" {
  description = "Short resource ID of the projected read-only Agent Registry McpServer (from terraform/core output mcp_server_resource_id or basename(mcp_registry_resource)). Used for IAP egressor IAM bindings."
  type        = string

  validation {
    condition     = length(trimspace(var.mcp_server_resource_id)) > 0
    error_message = "mcp_server_resource_id must be non-empty."
  }
}

variable "mcp_server_urn" {
  description = "Canonical Agent Registry URN of the target MCP server (format: urn:mcp:...). Used as target.identifier in google_agent_registry_binding."
  type        = string

  validation {
    condition     = startswith(trimspace(var.mcp_server_urn), "urn:mcp:")
    error_message = "mcp_server_urn must be a valid Agent Registry MCP server URN starting with \"urn:mcp:\"."
  }
}

variable "a2a_agent_resource_id" {
  description = "Short resource ID of the projected read-only Agent Registry Agent (from terraform/core output a2a_agent_resource_id or basename(agent_registry_resource)). Used for IAP egressor IAM bindings."
  type        = string

  validation {
    condition     = length(trimspace(var.a2a_agent_resource_id)) > 0
    error_message = "a2a_agent_resource_id must be non-empty."
  }
}

variable "a2a_agent_urn" {
  description = "Canonical Agent Registry URN of the source A2A agent (format: urn:agent:...). Used as source.identifier in google_agent_registry_binding."
  type        = string

  validation {
    condition     = startswith(trimspace(var.a2a_agent_urn), "urn:agent:")
    error_message = "a2a_agent_urn must be a valid Agent Registry Agent URN starting with \"urn:agent:\"."
  }
}

variable "enable_cmek_spec" {
  description = "When true, runs terraform_data to attach the CMEK key to the location's SkillEncryptionSpec. Defaults to false because Agent Registry enforces a strict ZERO-SKILLS precondition: updating SkillEncryptionSpec fails with FAILED_PRECONDITION if ANY Skill already exists in the target multi-region location (us or eu)."
  type        = bool
  default     = false
}

variable "enable_org_policy" {
  description = "When true (and org_id is non-empty), creates and enforces the custom Organization Policy constraint denying Agent Registry Skills with scanningConfig.policy == 'DISABLED'."
  type        = bool
  default     = false
}

variable "org_id" {
  description = "Numeric Google Cloud Organization ID (for example, \"123456789012\"). Required only when enable_org_policy is true."
  type        = string
  default     = ""

  validation {
    condition     = var.org_id == "" || can(regex("^[0-9]+$", var.org_id))
    error_message = "org_id must be empty or a numeric Google Cloud Organization ID."
  }
}

variable "audit_retention_days" {
  description = "Retention period in days for the Cloud Logging audit bucket (irreversible once lock_audit_bucket = true is applied)."
  type        = number
  default     = 90

  validation {
    condition     = var.audit_retention_days >= 1 && var.audit_retention_days <= 3650
    error_message = "audit_retention_days must be between 1 and 3650 days."
  }
}

# WARNING: Set `lock_audit_bucket = true` in PRODUCTION for immutable WORM compliance!
# Once `locked = true` is applied in Google Cloud, it is IRREVERSIBLE: retention cannot
# be shortened and the bucket cannot be deleted until all log entries age out.
# Defaults to `false` ONLY so ephemeral workshop sandboxes can run `terraform destroy` cleanly.
variable "lock_audit_bucket" {
  description = "Controls whether the Cloud Logging governance audit bucket is locked (WORM). WARNING: ALWAYS set true in production! Defaults to false so ephemeral workshop sandboxes can be cleanly destroyed."
  type        = bool
  default     = false
}

variable "auth_provider_id" {
  description = "Resource ID for the Agent Identity AuthProvider used by google_agent_registry_binding."
  type        = string
  default     = "finops-mcp-oauth"
}

variable "auth_provider_client_id" {
  description = "OAuth 2.0 client ID configured on the Agent Identity three-legged OAuth AuthProvider."
  type        = string
  default     = "finops-mcp-oauth-client"
}

variable "auth_provider_client_secret" {
  description = "OAuth 2.0 client secret for the Agent Identity AuthProvider. Supply via TF_VAR_auth_provider_client_secret or Secret Manager; never commit real secrets."
  type        = string
  default     = "REPLACE_VIA_TF_VAR_AUTH_PROVIDER_CLIENT_SECRET"
  sensitive   = true
}

variable "auth_provider_authorization_url" {
  description = "OAuth 2.0 authorization endpoint for the three-legged OAuth AuthProvider."
  type        = string
  default     = "https://accounts.google.com/o/oauth2/v2/auth"
}

variable "auth_provider_token_url" {
  description = "OAuth 2.0 token endpoint for the three-legged OAuth AuthProvider."
  type        = string
  default     = "https://oauth2.googleapis.com/token"
}

variable "auth_provider_scopes" {
  description = "Allowed and requested OAuth 2.0 scopes for the Agent Registry Binding."
  type        = list(string)
  default = [
    "https://www.googleapis.com/auth/cloud-billing.readonly",
    "https://www.googleapis.com/auth/bigquery.readonly",
  ]
}

variable "auth_provider_continue_uri" {
  description = "Redirect/continue URI configured on the AuthProvider and AuthProviderBinding after user consent."
  type        = string
  default     = "https://console.cloud.google.com/agent-registry/auth/callback"
}

variable "binding_id" {
  description = "Resource ID for the Agent Registry Binding connecting the A2A agent to the MCP server."
  type        = string
  default     = "finops-agent-to-mcp-binding"
}

variable "agent_gateway_name" {
  description = "Resource name for the Google-managed Network Services Agent Gateway."
  type        = string
  default     = "agent-marketplace-gateway"
}

variable "iap_egressor_member" {
  description = "IAM member granted roles/iap.egressor on the registered MCP server and A2A agent (for example, serviceAccount:... or principal://agents.global...). Defaults to serviceAccount:<middleware_sa_email> when omitted."
  type        = string
  default     = ""
}

variable "audit_bucket_id" {
  description = "Cloud Logging custom bucket ID for locked WORM agent governance audit logs."
  type        = string
  default     = "agent-governance-audit"
}
