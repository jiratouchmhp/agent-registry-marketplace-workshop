---
name: cloud-run-debugger
description: Diagnose Google Cloud Run revision startup failures, probe timeouts, IAM invoker denials, and Cloud Logging error spikes using a structured read-only triage runbook.
license: Apache-2.0
compatibility: Designed for Google Cloud Platform operators with read-only Cloud Run (`roles/run.viewer`) and Cloud Logging (`roles/logging.viewer`) visibility.
metadata:
  category: "cloud-operations"
  domain: "cloud-run"
---

# Cloud Run Debugger

Use this skill whenever a Google Cloud Run service fails to roll out a new revision, returns elevated HTTP `401`/`403`/`500`/`503`/`504` responses, restarts unexpectedly, or suffers startup and liveness probe timeouts.

## Safety & Governance Rules

1. **Read-only diagnosis first**: Inspect service status, revision conditions, IAM policies, and Cloud Logging entries before recommending any configuration edit or traffic rollback.
2. **Never execute traffic shifts or IAM mutations without explicit operator approval**: Present exact candidate `gcloud run` commands as proposals with clear blast radius and rollback steps.
3. **Never print secret values**: Inspect Secret Manager environment variable *references* (`secretKeyRef` name and version) only—never fetch or echo raw secret payloads.
4. **Cite concrete telemetry**: Always include project ID, region, service name, revision ID, exact condition `reason`/`message`, HTTP status distribution, and UTC timestamps.

## Triage Workflow

1. **Capture service & revision state**
   - Inspect service traffic splits and latest ready vs. latest created revisions:
     - `gcloud run services describe <SERVICE> --region=<REGION> --project=<PROJECT> --format=yaml`
     - `gcloud run revisions list --service=<SERVICE> --region=<REGION> --project=<PROJECT>`
     - `gcloud run revisions describe <REVISION> --region=<REGION> --project=<PROJECT> --format=yaml`
   - Check the three core conditions (`Ready`, `ConfigurationsReady`, `RoutesReady`) and note any `ContainerFailedToStart`, `HealthCheckContainerError`, or `ImageNotFound` messages.
2. **Walk the structured triage runbook**
   - Follow Phases 1–5 in `references/triage-runbook.md` systematically to isolate:
     - Container port binding (`$PORT`, default `8080`, listening on `0.0.0.0`, not `127.0.0.1`).
     - Startup probe and liveness probe paths, timeouts, `initialDelaySeconds`, and startup CPU boost.
     - IAM invoker policy (`roles/run.invoker`), service ingress restrictions (`internal`, `internal-and-cloud-load-balancing`), IAP headers, and Serverless VPC Access / Direct VPC egress.
     - Memory limits (`OOMKilled` / exit code `137`), request concurrency saturation, and downstream timeouts.
3. **Query Cloud Logging with scoped filters**
   - Pull startup and error logs bounded to the failing revision and time window:
     ```text
     resource.type="cloud_run_revision"
     resource.labels.service_name="<SERVICE>"
     resource.labels.revision_name="<REVISION>"
     severity>=WARNING
     timestamp>="<UTC_START>"
     ```
   - Correlate `httpRequest.status>=500` entries with container stdout/stderr using `trace` IDs.
4. **Deliver a structured incident triage brief**
   - **Symptom & Scope**: Service, region, failing revision, percentage of traffic affected, and UTC start time.
   - **Verified Evidence**: Exact revision condition message, probe configuration, IAM/ingress setting, and representative log lines.
   - **Root Cause Hypothesis**: Primary cause ranked by confidence with ruling-out evidence for secondary causes.
   - **Recommended Remediation**: Safe immediate mitigation (e.g., traffic pin to last known-good revision) followed by permanent configuration or code fix.
