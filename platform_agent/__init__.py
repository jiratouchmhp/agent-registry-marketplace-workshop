"""Enterprise Platform Agent consumer & ADK orchestrator for Google Cloud Agent Registry (v1).

Demonstrates how platform agents running on Vertex AI Agent Engine or Cloud Run
discover and consume approved Agents, MCP Servers, Auth Manager Bindings, and
Standalone Skills directly from Google Cloud Agent Registry.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from platform_agent.rest_consumer import (
    APPROVED_SKILL_FILTER,
    DEFAULT_REGISTRY_BASE_URL,
    PRIVATE_SKILL_PREFIX,
    DiscoveredCapabilities,
    ExtractedSkill,
    PlatformAgentConsumer,
    SkillGovernanceError,
    SkillIntegrityError,
    SkillSignatureError,
    compute_canonical_skill_digest,
    extract_skill_zip,
    verify_kms_ecdsa_p256_signature,
)

_ORCHESTRATOR_EXPORTS = frozenset(
    {
        "ADK_AVAILABLE",
        "OrchestratorConfig",
        "PlatformAgentOrchestrator",
        "build_adk_agent_config",
        "build_live_adk_agent",
        "register_gcp_auth_provider",
    }
)

__all__ = [
    "ADK_AVAILABLE",
    "APPROVED_SKILL_FILTER",
    "DEFAULT_REGISTRY_BASE_URL",
    "PRIVATE_SKILL_PREFIX",
    "DiscoveredCapabilities",
    "ExtractedSkill",
    "OrchestratorConfig",
    "PlatformAgentConsumer",
    "PlatformAgentOrchestrator",
    "SkillGovernanceError",
    "SkillIntegrityError",
    "SkillSignatureError",
    "build_adk_agent_config",
    "build_live_adk_agent",
    "compute_canonical_skill_digest",
    "extract_skill_zip",
    "register_gcp_auth_provider",
    "verify_kms_ecdsa_p256_signature",
]


def __getattr__(name: str) -> Any:
    """Lazily expose orchestrator symbols so ``python -m platform_agent.adk_orchestrator`` runs cleanly."""
    if name in _ORCHESTRATOR_EXPORTS:
        module = import_module("platform_agent.adk_orchestrator")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
