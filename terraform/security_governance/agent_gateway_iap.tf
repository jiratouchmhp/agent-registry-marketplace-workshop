# Network Services Agent Gateway & IAP Egressor IAM Bindings (GA)
#
# Regional architecture:
#   Aim for Bangkok (`asia-southeast3`) for all supported services; use Singapore
#   (`asia-southeast1`) only for services not yet available in Bangkok (Agent
#   Identity AuthProvider).
#
# Governs runtime agent-to-MCP and client-to-agent traffic against resources
# registered in Google Cloud Agent Registry (`var.location`, default Bangkok
# `asia-southeast3`). Pair with the IAM v3 Unified Access Policy
# (`security-governance/iap-uap/mcp-tool-annotations-uap.json`) to enforce
# tool-level annotation rules (`read_only_hint`, `destructive_hint`) at runtime.

# Optional API-mandated regional resource:
# Network Services Agent Gateway on Cloud Run is currently GA only in
# `us-central1` and `europe-west1`. Keep `enable_agent_gateway = false` by
# default for APAC deployments, or set `enable_agent_gateway = true` with
# `agent_gateway_location = "us-central1"` (or `"europe-west1"`).
resource "google_network_services_agent_gateway" "marketplace" {
  count = var.enable_agent_gateway ? 1 : 0

  project     = var.project_id
  name        = var.agent_gateway_name
  location    = var.agent_gateway_location
  description = "Google-managed Agent Gateway governing client-to-agent and MCP tool access via Agent Registry and IAP."

  google_managed {
    governed_access_path = "CLIENT_TO_AGENT"
  }

  registries = [
    "//agentregistry.googleapis.com/projects/${var.project_id}/locations/${var.location}",
  ]

  depends_on = [
    google_project_service.security_governance["networkservices.googleapis.com"],
    google_project_service.security_governance["agentregistry.googleapis.com"],
  ]
}

# Grant roles/iap.egressor on the registered FinOps MCP Server resource ID.
resource "google_iap_agent_registry_mcp_server_iam_member" "mcp_egressor" {
  project       = var.project_id
  location      = var.location
  mcp_server_id = var.mcp_server_resource_id
  role          = "roles/iap.egressor"
  member        = local.iap_egressor_member

  depends_on = [
    google_project_service.security_governance["iap.googleapis.com"],
    google_project_service.security_governance["agentregistry.googleapis.com"],
  ]
}

# Grant roles/iap.egressor on the registered FinOps A2A Agent resource ID.
resource "google_iap_agent_registry_agent_iam_member" "agent_egressor" {
  project  = var.project_id
  location = var.location
  agent_id = var.a2a_agent_resource_id
  role     = "roles/iap.egressor"
  member   = local.iap_egressor_member

  depends_on = [
    google_project_service.security_governance["iap.googleapis.com"],
    google_project_service.security_governance["agentregistry.googleapis.com"],
  ]
}
