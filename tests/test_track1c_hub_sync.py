"""Track 1C tests: hub-sync 1-command CLI, 4-IDE adapters, hydration safety, and starter exercises."""

from __future__ import annotations

import hashlib
import io
import json
import os
import py_compile
import stat
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from schemas import (
    validate_agent_plugin_bundle,
    validate_antigravity_bundle,
    validate_antigravity_marketplace,
    validate_claude_marketplace,
    validate_claude_plugin_bundle,
    validate_codex_marketplace,
    validate_codex_plugin_bundle,
    validate_copilot_marketplace,
    validate_copilot_plugin_bundle,
    validate_skill_md,
)
from tools.hub_sync import (
    CatalogFetchError,
    IntegrityError,
    ZipSecurityError,
    cli,
    get_hub_status,
    load_sync_manifest,
    safe_extract_zip,
    sanitize_mcp_key,
    sync_hub,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Polyglot 5-IDE sample plugin ZIP fixture builder
# ---------------------------------------------------------------------------


def _add_zip_file(
    zf: zipfile.ZipFile,
    arcname: str,
    content: str | bytes,
    mode: int = 0o644,
) -> None:
    info = zipfile.ZipInfo(arcname)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | mode) << 16
    data = content.encode("utf-8") if isinstance(content, str) else content
    zf.writestr(info, data)


def _build_polyglot_plugin_zip(
    *,
    plugin_name: str = "gcp-finops-toolkit",
    display_name: str = "GCP FinOps Toolkit",
    version: str = "1.0.0",
    mcp_server_key: str = "finops-mcp-server",
    mcp_server_url: str = "http://127.0.0.1:8091/mcp",
    agent_name: str = "finops-advisor",
    skills: tuple[str, ...] = ("cost-anomaly-triage", "bq-query-optimizer"),
    category: str = "Cloud Operations",
    wrap_top_dir: bool = False,
) -> tuple[bytes, str]:
    """Create a complete 5-IDE Polyglot plugin ZIP passing every schemas.validator."""
    prefix = f"{plugin_name}/" if wrap_top_dir else ""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # 1. Root Agent Plugins 1.0 manifest
        _add_zip_file(
            zf,
            f"{prefix}plugin.json",
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                    "name": plugin_name,
                    "version": version,
                    "description": f"{display_name} governed bundle.",
                    "author": {
                        "name": "Acme Platform Security",
                        "email": "platform-security@acme.example.com",
                        "url": "https://acme.example.com/platform",
                    },
                    "license": "Apache-2.0",
                    "keywords": ["gcp", "finops"],
                    "extensions": {"com.openai": {"category": category}},
                },
                indent=2,
            ),
        )

        # 2. Antigravity v1 manifest
        _add_zip_file(
            zf,
            f"{prefix}.antigravity-plugin/plugin.json",
            json.dumps(
                {
                    "$schema": "https://antigravity.google/schemas/v1/plugin.json",
                    "name": plugin_name,
                    "displayName": display_name,
                    "description": f"{display_name} governed bundle.",
                    "version": version,
                    "logo": "assets/logo.svg",
                    "suggestedPrompts": [
                        "Triage yesterday's BigQuery billing spike",
                        "Optimize this BigQuery SQL query",
                    ],
                    "author": {
                        "name": "Acme Platform Security",
                        "email": "platform-security@acme.example.com",
                        "url": "https://acme.example.com/platform",
                    },
                    "license": "Apache-2.0",
                    "keywords": ["gcp", "finops"],
                },
                indent=2,
            ),
        )

        # 3. Claude Code & Codex plugin manifests
        simple_manifest = json.dumps(
            {
                "name": plugin_name,
                "version": version,
                "description": f"{display_name} governed bundle.",
                "author": {
                    "name": "Acme Platform Security",
                    "email": "platform-security@acme.example.com",
                },
            },
            indent=2,
        )
        _add_zip_file(zf, f"{prefix}.claude-plugin/plugin.json", simple_manifest)
        _add_zip_file(zf, f"{prefix}.codex-plugin/plugin.json", simple_manifest)

        # 4. 1:1 square SVG logo
        _add_zip_file(
            zf,
            f"{prefix}assets/logo.svg",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
            '<rect width="64" height="64" rx="12" fill="#1a73e8"/></svg>\n',
        )

        # 5. MCP configs for Agent Plugins 1.0, Claude Code, and Antigravity
        _add_zip_file(
            zf,
            f"{prefix}mcp.json",
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                    "mcpServers": {
                        mcp_server_key: {
                            "type": "streamable-http",
                            "url": mcp_server_url,
                        }
                    },
                },
                indent=2,
            ),
        )
        _add_zip_file(
            zf,
            f"{prefix}.mcp.json",
            json.dumps(
                {
                    "mcpServers": {
                        mcp_server_key: {
                            "type": "streamable-http",
                            "url": mcp_server_url,
                        }
                    }
                },
                indent=2,
            ),
        )
        _add_zip_file(
            zf,
            f"{prefix}mcp_config.json",
            json.dumps(
                {
                    "mcpServers": {
                        mcp_server_key: {
                            "url": mcp_server_url,
                            "authProviderType": "google_credentials",
                        }
                    }
                },
                indent=2,
            ),
        )

        # 6. Skills (uppercase SKILL.md, folder == name, scalar string metadata)
        for skill_name in skills:
            _add_zip_file(
                zf,
                f"{prefix}skills/{skill_name}/SKILL.md",
                "---\n"
                f"name: {skill_name}\n"
                f"description: Governed workflow for {skill_name}.\n"
                "metadata:\n"
                f'  plugin_name: "{plugin_name}"\n'
                f'  plugin_version: "{version}"\n'
                "---\n\n"
                f"# {skill_name}\n\nFollow Acme FinOps guardrails.\n",
            )

        # 7. Rules, executable 0755 helper script, and named hooks.json
        _add_zip_file(
            zf,
            f"{prefix}rules/AGENTS.md",
            "# FinOps Guardrails\n\nNever mutate budgets without human approval.\n",
        )
        _add_zip_file(
            zf,
            f"{prefix}rules/sql-guard.md",
            "---\n"
            "trigger: glob\n"
            'globs: "**/*.sql, **/*.tf"\n'
            "---\n\n"
            "Require partition filters before executing BigQuery SQL.\n",
        )
        _add_zip_file(
            zf,
            f"{prefix}scripts/policy_guard.sh",
            "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
            mode=0o755,
        )
        _add_zip_file(
            zf,
            f"{prefix}hooks.json",
            json.dumps(
                {
                    "policy-guard": {
                        "enabled": True,
                        "PreToolUse": [
                            {
                                "matcher": f"mcp_{mcp_server_key}_.*",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "./scripts/policy_guard.sh",
                                        "timeout": 10,
                                    }
                                ],
                            }
                        ],
                    }
                },
                indent=2,
            ),
        )

        # 8. Subagent definitions (Antigravity/Claude + Copilot)
        _add_zip_file(
            zf,
            f"{prefix}agents/{agent_name}.md",
            "---\n"
            f"name: {agent_name}\n"
            f"description: Read-only FinOps advisor from {plugin_name}.\n"
            "subagent: true\n"
            "inheritMcp: true\n"
            "---\n\n"
            "Inspect billing anomalies and cite exact SKU deltas.\n",
        )
        _add_zip_file(
            zf,
            f"{prefix}com.github.copilot/agents/{agent_name}.agent.md",
            "---\n"
            f"name: {agent_name}\n"
            f"description: Read-only FinOps advisor from {plugin_name}.\n"
            "tools:\n"
            "  - mcp_finops_query_costs\n"
            "---\n\n"
            "Inspect billing anomalies and cite exact SKU deltas.\n",
        )

    zip_bytes = buf.getvalue()
    return zip_bytes, hashlib.sha256(zip_bytes).hexdigest()


def _make_mock_transport(
    plugins_spec: list[dict[str, Any]],
    zip_blobs: dict[str, bytes],
    *,
    catalog_status: int = 200,
    catalog_mcp_servers: dict[str, Any] | None = None,
    recorded_requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Build an httpx.MockTransport emulating the Enterprise Middleware API."""

    def handler(request: httpx.Request) -> httpx.Response:
        if recorded_requests is not None:
            recorded_requests.append(request)

        if request.url.path == "/v1/catalog":
            if catalog_status != 200:
                return httpx.Response(catalog_status, text="upstream catalog error")
            payload: dict[str, Any] = {
                "marketplace": {
                    "name": "acme-enterprise-marketplace",
                    "display_name": "Acme Enterprise Agent Marketplace",
                    "description": (
                        "Governed enterprise plugins, skills, MCP servers, and A2A agents "
                        "backed by Google Cloud Agent Registry."
                    ),
                    "owner": {
                        "name": "Acme Platform Security",
                        "email": "platform-security@acme.example.com",
                    },
                    "version": "1.0.0",
                },
                "plugins": plugins_spec,
            }
            if catalog_mcp_servers is not None:
                payload["mcp_servers"] = catalog_mcp_servers
            return httpx.Response(200, json=payload)

        if request.url.path.startswith("/v1/plugins/") and request.url.path.endswith(".zip"):
            plugin_name = request.url.path.removeprefix("/v1/plugins/").removesuffix(".zip")
            if plugin_name in zip_blobs:
                blob = zip_blobs[plugin_name]
                return httpx.Response(
                    200,
                    content=blob,
                    headers={
                        "Content-Type": "application/zip",
                        "X-Content-SHA256": hashlib.sha256(blob).hexdigest(),
                    },
                )
            return httpx.Response(404, text=f"Plugin {plugin_name} not found")

        return httpx.Response(404, text=f"Unhandled path: {request.url.path}")

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# 1. Full end-to-end 4-IDE sync + schema & TOML validation
# ---------------------------------------------------------------------------


def test_full_sync_validates_all_4_ides_and_bundles(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    zip_bytes, sha = _build_polyglot_plugin_zip(wrap_top_dir=True)
    recorded: list[httpx.Request] = []
    transport = _make_mock_transport(
        plugins_spec=[
            {
                "name": "gcp-finops-toolkit",
                "display_name": "GCP FinOps Toolkit",
                "version": "1.0.0",
                "description": "Enterprise GCP FinOps toolkit.",
                "sha256": sha,
                "download_url": "/v1/plugins/gcp-finops-toolkit.zip",
                "category": "Cloud Operations",
                "tags": ["gcp", "finops"],
                "skills": ["cost-anomaly-triage", "bq-query-optimizer"],
            }
        ],
        zip_blobs={"gcp-finops-toolkit": zip_bytes},
        recorded_requests=recorded,
    )

    result = sync_hub(
        middleware_url="http://localhost:8080",
        home_dir=home_dir,
        tools="all",
        token="test-bearer-token",
        apply_vscode=True,
        no_exec=True,
        http_transport=transport,
    )

    hub_dir = home_dir / ".acme-agent-hub"
    plugin_dir = hub_dir / "plugins" / "gcp-finops-toolkit"

    # Auth header was forwarded to Middleware
    assert any(r.headers.get("authorization") == "Bearer test-bearer-token" for r in recorded)

    # 1. Extracted Polyglot plugin passes EVERY schemas.validator (including 0755 scripts!)
    assert validate_agent_plugin_bundle(plugin_dir).valid
    ag_res = validate_antigravity_bundle(plugin_dir)
    assert ag_res.valid, f"Antigravity bundle errors: {ag_res.errors}"
    assert validate_claude_plugin_bundle(plugin_dir).valid
    assert validate_copilot_plugin_bundle(plugin_dir).valid
    assert validate_codex_plugin_bundle(plugin_dir).valid

    # 2. All generated marketplace catalogs pass schemas.validators
    assert validate_copilot_marketplace(hub_dir / "marketplace.json").valid
    assert validate_copilot_marketplace(hub_dir / ".github" / "plugin" / "marketplace.json").valid
    assert validate_claude_marketplace(hub_dir / ".claude-plugin" / "marketplace.json").valid
    assert validate_codex_marketplace(hub_dir / ".agents" / "plugins" / "marketplace.json").valid
    assert validate_codex_marketplace(home_dir / ".agents" / "plugins" / "marketplace.json").valid
    ag_mkt_res = validate_antigravity_marketplace(hub_dir / "antigravity-marketplace.json")
    assert ag_mkt_res.valid, f"Antigravity marketplace errors: {ag_mkt_res.errors}"

    # 3. Claude Code settings.json wired properly
    claude_settings = json.loads((home_dir / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert claude_settings["extraKnownMarketplaces"]["acme-enterprise-marketplace"]["source"] == {
        "source": "directory",
        "path": str(hub_dir.resolve()),
    }
    assert claude_settings["enabledPlugins"]["gcp-finops-toolkit@acme-enterprise-marketplace"] is True

    # 4. VS Code snippet & applied User/settings.json + Copilot CLI commands
    snippet = json.loads((hub_dir / "vscode-settings-snippet.json").read_text(encoding="utf-8"))
    assert snippet["chat.plugins.enabled"] is True
    assert snippet["chat.plugins.marketplaces"] == [hub_dir.resolve().as_uri()]
    assert snippet["chat.pluginLocations"][str(plugin_dir.resolve())] is True

    vscode_user = json.loads(
        (home_dir / ".config" / "Code" / "User" / "settings.json").read_text(encoding="utf-8")
    )
    assert vscode_user["chat.plugins.enabled"] is True
    assert hub_dir.resolve().as_uri() in vscode_user["chat.plugins.marketplaces"]
    assert any(
        cmd.startswith("copilot plugin marketplace add ")
        for cmd in result.adapter_reports["copilot"].commands
    )
    assert (
        "copilot plugin install gcp-finops-toolkit@acme-enterprise-marketplace"
        in result.adapter_reports["copilot"].commands
    )

    # 5. OpenAI Codex TOML configs & agents parse cleanly with tomllib.loads
    codex_cfg_text = (home_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
    codex_cfg = tomllib.loads(codex_cfg_text)
    assert codex_cfg["mcp_servers"]["finops-mcp-server"]["url"] == "http://127.0.0.1:8091/mcp"
    assert codex_cfg["mcp_servers"]["finops-mcp-server"]["bearer_token_env_var"] == "HUB_AUTH_TOKEN"

    agent_toml_path = home_dir / ".codex" / "agents" / "finops-advisor.toml"
    agent_toml = tomllib.loads(agent_toml_path.read_text(encoding="utf-8"))
    assert agent_toml["name"] == "finops-advisor"
    assert "FinOps advisor" in agent_toml["description"]
    assert "SKU deltas" in agent_toml["instructions"]

    # Local Codex symlink points to extracted plugin directory
    codex_link = home_dir / ".agents" / "plugins" / "plugins" / "gcp-finops-toolkit"
    assert codex_link.is_symlink()
    assert codex_link.resolve() == plugin_dir.resolve()

    # 6. Google Antigravity mcp_config.json, plugin mirror, and verification commands
    ag_mcp = json.loads(
        (home_dir / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8")
    )
    assert ag_mcp["mcpServers"]["finops-mcp-server"] == {
        "url": "http://127.0.0.1:8091/mcp",
        "authProviderType": "google_credentials",
    }
    ag_installed = home_dir / ".gemini" / "config" / "plugins" / "gcp-finops-toolkit"
    assert ag_installed.is_dir()
    assert validate_antigravity_bundle(ag_installed).valid
    assert "agy plugin list --json" in result.adapter_reports["antigravity"].commands
    assert "agy --agent finops-advisor" in result.adapter_reports["antigravity"].commands

    # 7. Ownership manifest & status reporting
    manifest = load_sync_manifest(hub_dir)
    assert manifest.last_sync_complete is True
    assert manifest.owned_plugins["gcp-finops-toolkit"]["sha256"] == sha

    status = get_hub_status(home_dir=home_dir)
    assert status["synced"] is True
    assert status["plugins"]["gcp-finops-toolkit"]["version"] == "1.0.0"
    assert status["plugins"]["gcp-finops-toolkit"]["sha256"] == sha
    assert status["plugins"]["gcp-finops-toolkit"]["installed_on_disk"] is True


# ---------------------------------------------------------------------------
# 2. User config preservation across initial sync & re-sync
# ---------------------------------------------------------------------------


def test_preserves_user_entries_and_disabled_overrides_across_resync(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    (home_dir / ".claude").mkdir(parents=True)
    (home_dir / ".gemini" / "config").mkdir(parents=True)
    (home_dir / ".codex" / "agents").mkdir(parents=True)
    (home_dir / ".config" / "Code" / "User").mkdir(parents=True)

    # Pre-existing user Claude Code settings
    (home_dir / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "theme": "dark",
                "extraKnownMarketplaces": {
                    "personal-mkt": {"source": {"source": "directory", "path": "/tmp/personal"}}
                },
                "enabledPlugins": {
                    "my-personal-plugin@personal-mkt": True,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Pre-existing user Antigravity mcp_config.json (including a colliding user server!)
    (home_dir / ".gemini" / "config" / "mcp_config.json").write_text(
        json.dumps(
            {
                "customTopLevel": "keep-me",
                "mcpServers": {
                    "my-local-mcp": {"url": "http://localhost:9999/mcp"},
                    "shared-name-mcp": {"url": "http://localhost:7777/user-owned"},
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Pre-existing user Codex config.toml & user custom agent
    (home_dir / ".codex" / "config.toml").write_text(
        '# Personal Codex settings\n'
        'model = "o3"\n\n'
        '[profiles.personal]\n'
        'model = "gpt-4.1"\n\n'
        '[mcp_servers.my-personal-srv]\n'
        'url = "https://personal.example.com/mcp"\n\n'
        '[mcp_servers.shared-name-mcp]\n'
        'url = "http://localhost:7777/user-owned"\n',
        encoding="utf-8",
    )
    (home_dir / ".codex" / "agents" / "user-custom-agent.toml").write_text(
        'name = "user-custom-agent"\ndescription = "Mine"\ninstructions = "Keep"\n',
        encoding="utf-8",
    )

    # Pre-existing VS Code settings
    (home_dir / ".config" / "Code" / "User" / "settings.json").write_text(
        json.dumps(
            {
                "editor.fontSize": 14,
                "chat.plugins.marketplaces": ["file:///opt/personal-marketplace"],
                "chat.pluginLocations": {"/opt/personal-plugin": True},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Sync #1: hydrates finops-mcp-server AND tries to publish colliding shared-name-mcp
    zip_v1, sha_v1 = _build_polyglot_plugin_zip(
        version="1.0.0", mcp_server_url="http://127.0.0.1:8091/mcp"
    )
    transport_v1 = _make_mock_transport(
        plugins_spec=[
            {
                "name": "gcp-finops-toolkit",
                "version": "1.0.0",
                "sha256": sha_v1,
            }
        ],
        zip_blobs={"gcp-finops-toolkit": zip_v1},
        catalog_mcp_servers={
            "shared-name-mcp": {"url": "http://127.0.0.1:8080/should-not-overwrite"}
        },
    )

    sync_hub(
        middleware_url="http://localhost:8080",
        home_dir=home_dir,
        tools="all",
        apply_vscode=True,
        no_exec=True,
        http_transport=transport_v1,
    )

    # Simulate user toggling `disabled: true` + `disabledTools` on the hydrated MCP server
    ag_mcp_path = home_dir / ".gemini" / "config" / "mcp_config.json"
    ag_mcp_data = json.loads(ag_mcp_path.read_text(encoding="utf-8"))
    ag_mcp_data["mcpServers"]["finops-mcp-server"]["disabled"] = True
    ag_mcp_data["mcpServers"]["finops-mcp-server"]["disabledTools"] = ["delete_budget"]
    ag_mcp_path.write_text(json.dumps(ag_mcp_data, indent=2), encoding="utf-8")

    # Simulate user adding `disabled = true` on hydrated Codex MCP server
    codex_cfg_path = home_dir / ".codex" / "config.toml"
    codex_cfg_path.write_text(
        codex_cfg_path.read_text(encoding="utf-8").replace(
            '[mcp_servers.finops-mcp-server]\n',
            '[mcp_servers.finops-mcp-server]\ndisabled = true\ndisabled_tools = ["delete_budget"]\n',
        ),
        encoding="utf-8",
    )

    # Simulate user toggling Claude plugin to False
    claude_path = home_dir / ".claude" / "settings.json"
    claude_data = json.loads(claude_path.read_text(encoding="utf-8"))
    claude_data["enabledPlugins"]["gcp-finops-toolkit@acme-enterprise-marketplace"] = False
    claude_path.write_text(json.dumps(claude_data, indent=2), encoding="utf-8")

    # Sync #2: plugin upgraded to v1.1.0 with updated MCP URL
    zip_v2, sha_v2 = _build_polyglot_plugin_zip(
        version="1.1.0", mcp_server_url="http://127.0.0.1:8091/v2/mcp"
    )
    transport_v2 = _make_mock_transport(
        plugins_spec=[
            {
                "name": "gcp-finops-toolkit",
                "version": "1.1.0",
                "sha256": sha_v2,
            }
        ],
        zip_blobs={"gcp-finops-toolkit": zip_v2},
        catalog_mcp_servers={
            "shared-name-mcp": {"url": "http://127.0.0.1:8080/should-not-overwrite"}
        },
    )

    sync_hub(
        middleware_url="http://localhost:8080",
        home_dir=home_dir,
        tools="all",
        apply_vscode=True,
        no_exec=True,
        http_transport=transport_v2,
    )

    # Verify Antigravity preserved user servers, colliding user server, AND disabled/disabledTools!
    ag_after = json.loads(ag_mcp_path.read_text(encoding="utf-8"))
    assert ag_after["customTopLevel"] == "keep-me"
    assert ag_after["mcpServers"]["my-local-mcp"] == {"url": "http://localhost:9999/mcp"}
    assert ag_after["mcpServers"]["shared-name-mcp"] == {
        "url": "http://localhost:7777/user-owned"
    }
    assert ag_after["mcpServers"]["finops-mcp-server"]["url"] == "http://127.0.0.1:8091/v2/mcp"
    assert ag_after["mcpServers"]["finops-mcp-server"]["disabled"] is True
    assert ag_after["mcpServers"]["finops-mcp-server"]["disabledTools"] == ["delete_budget"]

    # Verify Codex preserved user top-level, profiles, user MCPs, colliding MCP, user agent, AND disabled!
    codex_after_text = codex_cfg_path.read_text(encoding="utf-8")
    codex_after = tomllib.loads(codex_after_text)
    assert "# Personal Codex settings" in codex_after_text
    assert codex_after["model"] == "o3"
    assert codex_after["profiles"]["personal"]["model"] == "gpt-4.1"
    assert codex_after["mcp_servers"]["my-personal-srv"]["url"] == "https://personal.example.com/mcp"
    assert codex_after["mcp_servers"]["shared-name-mcp"]["url"] == "http://localhost:7777/user-owned"
    assert codex_after["mcp_servers"]["finops-mcp-server"]["url"] == "http://127.0.0.1:8091/v2/mcp"
    assert codex_after["mcp_servers"]["finops-mcp-server"]["disabled"] is True
    assert codex_after["mcp_servers"]["finops-mcp-server"]["disabled_tools"] == ["delete_budget"]
    assert (home_dir / ".codex" / "agents" / "user-custom-agent.toml").is_file()

    # Verify Claude preserved theme, personal marketplace, personal plugin, AND user's False toggle
    claude_after = json.loads(claude_path.read_text(encoding="utf-8"))
    assert claude_after["theme"] == "dark"
    assert "personal-mkt" in claude_after["extraKnownMarketplaces"]
    assert claude_after["enabledPlugins"]["my-personal-plugin@personal-mkt"] is True
    assert claude_after["enabledPlugins"]["gcp-finops-toolkit@acme-enterprise-marketplace"] is False

    # Verify VS Code preserved user settings and personal marketplace/pluginLocation
    vscode_after = json.loads(
        (home_dir / ".config" / "Code" / "User" / "settings.json").read_text(encoding="utf-8")
    )
    assert vscode_after["editor.fontSize"] == 14
    assert "file:///opt/personal-marketplace" in vscode_after["chat.plugins.marketplaces"]
    assert vscode_after["chat.pluginLocations"]["/opt/personal-plugin"] is True


# ---------------------------------------------------------------------------
# 3. Clean pruning on removal vs preservation on failed catalog fetch
# ---------------------------------------------------------------------------


def test_clean_pruning_and_failure_preservation(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    # Seed user-authored MCP server to ensure pruning NEVER touches user entries
    (home_dir / ".gemini" / "config").mkdir(parents=True)
    (home_dir / ".gemini" / "config" / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"personal-srv": {"url": "http://localhost:9000/mcp"}}}),
        encoding="utf-8",
    )

    zip_a, sha_a = _build_polyglot_plugin_zip(
        plugin_name="gcp-finops-toolkit",
        display_name="GCP FinOps Toolkit",
        mcp_server_key="finops-mcp-server",
        agent_name="finops-advisor",
    )
    zip_b, sha_b = _build_polyglot_plugin_zip(
        plugin_name="secops-audit-kit",
        display_name="SecOps Audit Kit",
        mcp_server_key="secops-mcp-server",
        mcp_server_url="http://127.0.0.1:8095/mcp",
        agent_name="secops-auditor",
        skills=("iam-drift-check",),
    )

    # Sync #1: Both plugins installed
    transport_two = _make_mock_transport(
        plugins_spec=[
            {"name": "gcp-finops-toolkit", "version": "1.0.0", "sha256": sha_a},
            {"name": "secops-audit-kit", "version": "1.0.0", "sha256": sha_b},
        ],
        zip_blobs={"gcp-finops-toolkit": zip_a, "secops-audit-kit": zip_b},
    )
    sync_hub(
        middleware_url="http://localhost:8080",
        home_dir=home_dir,
        tools="all",
        no_exec=True,
        http_transport=transport_two,
    )

    hub_dir = home_dir / ".acme-agent-hub"
    assert (hub_dir / "plugins" / "gcp-finops-toolkit").is_dir()
    assert (hub_dir / "plugins" / "secops-audit-kit").is_dir()
    assert (home_dir / ".codex" / "agents" / "secops-auditor.toml").is_file()
    assert (home_dir / ".gemini" / "config" / "plugins" / "secops-audit-kit").is_dir()

    # Sync #2: Catalog fetch FAILS (HTTP 503) — EVERYTHING must remain intact!
    failing_transport = _make_mock_transport(plugins_spec=[], zip_blobs={}, catalog_status=503)
    with pytest.raises(CatalogFetchError):
        sync_hub(
            middleware_url="http://localhost:8080",
            home_dir=home_dir,
            tools="all",
            no_exec=True,
            http_transport=failing_transport,
        )

    assert (hub_dir / "plugins" / "gcp-finops-toolkit").is_dir()
    assert (hub_dir / "plugins" / "secops-audit-kit").is_dir()
    assert (home_dir / ".codex" / "agents" / "secops-auditor.toml").is_file()
    assert (home_dir / ".gemini" / "config" / "plugins" / "secops-audit-kit").is_dir()
    ag_mcp_after_fail = json.loads(
        (home_dir / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8")
    )
    assert "secops-mcp-server" in ag_mcp_after_fail["mcpServers"]

    # Sync #3: Clean full sync with secops-audit-kit removed -> cleanly pruned everywhere!
    transport_one = _make_mock_transport(
        plugins_spec=[{"name": "gcp-finops-toolkit", "version": "1.0.0", "sha256": sha_a}],
        zip_blobs={"gcp-finops-toolkit": zip_a},
    )
    res_prune = sync_hub(
        middleware_url="http://localhost:8080",
        home_dir=home_dir,
        tools="all",
        no_exec=True,
        http_transport=transport_one,
    )

    assert res_prune.pruned_plugins == ["secops-audit-kit"]
    assert (hub_dir / "plugins" / "gcp-finops-toolkit").is_dir()
    assert not (hub_dir / "plugins" / "secops-audit-kit").exists()
    assert not (home_dir / ".gemini" / "config" / "plugins" / "secops-audit-kit").exists()
    assert not (home_dir / ".codex" / "agents" / "secops-auditor.toml").exists()
    assert not (home_dir / ".agents" / "plugins" / "plugins" / "secops-audit-kit").exists()

    claude_after = json.loads((home_dir / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "secops-audit-kit@acme-enterprise-marketplace" not in claude_after["enabledPlugins"]
    assert "gcp-finops-toolkit@acme-enterprise-marketplace" in claude_after["enabledPlugins"]

    ag_mcp_after = json.loads(
        (home_dir / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8")
    )
    assert "secops-mcp-server" not in ag_mcp_after["mcpServers"]
    assert "finops-mcp-server" in ag_mcp_after["mcpServers"]
    assert "personal-srv" in ag_mcp_after["mcpServers"]  # user entry preserved!

    codex_after = tomllib.loads((home_dir / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert "secops-mcp-server" not in codex_after["mcp_servers"]
    assert "finops-mcp-server" in codex_after["mcp_servers"]


# ---------------------------------------------------------------------------
# 4. Security checks: SHA-256 verification, zip-slip, symlinks, redirect header stripping
# ---------------------------------------------------------------------------


def test_sha256_mismatch_aborts_before_extraction(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    zip_bytes, _real_sha = _build_polyglot_plugin_zip()
    wrong_sha = "0" * 64

    transport = _make_mock_transport(
        plugins_spec=[{"name": "gcp-finops-toolkit", "version": "1.0.0", "sha256": wrong_sha}],
        zip_blobs={"gcp-finops-toolkit": zip_bytes},
    )
    with pytest.raises(IntegrityError, match="SHA-256 mismatch"):
        sync_hub(
            middleware_url="http://localhost:8080",
            home_dir=home_dir,
            tools="all",
            no_exec=True,
            http_transport=transport,
        )

    assert not (home_dir / ".acme-agent-hub" / "plugins" / "gcp-finops-toolkit").exists()


def test_zip_slip_absolute_and_symlink_blocked(tmp_path: Path) -> None:
    # 1. Parent traversal '..'
    traversal_buf = io.BytesIO()
    with zipfile.ZipFile(traversal_buf, "w") as zf:
        zf.writestr("../../pwned.txt", "evil")
    with pytest.raises(ZipSecurityError, match="traversal"):
        safe_extract_zip(traversal_buf.getvalue(), tmp_path / "out1")
    assert not (tmp_path / "pwned.txt").exists()

    # 2. Absolute path
    abs_buf = io.BytesIO()
    with zipfile.ZipFile(abs_buf, "w") as zf:
        zf.writestr("/tmp/pwned.txt", "evil")
    with pytest.raises(ZipSecurityError, match="Absolute path"):
        safe_extract_zip(abs_buf.getvalue(), tmp_path / "out2")

    # 3. Symlink member
    sym_buf = io.BytesIO()
    with zipfile.ZipFile(sym_buf, "w") as zf:
        info = zipfile.ZipInfo("evil-link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ZipSecurityError, match="Symlink"):
        safe_extract_zip(sym_buf.getvalue(), tmp_path / "out3")


def test_sanitize_mcp_key() -> None:
    assert sanitize_mcp_key("finops-mcp-server") == "finops-mcp-server"
    assert sanitize_mcp_key("finops.mcp.server") == "finops-mcp-server"
    long_key = sanitize_mcp_key("very.long.enterprise.billing.analytics.server")
    assert len(long_key) <= 20
    assert "." not in long_key


def test_cross_origin_redirect_strips_auth_and_x_goog_headers(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    zip_bytes, sha = _build_polyglot_plugin_zip()
    hop1_headers: dict[str, str] = {}
    gcs_seen_headers: dict[str, str] = {}

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "127.0.0.1":
            if request.url.path == "/v1/catalog":
                return httpx.Response(
                    200,
                    json={
                        "plugins": [
                            {
                                "name": "gcp-finops-toolkit",
                                "version": "1.0.0",
                                "sha256": sha,
                                "bundleUrl": "http://127.0.0.1:8080/v1/plugins/gcp-finops-toolkit.zip",
                            }
                        ]
                    },
                )
            if request.url.path == "/v1/plugins/gcp-finops-toolkit.zip":
                hop1_headers.update({k.lower(): v for k, v in request.headers.items()})
                return httpx.Response(
                    302,
                    headers={"Location": "https://storage.googleapis.com/signed-bucket/finops.zip"},
                )
        if request.url.host == "storage.googleapis.com":
            gcs_seen_headers.update({k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)

    sync_hub(
        middleware_url="http://127.0.0.1:8080",
        home_dir=home_dir,
        tools="claude",
        token="secret-token",
        quota_project="acme-agent-governance",
        extra_headers={"x-goog-api-client": "hub-sync-test/1.0"},
        no_exec=True,
        http_transport=httpx.MockTransport(redirect_handler),
    )
    assert hop1_headers.get("authorization") == "Bearer secret-token"
    assert hop1_headers.get("x-goog-user-project") == "acme-agent-governance"
    assert hop1_headers.get("x-goog-api-client") == "hub-sync-test/1.0"
    assert "authorization" not in gcs_seen_headers
    assert not any(k.startswith("x-goog-") for k in gcs_seen_headers)
    assert (home_dir / ".acme-agent-hub" / "plugins" / "gcp-finops-toolkit").is_dir()

    # Non-HTTP(S) redirect target scheme must be rejected
    def bad_scheme_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "file:///etc/passwd"})

    with pytest.raises(CatalogFetchError, match="unsupported scheme"):
        sync_hub(
            middleware_url="http://127.0.0.1:8080",
            home_dir=home_dir,
            tools="claude",
            no_exec=True,
            http_transport=httpx.MockTransport(bad_scheme_handler),
        )


def test_real_middleware_catalog_contract_full_and_bootstrap_mcp_only(tmp_path: Path) -> None:
    """Verify exact middleware/app/main.py /v1/catalog payload in full & --bootstrap-mcp-only modes."""
    zip_bytes, sha = _build_polyglot_plugin_zip()
    middleware_catalog_payload: dict[str, Any] = {
        "marketplace": {
            "name": "acme-enterprise-marketplace",
            "displayName": "Acme Enterprise Agent Marketplace",
            "description": "Governed enterprise plugins backed by Agent Registry.",
            "projectId": "acme-agent-governance",
            "location": "us-central1",
        },
        "plugins": [
            {
                "name": "gcp-finops-toolkit",
                "displayName": "GCP FinOps Toolkit",
                "version": "1.0.0",
                "description": "Enterprise GCP FinOps toolkit.",
                "sha256": sha,
                "bundleUrl": "http://127.0.0.1:8080/v1/plugins/gcp-finops-toolkit.zip",
                "skills": ["cost-anomaly-triage", "bq-query-optimizer"],
                "mcpServers": [
                    "projects/acme-agent-governance/locations/us-central1/mcpServers/finops-mcp-server"
                ],
                "agents": [
                    "projects/acme-agent-governance/locations/us-central1/agents/finops-advisor-agent"
                ],
                "warnings": [],
                "keywords": ["gcp", "finops"],
            }
        ],
        "mcpServers": [
            {
                "name": "projects/acme-agent-governance/locations/us-central1/mcpServers/finops-mcp-server",
                "mcpServerId": "finops-mcp-server",
                "displayName": "FinOps MCP Server",
                "interfaces": [
                    {
                        "url": "http://127.0.0.1:8091/mcp",
                        "protocolBinding": "HTTP_JSON",
                    }
                ],
            },
            {
                # Entry without interfaces/url/command must be safely dropped
                "name": "projects/acme-agent-governance/locations/us-central1/mcpServers/empty-stub",
                "interfaces": [],
            },
        ],
        "agents": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/catalog":
            return httpx.Response(200, json=middleware_catalog_payload)
        if request.url.path == "/v1/plugins/gcp-finops-toolkit.zip":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)

    # 1. --bootstrap-mcp-only mode: wires both basename finops-mcp-server AND acme-agent-hub /mcp
    boot_home = tmp_path / "boot-home"
    boot_home.mkdir()
    sync_hub(
        middleware_url="http://127.0.0.1:8080",
        home_dir=boot_home,
        tools="antigravity,codex",
        bootstrap_mcp_only=True,
        no_exec=True,
        http_transport=httpx.MockTransport(handler),
    )
    ag_boot = json.loads(
        (boot_home / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8")
    )
    assert set(ag_boot["mcpServers"].keys()) == {"finops-mcp-server", "acme-agent-hub"}
    assert ag_boot["mcpServers"]["finops-mcp-server"] == {
        "url": "http://127.0.0.1:8091/mcp",
        "authProviderType": "google_credentials",
    }
    assert ag_boot["mcpServers"]["acme-agent-hub"] == {
        "url": "http://127.0.0.1:8080/mcp",
        "authProviderType": "google_credentials",
    }
    codex_boot = tomllib.loads((boot_home / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert set(codex_boot["mcp_servers"].keys()) == {"finops-mcp-server", "acme-agent-hub"}
    assert codex_boot["mcp_servers"]["finops-mcp-server"]["url"] == "http://127.0.0.1:8091/mcp"
    assert codex_boot["mcp_servers"]["acme-agent-hub"]["url"] == "http://127.0.0.1:8080/mcp"

    # 2. Full sync mode against exact Middleware response (uses bundleUrl & preserves clean MCP key)
    full_home = tmp_path / "full-home"
    full_home.mkdir()
    sync_hub(
        middleware_url="http://127.0.0.1:8080",
        home_dir=full_home,
        tools="all",
        no_exec=True,
        http_transport=httpx.MockTransport(handler),
    )
    ag_full = json.loads(
        (full_home / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8")
    )
    assert list(ag_full["mcpServers"].keys()) == ["finops-mcp-server"]
    assert ag_full["mcpServers"]["finops-mcp-server"]["url"] == "http://127.0.0.1:8091/mcp"
    assert (full_home / ".acme-agent-hub" / "plugins" / "gcp-finops-toolkit").is_dir()


def test_codex_preserves_user_plugin_directory_across_multiple_syncs_and_subset_prune(
    tmp_path: Path,
) -> None:
    """Regression test for Blocker 2: independent owned_codex_plugins tracking."""
    home_dir = tmp_path / "home"
    user_plugin_dir = home_dir / ".agents" / "plugins" / "plugins" / "gcp-finops-toolkit"
    user_plugin_dir.mkdir(parents=True)
    user_marker = user_plugin_dir / "user-authored-keep.txt"
    user_marker.write_text("DO NOT DELETE", encoding="utf-8")

    codex_mkt_path = home_dir / ".agents" / "plugins" / "marketplace.json"
    codex_mkt_path.write_text(
        json.dumps(
            {
                "name": "personal-codex-mkt",
                "interface": {"displayName": "Personal Codex"},
                "plugins": [
                    {
                        "name": "gcp-finops-toolkit",
                        "source": {"source": "local", "path": "./plugins/gcp-finops-toolkit"},
                        "policy": {
                            "installation": "INSTALLED_BY_DEFAULT",
                            "authentication": "ON_USE",
                        },
                        "category": "Personal Custom",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    zip_a, sha_a = _build_polyglot_plugin_zip(plugin_name="gcp-finops-toolkit")
    zip_b, sha_b = _build_polyglot_plugin_zip(
        plugin_name="secops-audit-kit",
        display_name="SecOps Audit Kit",
        mcp_server_key="secops-mcp-server",
        agent_name="secops-auditor",
        skills=("iam-drift-check",),
    )
    transport_two = _make_mock_transport(
        plugins_spec=[
            {"name": "gcp-finops-toolkit", "version": "1.0.0", "sha256": sha_a},
            {"name": "secops-audit-kit", "version": "1.0.0", "sha256": sha_b},
        ],
        zip_blobs={"gcp-finops-toolkit": zip_a, "secops-audit-kit": zip_b},
    )

    # Sync #1 & Sync #2: user's gcp-finops-toolkit directory and marketplace entry survive untouched!
    for _ in range(2):
        sync_hub(
            middleware_url="http://127.0.0.1:8080",
            home_dir=home_dir,
            tools="all",
            no_exec=True,
            http_transport=transport_two,
        )
        assert not user_plugin_dir.is_symlink()
        assert user_marker.is_file()
        assert user_marker.read_text(encoding="utf-8") == "DO NOT DELETE"
        mkt = json.loads(codex_mkt_path.read_text(encoding="utf-8"))
        user_entries = [p for p in mkt["plugins"] if p["name"] == "gcp-finops-toolkit"]
        assert len(user_entries) == 1
        assert user_entries[0]["category"] == "Personal Custom"
        manifest = load_sync_manifest(home_dir / ".acme-agent-hub")
        assert "gcp-finops-toolkit" not in manifest.owned_codex_plugins
        assert "secops-audit-kit" in manifest.owned_codex_plugins

    # Subset sync: remove BOTH plugins from catalog, run `--tools antigravity` first,
    # then run `--tools codex` -> Codex MUST still prune its owned `secops-audit-kit` symlink
    # while leaving user's `gcp-finops-toolkit` directory & marketplace entry completely untouched!
    empty_transport = _make_mock_transport(plugins_spec=[], zip_blobs={})
    sync_hub(
        middleware_url="http://127.0.0.1:8080",
        home_dir=home_dir,
        tools="antigravity",
        no_exec=True,
        http_transport=empty_transport,
    )
    sync_hub(
        middleware_url="http://127.0.0.1:8080",
        home_dir=home_dir,
        tools="codex",
        no_exec=True,
        http_transport=empty_transport,
    )
    assert not (home_dir / ".agents" / "plugins" / "plugins" / "secops-audit-kit").exists()
    assert not (home_dir / ".agents" / "plugins" / "plugins" / "secops-audit-kit").is_symlink()
    assert user_marker.is_file()
    mkt_after = json.loads(codex_mkt_path.read_text(encoding="utf-8"))
    assert [p["name"] for p in mkt_after["plugins"]] == ["gcp-finops-toolkit"]


def test_path_traversal_blocked_in_catalog_plugin_name_and_agent_frontmatter(
    tmp_path: Path,
) -> None:
    """Regression test for Blocker 3: untrusted catalog plugin.name and agent frontmatter name."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    zip_ok, sha_ok = _build_polyglot_plugin_zip()

    # Vector (a): path traversal via catalog plugin.name
    bad_catalog_transport = _make_mock_transport(
        plugins_spec=[{"name": "../../escaped-dir", "version": "1.0.0", "sha256": sha_ok}],
        zip_blobs={"../../escaped-dir": zip_ok},
    )
    with pytest.raises(CatalogFetchError, match="Unsafe or invalid identifier"):
        sync_hub(
            middleware_url="http://127.0.0.1:8080",
            home_dir=home_dir,
            tools="all",
            no_exec=True,
            http_transport=bad_catalog_transport,
        )
    assert not (home_dir / ".gemini" / "escaped-dir").exists()
    assert not (home_dir / "escaped-dir").exists()

    # Vector (b): valid ZIP member path `agents/legit-filename.md` with traversal frontmatter name
    evil_zip, evil_sha = _build_polyglot_plugin_zip(
        plugin_name="evil-agent-plugin",
        agent_name="placeholder",
    )
    # Replace agents/placeholder.md with agents/legit-filename.md carrying `name: ../../pwned-agent`
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(evil_zip), "r") as zin, zipfile.ZipFile(buf, "w") as zout:
        for item in zin.infolist():
            if item.filename.endswith("placeholder.md") or item.filename.endswith("placeholder.agent.md"):
                continue
            zout.writestr(item, zin.read(item.filename))
        _add_zip_file(
            zout,
            "agents/legit-filename.md",
            "---\nname: ../../pwned-agent\ndescription: Traversal attempt.\n---\nEscape.\n",
        )
    evil_bytes = buf.getvalue()
    evil_digest = hashlib.sha256(evil_bytes).hexdigest()

    bad_agent_transport = _make_mock_transport(
        plugins_spec=[{"name": "evil-agent-plugin", "version": "1.0.0", "sha256": evil_digest}],
        zip_blobs={"evil-agent-plugin": evil_bytes},
    )
    with pytest.raises(ZipSecurityError, match="Unsafe or invalid identifier"):
        sync_hub(
            middleware_url="http://127.0.0.1:8080",
            home_dir=home_dir,
            tools="all",
            no_exec=True,
            http_transport=bad_agent_transport,
        )
    assert not (home_dir / "pwned-agent.toml").exists()
    assert not (home_dir / ".acme-agent-hub" / "plugins" / "evil-agent-plugin").exists()


def test_agy_timeout_and_symlink_dest_swap_resilience(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify agy subprocess timeout/OSError warns without aborting, and symlink dest swaps safely."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    zip_bytes, sha = _build_polyglot_plugin_zip()

    # Pre-seed dest_dir as a symlink to verify _swap_directory_atomically unlinks symlink backups cleanly
    symlink_target = tmp_path / "old-target"
    symlink_target.mkdir()
    dest_dir = tmp_path / "extract-dest"
    os.symlink(symlink_target, dest_dir)
    safe_extract_zip(zip_bytes, dest_dir)
    assert dest_dir.is_dir() and not dest_dir.is_symlink()
    assert symlink_target.is_dir()

    # Simulate `agy` binary on PATH raising TimeoutExpired -> sync completes & persists manifest!
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_agy = fake_bin / "agy"
    fake_agy.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_agy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    def raising_run(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=["agy", "plugin", "install"], timeout=60)

    monkeypatch.setattr("tools.hub_sync.adapters.subprocess.run", raising_run)

    res = sync_hub(
        middleware_url="http://127.0.0.1:8080",
        home_dir=home_dir,
        tools="antigravity",
        no_exec=False,
        http_transport=_make_mock_transport(
            plugins_spec=[{"name": "gcp-finops-toolkit", "version": "1.0.0", "sha256": sha}],
            zip_blobs={"gcp-finops-toolkit": zip_bytes},
        ),
    )
    assert (home_dir / ".acme-agent-hub" / ".sync-manifest.json").is_file()
    assert any("failed to execute cleanly" in w for w in res.adapter_reports["antigravity"].warnings)


def test_partial_plugin_download_failure_preserves_existing_install(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    zip_a, sha_a = _build_polyglot_plugin_zip(plugin_name="gcp-finops-toolkit")

    # Initial clean sync installs gcp-finops-toolkit
    sync_hub(
        middleware_url="http://localhost:8080",
        home_dir=home_dir,
        tools="all",
        no_exec=True,
        http_transport=_make_mock_transport(
            plugins_spec=[{"name": "gcp-finops-toolkit", "version": "1.0.0", "sha256": sha_a}],
            zip_blobs={"gcp-finops-toolkit": zip_a},
        ),
    )
    assert (home_dir / ".acme-agent-hub" / "plugins" / "gcp-finops-toolkit").is_dir()

    # Next sync replaces catalog with [broken-plugin] whose ZIP returns HTTP 404
    with pytest.raises(CatalogFetchError):
        sync_hub(
            middleware_url="http://localhost:8080",
            home_dir=home_dir,
            tools="all",
            no_exec=True,
            http_transport=_make_mock_transport(
                plugins_spec=[{"name": "missing-plugin", "version": "1.0.0"}],
                zip_blobs={},
            ),
        )

    # Original installation and IDE configs remain completely intact!
    assert (home_dir / ".acme-agent-hub" / "plugins" / "gcp-finops-toolkit").is_dir()
    assert (home_dir / ".gemini" / "config" / "plugins" / "gcp-finops-toolkit").is_dir()
    assert (home_dir / ".codex" / "agents" / "finops-advisor.toml").is_file()


def test_cli_module_subprocess_sync_and_status(tmp_path: Path) -> None:
    """End-to-end test invoking `python -m tools.hub_sync sync` and `status` over localhost HTTP."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    home_dir = tmp_path / "home"
    home_dir.mkdir()
    zip_bytes, sha = _build_polyglot_plugin_zip()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/catalog":
                body = json.dumps(
                    {
                        "plugins": [
                            {
                                "name": "gcp-finops-toolkit",
                                "display_name": "GCP FinOps Toolkit",
                                "version": "1.0.0",
                                "sha256": sha,
                                "download_url": "/v1/plugins/gcp-finops-toolkit.zip",
                            }
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/v1/plugins/gcp-finops-toolkit.zip":
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(zip_bytes)))
                self.end_headers()
                self.wfile.write(zip_bytes)
            else:
                self.send_response(404)
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sync_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.hub_sync",
                "sync",
                "--middleware-url",
                f"http://127.0.0.1:{port}",
                "--home",
                str(home_dir),
                "--tools",
                "all",
                "--no-exec",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert sync_proc.returncode == 0, sync_proc.stderr + sync_proc.stdout
        assert "copilot plugin marketplace add" in sync_proc.stdout
        assert "agy plugin list --json" in sync_proc.stdout
        assert "agy --agent finops-advisor" in sync_proc.stdout

        status_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.hub_sync",
                "status",
                "--home",
                str(home_dir),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert status_proc.returncode == 0, status_proc.stderr + status_proc.stdout
        assert "gcp-finops-toolkit" in status_proc.stdout
        assert sha in status_proc.stdout
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# 5. CLI modes: --dry-run, --bootstrap-mcp-only, --tools subset, agy exec, status
# ---------------------------------------------------------------------------


def test_dry_run_and_bootstrap_mcp_only_and_cli_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    zip_bytes, sha = _build_polyglot_plugin_zip()

    transport = _make_mock_transport(
        plugins_spec=[{"name": "gcp-finops-toolkit", "version": "1.0.0", "sha256": sha}],
        zip_blobs={"gcp-finops-toolkit": zip_bytes},
    )

    # 1. --dry-run creates zero files on disk
    dry_res = sync_hub(
        middleware_url="http://localhost:8080",
        home_dir=home_dir,
        tools="all",
        dry_run=True,
        no_exec=True,
        http_transport=transport,
    )
    assert dry_res.dry_run is True
    assert not (home_dir / ".acme-agent-hub").exists()
    assert not (home_dir / ".claude").exists()
    assert not (home_dir / ".codex").exists()
    assert not (home_dir / ".gemini").exists()

    # 2. --bootstrap-mcp-only does NOT download plugin ZIPs, wires hub /mcp
    recorded_boot: list[httpx.Request] = []
    boot_transport = _make_mock_transport(
        plugins_spec=[{"name": "gcp-finops-toolkit", "version": "1.0.0", "sha256": sha}],
        zip_blobs={"gcp-finops-toolkit": zip_bytes},
        recorded_requests=recorded_boot,
    )
    sync_hub(
        middleware_url="http://localhost:8080",
        home_dir=home_dir,
        tools="all",
        bootstrap_mcp_only=True,
        no_exec=True,
        http_transport=boot_transport,
    )
    assert all(not r.url.path.endswith(".zip") for r in recorded_boot)
    ag_mcp = json.loads(
        (home_dir / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8")
    )
    assert ag_mcp["mcpServers"]["acme-agent-hub"]["url"] == "http://localhost:8080/mcp"
    codex_cfg = tomllib.loads((home_dir / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert codex_cfg["mcp_servers"]["acme-agent-hub"]["url"] == "http://localhost:8080/mcp"
    claude_boot_mcp = json.loads(
        (home_dir / ".acme-agent-hub" / "claude-bootstrap-mcp.json").read_text(encoding="utf-8")
    )
    assert claude_boot_mcp["mcpServers"]["acme-agent-hub"]["url"] == "http://localhost:8080/mcp"
    vscode_boot_mcp = json.loads(
        (home_dir / ".acme-agent-hub" / "vscode-mcp-snippet.json").read_text(encoding="utf-8")
    )
    assert vscode_boot_mcp["servers"]["acme-agent-hub"]["url"] == "http://localhost:8080/mcp"

    # 3. Fake `agy` binary on PATH is invoked when --no-exec is False
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    agy_log = tmp_path / "agy-calls.log"
    fake_agy = fake_bin / "agy"
    fake_agy.write_text(
        f"#!/usr/bin/env bash\necho \"$@\" >> {agy_log}\nexit 0\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    res_agy = sync_hub(
        middleware_url="http://localhost:8080",
        home_dir=home_dir,
        tools="antigravity",
        no_exec=False,
        http_transport=transport,
    )
    assert len(res_agy.adapter_reports["antigravity"].executed_commands) == 1
    assert "plugin install" in agy_log.read_text(encoding="utf-8")

    # 4. Click CLI `status` subcommand (text & JSON)
    runner = CliRunner()
    txt_res = runner.invoke(cli, ["status", "--home", str(home_dir)])
    assert txt_res.exit_code == 0, txt_res.output
    assert "gcp-finops-toolkit" in txt_res.output
    assert sha in txt_res.output

    json_res = runner.invoke(cli, ["status", "--home", str(home_dir), "--json"])
    assert json_res.exit_code == 0, json_res.output
    parsed_status = json.loads(json_res.output)
    assert parsed_status["synced"] is True
    assert parsed_status["plugins"]["gcp-finops-toolkit"]["sha256"] == sha


# ---------------------------------------------------------------------------
# 6. Starter exercises compile cleanly & Lab 01 skeleton is valid
# ---------------------------------------------------------------------------


def test_starter_exercises_compile_and_have_todo_markers() -> None:
    starter_dir = REPO_ROOT / "starter"
    assert (starter_dir / "README.md").is_file()

    # Lab 01 skeleton validates as Agent Plugins 1.0 + agentskills.io SKILL.md
    lab01_dir = starter_dir / "lab01_plugin_skeleton"
    assert validate_agent_plugin_bundle(lab01_dir).valid
    assert validate_skill_md(
        lab01_dir / "skills" / "hello-finops" / "SKILL.md", expected_name="hello-finops"
    ).valid

    # All starter/*.py files compile cleanly with py_compile and contain WORKSHOP-TODO markers
    py_files = sorted(starter_dir.glob("*.py"))
    expected_names = {
        "lab02_plugin_sync_exercise.py",
        "lab03_middleware_redirect_exercise.py",
        "lab04_hub_sync_adapter_exercise.py",
    }
    assert {p.name for p in py_files} == expected_names

    for py_file in py_files:
        py_compile.compile(str(py_file), doraise=True)
        source = py_file.read_text(encoding="utf-8")
        assert "# WORKSHOP-TODO(lab-" in source

        # Running each exercise's __main__ self-check with exact README invocation (no PYTHONPATH override)
        clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.run(
            [sys.executable, str(py_file)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            env=clean_env,
        )
        assert proc.returncode == 0, f"{py_file.name} failed:\n{proc.stderr}\n{proc.stdout}"
        assert "[TODO]" in proc.stdout


def test_hub_sync_single_bangkok_endpoint_and_cli_options(tmp_path: Path) -> None:
    """Verify hub_sync targets the single Bangkok Middleware endpoint without 2-region failover flags."""
    zip_bytes, zip_sha = _build_polyglot_plugin_zip()
    bangkok_url = "https://polyglot-middleware-abc123.asia-southeast3.run.app"
    seen_hosts: list[str] = []

    def _bangkok_handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        if request.url.path == "/v1/catalog":
            return httpx.Response(
                200,
                json={
                    "marketplace": {"name": "acme-enterprise-marketplace"},
                    "plugins": [
                        {
                            "name": "gcp-finops-toolkit",
                            "displayName": "GCP FinOps Toolkit",
                            "version": "1.0.0",
                            "sha256": zip_sha,
                            "bundleUrl": f"{bangkok_url}/v1/plugins/gcp-finops-toolkit.zip",
                        }
                    ],
                },
            )
        if request.url.path == "/v1/plugins/gcp-finops-toolkit.zip":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)

    home_dir = tmp_path / "home"
    result = sync_hub(
        middleware_url=f"{bangkok_url}/",
        home_dir=home_dir,
        tools=["claude", "antigravity"],
        no_exec=True,
        http_transport=httpx.MockTransport(_bangkok_handler),
    )

    assert not hasattr(result, "failover_used")
    assert result.middleware_url == bangkok_url
    assert result.manifest.middleware_url == bangkok_url
    assert set(seen_hosts) == {"polyglot-middleware-abc123.asia-southeast3.run.app"}
    assert [p.name for p in result.plugins] == ["gcp-finops-toolkit"]
    assert (home_dir / ".acme-agent-hub" / "plugins" / "gcp-finops-toolkit" / "plugin.json").is_file()

    # HTTP 503 from the Bangkok endpoint surfaces CatalogFetchError cleanly
    with pytest.raises(CatalogFetchError):
        sync_hub(
            middleware_url=bangkok_url,
            home_dir=tmp_path / "home-503",
            tools=["claude"],
            no_exec=True,
            http_transport=httpx.MockTransport(
                lambda _req: httpx.Response(503, text="Bangkok middleware temporarily unavailable")
            ),
        )

    # Verify IntegrityError (tampered ZIP) aborts cleanly without extracting
    def _tampered_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/catalog":
            return httpx.Response(
                200,
                json={
                    "marketplace": {"name": "acme-enterprise-marketplace"},
                    "plugins": [
                        {
                            "name": "gcp-finops-toolkit",
                            "version": "1.0.0",
                            "sha256": "0" * 64,
                            "bundleUrl": f"{bangkok_url}/v1/plugins/gcp-finops-toolkit.zip",
                        }
                    ],
                },
            )
        return httpx.Response(200, content=zip_bytes)

    with pytest.raises(IntegrityError):
        sync_hub(
            middleware_url=bangkok_url,
            home_dir=tmp_path / "home-tampered",
            tools=["claude"],
            no_exec=True,
            http_transport=httpx.MockTransport(_tampered_handler),
        )

    # Verify CLI exposes --middleware-url / HUB_MIDDLEWARE_URL and does NOT expose 2-region failover flags
    help_res = CliRunner().invoke(cli, ["sync", "--help"])
    assert help_res.exit_code == 0
    assert "--middleware-url" in help_res.output
    assert "HUB_MIDDLEWARE_URL" in help_res.output
    assert "--fallback-middleware-url" not in help_res.output
    assert "HUB_FALLBACK_MIDDLEWARE_URL" not in help_res.output
