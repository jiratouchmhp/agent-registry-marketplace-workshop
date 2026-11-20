---
trigger: glob
globs: "**/*.sql, **/*.tf"
---

# FinOps SQL & Terraform Guardrails

When authoring or reviewing `.sql` or `.tf` files:

1. **BigQuery SQL (`.sql`)**:
   - Never use `SELECT *` on partitioned or multi-gigabyte tables; project only required columns.
   - Require an explicit partition predicate (`_PARTITIONDATE`, `_PARTITIONTIME`, or partitioned date/timestamp column) on partitioned tables.
   - Avoid `CROSS JOIN` without an explicit cardinality justification.
2. **Terraform (`.tf`)**:
   - Attach FinOps attribution labels (`cost_center`, `env`, `owner`) to billable Google Cloud resources where supported.
   - Set `require_partition_filter = true` on new BigQuery tables and `default_table_expiration_ms` on scratch/staging datasets.
   - Prefer `deletion_protection = true` on production datasets and stateful databases.
