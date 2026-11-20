"""Unit tests for Track 1D: Enterprise Platform Agent (ADK + Agent Registry v1 REST consumer)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import io
import json
import os
import stat
import struct
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from platform_agent import (
    APPROVED_SKILL_FILTER,
    OrchestratorConfig,
    PlatformAgentConsumer,
    PlatformAgentOrchestrator,
    SkillGovernanceError,
    SkillIntegrityError,
    SkillSignatureError,
    build_adk_agent_config,
    build_live_adk_agent,
    compute_canonical_skill_digest,
    extract_skill_zip,
)
from platform_agent.adk_orchestrator import main as orchestrator_main

WORKSHOP_ROOT = Path(__file__).resolve().parent.parent
WORKSHOP_SPEC = json.loads((WORKSHOP_ROOT / "WORKSHOP_SPEC.json").read_text(encoding="utf-8"))

PROJECT_ID = WORKSHOP_SPEC["project_defaults"]["project_id"]
LOCATION = WORKSHOP_SPEC["project_defaults"]["location"]
PARENT = f"projects/{PROJECT_ID}/locations/{LOCATION}"
SAMPLE_PLUGIN = WORKSHOP_SPEC["sample_plugin"]
MCP_URN = WORKSHOP_SPEC["urns"]["mcp_server"]
AGENT_URN = WORKSHOP_SPEC["urns"]["agent"]
SKILL_RESOURCE = WORKSHOP_SPEC["resource_names"]["skills"]["cost-anomaly-triage"]
SKILL_URN = WORKSHOP_SPEC["urns"]["skills"]["cost-anomaly-triage"]
REVISION_RESOURCE = f"{SKILL_RESOURCE}/revisions/rev-20260401-a1"


def _make_skill_md(
    *,
    name: str = "cost-anomaly-triage",
    description: str = "Triage GCP billing and BigQuery cost spikes using approved FinOps runbooks.",
    metadata: dict[str, str] | None = None,
    body: str = "# Cost Anomaly Triage\n\n1. Query billing anomalies via finops-mcp-server.\n2. Recommend committed-use optimizations.\n",
) -> str:
    frontmatter: dict[str, Any] = {
        "name": name,
        "description": description,
        "license": "Apache-2.0",
        "compatibility": "Requires finops-mcp-server",
    }
    if metadata is not None:
        frontmatter["metadata"] = metadata
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False).strip()
    return f"---\n{yaml_text}\n---\n\n{body}"


def _build_skill_zip(
    *,
    skill_md: str,
    prefix: str = "cost-anomaly-triage/",
    extra_files: dict[str, bytes | str] | None = None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{prefix}SKILL.md", skill_md.encode("utf-8"))
        for rel, data in (extra_files or {}).items():
            raw = data.encode("utf-8") if isinstance(data, str) else data
            zf.writestr(f"{prefix}{rel}", raw)
    return buf.getvalue()


def _generate_ec_p256_keypair() -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, public_pem


def _build_mock_registry_transport(
    *,
    zip_bytes: bytes,
    reported_sha256: str | None = None,
    skill_state: str = "STATE_ACTIVE",
    scan_state: str = "SUCCEEDED",
    scanning_policy: str = "ENABLED_BLOCKING",
    scan_findings: list[str] | None = None,
    revision_metadata: dict[str, str] | None = None,
    redirect_url: str = "https://storage.googleapis.com/agentregistry-skills-signed/rev-20260401-a1.zip?X-Goog-Signature=deadbeef",
    recorded_requests: list[dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    """In-process Agent Registry v1 + GCS signed-URL 302 redirect mock transport."""
    actual_sha = hashlib.sha256(zip_bytes).hexdigest()
    sha_to_report = reported_sha256 if reported_sha256 is not None else actual_sha

    skill_payload = {
        "name": SKILL_RESOURCE,
        "uid": "11111111-2222-4333-8444-555555555555",
        "skillId": SKILL_URN,
        "displayName": "Cost Anomaly Triage",
        "description": "Triage GCP billing anomalies safely.",
        "type": "SIMPLE",
        "state": skill_state,
        "targetState": "TARGET_STATE_ACTIVE",
        "defaultRevision": REVISION_RESOURCE,
        "frontmatter": {
            "name": "cost-anomaly-triage",
            "description": "Triage GCP billing anomalies safely.",
            "metadata": revision_metadata or {"plugin_name": SAMPLE_PLUGIN["name"]},
        },
        "scanningConfig": {"policy": scanning_policy},
    }

    revision_payload = {
        "name": REVISION_RESOURCE,
        "uid": "66666666-7777-4888-8999-000000000000",
        "state": "ACTIVE",
        "sha256Hash": sha_to_report,
        "sizeBytes": str(len(zip_bytes)),
        "frontmatter": skill_payload["frontmatter"],
        "scanResult": {
            "scanState": scan_state,
            "scanTime": "2026-04-01T12:00:00Z",
            "findings": scan_findings or [],
        },
    }

    mcp_server_payload = {
        "name": f"{PARENT}/mcpServers/{SAMPLE_PLUGIN['mcp_service_id']}",
        "mcpServerId": MCP_URN,
        "displayName": "FinOps MCP Server",
        "description": "Read-only FinOps billing & BigQuery optimization MCP server.",
        "interfaces": [
            {
                "url": "https://finops-mcp.internal.example.com/mcp",
                "protocolBinding": "HTTP_JSON",
            }
        ],
        "tools": [
            {
                "name": "list_billing_anomalies",
                "description": "List billing cost anomalies.",
                "annotations": {
                    "title": "List Billing Anomalies",
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            }
        ],
    }

    agent_payload = {
        "name": f"{PARENT}/agents/{SAMPLE_PLUGIN['agent_service_id']}",
        "agentId": AGENT_URN,
        "displayName": "FinOps Advisor Agent",
        "description": "A2A domain advisor for cloud cost triage.",
        "version": "1.0.0",
        "protocols": [
            {
                "type": "A2A_AGENT",
                "protocolVersion": "1.0",
                "interfaces": [
                    {
                        "url": "https://finops-agent.internal.example.com/a2a",
                        "protocolBinding": "JSONRPC",
                    }
                ],
            }
        ],
        "skills": [
            {
                "id": "cost-triage",
                "name": "Cost Triage",
                "description": "Analyze billing spikes.",
                "tags": ["finops", "billing"],
            }
        ],
    }

    bindings_payload = [
        {
            "name": f"{PARENT}/bindings/wildcard-finops-mcp",
            "target": {"identifier": MCP_URN},
            "authProviderBinding": {
                "authProvider": f"{PARENT}/authProviders/finops-shared-oauth",
                "scopes": ["https://www.googleapis.com/auth/cloud-billing.readonly"],
            },
            "updateTime": "2026-04-02T10:00:00Z",
        },
        {
            "name": f"{PARENT}/bindings/agent-older-finops-mcp",
            "source": {"identifier": AGENT_URN},
            "target": {"identifier": MCP_URN},
            "authProviderBinding": {
                "authProvider": f"{PARENT}/authProviders/finops-agent-oauth-v1",
                "scopes": ["https://www.googleapis.com/auth/cloud-billing.readonly"],
            },
            "updateTime": "2026-04-01T08:00:00.100Z",
        },
        {
            "name": f"{PARENT}/bindings/agent-latest-finops-mcp",
            "source": {"identifier": AGENT_URN},
            "target": {"identifier": MCP_URN},
            "authProviderBinding": {
                "authProvider": f"{PARENT}/authProviders/finops-agent-oauth-v2",
                "scopes": [
                    "https://www.googleapis.com/auth/cloud-billing.readonly",
                    "https://www.googleapis.com/auth/bigquery.readonly",
                ],
                "continueUri": "https://platform-agent.acme.example.com/oauth/callback",
            },
            "updateTime": "2026-04-01T08:00:00.900000Z",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body_json = None
        if request.content:
            try:
                body_json = json.loads(request.content.decode("utf-8"))
            except Exception:
                body_json = None

        if recorded_requests is not None:
            recorded_requests.append(
                {
                    "method": request.method,
                    "url": str(request.url),
                    "host": request.url.host,
                    "port": request.url.port,
                    "path": request.url.path,
                    "query": dict(request.url.params),
                    "headers": {k.lower(): v for k, v in request.headers.items()},
                    "json": body_json,
                }
            )

        # Signed GCS download hop (cross-origin)
        if str(request.url) == redirect_url:
            # Emulate strict GCS signed-URL validation: reject any x-goog-* or Authorization header
            leaked_headers = [
                h
                for h in request.headers.keys()
                if h.lower().startswith("x-goog-") or h.lower() == "authorization"
            ]
            if leaked_headers:
                return httpx.Response(
                    403,
                    text=f"SignatureDoesNotMatch: unexpected headers {leaked_headers}",
                )
            return httpx.Response(
                200,
                content=zip_bytes,
                headers={"Content-Type": "application/zip"},
            )

        path = request.url.path
        if request.method == "POST" and path == f"/v1/{PARENT}/skills:search":
            page_token = (body_json or {}).get("pageToken")
            if page_token == "page-2":
                return httpx.Response(200, json={"skills": []})
            return httpx.Response(200, json={"skills": [skill_payload]})

        if request.method == "GET" and path == f"/v1/{SKILL_RESOURCE}":
            return httpx.Response(200, json=skill_payload)

        if request.method == "GET" and path == f"/v1/{REVISION_RESOURCE}":
            if request.url.params.get("alt") == "media":
                return httpx.Response(302, headers={"Location": redirect_url})
            return httpx.Response(200, json=revision_payload)

        if request.method == "POST" and path == f"/v1/{PARENT}/mcpServers:search":
            return httpx.Response(200, json={"mcpServers": [mcp_server_payload]})

        if request.method == "GET" and path == f"/v1/{PARENT}/mcpServers":
            return httpx.Response(200, json={"mcpServers": [mcp_server_payload]})

        if request.method == "POST" and path == f"/v1/{PARENT}/agents:search":
            return httpx.Response(200, json={"agents": [agent_payload]})

        if request.method == "GET" and path == f"/v1/{PARENT}/agents":
            return httpx.Response(200, json={"agents": [agent_payload]})

        if request.method == "GET" and path == f"/v1/{PARENT}/bindings:fetchAvailable":
            return httpx.Response(200, json={"bindings": bindings_payload})

        return httpx.Response(404, json={"error": {"code": 404, "message": f"Unhandled {request.method} {path}"}})

    return httpx.MockTransport(handler)


def test_skills_search_sends_semantic_query_and_zero_finding_filter() -> None:
    recorded: list[dict[str, Any]] = []
    zip_bytes = _build_skill_zip(skill_md=_make_skill_md())
    transport = _build_mock_registry_transport(zip_bytes=zip_bytes, recorded_requests=recorded)

    async def _run() -> list[dict[str, Any]]:
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            access_token="fake-oauth-token",
            transport=transport,
        ) as consumer:
            return await consumer.search_skills("cost anomaly billing spike")

    results = asyncio.run(_run())
    assert len(results) == 1
    assert results[0]["name"] == SKILL_RESOURCE
    assert results[0]["skillId"] == SKILL_URN

    req = recorded[0]
    assert req["method"] == "POST"
    assert req["path"] == f"/v1/{PARENT}/skills:search"
    assert req["json"] == {
        "searchType": "SEMANTIC",
        "searchString": "cost anomaly billing spike",
        "filter": APPROVED_SKILL_FILTER,
    }
    assert req["headers"]["authorization"] == "Bearer fake-oauth-token"
    assert req["headers"]["x-goog-user-project"] == PROJECT_ID
    assert "x-goog-api-client" in req["headers"]


def test_skill_alt_media_302_redirect_strips_x_goog_and_authorization_headers() -> None:
    recorded: list[dict[str, Any]] = []
    zip_bytes = _build_skill_zip(
        skill_md=_make_skill_md(),
        extra_files={
            "references/bigquery-slots.md": "# Slot Guide\nUse reservation autoscaling.\n",
            "scripts/check_cost.py": "print('ok')\n",
        },
    )
    transport = _build_mock_registry_transport(zip_bytes=zip_bytes, recorded_requests=recorded)

    async def _run():
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            access_token="secret-gcp-token",
            transport=transport,
        ) as consumer:
            return await consumer.fetch_and_extract_skill(
                consumer.private_skill_name("cost-anomaly-triage")
            )

    extracted = asyncio.run(_run())
    assert extracted.name == "cost-anomaly-triage"
    assert extracted.resource_name == SKILL_RESOURCE
    assert extracted.revision_name == REVISION_RESOURCE
    assert extracted.sha256_hash == hashlib.sha256(zip_bytes).hexdigest()
    assert "Query billing anomalies" in extracted.instructions
    assert "bigquery-slots.md" in extracted.references
    assert "check_cost.py" in extracted.scripts

    # Verify all 4 HTTP hops: GetSkill -> GetSkillRevision -> ?alt=media (302) -> GCS signed URL
    assert len(recorded) == 4
    api_media_hop = recorded[2]
    gcs_signed_hop = recorded[3]

    assert api_media_hop["host"] == "agentregistry.googleapis.com"
    assert api_media_hop["query"] == {"alt": "media"}
    assert api_media_hop["headers"]["x-goog-user-project"] == PROJECT_ID
    assert "x-goog-api-client" in api_media_hop["headers"]
    assert api_media_hop["headers"]["authorization"] == "Bearer secret-gcp-token"

    assert gcs_signed_hop["host"] == "storage.googleapis.com"
    assert not any(k.startswith("x-goog-") for k in gcs_signed_hop["headers"])
    assert "authorization" not in gcs_signed_hop["headers"]


def test_skill_alt_media_localhost_cross_port_redirect_strips_x_goog_headers() -> None:
    recorded: list[dict[str, Any]] = []
    zip_bytes = _build_skill_zip(skill_md=_make_skill_md(), prefix="")
    local_gcs_url = "http://127.0.0.1:8099/signed-bucket/rev-1.zip?sig=local"
    transport = _build_mock_registry_transport(
        zip_bytes=zip_bytes,
        redirect_url=local_gcs_url,
        recorded_requests=recorded,
    )

    async def _run():
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            base_url="http://127.0.0.1:8090/v1",
            access_token="local-workshop-token",
            transport=transport,
        ) as consumer:
            return await consumer.fetch_and_extract_skill(SKILL_RESOURCE)

    extracted = asyncio.run(_run())
    assert extracted.skill_md_path == "SKILL.md"

    gcs_hop = recorded[-1]
    assert gcs_hop["host"] == "127.0.0.1"
    assert gcs_hop["port"] == 8099
    assert not any(k.startswith("x-goog-") for k in gcs_hop["headers"])
    assert "authorization" not in gcs_hop["headers"]


def test_skill_download_rejects_sha256_mismatch_and_governance_blocks() -> None:
    zip_bytes = _build_skill_zip(skill_md=_make_skill_md())

    # 1. Tampered ZIP vs server-computed sha256Hash
    bad_hash_transport = _build_mock_registry_transport(
        zip_bytes=zip_bytes,
        reported_sha256="0" * 64,
    )

    async def _fetch(transport: httpx.MockTransport):
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            transport=transport,
        ) as consumer:
            return await consumer.fetch_and_extract_skill(SKILL_RESOURCE)

    with pytest.raises(SkillIntegrityError, match="SHA-256 mismatch"):
        asyncio.run(_fetch(bad_hash_transport))

    # 2. Quarantined / DRAFT skill blocked
    draft_transport = _build_mock_registry_transport(
        zip_bytes=zip_bytes,
        skill_state="STATE_DRAFT",
    )
    with pytest.raises(SkillGovernanceError, match="non-servable state"):
        asyncio.run(_fetch(draft_transport))

    # 3. Revision with security scan findings blocked
    finding_transport = _build_mock_registry_transport(
        zip_bytes=zip_bytes,
        scan_findings=[f"{PARENT}/findings/finding-secret-001"],
    )
    with pytest.raises(SkillGovernanceError, match="security finding"):
        asyncio.run(_fetch(finding_transport))


def test_extract_skill_zip_enforces_security_guardrails() -> None:
    # Path traversal rejected
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", _make_skill_md())
        zf.writestr("../escape.txt", "evil")
    with pytest.raises(SkillIntegrityError, match="Path traversal"):
        extract_skill_zip(buf.getvalue())

    # Symlink rejected
    buf_sym = io.BytesIO()
    with zipfile.ZipFile(buf_sym, "w") as zf:
        zf.writestr("SKILL.md", _make_skill_md())
        info = zipfile.ZipInfo("references/link.md")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(SkillIntegrityError, match="Symlink"):
        extract_skill_zip(buf_sym.getvalue())

    # Multiple SKILL.md rejected
    buf_multi = io.BytesIO()
    with zipfile.ZipFile(buf_multi, "w") as zf:
        zf.writestr("a/SKILL.md", _make_skill_md())
        zf.writestr("b/SKILL.md", _make_skill_md())
    with pytest.raises(SkillIntegrityError, match="strictly ONE uppercase SKILL.md"):
        extract_skill_zip(buf_multi.getvalue())


def test_kms_ecdsa_p256_signature_verification_sidecar_and_in_frontmatter() -> None:
    private_key, public_pem = _generate_ec_p256_keypair()
    _, wrong_public_pem = _generate_ec_p256_keypair()
    kms_key_ver = f"projects/{PROJECT_ID}/locations/{LOCATION}/keyRings/skills/cryptoKeys/skill-signer/cryptoKeyVersions/1"

    # Mode A: Cloud KMS AsymmetricSign(digest={"sha256": raw_zip_sha256}) via sidecar/revision metadata
    unsigned_zip = _build_skill_zip(skill_md=_make_skill_md())
    raw_digest = hashlib.sha256(unsigned_zip).digest()
    kms_sig_bytes = private_key.sign(raw_digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    kms_sig_b64 = base64.b64encode(kms_sig_bytes).decode("ascii")

    transport_sidecar = _build_mock_registry_transport(
        zip_bytes=unsigned_zip,
        revision_metadata={
            "plugin_name": SAMPLE_PLUGIN["name"],
            "kms_signature": kms_sig_b64,
            "kms_key_version": kms_key_ver,
        },
    )

    async def _verify(transport: httpx.MockTransport, pem: bytes):
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            kms_public_key_pem=pem,
            require_kms_signature=True,
            transport=transport,
        ) as consumer:
            return await consumer.fetch_and_extract_skill(SKILL_RESOURCE)

    verified = asyncio.run(_verify(transport_sidecar, public_pem))
    assert verified.kms_verified is True
    assert verified.kms_key_version == kms_key_ver

    # Wrong key fails closed
    with pytest.raises(SkillSignatureError, match="signature verification failed"):
        asyncio.run(_verify(transport_sidecar, wrong_public_pem))

    # Mode B: In-frontmatter kms_signature over canonical unsigned skill content digest
    base_meta = {"plugin_name": SAMPLE_PLUGIN["name"], "plugin_version": "1.0.0"}
    body = "# Cost Anomaly Triage\n\nFollow the verified FinOps playbook.\n"
    fm_for_hash = {
        "name": "cost-anomaly-triage",
        "description": "Triage GCP billing and BigQuery cost spikes using approved FinOps runbooks.",
        "license": "Apache-2.0",
        "compatibility": "Requires finops-mcp-server",
        "metadata": base_meta,
    }
    canonical_digest = compute_canonical_skill_digest(fm_for_hash, body)
    fm_sig_b64 = base64.b64encode(
        private_key.sign(canonical_digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    ).decode("ascii")

    signed_zip = _build_skill_zip(
        skill_md=_make_skill_md(
            metadata={
                **base_meta,
                "kms_signature": fm_sig_b64,
                "kms_key_version": kms_key_ver,
            },
            body=body,
        )
    )
    transport_fm = _build_mock_registry_transport(zip_bytes=signed_zip)
    verified_fm = asyncio.run(_verify(transport_fm, public_pem))
    assert verified_fm.kms_verified is True
    assert verified_fm.kms_key_version == kms_key_ver

    # Missing signature when required fails closed
    transport_unsigned = _build_mock_registry_transport(zip_bytes=unsigned_zip)
    with pytest.raises(SkillSignatureError, match="missing required kms_signature"):
        asyncio.run(_verify(transport_unsigned, public_pem))


def test_agents_and_mcp_servers_search_use_v1_keyword_contract() -> None:
    recorded: list[dict[str, Any]] = []
    zip_bytes = _build_skill_zip(skill_md=_make_skill_md())
    transport = _build_mock_registry_transport(zip_bytes=zip_bytes, recorded_requests=recorded)

    async def _run():
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            transport=transport,
        ) as consumer:
            agents = await consumer.search_agents("finops")
            servers = await consumer.search_mcp_servers("finops")
            return agents, servers

    agents, servers = asyncio.run(_run())
    assert len(agents) == 1
    assert agents[0]["agentId"] == AGENT_URN
    assert len(servers) == 1
    assert servers[0]["mcpServerId"] == MCP_URN

    agent_req, mcp_req = recorded[0], recorded[1]
    assert agent_req["path"] == f"/v1/{PARENT}/agents:search"
    assert agent_req["json"] == {"searchString": "finops"}
    assert "searchType" not in agent_req["json"]

    assert mcp_req["path"] == f"/v1/{PARENT}/mcpServers:search"
    assert mcp_req["json"] == {"searchString": "finops"}
    assert "searchType" not in mcp_req["json"]


def test_bindings_fetch_available_and_resolution_prefers_source_and_latest_update() -> None:
    recorded: list[dict[str, Any]] = []
    zip_bytes = _build_skill_zip(skill_md=_make_skill_md())
    transport = _build_mock_registry_transport(zip_bytes=zip_bytes, recorded_requests=recorded)

    async def _run():
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            transport=transport,
        ) as consumer:
            all_bindings = await consumer.fetch_available_bindings(
                source_identifier=AGENT_URN,
                target_identifier=MCP_URN,
            )
            chosen = await consumer.resolve_auth_binding(
                source_identifier=AGENT_URN,
                target_identifier=MCP_URN,
            )
            return all_bindings, chosen

    all_bindings, chosen = asyncio.run(_run())
    assert len(all_bindings) == 3
    assert recorded[0]["path"] == f"/v1/{PARENT}/bindings:fetchAvailable"
    assert recorded[0]["query"] == {
        "sourceIdentifier": AGENT_URN,
        "targetIdentifier": MCP_URN,
    }
    assert chosen is not None
    # Must prefer AGENT_URN-scoped binding over wildcard (even though wildcard is newer),
    # and among AGENT_URN-scoped bindings pick the latest updateTime (oauth-v2).
    assert chosen["authProvider"] == f"{PARENT}/authProviders/finops-agent-oauth-v2"
    assert chosen["continueUri"] == "https://platform-agent.acme.example.com/oauth/callback"


def test_private_skill_auto_prefix_helper() -> None:
    consumer = PlatformAgentConsumer(project_id=PROJECT_ID, location=LOCATION)
    try:
        assert consumer.private_skill_name("cost-anomaly-triage") == SKILL_RESOURCE
        assert consumer.private_skill_name("private-cost-anomaly-triage") == SKILL_RESOURCE
        assert consumer.private_skill_name(SKILL_RESOURCE) == SKILL_RESOURCE
        with pytest.raises(ValueError, match="Invalid skill resource ID"):
            consumer.normalize_skill_name("../bad/path")
    finally:
        asyncio.run(consumer.aclose())


def test_build_adk_agent_config_matches_verified_specs() -> None:
    cfg = build_adk_agent_config(
        project_id=PROJECT_ID,
        location=LOCATION,
        mode="adk",
        mcp_search_string="finops",
        agent_search_string="finops",
        require_kms_signature=True,
    )

    assert cfg["mode"] == "adk"
    assert cfg["adk_min_version"] == "1.29.0"
    assert cfg["registry"]["api_service"] == WORKSHOP_SPEC["agent_registry"]["api_service"]
    assert cfg["registry"]["api_version"] == "v1"
    assert cfg["registry"]["base_url"] == WORKSHOP_SPEC["agent_registry"]["rest_base"]
    assert cfg["registry"]["parent"] == PARENT

    assert cfg["auth_manager"]["registration_call"] == (
        "CredentialManager.register_auth_provider(GcpAuthProvider())"
    )
    assert cfg["auth_manager"]["auto_resolve_bindings"] is True
    assert "roles/agentidentity.user" in cfg["auth_manager"]["required_iam_roles"]
    assert "roles/agentregistry.viewer" in cfg["auth_manager"]["required_iam_roles"]

    ar_cfg = cfg["integrations"]["agent_registry"]
    assert ar_cfg["import_path"] == "google.adk.integrations.agent_registry.AgentRegistry"
    for expected_method in (
        "list_agents",
        "search_agents",
        "get_remote_a2a_agent",
        "get_mcp_toolset",
    ):
        assert expected_method in ar_cfg["methods"]

    sr_cfg = cfg["integrations"]["skill_registry"]
    assert sr_cfg["import_path"] == "google.adk.integrations.skill_registry.GCPSkillRegistry"
    assert sr_cfg["toolset_import_path"] == "google.adk.tools.skill_toolset.SkillToolset"
    assert sr_cfg["search_type"] == "SEMANTIC"
    assert sr_cfg["filter"] == APPROVED_SKILL_FILTER
    assert "x-goog-*" in sr_cfg["media_download_redirect_strips_headers"]
    assert sr_cfg["require_kms_signature"] is True

    gov = cfg["governance"]
    assert gov["recommended_runtime_role"] == "roles/agentregistry.viewer"
    assert "roles/agentregistry.editor" in gov["disallowed_runtime_roles"]
    assert "roles/agentregistry.admin" in gov["disallowed_runtime_roles"]
    assert gov["enforce_zero_findings"] is True


def test_build_live_adk_agent_wiring_with_fake_adk_classes() -> None:
    events: list[tuple[str, Any]] = []

    class FakeGcpAuthProvider:
        pass

    class FakeCredentialManager:
        @classmethod
        def register_auth_provider(cls, provider: Any) -> None:
            events.append(("register_auth_provider", type(provider).__name__))

    class FakeAgentRegistry:
        def __init__(self, project_id: str, location: str) -> None:
            events.append(("AgentRegistry.__init__", (project_id, location)))

        def search_mcp_servers(self, *, search_string: str) -> dict[str, Any]:
            events.append(("search_mcp_servers", search_string))
            return {"mcpServers": [{"name": f"{PARENT}/mcpServers/finops-mcp-server"}]}

        def get_mcp_toolset(self, mcp_server_name: str, *, continue_uri: str | None = None) -> dict[str, Any]:
            events.append(("get_mcp_toolset", (mcp_server_name, continue_uri)))
            return {"kind": "mcp_toolset", "name": mcp_server_name}

        def search_agents(self, *, search_string: str) -> dict[str, Any]:
            events.append(("search_agents", search_string))
            return {"agents": [{"name": f"{PARENT}/agents/finops-advisor-agent"}]}

        def get_remote_a2a_agent(self, agent_name: str, *, continue_uri: str | None = None) -> dict[str, Any]:
            events.append(("get_remote_a2a_agent", (agent_name, continue_uri)))
            return {"kind": "remote_a2a_agent", "name": agent_name}

    class FakeGCPSkillRegistry:
        def __init__(self, *, project_id: str, location: str) -> None:
            events.append(("GCPSkillRegistry.__init__", (project_id, location)))

    class FakeSkillToolset:
        def __init__(self, *, registry: Any) -> None:
            events.append(("SkillToolset.__init__", type(registry).__name__))
            self.registry = registry

    class FakeLlmAgent:
        def __init__(
            self,
            *,
            name: str,
            model: str,
            instruction: str,
            tools: list[Any],
            sub_agents: list[Any],
        ) -> None:
            self.name = name
            self.model = model
            self.instruction = instruction
            self.tools = tools
            self.sub_agents = sub_agents

    config = OrchestratorConfig(
        project_id=PROJECT_ID,
        location=LOCATION,
        mode="adk",
        mcp_search_string="finops",
        agent_search_string="finops",
        continue_uri="https://platform-agent.acme.example.com/oauth/callback",
    )
    agent = build_live_adk_agent(
        config,
        registry_cls=FakeAgentRegistry,
        skill_registry_cls=FakeGCPSkillRegistry,
        skill_toolset_cls=FakeSkillToolset,
        llm_agent_cls=FakeLlmAgent,
        credential_manager_cls=FakeCredentialManager,
        gcp_auth_provider_cls=FakeGcpAuthProvider,
    )

    assert agent.name == config.agent_name
    assert len(agent.tools) == 2
    assert isinstance(agent.tools[0], FakeSkillToolset)
    assert agent.tools[1]["name"] == f"{PARENT}/mcpServers/finops-mcp-server"
    assert len(agent.sub_agents) == 1
    assert agent.sub_agents[0]["name"] == f"{PARENT}/agents/finops-advisor-agent"

    # Auth provider MUST be registered first before building AgentRegistry toolsets
    assert events[0] == ("register_auth_provider", "FakeGcpAuthProvider")
    assert ("AgentRegistry.__init__", (PROJECT_ID, LOCATION)) in events
    assert ("GCPSkillRegistry.__init__", (PROJECT_ID, LOCATION)) in events
    assert ("SkillToolset.__init__", "FakeGCPSkillRegistry") in events


def test_orchestrator_rest_mode_end_to_end_and_cli_print_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    zip_bytes = _build_skill_zip(skill_md=_make_skill_md())
    transport = _build_mock_registry_transport(zip_bytes=zip_bytes)

    orchestrator = PlatformAgentOrchestrator(
        OrchestratorConfig(
            project_id=PROJECT_ID,
            location=LOCATION,
            mode="rest",
            registry_base_url="https://agentregistry.googleapis.com/v1",
            skill_search_string="cost anomaly",
            mcp_search_string="finops",
            agent_search_string="finops",
            source_agent_urn=AGENT_URN,
        ),
        transport=transport,
    )
    capabilities = asyncio.run(orchestrator.run(fetch_skill_contents=True))
    snapshot = capabilities.to_dict()

    assert len(snapshot["skills"]) == 1
    assert len(snapshot["extracted_skills"]) == 1
    assert snapshot["extracted_skills"][0]["name"] == "cost-anomaly-triage"
    assert len(snapshot["mcp_servers"]) == 1
    assert len(snapshot["agents"]) == 1
    assert MCP_URN in snapshot["resolved_bindings"]
    assert snapshot["resolved_bindings"][MCP_URN]["authProvider"].endswith(
        "/authProviders/finops-agent-oauth-v2"
    )

    # CLI --print-config works cleanly without google-adk installed
    exit_code = orchestrator_main(["--mode", "adk", "--print-config"])
    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["registry"]["api_service"] == "agentregistry.googleapis.com"
    assert printed["mode"] == "adk"


def test_skill_alt_media_same_origin_scotty_signed_redirect_strips_headers_and_bounds_hops() -> None:
    recorded: list[dict[str, Any]] = []
    zip_bytes = _build_skill_zip(skill_md=_make_skill_md(), prefix="")
    same_origin_signed = "http://127.0.0.1:8090/scotty-signed/tok-same-origin-123"
    transport = _build_mock_registry_transport(
        zip_bytes=zip_bytes,
        redirect_url=same_origin_signed,
        recorded_requests=recorded,
    )

    async def _run():
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            base_url="http://127.0.0.1:8090/v1",
            access_token="secret-bearer-token",
            transport=transport,
        ) as consumer:
            return await consumer.fetch_and_extract_skill(SKILL_RESOURCE)

    extracted = asyncio.run(_run())
    assert extracted.name == "cost-anomaly-triage"
    assert extracted.sha256_hash == hashlib.sha256(zip_bytes).hexdigest()

    # Initial ?alt=media hop carries API headers; same-origin /scotty-signed hop strips ALL of them.
    api_hop = recorded[2]
    signed_hop = recorded[3]
    assert api_hop["headers"]["authorization"] == "Bearer secret-bearer-token"
    assert api_hop["headers"]["x-goog-user-project"] == PROJECT_ID
    assert signed_hop["url"] == same_origin_signed
    assert not any(k.startswith("x-goog-") for k in signed_hop["headers"])
    assert "authorization" not in signed_hop["headers"]

    # Infinite redirect loop stops cleanly at max_redirects
    loop_transport = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"Location": "/scotty-signed/loop"})
    )

    async def _loop():
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            base_url="http://127.0.0.1:8090/v1",
            transport=loop_transport,
        ) as consumer:
            return await consumer.download_skill_archive(REVISION_RESOURCE, max_redirects=3)

    with pytest.raises(httpx.TooManyRedirects):
        asyncio.run(_loop())


def test_live_asgi_integration_against_workshop_mock_registry() -> None:
    """End-to-end ASGI test against mock_registry.app.create_app() including /scotty-signed 302."""
    mock_registry_app_mod = importlib.import_module("mock_registry.app")
    app = mock_registry_app_mod.create_app()
    zip_bytes = _build_skill_zip(
        skill_md=_make_skill_md(
            metadata={"plugin_name": SAMPLE_PLUGIN["name"], "plugin_version": "1.0.0"}
        ),
        extra_files={"references/runbook.md": "# Runbook\nVerify BigQuery bytes billed.\n"},
    )
    zip_b64 = base64.b64encode(zip_bytes).decode("ascii")

    async def _run():
        transport = httpx.ASGITransport(app=app)
        base_url = "http://127.0.0.1:8090/v1"

        # Seed mock_registry via its real v1 REST endpoints
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as admin:
            mcp_resp = await admin.post(
                f"/{PARENT}/services",
                params={"serviceId": SAMPLE_PLUGIN["mcp_service_id"]},
                json={
                    "displayName": "FinOps MCP Server",
                    "description": "FinOps billing tools",
                    "interfaces": [
                        {"url": "http://127.0.0.1:8091/mcp", "protocolBinding": "HTTP_JSON"}
                    ],
                    "mcpServerSpec": {
                        "type": "TOOL_SPEC",
                        "content": json.dumps(
                            {
                                "tools": [
                                    {
                                        "name": "list_billing_anomalies",
                                        "description": "List billing spikes",
                                        "annotations": {"readOnlyHint": True},
                                    }
                                ]
                            }
                        ),
                    },
                },
            )
            assert mcp_resp.status_code == 200, mcp_resp.text
            mcp_urn = mcp_resp.json()["response"]["registryResource"]

            agent_resp = await admin.post(
                f"/{PARENT}/services",
                params={"serviceId": SAMPLE_PLUGIN["agent_service_id"]},
                json={
                    "displayName": "FinOps Advisor Agent",
                    "description": "A2A FinOps advisor",
                    "agentSpec": {
                        "type": "A2A_AGENT_CARD",
                        "content": json.dumps(
                            {
                                "name": "FinOps Advisor Agent",
                                "description": "A2A FinOps advisor",
                                "version": "1.0.0",
                                "supportedInterfaces": [
                                    {
                                        "url": "http://127.0.0.1:8092/a2a",
                                        "protocolBinding": "JSONRPC",
                                    }
                                ],
                                "capabilities": {},
                                "defaultInputModes": ["text"],
                                "defaultOutputModes": ["text"],
                                "skills": [
                                    {
                                        "id": "cost-triage",
                                        "name": "Cost Triage",
                                        "description": "Analyze cost anomalies",
                                    }
                                ],
                            }
                        ),
                    },
                },
            )
            assert agent_resp.status_code == 200, agent_resp.text
            agent_urn = agent_resp.json()["response"]["registryResource"]

            # Resolve projected agentId & mcpServerId URNs from read-only endpoints
            mcp_details = (await admin.get(f"/{mcp_urn}")).json()
            agent_details = (await admin.get(f"/{agent_urn}")).json()
            mcp_id = mcp_details["mcpServerId"]
            agent_id = agent_details["agentId"]

            binding_resp = await admin.post(
                f"/{PARENT}/bindings",
                params={"bindingId": "finops-agent-to-mcp"},
                json={
                    "displayName": "FinOps Agent to MCP",
                    "source": {"identifier": agent_id},
                    "target": {"identifier": mcp_id},
                    "authProviderBinding": {
                        "authProvider": f"{PARENT}/authProviders/finops-oauth",
                        "scopes": ["https://www.googleapis.com/auth/cloud-billing.readonly"],
                    },
                },
            )
            assert binding_resp.status_code == 200, binding_resp.text

            skill_resp = await admin.post(
                f"/{PARENT}/skills",
                params={"skillId": "cost-anomaly-triage"},
                json={
                    "displayName": "Cost Anomaly Triage",
                    "description": "Triage GCP billing cost spikes.",
                    "type": "SIMPLE",
                    "targetState": "TARGET_STATE_ACTIVE",
                    "scanningConfig": {"policy": "ENABLED_BLOCKING"},
                    "initialRevision": {
                        "archiveUploadSource": {"archiveContent": zip_b64}
                    },
                },
            )
            assert skill_resp.status_code == 200, skill_resp.text

        # Now exercise PlatformAgentConsumer with active Bearer + x-goog-* headers against mock_registry!
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            base_url=base_url,
            access_token="live-mock-bearer-token",
            transport=httpx.ASGITransport(app=app),
        ) as consumer:
            capabilities = await consumer.discover_capabilities(
                skill_query="cost anomaly",
                mcp_query="finops",
                agent_query="finops",
                source_agent_urn=agent_id,
                fetch_skill_contents=True,
            )
            return capabilities, mcp_id

    capabilities, mcp_id = asyncio.run(_run())
    assert len(capabilities.skills) == 1
    assert len(capabilities.extracted_skills) == 1
    extracted = capabilities.extracted_skills[0]
    assert extracted.name == "cost-anomaly-triage"
    assert extracted.sha256_hash == hashlib.sha256(zip_bytes).hexdigest()
    assert "runbook.md" in extracted.references
    assert len(capabilities.mcp_servers) == 1
    assert len(capabilities.agents) == 1
    assert capabilities.resolved_bindings[mcp_id]["authProvider"] == (
        f"{PARENT}/authProviders/finops-oauth"
    )


def test_build_live_adk_agent_compat_1_29_vs_2_8_and_env_restoration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify google-adk 1.29.0-2.7.x signatures (no continue_uri) and 2.8.0+ signatures."""
    monkeypatch.setenv("AGENT_REGISTRY_ENDPOINT", "https://original.example.com/v1")
    observed_endpoints: list[str | None] = []

    class FakeGcpAuthProvider:
        pass

    class FakeCredentialManager:
        @classmethod
        def register_auth_provider(cls, provider: Any) -> None:
            pass

    class Legacy129AgentRegistry:
        """Matches google-adk 1.29.0-2.7.x signatures (no continue_uri parameter)."""

        def __init__(self, project_id: str, location: str) -> None:
            self.project_id = project_id
            self.location = location

        def list_mcp_servers(self) -> dict[str, Any]:
            return {"mcpServers": [{"name": f"{PARENT}/mcpServers/finops-mcp-server"}]}

        def get_mcp_toolset(self, mcp_server_name: str) -> dict[str, Any]:
            return {"kind": "legacy_mcp", "name": mcp_server_name}

        def list_agents(self) -> dict[str, Any]:
            return {"agents": [{"name": f"{PARENT}/agents/finops-advisor-agent"}]}

        def get_remote_a2a_agent(
            self, agent_name: str, *, httpx_client: httpx.AsyncClient | None = None
        ) -> dict[str, Any]:
            return {"kind": "legacy_a2a", "name": agent_name, "httpx_client": httpx_client}

    class FakeGCPSkillRegistry:
        def __init__(self, *, project_id: str, location: str) -> None:
            observed_endpoints.append(os.environ.get("AGENT_REGISTRY_ENDPOINT"))

    class FakeSkillToolset:
        def __init__(self, *, registry: Any) -> None:
            self.registry = registry

    class FakeLlmAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    # 1. With continue_uri=None, google-adk 1.29.0-2.7.x succeeds without TypeError
    cfg_no_continue = OrchestratorConfig(
        project_id=PROJECT_ID,
        location=LOCATION,
        mode="adk",
        registry_base_url="https://agentregistry.googleapis.com/v1",
        continue_uri=None,
    )
    agent = build_live_adk_agent(
        cfg_no_continue,
        registry_cls=Legacy129AgentRegistry,
        skill_registry_cls=FakeGCPSkillRegistry,
        skill_toolset_cls=FakeSkillToolset,
        llm_agent_cls=FakeLlmAgent,
        credential_manager_cls=FakeCredentialManager,
        gcp_auth_provider_cls=FakeGcpAuthProvider,
    )
    assert agent.kwargs["tools"][1]["kind"] == "legacy_mcp"
    assert agent.kwargs["sub_agents"][0]["kind"] == "legacy_a2a"
    # Endpoint was temporarily set during GCPSkillRegistry init and cleanly restored
    assert observed_endpoints == ["https://agentregistry.googleapis.com/v1"]
    assert os.environ.get("AGENT_REGISTRY_ENDPOINT") == "https://original.example.com/v1"

    # 2. With explicit continue_uri on legacy 1.29.0-2.7.x, raise a clear RuntimeError
    cfg_with_continue = OrchestratorConfig(
        project_id=PROJECT_ID,
        location=LOCATION,
        mode="adk",
        continue_uri="https://platform-agent.acme.example.com/oauth/callback",
    )
    with pytest.raises(RuntimeError, match=r"google-adk>=2\.8\.0"):
        build_live_adk_agent(
            cfg_with_continue,
            registry_cls=Legacy129AgentRegistry,
            skill_registry_cls=FakeGCPSkillRegistry,
            skill_toolset_cls=FakeSkillToolset,
            llm_agent_cls=FakeLlmAgent,
            credential_manager_cls=FakeCredentialManager,
            gcp_auth_provider_cls=FakeGcpAuthProvider,
        )


def test_from_env_and_cli_main_fail_closed_on_require_kms_signature(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify REQUIRE_KMS_SIGNATURE=true without PEM fails closed in from_env() and CLI main()."""
    monkeypatch.setenv("GCP_PROJECT_ID", PROJECT_ID)
    monkeypatch.setenv("GCP_LOCATION", LOCATION)
    monkeypatch.setenv("AGENT_REGISTRY_BASE_URL", "http://127.0.0.1:8090/v1")
    monkeypatch.setenv("REQUIRE_KMS_SIGNATURE", "true")
    monkeypatch.delenv("KMS_PUBLIC_KEY_PEM_PATH", raising=False)
    monkeypatch.setenv("PLATFORM_AGENT_MODEL", "gemini-2.5-pro")
    monkeypatch.setenv("OAUTH_CONTINUE_URI", "https://agent.acme.example.com/callback")
    monkeypatch.setenv("AGENT_REGISTRY_ACCESS_TOKEN", "env-token")

    # 1. PlatformAgentConsumer.from_env() must NOT silently disable KMS enforcement
    zip_bytes = _build_skill_zip(skill_md=_make_skill_md())
    transport = _build_mock_registry_transport(zip_bytes=zip_bytes)

    async def _attempt_fetch():
        async with PlatformAgentConsumer.from_env(transport=transport) as consumer:
            assert consumer.require_kms_signature is True
            assert consumer.kms_public_key_pem is None
            return await consumer.fetch_and_extract_skill(SKILL_RESOURCE)

    with pytest.raises(SkillSignatureError, match="no kms_public_key_pem was provided"):
        asyncio.run(_attempt_fetch())

    # 2. OrchestratorConfig.from_env() preserves require_kms_signature=True and env vars
    env_cfg = OrchestratorConfig.from_env()
    assert env_cfg.require_kms_signature is True
    assert env_cfg.kms_public_key_pem_path is None
    assert env_cfg.model == "gemini-2.5-pro"
    assert env_cfg.continue_uri == "https://agent.acme.example.com/callback"
    assert env_cfg.access_token == "env-token"

    # 3. CLI main() starts from from_env() so security/model/oauth env vars are preserved,
    #    while explicit CLI flags still override targeted fields.
    exit_code = orchestrator_main(["--mode", "adk", "--location", "europe-west1", "--print-config"])
    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "adk"
    assert printed["registry"]["location"] == "europe-west1"
    assert printed["registry"]["base_url"] == "http://127.0.0.1:8090/v1"
    assert printed["agent"]["model"] == "gemini-2.5-pro"
    assert printed["auth_manager"]["continue_uri"] == "https://agent.acme.example.com/callback"
    assert printed["integrations"]["skill_registry"]["require_kms_signature"] is True


def test_enabled_blocking_scan_state_gate_and_binding_and_zip_bomb_hardening() -> None:
    zip_bytes = _build_skill_zip(skill_md=_make_skill_md())

    async def _fetch_with_scan(scan_state: str, policy: str = "ENABLED_BLOCKING"):
        transport = _build_mock_registry_transport(
            zip_bytes=zip_bytes,
            scan_state=scan_state,
            scanning_policy=policy,
        )
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            transport=transport,
        ) as consumer:
            return await consumer.fetch_and_extract_skill(SKILL_RESOURCE)

    # ENABLED_BLOCKING blocks RUNNING and FAILED scans even when findings=[]
    with pytest.raises(SkillGovernanceError, match="scanState is 'RUNNING'"):
        asyncio.run(_fetch_with_scan("RUNNING"))
    with pytest.raises(SkillGovernanceError, match="scanState is 'FAILED'"):
        asyncio.run(_fetch_with_scan("FAILED"))

    # ENABLED_NONBLOCKING permits RUNNING scan when findings=[]
    ok_nonblocking = asyncio.run(_fetch_with_scan("RUNNING", policy="ENABLED_NONBLOCKING"))
    assert ok_nonblocking.name == "cost-anomaly-triage"

    # Binding resolution handles explicit JSON nulls & never picks another agent's binding
    other_agent_only_transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={
                "bindings": [
                    {"name": "b-nulls", "source": None, "target": None, "authProviderBinding": None},
                    {
                        "name": "b-other-agent",
                        "source": {"identifier": "urn:agent:projects-123:other-agent"},
                        "target": {"identifier": MCP_URN},
                        "authProviderBinding": {
                            "authProvider": f"{PARENT}/authProviders/other-agent-secret"
                        },
                        "updateTime": "2026-04-05T00:00:00Z",
                    },
                ]
            },
        )
    )

    async def _resolve_other():
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            transport=other_agent_only_transport,
        ) as consumer:
            return await consumer.resolve_auth_binding(AGENT_URN, MCP_URN)

    assert asyncio.run(_resolve_other()) is None

    # 1. Legitimate oversized member (> 1 MiB uncompressed, small compressed) is rejected
    oversized_buf = io.BytesIO()
    with zipfile.ZipFile(oversized_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SKILL.md", _make_skill_md())
        zf.writestr("references/huge.txt", b"A" * (1024 * 1024 + 2048))
    with pytest.raises(SkillIntegrityError, match="exceeds .* per-file limit"):
        extract_skill_zip(oversized_buf.getvalue())

    # 2. Spoofed-size ZIP header (forged 10-byte file_size) fails closed as SkillIntegrityError
    raw_zip = bytearray(oversized_buf.getvalue())
    actual_len = 1024 * 1024 + 2048
    packed_real = struct.pack("<I", actual_len)
    packed_fake = struct.pack("<I", 10)
    assert raw_zip.count(packed_real) >= 2
    spoofed_zip = bytes(raw_zip).replace(packed_real, packed_fake)
    with pytest.raises(SkillIntegrityError):
        extract_skill_zip(spoofed_zip)


def test_platform_agent_unified_oauth_env_and_metadata_server_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Improvement 1: PlatformAgentConsumer & OrchestratorConfig unify env var resolution
    and acquire/cache GCE metadata server OAuth2 tokens for live googleapis.com endpoints."""
    monkeypatch.delenv("AGENT_REGISTRY_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_REGISTRY_AUTH_TOKEN", "unified-ar-auth-token")
    monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)

    env_consumer = PlatformAgentConsumer.from_env()
    try:
        assert env_consumer.access_token == "unified-ar-auth-token"
    finally:
        asyncio.run(env_consumer.aclose())
    assert OrchestratorConfig.from_env().access_token == "unified-ar-auth-token"

    monkeypatch.delenv("AGENT_REGISTRY_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "unified-google-oauth-token")
    env_consumer2 = PlatformAgentConsumer.from_env()
    try:
        assert env_consumer2.access_token == "unified-google-oauth-token"
    finally:
        asyncio.run(env_consumer2.aclose())
    assert OrchestratorConfig.from_env().access_token == "unified-google-oauth-token"

    # Metadata server token acquisition + caching + 302 redirect header stripping
    meta_calls = 0

    def _meta_handler(request: httpx.Request) -> httpx.Response:
        nonlocal meta_calls
        meta_calls += 1
        assert request.headers.get("metadata-flavor") == "Google"
        return httpx.Response(
            200,
            json={"access_token": "platform-sa-metadata-token", "expires_in": 3600},
        )

    skill_zip = _build_skill_zip(skill_md=_make_skill_md())
    recorded: list[dict[str, Any]] = []
    registry_transport = _build_mock_registry_transport(
        zip_bytes=skill_zip,
        recorded_requests=recorded,
    )

    async def _run() -> Any:
        async with PlatformAgentConsumer(
            project_id=PROJECT_ID,
            location=LOCATION,
            base_url="https://agentregistry.googleapis.com/v1",
            access_token=None,
            transport=registry_transport,
            metadata_transport=httpx.MockTransport(_meta_handler),
        ) as consumer:
            skills = await consumer.search_skills("cost anomaly")
            assert len(skills) == 1
            return await consumer.fetch_and_extract_skill(SKILL_RESOURCE)

    extracted = asyncio.run(_run())
    assert extracted.name == "cost-anomaly-triage"
    assert meta_calls == 1

    api_reqs = [r for r in recorded if r["host"] == "agentregistry.googleapis.com"]
    gcs_reqs = [r for r in recorded if r["host"] == "storage.googleapis.com"]
    assert len(api_reqs) >= 3
    assert all(r["headers"].get("authorization") == "Bearer platform-sa-metadata-token" for r in api_reqs)
    assert len(gcs_reqs) == 1
    assert "authorization" not in gcs_reqs[0]["headers"]
    assert not any(k.startswith("x-goog-") for k in gcs_reqs[0]["headers"])
