"""Plugin-to-Agent-Registry sync tool for Agent Plugins 1.0 bundles.

Decomposes an agent-plugins.org plugin directory into:
  - Per-skill deterministic ZIP archives (agentskills.io) with canonical frontmatter.metadata
    and optional ECDSA P-256 (local PEM or Cloud KMS) signatures
  - Google Cloud Agent Registry v1 Skill / SkillRevision resources (handling private- prefixing)
  - Google Cloud Agent Registry v1 Service resources for MCP Servers (TOOL_SPEC) and
    A2A v1.0 Agents (A2A_AGENT_CARD with empty top-level interfaces)
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from schemas.validators import (
    parse_frontmatter,
    validate_agent_plugin_bundle,
    validate_skill_md,
)

# Agent Registry v1 archive limits (VERIFIED_SPECS.md & skill.proto)
MAX_ZIP_COMPRESSED_BYTES = 1 * 1024 * 1024  # 1 MB hard limit
INLINE_ZIP_RECOMMENDED_BYTES = 500 * 1024  # 500 KB recommended for archiveUploadSource
MAX_ZIP_UNCOMPRESSED_BYTES = 10 * 1024 * 1024  # 10 MB total uncompressed
MAX_FILE_UNCOMPRESSED_BYTES = 1 * 1024 * 1024  # 1 MB individual file
MAX_ARCHIVE_DEPTH = 8
MAX_SERVICE_SPEC_BYTES = 10 * 1024  # 10 KB limit for agentSpec / mcpServerSpec content

ZIP_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
PRIVATE_SKILL_PREFIX = "private-"
SIGNATURE_METADATA_KEYS = frozenset({"kms_signature", "kms_key_version"})
SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
VALID_TARGET_STATES = frozenset(
    {
        "TARGET_STATE_DRAFT",
        "TARGET_STATE_ACTIVE",
        "TARGET_STATE_DISABLED",
        "TARGET_STATE_DEPRECATED",
        "TARGET_STATE_DECOMMISSIONED",
    }
)
VALID_SCANNING_POLICIES = frozenset(
    {"ENABLED_BLOCKING", "ENABLED_NONBLOCKING", "DISABLED"}
)
SERVICE_ID_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")


class PluginSyncError(Exception):
    """Base exception raised by plugin_sync."""


class SkillArchiveError(PluginSyncError):
    """Raised when a skill directory or ZIP violates Agent Registry constraints."""


class RegistryApiError(PluginSyncError):
    """Raised when an Agent Registry v1 REST API call or LRO fails."""


@dataclass
class PackagedSkill:
    """In-memory representation of a validated, packaged, and optionally signed skill."""

    raw_skill_id: str
    private_skill_id: str
    display_name: str
    description: str
    frontmatter: dict[str, Any]
    zip_bytes: bytes
    sha256_hex: str
    unsigned_sha256_hex: str
    kms_signature: str | None = None
    kms_key_version: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class PackagedService:
    """In-memory representation of an Agent Registry Service payload (MCP or A2A)."""

    service_id: str
    kind: str  # "mcp" or "a2a"
    payload: dict[str, Any]


@dataclass
class SyncPlan:
    """Complete decomposed sync plan ready for dry-run inspection or live upload."""

    plugin_name: str
    plugin_display_name: str
    plugin_version: str
    source_repo: str
    git_commit: str
    project: str
    location: str
    target_state: str
    scanning_policy: str
    skills: list[PackagedSkill] = field(default_factory=list)
    mcp_services: list[PackagedService] = field(default_factory=list)
    agent_services: list[PackagedService] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def canonical_private_skill_id(skill_id: str) -> str:
    """Return the Agent Registry PRIVATE-publisher resource ID (private-<id>)."""
    cleaned = skill_id.strip()
    if not cleaned:
        raise SkillArchiveError("skill_id must not be empty")
    return cleaned if cleaned.startswith(PRIVATE_SKILL_PREFIX) else f"{PRIVATE_SKILL_PREFIX}{cleaned}"


def raw_create_skill_id(skill_id: str) -> str:
    """Strip any leading 'private-' prefix for initial POST .../skills?skillId=... calls."""
    cleaned = skill_id.strip()
    if not cleaned:
        raise SkillArchiveError("skill_id must not be empty")
    if cleaned.startswith(PRIVATE_SKILL_PREFIX):
        return cleaned[len(PRIVATE_SKILL_PREFIX) :]
    return cleaned


def sanitize_resource_id(raw: str, max_len: int = 63) -> str:
    """Convert an arbitrary string into a valid GCP resource ID ([a-z0-9-])."""
    slug = SERVICE_ID_SANITIZE_RE.sub("-", raw.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        slug = "service"
    return slug[:max_len].rstrip("-")


def compute_sha256_digest(data: bytes) -> bytes:
    """Return raw 32-byte SHA-256 digest of data."""
    return hashlib.sha256(data).digest()


def _serialize_frontmatter_markdown(frontmatter: dict[str, Any], body: str) -> str:
    """Deterministically serialize YAML frontmatter + markdown body."""
    yaml_text = yaml.safe_dump(
        frontmatter,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        width=120,
    ).strip()
    cleaned_body = body.lstrip("\n")
    if cleaned_body and not cleaned_body.endswith("\n"):
        cleaned_body += "\n"
    return f"---\n{yaml_text}\n---\n\n{cleaned_body}"


def _build_deterministic_zip_from_entries(entries: list[tuple[str, bytes, int]]) -> bytes:
    """Build a byte-for-byte deterministic ZIP archive from (arcname, data, mode) tuples."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arcname, data, mode in sorted(entries, key=lambda item: item[0]):
            info = zipfile.ZipInfo(filename=arcname, date_time=ZIP_FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix
            info.external_attr = (0o100000 | (mode & 0o777)) << 16
            zf.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buf.getvalue()


def _canonicalize_skill_md(raw_text: str) -> bytes:
    """Canonicalize SKILL.md excluding kms_signature/kms_key_version for non-circular signing.

    Matches `middleware/app/security_verifier.py` byte-for-byte.
    """
    frontmatter, body, err = parse_frontmatter(raw_text)
    if err or frontmatter is None:
        raise SkillArchiveError(f"Cannot canonicalize invalid SKILL.md: {err}")

    cleaned_fm = dict(frontmatter)
    metadata = cleaned_fm.get("metadata")
    if isinstance(metadata, dict):
        cleaned_meta = {
            k: v for k, v in metadata.items() if k not in SIGNATURE_METADATA_KEYS
        }
        if cleaned_meta:
            cleaned_fm["metadata"] = cleaned_meta
        else:
            cleaned_fm.pop("metadata", None)

    canonical_fm = json.dumps(cleaned_fm, sort_keys=True, separators=(",", ":"))
    normalized_body = body.replace("\r\n", "\n").strip()
    return f"{canonical_fm}\n---\n{normalized_body}\n".encode("utf-8")


def compute_canonical_skill_payload(zip_bytes: bytes) -> bytes:
    """Build zlib-independent canonical content stream covering every file in a skill ZIP.

    Matches `middleware/app/security_verifier.py::compute_canonical_skill_payload` byte-for-byte:
    - Prefix `b"agent-registry-skill-v1\\x00"`
    - Sorted non-dir entries framed as `rel_name\\x00len\\x00payload\\x00`
    - Strips leading `<skill-dir>/` when `SKILL.md` is nested inside `<skill-dir>/SKILL.md`
    - Canonicalizes `SKILL.md` without `kms_signature` and `kms_key_version`
    """
    chunks: list[bytes] = [b"agent-registry-skill-v1\x00"]
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        entries = sorted(
            (info for info in zf.infolist() if not info.is_dir()),
            key=lambda i: i.filename,
        )
        skill_paths = [
            i.filename for i in entries if PurePosixPath(i.filename).name == "SKILL.md"
        ]
        top_prefix = ""
        if len(skill_paths) == 1:
            parts = PurePosixPath(skill_paths[0]).parts
            if len(parts) == 2:
                top_prefix = f"{parts[0]}/"

        for info in entries:
            rel_name = info.filename
            if top_prefix and rel_name.startswith(top_prefix):
                rel_name = rel_name[len(top_prefix) :]
            raw = zf.read(info)
            if PurePosixPath(rel_name).name == "SKILL.md":
                payload = _canonicalize_skill_md(raw.decode("utf-8"))
            else:
                payload = raw
            chunks.append(rel_name.encode("utf-8"))
            chunks.append(b"\x00")
            chunks.append(str(len(payload)).encode("ascii"))
            chunks.append(b"\x00")
            chunks.append(payload)
            chunks.append(b"\x00")
    return b"".join(chunks)


def compute_canonical_skill_digest(zip_bytes: bytes) -> bytes:
    """Compute the 32-byte SHA-256 digest of the canonical skill payload."""
    return hashlib.sha256(compute_canonical_skill_payload(zip_bytes)).digest()


def _validate_skill_md_content(
    markdown_text: str, *, label: str, expected_name: str | None = None
) -> None:
    """Validate SKILL.md YAML frontmatter fields when inspecting standalone ZIPs."""
    frontmatter, _body, fm_err = parse_frontmatter(markdown_text)
    if fm_err or frontmatter is None:
        raise SkillArchiveError(f"{label}: {fm_err}")

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name:
        raise SkillArchiveError(f"{label}: frontmatter missing required non-empty string 'name'")
    if not SKILL_NAME_RE.match(name) or "--" in name:
        raise SkillArchiveError(
            f"{label}: frontmatter 'name' ({name!r}) must be 1-64 chars lowercase [a-z0-9-] "
            f"with no leading, trailing, or consecutive hyphens"
        )
    if expected_name is not None and name != expected_name:
        raise SkillArchiveError(
            f"{label}: frontmatter 'name' ({name!r}) must match skill folder name ({expected_name!r})"
        )

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillArchiveError(
            f"{label}: frontmatter missing required non-empty string 'description'"
        )
    if len(description) > 1024:
        raise SkillArchiveError(
            f"{label}: frontmatter 'description' exceeds 1024 chars ({len(description)})"
        )

    metadata = frontmatter.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise SkillArchiveError(f"{label}: frontmatter 'metadata' must be a map<string, string>")
        for k, v in metadata.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise SkillArchiveError(
                    f"{label}: frontmatter metadata[{k!r}] must have scalar string key and value "
                    f"(got {type(v).__name__})"
                )


def validate_skill_zip(zip_bytes: bytes, expected_skill_name: str | None = None) -> list[str]:
    """Validate an existing ZIP archive against all Agent Registry Skill constraints.

    Returns a list of non-fatal warnings; raises SkillArchiveError on any constraint violation.
    """
    warnings: list[str] = []
    if len(zip_bytes) > MAX_ZIP_COMPRESSED_BYTES:
        raise SkillArchiveError(
            f"Compressed skill ZIP ({len(zip_bytes)} bytes) exceeds Agent Registry 1 MB limit "
            f"({MAX_ZIP_COMPRESSED_BYTES} bytes)"
        )
    if len(zip_bytes) > INLINE_ZIP_RECOMMENDED_BYTES:
        warnings.append(
            f"Compressed skill ZIP ({len(zip_bytes)} bytes) exceeds 500 KB inline "
            f"archiveUploadSource recommendation; consider gcsSource."
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r")
    except zipfile.BadZipFile as exc:
        raise SkillArchiveError(f"Invalid ZIP archive: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if not infos:
            raise SkillArchiveError("Skill ZIP archive is empty")

        total_uncompressed = 0
        skill_md_paths: list[str] = []
        skill_md_content: str | None = None
        top_level_dirs: set[str] = set()
        root_level_files: list[str] = []

        for info in infos:
            name = info.filename
            # Path traversal / absolute path / backslash checks
            if name.startswith("/") or name.startswith("\\") or "\\" in name:
                raise SkillArchiveError(f"Forbidden absolute or backslash path in ZIP: {name!r}")
            posix = PurePosixPath(name)
            if any(part == ".." for part in posix.parts):
                raise SkillArchiveError(f"Path traversal ('..') forbidden in ZIP: {name!r}")

            # Symlink detection via Unix mode bits in external_attr (S_IFLNK = 0o120000)
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise SkillArchiveError(f"Symlink entry forbidden in skill ZIP: {name!r}")

            if info.is_dir():
                continue

            parts = [p for p in posix.parts if p and p != "."]
            if not parts:
                continue
            if len(parts) > MAX_ARCHIVE_DEPTH:
                raise SkillArchiveError(
                    f"Archive entry {name!r} exceeds maximum nesting depth {MAX_ARCHIVE_DEPTH} "
                    f"(depth={len(parts)})"
                )
            if len(parts) > 1:
                top_level_dirs.add(parts[0])
            else:
                root_level_files.append(name)

            # Read actual bytes (guards against forged ZIP header sizes)
            content = zf.read(info)
            if len(content) > MAX_FILE_UNCOMPRESSED_BYTES:
                raise SkillArchiveError(
                    f"File {name!r} ({len(content)} bytes) exceeds 1 MB per-file uncompressed limit"
                )
            total_uncompressed += len(content)
            if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise SkillArchiveError(
                    f"Total uncompressed archive size ({total_uncompressed} bytes) exceeds 10 MB limit"
                )

            base = parts[-1]
            if base == "SKILL.md":
                skill_md_paths.append(name)
                try:
                    skill_md_content = content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise SkillArchiveError(f"{name!r} is not valid UTF-8: {exc}") from exc
            elif base.lower() == "skill.md":
                raise SkillArchiveError(
                    f"Found non-uppercase skill manifest {name!r}; must be strictly uppercase 'SKILL.md'"
                )

        if len(skill_md_paths) == 0:
            raise SkillArchiveError("Skill ZIP must contain exactly one uppercase 'SKILL.md' (found 0)")
        if len(skill_md_paths) > 1:
            raise SkillArchiveError(
                f"Skill ZIP must contain strictly ONE 'SKILL.md' (found {len(skill_md_paths)}: "
                f"{skill_md_paths})"
            )

        skill_md_arcname = skill_md_paths[0]
        md_parts = PurePosixPath(skill_md_arcname).parts
        folder_expected_name = expected_skill_name
        if len(md_parts) == 1:
            # Root-level SKILL.md
            pass
        elif len(md_parts) == 2:
            if root_level_files:
                raise SkillArchiveError(
                    f"Stray root-level files forbidden when SKILL.md is inside "
                    f"'{md_parts[0]}/SKILL.md': {sorted(root_level_files)}"
                )
            if len(top_level_dirs) > 1:
                raise SkillArchiveError(
                    f"Skill ZIP with top-level folder must contain a single root directory "
                    f"(found {sorted(top_level_dirs)})"
                )
            if expected_skill_name is not None and md_parts[0] != expected_skill_name:
                raise SkillArchiveError(
                    f"Top-level folder {md_parts[0]!r} does not match expected skill name "
                    f"{expected_skill_name!r}"
                )
            folder_expected_name = md_parts[0]
        else:
            raise SkillArchiveError(
                f"'SKILL.md' must be at archive root or '<skill-name>/SKILL.md', got {skill_md_arcname!r}"
            )

        assert skill_md_content is not None
        _validate_skill_md_content(
            skill_md_content,
            label=skill_md_arcname,
            expected_name=folder_expected_name,
        )

    return warnings


def sign_digest_with_pem(digest: bytes, pem_path: str | Path) -> str:
    """Sign a 32-byte SHA-256 digest deterministically (RFC 6979) with an ECDSA P-256 PEM key.

    Returns base64-encoded DER signature matching Cloud KMS EC_SIGN_P256_SHA256 output.
    """
    if len(digest) != 32:
        raise ValueError(f"Expected 32-byte SHA-256 digest, got {len(digest)} bytes")
    pem_bytes = Path(pem_path).read_bytes()
    private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        raise PluginSyncError("Signing key must be an ECDSA P-256 (SECP256R1) private key")
    signature_der = private_key.sign(
        digest,
        ec.ECDSA(utils.Prehashed(hashes.SHA256()), deterministic_signing=True),
    )
    return base64.b64encode(signature_der).decode("ascii")


def sign_digest_with_kms(
    digest: bytes,
    kms_key_version: str,
    *,
    auth_token: str | None = None,
    kms_base_url: str = "https://cloudkms.googleapis.com/v1",
    http_client: httpx.Client | None = None,
) -> str:
    """Sign a 32-byte SHA-256 digest using Cloud KMS AsymmetricSign (EC_SIGN_P256_SHA256)."""
    if len(digest) != 32:
        raise ValueError(f"Expected 32-byte SHA-256 digest, got {len(digest)} bytes")
    token = auth_token or _resolve_bearer_token(kms_base_url)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{kms_base_url.rstrip('/')}/{kms_key_version.lstrip('/')}:asymmetricSign"
    payload = {"digest": {"sha256": base64.b64encode(digest).decode("ascii")}}

    close_client = http_client is None
    client = http_client or httpx.Client(timeout=30.0)
    try:
        resp = client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise PluginSyncError(
                f"Cloud KMS asymmetricSign failed ({resp.status_code}): {resp.text}"
            )
        data = resp.json()
        sig = data.get("signature")
        if not isinstance(sig, str) or not sig:
            raise PluginSyncError(f"Cloud KMS response missing 'signature': {data}")
        return sig
    finally:
        if close_client:
            client.close()


def verify_digest_signature_with_pem(
    digest: bytes, signature_b64: str, public_key_pem: bytes | str | Path
) -> bool:
    """Verify a base64 DER ECDSA P-256 signature over a 32-byte SHA-256 digest."""
    if len(digest) != 32:
        return False
    if isinstance(public_key_pem, Path):
        pem_bytes = public_key_pem.read_bytes()
    elif isinstance(public_key_pem, str):
        possible_path = Path(public_key_pem)
        pem_bytes = (
            possible_path.read_bytes()
            if "BEGIN PUBLIC KEY" not in public_key_pem and possible_path.is_file()
            else public_key_pem.encode("utf-8")
        )
    else:
        pem_bytes = public_key_pem

    public_key = serialization.load_pem_public_key(pem_bytes)
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        return False
    try:
        signature_der = base64.b64decode(signature_b64, validate=True)
        public_key.verify(signature_der, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return True
    except (ValueError, InvalidSignature):
        return False


def verify_skill_zip_signature(
    zip_bytes: bytes, public_key_pem: bytes | str | Path
) -> tuple[bool, str]:
    """Verify an ECDSA P-256 signature embedded in SKILL.md frontmatter.metadata['kms_signature'].

    Computes the zlib-independent canonical skill payload SHA-256 digest (matching
    `middleware/app/security_verifier.py`) and verifies `kms_signature` against `public_key_pem`.
    """
    try:
        validate_skill_zip(zip_bytes)
    except SkillArchiveError as exc:
        return False, f"Invalid skill ZIP: {exc}"

    signature_b64: str | None = None
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = PurePosixPath(info.filename).parts
            if parts and parts[-1] == "SKILL.md":
                fm, _body, fm_err = parse_frontmatter(zf.read(info).decode("utf-8"))
                if fm_err or fm is None:
                    return False, f"Failed to parse SKILL.md frontmatter: {fm_err}"
                metadata = fm.get("metadata")
                if not isinstance(metadata, dict) or not isinstance(
                    metadata.get("kms_signature"), str
                ):
                    return False, "SKILL.md frontmatter.metadata missing 'kms_signature'"
                signature_b64 = metadata["kms_signature"]
                break

    if not signature_b64:
        return False, "Missing kms_signature in archive"

    digest = compute_canonical_skill_digest(zip_bytes)
    ok = verify_digest_signature_with_pem(digest, signature_b64, public_key_pem)
    if ok:
        return True, "Valid ECDSA P-256 signature"
    return False, "Signature verification failed (payload tampered or wrong public key)"


def build_skill_zip(
    skill_dir: str | Path,
    metadata_inject: dict[str, str],
    *,
    sign_pem: str | Path | None = None,
    kms_key_version: str | None = None,
    kms_auth_token: str | None = None,
    kms_base_url: str = "https://cloudkms.googleapis.com/v1",
    http_client: httpx.Client | None = None,
) -> PackagedSkill:
    """Package a single skill directory into a validated, deterministic Agent Registry ZIP.

    Injects canonical metadata keys into SKILL.md frontmatter.metadata and optionally signs
    the deterministic unsigned ZIP SHA-256 digest with an ECDSA P-256 PEM or Cloud KMS key.
    """
    skill_path = Path(skill_dir)
    if not skill_path.is_dir():
        raise SkillArchiveError(f"Skill directory not found: {skill_path}")
    if skill_path.is_symlink():
        raise SkillArchiveError(f"Skill directory must not be a symlink: {skill_path}")

    skill_name = skill_path.name
    skill_md_path = skill_path / "SKILL.md"
    if not skill_md_path.is_file():
        if (skill_path / "skill.md").is_file():
            raise SkillArchiveError(
                f"Skill '{skill_name}' contains lowercase 'skill.md'; must be uppercase 'SKILL.md'"
            )
        raise SkillArchiveError(f"Skill '{skill_name}' is missing required 'SKILL.md'")

    md_res = validate_skill_md(skill_md_path, expected_name=skill_name)
    if not md_res.valid:
        raise SkillArchiveError(f"Invalid SKILL.md in '{skill_name}': {'; '.join(md_res.errors)}")

    raw_md = skill_md_path.read_text(encoding="utf-8")
    frontmatter, body, fm_err = parse_frontmatter(raw_md)
    if fm_err or frontmatter is None:
        raise SkillArchiveError(f"Invalid SKILL.md frontmatter in '{skill_name}': {fm_err}")

    # Collect all files under skill_path, enforcing no symlinks, no extra SKILL.md, depth <= 8
    other_entries: list[tuple[str, bytes, int]] = []
    skill_md_count = 0

    for root_str, dirnames, filenames in os.walk(skill_path, followlinks=False):
        root_p = Path(root_str)
        # Filter hidden/cache dirs and reject directory symlinks
        clean_dirs: list[str] = []
        for d in sorted(dirnames):
            if d == "__pycache__" or d.startswith("."):
                continue
            d_path = root_p / d
            if d_path.is_symlink():
                raise SkillArchiveError(
                    f"Symlink directory forbidden inside skill package: {d_path.relative_to(skill_path)}"
                )
            clean_dirs.append(d)
        dirnames[:] = clean_dirs

        for fname in sorted(filenames):
            if fname.startswith(".") or fname.endswith(".pyc"):
                continue
            fpath = root_p / fname
            if fpath.is_symlink():
                raise SkillArchiveError(
                    f"Symlink file forbidden inside skill package: {fpath.relative_to(skill_path)}"
                )
            rel_inside_skill = fpath.relative_to(skill_path)
            arcname = f"{skill_name}/{rel_inside_skill.as_posix()}"
            depth = len(PurePosixPath(arcname).parts)
            if depth > MAX_ARCHIVE_DEPTH:
                raise SkillArchiveError(
                    f"File {arcname!r} exceeds maximum nesting depth {MAX_ARCHIVE_DEPTH} (depth={depth})"
                )

            if fname == "SKILL.md":
                skill_md_count += 1
                if rel_inside_skill != Path("SKILL.md"):
                    raise SkillArchiveError(
                        f"Nested 'SKILL.md' forbidden at {arcname!r}; must have strictly one "
                        f"'<skill-name>/SKILL.md'"
                    )
                continue
            if fname.lower() == "skill.md":
                raise SkillArchiveError(
                    f"Non-uppercase skill file forbidden at {arcname!r}; only '<skill-name>/SKILL.md' is allowed"
                )

            data = fpath.read_bytes()
            if len(data) > MAX_FILE_UNCOMPRESSED_BYTES:
                raise SkillArchiveError(
                    f"File {arcname!r} ({len(data)} bytes) exceeds 1 MB uncompressed file limit"
                )
            file_mode = 0o755 if os.access(fpath, os.X_OK) else 0o644
            other_entries.append((arcname, data, file_mode))

    if skill_md_count != 1:
        raise SkillArchiveError(
            f"Expected strictly 1 'SKILL.md' inside '{skill_name}', found {skill_md_count}"
        )

    # Merge canonical string metadata into frontmatter.metadata
    existing_meta = frontmatter.get("metadata") or {}
    if not isinstance(existing_meta, dict):
        raise SkillArchiveError(f"SKILL.md metadata in '{skill_name}' must be a map<string, string>")
    merged_meta: dict[str, str] = {str(k): str(v) for k, v in existing_meta.items()}
    for k, v in metadata_inject.items():
        if v is not None and str(v) != "":
            merged_meta[str(k)] = str(v)

    # Drop any stale kms_signature before computing unsigned digest
    merged_meta.pop("kms_signature", None)

    signing_enabled = bool(sign_pem or kms_key_version)
    effective_key_version = kms_key_version
    if signing_enabled and not effective_key_version:
        effective_key_version = "local-ecdsa-p256-pem"
    if signing_enabled and effective_key_version:
        merged_meta["kms_key_version"] = effective_key_version

    unsigned_fm = dict(frontmatter)
    unsigned_fm["metadata"] = dict(merged_meta)
    unsigned_md_bytes = _serialize_frontmatter_markdown(unsigned_fm, body).encode("utf-8")

    skill_md_arcname = f"{skill_name}/SKILL.md"
    unsigned_zip_bytes = _build_deterministic_zip_from_entries(
        other_entries + [(skill_md_arcname, unsigned_md_bytes, 0o644)]
    )
    # Sign the zlib-independent canonical skill payload digest so both plugin_sync and
    # Middleware (`middleware/app/security_verifier.py`) verify identically regardless of ZIP compression.
    canonical_digest = compute_canonical_skill_digest(unsigned_zip_bytes)
    unsigned_sha256_hex = canonical_digest.hex()

    kms_sig: str | None = None
    final_fm = unsigned_fm
    final_zip_bytes = unsigned_zip_bytes

    if signing_enabled:
        if sign_pem is not None:
            kms_sig = sign_digest_with_pem(canonical_digest, sign_pem)
        elif kms_key_version is not None:
            kms_sig = sign_digest_with_kms(
                canonical_digest,
                kms_key_version,
                auth_token=kms_auth_token,
                kms_base_url=kms_base_url,
                http_client=http_client,
            )
        assert kms_sig is not None
        signed_meta = dict(merged_meta)
        signed_meta["kms_signature"] = kms_sig
        final_fm = dict(frontmatter)
        final_fm["metadata"] = signed_meta
        signed_md_bytes = _serialize_frontmatter_markdown(final_fm, body).encode("utf-8")
        final_zip_bytes = _build_deterministic_zip_from_entries(
            other_entries + [(skill_md_arcname, signed_md_bytes, 0o644)]
        )

    warnings = validate_skill_zip(final_zip_bytes, expected_skill_name=skill_name)
    final_sha256_hex = compute_sha256_digest(final_zip_bytes).hex()

    display_name = skill_name.replace("-", " ").title()[:128]
    description = str(final_fm.get("description") or "")[:2048]

    return PackagedSkill(
        raw_skill_id=raw_create_skill_id(skill_name),
        private_skill_id=canonical_private_skill_id(skill_name),
        display_name=display_name,
        description=description,
        frontmatter=final_fm,
        zip_bytes=final_zip_bytes,
        sha256_hex=final_sha256_hex,
        unsigned_sha256_hex=unsigned_sha256_hex,
        kms_signature=kms_sig,
        kms_key_version=effective_key_version if signing_enabled else None,
        warnings=warnings,
    )


def fetch_mcp_tool_spec(
    mcp_url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
    http_client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Query a Streamable HTTP MCP server via JSON-RPC `tools/list` to build a TOOL_SPEC."""
    req_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    close_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout)
    try:
        resp = client.post(mcp_url, json=body, headers=req_headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
        result = data.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            return {"tools": result["tools"]}
        return None
    except Exception:
        return None
    finally:
        if close_client:
            client.close()


def _default_fallback_tool_spec(server_id: str) -> dict[str, Any]:
    """Static fallback TOOL_SPEC used when a remote MCP server cannot be probed at sync time."""
    if server_id == "finops-mcp-server":
        return {
            "tools": [
                {
                    "name": "get_billing_summary",
                    "description": "Return read-only Google Cloud Billing spend summary and SKU deltas.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "string"},
                            "days": {
                                "type": "integer",
                                "default": 7,
                                "minimum": 1,
                                "maximum": 90,
                            },
                        },
                        "required": ["project_id"],
                        "additionalProperties": False,
                    },
                    "annotations": {
                        "title": "Get Cloud Billing Summary",
                        "readOnlyHint": True,
                        "destructiveHint": False,
                        "idempotentHint": True,
                        "openWorldHint": False,
                    },
                },
                {
                    "name": "analyze_bq_query",
                    "description": "Inspect BigQuery SQL for full-table scans and missing partition filters.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "sql": {"type": "string"},
                            "project_id": {"type": "string"},
                        },
                        "required": ["sql"],
                        "additionalProperties": False,
                    },
                    "annotations": {
                        "title": "Analyze BigQuery SQL Query",
                        "readOnlyHint": True,
                        "destructiveHint": False,
                        "idempotentHint": True,
                        "openWorldHint": False,
                    },
                },
                {
                    "name": "delete_idle_dataset",
                    "description": "Permanently delete an idle BigQuery dataset (destructive).",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "string"},
                            "dataset_id": {"type": "string"},
                            "confirm_delete": {"type": "boolean", "default": False},
                            "reason": {"type": "string"},
                        },
                        "required": ["project_id", "dataset_id"],
                        "additionalProperties": False,
                    },
                    "annotations": {
                        "title": "Delete Idle BigQuery Dataset",
                        "readOnlyHint": False,
                        "destructiveHint": True,
                        "idempotentHint": False,
                        "openWorldHint": False,
                    },
                },
            ]
        }
    return {"tools": []}


def _validate_a2a_agent_card(card: dict[str, Any], source_label: str) -> None:
    """Validate required A2A v1.0 Agent Card fields and 10 KB Agent Registry limit."""
    encoded = json.dumps(card, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_SERVICE_SPEC_BYTES:
        raise PluginSyncError(
            f"A2A agent card {source_label} ({len(encoded)} bytes) exceeds 10 KB Agent Registry limit"
        )
    for req_field in (
        "name",
        "supportedInterfaces",
        "capabilities",
        "defaultInputModes",
        "defaultOutputModes",
        "skills",
    ):
        if req_field not in card:
            raise PluginSyncError(f"A2A agent card {source_label} missing required field {req_field!r}")
    if not isinstance(card["supportedInterfaces"], list) or not card["supportedInterfaces"]:
        raise PluginSyncError(
            f"A2A agent card {source_label} 'supportedInterfaces' must be a non-empty list"
        )
    for idx, iface in enumerate(card["supportedInterfaces"]):
        if not isinstance(iface, dict) or not isinstance(iface.get("url"), str) or not iface["url"].strip():
            raise PluginSyncError(
                f"A2A agent card {source_label} supportedInterfaces[{idx}].url must be non-empty"
            )


def _detect_git_commit(plugin_dir: Path) -> str:
    """Detect current git commit SHA if inside a git repository, else 'unknown'."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(plugin_dir), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _resolve_bearer_token(url: str) -> str | None:
    """Resolve Bearer token from env var or gcloud ADC for googleapis.com endpoints."""
    env_token = os.environ.get("AGENT_REGISTRY_AUTH_TOKEN") or os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    if env_token:
        return env_token.strip()
    if "googleapis.com" not in url:
        return None
    try:
        proc = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def build_sync_plan(
    plugin_dir: str | Path,
    *,
    project: str,
    location: str,
    target_state: str = "TARGET_STATE_ACTIVE",
    scanning_policy: str = "ENABLED_BLOCKING",
    source_repo: str | None = None,
    git_commit: str | None = None,
    sign_pem: str | Path | None = None,
    kms_key_version: str | None = None,
    kms_auth_token: str | None = None,
    kms_base_url: str = "https://cloudkms.googleapis.com/v1",
    probe_mcp: bool = True,
    fallback_tool_specs: dict[str, dict[str, Any]] | None = None,
    http_client: httpx.Client | None = None,
) -> SyncPlan:
    """Decompose an Agent Plugins 1.0 plugin directory into validated Agent Registry artifacts."""
    if target_state not in VALID_TARGET_STATES:
        raise PluginSyncError(
            f"Invalid target_state {target_state!r}; expected one of {sorted(VALID_TARGET_STATES)}"
        )
    if scanning_policy not in VALID_SCANNING_POLICIES:
        raise PluginSyncError(
            f"Invalid scanning_policy {scanning_policy!r}; expected one of "
            f"{sorted(VALID_SCANNING_POLICIES)}"
        )

    plugin_root = Path(plugin_dir)
    bundle_res = validate_agent_plugin_bundle(plugin_root)
    if not bundle_res.valid:
        raise PluginSyncError(
            f"Plugin bundle failed Agent Plugins 1.0 validation: {'; '.join(bundle_res.errors)}"
        )

    manifest = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
    plugin_name = manifest["name"]
    plugin_version = str(manifest.get("version") or "1.0.0")
    extensions = manifest.get("extensions") or {}
    acme_ext = extensions.get("com.acme.agentregistry") if isinstance(extensions, dict) else {}
    if not isinstance(acme_ext, dict):
        acme_ext = {}

    ag_manifest_path = plugin_root / ".antigravity-plugin" / "plugin.json"
    ag_display_name: str | None = None
    if ag_manifest_path.is_file():
        ag_data = json.loads(ag_manifest_path.read_text(encoding="utf-8"))
        if isinstance(ag_data.get("displayName"), str) and ag_data["displayName"].strip():
            ag_display_name = ag_data["displayName"].strip()

    plugin_display_name = (
        ag_display_name
        or (acme_ext.get("displayName") if isinstance(acme_ext.get("displayName"), str) else None)
        or plugin_name.replace("-", " ").title()
    )
    resolved_source_repo = source_repo or str(manifest.get("repository") or "")
    resolved_git_commit = git_commit or _detect_git_commit(plugin_root)

    plan = SyncPlan(
        plugin_name=plugin_name,
        plugin_display_name=plugin_display_name,
        plugin_version=plugin_version,
        source_repo=resolved_source_repo,
        git_commit=resolved_git_commit,
        project=project,
        location=location,
        target_state=target_state,
        scanning_policy=scanning_policy,
    )

    # 1. Decompose MCP Servers from mcp.json (Agent Plugins 1.0)
    mcp_json_path = plugin_root / "mcp.json"
    if mcp_json_path.is_file():
        mcp_doc = json.loads(mcp_json_path.read_text(encoding="utf-8"))
        servers = mcp_doc.get("mcpServers") or {}
        for server_name, cfg in sorted(servers.items()):
            if not isinstance(cfg, dict):
                continue
            srv_type = cfg.get("type")
            if srv_type not in ("streamable-http", "sse"):
                plan.warnings.append(
                    f"Skipping local stdio MCP server '{server_name}' (Agent Registry Service "
                    f"registration applies to remote HTTP/gRPC endpoints)."
                )
                continue
            url = cfg.get("url")
            if not isinstance(url, str) or not url.strip():
                continue

            service_id = sanitize_resource_id(server_name)
            tool_spec: dict[str, Any] | None = None
            if probe_mcp:
                tool_spec = fetch_mcp_tool_spec(
                    url,
                    headers=cfg.get("headers") if isinstance(cfg.get("headers"), dict) else None,
                    http_client=http_client,
                )
            if tool_spec is None and fallback_tool_specs and service_id in fallback_tool_specs:
                tool_spec = fallback_tool_specs[service_id]
            if tool_spec is None:
                static_spec_path = plugin_root / "mcp-tool-spec.json"
                if static_spec_path.is_file():
                    tool_spec = json.loads(static_spec_path.read_text(encoding="utf-8"))
            if tool_spec is None:
                tool_spec = _default_fallback_tool_spec(service_id)
                plan.warnings.append(
                    f"Could not probe live MCP tools/list at {url}; using fallback static TOOL_SPEC "
                    f"for '{service_id}'."
                )

            spec_bytes = json.dumps(tool_spec, separators=(",", ":")).encode("utf-8")
            if len(spec_bytes) > MAX_SERVICE_SPEC_BYTES:
                raise PluginSyncError(
                    f"MCP TOOL_SPEC for '{service_id}' ({len(spec_bytes)} bytes) exceeds 10 KB limit"
                )

            protocol_binding = "JSONRPC" if srv_type == "streamable-http" else "HTTP_JSON"
            service_payload: dict[str, Any] = {
                "displayName": f"{plugin_display_name} MCP ({service_id})"[:63],
                "description": (
                    f"MCP server '{service_id}' from plugin '{plugin_name}' v{plugin_version}."
                )[:2048],
                "interfaces": [
                    {
                        "url": url,
                        "protocolBinding": protocol_binding,
                    }
                ],
                "mcpServerSpec": {
                    "type": "TOOL_SPEC",
                    "content": tool_spec,
                },
            }
            plan.mcp_services.append(
                PackagedService(service_id=service_id, kind="mcp", payload=service_payload)
            )

    # 2. Decompose A2A Agents from agents/*/agent-card.json
    agents_dir = plugin_root / "agents"
    if agents_dir.is_dir():
        card_files = sorted(agents_dir.glob("*/agent-card.json"))
        for card_path in card_files:
            card = json.loads(card_path.read_text(encoding="utf-8"))
            _validate_a2a_agent_card(card, str(card_path.relative_to(plugin_root)))

            folder_slug = sanitize_resource_id(card_path.parent.name)
            ext_agent_id = acme_ext.get("agentServiceId")
            if len(card_files) == 1 and isinstance(ext_agent_id, str) and ext_agent_id.strip():
                agent_service_id = sanitize_resource_id(ext_agent_id)
            else:
                agent_service_id = (
                    folder_slug if folder_slug.endswith("-agent") else f"{folder_slug}-agent"
                )

            # CRITICAL Agent Registry v1 rule: top-level `interfaces` MUST be empty for A2A_AGENT_CARD!
            agent_payload: dict[str, Any] = {
                "displayName": str(card.get("name") or agent_service_id)[:63],
                "description": str(
                    card.get("description")
                    or f"A2A agent '{agent_service_id}' from plugin '{plugin_name}'."
                )[:2048],
                "interfaces": [],
                "agentSpec": {
                    "type": "A2A_AGENT_CARD",
                    "content": card,
                },
            }
            plan.agent_services.append(
                PackagedService(service_id=agent_service_id, kind="a2a", payload=agent_payload)
            )

    # 3. Decompose Skills from skills/<skill-name>/
    primary_mcp_id = (
        plan.mcp_services[0].service_id
        if plan.mcp_services
        else str(acme_ext.get("mcpServiceId") or "")
    )
    primary_agent_id = (
        plan.agent_services[0].service_id
        if plan.agent_services
        else str(acme_ext.get("agentServiceId") or "")
    )

    canonical_metadata: dict[str, str] = {
        "plugin_name": plugin_name,
        "plugin_display_name": plugin_display_name,
        "plugin_version": plugin_version,
        "source_repo": resolved_source_repo,
        "git_commit": resolved_git_commit,
        "mcp_service_id": primary_mcp_id,
        "agent_service_id": primary_agent_id,
    }

    skills_dir = plugin_root / "skills"
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            if entry.name.startswith(".") or entry.name == "__pycache__" or not entry.is_dir():
                continue
            packaged = build_skill_zip(
                entry,
                canonical_metadata,
                sign_pem=sign_pem,
                kms_key_version=kms_key_version,
                kms_auth_token=kms_auth_token,
                kms_base_url=kms_base_url,
                http_client=http_client,
            )
            plan.skills.append(packaged)
            plan.warnings.extend(packaged.warnings)

    return plan


def poll_operation(
    client: httpx.Client,
    registry_url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout_seconds: float = 120.0,
    poll_interval: float = 0.2,
) -> dict[str, Any]:
    """Poll a Google Cloud Agent Registry Long-Running Operation until done."""
    if not isinstance(body, dict):
        return {}
    is_lro = "done" in body or (
        isinstance(body.get("name"), str) and "/operations/" in body["name"]
    )
    if not is_lro:
        return body

    op = body
    deadline = time.monotonic() + timeout_seconds
    base = registry_url.rstrip("/")

    while not op.get("done", False):
        if time.monotonic() >= deadline:
            raise RegistryApiError(
                f"Timed out after {timeout_seconds}s waiting for LRO {op.get('name')}"
            )
        op_name = op.get("name")
        if not isinstance(op_name, str) or not op_name:
            raise RegistryApiError(f"Incomplete LRO payload missing 'name': {op}")
        time.sleep(poll_interval)
        resp = client.get(f"{base}/{op_name.lstrip('/')}", headers=headers)
        if resp.status_code >= 400:
            raise RegistryApiError(
                f"Failed polling LRO {op_name} ({resp.status_code}): {resp.text}"
            )
        op = resp.json()

    if op.get("error"):
        raise RegistryApiError(f"LRO {op.get('name')} failed: {op['error']}")
    response = op.get("response")
    return response if isinstance(response, dict) else op


def _build_skill_create_payload(
    skill: PackagedSkill, *, target_state: str, scanning_policy: str
) -> dict[str, Any]:
    return {
        "displayName": skill.display_name,
        "description": skill.description,
        "type": "SIMPLE",
        "targetState": target_state,
        "scanningConfig": {"policy": scanning_policy},
        "initialRevision": {
            "archiveUploadSource": {
                "archiveContent": base64.b64encode(skill.zip_bytes).decode("ascii"),
            }
        },
    }


def _build_revision_create_payload(skill: PackagedSkill) -> dict[str, Any]:
    return {
        "archiveUploadSource": {
            "archiveContent": base64.b64encode(skill.zip_bytes).decode("ascii"),
        }
    }


def _compute_revision_id(plugin_version: str, zip_sha256_hex: str) -> str:
    ver_slug = sanitize_resource_id(plugin_version, max_len=24)
    return sanitize_resource_id(f"v-{ver_slug}-{zip_sha256_hex[:8]}", max_len=63)


def apply_sync_plan(
    plan: SyncPlan,
    *,
    registry_url: str,
    auth_token: str | None = None,
    dry_run: bool = False,
    output_dir: str | Path | None = None,
    http_client: httpx.Client | None = None,
    poll_interval: float = 0.2,
) -> dict[str, Any]:
    """Execute (or dry-run) a SyncPlan against Google Cloud Agent Registry v1 REST API."""
    base = registry_url.rstrip("/")
    parent = f"projects/{plan.project}/locations/{plan.location}"

    if output_dir is not None:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        for skill in plan.skills:
            (out_path / f"{skill.raw_skill_id}.zip").write_bytes(skill.zip_bytes)

    summary: dict[str, Any] = {
        "plugin_name": plan.plugin_name,
        "plugin_version": plan.plugin_version,
        "project": plan.project,
        "location": plan.location,
        "registry_url": base,
        "dry_run": dry_run,
        "target_state": plan.target_state,
        "scanning_policy": plan.scanning_policy,
        "skills": [],
        "mcp_services": [],
        "agent_services": [],
        "warnings": list(plan.warnings),
    }

    if dry_run:
        for skill in plan.skills:
            summary["skills"].append(
                {
                    "action": "dry_run",
                    "raw_skill_id": skill.raw_skill_id,
                    "private_skill_id": skill.private_skill_id,
                    "resource_name": f"{parent}/skills/{skill.private_skill_id}",
                    "zip_bytes": len(skill.zip_bytes),
                    "sha256_hex": skill.sha256_hex,
                    "unsigned_sha256_hex": skill.unsigned_sha256_hex,
                    "signed": skill.kms_signature is not None,
                    "kms_key_version": skill.kms_key_version,
                    "frontmatter_metadata": skill.frontmatter.get("metadata", {}),
                }
            )
        for svc in plan.mcp_services:
            summary["mcp_services"].append(
                {
                    "action": "dry_run",
                    "service_id": svc.service_id,
                    "resource_name": f"{parent}/services/{svc.service_id}",
                    "payload": svc.payload,
                }
            )
        for svc in plan.agent_services:
            summary["agent_services"].append(
                {
                    "action": "dry_run",
                    "service_id": svc.service_id,
                    "resource_name": f"{parent}/services/{svc.service_id}",
                    "payload": svc.payload,
                }
            )
        if output_dir is not None:
            (Path(output_dir) / "sync-plan.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
        return summary

    token = auth_token or _resolve_bearer_token(base)
    headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    close_client = http_client is None
    client = http_client or httpx.Client(timeout=60.0)
    try:
        # 1. Sync Skills (handling private- prefixing on GET/POST revisions/PATCH)
        for skill in plan.skills:
            skill_resource_path = f"{parent}/skills/{skill.private_skill_id}"
            get_resp = client.get(f"{base}/{skill_resource_path}", headers=headers)

            if get_resp.status_code == 404:
                create_url = f"{base}/{parent}/skills"
                create_payload = _build_skill_create_payload(
                    skill,
                    target_state=plan.target_state,
                    scanning_policy=plan.scanning_policy,
                )
                post_resp = client.post(
                    create_url,
                    params={"skillId": skill.raw_skill_id},
                    json=create_payload,
                    headers=headers,
                )
                if post_resp.status_code == 409:
                    # Skill already created concurrently; fall through to revision update
                    get_resp = client.get(f"{base}/{skill_resource_path}", headers=headers)
                elif post_resp.status_code >= 400:
                    raise RegistryApiError(
                        f"Failed creating skill '{skill.raw_skill_id}' ({post_resp.status_code}): "
                        f"{post_resp.text}"
                    )
                else:
                    created = poll_operation(
                        client, base, post_resp.json(), headers=headers, poll_interval=poll_interval
                    )
                    summary["skills"].append(
                        {
                            "action": "created",
                            "raw_skill_id": skill.raw_skill_id,
                            "private_skill_id": skill.private_skill_id,
                            "resource_name": created.get("name", skill_resource_path),
                            "sha256_hex": skill.sha256_hex,
                            "signed": skill.kms_signature is not None,
                            "response": created,
                        }
                    )
                    continue

            if get_resp.status_code >= 400:
                raise RegistryApiError(
                    f"Failed fetching skill '{skill.private_skill_id}' ({get_resp.status_code}): "
                    f"{get_resp.text}"
                )

            # Existing skill: upload new immutable SkillRevision under private-<id>, then PATCH defaultRevision
            rev_id = _compute_revision_id(plan.plugin_version, skill.sha256_hex)
            rev_url = f"{base}/{skill_resource_path}/revisions"
            rev_resp = client.post(
                rev_url,
                params={"skillRevisionId": rev_id},
                json=_build_revision_create_payload(skill),
                headers=headers,
            )
            revision_name = f"{skill_resource_path}/revisions/{rev_id}"
            if rev_resp.status_code == 409:
                rev_result: dict[str, Any] = {"name": revision_name, "alreadyExists": True}
            elif rev_resp.status_code >= 400:
                raise RegistryApiError(
                    f"Failed creating revision '{rev_id}' for '{skill.private_skill_id}' "
                    f"({rev_resp.status_code}): {rev_resp.text}"
                )
            else:
                rev_result = poll_operation(
                    client, base, rev_resp.json(), headers=headers, poll_interval=poll_interval
                )
                revision_name = rev_result.get("name", revision_name)

            patch_payload = {
                "displayName": skill.display_name,
                "description": skill.description,
                "defaultRevision": revision_name,
                "targetState": plan.target_state,
                "scanningConfig": {"policy": plan.scanning_policy},
            }
            patch_resp = client.patch(
                f"{base}/{skill_resource_path}",
                params={
                    "updateMask": "displayName,description,defaultRevision,targetState,scanningConfig"
                },
                json=patch_payload,
                headers=headers,
            )
            if patch_resp.status_code >= 400:
                raise RegistryApiError(
                    f"Failed updating skill '{skill.private_skill_id}' ({patch_resp.status_code}): "
                    f"{patch_resp.text}"
                )
            updated = poll_operation(
                client, base, patch_resp.json(), headers=headers, poll_interval=poll_interval
            )
            summary["skills"].append(
                {
                    "action": "updated",
                    "raw_skill_id": skill.raw_skill_id,
                    "private_skill_id": skill.private_skill_id,
                    "resource_name": updated.get("name", skill_resource_path),
                    "revision_name": revision_name,
                    "sha256_hex": skill.sha256_hex,
                    "signed": skill.kms_signature is not None,
                    "response": updated,
                }
            )

        # 2. Sync MCP Services & A2A Agent Services
        for svc, target_list, update_mask in [
            *(
                (
                    s,
                    summary["mcp_services"],
                    "displayName,description,interfaces,mcpServerSpec.content",
                )
                for s in plan.mcp_services
            ),
            *(
                (
                    s,
                    summary["agent_services"],
                    "displayName,description,agentSpec.content",
                )
                for s in plan.agent_services
            ),
        ]:
            svc_path = f"{parent}/services/{svc.service_id}"
            get_resp = client.get(f"{base}/{svc_path}", headers=headers)

            if get_resp.status_code == 404:
                post_resp = client.post(
                    f"{base}/{parent}/services",
                    params={"serviceId": svc.service_id},
                    json=svc.payload,
                    headers=headers,
                )
                if post_resp.status_code != 409:
                    if post_resp.status_code >= 400:
                        raise RegistryApiError(
                            f"Failed creating {svc.kind} service '{svc.service_id}' "
                            f"({post_resp.status_code}): {post_resp.text}"
                        )
                    created_svc = poll_operation(
                        client, base, post_resp.json(), headers=headers, poll_interval=poll_interval
                    )
                    target_list.append(
                        {
                            "action": "created",
                            "service_id": svc.service_id,
                            "resource_name": created_svc.get("name", svc_path),
                            "response": created_svc,
                        }
                    )
                    continue
            elif get_resp.status_code >= 400:
                raise RegistryApiError(
                    f"Failed fetching service '{svc.service_id}' ({get_resp.status_code}): "
                    f"{get_resp.text}"
                )

            patch_resp = client.patch(
                f"{base}/{svc_path}",
                params={"updateMask": update_mask},
                json=svc.payload,
                headers=headers,
            )
            if patch_resp.status_code >= 400:
                raise RegistryApiError(
                    f"Failed updating {svc.kind} service '{svc.service_id}' "
                    f"({patch_resp.status_code}): {patch_resp.text}"
                )
            updated_svc = poll_operation(
                client, base, patch_resp.json(), headers=headers, poll_interval=poll_interval
            )
            target_list.append(
                {
                    "action": "updated",
                    "service_id": svc.service_id,
                    "resource_name": updated_svc.get("name", svc_path),
                    "response": updated_svc,
                }
            )
    finally:
        if close_client:
            client.close()

    if output_dir is not None:
        (Path(output_dir) / "sync-plan.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
    return summary
