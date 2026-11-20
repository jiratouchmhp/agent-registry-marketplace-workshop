"""Async REST consumer for Google Cloud Agent Registry (`v1`) and local mock-registry.

Implements the platform-agent discovery and skill-consumption contract against
``agentregistry.googleapis.com/v1`` (or ``AGENT_REGISTRY_BASE_URL`` for local
workshop runs):

- Semantic and keyword Skill discovery via ``POST .../skills:search`` filtered
  to approved, zero-finding skills (``state=ACTIVE AND defaultRevisionFindingCount=0``)
- Two-step Skill retrieval (``GetSkill`` -> ``GetSkillRevision`` metadata ->
  ``GetSkillRevision?alt=media``) following HTTP 302 redirects to short-lived
  GCS signed URLs while stripping ``x-goog-*`` and ``Authorization`` headers on
  cross-origin hops
- Server-computed ``sha256Hash`` integrity verification, safe ``agentskills.io``
  ZIP extraction, and optional Cloud KMS ECDSA P-256 (``EC_SIGN_P256_SHA256``)
  cryptographic signature verification
- MCP Server discovery (``POST .../mcpServers:search``, ``GET .../mcpServers``)
- A2A Agent discovery (``POST .../agents:search``, ``GET .../agents``)
- Auth Manager binding resolution (``GET .../bindings:fetchAvailable``)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import posixpath
import re
import stat
import subprocess
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote

import httpx
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

DEFAULT_REGISTRY_BASE_URL = "https://agentregistry.googleapis.com/v1"
DEFAULT_PROJECT_ID = "acme-agent-governance"
DEFAULT_LOCATION = "us-central1"
PRIVATE_SKILL_PREFIX = "private-"
APPROVED_SKILL_FILTER = "state=ACTIVE AND defaultRevisionFindingCount=0"
GCE_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
TOKEN_REFRESH_SKEW_SECONDS = 60.0

# Official Agent Registry v1 / agentskills.io archive constraints
MAX_COMPRESSED_ZIP_BYTES = 1 * 1024 * 1024  # 1 MiB
MAX_UNCOMPRESSED_TOTAL_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_UNCOMPRESSED_FILE_BYTES = 1 * 1024 * 1024  # 1 MiB
MAX_ARCHIVE_NESTING_DEPTH = 8

SERVABLE_SKILL_STATES = frozenset({"STATE_ACTIVE", "STATE_DEPRECATED", "ACTIVE", "DEPRECATED"})
ALLOWED_BLOCKING_SCAN_STATES = frozenset(
    {"SUCCEEDED", "DISABLED", "SCAN_STATE_SUCCEEDED", "SCAN_STATE_DISABLED"}
)
DEFAULT_MAX_MEDIA_REDIRECTS = 5
_SAFE_SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SIGNATURE_METADATA_KEYS = frozenset({"kms_signature", "kms_key_version"})


class SkillIntegrityError(ValueError):
    """Raised when a downloaded skill archive fails SHA-256 or archive safety checks."""


class SkillGovernanceError(RuntimeError):
    """Raised when a skill is quarantined, blocked by scan findings, or not servable."""


class SkillSignatureError(ValueError):
    """Raised when Cloud KMS ECDSA P-256 signature verification fails or is missing."""


@dataclass(frozen=True)
class ExtractedSkill:
    """Verified, extracted Agent Registry standalone Skill revision."""

    resource_name: str
    skill_id: str
    revision_name: str
    state: str
    sha256_hash: str
    size_bytes: int
    frontmatter: dict[str, Any]
    instructions: str
    skill_md_path: str
    references: dict[str, str] = field(default_factory=dict)
    assets: dict[str, bytes] = field(default_factory=dict)
    scripts: dict[str, str] = field(default_factory=dict)
    kms_verified: bool = False
    kms_key_version: str | None = None

    @property
    def name(self) -> str:
        return str(self.frontmatter.get("name", ""))

    @property
    def description(self) -> str:
        return str(self.frontmatter.get("description", ""))

    @property
    def metadata(self) -> dict[str, str]:
        raw = self.frontmatter.get("metadata")
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        return {}

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "resource_name": self.resource_name,
            "skill_id": self.skill_id,
            "revision_name": self.revision_name,
            "state": self.state,
            "name": self.name,
            "description": self.description,
            "sha256_hash": self.sha256_hash,
            "size_bytes": self.size_bytes,
            "skill_md_path": self.skill_md_path,
            "references": sorted(self.references.keys()),
            "assets": sorted(self.assets.keys()),
            "scripts": sorted(self.scripts.keys()),
            "metadata": self.metadata,
            "kms_verified": self.kms_verified,
            "kms_key_version": self.kms_key_version,
        }


@dataclass
class DiscoveredCapabilities:
    """Aggregated Agent Registry discovery snapshot for a platform agent."""

    project_id: str
    location: str
    base_url: str
    skills: list[dict[str, Any]] = field(default_factory=list)
    extracted_skills: list[ExtractedSkill] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    agents: list[dict[str, Any]] = field(default_factory=list)
    resolved_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "location": self.location,
            "base_url": self.base_url,
            "skills": self.skills,
            "extracted_skills": [s.to_summary_dict() for s in self.extracted_skills],
            "mcp_servers": self.mcp_servers,
            "agents": self.agents,
            "resolved_bindings": self.resolved_bindings,
        }


def _url_origin(url: httpx.URL) -> tuple[str, str, int | None]:
    """Return normalized ``(scheme, host, effective_port)`` for origin comparison."""
    scheme = url.scheme.lower()
    host = (url.host or "").lower()
    port = url.port
    if port is None:
        port = 443 if scheme == "https" else (80 if scheme == "http" else None)
    return (scheme, host, port)


def _normalize_sha256(value: str) -> str:
    cleaned = value.strip().lower()
    if cleaned.startswith("sha256:"):
        cleaned = cleaned[len("sha256:") :]
    return cleaned


def _parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter and markdown body from ``SKILL.md``."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillIntegrityError("SKILL.md must begin with YAML frontmatter delimited by '---'")
    closing_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            closing_idx = idx
            break
    if closing_idx is None:
        raise SkillIntegrityError("SKILL.md YAML frontmatter is missing closing '---'")

    yaml_block = "\n".join(lines[1:closing_idx])
    body = "\n".join(lines[closing_idx + 1 :]).lstrip("\n")
    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        raise SkillIntegrityError(f"Invalid SKILL.md YAML frontmatter: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SkillIntegrityError("SKILL.md frontmatter must be a YAML mapping")

    name = parsed.get("name")
    description = parsed.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillIntegrityError("SKILL.md frontmatter missing required 'name'")
    if not isinstance(description, str) or not description.strip():
        raise SkillIntegrityError("SKILL.md frontmatter missing required 'description'")

    return parsed, body


def _canonicalize_frontmatter_for_signing(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Return frontmatter copy with signature metadata keys removed."""
    cleaned = dict(frontmatter)
    raw_meta = cleaned.get("metadata")
    if isinstance(raw_meta, dict):
        filtered_meta = {
            str(k): str(v)
            for k, v in raw_meta.items()
            if str(k) not in _SIGNATURE_METADATA_KEYS
        }
        cleaned["metadata"] = filtered_meta
    return cleaned


def compute_canonical_skill_digest(
    frontmatter: dict[str, Any],
    instructions: str,
    *,
    references: dict[str, str] | None = None,
    assets: dict[str, bytes] | None = None,
    scripts: dict[str, str] | None = None,
) -> bytes:
    """Compute a deterministic SHA-256 digest of unsigned skill contents.

    This supports in-frontmatter ``kms_signature`` attestation where CI signs
    all skill content excluding ``kms_signature`` and ``kms_key_version``.
    """
    hasher = hashlib.sha256()

    def _feed(tag: str, value: str | bytes) -> None:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        tag_bytes = tag.encode("utf-8")
        hasher.update(len(tag_bytes).to_bytes(4, "big"))
        hasher.update(tag_bytes)
        hasher.update(len(raw).to_bytes(8, "big"))
        hasher.update(raw)

    canonical_fm = _canonicalize_frontmatter_for_signing(frontmatter)
    _feed("frontmatter", json.dumps(canonical_fm, sort_keys=True, separators=(",", ":")))
    _feed("instructions", instructions.replace("\r\n", "\n").strip())

    for rel_path, content in sorted((references or {}).items()):
        _feed(f"references/{rel_path}", content)
    for rel_path, content in sorted((assets or {}).items()):
        _feed(f"assets/{rel_path}", content)
    for rel_path, content in sorted((scripts or {}).items()):
        _feed(f"scripts/{rel_path}", content)

    return hasher.digest()


def _canonicalize_skill_md_v1(raw_text: str) -> bytes:
    """Canonicalize SKILL.md stripping kms_signature/kms_key_version for v1 ZIP attestation.

    Byte-for-byte compatible with ``tools/plugin_sync/sync.py``,
    ``middleware/app/security_verifier.py``, and ``security-governance/kms/sign_and_verify.py``.
    """
    frontmatter, body = _parse_skill_md(raw_text)
    cleaned_fm = dict(frontmatter)
    metadata = cleaned_fm.get("metadata")
    if isinstance(metadata, dict):
        cleaned_meta = {
            k: v for k, v in metadata.items() if k not in _SIGNATURE_METADATA_KEYS
        }
        if cleaned_meta:
            cleaned_fm["metadata"] = cleaned_meta
        else:
            cleaned_fm.pop("metadata", None)

    canonical_fm = json.dumps(cleaned_fm, sort_keys=True, separators=(",", ":"))
    normalized_body = body.replace("\r\n", "\n").strip()
    return f"{canonical_fm}\n---\n{normalized_body}\n".encode("utf-8")


def compute_canonical_skill_zip_digest(zip_bytes: bytes) -> bytes:
    """Compute the 32-byte SHA-256 digest of the canonical ``agent-registry-skill-v1`` ZIP payload.

    Matches ``tools/plugin_sync/sync.py::compute_canonical_skill_digest``,
    ``middleware/app/security_verifier.py::compute_canonical_skill_digest``, and
    ``security-governance/kms/sign_and_verify.py::compute_canonical_skill_digest`` byte-for-byte.
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
                payload = _canonicalize_skill_md_v1(raw.decode("utf-8"))
            else:
                payload = raw
            chunks.append(rel_name.encode("utf-8"))
            chunks.append(b"\x00")
            chunks.append(str(len(payload)).encode("ascii"))
            chunks.append(b"\x00")
            chunks.append(payload)
            chunks.append(b"\x00")
    return hashlib.sha256(b"".join(chunks)).digest()


def verify_kms_ecdsa_p256_signature(
    public_key_pem: bytes | str,
    signature_b64: str,
    payload: bytes,
    *,
    prehashed: bool = False,
) -> bool:
    """Verify an ASN.1 DER ECDSA P-256 SHA-256 signature produced by Cloud KMS.

    Matches Cloud KMS ``EC_SIGN_P256_SHA256`` asymmetric signing keys:
    - When ``prehashed=False`` (default), ``payload`` is raw bytes hashed with SHA-256.
    - When ``prehashed=True``, ``payload`` must be a raw 32-byte SHA-256 digest
      (matching ``AsymmetricSign(digest={"sha256": ...})``).
    """
    pem_bytes = (
        public_key_pem.encode("utf-8")
        if isinstance(public_key_pem, str)
        else public_key_pem
    )
    try:
        public_key = serialization.load_pem_public_key(pem_bytes)
    except Exception as exc:
        raise SkillSignatureError(f"Failed to parse KMS PEM public key: {exc}") from exc

    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise SkillSignatureError(
            "KMS public key must be an EC P-256 (SECP256R1) key for EC_SIGN_P256_SHA256"
        )

    try:
        sig_bytes = base64.b64decode(signature_b64.strip(), validate=True)
    except Exception as exc:
        raise SkillSignatureError(f"Invalid base64 kms_signature: {exc}") from exc

    algorithm: ec.ECDSA = (
        ec.ECDSA(utils.Prehashed(hashes.SHA256()))
        if prehashed
        else ec.ECDSA(hashes.SHA256())
    )
    try:
        public_key.verify(sig_bytes, payload, algorithm)
        return True
    except InvalidSignature:
        return False


def _validate_zip_member_path(raw_name: str) -> str:
    """Validate an archive path against traversal, absolute paths, and max nesting."""
    if not raw_name or "\x00" in raw_name:
        raise SkillIntegrityError(f"Invalid archive member path: {raw_name!r}")
    normalized_slashes = raw_name.replace("\\", "/")
    if normalized_slashes.startswith("/") or re.match(r"^[A-Za-z]:/", normalized_slashes):
        raise SkillIntegrityError(f"Absolute path forbidden in skill archive: {raw_name!r}")
    parts = [part for part in normalized_slashes.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise SkillIntegrityError(f"Path traversal forbidden in skill archive: {raw_name!r}")
    if len(parts) > MAX_ARCHIVE_NESTING_DEPTH:
        raise SkillIntegrityError(
            f"Archive member exceeds max nesting depth ({MAX_ARCHIVE_NESTING_DEPTH}): {raw_name!r}"
        )
    return posixpath.join(*parts) if parts else ""


def extract_skill_zip(zip_bytes: bytes) -> dict[str, Any]:
    """Safely validate and extract an ``agentskills.io`` Skill ZIP archive.

    Enforces official Agent Registry v1 archive guardrails:
    - Compressed archive <= 1 MiB, uncompressed total <= 10 MiB, per file <= 1 MiB
    - Max nesting depth 8, no symlinks, no ``..`` traversal, no absolute paths
    - Strictly ONE uppercase ``SKILL.md`` at root (``SKILL.md``) or inside a
      single top-level folder (``<dir>/SKILL.md``)
    """
    if len(zip_bytes) > MAX_COMPRESSED_ZIP_BYTES:
        raise SkillIntegrityError(
            f"Compressed skill ZIP ({len(zip_bytes)} bytes) exceeds "
            f"{MAX_COMPRESSED_ZIP_BYTES} bytes limit"
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r")
    except zipfile.BadZipFile as exc:
        raise SkillIntegrityError(f"Skill payload is not a valid ZIP archive: {exc}") from exc

    files: dict[str, bytes] = {}
    total_uncompressed = 0

    with archive:
        for info in archive.infolist():
            if info.is_dir():
                _validate_zip_member_path(info.filename)
                continue

            # Detect POSIX symlinks stored in external_attr upper 16 bits
            unix_mode = info.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise SkillIntegrityError(
                    f"Symlink entries are forbidden in skill archives: {info.filename!r}"
                )

            rel_path = _validate_zip_member_path(info.filename)
            if not rel_path:
                continue

            if info.file_size > MAX_UNCOMPRESSED_FILE_BYTES:
                raise SkillIntegrityError(
                    f"Archive member {rel_path!r} ({info.file_size} bytes) exceeds "
                    f"{MAX_UNCOMPRESSED_FILE_BYTES} bytes per-file limit"
                )

            try:
                with archive.open(info, mode="r") as member_fh:
                    content = member_fh.read(MAX_UNCOMPRESSED_FILE_BYTES + 1)
            except (zipfile.BadZipFile, zlib.error) as exc:
                raise SkillIntegrityError(
                    f"Corrupted or spoofed ZIP member {rel_path!r}: {exc}"
                ) from exc
            if len(content) > MAX_UNCOMPRESSED_FILE_BYTES:
                raise SkillIntegrityError(
                    f"Decompressed member {rel_path!r} exceeds "
                    f"{MAX_UNCOMPRESSED_FILE_BYTES} bytes per-file limit"
                )
            total_uncompressed += len(content)
            if total_uncompressed > MAX_UNCOMPRESSED_TOTAL_BYTES:
                raise SkillIntegrityError(
                    f"Total uncompressed archive size exceeds {MAX_UNCOMPRESSED_TOTAL_BYTES} bytes"
                )

            if rel_path in files:
                raise SkillIntegrityError(f"Duplicate archive entry: {rel_path!r}")
            files[rel_path] = content

    skill_md_candidates = [
        path for path in files if posixpath.basename(path) == "SKILL.md"
    ]
    if len(skill_md_candidates) != 1:
        raise SkillIntegrityError(
            f"Skill archive must contain strictly ONE uppercase SKILL.md, "
            f"found {len(skill_md_candidates)}: {skill_md_candidates}"
        )

    skill_md_path = skill_md_candidates[0]
    path_parts = skill_md_path.split("/")
    if len(path_parts) == 1:
        root_prefix = ""
    elif len(path_parts) == 2:
        root_prefix = f"{path_parts[0]}/"
    else:
        raise SkillIntegrityError(
            f"SKILL.md must be at archive root or inside a single top-level directory, "
            f"got {skill_md_path!r}"
        )

    try:
        skill_md_text = files[skill_md_path].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillIntegrityError(f"SKILL.md is not valid UTF-8: {exc}") from exc

    frontmatter, instructions = _parse_skill_md(skill_md_text)

    references: dict[str, str] = {}
    assets: dict[str, bytes] = {}
    scripts: dict[str, str] = {}

    for path, raw_bytes in files.items():
        if path == skill_md_path:
            continue
        if root_prefix and not path.startswith(root_prefix):
            continue
        inner = path[len(root_prefix) :]
        if inner.startswith("references/"):
            rel = inner[len("references/") :]
            references[rel] = raw_bytes.decode("utf-8", errors="replace")
        elif inner.startswith("scripts/"):
            rel = inner[len("scripts/") :]
            scripts[rel] = raw_bytes.decode("utf-8", errors="replace")
        elif inner.startswith("assets/"):
            rel = inner[len("assets/") :]
            assets[rel] = raw_bytes

    return {
        "skill_md_path": skill_md_path,
        "frontmatter": frontmatter,
        "instructions": instructions,
        "references": references,
        "assets": assets,
        "scripts": scripts,
    }


def _update_time_sort_key(binding: dict[str, Any]) -> tuple[str, str]:
    """Sort key for UTC RFC-3339 ``updateTime`` matching ADK's AgentRegistry client."""
    seconds, _, fraction = (
        str(binding.get("updateTime", "")).removesuffix("Z").partition(".")
    )
    return seconds, fraction.ljust(9, "0")


class PlatformAgentConsumer:
    """Async REST client for enterprise platform agents consuming Agent Registry ``v1``."""

    def __init__(
        self,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        location: str = DEFAULT_LOCATION,
        base_url: str = DEFAULT_REGISTRY_BASE_URL,
        access_token: str | None = None,
        quota_project_id: str | None = None,
        kms_public_key_pem: bytes | str | None = None,
        require_kms_signature: bool = False,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        metadata_token_url: str = GCE_METADATA_TOKEN_URL,
        metadata_transport: httpx.AsyncBaseTransport | None = None,
        enable_adc_fallback: bool | None = None,
    ) -> None:
        if not project_id or not location:
            raise ValueError("project_id and location must be non-empty strings")

        self.project_id = project_id
        self.location = location
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token.strip() if isinstance(access_token, str) and access_token.strip() else None
        self.quota_project_id = quota_project_id or project_id
        self.kms_public_key_pem = kms_public_key_pem
        self.require_kms_signature = require_kms_signature
        self._metadata_token_url = metadata_token_url
        self._metadata_transport = metadata_transport
        self._enable_adc_fallback = (
            enable_adc_fallback
            if enable_adc_fallback is not None
            else (transport is None or metadata_transport is not None)
        )
        self._cached_oauth_token: str = ""
        self._cached_oauth_expiry: float = 0.0
        self._token_lock = asyncio.Lock()
        self._base_origin = _url_origin(httpx.URL(self.base_url))
        self._client = self._build_httpx_client(timeout=timeout, transport=transport)

    @classmethod
    def from_env(
        cls,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> PlatformAgentConsumer:
        """Build a consumer from standard workshop / GCP environment variables."""
        project_id = (
            os.environ.get("GCP_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or DEFAULT_PROJECT_ID
        )
        location = (
            os.environ.get("GCP_LOCATION")
            or os.environ.get("GOOGLE_CLOUD_LOCATION")
            or DEFAULT_LOCATION
        )
        base_url = os.environ.get("AGENT_REGISTRY_BASE_URL", DEFAULT_REGISTRY_BASE_URL)
        access_token = (
            os.environ.get("AGENT_REGISTRY_ACCESS_TOKEN")
            or os.environ.get("AGENT_REGISTRY_AUTH_TOKEN")
            or os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
            or None
        )
        require_sig = (
            os.environ.get("REQUIRE_KMS_SIGNATURE", "false").strip().lower()
            in {"1", "true", "yes"}
        )
        pem_path = os.environ.get("KMS_PUBLIC_KEY_PEM_PATH", "").strip()
        kms_pem: bytes | None = None
        if pem_path:
            kms_pem = Path(pem_path).read_bytes()

        return cls(
            project_id=project_id,
            location=location,
            base_url=base_url,
            access_token=access_token,
            kms_public_key_pem=kms_pem,
            require_kms_signature=require_sig,
            transport=transport,
        )

    @property
    def parent(self) -> str:
        return f"projects/{self.project_id}/locations/{self.location}"

    def _build_httpx_client(
        self,
        *,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None,
    ) -> httpx.AsyncClient:
        """Create an ``httpx.AsyncClient`` that strips ``x-goog-*`` & auth on redirect.

        Agent Registry's ``GetSkillRevision?alt=media`` endpoint replies with an
        HTTP 302 redirect to a short-lived GCS signed URL. GCS validates every
        ``x-goog-*`` header against the URL signature and rejects requests carrying
        ``x-goog-user-project``, ``x-goog-api-client``, or an API ``Authorization``
        bearer token. This request hook strips them whenever the redirect hops to
        a different origin.
        """
        base_origin = self._base_origin

        async def _strip_cross_origin_google_headers(request: httpx.Request) -> None:
            if _url_origin(request.url) != base_origin:
                for header_name in list(request.headers.keys()):
                    lower = header_name.lower()
                    if lower.startswith("x-goog-") or lower == "authorization":
                        del request.headers[header_name]

        return httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            event_hooks={"request": [_strip_cross_origin_google_headers]},
            transport=transport,
        )

    async def __aenter__(self) -> PlatformAgentConsumer:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def _is_googleapis(self) -> bool:
        return "googleapis.com" in self.base_url.lower()

    def _api_headers(self) -> dict[str, str]:
        headers = {
            "x-goog-api-client": "workshop-platform-agent/1.0.0",
        }
        if self.quota_project_id:
            headers["x-goog-user-project"] = self.quota_project_id
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    async def _fetch_metadata_server_token(self) -> tuple[str, float] | None:
        """Fetch an OAuth2 access token from the GCE / Cloud Run / Agent Engine metadata server."""
        try:
            async with httpx.AsyncClient(
                timeout=2.0,
                transport=self._metadata_transport,
            ) as meta_client:
                resp = await meta_client.get(
                    self._metadata_token_url,
                    headers={"Metadata-Flavor": "Google"},
                )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            token = str(payload.get("access_token", "")).strip()
            if not token:
                return None
            expires_in = float(payload.get("expires_in", 3600))
            ttl = max(10.0, expires_in - TOKEN_REFRESH_SKEW_SECONDS)
            return token, time.monotonic() + ttl
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return None

    async def _fetch_gcloud_adc_token(self) -> tuple[str, float] | None:
        """Fallback for local workstations targeting live googleapis.com via gcloud ADC."""

        def _run_gcloud() -> str:
            for cmd in (
                ["gcloud", "auth", "application-default", "print-access-token"],
                ["gcloud", "auth", "print-access-token"],
            ):
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=5,
                    )
                except (FileNotFoundError, subprocess.SubprocessError, OSError):
                    continue
                if proc.returncode == 0 and proc.stdout.strip():
                    return proc.stdout.strip()
            return ""

        token = await asyncio.to_thread(_run_gcloud)
        if not token:
            return None
        return token, time.monotonic() + 300.0

    async def get_access_token(self) -> str | None:
        """Resolve and cache an OAuth2 Bearer token for live googleapis.com calls."""
        if self.access_token:
            return self.access_token
        if not self._is_googleapis or not self._enable_adc_fallback:
            return None
        now = time.monotonic()
        if self._cached_oauth_token and now < self._cached_oauth_expiry:
            return self._cached_oauth_token

        async with self._token_lock:
            now = time.monotonic()
            if self._cached_oauth_token and now < self._cached_oauth_expiry:
                return self._cached_oauth_token

            fetched = await self._fetch_metadata_server_token()
            if fetched is None:
                fetched = await self._fetch_gcloud_adc_token()
            if fetched is not None:
                self._cached_oauth_token, self._cached_oauth_expiry = fetched
                return self._cached_oauth_token
            return None

    async def _resolve_api_headers(self) -> dict[str, str]:
        headers = self._api_headers()
        if "Authorization" not in headers and self._is_googleapis:
            token = await self.get_access_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def private_skill_name(self, raw_skill_id: str) -> str:
        """Return full Skill resource name applying Agent Registry's ``private-`` prefix."""
        cleaned = raw_skill_id.strip()
        if cleaned.startswith("projects/"):
            return cleaned
        if not cleaned.startswith(PRIVATE_SKILL_PREFIX):
            cleaned = f"{PRIVATE_SKILL_PREFIX}{cleaned}"
        return self.normalize_skill_name(cleaned)

    def normalize_skill_name(self, skill_ref: str) -> str:
        """Normalize a full Skill resource name or short skill ID into ``projects/.../skills/...``."""
        cleaned = skill_ref.strip().lstrip("/")
        if cleaned.startswith("projects/"):
            return cleaned
        if cleaned.startswith("skills/"):
            segment = cleaned[len("skills/") :]
        else:
            segment = cleaned
        if not _SAFE_SEGMENT_RE.fullmatch(segment):
            raise ValueError(
                f"Invalid skill resource ID {segment!r}: expected safe single path segment"
            )
        return f"{self.parent}/skills/{quote(segment, safe='')}"

    def _build_url(self, resource_or_relative_path: str) -> str:
        cleaned = resource_or_relative_path.lstrip("/")
        if cleaned.startswith("projects/"):
            return f"{self.base_url}/{cleaned}"
        return f"{self.base_url}/{self.parent}/{cleaned}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = self._build_url(path)
        headers = await self._resolve_api_headers()
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = await self._client.request(
            method.upper(),
            url,
            headers=headers,
            params=params,
            json=json_body,
        )
        response.raise_for_status()
        return response

    # --- Standalone Skills (GA v1) ---

    async def search_skills_page(
        self,
        search_string: str = "",
        *,
        search_type: Literal["SEMANTIC", "KEYWORD"] = "SEMANTIC",
        filter_expr: str | None = APPROVED_SKILL_FILTER,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Execute one page of ``POST .../skills:search``."""
        payload: dict[str, Any] = {"searchType": search_type}
        if search_string:
            payload["searchString"] = search_string
        if filter_expr:
            payload["filter"] = filter_expr
        if page_size is not None:
            payload["pageSize"] = page_size
        if page_token:
            payload["pageToken"] = page_token
        resp = await self._request("POST", "skills:search", json_body=payload)
        return resp.json()

    async def search_skills(
        self,
        search_string: str = "",
        *,
        search_type: Literal["SEMANTIC", "KEYWORD"] = "SEMANTIC",
        filter_expr: str | None = APPROVED_SKILL_FILTER,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        """Discover approved Skills via ``skills:search`` (follows pagination)."""
        skills: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            data = await self.search_skills_page(
                search_string=search_string,
                search_type=search_type,
                filter_expr=filter_expr,
                page_size=page_size,
                page_token=page_token,
            )
            skills.extend(data.get("skills", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return skills

    async def get_skill(self, skill_name: str) -> dict[str, Any]:
        """Fetch logical ``Skill`` metadata (Step 1 of 2-step skill download)."""
        normalized = self.normalize_skill_name(skill_name)
        resp = await self._request("GET", normalized)
        return resp.json()

    async def get_skill_revision(self, revision_name: str) -> dict[str, Any]:
        """Fetch immutable ``SkillRevision`` metadata (``sha256Hash``, ``scanResult``)."""
        resp = await self._request("GET", revision_name)
        return resp.json()

    async def download_skill_archive(
        self,
        revision_name: str,
        *,
        expected_sha256: str | None = None,
        max_redirects: int = DEFAULT_MAX_MEDIA_REDIRECTS,
    ) -> tuple[bytes, str]:
        """Download Skill ZIP bytes via ``GetSkillRevision?alt=media`` (Step 2).

        Sends the initial API request with ``follow_redirects=False`` and API
        headers, then follows HTTP 3xx redirects (both cross-origin GCS signed
        URLs and same-origin ``/scotty-signed/{token}`` redirects) with NO
        ``Authorization`` and NO ``x-goog-*`` headers. Verifies the SHA-256
        digest when ``expected_sha256`` is provided.
        """
        url = self._build_url(revision_name)
        resp = await self._client.request(
            "GET",
            url,
            headers=await self._resolve_api_headers(),
            params={"alt": "media"},
            follow_redirects=False,
        )
        hops = 0
        while resp.is_redirect and "location" in resp.headers:
            if hops >= max_redirects:
                raise httpx.TooManyRedirects(
                    f"Exceeded {max_redirects} redirects downloading {revision_name}",
                    request=resp.request,
                )
            next_url = resp.url.join(resp.headers["location"])
            resp = await self._client.request(
                "GET",
                str(next_url),
                headers={},
                follow_redirects=False,
            )
            hops += 1
        resp.raise_for_status()
        zip_bytes = resp.content
        computed_sha256 = hashlib.sha256(zip_bytes).hexdigest()

        if expected_sha256:
            normalized_expected = _normalize_sha256(expected_sha256)
            if not hmac.compare_digest(computed_sha256, normalized_expected):
                raise SkillIntegrityError(
                    f"SHA-256 mismatch for {revision_name}: "
                    f"expected {normalized_expected}, got {computed_sha256}"
                )
        return zip_bytes, computed_sha256

    async def fetch_and_extract_skill(
        self,
        skill_name: str,
        *,
        verify_hash: bool = True,
        verify_kms_signature: bool | None = None,
        public_key_pem: bytes | str | None = None,
        attestation_signature_b64: str | None = None,
        allow_non_active: bool = False,
    ) -> ExtractedSkill:
        """Execute full governed 2-step Skill fetch, verification, and extraction.

        1. ``GET .../skills/{skill}`` -> check lifecycle state & ``defaultRevision``
        2. ``GET .../{defaultRevision}`` -> inspect ``scanResult`` & ``sha256Hash``
        3. ``GET .../{defaultRevision}?alt=media`` -> follow 302 redirect (stripping
           ``x-goog-*`` and ``Authorization``), verify ``sha256Hash``
        4. Safely extract ZIP & parse ``SKILL.md``
        5. Optionally verify Cloud KMS ``EC_SIGN_P256_SHA256`` signature
        """
        skill_data = await self.get_skill(skill_name)
        resource_name = str(skill_data.get("name") or self.normalize_skill_name(skill_name))
        skill_id = str(skill_data.get("skillId") or resource_name.split("/")[-1])
        state = str(skill_data.get("state") or "")

        if not allow_non_active and state and state not in SERVABLE_SKILL_STATES:
            raise SkillGovernanceError(
                f"Skill {resource_name!r} is in non-servable state {state!r}"
            )

        default_revision = skill_data.get("defaultRevision") or skill_data.get(
            "default_revision"
        )
        if not default_revision or not isinstance(default_revision, str):
            raise SkillGovernanceError(
                f"Skill {resource_name!r} has no promoted defaultRevision"
            )

        revision_data = await self.get_skill_revision(default_revision)
        raw_scan_result = revision_data.get("scanResult")
        scan_result = raw_scan_result if isinstance(raw_scan_result, dict) else {}
        findings = scan_result.get("findings") or []
        raw_scan_cfg = skill_data.get("scanningConfig")
        scanning_policy = (
            str(raw_scan_cfg.get("policy") or "ENABLED_BLOCKING")
            if isinstance(raw_scan_cfg, dict)
            else "ENABLED_BLOCKING"
        )
        if not allow_non_active:
            if findings:
                raise SkillGovernanceError(
                    f"Skill revision {default_revision!r} has {len(findings)} security finding(s)"
                )
            if scanning_policy in {"ENABLED_BLOCKING", "POLICY_UNSPECIFIED", ""}:
                scan_state = scan_result.get("scanState")
                if (
                    isinstance(scan_state, str)
                    and scan_state
                    and scan_state not in ALLOWED_BLOCKING_SCAN_STATES
                ):
                    raise SkillGovernanceError(
                        f"Skill revision {default_revision!r} blocked by {scanning_policy} "
                        f"policy: scanState is {scan_state!r}"
                    )

        expected_sha256 = (
            revision_data.get("sha256Hash") or revision_data.get("sha256_hash")
        )
        if verify_hash and not expected_sha256:
            raise SkillIntegrityError(
                f"Skill revision {default_revision!r} missing server-computed sha256Hash"
            )

        zip_bytes, actual_sha256 = await self.download_skill_archive(
            default_revision,
            expected_sha256=expected_sha256 if verify_hash else None,
        )

        extracted = extract_skill_zip(zip_bytes)
        frontmatter: dict[str, Any] = extracted["frontmatter"]
        instructions: str = extracted["instructions"]
        references: dict[str, str] = extracted["references"]
        assets: dict[str, bytes] = extracted["assets"]
        scripts: dict[str, str] = extracted["scripts"]

        meta = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}
        revision_meta = (
            revision_data.get("frontmatter", {}).get("metadata", {})
            if isinstance(revision_data.get("frontmatter"), dict)
            else {}
        )
        sig_b64 = (
            attestation_signature_b64
            or (meta.get("kms_signature") if isinstance(meta, dict) else None)
            or (revision_meta.get("kms_signature") if isinstance(revision_meta, dict) else None)
        )
        key_version = (
            (meta.get("kms_key_version") if isinstance(meta, dict) else None)
            or (revision_meta.get("kms_key_version") if isinstance(revision_meta, dict) else None)
        )

        should_verify_kms = (
            self.require_kms_signature
            if verify_kms_signature is None
            else verify_kms_signature
        )
        effective_pem = public_key_pem or self.kms_public_key_pem
        kms_verified = False

        if should_verify_kms:
            if not effective_pem:
                raise SkillSignatureError(
                    "KMS signature verification requested but no kms_public_key_pem was provided"
                )
            if not sig_b64 or not isinstance(sig_b64, str):
                raise SkillSignatureError(
                    f"Skill {resource_name!r} is missing required kms_signature attestation"
                )
            kms_verified = self._verify_skill_signature(
                public_key_pem=effective_pem,
                signature_b64=sig_b64,
                zip_bytes=zip_bytes,
                sha256_hex=actual_sha256,
                frontmatter=frontmatter,
                instructions=instructions,
                references=references,
                assets=assets,
                scripts=scripts,
            )
            if not kms_verified:
                raise SkillSignatureError(
                    f"Cloud KMS ECDSA P-256 signature verification failed for {resource_name!r}"
                )

        return ExtractedSkill(
            resource_name=resource_name,
            skill_id=skill_id,
            revision_name=default_revision,
            state=state or "STATE_ACTIVE",
            sha256_hash=actual_sha256,
            size_bytes=len(zip_bytes),
            frontmatter=frontmatter,
            instructions=instructions,
            skill_md_path=extracted["skill_md_path"],
            references=references,
            assets=assets,
            scripts=scripts,
            kms_verified=kms_verified,
            kms_key_version=str(key_version) if key_version else None,
        )

    @staticmethod
    def _verify_skill_signature(
        *,
        public_key_pem: bytes | str,
        signature_b64: str,
        zip_bytes: bytes,
        sha256_hex: str,
        frontmatter: dict[str, Any],
        instructions: str,
        references: dict[str, str],
        assets: dict[str, bytes],
        scripts: dict[str, str],
    ) -> bool:
        """Verify Cloud KMS ``EC_SIGN_P256_SHA256`` signature across supported signing modes.

        Supports:
        1. Cloud KMS ``AsymmetricSign(digest={"sha256": raw_zip_sha256})`` (equivalent to
           ECDSA-SHA256 over ``zip_bytes``) for sidecar / revision attestations.
        2. ECDSA-SHA256 over ``sha256_hex.encode("utf-8")`` when CI signs the hex
           ``SkillRevision.sha256Hash`` string directly.
        3. ECDSA-SHA256 over the canonical ``agent-registry-skill-v1`` ZIP payload digest
           produced by ``tools/plugin_sync``, ``middleware/app/security_verifier``, and
           ``security-governance/kms/sign_and_verify.py``.
        4. ECDSA-SHA256 over the structured canonical unsigned skill content digest (for
           signatures embedded inside ``SKILL.md`` ``frontmatter.metadata.kms_signature``).
        """
        if verify_kms_ecdsa_p256_signature(
            public_key_pem, signature_b64, bytes.fromhex(sha256_hex), prehashed=True
        ):
            return True
        if verify_kms_ecdsa_p256_signature(
            public_key_pem, signature_b64, sha256_hex.encode("utf-8"), prehashed=False
        ):
            return True
        try:
            v1_zip_digest = compute_canonical_skill_zip_digest(zip_bytes)
            if verify_kms_ecdsa_p256_signature(
                public_key_pem, signature_b64, v1_zip_digest, prehashed=True
            ):
                return True
        except Exception:
            pass
        canonical_digest = compute_canonical_skill_digest(
            frontmatter,
            instructions,
            references=references,
            assets=assets,
            scripts=scripts,
        )
        return verify_kms_ecdsa_p256_signature(
            public_key_pem, signature_b64, canonical_digest, prehashed=True
        )

    # --- MCP Servers (GA v1) ---

    async def list_mcp_servers(
        self,
        *,
        filter_expr: str | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        """List registered MCP Servers via ``GET .../mcpServers``."""
        servers: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {}
            if filter_expr:
                params["filter"] = filter_expr
            if page_size is not None:
                params["pageSize"] = str(page_size)
            if page_token:
                params["pageToken"] = page_token
            resp = await self._request("GET", "mcpServers", params=params or None)
            data = resp.json()
            servers.extend(data.get("mcpServers", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return servers

    async def search_mcp_servers(
        self,
        search_string: str = "",
        *,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search MCP Servers via ``POST .../mcpServers:search`` (keyword/prefix only)."""
        servers: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            payload: dict[str, Any] = {}
            if search_string:
                payload["searchString"] = search_string
            if page_size is not None:
                payload["pageSize"] = page_size
            if page_token:
                payload["pageToken"] = page_token
            resp = await self._request("POST", "mcpServers:search", json_body=payload)
            data = resp.json()
            servers.extend(data.get("mcpServers", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return servers

    async def get_mcp_server(self, mcp_server_name: str) -> dict[str, Any]:
        """Fetch details of a single registered MCP Server."""
        resp = await self._request("GET", mcp_server_name)
        return resp.json()

    # --- A2A Agents (GA v1) ---

    async def list_agents(
        self,
        *,
        filter_expr: str | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        """List registered A2A Agents via ``GET .../agents``."""
        agents: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {}
            if filter_expr:
                params["filter"] = filter_expr
            if page_size is not None:
                params["pageSize"] = str(page_size)
            if page_token:
                params["pageToken"] = page_token
            resp = await self._request("GET", "agents", params=params or None)
            data = resp.json()
            agents.extend(data.get("agents", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return agents

    async def search_agents(
        self,
        search_string: str = "",
        *,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search A2A Agents via ``POST .../agents:search`` (keyword/prefix only)."""
        agents: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            payload: dict[str, Any] = {}
            if search_string:
                payload["searchString"] = search_string
            if page_size is not None:
                payload["pageSize"] = page_size
            if page_token:
                payload["pageToken"] = page_token
            resp = await self._request("POST", "agents:search", json_body=payload)
            data = resp.json()
            agents.extend(data.get("agents", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return agents

    async def get_agent(self, agent_name: str) -> dict[str, Any]:
        """Fetch details of a single registered A2A Agent."""
        resp = await self._request("GET", agent_name)
        return resp.json()

    # --- Auth Manager Bindings (GA v1) ---

    async def fetch_available_bindings(
        self,
        source_identifier: str,
        target_identifier: str | None = None,
        *,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch bindings accessible to the caller via ``GET .../bindings:fetchAvailable``.

        Agent Registry actively filters out bindings whose ``AuthProvider`` the
        caller lacks ``roles/agentidentity.user`` permission on.
        """
        bindings: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"sourceIdentifier": source_identifier}
            if target_identifier:
                params["targetIdentifier"] = target_identifier
            if page_size is not None:
                params["pageSize"] = str(page_size)
            if page_token:
                params["pageToken"] = page_token
            resp = await self._request("GET", "bindings:fetchAvailable", params=params)
            data = resp.json()
            bindings.extend(data.get("bindings", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return bindings

    async def resolve_auth_binding(
        self,
        source_identifier: str,
        target_identifier: str,
    ) -> dict[str, Any] | None:
        """Resolve the winning ``authProviderBinding`` for a source agent and target resource.

        Mirrors ADK ``AgentRegistry._fetch_available_auth_scheme`` selection logic:
        prefers bindings scoped specifically to ``source_identifier`` over wildcard
        bindings, then picks the binding with the latest ``updateTime``.
        """
        bindings = await self.fetch_available_bindings(
            source_identifier=source_identifier,
            target_identifier=target_identifier,
        )
        matching: list[dict[str, Any]] = []
        for b in bindings:
            if not isinstance(b, dict):
                continue
            target = b.get("target") or {}
            auth_binding = b.get("authProviderBinding") or {}
            if not isinstance(target, dict) or not isinstance(auth_binding, dict):
                continue
            target_id = target.get("identifier") or ""
            if (
                isinstance(target_id, str)
                and target_id.endswith(target_identifier)
                and auth_binding.get("authProvider")
            ):
                matching.append(b)
        if not matching:
            return None

        source_scoped = [
            b
            for b in matching
            if isinstance(b.get("source"), dict)
            and (b.get("source") or {}).get("identifier") == source_identifier
        ]
        wildcard_bindings = [
            b
            for b in matching
            if not (
                isinstance(b.get("source"), dict)
                and (b.get("source") or {}).get("identifier")
            )
        ]
        candidates = source_scoped or wildcard_bindings
        if not candidates:
            return None
        newest = max(candidates, key=_update_time_sort_key)
        return dict(newest["authProviderBinding"])

    # --- High-level Workspace Discovery ---

    async def discover_capabilities(
        self,
        *,
        skill_query: str = "",
        skill_search_type: Literal["SEMANTIC", "KEYWORD"] = "SEMANTIC",
        skill_filter: str | None = APPROVED_SKILL_FILTER,
        mcp_query: str = "",
        agent_query: str = "",
        source_agent_urn: str | None = None,
        fetch_skill_contents: bool = False,
        verify_kms_signature: bool | None = None,
    ) -> DiscoveredCapabilities:
        """Discover approved Skills, MCP Servers, A2A Agents, and Auth Bindings."""
        skills = await self.search_skills(
            search_string=skill_query,
            search_type=skill_search_type,
            filter_expr=skill_filter,
        )
        mcp_servers = await self.search_mcp_servers(search_string=mcp_query)
        agents = await self.search_agents(search_string=agent_query)

        extracted_skills: list[ExtractedSkill] = []
        if fetch_skill_contents:
            for skill in skills:
                skill_name = skill.get("name")
                if isinstance(skill_name, str) and skill_name:
                    extracted = await self.fetch_and_extract_skill(
                        skill_name,
                        verify_kms_signature=verify_kms_signature,
                    )
                    extracted_skills.append(extracted)

        resolved_bindings: dict[str, dict[str, Any]] = {}
        if source_agent_urn:
            for server in mcp_servers:
                target_id = server.get("mcpServerId")
                if isinstance(target_id, str) and target_id:
                    binding = await self.resolve_auth_binding(source_agent_urn, target_id)
                    if binding:
                        resolved_bindings[target_id] = binding
            for agent in agents:
                target_id = agent.get("agentId")
                if isinstance(target_id, str) and target_id and target_id != source_agent_urn:
                    binding = await self.resolve_auth_binding(source_agent_urn, target_id)
                    if binding:
                        resolved_bindings[target_id] = binding

        return DiscoveredCapabilities(
            project_id=self.project_id,
            location=self.location,
            base_url=self.base_url,
            skills=skills,
            extracted_skills=extracted_skills,
            mcp_servers=mcp_servers,
            agents=agents,
            resolved_bindings=resolved_bindings,
        )
