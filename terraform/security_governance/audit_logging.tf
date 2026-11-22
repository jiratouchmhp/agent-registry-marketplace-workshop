# Audit Logging, Data Access Config, and Immutable (WORM) Log Retention
#
# 1. Enables ADMIN_READ Data Access audit logs on BOTH:
#    - `agentregistry.googleapis.com` (skills/services/agents discovery & reads)
#    - `apphubendpoint.googleapis.com` (backing workload resolution lookups)
# 2. Creates a locked Write-Once-Read-Many (WORM) Cloud Logging bucket with
#    Log Analytics enabled so compliance teams can run BigQuery SQL queries
#    directly over immutable logs (`security-governance/audit/compliance-queries.md`).
# 3. Routes Admin Activity, Data Access, and Agent Gateway request logs into
#    the locked WORM bucket via a dedicated project sink.
#
# WARNING: Setting `lock_audit_bucket = true` (`locked = true` on
# `google_logging_project_bucket_config`) is IRREVERSIBLE once applied in Google
# Cloud and is STRONGLY RECOMMENDED for production compliance (WORM). A locked
# bucket's retention period cannot be reduced, and the bucket cannot be deleted
# until its retention window expires and all log entries age out. Keep
# `lock_audit_bucket = false` only in ephemeral workshop sandboxes.

resource "google_project_iam_audit_config" "agent_registry" {
  project = var.project_id
  service = "agentregistry.googleapis.com"

  audit_log_config {
    log_type = "ADMIN_READ"
  }

  depends_on = [google_project_service.security_governance["agentregistry.googleapis.com"]]
}

resource "google_project_iam_audit_config" "apphub_endpoint" {
  project = var.project_id
  service = "apphubendpoint.googleapis.com"

  audit_log_config {
    log_type = "ADMIN_READ"
  }
}

resource "google_logging_project_bucket_config" "agent_governance_audit" {
  project          = var.project_id
  location         = var.location
  bucket_id        = var.audit_bucket_id
  description      = "Locked WORM audit log bucket for Agent Registry, App Hub Endpoint, IAP egress, KMS signing, and Agent Gateway traffic."
  retention_days   = var.audit_retention_days
  locked           = var.lock_audit_bucket
  enable_analytics = true

  depends_on = [google_project_service.security_governance["logging.googleapis.com"]]
}

resource "google_logging_project_sink" "agent_governance_audit" {
  name                   = "agent-governance-audit-sink"
  project                = var.project_id
  description            = "Captures Agent Registry/App Hub/IAP/KMS audit logs and Network Services Agent Gateway request logs into the locked WORM bucket."
  destination            = "logging.googleapis.com/${google_logging_project_bucket_config.agent_governance_audit.id}"
  unique_writer_identity = true

  filter = <<-EOT
    (
      log_id("cloudaudit.googleapis.com/activity")
      AND protoPayload.serviceName=(
        "agentregistry.googleapis.com"
        OR "apphubendpoint.googleapis.com"
        OR "agentidentity.googleapis.com"
        OR "iap.googleapis.com"
        OR "networkservices.googleapis.com"
        OR "cloudkms.googleapis.com"
      )
    )
    OR (
      log_id("cloudaudit.googleapis.com/data_access")
      AND protoPayload.serviceName=(
        "agentregistry.googleapis.com"
        OR "apphubendpoint.googleapis.com"
        OR "iap.googleapis.com"
        OR "cloudkms.googleapis.com"
      )
    )
    OR log_id("networkservices.googleapis.com/gateway_requests")
  EOT
}

resource "google_project_iam_member" "audit_sink_bucket_writer" {
  project = var.project_id
  role    = "roles/logging.bucketWriter"
  member  = google_logging_project_sink.agent_governance_audit.writer_identity
}
