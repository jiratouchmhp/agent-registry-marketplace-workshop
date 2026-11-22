# Workstream B — Security & Governance Outputs
# Includes the canonical output keys declared in WORKSHOP_SPEC.json so downstream
# automation, CI signing pipelines, and Workstream A middleware configuration can
# consume them consistently.

output "agent_registry_p4sa" {
  description = "Per-product per-project service agent (P4SA) granted CMEK encrypter/decrypter access."
  value       = local.agent_registry_p4sa
}

output "skill_cmek_location" {
  description = "Agent Registry multi-region skill location corresponding to var.cmek_location (us -> us, europe -> eu)."
  value       = local.skill_cmek_location
}

output "cmek_crypto_key_id" {
  description = "Full Cloud KMS resource ID of the symmetric ENCRYPT_DECRYPT key for Agent Registry Skill CMEK."
  value       = google_kms_crypto_key.skill_cmek.id
}

output "signing_crypto_key_id" {
  description = "Full Cloud KMS resource ID of the asymmetric EC_SIGN_P256_SHA256 key for cryptographic skill artifact signing."
  value       = google_kms_crypto_key.skill_artifact_signer.id
}

output "auth_provider_name" {
  description = "Full resource name of the Agent Identity OAuth AuthProvider."
  value       = google_agent_identity_auth_provider.finops_oauth.name
}

output "auth_provider_location" {
  description = "Google Cloud region hosting the Agent Identity AuthProvider (defaults to asia-southeast1, Singapore, as a service-specific fallback ONLY because Agent Identity is GA in Singapore and not yet available in Bangkok)."
  value       = var.auth_provider_location
}

output "binding_name" {
  description = "Full resource name of the Agent Registry Binding connecting the A2A agent to the MCP server."
  value       = google_agent_registry_binding.finops_agent_to_mcp.name
}

output "agent_gateway_name" {
  description = "Name of the optional Google-managed Network Services Agent Gateway (empty when enable_agent_gateway is false)."
  value       = try(google_network_services_agent_gateway.marketplace[0].name, "")
}

output "agent_gateway_id" {
  description = "Full resource ID of the optional Google-managed Network Services Agent Gateway (empty when enable_agent_gateway is false)."
  value       = try(google_network_services_agent_gateway.marketplace[0].id, "")
}

output "audit_log_bucket_id" {
  description = "Full resource ID of the locked WORM Cloud Logging bucket for agent governance audit logs."
  value       = google_logging_project_bucket_config.agent_governance_audit.id
}

output "audit_log_sink_writer_identity" {
  description = "Unique writer identity for the agent-governance-audit Cloud Logging sink."
  value       = google_logging_project_sink.agent_governance_audit.writer_identity
}

output "org_policy_custom_constraint_name" {
  description = "Full name of the optional custom Organization Policy constraint denying DISABLED skill scanning (empty when disabled)."
  value       = length(google_org_policy_custom_constraint.require_skill_scanning) > 0 ? google_org_policy_custom_constraint.require_skill_scanning[0].name : ""
}
