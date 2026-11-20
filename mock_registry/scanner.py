"""Strict Agent Registry Skill ZIP validation and simulated LLM/static security scanner."""

from __future__ import annotations

import hashlib
import io
import re
import stat
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from mock_registry.models import (
    SKILL_MAX_FILE_BYTES,
    SKILL_MAX_NESTING_DEPTH,
    SKILL_MAX_UNCOMPRESSED_BYTES,
    SKILL_MAX_ZIP_BYTES,
)
from schemas.validators import SKILL_NAME_RE, parse_frontmatter

TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".py",
        ".sh",
        ".bash",
        ".js",
        ".ts",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".sql",
        ".tf",
        ".hcl",
        ".env",
        ".cfg",
        ".ini",
        ".pem",
        ".key",
        ".xml",
        ".html",
        ".csv",
    }
)


class SkillArchiveValidationError(ValueError):
    """Raised when an uploaded Skill ZIP fails Agent Registry structural validation."""


@dataclass
class ValidatedSkillArchive:
    """Result of strict ZIP & SKILL.md validation."""

    sha256_hash: str
    size_bytes: int
    frontmatter: dict[str, Any]
    skill_md_path: str
    skill_md_body: str
    files_text: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _ScannerRule:
    rule_id: str
    finding_class: str  # "SECRET" | "VULNERABILITY"
    severity: str  # "SEVERITY_CRITICAL" | "SEVERITY_HIGH" | "SEVERITY_MEDIUM" | "SEVERITY_LOW"
    pattern: re.Pattern[str]
    detail_template: str


_SCANNER_RULES: tuple[_ScannerRule, ...] = (
    _ScannerRule(
        rule_id="aws-access-key",
        finding_class="SECRET",
        severity="SEVERITY_CRITICAL",
        pattern=re.compile(r"\bAKIA[0-9A-Z]{8,24}\b"),
        detail_template="Hardcoded AWS Access Key ID detected (AKIA*).",
    ),
    _ScannerRule(
        rule_id="google-api-key",
        finding_class="SECRET",
        severity="SEVERITY_CRITICAL",
        pattern=re.compile(r"\bAIza[0-9A-Za-z\-_]{10,45}\b"),
        detail_template="Hardcoded Google Cloud API key detected (AIza*).",
    ),
    _ScannerRule(
        rule_id="github-pat",
        finding_class="SECRET",
        severity="SEVERITY_CRITICAL",
        pattern=re.compile(r"\b(?:ghp_[0-9A-Za-z]{8,}|github_pat_[0-9A-Za-z_]{12,})\b"),
        detail_template="Hardcoded GitHub personal access token detected (ghp_*).",
    ),
    _ScannerRule(
        rule_id="private-key-material",
        finding_class="SECRET",
        severity="SEVERITY_CRITICAL",
        pattern=re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
            r"\bPRIVATE_KEY\s*[:=]\s*['\"]?[A-Za-z0-9+/=_-]{16,}"
        ),
        detail_template="Embedded cryptographic PRIVATE KEY material detected.",
    ),
    _ScannerRule(
        rule_id="prompt-injection-ignore",
        finding_class="VULNERABILITY",
        severity="SEVERITY_HIGH",
        pattern=re.compile(
            r"IGNORE\s+(?:ALL\s+)?PREVIOUS\s+INSTRUCTIONS|"
            r"DISREGARD\s+(?:ALL\s+)?PREVIOUS\s+INSTRUCTIONS|"
            r"FORGET\s+(?:ALL\s+)?PRIOR\s+INSTRUCTIONS",
            re.IGNORECASE,
        ),
        detail_template="Prompt injection marker detected attempting instruction override.",
    ),
    _ScannerRule(
        rule_id="prompt-injection-exfiltration",
        finding_class="VULNERABILITY",
        severity="SEVERITY_HIGH",
        pattern=re.compile(
            r"(?:exfiltrate|steal|leak)\s+(?:all\s+)?(?:secrets|credentials|tokens|env|keys)|"
            r"curl\s+[^\n]*\|\s*(?:ba)?sh|"
            r"wget\s+[^\n]*\|\s*(?:ba)?sh",
            re.IGNORECASE,
        ),
        detail_template="Suspicious credential exfiltration or remote shell execution pattern detected.",
    ),
)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    unix_mode = info.external_attr >> 16
    return stat.S_ISLNK(unix_mode)


def _is_scannable_file(rel_path: str, raw_bytes: bytes) -> bool:
    if b"\x00" in raw_bytes[:4096]:
        return False
    suffix = PurePosixPath(rel_path).suffix.lower()
    if suffix in TEXT_EXTENSIONS or suffix == "":
        return True
    try:
        raw_bytes.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def validate_skill_zip(zip_bytes: bytes) -> ValidatedSkillArchive:
    """Strictly validate a Skill ZIP archive according to Google Cloud Agent Registry v1 rules."""
    if not zip_bytes:
        raise SkillArchiveValidationError("Skill archive payload must not be empty.")
    if len(zip_bytes) > SKILL_MAX_ZIP_BYTES:
        raise SkillArchiveValidationError(
            f"Compressed ZIP size {len(zip_bytes)} bytes exceeds maximum of "
            f"{SKILL_MAX_ZIP_BYTES} bytes (1 MB)."
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r")
    except zipfile.BadZipFile as exc:
        raise SkillArchiveValidationError(f"Invalid ZIP archive: {exc}") from exc

    total_uncompressed = 0
    file_entries: dict[str, bytes] = {}
    skill_md_candidates: list[str] = []
    lowercase_skill_md: list[str] = []

    with zf:
        infos = zf.infolist()
        if not infos:
            raise SkillArchiveValidationError("ZIP archive is empty.")

        for info in infos:
            if _is_symlink(info):
                raise SkillArchiveValidationError(
                    f"Symlinks are forbidden in skill archives: '{info.filename}'."
                )

            raw_name = info.filename
            if raw_name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw_name):
                raise SkillArchiveValidationError(
                    f"Absolute paths are forbidden in skill archives: '{raw_name}'."
                )

            normalized = raw_name.replace("\\", "/")
            if normalized.endswith("/"):
                # Directory entry: still verify no traversal / depth violation.
                stripped = normalized.rstrip("/")
                if stripped:
                    parts = PurePosixPath(stripped).parts
                    if ".." in parts:
                        raise SkillArchiveValidationError(
                            f"Path traversal ('..') is forbidden: '{raw_name}'."
                        )
                    if len(parts) > SKILL_MAX_NESTING_DEPTH:
                        raise SkillArchiveValidationError(
                            f"Directory nesting depth {len(parts)} exceeds maximum "
                            f"{SKILL_MAX_NESTING_DEPTH}: '{raw_name}'."
                        )
                continue

            parts = PurePosixPath(normalized).parts
            if not parts:
                continue
            if ".." in parts:
                raise SkillArchiveValidationError(
                    f"Path traversal ('..') is forbidden: '{raw_name}'."
                )
            if len(parts) > SKILL_MAX_NESTING_DEPTH:
                raise SkillArchiveValidationError(
                    f"File nesting depth {len(parts)} exceeds maximum "
                    f"{SKILL_MAX_NESTING_DEPTH}: '{raw_name}'."
                )

            # Check declared and actual uncompressed sizes to prevent zip bombs.
            if info.file_size > SKILL_MAX_FILE_BYTES:
                raise SkillArchiveValidationError(
                    f"File '{normalized}' uncompressed size {info.file_size} bytes exceeds "
                    f"individual file limit of {SKILL_MAX_FILE_BYTES} bytes (1 MB)."
                )

            with zf.open(info, mode="r") as entry_fh:
                content = entry_fh.read(SKILL_MAX_FILE_BYTES + 1)
            if len(content) > SKILL_MAX_FILE_BYTES:
                raise SkillArchiveValidationError(
                    f"File '{normalized}' actual uncompressed size exceeds "
                    f"individual file limit of {SKILL_MAX_FILE_BYTES} bytes (1 MB)."
                )
            total_uncompressed += len(content)
            if total_uncompressed > SKILL_MAX_UNCOMPRESSED_BYTES:
                raise SkillArchiveValidationError(
                    f"Total uncompressed archive size exceeds maximum of "
                    f"{SKILL_MAX_UNCOMPRESSED_BYTES} bytes (10 MB)."
                )

            rel_path = "/".join(parts)
            file_entries[rel_path] = content

            basename = parts[-1]
            if basename == "SKILL.md":
                skill_md_candidates.append(rel_path)
            elif basename.lower() == "skill.md":
                lowercase_skill_md.append(rel_path)

    if lowercase_skill_md:
        raise SkillArchiveValidationError(
            f"Skill instruction file must be strictly uppercase 'SKILL.md'; "
            f"found invalid casing: {', '.join(lowercase_skill_md)}."
        )
    if len(skill_md_candidates) == 0:
        raise SkillArchiveValidationError(
            "Skill archive must contain strictly one uppercase 'SKILL.md' at root or inside "
            "a single top-level directory."
        )
    if len(skill_md_candidates) > 1:
        raise SkillArchiveValidationError(
            f"Skill archive must contain strictly one 'SKILL.md', found {len(skill_md_candidates)}: "
            f"{', '.join(sorted(skill_md_candidates))}."
        )

    skill_md_path = skill_md_candidates[0]
    skill_md_parts = PurePosixPath(skill_md_path).parts
    if len(skill_md_parts) > 2:
        raise SkillArchiveValidationError(
            f"'SKILL.md' must be at archive root ('SKILL.md') or inside a single top-level "
            f"directory ('<dir>/SKILL.md'), got '{skill_md_path}'."
        )
    if len(skill_md_parts) == 2:
        top_dir = skill_md_parts[0]
        for path in file_entries:
            if PurePosixPath(path).parts[0] != top_dir:
                raise SkillArchiveValidationError(
                    f"All archive files must live under top-level directory '{top_dir}/' "
                    f"when SKILL.md is nested, found '{path}'."
                )

    try:
        skill_md_text = file_entries[skill_md_path].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillArchiveValidationError(f"'SKILL.md' must be valid UTF-8: {exc}") from exc

    frontmatter, body, fm_err = parse_frontmatter(skill_md_text)
    if fm_err or frontmatter is None:
        raise SkillArchiveValidationError(f"Invalid SKILL.md frontmatter: {fm_err}")

    fm_name = frontmatter.get("name")
    if not isinstance(fm_name, str) or not SKILL_NAME_RE.match(fm_name) or "--" in fm_name:
        raise SkillArchiveValidationError(
            f"SKILL.md frontmatter 'name' ({fm_name!r}) must be 1-64 chars lowercase [a-z0-9-] "
            "with no leading, trailing, or consecutive hyphens."
        )

    fm_desc = frontmatter.get("description")
    if not isinstance(fm_desc, str) or not fm_desc.strip() or len(fm_desc) > 1024:
        raise SkillArchiveValidationError(
            "SKILL.md frontmatter 'description' must be a non-empty string of 1-1024 chars."
        )

    fm_metadata = frontmatter.get("metadata")
    if fm_metadata is not None:
        if not isinstance(fm_metadata, dict):
            raise SkillArchiveValidationError(
                "SKILL.md frontmatter 'metadata' must be a map<string, string>."
            )
        for k, v in fm_metadata.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise SkillArchiveValidationError(
                    f"SKILL.md frontmatter metadata[{k!r}] must have scalar string key and value."
                )

    normalized_frontmatter: dict[str, Any] = {
        "name": fm_name,
        "description": fm_desc,
    }
    if isinstance(frontmatter.get("license"), str):
        normalized_frontmatter["license"] = frontmatter["license"]
    if isinstance(frontmatter.get("compatibility"), str):
        normalized_frontmatter["compatibility"] = frontmatter["compatibility"]
    if isinstance(fm_metadata, dict):
        normalized_frontmatter["metadata"] = dict(fm_metadata)

    files_text: dict[str, str] = {}
    for rel_path, raw in sorted(file_entries.items()):
        if _is_scannable_file(rel_path, raw):
            files_text[rel_path] = raw.decode("utf-8", errors="replace")

    return ValidatedSkillArchive(
        sha256_hash=hashlib.sha256(zip_bytes).hexdigest(),
        size_bytes=len(zip_bytes),
        frontmatter=normalized_frontmatter,
        skill_md_path=skill_md_path,
        skill_md_body=body,
        files_text=files_text,
    )


def scan_skill_archive(
    files_text: dict[str, str],
    *,
    resource_name: str,
    project: str,
    location: str,
) -> list[dict[str, Any]]:
    """Run simulated static + LLM security scan over extracted skill text files.

    Detects hardcoded secrets (AKIA, AIza, ghp_, PRIVATE KEY) and prompt-injection
    or exfiltration patterns, returning Agent Registry v1 Finding resource dicts.
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    findings: list[dict[str, Any]] = []

    for rule in _SCANNER_RULES:
        matched_locations: list[dict[str, Any]] = []
        for rel_path, content in sorted(files_text.items()):
            for line_no, line in enumerate(content.splitlines(), start=1):
                if rule.pattern.search(line):
                    matched_locations.append({"path": rel_path, "lineNumber": line_no})

        if matched_locations:
            uid = str(uuid.uuid4())
            finding_name = f"projects/{project}/locations/{location}/findings/{uid}"
            findings.append(
                {
                    "name": finding_name,
                    "uid": uid,
                    "resource": resource_name,
                    "severity": rule.severity,
                    "findingClass": rule.finding_class,
                    "files": matched_locations,
                    "detail": rule.detail_template,
                    "createTime": now,
                    "updateTime": now,
                }
            )

    return findings
