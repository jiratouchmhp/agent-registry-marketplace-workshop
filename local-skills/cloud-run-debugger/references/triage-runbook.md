# Cloud Run Revision & Runtime Triage Runbook

Execute these phases in order during a `cloud-run-debugger` investigation. Stick strictly to read-only inspection until the operator approves remediation.

## Phase 1 — Service Health & Revision Conditions

- [ ] Confirm target `project`, `region`, `service_name`, and whether traffic is pinned to a previous healthy revision or routed to `latest`.
- [ ] Compare `status.latestCreatedRevisionName` against `status.latestReadyRevisionName`.
- [ ] Inspect `status.conditions[]` on the failing revision (`Ready`, `Active`, `ContainerHealthy`, `ResourcesAvailable`).
- [ ] Diff the failing revision spec against the last healthy revision (container image digest, `env`, `resources.limits`, `serviceAccountName`, `vpcAccess`, `startupProbe`).

## Phase 2 — Container Startup, Port Binding & Probes

- [ ] Verify the container listens on `0.0.0.0:${PORT}` (not `127.0.0.1` or a mismatched hardcoded port).
- [ ] Check whether the container exits before the startup probe succeeds (missing binary, wrong CPU architecture `arm64` vs `amd64`, missing shared libraries, or invalid entrypoint).
- [ ] Inspect `startupProbe` parameters (`initialDelaySeconds`, `periodSeconds`, `timeoutSeconds`, `failureThreshold`, `tcpSocket` vs `httpGet` path).
- [ ] Confirm `startup_cpu_boost` is enabled for heavy runtimes (JVM, Python ML imports, Node.js bundlers) that spike CPU during cold starts.
- [ ] Verify `livenessProbe` does not depend on slow external dependencies (databases, third-party APIs) that could trigger cascading container restarts.

## Phase 3 — IAM Invoker, Ingress & Networking

- [ ] For HTTP `401`/`403` errors, inspect the service IAM policy (`gcloud run services get-iam-policy <SERVICE> --region=<REGION>`) and verify caller identity holds `roles/run.invoker`.
- [ ] Verify whether the caller presents a valid Google OIDC identity token whose `aud` claim matches the Cloud Run service URL (or custom audience).
- [ ] Check ingress controls (`all`, `internal`, `internal-and-cloud-load-balancing`) and Organization Policy constraints (`run.allowedIngress`).
- [ ] For egress timeouts to private databases or internal APIs, verify Direct VPC Egress / Serverless VPC Access connector status, subnet IP exhaustion, Cloud NAT, and VPC firewall egress rules.
- [ ] Check runtime service account permissions for Secret Manager (`roles/secretmanager.secretAccessor`), Artifact Registry (`roles/artifactregistry.reader` on deployment agent), and downstream GCP APIs.

## Phase 4 — Cloud Logging, Memory & Concurrency Analysis

- [ ] Query Cloud Logging for `resource.type="cloud_run_revision"` filtered by `revision_name` and `severity>=ERROR`.
- [ ] Look for memory limit exceeded notices (`Memory limit of <X> MiB exceeded`, exit status `137`, or sudden request resets mid-flight).
- [ ] Check request concurrency (`containerConcurrency`) vs. instance count (`maxScale`) for `429`/`503` queueing or instance cap exhaustion.
- [ ] Check request timeout (`timeoutSeconds`) against upstream load balancer or client timeout budgets for `504` responses.

## Phase 5 — Safe Mitigation & Permanent Fix Proposal

- [ ] If production traffic is degraded and a prior healthy revision exists, propose routing 100% traffic back to `<LAST_GOOD_REVISION>` via `gcloud run services update-traffic` (operator-approved).
- [ ] Document the exact Terraform or deployment manifest change required for a permanent fix (probe tuning, memory bump, IAM `roles/run.invoker` member binding, or image patch).
- [ ] Define post-fix verification checks (revision `Ready=True`, zero `5xx` over 10 minutes, p95 latency back to baseline).
