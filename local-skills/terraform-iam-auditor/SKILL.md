---
name: terraform-iam-auditor
description: Audit Terraform Google Cloud IAM configurations for least-privilege violations including primitive roles, public allUsers bindings, and over-privileged autonomous agent access to Agent Registry.
license: Apache-2.0
compatibility: Static read-only audit skill for Terraform HCL (`.tf`, `.tfvars`) repositories using the `hashicorp/google` provider.
metadata:
  category: "security-governance"
  domain: "terraform-iam"
---

# Terraform IAM Auditor

Use this skill whenever reviewing Terraform pull requests, auditing Google Cloud IAM modules, or validating service account permissions for workloads, CI/CD pipelines, and autonomous AI agents.

## Mandatory Security Invariants

1. **Reject primitive roles in production bindings**: Flag every assignment of `roles/owner`, `roles/editor`, and broad project/organization `roles/viewer` where a scoped predefined or custom role exists.
2. **Flag public principals immediately**: Flag any binding granting permissions to `allUsers` or `allAuthenticatedUsers` unless explicitly documented as an intentional public static asset or public gateway with compensating WAF/DDoS controls.
3. **Protect Agent Registry from autonomous agent privilege escalation**:
   - **CRITICAL FINDING**: Explicitly flag granting `roles/agentregistry.editor` or `roles/agentregistry.admin` to any autonomous agent runtime service account, AI runtime identity, or general developer/IDE group!
   - Autonomous agents and developer IDE identities must receive at most `roles/agentregistry.viewer` (or skill-level `roles/agentregistry.viewer`) plus runtime egress controls (`roles/iap.egressor` via IAP Unified Access Policies), while `roles/agentregistry.editor` and `roles/agentregistry.admin` must be restricted strictly to CI/CD publication pipelines and platform security administrators.
4. **Prefer additive IAM resources**: Flag authoritative resources (`google_project_iam_policy`, `google_project_iam_binding`, `google_folder_iam_policy`, `google_organization_iam_policy`) that risk overwriting out-of-band or service-agent bindings; recommend `*_iam_member` resources with narrow IAM Conditions.
5. **Forbid static service account keys**: Flag `google_service_account_key` resources and recommend Workload Identity Federation (OIDC/WIF) or attached service accounts instead.

## Audit Workflow

1. **Inventory all IAM resources and variables**
   - Scan all `.tf` and `.tfvars` files for `*_iam_member`, `*_iam_binding`, `*_iam_policy`, `google_service_account`, `google_service_account_key`, and `google_iap_agent_registry_*_iam_member` blocks.
   - Trace variable indirection (e.g., `for_each`, `locals`, and `var.*_roles`) so violations hidden in lists or maps are not missed.
2. **Evaluate against the least-privilege checklist**
   - Check every binding systematically using `references/least-privilege-checklist.md`.
   - Verify resource-level scoping (bucket, secret, KMS crypto key, Cloud Run service, or specific Agent Registry skill) is used instead of project-wide grants wherever possible.
   - Verify IAM Conditions (`condition { title, description, expression }`) constrain temporary grants, environment tags, or destructive tool access (`destination.agent_registry.mcp_server.tool.annotations.destructive_hint`).
3. **Produce a severity-ranked audit report**
   - Group findings by **CRITICAL**, **HIGH**, **MEDIUM**, and **LOW**.
   - For each finding, report:
     - **Location**: File path, line range, and Terraform resource address.
     - **Principal & Role**: Exact `member`/`members` and `role`.
     - **Risk**: Concrete escalation or exposure path.
     - **Least-Privilege Remediation**: Drop-in HCL replacement snippet.
