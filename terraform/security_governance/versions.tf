# Workstream B — Security & Governance Infrastructure for Google Cloud Agent Registry
#
# Designed to run EITHER layered on top of Workstream A (`terraform/core/`) OR
# standalone against any existing Google Cloud Agent Registry project.
#
# Uses the GA hashicorp/google provider (>= 7.39.0). No google-beta provider is
# required for any resource in this stack.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.39.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.location
}

data "google_project" "current" {
  project_id = var.project_id
}

locals {
  # Per-product per-project service agent (P4SA) for Agent Registry.
  agent_registry_p4sa = "service-${data.google_project.current.number}@gcp-sa-agentregistry.iam.gserviceaccount.com"

  # Agent Registry CMEK skill locations (`us` or `eu`) map to Cloud KMS
  # multi-region locations (`us` or `europe`). Specifically, `eu` skills
  # MUST use a Cloud KMS key in location `europe`.
  skill_cmek_location = var.cmek_location == "europe" ? "eu" : var.cmek_location

  # Allow standalone runs without passing ci_publisher_sa_email explicitly by
  # defaulting to the Workstream A naming convention when omitted.
  ci_publisher_sa_email = (
    trimspace(var.ci_publisher_sa_email) != ""
    ? trimspace(var.ci_publisher_sa_email)
    : "agent-registry-ci-publisher@${var.project_id}.iam.gserviceaccount.com"
  )

  # Default IAP egressor principal to the middleware runtime SA unless an
  # explicit agent workload identity (e.g., principal://agents.global...) is supplied.
  iap_egressor_member = (
    trimspace(var.iap_egressor_member) != ""
    ? trimspace(var.iap_egressor_member)
    : "serviceAccount:${var.middleware_sa_email}"
  )
}
