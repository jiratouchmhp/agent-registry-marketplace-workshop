variable "project_id" {
  description = "Google Cloud project ID hosting Agent Registry, Cloud Run services, Artifact Registry, and skill staging storage."
  type        = string

  validation {
    condition     = length(trimspace(var.project_id)) > 0
    error_message = "project_id must be a non-empty Google Cloud project ID."
  }
}

variable "location" {
  description = "Google Cloud region for Agent Registry services, Cloud Run workloads, Artifact Registry, and skill staging storage."
  type        = string
  default     = "us-central1"

  validation {
    condition     = length(trimspace(var.location)) > 0
    error_message = "location must be a non-empty Google Cloud region (for example, us-central1)."
  }
}

variable "middleware_image" {
  description = "Container image URI for the polyglot-middleware Cloud Run service. Leave empty to default to the Artifact Registry repository created by this module."
  type        = string
  default     = ""
}

variable "sample_mcp_image" {
  description = "Container image URI for the sample-mcp-server Cloud Run service. Leave empty to default to the Artifact Registry repository created by this module."
  type        = string
  default     = ""
}

variable "sample_a2a_image" {
  description = "Container image URI for the sample-a2a-agent Cloud Run service. Leave empty to default to the Artifact Registry repository created by this module."
  type        = string
  default     = ""
}

variable "enable_skills_bootstrap" {
  description = "When true, runs the terraform_data bootstrapper to publish standalone Agent Registry skills from starter_plugin_dir via plugin-sync / REST v1."
  type        = bool
  default     = true
}

variable "starter_plugin_dir" {
  description = "Path (relative to terraform/core/ or absolute) to the starter plugin directory whose skills are published by the bootstrapper."
  type        = string
  default     = "../../starter-marketplace/plugins/gcp-finops-toolkit"
}

variable "allow_unauthenticated_workshop_demo" {
  description = "Secure by default (false). Set true ONLY for ephemeral workshop sandboxes to grant allUsers roles/run.invoker on the Cloud Run demo services."
  type        = bool
  default     = false
}

variable "middleware_public_url" {
  description = "Optional custom public base URL for polyglot-middleware (for example, behind a custom domain or HTTPS Load Balancer). Defaults to the deterministic Cloud Run URL."
  type        = string
  default     = ""
}

variable "require_kms_signature" {
  description = "Controls REQUIRE_KMS_SIGNATURE on polyglot-middleware. Defaults to false in Core; set true when Workstream B KMS cryptographic signing verification is enabled."
  type        = bool
  default     = false
}

variable "kms_public_key_pem_path" {
  description = "Optional container path to the trusted Cloud KMS asymmetric signing public key PEM file used when require_kms_signature is true."
  type        = string
  default     = ""
}

variable "artifact_registry_repo_id" {
  description = "Artifact Registry Docker repository ID for workshop container images."
  type        = string
  default     = "agent-marketplace"
}

variable "skill_staging_bucket_name" {
  description = "Optional globally unique GCS bucket name for staging skill ZIP archives. Defaults to <project_id>-agent-registry-skills."
  type        = string
  default     = ""
}

variable "finops_billing_api_url" {
  description = "External REST API URL registered as the finops-billing-api Endpoint service in Agent Registry."
  type        = string
  default     = "https://cloudbilling.googleapis.com/v1"
}

variable "deletion_protection" {
  description = "Whether to enable Cloud Run deletion_protection on workshop services. Set true in production environments."
  type        = bool
  default     = false
}

variable "skills_bootstrap_command" {
  description = "Optional custom shell command override executed by terraform_data.bootstrap_starter_skills instead of the default plugin-sync invocation."
  type        = string
  default     = ""
}
