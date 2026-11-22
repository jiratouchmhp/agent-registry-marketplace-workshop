"""Streamable HTTP JSON-RPC 2.0 MCP Gateway for governed Agent Registry discovery & skill loading.

Bridges the gap where Google Cloud's native `agentregistry.googleapis.com/mcp`
exposes metadata only and has NO `?alt=media` skill download tool:
- `search_catalog(query, kind)`
- `load_verified_skill(skill_name)`
- `list_governed_mcp_servers()`
- `call_remote_a2a_agent(agent_name, task)`
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from middleware.app.registry_client import AgentRegistryClient, RegistryClientError
from middleware.app.security_verifier import SecurityVerifier, extract_skill_md_from_zip

MCP_PROTOCOL_VERSION = "2025-03-26"
GCE_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)
ID_TOKEN_REFRESH_SKEW_SECONDS = 60.0
DEFAULT_ID_TOKEN_TTL_SECONDS = 300.0

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_catalog",
        "description": (
            "Search governed enterprise Skills, MCP Servers, and A2A Agents registered in "
            "Google Cloud Agent Registry."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword or phrase to filter matching catalog entries.",
                    "default": "",
                },
                "kind": {
                    "type": "string",
                    "enum": ["all", "skill", "mcp_server", "agent"],
                    "description": "Resource type filter.",
                    "default": "all",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Search Governed Agent Catalog",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "load_verified_skill",
        "description": (
            "Download a governed Skill revision via ?alt=media, verify SHA-256 integrity, "
            "security scan findings=0, and Cloud KMS ECDSA P-256 signature, and return "
            "the attested SKILL.md instructions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": (
                        "Skill short name (e.g. 'cost-anomaly-triage'), prefixed ID "
                        "('private-cost-anomaly-triage'), or full resource name."
                    ),
                }
            },
            "required": ["skill_name"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Load Cryptographically Verified Skill",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "list_governed_mcp_servers",
        "description": (
            "List approved MCP Servers registered in Agent Registry along with their "
            "endpoint interfaces and tool safety annotations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "title": "List Governed MCP Servers",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "call_remote_a2a_agent",
        "description": (
            "Proxy a task request to a governed A2A Agent registered in Google Cloud Agent Registry."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Registered A2A agent short ID, displayName, or resource name.",
                },
                "task": {
                    "type": "string",
                    "description": "Natural-language task prompt to send to the A2A agent.",
                },
            },
            "required": ["agent_name", "task"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Invoke Governed Remote A2A Agent",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
]


def _jsonrpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _tool_response(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "structuredContent": payload,
        "isError": is_error,
    }


def _basename(resource_name: str) -> str:
    return resource_name.rsplit("/", 1)[-1] if "/" in resource_name else resource_name


def _extract_cloud_run_audience(
    endpoint_url: str, extra_iam_hosts: frozenset[str] = frozenset()
) -> str | None:
    """Return the OIDC audience origin if endpoint_url targets an IAM-protected Cloud Run service."""
    parsed = urlparse(endpoint_url)
    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower()
    if scheme != "https" or not hostname or not parsed.netloc:
        return None
    if hostname.endswith(".run.app") or hostname in extra_iam_hosts:
        return f"{scheme}://{parsed.netloc}"
    return None


def _parse_jwt_expiry_monotonic(token: str) -> float:
    """Compute a monotonic expiry timestamp from a JWT's 'exp' claim (best-effort)."""
    parts = token.split(".")
    if len(parts) == 3:
        try:
            payload_b64 = parts[1]
            padding = "=" * (-len(payload_b64) % 4)
            claims = json.loads(
                base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
            )
            exp = float(claims.get("exp", 0))
            remaining = exp - time.time() - ID_TOKEN_REFRESH_SKEW_SECONDS
            if remaining > 10.0:
                return time.monotonic() + remaining
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            pass
    return time.monotonic() + DEFAULT_ID_TOKEN_TTL_SECONDS


class McpGateway:
    """Handles Streamable HTTP JSON-RPC 2.0 MCP requests backed by Agent Registry."""

    def __init__(
        self,
        *,
        registry_client: AgentRegistryClient,
        verifier: SecurityVerifier,
        catalog_loader: Callable[[], Awaitable[dict[str, Any]]],
        a2a_transport: httpx.AsyncBaseTransport | None = None,
        metadata_identity_url: str = GCE_METADATA_IDENTITY_URL,
        metadata_transport: httpx.AsyncBaseTransport | None = None,
        enable_id_token_fallback: bool | None = None,
        a2a_iam_hosts: tuple[str, ...] | list[str] | set[str] | None = None,
    ) -> None:
        self.registry_client = registry_client
        self.verifier = verifier
        self.catalog_loader = catalog_loader
        self.a2a_transport = a2a_transport
        self._metadata_identity_url = metadata_identity_url
        self._metadata_transport = metadata_transport
        self._enable_id_token_fallback = (
            enable_id_token_fallback
            if enable_id_token_fallback is not None
            else (a2a_transport is None or metadata_transport is not None)
        )
        env_hosts = [
            h.strip().lower()
            for h in os.getenv("A2A_IAM_HOSTS", "").split(",")
            if h.strip()
        ]
        extra_hosts = [h.strip().lower() for h in (a2a_iam_hosts or ()) if str(h).strip()]
        self._a2a_iam_hosts: frozenset[str] = frozenset(env_hosts + extra_hosts)
        self._id_token_cache: dict[str, tuple[str, float]] = {}
        self._id_token_lock = asyncio.Lock()

    async def _fetch_metadata_identity_token(self, audience: str) -> tuple[str, float] | None:
        """Fetch an OIDC Identity Token for a Cloud Run target audience from the metadata server."""
        try:
            async with httpx.AsyncClient(
                timeout=2.0,
                transport=self._metadata_transport,
            ) as meta_client:
                resp = await meta_client.get(
                    self._metadata_identity_url,
                    params={"audience": audience, "format": "full"},
                    headers={"Metadata-Flavor": "Google"},
                )
            if resp.status_code != 200:
                return None
            token = resp.text.strip()
            if not token:
                return None
            return token, _parse_jwt_expiry_monotonic(token)
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def _fetch_gcloud_identity_token(self, audience: str) -> tuple[str, float] | None:
        """Fallback for local workstations invoking live IAM-protected Cloud Run A2A services."""

        def _run_gcloud() -> str:
            gcloud_bin = shutil.which("gcloud") or str(Path.home() / ".local" / "bin" / "gcloud")
            for cmd in (
                [gcloud_bin, "auth", "print-identity-token", f"--audiences={audience}", "--quiet"],
                [gcloud_bin, "auth", "print-identity-token", "--quiet"],
            ):
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=5,
                    )
                except (FileNotFoundError, subprocess.SubprocessError, OSError):
                    continue
                if proc.returncode == 0 and proc.stdout.strip():
                    return proc.stdout.strip()
            return ""

        token = await asyncio.to_thread(_run_gcloud)
        if not token:
            return None
        return token, _parse_jwt_expiry_monotonic(token)

    async def _get_cloud_run_id_token(self, audience: str) -> str:
        """Resolve and cache an OIDC Identity Token for an IAM-protected Cloud Run A2A service."""
        now = time.monotonic()
        cached = self._id_token_cache.get(audience)
        if cached is not None and now < cached[1]:
            return cached[0]

        async with self._id_token_lock:
            now = time.monotonic()
            cached = self._id_token_cache.get(audience)
            if cached is not None and now < cached[1]:
                return cached[0]

            fetched = await self._fetch_metadata_identity_token(audience)
            if fetched is None and self._metadata_transport is None:
                fetched = await self._fetch_gcloud_identity_token(audience)
            if fetched is not None:
                self._id_token_cache[audience] = fetched
                return fetched[0]
            return ""

    async def handle_jsonrpc(self, body: Any) -> dict[str, Any] | None:
        """Process a single JSON-RPC 2.0 message. Returns None for notifications."""
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            return _jsonrpc_error(None, -32600, "Invalid Request: expected JSON-RPC 2.0 object.")

        method = body.get("method")
        req_id = body.get("id")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}

        if not isinstance(method, str) or not method:
            return _jsonrpc_error(req_id, -32600, "Invalid Request: missing 'method'.")

        # Notifications have no 'id'
        if method.startswith("notifications/"):
            return None

        if method == "initialize":
            client_proto = params.get("protocolVersion")
            proto = client_proto if isinstance(client_proto, str) and client_proto else MCP_PROTOCOL_VERSION
            return _jsonrpc_result(
                req_id,
                {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "gcp-agent-registry-middleware-mcp",
                        "version": "1.0.0",
                    },
                },
            )

        if method == "ping":
            return _jsonrpc_result(req_id, {})

        if method == "tools/list":
            return _jsonrpc_result(req_id, {"tools": MCP_TOOLS})

        if method == "tools/call":
            tool_name = params.get("name")
            raw_arguments = params.get("arguments")
            arguments = {} if raw_arguments is None else raw_arguments
            if not isinstance(tool_name, str) or not isinstance(arguments, dict):
                return _jsonrpc_error(
                    req_id, -32602, "Invalid params: 'name' (str) and 'arguments' (object) required."
                )
            return await self._dispatch_tool(req_id, tool_name, arguments)

        return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    async def _dispatch_tool(
        self, req_id: Any, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if tool_name == "search_catalog":
            query = str(arguments.get("query", "")).strip().lower()
            kind = str(arguments.get("kind", "all")).strip().lower()
            if kind not in ("all", "skill", "mcp_server", "agent"):
                return _jsonrpc_error(
                    req_id,
                    -32602,
                    f"Invalid kind {kind!r}; expected one of 'all', 'skill', 'mcp_server', 'agent'.",
                )
            result = await self._tool_search_catalog(query=query, kind=kind)
            return _jsonrpc_result(req_id, _tool_response(result))

        if tool_name == "load_verified_skill":
            skill_name = arguments.get("skill_name")
            if not isinstance(skill_name, str) or not skill_name.strip():
                return _jsonrpc_error(req_id, -32602, "'skill_name' must be a non-empty string.")
            payload, is_err = await self._tool_load_verified_skill(skill_name.strip())
            return _jsonrpc_result(req_id, _tool_response(payload, is_error=is_err))

        if tool_name == "list_governed_mcp_servers":
            result = await self._tool_list_governed_mcp_servers()
            return _jsonrpc_result(req_id, _tool_response(result))

        if tool_name == "call_remote_a2a_agent":
            agent_name = arguments.get("agent_name")
            task = arguments.get("task")
            if not isinstance(agent_name, str) or not agent_name.strip():
                return _jsonrpc_error(req_id, -32602, "'agent_name' must be a non-empty string.")
            if not isinstance(task, str) or not task.strip():
                return _jsonrpc_error(req_id, -32602, "'task' must be a non-empty string.")
            payload, is_err = await self._tool_call_remote_a2a_agent(
                agent_name=agent_name.strip(), task=task.strip()
            )
            return _jsonrpc_result(req_id, _tool_response(payload, is_error=is_err))

        return _jsonrpc_error(req_id, -32601, f"Unknown MCP tool: {tool_name}")

    async def _tool_search_catalog(self, *, query: str, kind: str) -> dict[str, Any]:
        catalog = await self.catalog_loader()
        matches: dict[str, list[dict[str, Any]]] = {
            "skills": [],
            "mcpServers": [],
            "agents": [],
        }

        def _matches(obj: dict[str, Any]) -> bool:
            if not query:
                return True
            return query in json.dumps(obj).lower()

        if kind in ("all", "skill"):
            matches["skills"] = [s for s in catalog.get("skills", []) if _matches(s)]
        if kind in ("all", "mcp_server"):
            matches["mcpServers"] = [m for m in catalog.get("mcpServers", []) if _matches(m)]
        if kind in ("all", "agent"):
            matches["agents"] = [a for a in catalog.get("agents", []) if _matches(a)]

        return {
            "query": query,
            "kind": kind,
            "totalMatches": sum(len(v) for v in matches.values()),
            "results": matches,
        }

    async def _tool_load_verified_skill(self, skill_name: str) -> tuple[dict[str, Any], bool]:
        """Resolve, download (?alt=media), verify SHA-256 + KMS signature, and return SKILL.md."""
        skills = await self.registry_client.list_skills()
        target_skill: dict[str, Any] | None = None
        for sk in skills:
            full_name = str(sk.get("name", ""))
            base = _basename(full_name)
            fm_name = str((sk.get("frontmatter") or {}).get("name", ""))
            if skill_name in (full_name, base, fm_name, f"private-{skill_name}") or (
                base == f"private-{skill_name}"
            ):
                target_skill = sk
                break

        if target_skill is None:
            return {"error": f"Skill not found in Agent Registry: '{skill_name}'"}, True

        default_rev_name = str(target_skill.get("defaultRevision", ""))
        rev_doc: dict[str, Any] | None = None
        if default_rev_name:
            try:
                rev_doc = await self.registry_client.get_skill_revision(default_rev_name)
            except RegistryClientError as exc:
                return {"error": f"Failed to load defaultRevision metadata: {exc}"}, True

        ok, warnings, reason = self.verifier.check_metadata_policy(target_skill, rev_doc)
        if not ok or rev_doc is None:
            return {
                "error": "Skill blocked by governance policy.",
                "skill": target_skill.get("name"),
                "state": target_skill.get("state"),
                "reason": reason,
            }, True

        try:
            zip_bytes, actual_sha256 = await self.registry_client.download_skill_revision_media(
                default_rev_name,
                expected_sha256=rev_doc.get("sha256Hash"),
            )
        except RegistryClientError as exc:
            return {
                "error": "Failed to download verified skill archive.",
                "skill": target_skill.get("name"),
                "reason": str(exc),
            }, True

        verification = self.verifier.verify_skill_package(
            skill=target_skill,
            revision=rev_doc,
            zip_bytes=zip_bytes,
            downloaded_sha256=actual_sha256,
        )
        if not verification.approved:
            return {
                "error": "Skill failed cryptographic/policy verification.",
                "skill": target_skill.get("name"),
                "revision": default_rev_name,
                "reason": verification.rejection_reason,
            }, True

        skill_md_path, frontmatter, body = extract_skill_md_from_zip(zip_bytes)
        bundled_files: list[str] = []
        with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
            bundled_files = sorted(i.filename for i in zf.infolist() if not i.is_dir())

        return (
            {
                "skill": target_skill["name"],
                "skillId": target_skill.get("skillId", ""),
                "revision": default_rev_name,
                "state": verification.state,
                "sha256": verification.sha256,
                "kmsVerified": verification.kms_verified,
                "kmsKeyVersion": verification.kms_key_version,
                "warnings": warnings,
                "frontmatter": frontmatter,
                "skillMdPath": skill_md_path,
                "instructions": body.strip(),
                "files": bundled_files,
            },
            False,
        )

    async def _tool_list_governed_mcp_servers(self) -> dict[str, Any]:
        servers = await self.registry_client.list_mcp_servers()
        formatted = [
            {
                "name": srv.get("name"),
                "mcpServerId": srv.get("mcpServerId"),
                "displayName": srv.get("displayName"),
                "description": srv.get("description"),
                "interfaces": srv.get("interfaces", []),
                "tools": srv.get("tools", []),
            }
            for srv in servers
        ]
        return {"mcpServers": formatted, "count": len(formatted)}

    async def _tool_call_remote_a2a_agent(
        self, *, agent_name: str, task: str
    ) -> tuple[dict[str, Any], bool]:
        agents = await self.registry_client.list_agents()
        target_agent: dict[str, Any] | None = None
        for ag in agents:
            full_name = str(ag.get("name", ""))
            base = _basename(full_name)
            disp = str(ag.get("displayName", ""))
            urn = str(ag.get("agentId", ""))
            if (
                agent_name in (full_name, base, disp, urn)
                or base.endswith(f":{agent_name}")
                or urn.endswith(f":{agent_name}")
            ):
                target_agent = ag
                break

        if target_agent is None:
            return {"error": f"Registered A2A agent not found: '{agent_name}'"}, True

        endpoint_url = ""
        interfaces = target_agent.get("interfaces") or []
        for iface in interfaces:
            if isinstance(iface, dict) and iface.get("url"):
                endpoint_url = str(iface["url"]).strip()
                break
        if not endpoint_url:
            for proto in target_agent.get("protocols") or []:
                if isinstance(proto, dict):
                    for iface in proto.get("interfaces") or []:
                        if isinstance(iface, dict) and iface.get("url"):
                            endpoint_url = str(iface["url"]).strip()
                            break
                if endpoint_url:
                    break
        if not endpoint_url:
            card = target_agent.get("card") or {}
            card_content = card.get("content") if isinstance(card, dict) else None
            if isinstance(card_content, str):
                try:
                    card_content = json.loads(card_content)
                except Exception:
                    card_content = None
            candidates = (
                (card.get("supportedInterfaces") or []) if isinstance(card, dict) else []
            ) + (
                (card_content.get("supportedInterfaces") or [])
                if isinstance(card_content, dict)
                else []
            )
            for iface in candidates:
                if isinstance(iface, dict) and iface.get("url"):
                    endpoint_url = str(iface["url"]).strip()
                    break

        if not endpoint_url:
            return {
                "error": f"Registered A2A agent '{target_agent['name']}' has no callable interface URL."
            }, True

        a2a_payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": task}],
                }
            },
        }
        req_headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        audience = _extract_cloud_run_audience(endpoint_url, self._a2a_iam_hosts)
        if audience and self._enable_id_token_fallback:
            id_token = await self._get_cloud_run_id_token(audience)
            if id_token:
                req_headers["Authorization"] = f"Bearer {id_token}"

        try:
            async with httpx.AsyncClient(
                timeout=30.0, transport=self.a2a_transport
            ) as client:
                resp = await client.post(
                    endpoint_url,
                    json=a2a_payload,
                    headers=req_headers,
                )
        except httpx.HTTPError as exc:
            return {
                "error": f"Failed to invoke remote A2A agent at {endpoint_url}: {exc}",
                "agent": target_agent["name"],
            }, True

        if resp.status_code >= 400:
            return {
                "error": f"Remote A2A agent returned HTTP {resp.status_code}",
                "agent": target_agent["name"],
                "endpoint": endpoint_url,
                "response": resp.text,
            }, True

        try:
            agent_response = resp.json()
        except Exception:
            agent_response = {"raw": resp.text}

        return (
            {
                "agent": target_agent["name"],
                "agentId": target_agent.get("agentId", ""),
                "endpoint": endpoint_url,
                "task": task,
                "response": agent_response,
            },
            False,
        )
