"""Click CLI for syncing Agent Plugins 1.0 bundles into Google Cloud Agent Registry (v1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from tools.plugin_sync.sources import ingest_source
from tools.plugin_sync.sync import (
    PluginSyncError,
    VALID_SCANNING_POLICIES,
    VALID_TARGET_STATES,
    apply_sync_plan,
    build_sync_plan,
    verify_skill_zip_signature,
)


@click.group(
    help="Package, sign, and sync Agent Plugins 1.0 directories to Google Cloud Agent Registry (v1)."
)
def cli() -> None:
    """Top-level command group."""


@cli.command("sync")
@click.option(
    "--plugin-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the Agent Plugins 1.0 plugin directory.",
)
@click.option(
    "--registry-url",
    default="https://agentregistry.googleapis.com/v1",
    show_default=True,
    envvar="AGENT_REGISTRY_BASE_URL",
    help="Agent Registry v1 REST API base URL (or local mock registry URL).",
)
@click.option(
    "--project",
    required=True,
    envvar="GCP_PROJECT_ID",
    help="Google Cloud project ID owning the Agent Registry resources.",
)
@click.option(
    "--location",
    default="asia-southeast3",
    show_default=True,
    envvar="GCP_LOCATION",
    help="Agent Registry location for Services (e.g. asia-southeast3, asia-southeast1, global, us, eu).",
)
@click.option(
    "--skills-location",
    default=None,
    envvar="AGENT_REGISTRY_SKILLS_LOCATION",
    help=(
        "Optional Agent Registry location for standalone Skill resources "
        "(e.g. global, us, eu). Defaults to --location when unset."
    ),
)
@click.option(
    "--target-state",
    default="TARGET_STATE_ACTIVE",
    show_default=True,
    type=click.Choice(sorted(VALID_TARGET_STATES), case_sensitive=True),
    help="Target lifecycle state for synced Skill resources.",
)
@click.option(
    "--scanning-policy",
    default="ENABLED_BLOCKING",
    show_default=True,
    type=click.Choice(sorted(VALID_SCANNING_POLICIES), case_sensitive=True),
    help="Agent Registry security scanning policy for uploaded Skill revisions.",
)
@click.option(
    "--source-repo",
    default=None,
    help="Override source repository URL injected into SKILL.md frontmatter.metadata.",
)
@click.option(
    "--git-commit",
    default=None,
    help="Override Git commit SHA injected into SKILL.md frontmatter.metadata.",
)
@click.option(
    "--sign-pem",
    default=None,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
    help="Path to a local ECDSA P-256 private key PEM file for signing skill ZIP digests.",
)
@click.option(
    "--kms-key-version",
    default=None,
    help=(
        "Cloud KMS asymmetric signing key version resource name "
        "(projects/.../cryptoKeyVersions/1) or logical key version label when used with --sign-pem."
    ),
)
@click.option(
    "--auth-token",
    default=None,
    envvar="AGENT_REGISTRY_AUTH_TOKEN",
    help="Optional OAuth 2.0 Bearer token for Agent Registry and Cloud KMS calls.",
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help="Optional output directory to write packaged skill ZIPs and sync-plan.json.",
)
@click.option(
    "--skip-mcp-probe",
    is_flag=True,
    default=False,
    help="Skip live HTTP tools/list probing and use static fallback TOOL_SPEC.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate, package, and sign locally without making mutating API calls.",
)
@click.option(
    "--wait-for-scan",
    is_flag=True,
    default=False,
    help=(
        "Wait for live Agent Registry async security scanning to transition STATE_DRAFT "
        "skills to STATE_ACTIVE (or a terminal scan state)."
    ),
)
@click.option(
    "--scan-timeout",
    default=90.0,
    show_default=True,
    type=float,
    help="Maximum seconds to poll per skill when --wait-for-scan is enabled.",
)
def sync_command(
    plugin_dir: Path,
    registry_url: str,
    project: str,
    location: str,
    skills_location: str | None,
    target_state: str,
    scanning_policy: str,
    source_repo: str | None,
    git_commit: str | None,
    sign_pem: Path | None,
    kms_key_version: str | None,
    auth_token: str | None,
    output_dir: Path | None,
    skip_mcp_probe: bool,
    dry_run: bool,
    wait_for_scan: bool,
    scan_timeout: float,
) -> None:
    """Decompose, package, optionally sign, and sync a plugin to Agent Registry."""
    try:
        plan = build_sync_plan(
            plugin_dir,
            project=project,
            location=location,
            skills_location=skills_location,
            target_state=target_state,
            scanning_policy=scanning_policy,
            source_repo=source_repo,
            git_commit=git_commit,
            sign_pem=sign_pem,
            kms_key_version=kms_key_version,
            kms_auth_token=auth_token,
            probe_mcp=not skip_mcp_probe,
        )
        result = apply_sync_plan(
            plan,
            registry_url=registry_url,
            auth_token=auth_token,
            dry_run=dry_run,
            output_dir=output_dir,
            wait_for_scan=wait_for_scan,
            scan_timeout=scan_timeout,
        )
    except PluginSyncError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(json.dumps(result, indent=2))


@cli.command("ingest")
@click.option(
    "--source-type",
    default=None,
    type=click.Choice(["local", "git"], case_sensitive=False),
    help="Skill source mode: 'local' (disk directory) or 'git' (shallow clone). Auto-inferred if omitted.",
)
@click.option(
    "--path",
    "local_path",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help="Local directory path containing standalone skills and/or plugins (for --source-type local).",
)
@click.option(
    "--repo-url",
    default=None,
    help="Git repository URL or local Git repo path to shallow-clone (for --source-type git).",
)
@click.option(
    "--ref",
    default=None,
    help="Optional Git branch, tag, or commit SHA to check out when using --source-type git.",
)
@click.option(
    "--subdir",
    default=None,
    help="Optional relative subdirectory inside --path or cloned --repo-url to scan.",
)
@click.option(
    "--plugin-name",
    default=None,
    help="Override bundle plugin name used when grouping standalone skills without plugin.json.",
)
@click.option(
    "--plugin-display-name",
    default=None,
    help="Override human-readable displayName for bundled standalone skills.",
)
@click.option(
    "--plugin-version",
    default="1.0.0",
    show_default=True,
    help="Plugin version injected into standalone skill frontmatter.metadata.",
)
@click.option(
    "--per-skill-plugin",
    is_flag=True,
    default=False,
    help="Emit one separate plugin bundle per standalone skill instead of grouping under --plugin-name.",
)
@click.option(
    "--git-token-env",
    default=None,
    help="Environment variable name holding a private Git token (passed via ephemeral http.extraHeader).",
)
@click.option(
    "--only-skills",
    is_flag=True,
    default=False,
    help="Ingest Skill resources only and skip MCP/A2A Service registration.",
)
@click.option(
    "--registry-url",
    default="https://agentregistry.googleapis.com/v1",
    show_default=True,
    envvar="AGENT_REGISTRY_BASE_URL",
    help="Agent Registry v1 REST API base URL (or local mock registry URL).",
)
@click.option(
    "--project",
    required=True,
    envvar="GCP_PROJECT_ID",
    help="Google Cloud project ID owning the Agent Registry resources.",
)
@click.option(
    "--location",
    default="asia-southeast3",
    show_default=True,
    envvar="GCP_LOCATION",
    help="Agent Registry location for Services (e.g. asia-southeast3, asia-southeast1, global, us, eu).",
)
@click.option(
    "--skills-location",
    default=None,
    envvar="AGENT_REGISTRY_SKILLS_LOCATION",
    help=(
        "Optional Agent Registry location for standalone Skill resources "
        "(e.g. global, us, eu). Defaults to --location when unset."
    ),
)
@click.option(
    "--target-state",
    default="TARGET_STATE_ACTIVE",
    show_default=True,
    type=click.Choice(sorted(VALID_TARGET_STATES), case_sensitive=True),
    help="Target lifecycle state for ingested Skill resources.",
)
@click.option(
    "--scanning-policy",
    default="ENABLED_BLOCKING",
    show_default=True,
    type=click.Choice(sorted(VALID_SCANNING_POLICIES), case_sensitive=True),
    help="Agent Registry security scanning policy for uploaded Skill revisions.",
)
@click.option(
    "--source-repo",
    default=None,
    help="Override source repository URL injected into SKILL.md frontmatter.metadata.",
)
@click.option(
    "--git-commit",
    default=None,
    help="Override Git commit SHA injected into SKILL.md frontmatter.metadata.",
)
@click.option(
    "--sign-pem",
    default=None,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
    help="Path to a local ECDSA P-256 private key PEM file for signing skill ZIP digests.",
)
@click.option(
    "--kms-key-version",
    default=None,
    help=(
        "Cloud KMS asymmetric signing key version resource name "
        "(projects/.../cryptoKeyVersions/1) or logical key version label when used with --sign-pem."
    ),
)
@click.option(
    "--auth-token",
    default=None,
    envvar="AGENT_REGISTRY_AUTH_TOKEN",
    help="Optional OAuth 2.0 Bearer token for Agent Registry and Cloud KMS calls.",
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help="Optional output directory to write per-plan skill ZIPs, sync-plan.json, and ingest-summary.json.",
)
@click.option(
    "--skip-mcp-probe",
    is_flag=True,
    default=False,
    help="Skip live HTTP tools/list probing and use static fallback TOOL_SPEC.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate, package, and sign locally without making mutating API calls.",
)
@click.option(
    "--wait-for-scan",
    is_flag=True,
    default=False,
    help=(
        "Wait for live Agent Registry async security scanning to transition STATE_DRAFT "
        "skills to STATE_ACTIVE (or a terminal scan state)."
    ),
)
@click.option(
    "--scan-timeout",
    default=90.0,
    show_default=True,
    type=float,
    help="Maximum seconds to poll per skill when --wait-for-scan is enabled.",
)
def ingest_command(
    source_type: str | None,
    local_path: Path | None,
    repo_url: str | None,
    ref: str | None,
    subdir: str | None,
    plugin_name: str | None,
    plugin_display_name: str | None,
    plugin_version: str,
    per_skill_plugin: bool,
    git_token_env: str | None,
    only_skills: bool,
    registry_url: str,
    project: str,
    location: str,
    skills_location: str | None,
    target_state: str,
    scanning_policy: str,
    source_repo: str | None,
    git_commit: str | None,
    sign_pem: Path | None,
    kms_key_version: str | None,
    auth_token: str | None,
    output_dir: Path | None,
    skip_mcp_probe: bool,
    dry_run: bool,
    wait_for_scan: bool,
    scan_timeout: float,
) -> None:
    """Auto-discover and ingest local or centralized Git skills/plugins into Agent Registry."""
    cleaned_repo_url = repo_url.strip() if isinstance(repo_url, str) and repo_url.strip() else None

    if source_type is None:
        if cleaned_repo_url is not None and local_path is not None:
            raise click.UsageError(
                "Cannot specify both --path and --repo-url without choosing a single source."
            )
        resolved_source_type = "git" if cleaned_repo_url is not None else "local"
    else:
        resolved_source_type = source_type.lower()

    if resolved_source_type == "local":
        if cleaned_repo_url is not None:
            raise click.UsageError("--repo-url cannot be used with --source-type local.")
        if local_path is None:
            raise click.UsageError("--path is required when --source-type local is selected.")
        if not local_path.is_dir():
            raise click.UsageError(f"--path directory does not exist: {local_path}")
    else:
        if local_path is not None:
            raise click.UsageError("--path cannot be used with --source-type git.")
        if cleaned_repo_url is None:
            raise click.UsageError("--repo-url is required when --source-type git is selected.")

    try:
        result = ingest_source(
            source_type=resolved_source_type,
            path=local_path,
            repo_url=cleaned_repo_url,
            ref=ref,
            subdir=subdir,
            plugin_name=plugin_name,
            plugin_display_name=plugin_display_name,
            plugin_version=plugin_version,
            per_skill_plugin=per_skill_plugin,
            git_token_env=git_token_env,
            only_skills=only_skills,
            registry_url=registry_url,
            project=project,
            location=location,
            skills_location=skills_location,
            target_state=target_state,
            scanning_policy=scanning_policy,
            source_repo=source_repo,
            git_commit=git_commit,
            sign_pem=sign_pem,
            kms_key_version=kms_key_version,
            auth_token=auth_token,
            output_dir=output_dir,
            probe_mcp=not skip_mcp_probe,
            dry_run=dry_run,
            wait_for_scan=wait_for_scan,
            scan_timeout=scan_timeout,
        )
    except PluginSyncError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(json.dumps(result, indent=2))


@cli.command("verify")
@click.option(
    "--skill-zip",
    required=True,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
    help="Path to a signed skill ZIP archive.",
)
@click.option(
    "--public-key-pem",
    required=True,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
    help="Path to the trusted ECDSA P-256 public key PEM file.",
)
def verify_command(skill_zip: Path, public_key_pem: Path) -> None:
    """Verify the ECDSA P-256 signature embedded in a skill ZIP's SKILL.md metadata."""
    ok, reason = verify_skill_zip_signature(skill_zip.read_bytes(), public_key_pem)
    payload = {"valid": ok, "reason": reason, "skill_zip": str(skill_zip)}
    click.echo(json.dumps(payload, indent=2))
    if not ok:
        sys.exit(1)


def main() -> None:
    cli()
