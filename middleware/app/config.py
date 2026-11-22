"""Environment-driven configuration for the Polyglot Marketplace Middleware."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class MiddlewareSettings:
    """Runtime settings matching WORKSHOP_SPEC.json env_vars."""

    agent_registry_base_url: str = "http://127.0.0.1:8090/v1"
    gcp_project_id: str = "acme-agent-governance"
    gcp_location: str = "asia-southeast3"
    gcp_secondary_location: str = "asia-southeast1"
    agent_registry_location: str = ""
    agent_registry_skills_location: str = ""
    include_public_skills: bool = False
    middleware_public_url: str = "http://127.0.0.1:8080"
    require_kms_signature: bool = True
    kms_public_key_pem_path: str = ""
    kms_public_key_pem: str = ""
    kms_signing_key_version: str = ""
    agent_registry_auth_token: str = ""
    hub_auth_token: str = ""
    gcp_quota_project: str = ""
    allow_deprecated_skills: bool = True
    marketplace_name: str = "acme-enterprise-marketplace"
    marketplace_display_name: str = "Acme Enterprise Agent Marketplace"
    marketplace_description: str = (
        "Governed enterprise plugins, skills, MCP servers, and A2A agents "
        "backed by Google Cloud Agent Registry."
    )
    marketplace_owner_name: str = "Acme Platform Security"
    marketplace_owner_email: str = "platform-security@acme.example.com"
    marketplace_owner_url: str = "https://acme.example.com/platform"

    @property
    def effective_registry_location(self) -> str:
        return self.agent_registry_location.strip() or self.gcp_location

    @property
    def effective_skills_location(self) -> str:
        explicit = self.agent_registry_skills_location.strip()
        if explicit:
            return explicit
        if (
            "googleapis.com" in self.agent_registry_base_url.lower()
            and self.effective_registry_location not in ("global", "us", "eu")
        ):
            return "global"
        return self.effective_registry_location

    @property
    def effective_quota_project(self) -> str:
        return self.gcp_quota_project or self.gcp_project_id

    @property
    def normalized_public_url(self) -> str:
        return self.middleware_public_url.rstrip("/")

    @property
    def normalized_registry_base_url(self) -> str:
        return self.agent_registry_base_url.rstrip("/")

    @classmethod
    def from_env(cls) -> MiddlewareSettings:
        project_id = os.getenv("GCP_PROJECT_ID", "acme-agent-governance")
        return cls(
            agent_registry_base_url=os.getenv(
                "AGENT_REGISTRY_BASE_URL", "http://127.0.0.1:8090/v1"
            ),
            gcp_project_id=project_id,
            gcp_location=os.getenv("GCP_LOCATION", "asia-southeast3"),
            gcp_secondary_location=os.getenv("GCP_SECONDARY_LOCATION", "asia-southeast1"),
            agent_registry_location=os.getenv("AGENT_REGISTRY_LOCATION", ""),
            agent_registry_skills_location=os.getenv("AGENT_REGISTRY_SKILLS_LOCATION", ""),
            include_public_skills=_parse_bool(os.getenv("INCLUDE_PUBLIC_SKILLS"), False),
            middleware_public_url=os.getenv("MIDDLEWARE_PUBLIC_URL", "http://127.0.0.1:8080"),
            require_kms_signature=_parse_bool(os.getenv("REQUIRE_KMS_SIGNATURE"), True),
            kms_public_key_pem_path=os.getenv("KMS_PUBLIC_KEY_PEM_PATH", ""),
            kms_public_key_pem=os.getenv("KMS_PUBLIC_KEY_PEM", ""),
            kms_signing_key_version=os.getenv("KMS_SIGNING_KEY_VERSION", ""),
            agent_registry_auth_token=(
                os.getenv("AGENT_REGISTRY_AUTH_TOKEN", "").strip()
                or os.getenv("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
            ),
            hub_auth_token=os.getenv("HUB_AUTH_TOKEN", ""),
            gcp_quota_project=os.getenv("GCP_QUOTA_PROJECT", project_id),
            allow_deprecated_skills=_parse_bool(os.getenv("ALLOW_DEPRECATED_SKILLS"), True),
            marketplace_name=os.getenv("MARKETPLACE_NAME", "acme-enterprise-marketplace"),
            marketplace_display_name=os.getenv(
                "MARKETPLACE_DISPLAY_NAME", "Acme Enterprise Agent Marketplace"
            ),
            marketplace_description=os.getenv(
                "MARKETPLACE_DESCRIPTION",
                "Governed enterprise plugins, skills, MCP servers, and A2A agents "
                "backed by Google Cloud Agent Registry.",
            ),
            marketplace_owner_name=os.getenv("MARKETPLACE_OWNER_NAME", "Acme Platform Security"),
            marketplace_owner_email=os.getenv(
                "MARKETPLACE_OWNER_EMAIL", "platform-security@acme.example.com"
            ),
            marketplace_owner_url=os.getenv(
                "MARKETPLACE_OWNER_URL", "https://acme.example.com/platform"
            ),
        )
