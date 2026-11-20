"""Unit & in-process integration tests for dual skill sources (Option 1 local & Option 2 git)."""

from __future__ import annotations

import base64
import http.server
import io
import json
import os
import re
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi.testclient import TestClient

from mock_registry.app import create_app as create_mock_registry_app
from mock_registry.scanner import scan_skill_archive, validate_skill_zip as mock_validate_skill_zip
from schemas.validators import parse_frontmatter, validate_skill_md
from tools.plugin_sync import (
    PluginSyncError,
    build_skill_zip,
    build_standalone_skills_plans,
    clone_git_repository,
    derive_default_plugin_name,
    discover_ingest_targets,
    ingest_source,
    sanitize_kebab_name,
    sanitize_repo_url,
    validate_skill_zip,
    verify_skill_zip_signature,
)
from tools.plugin_sync.cli import cli
import tools.plugin_sync.sync as sync_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_SKILLS_DIR = REPO_ROOT / "local-skills"
STARTER_PLUGIN_DIR = REPO_ROOT / "starter-marketplace" / "plugins" / "gcp-finops-toolkit"
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
PROJECT_ID = "acme-agent-governance"
LOCATION = "us-central1"


def _generate_ecdsa_p256_keypair(tmp_path: Path, prefix: str = "dual") -> tuple[Path, Path]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path = tmp_path / f"{prefix}_private.pem"
    pub_path = tmp_path / f"{prefix}_public.pem"
    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)
    return priv_path, pub_path


def _write_minimal_skill(skill_dir: Path, name: str, description: str = "Test standalone skill.") -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nlicense: Apache-2.0\n"
        f"metadata:\n  domain: \"testing\"\n---\n\n# {name}\n\nRun safe read-only diagnostics.\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_minimal_plugin(plugin_dir: Path, plugin_name: str, skill_name: str) -> Path:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": plugin_name,
                "version": "1.2.3",
                "description": f"Minimal test plugin {plugin_name}.",
                "author": {"name": "Platform Security", "email": "sec@acme.example.com"},
                "license": "Apache-2.0",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_minimal_skill(plugin_dir / "skills" / skill_name, skill_name)
    return plugin_dir


def _git(repo_dir: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Workshop Test",
        "GIT_AUTHOR_EMAIL": "workshop-test@acme.example.com",
        "GIT_COMMITTER_NAME": "Workshop Test",
        "GIT_COMMITTER_EMAIL": "workshop-test@acme.example.com",
    }
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


class _InProcessASGITransport(httpx.BaseTransport):
    """Bridges synchronous httpx.Client calls onto a FastAPI TestClient ASGI app in-process."""

    def __init__(self, test_client: TestClient) -> None:
        self._tc = test_client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = self._tc.request(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            content=request.content,
            follow_redirects=False,
        )
        return httpx.Response(
            status_code=resp.status_code,
            headers=resp.headers,
            content=resp.content,
            request=request,
        )


def _patch_sync_http_to_asgi(monkeypatch: pytest.MonkeyPatch, test_client: TestClient) -> None:
    """Route plugin_sync's internal httpx.Client calls to an in-process ASGI mock_registry."""
    transport = _InProcessASGITransport(test_client)
    real_client_cls = httpx.Client

    def _make_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(sync_mod.httpx, "Client", _make_client)


# ---------------------------------------------------------------------------
# 1. Option 1 pre-prepared local-skills/ validation & WORKSHOP_SPEC contract
# ---------------------------------------------------------------------------


def test_local_skills_pass_validation_packaging_and_security_scan() -> None:
    spec = json.loads((REPO_ROOT / "WORKSHOP_SPEC.json").read_text(encoding="utf-8"))
    skill_sources = spec["skill_sources"]
    assert skill_sources["supported_modes"] == ["local", "git"]
    assert skill_sources["local"]["dir"] == "local-skills"
    assert skill_sources["local"]["plugin_name"] == "local-ops-toolkit"
    expected_skills = [
        "cloud-run-debugger",
        "terraform-iam-auditor",
        "incident-postmortem-writer",
    ]
    assert skill_sources["local"]["skills"] == expected_skills

    # Required documentation & reference files
    readme_text = (LOCAL_SKILLS_DIR / "README.md").read_text(encoding="utf-8")
    assert "tools.plugin_sync ingest" in readme_text
    assert "--source-type local" in readme_text
    assert (LOCAL_SKILLS_DIR / "cloud-run-debugger" / "references" / "triage-runbook.md").is_file()
    assert (
        LOCAL_SKILLS_DIR / "terraform-iam-auditor" / "references" / "least-privilege-checklist.md"
    ).is_file()

    # Ensure terraform-iam-auditor explicitly enforces least-privilege and Agent Registry guardrails
    iam_skill_text = (LOCAL_SKILLS_DIR / "terraform-iam-auditor" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for required_token in (
        "roles/owner",
        "roles/editor",
        "allUsers",
        "roles/agentregistry.editor",
        "roles/agentregistry.admin",
    ):
        assert required_token in iam_skill_text, f"Missing {required_token!r} in terraform-iam-auditor"

    for skill_name in expected_skills:
        skill_dir = LOCAL_SKILLS_DIR / skill_name
        skill_md = skill_dir / "SKILL.md"
        assert skill_md.is_file()

        res = validate_skill_md(skill_md, expected_name=skill_name)
        assert res.valid, f"{skill_name} failed validate_skill_md: {res.errors}"

        fm, _body, err = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        assert err is None and fm is not None
        assert fm["name"] == skill_name
        assert 1 <= len(fm["description"]) <= 1024
        assert fm.get("license") == "Apache-2.0"
        assert isinstance(fm.get("compatibility"), str) and fm["compatibility"]
        assert isinstance(fm.get("metadata"), dict)
        assert "category" in fm["metadata"] and "domain" in fm["metadata"]

        packaged = build_skill_zip(
            skill_dir,
            {
                "plugin_name": "local-ops-toolkit",
                "plugin_display_name": "Local Ops Toolkit",
                "plugin_version": "1.0.0",
                "source_repo": "local://local-skills",
                "git_commit": "0" * 40,
            },
        )
        warnings = validate_skill_zip(packaged.zip_bytes, expected_skill_name=skill_name)
        assert warnings == []

        # Confirm mock_registry scanner reports zero security/secret findings
        validated = mock_validate_skill_zip(packaged.zip_bytes)
        findings = scan_skill_archive(
            validated.files_text,
            resource_name=f"projects/{PROJECT_ID}/locations/{LOCATION}/skills/private-{skill_name}",
            project=PROJECT_ID,
            location=LOCATION,
        )
        assert findings == [], f"Unexpected scanner findings in {skill_name}: {findings}"


# ---------------------------------------------------------------------------
# 2. URL sanitization & plugin name normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        (
            "https://oauth2:glpat-secret123@gitlab.com/org/repo.git",
            "https://gitlab.com/org/repo.git",
        ),
        (
            "https://my-token@github.com/org/repo.git",
            "https://github.com/org/repo.git",
        ),
        (
            "https://user:p@ssword@gitlab.example.com:8443/team/skills.git?ref=main",
            "https://gitlab.example.com:8443/team/skills.git?ref=main",
        ),
        (
            "http://ci-bot:topsecret@127.0.0.1:8080/platform/repo.git",
            "http://127.0.0.1:8080/platform/repo.git",
        ),
        (
            "ssh://user:secret@gitlab.com:2222/org/repo.git",
            "ssh://gitlab.com:2222/org/repo.git",
        ),
        (
            "ssh://token@gitlab.com/org/repo.git",
            "ssh://gitlab.com/org/repo.git",
        ),
        (
            "ssh://git@gitlab.com/org/repo.git",
            "ssh://git@gitlab.com/org/repo.git",
        ),
        (
            "git@gitlab.com:org/repo.git",
            "git@gitlab.com:org/repo.git",
        ),
        (
            "git://token@example.com/org/repo.git",
            "git://example.com/org/repo.git",
        ),
        (
            "git://user:secret@example.com:9418/org/repo.git",
            "git://example.com:9418/org/repo.git",
        ),
        (
            "file:///tmp/sample-repo",
            "file:///tmp/sample-repo",
        ),
    ],
)
def test_sanitize_repo_url_strips_credentials_and_preserves_ssh_user(
    raw_url: str, expected: str
) -> None:
    assert sanitize_repo_url(raw_url) == expected


def test_derive_default_plugin_name_and_brand_prefix_guard() -> None:
    assert derive_default_plugin_name("local-skills") == "local-ops-toolkit"
    assert derive_default_plugin_name("/opt/workshop/local-skills/") == "local-ops-toolkit"
    assert (
        derive_default_plugin_name("https://gitlab.example.com/ai-platform/agent-marketplace.git")
        == "agent-marketplace"
    )
    assert derive_default_plugin_name("git@github.com:org/platform_skills.git") == "platform-skills"

    for disallowed in ("antigravity-tools", "gemini-skills", "claude-ops", "cursor-helpers"):
        derived = derive_default_plugin_name(disallowed)
        assert derived.startswith("enterprise-")
        assert sanitize_kebab_name(disallowed) == derived


# ---------------------------------------------------------------------------
# 3. Auto-discovery across all 5 layouts + subdir path containment
# ---------------------------------------------------------------------------


def test_discover_ingest_targets_all_five_layouts_and_security(tmp_path: Path) -> None:
    # Layout 1: single_plugin
    single_plugin_dir = _write_minimal_plugin(tmp_path / "l1-plugin", "fin-tools", "budget-check")
    d1 = discover_ingest_targets(single_plugin_dir)
    assert d1.layout == "single_plugin"
    assert d1.plugin_dirs == [single_plugin_dir.resolve()]
    assert d1.standalone_skill_dirs == []

    # Layout 2: multi_plugin (both plugins/*/plugin.json and direct */plugin.json)
    multi_root = tmp_path / "l2-multi"
    p_a = _write_minimal_plugin(multi_root / "plugins" / "alpha-kit", "alpha-kit", "alpha-skill")
    p_b = _write_minimal_plugin(multi_root / "beta-kit", "beta-kit", "beta-skill")
    d2 = discover_ingest_targets(multi_root)
    assert d2.layout == "multi_plugin"
    assert d2.plugin_dirs == sorted([p_a.resolve(), p_b.resolve()])
    assert d2.standalone_skill_dirs == []

    # Layout 3: single_skill
    single_skill_dir = _write_minimal_skill(tmp_path / "l3-skill" / "solo-helper", "solo-helper")
    d3 = discover_ingest_targets(single_skill_dir)
    assert d3.layout == "single_skill"
    assert d3.plugin_dirs == []
    assert d3.standalone_skill_dirs == [single_skill_dir.resolve()]

    # Layout 4: standalone_skills (both skills/*/SKILL.md and direct */SKILL.md)
    skills_root = tmp_path / "l4-skills"
    s_1 = _write_minimal_skill(skills_root / "skills" / "log-triage", "log-triage")
    s_2 = _write_minimal_skill(skills_root / "iam-check", "iam-check")
    d4 = discover_ingest_targets(skills_root)
    assert d4.layout == "standalone_skills"
    assert d4.plugin_dirs == []
    assert d4.standalone_skill_dirs == sorted([s_1.resolve(), s_2.resolve()])

    # Layout 5: hybrid (plugins + standalone skills, never double-counting plugin dirs)
    hybrid_root = tmp_path / "l5-hybrid"
    hp = _write_minimal_plugin(hybrid_root / "plugins" / "ops-bundle", "ops-bundle", "ops-skill")
    hs = _write_minimal_skill(hybrid_root / "skills" / "standalone-audit", "standalone-audit")
    # Even if a plugin directory itself contains a stray SKILL.md, it must not be classified as standalone
    (hp / "SKILL.md").write_text("---\nname: ops-bundle\ndescription: x\n---\n", encoding="utf-8")
    d5 = discover_ingest_targets(hybrid_root)
    assert d5.layout == "hybrid"
    assert d5.plugin_dirs == [hp.resolve()]
    assert d5.standalone_skill_dirs == [hs.resolve()]

    # Subdir containment: rejects .. traversal, absolute path, and symlink escape
    outside_dir = _write_minimal_skill(tmp_path / "outside-secret" / "leak-skill", "leak-skill")
    escape_link = skills_root / "escape-link"
    escape_link.symlink_to(outside_dir.parent)

    with pytest.raises(PluginSyncError, match="traversal"):
        discover_ingest_targets(skills_root, subdir="../outside-secret")
    with pytest.raises(PluginSyncError, match="relative POSIX path"):
        discover_ingest_targets(skills_root, subdir=str(outside_dir))
    with pytest.raises(PluginSyncError, match="escapes source root"):
        discover_ingest_targets(skills_root, subdir="escape-link")

    # Empty directory rejected cleanly
    empty_dir = tmp_path / "empty-dir"
    empty_dir.mkdir()
    with pytest.raises(PluginSyncError, match="No plugins"):
        discover_ingest_targets(empty_dir)


# ---------------------------------------------------------------------------
# 4. clone_git_repository against real temporary local Git repositories
# ---------------------------------------------------------------------------


def test_clone_git_repository_refs_cleanup_and_token_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin_repo = tmp_path / "origin-skills-repo"
    origin_repo.mkdir()
    _git(origin_repo, "init", "-b", "main")
    _write_minimal_skill(origin_repo / "skills" / "v1-skill", "v1-skill", "Version 1 skill")
    _git(origin_repo, "add", ".")
    _git(origin_repo, "commit", "-m", "initial commit")
    main_sha = _git(origin_repo, "rev-parse", "HEAD")
    _git(origin_repo, "tag", "v1.0.0")

    # Add a second branch with another skill
    _git(origin_repo, "checkout", "-b", "release/v2")
    _write_minimal_skill(origin_repo / "skills" / "v2-skill", "v2-skill", "Version 2 skill")
    _git(origin_repo, "add", ".")
    _git(origin_repo, "commit", "-m", "add v2 skill")
    v2_sha = _git(origin_repo, "rev-parse", "HEAD")
    _git(origin_repo, "checkout", "main")

    # 1. Default clone + automatic temp dir cleanup on exit
    captured_clone_path: Path | None = None
    with clone_git_repository(str(origin_repo)) as cloned:
        captured_clone_path = cloned.path
        assert cloned.path.is_dir()
        assert HEX40_RE.match(cloned.git_commit)
        assert cloned.git_commit == main_sha
        assert (cloned.path / "skills" / "v1-skill" / "SKILL.md").is_file()
        assert not (cloned.path / "skills" / "v2-skill").exists()
    assert captured_clone_path is not None
    assert not captured_clone_path.exists()
    assert not captured_clone_path.parent.exists()

    # 2. Branch --ref clone
    with clone_git_repository(origin_repo.as_uri(), ref="release/v2") as cloned_branch:
        assert cloned_branch.git_commit == v2_sha
        assert (cloned_branch.path / "skills" / "v2-skill" / "SKILL.md").is_file()

    # 3. Tag --ref clone
    with clone_git_repository(origin_repo.as_uri(), ref="v1.0.0") as cloned_tag:
        assert cloned_tag.git_commit == main_sha

    # 4. Exact commit SHA --ref fallback checkout
    with clone_git_repository(origin_repo.as_uri(), ref=v2_sha) as cloned_sha:
        assert cloned_sha.git_commit == v2_sha
        assert (cloned_sha.path / "skills" / "v2-skill" / "SKILL.md").is_file()

    # 5. Missing token_env fails cleanly without leaking secrets
    monkeypatch.delenv("MISSING_GIT_TOKEN", raising=False)
    with pytest.raises(PluginSyncError, match="MISSING_GIT_TOKEN"):
        with clone_git_repository(str(origin_repo), token_env="MISSING_GIT_TOKEN"):
            pass

    # 6. Token set in env does not leak into .git/config or raised error messages
    secret_val = "glpat-super-secret-value-999"
    monkeypatch.setenv("TEST_GIT_TOKEN", secret_val)
    with clone_git_repository(str(origin_repo), token_env="TEST_GIT_TOKEN") as cloned_auth:
        git_cfg = (cloned_auth.path / ".git" / "config").read_text(encoding="utf-8")
        assert secret_val not in git_cfg

    # Failing HTTP clone with embedded/env token must never leak secret_val
    with pytest.raises(PluginSyncError) as exc_info:
        with clone_git_repository(
            f"http://oauth2:{secret_val}@127.0.0.1:1/nonexistent/repo.git",
            token_env="TEST_GIT_TOKEN",
            timeout=10,
        ):
            pass
    assert secret_val not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. CLI `ingest` (Option 1 local + Option 2 git) against in-process mock_registry
# ---------------------------------------------------------------------------


def test_cli_ingest_argument_validation() -> None:
    runner = CliRunner()

    # local without --path
    res = runner.invoke(cli, ["ingest", "--source-type", "local", "--project", PROJECT_ID])
    assert res.exit_code != 0
    assert "--path is required" in res.output

    # local with --repo-url
    res = runner.invoke(
        cli,
        [
            "ingest",
            "--source-type",
            "local",
            "--path",
            str(LOCAL_SKILLS_DIR),
            "--repo-url",
            "https://example.com/repo.git",
            "--project",
            PROJECT_ID,
        ],
    )
    assert res.exit_code != 0
    assert "--repo-url cannot be used with --source-type local" in res.output

    # git without --repo-url
    res = runner.invoke(cli, ["ingest", "--source-type", "git", "--project", PROJECT_ID])
    assert res.exit_code != 0
    assert "--repo-url is required" in res.output

    # git with --path
    res = runner.invoke(
        cli,
        [
            "ingest",
            "--source-type",
            "git",
            "--repo-url",
            "https://example.com/repo.git",
            "--path",
            str(LOCAL_SKILLS_DIR),
            "--project",
            PROJECT_ID,
        ],
    )
    assert res.exit_code != 0
    assert "--path cannot be used with --source-type git" in res.output


def test_cli_ingest_option1_local_dry_run_per_skill_only_skills_and_signing(tmp_path: Path) -> None:
    priv_pem, pub_pem = _generate_ecdsa_p256_keypair(tmp_path)
    out_dir = tmp_path / "dist-local"
    runner = CliRunner()

    # 1. Option 1 grouped bundle with ECDSA signing + output-dir
    res = runner.invoke(
        cli,
        [
            "ingest",
            "--source-type",
            "local",
            "--path",
            str(LOCAL_SKILLS_DIR),
            "--plugin-name",
            "local-ops-toolkit",
            "--sign-pem",
            str(priv_pem),
            "--output-dir",
            str(out_dir),
            "--project",
            PROJECT_ID,
            "--location",
            LOCATION,
            "--dry-run",
        ],
    )
    assert res.exit_code == 0, res.output
    summary = json.loads(res.output)
    assert summary["source_type"] == "local"
    assert summary["layout"] == "standalone_skills"
    assert summary["plans_count"] == 1
    assert summary["skills_count"] == 3
    assert summary["mcp_services_count"] == 0
    assert summary["agent_services_count"] == 0
    assert (out_dir / "ingest-summary.json").is_file()
    assert (out_dir / "local-ops-toolkit" / "sync-plan.json").is_file()

    for skill_name in ("cloud-run-debugger", "terraform-iam-auditor", "incident-postmortem-writer"):
        zip_path = out_dir / "local-ops-toolkit" / f"{skill_name}.zip"
        assert zip_path.is_file()
        ok, reason = verify_skill_zip_signature(zip_path.read_bytes(), pub_pem)
        assert ok, reason

    # 2. Option 1 --per-skill-plugin produces 3 distinct plugin plans & non-clobbering subdirs
    per_skill_out = tmp_path / "dist-per-skill"
    res_per = runner.invoke(
        cli,
        [
            "ingest",
            "--source-type",
            "local",
            "--path",
            str(LOCAL_SKILLS_DIR),
            "--per-skill-plugin",
            "--output-dir",
            str(per_skill_out),
            "--project",
            PROJECT_ID,
            "--location",
            LOCATION,
            "--dry-run",
        ],
    )
    assert res_per.exit_code == 0, res_per.output
    per_summary = json.loads(res_per.output)
    assert per_summary["plans_count"] == 3
    assert per_summary["skills_count"] == 3
    assert {p["plugin_name"] for p in per_summary["plans"]} == {
        "cloud-run-debugger",
        "terraform-iam-auditor",
        "incident-postmortem-writer",
    }
    for skill_name in ("cloud-run-debugger", "terraform-iam-auditor", "incident-postmortem-writer"):
        assert (per_skill_out / skill_name / f"{skill_name}.zip").is_file()

    # 3. Option 1 starter plugin with --only-skills skips MCP/A2A Services while preserving metadata
    res_only = runner.invoke(
        cli,
        [
            "ingest",
            "--source-type",
            "local",
            "--path",
            str(STARTER_PLUGIN_DIR),
            "--only-skills",
            "--project",
            PROJECT_ID,
            "--location",
            LOCATION,
            "--dry-run",
        ],
    )
    assert res_only.exit_code == 0, res_only.output
    only_summary = json.loads(res_only.output)
    assert only_summary["layout"] == "single_plugin"
    assert only_summary["only_skills"] is True
    assert only_summary["skills_count"] == 2
    assert only_summary["mcp_services_count"] == 0
    assert only_summary["agent_services_count"] == 0
    first_meta = only_summary["plans"][0]["skills"][0]["frontmatter_metadata"]
    assert first_meta["mcp_service_id"] == "finops-mcp-server"
    assert first_meta["agent_service_id"] == "finops-advisor-agent"


def test_cli_ingest_option1_and_option2_against_inprocess_mock_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    priv_pem, pub_pem = _generate_ecdsa_p256_keypair(tmp_path, prefix="live")
    mock_app = create_mock_registry_app()
    runner = CliRunner()

    # Build a temporary centralized Git repo containing a subdir with standalone skills
    central_repo = tmp_path / "central-skills-repo"
    central_repo.mkdir()
    _git(central_repo, "init", "-b", "main")
    _write_minimal_skill(
        central_repo / "catalog" / "gke-autopilot-triage",
        "gke-autopilot-triage",
        "Triage GKE Autopilot pod scheduling and node provisioning issues safely.",
    )
    _write_minimal_skill(
        central_repo / "catalog" / "cloud-sql-auditor",
        "cloud-sql-auditor",
        "Audit Cloud SQL private IP, SSL enforcement, and backup retention settings.",
    )
    _git(central_repo, "add", ".")
    _git(central_repo, "commit", "-m", "add centralized catalog skills")
    expected_commit = _git(central_repo, "rev-parse", "HEAD")
    assert HEX40_RE.match(expected_commit)

    with TestClient(mock_app) as tc:
        _patch_sync_http_to_asgi(monkeypatch, tc)
        registry_v1 = "http://testserver/v1"

        # Option 1: Ingest local-skills/ live into mock_registry
        opt1_res = runner.invoke(
            cli,
            [
                "ingest",
                "--source-type",
                "local",
                "--path",
                str(LOCAL_SKILLS_DIR),
                "--plugin-name",
                "local-ops-toolkit",
                "--sign-pem",
                str(priv_pem),
                "--registry-url",
                registry_v1,
                "--project",
                PROJECT_ID,
                "--location",
                LOCATION,
            ],
        )
        assert opt1_res.exit_code == 0, opt1_res.output
        opt1_summary = json.loads(opt1_res.output)
        assert opt1_summary["skills_count"] == 3
        assert opt1_summary["plans"][0]["plugin_name"] == "local-ops-toolkit"

        # Verify each Option 1 skill exists in mock_registry under private-<id> and is STATE_ACTIVE
        for skill_id in (
            "cloud-run-debugger",
            "terraform-iam-auditor",
            "incident-postmortem-writer",
        ):
            get_resp = tc.get(
                f"/v1/projects/{PROJECT_ID}/locations/{LOCATION}/skills/private-{skill_id}"
            )
            assert get_resp.status_code == 200, get_resp.text
            skill_obj = get_resp.json()
            assert skill_obj["name"].endswith(f"/skills/private-{skill_id}")
            assert skill_obj["state"] == "STATE_ACTIVE"
            assert skill_obj["defaultRevisionFindingCount"] == 0

        # Option 2: Ingest temporary Git repo via --source-type git --repo-url ... --subdir catalog
        repo_uri = central_repo.as_uri()
        opt2_res = runner.invoke(
            cli,
            [
                "ingest",
                "--source-type",
                "git",
                "--repo-url",
                repo_uri,
                "--ref",
                "main",
                "--subdir",
                "catalog",
                "--plugin-name",
                "central-platform-skills",
                "--sign-pem",
                str(priv_pem),
                "--registry-url",
                registry_v1,
                "--project",
                PROJECT_ID,
                "--location",
                LOCATION,
            ],
        )
        assert opt2_res.exit_code == 0, opt2_res.output
        opt2_summary = json.loads(opt2_res.output)
        assert opt2_summary["source_type"] == "git"
        assert opt2_summary["source_repo"] == repo_uri
        assert opt2_summary["git_commit"] == expected_commit
        assert opt2_summary["layout"] == "standalone_skills"
        assert opt2_summary["skills_count"] == 2

        # Download the registered Option 2 skill ZIP via ?alt=media (following 302 without auth headers)
        for skill_id in ("gke-autopilot-triage", "cloud-sql-auditor"):
            skill_resp = tc.get(
                f"/v1/projects/{PROJECT_ID}/locations/{LOCATION}/skills/private-{skill_id}"
            )
            assert skill_resp.status_code == 200
            skill_obj = skill_resp.json()
            assert skill_obj["state"] == "STATE_ACTIVE"
            default_rev = skill_obj["defaultRevision"]

            redir = tc.get(f"/v1/{default_rev}?alt=media", follow_redirects=False)
            assert redir.status_code == 302
            signed_url = redir.headers["location"]
            dl = tc.get(signed_url)
            assert dl.status_code == 200

            ok, reason = verify_skill_zip_signature(dl.content, pub_pem)
            assert ok, reason

            with zipfile.ZipFile(io.BytesIO(dl.content), "r") as zf:
                fm, _body, err = parse_frontmatter(zf.read(f"{skill_id}/SKILL.md").decode("utf-8"))
                assert err is None and fm is not None
                meta = fm["metadata"]
                assert meta["plugin_name"] == "central-platform-skills"
                assert meta["source_repo"] == repo_uri
                assert meta["git_commit"] == expected_commit


def test_build_standalone_skills_plans_and_ingest_source_programmatic(tmp_path: Path) -> None:
    plans = build_standalone_skills_plans(
        [LOCAL_SKILLS_DIR / "cloud-run-debugger", LOCAL_SKILLS_DIR / "terraform-iam-auditor"],
        plugin_name="local-ops-toolkit",
        plugin_display_name="Local Ops Toolkit",
        plugin_version="2.0.0",
        source_repo="https://oauth2:secret@gitlab.example.com/org/skills.git",
        git_commit="a" * 40,
        project=PROJECT_ID,
        location=LOCATION,
    )
    assert len(plans) == 1
    assert plans[0].source_repo == "https://gitlab.example.com/org/skills.git"
    assert len(plans[0].skills) == 2

    # Verify programmatic ingest_source dry-run works directly
    res = ingest_source(
        source_type="local",
        path=LOCAL_SKILLS_DIR / "incident-postmortem-writer",
        project=PROJECT_ID,
        location=LOCATION,
        dry_run=True,
    )
    assert res["layout"] == "single_skill"
    assert res["plans_count"] == 1
    assert res["skills_count"] == 1
    assert res["plans"][0]["plugin_name"] == "incident-postmortem-writer"


# ---------------------------------------------------------------------------
# 6. Adversarial security & regression tests (symlinks, KMS, hybrid, git auth)
# ---------------------------------------------------------------------------


def test_discover_ingest_targets_blocks_symlink_escapes_without_subdir(tmp_path: Path) -> None:
    outside = tmp_path / "outside-vault"
    outside_skills_dir = outside / "secret-skills"
    _write_minimal_skill(outside_skills_dir / "exfil-skill", "exfil-skill", "Outside skill")
    outside_plugins_dir = outside / "secret-plugins"
    outside_plugin = _write_minimal_plugin(
        outside_plugins_dir / "exfil-plugin", "exfil-plugin", "exfil-plugin-skill"
    )

    # Vector 1: repo/skills -> /outside/secret-skills (without --subdir)
    repo1 = tmp_path / "repo-symlink-skills"
    repo1.mkdir()
    (repo1 / "skills").symlink_to(outside_skills_dir)
    with pytest.raises(PluginSyncError, match="Symlink"):
        discover_ingest_targets(repo1)

    # Vector 2: repo/plugins -> /outside/secret-plugins (without --subdir)
    repo2 = tmp_path / "repo-symlink-plugins"
    repo2.mkdir()
    (repo2 / "plugins").symlink_to(outside_plugins_dir)
    with pytest.raises(PluginSyncError, match="Symlink"):
        discover_ingest_targets(repo2)

    # Vector 3: repo/plugins/evil-plugin -> /outside/secret-plugins/exfil-plugin
    repo3 = tmp_path / "repo-symlink-plugin-child"
    (repo3 / "plugins").mkdir(parents=True)
    (repo3 / "plugins" / "evil-plugin").symlink_to(outside_plugin)
    with pytest.raises(PluginSyncError, match="Symlink"):
        discover_ingest_targets(repo3)

    # Vector 4: repo/exfil-skill -> /outside/secret-skills/exfil-skill
    repo4 = tmp_path / "repo-symlink-root-skill"
    repo4.mkdir()
    (repo4 / "exfil-skill").symlink_to(outside_skills_dir / "exfil-skill")
    with pytest.raises(PluginSyncError, match="Symlink"):
        discover_ingest_targets(repo4)

    # Vector 5: repo/SKILL.md -> /outside/.../SKILL.md and repo/plugin.json -> /outside/.../plugin.json
    repo5a = tmp_path / "repo-symlink-skill-md"
    repo5a.mkdir()
    (repo5a / "SKILL.md").symlink_to(outside_skills_dir / "exfil-skill" / "SKILL.md")
    with pytest.raises(PluginSyncError, match="Symlink"):
        discover_ingest_targets(repo5a)

    repo5b = tmp_path / "repo-symlink-plugin-json"
    repo5b.mkdir()
    (repo5b / "plugin.json").symlink_to(outside_plugin / "plugin.json")
    with pytest.raises(PluginSyncError, match="Symlink"):
        discover_ingest_targets(repo5b)

    # Vector 6: valid plugin with symlinked internal skills/ pointing outside
    repo6 = tmp_path / "repo-symlink-nested-plugin-skills"
    nested_plugin = _write_minimal_plugin(repo6 / "plugins" / "good-plugin", "good-plugin", "ok-skill")
    import shutil

    shutil.rmtree(nested_plugin / "skills")
    (nested_plugin / "skills").symlink_to(outside_skills_dir)
    with pytest.raises(PluginSyncError, match="Symlink"):
        discover_ingest_targets(repo6)

    # Lowercase skills/*/skill.md emits helpful uppercase SKILL.md hint
    repo_lower = tmp_path / "repo-lowercase-skills"
    bad_skill = repo_lower / "skills" / "bad-case"
    bad_skill.mkdir(parents=True)
    (bad_skill / "skill.md").write_text("---\nname: bad-case\ndescription: x\n---\n", encoding="utf-8")
    with pytest.raises(PluginSyncError, match="strictly uppercase 'SKILL.md'"):
        discover_ingest_targets(repo_lower)


def test_local_plugin_source_repo_fallback_and_credential_sanitization(tmp_path: Path) -> None:
    priv_pem, pub_pem = _generate_ecdsa_p256_keypair(tmp_path, prefix="local_plug")

    # (a) Local plugin without `repository` in plugin.json gets fallback source_repo inside signed SKILL.md
    no_repo_plugin = _write_minimal_plugin(tmp_path / "no-repo-plugin", "no-repo-plugin", "audit-one")
    out_a = tmp_path / "out-no-repo"
    res_a = ingest_source(
        source_type="local",
        path=no_repo_plugin,
        project=PROJECT_ID,
        location=LOCATION,
        sign_pem=priv_pem,
        output_dir=out_a,
        dry_run=True,
    )
    assert res_a["source_repo"] == "local://no-repo-plugin"
    skill_meta_a = res_a["plans"][0]["skills"][0]["frontmatter_metadata"]
    assert skill_meta_a["source_repo"] == "local://no-repo-plugin"

    zip_a = (out_a / "no-repo-plugin" / "audit-one.zip").read_bytes()
    ok_a, reason_a = verify_skill_zip_signature(zip_a, pub_pem)
    assert ok_a, reason_a
    with zipfile.ZipFile(io.BytesIO(zip_a), "r") as zf:
        fm_a, _b, err_a = parse_frontmatter(zf.read("audit-one/SKILL.md").decode("utf-8"))
        assert err_a is None and fm_a is not None
        assert fm_a["metadata"]["source_repo"] == "local://no-repo-plugin"

    # (b) Local plugin with embedded credentials in plugin.json `repository` is sanitized before signing
    cred_plugin = _write_minimal_plugin(tmp_path / "cred-plugin", "cred-plugin", "audit-two")
    manifest_path = cred_plugin / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["repository"] = "https://oauth2:glpat-leak-me-not@gitlab.example.com/sec/cred-plugin.git"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    out_b = tmp_path / "out-cred-plugin"
    res_b = ingest_source(
        source_type="local",
        path=cred_plugin,
        project=PROJECT_ID,
        location=LOCATION,
        sign_pem=priv_pem,
        output_dir=out_b,
        dry_run=True,
    )
    skill_meta_b = res_b["plans"][0]["skills"][0]["frontmatter_metadata"]
    assert skill_meta_b["source_repo"] == "https://gitlab.example.com/sec/cred-plugin.git"
    assert "glpat-leak-me-not" not in json.dumps(res_b)

    zip_b = (out_b / "cred-plugin" / "audit-two.zip").read_bytes()
    ok_b, reason_b = verify_skill_zip_signature(zip_b, pub_pem)
    assert ok_b, reason_b
    with zipfile.ZipFile(io.BytesIO(zip_b), "r") as zf:
        raw_md = zf.read("audit-two/SKILL.md").decode("utf-8")
        assert "glpat-leak-me-not" not in raw_md
        fm_b, _b, err_b = parse_frontmatter(raw_md)
        assert err_b is None and fm_b is not None
        assert fm_b["metadata"]["source_repo"] == "https://gitlab.example.com/sec/cred-plugin.git"


def test_clone_git_repository_security_hardening_and_origin_scoped_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin_repo = tmp_path / "safe-repo"
    origin_repo.mkdir()
    _git(origin_repo, "init", "-b", "main")
    _write_minimal_skill(origin_repo / "skills" / "ok-skill", "ok-skill")
    _git(origin_repo, "add", ".")
    _git(origin_repo, "commit", "-m", "init")

    # 1. Reject ext:: protocol and leading '-' option injection in repo_url and ref
    with pytest.raises(PluginSyncError, match="ext::"):
        with clone_git_repository("ext::sh -c id"):
            pass
    with pytest.raises(PluginSyncError, match="must not start with '-'"):
        with clone_git_repository("--upload-pack=evil"):
            pass
    with pytest.raises(PluginSyncError, match="must not start with '-'"):
        with clone_git_repository(str(origin_repo), ref="--exec=evil"):
            pass

    # 2. Reject control characters (CR, LF, NUL) in repo_url, ref, and token_env value
    with pytest.raises(PluginSyncError, match="control characters"):
        with clone_git_repository("https://example.com/org/repo.git\nX-Injected: 1"):
            pass
    with pytest.raises(PluginSyncError, match="control characters"):
        with clone_git_repository(str(origin_repo), ref="main\r\n--evil"):
            pass
    monkeypatch.setenv("BAD_CTRL_TOKEN", "secret-val\r\nX-Evil: 1")
    with pytest.raises(PluginSyncError, match="control characters") as ctrl_exc:
        with clone_git_repository(str(origin_repo), token_env="BAD_CTRL_TOKEN"):
            pass
    assert "secret-val" not in str(ctrl_exc.value)

    # 3. Embedded credentials scrubbed from errors even without token_env and with whitespace
    embedded_secret = "super-hidden-pass-42"
    with pytest.raises(PluginSyncError) as scrub_exc:
        with clone_git_repository(
            f"  http://botuser:{embedded_secret}@127.0.0.1:1/org/repo.git  ",
            timeout=10,
        ):
            pass
    assert embedded_secret not in str(scrub_exc.value)
    assert "botuser" not in str(scrub_exc.value)

    # 4. Origin-scoped http.<origin>/.extraHeader sends Authorization to target origin only
    received_headers: list[dict[str, str]] = []

    class _AuthCaptureHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            received_headers.append({k.lower(): v for k, v in self.headers.items()})
            self.send_response(401)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _AuthCaptureHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        tok = "scoped-token-xyz"
        monkeypatch.setenv("SCOPED_HTTP_TOKEN", tok)
        with pytest.raises(PluginSyncError) as http_exc:
            with clone_git_repository(
                f"http://127.0.0.1:{port}/org/private-skills.git",
                token_env="SCOPED_HTTP_TOKEN",
                timeout=10,
            ):
                pass
        assert tok not in str(http_exc.value)
        assert received_headers, "Expected git to hit local HTTP server"
        expected_auth = "Basic " + base64.b64encode(f"oauth2:{tok}".encode("utf-8")).decode("ascii")
        assert received_headers[0].get("authorization") == expected_auth

        # Confirm git URL matching scopes the header strictly to this origin (not another host)
        origin_scope = f"http://127.0.0.1:{port}/"
        scoped_env = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{origin_scope}.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: {expected_auth}",
        }
        same_origin = subprocess.run(
            ["git", "config", "--get-urlmatch", "http.extraHeader", f"http://127.0.0.1:{port}/a/b"],
            env=scoped_env,
            capture_output=True,
            text=True,
            check=False,
        )
        other_origin = subprocess.run(
            ["git", "config", "--get-urlmatch", "http.extraHeader", "https://other.example.com/a/b"],
            env=scoped_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert same_origin.returncode == 0 and expected_auth in same_origin.stdout
        assert other_origin.returncode != 0 and expected_auth not in other_origin.stdout
    finally:
        server.shutdown()
        server.server_close()


def test_ingest_source_cloud_kms_signing_and_hybrid_layout(tmp_path: Path) -> None:
    priv_pem, pub_pem = _generate_ecdsa_p256_keypair(tmp_path, prefix="kms_ingest")
    private_key = serialization.load_pem_private_key(priv_pem.read_bytes(), password=None)
    assert isinstance(private_key, ec.EllipticCurvePrivateKey)

    kms_calls: list[str] = []

    def kms_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":asymmetricSign")
        kms_calls.append(request.url.path)
        payload = json.loads(request.content.decode("utf-8"))
        digest = base64.b64decode(payload["digest"]["sha256"])
        sig_der = private_key.sign(
            digest, ec.ECDSA(utils.Prehashed(hashes.SHA256()), deterministic_signing=True)
        )
        return httpx.Response(200, json={"signature": base64.b64encode(sig_der).decode("ascii")})

    kms_ver = (
        f"projects/{PROJECT_ID}/locations/{LOCATION}/keyRings/workshop/"
        "cryptoKeys/skill-signer/cryptoKeyVersions/1"
    )
    kms_out = tmp_path / "kms-dist"
    with httpx.Client(transport=httpx.MockTransport(kms_handler)) as kms_http:
        kms_summary = ingest_source(
            source_type="local",
            path=LOCAL_SKILLS_DIR,
            plugin_name="local-ops-toolkit",
            project=PROJECT_ID,
            location=LOCATION,
            kms_key_version=kms_ver,
            auth_token="fake-kms-bearer",
            output_dir=kms_out,
            dry_run=True,
            http_client=kms_http,
        )
    assert len(kms_calls) == 3
    assert kms_summary["skills_count"] == 3
    for skill_name in ("cloud-run-debugger", "terraform-iam-auditor", "incident-postmortem-writer"):
        zpath = kms_out / "local-ops-toolkit" / f"{skill_name}.zip"
        ok, reason = verify_skill_zip_signature(zpath.read_bytes(), pub_pem)
        assert ok, reason

    # Hybrid layout ingestion (plugin with MCP server + standalone skills)
    hybrid_root = tmp_path / "hybrid-catalog"
    plug_dir = _write_minimal_plugin(
        hybrid_root / "plugins" / "platform-pack", "platform-pack", "pack-skill"
    )
    (plug_dir / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "platform-mcp": {
                        "type": "streamable-http",
                        "url": "https://platform-mcp.example.com/mcp",
                    }
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_minimal_skill(hybrid_root / "skills" / "solo-one", "solo-one")
    _write_minimal_skill(hybrid_root / "skills" / "solo-two", "solo-two")

    hybrid_out = tmp_path / "hybrid-dist"
    hybrid_summary = ingest_source(
        source_type="local",
        path=hybrid_root,
        plugin_name="hybrid-standalone-pack",
        project=PROJECT_ID,
        location=LOCATION,
        output_dir=hybrid_out,
        probe_mcp=False,
        dry_run=True,
    )
    assert hybrid_summary["layout"] == "hybrid"
    assert hybrid_summary["plans_count"] == 2
    assert hybrid_summary["skills_count"] == 3
    assert hybrid_summary["mcp_services_count"] == 1
    assert (hybrid_out / "platform-pack" / "pack-skill.zip").is_file()
    assert (hybrid_out / "hybrid-standalone-pack" / "solo-one.zip").is_file()
    assert (hybrid_out / "hybrid-standalone-pack" / "solo-two.zip").is_file()
    assert (hybrid_out / "ingest-summary.json").is_file()

    # Same hybrid layout with only_skills=True skips MCP service registration
    hybrid_only = ingest_source(
        source_type="local",
        path=hybrid_root,
        plugin_name="hybrid-standalone-pack",
        only_skills=True,
        project=PROJECT_ID,
        location=LOCATION,
        dry_run=True,
    )
    assert hybrid_only["layout"] == "hybrid"
    assert hybrid_only["skills_count"] == 3
    assert hybrid_only["mcp_services_count"] == 0
    assert hybrid_only["agent_services_count"] == 0
