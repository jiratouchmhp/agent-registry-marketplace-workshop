"""Click CLI for syncing Agent Plugins 1.0 bundles into Google Cloud Agent Registry (v1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

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
    default="us-central1",
    show_default=True,
    envvar="GCP_LOCATION",
    help="Agent Registry location (e.g. us-central1, global, us, eu).",
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
def sync_command(
    plugin_dir: Path,
    registry_url: str,
    project: str,
    location: str,
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
) -> None:
    """Decompose, package, optionally sign, and sync a plugin to Agent Registry."""
    try:
        plan = build_sync_plan(
            plugin_dir,
            project=project,
            location=location,
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
