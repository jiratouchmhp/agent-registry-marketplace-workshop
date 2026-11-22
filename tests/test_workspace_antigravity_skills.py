"""Automated validation for workspace-scoped Antigravity skills under .agents/skills/."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from schemas.validators import parse_frontmatter, validate_skill_md
from tools.plugin_sync.sync import MAX_ZIP_COMPRESSED_BYTES, build_skill_zip, validate_skill_zip

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"
MAKEFILE_PATH = REPO_ROOT / "Makefile"
README_PATH = REPO_ROOT / "README.md"

EXPECTED_SKILL_NAMES: tuple[str, ...] = (
    "agent-marketplace-workshop-guide",
    "polyglot-agent-plugin-authoring",
    "agent-registry-v1-ingest-and-lifecycle",
    "marketplace-middleware-and-ide-delivery",
    "agent-registry-security-and-prod-hardening",
)

NON_REPO_CLI_MODULES: frozenset[str] = frozenset(
    {
        "json.tool",
        "zipfile",
        "uvicorn",
        "venv",
    }
)

# Built from parts so this public test module itself stays clean under repo hygiene scans.
_FORBIDDEN_INTERNAL_TOKEN_PARTS: tuple[tuple[str, ...], ...] = (
    ("corp", ".google.com"),
    ("/goo", "gle/src"),
    ("/goo", "gle/bin"),
    ("/usr/local/", "google"),
    ("g3", "terraform"),
    ("goo", "gle3"),
    ("mo", "ma"),
    ("ci", "tc"),
    ("pi", "per"),
    ("buga", "nizer"),
    ("ya", "qs"),
    ("crit", "ique"),
    ("cloud", "top"),
)
FORBIDDEN_INTERNAL_PATTERNS: tuple[str, ...] = tuple(
    "".join(parts) for parts in _FORBIDDEN_INTERNAL_TOKEN_PARTS
)


def _all_skill_markdown_files() -> list[Path]:
    return sorted(SKILLS_ROOT.rglob("*.md"))


def _strip_fenced_code_blocks(markdown_text: str) -> str:
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

    for span in re.findall(r"`([^`\n]+)`", markdown_text):
        for match in re.finditer(r"(?:^|[;&|]\s*|\s)make\s+([a-z][a-z0-9_-]*)\b", span):
            referenced.add(match.group(1))

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


def _is_non_repo_module(mod_name: str) -> bool:
    if mod_name in NON_REPO_CLI_MODULES:
        return True
    top_level = mod_name.split(".", 1)[0]
    return top_level in sys.stdlib_module_names


def test_exact_five_workspace_skill_directories_exist() -> None:
    """Exactly the 5 expected workspace skills exist under .agents/skills/ with zero symlinks."""
    assert SKILLS_ROOT.is_dir(), f"Missing workspace skills directory: {SKILLS_ROOT}"

    actual_dirs = sorted(
        entry.name
        for entry in SKILLS_ROOT.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    )
    assert actual_dirs == sorted(EXPECTED_SKILL_NAMES), (
        f"Expected exact workspace skill directories {sorted(EXPECTED_SKILL_NAMES)}, "
        f"got {actual_dirs}"
    )

    symlinks = [p for p in SKILLS_ROOT.rglob("*") if p.is_symlink()]
    assert not symlinks, f"Symlinks are forbidden inside .agents/skills/: {symlinks}"


@pytest.mark.parametrize("skill_name", EXPECTED_SKILL_NAMES)
def test_skill_passes_agentskills_validator_and_deterministic_zip_packaging(skill_name: str) -> None:
    """Each workspace skill passes validate_skill_md() and deterministic build_skill_zip() <= 1 MB."""
    skill_dir = SKILLS_ROOT / skill_name
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file(), f"Missing uppercase SKILL.md: {skill_md}"

    md_res = validate_skill_md(skill_md, expected_name=skill_name)
    assert md_res.valid, f"{skill_name} failed validate_skill_md(): {md_res.errors}"

    pkg_1 = build_skill_zip(skill_dir, {"plugin_name": "workspace-skills-check"})
    pkg_2 = build_skill_zip(skill_dir, {"plugin_name": "workspace-skills-check"})

    assert len(pkg_1.zip_bytes) <= MAX_ZIP_COMPRESSED_BYTES, (
        f"{skill_name} ZIP exceeds 1 MB ({len(pkg_1.zip_bytes)} bytes)"
    )
    assert pkg_1.sha256_hex == pkg_2.sha256_hex, (
        f"{skill_name} ZIP packaging is not deterministic"
    )
    validate_skill_zip(pkg_1.zip_bytes, expected_skill_name=skill_name)


@pytest.mark.parametrize("skill_name", EXPECTED_SKILL_NAMES)
def test_skill_triggering_and_craft_quality(skill_name: str) -> None:
    """Each SKILL.md meets length, mutually exclusive routing, and structural craft standards."""
    skill_md = SKILLS_ROOT / skill_name / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines()

    assert 100 <= len(lines) <= 350, (
        f"{skill_name}/SKILL.md line count ({len(lines)}) must be between 100 and 350"
    )

    frontmatter, body, err = parse_frontmatter(text)
    assert err is None and frontmatter is not None, f"{skill_name}: {err}"
    assert frontmatter.get("name") == skill_name

    description = frontmatter.get("description", "")
    assert isinstance(description, str) and 1 <= len(description) <= 1024
    assert "Use when" in description, f"{skill_name} description must contain 'Use when'"
    assert "Don't use for" in description, f"{skill_name} description must contain \"Don't use for\""

    siblings = [other for other in EXPECTED_SKILL_NAMES if other != skill_name]
    missing_siblings = [other for other in siblings if other not in description]
    assert not missing_siblings, (
        f"{skill_name} description missing sibling skill routing references: {missing_siblings}"
    )

    h2_headings = [line.strip() for line in body.splitlines() if line.startswith("## ")]
    headings_joined = "\n".join(h2_headings)

    assert any("Decision Tree" in h for h in h2_headings), (
        f"{skill_name} missing Decision Tree section (headings: {headings_joined})"
    )
    assert any("Gotchas" in h and "Do This, Not That" in h for h in h2_headings), (
        f"{skill_name} missing Gotchas ('Do This, Not That') section (headings: {headings_joined})"
    )
    assert any("Quick-Start Patterns" in h for h in h2_headings), (
        f"{skill_name} missing Quick-Start Patterns section (headings: {headings_joined})"
    )
    assert any("Verification" in h for h in h2_headings), (
        f"{skill_name} missing Verification section (headings: {headings_joined})"
    )
    assert any("Reference" in h for h in h2_headings), (
        f"{skill_name} missing Reference Guides section (headings: {headings_joined})"
    )


@pytest.mark.parametrize("skill_name", EXPECTED_SKILL_NAMES)
def test_skill_references_and_relative_links_integrity(skill_name: str) -> None:
    """Every references/*.md is >300 bytes and cited in SKILL.md, and all relative links exist."""
    skill_dir = SKILLS_ROOT / skill_name
    skill_md = skill_dir / "SKILL.md"
    skill_text = skill_md.read_text(encoding="utf-8")

    refs_dir = skill_dir / "references"
    assert refs_dir.is_dir(), f"{skill_name} missing references/ directory"
    ref_files = sorted(refs_dir.glob("*.md"))
    assert ref_files, f"{skill_name}/references/ contains no Markdown files"

    for ref_file in ref_files:
        size = ref_file.stat().st_size
        assert size > 300, f"{ref_file.relative_to(REPO_ROOT)} appears empty/too small ({size} bytes)"
        rel_ref = f"references/{ref_file.name}"
        assert rel_ref in skill_text, (
            f"{skill_name}/SKILL.md does not explicitly reference {rel_ref!r}"
        )

    # Every `references/...` path mentioned in SKILL.md must exist on disk.
    mentioned_refs = sorted(set(re.findall(r"references/[A-Za-z0-9_./-]+\.md", skill_text)))
    assert mentioned_refs, f"{skill_name}/SKILL.md mentions no references/*.md paths"
    for rel_ref in mentioned_refs:
        target = (skill_dir / rel_ref).resolve()
        target.relative_to(skill_dir.resolve())
        assert target.is_file(), f"{skill_name}/SKILL.md references missing file: {rel_ref}"

    # Verify all relative Markdown links across SKILL.md and its references/*.md resolve cleanly.
    link_re = re.compile(r"!?\[[^\]]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    for md_file in [skill_md, *ref_files]:
        prose = _strip_fenced_code_blocks(md_file.read_text(encoding="utf-8"))
        for match in link_re.finditer(prose):
            raw_target = match.group(1).strip()
            if raw_target.startswith(("http://", "https://", "mailto:", "#", "<")):
                continue
            clean_rel = raw_target.split("#", 1)[0].split("?", 1)[0]
            if not clean_rel:
                continue
            resolved = (md_file.parent / clean_rel).resolve()
            resolved.relative_to(REPO_ROOT.resolve())
            assert resolved.exists(), (
                f"Broken relative link in {md_file.relative_to(REPO_ROOT)}: {raw_target!r}"
            )


def test_all_make_targets_in_skills_exist_in_makefile() -> None:
    """Every `make <target>` mentioned across .agents/skills/**/*.md exists in Makefile."""
    defined = _extract_make_targets_defined(MAKEFILE_PATH.read_text(encoding="utf-8"))
    missing: list[str] = []
    discovered: set[str] = set()

    for md_file in _all_skill_markdown_files():
        refs = _extract_referenced_make_targets(md_file.read_text(encoding="utf-8"))
        discovered.update(refs)
        for target in sorted(refs):
            if target not in defined:
                missing.append(
                    f"{md_file.relative_to(REPO_ROOT)} references undefined `make {target}`"
                )

    assert discovered, "Expected .agents/skills/**/*.md to reference at least one Makefile target"
    assert not missing, "Undefined Makefile targets in workspace skills:\n" + "\n".join(missing)


def test_all_python_modules_in_skills_are_importable_repo_modules() -> None:
    """Every non-stdlib `python -m <mod>` in .agents/skills/**/*.md is an importable repo module."""
    unresolved: list[str] = []
    discovered_repo_mods: set[str] = set()
    repo_root_resolved = REPO_ROOT.resolve()

    for md_file in _all_skill_markdown_files():
        mods = _extract_python_dash_m_modules(md_file.read_text(encoding="utf-8"))
        for mod_name in sorted(mods):
            if _is_non_repo_module(mod_name):
                continue
            discovered_repo_mods.add(mod_name)
            try:
                spec = importlib.util.find_spec(mod_name)
            except (ImportError, ModuleNotFoundError, ValueError):
                spec = None
            if spec is None:
                unresolved.append(
                    f"{md_file.relative_to(REPO_ROOT)} references unimportable `python -m {mod_name}`"
                )
                continue

            # Confirm the resolved module belongs to this repository.
            candidate_paths: list[Path] = []
            if spec.origin and spec.origin not in ("built-in", "frozen"):
                candidate_paths.append(Path(spec.origin).resolve())
            if spec.submodule_search_locations:
                candidate_paths.extend(Path(p).resolve() for p in spec.submodule_search_locations)
            in_repo = any(
                p == repo_root_resolved or repo_root_resolved in p.parents for p in candidate_paths
            )
            if not in_repo:
                unresolved.append(
                    f"{md_file.relative_to(REPO_ROOT)} references non-repo module "
                    f"`python -m {mod_name}` (resolved to {candidate_paths})"
                )

    assert {"tools.plugin_sync", "tools.hub_sync", "platform_agent.adk_orchestrator"} <= (
        discovered_repo_mods
    ), f"Expected core CLI modules in workspace skills, found {sorted(discovered_repo_mods)}"
    assert not unresolved, "Unresolvable `python -m` modules in skills:\n" + "\n".join(unresolved)


def test_workspace_skills_public_github_hygiene() -> None:
    """Zero internal paths or strings appear anywhere under .agents/skills/."""
    pattern = re.compile(
        "|".join(re.escape(token) for token in FORBIDDEN_INTERNAL_PATTERNS),
        re.IGNORECASE,
    )
    violations: list[str] = []

    for path in sorted(SKILLS_ROOT.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if match:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_no} matched forbidden pattern "
                    f"{match.group(0)!r}"
                )

    assert not violations, "Public GitHub hygiene violations in .agents/skills/:\n" + "\n".join(
        violations
    )


def test_workspace_skills_no_invalid_gcloud_file_flag_or_double_url() -> None:
    """Regression guard against `gcloud builds submit --file` and prefixed `${SIGNED_LOC}`."""
    invalid_gcloud_re = re.compile(r"gcloud\s+builds\s+submit[^\n]*--file(?:=|\s)")
    double_url_re = re.compile(r"https?://[^\s\"']+\$\{SIGNED_LOC\}")

    for md_file in _all_skill_markdown_files():
        non_comment_lines = [
            line
            for line in md_file.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        joined = re.sub(r"\\\n\s*", " ", "\n".join(non_comment_lines))
        assert not invalid_gcloud_re.search(joined), (
            f"{md_file.relative_to(REPO_ROOT)} contains invalid `gcloud builds submit --file`"
        )
        assert not double_url_re.search(joined), (
            f"{md_file.relative_to(REPO_ROOT)} prepends a scheme/host onto ${{SIGNED_LOC}}"
        )


def test_readme_documents_all_five_workspace_skills() -> None:
    """README.md documents the Antigravity Workspace Skills section, all 5 skills, and repo map."""
    readme = README_PATH.read_text(encoding="utf-8")
    assert "## Antigravity Workspace Skills (`.agents/skills/`)" in readme
    assert "[.agents/skills/](.agents/skills/)" in readme
    for skill_name in EXPECTED_SKILL_NAMES:
        assert f"/{skill_name}" in readme, f"README.md missing slash command /{skill_name}"
        assert f".agents/skills/{skill_name}/SKILL.md" in readme, (
            f"README.md missing link to .agents/skills/{skill_name}/SKILL.md"
        )


def test_workspace_skills_match_bangkok_first_architecture() -> None:
    """Workspace skills omit removed 2-region failover flags and document Bangkok primary + Singapore Agent Identity fallback + global Skills."""
    forbidden_tokens = (
        "--enable-secondary",
        "ENABLE_SECONDARY",
        "enable_secondary_region",
        "secondary_location",
        "secondary_middleware_url",
        "--fallback-middleware-url",
        "HUB_FALLBACK_MIDDLEWARE_URL",
        "GCP_SECONDARY_LOCATION",
    )
    violations: list[str] = []
    for md_file in _all_skill_markdown_files():
        text = md_file.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in text:
                violations.append(
                    f"{md_file.relative_to(REPO_ROOT)} contains removed 2-region token {token!r}"
                )
    assert not violations, "Stale 2-region references in .agents/skills/:\n" + "\n".join(violations)

    workshop_guide = (SKILLS_ROOT / "agent-marketplace-workshop-guide" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    security_skill = (
        SKILLS_ROOT / "agent-registry-security-and-prod-hardening" / "SKILL.md"
    ).read_text(encoding="utf-8")
    for label, text in (
        ("agent-marketplace-workshop-guide", workshop_guide),
        ("agent-registry-security-and-prod-hardening", security_skill),
    ):
        for token in ("asia-southeast3", 'auth_provider_location = "asia-southeast1"', "global"):
            assert token in text, f"{label} missing Bangkok-first regional token: {token}"
