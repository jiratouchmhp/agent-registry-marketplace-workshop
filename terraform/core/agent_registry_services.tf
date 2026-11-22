# Native GA Google Cloud Agent Registry Service registrations and read-only
# discovery data sources (hashicorp/google >= 7.39.0).
#
# Notes from Agent Registry v1 GA specification:
# - Service is the writable manual registration resource; creating a Service
#   projects a read-only McpServer, Agent, or Endpoint in `registry_resource`.
# - No generic `labels` map exists on Service, McpServer, Agent, or Endpoint.
# - For A2A_AGENT_CARD agents, endpoints MUST live inside `supportedInterfaces`
#   in the A2A v1.0 card JSON, and the top-level `interfaces` block MUST be
#   omitted.
# - Using basename(google_agent_registry_service.<r>.registry_resource) directly
#   resolves each projected resource ID without fragile filter/sleep hacks.

# -----------------------------------------------------------------------------
# 1. FinOps MCP Server Service (TOOL_SPEC + JSONRPC interface)
# -----------------------------------------------------------------------------

resource "google_agent_registry_service" "finops_mcp" {
  project         = var.project_id
  location        = local.agent_registry_location
  service_id      = "finops-mcp-server"
  display_name    = "FinOps Cost & BigQuery MCP Server"
  description     = "Governed MCP server exposing read-only Cloud Billing and BigQuery SQL analysis tools alongside a destructive idle BigQuery dataset deletion tool with explicit MCP tool annotations."
  deletion_policy = "DELETE"

  interfaces {
    url              = "${google_cloud_run_v2_service.sample_mcp_server.uri}/mcp"
    protocol_binding = "JSONRPC"
  }

  mcp_server_spec {
    type = "TOOL_SPEC"
    content = jsonencode({
      tools = [
        {
          name        = "get_billing_summary"
          description = "Return read-only Google Cloud Billing spend summary, baseline comparison, and top SKU deltas for a GCP project."
          inputSchema = {
            type = "object"
            properties = {
              project_id = {
                type        = "string"
                description = "Target GCP project ID (e.g. acme-agent-governance)."
              }
              days = {
                type        = "integer"
                description = "Lookback window in days (1-90)."
                default     = 7
                minimum     = 1
                maximum     = 90
              }
            }
            required             = ["project_id"]
            additionalProperties = false
          }
          annotations = {
            title           = "Get Cloud Billing Summary"
            readOnlyHint    = true
            destructiveHint = false
            idempotentHint  = true
            openWorldHint   = false
          }
        },
        {
          name        = "analyze_bq_query"
          description = "Inspect a BigQuery Standard SQL query for full-table scans, SELECT *, missing partition filters, and estimated on-demand bytes billed."
          inputSchema = {
            type = "object"
            properties = {
              sql = {
                type        = "string"
                description = "BigQuery Standard SQL query text to analyze."
              }
              project_id = {
                type        = "string"
                description = "Optional GCP project ID owning the target datasets."
              }
            }
            required             = ["sql"]
            additionalProperties = false
          }
          annotations = {
            title           = "Analyze BigQuery SQL Query"
            readOnlyHint    = true
            destructiveHint = false
            idempotentHint  = true
            openWorldHint   = false
          }
        },
        {
          name        = "delete_idle_dataset"
          description = "Permanently delete an idle BigQuery dataset to reclaim storage spend. Destructive action intended to be gated by Agent Gateway + IAP v2 UAP."
          inputSchema = {
            type = "object"
            properties = {
              project_id = {
                type        = "string"
                description = "GCP project ID containing the dataset."
              }
              dataset_id = {
                type        = "string"
                description = "BigQuery dataset ID to delete."
              }
              confirm_delete = {
                type        = "boolean"
                description = "Must be true to execute deletion; false runs a dry-run check."
                default     = false
              }
              reason = {
                type        = "string"
                description = "Audit justification / ticket reference for dataset removal."
              }
            }
            required             = ["project_id", "dataset_id"]
            additionalProperties = false
          }
          annotations = {
            title           = "Delete Idle BigQuery Dataset"
            readOnlyHint    = false
            destructiveHint = true
            idempotentHint  = false
            openWorldHint   = false
          }
        },
      ]
    })
  }

  depends_on = [google_project_service.core["agentregistry.googleapis.com"]]
}

# -----------------------------------------------------------------------------
# 2. FinOps Advisor A2A Agent Service (A2A_AGENT_CARD, NO top-level interfaces)
# -----------------------------------------------------------------------------

resource "google_agent_registry_service" "finops_a2a_agent" {
  project         = var.project_id
  location        = local.agent_registry_location
  service_id      = "finops-advisor-agent"
  display_name    = "GCP FinOps Advisor"
  description     = "Enterprise A2A v1.0 FinOps specialist that analyzes GCP Cloud Billing anomalies, BigQuery query efficiency, and safe cost-remediation plans."
  deletion_policy = "DELETE"

  # IMPORTANT: Top-level `interfaces` MUST be omitted for type = "A2A_AGENT_CARD".
  # Connection details are declared inside `supportedInterfaces` in the card JSON.
  agent_spec {
    type = "A2A_AGENT_CARD"
    content = jsonencode({
      name        = "GCP FinOps Advisor"
      description = "Enterprise A2A v1.0 FinOps specialist that analyzes GCP Cloud Billing anomalies, BigQuery query efficiency, and safe cost-remediation plans."
      version     = "1.0.0"
      provider = {
        organization = "Acme Platform Security"
        url          = "https://acme.example.com/platform"
      }
      supportedInterfaces = [
        {
          url             = "${google_cloud_run_v2_service.sample_a2a_agent.uri}/a2a"
          protocolBinding = "JSONRPC"
          protocolVersion = "1.0"
        },
        {
          url             = "${google_cloud_run_v2_service.sample_a2a_agent.uri}/a2a/http"
          protocolBinding = "HTTP+JSON"
          protocolVersion = "1.0"
        },
      ]
      capabilities = {
        streaming         = false
        pushNotifications = false
      }
      defaultInputModes  = ["text/plain", "application/json"]
      defaultOutputModes = ["text/plain", "application/json"]
      skills = [
        {
          id          = "cost-anomaly-triage"
          name        = "Cost Anomaly Triage"
          description = "Diagnose unexpected GCP Cloud Billing spend spikes by service, SKU, and project labels without making destructive changes."
          tags        = ["finops", "billing", "anomaly", "gcp"]
          examples = [
            "Why did BigQuery spend in acme-analytics jump 45% yesterday?",
            "Triage the top 3 SKUs contributing to our weekly Cloud Billing alert.",
          ]
        },
        {
          id          = "bq-query-optimization"
          name        = "BigQuery Query Optimization"
          description = "Inspect BigQuery Standard SQL queries for full table scans, missing partition filters, and SELECT * anti-patterns, then propose safe rewrites."
          tags        = ["finops", "bigquery", "sql", "cost-optimization"]
          examples = [
            "Optimize SELECT * FROM analytics.events for lower bytes scanned.",
            "Estimate on-demand savings from adding a _PARTITIONDATE filter.",
          ]
        },
      ]
    })
  }

  depends_on = [google_project_service.core["agentregistry.googleapis.com"]]
}

# -----------------------------------------------------------------------------
# 3. FinOps Billing API Endpoint Service (NO_SPEC + HTTP_JSON interface)
# -----------------------------------------------------------------------------

resource "google_agent_registry_service" "finops_endpoint" {
  project         = var.project_id
  location        = local.agent_registry_location
  service_id      = "finops-billing-api"
  display_name    = "FinOps Cloud Billing API Endpoint"
  description     = "External Cloud Billing REST API endpoint registered for governed agent egress policies."
  deletion_policy = "DELETE"

  interfaces {
    url              = var.finops_billing_api_url
    protocol_binding = "HTTP_JSON"
  }

  endpoint_spec {
    type = "NO_SPEC"
  }

  depends_on = [google_project_service.core["agentregistry.googleapis.com"]]
}

# -----------------------------------------------------------------------------
# Read-Only Discovery Data Sources (Direct ID Lookup via registry_resource)
# -----------------------------------------------------------------------------

data "google_agent_registry_mcp_server" "finops_mcp" {
  project       = var.project_id
  location      = local.agent_registry_location
  mcp_server_id = basename(google_agent_registry_service.finops_mcp.registry_resource)
}

data "google_agent_registry_agent" "finops_a2a_agent" {
  project  = var.project_id
  location = local.agent_registry_location
  agent_id = basename(google_agent_registry_service.finops_a2a_agent.registry_resource)
}

data "google_agent_registry_endpoint" "finops_endpoint" {
  project     = var.project_id
  location    = local.agent_registry_location
  endpoint_id = basename(google_agent_registry_service.finops_endpoint.registry_resource)
}
