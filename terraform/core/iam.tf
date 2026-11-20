# Least-privilege IAM identities, Artifact Registry Docker repository, and GCS
# skill staging bucket with P4SA read access for Agent Registry ingestion.

locals {
  agent_registry_p4sa = "service-${data.google_project.current.number}@gcp-sa-agentregistry.iam.gserviceaccount.com"

  skill_staging_bucket_name = (
    var.skill_staging_bucket_name != ""
    ? var.skill_staging_bucket_name
    : "${var.project_id}-agent-registry-skills"
  )
}

# -----------------------------------------------------------------------------
# Least-Privilege Service Accounts
# -----------------------------------------------------------------------------

# 1. Runtime identity for the polyglot IDE middleware: read-only viewer ONLY.
#    NEVER grant roles/agentregistry.editor or roles/agentregistry.admin to
#    serving middleware or runtime agents.
resource "google_service_account" "middleware_sa" {
  project      = var.project_id
  account_id   = "agent-marketplace-middleware"
  display_name = "Polyglot Agent Marketplace Middleware"
  description  = "Least-privilege runtime identity for polyglot-middleware with read-only Agent Registry viewer access."

  depends_on = [google_project_service.core["iam.googleapis.com"]]
}

resource "google_project_iam_member" "middleware_sa_agent_registry_viewer" {
  project = var.project_id
  role    = "roles/agentregistry.viewer"
  member  = "serviceAccount:${google_service_account.middleware_sa.email}"

  depends_on = [google_project_service.core["agentregistry.googleapis.com"]]
}

# 2. CI/CD publisher identity (GitLab CI / Cloud Build): registers Services and
#    Skills via roles/agentregistry.editor, but CANNOT modify Bindings, Findings,
#    Skill IAM policies, or SkillEncryptionSpec (those require admin).
resource "google_service_account" "ci_publisher_sa" {
  project      = var.project_id
  account_id   = "agent-registry-ci-publisher"
  display_name = "Agent Registry CI Publisher"
  description  = "CI/CD publisher identity that registers Agent Registry Services and Skills without admin privileges."

  depends_on = [google_project_service.core["iam.googleapis.com"]]
}

resource "google_project_iam_member" "ci_publisher_sa_agent_registry_editor" {
  project = var.project_id
  role    = "roles/agentregistry.editor"
  member  = "serviceAccount:${google_service_account.ci_publisher_sa.email}"

  depends_on = [google_project_service.core["agentregistry.googleapis.com"]]
}

# 3. Standalone Skill author identity: demonstrates roles/agentregistry.user
#    (viewer + standalone Skills CRUD only, without Service registration).
resource "google_service_account" "skill_author_sa" {
  project      = var.project_id
  account_id   = "agent-registry-skill-author"
  display_name = "Agent Registry Standalone Skill Author"
  description  = "Demonstrates standalone-skill-only authoring with roles/agentregistry.user."

  depends_on = [google_project_service.core["iam.googleapis.com"]]
}

resource "google_project_iam_member" "skill_author_sa_agent_registry_user" {
  project = var.project_id
  role    = "roles/agentregistry.user"
  member  = "serviceAccount:${google_service_account.skill_author_sa.email}"

  depends_on = [google_project_service.core["agentregistry.googleapis.com"]]
}

# 4. Isolated zero-privilege runtime identity for the FinOps sample MCP server
#    and A2A agent workloads so they never fall back to the default Compute SA.
resource "google_service_account" "sample_workloads_sa" {
  project      = var.project_id
  account_id   = "finops-sample-workloads"
  display_name = "FinOps Sample MCP & A2A Workloads"
  description  = "Zero-Agent-Registry-privilege runtime identity for sample-mcp-server and sample-a2a-agent."

  depends_on = [google_project_service.core["iam.googleapis.com"]]
}

# -----------------------------------------------------------------------------
# Artifact Registry Docker Repository
# -----------------------------------------------------------------------------

resource "google_artifact_registry_repository" "workshop_images" {
  project       = var.project_id
  location      = var.location
  repository_id = var.artifact_registry_repo_id
  description   = "Docker container images for polyglot-middleware, sample-mcp-server, and sample-a2a-agent."
  format        = "DOCKER"

  depends_on = [google_project_service.core["artifactregistry.googleapis.com"]]
}

resource "google_artifact_registry_repository_iam_member" "ci_publisher_ar_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.workshop_images.location
  repository = google_artifact_registry_repository.workshop_images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ci_publisher_sa.email}"
}

# -----------------------------------------------------------------------------
# GCS Skill Staging Bucket & Agent Registry P4SA Access
# -----------------------------------------------------------------------------

resource "google_storage_bucket" "skill_staging" {
  project                     = var.project_id
  name                        = local.skill_staging_bucket_name
  location                    = var.location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.core["storage.googleapis.com"]]
}

# Agent Registry imports GCS-staged skill ZIPs (gcsSource) using its per-product
# per-project service agent (P4SA). Grant strictly bucket-level objectViewer.
resource "google_storage_bucket_iam_member" "agent_registry_p4sa_skill_reader" {
  bucket = google_storage_bucket.skill_staging.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${local.agent_registry_p4sa}"

  depends_on = [google_project_service.core["agentregistry.googleapis.com"]]
}

resource "google_storage_bucket_iam_member" "ci_publisher_skill_staging_user" {
  bucket = google_storage_bucket.skill_staging.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.ci_publisher_sa.email}"
}

resource "google_storage_bucket_iam_member" "skill_author_skill_staging_user" {
  bucket = google_storage_bucket.skill_staging.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.skill_author_sa.email}"
}
