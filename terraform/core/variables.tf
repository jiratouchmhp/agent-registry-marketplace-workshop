variable "project_id" {
  description = "Google Cloud project ID hosting Agent Registry, Cloud Run services, Artifact Registry, and skill staging storage."
  type        = string

  validation {
    condition     = length(trimspace(var.project_id)) > 0
    error_message = "project_id must be a non-empty Google Cloud project ID."
  }
}

variable "location" {
  description = "Primary Google Cloud region for Agent Registry services, Cloud Run workloads, Artifact Registry, and skill staging storage (default: asia-southeast3, Bangkok)."
  type        = string
  default     = "asia-southeast3"

  validation {
    condition     = length(trimspace(var.location)) > 0
    error_message = "location must be a non-empty Google Cloud region (for example, asia-southeast3)."
  }
}

variable "secondary_location" {
  description = "Secondary Google Cloud region for optional active-standby Cloud Run and Artifact Registry deployment (default: asia-southeast1, Singapore)."
  type        = string
  default     = "asia-southeast1"

  validation {
    condition     = length(trimspace(var.secondary_location)) > 0
    error_message = "secondary_location must be a non-empty Google Cloud region (for example, asia-southeast1)."
  }
}

variable "enable_secondary_region" {
  description = "When true, deploys active-standby Cloud Run workloads and an Artifact Registry repository in secondary_location (asia-southeast1, Singapore)."
  type        = bool
  default     = false
}

variable "agent_registry_location" {
  description = "Optional Google Cloud location for regional Agent Registry Services (MCP Servers, A2A Agents, Endpoints). Leave empty to default to var.location (asia-southeast3), or override to decouple the Agent Registry Services region from Cloud Run compute."
  type        = string
  default     = ""
}

variable "skills_location" {
  description = "Google Cloud multi-region location for standalone Agent Registry v1 Skill resources (\"global\", \"us\", or \"eu\"). Agent Registry v1 standalone Skill resources are hosted only in global, us, or eu while Services (MCP Servers, A2A Agents, Endpoints) and Cloud Run workloads run regionally in Bangkok (asia-southeast3) and Singapore (asia-southeast1)."
  type        = string
  default     = "global"

  validation {
    condition     = contains(["global", "us", "eu"], var.skills_location)
    error_message = "skills_location must be one of \"global\", \"us\", or \"eu\" (Agent Registry v1 standalone Skill resources are only supported in global, us, or eu)."
  }
}

variable "force_destroy_staging_bucket" {
  description = "When true, allows Terraform to destroy the GCS skill staging bucket even when it still contains staged ZIP objects. Keep false in production; set true in ephemeral workshop sandboxes so terraform destroy cleans up cleanly."
  type        = bool
  default     = false
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

variable "secondary_middleware_image" {
  description = "Optional container image URI for the secondary-region polyglot-middleware Cloud Run service when enable_secondary_region is true. Leave empty to default to the secondary Artifact Registry repository in secondary_location."
  type        = string
  default     = ""
}

variable "secondary_sample_mcp_image" {
  description = "Optional container image URI for the secondary-region sample-mcp-server Cloud Run service when enable_secondary_region is true. Leave empty to default to the secondary Artifact Registry repository in secondary_location."
  type        = string
  default     = ""
}

variable "secondary_sample_a2a_image" {
  description = "Optional container image URI for the secondary-region sample-a2a-agent Cloud Run service when enable_secondary_region is true. Leave empty to default to the secondary Artifact Registry repository in secondary_location."
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

variable "skills_source_mode" {
  description = "Skill ingestion source mode for the bootstrapper: \"local\" (Option 1: local starter plugin plus optional standalone local skills) or \"git\" (Option 2: shallow-clone and ingest a centralized GitLab/GitHub repository)."
  type        = string
  default     = "local"

  validation {
    condition     = contains(["local", "git"], var.skills_source_mode)
    error_message = "skills_source_mode must be either \"local\" or \"git\"."
  }
}

variable "enable_local_standalone_skills" {
  description = "When true and skills_source_mode is \"local\", also ingests standalone skills from local_skills_dir alongside starter_plugin_dir."
  type        = bool
  default     = true
}

variable "local_skills_dir" {
  description = "Local path (relative to terraform/core/ or absolute) to pre-prepared standalone agentskills.io skills ingested when skills_source_mode is \"local\" and enable_local_standalone_skills is true."
  type        = string
  default     = "../../local-skills"
}

variable "local_skills_plugin_name" {
  description = "Logical plugin bundle name assigned to standalone skills ingested from local_skills_dir."
  type        = string
  default     = "local-ops-toolkit"

  validation {
    condition     = length(var.local_skills_plugin_name) <= 64 && can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", var.local_skills_plugin_name))
    error_message = "local_skills_plugin_name must be lowercase kebab-case (1-64 chars, matching ^[a-z0-9]+(-[a-z0-9]+)*$)."
  }
}

variable "skills_repo_url" {
  description = "Centralized GitLab or GitHub repository URL to shallow-clone and ingest when skills_source_mode is \"git\"."
  type        = string
  default     = ""
}

variable "skills_repo_ref" {
  description = "Git branch, tag, or commit ref to clone when skills_source_mode is \"git\"."
  type        = string
  default     = "main"

  validation {
    condition     = length(trimspace(var.skills_repo_ref)) > 0
    error_message = "skills_repo_ref must be a non-empty Git branch, tag, or commit ref."
  }
}

variable "skills_repo_subdir" {
  description = "Optional relative subdirectory inside the cloned Git repository to ingest (for example, \"plugins\" or \"skills\")."
  type        = string
  default     = ""

  validation {
    condition     = var.skills_repo_subdir == "" || (!startswith(var.skills_repo_subdir, "/") && !can(regex("(^|/)\\.\\.(/|$)", var.skills_repo_subdir)))
    error_message = "skills_repo_subdir must be empty or a relative path without '..' traversal segments."
  }
}

variable "skills_repo_token_env" {
  description = "Optional NAME of a local environment variable holding a private Git token (for example, \"GITLAB_TOKEN\" or \"GITHUB_TOKEN\"). NEVER place raw tokens in .tfvars files."
  type        = string
  default     = ""

  validation {
    condition     = var.skills_repo_token_env == "" || can(regex("^[A-Za-z_][A-Za-z0-9_]*$", var.skills_repo_token_env))
    error_message = "skills_repo_token_env must be empty or a valid environment variable name (for example, GITLAB_TOKEN). Never place raw tokens in .tfvars files."
  }
}

variable "skills_repo_plugin_name" {
  description = "Default logical plugin bundle name assigned to standalone skills discovered inside the cloned Git repository when skills_source_mode is \"git\"."
  type        = string
  default     = "central-repo-skills"

  validation {
    condition     = length(var.skills_repo_plugin_name) <= 64 && can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", var.skills_repo_plugin_name))
    error_message = "skills_repo_plugin_name must be lowercase kebab-case (1-64 chars, matching ^[a-z0-9]+(-[a-z0-9]+)*$)."
  }
}

variable "skills_per_skill_plugin" {
  description = "When true, passes --per-skill-plugin to tools.plugin_sync ingest so each standalone skill is emitted as its own logical plugin bundle."
  type        = bool
  default     = false
}
