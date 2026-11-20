"""FastAPI application for the Polyglot Agent Marketplace Middleware & MCP Gateway.

Exposes:
- GET  /healthz
- GET  /v1/catalog
- GET  /v1/marketplace/claude.json
- GET  /v1/marketplace/copilot.json
- GET  /v1/marketplace/codex.json
- GET  /v1/marketplace/antigravity.json
- GET  /v1/plugins/{plugin_name}.zip
- POST /mcp (Streamable HTTP JSON-RPC 2.0 MCP Gateway)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from middleware.app.config import MiddlewareSettings
from middleware.app.mcp_gateway import McpGateway
from middleware.app.polyglot_builder import (
    BuiltPluginBundle,
    VerifiedSkillArtifact,
    build_plugin_bundles,
)
from middleware.app.registry_client import AgentRegistryClient, RegistryClientError
from middleware.app.security_verifier import SecurityVerifier


@dataclass
class CatalogSnapshot:
    """Verified live snapshot of governed plugins, skills, MCP servers, and A2A agents."""

    bundles: list[BuiltPluginBundle] = field(default_factory=list)
    verified_skills: list[VerifiedSkillArtifact] = field(default_factory=list)
    rejected_skills: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    agents: list[dict[str, Any]] = field(default_factory=list)

    def bundle_by_name(self, plugin_name: str) -> BuiltPluginBundle | None:
        for bundle in self.bundles:
            if bundle.name == plugin_name:
                return bundle
        return None


class CatalogService:
    """Fetches, filters, verifies, and packages governed Agent Registry resources."""

    def __init__(
        self,
        *,
        settings: MiddlewareSettings,
        registry_client: AgentRegistryClient,
        verifier: SecurityVerifier,
    ) -> None:
        self.settings = settings
        self.registry_client = registry_client
        self.verifier = verifier

    async def build_snapshot(self) -> CatalogSnapshot:
        skills = await self.registry_client.list_skills()
        mcp_servers = await self.registry_client.list_mcp_servers()
        agents = await self.registry_client.list_agents()

        verified_skills: list[VerifiedSkillArtifact] = []
        rejected_skills: list[dict[str, Any]] = []

        for skill in skills:
            skill_name = str(skill.get("name", "<unknown>"))
            default_rev = str(skill.get("defaultRevision", ""))
            rev_doc: dict[str, Any] | None = None

            if default_rev:
                try:
                    rev_doc = await self.registry_client.get_skill_revision(default_rev)
                except RegistryClientError as exc:
                    rejected_skills.append(
                        {
                            "name": skill_name,
                            "state": skill.get("state"),
                            "reason": f"Failed to fetch defaultRevision metadata: {exc}",
                        }
                    )
                    continue

            ok, _warnings, reason = self.verifier.check_metadata_policy(skill, rev_doc)
            if not ok or rev_doc is None:
                rejected_skills.append(
                    {
                        "name": skill_name,
                        "state": skill.get("state"),
                        "reason": reason or "Rejected by metadata policy.",
                    }
                )
                continue

            try:
                zip_bytes, actual_sha256 = (
                    await self.registry_client.download_skill_revision_media(
                        default_rev,
                        expected_sha256=rev_doc.get("sha256Hash"),
                    )
                )
            except RegistryClientError as exc:
                rejected_skills.append(
                    {
                        "name": skill_name,
                        "state": skill.get("state"),
                        "revision": default_rev,
                        "reason": f"Media download/integrity check failed: {exc}",
                    }
                )
                continue

            verification = self.verifier.verify_skill_package(
                skill=skill,
                revision=rev_doc,
                zip_bytes=zip_bytes,
                downloaded_sha256=actual_sha256,
            )
            if not verification.approved:
                rejected_skills.append(
                    {
                        "name": skill_name,
                        "state": skill.get("state"),
                        "revision": default_rev,
                        "reason": verification.rejection_reason or "Verification failed.",
                    }
                )
                continue

            verified_skills.append(
                VerifiedSkillArtifact(
                    skill=skill,
                    revision=rev_doc,
                    verification=verification,
                    zip_bytes=zip_bytes,
                )
            )

        bundles = build_plugin_bundles(
            verified_skills=verified_skills,
            mcp_servers=mcp_servers,
            agents=agents,
            settings=self.settings,
        )
        return CatalogSnapshot(
            bundles=bundles,
            verified_skills=verified_skills,
            rejected_skills=rejected_skills,
            mcp_servers=mcp_servers,
            agents=agents,
        )

    def to_catalog_response(self, snapshot: CatalogSnapshot) -> dict[str, Any]:
        public_url = self.settings.normalized_public_url
        return {
            "marketplace": {
                "name": self.settings.marketplace_name,
                "displayName": self.settings.marketplace_display_name,
                "description": self.settings.marketplace_description,
                "projectId": self.settings.gcp_project_id,
                "location": self.settings.gcp_location,
            },
            "plugins": [
                {
                    "name": b.name,
                    "displayName": b.display_name,
                    "version": b.version,
                    "description": b.description,
                    "sha256": b.sha256,
                    "bundleUrl": f"{public_url}/v1/plugins/{b.name}.zip",
                    "download_url": f"{public_url}/v1/plugins/{b.name}.zip",
                    "skills": b.skills,
                    "mcpServers": [m.get("name") for m in b.mcp_servers],
                    "agents": [a.get("name") for a in b.agents],
                    "warnings": b.warnings,
                    "keywords": b.keywords,
                }
                for b in snapshot.bundles
            ],
            "skills": [
                {
                    "name": art.skill.get("name"),
                    "skillId": art.skill.get("skillId"),
                    "displayName": art.skill.get("displayName"),
                    "description": art.verification.frontmatter.get(
                        "description", art.skill.get("description", "")
                    ),
                    "state": art.verification.state,
                    "revision": art.verification.revision_name,
                    "sha256": art.verification.sha256,
                    "kmsVerified": art.verification.kms_verified,
                    "kmsKeyVersion": art.verification.kms_key_version,
                    "warnings": art.verification.warnings,
                    "frontmatter": art.verification.frontmatter,
                }
                for art in snapshot.verified_skills
            ],
            "rejectedSkills": snapshot.rejected_skills,
            "mcpServers": snapshot.mcp_servers,
            "agents": snapshot.agents,
        }


def create_app(
    *,
    settings: MiddlewareSettings | None = None,
    registry_client: AgentRegistryClient | None = None,
    registry_transport: httpx.AsyncBaseTransport | None = None,
    a2a_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Construct the Polyglot Marketplace Middleware FastAPI application."""
    cfg = settings if settings is not None else MiddlewareSettings.from_env()
    owns_client = registry_client is None
    client = (
        registry_client
        if registry_client is not None
        else AgentRegistryClient(
            base_url=cfg.normalized_registry_base_url,
            project_id=cfg.gcp_project_id,
            location=cfg.gcp_location,
            auth_token=cfg.agent_registry_auth_token,
            quota_project=cfg.effective_quota_project,
            transport=registry_transport,
        )
    )
    verifier = SecurityVerifier(
        require_kms_signature=cfg.require_kms_signature,
        kms_public_key_pem_path=cfg.kms_public_key_pem_path,
        kms_public_key_pem=cfg.kms_public_key_pem,
        kms_signing_key_version=cfg.kms_signing_key_version,
        kms_auth_token=cfg.agent_registry_auth_token,
        allow_deprecated=cfg.allow_deprecated_skills,
    )
    catalog_service = CatalogService(
        settings=cfg,
        registry_client=client,
        verifier=verifier,
    )

    async def _load_catalog_dict() -> dict[str, Any]:
        snap = await catalog_service.build_snapshot()
        return catalog_service.to_catalog_response(snap)

    mcp_gateway = McpGateway(
        registry_client=client,
        verifier=verifier,
        catalog_loader=_load_catalog_dict,
        a2a_transport=a2a_transport,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if owns_client:
            await client.aclose()

    application = FastAPI(
        title="Polyglot Enterprise Agent Marketplace Middleware",
        version="1.0.0",
        description=(
            "Governed multi-IDE marketplace and Streamable HTTP MCP gateway backed by "
            "Google Cloud Agent Registry (v1)."
        ),
        lifespan=lifespan,
    )
    application.state.settings = cfg
    application.state.registry_client = client
    application.state.verifier = verifier
    application.state.catalog_service = catalog_service
    application.state.mcp_gateway = mcp_gateway

    @application.exception_handler(RegistryClientError)
    async def _handle_registry_error(_request: Request, exc: RegistryClientError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "error": "upstream_registry_error",
                "message": str(exc),
                "upstreamStatus": exc.status_code,
            },
        )

    @application.exception_handler(httpx.HTTPError)
    async def _handle_httpx_error(_request: Request, exc: httpx.HTTPError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "error": "upstream_registry_unreachable",
                "message": str(exc),
            },
        )

    @application.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "agent-registry-marketplace-middleware",
            "projectId": cfg.gcp_project_id,
            "location": cfg.gcp_location,
            "requireKmsSignature": cfg.require_kms_signature,
        }

    @application.get("/v1/catalog")
    async def get_catalog() -> dict[str, Any]:
        snapshot = await catalog_service.build_snapshot()
        return catalog_service.to_catalog_response(snapshot)

    @application.get("/v1/marketplace/claude.json")
    async def get_claude_marketplace() -> dict[str, Any]:
        snapshot = await catalog_service.build_snapshot()
        public_url = cfg.normalized_public_url
        return {
            "name": cfg.marketplace_name,
            "owner": {
                "name": cfg.marketplace_owner_name,
                "email": cfg.marketplace_owner_email,
            },
            "metadata": {
                "description": cfg.marketplace_description,
                "version": "1.0.0",
            },
            "plugins": [
                {
                    "name": b.name,
                    "description": b.description,
                    "version": b.version,
                    "source": {
                        "source": "archive",
                        "url": f"{public_url}/v1/plugins/{b.name}.zip",
                    },
                }
                for b in snapshot.bundles
            ],
        }

    @application.get("/v1/marketplace/copilot.json")
    async def get_copilot_marketplace() -> dict[str, Any]:
        snapshot = await catalog_service.build_snapshot()
        public_url = cfg.normalized_public_url
        return {
            "name": cfg.marketplace_name,
            "owner": {
                "name": cfg.marketplace_owner_name,
                "email": cfg.marketplace_owner_email,
            },
            "metadata": {
                "description": cfg.marketplace_description,
                "version": "1.0.0",
            },
            "plugins": [
                {
                    "name": b.name,
                    "description": b.description,
                    "version": b.version,
                    "category": b.category,
                    "tags": b.keywords,
                    "source": {
                        "source": "archive",
                        "url": f"{public_url}/v1/plugins/{b.name}.zip",
                    },
                }
                for b in snapshot.bundles
            ],
        }

    @application.get("/v1/marketplace/codex.json")
    async def get_codex_marketplace() -> dict[str, Any]:
        snapshot = await catalog_service.build_snapshot()
        public_url = cfg.normalized_public_url
        return {
            "name": cfg.marketplace_name,
            "interface": {
                "displayName": cfg.marketplace_display_name,
            },
            "plugins": [
                {
                    "name": b.name,
                    "source": {
                        "source": "archive",
                        "url": f"{public_url}/v1/plugins/{b.name}.zip",
                    },
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                    "category": b.category,
                }
                for b in snapshot.bundles
            ],
        }

    @application.get("/v1/marketplace/antigravity.json")
    async def get_antigravity_marketplace() -> dict[str, Any]:
        snapshot = await catalog_service.build_snapshot()
        public_url = cfg.normalized_public_url
        return {
            "name": cfg.marketplace_name,
            "description": cfg.marketplace_description,
            "plugins": [
                {
                    "name": b.name,
                    "displayName": b.display_name,
                    "description": b.description,
                    "version": b.version,
                    "source": {
                        "type": "archive",
                        "url": f"{public_url}/v1/plugins/{b.name}.zip",
                        "sha256": b.sha256,
                    },
                }
                for b in snapshot.bundles
            ],
        }

    @application.get("/v1/plugins/{plugin_name}.zip")
    async def download_plugin_bundle(plugin_name: str) -> Response:
        snapshot = await catalog_service.build_snapshot()
        bundle = snapshot.bundle_by_name(plugin_name)
        if bundle is None:
            raise HTTPException(
                status_code=404,
                detail=f"Verified plugin bundle not found: '{plugin_name}'",
            )
        return Response(
            content=bundle.zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{bundle.name}.zip"',
                "X-Bundle-Sha256": bundle.sha256,
                "Cache-Control": "no-store",
            },
        )

    @application.post("/mcp")
    async def mcp_streamable_http(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error: invalid JSON payload."},
                },
            )

        result = await mcp_gateway.handle_jsonrpc(body)
        if result is None:
            return Response(status_code=202)
        headers: dict[str, str] = {}
        if (
            isinstance(body, dict)
            and body.get("method") == "initialize"
            and "result" in result
        ):
            headers["Mcp-Session-Id"] = str(uuid.uuid4())
        return JSONResponse(status_code=200, content=result, headers=headers)

    return application


app = create_app()
