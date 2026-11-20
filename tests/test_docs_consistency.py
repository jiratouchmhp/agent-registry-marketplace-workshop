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
SPEC_PATH = REPO_ROOT / "WORKSHOP_SPEC.json"

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
        "test",
        "test-e2e",
        "validate-hcl",
        "run-mock",
        "run-samples",
        "run-middleware",
        "demo-sync",
        "demo-hub-sync",
        "tf-core-plan",
        "tf-sec-plan",
    }
)


def _all_doc_markdown_files() -> list[Path]:
    files = [README_PATH]
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
    """All 9 lab guides, README.md, executive brief, architecture guide, and Makefile exist."""
    required_files = [
        README_PATH,
        MAKEFILE_PATH,
        DOCS_DIR / "customer-usecase-3-brief.md",
        DOCS_DIR / "architecture.md",
        *(LABS_DIR / name for name in EXPECTED_LAB_FILES),
    ]
    for path in required_files:
        assert path.is_file(), f"Missing required curriculum file: {path}"
        content = path.read_text(encoding="utf-8").strip()
        assert len(content) > 200, f"Curriculum file appears incomplete/empty: {path}"


def test_makefile_defines_required_targets_and_venv_defaults() -> None:
    """Makefile defines all 10 required targets and uses .venv defaults."""
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
    """README.md links executive brief, architecture, starter, and all 9 lab guides with clear split."""
    readme = README_PATH.read_text(encoding="utf-8")
    assert "```mermaid" in readme
    assert "Workstream A" in readme
    assert "Workstream B" in readme
    assert "docs/customer-usecase-3-brief.md" in readme
    assert "docs/architecture.md" in readme
    assert "starter/README.md" in readme
    for lab_file in EXPECTED_LAB_FILES:
        assert f"docs/labs/{lab_file}" in readme, f"README.md missing link to docs/labs/{lab_file}"


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
