---
name: cost-anomaly-triage
description: Investigate unexpected Google Cloud Billing cost spikes safely using read-only FinOps telemetry, SKU delta breakdown, and structured root-cause checklists.
license: Apache-2.0
compatibility: Requires finops-mcp-server (read-only billing summary and BigQuery analysis tools) and GCP Cloud Billing export visibility.
metadata:
  plugin_name: "gcp-finops-toolkit"
  plugin_display_name: "GCP FinOps Toolkit"
  plugin_version: "1.0.0"
  mcp_service_id: "finops-mcp-server"
  agent_service_id: "finops-advisor-agent"
---

# Cost Anomaly Triage

Use this skill whenever a user reports an unexpected Google Cloud spend increase, budget alert, or BigQuery/GCE/Cloud Run SKU anomaly.

## Safety & Governance Rules

1. **Read-only first**: Start exclusively with read-only telemetry (`get_billing_summary` and `analyze_bq_query`).
2. **Never invoke destructive remediation automatically**: Do NOT call `delete_idle_dataset` unless the user explicitly approves deletion and holds an authorized IAM/IAP egress grant.
3. **Cite exact figures**: Always quote project ID, billing window, SKU name, baseline spend, observed spend, and percentage delta.

## Triage Workflow

1. **Gather baseline and observed billing data**
   - Call `get_billing_summary` via `finops-mcp-server` for the affected `project_id` and `days` window (default `7`, expand to `30` for seasonality).
2. **Walk the structured checklist**
   - Follow every phase in `references/triage-checklist.md` to isolate whether the spike originates from BigQuery on-demand analysis, storage growth, Compute Engine autoscaling, or data egress.
3. **Inspect top offending queries or resources**
   - When BigQuery Analysis accounts for >25% of the delta, pass candidate SQL queries to `analyze_bq_query` to quantify bytes scanned and missing partition filters.
4. **Produce a root-cause brief**
   - Deliver a concise report with:
     - **Impact**: Total USD delta and annualized run-rate.
     - **Primary Drivers**: Top 3 SKUs/projects with percentage share.
     - **Immediate Mitigations**: Low-risk guardrails (query caps, partition enforcement, label attribution).
     - **Follow-Up Owner**: Recommended team or FinOps escalation channel.
