"""Enterprise Platform Agent Orchestrator for Google Cloud Agent Registry (v1).

Demonstrates how enterprise platform agents (running on Vertex AI Agent Engine
or Cloud Run) consume approved Agents, MCP Servers, Auth Manager Bindings, and
Standalone Skills directly from Google Cloud Agent Registry in two modes:

1. **Live ADK Mode** (``mode="adk"``, requires ``google-adk[a2a,agent-identity,mcp]>=1.29.0``):
   - Registers ``GcpAuthProvider()`` with ``CredentialManager`` so
     ``AgentRegistry.get_mcp_toolset()`` and ``get_remote_a2a_agent()``
     automatically resolve Agent Registry Auth Manager ``Binding`` resources.
   - Wires ``AgentRegistry`` (``list_agents``, ``search_agents``,
     ``get_remote_a2a_agent``, ``list_mcp_servers``, ``search_mcp_servers``,
     ``get_mcp_toolset``) to an ADK ``LlmAgent``.
   - Wires ``GCPSkillRegistry`` + ``SkillToolset`` for on-demand semantic skill
     discovery and ``?alt=media`` ZIP loading.

2. **Local HTTP / Workshop Mode** (``mode="rest"``, zero ADK dependency):
   - Uses :class:`platform_agent.rest_consumer.PlatformAgentConsumer` over
     ``httpx`` against either live ``https://agentregistry.googleapis.com/v1``
     or the local workshop ``mock-registry`` (``http://127.0.0.1:8090/v1``),
     stripping ``x-goog-*`` and ``Authorization`` headers on cross-origin
     ``?alt=media`` 302 redirects, verifying ``sha256Hash``, and optionally
     verifying Cloud KMS ``EC_SIGN_P256_SHA256`` signatures.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import httpx

from platform_agent.rest_consumer import (
    APPROVED_SKILL_FILTER,
    DEFAULT_LOCATION,
    DEFAULT_PROJECT_ID,
    DEFAULT_REGISTRY_BASE_URL,
    DiscoveredCapabilities,
    PlatformAgentConsumer,
)

# Gracefully guard optional google-adk>=1.29.0 imports so this module can be
# inspected, tested, and run in Local HTTP / Workshop Mode without google-adk.
try:
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.auth.credential_manager import CredentialManager
    from google.adk.integrations.agent_identity import GcpAuthProvider
    from google.adk.integrations.agent_registry import AgentRegistry
    from google.adk.integrations.skill_registry import GCPSkillRegistry
    from google.adk.tools.skill_toolset import SkillToolset

    ADK_AVAILABLE = True
except ImportError:
    LlmAgent = None  # type: ignore[assignment]
    CredentialManager = None  # type: ignore[assignment]
    GcpAuthProvider = None  # type: ignore[assignment]
    AgentRegistry = None  # type: ignore[assignment]
    GCPSkillRegistry = None  # type: ignore[assignment]
    SkillToolset = None  # type: ignore[assignment]
    ADK_AVAILABLE = False

ADK_MIN_VERSION = "1.29.0"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_AGENT_NAME = "enterprise_platform_orchestrator"
DEFAULT_SYSTEM_INSTRUCTION = (
    "You are an enterprise platform orchestrator running with least-privilege "
    "Agent Registry Viewer access. Discover approved skills via SkillToolset "
    "before executing specialized runbooks, invoke registered MCP server tools "
    "using Auth Manager bindings, and delegate domain tasks to registered A2A "
    "sub-agents."
)


@dataclass(frozen=True)
class OrchestratorConfig:
    """Configuration for both Live ADK and Local HTTP / Workshop orchestrator modes."""

    project_id: str = DEFAULT_PROJECT_ID
    location: str = DEFAULT_LOCATION
    mode: Literal["adk", "rest"] = "rest"
    model: str = DEFAULT_MODEL
    agent_name: str = DEFAULT_AGENT_NAME
    instruction: str = DEFAULT_SYSTEM_INSTRUCTION
    registry_base_url: str = DEFAULT_REGISTRY_BASE_URL
    mcp_search_string: str = ""
    agent_search_string: str = ""
    skill_search_string: str = ""
    skill_filter: str = APPROVED_SKILL_FILTER
    mcp_server_names: tuple[str, ...] = field(default_factory=tuple)
    remote_agent_names: tuple[str, ...] = field(default_factory=tuple)
    source_agent_urn: str | None = None
    continue_uri: str | None = None
    require_kms_signature: bool = False
    kms_public_key_pem_path: str | None = None
    access_token: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("adk", "rest"):
            raise ValueError(f"Unsupported orchestrator mode {self.mode!r}; use 'adk' or 'rest'")
        if not self.project_id or not self.location:
            raise ValueError("project_id and location must be non-empty")

    @property
    def parent(self) -> str:
        return f"projects/{self.project_id}/locations/{self.location}"

    @classmethod
    def from_env(
        cls,
        *,
        mode: Literal["adk", "rest"] | None = None,
    ) -> OrchestratorConfig:
        """Build configuration from workshop / GCP environment variables."""
        resolved_mode = mode or (
            "adk"
            if os.environ.get("PLATFORM_AGENT_MODE", "rest").strip().lower() == "adk"
            else "rest"
        )
        project_id = (
            os.environ.get("GCP_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or DEFAULT_PROJECT_ID
        )
        location = (
            os.environ.get("GCP_LOCATION")
            or os.environ.get("GOOGLE_CLOUD_LOCATION")
            or DEFAULT_LOCATION
        )
        base_url = os.environ.get("AGENT_REGISTRY_BASE_URL", DEFAULT_REGISTRY_BASE_URL)
        require_sig = (
            os.environ.get("REQUIRE_KMS_SIGNATURE", "false").strip().lower()
            in {"1", "true", "yes"}
        )
        pem_path = os.environ.get("KMS_PUBLIC_KEY_PEM_PATH", "").strip() or None

        return cls(
            project_id=project_id,
            location=location,
            mode=resolved_mode,
            model=os.environ.get("PLATFORM_AGENT_MODEL", DEFAULT_MODEL),
            registry_base_url=base_url,
            source_agent_urn=os.environ.get("PLATFORM_AGENT_SOURCE_URN") or None,
            continue_uri=os.environ.get("OAUTH_CONTINUE_URI") or None,
            require_kms_signature=require_sig,
            kms_public_key_pem_path=pem_path,
            access_token=(
                os.environ.get("AGENT_REGISTRY_ACCESS_TOKEN")
                or os.environ.get("AGENT_REGISTRY_AUTH_TOKEN")
                or os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
                or None
            ),
        )


def register_gcp_auth_provider(
    *,
    credential_manager_cls: Any = None,
    gcp_auth_provider_cls: Any = None,
) -> bool:
    """Register ``GcpAuthProvider`` on ADK ``CredentialManager`` for Binding auto-resolution."""
    cm_cls = credential_manager_cls or CredentialManager
    provider_cls = gcp_auth_provider_cls or GcpAuthProvider
    if cm_cls is None or provider_cls is None:
        raise RuntimeError(
            "google-adk with agent-identity extra is not installed. "
            "Install with: pip install -r platform_agent/requirements.txt"
        )
    cm_cls.register_auth_provider(provider_cls())
    return True


def build_adk_agent_config(
    config: OrchestratorConfig | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build a declarative, spec-verifiable ADK Platform Agent configuration.

    Runs cleanly without ``google-adk`` installed so workshops, CI checks, and
    architecture reviews can validate exact ADK integration wiring, IAM posture,
    and Agent Registry ``v1`` endpoints.
    """
    cfg = config or OrchestratorConfig()
    if overrides:
        normalized = dict(overrides)
        if "mcp_server_names" in normalized and isinstance(
            normalized["mcp_server_names"], list
        ):
            normalized["mcp_server_names"] = tuple(normalized["mcp_server_names"])
        if "remote_agent_names" in normalized and isinstance(
            normalized["remote_agent_names"], list
        ):
            normalized["remote_agent_names"] = tuple(normalized["remote_agent_names"])
        cfg = replace(cfg, **normalized)

    return {
        "mode": cfg.mode,
        "adk_available": ADK_AVAILABLE,
        "adk_min_version": ADK_MIN_VERSION,
        "agent": {
            "name": cfg.agent_name,
            "model": cfg.model,
            "instruction": cfg.instruction,
            "deployment_targets": ["vertex-ai-agent-engine", "cloud-run"],
        },
        "registry": {
            "api_service": "agentregistry.googleapis.com",
            "api_version": "v1",
            "base_url": cfg.registry_base_url.rstrip("/"),
            "project_id": cfg.project_id,
            "location": cfg.location,
            "parent": cfg.parent,
        },
        "auth_manager": {
            "registration_call": "CredentialManager.register_auth_provider(GcpAuthProvider())",
            "auto_resolve_bindings": True,
            "binding_endpoint": f"{cfg.parent}/bindings:fetchAvailable",
            "required_iam_roles": [
                "roles/agentregistry.viewer",
                "roles/agentidentity.user",
            ],
            "continue_uri": cfg.continue_uri,
        },
        "integrations": {
            "agent_registry": {
                "import_path": "google.adk.integrations.agent_registry.AgentRegistry",
                "methods": [
                    "list_agents",
                    "search_agents",
                    "get_remote_a2a_agent",
                    "list_mcp_servers",
                    "search_mcp_servers",
                    "get_mcp_toolset",
                ],
                "mcp_search_string": cfg.mcp_search_string,
                "agent_search_string": cfg.agent_search_string,
                "mcp_server_names": list(cfg.mcp_server_names),
                "remote_agent_names": list(cfg.remote_agent_names),
            },
            "skill_registry": {
                "import_path": "google.adk.integrations.skill_registry.GCPSkillRegistry",
                "toolset_import_path": "google.adk.tools.skill_toolset.SkillToolset",
                "search_type": "SEMANTIC",
                "filter": cfg.skill_filter,
                "media_download_redirect_strips_headers": [
                    "x-goog-*",
                    "Authorization",
                ],
                "verify_sha256_hash": True,
                "require_kms_signature": cfg.require_kms_signature,
            },
        },
        "governance": {
            "recommended_runtime_role": "roles/agentregistry.viewer",
            "disallowed_runtime_roles": [
                "roles/agentregistry.editor",
                "roles/agentregistry.admin",
            ],
            "skill_filter": cfg.skill_filter,
            "enforce_zero_findings": "defaultRevisionFindingCount=0" in cfg.skill_filter,
            "egress_policy": "Agent Gateway + IAP v2 UAP (iap.googleapis.com/resources.egressViaIAP)",
        },
    }


def _call_with_optional_continue_uri(
    method: Any,
    resource_name: str,
    continue_uri: str | None,
) -> Any:
    """Call an ``AgentRegistry`` builder method across ``google-adk`` 1.29.0+ versions.

    ``continue_uri`` was added to ``get_mcp_toolset`` and ``get_remote_a2a_agent``
    in ``google-adk>=2.8.0``. Omit it whenever ``continue_uri is None``, and
    validate signature support when an explicit ``continue_uri`` is requested.
    """
    if continue_uri is None:
        return method(resource_name)
    params = inspect.signature(method).parameters
    accepts_continue_uri = "continue_uri" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if not accepts_continue_uri:
        method_label = getattr(method, "__qualname__", str(method))
        raise RuntimeError(
            f"{method_label} does not accept 'continue_uri' in the installed "
            "google-adk version; upgrade to google-adk>=2.8.0 or omit continue_uri."
        )
    return method(resource_name, continue_uri=continue_uri)


def build_live_adk_agent(
    config: OrchestratorConfig | None = None,
    *,
    registry_cls: Any = None,
    skill_registry_cls: Any = None,
    skill_toolset_cls: Any = None,
    llm_agent_cls: Any = None,
    credential_manager_cls: Any = None,
    gcp_auth_provider_cls: Any = None,
) -> Any:
    """Construct a live Google ADK ``LlmAgent`` backed by Agent Registry & Skill Registry.

    All ADK class arguments default to the real ``google.adk`` classes when
    installed, and can be injected in unit tests to verify exact wiring without
    live GCP credentials.
    """
    cfg = config or OrchestratorConfig(mode="adk")

    reg_cls = registry_cls or AgentRegistry
    sk_reg_cls = skill_registry_cls or GCPSkillRegistry
    sk_ts_cls = skill_toolset_cls or SkillToolset
    agent_cls = llm_agent_cls or LlmAgent
    cm_cls = credential_manager_cls or CredentialManager
    auth_prov_cls = gcp_auth_provider_cls or GcpAuthProvider

    missing = [
        label
        for label, obj in (
            ("AgentRegistry", reg_cls),
            ("GCPSkillRegistry", sk_reg_cls),
            ("SkillToolset", sk_ts_cls),
            ("LlmAgent", agent_cls),
            ("CredentialManager", cm_cls),
            ("GcpAuthProvider", auth_prov_cls),
        )
        if obj is None
    ]
    if missing:
        raise RuntimeError(
            f"Live ADK mode requires google-adk[a2a,agent-identity,mcp]>={ADK_MIN_VERSION} "
            f"(missing: {', '.join(missing)}). Install with: "
            "pip install -r platform_agent/requirements.txt, or use mode='rest'."
        )

    # 1. Enable automatic Auth Manager Binding resolution for MCP & A2A calls.
    register_gcp_auth_provider(
        credential_manager_cls=cm_cls,
        gcp_auth_provider_cls=auth_prov_cls,
    )

    # 2. Initialize Google Cloud Agent Registry client (v1).
    registry = reg_cls(project_id=cfg.project_id, location=cfg.location)

    # 3. Discover and materialize approved MCP Servers as ADK McpToolsets.
    mcp_names = list(cfg.mcp_server_names)
    if not mcp_names:
        mcp_response = (
            registry.search_mcp_servers(search_string=cfg.mcp_search_string)
            if cfg.mcp_search_string
            else registry.list_mcp_servers()
        )
        mcp_names = [
            srv["name"]
            for srv in mcp_response.get("mcpServers", [])
            if isinstance(srv, dict) and isinstance(srv.get("name"), str)
        ]

    mcp_toolsets = [
        _call_with_optional_continue_uri(
            registry.get_mcp_toolset, name, cfg.continue_uri
        )
        for name in mcp_names
    ]

    # 4. Discover and materialize approved A2A Agents as RemoteA2aAgent sub-agents.
    agent_names = list(cfg.remote_agent_names)
    if not agent_names:
        agents_response = (
            registry.search_agents(search_string=cfg.agent_search_string)
            if cfg.agent_search_string
            else registry.list_agents()
        )
        agent_names = [
            ag["name"]
            for ag in agents_response.get("agents", [])
            if isinstance(ag, dict) and isinstance(ag.get("name"), str)
        ]

    remote_agents = [
        _call_with_optional_continue_uri(
            registry.get_remote_a2a_agent, name, cfg.continue_uri
        )
        for name in agent_names
    ]

    # 5. Wire GCPSkillRegistry + SkillToolset for semantic search & on-demand ?alt=media load.
    prev_endpoint = os.environ.get("AGENT_REGISTRY_ENDPOINT")
    if cfg.registry_base_url:
        os.environ["AGENT_REGISTRY_ENDPOINT"] = cfg.registry_base_url.rstrip("/")
    try:
        skill_registry = sk_reg_cls(project_id=cfg.project_id, location=cfg.location)
    finally:
        if cfg.registry_base_url:
            if prev_endpoint is None:
                os.environ.pop("AGENT_REGISTRY_ENDPOINT", None)
            else:
                os.environ["AGENT_REGISTRY_ENDPOINT"] = prev_endpoint
    skill_toolset = sk_ts_cls(registry=skill_registry)

    # 6. Assemble the root ADK LlmAgent.
    return agent_cls(
        name=cfg.agent_name,
        model=cfg.model,
        instruction=cfg.instruction,
        tools=[skill_toolset, *mcp_toolsets],
        sub_agents=remote_agents,
    )


class PlatformAgentOrchestrator:
    """Dual-mode enterprise platform agent orchestrator (Live ADK or Local REST)."""

    def __init__(
        self,
        config: OrchestratorConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        kms_public_key_pem: bytes | str | None = None,
    ) -> None:
        self.config = config or OrchestratorConfig()
        self._transport = transport
        self._kms_public_key_pem = kms_public_key_pem

    def describe_config(self) -> dict[str, Any]:
        """Return the declarative ADK/REST configuration specification."""
        return build_adk_agent_config(self.config)

    def build_adk_agent(self, **injected_classes: Any) -> Any:
        """Build a Live ADK ``LlmAgent`` (requires ``mode='adk'`` or explicit call)."""
        return build_live_adk_agent(self.config, **injected_classes)

    def _load_kms_public_key(self) -> bytes | str | None:
        if self._kms_public_key_pem is not None:
            return self._kms_public_key_pem
        if self.config.kms_public_key_pem_path:
            return Path(self.config.kms_public_key_pem_path).read_bytes()
        return None

    async def discover_rest(
        self,
        *,
        fetch_skill_contents: bool = True,
    ) -> DiscoveredCapabilities:
        """Run Local HTTP / Workshop Mode discovery via ``PlatformAgentConsumer``."""
        kms_pem = self._load_kms_public_key()
        async with PlatformAgentConsumer(
            project_id=self.config.project_id,
            location=self.config.location,
            base_url=self.config.registry_base_url,
            access_token=self.config.access_token,
            kms_public_key_pem=kms_pem,
            require_kms_signature=self.config.require_kms_signature,
            transport=self._transport,
        ) as consumer:
            return await consumer.discover_capabilities(
                skill_query=self.config.skill_search_string,
                skill_search_type="SEMANTIC",
                skill_filter=self.config.skill_filter,
                mcp_query=self.config.mcp_search_string,
                agent_query=self.config.agent_search_string,
                source_agent_urn=self.config.source_agent_urn,
                fetch_skill_contents=fetch_skill_contents,
                verify_kms_signature=self.config.require_kms_signature,
            )

    async def run(self, *, fetch_skill_contents: bool = True) -> Any:
        """Execute according to ``config.mode`` (``'adk'`` or ``'rest'``)."""
        if self.config.mode == "adk":
            return self.build_adk_agent()
        return await self.discover_rest(fetch_skill_contents=fetch_skill_contents)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enterprise Platform Agent orchestrator consuming Google Cloud Agent Registry (v1)."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("rest", "adk"),
        default=None,
        help="Execution mode: 'rest' (local/mock or direct v1 HTTP) or 'adk' (Live Google ADK).",
    )
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--location", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--skill-query", default="")
    parser.add_argument("--mcp-query", default="")
    parser.add_argument("--agent-query", default="")
    parser.add_argument("--source-agent-urn", default=None)
    parser.add_argument(
        "--no-download-skills",
        action="store_true",
        help="Only search skills metadata without downloading ?alt=media ZIP payloads.",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print declarative ADK/REST configuration JSON and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    base_config = OrchestratorConfig.from_env(mode=args.mode)
    overrides: dict[str, Any] = {
        "skill_search_string": args.skill_query,
        "mcp_search_string": args.mcp_query,
        "agent_search_string": args.agent_query,
    }
    if args.project_id is not None:
        overrides["project_id"] = args.project_id
    if args.location is not None:
        overrides["location"] = args.location
    if args.base_url is not None:
        overrides["registry_base_url"] = args.base_url
    if args.source_agent_urn is not None:
        overrides["source_agent_urn"] = args.source_agent_urn
    config = replace(base_config, **overrides)
    orchestrator = PlatformAgentOrchestrator(config)
    if args.print_config:
        print(json.dumps(orchestrator.describe_config(), indent=2))
        return 0

    result = asyncio.run(
        orchestrator.run(fetch_skill_contents=not args.no_download_skills)
    )
    if isinstance(result, DiscoveredCapabilities):
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(
            json.dumps(
                {
                    "status": "built_adk_agent",
                    "name": getattr(result, "name", config.agent_name),
                    "model": config.model,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
