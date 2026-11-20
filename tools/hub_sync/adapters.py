"""Multi-IDE marketplace generators and config adapters for Acme Hub Sync.

Supports all 4 enterprise IDE/CLI surfaces:
1. Claude Code (``~/.claude/settings.json``, ``<hub>/.claude-plugin/marketplace.json``)
2. VS Code & GitHub Copilot (``<hub>/marketplace.json``, ``<hub>/.github/plugin/marketplace.json``,
   ``<hub>/vscode-settings-snippet.json``, optional ``~/.config/Code/User/settings.json``)
3. OpenAI Codex (``<hub>/.agents/plugins/marketplace.json``, ``~/.agents/plugins/marketplace.json``,
   ``~/.codex/agents/<agent>.toml``, ``~/.codex/config.toml``)
4. Google Antigravity (``<hub>/antigravity-marketplace.json``, ``~/.gemini/config/mcp_config.json``,
   ``~/.gemini/config/plugins/<plugin>``, optional ``agy plugin install``)
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.hub_sync.hydration import (
    AgentSpec,
    ExtractedPlugin,
    HubSyncError,
    MarketplaceMetadata,
    SyncManifest,
    ZipSecurityError,
    atomic_write_json,
    atomic_write_text,
    copy_directory_atomically,
    ensure_contained_path,
    load_json_object,
    merge_mcp_servers_dict,
    sanitize_mcp_key,
    validate_safe_identifier,
)

SUPPORTED_TOOLS: tuple[str, ...] = ("claude", "copilot", "codex", "antigravity")
RESERVED_MCP_ENV_KEYS = frozenset({"PLUGIN_ROOT", "PLUGIN_DATA"})
TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class AdapterReport:
    """Summary of changes, warnings, and follow-up commands from an IDE adapter."""

    tool: str
    written_files: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    executed_commands: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level marketplace catalog builders
# ---------------------------------------------------------------------------


def build_copilot_marketplace_doc(
    marketplace: MarketplaceMetadata, plugins: list[ExtractedPlugin]
) -> dict[str, Any]:
    """Build a VS Code / GitHub Copilot ``marketplace.json`` catalog."""
    return {
        "name": marketplace.name,
        "owner": dict(marketplace.owner),
        "metadata": {
            "description": marketplace.description,
            "version": marketplace.version,
            "pluginRoot": "./plugins",
        },
        "plugins": [
            {
                "name": plugin.name,
                "description": plugin.description,
                "version": plugin.version,
                "source": f"./plugins/{plugin.name}",
                "category": plugin.category,
                "tags": list(plugin.tags),
            }
            for plugin in plugins
        ],
    }


def build_claude_marketplace_doc(
    marketplace: MarketplaceMetadata, plugins: list[ExtractedPlugin]
) -> dict[str, Any]:
    """Build a local-directory Claude Code ``.claude-plugin/marketplace.json`` catalog."""
    return {
        "name": marketplace.name,
        "owner": dict(marketplace.owner),
        "metadata": {
            "description": marketplace.description,
            "version": marketplace.version,
            "pluginRoot": "./plugins",
        },
        "plugins": [
            {
                "name": plugin.name,
                "description": plugin.description,
                "version": plugin.version,
                "source": f"./plugins/{plugin.name}",
            }
            for plugin in plugins
        ],
    }


def build_codex_marketplace_doc(
    marketplace: MarketplaceMetadata, plugins: list[ExtractedPlugin]
) -> dict[str, Any]:
    """Build an OpenAI Codex ``.agents/plugins/marketplace.json`` catalog."""
    return {
        "name": marketplace.name,
        "interface": {
            "displayName": marketplace.display_name,
        },
        "plugins": [
            {
                "name": plugin.name,
                "source": {
                    "source": "local",
                    "path": f"./plugins/{plugin.name}",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": plugin.category or "Enterprise",
            }
            for plugin in plugins
        ],
    }


def _can_use_archive_source(url: str, sha256: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        return False
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        return False
    if not parsed.path.lower().endswith(".zip"):
        return False
    return bool(SHA256_HEX_RE.match(sha256))


def build_antigravity_marketplace_doc(
    marketplace: MarketplaceMetadata, plugins: list[ExtractedPlugin]
) -> dict[str, Any]:
    """Build a Google Antigravity archive-mode ``marketplace.json`` catalog."""
    entries: list[dict[str, Any]] = []
    for plugin in plugins:
        if _can_use_archive_source(plugin.archive_url, plugin.sha256):
            source: dict[str, Any] = {
                "type": "archive",
                "url": plugin.archive_url,
                "sha256": plugin.sha256,
            }
        else:
            source = {
                "type": "local",
                "path": str(plugin.install_path),
            }
        entries.append(
            {
                "name": plugin.name,
                "displayName": plugin.display_name,
                "description": plugin.description,
                "version": plugin.version,
                "source": source,
            }
        )
    return {
        "name": marketplace.name,
        "description": marketplace.description,
        "plugins": entries,
    }


def write_hub_marketplace_indexes(
    hub_dir: Path,
    marketplace: MarketplaceMetadata,
    plugins: list[ExtractedPlugin],
    *,
    dry_run: bool = False,
) -> list[Path]:
    """Write all top-level marketplace catalogs under ``<hub_dir>``."""
    copilot_doc = build_copilot_marketplace_doc(marketplace, plugins)
    claude_doc = build_claude_marketplace_doc(marketplace, plugins)
    codex_doc = build_codex_marketplace_doc(marketplace, plugins)
    antigravity_doc = build_antigravity_marketplace_doc(marketplace, plugins)

    targets: list[tuple[Path, dict[str, Any]]] = [
        (hub_dir / "marketplace.json", copilot_doc),
        (hub_dir / ".github" / "plugin" / "marketplace.json", copilot_doc),
        (hub_dir / ".claude-plugin" / "marketplace.json", claude_doc),
        (hub_dir / ".agents" / "plugins" / "marketplace.json", codex_doc),
        (hub_dir / "antigravity-marketplace.json", antigravity_doc),
    ]

    written: list[Path] = []
    for path, doc in targets:
        if not dry_run:
            atomic_write_json(path, doc)
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# MCP config normalizers per IDE surface
# ---------------------------------------------------------------------------


def collect_desired_mcp_servers(
    plugins: list[ExtractedPlugin],
    catalog_mcp_servers: dict[str, dict[str, Any]],
    middleware_url: str,
    *,
    bootstrap_mcp_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """Aggregate and sanitize all desired MCP servers from catalog and plugins.

    Merges catalog-level MCP servers first, then overlays plugin-declared MCP
    servers (when not in ``bootstrap_mcp_only`` mode). In ``bootstrap_mcp_only``
    mode, always ensures the hub MCP gateway (``<middleware_url>/mcp``) is
    registered unless an existing entry already targets that URL.
    """
    desired: dict[str, dict[str, Any]] = {}

    for raw_key, cfg in catalog_mcp_servers.items():
        key = sanitize_mcp_key(raw_key)
        desired[key] = copy.deepcopy(cfg)

    if not bootstrap_mcp_only:
        for plugin in plugins:
            for raw_key, cfg in plugin.mcp_servers.items():
                key = sanitize_mcp_key(raw_key)
                merged = dict(desired.get(key, {}))
                merged.update(copy.deepcopy(cfg))
                desired[key] = merged

    if bootstrap_mcp_only:
        hub_mcp_url = f"{middleware_url.rstrip('/')}/mcp"
        hub_mcp_norm = hub_mcp_url.rstrip("/")
        already_targets_hub = any(
            isinstance(cfg.get("url") or cfg.get("serverUrl"), str)
            and str(cfg.get("url") or cfg.get("serverUrl")).strip().rstrip("/") == hub_mcp_norm
            for cfg in desired.values()
        )
        if not already_targets_hub:
            desired["acme-agent-hub"] = {
                "url": hub_mcp_url,
                "authProviderType": "google_credentials",
                "bearer_token_env_var": "HUB_AUTH_TOKEN",
            }

    return desired


def format_antigravity_mcp_entry(raw_cfg: dict[str, Any]) -> dict[str, Any]:
    """Translate a normalized MCP server config into Antigravity ``mcp_config.json`` shape."""
    entry: dict[str, Any] = {}
    url = raw_cfg.get("url") or raw_cfg.get("serverUrl")
    command = raw_cfg.get("command")

    if isinstance(url, str) and url.strip():
        entry["url"] = url.strip()
        auth_provider = raw_cfg.get("authProviderType")
        if auth_provider == "google_credentials":
            entry["authProviderType"] = "google_credentials"
        if isinstance(raw_cfg.get("oauth"), dict):
            entry["oauth"] = copy.deepcopy(raw_cfg["oauth"])
        if isinstance(raw_cfg.get("headers"), dict) and raw_cfg["headers"]:
            entry["headers"] = {
                str(k): str(v) for k, v in raw_cfg["headers"].items() if isinstance(v, str)
            }
    elif isinstance(command, str) and command.strip():
        entry["command"] = command.strip()
        if isinstance(raw_cfg.get("args"), list):
            entry["args"] = [str(a) for a in raw_cfg["args"]]
        if isinstance(raw_cfg.get("env"), dict):
            clean_env = {
                str(k): str(v)
                for k, v in raw_cfg["env"].items()
                if str(k) not in RESERVED_MCP_ENV_KEYS and isinstance(v, str)
            }
            if clean_env:
                entry["env"] = clean_env
    return entry


def format_codex_mcp_entry(raw_cfg: dict[str, Any]) -> dict[str, Any]:
    """Translate a normalized MCP server config into Codex ``config.toml`` table fields."""
    entry: dict[str, Any] = {}
    url = raw_cfg.get("url") or raw_cfg.get("serverUrl")
    command = raw_cfg.get("command")

    if isinstance(url, str) and url.strip():
        entry["url"] = url.strip()
        bearer_env = raw_cfg.get("bearer_token_env_var")
        if isinstance(bearer_env, str) and bearer_env.strip():
            entry["bearer_token_env_var"] = bearer_env.strip()
        elif raw_cfg.get("authProviderType") == "google_credentials":
            entry["bearer_token_env_var"] = "HUB_AUTH_TOKEN"
        if isinstance(raw_cfg.get("headers"), dict) and raw_cfg["headers"]:
            entry["http_headers"] = {
                str(k): str(v) for k, v in raw_cfg["headers"].items() if isinstance(v, str)
            }
    elif isinstance(command, str) and command.strip():
        entry["command"] = command.strip()
        if isinstance(raw_cfg.get("args"), list):
            entry["args"] = [str(a) for a in raw_cfg["args"]]
        if isinstance(raw_cfg.get("env"), dict):
            clean_env = {
                str(k): str(v)
                for k, v in raw_cfg["env"].items()
                if str(k) not in RESERVED_MCP_ENV_KEYS and isinstance(v, str)
            }
            if clean_env:
                entry["env"] = clean_env
    return entry


# ---------------------------------------------------------------------------
# Pure-stdlib TOML renderer & section-preserving merger for OpenAI Codex
# ---------------------------------------------------------------------------


def _format_toml_key(key: str) -> str:
    if TOML_BARE_KEY_RE.match(key):
        return key
    return json.dumps(key)


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_format_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = [f"{_format_toml_key(str(k))} = {_format_toml_value(v)}" for k, v in value.items()]
        return "{ " + ", ".join(pairs) + " }"
    raise HubSyncError(f"Unsupported TOML value type: {type(value).__name__}")


def _render_toml_table(path_parts: tuple[str, ...], table: dict[str, Any]) -> str:
    header = ".".join(_format_toml_key(part) for part in path_parts)
    lines = [f"[{header}]"]
    nested_tables: list[tuple[str, dict[str, Any]]] = []

    for key, val in table.items():
        if val is None:
            continue
        if isinstance(val, dict):
            nested_tables.append((str(key), val))
        else:
            lines.append(f"{_format_toml_key(str(key))} = {_format_toml_value(val)}")

    blocks = ["\n".join(lines)]
    for sub_key, sub_dict in nested_tables:
        if sub_dict:
            blocks.append(_render_toml_table((*path_parts, sub_key), sub_dict))
    return "\n\n".join(blocks)


def _parse_toml_table_header(line: str) -> tuple[str, ...] | None:
    """Parse a single ``[table.path]`` line into its key tuple using ``tomllib``."""
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    try:
        probe = tomllib.loads(f"{stripped}\n__probe__ = 1\n")
    except tomllib.TOMLDecodeError:
        return None
    parts: list[str] = []
    node: Any = probe
    while isinstance(node, dict):
        keys = [k for k in node.keys() if k != "__probe__"]
        if len(keys) != 1:
            break
        key = keys[0]
        parts.append(key)
        node = node[key]
    return tuple(parts) if parts else None


def _split_toml_sections(text: str) -> tuple[list[str], list[tuple[tuple[str, ...] | None, list[str]]]]:
    """Split a TOML document into preamble lines and table section blocks.

    Tracks multiline string delimiters so ``[...]`` inside multiline strings is
    never misidentified as a table header.
    """
    preamble: list[str] = []
    sections: list[tuple[tuple[str, ...] | None, list[str]]] = []
    current_header: tuple[str, ...] | None = None
    current_lines: list[str] | None = None
    multiline_delim: str | None = None

    for line in text.splitlines(keepends=True):
        if multiline_delim is not None:
            if current_lines is None:
                preamble.append(line)
            else:
                current_lines.append(line)
            # Toggle off if delimiter appears an odd number of times on this line
            if line.count(multiline_delim) % 2 == 1:
                multiline_delim = None
            continue

        stripped = line.lstrip()
        is_new_section = False
        header_path: tuple[str, ...] | None = None

        if stripped.startswith("[["):
            is_new_section = True
            header_path = None
        elif stripped.startswith("["):
            parsed_hdr = _parse_toml_table_header(stripped)
            if parsed_hdr is not None:
                is_new_section = True
                header_path = parsed_hdr

        if is_new_section:
            if current_lines is not None:
                sections.append((current_header, current_lines))
            current_header = header_path
            current_lines = [line]
        else:
            if current_lines is None:
                preamble.append(line)
            else:
                current_lines.append(line)

            # Check for opening multiline string delimiter outside comments
            code_part = line.split("#", 1)[0]
            for delim in ('"""', "'''"):
                if code_part.count(delim) % 2 == 1:
                    multiline_delim = delim
                    break

    if current_lines is not None:
        sections.append((current_header, current_lines))

    return preamble, sections


def merge_codex_config_toml(
    existing_text: str,
    desired_servers: dict[str, dict[str, Any]],
    previous_owned_keys: set[str],
    *,
    allow_prune: bool,
) -> tuple[str, list[str], list[str], list[str]]:
    """Section-preserving merge of ``[mcp_servers.<key>]`` into ``~/.codex/config.toml``.

    Preserves all user-authored top-level keys, comments, other tables, and
    user-authored ``[mcp_servers.<user>]`` sections verbatim. Validates the
    merged output with ``tomllib.loads`` before returning.
    """
    existing_parsed: dict[str, Any] = {}
    if existing_text.strip():
        try:
            existing_parsed = tomllib.loads(existing_text)
        except tomllib.TOMLDecodeError as exc:
            raise HubSyncError(f"Existing Codex config.toml is invalid TOML: {exc}") from exc

    raw_existing_mcp = existing_parsed.get("mcp_servers")
    existing_mcp = raw_existing_mcp if isinstance(raw_existing_mcp, dict) else {}

    merged_mcp, new_owned, skipped_conflicts, pruned = merge_mcp_servers_dict(
        existing_mcp,
        desired_servers,
        previous_owned_keys,
        allow_prune=allow_prune,
    )

    managed_or_pruned = set(new_owned) | set(pruned)
    emitted_keys: set[str] = set()

    preamble, sections = _split_toml_sections(existing_text)
    chunks: list[str] = []

    preamble_text = "".join(preamble).rstrip()
    if preamble_text:
        chunks.append(preamble_text)

    for header_path, raw_lines in sections:
        if (
            header_path is not None
            and len(header_path) >= 2
            and header_path[0] == "mcp_servers"
            and header_path[1] in managed_or_pruned
        ):
            server_key = header_path[1]
            if server_key in new_owned and server_key not in emitted_keys:
                chunks.append(
                    _render_toml_table(("mcp_servers", server_key), merged_mcp[server_key])
                )
                emitted_keys.add(server_key)
            # Drop old managed block / sub-tables (or pruned server blocks)
            continue

        block_text = "".join(raw_lines).rstrip()
        if block_text:
            chunks.append(block_text)

    for server_key in new_owned:
        if server_key not in emitted_keys and server_key in merged_mcp:
            chunks.append(
                _render_toml_table(("mcp_servers", server_key), merged_mcp[server_key])
            )
            emitted_keys.add(server_key)

    final_text = ("\n\n".join(chunks).rstrip() + "\n") if chunks else ""

    # Self-verify syntactically valid TOML before returning
    if final_text.strip():
        try:
            tomllib.loads(final_text)
        except tomllib.TOMLDecodeError as exc:
            raise HubSyncError(f"Generated Codex config.toml failed TOML validation: {exc}") from exc

    return final_text, new_owned, skipped_conflicts, pruned


def render_codex_agent_toml(agent: AgentSpec) -> str:
    """Render a valid ``~/.codex/agents/<agent>.toml`` file verified by ``tomllib``."""
    safe_instructions = agent.instructions.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if safe_instructions.endswith('"'):
        safe_instructions = safe_instructions[:-1] + '\\"'

    candidate = (
        f"# Managed by acme-hub-sync (plugin: {agent.plugin_name})\n"
        f"name = {json.dumps(agent.name)}\n"
        f"description = {json.dumps(agent.description)}\n"
        f'instructions = """\n{safe_instructions}\n"""\n'
    )
    try:
        parsed = tomllib.loads(candidate)
        if parsed.get("instructions", "").strip() == agent.instructions.strip():
            return candidate
    except tomllib.TOMLDecodeError:
        pass

    fallback = (
        f"# Managed by acme-hub-sync (plugin: {agent.plugin_name})\n"
        f"name = {json.dumps(agent.name)}\n"
        f"description = {json.dumps(agent.description)}\n"
        f"instructions = {json.dumps(agent.instructions)}\n"
    )
    tomllib.loads(fallback)
    return fallback


# ---------------------------------------------------------------------------
# IDE Surface Adapters
# ---------------------------------------------------------------------------


def wire_claude(
    *,
    home_dir: Path,
    hub_dir: Path,
    marketplace: MarketplaceMetadata,
    plugins: list[ExtractedPlugin],
    manifest: SyncManifest,
    allow_prune: bool,
    desired_mcp_servers: dict[str, dict[str, Any]] | None = None,
    bootstrap_mcp_only: bool = False,
    dry_run: bool = False,
) -> tuple[AdapterReport, list[str]]:
    """Wire Claude Code ``~/.claude/settings.json`` to the local hub marketplace."""
    report = AdapterReport(tool="claude")
    settings_path = home_dir / ".claude" / "settings.json"
    settings = load_json_object(settings_path, default={})

    extra_mkts = settings.get("extraKnownMarketplaces")
    if not isinstance(extra_mkts, dict):
        extra_mkts = {}
    else:
        extra_mkts = copy.deepcopy(extra_mkts)

    extra_mkts[marketplace.name] = {
        "source": {
            "source": "directory",
            "path": str(hub_dir.resolve()),
        }
    }
    settings["extraKnownMarketplaces"] = extra_mkts

    enabled_plugins = settings.get("enabledPlugins")
    if not isinstance(enabled_plugins, dict):
        enabled_plugins = {}
    else:
        enabled_plugins = copy.deepcopy(enabled_plugins)

    previous_owned = set(manifest.owned_claude_plugins)
    new_owned: set[str] = set()

    if not bootstrap_mcp_only:
        desired_keys = {f"{plugin.name}@{marketplace.name}" for plugin in plugins}
        for plugin_key in sorted(desired_keys):
            if plugin_key in enabled_plugins and plugin_key not in previous_owned:
                # User pre-configured this plugin key — preserve their boolean value
                report.warnings.append(
                    f"Preserved pre-existing Claude enabledPlugins entry '{plugin_key}'"
                )
                continue
            if plugin_key in enabled_plugins and plugin_key in previous_owned:
                # Preserve user toggle (e.g. if user disabled it to False)
                new_owned.add(plugin_key)
                continue
            enabled_plugins[plugin_key] = True
            new_owned.add(plugin_key)

        if allow_prune:
            for old_key in sorted(previous_owned):
                if old_key not in desired_keys and old_key in enabled_plugins:
                    del enabled_plugins[old_key]
                    report.pruned.append(old_key)
        else:
            for old_key in previous_owned:
                if old_key in enabled_plugins:
                    new_owned.add(old_key)
    else:
        for old_key in previous_owned:
            if old_key in enabled_plugins:
                new_owned.add(old_key)

    settings["enabledPlugins"] = enabled_plugins

    if not dry_run:
        atomic_write_json(settings_path, settings)

    report.written_files.append(str(settings_path))

    if bootstrap_mcp_only and desired_mcp_servers:
        claude_mcp_servers: dict[str, dict[str, Any]] = {}
        for raw_key, cfg in sorted(desired_mcp_servers.items()):
            url = cfg.get("url") or cfg.get("serverUrl")
            if isinstance(url, str) and url.strip():
                key = sanitize_mcp_key(raw_key)
                entry: dict[str, Any] = {
                    "type": "streamable-http",
                    "url": url.strip(),
                }
                if cfg.get("bearer_token_env_var") or cfg.get("authProviderType") == "google_credentials":
                    env_var = str(cfg.get("bearer_token_env_var") or "HUB_AUTH_TOKEN")
                    entry["headers"] = {"Authorization": f"Bearer ${{{env_var}}}"}
                claude_mcp_servers[key] = entry
                report.commands.append(
                    f"claude mcp add --transport http {key} {url.strip()}"
                )
        if claude_mcp_servers:
            claude_mcp_snippet = hub_dir.resolve() / "claude-bootstrap-mcp.json"
            if not dry_run:
                atomic_write_json(claude_mcp_snippet, {"mcpServers": claude_mcp_servers})
            report.written_files.append(str(claude_mcp_snippet))

    report.commands.append("claude plugin list")
    return report, sorted(new_owned)


def wire_copilot(
    *,
    home_dir: Path,
    hub_dir: Path,
    marketplace: MarketplaceMetadata,
    plugins: list[ExtractedPlugin],
    manifest: SyncManifest,
    allow_prune: bool,
    desired_mcp_servers: dict[str, dict[str, Any]] | None = None,
    apply_vscode: bool = False,
    bootstrap_mcp_only: bool = False,
    dry_run: bool = False,
) -> tuple[AdapterReport, list[str]]:
    """Generate VS Code settings snippet + Copilot CLI commands (and optionally apply settings)."""
    report = AdapterReport(tool="copilot")
    hub_resolved = hub_dir.resolve()
    hub_uri = hub_resolved.as_uri()

    desired_locations = (
        {str(plugin.install_path.resolve()): True for plugin in plugins}
        if not bootstrap_mcp_only
        else {}
    )

    snippet_path = hub_resolved / "vscode-settings-snippet.json"
    snippet_doc = {
        "chat.plugins.enabled": True,
        "chat.plugins.marketplaces": [hub_uri],
        "chat.pluginLocations": desired_locations,
    }
    if not dry_run:
        atomic_write_json(snippet_path, snippet_doc)
    report.written_files.append(str(snippet_path))

    if bootstrap_mcp_only and desired_mcp_servers:
        vscode_mcp_servers: dict[str, dict[str, Any]] = {}
        for raw_key, cfg in sorted(desired_mcp_servers.items()):
            url = cfg.get("url") or cfg.get("serverUrl")
            if isinstance(url, str) and url.strip():
                vscode_mcp_servers[sanitize_mcp_key(raw_key)] = {
                    "type": "http",
                    "url": url.strip(),
                }
        if vscode_mcp_servers:
            vscode_mcp_snippet_path = hub_resolved / "vscode-mcp-snippet.json"
            if not dry_run:
                atomic_write_json(vscode_mcp_snippet_path, {"servers": vscode_mcp_servers})
            report.written_files.append(str(vscode_mcp_snippet_path))

    previous_owned_locs = set(manifest.owned_vscode_plugin_locations)
    new_owned_locs: set[str] = set(previous_owned_locs)

    if apply_vscode:
        vscode_settings_path = home_dir / ".config" / "Code" / "User" / "settings.json"
        settings = load_json_object(vscode_settings_path, default={})
        settings["chat.plugins.enabled"] = True

        existing_mkts = settings.get("chat.plugins.marketplaces")
        mkts_list = list(existing_mkts) if isinstance(existing_mkts, list) else []
        if hub_uri not in mkts_list:
            mkts_list.append(hub_uri)
        settings["chat.plugins.marketplaces"] = mkts_list

        existing_locs = settings.get("chat.pluginLocations")
        locs_map = copy.deepcopy(existing_locs) if isinstance(existing_locs, dict) else {}

        if not bootstrap_mcp_only:
            new_owned_locs = set()
            for loc_path in sorted(desired_locations):
                if loc_path in locs_map and loc_path not in previous_owned_locs:
                    report.warnings.append(
                        f"Preserved pre-existing VS Code chat.pluginLocations entry '{loc_path}'"
                    )
                    continue
                if loc_path not in locs_map:
                    locs_map[loc_path] = True
                new_owned_locs.add(loc_path)

            if allow_prune:
                for old_loc in sorted(previous_owned_locs):
                    if old_loc not in desired_locations and old_loc in locs_map:
                        del locs_map[old_loc]
                        report.pruned.append(old_loc)
            else:
                for old_loc in previous_owned_locs:
                    if old_loc in locs_map:
                        new_owned_locs.add(old_loc)

        settings["chat.pluginLocations"] = locs_map
        if not dry_run:
            atomic_write_json(vscode_settings_path, settings)
        report.written_files.append(str(vscode_settings_path))

    report.commands.append(f"copilot plugin marketplace add {hub_resolved}")
    for plugin in plugins:
        report.commands.append(f"copilot plugin install {plugin.name}@{marketplace.name}")
    return report, sorted(new_owned_locs)


def wire_codex(
    *,
    home_dir: Path,
    hub_dir: Path,
    marketplace: MarketplaceMetadata,
    plugins: list[ExtractedPlugin],
    desired_mcp_servers: dict[str, dict[str, Any]],
    manifest: SyncManifest,
    allow_prune: bool,
    bootstrap_mcp_only: bool = False,
    dry_run: bool = False,
) -> tuple[AdapterReport, list[str], list[str], list[str]]:
    """Wire OpenAI Codex marketplace, local plugin symlinks, agents TOML, and ``config.toml``."""
    report = AdapterReport(tool="codex")

    # 1. ~/.agents/plugins/marketplace.json + local symlinks under ~/.agents/plugins/plugins/
    codex_mkt_dir = home_dir / ".agents" / "plugins"
    codex_mkt_path = codex_mkt_dir / "marketplace.json"
    codex_plugins_dir = codex_mkt_dir / "plugins"

    previous_owned_codex_plugins = set(manifest.owned_codex_plugins)
    new_owned_codex_plugins: set[str] = set()

    if not bootstrap_mcp_only:
        desired_codex_entries: dict[str, dict[str, Any]] = {}
        for plugin in plugins:
            safe_pname = validate_safe_identifier(
                plugin.name, label="Codex plugin name", error_cls=ZipSecurityError
            )
            desired_codex_entries[safe_pname] = {
                "name": safe_pname,
                "source": {"source": "local", "path": f"./plugins/{safe_pname}"},
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": plugin.category or "Enterprise",
            }

        existing_mkt = load_json_object(codex_mkt_path, default={})
        existing_entries = (
            existing_mkt.get("plugins") if isinstance(existing_mkt.get("plugins"), list) else []
        )

        # Preserve any user-authored Codex marketplace plugin entries
        merged_entries: list[dict[str, Any]] = []
        user_entry_names: set[str] = set()
        for entry in existing_entries:
            if not isinstance(entry, dict):
                continue
            ename = entry.get("name")
            if not isinstance(ename, str):
                continue
            if ename not in previous_owned_codex_plugins:
                merged_entries.append(entry)
                user_entry_names.add(ename)
            elif not allow_prune and ename not in desired_codex_entries:
                merged_entries.append(entry)
                new_owned_codex_plugins.add(ename)

        plugins_to_link: list[ExtractedPlugin] = []
        for plugin in plugins:
            safe_pname = validate_safe_identifier(
                plugin.name, label="Codex plugin name", error_cls=ZipSecurityError
            )
            link_path = ensure_contained_path(
                codex_plugins_dir,
                codex_plugins_dir / safe_pname,
                label=f"Codex plugin path '{safe_pname}'",
                error_cls=ZipSecurityError,
                follow_final_symlink=False,
            )
            if safe_pname in user_entry_names:
                report.warnings.append(
                    f"Preserved pre-existing Codex marketplace plugin entry '{safe_pname}'"
                )
                continue
            if (link_path.exists() or link_path.is_symlink()) and (
                safe_pname not in previous_owned_codex_plugins
            ):
                report.warnings.append(
                    f"Preserved pre-existing user Codex plugin path '{link_path}'"
                )
                continue

            merged_entries.append(desired_codex_entries[safe_pname])
            plugins_to_link.append(plugin)
            new_owned_codex_plugins.add(safe_pname)

        codex_mkt_doc = {
            "name": marketplace.name,
            "interface": {"displayName": marketplace.display_name},
            "plugins": merged_entries,
        }

        if not dry_run:
            codex_plugins_dir.mkdir(parents=True, exist_ok=True)
            for plugin in plugins_to_link:
                safe_pname = validate_safe_identifier(
                    plugin.name, label="Codex plugin name", error_cls=ZipSecurityError
                )
                link_path = ensure_contained_path(
                    codex_plugins_dir,
                    codex_plugins_dir / safe_pname,
                    label=f"Codex plugin symlink '{safe_pname}'",
                    error_cls=ZipSecurityError,
                    follow_final_symlink=False,
                )
                if link_path.is_symlink():
                    link_path.unlink()
                elif link_path.exists():
                    # Previously Codex-owned directory — safe to replace with symlink
                    shutil.rmtree(link_path)
                os.symlink(plugin.install_path.resolve(), link_path)

            if allow_prune:
                desired_names = set(desired_codex_entries.keys())
                for old_name in sorted(previous_owned_codex_plugins):
                    if old_name not in desired_names:
                        safe_old = validate_safe_identifier(
                            old_name, label="Codex pruned plugin name", error_cls=ZipSecurityError
                        )
                        old_link = ensure_contained_path(
                            codex_plugins_dir,
                            codex_plugins_dir / safe_old,
                            label=f"Codex pruned plugin '{safe_old}'",
                            error_cls=ZipSecurityError,
                            follow_final_symlink=False,
                        )
                        if old_link.is_symlink():
                            old_link.unlink()
                        elif old_link.is_dir():
                            shutil.rmtree(old_link)
                        report.pruned.append(f"codex-plugin:{safe_old}")

            atomic_write_json(codex_mkt_path, codex_mkt_doc)
        elif allow_prune:
            desired_names = set(desired_codex_entries.keys())
            for old_name in sorted(previous_owned_codex_plugins):
                if old_name not in desired_names:
                    report.pruned.append(f"codex-plugin:{old_name}")
        report.written_files.append(str(codex_mkt_path))
    else:
        new_owned_codex_plugins = set(previous_owned_codex_plugins)

    # 2. Materialize ~/.codex/agents/<agent>.toml
    agents_dir = home_dir / ".codex" / "agents"
    previous_owned_agents = set(manifest.owned_codex_agents)
    new_owned_agents: set[str] = set()

    if not bootstrap_mcp_only:
        desired_agents: dict[str, AgentSpec] = {}
        for plugin in plugins:
            for agent in plugin.agents:
                safe_agent_name = validate_safe_identifier(
                    agent.name,
                    label=f"Codex agent name in plugin '{plugin.name}'",
                    error_cls=ZipSecurityError,
                )
                desired_agents[safe_agent_name] = agent

        for agent_name, agent_spec in sorted(desired_agents.items()):
            agent_file = ensure_contained_path(
                agents_dir,
                agents_dir / f"{agent_name}.toml",
                label=f"Codex agent file '{agent_name}.toml'",
                error_cls=ZipSecurityError,
                follow_final_symlink=False,
            )
            if (agent_file.exists() or agent_file.is_symlink()) and (
                agent_name not in previous_owned_agents
            ):
                report.warnings.append(
                    f"Preserved pre-existing user Codex agent file '{agent_file}'"
                )
                continue
            rendered = render_codex_agent_toml(agent_spec)
            if not dry_run:
                atomic_write_text(agent_file, rendered)
            report.written_files.append(str(agent_file))
            new_owned_agents.add(agent_name)

        if allow_prune:
            for old_agent in sorted(previous_owned_agents):
                if old_agent not in desired_agents:
                    safe_old_agent = validate_safe_identifier(
                        old_agent, label="Codex pruned agent name", error_cls=ZipSecurityError
                    )
                    old_file = ensure_contained_path(
                        agents_dir,
                        agents_dir / f"{safe_old_agent}.toml",
                        label=f"Codex pruned agent file '{safe_old_agent}.toml'",
                        error_cls=ZipSecurityError,
                        follow_final_symlink=False,
                    )
                    if old_file.is_file() or old_file.is_symlink():
                        if not dry_run:
                            old_file.unlink()
                        report.pruned.append(f"codex-agent:{safe_old_agent}")
        else:
            for old_agent in previous_owned_agents:
                if (agents_dir / f"{old_agent}.toml").is_file():
                    new_owned_agents.add(old_agent)
    else:
        for old_agent in previous_owned_agents:
            if (agents_dir / f"{old_agent}.toml").is_file():
                new_owned_agents.add(old_agent)

    # 3. Merge [mcp_servers.<sanitized_key>] into ~/.codex/config.toml
    codex_config_path = home_dir / ".codex" / "config.toml"
    existing_toml = codex_config_path.read_text(encoding="utf-8") if codex_config_path.is_file() else ""
    codex_desired_mcp = {
        sanitize_mcp_key(k): format_codex_mcp_entry(v)
        for k, v in desired_mcp_servers.items()
        if format_codex_mcp_entry(v)
    }

    merged_toml, new_owned_codex_mcp, skipped_mcp, pruned_mcp = merge_codex_config_toml(
        existing_toml,
        codex_desired_mcp,
        set(manifest.owned_codex_mcp_servers),
        allow_prune=allow_prune and not bootstrap_mcp_only,
    )
    for key in skipped_mcp:
        report.warnings.append(
            f"Preserved user-authored Codex [mcp_servers.{key}] in {codex_config_path}"
        )
    for key in pruned_mcp:
        report.pruned.append(f"codex-mcp:{key}")

    if not dry_run:
        atomic_write_text(codex_config_path, merged_toml)
    report.written_files.append(str(codex_config_path))
    report.commands.append("codex mcp list")

    return report, new_owned_codex_mcp, sorted(new_owned_agents), sorted(new_owned_codex_plugins)


def wire_antigravity(
    *,
    home_dir: Path,
    hub_dir: Path,
    plugins: list[ExtractedPlugin],
    desired_mcp_servers: dict[str, dict[str, Any]],
    manifest: SyncManifest,
    allow_prune: bool,
    bootstrap_mcp_only: bool = False,
    no_exec: bool = False,
    dry_run: bool = False,
) -> tuple[AdapterReport, list[str], list[str]]:
    """Wire Google Antigravity ``mcp_config.json`` and installed plugins."""
    report = AdapterReport(tool="antigravity")

    # 1. Merge sanitized MCP entries into ~/.gemini/config/mcp_config.json
    mcp_config_path = home_dir / ".gemini" / "config" / "mcp_config.json"
    existing_doc = load_json_object(mcp_config_path, default={"mcpServers": {}})
    existing_servers = existing_doc.get("mcpServers")
    if not isinstance(existing_servers, dict):
        existing_servers = {}

    ag_desired_mcp = {
        sanitize_mcp_key(k): format_antigravity_mcp_entry(v)
        for k, v in desired_mcp_servers.items()
        if format_antigravity_mcp_entry(v)
    }

    merged_servers, new_owned_mcp, skipped_mcp, pruned_mcp = merge_mcp_servers_dict(
        existing_servers,
        ag_desired_mcp,
        set(manifest.owned_mcp_servers),
        allow_prune=allow_prune and not bootstrap_mcp_only,
    )
    for key in skipped_mcp:
        report.warnings.append(
            f"Preserved user-authored Antigravity MCP server '{key}' in {mcp_config_path}"
        )
    for key in pruned_mcp:
        report.pruned.append(f"antigravity-mcp:{key}")

    updated_doc = copy.deepcopy(existing_doc)
    updated_doc["mcpServers"] = merged_servers
    if not dry_run:
        atomic_write_json(mcp_config_path, updated_doc)
    report.written_files.append(str(mcp_config_path))

    # 2. Mirror/install plugins into ~/.gemini/config/plugins/<plugin-name>
    ag_plugins_root = home_dir / ".gemini" / "config" / "plugins"
    previous_owned_ag = set(manifest.owned_antigravity_plugins)
    new_owned_ag: set[str] = set()
    agy_bin = shutil.which("agy") if not no_exec else None

    if not bootstrap_mcp_only:
        desired_names = {p.name for p in plugins}
        for plugin in plugins:
            safe_pname = validate_safe_identifier(
                plugin.name, label="Antigravity plugin name", error_cls=ZipSecurityError
            )
            dest_plugin_dir = ensure_contained_path(
                ag_plugins_root,
                ag_plugins_root / safe_pname,
                label=f"Antigravity plugin path '{safe_pname}'",
                error_cls=ZipSecurityError,
                follow_final_symlink=False,
            )
            if (dest_plugin_dir.exists() or dest_plugin_dir.is_symlink()) and (
                safe_pname not in previous_owned_ag
            ):
                report.warnings.append(
                    f"Preserved pre-existing user Antigravity plugin '{safe_pname}' "
                    f"at {dest_plugin_dir}"
                )
                continue

            if not dry_run:
                copy_directory_atomically(plugin.install_path, dest_plugin_dir)
                if agy_bin:
                    cmd = [agy_bin, "plugin", "install", str(plugin.install_path)]
                    try:
                        proc = subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=60,
                        )
                    except (subprocess.TimeoutExpired, OSError) as exc:
                        report.executed_commands.append(
                            {
                                "command": cmd,
                                "returncode": None,
                                "stdout": "",
                                "stderr": str(exc),
                            }
                        )
                        report.warnings.append(
                            f"`agy plugin install` failed to execute cleanly: {exc}"
                        )
                    else:
                        report.executed_commands.append(
                            {
                                "command": cmd,
                                "returncode": proc.returncode,
                                "stdout": proc.stdout.strip(),
                                "stderr": proc.stderr.strip(),
                            }
                        )
                        if proc.returncode != 0:
                            report.warnings.append(
                                f"`agy plugin install` exited with {proc.returncode}: "
                                f"{proc.stderr.strip() or proc.stdout.strip()}"
                            )

            report.written_files.append(str(dest_plugin_dir))
            new_owned_ag.add(safe_pname)

        if allow_prune:
            for old_name in sorted(previous_owned_ag):
                if old_name not in desired_names:
                    safe_old = validate_safe_identifier(
                        old_name, label="Antigravity pruned plugin name", error_cls=ZipSecurityError
                    )
                    old_dir = ensure_contained_path(
                        ag_plugins_root,
                        ag_plugins_root / safe_old,
                        label=f"Antigravity pruned plugin '{safe_old}'",
                        error_cls=ZipSecurityError,
                        follow_final_symlink=False,
                    )
                    if old_dir.exists() or old_dir.is_symlink():
                        if not dry_run:
                            if old_dir.is_symlink():
                                old_dir.unlink()
                            else:
                                shutil.rmtree(old_dir)
                        report.pruned.append(f"antigravity-plugin:{safe_old}")
        else:
            for old_name in previous_owned_ag:
                if (ag_plugins_root / old_name).exists():
                    new_owned_ag.add(old_name)
    else:
        for old_name in previous_owned_ag:
            if (ag_plugins_root / old_name).exists():
                new_owned_ag.add(old_name)

    # Exact verification commands
    report.commands.append("agy plugin list --json")
    agent_names = sorted({agent.name for plugin in plugins for agent in plugin.agents})
    for agent_name in agent_names:
        report.commands.append(f"agy --agent {agent_name}")

    return report, new_owned_mcp, sorted(new_owned_ag)
