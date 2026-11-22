"""Automated tests for workshop shell scripts, Cloud Build configs, starter verifier,
plugin_sync live scan polling & quota header, and hub_sync regional failover."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from click.testing import CliRunner

from tools.hub_sync.cli import cli as hub_sync_cli
from tools.hub_sync.cli import sync_hub
from tools.plugin_sync.cli import cli as plugin_sync_cli
from tools.plugin_sync.sync import apply_sync_plan, build_sync_plan

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
CLOUDBUILD_DIR = REPO_ROOT / "cloudbuild"
STARTER_PLUGIN_DIR = REPO_ROOT / "starter-marketplace" / "plugins" / "gcp-finops-toolkit"

EXPECTED_SHELL_SCRIPTS: tuple[str, ...] = (
    "preflight_check.sh",
    "run_local_stack.sh",
    "local_smoke_test.sh",
    "build_images_cloudbuild.sh",
    "deploy_gcp.sh",
    "validate_live_gcp.sh",
    "destroy_gcp.sh",
)

EXPECTED_CLOUDBUILD_CONFIGS: tuple[str, ...] = (
    "middleware.yaml",
    "sample-mcp-server.yaml",
    "sample-a2a-agent.yaml",
)

GCLOUD_SUBCOMMANDS: tuple[str, ...] = (
    "agent-registry",
    "alpha",
    "artifacts",
    "auth",
    "beta",
    "builds",
    "config",
    "iam",
    "kms",
    "logging",
    "org-policies",
    "projects",
    "run",
    "services",
    "storage",
    "version",
)

_GCLOUD_CMD_RE = re.compile(
    r'(?:^|[;(\s]|run_cmd\s+|CLOUDSDK_[A-Z0-9_]+=\S+\s+)'
    r'(?:"\$\{?GCLOUD(?:_BIN)?\}?"|\$\{?GCLOUD(?:_BIN)?\}?|\bgcloud)\s+'
    r'(' + "|".join(GCLOUD_SUBCOMMANDS) + r')\b'
)


def _strip_heredocs_and_join_continuations(script_text: str) -> list[tuple[int, str]]:
    """Strip shell heredoc bodies and comment lines, joining backslash continuations."""
    logical_lines: list[tuple[int, str]] = []
    in_heredoc_delim: str | None = None
    buf = ""
    buf_start_line = 0

    for line_no, raw_line in enumerate(script_text.splitlines(), start=1):
        if in_heredoc_delim is not None:
            if raw_line.strip() == in_heredoc_delim:
                in_heredoc_delim = None
            continue

        stripped = raw_line.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue

        # Detect start of heredoc (e.g., `cat <<'EOF'` or `cat <<EOF`)
        heredoc_match = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", raw_line)
        if heredoc_match and not stripped.startswith("#"):
            in_heredoc_delim = heredoc_match.group(1)

        if not buf:
            buf_start_line = line_no

        if raw_line.rstrip().endswith("\\"):
            buf += raw_line.rstrip()[:-1] + " "
            continue

        buf += raw_line
        logical_lines.append((buf_start_line, buf))
        buf = ""

    if buf:
        logical_lines.append((buf_start_line, buf))
    return logical_lines


def _extract_gcloud_invocations(script_text: str) -> list[tuple[int, str]]:
    """Extract executable gcloud command lines (excluding comments, heredocs, and echo/print hints)."""
    invocations: list[tuple[int, str]] = []
    for line_no, cmd_line in _strip_heredocs_and_join_continuations(script_text):
        stripped = cmd_line.strip()
        # Ignore informational echo / print_* log lines (but NOT `printf ' %q' "${GCLOUD}" ...` dry-run wrappers)
        if re.match(r"^(?:echo|print_(?:ok|warn|info|err|error|step))\b", stripped):
            continue
        if "command -v gcloud" in stripped:
            continue
        if _GCLOUD_CMD_RE.search(stripped):
            invocations.append((line_no, stripped))
    return invocations


def _make_valid_polyglot_plugin_zip(plugin_name: str = "gcp-finops-toolkit") -> tuple[bytes, str]:
    """Build a minimal valid Polyglot plugin ZIP archive and return (bytes, sha256_hex)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "plugin.json",
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                    "name": plugin_name,
                    "version": "1.0.0",
                    "description": "Failover verification plugin.",
                }
            ),
        )
        zf.writestr(
            ".antigravity-plugin/plugin.json",
            json.dumps(
                {
                    "$schema": "https://antigravity.google/schemas/v1/plugin.json",
                    "name": plugin_name,
                    "displayName": "GCP FinOps Toolkit",
                    "version": "1.0.0",
                    "description": "Failover verification plugin.",
                    "logo": "assets/logo.svg",
                }
            ),
        )
        zf.writestr(
            "assets/logo.svg",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64"></svg>',
        )
        zf.writestr(
            "mcp_config.json",
            json.dumps(
                {
                    "mcpServers": {
                        "finops-mcp-server": {
                            "url": "https://sample-mcp-server.asia-southeast1.run.app/mcp",
                            "authProviderType": "google_credentials",
                        }
                    }
                }
            ),
        )
        zf.writestr(
            "skills/cost-anomaly-triage/SKILL.md",
            "---\nname: cost-anomaly-triage\ndescription: Triage billing spikes.\n---\n\n# Cost Anomaly Triage\n",
        )
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 1. Shell Script Hygiene, Syntax, Executability & gcloud Guardrails
# ---------------------------------------------------------------------------


def test_expected_shell_scripts_exist_and_are_executable() -> None:
    """All workshop shell scripts under scripts/ exist and have executable permissions."""
    assert SCRIPTS_DIR.is_dir(), f"Missing scripts directory: {SCRIPTS_DIR}"
    all_sh = sorted(SCRIPTS_DIR.glob("*.sh"))
    actual_names = {p.name for p in all_sh}
    missing = set(EXPECTED_SHELL_SCRIPTS) - actual_names
    assert not missing, f"Missing expected shell scripts under scripts/: {sorted(missing)}"

    for script_path in all_sh:
        assert os.access(script_path, os.X_OK), (
            f"Shell script is not executable (run chmod +x): {script_path.relative_to(REPO_ROOT)}"
        )


def test_all_shell_scripts_pass_bash_syntax_check() -> None:
    """Every scripts/*.sh passes `bash -n` syntax validation cleanly."""
    for script_path in sorted(SCRIPTS_DIR.glob("*.sh")):
        proc = subprocess.run(
            ["bash", "-n", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, (
            f"`bash -n` failed for {script_path.relative_to(REPO_ROOT)}:\n{proc.stderr}"
        )


def test_shell_scripts_gcloud_flags_and_no_invalid_skills_delete_force() -> None:
    """Every gcloud call in scripts/*.sh uses --quiet, list uses --limit, and no invalid skills delete --force."""
    missing_quiet: list[str] = []
    missing_limit: list[str] = []
    invalid_delete_force: list[str] = []
    total_gcloud_calls = 0

    for script_path in sorted(SCRIPTS_DIR.glob("*.sh")):
        rel = script_path.relative_to(REPO_ROOT)
        text = script_path.read_text(encoding="utf-8")

        # Regression guard: `gcloud agent-registry skills delete` has NO `--force` flag
        for line_no, line in _strip_heredocs_and_join_continuations(text):
            if (
                re.search(r"agent-registry\s+skills\s+delete\b", line)
                and re.search(r"--force\b", line)
            ):
                invalid_delete_force.append(f"{rel}:{line_no}: {line.strip()}")

        for line_no, cmd in _extract_gcloud_invocations(text):
            total_gcloud_calls += 1
            if not re.search(r"(?:^|\s)(?:--quiet|-q)(?:\s|$|\"|'|\)|;|\|)", cmd):
                missing_quiet.append(f"{rel}:{line_no}: {cmd}")

            # Check `gcloud ... list` commands carry explicit `--limit`
            if re.search(
                r'(?:"\$\{?GCLOUD(?:_BIN)?\}?"|\$\{?GCLOUD(?:_BIN)?\}?|\bgcloud)\s+'
                r"(?:[a-z0-9-]+\s+){1,3}list\b",
                cmd,
            ):
                if "--limit" not in cmd:
                    missing_limit.append(f"{rel}:{line_no}: {cmd}")

    assert total_gcloud_calls > 0, "Expected scripts/*.sh to contain gcloud invocations"
    assert not invalid_delete_force, (
        "Invalid `gcloud agent-registry skills delete --force` detected:\n"
        + "\n".join(invalid_delete_force)
    )
    assert not missing_quiet, (
        "gcloud invocations missing `--quiet`:\n" + "\n".join(missing_quiet)
    )
    assert not missing_limit, (
        "`gcloud ... list` invocations missing `--limit`:\n" + "\n".join(missing_limit)
    )


# ---------------------------------------------------------------------------
# 2. Checked-in Cloud Build Config Validation (Zero Local Docker)
# ---------------------------------------------------------------------------


def test_cloudbuild_configs_valid_yaml_with_cloud_logging_only_and_image_uri() -> None:
    """Every cloudbuild/*.yaml parses cleanly with CLOUD_LOGGING_ONLY and _IMAGE_URI."""
    assert CLOUDBUILD_DIR.is_dir(), f"Missing cloudbuild directory: {CLOUDBUILD_DIR}"
    yaml_files = sorted(CLOUDBUILD_DIR.glob("*.yaml"))
    actual_names = {p.name for p in yaml_files}
    missing = set(EXPECTED_CLOUDBUILD_CONFIGS) - actual_names
    assert not missing, f"Missing expected Cloud Build configs: {sorted(missing)}"

    for cfg_path in yaml_files:
        rel = cfg_path.relative_to(REPO_ROOT)
        parsed = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict), f"{rel} must parse to a YAML mapping"

        options = parsed.get("options")
        assert isinstance(options, dict) and options.get("logging") == "CLOUD_LOGGING_ONLY", (
            f"{rel} must set options.logging: CLOUD_LOGGING_ONLY"
        )

        substitutions = parsed.get("substitutions")
        assert isinstance(substitutions, dict) and "_IMAGE_URI" in substitutions, (
            f"{rel} must declare _IMAGE_URI in substitutions"
        )

        images = parsed.get("images")
        assert isinstance(images, list) and any("_IMAGE_URI" in str(img) for img in images), (
            f"{rel} must reference ${{_IMAGE_URI}} in images[]"
        )

        steps = parsed.get("steps")
        assert isinstance(steps, list) and steps, f"{rel} must define build steps[]"
        steps_text = json.dumps(steps)
        assert "_IMAGE_URI" in steps_text, f"{rel} must tag built image using ${{_IMAGE_URI}}"


# ---------------------------------------------------------------------------
# 3. Starter Exercise Verification Harness (`scripts/check_starter.py`)
# ---------------------------------------------------------------------------


def test_check_starter_default_and_verify_reference_solutions() -> None:
    """scripts/check_starter.py passes in both default mode and --verify-reference-solutions."""
    checker = SCRIPTS_DIR / "check_starter.py"
    assert checker.is_file(), f"Missing starter checker script: {checker}"

    default_proc = subprocess.run(
        [sys.executable, str(checker)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert default_proc.returncode == 0, (
        f"check_starter.py (default mode) failed:\nSTDOUT:\n{default_proc.stdout}\nSTDERR:\n{default_proc.stderr}"
    )
    assert "Lab 01 Antigravity Coexistence Checklist (0/6 present)" in default_proc.stdout
    assert "Progress: 0/4 checks passed" in default_proc.stdout
    assert default_proc.stdout.count("Progress: 0/3 checks passed") == 2

    ref_proc = subprocess.run(
        [sys.executable, str(checker), "--verify-reference-solutions"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert ref_proc.returncode == 0, (
        f"check_starter.py --verify-reference-solutions failed:\nSTDOUT:\n{ref_proc.stdout}\nSTDERR:\n{ref_proc.stderr}"
    )


def test_gcloudignore_and_dockerignore_exclude_local_bloat_and_keep_build_inputs() -> None:
    """Repo root ships .gcloudignore and .dockerignore excluding .venv/.git/terraform/docs/tests."""
    required_excludes = (
        ".venv/",
        ".git/",
        ".pytest_cache/",
        "__pycache__/",
        "terraform/",
        "docs/",
        "tests/",
        "*.tfvars",
    )
    kept_build_inputs = (
        "middleware/",
        "schemas/",
        "samples/",
        "cloudbuild/",
        "pyproject.toml",
    )
    for filename in (".gcloudignore", ".dockerignore"):
        ignore_path = REPO_ROOT / filename
        assert ignore_path.is_file(), f"Missing {filename} at repository root"
        lines = {
            line.strip()
            for line in ignore_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        for pat in required_excludes:
            assert pat in lines, f"{filename} must exclude {pat!r}"
        for keep in kept_build_inputs:
            assert keep not in lines, f"{filename} must NOT exclude build input {keep!r}"


def test_preflight_check_reports_ports_terraform_1_6_and_readiness_flags() -> None:
    """scripts/preflight_check.sh runs cleanly, checks ports & Terraform >= 1.6.0, and emits flags."""
    preflight = SCRIPTS_DIR / "preflight_check.sh"
    proc = subprocess.run(
        [str(preflight)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"preflight_check.sh failed:\n{proc.stdout}\n{proc.stderr}"
    assert "local ports" in proc.stdout
    assert "Terraform >= 1.8" not in proc.stdout
    for token in ("ports_free=", "gcloud_on_path=", "gcloud_adc=", "agent_registry_cli="):
        assert token in proc.stdout, f"preflight_check.sh output missing flag {token!r}"


# ---------------------------------------------------------------------------
# 4. `tools.plugin_sync`: `x-goog-user-project` & `--wait-for-scan` Polling
# ---------------------------------------------------------------------------


def test_plugin_sync_adds_quota_project_header_and_polls_wait_for_scan() -> None:
    """apply_sync_plan adds x-goog-user-project for googleapis.com and polls STATE_DRAFT -> STATE_ACTIVE."""
    plan = build_sync_plan(
        STARTER_PLUGIN_DIR,
        project="bkk-agent-workshop",
        location="asia-southeast3",
        target_state="TARGET_STATE_ACTIVE",
        scanning_policy="ENABLED_BLOCKING",
        probe_mcp=False,
    )
    # Scope to a single skill and no services so the MockTransport trace is deterministic
    plan.skills = plan.skills[:1]
    plan.mcp_services = []
    plan.agent_services = []
    skill = plan.skills[0]

    skill_path = (
        f"/v1/projects/bkk-agent-workshop/locations/asia-southeast3/skills/{skill.private_skill_id}"
    )
    rev_path = f"{skill_path}/revisions/rev-1"

    captured_requests: list[tuple[str, str, str | None]] = []
    skill_get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal skill_get_count
        quota_hdr = request.headers.get("x-goog-user-project")
        captured_requests.append((request.method, request.url.path, quota_hdr))

        if request.method == "GET" and request.url.path == skill_path:
            skill_get_count += 1
            if skill_get_count == 1:
                # Initial existence check -> not found
                return httpx.Response(404, json={"error": {"code": 404, "message": "not found"}})
            if skill_get_count == 2:
                # First --wait-for-scan poll -> still STATE_DRAFT while async LLM scan runs
                return httpx.Response(
                    200,
                    json={
                        "name": skill_path.removeprefix("/v1/"),
                        "state": "STATE_DRAFT",
                        "latestRevision": rev_path.removeprefix("/v1/"),
                        "defaultRevisionFindingCount": 0,
                    },
                )
            # Second --wait-for-scan poll -> auto-promoted to STATE_ACTIVE!
            return httpx.Response(
                200,
                json={
                    "name": skill_path.removeprefix("/v1/"),
                    "state": "STATE_ACTIVE",
                    "defaultRevision": rev_path.removeprefix("/v1/"),
                    "defaultRevisionFindingCount": 0,
                },
            )

        if request.method == "POST" and request.url.path.endswith("/skills"):
            return httpx.Response(
                200,
                json={
                    "name": "projects/bkk-agent-workshop/locations/asia-southeast3/operations/op-1",
                    "done": True,
                    "response": {
                        "name": skill_path.removeprefix("/v1/"),
                        "state": "STATE_DRAFT",
                        "latestRevision": rev_path.removeprefix("/v1/"),
                    },
                },
            )

        if request.method == "GET" and request.url.path == rev_path:
            if skill_get_count == 2:
                return httpx.Response(
                    200,
                    json={
                        "name": rev_path.removeprefix("/v1/"),
                        "scanResult": {"scanState": "RUNNING", "findings": []},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "name": rev_path.removeprefix("/v1/"),
                    "scanResult": {"scanState": "SUCCEEDED", "findings": []},
                },
            )

        return httpx.Response(404, json={"error": f"unexpected {request.method} {request.url.path}"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = apply_sync_plan(
            plan,
            registry_url="https://agentregistry.googleapis.com/v1",
            auth_token="test-oauth-token",
            http_client=client,
            wait_for_scan=True,
            scan_timeout=10.0,
            scan_poll_interval=0.01,
        )

    assert len(result["skills"]) == 1
    skill_entry = result["skills"][0]
    assert skill_entry["action"] == "created"
    assert skill_entry["final_state"] == "STATE_ACTIVE"
    assert skill_entry["scan_state"] == "SUCCEEDED"
    assert skill_entry["scan_timed_out"] is False
    assert skill_get_count == 3

    # Every googleapis.com request MUST include x-goog-user-project == project
    assert captured_requests, "Expected at least one googleapis.com request"
    for method, path, quota_hdr in captured_requests:
        assert quota_hdr == "bkk-agent-workshop", (
            f"Missing or wrong x-goog-user-project on {method} {path}: got {quota_hdr!r}"
        )


def test_plugin_sync_cli_exposes_wait_for_scan_option() -> None:
    """Both `tools.plugin_sync sync` and `ingest` expose --wait-for-scan."""
    runner = CliRunner()
    for subcmd in ("sync", "ingest"):
        res = runner.invoke(plugin_sync_cli, [subcmd, "--help"])
        assert res.exit_code == 0
        assert "--wait-for-scan" in res.output


# ---------------------------------------------------------------------------
# 5. `tools.hub_sync`: `--fallback-middleware-url` Multi-Region Failover
# ---------------------------------------------------------------------------


def test_hub_sync_fallback_middleware_url_failover(tmp_path: Path) -> None:
    """tools.hub_sync seamlessly fails over from an unreachable primary to a healthy secondary."""
    primary_url = "https://polyglot-middleware-bkk.asia-southeast3.run.app"
    secondary_url = "https://polyglot-middleware-sg.asia-southeast1.run.app"
    zip_bytes, zip_sha = _make_valid_polyglot_plugin_zip("gcp-finops-toolkit")

    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        if request.url.host == "polyglot-middleware-bkk.asia-southeast3.run.app":
            raise httpx.ConnectError("Simulated Bangkok primary regional outage", request=request)

        if request.url.host == "polyglot-middleware-sg.asia-southeast1.run.app":
            if request.url.path == "/v1/catalog":
                return httpx.Response(
                    200,
                    json={
                        "marketplace": {
                            "name": "acme-enterprise-marketplace",
                            "displayName": "Acme Enterprise Agent Marketplace",
                            "description": "Governed enterprise marketplace.",
                            "owner": {
                                "name": "Acme Platform Security",
                                "email": "platform-security@acme.example.com",
                            },
                        },
                        "plugins": [
                            {
                                "name": "gcp-finops-toolkit",
                                "displayName": "GCP FinOps Toolkit",
                                "version": "1.0.0",
                                "description": "Failover verification plugin.",
                                "sha256": zip_sha,
                                "download_url": f"{secondary_url}/v1/plugins/gcp-finops-toolkit.zip",
                                "skills": ["cost-anomaly-triage"],
                                "mcpServers": {},
                            }
                        ],
                        "mcpServers": [],
                    },
                )
            if request.url.path == "/v1/plugins/gcp-finops-toolkit.zip":
                return httpx.Response(
                    200,
                    content=zip_bytes,
                    headers={
                        "Content-Type": "application/zip",
                        "X-Bundle-Sha256": zip_sha,
                    },
                )
        return httpx.Response(404)

    result = sync_hub(
        middleware_url=primary_url,
        fallback_middleware_url=secondary_url,
        home_dir=tmp_path,
        tools="all",
        no_exec=True,
        http_transport=httpx.MockTransport(handler),
    )

    assert "polyglot-middleware-bkk.asia-southeast3.run.app" in seen_hosts
    assert "polyglot-middleware-sg.asia-southeast1.run.app" in seen_hosts
    assert result.middleware_url == secondary_url
    assert result.catalog_complete is True
    assert [p.name for p in result.plugins] == ["gcp-finops-toolkit"]
    assert (tmp_path / ".acme-agent-hub" / "plugins" / "gcp-finops-toolkit" / "plugin.json").is_file()
    manifest_data: dict[str, Any] = json.loads(
        (tmp_path / ".acme-agent-hub" / ".sync-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest_data["middleware_url"] == secondary_url


def test_hub_sync_cli_exposes_fallback_middleware_url_option() -> None:
    """`tools.hub_sync sync` exposes --fallback-middleware-url."""
    runner = CliRunner()
    res = runner.invoke(hub_sync_cli, ["sync", "--help"])
    assert res.exit_code == 0
    assert "--fallback-middleware-url" in res.output


def test_wait_for_skill_scan_keeps_polling_after_succeeded_draft_and_stops_on_findings() -> None:
    """wait_for_skill_scan_completion keeps polling when scanState=SUCCEEDED & findings=0 while STATE_DRAFT, until STATE_ACTIVE."""
    from tools.plugin_sync.sync import wait_for_skill_scan_completion

    skill_path = "projects/bkk-agent-workshop/locations/global/skills/private-cost-anomaly-triage"
    rev_path = f"{skill_path}/revisions/rev-1"

    # Case 1: scanState=SUCCEEDED & findings=0 for 4 polls while state=STATE_DRAFT, then 5th poll flips to STATE_ACTIVE
    poll_count = 0

    def _delayed_promotion_handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.url.path == f"/v1/{skill_path}":
            poll_count += 1
            state = "STATE_ACTIVE" if poll_count >= 5 else "STATE_DRAFT"
            return httpx.Response(
                200,
                json={
                    "name": skill_path,
                    "state": state,
                    "defaultRevision": rev_path,
                    "defaultRevisionFindingCount": 0,
                },
            )
        if request.url.path == f"/v1/{rev_path}":
            return httpx.Response(
                200,
                json={
                    "name": rev_path,
                    "scanResult": {"scanState": "SUCCEEDED", "findings": []},
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(_delayed_promotion_handler)) as client:
        res = wait_for_skill_scan_completion(
            client,
            "https://agentregistry.googleapis.com/v1",
            skill_path,
            headers={"Authorization": "Bearer tok"},
            timeout_seconds=10.0,
            poll_interval=0.005,
        )
    assert poll_count == 5
    assert res["final_state"] == "STATE_ACTIVE"
    assert res["scan_state"] == "SUCCEEDED"
    assert res["timed_out"] is False

    # Case 2: scanState=SUCCEEDED with findings > 0 returns immediately (blocking findings detected)
    findings_polls = 0

    def _findings_handler(request: httpx.Request) -> httpx.Response:
        nonlocal findings_polls
        if request.url.path == f"/v1/{skill_path}":
            findings_polls += 1
            return httpx.Response(
                200,
                json={
                    "name": skill_path,
                    "state": "STATE_DRAFT",
                    "defaultRevision": rev_path,
                    "defaultRevisionFindingCount": 1,
                },
            )
        if request.url.path == f"/v1/{rev_path}":
            return httpx.Response(
                200,
                json={
                    "name": rev_path,
                    "scanResult": {"scanState": "SUCCEEDED", "findings": ["secret-leak"]},
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(_findings_handler)) as client:
        res_findings = wait_for_skill_scan_completion(
            client,
            "https://agentregistry.googleapis.com/v1",
            skill_path,
            headers={"Authorization": "Bearer tok"},
            timeout_seconds=10.0,
            poll_interval=0.005,
        )
    assert findings_polls == 1
    assert res_findings["final_state"] == "STATE_DRAFT"
    assert res_findings["scan_state"] == "SUCCEEDED"
    assert res_findings["timed_out"] is False

    # Case 3: scanState=UNSUPPORTED_FORMAT returns immediately
    unsupported_polls = 0

    def _unsupported_handler(request: httpx.Request) -> httpx.Response:
        nonlocal unsupported_polls
        if request.url.path == f"/v1/{skill_path}":
            unsupported_polls += 1
            return httpx.Response(
                200,
                json={"name": skill_path, "state": "STATE_DRAFT", "latestRevision": rev_path},
            )
        if request.url.path == f"/v1/{rev_path}":
            return httpx.Response(
                200,
                json={"name": rev_path, "scanResult": {"scanState": "UNSUPPORTED_FORMAT", "findings": []}},
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(_unsupported_handler)) as client:
        res_unsup = wait_for_skill_scan_completion(
            client,
            "https://agentregistry.googleapis.com/v1",
            skill_path,
            headers={"Authorization": "Bearer tok"},
            timeout_seconds=10.0,
            poll_interval=0.005,
        )
    assert unsupported_polls == 1
    assert res_unsup["scan_state"] == "UNSUPPORTED_FORMAT"
    assert res_unsup["timed_out"] is False


def test_plugin_sync_and_gcp_scripts_support_global_skills_location() -> None:
    """plugin_sync CLI and GCP deploy/validate/destroy scripts route standalone Skills to global while Services stay regional."""
    runner = CliRunner()
    sync_res = runner.invoke(
        plugin_sync_cli,
        [
            "sync",
            "--plugin-dir",
            str(STARTER_PLUGIN_DIR),
            "--project",
            "acme-agent-governance",
            "--location",
            "asia-southeast3",
            "--skills-location",
            "global",
            "--skip-mcp-probe",
            "--dry-run",
        ],
    )
    assert sync_res.exit_code == 0, sync_res.output
    sync_doc = json.loads(sync_res.output)
    assert sync_doc["location"] == "asia-southeast3"
    assert sync_doc["skills_location"] == "global"
    assert all("/locations/global/skills/private-" in s["resource_name"] for s in sync_doc["skills"])
    assert all("/locations/asia-southeast3/services/" in s["resource_name"] for s in sync_doc["mcp_services"])

    # Dry-run all 3 live GCP scripts with --enable-secondary --skills-location global
    deploy_proc = subprocess.run(
        [
            str(SCRIPTS_DIR / "deploy_gcp.sh"),
            "--project",
            "acme-agent-governance",
            "--primary-region",
            "asia-southeast3",
            "--secondary-region",
            "asia-southeast1",
            "--skills-location",
            "global",
            "--enable-secondary",
            "--skip-build",
            "--dry-run",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert deploy_proc.returncode == 0, deploy_proc.stderr
    assert "AGENT_REGISTRY_SKILLS_LOCATION=global" in deploy_proc.stdout
    assert "storage.googleapis.com" in deploy_proc.stdout
    assert deploy_proc.stdout.count("[Step 6/7] Ingesting standalone Skills ONCE in global") == 1
    assert "--location global" in deploy_proc.stdout

    validate_proc = subprocess.run(
        [
            str(SCRIPTS_DIR / "validate_live_gcp.sh"),
            "--project",
            "acme-agent-governance",
            "--primary-region",
            "asia-southeast3",
            "--secondary-region",
            "asia-southeast1",
            "--skills-location",
            "global",
            "--enable-secondary",
            "--dry-run",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert validate_proc.returncode == 0, validate_proc.stderr
    assert "locations/global/skills" in validate_proc.stdout
    assert "skills=global" in validate_proc.stdout

    destroy_proc = subprocess.run(
        [
            str(SCRIPTS_DIR / "destroy_gcp.sh"),
            "--project",
            "acme-agent-governance",
            "--primary-region",
            "asia-southeast3",
            "--secondary-region",
            "asia-southeast1",
            "--skills-location",
            "global",
            "--enable-secondary",
            "--dry-run",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert destroy_proc.returncode == 0, destroy_proc.stderr
    assert "locations/global/skills/private-cost-anomaly-triage?force=true" in destroy_proc.stdout
    assert "REST GET (expect 404) https://agentregistry.googleapis.com/v1/projects/acme-agent-governance/locations/global/skills/private-cost-anomaly-triage" in destroy_proc.stdout
    assert "agent-registry skills list" not in destroy_proc.stdout
