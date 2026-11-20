# Required Google Cloud APIs for Workstream B (Security & Governance).
# Safe to run alongside terraform/core/ or standalone against an existing project
# because disable_on_destroy is false.

locals {
  security_governance_apis = toset([
    "agentidentity.googleapis.com",
    "agentregistry.googleapis.com",
    "cloudkms.googleapis.com",
    "iap.googleapis.com",
    "logging.googleapis.com",
    "networkservices.googleapis.com",
    "orgpolicy.googleapis.com",
  ])
}

resource "google_project_service" "security_governance" {
  for_each = local.security_governance_apis

  project                    = var.project_id
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false
}
