"""Complete End-to-End Workshop Pipeline Integration Tests.

Exercises ALL workshop components running together as real HTTP/ASGI servers
on ephemeral localhost ports (zero port conflicts):
  1. `mock_registry` (Google Cloud Agent Registry v1 simulator)
  2. `samples/mcp-server` (Streamable HTTP FinOps MCP Server)
  3. `samples/a2a-agent` (A2A v1.0 FinOps Advisor Agent)
  4. `middleware` (Polyglot Enterprise Marketplace Middleware & MCP Gateway
     with `REQUIRE_KMS_SIGNATURE=true`)
  5. `tools.plugin_sync` (ECDSA P-256 signing & Agent Registry v1 registration)
  6. `tools.hub_sync` (Multi-IDE safe ownership-tracked hydration & validation)
  7. `platform_agent` (`PlatformAgentConsumer` & `PlatformAgentOrchestrator`)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import yaml
from click.testing import CliRunner
from jsonschema import Draft202012Validator

from middleware.app.config import MiddlewareSettings
from middleware.app.main import create_app as create_middleware_app
from middleware.app.security_verifier import (
    generate_ecdsa_p256_keypair,
    sign_skill_zip,
)
from mock_registry.app import create_app as create_mock_registry_app
from platform_agent import (
    APPROVED_SKILL_FILTER,
    OrchestratorConfig,
    PlatformAgentConsumer,
    PlatformAgentOrchestrator,
    SkillGovernanceError,
    SkillSignatureError,
)
from schemas.validators import (
    load_schema,
    parse_frontmatter,
    validate_agent_plugin_bundle,
    validate_antigravity_bundle,
    validate_antigravity_marketplace,
    validate_claude_marketplace,
    validate_claude_plugin_bundle,
    validate_codex_marketplace,
    validate_codex_plugin_bundle,
    validate_copilot_marketplace,
    validate_copilot_plugin_bundle,
)
from tools.hub_sync.cli import get_hub_status, sync_hub
from tools.plugin_sync.cli import cli as plugin_sync_cli
from tools.plugin_sync.sync import fetch_mcp_tool_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSHOP_SPEC = json.loads((REPO_ROOT / "WORKSHOP_SPEC.json").read_text(encoding="utf-8"))
STARTER_PLUGIN_DIR = (
    REPO_ROOT / "starter-marketplace" / "plugins" / "gcp-finops-toolkit"
)
LOCAL_SKILLS_DIR = REPO_ROOT / "local-skills"
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
MCP_SERVER_APP_PATH = REPO_ROOT / "samples" / "mcp-server" / "app.py"
A2A_AGENT_APP_PATH = REPO_ROOT / "samples" / "a2a-agent" / "app.py"

PROJECT_ID = WORKSHOP_SPEC["project_defaults"]["project_id"]
LOCATION = WORKSHOP_SPEC["project_defaults"]["location"]
PARENT = f"projects/{PROJECT_ID}/locations/{LOCATION}"
KMS_KEY_VERSION = (
    f"projects/{PROJECT_ID}/locations/{LOCATION}/keyRings/"
    "agent-registry-skill-signing/cryptoKeys/skill-artifact-ecdsa-p256/cryptoKeyVersions/1"
)


def _load_module(module_name: str, file_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _bind_ephemeral_socket() -> tuple[socket.socket, int]:
    """Bind a localhost TCP socket on an OS-assigned ephemeral port (zero conflicts)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    return sock, port


class _LiveUvicornServer:
    """Runs a FastAPI app inside a background uvicorn thread on a pre-bound ephemeral port."""

    def __init__(self, app: Any, name: str, sock: socket.socket | None = None) -> None:
        self.app = app
        self.name = name
        if sock is None:
            self._sock, self.port = _bind_ephemeral_socket()
        else:
            self._sock = sock
            self.port = sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def start(self, timeout: float = 15.0) -> str:
        config = uvicorn.Config(
            app=self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        # Never overwrite pytest's main-thread signal handlers from a worker thread.
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._server = server

        def _run() -> None:
            try:
                server.run(sockets=[self._sock])
            except BaseException as exc:  # pragma: no cover - surfaced on readiness wait
                self._error = exc

        self._thread = threading.Thread(
            target=_run,
            name=f"e2e-uvicorn-{self.name}-{self.port}",
            daemon=True,
        )
        self._thread.start()

        deadline = time.monotonic() + timeout
        with httpx.Client(timeout=1.0) as client:
            while time.monotonic() < deadline:
                if self._error is not None:
                    raise RuntimeError(
                        f"Server {self.name!r} crashed during startup: {self._error}"
                    ) from self._error
                try:
                    resp = client.get(f"{self.base_url}/healthz")
                    if resp.status_code == 200:
                        return self.base_url
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)

        raise TimeoutError(
            f"Server {self.name!r} did not pass /healthz on {self.base_url} within {timeout}s"
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> _LiveUvicornServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


@dataclass
class LiveWorkshopStack:
    """Handles and URLs for all 4 live workshop servers + ephemeral signing keys."""

    mock_registry_url: str
    mcp_server_url: str
    a2a_agent_url: str
    middleware_url: str
    private_key_pem_path: Path
    public_key_pem_path: Path
    private_key_pem: bytes
    public_key_pem: bytes


@pytest.fixture()
def live_workshop_stack(tmp_path: Path):
    """Start mock_registry, samples/mcp-server, samples/a2a-agent, and middleware on ephemeral ports."""
    priv_pem, pub_pem = generate_ecdsa_p256_keypair()
    priv_path = tmp_path / "kms_signing_private.pem"
    pub_path = tmp_path / "kms_signing_public.pem"
    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)

    mock_app = create_mock_registry_app()
    mcp_mod = _load_module("e2e_sample_mcp_server", MCP_SERVER_APP_PATH)
    a2a_mod = _load_module("e2e_sample_a2a_agent", A2A_AGENT_APP_PATH)

    # Pre-bind the middleware socket so MiddlewareSettings.middleware_public_url matches
    # the exact ephemeral port without any TOCTOU race.
    mw_sock, mw_port = _bind_ephemeral_socket()

    mock_srv = _LiveUvicornServer(mock_app, name="mock-registry")
    mcp_srv = _LiveUvicornServer(mcp_mod.app, name="sample-mcp")
    a2a_srv = _LiveUvicornServer(a2a_mod.app, name="sample-a2a")

    mw_srv: _LiveUvicornServer | None = None
    try:
        mock_url = mock_srv.start()
        mcp_url = mcp_srv.start()
        a2a_url = a2a_srv.start()

        mw_settings = MiddlewareSettings(
            agent_registry_base_url=f"{mock_url}/v1",
            gcp_project_id=PROJECT_ID,
            gcp_location=LOCATION,
            middleware_public_url=f"http://127.0.0.1:{mw_port}",
            require_kms_signature=True,
            kms_public_key_pem_path=str(pub_path),
        )
        mw_app = create_middleware_app(settings=mw_settings)
        mw_srv = _LiveUvicornServer(mw_app, name="middleware", sock=mw_sock)
        mw_url = mw_srv.start()

        yield LiveWorkshopStack(
            mock_registry_url=mock_url,
            mcp_server_url=mcp_url,
            a2a_agent_url=a2a_url,
            middleware_url=mw_url,
            private_key_pem_path=priv_path,
            public_key_pem_path=pub_path,
            private_key_pem=priv_pem,
            public_key_pem=pub_pem,
        )
    finally:
        if mw_srv is not None:
            mw_srv.stop()
        else:
            try:
                mw_sock.close()
            except OSError:
                pass
        a2a_srv.stop()
        mcp_srv.stop()
        mock_srv.stop()


def _build_skill_zip_bytes(
    *,
    skill_name: str,
    description: str,
    body: str,
    metadata: dict[str, str],
) -> bytes:
    """Create a valid agentskills.io ZIP with `<skill_name>/SKILL.md`."""
    frontmatter: dict[str, Any] = {
        "name": skill_name,
        "description": description,
        "license": "Apache-2.0",
        "metadata": dict(metadata),
    }
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=True, allow_unicode=True).strip()
    md_text = f"---\n{yaml_text}\n---\n\n{body.strip()}\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{skill_name}/SKILL.md", md_text.encode("utf-8"))
    return buf.getvalue()


def _tamper_skill_zip_signature_or_body(
    signed_zip_bytes: bytes,
    *,
    corrupt_signature: bool = False,
    append_body: str = "",
) -> bytes:
    """Return a new valid Skill ZIP whose signature no longer matches its content."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(signed_zip_bytes), mode="r") as src,
        zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as dst,
    ):
        for info in sorted(src.infolist(), key=lambda i: i.filename):
            if info.is_dir():
                continue
            raw = src.read(info)
            if info.filename.endswith("SKILL.md"):
                fm, body, err = parse_frontmatter(raw.decode("utf-8"))
                assert err is None and fm is not None
                if corrupt_signature:
                    meta = dict(fm.get("metadata") or {})
                    orig_sig = bytearray(base64.b64decode(meta["kms_signature"]))
                    orig_sig[-1] ^= 0xFF
                    meta["kms_signature"] = base64.b64encode(bytes(orig_sig)).decode("ascii")
                    fm["metadata"] = meta
                if append_body:
                    body = f"{body.rstrip()}\n\n{append_body}\n"
                yaml_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
                raw = f"---\n{yaml_text}\n---\n{body.lstrip(chr(10))}".encode("utf-8")
            dst.writestr(info.filename, raw)
    return out.getvalue()


def _preseed_user_home(home_dir: Path) -> dict[str, Path]:
    """Populate a developer `$HOME` with user-authored Claude, Antigravity, Codex & VS Code files."""
    # 1. Claude Code user settings
    claude_settings_path = home_dir / ".claude" / "settings.json"
    claude_settings_path.parent.mkdir(parents=True, exist_ok=True)
    claude_settings_path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "extraKnownMarketplaces": {
                    "user-personal-marketplace": {
                        "source": {"source": "directory", "path": "/tmp/user-mkt"}
                    }
                },
                "enabledPlugins": {
                    "user-helper@user-personal-marketplace": True,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # 2. Antigravity user MCP config + user personal plugin
    ag_mcp_path = home_dir / ".gemini" / "config" / "mcp_config.json"
    ag_mcp_path.parent.mkdir(parents=True, exist_ok=True)
    ag_mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "user-personal-mcp": {
                        "url": "http://127.0.0.1:9999/mcp",
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    ag_user_plugin_manifest = (
        home_dir / ".gemini" / "config" / "plugins" / "user-personal-plugin" / "plugin.json"
    )
    ag_user_plugin_manifest.parent.mkdir(parents=True, exist_ok=True)
    ag_user_plugin_manifest.write_text(
        json.dumps({"name": "user-personal-plugin", "version": "0.1.0"}, indent=2) + "\n",
        encoding="utf-8",
    )

    # 3. OpenAI Codex user config.toml, user agent TOML, and user marketplace entry
    codex_config_path = home_dir / ".codex" / "config.toml"
    codex_config_path.parent.mkdir(parents=True, exist_ok=True)
    codex_config_path.write_text(
        '# Personal Codex settings\n'
        'model = "o4-mini"\n\n'
        '[mcp_servers.user-codex-mcp]\n'
        'url = "http://127.0.0.1:9998/mcp"\n',
        encoding="utf-8",
    )
    codex_user_agent_path = home_dir / ".codex" / "agents" / "user-personal-agent.toml"
    codex_user_agent_path.parent.mkdir(parents=True, exist_ok=True)
    codex_user_agent_path.write_text(
        'name = "user-personal-agent"\n'
        'description = "User personal Codex helper"\n'
        'instructions = "Keep my personal notes organized."\n',
        encoding="utf-8",
    )
    codex_mkt_path = home_dir / ".agents" / "plugins" / "marketplace.json"
    codex_mkt_path.parent.mkdir(parents=True, exist_ok=True)
    codex_mkt_path.write_text(
        json.dumps(
            {
                "name": "user-local-codex",
                "interface": {"displayName": "User Local Plugins"},
                "plugins": [
                    {
                        "name": "user-codex-plugin",
                        "source": {"source": "local", "path": "./plugins/user-codex-plugin"},
                        "policy": {
                            "installation": "AVAILABLE",
                            "authentication": "ON_INSTALL",
                        },
                        "category": "Personal",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # 4. VS Code user settings
    vscode_settings_path = home_dir / ".config" / "Code" / "User" / "settings.json"
    vscode_settings_path.parent.mkdir(parents=True, exist_ok=True)
    vscode_settings_path.write_text(
        json.dumps(
            {
                "editor.fontSize": 14,
                "chat.pluginLocations": {
                    "/tmp/user-custom-plugin": True,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return {
        "claude_settings": claude_settings_path,
        "ag_mcp": ag_mcp_path,
        "ag_user_plugin": ag_user_plugin_manifest,
        "codex_config": codex_config_path,
        "codex_user_agent": codex_user_agent_path,
        "codex_mkt": codex_mkt_path,
        "vscode_settings": vscode_settings_path,
    }


def test_e2e_complete_workshop_pipeline(
    live_workshop_stack: LiveWorkshopStack, tmp_path: Path
) -> None:
    """Exercise all 6 end-to-end workshop pipeline stages across live HTTP services."""
    stack = live_workshop_stack
    registry_v1 = f"{stack.mock_registry_url}/v1"

    with httpx.Client(timeout=20.0) as client:
        # =====================================================================
        # STAGE 1: Verify all 4 live HTTP servers (mock_registry, MCP, A2A, middleware)
        # =====================================================================
        assert LOCATION == "asia-southeast3"
        assert WORKSHOP_SPEC["project_defaults"]["fallback_region"] == "asia-southeast1"
        assert WORKSHOP_SPEC["project_defaults"]["skills_location"] == "global"
        assert WORKSHOP_SPEC["project_defaults"]["auth_provider_location"] == "asia-southeast1"
        assert "secondary_location" not in WORKSHOP_SPEC["project_defaults"]
        assert "GCP_SECONDARY_LOCATION" not in WORKSHOP_SPEC["env_vars"]
        assert "HUB_FALLBACK_MIDDLEWARE_URL" not in WORKSHOP_SPEC["env_vars"]

        assert client.get(f"{stack.mock_registry_url}/healthz").json()["status"] == "ok"
        assert client.get(f"{stack.mcp_server_url}/healthz").json()["status"] == "ok"
        assert client.get(f"{stack.a2a_agent_url}/healthz").json()["status"] == "ok"
        for health_path in ("/healthz", "/health", "/v1/health"):
            mw_health = client.get(f"{stack.middleware_url}{health_path}").json()
            assert mw_health["status"] == "ok"
            assert mw_health["location"] == "asia-southeast3"
            assert mw_health["region"] == "asia-southeast3"
            assert mw_health["agentRegistryLocation"] == "asia-southeast3"
            assert mw_health["skillsLocation"] == "asia-southeast3"
            assert "secondaryRegion" not in mw_health
            assert mw_health["requireKmsSignature"] is True

        # Exercise live sample MCP server (initialize, tools/list, tools/call)
        mcp_endpoint = f"{stack.mcp_server_url}/mcp"
        init_resp = client.post(
            mcp_endpoint,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert init_resp.status_code == 200
        assert "mcp-session-id" in init_resp.headers

        live_tool_spec = fetch_mcp_tool_spec(mcp_endpoint, http_client=client)
        assert live_tool_spec is not None
        tool_by_name = {t["name"]: t for t in live_tool_spec["tools"]}
        assert set(tool_by_name.keys()) == {
            "get_billing_summary",
            "analyze_bq_query",
            "delete_idle_dataset",
        }
        assert tool_by_name["get_billing_summary"]["annotations"]["readOnlyHint"] is True
        assert tool_by_name["delete_idle_dataset"]["annotations"]["destructiveHint"] is True

        billing_call = client.post(
            mcp_endpoint,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_billing_summary",
                    "arguments": {"project_id": PROJECT_ID, "days": 7},
                },
            },
        ).json()
        assert billing_call["result"]["isError"] is False
        assert billing_call["result"]["structuredContent"]["anomaly_detected"] is True

        # Exercise live sample A2A agent card & task endpoint
        live_a2a_card = client.get(f"{stack.a2a_agent_url}/.well-known/agent-card.json").json()
        assert live_a2a_card["supportedInterfaces"][0]["url"] == f"{stack.a2a_agent_url}/a2a"
        a2a_direct = client.post(
            f"{stack.a2a_agent_url}/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "e2e-direct-1",
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "Optimize SELECT * FROM analytics.events"}],
                    }
                },
            },
        ).json()
        assert a2a_direct["result"]["status"]["state"] == "completed"

        # =====================================================================
        # STAGE 2: Run plugin_sync with ECDSA P-256 signing key on real starter plugin
        # =====================================================================
        sync_out_dir = tmp_path / "plugin_sync_dist"
        runner = CliRunner()
        cli_result = runner.invoke(
            plugin_sync_cli,
            [
                "sync",
                "--plugin-dir",
                str(STARTER_PLUGIN_DIR),
                "--registry-url",
                registry_v1,
                "--project",
                PROJECT_ID,
                "--location",
                LOCATION,
                "--target-state",
                "TARGET_STATE_ACTIVE",
                "--scanning-policy",
                "ENABLED_BLOCKING",
                "--sign-pem",
                str(stack.private_key_pem_path),
                "--kms-key-version",
                KMS_KEY_VERSION,
                "--output-dir",
                str(sync_out_dir),
                "--skip-mcp-probe",
            ],
        )
        assert cli_result.exit_code == 0, f"plugin_sync failed:\n{cli_result.output}"
        sync_summary = json.loads(cli_result.output)
        assert sync_summary["plugin_name"] == "gcp-finops-toolkit"
        assert len(sync_summary["skills"]) == 2
        assert all(s["signed"] is True for s in sync_summary["skills"])
        assert {s["private_skill_id"] for s in sync_summary["skills"]} == {
            "private-cost-anomaly-triage",
            "private-bq-query-optimizer",
        }
        assert [m["service_id"] for m in sync_summary["mcp_services"]] == ["finops-mcp-server"]
        assert [a["service_id"] for a in sync_summary["agent_services"]] == ["finops-advisor-agent"]

        # Point registered Services at our live ephemeral sample MCP/A2A servers so
        # Middleware's call_remote_a2a_agent and hub_sync wire real reachable endpoints.
        patch_mcp = client.patch(
            f"{registry_v1}/{PARENT}/services/finops-mcp-server",
            params={"updateMask": "interfaces,mcpServerSpec.content"},
            json={
                "interfaces": [{"url": mcp_endpoint, "protocolBinding": "JSONRPC"}],
                "mcpServerSpec": {"type": "TOOL_SPEC", "content": live_tool_spec},
            },
        )
        assert patch_mcp.status_code == 200, patch_mcp.text

        patch_a2a = client.patch(
            f"{registry_v1}/{PARENT}/services/finops-advisor-agent",
            params={"updateMask": "agentSpec.content"},
            json={
                "interfaces": [],
                "agentSpec": {"type": "A2A_AGENT_CARD", "content": live_a2a_card},
            },
        )
        assert patch_a2a.status_code == 200, patch_a2a.text
        svc_a2a = client.get(f"{registry_v1}/{PARENT}/services/finops-advisor-agent").json()
        assert svc_a2a["interfaces"] == []

        # Register an Auth Manager Binding connecting finops-advisor-agent -> finops-mcp-server
        projected_mcp = client.get(f"{registry_v1}/{PARENT}/mcpServers/finops-mcp-server").json()
        projected_agent = client.get(f"{registry_v1}/{PARENT}/agents/finops-advisor-agent").json()
        create_binding = client.post(
            f"{registry_v1}/{PARENT}/bindings",
            params={"bindingId": "finops-agent-to-mcp-binding"},
            json={
                "displayName": "FinOps Advisor to FinOps MCP OAuth Binding",
                "source": {"identifier": projected_agent["agentId"]},
                "target": {"identifier": projected_mcp["mcpServerId"]},
                "authProviderBinding": {
                    "authProvider": f"{PARENT}/authProviders/finops-mcp-oauth",
                    "scopes": [
                        "https://www.googleapis.com/auth/cloud-billing.readonly",
                        "https://www.googleapis.com/auth/bigquery.readonly",
                    ],
                    "continueUri": "https://console.cloud.google.com/agent-registry/auth/callback",
                },
            },
        )
        assert create_binding.status_code == 200, create_binding.text

        # =====================================================================
        # STAGE 3: Malicious skill with hardcoded secret (AKIAIOSFODNN7EXAMPLE)
        #          under ENABLED_BLOCKING -> Finding + blocked ?alt=media + excluded
        # =====================================================================
        malicious_unsigned_zip = _build_skill_zip_bytes(
            skill_name="malicious-secret-skill",
            description="Diagnostic script accidentally embedding an AWS key.",
            body=(
                "# Malicious Diagnostic Skill\n\n"
                "Export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE before running diagnostics.\n"
            ),
            metadata={
                "plugin_name": "malicious-toolkit",
                "plugin_display_name": "Malicious Toolkit",
                "plugin_version": "1.0.0",
            },
        )
        # Even sign it with the trusted key to prove ENABLED_BLOCKING blocks it regardless.
        malicious_signed_zip, _sig = sign_skill_zip(
            malicious_unsigned_zip,
            stack.private_key_pem,
            key_version=KMS_KEY_VERSION,
        )
        malicious_resp = client.post(
            f"{registry_v1}/{PARENT}/skills",
            params={"skillId": "malicious-secret-skill"},
            json={
                "displayName": "Malicious Secret Skill",
                "description": "Contains a hardcoded AWS key.",
                "type": "SIMPLE",
                "targetState": "TARGET_STATE_ACTIVE",
                "scanningConfig": {"policy": "ENABLED_BLOCKING"},
                "initialRevision": {
                    "archiveUploadSource": {
                        "archiveContent": base64.b64encode(malicious_signed_zip).decode("ascii")
                    }
                },
            },
        )
        assert malicious_resp.status_code == 200, malicious_resp.text
        malicious_skill = malicious_resp.json()["response"]
        assert malicious_skill["name"] == f"{PARENT}/skills/private-malicious-secret-skill"
        assert malicious_skill["state"] == "STATE_DRAFT"
        assert malicious_skill["defaultRevision"] == ""
        assert malicious_skill["defaultRevisionFindingCount"] >= 1

        # Verify Finding created in mock_registry
        malicious_rev_name = f"{PARENT}/skills/private-malicious-secret-skill/revisions/rev-1"
        findings_resp = client.get(
            f"{registry_v1}/{PARENT}/findings",
            params={"filter": f'resource="{malicious_rev_name}"'},
        )
        assert findings_resp.status_code == 200
        findings = findings_resp.json()["findings"]
        assert len(findings) >= 1
        assert any(
            f["findingClass"] == "SECRET" and f["severity"] == "SEVERITY_CRITICAL"
            for f in findings
        )

        # Verify mock_registry blocks ?alt=media on the finding-tainted revision
        blocked_media = client.get(
            f"{registry_v1}/{malicious_rev_name}",
            params={"alt": "media"},
            follow_redirects=False,
        )
        assert blocked_media.status_code == 400
        assert blocked_media.json()["error"]["status"] == "FAILED_PRECONDITION"
        assert "ENABLED_BLOCKING" in blocked_media.json()["error"]["message"]

        # Verify Middleware excludes malicious skill from /v1/catalog, marketplaces, ZIP & MCP
        catalog_before_tamper = client.get(f"{stack.middleware_url}/v1/catalog").json()
        assert [p["name"] for p in catalog_before_tamper["plugins"]] == ["gcp-finops-toolkit"]
        assert "private-malicious-secret-skill" not in {
            s["name"].rsplit("/", 1)[-1] for s in catalog_before_tamper["skills"]
        }
        assert any(
            r["name"].endswith("/private-malicious-secret-skill")
            for r in catalog_before_tamper["rejectedSkills"]
        )
        for target in ("claude", "copilot", "codex", "antigravity"):
            mkt = client.get(f"{stack.middleware_url}/v1/marketplace/{target}.json").json()
            assert [p["name"] for p in mkt["plugins"]] == ["gcp-finops-toolkit"]
        assert (
            client.get(f"{stack.middleware_url}/v1/plugins/malicious-toolkit.zip").status_code
            == 404
        )
        malicious_mcp_call = client.post(
            f"{stack.middleware_url}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 30,
                "method": "tools/call",
                "params": {
                    "name": "load_verified_skill",
                    "arguments": {"skill_name": "malicious-secret-skill"},
                },
            },
        ).json()
        assert malicious_mcp_call["result"]["isError"] is True

        # =====================================================================
        # STAGE 4: REQUIRE_KMS_SIGNATURE=true valid serving + signature tampering refusal
        # =====================================================================
        # 4a. Register a clean ACTIVE skill with a TAMPERED signature (0 scan findings,
        #     matching sha256Hash, so ONLY KMS signature verification catches it!)
        clean_other_unsigned = _build_skill_zip_bytes(
            skill_name="tampered-finops-skill",
            description="Clean FinOps guide whose body is tampered after KMS signing.",
            body="# Legitimate FinOps Runbook\n\nInspect billing exports safely.\n",
            metadata={
                "plugin_name": "tampered-finops-plugin",
                "plugin_display_name": "Tampered FinOps Plugin",
                "plugin_version": "1.0.0",
            },
        )
        clean_other_signed, _ = sign_skill_zip(
            clean_other_unsigned,
            stack.private_key_pem,
            key_version=KMS_KEY_VERSION,
        )
        tampered_zip = _tamper_skill_zip_signature_or_body(
            clean_other_signed,
            append_body="Tampered post-sign instruction injected after CI attestation.",
        )
        tampered_resp = client.post(
            f"{registry_v1}/{PARENT}/skills",
            params={"skillId": "tampered-finops-skill"},
            json={
                "displayName": "Tampered FinOps Skill",
                "description": "Tampered after signing.",
                "type": "SIMPLE",
                "targetState": "TARGET_STATE_ACTIVE",
                "scanningConfig": {"policy": "ENABLED_BLOCKING"},
                "initialRevision": {
                    "archiveUploadSource": {
                        "archiveContent": base64.b64encode(tampered_zip).decode("ascii")
                    }
                },
            },
        )
        assert tampered_resp.status_code == 200, tampered_resp.text
        tampered_skill = tampered_resp.json()["response"]
        # Registry itself sees clean scan -> STATE_ACTIVE; Middleware MUST catch the signature mismatch!
        assert tampered_skill["state"] == "STATE_ACTIVE"
        assert tampered_skill["defaultRevisionFindingCount"] == 0

        # 4b. Verify Middleware serves valid signed gcp-finops-toolkit and refuses tampered skill
        catalog = client.get(f"{stack.middleware_url}/v1/catalog").json()
        assert [p["name"] for p in catalog["plugins"]] == ["gcp-finops-toolkit"]
        verified_skill_basenames = {s["name"].rsplit("/", 1)[-1] for s in catalog["skills"]}
        assert verified_skill_basenames == {
            "private-cost-anomaly-triage",
            "private-bq-query-optimizer",
        }
        assert all(s["kmsVerified"] is True for s in catalog["skills"])
        rejected_by_name = {
            r["name"].rsplit("/", 1)[-1]: r["reason"] for r in catalog["rejectedSkills"]
        }
        assert "private-malicious-secret-skill" in rejected_by_name
        assert "private-tampered-finops-skill" in rejected_by_name
        assert "signature" in rejected_by_name["private-tampered-finops-skill"].lower()

        # Validate all 4 live Middleware marketplace endpoints against schemas.validators
        claude_mkt = client.get(f"{stack.middleware_url}/v1/marketplace/claude.json").json()
        copilot_mkt = client.get(f"{stack.middleware_url}/v1/marketplace/copilot.json").json()
        codex_mkt = client.get(f"{stack.middleware_url}/v1/marketplace/codex.json").json()
        ag_mkt = client.get(f"{stack.middleware_url}/v1/marketplace/antigravity.json").json()

        assert validate_claude_marketplace(claude_mkt, is_remote_http=True).valid
        assert validate_copilot_marketplace(copilot_mkt, is_remote_http=True).valid
        assert validate_codex_marketplace(codex_mkt).valid
        assert validate_antigravity_marketplace(ag_mkt).valid
        for mkt in (claude_mkt, copilot_mkt, codex_mkt, ag_mkt):
            assert [p["name"] for p in mkt["plugins"]] == ["gcp-finops-toolkit"]

        # Verify plugin ZIP download + SHA-256 consistency
        bundle_resp = client.get(f"{stack.middleware_url}/v1/plugins/gcp-finops-toolkit.zip")
        assert bundle_resp.status_code == 200
        bundle_sha = hashlib.sha256(bundle_resp.content).hexdigest()
        assert bundle_resp.headers["x-bundle-sha256"] == bundle_sha
        assert ag_mkt["plugins"][0]["source"]["sha256"] == bundle_sha
        assert (
            client.get(f"{stack.middleware_url}/v1/plugins/tampered-finops-plugin.zip").status_code
            == 404
        )

        # Verify Middleware POST /mcp gateway tools
        mw_mcp_url = f"{stack.middleware_url}/mcp"
        mw_init = client.post(
            mw_mcp_url,
            json={"jsonrpc": "2.0", "id": 100, "method": "initialize", "params": {}},
        )
        assert mw_init.status_code == 200
        assert "mcp-session-id" in mw_init.headers

        mw_tools = client.post(
            mw_mcp_url,
            json={"jsonrpc": "2.0", "id": 101, "method": "tools/list", "params": {}},
        ).json()["result"]["tools"]
        assert {t["name"] for t in mw_tools} == {
            "search_catalog",
            "load_verified_skill",
            "list_governed_mcp_servers",
            "call_remote_a2a_agent",
        }

        # load_verified_skill returns verified SKILL.md for both valid skills
        for skill_short, expected_heading in (
            ("cost-anomaly-triage", "Cost Anomaly Triage"),
            ("bq-query-optimizer", "BigQuery Query Optimizer"),
        ):
            load_resp = client.post(
                mw_mcp_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 102,
                    "method": "tools/call",
                    "params": {
                        "name": "load_verified_skill",
                        "arguments": {"skill_name": skill_short},
                    },
                },
            ).json()
            assert load_resp["result"]["isError"] is False
            content = load_resp["result"]["structuredContent"]
            assert content["kmsVerified"] is True
            assert content["frontmatter"]["name"] == skill_short
            assert expected_heading in content["instructions"]

        # load_verified_skill REFUSES the tampered skill
        tampered_load = client.post(
            mw_mcp_url,
            json={
                "jsonrpc": "2.0",
                "id": 103,
                "method": "tools/call",
                "params": {
                    "name": "load_verified_skill",
                    "arguments": {"skill_name": "tampered-finops-skill"},
                },
            },
        ).json()
        assert tampered_load["result"]["isError"] is True
        assert "signature" in tampered_load["result"]["structuredContent"]["reason"].lower()

        # call_remote_a2a_agent proxies over real HTTP to the live ephemeral samples/a2a-agent
        a2a_proxy = client.post(
            mw_mcp_url,
            json={
                "jsonrpc": "2.0",
                "id": 104,
                "method": "tools/call",
                "params": {
                    "name": "call_remote_a2a_agent",
                    "arguments": {
                        "agent_name": "finops-advisor-agent",
                        "task": "Why did BigQuery spend in acme-agent-governance spike 45%?",
                    },
                },
            },
        ).json()
        assert a2a_proxy["result"]["isError"] is False
        assert a2a_proxy["result"]["structuredContent"]["endpoint"] == f"{stack.a2a_agent_url}/a2a"
        assert (
            a2a_proxy["result"]["structuredContent"]["response"]["result"]["status"]["state"]
            == "completed"
        )

        # 4c. Tamper directly with a gcp-finops-toolkit skill's signature via a new revision,
        #     verify Middleware refuses serving it, then roll back defaultRevision cleanly.
        orig_cost_zip = (sync_out_dir / "cost-anomaly-triage.zip").read_bytes()
        corrupted_sig_zip = _tamper_skill_zip_signature_or_body(
            orig_cost_zip, corrupt_signature=True
        )
        corrupted_rev = client.post(
            f"{registry_v1}/{PARENT}/skills/private-cost-anomaly-triage/revisions",
            params={"skillRevisionId": "rev-corrupted-sig"},
            json={
                "archiveUploadSource": {
                    "archiveContent": base64.b64encode(corrupted_sig_zip).decode("ascii")
                }
            },
        )
        assert corrupted_rev.status_code == 200, corrupted_rev.text
        promote_corrupted = client.patch(
            f"{registry_v1}/{PARENT}/skills/private-cost-anomaly-triage",
            params={"updateMask": "defaultRevision"},
            json={
                "defaultRevision": (
                    f"{PARENT}/skills/private-cost-anomaly-triage/revisions/rev-corrupted-sig"
                )
            },
        )
        assert promote_corrupted.status_code == 200, promote_corrupted.text

        refused_corrupted = client.post(
            mw_mcp_url,
            json={
                "jsonrpc": "2.0",
                "id": 105,
                "method": "tools/call",
                "params": {
                    "name": "load_verified_skill",
                    "arguments": {"skill_name": "cost-anomaly-triage"},
                },
            },
        ).json()
        assert refused_corrupted["result"]["isError"] is True
        assert "signature" in refused_corrupted["result"]["structuredContent"]["reason"].lower()

        cat_while_corrupted = client.get(f"{stack.middleware_url}/v1/catalog").json()
        assert "private-cost-anomaly-triage" not in {
            s["name"].rsplit("/", 1)[-1] for s in cat_while_corrupted["skills"]
        }

        # Roll back defaultRevision to the valid signed rev-1 and confirm recovery
        rollback_resp = client.patch(
            f"{registry_v1}/{PARENT}/skills/private-cost-anomaly-triage",
            params={"updateMask": "defaultRevision"},
            json={
                "defaultRevision": f"{PARENT}/skills/private-cost-anomaly-triage/revisions/rev-1"
            },
        )
        assert rollback_resp.status_code == 200, rollback_resp.text
        recovered = client.post(
            mw_mcp_url,
            json={
                "jsonrpc": "2.0",
                "id": 106,
                "method": "tools/call",
                "params": {
                    "name": "load_verified_skill",
                    "arguments": {"skill_name": "cost-anomaly-triage"},
                },
            },
        ).json()
        assert recovered["result"]["isError"] is False
        assert recovered["result"]["structuredContent"]["kmsVerified"] is True

        # =====================================================================
        # STAGE 5: hub_sync against live Middleware into pre-seeded temp $HOME
        #          + validate EVERY generated file + verify user configs preserved
        # =====================================================================
        temp_home = tmp_path / "developer_home"
        temp_home.mkdir(parents=True, exist_ok=True)
        preseeded = _preseed_user_home(temp_home)

        hub_result = sync_hub(
            middleware_url=stack.middleware_url,
            home_dir=temp_home,
            tools="all",
            apply_vscode=True,
            no_exec=True,
        )
        assert hub_result.catalog_complete is True
        assert [p.name for p in hub_result.plugins] == ["gcp-finops-toolkit"]
        assert sorted(hub_result.plugins[0].skills) == [
            "bq-query-optimizer",
            "cost-anomaly-triage",
        ]

        hub_dir = temp_home / ".acme-agent-hub"
        extracted_plugin_dir = hub_dir / "plugins" / "gcp-finops-toolkit"
        ag_plugin_dir = temp_home / ".gemini" / "config" / "plugins" / "gcp-finops-toolkit"
        codex_plugin_link = temp_home / ".agents" / "plugins" / "plugins" / "gcp-finops-toolkit"

        # 5a. Validate extracted Polyglot bundle against ALL 5 IDE validators
        for label, validator in (
            ("agent-plugins-1.0", validate_agent_plugin_bundle),
            ("antigravity", validate_antigravity_bundle),
            ("claude-code", validate_claude_plugin_bundle),
            ("copilot", validate_copilot_plugin_bundle),
            ("codex", validate_codex_plugin_bundle),
        ):
            res = validator(extracted_plugin_dir)
            assert res.valid, f"Extracted hub bundle failed {label} validation: {res.errors}"

        # Strict Antigravity v1/plugin.json Draft 2020-12 (additionalProperties: false) + all 7 checks
        ag_res = validate_antigravity_bundle(ag_plugin_dir)
        assert ag_res.valid, f"Installed Antigravity plugin failed validation: {ag_res.errors}"
        ag_manifest = json.loads(
            (ag_plugin_dir / ".antigravity-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        ag_schema = load_schema("antigravity_plugin_v1.schema.json")
        assert ag_schema["additionalProperties"] is False
        Draft202012Validator(ag_schema).validate(ag_manifest)

        # Codex symlinked plugin passes Codex validator
        assert codex_plugin_link.is_symlink()
        codex_bundle_res = validate_codex_plugin_bundle(codex_plugin_link)
        assert codex_bundle_res.valid, codex_bundle_res.errors

        # 5b. Validate EVERY generated marketplace JSON file
        for label, validator, path in (
            ("hub/marketplace.json", validate_copilot_marketplace, hub_dir / "marketplace.json"),
            (
                "hub/.github/plugin/marketplace.json",
                validate_copilot_marketplace,
                hub_dir / ".github" / "plugin" / "marketplace.json",
            ),
            (
                "hub/.claude-plugin/marketplace.json",
                validate_claude_marketplace,
                hub_dir / ".claude-plugin" / "marketplace.json",
            ),
            (
                "hub/.agents/plugins/marketplace.json",
                validate_codex_marketplace,
                hub_dir / ".agents" / "plugins" / "marketplace.json",
            ),
            (
                "home/.agents/plugins/marketplace.json",
                validate_codex_marketplace,
                preseeded["codex_mkt"],
            ),
            (
                "hub/antigravity-marketplace.json",
                validate_antigravity_marketplace,
                hub_dir / "antigravity-marketplace.json",
            ),
        ):
            res = validator(path)
            assert res.valid, f"Generated {label} failed validation: {res.errors}"

        # 5c. Verify generated IDE config files AND preservation of pre-existing user entries
        # Claude Code
        claude_settings = json.loads(preseeded["claude_settings"].read_text(encoding="utf-8"))
        assert claude_settings["theme"] == "dark"
        assert "user-personal-marketplace" in claude_settings["extraKnownMarketplaces"]
        assert "acme-enterprise-marketplace" in claude_settings["extraKnownMarketplaces"]
        assert claude_settings["enabledPlugins"]["user-helper@user-personal-marketplace"] is True
        assert (
            claude_settings["enabledPlugins"]["gcp-finops-toolkit@acme-enterprise-marketplace"]
            is True
        )

        # Antigravity
        ag_mcp = json.loads(preseeded["ag_mcp"].read_text(encoding="utf-8"))
        assert "variables" not in ag_mcp
        assert ag_mcp["mcpServers"]["user-personal-mcp"]["url"] == "http://127.0.0.1:9999/mcp"
        assert ag_mcp["mcpServers"]["finops-mcp-server"]["url"] == mcp_endpoint
        assert ag_mcp["mcpServers"]["finops-mcp-server"]["authProviderType"] == "google_credentials"
        assert ag_mcp["mcpServers"]["agent-hub-gw"]["url"] == mw_mcp_url
        assert ag_mcp["mcpServers"]["agent-hub-gw"]["authProviderType"] == "google_credentials"
        assert preseeded["ag_user_plugin"].is_file()
        assert json.loads(preseeded["ag_user_plugin"].read_text(encoding="utf-8"))["name"] == (
            "user-personal-plugin"
        )

        # OpenAI Codex
        codex_toml_text = preseeded["codex_config"].read_text(encoding="utf-8")
        assert "# Personal Codex settings" in codex_toml_text
        codex_cfg = tomllib.loads(codex_toml_text)
        assert codex_cfg["model"] == "o4-mini"
        assert codex_cfg["mcp_servers"]["user-codex-mcp"]["url"] == "http://127.0.0.1:9998/mcp"
        assert codex_cfg["mcp_servers"]["finops-mcp-server"]["url"] == mcp_endpoint
        assert codex_cfg["mcp_servers"]["agent-hub-gw"]["url"] == mw_mcp_url

        assert preseeded["codex_user_agent"].is_file()
        codex_hub_agent_path = temp_home / ".codex" / "agents" / "finops-advisor-agent.toml"
        assert codex_hub_agent_path.is_file()
        codex_hub_agent = tomllib.loads(codex_hub_agent_path.read_text(encoding="utf-8"))
        assert codex_hub_agent["name"] == "finops-advisor-agent"
        assert codex_hub_agent["description"]
        assert codex_hub_agent["instructions"]

        codex_home_mkt = json.loads(preseeded["codex_mkt"].read_text(encoding="utf-8"))
        assert {p["name"] for p in codex_home_mkt["plugins"]} == {
            "user-codex-plugin",
            "gcp-finops-toolkit",
        }

        # VS Code & Copilot
        vscode_snippet = json.loads(
            (hub_dir / "vscode-settings-snippet.json").read_text(encoding="utf-8")
        )
        assert vscode_snippet["chat.plugins.enabled"] is True
        vscode_user_cfg = json.loads(preseeded["vscode_settings"].read_text(encoding="utf-8"))
        assert vscode_user_cfg["editor.fontSize"] == 14
        assert vscode_user_cfg["chat.pluginLocations"]["/tmp/user-custom-plugin"] is True
        assert vscode_user_cfg["chat.pluginLocations"][str(extracted_plugin_dir.resolve())] is True

        # Ensure malicious and tampered plugins were NEVER hydrated
        assert not (hub_dir / "plugins" / "malicious-toolkit").exists()
        assert not (hub_dir / "plugins" / "tampered-finops-plugin").exists()

        # Verify hub_sync status reports healthy synced state
        status = get_hub_status(home_dir=temp_home)
        assert status["synced"] is True
        assert status["last_sync_complete"] is True
        assert list(status["plugins"].keys()) == ["gcp-finops-toolkit"]
        assert status["plugins"]["gcp-finops-toolkit"]["installed_on_disk"] is True

        # =====================================================================
        # STAGE 6: platform_agent PlatformAgentConsumer against live mock_registry
        #          (skill search, ?alt=media 302 header stripping, KMS verification,
        #           MCP/Agent discovery & Auth Manager binding resolution)
        # =====================================================================
        # First prove mock_registry's /scotty-signed/{token} strictly rejects leaked headers (HTTP 403)
        redirect_probe = client.get(
            f"{registry_v1}/{PARENT}/skills/private-cost-anomaly-triage/revisions/rev-1",
            params={"alt": "media"},
            headers={
                "Authorization": "Bearer leaked-secret",
                "x-goog-user-project": PROJECT_ID,
            },
            follow_redirects=False,
        )
        assert redirect_probe.status_code == 302
        signed_location = redirect_probe.headers["location"]
        assert "/scotty-signed/" in signed_location
        leaked_resp = client.get(
            signed_location,
            headers={
                "Authorization": "Bearer leaked-secret",
                "x-goog-user-project": PROJECT_ID,
            },
        )
        assert leaked_resp.status_code == 403

    async def _run_platform_agent_stage() -> None:
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            base_url=registry_v1,
            access_token="e2e-platform-agent-bearer-token",
            quota_project_id=PROJECT_ID,
            kms_public_key_pem=stack.public_key_pem,
            require_kms_signature=True,
        ) as consumer:
            # 6a. Semantic skill search filtered to APPROVED_SKILL_FILTER
            found_skills = await consumer.search_skills(
                "bigquery cost anomaly triage",
                search_type="SEMANTIC",
                filter_expr=APPROVED_SKILL_FILTER,
            )
            found_skill_names = {s["name"].rsplit("/", 1)[-1] for s in found_skills}
            assert "private-cost-anomaly-triage" in found_skill_names
            assert "private-bq-query-optimizer" in found_skill_names
            assert "private-malicious-secret-skill" not in found_skill_names

            # 6b. 2-step fetch + ?alt=media 302 redirect header-stripping download + SHA-256 + KMS
            extracted_cost = await consumer.fetch_and_extract_skill(
                consumer.private_skill_name("cost-anomaly-triage")
            )
            assert extracted_cost.name == "cost-anomaly-triage"
            assert extracted_cost.kms_verified is True
            assert extracted_cost.kms_key_version == KMS_KEY_VERSION
            assert "Cost Anomaly Triage" in extracted_cost.instructions
            assert "triage-checklist.md" in extracted_cost.references

            extracted_bq = await consumer.fetch_and_extract_skill(
                consumer.private_skill_name("bq-query-optimizer")
            )
            assert extracted_bq.name == "bq-query-optimizer"
            assert extracted_bq.kms_verified is True
            assert "BigQuery Query Optimizer" in extracted_bq.instructions

            # 6c. PlatformAgentConsumer rejects tampered signature AND quarantined malicious skill
            with pytest.raises(SkillSignatureError):
                await consumer.fetch_and_extract_skill(
                    consumer.private_skill_name("tampered-finops-skill")
                )
            with pytest.raises(SkillGovernanceError):
                await consumer.fetch_and_extract_skill(
                    consumer.private_skill_name("malicious-secret-skill")
                )

            # 6d. MCP Server & A2A Agent discovery + Auth Manager Binding resolution
            mcp_servers = await consumer.search_mcp_servers("finops")
            assert len(mcp_servers) == 1
            assert mcp_servers[0]["name"].endswith("/mcpServers/finops-mcp-server")
            assert mcp_servers[0]["interfaces"][0]["url"] == mcp_endpoint
            assert len(mcp_servers[0]["tools"]) == 3

            agents = await consumer.search_agents("finops")
            assert len(agents) == 1
            assert agents[0]["name"].endswith("/agents/finops-advisor-agent")
            assert agents[0]["interfaces"][0]["url"] == f"{stack.a2a_agent_url}/a2a"

            resolved_binding = await consumer.resolve_auth_binding(
                source_identifier=agents[0]["agentId"],
                target_identifier=mcp_servers[0]["mcpServerId"],
            )
            assert resolved_binding is not None
            assert resolved_binding["authProvider"].endswith("/authProviders/finops-mcp-oauth")

        # 6e. End-to-end PlatformAgentOrchestrator (rest mode) discovery
        orchestrator = PlatformAgentOrchestrator(
            OrchestratorConfig(
                project_id=PROJECT_ID,
                location=LOCATION,
                mode="rest",
                registry_base_url=registry_v1,
                skill_search_string="cost anomaly triage",
                mcp_search_string="finops",
                agent_search_string="finops",
                source_agent_urn=projected_agent["agentId"],
                require_kms_signature=True,
                kms_public_key_pem_path=str(stack.public_key_pem_path),
                access_token="e2e-orchestrator-token",
            )
        )
        discovered = await orchestrator.discover_rest(fetch_skill_contents=True)
        assert any(s.name == "cost-anomaly-triage" and s.kms_verified for s in discovered.extracted_skills)
        assert len(discovered.mcp_servers) == 1
        assert len(discovered.agents) == 1
        assert projected_mcp["mcpServerId"] in discovered.resolved_bindings

    asyncio.run(_run_platform_agent_stage())


def test_e2e_kms_cross_tool_interop_and_hub_sync_lifecycle(
    live_workshop_stack: LiveWorkshopStack, tmp_path: Path
) -> None:
    """Verify cross-tool KMS signing interop and hub_sync toggle preservation + clean pruning."""
    stack = live_workshop_stack
    registry_v1 = f"{stack.mock_registry_url}/v1"
    sign_verify_mod = _load_module(
        "e2e_kms_sign_and_verify",
        REPO_ROOT / "security-governance" / "kms" / "sign_and_verify.py",
    )

    # 1. Sync gcp-finops-toolkit with ECDSA P-256 signing via plugin_sync
    dist_dir = tmp_path / "dist"
    runner = CliRunner()
    res = runner.invoke(
        plugin_sync_cli,
        [
            "sync",
            "--plugin-dir",
            str(STARTER_PLUGIN_DIR),
            "--registry-url",
            registry_v1,
            "--project",
            PROJECT_ID,
            "--location",
            LOCATION,
            "--sign-pem",
            str(stack.private_key_pem_path),
            "--kms-key-version",
            KMS_KEY_VERSION,
            "--output-dir",
            str(dist_dir),
            "--skip-mcp-probe",
        ],
    )
    assert res.exit_code == 0, res.output

    # 2. Verify plugin_sync-signed ZIP passes security-governance/kms/sign_and_verify.py
    signed_zip = (dist_dir / "cost-anomaly-triage.zip").read_bytes()
    expected_sha = hashlib.sha256(signed_zip).hexdigest()
    report = sign_verify_mod.verify_skill_zip(
        signed_zip,
        public_key_pem=stack.public_key_pem,
        expected_sha256=expected_sha,
    )
    assert report.verified is True
    assert report.signature_verified is True
    assert report.sha256_matched is True

    # Tampered copy MUST fail security-governance/kms/sign_and_verify.py too
    tampered_zip = _tamper_skill_zip_signature_or_body(signed_zip, corrupt_signature=True)
    bad_report = sign_verify_mod.verify_skill_zip(
        tampered_zip,
        public_key_pem=stack.public_key_pem,
        expected_sha256=hashlib.sha256(tampered_zip).hexdigest(),
    )
    assert bad_report.verified is False
    assert bad_report.signature_verified is False

    # 3. Initial hub_sync into pre-seeded HOME
    temp_home = tmp_path / "lifecycle_home"
    temp_home.mkdir(parents=True, exist_ok=True)
    preseeded = _preseed_user_home(temp_home)

    first = sync_hub(
        middleware_url=stack.middleware_url,
        home_dir=temp_home,
        tools="all",
        apply_vscode=True,
        no_exec=True,
    )
    assert [p.name for p in first.plugins] == ["gcp-finops-toolkit"]

    # 4. User customizes disabled/disabledTools on hydrated Antigravity MCP entry;
    #    a re-sync MUST preserve those user preferences.
    ag_mcp = json.loads(preseeded["ag_mcp"].read_text(encoding="utf-8"))
    ag_mcp["mcpServers"]["finops-mcp-server"]["disabled"] = True
    ag_mcp["mcpServers"]["finops-mcp-server"]["disabledTools"] = ["delete_idle_dataset"]
    preseeded["ag_mcp"].write_text(json.dumps(ag_mcp, indent=2) + "\n", encoding="utf-8")

    second = sync_hub(
        middleware_url=stack.middleware_url,
        home_dir=temp_home,
        tools="all",
        apply_vscode=True,
        no_exec=True,
    )
    assert [p.name for p in second.plugins] == ["gcp-finops-toolkit"]
    ag_mcp_after = json.loads(preseeded["ag_mcp"].read_text(encoding="utf-8"))
    assert ag_mcp_after["mcpServers"]["finops-mcp-server"]["disabled"] is True
    assert ag_mcp_after["mcpServers"]["finops-mcp-server"]["disabledTools"] == [
        "delete_idle_dataset"
    ]
    assert ag_mcp_after["mcpServers"]["user-personal-mcp"]["url"] == "http://127.0.0.1:9999/mcp"

    # 5. Disable both skills in mock_registry -> Middleware serves 0 plugins ->
    #    clean hub_sync prunes gcp-finops-toolkit while keeping ALL user-authored configs!
    with httpx.Client(timeout=10.0) as client:
        for sid in ("private-cost-anomaly-triage", "private-bq-query-optimizer"):
            patch_resp = client.patch(
                f"{registry_v1}/{PARENT}/skills/{sid}",
                params={"updateMask": "targetState"},
                json={"targetState": "TARGET_STATE_DISABLED"},
            )
            assert patch_resp.status_code == 200, patch_resp.text

        empty_cat = client.get(f"{stack.middleware_url}/v1/catalog").json()
        assert empty_cat["plugins"] == []

    third = sync_hub(
        middleware_url=stack.middleware_url,
        home_dir=temp_home,
        tools="all",
        apply_vscode=True,
        no_exec=True,
    )
    assert third.plugins == []
    assert third.pruned_plugins == ["gcp-finops-toolkit"]
    assert not (temp_home / ".acme-agent-hub" / "plugins" / "gcp-finops-toolkit").exists()
    assert not (temp_home / ".gemini" / "config" / "plugins" / "gcp-finops-toolkit").exists()

    # Every pre-existing user config survives clean pruning untouched
    claude_cfg = json.loads(preseeded["claude_settings"].read_text(encoding="utf-8"))
    assert claude_cfg["enabledPlugins"]["user-helper@user-personal-marketplace"] is True
    assert "gcp-finops-toolkit@acme-enterprise-marketplace" not in claude_cfg["enabledPlugins"]

    ag_mcp_final = json.loads(preseeded["ag_mcp"].read_text(encoding="utf-8"))
    assert ag_mcp_final["mcpServers"]["user-personal-mcp"]["url"] == "http://127.0.0.1:9999/mcp"
    assert preseeded["ag_user_plugin"].is_file()

    codex_cfg_final = tomllib.loads(preseeded["codex_config"].read_text(encoding="utf-8"))
    assert codex_cfg_final["mcp_servers"]["user-codex-mcp"]["url"] == "http://127.0.0.1:9998/mcp"
    assert preseeded["codex_user_agent"].is_file()
    assert not (temp_home / ".codex" / "agents" / "finops-advisor-agent.toml").exists()
    codex_mkt_final = json.loads(preseeded["codex_mkt"].read_text(encoding="utf-8"))
    assert [p["name"] for p in codex_mkt_final["plugins"]] == ["user-codex-plugin"]


def _git_run(repo_dir: Path, *args: str) -> str:
    """Execute a hermetic git command inside `repo_dir` without relying on global git config."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Workshop E2E",
        "GIT_AUTHOR_EMAIL": "workshop-e2e@acme.example.com",
        "GIT_COMMITTER_NAME": "Workshop E2E",
        "GIT_COMMITTER_EMAIL": "workshop-e2e@acme.example.com",
    }
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_e2e_dual_skill_sources_local_and_cloned_git_repo(
    live_workshop_stack: LiveWorkshopStack, tmp_path: Path
) -> None:
    """End-to-end verification of Option 1 (local-skills/) AND Option 2 (cloned Git repo).

    Runs against live in-process `mock_registry` + `middleware` (`REQUIRE_KMS_SIGNATURE=true`)
    + `tools.hub_sync`:
      1. Option 1: Ingests `local-skills/` as `local-ops-toolkit` (3 skills, ECDSA P-256 signed).
      2. Option 2: Creates a real temp local Git repo containing `central-security-checker/SKILL.md`,
         commits it, and ingests via `tools.plugin_sync ingest --source-type git` as
         `central-sec-toolkit` (ECDSA P-256 signed, real 40-char `git_commit`).
      3. Verifies Middleware serves BOTH bundles across `/v1/catalog`, all 4 IDE marketplaces,
         `/v1/plugins/{name}.zip`, and `POST /mcp` (`load_verified_skill`) with exact provenance.
      4. Runs `hub_sync` into a temp `$HOME` and validates ALL generated Polyglot bundles &
         4-IDE configs with `schemas.validators`.
    """
    stack = live_workshop_stack
    registry_v1 = f"{stack.mock_registry_url}/v1"
    runner = CliRunner()

    # =========================================================================
    # 1. Option 1: Ingest pre-prepared `local-skills/` as `local-ops-toolkit`
    # =========================================================================
    opt1_out_dir = tmp_path / "opt1_local_dist"
    opt1_res = runner.invoke(
        plugin_sync_cli,
        [
            "ingest",
            "--source-type",
            "local",
            "--path",
            str(LOCAL_SKILLS_DIR),
            "--plugin-name",
            "local-ops-toolkit",
            "--registry-url",
            registry_v1,
            "--project",
            PROJECT_ID,
            "--location",
            LOCATION,
            "--target-state",
            "TARGET_STATE_ACTIVE",
            "--scanning-policy",
            "ENABLED_BLOCKING",
            "--sign-pem",
            str(stack.private_key_pem_path),
            "--kms-key-version",
            KMS_KEY_VERSION,
            "--output-dir",
            str(opt1_out_dir),
        ],
    )
    assert opt1_res.exit_code == 0, f"Option 1 local ingest failed:\n{opt1_res.output}"
    opt1_summary = json.loads(opt1_res.output)
    assert opt1_summary["source_type"] == "local"
    assert opt1_summary["layout"] == "standalone_skills"
    assert opt1_summary["plans_count"] == 1
    assert opt1_summary["skills_count"] == 3
    opt1_plan = opt1_summary["plans"][0]
    assert opt1_plan["plugin_name"] == "local-ops-toolkit"
    assert all(s["signed"] is True for s in opt1_plan["skills"])
    assert {s["private_skill_id"] for s in opt1_plan["skills"]} == {
        "private-cloud-run-debugger",
        "private-terraform-iam-auditor",
        "private-incident-postmortem-writer",
    }

    # =========================================================================
    # 2. Option 2: Create a real temporary Git repo & ingest via `--source-type git`
    # =========================================================================
    tmp_git_repo = tmp_path / "central-skills-git-repo"
    custom_skill_dir = tmp_git_repo / "skills" / "central-security-checker"
    custom_skill_dir.mkdir(parents=True, exist_ok=True)
    (custom_skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: central-security-checker\n"
        "description: Audit service configs for least-privilege IAM, Workload Identity Federation, and secret hygiene.\n"
        "license: Apache-2.0\n"
        "metadata:\n"
        '  category: "security-governance"\n'
        '  domain: "central-security"\n'
        "---\n\n"
        "# Central Security Checker\n\n"
        "Inspect deployment manifests and IAM policies to verify Workload Identity Federation,\n"
        "read-only Agent Registry runtime bindings, and zero hardcoded credentials.\n",
        encoding="utf-8",
    )
    _git_run(tmp_git_repo, "init")
    _git_run(tmp_git_repo, "checkout", "-B", "main")
    _git_run(tmp_git_repo, "add", ".")
    _git_run(tmp_git_repo, "commit", "-m", "Add centralized central-security-checker skill")
    expected_commit = _git_run(tmp_git_repo, "rev-parse", "HEAD")
    assert HEX40_RE.match(expected_commit), f"Expected 40-char commit SHA, got {expected_commit!r}"

    opt2_out_dir = tmp_path / "opt2_git_dist"
    opt2_res = runner.invoke(
        plugin_sync_cli,
        [
            "ingest",
            "--source-type",
            "git",
            "--repo-url",
            str(tmp_git_repo),
            "--ref",
            "main",
            "--plugin-name",
            "central-sec-toolkit",
            "--registry-url",
            registry_v1,
            "--project",
            PROJECT_ID,
            "--location",
            LOCATION,
            "--target-state",
            "TARGET_STATE_ACTIVE",
            "--scanning-policy",
            "ENABLED_BLOCKING",
            "--sign-pem",
            str(stack.private_key_pem_path),
            "--kms-key-version",
            KMS_KEY_VERSION,
            "--output-dir",
            str(opt2_out_dir),
        ],
    )
    assert opt2_res.exit_code == 0, f"Option 2 git ingest failed:\n{opt2_res.output}"
    opt2_summary = json.loads(opt2_res.output)
    assert opt2_summary["source_type"] == "git"
    assert opt2_summary["layout"] == "standalone_skills"
    assert opt2_summary["git_commit"] == expected_commit
    assert opt2_summary["plans_count"] == 1
    assert opt2_summary["skills_count"] == 1
    opt2_plan = opt2_summary["plans"][0]
    assert opt2_plan["plugin_name"] == "central-sec-toolkit"
    assert len(opt2_plan["skills"]) == 1
    assert opt2_plan["skills"][0]["private_skill_id"] == "private-central-security-checker"
    assert opt2_plan["skills"][0]["signed"] is True

    # =========================================================================
    # 3. Verify Middleware (REQUIRE_KMS_SIGNATURE=true) serves BOTH toolkits
    # =========================================================================
    expected_plugins = ["central-sec-toolkit", "local-ops-toolkit"]
    with httpx.Client(timeout=20.0) as client:
        catalog = client.get(f"{stack.middleware_url}/v1/catalog").json()
        assert catalog["rejectedSkills"] == []
        assert [p["name"] for p in catalog["plugins"]] == expected_plugins

        plugins_by_name = {p["name"]: p for p in catalog["plugins"]}
        assert sorted(plugins_by_name["local-ops-toolkit"]["skills"]) == [
            "cloud-run-debugger",
            "incident-postmortem-writer",
            "terraform-iam-auditor",
        ]
        assert plugins_by_name["central-sec-toolkit"]["skills"] == ["central-security-checker"]

        assert {s["name"].rsplit("/", 1)[-1] for s in catalog["skills"]} == {
            "private-cloud-run-debugger",
            "private-terraform-iam-auditor",
            "private-incident-postmortem-writer",
            "private-central-security-checker",
        }
        assert all(s["kmsVerified"] is True for s in catalog["skills"])

        # Validate all 4 IDE marketplace feeds serve both plugins and pass schemas.validators
        claude_mkt = client.get(f"{stack.middleware_url}/v1/marketplace/claude.json").json()
        copilot_mkt = client.get(f"{stack.middleware_url}/v1/marketplace/copilot.json").json()
        codex_mkt = client.get(f"{stack.middleware_url}/v1/marketplace/codex.json").json()
        ag_mkt = client.get(f"{stack.middleware_url}/v1/marketplace/antigravity.json").json()

        assert validate_claude_marketplace(claude_mkt, is_remote_http=True).valid
        assert validate_copilot_marketplace(copilot_mkt, is_remote_http=True).valid
        assert validate_codex_marketplace(codex_mkt).valid
        assert validate_antigravity_marketplace(ag_mkt).valid
        for mkt in (claude_mkt, copilot_mkt, codex_mkt, ag_mkt):
            assert [p["name"] for p in mkt["plugins"]] == expected_plugins

        ag_sha_by_name = {p["name"]: p["source"]["sha256"] for p in ag_mkt["plugins"]}

        # Download & inspect /v1/plugins/{name}.zip for BOTH bundles
        local_zip_resp = client.get(f"{stack.middleware_url}/v1/plugins/local-ops-toolkit.zip")
        assert local_zip_resp.status_code == 200
        local_zip_sha = hashlib.sha256(local_zip_resp.content).hexdigest()
        assert local_zip_resp.headers["x-bundle-sha256"] == local_zip_sha
        assert ag_sha_by_name["local-ops-toolkit"] == local_zip_sha
        with zipfile.ZipFile(io.BytesIO(local_zip_resp.content), mode="r") as zf:
            local_members = set(zf.namelist())
            assert "skills/cloud-run-debugger/SKILL.md" in local_members
            assert "skills/cloud-run-debugger/references/triage-runbook.md" in local_members
            assert "skills/terraform-iam-auditor/SKILL.md" in local_members
            assert "skills/terraform-iam-auditor/references/least-privilege-checklist.md" in local_members
            assert "skills/incident-postmortem-writer/SKILL.md" in local_members

        central_zip_resp = client.get(f"{stack.middleware_url}/v1/plugins/central-sec-toolkit.zip")
        assert central_zip_resp.status_code == 200
        central_zip_sha = hashlib.sha256(central_zip_resp.content).hexdigest()
        assert central_zip_resp.headers["x-bundle-sha256"] == central_zip_sha
        assert ag_sha_by_name["central-sec-toolkit"] == central_zip_sha
        with zipfile.ZipFile(io.BytesIO(central_zip_resp.content), mode="r") as zf:
            central_skill_md = zf.read("skills/central-security-checker/SKILL.md").decode("utf-8")
            central_fm, _central_body, central_err = parse_frontmatter(central_skill_md)
            assert central_err is None and central_fm is not None
            assert central_fm["metadata"]["plugin_name"] == "central-sec-toolkit"
            assert central_fm["metadata"]["source_repo"] == str(tmp_git_repo)
            assert central_fm["metadata"]["git_commit"] == expected_commit
            assert central_fm["metadata"]["kms_key_version"] == KMS_KEY_VERSION
            assert central_fm["metadata"]["kms_signature"]

        # Verify POST /mcp (`load_verified_skill`) across all 4 skills + exact git_commit provenance
        mw_mcp_url = f"{stack.middleware_url}/mcp"
        for req_id, (skill_short, expected_heading) in enumerate(
            (
                ("cloud-run-debugger", "Cloud Run Debugger"),
                ("terraform-iam-auditor", "Terraform IAM Auditor"),
                ("incident-postmortem-writer", "Incident Postmortem Writer"),
                ("central-security-checker", "Central Security Checker"),
            ),
            start=200,
        ):
            load_resp = client.post(
                mw_mcp_url,
                json={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {
                        "name": "load_verified_skill",
                        "arguments": {"skill_name": skill_short},
                    },
                },
            ).json()
            assert load_resp["result"]["isError"] is False, load_resp
            structured = load_resp["result"]["structuredContent"]
            assert structured["kmsVerified"] is True
            assert structured["frontmatter"]["name"] == skill_short
            assert expected_heading in structured["instructions"]
            if skill_short == "central-security-checker":
                assert structured["frontmatter"]["metadata"]["git_commit"] == expected_commit
                assert structured["frontmatter"]["metadata"]["plugin_name"] == "central-sec-toolkit"
            else:
                assert structured["frontmatter"]["metadata"]["plugin_name"] == "local-ops-toolkit"

    # =========================================================================
    # 4. Run hub_sync into a temp $HOME & validate ALL bundles + 4-IDE configs
    # =========================================================================
    temp_home = tmp_path / "dual_sources_home"
    temp_home.mkdir(parents=True, exist_ok=True)
    preseeded = _preseed_user_home(temp_home)

    hub_result = sync_hub(
        middleware_url=stack.middleware_url,
        home_dir=temp_home,
        tools="all",
        apply_vscode=True,
        no_exec=True,
    )
    assert hub_result.catalog_complete is True
    assert sorted(p.name for p in hub_result.plugins) == expected_plugins

    hub_dir = temp_home / ".acme-agent-hub"
    ag_schema = load_schema("antigravity_plugin_v1.schema.json")
    assert ag_schema["additionalProperties"] is False

    for plugin_name in expected_plugins:
        extracted_dir = hub_dir / "plugins" / plugin_name
        ag_plugin_dir = temp_home / ".gemini" / "config" / "plugins" / plugin_name
        codex_plugin_link = temp_home / ".agents" / "plugins" / "plugins" / plugin_name

        for label, validator in (
            ("agent-plugins-1.0", validate_agent_plugin_bundle),
            ("antigravity", validate_antigravity_bundle),
            ("claude-code", validate_claude_plugin_bundle),
            ("copilot", validate_copilot_plugin_bundle),
            ("codex", validate_codex_plugin_bundle),
        ):
            res = validator(extracted_dir)
            assert res.valid, f"Extracted {plugin_name} failed {label} validation: {res.errors}"

        ag_res = validate_antigravity_bundle(ag_plugin_dir)
        assert ag_res.valid, f"Installed Antigravity {plugin_name} failed validation: {ag_res.errors}"
        ag_manifest = json.loads(
            (ag_plugin_dir / ".antigravity-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(ag_schema).validate(ag_manifest)

        assert codex_plugin_link.is_symlink()
        codex_res = validate_codex_plugin_bundle(codex_plugin_link)
        assert codex_res.valid, f"Codex symlink {plugin_name} failed validation: {codex_res.errors}"

    # Validate every generated marketplace catalog JSON across all 4 IDEs
    for label, validator, path in (
        ("hub/marketplace.json", validate_copilot_marketplace, hub_dir / "marketplace.json"),
        (
            "hub/.github/plugin/marketplace.json",
            validate_copilot_marketplace,
            hub_dir / ".github" / "plugin" / "marketplace.json",
        ),
        (
            "hub/.claude-plugin/marketplace.json",
            validate_claude_marketplace,
            hub_dir / ".claude-plugin" / "marketplace.json",
        ),
        (
            "hub/.agents/plugins/marketplace.json",
            validate_codex_marketplace,
            hub_dir / ".agents" / "plugins" / "marketplace.json",
        ),
        (
            "home/.agents/plugins/marketplace.json",
            validate_codex_marketplace,
            preseeded["codex_mkt"],
        ),
        (
            "hub/antigravity-marketplace.json",
            validate_antigravity_marketplace,
            hub_dir / "antigravity-marketplace.json",
        ),
    ):
        res = validator(path)
        assert res.valid, f"Generated {label} failed validation: {res.errors}"

    # Verify all 4 IDE configs contain both bundles while preserving user-authored entries
    claude_cfg = json.loads(preseeded["claude_settings"].read_text(encoding="utf-8"))
    assert claude_cfg["enabledPlugins"]["user-helper@user-personal-marketplace"] is True
    for plugin_name in expected_plugins:
        assert claude_cfg["enabledPlugins"][f"{plugin_name}@acme-enterprise-marketplace"] is True

    ag_mcp = json.loads(preseeded["ag_mcp"].read_text(encoding="utf-8"))
    assert "variables" not in ag_mcp
    assert ag_mcp["mcpServers"]["user-personal-mcp"]["url"] == "http://127.0.0.1:9999/mcp"
    assert ag_mcp["mcpServers"]["agent-hub-gw"]["url"] == f"{stack.middleware_url}/mcp"
    assert ag_mcp["mcpServers"]["agent-hub-gw"]["authProviderType"] == "google_credentials"

    codex_cfg = tomllib.loads(preseeded["codex_config"].read_text(encoding="utf-8"))
    assert codex_cfg["mcp_servers"]["user-codex-mcp"]["url"] == "http://127.0.0.1:9998/mcp"
    assert codex_cfg["mcp_servers"]["agent-hub-gw"]["url"] == f"{stack.middleware_url}/mcp"
    codex_home_mkt = json.loads(preseeded["codex_mkt"].read_text(encoding="utf-8"))
    assert {p["name"] for p in codex_home_mkt["plugins"]} == {
        "user-codex-plugin",
        "local-ops-toolkit",
        "central-sec-toolkit",
    }
    for plugin_name in expected_plugins:
        agent_toml = temp_home / ".codex" / "agents" / f"{plugin_name}-advisor.toml"
        assert agent_toml.is_file(), f"Missing synthesized Codex agent TOML: {agent_toml}"
        parsed_agent = tomllib.loads(agent_toml.read_text(encoding="utf-8"))
        assert parsed_agent["name"] == f"{plugin_name}-advisor"

    vscode_cfg = json.loads(preseeded["vscode_settings"].read_text(encoding="utf-8"))
    assert vscode_cfg["chat.pluginLocations"]["/tmp/user-custom-plugin"] is True
    for plugin_name in expected_plugins:
        extracted_path = str((hub_dir / "plugins" / plugin_name).resolve())
        assert vscode_cfg["chat.pluginLocations"][extracted_path] is True

    hub_status = get_hub_status(home_dir=temp_home)
    assert hub_status["synced"] is True
    assert hub_status["last_sync_complete"] is True
    assert sorted(hub_status["plugins"].keys()) == expected_plugins
    for plugin_name in expected_plugins:
        assert hub_status["plugins"][plugin_name]["installed_on_disk"] is True


