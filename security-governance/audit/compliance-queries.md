# Compliance & Forensic Audit Queries for Agent Registry, IAP, and Agent Gateway

This playbook provides verified **Cloud Logging** filters and **BigQuery Log Analytics**
SQL queries targeting the locked WORM audit bucket provisioned by
`terraform/security_governance/audit_logging.tf` (`agent-governance-audit`).

> **Prerequisites**:
> - `ADMIN_READ` Data Access audit logging enabled for both `agentregistry.googleapis.com`
>   and `apphubendpoint.googleapis.com` (`terraform/security_governance/audit_logging.tf`).
> - Locked WORM Cloud Logging bucket (`locked = true`, `enable_analytics = true`) receiving
>   `cloudaudit.googleapis.com/activity`, `cloudaudit.googleapis.com/data_access`, and
>   `networkservices.googleapis.com/gateway_requests`.

---

## 1. Agent Registry Config & Lifecycle Tampering (Admin Activity)

Detect creation, mutation, deletion, scanning policy downgrades, IAM policy edits,
binding mutations, and CMEK `SkillEncryptionSpec` changes. Admin Activity logs are
always enabled on `agentregistry.googleapis.com`.

```text
log_id("cloudaudit.googleapis.com/activity")
protoPayload.serviceName="agentregistry.googleapis.com"
(
  protoPayload.methodName=~"CreateService|UpdateService|DeleteService"
  OR protoPayload.methodName=~"CreateSkill|UpdateSkill|DeleteSkill|CreateSkillRevision"
  OR protoPayload.methodName=~"UpdateSkillEncryptionSpec"
  OR protoPayload.methodName=~"CreateBinding|UpdateBinding|DeleteBinding"
  OR protoPayload.methodName=~"SetIamPolicy"
)
```

### Alert: Attempt to Disable Skill Vulnerability Scanning (`scanningConfig.policy = DISABLED`)

```text
log_id("cloudaudit.googleapis.com/activity")
protoPayload.serviceName="agentregistry.googleapis.com"
protoPayload.methodName=~"(CreateSkill|UpdateSkill)"
protoPayload.request.skill.scanningConfig.policy="DISABLED"
```

---

## 2. Data Access Discovery & Resolution Logs (`ADMIN_READ`)

Track which principals list, search, or download Skills, Services, MCP servers,
and App Hub workload resolutions. Requires `ADMIN_READ` Data Access enabled on
**both** `agentregistry.googleapis.com` and `apphubendpoint.googleapis.com`.

```text
log_id("cloudaudit.googleapis.com/data_access")
(
  protoPayload.serviceName="agentregistry.googleapis.com"
  OR protoPayload.serviceName="apphubendpoint.googleapis.com"
)
```

### Narrow to Skill Archive Downloads & Searches

```text
log_id("cloudaudit.googleapis.com/data_access")
protoPayload.serviceName="agentregistry.googleapis.com"
protoPayload.methodName=~"(SearchSkills|GetSkill|GetSkillRevision|ListSkills)"
```

---

## 3. Agent Gateway & IAP `egressViaIAP` Violations

Inspect runtime authorization decisions enforced by IAP Unified Access Policies
(UAP) on traffic transiting the Network Services Agent Gateway.

### Denied Tool or Agent Invocations (`granted = false`)

```text
protoPayload.serviceName="iap.googleapis.com"
protoPayload.authorizationInfo.permission="iap.resources.egressViaIAP"
protoPayload.authorizationInfo.granted=false
```

### Unregistered Destination Attempts (Shadow MCP / Shadow Agent Egress)

```text
protoPayload.serviceName="iap.googleapis.com"
protoPayload.authorizationInfo.permission="iap.resources.egressViaIAP"
labels."iap.googleapis.com/audited_resource_name"="unregisteredResource"
```

### IAP UAP Dry-Run Mode Impact Report (`iamEnforcementMode = "DRY_RUN"`)

Before switching a UAP policy from `DRY_RUN` to enforced mode, inspect every call
that *would* have been denied:

```text
protoPayload.serviceName="iap.googleapis.com"
protoPayload.authorizationInfo.permission="iap.resources.egressViaIAP"
protoPayload.metadata.iamEnforcementMode="DRY_RUN"
protoPayload.authorizationInfo.granted=false
```

### Network Services Agent Gateway HTTP 403 Request Logs

```text
log_id("networkservices.googleapis.com/gateway_requests")
resource.type="networkservices.googleapis.com/Gateway"
resource.labels.gateway_type="SECURE_WEB_GATEWAY"
httpRequest.status=403
-httpRequest.requestMethod="CONNECT"
```

---

## 4. Cloud KMS Asymmetric Signing & CMEK Audit Trail

Verify that only the CI publisher SA invokes `AsymmetricSign` on the signing key
and monitor `GetPublicKey` calls from middleware.

```text
log_id("cloudaudit.googleapis.com/data_access")
protoPayload.serviceName="cloudkms.googleapis.com"
protoPayload.methodName=~"(AsymmetricSign|GetPublicKey|Decrypt|Encrypt)"
```

---

## 5. BigQuery Log Analytics SQL Queries (WORM Bucket `_AllLogs`)

Run these queries in **Cloud Logging → Log Analytics** (or BigQuery against the
linked dataset for the `agent-governance-audit` bucket). Replace
`PROJECT_ID` and `REGION` with your project and bucket location (for example,
`acme-agent-governance` and `us-central1`).

### 5.1 Top Denied IAP `egressViaIAP` Calls by Principal and Destination (Last 7 Days)

```sql
SELECT
  timestamp,
  proto_payload.audit_log.authentication_info.principal_subject AS caller_principal,
  auth.resource AS target_resource,
  JSON_VALUE(labels, '$."iap.googleapis.com/audited_resource_name"') AS audited_resource,
  JSON_VALUE(proto_payload.audit_log.metadata, '$.iamEnforcementMode') AS enforcement_mode,
  COUNT(*) AS denied_count
FROM
  `PROJECT_ID.REGION.agent-governance-audit._AllLogs`,
  UNNEST(proto_payload.audit_log.authorization_info) AS auth
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND proto_payload.audit_log.service_name = 'iap.googleapis.com'
  AND auth.permission = 'iap.resources.egressViaIAP'
  AND auth.granted = FALSE
GROUP BY
  1, 2, 3, 4, 5
ORDER BY
  denied_count DESC,
  timestamp DESC
LIMIT 100;
```

### 5.2 Agent Registry Admin Activity & Scanning Policy Governance Report (Last 30 Days)

```sql
SELECT
  DATE(timestamp) AS audit_date,
  proto_payload.audit_log.authentication_info.principal_email AS actor_email,
  proto_payload.audit_log.method_name AS api_method,
  proto_payload.audit_log.resource_name AS target_resource,
  JSON_VALUE(proto_payload.audit_log.request, '$.skill.scanningConfig.policy') AS requested_scan_policy,
  proto_payload.audit_log.status.code AS status_code,
  proto_payload.audit_log.status.message AS status_message
FROM
  `PROJECT_ID.REGION.agent-governance-audit._AllLogs`
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  AND log_id = 'cloudaudit.googleapis.com/activity'
  AND proto_payload.audit_log.service_name = 'agentregistry.googleapis.com'
ORDER BY
  timestamp DESC
LIMIT 200;
```

### 5.3 IAP UAP Dry-Run Readiness Summary (Would-Deny Breakdown)

```sql
SELECT
  proto_payload.audit_log.authentication_info.principal_subject AS agent_principal,
  auth.resource AS destination_resource,
  JSON_VALUE(proto_payload.audit_log.metadata, '$.mcp_attributes.base_protocol_method') AS is_base_protocol,
  COUNTIF(auth.granted = TRUE) AS would_allow_calls,
  COUNTIF(auth.granted = FALSE) AS would_deny_calls
FROM
  `PROJECT_ID.REGION.agent-governance-audit._AllLogs`,
  UNNEST(proto_payload.audit_log.authorization_info) AS auth
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
  AND proto_payload.audit_log.service_name = 'iap.googleapis.com'
  AND auth.permission = 'iap.resources.egressViaIAP'
  AND JSON_VALUE(proto_payload.audit_log.metadata, '$.iamEnforcementMode') = 'DRY_RUN'
GROUP BY
  1, 2, 3
ORDER BY
  would_deny_calls DESC;
```
