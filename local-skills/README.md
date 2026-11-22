# Pre-Prepared Standalone Local Skills (Option 1)

This directory contains ready-to-ingest standalone [Agent Skills (`agentskills.io`)](https://agentskills.io) packages for **Option 1 — Local Skills Path**.

No `plugin.json` manifest, external Git repository, or GitLab/GitHub access token is required to register these skills in Google Cloud Agent Registry (`v1`).

## Directory Contract

Each standalone skill lives in its own kebab-case folder containing a strictly uppercase `SKILL.md` manifest plus optional supporting folders (`references/`, `scripts/`, `assets/`):

```text
local-skills/
├── cloud-run-debugger/
│   ├── SKILL.md                       # frontmatter `name: cloud-run-debugger`
│   └── references/
│       └── triage-runbook.md
├── terraform-iam-auditor/
│   ├── SKILL.md                       # frontmatter `name: terraform-iam-auditor`
│   └── references/
│       └── least-privilege-checklist.md
└── incident-postmortem-writer/
    └── SKILL.md                       # frontmatter `name: incident-postmortem-writer`
```

### Required Invariants (Enforced by `tools.plugin_sync` & Agent Registry `v1`)

- **Strict uppercase `SKILL.md`**: Exactly one `SKILL.md` per skill directory; lowercase `skill.md` and nested duplicate `SKILL.md` files are rejected.
- **Matching `name`**: YAML frontmatter `name` (`^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`, no `--`) must exactly equal the skill directory name.
- **Concise `description`**: Required 1–1024 character string explaining when and how an agent should apply the skill.
- **Scalar `metadata`**: Optional `metadata` block must contain only `map<string, string>` scalar values (used by `tools.plugin_sync` to attach `plugin_name`, `plugin_display_name`, `plugin_version`, `source_repo`, `git_commit`, `kms_signature`, and `kms_key_version`).
- **Archive safety**: Compressed ZIP <= 1 MB (<= 500 KB recommended for inline upload), uncompressed total <= 10 MB, individual file <= 1 MB, max nesting depth 8, no symlinks, and no path traversal.

## Bundled Skills

| Skill | Category | Supporting Files | Purpose |
| :--- | :--- | :--- | :--- |
| `cloud-run-debugger` | `cloud-operations` | `references/triage-runbook.md` | Structured read-only triage for Cloud Run revision startup failures, probe timeouts, IAM invoker denials, and Cloud Logging error spikes. |
| `terraform-iam-auditor` | `security-governance` | `references/least-privilege-checklist.md` | Static least-privilege audit of Terraform GCP IAM bindings, flagging primitive roles, `allUsers`, and over-privileged Agent Registry grants to autonomous agents. |
| `incident-postmortem-writer` | `site-reliability` | *(none)* | Blameless SRE postmortem authoring workflow with UTC timelines, five-whys root cause analysis, SLO impact, and prioritized action items. |

## One-Command Ingestion Examples

### 1. Ingest all local skills as one cohesive IDE plugin bundle (`local-ops-toolkit`)

```bash
python -m tools.plugin_sync ingest \
  --source-type local \
  --path local-skills \
  --plugin-name local-ops-toolkit \
  --registry-url http://127.0.0.1:8090/v1 \
  --project acme-agent-governance \
  --location asia-southeast3
```

*(Against live `https://agentregistry.googleapis.com/v1`, pass `--skills-location global --wait-for-scan --only-skills` so standalone skills are hosted in `global` — or `us`/`eu` for CMEK — and async `ENABLED_BLOCKING` scans complete before promoting `defaultRevision`.)*

### 2. Ingest with ECDSA P-256 signing and dry-run artifact inspection

```bash
python -m tools.plugin_sync ingest \
  --source-type local \
  --path local-skills \
  --plugin-name local-ops-toolkit \
  --sign-pem /path/to/signing-private.pem \
  --output-dir dist/local-skills \
  --project acme-agent-governance \
  --location asia-southeast3 \
  --dry-run
```

### 3. Ingest each standalone skill as its own separate IDE plugin bundle

```bash
python -m tools.plugin_sync ingest \
  --source-type local \
  --path local-skills \
  --per-skill-plugin \
  --registry-url http://127.0.0.1:8090/v1 \
  --project acme-agent-governance \
  --location asia-southeast3
```

### 4. Ingest a single standalone skill directory

```bash
python -m tools.plugin_sync ingest \
  --source-type local \
  --path local-skills/cloud-run-debugger \
  --registry-url http://127.0.0.1:8090/v1 \
  --project acme-agent-governance \
  --location asia-southeast3
```

Once registered, Agent Registry stores each skill with the automatic `private-` resource ID prefix (for example, `private-cloud-run-debugger`), and the Polyglot Marketplace Middleware automatically serves `local-ops-toolkit` across `/v1/catalog`, `/v1/marketplace/{claude,copilot,codex,antigravity}.json`, and `/mcp`.
