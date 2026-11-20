---
name: incident-postmortem-writer
description: Draft structured, blameless SRE incident postmortems with UTC timelines, five-whys root-cause analysis, customer impact quantification, and tracked preventive action items.
license: Apache-2.0
compatibility: Works with incident logs, alert histories, chat transcripts, and ticket notes to generate publication-ready Markdown postmortems.
metadata:
  category: "site-reliability"
  domain: "incident-management"
---

# Incident Postmortem Writer

Use this skill after any severity-1/severity-2 outage, SLO error-budget burn event, security near-miss, or significant degradation to produce a clear, blameless SRE postmortem.

## Blameless Authoring Principles

1. **Focus on systems, never individuals**: Assume every responder acted rationally with the information and tooling available at the time. Replace personal attribution ("Alice broke prod") with system behavior ("The deployment pipeline lacked a canary health gate for migration rollback").
2. **Separate facts from hypotheses**: Mark verified timestamps and metrics clearly; label unconfirmed explanations as open questions requiring follow-up telemetry.
3. **Standardize all timestamps in UTC (`YYYY-MM-DD HH:MM UTC`)**: Convert local responder times so cross-region teams share one canonical timeline.
4. **Make every action item actionable and bounded**: Every action item must specify category (`Prevent`, `Detect`, or `Mitigate`), priority (`P0`/`P1`/`P2`), owning team/role, tracking ticket placeholder, and target completion date.

## Authoring Workflow

1. **Extract core incident facts**
   - Gather incident ID, severity, affected services/regions, start time, detection time, mitigation time, full resolution time, and SLO/error-budget burn.
   - Calculate **TTD** (Time to Detect), **TTM** (Time to Mitigate), and **TTR** (Time to Resolve).
2. **Construct the root-cause analysis**
   - Distinguish the **proximate trigger** (what initiated the failure) from **contributing systemic factors** (missing guardrails, noisy alerts, brittle rollback, capacity headroom).
   - Walk a concise **Five Whys** chain stopping only at actionable systemic causes.
3. **Draft the postmortem using the standard template**

```markdown
# Postmortem: <Concise Incident Title>

- **Incident ID**: <INC-XXXX>
- **Date**: <YYYY-MM-DD>
- **Severity**: <SEV-1 | SEV-2 | SEV-3>
- **Status**: <Draft | In Review | Final>
- **Affected Services / Regions**: <Services and regions>
- **Duration**: <Total duration> (TTD: <Xm>, TTM: <Xm>, TTR: <Xm>)

## 1. Executive Summary
<2-4 sentences summarizing what failed, customer impact, how it was mitigated, and the primary systemic fix.>

## 2. Customer & SLO Impact
- **User-Visible Symptoms**: <Error rates, latency spikes, failed workflows>
- **Peak Impact**: <Percentage of requests/users affected>
- **Error Budget Burn**: <% of 30-day SLO budget consumed>

## 3. Trigger & Root Cause (Five Whys)
- **Trigger**: <Immediate event that exposed the vulnerability>
- **Root Cause**: <Underlying technical and process conditions>
- **Five Whys**:
  1. Why did the outage occur? ...
  2. Why did that condition exist? ...
  3. Why was it not caught before production? ...
  4. Why was detection/mitigation delayed? ...
  5. What systemic safeguard prevents recurrence? ...

## 4. Detection & Resolution Timeline (UTC)
| Time (UTC) | Event |
| :--- | :--- |
| `YYYY-MM-DD HH:MM` | Triggering change or anomaly began |
| `YYYY-MM-DD HH:MM` | First alert fired / customer report received |
| `YYYY-MM-DD HH:MM` | Incident commander assigned and mitigation started |
| `YYYY-MM-DD HH:MM` | Mitigation verified; service metrics returned to baseline |
| `YYYY-MM-DD HH:MM` | Incident resolved and monitoring confirmed stable |

## 5. Lessons Learned
### What Went Well
- <Effective alerts, runbooks, rapid rollback, clear comms>

### What Went Poorly
- <Monitoring blind spots, slow deploys, missing automation>

### Where We Got Lucky
- <Low traffic window, partial region blast radius, manual notice>

## 6. Action Items
| Priority | Type | Action Item | Owner | Target Date | Ticket |
| :--- | :--- | :--- | :--- | :--- | :--- |
| P0 | Prevent | <Concrete engineering safeguard> | <Team/Role> | <YYYY-MM-DD> | <TBD> |
| P1 | Detect | <Alert/SLO or synthetic probe addition> | <Team/Role> | <YYYY-MM-DD> | <TBD> |
| P1 | Mitigate | <Runbook or automated rollback improvement> | <Team/Role> | <YYYY-MM-DD> | <TBD> |
```

4. **Quality self-check before finalizing**
   - Confirm zero blame language or unredacted secrets/PII appear anywhere in the draft.
   - Verify at least one `P0`/`P1` preventive action item directly addresses the root cause and one `Detect`/`Mitigate` action item reduces future TTD/TTM.
