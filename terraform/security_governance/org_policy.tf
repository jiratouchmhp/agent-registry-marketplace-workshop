# Organization Policy Custom Constraint: Require Agent Registry Skill Scanning
#
# Toggleable (`enable_org_policy = true` + non-empty `org_id`) so workshop
# participants without Organization Policy Administrator (`roles/orgpolicy.policyAdmin`)
# privileges can apply the rest of Workstream B cleanly.
#
# Standalone CLI alternative: `security-governance/org-policy/require-skill-scanning.yaml`.

resource "google_org_policy_custom_constraint" "require_skill_scanning" {
  count = var.enable_org_policy && var.org_id != "" ? 1 : 0

  name         = "custom.requireAgentRegistrySkillScanning"
  parent       = "organizations/${var.org_id}"
  display_name = "Prevent Disabling Agent Registry Skill Scanning"
  description  = "Denies creating or updating Agent Registry Skills when scanningConfig.policy is DISABLED."

  resource_types = ["agentregistry.googleapis.com/Skill"]
  method_types   = ["CREATE", "UPDATE"]
  condition      = "resource.scanningConfig.policy == 'DISABLED'"
  action_type    = "DENY"

  depends_on = [google_project_service.security_governance["orgpolicy.googleapis.com"]]
}

resource "google_org_policy_policy" "enforce_skill_scanning" {
  count = var.enable_org_policy && var.org_id != "" ? 1 : 0

  name   = "organizations/${var.org_id}/policies/${google_org_policy_custom_constraint.require_skill_scanning[0].name}"
  parent = "organizations/${var.org_id}"

  spec {
    rules {
      enforce = "TRUE"
    }
  }
}
