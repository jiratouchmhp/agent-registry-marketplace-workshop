"""1-Command Enterprise Agent Hub Installer CLI (``python -m tools.hub_sync``).

Subcommands:
- ``sync``: Fetches Middleware ``/v1/catalog`` + verified plugin ZIPs, performs
  safe ownership-tracked hydration into ``<home>/.acme-agent-hub``, generates
  top-level marketplace catalogs, and wires up Claude Code, VS Code/Copilot,
  OpenAI Codex, and Google Antigravity without clobbering user configs.
- ``status``: Displays installed plugins, versions, SHA-256 digests, owned MCP
  servers, and wired IDE surfaces.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
import httpx

from tools.hub_sync.adapters import (
    SUPPORTED_TOOLS,
    AdapterReport,
    collect_desired_mcp_servers,
    wire_antigravity,
    wire_claude,
    wire_codex,
    wire_copilot,
    write_hub_marketplace_indexes,
)
from tools.hub_sync.hydration import (
    SYNC_MANIFEST_FILENAME,
    CatalogFetchError,
    CatalogPlugin,
    ExtractedPlugin,
    HubClient,
    HubSyncError,
    SyncManifest,
    ZipSecurityError,
    ensure_contained_path,
    inspect_extracted_plugin,
    load_sync_manifest,
    resolve_hub_dir,
    safe_extract_zip,
    save_sync_manifest,
    utc_now_iso,
    validate_safe_identifier,
    validate_zip_bytes,
)


@dataclass
class SyncResult:
    """Structured outcome returned by :func:`sync_hub`."""

    hub_dir: Path
    middleware_url: str
    marketplace_name: str
    tools: list[str]
    dry_run: bool
    bootstrap_mcp_only: bool
    catalog_complete: bool
    plugins: list[ExtractedPlugin] = field(default_factory=list)
    marketplace_files: list[str] = field(default_factory=list)
    pruned_plugins: list[str] = field(default_factory=list)
    adapter_reports: dict[str, AdapterReport] = field(default_factory=dict)
    manifest: SyncManifest = field(default_factory=SyncManifest)


def parse_tools_option(tools_spec: str) -> list[str]:
    """Parse ``--tools all|claude,copilot,codex,antigravity`` into canonical list."""
    raw = tools_spec.strip().lower()
    if not raw or raw == "all":
        return list(SUPPORTED_TOOLS)
    chosen: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if token == "all":
            return list(SUPPORTED_TOOLS)
        if token not in SUPPORTED_TOOLS:
            raise ValueError(
                f"Unsupported tool {token!r}. Choose 'all' or comma-separated values from "
                f"{', '.join(SUPPORTED_TOOLS)}."
            )
        if token not in chosen:
            chosen.append(token)
    if not chosen:
        raise ValueError(
            f"No tools specified. Choose 'all' or values from {', '.join(SUPPORTED_TOOLS)}."
        )
    return chosen


def _build_dry_run_plugin(plugin: CatalogPlugin, hub_dir: Path, sha256: str) -> ExtractedPlugin:
    return ExtractedPlugin(
        name=plugin.name,
        display_name=plugin.display_name,
        version=plugin.version,
        description=plugin.description,
        sha256=sha256,
        archive_url=plugin.download_url,
        install_path=(hub_dir / "plugins" / plugin.name).resolve(),
        category=plugin.category,
        tags=list(plugin.tags),
        skills=list(plugin.skills),
        mcp_servers=copy.deepcopy(plugin.mcp_servers),
        agents=[],
    )


def sync_hub(
    *,
    middleware_url: str,
    home_dir: Path,
    tools: list[str] | str = "all",
    token: str | None = None,
    quota_project: str | None = None,
    extra_headers: dict[str, str] | None = None,
    bootstrap_mcp_only: bool = False,
    dry_run: bool = False,
    apply_vscode: bool = False,
    no_exec: bool = False,
    hub_dir: Path | None = None,
    marketplace_name: str | None = None,
    http_client: httpx.Client | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> SyncResult:
    """Execute an end-to-end ownership-tracked sync from Middleware to local IDEs."""
    resolved_home = home_dir.expanduser().resolve()
    resolved_hub = resolve_hub_dir(resolved_home, hub_dir)
    selected_tools = parse_tools_option(tools) if isinstance(tools, str) else list(tools)
    for t in selected_tools:
        if t not in SUPPORTED_TOOLS:
            raise ValueError(f"Unsupported tool {t!r}")

    previous_manifest = load_sync_manifest(resolved_hub)

    # Phase 1: Fetch catalog & validate all plugin archives before touching disk
    with HubClient(
        middleware_url,
        token=token,
        quota_project=quota_project,
        extra_headers=extra_headers,
        client=http_client,
        transport=http_transport,
    ) as client:
        catalog = client.fetch_catalog(marketplace_name_override=marketplace_name)

        downloaded: list[tuple[CatalogPlugin, bytes, str]] = []
        if not bootstrap_mcp_only:
            for plugin in catalog.plugins:
                validate_safe_identifier(
                    plugin.name, label="plugin.name", error_cls=CatalogFetchError
                )
                zip_bytes, verified_sha = client.download_plugin_zip(plugin)
                # Validate ZIP traversal / symlink / agent-name safety in memory before disk writes
                validate_zip_bytes(zip_bytes)
                downloaded.append((plugin, zip_bytes, verified_sha))

    # Catalog fetch + all downloads/SHA-256/ZIP checks succeeded cleanly!
    catalog_complete = True
    allow_prune = catalog_complete and not bootstrap_mcp_only

    # Phase 2: Extract plugins into <hub_dir>/plugins/<name> and inspect bundles
    extracted_plugins: list[ExtractedPlugin] = []
    plugins_root = resolved_hub / "plugins"
    pruned_plugins: list[str] = []

    if not bootstrap_mcp_only:
        for plugin, zip_bytes, verified_sha in downloaded:
            safe_pname = validate_safe_identifier(
                plugin.name, label="plugin.name", error_cls=CatalogFetchError
            )
            dest_dir = ensure_contained_path(
                plugins_root,
                plugins_root / safe_pname,
                label=f"Hub plugin directory '{safe_pname}'",
                error_cls=ZipSecurityError,
                follow_final_symlink=False,
            )
            if not dry_run:
                safe_extract_zip(zip_bytes, dest_dir)
                extracted_plugins.append(
                    inspect_extracted_plugin(dest_dir, plugin, verified_sha)
                )
            else:
                extracted_plugins.append(_build_dry_run_plugin(plugin, resolved_hub, verified_sha))

        # Prune removed hub plugin directories ONLY on clean full sync
        if allow_prune:
            active_names = {p.name for p in extracted_plugins}
            for old_name in sorted(previous_manifest.owned_plugins):
                if old_name not in active_names:
                    safe_old = validate_safe_identifier(
                        old_name, label="pruned hub plugin name", error_cls=ZipSecurityError
                    )
                    old_dir = ensure_contained_path(
                        plugins_root,
                        plugins_root / safe_old,
                        label=f"Pruned hub plugin directory '{safe_old}'",
                        error_cls=ZipSecurityError,
                        follow_final_symlink=False,
                    )
                    if old_dir.exists() or old_dir.is_symlink():
                        if not dry_run:
                            if old_dir.is_symlink():
                                old_dir.unlink()
                            else:
                                shutil.rmtree(old_dir)
                    pruned_plugins.append(safe_old)

    # Phase 3: Generate top-level marketplace catalogs under <hub_dir>
    marketplace_files: list[str] = []
    if not bootstrap_mcp_only:
        mkt_paths = write_hub_marketplace_indexes(
            resolved_hub,
            catalog.marketplace,
            extracted_plugins,
            dry_run=dry_run,
        )
        marketplace_files = [str(p) for p in mkt_paths]

    # Phase 4: Collect desired MCP servers and wire selected IDE adapters
    desired_mcp_servers = collect_desired_mcp_servers(
        extracted_plugins,
        catalog.mcp_servers,
        middleware_url,
        bootstrap_mcp_only=bootstrap_mcp_only,
    )

    reports: dict[str, AdapterReport] = {}
    new_owned_claude = list(previous_manifest.owned_claude_plugins)
    new_owned_vscode = list(previous_manifest.owned_vscode_plugin_locations)
    new_owned_codex_mcp = list(previous_manifest.owned_codex_mcp_servers)
    new_owned_codex_plugins = list(previous_manifest.owned_codex_plugins)
    new_owned_codex_agents = list(previous_manifest.owned_codex_agents)
    new_owned_ag_mcp = list(previous_manifest.owned_mcp_servers)
    new_owned_ag_plugins = list(previous_manifest.owned_antigravity_plugins)

    if "claude" in selected_tools:
        report, new_owned_claude = wire_claude(
            home_dir=resolved_home,
            hub_dir=resolved_hub,
            marketplace=catalog.marketplace,
            plugins=extracted_plugins,
            manifest=previous_manifest,
            allow_prune=allow_prune,
            desired_mcp_servers=desired_mcp_servers,
            bootstrap_mcp_only=bootstrap_mcp_only,
            dry_run=dry_run,
        )
        reports["claude"] = report

    if "copilot" in selected_tools:
        report, new_owned_vscode = wire_copilot(
            home_dir=resolved_home,
            hub_dir=resolved_hub,
            marketplace=catalog.marketplace,
            plugins=extracted_plugins,
            manifest=previous_manifest,
            allow_prune=allow_prune,
            desired_mcp_servers=desired_mcp_servers,
            apply_vscode=apply_vscode,
            bootstrap_mcp_only=bootstrap_mcp_only,
            dry_run=dry_run,
        )
        reports["copilot"] = report

    if "codex" in selected_tools:
        report, new_owned_codex_mcp, new_owned_codex_agents, new_owned_codex_plugins = wire_codex(
            home_dir=resolved_home,
            hub_dir=resolved_hub,
            marketplace=catalog.marketplace,
            plugins=extracted_plugins,
            desired_mcp_servers=desired_mcp_servers,
            manifest=previous_manifest,
            allow_prune=allow_prune,
            bootstrap_mcp_only=bootstrap_mcp_only,
            dry_run=dry_run,
        )
        reports["codex"] = report

    if "antigravity" in selected_tools:
        report, new_owned_ag_mcp, new_owned_ag_plugins = wire_antigravity(
            home_dir=resolved_home,
            hub_dir=resolved_hub,
            plugins=extracted_plugins,
            desired_mcp_servers=desired_mcp_servers,
            manifest=previous_manifest,
            allow_prune=allow_prune,
            bootstrap_mcp_only=bootstrap_mcp_only,
            no_exec=no_exec,
            dry_run=dry_run,
        )
        reports["antigravity"] = report

    # Phase 5: Update and atomically persist ownership manifest
    if bootstrap_mcp_only:
        owned_plugins_map = copy.deepcopy(previous_manifest.owned_plugins)
        owned_skill_dirs = list(previous_manifest.owned_skill_dirs)
    else:
        owned_plugins_map = {}
        owned_skill_dirs = []
        for plugin in extracted_plugins:
            entry: dict[str, Any] = {
                "version": plugin.version,
                "display_name": plugin.display_name,
                "sha256": plugin.sha256,
                "path": str(plugin.install_path),
                "skills": list(plugin.skills),
                "mcp_servers": sorted(plugin.mcp_servers.keys()),
                "agents": [a.name for a in plugin.agents],
            }
            if plugin.name in new_owned_ag_plugins:
                entry["antigravity_plugin_path"] = str(
                    (resolved_home / ".gemini" / "config" / "plugins" / plugin.name).resolve()
                )
            owned_plugins_map[plugin.name] = entry
            for skill_name in plugin.skills:
                owned_skill_dirs.append(str((plugin.install_path / "skills" / skill_name).resolve()))

    merged_tools = sorted(set(previous_manifest.tools) | set(selected_tools))
    updated_manifest = SyncManifest(
        middleware_url=middleware_url.rstrip("/"),
        marketplace_name=catalog.marketplace.name,
        last_synced_at=utc_now_iso(),
        last_sync_complete=catalog_complete,
        tools=merged_tools,
        owned_plugins=owned_plugins_map,
        owned_mcp_servers=new_owned_ag_mcp,
        owned_codex_mcp_servers=new_owned_codex_mcp,
        owned_codex_plugins=new_owned_codex_plugins,
        owned_codex_agents=new_owned_codex_agents,
        owned_claude_plugins=new_owned_claude,
        owned_antigravity_plugins=new_owned_ag_plugins,
        owned_vscode_plugin_locations=new_owned_vscode,
        owned_skill_dirs=owned_skill_dirs,
    )

    if not dry_run:
        save_sync_manifest(resolved_hub, updated_manifest)

    return SyncResult(
        hub_dir=resolved_hub,
        middleware_url=middleware_url.rstrip("/"),
        marketplace_name=catalog.marketplace.name,
        tools=selected_tools,
        dry_run=dry_run,
        bootstrap_mcp_only=bootstrap_mcp_only,
        catalog_complete=catalog_complete,
        plugins=extracted_plugins,
        marketplace_files=marketplace_files,
        pruned_plugins=pruned_plugins,
        adapter_reports=reports,
        manifest=updated_manifest,
    )


def get_hub_status(*, home_dir: Path, hub_dir: Path | None = None) -> dict[str, Any]:
    """Return structured status of the local hub installation and wired IDE configs."""
    resolved_home = home_dir.expanduser().resolve()
    resolved_hub = resolve_hub_dir(resolved_home, hub_dir)
    manifest_path = resolved_hub / SYNC_MANIFEST_FILENAME

    if not manifest_path.is_file():
        return {
            "synced": False,
            "hub_dir": str(resolved_hub),
            "manifest_path": str(manifest_path),
            "middleware_url": None,
            "marketplace_name": None,
            "last_synced_at": None,
            "tools": [],
            "plugins": {},
            "owned_mcp_servers": [],
            "ide_configs": {},
        }

    manifest = load_sync_manifest(resolved_hub)
    plugins_status: dict[str, dict[str, Any]] = {}
    for pname, pmeta in sorted(manifest.owned_plugins.items()):
        ppath = Path(str(pmeta.get("path") or (resolved_hub / "plugins" / pname)))
        plugins_status[pname] = {
            "version": pmeta.get("version", "unknown"),
            "display_name": pmeta.get("display_name", pname),
            "sha256": pmeta.get("sha256", ""),
            "path": str(ppath),
            "installed_on_disk": ppath.is_dir(),
            "skills": list(pmeta.get("skills") or []),
            "mcp_servers": list(pmeta.get("mcp_servers") or []),
            "agents": list(pmeta.get("agents") or []),
        }

    ide_configs = {
        "claude": {
            "wired": "claude" in manifest.tools,
            "config_path": str(resolved_home / ".claude" / "settings.json"),
            "exists": (resolved_home / ".claude" / "settings.json").is_file(),
        },
        "copilot": {
            "wired": "copilot" in manifest.tools,
            "config_path": str(resolved_hub / "vscode-settings-snippet.json"),
            "exists": (resolved_hub / "vscode-settings-snippet.json").is_file(),
        },
        "codex": {
            "wired": "codex" in manifest.tools,
            "config_path": str(resolved_home / ".codex" / "config.toml"),
            "exists": (resolved_home / ".codex" / "config.toml").is_file(),
        },
        "antigravity": {
            "wired": "antigravity" in manifest.tools,
            "config_path": str(resolved_home / ".gemini" / "config" / "mcp_config.json"),
            "exists": (resolved_home / ".gemini" / "config" / "mcp_config.json").is_file(),
        },
    }

    return {
        "synced": True,
        "hub_dir": str(resolved_hub),
        "manifest_path": str(manifest_path),
        "middleware_url": manifest.middleware_url,
        "marketplace_name": manifest.marketplace_name,
        "last_synced_at": manifest.last_synced_at,
        "last_sync_complete": manifest.last_sync_complete,
        "tools": list(manifest.tools),
        "plugins": plugins_status,
        "owned_mcp_servers": sorted(
            set(manifest.owned_mcp_servers) | set(manifest.owned_codex_mcp_servers)
        ),
        "ide_configs": ide_configs,
    }


def _print_sync_summary(result: SyncResult) -> None:
    prefix = "[DRY-RUN] " if result.dry_run else ""
    click.echo(f"{prefix}Acme Agent Hub Sync Complete")
    click.echo(f"  Middleware : {result.middleware_url}")
    click.echo(f"  Hub Dir    : {result.hub_dir}")
    click.echo(f"  Marketplace: {result.marketplace_name}")
    click.echo(f"  Tools      : {', '.join(result.tools)}")

    if result.bootstrap_mcp_only:
        click.echo("  Mode       : bootstrap-mcp-only (skipped plugin ZIP downloads)")
    else:
        click.echo(f"\nSynced Plugins ({len(result.plugins)}):")
        for plugin in result.plugins:
            click.echo(
                f"  - {plugin.name} v{plugin.version} (sha256: {plugin.sha256[:12]}...) "
                f"skills={plugin.skills} mcp={sorted(plugin.mcp_servers.keys())}"
            )
        if result.pruned_plugins:
            click.echo(f"\nPruned Removed Plugins ({len(result.pruned_plugins)}):")
            for old_name in result.pruned_plugins:
                click.echo(f"  - {old_name}")

    all_warnings: list[str] = []
    for report in result.adapter_reports.values():
        all_warnings.extend(report.warnings)
    if all_warnings:
        click.echo("\nPreserved User Configs / Warnings:")
        for warn in all_warnings:
            click.echo(f"  ! {warn}")

    click.echo("\nNext Steps & Verification Commands:")
    for tool_name in result.tools:
        report = result.adapter_reports.get(tool_name)
        if not report or not report.commands:
            continue
        click.echo(f"  [{tool_name}]")
        for cmd in report.commands:
            click.echo(f"    {cmd}")


@click.group(help="1-Command Enterprise Agent Hub Installer & Status CLI.")
def cli() -> None:
    """Root CLI command group."""


@cli.command("sync")
@click.option(
    "--middleware-url",
    default=lambda: os.environ.get("HUB_MIDDLEWARE_URL", "http://127.0.0.1:8080"),
    show_default="http://127.0.0.1:8080 (or $HUB_MIDDLEWARE_URL)",
    help="Base URL of the enterprise Agent Hub Middleware.",
)
@click.option(
    "--home",
    "home_path",
    type=click.Path(path_type=Path),
    default=lambda: Path.home(),
    show_default="~",
    help="Developer home directory to hydrate IDE configs into.",
)
@click.option(
    "--hub-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Override local hub directory (default: <home>/.acme-agent-hub).",
)
@click.option(
    "--tools",
    default="all",
    show_default=True,
    help="Comma-separated IDE targets: all or subset of claude,copilot,codex,antigravity.",
)
@click.option(
    "--token",
    default=lambda: os.environ.get("HUB_AUTH_TOKEN"),
    help="Optional Bearer token for Middleware authentication (or $HUB_AUTH_TOKEN).",
)
@click.option(
    "--bootstrap-mcp-only",
    is_flag=True,
    default=False,
    help="Wire MCP server config only without downloading plugin ZIP archives.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview planned sync actions without writing any files.",
)
@click.option(
    "--apply-vscode",
    is_flag=True,
    default=False,
    help="Also merge VS Code plugin settings into <home>/.config/Code/User/settings.json.",
)
@click.option(
    "--no-exec",
    is_flag=True,
    default=False,
    help="Do not invoke local `agy plugin install` even if `agy` is on PATH.",
)
@click.option(
    "--marketplace-name",
    default=None,
    help="Override enterprise marketplace identifier.",
)
def sync_command(
    middleware_url: str,
    home_path: Path,
    hub_dir: Path | None,
    tools: str,
    token: str | None,
    bootstrap_mcp_only: bool,
    dry_run: bool,
    apply_vscode: bool,
    no_exec: bool,
    marketplace_name: str | None,
) -> None:
    """Sync governed plugins and MCP configs from Middleware into local IDEs."""
    try:
        parsed_tools = parse_tools_option(tools)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--tools") from exc

    try:
        result = sync_hub(
            middleware_url=middleware_url,
            home_dir=home_path,
            tools=parsed_tools,
            token=token,
            bootstrap_mcp_only=bootstrap_mcp_only,
            dry_run=dry_run,
            apply_vscode=apply_vscode,
            no_exec=no_exec,
            hub_dir=hub_dir,
            marketplace_name=marketplace_name,
        )
    except HubSyncError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)

    _print_sync_summary(result)


@cli.command("status")
@click.option(
    "--home",
    "home_path",
    type=click.Path(path_type=Path),
    default=lambda: Path.home(),
    show_default="~",
    help="Developer home directory containing .acme-agent-hub.",
)
@click.option(
    "--hub-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Override local hub directory (default: <home>/.acme-agent-hub).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output status as machine-readable JSON.",
)
def status_command(home_path: Path, hub_dir: Path | None, as_json: bool) -> None:
    """Display installed hub plugins, versions, SHA-256 digests, and wired IDEs."""
    status = get_hub_status(home_dir=home_path, hub_dir=hub_dir)
    if as_json:
        click.echo(json.dumps(status, indent=2))
        return

    if not status["synced"]:
        click.echo(f"Hub has not been synced yet (expected manifest at {status['manifest_path']}).")
        return

    click.echo("Acme Agent Hub Status")
    click.echo(f"  Hub Dir     : {status['hub_dir']}")
    click.echo(f"  Middleware  : {status['middleware_url']}")
    click.echo(f"  Marketplace : {status['marketplace_name']}")
    click.echo(f"  Last Synced : {status['last_synced_at']}")
    click.echo(f"  Wired Tools : {', '.join(status['tools']) or '(none)'}")

    click.echo(f"\nInstalled Plugins ({len(status['plugins'])}):")
    if not status["plugins"]:
        click.echo("  (none)")
    for pname, info in status["plugins"].items():
        state = "OK" if info["installed_on_disk"] else "MISSING"
        click.echo(
            f"  - {pname} v{info['version']} [{state}] sha256={info['sha256']}"
        )

    click.echo("\nWired IDE Configs:")
    for tool_name, cfg in status["ide_configs"].items():
        marker = "wired" if cfg["wired"] else "not wired"
        disk = "present" if cfg["exists"] else "absent"
        click.echo(f"  - {tool_name:12s} ({marker}, {disk}): {cfg['config_path']}")


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint."""
    cli.main(args=argv, standalone_mode=True)
