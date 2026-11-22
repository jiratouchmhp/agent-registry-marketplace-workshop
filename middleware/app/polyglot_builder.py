"""Polyglot Single-Bundle ZIP builder for multi-IDE zero-friction coexistence.

Groups verified Skills + linked MCP Servers + linked A2A Agents by
`frontmatter.metadata.plugin_name` and produces a deterministic ZIP bundle that
passes validators for ALL FIVE targets simultaneously:
- Agent Plugins 1.0 (`plugin.json`, `mcp.json`, `skills/`)
- Google Antigravity (`.antigravity-plugin/plugin.json`, `mcp_config.json`, `assets/logo.svg`,
  `skills/`, `agents/`, `rules/`)
- Claude Code (`.claude-plugin/plugin.json`, `.mcp.json`, `agents/`, `skills/`)
- VS Code & GitHub Copilot (`plugin.json`, `mcp.json`, `com.github.copilot/agents/*.agent.md`)
- OpenAI Codex (`plugin.json` with `extensions["com.openai"]`, `.codex-plugin/plugin.json`)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from middleware.app.config import MiddlewareSettings
from middleware.app.security_verifier import VerificationResult

DISALLOWED_BRAND_PREFIXES = ("antigravity", "gemini", "claude", "cursor")
FIXED_ZIP_TIMESTAMP = (2025, 1, 1, 0, 0, 0)
REGULAR_FILE_ATTR = (stat.S_IFREG | 0o644) << 16
EXECUTABLE_FILE_ATTR = (stat.S_IFREG | 0o755) << 16

DEFAULT_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128">\n'
    '  <defs>\n'
    '    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">\n'
    '      <stop offset="0%" stop-color="#1a73e8"/>\n'
    '      <stop offset="100%" stop-color="#0d47a1"/>\n'
    '    </linearGradient>\n'
    '  </defs>\n'
    '  <rect width="128" height="128" rx="24" fill="url(#bg)"/>\n'
    '  <path d="M64 26 L98 44 V70 C98 89 83 102 64 108 C45 102 30 89 30 70 V44 Z" '
    'fill="none" stroke="#ffffff" stroke-width="6" stroke-linejoin="round"/>\n'
    '  <path d="M49 66 L60 77 L81 54" fill="none" stroke="#8ab4f8" '
    'stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>\n'
    '</svg>\n'
)


@dataclass
class VerifiedSkillArtifact:
    """An approved skill paired with its revision metadata, verification result, and ZIP bytes."""

    skill: dict[str, Any]
    revision: dict[str, Any]
    verification: VerificationResult
    zip_bytes: bytes


@dataclass
class BuiltPluginBundle:
    """Result of deterministically building a Polyglot Single-Bundle ZIP."""

    name: str
    display_name: str
    version: str
    description: str
    sha256: str
    zip_bytes: bytes
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    agents: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    category: str = "Cloud Operations"


def sanitize_kebab_name(raw: str, *, fallback: str = "enterprise-plugin") -> str:
    """Normalize a string to lowercase kebab-case without disallowed brand prefixes."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    for prefix in DISALLOWED_BRAND_PREFIXES:
        if cleaned == prefix:
            cleaned = f"enterprise-{cleaned}"
            break
        if cleaned.startswith(f"{prefix}-") or cleaned.startswith(prefix):
            cleaned = f"enterprise-{cleaned}"
            break
    cleaned = cleaned[:64].strip("-")
    return cleaned or fallback


def sanitize_display_name(raw: str, plugin_name: str) -> str:
    """Ensure displayName is non-empty and does not start with a disallowed brand prefix."""
    candidate = raw.strip() if raw and raw.strip() else " ".join(
        word.upper() if word in ("gcp", "sql", "bq", "iam", "mcp", "a2a") else word.capitalize()
        for word in plugin_name.split("-")
    )
    lowered = candidate.lower()
    for prefix in DISALLOWED_BRAND_PREFIXES:
        if lowered.startswith(prefix):
            candidate = f"Enterprise {candidate}"
            break
    return candidate


def sanitize_antigravity_mcp_key(raw: str) -> str:
    """Sanitize MCP server key to `^[a-zA-Z0-9_-]{1,20}$` (no dots, <=20 chars)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
    if not cleaned:
        cleaned = "mcp-server"
    if len(cleaned) > 20:
        cleaned = cleaned[:20].rstrip("-_")
    return cleaned or "mcp-server"


def _resource_basename(name: str) -> str:
    return name.rsplit("/", 1)[-1] if "/" in name else name


def _match_service_resource(ref: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    needle = ref.strip()
    if not needle:
        return None
    for item in items:
        name = str(item.get("name", ""))
        source_svc = str(item.get("sourceService", ""))
        urn = str(item.get("mcpServerId") or item.get("agentId") or "")
        if needle in (name, source_svc, urn):
            return item
        if _resource_basename(name) == needle or _resource_basename(source_svc) == needle:
            return item
        if any(val.endswith(f":{needle}") for val in (name, urn) if val):
            return item
    return None


def _validate_zip_member(info: zipfile.ZipInfo) -> str:
    """Strictly validate a ZIP member path and mode against zip-slip, backslashes, NUL, and symlinks."""
    unix_mode = info.external_attr >> 16
    if stat.S_ISLNK(unix_mode):
        raise ValueError(f"Symlink entry is forbidden in ZIP archive: {info.filename!r}")

    raw_name = info.filename
    if not raw_name or "\x00" in raw_name:
        raise ValueError(f"Empty or NUL-containing path is forbidden in ZIP archive: {raw_name!r}")
    if "\\" in raw_name:
        raise ValueError(f"Backslash path is forbidden in ZIP archive: {raw_name!r}")
    if raw_name.startswith("/") or re.match(r"^[A-Za-z]:", raw_name):
        raise ValueError(f"Absolute or drive-letter path is forbidden in ZIP archive: {raw_name!r}")

    parts = PurePosixPath(raw_name).parts
    if any(part == ".." for part in parts):
        raise ValueError(f"Path traversal ('..') is forbidden in ZIP archive: {raw_name!r}")

    clean_parts = [part for part in parts if part not in ("", ".")]
    if not clean_parts:
        raise ValueError(f"ZIP entry resolves to empty relative path: {raw_name!r}")
    return "/".join(clean_parts)


def _extract_skill_files(
    zip_bytes: bytes, skill_folder_name: str
) -> list[tuple[str, bytes, bool]]:
    """Extract files from a verified Skill ZIP under `skills/<skill_folder_name>/...`.

    Strictly rejects symlinks, NUL bytes, backslashes, absolute/drive paths, and `..` segments.
    Returns tuples of `(dest_rel_path, raw_bytes, is_executable)`.
    """
    if (
        not skill_folder_name
        or "/" in skill_folder_name
        or "\\" in skill_folder_name
        or "\x00" in skill_folder_name
        or skill_folder_name in (".", "..")
    ):
        raise ValueError(f"Invalid skill folder name: {skill_folder_name!r}")

    extracted: list[tuple[str, bytes, bool]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        all_infos = sorted(zf.infolist(), key=lambda i: i.filename)
        validated_files: list[tuple[zipfile.ZipInfo, str]] = []
        for info in all_infos:
            if info.is_dir():
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError(f"Symlink directory is forbidden in ZIP archive: {info.filename!r}")
                raw_dir = info.filename.rstrip("/")
                if raw_dir:
                    _validate_zip_member(
                        zipfile.ZipInfo(filename=raw_dir)
                    )
                continue
            clean_name = _validate_zip_member(info)
            validated_files.append((info, clean_name))

        skill_md_paths = [
            clean for _info, clean in validated_files if PurePosixPath(clean).name == "SKILL.md"
        ]
        top_prefix = ""
        if len(skill_md_paths) == 1:
            parts = PurePosixPath(skill_md_paths[0]).parts
            if len(parts) == 2:
                top_prefix = f"{parts[0]}/"

        for info, clean_name in validated_files:
            rel = clean_name
            if top_prefix and rel.startswith(top_prefix):
                rel = rel[len(top_prefix) :]
            if not rel:
                continue
            rel_parts = PurePosixPath(rel).parts
            if any(part == ".." for part in rel_parts):
                raise ValueError(f"Path traversal ('..') is forbidden in skill archive: {info.filename!r}")

            raw = zf.read(info)
            dest_path = f"skills/{skill_folder_name}/{rel}"

            unix_mode = info.external_attr >> 16
            in_scripts = "scripts" in rel_parts[:-1]
            ext = PurePosixPath(rel).suffix.lower()
            is_exec = bool(unix_mode & 0o111) or (
                in_scripts and ext not in (".md", ".txt", ".json", ".yaml", ".yml", ".pyc")
            )
            extracted.append((dest_path, raw, is_exec))
    return extracted


def _render_yaml_frontmatter_md(frontmatter: dict[str, Any], body: str) -> bytes:
    yaml_str = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_str}\n---\n\n{body.strip()}\n".encode("utf-8")


def _build_mcp_entries(
    settings: MiddlewareSettings,
    linked_mcp_servers: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return `(agent_plugins_mcp, claude_mcp, antigravity_mcp_config)` server maps."""
    ap_servers: dict[str, dict[str, Any]] = {}
    claude_servers: dict[str, dict[str, Any]] = {}
    ag_servers: dict[str, dict[str, Any]] = {}

    used_keys: set[str] = set()

    def _register(raw_key: str, url: str) -> None:
        base_key = sanitize_antigravity_mcp_key(raw_key)
        key = base_key
        suffix = 2
        while key in used_keys:
            trimmed = base_key[: max(1, 18 - len(str(suffix)))]
            key = f"{trimmed}-{suffix}"
            suffix += 1
        used_keys.add(key)

        # Agent Plugins 1.0 mcp.json: streamable-http, NO ${ENV} expansion in url/headers
        ap_servers[key] = {
            "type": "streamable-http",
            "url": url,
        }
        # Claude Code .mcp.json
        claude_servers[key] = {
            "type": "streamable-http",
            "url": url,
        }
        # Antigravity mcp_config.json: google_credentials auth, no variables[] so enabled immediately
        ag_servers[key] = {
            "url": url,
            "authProviderType": "google_credentials",
        }

    for srv in linked_mcp_servers:
        interfaces = srv.get("interfaces") or []
        url = ""
        for iface in interfaces:
            if isinstance(iface, dict) and iface.get("url"):
                url = str(iface["url"]).strip()
                break
        if not url:
            continue
        raw_id = _resource_basename(str(srv.get("name", "mcp-server")))
        if ":" in raw_id:
            raw_id = raw_id.rsplit(":", 1)[-1]
        _register(raw_id, url)

    # Always expose the governed Middleware MCP gateway (for load_verified_skill & catalog search)
    gateway_url = f"{settings.normalized_public_url}/mcp"
    _register("agent-hub-gw", gateway_url)

    return ap_servers, claude_servers, ag_servers


def build_plugin_bundles(
    *,
    verified_skills: list[VerifiedSkillArtifact],
    mcp_servers: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    settings: MiddlewareSettings,
) -> list[BuiltPluginBundle]:
    """Group approved skills + linked MCP servers + linked A2A agents and build deterministic ZIPs."""
    grouped: dict[str, list[VerifiedSkillArtifact]] = {}
    for artifact in verified_skills:
        fm = artifact.verification.frontmatter
        meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
        raw_plugin_name = str(meta.get("plugin_name") or fm.get("name") or "enterprise-plugin")
        plugin_name = sanitize_kebab_name(raw_plugin_name)
        grouped.setdefault(plugin_name, []).append(artifact)

    bundles: list[BuiltPluginBundle] = []
    for plugin_name in sorted(grouped.keys()):
        skill_artifacts = sorted(
            grouped[plugin_name],
            key=lambda a: str(a.verification.frontmatter.get("name", a.skill.get("name", ""))),
        )
        bundles.append(
            _build_single_bundle(
                plugin_name=plugin_name,
                skill_artifacts=skill_artifacts,
                all_mcp_servers=mcp_servers,
                all_agents=agents,
                settings=settings,
            )
        )
    return bundles


def _build_single_bundle(
    *,
    plugin_name: str,
    skill_artifacts: list[VerifiedSkillArtifact],
    all_mcp_servers: list[dict[str, Any]],
    all_agents: list[dict[str, Any]],
    settings: MiddlewareSettings,
) -> BuiltPluginBundle:
    display_name_raw = ""
    version = "1.0.0"
    license_name = "Apache-2.0"
    warnings: list[str] = []
    keywords_set: set[str] = {"gcp", "agent-registry"}
    linked_mcp_by_name: dict[str, dict[str, Any]] = {}
    linked_agents_by_name: dict[str, dict[str, Any]] = {}
    suggested_prompts: list[str] = []
    skill_names: list[str] = []

    for art in skill_artifacts:
        fm = art.verification.frontmatter
        meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
        fm_name = str(fm.get("name", _resource_basename(str(art.skill.get("name", "")))))
        skill_names.append(fm_name)
        warnings.extend(art.verification.warnings)

        if not display_name_raw and meta.get("plugin_display_name"):
            display_name_raw = str(meta["plugin_display_name"])
        if meta.get("plugin_version"):
            version = str(meta["plugin_version"])
        if fm.get("license"):
            license_name = str(fm["license"])

        for tok in fm_name.split("-"):
            if len(tok) >= 2:
                keywords_set.add(tok)

        if len(suggested_prompts) < 3:
            desc = str(fm.get("description", "")).strip().rstrip(".")
            if desc:
                suggested_prompts.append(f"Use {fm_name} to {desc[0].lower()}{desc[1:]}")

        mcp_ref = str(meta.get("mcp_service_id", "")).strip()
        if mcp_ref:
            for single_ref in (r.strip() for r in mcp_ref.split(",") if r.strip()):
                matched_mcp = _match_service_resource(single_ref, all_mcp_servers)
                if matched_mcp is not None:
                    linked_mcp_by_name[str(matched_mcp["name"])] = matched_mcp

        agent_ref = str(meta.get("agent_service_id", "")).strip()
        if agent_ref:
            for single_ref in (r.strip() for r in agent_ref.split(",") if r.strip()):
                matched_agent = _match_service_resource(single_ref, all_agents)
                if matched_agent is not None:
                    linked_agents_by_name[str(matched_agent["name"])] = matched_agent

    display_name = sanitize_display_name(display_name_raw, plugin_name)
    linked_mcp_servers = [linked_mcp_by_name[k] for k in sorted(linked_mcp_by_name)]
    linked_agents = [linked_agents_by_name[k] for k in sorted(linked_agents_by_name)]
    keywords = sorted(keywords_set)

    description = (
        f"{display_name} — governed enterprise plugin bundling "
        f"{len(skill_names)} verified skill(s)"
        + (f", {len(linked_mcp_servers)} MCP server(s)" if linked_mcp_servers else "")
        + (f", and {len(linked_agents)} A2A agent(s)" if linked_agents else "")
        + " from Google Cloud Agent Registry."
    )

    if not suggested_prompts:
        suggested_prompts = [f"Run {display_name} diagnostics on my GCP project"]
    suggested_prompts = suggested_prompts[:3]

    author_obj = {
        "name": settings.marketplace_owner_name,
        "email": settings.marketplace_owner_email,
        "url": (
            settings.marketplace_owner_url
            if settings.marketplace_owner_url.startswith("https://")
            else "https://acme.example.com/platform"
        ),
    }

    # Build file map: rel_path -> (bytes, is_executable)
    files: dict[str, tuple[bytes, bool]] = {}

    # 1. Root plugin.json (Agent Plugins 1.0 + Codex extensions["com.openai"])
    root_plugin_manifest = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": plugin_name,
        "version": version,
        "description": description,
        "author": author_obj,
        "license": license_name,
        "keywords": keywords,
        "extensions": {
            "com.openai": {
                "category": "Cloud Operations",
                "displayName": display_name,
            },
            "com.google.agentregistry": {
                "project": settings.gcp_project_id,
                "location": settings.gcp_location,
                "verifiedSkills": [
                    {
                        "name": art.skill.get("name"),
                        "revision": art.verification.revision_name,
                        "sha256": art.verification.sha256,
                        "kmsVerified": art.verification.kms_verified,
                    }
                    for art in skill_artifacts
                ],
            },
        },
    }
    files["plugin.json"] = (
        (json.dumps(root_plugin_manifest, indent=2, sort_keys=False) + "\n").encode("utf-8"),
        False,
    )

    # 2. .antigravity-plugin/plugin.json (strict Antigravity v1 schema, additionalProperties: false)
    antigravity_manifest = {
        "$schema": "https://antigravity.google/schemas/v1/plugin.json",
        "name": plugin_name,
        "displayName": display_name,
        "version": version,
        "description": description,
        "logo": "assets/logo.svg",
        "suggestedPrompts": suggested_prompts,
        "author": author_obj,
        "license": license_name,
        "keywords": keywords,
    }
    files[".antigravity-plugin/plugin.json"] = (
        (json.dumps(antigravity_manifest, indent=2, sort_keys=False) + "\n").encode("utf-8"),
        False,
    )

    # 3. .claude-plugin/plugin.json
    claude_manifest = {
        "name": plugin_name,
        "version": version,
        "description": description,
        "author": author_obj,
    }
    files[".claude-plugin/plugin.json"] = (
        (json.dumps(claude_manifest, indent=2, sort_keys=False) + "\n").encode("utf-8"),
        False,
    )

    # 4. .codex-plugin/plugin.json
    codex_manifest = {
        "name": plugin_name,
        "version": version,
        "description": description,
        "author": author_obj,
    }
    files[".codex-plugin/plugin.json"] = (
        (json.dumps(codex_manifest, indent=2, sort_keys=False) + "\n").encode("utf-8"),
        False,
    )

    # 5. assets/logo.svg (1:1 square icon)
    files["assets/logo.svg"] = (DEFAULT_LOGO_SVG.encode("utf-8"), False)

    # 6. MCP manifests (mcp.json, .mcp.json, mcp_config.json)
    ap_mcp_servers, claude_mcp_servers, ag_mcp_servers = _build_mcp_entries(
        settings, linked_mcp_servers
    )
    files["mcp.json"] = (
        (
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                    "mcpServers": ap_mcp_servers,
                },
                indent=2,
            )
            + "\n"
        ).encode("utf-8"),
        False,
    )
    files[".mcp.json"] = (
        (json.dumps({"mcpServers": claude_mcp_servers}, indent=2) + "\n").encode("utf-8"),
        False,
    )
    files["mcp_config.json"] = (
        (json.dumps({"mcpServers": ag_mcp_servers}, indent=2) + "\n").encode("utf-8"),
        False,
    )

    # 7. Verified Skills (skills/<skill-name>/SKILL.md + references/scripts preserving 0755)
    for art in skill_artifacts:
        folder_name = str(
            art.verification.frontmatter.get("name")
            or _resource_basename(str(art.skill.get("name", "skill")))
        )
        for dest_path, raw_bytes, is_exec in _extract_skill_files(art.zip_bytes, folder_name):
            files[dest_path] = (raw_bytes, is_exec)

    # 8. Multi-IDE Agent definitions (agents/<name>.md & com.github.copilot/agents/<name>.agent.md)
    mcp_tool_names: list[str] = [
        "search_catalog",
        "load_verified_skill",
        "list_governed_mcp_servers",
        "call_remote_a2a_agent",
    ]
    for srv in linked_mcp_servers:
        for tool in srv.get("tools") or []:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                mcp_tool_names.append(tool["name"])
    deduped_tools = sorted(set(mcp_tool_names))

    agents_to_render: list[dict[str, Any]] = list(linked_agents)
    if not agents_to_render:
        agents_to_render.append(
            {
                "name": f"projects/{settings.gcp_project_id}/locations/{settings.gcp_location}/agents/{plugin_name}-advisor",
                "displayName": f"{display_name} Advisor",
                "description": f"Governed specialist agent for {display_name}.",
                "interfaces": [],
                "skills": [],
            }
        )

    for agent_doc in agents_to_render:
        raw_agent_id = _resource_basename(str(agent_doc.get("name", "advisor")))
        if ":" in raw_agent_id:
            raw_agent_id = raw_agent_id.rsplit(":", 1)[-1]
        agent_slug = sanitize_kebab_name(raw_agent_id)
        agent_desc = str(
            agent_doc.get("description") or f"Governed specialist agent for {display_name}."
        ).strip()
        agent_ifaces = list(agent_doc.get("interfaces") or [])
        if not agent_ifaces:
            for proto in agent_doc.get("protocols") or []:
                if isinstance(proto, dict):
                    agent_ifaces.extend(proto.get("interfaces") or [])
        endpoint_line = (
            f"- Remote A2A endpoint: `{agent_ifaces[0]['url']}`\n"
            if agent_ifaces and isinstance(agent_ifaces[0], dict) and agent_ifaces[0].get("url")
            else ""
        )
        agent_body = (
            f"# {agent_doc.get('displayName') or agent_slug}\n\n"
            f"{agent_desc}\n\n"
            "## Governed Capabilities\n"
            f"- Bundled verified skills: {', '.join(skill_names)}\n"
            f"{endpoint_line}"
            "- Always prefer read-only investigation tools before proposing mutations.\n"
            "- Call `load_verified_skill` on the `agent-hub-gw` MCP server when fetching "
            "cryptographically attested instructions dynamically.\n"
        )

        # Antigravity & Claude Code agent definition
        ag_agent_fm = {
            "name": agent_slug,
            "description": agent_desc,
            "mainAgent": True,
            "subagent": True,
            "model": "inherit",
            "commandExecutionPolicy": "sandbox",
            "inheritMcp": True,
        }
        files[f"agents/{agent_slug}.md"] = (
            _render_yaml_frontmatter_md(ag_agent_fm, agent_body),
            False,
        )

        # GitHub Copilot custom agent definition
        copilot_agent_fm = {
            "name": agent_slug,
            "description": agent_desc,
            "tools": deduped_tools,
        }
        files[f"com.github.copilot/agents/{agent_slug}.agent.md"] = (
            _render_yaml_frontmatter_md(copilot_agent_fm, agent_body),
            False,
        )

    # 9. Guardrail rules (comma-separated globs string; always-on rules/AGENTS.md)
    is_finops = "finops" in plugin_name.lower()
    files["rules/AGENTS.md"] = (
        (
            f"# {display_name} Always-On Guardrails\n\n"
            "- Only invoke governed tools registered in Google Cloud Agent Registry.\n"
            "- Never emit raw credentials, API keys, or private keys in prompts or code.\n"
            "- Prefer read-only inspection and dry-run checks before proposing mutations.\n"
        ).encode("utf-8"),
        False,
    )
    if is_finops:
        rule_filename = "rules/finops-guardrails.md"
        rule_fm = {
            "trigger": "glob",
            "globs": "**/*.sql, **/*.tf, **/*.yaml",
        }
        rule_body = (
            "# FinOps & Infrastructure Guardrails\n\n"
            "- Require partition filters and column pruning on BigQuery SQL queries.\n"
            "- Never bypass IAP Unified Access Policies or Agent Gateway egress controls.\n"
            "- Run read-only diagnostics before proposing infrastructure or billing changes.\n"
        )
    else:
        rule_filename = f"rules/{plugin_name}-guardrails.md"
        rule_fm = {
            "trigger": "glob",
            "globs": "**/*.yaml, **/*.json, **/*.tf",
        }
        rule_body = (
            f"# {display_name} Governance Guardrails\n\n"
            "- Only invoke MCP tools and A2A agents verified in Google Cloud Agent Registry.\n"
            "- Never bypass IAP Unified Access Policies or Agent Gateway egress controls.\n"
            "- Validate changes with read-only dry-run diagnostics before applying mutations.\n"
        )
    files[rule_filename] = (
        _render_yaml_frontmatter_md(rule_fm, rule_body),
        False,
    )

    # 10. README.md
    readme_lines = [
        f"# {display_name}",
        "",
        description,
        "",
        f"- **Plugin Name**: `{plugin_name}`",
        f"- **Version**: `{version}`",
        f"- **Source Project**: `{settings.gcp_project_id}` (`{settings.gcp_location}`)",
        "",
        "## Verified Skills",
        "",
    ]
    for art in skill_artifacts:
        fm = art.verification.frontmatter
        readme_lines.append(
            f"- `{fm.get('name')}` (revision `{_resource_basename(art.verification.revision_name)}`, "
            f"sha256 `{art.verification.sha256[:12]}...`, kms_verified=`{art.verification.kms_verified}`)"
        )
    if linked_mcp_servers:
        readme_lines.extend(["", "## Governed MCP Servers", ""])
        for srv in linked_mcp_servers:
            iface_url = (srv.get("interfaces") or [{}])[0].get("url", "")
            readme_lines.append(f"- `{_resource_basename(str(srv.get('name', '')))}`: {iface_url}")
    if linked_agents:
        readme_lines.extend(["", "## Governed A2A Agents", ""])
        for ag in linked_agents:
            readme_lines.append(
                f"- `{_resource_basename(str(ag.get('name', '')))}`: {ag.get('description', '')}"
            )
    readme_lines.append("")
    files["README.md"] = ("\n".join(readme_lines).encode("utf-8"), False)

    # Assemble deterministic ZIP archive (sorted paths, fixed timestamp, Unix permissions).
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in sorted(files.keys()):
            raw_bytes, is_exec = files[rel_path]
            zinfo = zipfile.ZipInfo(filename=rel_path, date_time=FIXED_ZIP_TIMESTAMP)
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.create_system = 3  # Unix
            zinfo.external_attr = EXECUTABLE_FILE_ATTR if is_exec else REGULAR_FILE_ATTR
            zf.writestr(zinfo, raw_bytes)

    zip_bytes = zip_buf.getvalue()
    bundle_sha256 = hashlib.sha256(zip_bytes).hexdigest()

    return BuiltPluginBundle(
        name=plugin_name,
        display_name=display_name,
        version=version,
        description=description,
        sha256=bundle_sha256,
        zip_bytes=zip_bytes,
        skills=skill_names,
        mcp_servers=linked_mcp_servers,
        agents=linked_agents,
        warnings=warnings,
        keywords=keywords,
    )


def extract_zip_preserving_permissions(zip_bytes: bytes, dest_dir: Path) -> None:
    """Extract a Polyglot ZIP archive to `dest_dir`, restoring Unix executable bits (0755).

    Strictly rejects symlinks, NUL bytes, backslashes, absolute/drive paths, and `..` traversal.
    """
    dest_root = dest_dir.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError(
                        f"Symlink directory is forbidden in ZIP archive: {info.filename!r}"
                    )
                raw_dir = info.filename.rstrip("/")
                if raw_dir:
                    _validate_zip_member(zipfile.ZipInfo(filename=raw_dir))
                continue
            clean_rel = _validate_zip_member(info)
            target = (dest_root / clean_rel).resolve()
            target.relative_to(dest_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            unix_mode = (info.external_attr >> 16) & 0o777
            if unix_mode:
                os.chmod(target, unix_mode)
