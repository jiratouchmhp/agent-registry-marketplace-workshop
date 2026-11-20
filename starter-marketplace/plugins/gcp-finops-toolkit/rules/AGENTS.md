# Enterprise GCP FinOps Guardrails

- Always prefer read-only FinOps diagnostics (`get_billing_summary`, `analyze_bq_query`) before proposing any resource mutation.
- Never invoke destructive billing or dataset operations (`delete_idle_dataset`) without explicit user confirmation and verified Agent Gateway / IAP v2 egress authorization.
- Include exact project IDs, SKUs, estimated bytes scanned, and USD impact in every FinOps recommendation.
