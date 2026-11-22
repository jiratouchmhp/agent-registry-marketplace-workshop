"""Lab 03 Starter Exercise: Agent Registry ``?alt=media`` Redirect & Polyglot Bundle Assembly.

Goal:
    When Enterprise Marketplace Middleware fetches a skill revision archive from
    Google Cloud Agent Registry ``v1``:
        ``GET /v1/projects/{p}/locations/{l}/skills/private-{id}/revisions/{rev}?alt=media``
    Agent Registry returns **HTTP 302 Found** redirecting to a short-lived
    Cloud Storage (GCS) signed URL on a different origin.

    CRITICAL GOTCHA:
    If your HTTP client blindly forwards ``Authorization`` or ``x-goog-*``
    headers (such as ``x-goog-user-project`` or ``x-goog-api-client``) across
    the redirect to GCS, GCS rejects the signed URL!

    In this exercise you will implement:
    1. ``strip_headers_for_cross_origin_redirect`` — removing ``Authorization``
       and every ``x-goog-*`` header (case-insensitively) when the redirect
       origin changes.
    2. ``download_skill_revision_media`` — following the ``?alt=media`` 302
       redirect safely without leaking auth/Google headers to GCS, and verifying
       the downloaded ZIP's SHA-256 digest against ``expected_sha256``.
    3. ``assemble_polyglot_plugin_zip`` — combining verified skill archives +
       MCP server config into a single Polyglot 5-IDE plugin ZIP containing
       manifests for Agent Plugins 1.0, Antigravity, Claude Code, Copilot, and Codex.

Reference Solution:
    ``middleware/``
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import zipfile
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx


def strip_headers_for_cross_origin_redirect(
    headers: dict[str, str],
    source_url: str,
    target_url: str,
) -> dict[str, str]:
    """Return headers safe to send to ``target_url`` after a 3xx redirect.

    Requirements:
    - Compare ``(scheme, netloc)`` of ``source_url`` and ``target_url``.
    - If the origin changed (cross-origin redirect, e.g.
      ``agentregistry.googleapis.com`` -> ``storage.googleapis.com``):
      strip ``Authorization`` AND every header starting with ``x-goog-``
      (case-insensitive check!).
    - If same-origin, preserve headers unchanged.
    """
    # WORKSHOP-TODO(lab-03): Compare origins with urlparse() and drop
    # `authorization` and any `x-goog-*` headers on cross-origin redirects.
    raise NotImplementedError(
        "WORKSHOP-TODO(lab-03): Implement strip_headers_for_cross_origin_redirect()"
    )


def download_skill_revision_media(
    client: httpx.Client,
    *,
    media_url: str,
    auth_token: str,
    quota_project: str,
    expected_sha256: str,
    max_redirects: int = 5,
) -> bytes:
    """Download ``?alt=media`` skill bytes safely across the 302 GCS redirect.

    Requirements:
    1. Start with headers:
       ``{
           "Authorization": f"Bearer {auth_token}",
           "x-goog-user-project": quota_project,
           "x-goog-api-client": "acme-marketplace-middleware/1.0",
       }``
    2. Call ``client.get(current_url, headers=headers, follow_redirects=False)``.
    3. While status is in ``(301, 302, 303, 307, 308)``:
       - Resolve ``Location`` with ``urljoin(current_url, location)``.
       - Call ``strip_headers_for_cross_origin_redirect(headers, current_url, next_url)``.
       - Follow up to ``max_redirects``.
    4. Raise ``RuntimeError`` if final HTTP status != 200.
    5. Verify ``hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256.lower())``
       before returning bytes!
    """
    # WORKSHOP-TODO(lab-03): Implement manual redirect loop using
    # strip_headers_for_cross_origin_redirect() and strict SHA-256 verification.
    raise NotImplementedError(
        "WORKSHOP-TODO(lab-03): Implement download_skill_revision_media()"
    )


def assemble_polyglot_plugin_zip(
    *,
    plugin_name: str,
    display_name: str,
    version: str,
    description: str,
    mcp_server_key: str,
    mcp_server_url: str,
    skill_markdowns: dict[str, str],
) -> tuple[bytes, str]:
    """Assemble a Polyglot 5-IDE plugin ZIP archive and return ``(zip_bytes, sha256_hex)``.

    Required files inside the generated ZIP:
    - ``plugin.json`` (Agent Plugins 1.0 ``$schema``)
    - ``.antigravity-plugin/plugin.json`` (Antigravity v1 ``$schema``, ``logo: "assets/logo.svg"``)
    - ``assets/logo.svg`` (valid 1:1 square SVG, e.g. ``viewBox="0 0 64 64"``)
    - ``.claude-plugin/plugin.json`` (Claude Code manifest)
    - ``.codex-plugin/plugin.json`` (OpenAI Codex manifest)
    - ``mcp.json`` (Agent Plugins 1.0 MCP config — NO ``${ENV_VAR}`` expansion!)
    - ``.mcp.json`` (Claude Code MCP config)
    - ``mcp_config.json`` (Antigravity MCP config with ``authProviderType: "google_credentials"``)
    - ``skills/<skill_name>/SKILL.md`` for each entry in ``skill_markdowns``
    """
    # WORKSHOP-TODO(lab-03): Write all required manifests, 1:1 SVG logo, MCP
    # configs, and skills/<name>/SKILL.md into an in-memory ZipFile and return
    # (zip_bytes, hashlib.sha256(zip_bytes).hexdigest()).
    raise NotImplementedError(
        "WORKSHOP-TODO(lab-03): Implement assemble_polyglot_plugin_zip()"
    )


def _run_self_check() -> None:
    """Offline MockTransport self-check for Lab 03."""
    print("Lab 03 Self-Check")
    passed = 0
    total = 3
    sample_zip = b"PK\x05\x06" + b"\x00" * 18
    sample_sha = hashlib.sha256(sample_zip).hexdigest()

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "agentregistry.googleapis.com":
            assert request.headers.get("authorization") == "Bearer test-token"
            return httpx.Response(
                302,
                headers={"Location": "https://storage.googleapis.com/signed-bucket/skill.zip?sig=ok"},
            )
        if request.url.host == "storage.googleapis.com":
            forbidden = [
                k for k in request.headers.keys()
                if k.lower() == "authorization" or k.lower().startswith("x-goog-")
            ]
            if forbidden:
                return httpx.Response(400, text=f"GCS rejected leaked headers: {forbidden}")
            return httpx.Response(200, content=sample_zip)
        return httpx.Response(404)

    try:
        cleaned = strip_headers_for_cross_origin_redirect(
            {
                "Authorization": "Bearer secret",
                "x-goog-user-project": "acme-agent-governance",
                "X-Goog-Api-Client": "middleware/1.0",
                "Accept": "*/*",
            },
            "https://agentregistry.googleapis.com/v1/projects/p/locations/l/skills/private-x/revisions/1?alt=media",
            "https://storage.googleapis.com/bucket/signed.zip",
        )
        assert "Authorization" not in cleaned
        assert not any(k.lower().startswith("x-goog-") for k in cleaned)
        assert cleaned.get("Accept") == "*/*"
        print("  [PASS] strip_headers_for_cross_origin_redirect")
        passed += 1
    except NotImplementedError as exc:
        print(f"  [TODO] {exc}")

    try:
        with httpx.Client(transport=httpx.MockTransport(mock_handler)) as client:
            data = download_skill_revision_media(
                client,
                media_url="https://agentregistry.googleapis.com/v1/projects/p/locations/asia-southeast3/skills/private-cost-anomaly-triage/revisions/rev-1?alt=media",
                auth_token="test-token",
                quota_project="acme-agent-governance",
                expected_sha256=sample_sha,
            )
        assert data == sample_zip
        print("  [PASS] download_skill_revision_media (302 GCS redirect + SHA-256 verified)")
        passed += 1
    except NotImplementedError as exc:
        print(f"  [TODO] {exc}")

    try:
        bundle_bytes, bundle_sha = assemble_polyglot_plugin_zip(
            plugin_name="gcp-finops-toolkit",
            display_name="GCP FinOps Toolkit",
            version="1.0.0",
            description="Enterprise GCP FinOps toolkit.",
            mcp_server_key="finops-mcp-server",
            mcp_server_url="http://127.0.0.1:8091/mcp",
            skill_markdowns={
                "cost-anomaly-triage": "---\nname: cost-anomaly-triage\ndescription: Triage spikes.\n---\n"
            },
        )
        assert hmac.compare_digest(hashlib.sha256(bundle_bytes).hexdigest(), bundle_sha)
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as zf:
            names = set(zf.namelist())
            for req in (
                "plugin.json",
                ".antigravity-plugin/plugin.json",
                ".claude-plugin/plugin.json",
                ".codex-plugin/plugin.json",
                "mcp.json",
                ".mcp.json",
                "mcp_config.json",
                "assets/logo.svg",
                "skills/cost-anomaly-triage/SKILL.md",
            ):
                assert req in names, f"Missing {req} in Polyglot ZIP"
        print(f"  [PASS] assemble_polyglot_plugin_zip (sha256={bundle_sha[:12]}...)")
        passed += 1
    except NotImplementedError as exc:
        print(f"  [TODO] {exc}")

    _ = (json, urljoin, urlparse, Any)
    if passed == total:
        print(f"  [DONE] Progress: {passed}/{total} checks passed — all Lab 03 checks passed!")
    else:
        print(f"  [INFO] Progress: {passed}/{total} checks passed")


if __name__ == "__main__":
    _run_self_check()
