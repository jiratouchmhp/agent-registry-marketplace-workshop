"""Async httpx client for Google Cloud Agent Registry v1 REST API.

Handles the critical ?alt=media 302 redirect behavior:
- Strips ALL x-goog-* headers and Authorization before following signed GCS URLs
- Verifies downloaded ZIP SHA-256 strictly matches SkillRevision.sha256Hash
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

GCE_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
TOKEN_REFRESH_SKEW_SECONDS = 60.0


class RegistryClientError(RuntimeError):
    """Raised when an Agent Registry v1 API request fails."""

    def __init__(self, message: str, status_code: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class IntegrityVerificationError(RegistryClientError):
    """Raised when downloaded skill media SHA-256 does not match SkillRevision.sha256Hash."""


def normalize_sha256_hex(raw: Any) -> str:
    """Normalize a SHA-256 hex digest string (strip whitespace and optional 'sha256:' prefix)."""
    if not isinstance(raw, str):
        return ""
    cleaned = raw.strip().lower()
    if cleaned.startswith("sha256:"):
        cleaned = cleaned[len("sha256:") :].strip()
    return cleaned


def _url_origin(url: httpx.URL) -> tuple[str, str, int | None]:
    """Return normalized (scheme, host, effective_port) for origin comparison."""
    scheme = url.scheme.lower()
    host = (url.host or "").lower()
    port = url.port
    if port is None:
        port = 443 if scheme == "https" else (80 if scheme == "http" else None)
    return (scheme, host, port)


def should_preserve_auth_on_redirect(
    initial_url: str | httpx.URL, next_url: str | httpx.URL
) -> bool:
    """Return True ONLY for same-origin Google API media download redirects (/download/...).

    Live Google Cloud Agent Registry returns HTTP 302 from
    `https://agentregistry.googleapis.com/v1/.../revisions/<rev>?alt=media` to
    `https://agentregistry.googleapis.com/download/v1/.../revisions/<rev>?alt=media`,
    which is the same-origin authenticated Scotty media download endpoint.

    For ALL cross-origin redirects (such as GCS signed URLs on storage.googleapis.com)
    and ALL signed-URL redirects (`/scotty-signed/` or query params `X-Goog-Signature`,
    `Signature`, `GoogleAccessId`, `X-Goog-Credential`), this returns False so that
    `Authorization` and all `x-goog-*` headers are strictly stripped.
    """
    src = httpx.URL(str(initial_url))
    dst = httpx.URL(str(next_url))
    if _url_origin(src) != _url_origin(dst):
        return False
    lower_path = (dst.path or "").lower()
    if "/scotty-signed/" in lower_path:
        return False
    query_keys = {k.lower() for k in dst.params.keys()}
    if query_keys & {"x-goog-signature", "signature", "googleaccessid", "x-goog-credential"}:
        return False
    return lower_path.startswith("/download/")


def strip_google_redirect_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip all x-goog-* and Authorization headers before following a signed GCS redirect."""
    return {
        k: v
        for k, v in headers.items()
        if not k.lower().startswith("x-goog-") and k.lower() != "authorization"
    }


def is_private_skill(skill: dict[str, Any]) -> bool:
    """Return True when an Agent Registry Skill belongs to the private enterprise publisher.

    Live Google Cloud Agent Registry ``locations/global/skills`` returns 100+ built-in
    first-party Google skills (e.g., ``cloud.google.com-*``, ``discoveryengine.googleapis.com-*``).
    Private enterprise skills always have resource IDs prefixed with ``private-`` (and may omit
    ``publisher`` on live v1 responses).
    """
    name = str(skill.get("name", "")).strip()
    short_id = name.rsplit("/", 1)[-1] if name else ""
    if short_id.startswith("private-"):
        return True
    publisher = str(skill.get("publisher", "")).strip().lower()
    if publisher and (
        publisher == "private"
        or publisher.endswith("/publishers/private")
        or publisher == "publisher_tier_private"
    ):
        return True
    return False


class AgentRegistryClient:
    """Async REST client for Google Cloud Agent Registry v1."""

    def __init__(
        self,
        *,
        base_url: str,
        project_id: str,
        location: str,
        skills_location: str | None = None,
        only_private_skills: bool = True,
        auth_token: str = "",
        quota_project: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        metadata_token_url: str = GCE_METADATA_TOKEN_URL,
        metadata_transport: httpx.AsyncBaseTransport | None = None,
        enable_adc_fallback: bool | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.project_id = project_id
        self.location = location
        cleaned_skills_loc = skills_location.strip() if isinstance(skills_location, str) else ""
        if cleaned_skills_loc:
            self.skills_location = cleaned_skills_loc
        elif self._is_googleapis and location not in ("global", "us", "eu"):
            self.skills_location = "global"
        else:
            self.skills_location = location
        self.only_private_skills = only_private_skills
        self.auth_token = auth_token.strip()
        self.quota_project = quota_project or project_id
        self._timeout = timeout
        self._transport = transport
        self._metadata_token_url = metadata_token_url
        self._metadata_transport = metadata_transport
        self._enable_adc_fallback = (
            enable_adc_fallback
            if enable_adc_fallback is not None
            else (transport is None or metadata_transport is not None)
        )
        self._client: httpx.AsyncClient | None = None
        self._cached_oauth_token: str = ""
        self._cached_oauth_expiry: float = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def parent(self) -> str:
        return f"projects/{self.project_id}/locations/{self.location}"

    @property
    def skills_parent(self) -> str:
        return f"projects/{self.project_id}/locations/{self.skills_location}"

    @property
    def _is_googleapis(self) -> bool:
        return "googleapis.com" in self.base_url.lower()

    def _default_api_headers(self, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "x-goog-user-project": self.quota_project,
            "x-goog-api-client": "gcp-agent-marketplace-middleware/1.0.0",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        elif not self._is_googleapis:
            # Send a simulated bearer header only against non-googleapis local/mock endpoints
            # so redirect stripping is actively exercised without leaking fake tokens to GCP.
            headers["Authorization"] = "Bearer adc-simulated-token"
        if extra_headers:
            headers.update(extra_headers)
        return headers

    async def _fetch_metadata_server_token(self) -> tuple[str, float] | None:
        """Fetch an OAuth2 access token from the Cloud Run / GCE metadata server."""
        try:
            async with httpx.AsyncClient(
                timeout=2.0,
                transport=self._metadata_transport,
            ) as meta_client:
                resp = await meta_client.get(
                    self._metadata_token_url,
                    headers={"Metadata-Flavor": "Google"},
                )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            token = str(payload.get("access_token", "")).strip()
            if not token:
                return None
            expires_in = float(payload.get("expires_in", 3600))
            ttl = max(10.0, expires_in - TOKEN_REFRESH_SKEW_SECONDS)
            return token, time.monotonic() + ttl
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return None

    async def _fetch_gcloud_adc_token(self) -> tuple[str, float] | None:
        """Fallback for local workstations calling live googleapis.com via gcloud ADC."""

        def _run_gcloud() -> str:
            gcloud_bin = shutil.which("gcloud") or str(Path.home() / ".local" / "bin" / "gcloud")
            for cmd in (
                [gcloud_bin, "auth", "application-default", "print-access-token", "--quiet"],
                [gcloud_bin, "auth", "print-access-token", "--quiet"],
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
        return token, time.monotonic() + 300.0

    async def get_access_token(self) -> str:
        """Resolve and cache an OAuth2 Bearer token for live googleapis.com endpoints."""
        if self.auth_token:
            return self.auth_token
        if not self._is_googleapis or not self._enable_adc_fallback:
            return ""
        now = time.monotonic()
        if self._cached_oauth_token and now < self._cached_oauth_expiry:
            return self._cached_oauth_token

        async with self._token_lock:
            now = time.monotonic()
            if self._cached_oauth_token and now < self._cached_oauth_expiry:
                return self._cached_oauth_token

            fetched = await self._fetch_metadata_server_token()
            if fetched is None:
                fetched = await self._fetch_gcloud_adc_token()
            if fetched is not None:
                self._cached_oauth_token, self._cached_oauth_expiry = fetched
                return self._cached_oauth_token
            return ""

    async def _resolve_api_headers(
        self, extra_headers: dict[str, str] | None = None
    ) -> dict[str, str]:
        headers = self._default_api_headers(extra_headers)
        if "Authorization" not in headers and self._is_googleapis:
            token = await self.get_access_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> AgentRegistryClient:
        self._get_client()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _resource_url(self, resource_path: str) -> str:
        cleaned = resource_path.lstrip("/")
        if cleaned.startswith("v1/"):
            cleaned = cleaned[3:]
        return f"{self.base_url}/{cleaned}"

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        client = self._get_client()
        url = self._resource_url(path)
        resp = await client.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=await self._resolve_api_headers(extra_headers),
        )
        if resp.status_code >= 400:
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text
            raise RegistryClientError(
                f"Agent Registry {method} {url} failed with HTTP {resp.status_code}: {err_body}",
                status_code=resp.status_code,
                body=err_body,
            )
        if not resp.content:
            return {}
        return resp.json()

    # ------------------------------------------------------------------
    # Skills & SkillRevisions
    # ------------------------------------------------------------------

    async def list_skills(
        self,
        filter_expr: str = "",
        *,
        page_size: int = 200,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        skills: list[dict[str, Any]] = []
        page_token = ""
        for _ in range(max_pages):
            params: dict[str, Any] = {"pageSize": page_size}
            if filter_expr:
                params["filter"] = filter_expr
            if page_token:
                params["pageToken"] = page_token
            data = await self._request_json("GET", f"{self.skills_parent}/skills", params=params)
            page_items = data.get("skills", [])
            if isinstance(page_items, list):
                skills.extend(page_items)
            next_token = str(data.get("nextPageToken") or "").strip()
            if not next_token or next_token == page_token:
                break
            page_token = next_token
        if self.only_private_skills:
            return [s for s in skills if isinstance(s, dict) and is_private_skill(s)]
        return skills

    async def get_skill(self, skill_name_or_id: str) -> dict[str, Any]:
        path = (
            skill_name_or_id
            if skill_name_or_id.startswith("projects/")
            else f"{self.skills_parent}/skills/{skill_name_or_id}"
        )
        return await self._request_json("GET", path)

    async def search_skills(
        self,
        *,
        search_string: str = "",
        search_type: str = "KEYWORD",
        filter_expr: str = "",
        page_size: int = 50,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        skills: list[dict[str, Any]] = []
        page_token = ""
        for _ in range(max_pages):
            body: dict[str, Any] = {
                "searchString": search_string,
                "searchType": search_type,
                "pageSize": page_size,
            }
            if filter_expr:
                body["filter"] = filter_expr
            if page_token:
                body["pageToken"] = page_token
            data = await self._request_json(
                "POST", f"{self.skills_parent}/skills:search", json_body=body
            )
            page_items = data.get("skills", [])
            if isinstance(page_items, list):
                skills.extend(page_items)
            next_token = str(data.get("nextPageToken") or "").strip()
            if not next_token or next_token == page_token:
                break
            page_token = next_token
        if self.only_private_skills:
            return [s for s in skills if isinstance(s, dict) and is_private_skill(s)]
        return skills

    async def list_skill_revisions(self, skill_name_or_id: str) -> list[dict[str, Any]]:
        path = (
            skill_name_or_id
            if skill_name_or_id.startswith("projects/")
            else f"{self.skills_parent}/skills/{skill_name_or_id}"
        )
        data = await self._request_json("GET", f"{path}/revisions")
        return list(data.get("skillRevisions", []))

    async def get_skill_revision(self, revision_name: str) -> dict[str, Any]:
        return await self._request_json("GET", revision_name)

    async def download_skill_revision_media(
        self,
        revision_name: str,
        *,
        expected_sha256: str | None = None,
        max_redirects: int = 5,
    ) -> tuple[bytes, str]:
        """Download a SkillRevision ZIP via ?alt=media, stripping Google headers on redirect.

        Returns `(zip_bytes, sha256_hex)` after strictly verifying SHA-256 against
        `expected_sha256` (or the revision's `sha256Hash` fetched from metadata).
        """
        normalized_expected = normalize_sha256_hex(expected_sha256)
        if not normalized_expected:
            rev_meta = await self.get_skill_revision(revision_name)
            normalized_expected = normalize_sha256_hex(rev_meta.get("sha256Hash"))

        if not normalized_expected:
            raise IntegrityVerificationError(
                f"SkillRevision '{revision_name}' is missing required 'sha256Hash'; "
                "refusing unverified media download."
            )

        client = self._get_client()
        url = self._resource_url(revision_name)
        initial_headers = await self._resolve_api_headers()
        initial_headers["Accept"] = "application/zip, application/octet-stream, */*"

        resp = await client.get(
            url,
            params={"alt": "media"},
            headers=initial_headers,
            follow_redirects=False,
        )

        # Manually follow 3xx redirects:
        # - Same-origin Google API /download/... hops keep Authorization & x-goog-* headers.
        # - Cross-origin redirects & signed GCS URLs strip ALL x-goog-* & Authorization headers.
        hops = 0
        initial_request_url = str(resp.request.url)
        current_url = initial_request_url
        current_headers = dict(initial_headers)
        while resp.status_code in (301, 302, 303, 307, 308):
            hops += 1
            if hops > max_redirects:
                raise RegistryClientError(
                    f"Exceeded max redirects ({max_redirects}) downloading {revision_name}"
                )
            location = resp.headers.get("location")
            if not location:
                raise RegistryClientError(
                    f"Redirect response ({resp.status_code}) missing Location header for "
                    f"{revision_name}"
                )
            next_url = urljoin(current_url, location)
            if should_preserve_auth_on_redirect(initial_request_url, next_url):
                next_headers = dict(current_headers)
            else:
                next_headers = strip_google_redirect_headers(current_headers)
            resp = await client.get(
                next_url,
                headers=next_headers,
                follow_redirects=False,
            )
            current_url = str(resp.request.url)
            current_headers = next_headers

        if resp.status_code != 200:
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text
            raise RegistryClientError(
                f"Failed to download media for {revision_name}: HTTP {resp.status_code}: "
                f"{err_body}",
                status_code=resp.status_code,
                body=err_body,
            )

        zip_bytes = resp.content
        actual_sha256 = hashlib.sha256(zip_bytes).hexdigest()

        if not hmac.compare_digest(actual_sha256.lower(), normalized_expected):
            raise IntegrityVerificationError(
                f"SHA-256 integrity mismatch for {revision_name}: "
                f"expected {normalized_expected}, computed {actual_sha256}"
            )

        return zip_bytes, actual_sha256

    # ------------------------------------------------------------------
    # Read-Only Discovery: MCP Servers, Agents, Endpoints, Bindings, Findings
    # ------------------------------------------------------------------

    async def list_mcp_servers(self, filter_expr: str = "") -> list[dict[str, Any]]:
        params = {"filter": filter_expr} if filter_expr else None
        data = await self._request_json("GET", f"{self.parent}/mcpServers", params=params)
        return list(data.get("mcpServers", []))

    async def get_mcp_server(self, server_name_or_id: str) -> dict[str, Any]:
        path = (
            server_name_or_id
            if server_name_or_id.startswith("projects/")
            else f"{self.parent}/mcpServers/{server_name_or_id}"
        )
        return await self._request_json("GET", path)

    async def search_mcp_servers(self, search_string: str = "") -> list[dict[str, Any]]:
        data = await self._request_json(
            "POST", f"{self.parent}/mcpServers:search", json_body={"searchString": search_string}
        )
        return list(data.get("mcpServers", []))

    async def list_agents(self, filter_expr: str = "") -> list[dict[str, Any]]:
        params = {"filter": filter_expr} if filter_expr else None
        data = await self._request_json("GET", f"{self.parent}/agents", params=params)
        return list(data.get("agents", []))

    async def get_agent(self, agent_name_or_id: str) -> dict[str, Any]:
        path = (
            agent_name_or_id
            if agent_name_or_id.startswith("projects/")
            else f"{self.parent}/agents/{agent_name_or_id}"
        )
        return await self._request_json("GET", path)

    async def search_agents(self, search_string: str = "") -> list[dict[str, Any]]:
        data = await self._request_json(
            "POST", f"{self.parent}/agents:search", json_body={"searchString": search_string}
        )
        return list(data.get("agents", []))

    async def list_endpoints(self, filter_expr: str = "") -> list[dict[str, Any]]:
        params = {"filter": filter_expr} if filter_expr else None
        data = await self._request_json("GET", f"{self.parent}/endpoints", params=params)
        return list(data.get("endpoints", []))

    async def fetch_available_bindings(
        self,
        *,
        source_identifier: str | None = None,
        target_identifier: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if source_identifier:
            params["sourceIdentifier"] = source_identifier
        if target_identifier:
            params["targetIdentifier"] = target_identifier
        data = await self._request_json(
            "GET",
            f"{self.parent}/bindings:fetchAvailable",
            params=params or None,
            extra_headers=extra_headers,
        )
        return list(data.get("bindings", []))

    async def list_findings(self, filter_expr: str = "") -> list[dict[str, Any]]:
        params = {"filter": filter_expr} if filter_expr else None
        data = await self._request_json("GET", f"{self.parent}/findings", params=params)
        return list(data.get("findings", []))
