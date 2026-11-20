# Standalone Agent Registry Skills Bootstrapper (Dual-Source Ingestion)
#
# Why this uses `terraform_data` instead of a native Google provider resource:
#   `hashicorp/google` (>= 7.39.0) provides GA resources for `Service`
#   (`google_agent_registry_service`) and `Binding`
#   (`google_agent_registry_binding`), as well as read-only discovery data
#   sources, but does NOT yet expose a native `google_agent_registry_skill`
#   (or `SkillRevision`) resource.
#
# Dual-source ingestion via `tools.plugin_sync ingest` (calling the GA Agent
# Registry `v1` REST API `POST /v1/projects/{p}/locations/{l}/skills?skillId={id}`,
# or equivalently `gcloud agent-registry skills create`):
#   - Option 1 (`skills_source_mode = "local"`, default):
#     Publishes standalone skills from `starter_plugin_dir`
#     (`starter-marketplace/plugins/gcp-finops-toolkit`) and, when
#     `enable_local_standalone_skills = true`, also ingests pre-prepared
#     standalone skills from `local_skills_dir` (`local-skills`) grouped under
#     `local_skills_plugin_name` (`local-ops-toolkit`).
#   - Option 2 (`skills_source_mode = "git"`):
#     Shallow-clones `skills_repo_url` at `skills_repo_ref` (with optional
#     `skills_repo_subdir` and private token read from `skills_repo_token_env`),
#     auto-detects plugins and/or standalone skills, and ingests them with real
#     Git provenance.
#
# Ownership boundary (critical):
#   `agent_registry_services.tf` is the sole Terraform owner of
#   `google_agent_registry_service` resources (pointing to live Cloud Run URLs).
#   Every `tools.plugin_sync ingest` invocation below ALWAYS passes
#   `--only-skills` and `--skip-mcp-probe`, guaranteeing that ONLY standalone
#   `Skill` resources are published and Terraform-managed `google_agent_registry_service`
#   resources are NEVER overwritten or clobbered.
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

  starter_plugin_files = (
    var.skills_source_mode == "local"
    ? sort(try(fileset(local.starter_plugin_path, "**"), []))
    : []
  )

  starter_plugin_content_hash = (
    length(local.starter_plugin_files) > 0
    ? sha256(join("", [
      for rel_path in local.starter_plugin_files :
      "${rel_path}:${filesha256("${local.starter_plugin_path}/${rel_path}")}\n"
    ]))
    : ""
  )

  local_skills_path = (
    startswith(var.local_skills_dir, "/")
    ? var.local_skills_dir
    : abspath("${path.module}/${var.local_skills_dir}")
  )

  local_skills_files = (
    var.skills_source_mode == "local" && var.enable_local_standalone_skills
    ? sort(try(fileset(local.local_skills_path, "**"), []))
    : []
  )

  local_skills_content_hash = (
    length(local.local_skills_files) > 0
    ? sha256(join("", [
      for rel_path in local.local_skills_files :
      "${rel_path}:${filesha256("${local.local_skills_path}/${rel_path}")}\n"
    ]))
    : ""
  )
}

resource "terraform_data" "bootstrap_starter_skills" {
  count = var.enable_skills_bootstrap ? 1 : 0

  triggers_replace = {
    project_id                     = var.project_id
    location                       = var.location
    skills_source_mode             = var.skills_source_mode
    plugin_dir                     = local.starter_plugin_path
    plugin_content_hash            = local.starter_plugin_content_hash
    enable_local_standalone_skills = var.enable_local_standalone_skills
    local_skills_dir               = local.local_skills_path
    local_skills_plugin_name       = var.local_skills_plugin_name
    local_skills_content_hash      = local.local_skills_content_hash
    skills_repo_url                = var.skills_repo_url
    skills_repo_ref                = var.skills_repo_ref
    skills_repo_subdir             = var.skills_repo_subdir
    skills_repo_plugin_name        = var.skills_repo_plugin_name
    skills_per_skill_plugin        = var.skills_per_skill_plugin
    mcp_service_id                 = google_agent_registry_service.finops_mcp.service_id
    agent_service_id               = google_agent_registry_service.finops_a2a_agent.service_id
    staging_bucket                 = google_storage_bucket.skill_staging.name
  }

  lifecycle {
    precondition {
      condition     = var.skills_bootstrap_command != "" || var.skills_source_mode != "git" || length(trimspace(var.skills_repo_url)) > 0
      error_message = "skills_repo_url must be non-empty when skills_source_mode is set to \"git\" (unless skills_bootstrap_command overrides the bootstrapper)."
    }
  }

  provisioner "local-exec" {
    working_dir = local.repo_root
    interpreter = ["/bin/bash", "-euo", "pipefail", "-c"]

    environment = {
      PYTHONPATH                     = local.repo_root
      GCP_PROJECT_ID                 = var.project_id
      GCP_LOCATION                   = var.location
      AGENT_REGISTRY_BASE_URL        = "https://agentregistry.googleapis.com/v1"
      SKILL_STAGING_BUCKET           = google_storage_bucket.skill_staging.name
      SKILLS_SOURCE_MODE             = var.skills_source_mode
      PLUGIN_DIR                     = local.starter_plugin_path
      ENABLE_LOCAL_STANDALONE_SKILLS = tostring(var.enable_local_standalone_skills)
      LOCAL_SKILLS_DIR               = local.local_skills_path
      LOCAL_SKILLS_PLUGIN_NAME       = var.local_skills_plugin_name
      SKILLS_REPO_URL                = var.skills_repo_url
      SKILLS_REPO_REF                = var.skills_repo_ref
      SKILLS_REPO_SUBDIR             = var.skills_repo_subdir
      SKILLS_REPO_TOKEN_ENV          = var.skills_repo_token_env
      SKILLS_REPO_PLUGIN_NAME        = var.skills_repo_plugin_name
      SKILLS_PER_SKILL_PLUGIN        = tostring(var.skills_per_skill_plugin)
      MCP_SERVICE_ID                 = google_agent_registry_service.finops_mcp.service_id
      AGENT_SERVICE_ID               = google_agent_registry_service.finops_a2a_agent.service_id
      CUSTOM_BOOTSTRAP_CMD           = var.skills_bootstrap_command
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
      # Passing --only-skills and --skip-mcp-probe guarantees agent_registry_services.tf
      # remains the sole Terraform owner of google_agent_registry_service resources.
      COMMON_ARGS=(
        --registry-url "$AGENT_REGISTRY_BASE_URL"
        --project "$GCP_PROJECT_ID"
        --location "$GCP_LOCATION"
        --target-state TARGET_STATE_ACTIVE
        --scanning-policy ENABLED_BLOCKING
        --only-skills
        --skip-mcp-probe
      )

      if [ "$SKILLS_SOURCE_MODE" = "local" ]; then
        # Option 1A: Ingest standalone skills from the starter agent-plugins.org plugin.
        "$PYTHON_BIN" -m tools.plugin_sync ingest \
          --source-type local \
          --path "$PLUGIN_DIR" \
          "$${COMMON_ARGS[@]}"

        # Option 1B: Optionally ingest pre-prepared standalone local skills.
        if [ "$ENABLE_LOCAL_STANDALONE_SKILLS" = "true" ] && [ -d "$LOCAL_SKILLS_DIR" ]; then
          LOCAL_STANDALONE_ARGS=(
            --source-type local
            --path "$LOCAL_SKILLS_DIR"
            --plugin-name "$LOCAL_SKILLS_PLUGIN_NAME"
          )
          if [ "$SKILLS_PER_SKILL_PLUGIN" = "true" ]; then
            LOCAL_STANDALONE_ARGS+=(--per-skill-plugin)
          fi

          "$PYTHON_BIN" -m tools.plugin_sync ingest \
            "$${LOCAL_STANDALONE_ARGS[@]}" \
            "$${COMMON_ARGS[@]}"
        fi
      elif [ "$SKILLS_SOURCE_MODE" = "git" ]; then
        # Option 2: Shallow-clone and ingest a centralized GitLab or GitHub skills/plugins repo.
        trimmed_repo_url="$(printf '%s' "$SKILLS_REPO_URL" | tr -d '[:space:]')"
        if [ -z "$trimmed_repo_url" ]; then
          echo "ERROR: SKILLS_REPO_URL (skills_repo_url) must be non-empty when skills_source_mode = \"git\"." >&2
          echo "Set skills_repo_url in terraform.tfvars (for example, https://gitlab.example.com/ai-platform/agent-marketplace.git) or switch skills_source_mode = \"local\"." >&2
          exit 1
        fi

        GIT_ARGS=(
          --source-type git
          --repo-url "$SKILLS_REPO_URL"
          --plugin-name "$SKILLS_REPO_PLUGIN_NAME"
        )
        if [ -n "$SKILLS_REPO_REF" ]; then
          GIT_ARGS+=(--ref "$SKILLS_REPO_REF")
        fi
        if [ -n "$SKILLS_REPO_SUBDIR" ]; then
          GIT_ARGS+=(--subdir "$SKILLS_REPO_SUBDIR")
        fi
        if [ -n "$SKILLS_REPO_TOKEN_ENV" ]; then
          GIT_ARGS+=(--git-token-env "$SKILLS_REPO_TOKEN_ENV")
        fi
        if [ "$SKILLS_PER_SKILL_PLUGIN" = "true" ]; then
          GIT_ARGS+=(--per-skill-plugin)
        fi

        "$PYTHON_BIN" -m tools.plugin_sync ingest \
          "$${GIT_ARGS[@]}" \
          "$${COMMON_ARGS[@]}"
      else
        echo "ERROR: Unsupported SKILLS_SOURCE_MODE=\"$SKILLS_SOURCE_MODE\". Expected \"local\" or \"git\"." >&2
        exit 1
      fi
    EOT
  }

  depends_on = [
    google_project_service.core["agentregistry.googleapis.com"],
    google_storage_bucket_iam_member.agent_registry_p4sa_skill_reader,
    google_agent_registry_service.finops_mcp,
    google_agent_registry_service.finops_a2a_agent,
  ]
}
