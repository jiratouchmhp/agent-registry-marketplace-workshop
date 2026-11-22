# Workstream A Core outputs.
# These outputs stand on their own and also feed cleanly into optional
# downstream Workstream B stacks (Bindings, Agent Gateway, IAP, KMS, Audit Logs).

output "project_id" {
  description = "Google Cloud project ID hosting the Agent Registry and marketplace infrastructure."
  value       = var.project_id
}

output "project_number" {
  description = "Numeric Google Cloud project number used for Agent Registry URNs and P4SA identities."
  value       = data.google_project.current.number
}

output "location" {
  description = "Primary Google Cloud region for Agent Registry services and Cloud Run workloads (default: asia-southeast3, Bangkok)."
  value       = var.location
}

output "secondary_location" {
  description = "Secondary Google Cloud region for optional active-standby Cloud Run and Artifact Registry deployment (default: asia-southeast1, Singapore)."
  value       = var.secondary_location
}

output "agent_registry_location" {
  description = "Effective Google Cloud location for regional Agent Registry Services and discovery data sources (defaults to var.location)."
  value       = local.agent_registry_location
}

output "skills_location" {
  description = "Google Cloud multi-region location for standalone Agent Registry v1 Skill resources (global, us, or eu; default: global)."
  value       = var.skills_location
}

output "agent_registry_p4sa" {
  description = "Per-product per-project service agent (P4SA) for Agent Registry."
  value       = local.agent_registry_p4sa
}

output "middleware_url" {
  description = "Base HTTPS URL of the polyglot-middleware Cloud Run service."
  value       = google_cloud_run_v2_service.polyglot_middleware.uri
}

output "middleware_mcp_url" {
  description = "Streamable HTTP MCP endpoint URL exposed by polyglot-middleware."
  value       = "${google_cloud_run_v2_service.polyglot_middleware.uri}/mcp"
}

output "secondary_middleware_url" {
  description = "Base HTTPS URL of the optional secondary-region polyglot-middleware Cloud Run service (empty when enable_secondary_region is false)."
  value       = try(google_cloud_run_v2_service.polyglot_middleware_secondary[0].uri, "")
}

output "secondary_middleware_mcp_url" {
  description = "Streamable HTTP MCP endpoint URL exposed by the optional secondary-region polyglot-middleware Cloud Run service (empty when enable_secondary_region is false)."
  value       = try("${google_cloud_run_v2_service.polyglot_middleware_secondary[0].uri}/mcp", "")
}

output "middleware_sa_email" {
  description = "Least-privilege service account email for polyglot-middleware (roles/agentregistry.viewer only)."
  value       = google_service_account.middleware_sa.email
}

output "ci_publisher_sa_email" {
  description = "CI/CD publisher service account email (roles/agentregistry.editor)."
  value       = google_service_account.ci_publisher_sa.email
}

output "skill_author_sa_email" {
  description = "Standalone skill author service account email (roles/agentregistry.user)."
  value       = google_service_account.skill_author_sa.email
}

output "sample_mcp_server_url" {
  description = "Base HTTPS URL of the sample-mcp-server Cloud Run service."
  value       = google_cloud_run_v2_service.sample_mcp_server.uri
}

output "sample_a2a_agent_url" {
  description = "Base HTTPS URL of the sample-a2a-agent Cloud Run service."
  value       = google_cloud_run_v2_service.sample_a2a_agent.uri
}

output "artifact_registry_repository_url" {
  description = "Docker image push/pull prefix for the workshop Artifact Registry repository."
  value       = "${google_artifact_registry_repository.workshop_images.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.workshop_images.repository_id}"
}

output "secondary_artifact_registry_repository_url" {
  description = "Docker image push/pull prefix for the optional secondary-region Artifact Registry repository (empty when enable_secondary_region is false)."
  value       = try("${google_artifact_registry_repository.workshop_images_secondary[0].location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.workshop_images_secondary[0].repository_id}", "")
}

output "skill_staging_bucket_name" {
  description = "GCS bucket name used to stage skill ZIP archives for Agent Registry gcsSource ingestion."
  value       = google_storage_bucket.skill_staging.name
}

output "mcp_service_name" {
  description = "Full resource name of the writable finops-mcp-server Service resource."
  value       = google_agent_registry_service.finops_mcp.name
}

output "mcp_registry_resource" {
  description = "Full resource name of the projected read-only McpServer resource."
  value       = google_agent_registry_service.finops_mcp.registry_resource
}

output "mcp_server_resource_id" {
  description = "Short resource ID of the projected read-only McpServer (for IAP IAM bindings and discovery lookups)."
  value       = data.google_agent_registry_mcp_server.finops_mcp.mcp_server_id
}

output "mcp_server_urn" {
  description = "Canonical Agent Registry URN of the finops-mcp-server McpServer."
  value       = data.google_agent_registry_mcp_server.finops_mcp.urn
}

output "agent_service_name" {
  description = "Full resource name of the writable finops-advisor-agent Service resource."
  value       = google_agent_registry_service.finops_a2a_agent.name
}

output "agent_registry_resource" {
  description = "Full resource name of the projected read-only Agent resource."
  value       = google_agent_registry_service.finops_a2a_agent.registry_resource
}

output "a2a_agent_resource_id" {
  description = "Short resource ID of the projected read-only A2A Agent (for IAP IAM bindings and discovery lookups)."
  value       = data.google_agent_registry_agent.finops_a2a_agent.agent_id
}

output "a2a_agent_urn" {
  description = "Canonical Agent Registry URN of the finops-advisor-agent Agent."
  value       = data.google_agent_registry_agent.finops_a2a_agent.urn
}

output "endpoint_service_name" {
  description = "Full resource name of the writable finops-billing-api Service resource."
  value       = google_agent_registry_service.finops_endpoint.name
}

output "endpoint_registry_resource" {
  description = "Full resource name of the projected read-only Endpoint resource."
  value       = google_agent_registry_service.finops_endpoint.registry_resource
}

output "endpoint_resource_id" {
  description = "Short resource ID of the projected read-only Endpoint (for IAP IAM bindings and discovery lookups)."
  value       = data.google_agent_registry_endpoint.finops_endpoint.endpoint_id
}

output "endpoint_urn" {
  description = "Canonical Agent Registry URN of the finops-billing-api Endpoint."
  value       = data.google_agent_registry_endpoint.finops_endpoint.urn
}
