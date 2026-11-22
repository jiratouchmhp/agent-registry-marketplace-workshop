"""Comprehensive tests for Track 1A: starter-marketplace, sample MCP/A2A apps, and plugin_sync."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from schemas.validators import (
    parse_frontmatter,
    validate_agent_plugin_bundle,
    validate_antigravity_bundle,
    validate_claude_plugin_bundle,
    validate_codex_plugin_bundle,
    validate_copilot_plugin_bundle,
)
from tools.plugin_sync import (
    PluginSyncError,
    SkillArchiveError,
    apply_sync_plan,
    build_skill_zip,
    build_sync_plan,
    canonical_private_skill_id,
    compute_canonical_skill_payload,
    raw_create_skill_id,
    validate_skill_zip,
    verify_skill_zip_signature,
)
from tools.plugin_sync.cli import cli
from middleware.app.security_verifier import (
    compute_canonical_skill_payload as middleware_canonical_payload,
    load_kms_public_key,
    verify_ecdsa_p256_signature,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STARTER_MARKETPLACE_DIR = REPO_ROOT / "starter-marketplace"
STARTER_PLUGIN_DIR = STARTER_MARKETPLACE_DIR / "plugins" / "gcp-finops-toolkit"
MCP_APP_PATH = REPO_ROOT / "samples" / "mcp-server" / "app.py"
A2A_APP_PATH = REPO_ROOT / "samples" / "a2a-agent" / "app.py"


def _load_module_from_path(module_name: str, file_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _generate_ecdsa_p256_keypair(tmp_path: Path, prefix: str = "signing") -> tuple[Path, Path]:
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


# ---------------------------------------------------------------------------
# 1. Starter Marketplace multi-tool plugin & GitLab CI validation
# ---------------------------------------------------------------------------


def test_starter_plugin_passes_all_five_ide_validators() -> None:
    for name, validator in [
        ("agent-plugins-1.0", validate_agent_plugin_bundle),
        ("antigravity", validate_antigravity_bundle),
        ("claude-code", validate_claude_plugin_bundle),
        ("copilot", validate_copilot_plugin_bundle),
        ("codex", validate_codex_plugin_bundle),
    ]:
        res = validator(STARTER_PLUGIN_DIR)
        assert res.valid, f"Starter plugin failed {name} validation: {res.errors}"


def test_starter_plugin_required_files_and_contracts() -> None:
    spec = json.loads((REPO_ROOT / "WORKSHOP_SPEC.json").read_text(encoding="utf-8"))

    # plugin.json (Agent Plugins 1.0)
    manifest = json.loads((STARTER_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert manifest["name"] == spec["sample_plugin"]["name"]
    assert manifest["version"] == spec["sample_plugin"]["version"]
    assert isinstance(manifest.get("extensions"), dict)

    # README & 128x128 SVG logo
    assert (STARTER_PLUGIN_DIR / "README.md").is_file()
    logo_text = (STARTER_PLUGIN_DIR / "assets" / "logo.svg").read_text(encoding="utf-8")
    assert 'viewBox="0 0 128 128"' in logo_text
    assert 'width="128"' in logo_text and 'height="128"' in logo_text

    # mcp.json: streamable-http finops-mcp-server with NO ${ENV} interpolation
    mcp_raw = (STARTER_PLUGIN_DIR / "mcp.json").read_text(encoding="utf-8")
    assert "${" not in mcp_raw
    mcp_doc = json.loads(mcp_raw)
    assert mcp_doc["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    finops_srv = mcp_doc["mcpServers"]["finops-mcp-server"]
    assert finops_srv["type"] == "streamable-http"
    assert finops_srv["url"].endswith("/mcp")

    # Skills + references
    assert (
        STARTER_PLUGIN_DIR / "skills" / "cost-anomaly-triage" / "references" / "triage-checklist.md"
    ).is_file()
    for skill_name in spec["sample_plugin"]["skills"]:
        skill_md = STARTER_PLUGIN_DIR / "skills" / skill_name / "SKILL.md"
        assert skill_md.is_file()
        fm, _body, err = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        assert err is None and fm is not None
        assert fm["name"] == skill_name
        assert fm.get("license")
        assert fm.get("compatibility")
        assert isinstance(fm.get("metadata"), dict)

    # A2A v1.0 Agent Card <= 10 KB
    card_path = STARTER_PLUGIN_DIR / "agents" / "finops-advisor" / "agent-card.json"
    card_bytes = card_path.read_bytes()
    assert len(card_bytes) <= 10 * 1024
    card = json.loads(card_bytes.decode("utf-8"))
    for key in (
        "name",
        "supportedInterfaces",
        "capabilities",
        "defaultInputModes",
        "defaultOutputModes",
        "skills",
    ):
        assert key in card
    assert len(card["supportedInterfaces"]) >= 1
    assert len(card["skills"]) >= 1

    # Rules frontmatter: trigger: glob + comma-separated string globs
    rule_text = (STARTER_PLUGIN_DIR / "rules" / "finops-guardrails.md").read_text(encoding="utf-8")
    rule_fm, _body, rule_err = parse_frontmatter(rule_text)
    assert rule_err is None and rule_fm is not None
    assert rule_fm["trigger"] == "glob"
    assert isinstance(rule_fm["globs"], str)
    assert rule_fm["globs"] == "**/*.sql, **/*.tf"


def test_gitlab_ci_pipeline_structure_and_jq_filter(tmp_path: Path) -> None:
    ci_path = STARTER_MARKETPLACE_DIR / ".gitlab-ci.yml"
    ci_text = ci_path.read_text(encoding="utf-8")
    ci = yaml.safe_load(ci_text)

    assert ci["stages"] == ["validate", "package-and-sign", "register-registry"]
    assert set(ci["package-and-sign"]["needs"]) == {"validate:schemas", "validate:antigravity-cli"}
    assert "schemas.validators" in ci_text
    assert "agy plugin validate --json" in ci_text
    assert "|| agy_rc=$?" in ci_text
    assert "(.findings // [])[]" in ci_text
    assert ".mcp.json" in ci_text
    assert "mcp.json" in ci_text
    assert "AGENTS.md" in ci_text
    assert "takes precedence" in ci_text
    assert "TARGET_STATE_DRAFT" in ci_text
    assert "TARGET_STATE_ACTIVE" in ci_text
    assert "tools.plugin_sync" in ci_text
    assert "WORKSHOP_TOOLS_PACKAGE" in ci_text
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in ci_text
    assert "sts.googleapis.com/v1/token" in ci_text
    assert "AGENT_REGISTRY_AUTH_TOKEN" in ci_text

    # Ensure packaging & registration jobs iterate dynamically over RESOLVED_PLUGINS_DIR
    # and configure GitLab OIDC id_tokens for keyless GCP WIF
    for job_name in ("package-and-sign", "register:draft-mr", "register:active-release"):
        job_script = "\n".join(ci[job_name]["script"])
        assert 'for plugin_dir in "${RESOLVED_PLUGINS_DIR}"/*/;' in job_script
        assert "GCP_OIDC_TOKEN" in ci[job_name].get("id_tokens", {})

    # Syntax-check EVERY CI job script with `bash -n`
    for job_name in (
        "validate:schemas",
        "validate:antigravity-cli",
        "package-and-sign",
        "register:draft-mr",
        "register:active-release",
    ):
        joined_script = "\n".join(ci[job_name]["script"])
        syntax_proc = subprocess.run(
            ["bash", "-n"], input=joined_script, text=True, capture_output=True
        )
        assert syntax_proc.returncode == 0, (
            f"GitLab CI job {job_name!r} failed bash -n syntax check:\n{syntax_proc.stderr}"
        )

    # Execute validate:schemas script cleanly from BOTH monorepo root AND standalone starter-marketplace/
    validate_script = "\n".join(ci["validate:schemas"]["script"])
    base_env = {
        **dict(os.environ),
        "PYTHON_BIN": sys.executable,
        "PYTHONPATH": str(REPO_ROOT),
    }
    for layout_label, cwd in (
        ("monorepo", REPO_ROOT),
        ("standalone", STARTER_MARKETPLACE_DIR),
    ):
        proc = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", validate_script],
            cwd=str(cwd),
            env=base_env,
            text=True,
            capture_output=True,
        )
        assert proc.returncode == 0, (
            f"validate:schemas failed in {layout_label} layout:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
        assert "[PASS]" in proc.stdout

    # Execute package-and-sign job script (--dry-run) from monorepo root into tmp_path/dist
    pkg_script = "\n".join(ci["package-and-sign"]["script"])
    pkg_dist = tmp_path / "ci-dist"
    pkg_env = {
        **base_env,
        "CI_DRY_RUN_ONLY": "true",
        "AGENT_REGISTRY_BASE_URL": "https://agentregistry.googleapis.com/v1",
        "GCP_PROJECT_ID": "acme-agent-governance",
        "GCP_LOCATION": "asia-southeast3",
        "SCANNING_POLICY": "ENABLED_BLOCKING",
        "DIST_DIR": str(pkg_dist),
    }
    pkg_env.pop("KMS_SIGNING_KEY_VERSION", None)
    pkg_env.pop("SIGNING_KEY_PEM_PATH", None)
    pkg_proc = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", pkg_script],
        cwd=str(REPO_ROOT),
        env=pkg_env,
        text=True,
        capture_output=True,
    )
    assert pkg_proc.returncode == 0, (
        f"package-and-sign failed:\nSTDOUT:\n{pkg_proc.stdout}\nSTDERR:\n{pkg_proc.stderr}"
    )
    assert (pkg_dist / "gcp-finops-toolkit" / "cost-anomaly-triage.zip").is_file()
    assert (pkg_dist / "gcp-finops-toolkit" / "sync-plan.json").is_file()

    # Verify register:draft-mr fails closed cleanly when targeting googleapis.com without WIF or token
    auth_guard_script = ci["register:draft-mr"]["script"][2]
    no_auth_env = {
        **base_env,
        "AGENT_REGISTRY_BASE_URL": "https://agentregistry.googleapis.com/v1",
        "CI_DRY_RUN_ONLY": "false",
    }
    for key in (
        "AGENT_REGISTRY_AUTH_TOKEN",
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "GCP_OIDC_TOKEN",
        "GCP_WORKLOAD_IDENTITY_PROVIDER",
        "GCP_SERVICE_ACCOUNT_EMAIL",
    ):
        no_auth_env.pop(key, None)
    guard_proc = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", auth_guard_script],
        cwd=str(REPO_ROOT),
        env=no_auth_env,
        text=True,
        capture_output=True,
    )
    assert guard_proc.returncode != 0
    assert "Workload Identity Federation" in guard_proc.stderr

    # Verify the jq multi-tool filter handles benign warnings, missing/null findings, and errors
    jq_filter = (
        '[(.findings // [])[] | select(.severity == "error" or ('
        '.severity == "warning" and (.file != ".mcp.json") and (.file != "mcp.json") '
        'and (.file != "AGENTS.md") and ((.file == "plugin.json" and '
        '(.message | contains("takes precedence"))) | not)))] | length == 0'
    )
    if subprocess.run(["which", "jq"], capture_output=True).returncode == 0:
        benign_report = json.dumps(
            {
                "findings": [
                    {"severity": "warning", "file": ".mcp.json", "message": "never loaded"},
                    {"severity": "warning", "file": "mcp.json", "message": "never loaded"},
                    {"severity": "warning", "file": "AGENTS.md", "message": "never loaded"},
                    {
                        "severity": "warning",
                        "file": "plugin.json",
                        "message": "ignored: .antigravity-plugin/plugin.json takes precedence",
                    },
                    {"severity": "note", "file": "hooks.json", "message": "info"},
                ]
            }
        )
        ok_proc = subprocess.run(
            ["jq", "-e", jq_filter], input=benign_report, text=True, capture_output=True
        )
        assert ok_proc.returncode == 0

        for empty_report in ("{}", '{"findings": null}'):
            empty_proc = subprocess.run(
                ["jq", "-e", jq_filter], input=empty_report, text=True, capture_output=True
            )
            assert empty_proc.returncode == 0

        real_error_report = json.dumps(
            {
                "findings": [
                    {"severity": "error", "file": "hooks.json", "message": "broken hook"},
                ]
            }
        )
        err_proc = subprocess.run(
            ["jq", "-e", jq_filter], input=real_error_report, text=True, capture_output=True
        )
        assert err_proc.returncode != 0


# ---------------------------------------------------------------------------
# 2. Sample FastAPI Streamable HTTP MCP Server tests
# ---------------------------------------------------------------------------


def test_sample_mcp_server_endpoints_and_annotations() -> None:
    assert (REPO_ROOT / "samples" / "mcp-server" / "Dockerfile").is_file()
    assert (REPO_ROOT / "samples" / "mcp-server" / "requirements.txt").is_file()

    mcp_mod = _load_module_from_path("sample_mcp_app", MCP_APP_PATH)
    client = TestClient(mcp_mod.app)

    # Health check
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["tool_count"] == 3

    # initialize
    init_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "pytest"}},
        },
    )
    assert init_resp.status_code == 200
    assert "mcp-session-id" in init_resp.headers
    init_body = init_resp.json()
    assert init_body["result"]["serverInfo"]["name"] == "finops-mcp-server"

    # notifications/initialized
    notif_resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notif_resp.status_code == 202

    # tools/list + annotations check + <= 10 KB TOOL_SPEC constraint
    list_resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert list_resp.status_code == 200
    tools_result = list_resp.json()["result"]
    assert len(json.dumps(tools_result).encode("utf-8")) <= 10 * 1024

    tools_by_name = {t["name"]: t for t in tools_result["tools"]}
    assert set(tools_by_name) == {
        "get_billing_summary",
        "analyze_bq_query",
        "delete_idle_dataset",
    }

    assert tools_by_name["get_billing_summary"]["annotations"]["readOnlyHint"] is True
    assert tools_by_name["get_billing_summary"]["annotations"]["destructiveHint"] is False
    assert tools_by_name["get_billing_summary"]["annotations"]["idempotentHint"] is True

    assert tools_by_name["analyze_bq_query"]["annotations"]["readOnlyHint"] is True
    assert tools_by_name["analyze_bq_query"]["annotations"]["destructiveHint"] is False
    assert tools_by_name["analyze_bq_query"]["annotations"]["idempotentHint"] is True

    assert tools_by_name["delete_idle_dataset"]["annotations"]["readOnlyHint"] is False
    assert tools_by_name["delete_idle_dataset"]["annotations"]["destructiveHint"] is True
    assert tools_by_name["delete_idle_dataset"]["annotations"]["idempotentHint"] is False

    # tools/call: get_billing_summary
    bill_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_billing_summary",
                "arguments": {"project_id": "acme-agent-governance", "days": 7},
            },
        },
    ).json()
    assert bill_resp["result"]["isError"] is False
    bill_data = bill_resp["result"]["structuredContent"]
    assert bill_data["project_id"] == "acme-agent-governance"
    assert bill_data["anomaly_detected"] is True
    assert len(bill_data["top_skus"]) >= 3

    # tools/call: analyze_bq_query (bad SQL vs optimized SQL)
    bad_sql_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "analyze_bq_query",
                "arguments": {"sql": "SELECT * FROM analytics.events CROSS JOIN analytics.users"},
            },
        },
    ).json()
    bad_data = bad_sql_resp["result"]["structuredContent"]
    codes = {f["code"] for f in bad_data["findings"]}
    assert "BQ_SELECT_STAR" in codes
    assert "BQ_MISSING_PARTITION_FILTER" in codes
    assert "BQ_CROSS_JOIN" in codes
    assert bad_data["safe_to_run"] is False

    good_sql_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "analyze_bq_query",
                "arguments": {
                    "sql": "SELECT user_id, cost_usd FROM analytics.events WHERE _PARTITIONDATE = CURRENT_DATE()"
                },
            },
        },
    ).json()
    assert good_sql_resp["result"]["structuredContent"]["safe_to_run"] is True

    # tools/call: delete_idle_dataset rejects string "false" for confirm_delete
    bad_bool_del = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 60,
            "method": "tools/call",
            "params": {
                "name": "delete_idle_dataset",
                "arguments": {
                    "project_id": "acme-agent-governance",
                    "dataset_id": "scratch_2024",
                    "confirm_delete": "false",
                },
            },
        },
    ).json()
    assert bad_bool_del["result"]["isError"] is True
    assert "must be a boolean" in bad_bool_del["result"]["content"][0]["text"]

    # tools/call: delete_idle_dataset (dry-run vs confirmed)
    dry_del = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "delete_idle_dataset",
                "arguments": {
                    "project_id": "acme-agent-governance",
                    "dataset_id": "scratch_2024",
                    "confirm_delete": False,
                },
            },
        },
    ).json()
    assert dry_del["result"]["structuredContent"]["status"] == "dry_run_confirmation_required"

    real_del = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "delete_idle_dataset",
                "arguments": {
                    "project_id": "acme-agent-governance",
                    "dataset_id": "scratch_2024",
                    "confirm_delete": True,
                    "reason": "CHG-1042 approved",
                },
            },
        },
    ).json()
    assert real_del["result"]["structuredContent"]["status"] == "deleted"
    assert real_del["result"]["structuredContent"]["destructive"] is True


# ---------------------------------------------------------------------------
# 3. Sample FastAPI A2A v1.0 Agent tests
# ---------------------------------------------------------------------------


def test_sample_a2a_agent_card_and_task_execution() -> None:
    assert (REPO_ROOT / "samples" / "a2a-agent" / "Dockerfile").is_file()
    assert (REPO_ROOT / "samples" / "a2a-agent" / "requirements.txt").is_file()

    a2a_mod = _load_module_from_path("sample_a2a_app", A2A_APP_PATH)
    client = TestClient(a2a_mod.app)

    assert client.get("/healthz").status_code == 200

    # /.well-known/agent-card.json
    card_resp = client.get("/.well-known/agent-card.json")
    assert card_resp.status_code == 200
    assert len(card_resp.content) <= 10 * 1024
    card = card_resp.json()
    assert card["name"] == "GCP FinOps Advisor"
    bindings = {iface["protocolBinding"] for iface in card["supportedInterfaces"]}
    assert {"JSONRPC", "HTTP+JSON"} <= bindings
    assert all(iface["protocolVersion"] == "1.0" for iface in card["supportedInterfaces"])
    assert "streaming" in card["capabilities"]
    assert "application/json" in card["defaultOutputModes"]
    assert len(card["skills"]) >= 2

    # POST /a2a via JSON-RPC 2.0
    rpc_resp = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "Optimize SELECT * FROM billing.events"}],
                }
            },
        },
    )
    assert rpc_resp.status_code == 200
    rpc_body = rpc_resp.json()
    task = rpc_body["result"]
    assert task["status"]["state"] == "completed"
    data_parts = [p["data"] for p in task["artifacts"][0]["parts"] if p.get("kind") == "data"]
    assert data_parts[0]["category"] == "bq-query-optimization"
    assert len(data_parts[0]["recommendations"]) >= 2

    # Retrieve via tasks/get JSON-RPC and REST
    get_rpc = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": "req-2", "method": "tasks/get", "params": {"id": task["id"]}},
    ).json()
    assert get_rpc["result"]["id"] == task["id"]
    assert client.get(f"/a2a/tasks/{task['id']}").status_code == 200

    # POST /a2a via HTTP+JSON
    http_resp = client.post(
        "/a2a",
        json={"prompt": "Triage a 45% Cloud Billing spike in acme-analytics"},
    )
    assert http_resp.status_code == 200
    http_task = http_resp.json()
    assert http_task["status"]["state"] == "completed"
    http_data = [p["data"] for p in http_task["artifacts"][0]["parts"] if p.get("kind") == "data"][0]
    assert http_data["category"] == "cost-anomaly-triage"
    assert http_data["project_id"] == "acme-analytics"


# ---------------------------------------------------------------------------
# 4. plugin_sync ZIP builder, constraints, frontmatter injection & ECDSA signing
# ---------------------------------------------------------------------------


def test_skill_zip_builder_deterministic_metadata_and_ecdsa_signing(tmp_path: Path) -> None:
    priv_pem, pub_pem = _generate_ecdsa_p256_keypair(tmp_path)
    skill_dir = STARTER_PLUGIN_DIR / "skills" / "cost-anomaly-triage"

    inject_meta = {
        "plugin_name": "gcp-finops-toolkit",
        "plugin_display_name": "GCP FinOps Toolkit",
        "plugin_version": "1.0.0",
        "source_repo": "https://gitlab.acme.example.com/platform/starter-marketplace",
        "git_commit": "0123456789abcdef",
        "mcp_service_id": "finops-mcp-server",
        "agent_service_id": "finops-advisor-agent",
    }
    kms_ver = "projects/acme-agent-governance/locations/us-central1/keyRings/finops/cryptoKeys/signer/cryptoKeyVersions/1"

    # Unsigned builds are byte-for-byte deterministic
    unsigned_1 = build_skill_zip(skill_dir, inject_meta)
    unsigned_2 = build_skill_zip(skill_dir, inject_meta)
    assert unsigned_1.zip_bytes == unsigned_2.zip_bytes
    assert unsigned_1.sha256_hex == unsigned_2.sha256_hex

    # Signed builds are byte-for-byte deterministic (RFC 6979 ECDSA P-256)
    signed = build_skill_zip(
        skill_dir,
        inject_meta,
        sign_pem=priv_pem,
        kms_key_version=kms_ver,
    )
    signed_repeat = build_skill_zip(
        skill_dir,
        inject_meta,
        sign_pem=priv_pem,
        kms_key_version=kms_ver,
    )
    assert signed.kms_signature == signed_repeat.kms_signature
    assert signed.zip_bytes == signed_repeat.zip_bytes
    assert signed.sha256_hex == signed_repeat.sha256_hex
    assert signed.raw_skill_id == "cost-anomaly-triage"
    assert signed.private_skill_id == "private-cost-anomaly-triage"
    assert signed.kms_signature is not None
    assert signed.kms_key_version == kms_ver

    # Non-P-256 EC curve must be rejected
    p384_key = ec.generate_private_key(ec.SECP384R1())
    p384_path = tmp_path / "p384_private.pem"
    p384_path.write_bytes(
        p384_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    with pytest.raises(PluginSyncError, match="SECP256R1"):
        build_skill_zip(skill_dir, inject_meta, sign_pem=p384_path)

    with zipfile.ZipFile(io.BytesIO(signed.zip_bytes), "r") as zf:
        names = sorted(zf.namelist())
        assert names == [
            "cost-anomaly-triage/SKILL.md",
            "cost-anomaly-triage/references/triage-checklist.md",
        ]
        fm, _body, err = parse_frontmatter(zf.read("cost-anomaly-triage/SKILL.md").decode("utf-8"))
        assert err is None and fm is not None
        meta = fm["metadata"]
        spec = json.loads((REPO_ROOT / "WORKSHOP_SPEC.json").read_text(encoding="utf-8"))
        for req_key in spec["skill_metadata_keys"]:
            assert req_key in meta and isinstance(meta[req_key], str) and meta[req_key]

    # Signature verification succeeds with BOTH plugin_sync AND Middleware security_verifier
    ok, reason = verify_skill_zip_signature(signed.zip_bytes, pub_pem)
    assert ok, reason

    mw_pub_key = load_kms_public_key(pem_path=str(pub_pem))
    for starter_skill_name in ("cost-anomaly-triage", "bq-query-optimizer"):
        pkg = build_skill_zip(
            STARTER_PLUGIN_DIR / "skills" / starter_skill_name,
            inject_meta,
            sign_pem=priv_pem,
            kms_key_version=kms_ver,
        )
        assert compute_canonical_skill_payload(pkg.zip_bytes) == middleware_canonical_payload(
            pkg.zip_bytes
        )
        assert verify_skill_zip_signature(pkg.zip_bytes, pub_pem)[0] is True
        assert pkg.kms_signature is not None
        assert (
            verify_ecdsa_p256_signature(
                pkg.zip_bytes, pkg.kms_signature, mw_pub_key, sha256_hex=pkg.sha256_hex
            )
            is True
        )

    # Wrong public key fails in both verifiers
    _, other_pub = _generate_ecdsa_p256_keypair(tmp_path, prefix="other")
    ok_wrong, _ = verify_skill_zip_signature(signed.zip_bytes, other_pub)
    assert not ok_wrong
    assert (
        verify_ecdsa_p256_signature(
            signed.zip_bytes,
            signed.kms_signature,
            load_kms_public_key(pem_path=str(other_pub)),
        )
        is False
    )

    # Tampering with a companion reference file inside the ZIP invalidates both verifiers
    tampered_buf = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(signed.zip_bytes), "r") as src_zf,
        zipfile.ZipFile(tampered_buf, "w", compression=zipfile.ZIP_DEFLATED) as dst_zf,
    ):
        for info in src_zf.infolist():
            content = src_zf.read(info)
            if info.filename.endswith("triage-checklist.md"):
                content += b"\n- [ ] Tampered step\n"
            dst_zf.writestr(info, content)
    tampered_bytes = tampered_buf.getvalue()
    ok_tampered, _ = verify_skill_zip_signature(tampered_bytes, pub_pem)
    assert not ok_tampered
    assert (
        verify_ecdsa_p256_signature(tampered_bytes, signed.kms_signature, mw_pub_key) is False
    )


def test_skill_zip_validation_constraints(tmp_path: Path) -> None:
    # 1. Lowercase skill.md rejected
    bad_case = tmp_path / "bad-case"
    bad_case.mkdir()
    (bad_case / "skill.md").write_text("---\nname: bad-case\ndescription: x\n---\n", encoding="utf-8")
    with pytest.raises(SkillArchiveError, match="uppercase 'SKILL.md'"):
        build_skill_zip(bad_case, {})

    # 2. Nested duplicate SKILL.md rejected
    dup_dir = tmp_path / "dup-skill"
    (dup_dir / "nested").mkdir(parents=True)
    (dup_dir / "SKILL.md").write_text("---\nname: dup-skill\ndescription: ok\n---\n", encoding="utf-8")
    (dup_dir / "nested" / "SKILL.md").write_text("---\nname: nested\ndescription: dup\n---\n", encoding="utf-8")
    with pytest.raises(SkillArchiveError, match="Nested 'SKILL.md' forbidden"):
        build_skill_zip(dup_dir, {})

    # 3. Symlink rejected
    sym_dir = tmp_path / "sym-skill"
    sym_dir.mkdir()
    (sym_dir / "SKILL.md").write_text("---\nname: sym-skill\ndescription: ok\n---\n", encoding="utf-8")
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    (sym_dir / "leak.txt").symlink_to(target)
    with pytest.raises(SkillArchiveError, match="Symlink"):
        build_skill_zip(sym_dir, {})

    # 4. Nesting depth > 8 rejected
    deep_dir = tmp_path / "deep-skill"
    nested = deep_dir / "a" / "b" / "c" / "d" / "e" / "f" / "g"
    nested.mkdir(parents=True)
    (deep_dir / "SKILL.md").write_text("---\nname: deep-skill\ndescription: ok\n---\n", encoding="utf-8")
    (nested / "too-deep.txt").write_text("depth 9", encoding="utf-8")
    with pytest.raises(SkillArchiveError, match="maximum nesting depth 8"):
        build_skill_zip(deep_dir, {})

    # 5. Path traversal ('..') and raw symlink in externally crafted ZIP rejected
    traversal_buf = io.BytesIO()
    with zipfile.ZipFile(traversal_buf, "w") as zf:
        zf.writestr("s/SKILL.md", "---\nname: s\ndescription: ok\n---\n")
        zf.writestr("s/../../etc/passwd", "root:x:0:0")
    with pytest.raises(SkillArchiveError, match="Path traversal"):
        validate_skill_zip(traversal_buf.getvalue())

    symlink_zip_buf = io.BytesIO()
    with zipfile.ZipFile(symlink_zip_buf, "w") as zf:
        zf.writestr("s/SKILL.md", "---\nname: s\ndescription: ok\n---\n")
        zi = zipfile.ZipInfo("s/link")
        zi.external_attr = (0o120777) << 16
        zf.writestr(zi, "/etc/passwd")
    with pytest.raises(SkillArchiveError, match="Symlink"):
        validate_skill_zip(symlink_zip_buf.getvalue())

    # 6. Per-file > 1 MB rejected
    big_dir = tmp_path / "big-skill"
    big_dir.mkdir()
    (big_dir / "SKILL.md").write_text("---\nname: big-skill\ndescription: ok\n---\n", encoding="utf-8")
    (big_dir / "huge.bin").write_bytes(b"A" * (1024 * 1024 + 10))
    with pytest.raises(SkillArchiveError, match="1 MB uncompressed file limit"):
        build_skill_zip(big_dir, {})

    # 7. Total uncompressed > 10 MB rejected (11 files x 960 KB = ~10.3 MB, compresses tiny)
    total_big_dir = tmp_path / "total-big-skill"
    total_big_dir.mkdir()
    (total_big_dir / "SKILL.md").write_text(
        "---\nname: total-big-skill\ndescription: ok\n---\n", encoding="utf-8"
    )
    chunk = b"0" * (960 * 1024)
    for idx in range(11):
        (total_big_dir / f"part_{idx}.txt").write_bytes(chunk)
    with pytest.raises(SkillArchiveError, match="exceeds 10 MB limit"):
        build_skill_zip(total_big_dir, {})

    # 8. Compressed ZIP > 1 MB rejected
    with pytest.raises(SkillArchiveError, match="exceeds Agent Registry 1 MB limit"):
        validate_skill_zip(b"Z" * (1024 * 1024 + 1))

    # 9. Stray root-level file alongside <dir>/SKILL.md rejected
    stray_buf = io.BytesIO()
    with zipfile.ZipFile(stray_buf, "w") as zf:
        zf.writestr("my-skill/SKILL.md", "---\nname: my-skill\ndescription: ok\n---\n")
        zf.writestr("stray.txt", "unexpected root file")
    with pytest.raises(SkillArchiveError, match="Stray root-level files forbidden"):
        validate_skill_zip(stray_buf.getvalue())

    # 10. Standalone validate_skill_zip validates SKILL.md YAML frontmatter
    bad_fm_buf = io.BytesIO()
    with zipfile.ZipFile(bad_fm_buf, "w") as zf:
        zf.writestr(
            "my-skill/SKILL.md",
            "---\nname: my-skill\ndescription: ok\nmetadata:\n  nested: [1, 2]\n---\n",
        )
    with pytest.raises(SkillArchiveError, match="scalar string"):
        validate_skill_zip(bad_fm_buf.getvalue())


def test_kms_asymmetric_sign_rest_integration(tmp_path: Path) -> None:
    priv_pem, pub_pem = _generate_ecdsa_p256_keypair(tmp_path, prefix="kms_sim")
    private_key = serialization.load_pem_private_key(priv_pem.read_bytes(), password=None)
    assert isinstance(private_key, ec.EllipticCurvePrivateKey)

    kms_calls: list[dict[str, Any]] = []

    def kms_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":asymmetricSign")
        payload = json.loads(request.content.decode("utf-8"))
        kms_calls.append(payload)
        digest = base64.b64decode(payload["digest"]["sha256"])
        assert len(digest) == 32
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import utils

        sig_der = private_key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return httpx.Response(
            200,
            json={"signature": base64.b64encode(sig_der).decode("ascii")},
        )

    kms_ver = "projects/acme-agent-governance/locations/us-central1/keyRings/finops/cryptoKeys/signer/cryptoKeyVersions/1"
    with httpx.Client(transport=httpx.MockTransport(kms_handler)) as mock_client:
        packaged = build_skill_zip(
            STARTER_PLUGIN_DIR / "skills" / "bq-query-optimizer",
            {"plugin_name": "gcp-finops-toolkit", "plugin_version": "1.0.0"},
            kms_key_version=kms_ver,
            kms_auth_token="fake-token",
            http_client=mock_client,
        )

    assert len(kms_calls) == 1
    ok, reason = verify_skill_zip_signature(packaged.zip_bytes, pub_pem)
    assert ok, reason


# ---------------------------------------------------------------------------
# 5. Agent Registry v1 REST payloads, private- prefixing, LRO polling & CLI
# ---------------------------------------------------------------------------


def test_plugin_sync_rest_create_and_update_flows(tmp_path: Path) -> None:
    assert canonical_private_skill_id("cost-anomaly-triage") == "private-cost-anomaly-triage"
    assert canonical_private_skill_id("private-cost-anomaly-triage") == "private-cost-anomaly-triage"
    assert raw_create_skill_id("private-cost-anomaly-triage") == "cost-anomaly-triage"

    priv_pem, _ = _generate_ecdsa_p256_keypair(tmp_path)

    # Mock Agent Registry v1 state + live MCP tools/list endpoint
    existing_skills: dict[str, dict[str, Any]] = {}
    existing_services: dict[str, dict[str, Any]] = {}
    pending_ops: dict[str, tuple[int, dict[str, Any]]] = {}
    recorded_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        recorded_requests.append((method, str(request.url), body))

        # Live MCP tools/list probe
        if path == "/mcp" and method == "POST":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "tools": [
                            {
                                "name": "get_billing_summary",
                                "description": "Read billing summary",
                                "inputSchema": {"type": "object"},
                                "annotations": {"readOnlyHint": True, "destructiveHint": False},
                            }
                        ]
                    },
                },
            )

        # LRO polling endpoint (returns done=False once, then done=True)
        if "/operations/" in path and method == "GET":
            op_name = path.lstrip("/").removeprefix("v1/")
            remaining, final_resp = pending_ops[op_name]
            if remaining > 0:
                pending_ops[op_name] = (remaining - 1, final_resp)
                return httpx.Response(200, json={"name": op_name, "done": False})
            return httpx.Response(200, json={"name": op_name, "done": True, "response": final_resp})

        # Skills GET / POST / revisions / PATCH
        if "/skills/" in path and path.endswith("/revisions") and method == "POST":
            skill_part = path.split("/skills/")[1].split("/revisions")[0]
            assert skill_part.startswith("private-"), f"Expected private- prefix on revision POST: {path}"
            rev_id = request.url.params["skillRevisionId"]
            rev_name = f"projects/acme-agent-governance/locations/us-central1/skills/{skill_part}/revisions/{rev_id}"
            op_name = f"projects/acme-agent-governance/locations/us-central1/operations/op-rev-{rev_id}"
            pending_ops[op_name] = (0, {"name": rev_name, "state": "ACTIVE"})
            return httpx.Response(200, json={"name": op_name, "done": False})

        if "/skills/" in path and method == "GET":
            skill_part = path.split("/skills/")[1]
            assert skill_part.startswith("private-"), f"Expected private- prefix on GET: {path}"
            if skill_part in existing_skills:
                return httpx.Response(200, json=existing_skills[skill_part])
            return httpx.Response(404, json={"error": {"code": 404, "message": "not found"}})

        if path.endswith("/skills") and method == "POST":
            raw_id = request.url.params["skillId"]
            assert not raw_id.startswith("private-"), "Initial POST ?skillId must use raw ID"
            private_id = f"private-{raw_id}"
            res_name = f"projects/acme-agent-governance/locations/us-central1/skills/{private_id}"
            assert body is not None
            assert body["type"] == "SIMPLE"
            assert body["scanningConfig"]["policy"] == "ENABLED_BLOCKING"
            zip_bytes = base64.b64decode(body["initialRevision"]["archiveUploadSource"]["archiveContent"])
            validate_skill_zip(zip_bytes, expected_skill_name=raw_id)
            created_obj = {"name": res_name, "targetState": body["targetState"]}
            existing_skills[private_id] = created_obj
            op_name = f"projects/acme-agent-governance/locations/us-central1/operations/op-create-{raw_id}"
            pending_ops[op_name] = (1, created_obj)
            return httpx.Response(200, json={"name": op_name, "done": False})

        if "/skills/" in path and method == "PATCH":
            skill_part = path.split("/skills/")[1]
            assert skill_part.startswith("private-"), f"Expected private- prefix on PATCH: {path}"
            assert body is not None and body["defaultRevision"].startswith(
                f"projects/acme-agent-governance/locations/us-central1/skills/{skill_part}/revisions/"
            )
            updated_obj = {"name": f"projects/acme-agent-governance/locations/us-central1/skills/{skill_part}", **body}
            existing_skills[skill_part] = updated_obj
            return httpx.Response(200, json={"name": "op-patch", "done": True, "response": updated_obj})

        # Services GET / POST / PATCH
        if "/services/" in path and method == "GET":
            svc_id = path.split("/services/")[1]
            if svc_id in existing_services:
                return httpx.Response(200, json=existing_services[svc_id])
            return httpx.Response(404, json={"error": {"code": 404}})

        if path.endswith("/services") and method == "POST":
            svc_id = request.url.params["serviceId"]
            assert body is not None
            if "agentSpec" in body:
                # CRITICAL Agent Registry v1 invariant: interfaces MUST be empty for A2A_AGENT_CARD
                assert body["agentSpec"]["type"] == "A2A_AGENT_CARD"
                assert body["interfaces"] == []
            if "mcpServerSpec" in body:
                assert body["mcpServerSpec"]["type"] == "TOOL_SPEC"
                assert len(body["interfaces"]) == 1
                assert len(body["mcpServerSpec"]["content"]["tools"]) >= 1
            res_name = f"projects/acme-agent-governance/locations/us-central1/services/{svc_id}"
            created_svc = {"name": res_name, **body}
            existing_services[svc_id] = created_svc
            return httpx.Response(200, json={"name": f"op-svc-{svc_id}", "done": True, "response": created_svc})

        if "/services/" in path and method == "PATCH":
            svc_id = path.split("/services/")[1]
            assert body is not None
            updated_svc = {"name": f"projects/acme-agent-governance/locations/us-central1/services/{svc_id}", **body}
            existing_services[svc_id] = updated_svc
            return httpx.Response(200, json={"name": f"op-patch-{svc_id}", "done": True, "response": updated_svc})

        return httpx.Response(500, json={"error": f"Unhandled {method} {path}"})

    with httpx.Client(transport=httpx.MockTransport(transport_handler)) as mock_http:
        # First sync: CREATE path
        plan1 = build_sync_plan(
            STARTER_PLUGIN_DIR,
            project="acme-agent-governance",
            location="us-central1",
            target_state="TARGET_STATE_DRAFT",
            sign_pem=priv_pem,
            http_client=mock_http,
        )
        res1 = apply_sync_plan(
            plan1,
            registry_url="http://mock-registry.local/v1",
            http_client=mock_http,
            poll_interval=0.01,
        )
        assert [s["action"] for s in res1["skills"]] == ["created", "created"]
        assert [s["action"] for s in res1["mcp_services"]] == ["created"]
        assert [s["action"] for s in res1["agent_services"]] == ["created"]
        assert res1["agent_services"][0]["service_id"] == "finops-advisor-agent"
        assert res1["mcp_services"][0]["service_id"] == "finops-mcp-server"

        # Second sync: UPDATE path (creates SkillRevision under private-<id> and PATCHes)
        plan2 = build_sync_plan(
            STARTER_PLUGIN_DIR,
            project="acme-agent-governance",
            location="us-central1",
            target_state="TARGET_STATE_ACTIVE",
            sign_pem=priv_pem,
            http_client=mock_http,
        )
        res2 = apply_sync_plan(
            plan2,
            registry_url="http://mock-registry.local/v1",
            http_client=mock_http,
            poll_interval=0.01,
        )
        assert [s["action"] for s in res2["skills"]] == ["updated", "updated"]
        assert all("private-" in s["revision_name"] for s in res2["skills"])
        assert [s["action"] for s in res2["mcp_services"]] == ["updated"]
        assert [s["action"] for s in res2["agent_services"]] == ["updated"]


def test_plugin_sync_cli_dry_run_and_verify(tmp_path: Path) -> None:
    priv_pem, pub_pem = _generate_ecdsa_p256_keypair(tmp_path)
    out_dir = tmp_path / "dist"

    runner = CliRunner()
    sync_res = runner.invoke(
        cli,
        [
            "sync",
            "--plugin-dir",
            str(STARTER_PLUGIN_DIR),
            "--registry-url",
            "http://127.0.0.1:8090/v1",
            "--project",
            "acme-agent-governance",
            "--location",
            "us-central1",
            "--target-state",
            "TARGET_STATE_DRAFT",
            "--sign-pem",
            str(priv_pem),
            "--output-dir",
            str(out_dir),
            "--skip-mcp-probe",
            "--dry-run",
        ],
    )
    assert sync_res.exit_code == 0, sync_res.output
    summary = json.loads(sync_res.output)
    assert summary["dry_run"] is True
    assert len(summary["skills"]) == 2
    assert all(s["signed"] is True for s in summary["skills"])
    assert summary["agent_services"][0]["payload"]["interfaces"] == []
    mcp_spec = summary["mcp_services"][0]["payload"]["mcpServerSpec"]
    assert mcp_spec["type"] == "TOOL_SPEC"
    assert len(mcp_spec["content"]["tools"]) == 3
    tools_map = {t["name"]: t for t in mcp_spec["content"]["tools"]}
    assert tools_map["get_billing_summary"]["inputSchema"]["properties"]["days"]["minimum"] == 1
    assert tools_map["get_billing_summary"]["inputSchema"]["properties"]["days"]["maximum"] == 90
    assert "reason" in tools_map["delete_idle_dataset"]["inputSchema"]["properties"]

    # Verify generated ZIP via CLI `verify` command
    zip_path = out_dir / "cost-anomaly-triage.zip"
    assert zip_path.is_file()
    verify_res = runner.invoke(
        cli,
        [
            "verify",
            "--skill-zip",
            str(zip_path),
            "--public-key-pem",
            str(pub_pem),
        ],
    )
    assert verify_res.exit_code == 0, verify_res.output
    assert json.loads(verify_res.output)["valid"] is True

    # Also verify `python -m tools.plugin_sync --help` entrypoint executes cleanly
    proc = subprocess.run(
        [sys.executable, "-m", "tools.plugin_sync", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "sync" in proc.stdout


def test_plugin_sync_bangkok_defaults_headers_gcloud_and_wait_for_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify asia-southeast3 defaults, x-goog-user-project, gcloud --quiet, and async scan polling."""
    import hashlib
    import importlib.util
    from tools.plugin_sync.sync import (
        _resolve_bearer_token,
        sign_digest_with_kms,
        wait_for_skill_scan_completion,
    )

    # 1. CLI default --location is asia-southeast3 on both `sync` and `ingest`
    runner = CliRunner()
    dry_res = runner.invoke(
        cli,
        [
            "sync",
            "--plugin-dir",
            str(STARTER_PLUGIN_DIR),
            "--project",
            "acme-agent-governance",
            "--skip-mcp-probe",
            "--dry-run",
        ],
    )
    assert dry_res.exit_code == 0, dry_res.output
    dry_summary = json.loads(dry_res.output)
    assert dry_summary["location"] == "asia-southeast3"
    assert "/locations/asia-southeast3/skills/private-" in dry_summary["skills"][0]["resource_name"]

    ingest_res = runner.invoke(
        cli,
        [
            "ingest",
            "--source-type",
            "local",
            "--path",
            str(REPO_ROOT / "local-skills"),
            "--project",
            "acme-agent-governance",
            "--dry-run",
        ],
    )
    assert ingest_res.exit_code == 0, ingest_res.output
    assert json.loads(ingest_res.output)["location"] == "asia-southeast3"

    # 2. _resolve_bearer_token uses --quiet and falls back to ~/.local/bin/gcloud
    seen_cmds: list[list[str]] = []

    def _fake_subprocess_run(cmd: list[str], **_kwargs: Any) -> Any:
        seen_cmds.append(list(cmd))

        class _Proc:
            returncode = 0 if "application-default" in cmd else 1
            stdout = "adc-live-token\n" if returncode == 0 else ""

        return _Proc()

    with monkeypatch.context() as mp:
        mp.delenv("AGENT_REGISTRY_AUTH_TOKEN", raising=False)
        mp.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)
        mp.setattr("tools.plugin_sync.sync.shutil.which", lambda _name: None)
        mp.setattr("tools.plugin_sync.sync.subprocess.run", _fake_subprocess_run)
        resolved = _resolve_bearer_token("https://agentregistry.googleapis.com/v1")
    assert resolved == "adc-live-token"
    assert len(seen_cmds) == 2
    assert all("--quiet" in c for c in seen_cmds)
    assert seen_cmds[0][0].endswith(".local/bin/gcloud")
    assert seen_cmds[0][1:] == ["auth", "print-access-token", "--quiet"]
    assert seen_cmds[1][1:] == ["auth", "application-default", "print-access-token", "--quiet"]

    # 3. sign_digest_with_kms sends x-goog-user-project parsed from kms_key_version
    kms_headers_seen: list[dict[str, str]] = []

    def _kms_handler(request: httpx.Request) -> httpx.Response:
        kms_headers_seen.append(dict(request.headers))
        return httpx.Response(200, json={"signature": base64.b64encode(b"sig-bytes").decode("ascii")})

    with httpx.Client(transport=httpx.MockTransport(_kms_handler)) as kms_http:
        sig = sign_digest_with_kms(
            hashlib.sha256(b"payload").digest(),
            "projects/bkk-prod-proj/locations/asia-southeast3/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1",
            auth_token="tok-123",
            http_client=kms_http,
        )
    assert sig == base64.b64encode(b"sig-bytes").decode("ascii")
    assert kms_headers_seen[0].get("x-goog-user-project") == "bkk-prod-proj"

    # 4. apply_sync_plan sends x-goog-user-project against googleapis.com & polls async scan to STATE_ACTIVE
    poll_counts: dict[str, int] = {}
    ar_headers_seen: list[dict[str, str]] = []

    def _live_ar_handler(request: httpx.Request) -> httpx.Response:
        ar_headers_seen.append(dict(request.headers))
        path = request.url.path
        if request.method == "GET" and "/skills/private-" in path and "/revisions/" not in path:
            skill_id = path.rsplit("/", 1)[-1]
            count = poll_counts.get(skill_id, 0)
            if count == 0:
                poll_counts[skill_id] = 1
                return httpx.Response(404, json={"error": {"code": 404}})
            poll_counts[skill_id] = count + 1
            if count == 1:
                return httpx.Response(
                    200,
                    json={
                        "name": path.removeprefix("/v1/"),
                        "state": "STATE_DRAFT",
                        "defaultRevision": f"{path.removeprefix('/v1/')}/revisions/rev-1",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "name": path.removeprefix("/v1/"),
                    "state": "STATE_ACTIVE",
                    "defaultRevision": f"{path.removeprefix('/v1/')}/revisions/rev-1",
                },
            )
        if request.method == "GET" and "/revisions/" in path:
            return httpx.Response(
                200,
                json={
                    "name": path.removeprefix("/v1/"),
                    "scanResult": {"scanState": "RUNNING", "findings": []},
                },
            )
        if request.method == "POST" and path.endswith("/skills"):
            raw_id = request.url.params["skillId"]
            res_name = f"projects/acme-agent-governance/locations/asia-southeast3/skills/private-{raw_id}"
            return httpx.Response(
                200,
                json={
                    "name": f"projects/acme-agent-governance/locations/asia-southeast3/operations/op-{raw_id}",
                    "done": True,
                    "response": {
                        "name": res_name,
                        "state": "STATE_DRAFT",
                        "defaultRevision": f"{res_name}/revisions/rev-1",
                    },
                },
            )
        if request.method == "GET" and "/services/" in path:
            return httpx.Response(404, json={"error": {"code": 404}})
        if request.method == "POST" and path.endswith("/services"):
            svc_id = request.url.params["serviceId"]
            return httpx.Response(
                200,
                json={
                    "name": f"op-svc-{svc_id}",
                    "done": True,
                    "response": {
                        "name": f"projects/acme-agent-governance/locations/asia-southeast3/services/{svc_id}"
                    },
                },
            )
        return httpx.Response(500, json={"error": f"Unhandled {request.method} {path}"})

    with httpx.Client(transport=httpx.MockTransport(_live_ar_handler)) as live_http:
        plan = build_sync_plan(
            STARTER_PLUGIN_DIR,
            project="acme-agent-governance",
            location="asia-southeast3",
            target_state="TARGET_STATE_ACTIVE",
            scanning_policy="ENABLED_BLOCKING",
            probe_mcp=False,
        )
        summary = apply_sync_plan(
            plan,
            registry_url="https://agentregistry.googleapis.com/v1",
            auth_token="live-ar-token",
            http_client=live_http,
            poll_interval=0.01,
            wait_for_scan=True,
            scan_timeout=5.0,
            scan_poll_interval=0.01,
        )

    assert all(h.get("x-goog-user-project") == "acme-agent-governance" for h in ar_headers_seen)
    assert all(s["final_state"] == "STATE_ACTIVE" for s in summary["skills"])
    assert all(isinstance(s["scan_wait_seconds"], float) for s in summary["skills"])

    # 5. wait_for_skill_scan_completion stops immediately on terminal FAILED scanState
    def _failed_scan_handler(request: httpx.Request) -> httpx.Response:
        if "/revisions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "name": request.url.path.removeprefix("/v1/"),
                    "scanResult": {"scanState": "FAILED", "findings": ["prompt-injection"]},
                },
            )
        return httpx.Response(
            200,
            json={
                "name": request.url.path.removeprefix("/v1/"),
                "state": "STATE_DRAFT",
                "defaultRevision": f"{request.url.path.removeprefix('/v1/')}/revisions/rev-bad",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(_failed_scan_handler)) as fail_http:
        fail_res = wait_for_skill_scan_completion(
            fail_http,
            "https://agentregistry.googleapis.com/v1",
            "projects/acme-agent-governance/locations/asia-southeast3/skills/private-bad",
            headers={"Authorization": "Bearer tok"},
            timeout_seconds=5.0,
            poll_interval=0.01,
        )
    assert fail_res["final_state"] == "STATE_DRAFT"
    assert fail_res["scan_state"] == "FAILED"
    assert fail_res["timed_out"] is False

    # 6. Verify security-governance/kms/sign_and_verify.py & apply-uap.sh Bangkok defaults & headers
    kms_mod_path = REPO_ROOT / "security-governance" / "kms" / "sign_and_verify.py"
    spec = importlib.util.spec_from_file_location("test_kms_sign_and_verify", kms_mod_path)
    assert spec is not None and spec.loader is not None
    kms_mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = kms_mod
    spec.loader.exec_module(kms_mod)
    assert "/locations/asia-southeast3/" in kms_mod.DEFAULT_KEY_VERSION_LABEL

    # Verify Cloud KMS REST calls include x-goog-user-project header
    kms_urllib_reqs: list[Any] = []
    _priv_pem, pub_pem = kms_mod.generate_ecdsa_p256_keypair()

    class _FakeResp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._raw = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._raw

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def _fake_urlopen(req: Any, timeout: float = 30) -> _FakeResp:
        kms_urllib_reqs.append(req)
        if req.full_url.endswith(":asymmetricSign"):
            return _FakeResp({"signature": base64.b64encode(b"kms-sig").decode("ascii")})
        return _FakeResp({"algorithm": "EC_SIGN_P256_SHA256", "pem": pub_pem.decode("utf-8")})

    monkeypatch.setattr(kms_mod.urllib.request, "urlopen", _fake_urlopen)
    key_ver = "projects/sg-kms-proj/locations/asia-southeast3/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"
    kms_mod.sign_digest_cloud_kms(hashlib.sha256(b"msg").digest(), key_ver, access_token="tok")
    kms_mod.fetch_public_key_cloud_kms(key_ver, access_token="tok")
    assert len(kms_urllib_reqs) == 2
    assert all(r.get_header("X-goog-user-project") == "sg-kms-proj" for r in kms_urllib_reqs)

    captured_uap = subprocess.run(
        [str(REPO_ROOT / "security-governance" / "iap-uap" / "apply-uap.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if k != "GCP_LOCATION"},
    )
    assert captured_uap.returncode == 0, captured_uap.stderr
    assert "/locations/asia-southeast3/agentGateways/" in captured_uap.stdout
    assert "--quiet" in captured_uap.stdout
