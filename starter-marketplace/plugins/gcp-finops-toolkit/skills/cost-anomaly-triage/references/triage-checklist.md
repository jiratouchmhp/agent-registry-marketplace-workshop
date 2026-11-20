# Cloud Billing Cost Anomaly Triage Checklist

Use this checklist sequentially during a `cost-anomaly-triage` investigation.

## Phase 1 — Scope & Baseline Verification
- [ ] Confirm target `project_id`, billing account scope, and alert window (UTC).
- [ ] Compare daily spend against trailing 7-day and 28-day moving averages.
- [ ] Verify whether the anomaly aligns with expected cyclical jobs (month-end close, quarterly ML backfill, BI refresh).

## Phase 2 — Service & SKU Attribution
- [ ] Rank top 5 services by absolute USD increase (`BigQuery`, `Compute Engine`, `Cloud Storage`, `Cloud Networking`, `Vertex AI`).
- [ ] Within the top service, isolate the exact SKU (e.g., `Analysis` vs `Active Logical Storage` vs `Inter-region Egress`).
- [ ] Check resource labels (`cost_center`, `env`, `owner`, `pipeline`) to identify the owning workload.

## Phase 3 — BigQuery-Specific Checks
- [ ] Identify queries scanning > 100 GiB per run or lacking `_PARTITIONDATE` / partition column predicates.
- [ ] Run `analyze_bq_query` on suspect SQL to detect `SELECT *`, unpartitioned scans, cross joins, or missing `LIMIT` / clustering keys.
- [ ] Check whether on-demand pricing vs. Capacity Reservations (`Enterprise` / `Standard` autoscaling slots) is appropriate.

## Phase 4 — Safe Mitigation & Escalation
- [ ] Recommend non-destructive guardrails first (`maximum_bytes_billed`, budget alerts, query rewrite, slot caps).
- [ ] For idle dataset cleanup candidates, document last-queried timestamp, table size, and owner approval before requesting any destructive action (`delete_idle_dataset`).
- [ ] Record the triage summary and open a tracking ticket if annualized impact exceeds $5,000/yr.
