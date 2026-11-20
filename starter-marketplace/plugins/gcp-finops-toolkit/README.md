# GCP FinOps Toolkit (`gcp-finops-toolkit`)

Enterprise-governed FinOps plugin built on the **Agent Plugins 1.0** standard (`agent-plugins.org`) and synced to **Google Cloud Agent Registry (`v1`)** for multi-IDE consumption across Google Antigravity, Claude Code, VS Code / GitHub Copilot, and OpenAI Codex.

## What's Included

| Component | Name | Description |
| :--- | :--- | :--- |
| **Skill** | `cost-anomaly-triage` | Structured workflow and checklist for investigating Cloud Billing spend anomalies using read-only telemetry. |
| **Skill** | `bq-query-optimizer` | BigQuery Standard SQL review playbook targeting full table scans, partition pruning, clustering, and `SELECT *` elimination. |
| **MCP Server** | `finops-mcp-server` | Streamable HTTP MCP server exposing `get_billing_summary`, `analyze_bq_query` (read-only), and `delete_idle_dataset` (destructive, gated via Agent Gateway + IAP v2 UAP). |
| **A2A Agent** | `finops-advisor-agent` | A2A v1.0 FinOps advisor exposing `/.well-known/agent-card.json` and `/a2a` for multi-turn cost remediation advice. |
| **Rules** | `rules/finops-guardrails.md` & `rules/AGENTS.md` | Glob-scoped SQL/Terraform FinOps checks (`**/*.sql, **/*.tf`) and always-on safety rules. |
| **Hooks** | `hooks.json` | Named `policy-guard` hook blocking unapproved destructive operations locally before tool execution. |

## Governance & Security

- **Security Scanning**: Every skill revision is uploaded to Agent Registry with `scanningConfig.policy = ENABLED_BLOCKING`. Revisions with any `VULNERABILITY` or `SECRET` finding remain quarantined in `STATE_DRAFT`.
- **Cryptographic Provenance**: Release pipelines sign each deterministic skill ZIP SHA-256 digest using a Cloud KMS Asymmetric Signing Key (`EC_SIGN_P256_SHA256`) and record `kms_signature` and `kms_key_version` in `SKILL.md` frontmatter metadata.
- **Runtime Tool Governance**: Destructive MCP tools (`destructiveHint: true`, such as `delete_idle_dataset`) are evaluated at egress by Agent Gateway + IAP v2 IAM Unified Access Policies (`iap.googleapis.com/resources.egressViaIAP`).

## Quick Start

1. Install from your organization's Enterprise Agent Marketplace in Antigravity, Claude Code, VS Code / GitHub Copilot, or Codex.
2. Try one of the suggested prompts:
   - *"Triage yesterday's Cloud Billing cost anomaly across GCP projects"*
   - *"Analyze my BigQuery SQL query to reduce bytes scanned and cost"*
   - *"Summarize the top GCP SKUs driving spend in the current billing period"*
