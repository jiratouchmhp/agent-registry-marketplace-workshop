---
name: bq-query-optimizer
description: Analyze BigQuery SQL queries for full table scans, missing partition or clustering filters, SELECT * anti-patterns, and excessive bytes billed.
license: Apache-2.0
compatibility: Requires BigQuery Standard SQL input; pairs with finops-mcp-server analyze_bq_query tool.
metadata:
  plugin_name: "gcp-finops-toolkit"
  plugin_display_name: "GCP FinOps Toolkit"
  plugin_version: "1.0.0"
  mcp_service_id: "finops-mcp-server"
  agent_service_id: "finops-advisor-agent"
---

# BigQuery Query Optimizer

Use this skill to review, diagnose, and rewrite BigQuery Standard SQL queries to minimize bytes scanned, slot-milliseconds, and on-demand cost while preserving exact result semantics.

## Optimization Protocol

1. **Run automated static & cost analysis**
   - Invoke `analyze_bq_query` via `finops-mcp-server` with the user's SQL and optional `project_id`.
2. **Eliminate column over-fetching**
   - Replace `SELECT *` with explicit column lists (BigQuery is columnar; each pruned column directly reduces bytes billed).
   - Remind the user that `LIMIT` does **not** reduce bytes billed on non-clustered tables.
3. **Enforce partition & cluster pruning**
   - Ensure a sargable filter on the partition column (e.g. `WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)`) with no function wrapping the partition column (`DATE(ts)` can defeat pruning unless partitioning is on `ts`).
   - Order `WHERE` predicates to align with table clustering columns.
4. **Optimize joins, aggregations, and CTEs**
   - Place the largest table on the left side of `JOIN` clauses and filter early before joining.
   - Replace `COUNT(DISTINCT x)` with `APPROX_COUNT_DISTINCT(x)` when approximate cardinality is acceptable.
   - Avoid repeated evaluation of expensive CTEs by materializing intermediate results into temporary tables when referenced multiple times.
5. **Validate with dry-run & safety caps**
   - Recommend setting `maximum_bytes_billed` in client/job configs and verifying estimated bytes with `bq query --dry_run`.
