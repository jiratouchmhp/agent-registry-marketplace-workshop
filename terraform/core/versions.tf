# Workstream A — Core Agent Registry & Polyglot Marketplace Infrastructure
#
# Uses the GA hashicorp/google provider (>= 7.39.0). No google-beta provider is
# required: google_agent_registry_service and the Agent Registry discovery data
# sources are GA in >= 7.39.0.

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
