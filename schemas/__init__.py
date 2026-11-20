"""Shared JSON schemas and multi-IDE plugin/marketplace validators."""

from schemas.validators import (
    SCHEMAS_DIR,
    ValidationResult,
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
    validate_skill_md,
)

__all__ = [
    "SCHEMAS_DIR",
    "ValidationResult",
    "load_schema",
    "parse_frontmatter",
    "validate_agent_plugin_bundle",
    "validate_antigravity_bundle",
    "validate_antigravity_marketplace",
    "validate_claude_marketplace",
    "validate_claude_plugin_bundle",
    "validate_codex_marketplace",
    "validate_codex_plugin_bundle",
    "validate_copilot_marketplace",
    "validate_copilot_plugin_bundle",
    "validate_skill_md",
]
