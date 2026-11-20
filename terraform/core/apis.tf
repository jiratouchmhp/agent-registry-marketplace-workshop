# Core Google Cloud APIs required by Workstream A (Agent Registry, Cloud Run,
# Artifact Registry, Cloud Storage, Cloud Build, and IAM).

locals {
  core_apis = toset([
    "agentregistry.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
    "cloudbuild.googleapis.com",
    "iam.googleapis.com",
  ])
}

data "google_project" "current" {
  project_id = var.project_id
}

resource "google_project_service" "core" {
  for_each = local.core_apis

  project                    = var.project_id
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false
}
