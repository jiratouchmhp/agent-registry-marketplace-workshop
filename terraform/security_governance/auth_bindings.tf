# Agent Identity AuthProvider & Agent Registry Binding (GA, hashicorp/google >= 7.39.0)
#
# Connects the FinOps A2A Agent (source URN) to the FinOps MCP Server (target URN)
# with scoped, delegated OAuth 2.0 credentials via `google_agent_identity_auth_provider`.
#
# Remember:
#   - `auth_provider_binding` is REQUIRED on `google_agent_registry_binding`.
#   - `GET .../bindings:fetchAvailable?sourceIdentifier=...&targetIdentifier=...`
#     actively filters out bindings whose AuthProvider the caller lacks
#     `roles/agentidentity.user` on.

resource "google_agent_identity_auth_provider" "finops_oauth" {
  project          = var.project_id
  location         = var.location
  auth_provider_id = var.auth_provider_id
  description      = "Scoped three-legged OAuth 2.0 AuthProvider for FinOps Agent-to-MCP delegated access."

  allowed_scopes = var.auth_provider_scopes

  # Explicitly block high-privilege directory/admin scopes as defense in depth.
  blocked_scopes = [
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/admin.directory.group",
  ]

  auth_provider_type_params {
    three_legged_oauth {
      client_id            = var.auth_provider_client_id
      client_secret        = var.auth_provider_client_secret
      authorization_url    = var.auth_provider_authorization_url
      token_url            = var.auth_provider_token_url
      enable_pkce          = true
      default_continue_uri = var.auth_provider_continue_uri
    }
  }

  depends_on = [google_project_service.security_governance["agentidentity.googleapis.com"]]
}

resource "google_agent_registry_binding" "finops_agent_to_mcp" {
  project         = var.project_id
  location        = var.location
  binding_id      = var.binding_id
  display_name    = "FinOps Advisor Agent -> FinOps MCP Server"
  description     = "Governed OAuth credential delegation binding from the FinOps A2A Agent to the FinOps MCP Server."
  deletion_policy = "DELETE"

  source {
    identifier = var.a2a_agent_urn
  }

  target {
    identifier = var.mcp_server_urn
  }

  auth_provider_binding {
    auth_provider = google_agent_identity_auth_provider.finops_oauth.name
    scopes        = var.auth_provider_scopes
    continue_uri  = var.auth_provider_continue_uri
  }

  depends_on = [
    google_project_service.security_governance["agentregistry.googleapis.com"],
    google_agent_identity_auth_provider.finops_oauth,
  ]
}
