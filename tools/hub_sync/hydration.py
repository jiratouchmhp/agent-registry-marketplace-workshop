"""Safe, ownership-tracked hydration engine for the Acme Agent Hub CLI.

Modeled after Google's ``hydrate-from-ar.sh`` design principles:
- Fetches catalog and Polyglot plugin ZIPs from the enterprise Middleware.
- Verifies SHA-256 digests and enforces strict zip-slip / symlink protection
  before extracting any bytes to disk.
- Tracks every owned plugin directory, MCP server key, Codex agent file,
  Claude enabledPlugins entry, and Antigravity plugin directory in
  ``<hub_dir>/.sync-manifest.json`` via atomic writes (tempfile + os.replace).
- NEVER overwrites user-authored entries in IDE config files.
- Preserves user-set preference fields (``disabled``, ``disabledTools``, etc.)
  on hydrated MCP server entries across re-syncs.
- Prunes previously hydrated items ONLY when an upstream full catalog fetch
  and download completed cleanly (never on partial failure).
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from schemas.validators import parse_frontmatter

DEFAULT_HUB_DIR_NAME = ".acme-agent-hub"
logger = logging.getLogger(__name__)
SYNC_MANIFEST_FILENAME = ".sync-manifest.json"
MANIFEST_SCHEMA_VERSION = "1.0.0"

DEFAULT_MARKETPLACE_NAME = "acme-enterprise-marketplace"
DEFAULT_MARKETPLACE_DISPLAY_NAME = "Acme Enterprise Agent Marketplace"
DEFAULT_MARKETPLACE_DESCRIPTION = (
    "Governed enterprise plugins, skills, MCP servers, and A2A agents "
    "backed by Google Cloud Agent Registry."
)
DEFAULT_MARKETPLACE_OWNER: dict[str, str] = {
    "name": "Acme Platform Security",
    "email": "platform-security@acme.example.com",
}

SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
INVALID_MCP_KEY_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]+")
NON_EXECUTABLE_SCRIPT_EXTS = frozenset({".md", ".txt", ".json", ".yaml", ".yml", ".pyc"})

# User-managed preference keys preserved on hydrated MCP server entries.
PRESERVED_MCP_USER_KEYS: tuple[str, ...] = (
    "disabled",
    "disabledTools",
    "disabled_tools",
    "enabled",
    "enabled_tools",
    "autoApprove",
    "alwaysAllow",
)

MAX_REDIRECTS = 5
DEFAULT_MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # 50 MiB safety cap
DEFAULT_MAX_ZIP_DEPTH = 16


class HubSyncError(Exception):
    """Base error raised by hub-sync operations."""


class CatalogFetchError(HubSyncError):
    """Raised when the Middleware catalog or archive cannot be fetched cleanly."""


class IntegrityError(HubSyncError):
    """Raised when a downloaded plugin ZIP fails SHA-256 verification."""


class ZipSecurityError(HubSyncError):
    """Raised when a ZIP archive contains traversal, absolute, or symlink entries."""


@dataclass
class MarketplaceMetadata:
    """Enterprise marketplace catalog metadata."""

    name: str = DEFAULT_MARKETPLACE_NAME
    display_name: str = DEFAULT_MARKETPLACE_DISPLAY_NAME
    description: str = DEFAULT_MARKETPLACE_DESCRIPTION
    owner: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MARKETPLACE_OWNER))
    version: str = "1.0.0"


@dataclass
class CatalogPlugin:
    """Single plugin entry parsed from Middleware ``GET /v1/catalog``."""

    name: str
    display_name: str
    version: str
    description: str
    download_url: str
    sha256: str | None = None
    category: str = "Enterprise"
    tags: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class CatalogPayload:
    """Normalized response from Middleware ``GET /v1/catalog``."""

    marketplace: MarketplaceMetadata
    plugins: list[CatalogPlugin] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class AgentSpec:
    """Normalized subagent definition discovered inside an extracted plugin."""

    name: str
    description: str
    instructions: str
    plugin_name: str
    source_path: str | None = None


@dataclass
class ExtractedPlugin:
    """Verified, extracted plugin bundle ready for IDE adapter wiring."""

    name: str
    display_name: str
    version: str
    description: str
    sha256: str
    archive_url: str
    install_path: Path
    category: str = "Enterprise"
    tags: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    agents: list[AgentSpec] = field(default_factory=list)


@dataclass
class SyncManifest:
    """Persistent ownership manifest saved at ``<hub_dir>/.sync-manifest.json``."""

    schema_version: str = MANIFEST_SCHEMA_VERSION
    middleware_url: str = ""
    marketplace_name: str = DEFAULT_MARKETPLACE_NAME
    last_synced_at: str = ""
    last_sync_complete: bool = False
    tools: list[str] = field(default_factory=list)
    owned_plugins: dict[str, dict[str, Any]] = field(default_factory=dict)
    owned_mcp_servers: list[str] = field(default_factory=list)
    owned_codex_mcp_servers: list[str] = field(default_factory=list)
    owned_codex_plugins: list[str] = field(default_factory=list)
    owned_codex_agents: list[str] = field(default_factory=list)
    owned_claude_plugins: list[str] = field(default_factory=list)
    owned_antigravity_plugins: list[str] = field(default_factory=list)
    owned_vscode_plugin_locations: list[str] = field(default_factory=list)
    owned_skill_dirs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "middleware_url": self.middleware_url,
            "marketplace_name": self.marketplace_name,
            "last_synced_at": self.last_synced_at,
            "last_sync_complete": self.last_sync_complete,
            "tools": sorted(set(self.tools)),
            "owned_plugins": self.owned_plugins,
            "owned_mcp_servers": sorted(set(self.owned_mcp_servers)),
            "owned_codex_mcp_servers": sorted(set(self.owned_codex_mcp_servers)),
            "owned_codex_plugins": sorted(set(self.owned_codex_plugins)),
            "owned_codex_agents": sorted(set(self.owned_codex_agents)),
            "owned_claude_plugins": sorted(set(self.owned_claude_plugins)),
            "owned_antigravity_plugins": sorted(set(self.owned_antigravity_plugins)),
            "owned_vscode_plugin_locations": sorted(set(self.owned_vscode_plugin_locations)),
            "owned_skill_dirs": sorted(set(self.owned_skill_dirs)),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SyncManifest:
        owned_mcp = list(data.get("owned_mcp_servers") or [])
        owned_codex_mcp = list(data.get("owned_codex_mcp_servers") or owned_mcp)
        return cls(
            schema_version=str(data.get("schema_version") or MANIFEST_SCHEMA_VERSION),
            middleware_url=str(data.get("middleware_url") or ""),
            marketplace_name=str(data.get("marketplace_name") or DEFAULT_MARKETPLACE_NAME),
            last_synced_at=str(data.get("last_synced_at") or ""),
            last_sync_complete=bool(data.get("last_sync_complete", False)),
            tools=[str(t) for t in (data.get("tools") or [])],
            owned_plugins=dict(data.get("owned_plugins") or {}),
            owned_mcp_servers=[str(k) for k in owned_mcp],
            owned_codex_mcp_servers=[str(k) for k in owned_codex_mcp],
            owned_codex_plugins=[str(p) for p in (data.get("owned_codex_plugins") or [])],
            owned_codex_agents=[str(a) for a in (data.get("owned_codex_agents") or [])],
            owned_claude_plugins=[str(p) for p in (data.get("owned_claude_plugins") or [])],
            owned_antigravity_plugins=[
                str(p) for p in (data.get("owned_antigravity_plugins") or [])
            ],
            owned_vscode_plugin_locations=[
                str(p) for p in (data.get("owned_vscode_plugin_locations") or [])
            ],
            owned_skill_dirs=[str(s) for s in (data.get("owned_skill_dirs") or [])],
        )


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def resolve_hub_dir(home_dir: Path, hub_dir: Path | None = None) -> Path:
    """Resolve the local hub directory (defaults to ``<home>/.acme-agent-hub``)."""
    if hub_dir is not None:
        return hub_dir.expanduser().resolve()
    return (home_dir.expanduser().resolve() / DEFAULT_HUB_DIR_NAME).resolve()


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Atomically write bytes to ``path`` via same-directory tempfile + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = fh.name
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    """Atomically write UTF-8 text to ``path``."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically serialize JSON with stable formatting and trailing newline."""
    rendered = json.dumps(data, indent=2, sort_keys=False) + "\n"
    atomic_write_text(path, rendered)


def load_json_object(path: Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load a JSON object from ``path``, returning a fresh default dict if absent."""
    if not path.is_file():
        return copy.deepcopy(default) if default is not None else {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return copy.deepcopy(default) if default is not None else {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise HubSyncError(f"Expected a JSON object in {path}, got {type(parsed).__name__}")
    return parsed


def load_sync_manifest(hub_dir: Path) -> SyncManifest:
    """Load ``<hub_dir>/.sync-manifest.json`` if present, else an empty manifest."""
    manifest_path = hub_dir / SYNC_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return SyncManifest()
    data = load_json_object(manifest_path)
    return SyncManifest.from_dict(data)


def save_sync_manifest(hub_dir: Path, manifest: SyncManifest) -> Path:
    """Atomically persist ``<hub_dir>/.sync-manifest.json``."""
    manifest_path = hub_dir / SYNC_MANIFEST_FILENAME
    atomic_write_json(manifest_path, manifest.to_dict())
    return manifest_path


def validate_safe_identifier(
    value: Any,
    *,
    label: str,
    error_cls: type[Exception] = HubSyncError,
) -> str:
    """Validate that ``value`` is a single safe filesystem path segment identifier.

    Blocks path separators (``/``, ``\\``), parent traversal (``..``), leading dots,
    NUL bytes, and overly long names.
    """
    if not isinstance(value, str):
        raise error_cls(f"{label} must be a non-empty string")
    cleaned = value.strip()
    if (
        not cleaned
        or ".." in cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or not SAFE_IDENTIFIER_RE.match(cleaned)
    ):
        raise error_cls(
            f"Unsafe or invalid identifier for {label}: {value!r} "
            f"(must match {SAFE_IDENTIFIER_RE.pattern} with no '..' or path separators)"
        )
    return cleaned


def ensure_contained_path(
    base_dir: Path,
    target_path: Path,
    *,
    label: str = "path",
    error_cls: type[Exception] = ZipSecurityError,
    follow_final_symlink: bool = True,
) -> Path:
    """Verify ``target_path`` stays strictly inside ``base_dir``.

    When ``follow_final_symlink=False`` (for managing symlink entries whose
    target points elsewhere), verifies the parent directory resolves within
    ``base_dir`` and the final filename is a safe single segment.
    """
    base_resolved = base_dir.resolve()
    if follow_final_symlink:
        resolved_target = target_path.resolve()
        try:
            resolved_target.relative_to(base_resolved)
        except ValueError as exc:
            raise error_cls(
                f"{label} ({target_path}) escapes base directory ({base_resolved})"
            ) from exc
        if resolved_target == base_resolved:
            raise error_cls(f"{label} must point to a child inside {base_resolved}")
        return resolved_target

    parent_resolved = target_path.parent.resolve()
    try:
        parent_resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise error_cls(
            f"{label} parent ({target_path.parent}) escapes base directory ({base_resolved})"
        ) from exc
    name = target_path.name
    if not name or name in (".", "..") or "/" in name or "\\" in name or ".." in name:
        raise error_cls(f"{label} filename ({name!r}) is not a safe path segment")
    return parent_resolved / name


def sanitize_mcp_key(raw_key: str, max_length: int = 20) -> str:
    """Sanitize an MCP server key to match ``^[a-zA-Z0-9_-]{1,64}$`` (recommended <= 20 chars).

    Replaces dots and disallowed punctuation with ``-``. Preserves short valid
    keys unchanged (e.g. ``finops-mcp-server`` -> ``finops-mcp-server``).
    Truncates longer keys deterministically with a 4-hex hash suffix so two
    long names with the same prefix never collide.
    """
    cleaned = INVALID_MCP_KEY_CHARS_RE.sub("-", raw_key.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned:
        cleaned = "mcp-server"
    effective_max = max(6, min(max_length, 64))
    if len(cleaned) <= effective_max:
        return cleaned
    suffix = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:4]
    prefix = cleaned[: effective_max - 5].rstrip("-_")
    if not prefix:
        prefix = "mcp"
    return f"{prefix}-{suffix}"


def compute_sha256(data: bytes) -> str:
    """Compute lowercase hexadecimal SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def verify_sha256(data: bytes, expected_sha256: str, label: str = "plugin archive") -> str:
    """Verify ``data`` matches ``expected_sha256`` using constant-time comparison."""
    expected = expected_sha256.strip().lower()
    if not SHA256_HEX_RE.match(expected):
        raise IntegrityError(
            f"Invalid expected SHA-256 digest for {label}: {expected_sha256!r}"
        )
    actual = compute_sha256(data)
    if not hmac.compare_digest(actual, expected):
        raise IntegrityError(
            f"SHA-256 mismatch for {label}: expected {expected}, got {actual}"
        )
    return actual


def _normalize_zip_member_parts(raw_name: str) -> tuple[str, ...]:
    """Validate and split a ZIP member path, blocking traversal and absolute paths."""
    if not raw_name or "\x00" in raw_name:
        raise ZipSecurityError(f"Invalid empty or NUL-containing ZIP member path: {raw_name!r}")
    if "\\" in raw_name:
        raise ZipSecurityError(f"Backslash path separator forbidden in ZIP member: {raw_name!r}")
    if raw_name.startswith("/") or re.match(r"^[A-Za-z]:", raw_name):
        raise ZipSecurityError(f"Absolute path forbidden in ZIP member: {raw_name!r}")

    posix_path = PurePosixPath(raw_name)
    parts: list[str] = []
    for part in posix_path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise ZipSecurityError(f"Path traversal ('..') forbidden in ZIP member: {raw_name!r}")
        parts.append(part)
    return tuple(parts)


def _detect_single_wrapper_prefix(member_parts_list: list[tuple[str, ...]]) -> str | None:
    """Detect whether all archive files are nested under a single top-level plugin wrapper folder."""
    non_empty = [parts for parts in member_parts_list if parts]
    if not non_empty:
        return None
    top_names = {parts[0] for parts in non_empty}
    if len(top_names) != 1:
        return None
    top = next(iter(top_names))
    nested_manifests = {
        (top, "plugin.json"),
        (top, ".antigravity-plugin", "plugin.json"),
        (top, ".claude-plugin", "plugin.json"),
        (top, ".codex-plugin", "plugin.json"),
    }
    if any(parts in nested_manifests for parts in non_empty):
        return top
    return None


def _swap_directory_atomically(staged_dir: Path, dest_dir: Path) -> None:
    """Atomically replace ``dest_dir`` with ``staged_dir``, rolling back on failure."""
    backup_dir: Path | None = None
    try:
        if dest_dir.exists() or dest_dir.is_symlink():
            backup_dir = Path(
                tempfile.mkdtemp(dir=str(dest_dir.parent), prefix=f".bak-{dest_dir.name}-")
            )
            backup_dir.rmdir()
            os.replace(dest_dir, backup_dir)
        os.replace(staged_dir, dest_dir)
        if backup_dir is not None:
            if backup_dir.is_symlink():
                backup_dir.unlink()
            elif backup_dir.exists():
                shutil.rmtree(backup_dir)
    except Exception:
        if (
            backup_dir is not None
            and (backup_dir.exists() or backup_dir.is_symlink())
            and not (dest_dir.exists() or dest_dir.is_symlink())
        ):
            try:
                os.replace(backup_dir, dest_dir)
            except OSError:
                pass
        raise


def validate_zip_bytes(
    zip_bytes: bytes,
    *,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_depth: int = DEFAULT_MAX_ZIP_DEPTH,
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
    """Validate ZIP archive bytes in memory before writing anything to disk.

    Blocks ``..`` path traversal, absolute paths, drive letters, backslashes,
    symlinks, special device files, excessive nesting depth, and oversized
    declared payloads.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile as exc:
        raise ZipSecurityError(f"Invalid ZIP archive: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if not infos:
            raise ZipSecurityError("ZIP archive is empty")

        validated_entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        declared_total = 0

        for info in infos:
            unix_mode = info.external_attr >> 16
            file_type = stat.S_IFMT(unix_mode)
            if file_type == stat.S_IFLNK:
                raise ZipSecurityError(f"Symlink entry forbidden in ZIP archive: {info.filename!r}")
            if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ZipSecurityError(
                    f"Unsupported special file entry in ZIP archive: {info.filename!r}"
                )

            parts = _normalize_zip_member_parts(info.filename)
            if not parts:
                continue
            if len(parts) > max_depth:
                raise ZipSecurityError(
                    f"ZIP member exceeds max nesting depth ({max_depth}): {info.filename!r}"
                )

            declared_total += info.file_size
            if declared_total > max_uncompressed_bytes:
                raise ZipSecurityError(
                    f"ZIP archive uncompressed size exceeds limit ({max_uncompressed_bytes} bytes)"
                )
            validated_entries.append((info, parts))

        # Also validate agent frontmatter `name` values in-memory before any extraction
        wrapper_prefix = _detect_single_wrapper_prefix([p for _, p in validated_entries])
        for info, parts in validated_entries:
            if info.is_dir():
                continue
            rel_parts = parts[1:] if (wrapper_prefix and parts[0] == wrapper_prefix) else parts
            is_agent_md = (
                (len(rel_parts) == 2 and rel_parts[0] == "agents" and rel_parts[1].endswith(".md"))
                or (
                    len(rel_parts) == 3
                    and rel_parts[0] == "com.github.copilot"
                    and rel_parts[1] == "agents"
                    and rel_parts[2].endswith(".agent.md")
                )
            )
            if not is_agent_md:
                continue
            try:
                raw_text = zf.read(info).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ZipSecurityError(
                    f"Agent file {info.filename!r} is not valid UTF-8: {exc}"
                ) from exc
            frontmatter, _body, fm_err = parse_frontmatter(raw_text)
            if fm_err or frontmatter is None:
                continue
            raw_agent_name = frontmatter.get("name")
            if isinstance(raw_agent_name, str) and raw_agent_name.strip():
                validate_safe_identifier(
                    raw_agent_name,
                    label=f"Agent frontmatter 'name' in {info.filename!r}",
                    error_cls=ZipSecurityError,
                )

    if not validated_entries:
        raise ZipSecurityError("ZIP archive contains no valid entries")
    return validated_entries


def safe_extract_zip(
    zip_bytes: bytes,
    dest_dir: Path,
    *,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_depth: int = DEFAULT_MAX_ZIP_DEPTH,
) -> None:
    """Safely validate and atomically extract a plugin ZIP archive into ``dest_dir``.

    Security protections:
    - Blocks ``..`` path traversal, absolute paths, drive letters, and backslashes.
    - Blocks symlinks and special device/FIFO entries.
    - Enforces maximum nesting depth and total uncompressed size cap.
    - Stages extraction into a sibling temporary directory before swapping into
      ``dest_dir``, guaranteeing no partial extraction on error.
    - Preserves Unix executable permissions (``0755``) on scripts.
    """
    validated_entries = validate_zip_bytes(
        zip_bytes,
        max_uncompressed_bytes=max_uncompressed_bytes,
        max_depth=max_depth,
    )

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        wrapper_prefix = _detect_single_wrapper_prefix([p for _, p in validated_entries])

        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        staged_dir = Path(
            tempfile.mkdtemp(dir=str(dest_dir.parent), prefix=f".stage-{dest_dir.name}-")
        )
        staged_resolved = staged_dir.resolve()
        actual_written = 0

        try:
            for info, parts in validated_entries:
                rel_parts = parts[1:] if (wrapper_prefix and parts[0] == wrapper_prefix) else parts
                if not rel_parts:
                    continue

                target = (staged_resolved / Path(*rel_parts)).resolve()
                try:
                    target.relative_to(staged_resolved)
                except ValueError as exc:
                    raise ZipSecurityError(
                        f"Resolved ZIP path escapes destination directory: {info.filename!r}"
                    ) from exc

                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, target.open("wb") as dst:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        actual_written += len(chunk)
                        if actual_written > max_uncompressed_bytes:
                            raise ZipSecurityError(
                                f"ZIP archive extracted size exceeds limit "
                                f"({max_uncompressed_bytes} bytes)"
                            )
                        dst.write(chunk)

                # Restore safe Unix file permissions (ensuring 0755 on executable scripts)
                perm_bits = (info.external_attr >> 16) & 0o777
                if perm_bits:
                    safe_mode = perm_bits | 0o600
                    if safe_mode & 0o100:
                        safe_mode |= 0o755
                    os.chmod(target, safe_mode)
                elif (
                    rel_parts[0] == "scripts"
                    and target.suffix.lower() not in NON_EXECUTABLE_SCRIPT_EXTS
                ):
                    os.chmod(target, 0o755)

            _swap_directory_atomically(staged_dir, dest_dir)
        except Exception:
            if staged_dir.exists():
                shutil.rmtree(staged_dir, ignore_errors=True)
            raise


def copy_directory_atomically(src_dir: Path, dest_dir: Path) -> None:
    """Atomically mirror ``src_dir`` into ``dest_dir``, preserving file permissions."""
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    staged_dir = Path(
        tempfile.mkdtemp(dir=str(dest_dir.parent), prefix=f".stage-{dest_dir.name}-")
    )
    try:
        shutil.copytree(src_dir, staged_dir, dirs_exist_ok=True, symlinks=False)
        _swap_directory_atomically(staged_dir, dest_dir)
    except Exception:
        if staged_dir.exists():
            shutil.rmtree(staged_dir, ignore_errors=True)
        raise


def _extract_mcp_endpoint_entry(raw_entry: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a single MCP server object (flat config or Agent Registry McpServer).

    Extracts ``url`` from Agent Registry ``interfaces[*].url`` when absent at top
    level (defaulting ``authProviderType`` to ``"google_credentials"``), and drops
    entries that lack any usable ``url``, ``serverUrl``, or ``command``.
    """
    entry = {
        k: copy.deepcopy(v)
        for k, v in raw_entry.items()
        if k not in ("name", "id", "mcpServerId", "server_id")
    }
    url = entry.get("url") or entry.get("serverUrl")
    command = entry.get("command")

    if isinstance(url, str) and url.strip():
        return entry
    if isinstance(command, str) and command.strip():
        return entry

    # Agent Registry v1 McpServer shape: {"interfaces": [{"url": "https://...", "protocolBinding": "..."}]}
    interfaces = raw_entry.get("interfaces")
    if isinstance(interfaces, list):
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            iface_url = iface.get("url")
            if isinstance(iface_url, str) and iface_url.strip():
                normalized: dict[str, Any] = {
                    "url": iface_url.strip(),
                    "authProviderType": (
                        entry.get("authProviderType")
                        if entry.get("authProviderType") == "google_credentials"
                        else "google_credentials"
                    ),
                }
                for opt_key in ("oauth", "headers", "bearer_token_env_var"):
                    if opt_key in entry and entry[opt_key] is not None:
                        normalized[opt_key] = copy.deepcopy(entry[opt_key])
                return normalized

    return None


def _normalize_mcp_servers_map(raw: Any) -> dict[str, dict[str, Any]]:
    """Normalize MCP server definitions from either a dict or list of objects."""
    result: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            base_name = key.rsplit("/", 1)[-1] if "/" in key else key
            normalized = _extract_mcp_endpoint_entry(value)
            if normalized is None:
                continue
            result[sanitize_mcp_key(base_name)] = normalized
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            raw_name = (
                item.get("name")
                or item.get("mcpServerId")
                or item.get("id")
                or item.get("server_id")
            )
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            base_name = raw_name.rsplit("/", 1)[-1] if "/" in raw_name else raw_name
            normalized = _extract_mcp_endpoint_entry(item)
            if normalized is None:
                continue
            result[sanitize_mcp_key(base_name)] = normalized
    return result


def parse_catalog_response(
    payload: dict[str, Any],
    middleware_url: str,
    *,
    marketplace_name_override: str | None = None,
) -> CatalogPayload:
    """Parse and normalize a Middleware ``GET /v1/catalog`` JSON document."""
    if not isinstance(payload, dict):
        raise CatalogFetchError("Middleware /v1/catalog did not return a JSON object")

    mkt_raw = payload.get("marketplace") if isinstance(payload.get("marketplace"), dict) else {}
    mkt_name = (
        marketplace_name_override
        or mkt_raw.get("name")
        or payload.get("marketplace_name")
        or payload.get("name")
        or DEFAULT_MARKETPLACE_NAME
    )
    mkt_display = (
        mkt_raw.get("display_name")
        or mkt_raw.get("displayName")
        or payload.get("display_name")
        or payload.get("displayName")
        or DEFAULT_MARKETPLACE_DISPLAY_NAME
    )
    mkt_desc = (
        mkt_raw.get("description")
        or payload.get("description")
        or DEFAULT_MARKETPLACE_DESCRIPTION
    )
    owner_raw = mkt_raw.get("owner") or payload.get("owner")
    owner = (
        {str(k): str(v) for k, v in owner_raw.items() if isinstance(v, str)}
        if isinstance(owner_raw, dict) and owner_raw.get("name")
        else dict(DEFAULT_MARKETPLACE_OWNER)
    )
    mkt_version = str(mkt_raw.get("version") or payload.get("version") or "1.0.0")

    marketplace = MarketplaceMetadata(
        name=str(mkt_name),
        display_name=str(mkt_display),
        description=str(mkt_desc),
        owner=owner,
        version=mkt_version,
    )

    raw_plugins = payload.get("plugins")
    if raw_plugins is None:
        raw_plugins = []
    if not isinstance(raw_plugins, list):
        raise CatalogFetchError("Middleware /v1/catalog 'plugins' field must be a JSON list")

    base_url = middleware_url.rstrip("/") + "/"
    plugins: list[CatalogPlugin] = []
    seen: set[str] = set()

    for idx, item in enumerate(raw_plugins):
        if not isinstance(item, dict):
            raise CatalogFetchError(f"Catalog plugins[{idx}] must be a JSON object")
        pname = validate_safe_identifier(
            item.get("name"),
            label=f"Catalog plugins[{idx}].name",
            error_cls=CatalogFetchError,
        )
        if pname in seen:
            raise CatalogFetchError(f"Catalog contains duplicate plugin name: {pname!r}")
        seen.add(pname)

        source_obj = item.get("source") if isinstance(item.get("source"), dict) else {}
        raw_url = (
            item.get("bundleUrl")
            or item.get("bundle_url")
            or item.get("download_url")
            or item.get("archive_url")
            or item.get("zip_url")
            or source_obj.get("url")
            or f"/v1/plugins/{pname}.zip"
        )
        download_url = (
            str(raw_url)
            if str(raw_url).startswith(("http://", "https://"))
            else urljoin(base_url, str(raw_url).lstrip("/"))
        )

        sha = (
            item.get("sha256")
            or item.get("bundle_sha256")
            or item.get("archive_sha256")
            or item.get("zip_sha256")
            or source_obj.get("sha256")
        )
        sha_str = str(sha).strip().lower() if isinstance(sha, str) and sha.strip() else None

        display_name = str(
            item.get("display_name") or item.get("displayName") or pname
        )
        version = str(item.get("version") or "1.0.0")
        description = str(item.get("description") or f"Enterprise plugin {pname}")
        category = str(item.get("category") or "Enterprise")

        raw_tags = item.get("tags") or item.get("keywords") or []
        tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []

        raw_skills = item.get("skills") or []
        skills = [str(s) for s in raw_skills] if isinstance(raw_skills, list) else []

        plugin_mcp = _normalize_mcp_servers_map(
            item.get("mcp_servers") or item.get("mcpServers")
        )

        plugins.append(
            CatalogPlugin(
                name=pname,
                display_name=display_name,
                version=version,
                description=description,
                download_url=download_url,
                sha256=sha_str,
                category=category,
                tags=tags,
                skills=skills,
                mcp_servers=plugin_mcp,
            )
        )

    top_mcp = _normalize_mcp_servers_map(
        payload.get("mcp_servers") or payload.get("mcpServers")
    )

    return CatalogPayload(marketplace=marketplace, plugins=plugins, mcp_servers=top_mcp)


class HubClient:
    """HTTP client for Middleware ``/v1/catalog`` and ``/v1/plugins/{name}.zip``."""

    def __init__(
        self,
        middleware_url: str,
        *,
        token: str | None = None,
        quota_project: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.middleware_url = middleware_url.rstrip("/")
        self.token = token
        self.quota_project = quota_project
        self.extra_headers = dict(extra_headers) if extra_headers else {}
        self.timeout = timeout
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HubClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _base_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, application/zip, */*"}
        headers.update(self.extra_headers)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.quota_project:
            headers["x-goog-user-project"] = self.quota_project
        return headers

    def _get_with_safe_redirects(self, url: str) -> httpx.Response:
        """Issue GET following redirects while stripping auth/x-goog-* headers cross-origin."""
        current_url = url
        headers = self._base_headers()
        origin = (urlparse(current_url).scheme, urlparse(current_url).netloc)

        for _ in range(MAX_REDIRECTS + 1):
            try:
                response = self._client.get(
                    current_url, headers=headers, follow_redirects=False
                )
            except httpx.HTTPError as exc:
                raise CatalogFetchError(f"HTTP request failed for {current_url}: {exc}") from exc

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise CatalogFetchError(
                        f"Redirect response ({response.status_code}) missing Location header"
                    )
                next_url = urljoin(current_url, location)
                parsed_next = urlparse(next_url)
                if parsed_next.scheme not in ("http", "https") or not parsed_next.netloc:
                    raise CatalogFetchError(
                        f"Redirect target uses unsupported scheme or missing host: {next_url!r}"
                    )
                next_origin = (parsed_next.scheme, parsed_next.netloc)
                if next_origin != origin:
                    headers = {
                        k: v
                        for k, v in headers.items()
                        if k.lower() != "authorization" and not k.lower().startswith("x-goog-")
                    }
                current_url = next_url
                origin = next_origin
                continue

            return response

        raise CatalogFetchError(f"Too many redirects while fetching {url}")

    def fetch_catalog(
        self, *, marketplace_name_override: str | None = None
    ) -> CatalogPayload:
        """Fetch and parse ``GET /v1/catalog`` from Middleware."""
        catalog_url = f"{self.middleware_url}/v1/catalog"
        response = self._get_with_safe_redirects(catalog_url)
        if response.status_code != 200:
            raise CatalogFetchError(
                f"GET {catalog_url} failed with HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise CatalogFetchError(f"GET {catalog_url} returned invalid JSON: {exc}") from exc

        return parse_catalog_response(
            data, self.middleware_url, marketplace_name_override=marketplace_name_override
        )

    def download_plugin_zip(self, plugin: CatalogPlugin) -> tuple[bytes, str]:
        """Download ``plugin.download_url`` and verify its SHA-256 digest."""
        response = self._get_with_safe_redirects(plugin.download_url)
        if response.status_code != 200:
            raise CatalogFetchError(
                f"GET {plugin.download_url} failed with HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        content = response.content
        expected_sha = (
            plugin.sha256
            or response.headers.get("x-content-sha256")
            or response.headers.get("x-bundle-sha256")
        )
        if expected_sha:
            verified_sha = verify_sha256(content, expected_sha, label=f"plugin '{plugin.name}'")
        else:
            verified_sha = compute_sha256(content)
        return content, verified_sha


def fetch_catalog_with_failover(
    primary_url: str,
    *,
    fallback_url: str | None = None,
    marketplace_name_override: str | None = None,
    token: str | None = None,
    quota_project: str | None = None,
    extra_headers: dict[str, str] | None = None,
    bootstrap_mcp_only: bool = False,
    http_client: httpx.Client | None = None,
    http_transport: httpx.BaseTransport | None = None,
    on_failover: Callable[[str, str, Exception], None] | None = None,
) -> tuple[CatalogPayload, list[tuple[CatalogPlugin, bytes, str]], str, bool]:
    """Fetch catalog and validated plugin ZIPs with automatic primary-to-fallback failover.

    Retries against ``fallback_url`` when fetching from ``primary_url`` raises
    :class:`CatalogFetchError` or :class:`httpx.HTTPError`. Security/integrity
    violations (:class:`IntegrityError`, :class:`ZipSecurityError`) are never
    swallowed.

    Returns ``(catalog, downloaded, active_middleware_url, failover_used)``.
    """
    primary = primary_url.strip().rstrip("/")
    fallback = (
        fallback_url.strip().rstrip("/")
        if isinstance(fallback_url, str) and fallback_url.strip()
        else None
    )

    def _fetch_from(
        target_url: str,
    ) -> tuple[CatalogPayload, list[tuple[CatalogPlugin, bytes, str]]]:
        with HubClient(
            target_url,
            token=token,
            quota_project=quota_project,
            extra_headers=extra_headers,
            client=http_client,
            transport=http_transport,
        ) as client:
            fetched_catalog = client.fetch_catalog(
                marketplace_name_override=marketplace_name_override
            )
            fetched_downloads: list[tuple[CatalogPlugin, bytes, str]] = []
            if not bootstrap_mcp_only:
                for plugin in fetched_catalog.plugins:
                    validate_safe_identifier(
                        plugin.name, label="plugin.name", error_cls=CatalogFetchError
                    )
                    zip_bytes, verified_sha = client.download_plugin_zip(plugin)
                    # Validate ZIP traversal / symlink / agent-name safety in memory before disk writes
                    validate_zip_bytes(zip_bytes)
                    fetched_downloads.append((plugin, zip_bytes, verified_sha))
            return fetched_catalog, fetched_downloads

    try:
        catalog, downloaded = _fetch_from(primary)
        return catalog, downloaded, primary, False
    except (CatalogFetchError, httpx.HTTPError) as exc:
        if not fallback or fallback == primary:
            raise
        if on_failover is not None:
            logger.debug(
                "Primary middleware '%s' failed (%s); failing over to fallback middleware '%s'.",
                primary,
                exc,
                fallback,
            )
            on_failover(primary, fallback, exc)
        else:
            logger.warning(
                "Primary middleware '%s' failed (%s); failing over to fallback middleware '%s'.",
                primary,
                exc,
                fallback,
            )
        catalog, downloaded = _fetch_from(fallback)
        return catalog, downloaded, fallback, True


def _extract_mcp_servers_from_bundle(plugin_dir: Path) -> dict[str, dict[str, Any]]:
    """Read MCP server configurations from an extracted plugin directory."""
    servers: dict[str, dict[str, Any]] = {}
    # Order from lowest to highest precedence so Antigravity mcp_config.json overrides generic fields
    for filename in (".mcp.json", "mcp.json", "mcp_config.json"):
        cfg_path = plugin_dir / filename
        if not cfg_path.is_file():
            continue
        try:
            data = load_json_object(cfg_path)
        except (HubSyncError, json.JSONDecodeError):
            continue
        raw_servers = data.get("mcpServers")
        if not isinstance(raw_servers, dict):
            continue
        for raw_key, raw_cfg in raw_servers.items():
            if not isinstance(raw_key, str) or not isinstance(raw_cfg, dict):
                continue
            key = sanitize_mcp_key(raw_key)
            merged = dict(servers.get(key, {}))
            merged.update(copy.deepcopy(raw_cfg))
            servers[key] = merged
    return servers


def _extract_agents_from_bundle(plugin_dir: Path, plugin_name: str) -> list[AgentSpec]:
    """Discover subagent definitions from ``agents/*.md`` or Copilot agent files."""
    discovered: dict[str, AgentSpec] = {}
    candidate_files: list[Path] = []

    agents_dir = plugin_dir / "agents"
    if agents_dir.is_dir():
        candidate_files.extend(sorted(agents_dir.glob("*.md")))

    copilot_agents_dir = plugin_dir / "com.github.copilot" / "agents"
    if copilot_agents_dir.is_dir():
        candidate_files.extend(sorted(copilot_agents_dir.glob("*.agent.md")))

    for md_path in candidate_files:
        text = md_path.read_text(encoding="utf-8")
        frontmatter, body, fm_err = parse_frontmatter(text)
        if fm_err or frontmatter is None:
            continue
        name = frontmatter.get("name")
        description = frontmatter.get("description")
        if not isinstance(name, str) or not name.strip():
            continue
        agent_name = validate_safe_identifier(
            name,
            label=f"Agent frontmatter 'name' in {md_path.name}",
            error_cls=ZipSecurityError,
        )
        if agent_name in discovered:
            continue
        desc_str = (
            description.strip()
            if isinstance(description, str) and description.strip()
            else f"Agent provided by {plugin_name}"
        )
        instructions = body.strip() or desc_str
        discovered[agent_name] = AgentSpec(
            name=agent_name,
            description=desc_str,
            instructions=instructions,
            plugin_name=plugin_name,
            source_path=str(md_path.relative_to(plugin_dir)),
        )

    return list(discovered.values())


def inspect_extracted_plugin(
    plugin_dir: Path,
    catalog_plugin: CatalogPlugin,
    verified_sha256: str,
) -> ExtractedPlugin:
    """Inspect an extracted plugin directory to gather manifests, skills, MCP servers, and agents."""
    ag_manifest: dict[str, Any] = {}
    root_manifest: dict[str, Any] = {}

    ag_path = plugin_dir / ".antigravity-plugin" / "plugin.json"
    if ag_path.is_file():
        ag_manifest = load_json_object(ag_path)
    root_path = plugin_dir / "plugin.json"
    if root_path.is_file():
        root_manifest = load_json_object(root_path)

    display_name = str(
        ag_manifest.get("displayName")
        or ag_manifest.get("display_name")
        or catalog_plugin.display_name
        or catalog_plugin.name
    )
    version = str(
        ag_manifest.get("version")
        or root_manifest.get("version")
        or catalog_plugin.version
        or "1.0.0"
    )
    description = str(
        ag_manifest.get("description")
        or root_manifest.get("description")
        or catalog_plugin.description
        or f"Enterprise plugin {catalog_plugin.name}"
    )

    # Category from Codex extension metadata if available, else catalog
    openai_ext = (
        root_manifest.get("extensions", {}).get("com.openai", {})
        if isinstance(root_manifest.get("extensions"), dict)
        else {}
    )
    category = str(
        (openai_ext.get("category") if isinstance(openai_ext, dict) else None)
        or catalog_plugin.category
        or "Enterprise"
    )

    keywords = (
        root_manifest.get("keywords")
        or ag_manifest.get("keywords")
        or catalog_plugin.tags
        or []
    )
    tags = [str(k) for k in keywords] if isinstance(keywords, list) else []

    # Discover skills from skills/<name>/SKILL.md
    skills_dir = plugin_dir / "skills"
    skills: list[str] = []
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                skills.append(entry.name)
    if not skills and catalog_plugin.skills:
        skills = list(catalog_plugin.skills)

    # Merge bundle MCP servers + catalog-declared MCP servers
    mcp_servers = copy.deepcopy(catalog_plugin.mcp_servers)
    for key, cfg in _extract_mcp_servers_from_bundle(plugin_dir).items():
        merged = dict(mcp_servers.get(key, {}))
        merged.update(cfg)
        mcp_servers[key] = merged

    agents = _extract_agents_from_bundle(plugin_dir, catalog_plugin.name)

    return ExtractedPlugin(
        name=catalog_plugin.name,
        display_name=display_name,
        version=version,
        description=description,
        sha256=verified_sha256,
        archive_url=catalog_plugin.download_url,
        install_path=plugin_dir.resolve(),
        category=category,
        tags=tags,
        skills=skills,
        mcp_servers=mcp_servers,
        agents=agents,
    )


def merge_mcp_servers_dict(
    existing_servers: dict[str, Any],
    desired_servers: dict[str, dict[str, Any]],
    previous_owned_keys: set[str],
    *,
    allow_prune: bool,
) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    """Ownership-safe merge for MCP server dictionaries.

    Guarantees:
    1. Pre-existing entries NOT in ``previous_owned_keys`` are treated as
       user-authored and are NEVER overwritten, claimed, or pruned.
    2. Hydrated entries that already exist preserve user-set override fields
       (``disabled``, ``disabledTools``, ``disabled_tools``, ``enabled``,
       ``enabled_tools``, ``autoApprove``, ``alwaysAllow``).
    3. Previously owned entries absent from ``desired_servers`` are removed
       ONLY when ``allow_prune`` is True (clean full sync).

    Returns:
        ``(merged_servers, new_owned_keys, skipped_user_conflicts, pruned_keys)``
    """
    merged: dict[str, Any] = copy.deepcopy(existing_servers)
    new_owned: set[str] = set()
    skipped_conflicts: list[str] = []
    pruned: list[str] = []

    for server_key, desired_cfg in desired_servers.items():
        if server_key in merged and server_key not in previous_owned_keys:
            # User-authored entry exists with this key — NEVER overwrite or take ownership
            skipped_conflicts.append(server_key)
            continue

        updated_cfg = copy.deepcopy(desired_cfg)
        existing_entry = merged.get(server_key)
        if isinstance(existing_entry, dict):
            for preserved_key in PRESERVED_MCP_USER_KEYS:
                if preserved_key in existing_entry:
                    updated_cfg[preserved_key] = copy.deepcopy(existing_entry[preserved_key])

        merged[server_key] = updated_cfg
        new_owned.add(server_key)

    if allow_prune:
        for old_key in sorted(previous_owned_keys):
            if old_key not in desired_servers and old_key in merged:
                del merged[old_key]
                pruned.append(old_key)
    else:
        for old_key in previous_owned_keys:
            if old_key in merged:
                new_owned.add(old_key)

    return merged, sorted(new_owned), sorted(skipped_conflicts), pruned
