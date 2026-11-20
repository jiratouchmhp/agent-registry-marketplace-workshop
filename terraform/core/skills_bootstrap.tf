# Standalone Agent Registry Skills Bootstrapper
#
# Why this uses `terraform_data` instead of a native Google provider resource:
#   `hashicorp/google` (>= 7.39.0) provides GA resources for `Service`
#   (`google_agent_registry_service`) and `Binding`
#   (`google_agent_registry_binding`), as well as read-only discovery data
#   sources, but does NOT yet expose a native `google_agent_registry_skill`
#   (or `SkillRevision`) resource.
#
# Therefore, standalone skills under `starter-marketplace/plugins/gcp-finops-toolkit/`
# are published via the workshop `tools.plugin_sync` helper (which calls the GA
# Agent Registry `v1` REST API `POST /v1/projects/{p}/locations/{l}/skills?skillId={id}`,
# or equivalently `gcloud agent-registry skills create`).
#
# Ownership boundary (critical):
#   `agent_registry_services.tf` is the sole Terraform owner of
#   `google_agent_registry_service` resources (pointing to live Cloud Run URLs).
#   The bootstrapper builds a `plugin-sync` plan with `probe_mcp=False` and
#   clears `plan.mcp_services` and `plan.agent_services` before calling
#   `apply_sync_plan(...)`, so ONLY standalone `Skill` resources are uploaded
#   without probing loopback URLs or clobbering Terraform-managed Services.
#
# Remember: Agent Registry automatically prefixes private skills with `private-`
# (for example, `skillId=cost-anomaly-triage` creates
# `projects/{p}/locations/{l}/skills/private-cost-anomaly-triage`).
#
# Set `enable_skills_bootstrap = false` to manage skill publishing exclusively
# from your CI/CD pipeline (GitLab CI / Cloud Build) instead of Terraform.

locals {
  repo_root = abspath("${path.module}/../..")

  starter_plugin_path = (
    startswith(var.starter_plugin_dir, "/")
    ? var.starter_plugin_dir
    : abspath("${path.module}/${var.starter_plugin_dir}")
  )

  starter_plugin_files = sort(fileset(local.starter_plugin_path, "**"))

  starter_plugin_content_hash = sha256(join("", [
    for rel_path in local.starter_plugin_files :
    "${rel_path}:${filesha256("${local.starter_plugin_path}/${rel_path}")}\n"
  ]))
}

resource "terraform_data" "bootstrap_starter_skills" {
  count = var.enable_skills_bootstrap ? 1 : 0

  triggers_replace = {
    project_id          = var.project_id
    location            = var.location
    plugin_dir          = local.starter_plugin_path
    plugin_content_hash = local.starter_plugin_content_hash
    mcp_service_id      = google_agent_registry_service.finops_mcp.service_id
    agent_service_id    = google_agent_registry_service.finops_a2a_agent.service_id
    staging_bucket      = google_storage_bucket.skill_staging.name
  }

  provisioner "local-exec" {
    working_dir = local.repo_root
    interpreter = ["/bin/bash", "-euo", "pipefail", "-c"]

    environment = {
      PYTHONPATH              = local.repo_root
      GCP_PROJECT_ID          = var.project_id
      GCP_LOCATION            = var.location
      AGENT_REGISTRY_BASE_URL = "https://agentregistry.googleapis.com/v1"
      SKILL_STAGING_BUCKET    = google_storage_bucket.skill_staging.name
      PLUGIN_DIR              = local.starter_plugin_path
      MCP_SERVICE_ID          = google_agent_registry_service.finops_mcp.service_id
      AGENT_SERVICE_ID        = google_agent_registry_service.finops_a2a_agent.service_id
      CUSTOM_BOOTSTRAP_CMD    = var.skills_bootstrap_command
    }

    command = <<-EOT
      if [ -n "$CUSTOM_BOOTSTRAP_CMD" ]; then
        eval "$CUSTOM_BOOTSTRAP_CMD"
        exit 0
      fi

      if [ -x "${local.repo_root}/.venv/bin/python" ]; then
        PYTHON_BIN="${local.repo_root}/.venv/bin/python"
      elif command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
      else
        echo "ERROR: Neither repo .venv nor python3 was found." >&2
        echo "Install the workshop venv, set skills_bootstrap_command, or set enable_skills_bootstrap = false and publish with gcloud agent-registry skills / REST v1." >&2
        exit 1
      fi

      # Publish ONLY standalone Skill resources via plugin-sync (Agent Registry v1 REST API).
      # MCP and A2A Service registrations remain solely owned by agent_registry_services.tf.
      "$PYTHON_BIN" - << 'PYEOF'
      import json
      import os

      from tools.plugin_sync.sync import apply_sync_plan, build_sync_plan

      plan = build_sync_plan(
          os.environ["PLUGIN_DIR"],
          project=os.environ["GCP_PROJECT_ID"],
          location=os.environ["GCP_LOCATION"],
          target_state="TARGET_STATE_ACTIVE",
          scanning_policy="ENABLED_BLOCKING",
          probe_mcp=False,
      )
      # Prevent overwriting Terraform-managed Cloud Run Service registrations.
      plan.mcp_services.clear()
      plan.agent_services.clear()

      result = apply_sync_plan(
          plan,
          registry_url=os.environ["AGENT_REGISTRY_BASE_URL"],
          dry_run=False,
      )
      print(json.dumps(result, indent=2))
      PYEOF
    EOT
  }

  depends_on = [
    google_project_service.core["agentregistry.googleapis.com"],
    google_storage_bucket_iam_member.agent_registry_p4sa_skill_reader,
    google_agent_registry_service.finops_mcp,
    google_agent_registry_service.finops_a2a_agent,
  ]
}
