"""Automated consistency checks for workshop documentation, Makefile, links, and CLI modules."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
LABS_DIR = DOCS_DIR / "labs"
MAKEFILE_PATH = REPO_ROOT / "Makefile"
README_PATH = REPO_ROOT / "README.md"
LOCAL_SKILLS_DIR = REPO_ROOT / "local-skills"
SPEC_PATH = REPO_ROOT / "WORKSHOP_SPEC.json"

EXPECTED_LOCAL_SKILL_NAMES: tuple[str, ...] = (
    "cloud-run-debugger",
    "terraform-iam-auditor",
    "incident-postmortem-writer",
)

EXPECTED_LAB_FILES: tuple[str, ...] = (
    "00-environment-setup.md",
    "01-author-agent-plugin.md",
    "02-register-agent-registry.md",
    "03-build-deploy-middleware.md",
    "04-ide-developer-experience.md",
    "05-platform-agents-gitlab-sot.md",
    "06-governance-scanning-lifecycle.md",
    "07-kms-encryption-and-signing.md",
    "08-access-control-and-audit.md",
)

REQUIRED_MAKE_TARGETS: frozenset[str] = frozenset(
    {
        "preflight",
        "starter-check",
        "smoke-local",
        "run-local-stack",
        "test",
        "test-e2e",
        "validate-hcl",
        "run-mock",
        "run-samples",
        "run-middleware",
        "demo-sync",
        "demo-sync-local",
        "demo-sync-repo",
        "demo-hub-sync",
        "gcp-build",
        "gcp-deploy",
        "gcp-validate",
        "gcp-destroy",
        "tf-core-plan",
        "tf-sec-plan",
    }
)


STARTER_README_PATH = REPO_ROOT / "starter" / "README.md"


def _all_doc_markdown_files() -> list[Path]:
    files = [README_PATH, LOCAL_SKILLS_DIR / "README.md", STARTER_README_PATH]
    files.extend(sorted(DOCS_DIR.rglob("*.md")))
    return files


def _strip_fenced_code_blocks(markdown_text: str) -> str:
    """Remove fenced code blocks (``` ... ```) so link parsing inspects real Markdown prose."""
    return re.sub(r"```[\s\S]*?```", "", markdown_text)


def _extract_make_targets_defined(makefile_text: str) -> set[str]:
    targets: set[str] = set()
    for line in makefile_text.splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", line)
        if match:
            name = match.group(1)
            if not name.startswith("."):
                targets.add(name)
    return targets


def _extract_referenced_make_targets(markdown_text: str) -> set[str]:
    """Extract every `make <target>` referenced in inline code spans or shell code blocks."""
    referenced: set[str] = set()

    # 1. Inline backtick code spans containing `make <target>`
    for span in re.findall(r"`([^`\n]+)`", markdown_text):
        for match in re.finditer(r"(?:^|[;&|]\s*|\s)make\s+([a-z][a-z0-9_-]*)\b", span):
            referenced.add(match.group(1))

    # 2. Shell / untagged fenced code blocks
    for block in re.finditer(r"```([a-zA-Z0-9_-]*)\n([\s\S]*?)```", markdown_text):
        lang = block.group(1).strip().lower()
        body = block.group(2)
        if lang not in ("", "bash", "sh", "shell", "zsh", "console"):
            continue
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            for match in re.finditer(r"(?:^|[;&|]\s*|\s)make\s+([a-z][a-z0-9_-]*)\b", line):
                referenced.add(match.group(1))

    return referenced


def _extract_python_dash_m_modules(markdown_text: str) -> set[str]:
    """Extract every `python -m <module>` (incl. `.venv/bin/python -m`, `python3 -m`)."""
    modules: set[str] = set()
    pattern = re.compile(
        r"(?:\.venv/bin/python|python3?|[$]\(PYTHON\))\s+-m\s+([A-Za-z_][A-Za-z0-9_.]*)"
    )
    for match in pattern.finditer(markdown_text):
        modules.add(match.group(1))
    return modules


def test_required_curriculum_files_exist_and_non_empty() -> None:
    """All 9 lab guides, README.md, local-skills docs/SKILL.md files, brief, arch, and Makefile exist."""
    required_files = [
        README_PATH,
        MAKEFILE_PATH,
        LOCAL_SKILLS_DIR / "README.md",
        *(LOCAL_SKILLS_DIR / name / "SKILL.md" for name in EXPECTED_LOCAL_SKILL_NAMES),
        DOCS_DIR / "attendee-quickstart.md",
        DOCS_DIR / "customer-usecase-3-brief.md",
        DOCS_DIR / "architecture.md",
        *(LABS_DIR / name for name in EXPECTED_LAB_FILES),
    ]
    for path in required_files:
        assert path.is_file(), f"Missing required curriculum file: {path}"
        content = path.read_text(encoding="utf-8").strip()
        assert len(content) > 200, f"Curriculum file appears incomplete/empty: {path}"


def test_makefile_defines_required_targets_and_venv_defaults() -> None:
    """Makefile defines all 12 required targets (including dual-source sync) and uses .venv defaults."""
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    assert "PYTHON ?= .venv/bin/python" in makefile_text
    assert "PYTEST ?= .venv/bin/pytest" in makefile_text

    defined = _extract_make_targets_defined(makefile_text)
    missing = REQUIRED_MAKE_TARGETS - defined
    assert not missing, f"Makefile missing required targets: {sorted(missing)}"


def test_all_relative_markdown_links_resolve() -> None:
    """Every relative Markdown link `[...](...)` in README.md and docs/**/*.md resolves on disk."""
    link_re = re.compile(r"!?\[[^\]]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    broken: list[str] = []

    for md_file in _all_doc_markdown_files():
        text = _strip_fenced_code_blocks(md_file.read_text(encoding="utf-8"))
        for match in link_re.finditer(text):
            raw_target = match.group(1).strip()
            if (
                raw_target.startswith(("http://", "https://", "mailto:", "#"))
                or raw_target.startswith("<")
            ):
                continue

            # Strip fragment (#...) and query (?...)
            clean_rel = raw_target.split("#", 1)[0].split("?", 1)[0]
            if not clean_rel:
                continue

            resolved = (md_file.parent / clean_rel).resolve()
            try:
                resolved.relative_to(REPO_ROOT.resolve())
            except ValueError:
                broken.append(
                    f"{md_file.relative_to(REPO_ROOT)} -> {raw_target!r} (escapes repo root)"
                )
                continue

            if not resolved.exists():
                broken.append(
                    f"{md_file.relative_to(REPO_ROOT)} -> {raw_target!r} "
                    f"(resolved to missing path {resolved})"
                )

    assert not broken, "Broken relative Markdown links detected:\n" + "\n".join(broken)


def test_all_referenced_make_targets_exist_in_makefile() -> None:
    """Every `make <target>` mentioned across README.md and docs/**/*.md exists in Makefile."""
    defined = _extract_make_targets_defined(MAKEFILE_PATH.read_text(encoding="utf-8"))
    missing_refs: list[str] = []
    total_refs = 0

    for md_file in _all_doc_markdown_files():
        refs = _extract_referenced_make_targets(md_file.read_text(encoding="utf-8"))
        total_refs += len(refs)
        for target in sorted(refs):
            if target not in defined:
                missing_refs.append(
                    f"{md_file.relative_to(REPO_ROOT)} references undefined `make {target}`"
                )

    assert total_refs > 0, "Expected docs to reference at least one `make <target>` command"
    assert not missing_refs, "Undefined Makefile targets referenced in docs:\n" + "\n".join(
        missing_refs
    )


def test_all_python_dash_m_modules_are_importable() -> None:
    """Every `python -m <module>` referenced across README.md and docs/**/*.md is importable."""
    unresolved: list[str] = []
    discovered_modules: set[str] = set()

    for md_file in _all_doc_markdown_files():
        mods = _extract_python_dash_m_modules(md_file.read_text(encoding="utf-8"))
        for mod_name in sorted(mods):
            discovered_modules.add(mod_name)
            try:
                spec = importlib.util.find_spec(mod_name)
            except (ImportError, ModuleNotFoundError, ValueError):
                spec = None
            if spec is None:
                unresolved.append(
                    f"{md_file.relative_to(REPO_ROOT)} references non-importable `python -m {mod_name}`"
                )

    assert "tools.plugin_sync" in discovered_modules
    assert "tools.hub_sync" in discovered_modules
    assert "platform_agent.adk_orchestrator" in discovered_modules
    assert not unresolved, "Unresolvable `python -m` modules in docs:\n" + "\n".join(unresolved)


def test_readme_links_workstreams_and_all_labs() -> None:
    """README.md links quickstart, executive brief, architecture, starter, local-skills, and all 9 lab guides."""
    readme = README_PATH.read_text(encoding="utf-8")
    assert "```mermaid" in readme
    assert "Workstream A" in readme
    assert "Workstream B" in readme
    assert "docs/attendee-quickstart.md" in readme
    assert "docs/customer-usecase-3-brief.md" in readme
    assert "docs/architecture.md" in readme
    assert "starter/README.md" in readme
    assert "local-skills/README.md" in readme
    assert "demo-sync-local" in readme
    assert "demo-sync-repo" in readme
    assert "skills_source_mode" in readme
    for lab_file in EXPECTED_LAB_FILES:
        assert f"docs/labs/{lab_file}" in readme, f"README.md missing link to docs/labs/{lab_file}"


def test_dual_skill_sources_documented_consistently() -> None:
    """Local-skills paths, dual-source Make targets, CLI flags, Terraform mode, and GitLab CI vars stay aligned."""
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    assert tuple(spec["skill_sources"]["local"]["skills"]) == EXPECTED_LOCAL_SKILL_NAMES

    local_readme = (LOCAL_SKILLS_DIR / "README.md").read_text(encoding="utf-8")
    for skill_name in EXPECTED_LOCAL_SKILL_NAMES:
        assert (LOCAL_SKILLS_DIR / skill_name / "SKILL.md").is_file()
        assert skill_name in local_readme

    lab01 = (LABS_DIR / "01-author-agent-plugin.md").read_text(encoding="utf-8")
    for skill_name in EXPECTED_LOCAL_SKILL_NAMES:
        assert skill_name in lab01

    lab02 = (LABS_DIR / "02-register-agent-registry.md").read_text(encoding="utf-8")
    for token in (
        "demo-sync-local",
        "demo-sync-repo",
        "--source-type local",
        "--source-type git",
        "--git-token-env",
        "skills_source_mode",
    ):
        assert token in lab02, f"Lab 02 missing dual-source token: {token}"

    lab05 = (LABS_DIR / "05-platform-agents-gitlab-sot.md").read_text(encoding="utf-8")
    for token in (
        "--source-type git",
        "WORKSHOP_TOOLS_PACKAGE",
        "PLUGINS_DIR",
        "GCP_WORKLOAD_IDENTITY_PROVIDER",
        "GCP_SERVICE_ACCOUNT_EMAIL",
    ):
        assert token in lab05, f"Lab 05 missing GitLab migration / dual-source token: {token}"


def test_executive_brief_and_architecture_cover_required_topics() -> None:
    """Executive brief answers all 4 Use Case 3 questions and architecture has Mermaid diagrams."""
    brief = (DOCS_DIR / "customer-usecase-3-brief.md").read_text(encoding="utf-8")
    for required_term in (
        "agent-plugins.org",
        "agentskills.io",
        "Claude Code",
        "OpenAI Codex",
        "GitHub Copilot",
        "Google Antigravity",
        "github.com",
        "AgentRegistry",
        "GCPSkillRegistry",
        "ENABLED_BLOCKING",
        "TARGET_STATE_DRAFT",
        "STATE_DECOMMISSIONED",
        "SkillEncryptionSpec",
        "EC_SIGN_P256_SHA256",
        "egressViaIAP",
        "Dual-Role Split",
        ".gitlab-ci.yml",
    ):
        assert required_term in brief, f"Executive brief missing required topic: {required_term}"

    arch = (DOCS_DIR / "architecture.md").read_text(encoding="utf-8")
    assert arch.count("```mermaid") >= 4, "docs/architecture.md should include >=4 Mermaid diagrams"
    assert "?alt=media" in arch
    assert "x-goog-" in arch
    assert ".sync-manifest.json" in arch

    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    for port in spec["ports"].values():
        assert str(port) in arch, f"docs/architecture.md missing canonical port {port}"


@pytest.mark.parametrize("lab_filename", EXPECTED_LAB_FILES)
def test_each_lab_guide_has_required_sections(lab_filename: str) -> None:
    """Every lab guide includes Objectives, Duration, Prerequisites, Verification, Troubleshooting, and Dual Modes."""
    lab_path = LABS_DIR / lab_filename
    text = lab_path.read_text(encoding="utf-8")
    assert "**Duration:**" in text
    assert "**Prerequisites:**" in text
    assert "## Learning Objectives" in text
    assert "## Verification Checkpoint" in text
    assert "## Troubleshooting" in text
    assert "Local" in text and "Live" in text

    if lab_filename.startswith(("06-", "07-", "08-")):
        assert "Workstream:** B" in text
        callout = "Running Workstream B Standalone"
        objectives = "## Learning Objectives"
        assert callout in text, (
            f"{lab_filename} must include an explicit '{callout}' callout"
        )
        assert text.index(callout) < text.index(objectives), (
            f"{lab_filename} must place '{callout}' before '{objectives}'"
        )


def test_no_invalid_gcloud_builds_file_flag_or_double_url_prefix() -> None:
    """Regression guard against nonexistent `gcloud builds submit --file` and malformed redirect URLs."""
    invalid_gcloud_re = re.compile(r"gcloud\s+builds\s+submit[^\n]*--file(?:=|\s)")
    double_url_re = re.compile(r"https?://[^\s\"']+\$\{SIGNED_LOC\}")

    for md_file in _all_doc_markdown_files():
        text = md_file.read_text(encoding="utf-8")
        # Join bash line continuations (`\\\n`) and ignore shell comment lines (`# ...`)
        non_comment_lines = [
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        ]
        joined = re.sub(r"\\\n\s*", " ", "\n".join(non_comment_lines))
        assert not invalid_gcloud_re.search(joined), (
            f"{md_file.relative_to(REPO_ROOT)} contains invalid `gcloud builds submit ... --file` flag"
        )
        assert not double_url_re.search(joined), (
            f"{md_file.relative_to(REPO_ROOT)} prepends a scheme/host onto ${{SIGNED_LOC}} "
            "(Location header is already an absolute URL)"
        )


def test_docs_use_consistent_track_1_2_3_naming() -> None:
    """All workshop docs consistently use Track 1 / Track 2 / Track 3 (never drifted Track A / Track B)."""
    drift_re = re.compile(r"\bTrack [AB]\b")
    for md_file in _all_doc_markdown_files():
        text = md_file.read_text(encoding="utf-8")
        assert not drift_re.search(text), (
            f"{md_file.relative_to(REPO_ROOT)} uses drifted 'Track A'/'Track B' naming instead of Track 1/2/3"
        )


def test_live_apac_regional_split_and_findings_documented_consistently() -> None:
    """Core docs and key labs consistently document the Bangkok-first + Singapore fallback + global Skills architecture and live gotchas."""
    readme = README_PATH.read_text(encoding="utf-8")
    quickstart = (DOCS_DIR / "attendee-quickstart.md").read_text(encoding="utf-8")
    arch = (DOCS_DIR / "architecture.md").read_text(encoding="utf-8")
    lab00 = (LABS_DIR / "00-environment-setup.md").read_text(encoding="utf-8")
    lab02 = (LABS_DIR / "02-register-agent-registry.md").read_text(encoding="utf-8")
    lab03 = (LABS_DIR / "03-build-deploy-middleware.md").read_text(encoding="utf-8")
    lab05 = (LABS_DIR / "05-platform-agents-gitlab-sot.md").read_text(encoding="utf-8")
    lab06 = (LABS_DIR / "06-governance-scanning-lifecycle.md").read_text(encoding="utf-8")
    lab07 = (LABS_DIR / "07-kms-encryption-and-signing.md").read_text(encoding="utf-8")
    lab08 = (LABS_DIR / "08-access-control-and-audit.md").read_text(encoding="utf-8")

    # Bangkok primary (asia-southeast3) + Singapore fallback for Agent Identity (asia-southeast1) + global Skills
    for name, doc in (
        ("README.md", readme),
        ("attendee-quickstart.md", quickstart),
        ("architecture.md", arch),
        ("00-environment-setup.md", lab00),
        ("02-register-agent-registry.md", lab02),
        ("03-build-deploy-middleware.md", lab03),
        ("05-platform-agents-gitlab-sot.md", lab05),
        ("08-access-control-and-audit.md", lab08),
    ):
        for token in ("asia-southeast3", "asia-southeast1", "global"):
            assert token in doc, f"{name} missing Bangkok-first regional architecture token: {token}"

    for name, doc in (
        ("README.md", readme),
        ("attendee-quickstart.md", quickstart),
        ("architecture.md", arch),
        ("02-register-agent-registry.md", lab02),
        ("03-build-deploy-middleware.md", lab03),
    ):
        assert "private-" in doc, f"{name} missing private- skill filter documentation"

    # Explicit Singapore service-specific fallback documentation for Agent Identity AuthProvider
    for name, doc in (
        ("README.md", readme),
        ("attendee-quickstart.md", quickstart),
        ("architecture.md", arch),
        ("00-environment-setup.md", lab00),
        ("07-kms-encryption-and-signing.md", lab07),
        ("08-access-control-and-audit.md", lab08),
    ):
        assert 'auth_provider_location = "asia-southeast1"' in doc, (
            f"{name} must document auth_provider_location = \"asia-southeast1\" fallback for Agent Identity"
        )

    # Live-discovered A2A v1.0 & ENABLED_BLOCKING scan-race gotchas in Lab 02
    for token in ("stateTransitionHistory", "/a2a/http", "--wait-for-scan", "--skills-location"):
        assert token in lab02, f"Lab 02 missing live finding documentation: {token}"

    # Live Cloud Run GFE /healthz -> /health, same-origin /download/v1/ redirect, and concurrency in Lab 03
    for token in ("/health", "/download/v1/", "CREDENTIALS_MISSING", "Semaphore(8)"):
        assert token in lab03, f"Lab 03 missing live finding documentation: {token}"

    # Live IAM v3 --condition=None teardown gotcha in Labs 05 & 08
    assert "--condition=None" in lab05, "Lab 05 missing IAM v3 --condition=None teardown documentation"
    assert "--condition=None" in lab08, "Lab 08 missing IAM v3 --condition=None teardown documentation"

    # Workstream B standalone Skills in global + APAC AuthProvider/Gateway notes
    assert 'AGENT_REGISTRY_SKILLS_LOCATION="global"' in lab06
    assert "wait_for_revision_scan_completion" in lab06
    assert 'AGENT_REGISTRY_SKILLS_LOCATION="global"' in lab08
    assert 'auth_provider_location = "asia-southeast1"' in lab08
    assert "enable_agent_gateway = false" in lab08


def test_single_region_bangkok_architecture_regression_guards() -> None:
    """Regression guard: terraform/core has zero duplicate *_secondary resources and docs contain zero 2-region failover flags."""
    tf_core_dir = REPO_ROOT / "terraform" / "core"
    assert tf_core_dir.is_dir(), f"Missing terraform/core directory: {tf_core_dir}"

    forbidden_tf_patterns = (
        re.compile(r"\b[a-z0-9_]+_secondary\b"),
        re.compile(r"\benable_secondary_region\b"),
        re.compile(r"\bsecondary_location\b"),
        re.compile(r"\bsecondary_middleware_url\b"),
        re.compile(r"\bsecondary_middleware_mcp_url\b"),
        re.compile(r"\bsecondary_artifact_registry_repository_url\b"),
    )
    tf_violations: list[str] = []
    for tf_file in sorted(tf_core_dir.glob("*.tf")) + sorted(tf_core_dir.glob("*.tfvars*")):
        text = tf_file.read_text(encoding="utf-8")
        for pat in forbidden_tf_patterns:
            match = pat.search(text)
            if match:
                tf_violations.append(
                    f"{tf_file.relative_to(REPO_ROOT)} contains forbidden secondary resource/var {match.group(0)!r}"
                )
    assert not tf_violations, (
        "Duplicate secondary-region definitions detected in terraform/core/:\n"
        + "\n".join(tf_violations)
    )

    forbidden_doc_tokens = (
        "--enable-secondary",
        "ENABLE_SECONDARY",
        "enable_secondary_region",
        "secondary_location",
        "secondary_middleware_url",
        "--fallback-middleware-url",
        "HUB_FALLBACK_MIDDLEWARE_URL",
        "GCP_SECONDARY_LOCATION",
    )
    doc_violations: list[str] = []
    for md_file in _all_doc_markdown_files():
        text = md_file.read_text(encoding="utf-8")
        for token in forbidden_doc_tokens:
            if token in text:
                doc_violations.append(
                    f"{md_file.relative_to(REPO_ROOT)} references removed 2-region token {token!r}"
                )
    assert not doc_violations, (
        "Removed 2-region / failover flags referenced in documentation:\n"
        + "\n".join(doc_violations)
    )


def test_all_labs_have_files_table_and_starter_guidance() -> None:
    """Every lab leads with Files You'll Work With, clear starter/ guidance, and avoids AI clichés."""
    files_heading = "## Files You'll Work With"
    for lab_filename in EXPECTED_LAB_FILES:
        text = (LABS_DIR / lab_filename).read_text(encoding="utf-8")
        assert files_heading in text, f"{lab_filename} missing '{files_heading}' section"
        files_idx = text.index(files_heading)
        anchor_idx = (
            text.index("## Step 1")
            if "## Step 1" in text
            else text.index("## Verification Checkpoint")
        )
        assert files_idx < anchor_idx, (
            f"{lab_filename} must place '{files_heading}' before '## Step 1' / '## Verification Checkpoint'"
        )

    starter_targets = (
        ("01-author-agent-plugin.md", "starter/lab01_plugin_skeleton"),
        ("02-register-agent-registry.md", "starter/lab02_plugin_sync_exercise.py"),
        ("03-build-deploy-middleware.md", "starter/lab03_middleware_redirect_exercise.py"),
        ("04-ide-developer-experience.md", "starter/lab04_hub_sync_adapter_exercise.py"),
    )
    for lab_filename, starter_target in starter_targets:
        text = (LABS_DIR / lab_filename).read_text(encoding="utf-8")
        assert starter_target in text, (
            f"{lab_filename} must explicitly reference starter target {starter_target!r}"
        )
        if "## Going Live on GCP" in text:
            assert text.index(starter_target) < text.index("## Going Live on GCP"), (
                f"{lab_filename} must reference {starter_target!r} before '## Going Live on GCP'"
            )

    no_starter_stub_re = re.compile(r"no\s+`?starter/`?\s+coding\s+stub", re.IGNORECASE)
    for lab_filename in (
        "05-platform-agents-gitlab-sot.md",
        "06-governance-scanning-lifecycle.md",
        "07-kms-encryption-and-signing.md",
        "08-access-control-and-audit.md",
    ):
        text = (LABS_DIR / lab_filename).read_text(encoding="utf-8")
        assert no_starter_stub_re.search(text), (
            f"{lab_filename} must explicitly state there is no `starter/` coding stub in that lab"
        )

    banned_cliches = (
        "faithfully simulating",
        "zero-friction",
        "in one atomic pass",
        "ultra-fast",
        "Articulate the critical",
    )
    checked_docs = [
        README_PATH,
        DOCS_DIR / "attendee-quickstart.md",
        STARTER_README_PATH,
        *(LABS_DIR / lab_filename for lab_filename in EXPECTED_LAB_FILES),
    ]
    cliche_hits: list[str] = []
    for doc_path in checked_docs:
        lower_text = doc_path.read_text(encoding="utf-8").lower()
        for phrase in banned_cliches:
            if phrase.lower() in lower_text:
                cliche_hits.append(
                    f"{doc_path.relative_to(REPO_ROOT)} contains banned cliché {phrase!r}"
                )
    assert not cliche_hits, "AI clichés detected in workshop documentation:\n" + "\n".join(
        cliche_hits
    )
