# Cloud Run v2 workloads for the FinOps sample MCP server, FinOps sample A2A
# agent, and the polyglot Agent Marketplace middleware.

locals {
  ar_image_prefix = "${var.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.workshop_images.repository_id}"

  sample_mcp_image = (
    var.sample_mcp_image != ""
    ? var.sample_mcp_image
    : "${local.ar_image_prefix}/sample-mcp-server:latest"
  )

  sample_a2a_image = (
    var.sample_a2a_image != ""
    ? var.sample_a2a_image
    : "${local.ar_image_prefix}/sample-a2a-agent:latest"
  )

  middleware_image = (
    var.middleware_image != ""
    ? var.middleware_image
    : "${local.ar_image_prefix}/polyglot-middleware:latest"
  )

  # Deterministic Cloud Run default URLs avoid Terraform self-reference cycles
  # while ensuring services advertise HTTPS URLs behind Cloud Run TLS termination.
  sample_a2a_public_url = "https://sample-a2a-agent-${data.google_project.current.number}.${var.location}.run.app"

  middleware_public_url = (
    var.middleware_public_url != ""
    ? var.middleware_public_url
    : "https://polyglot-middleware-${data.google_project.current.number}.${var.location}.run.app"
  )
}

# -----------------------------------------------------------------------------
# 1. Sample FinOps MCP Server
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "sample_mcp_server" {
  project             = var.project_id
  name                = "sample-mcp-server"
  location            = var.location
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.sample_workloads_sa.email

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      image = local.sample_mcp_image

      ports {
        container_port = 8091
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "GCP_LOCATION"
        value = var.location
      }

      resources {
        cpu_idle = true
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8091
        }
        period_seconds    = 10
        timeout_seconds   = 3
        failure_threshold = 3
      }
    }
  }

  depends_on = [google_project_service.core["run.googleapis.com"]]
}

# -----------------------------------------------------------------------------
# 2. Sample FinOps A2A Agent
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "sample_a2a_agent" {
  project             = var.project_id
  name                = "sample-a2a-agent"
  location            = var.location
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.sample_workloads_sa.email

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      image = local.sample_a2a_image

      ports {
        container_port = 8092
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "GCP_LOCATION"
        value = var.location
      }

      env {
        name  = "A2A_PUBLIC_URL"
        value = local.sample_a2a_public_url
      }

      resources {
        cpu_idle = true
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8092
        }
        period_seconds    = 10
        timeout_seconds   = 3
        failure_threshold = 3
      }
    }
  }

  depends_on = [google_project_service.core["run.googleapis.com"]]
}

# -----------------------------------------------------------------------------
# 3. Polyglot Agent Marketplace Middleware
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "polyglot_middleware" {
  project             = var.project_id
  name                = "polyglot-middleware"
  location            = var.location
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.middleware_sa.email

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    containers {
      image = local.middleware_image

      ports {
        container_port = 8080
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "GCP_LOCATION"
        value = var.location
      }

      env {
        name  = "AGENT_REGISTRY_BASE_URL"
        value = "https://agentregistry.googleapis.com/v1"
      }

      env {
        name  = "MIDDLEWARE_PUBLIC_URL"
        value = local.middleware_public_url
      }

      # Core defaults REQUIRE_KMS_SIGNATURE to "false"; Workstream B enables
      # Cloud KMS asymmetric signing and flips var.require_kms_signature = true.
      env {
        name  = "REQUIRE_KMS_SIGNATURE"
        value = var.require_kms_signature ? "true" : "false"
      }

      env {
        name  = "KMS_PUBLIC_KEY_PEM_PATH"
        value = var.kms_public_key_pem_path
      }

      resources {
        cpu_idle = true
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        period_seconds    = 10
        timeout_seconds   = 3
        failure_threshold = 3
      }
    }
  }

  depends_on = [
    google_project_service.core["run.googleapis.com"],
    google_project_iam_member.middleware_sa_agent_registry_viewer,
  ]
}

# -----------------------------------------------------------------------------
# Optional Unauthenticated Workshop Demo Access (OFF by default)
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service_iam_member" "middleware_unauthenticated_demo" {
  count = var.allow_unauthenticated_workshop_demo ? 1 : 0

  project  = google_cloud_run_v2_service.polyglot_middleware.project
  location = google_cloud_run_v2_service.polyglot_middleware.location
  name     = google_cloud_run_v2_service.polyglot_middleware.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "sample_mcp_unauthenticated_demo" {
  count = var.allow_unauthenticated_workshop_demo ? 1 : 0

  project  = google_cloud_run_v2_service.sample_mcp_server.project
  location = google_cloud_run_v2_service.sample_mcp_server.location
  name     = google_cloud_run_v2_service.sample_mcp_server.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "sample_a2a_unauthenticated_demo" {
  count = var.allow_unauthenticated_workshop_demo ? 1 : 0

  project  = google_cloud_run_v2_service.sample_a2a_agent.project
  location = google_cloud_run_v2_service.sample_a2a_agent.location
  name     = google_cloud_run_v2_service.sample_a2a_agent.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
