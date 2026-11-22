"""Unit tests for WORKSHOP_SPEC.json, official JSON schemas, and all bundle/marketplace validators."""

from __future__ import annotations

import json
import struct
import tomllib
import zlib
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from schemas import (
    load_schema,
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

REPO_ROOT = Path(__file__).resolve().parent.parent
VALID_SHA256 = "a" * 64


def _make_png_bytes(width: int, height: int) -> bytes:
    """Create minimal valid PNG bytes with the requested IHDR dimensions."""

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_scanlines = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw_scanlines))
        + chunk(b"IEND", b"")
    )


def _create_valid_multitool_bundle(base: Path) -> Path:
    """Create a complete multi-tool plugin bundle passing Antigravity & Agent Plugins 1.0."""
    plugin_dir = base / "gcp-finops-toolkit"
    (plugin_dir / ".antigravity-plugin").mkdir(parents=True)
    (plugin_dir / "assets").mkdir()
    (plugin_dir / "skills" / "cost-anomaly-triage").mkdir(parents=True)
    (plugin_dir / "skills" / "bq-query-optimizer").mkdir(parents=True)
    (plugin_dir / "rules").mkdir()
    (plugin_dir / "scripts").mkdir()
    (plugin_dir / "agents").mkdir()

    # Root Agent Plugins 1.0 manifest
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "gcp-finops-toolkit",
                "version": "1.0.0",
                "description": "Enterprise GCP FinOps toolkit for cost triage and BigQuery tuning.",
                "author": {
                    "name": "Acme Platform Security",
                    "email": "platform-security@acme.example.com",
                    "url": "https://acme.example.com/platform",
                },
                "license": "Apache-2.0",
                "keywords": ["gcp", "finops", "bigquery"],
                "extensions": {"com.openai": {"category": "Cloud Operations"}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Antigravity v1 manifest (.antigravity-plugin/plugin.json takes precedence)
    (plugin_dir / ".antigravity-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://antigravity.google/schemas/v1/plugin.json",
                "name": "gcp-finops-toolkit",
                "displayName": "GCP FinOps Toolkit",
                "description": "Enterprise GCP FinOps toolkit for cost triage and BigQuery tuning.",
                "version": "1.0.0",
                "logo": "assets/logo.svg",
                "suggestedPrompts": [
                    "Triage yesterday's BigQuery billing spike",
                    "Optimize this BigQuery SQL query for bytes scanned",
                ],
                "author": {
                    "name": "Acme Platform Security",
                    "email": "platform-security@acme.example.com",
                    "url": "https://acme.example.com/platform",
                },
                "license": "Apache-2.0",
                "keywords": ["gcp", "finops"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Local 1:1 square SVG logo
    (plugin_dir / "assets" / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
        '<rect width="64" height="64" rx="12" fill="#1a73e8"/></svg>\n',
        encoding="utf-8",
    )

    # Agent Plugins 1.0 root mcp.json (no ${ENV_VAR} expansion in remote url/headers)
    (plugin_dir / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "finops-mcp-server": {
                        "type": "streamable-http",
                        "url": "http://127.0.0.1:8091/mcp",
                    }
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Antigravity mcp_config.json (sanitized key, no root variables, no PLUGIN_ROOT in env)
    (plugin_dir / "mcp_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "finops-mcp-server": {
                        "url": "http://127.0.0.1:8091/mcp",
                        "authProviderType": "google_credentials",
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Skills (uppercase SKILL.md, folder == frontmatter name, scalar string metadata)
    for skill_name, desc in [
        ("cost-anomaly-triage", "Investigate unexpected GCP billing spikes safely."),
        ("bq-query-optimizer", "Analyze BigQuery SQL plans to reduce bytes scanned."),
    ]:
        (plugin_dir / "skills" / skill_name / "SKILL.md").write_text(
            "---\n"
            f"name: {skill_name}\n"
            f"description: {desc}\n"
            "metadata:\n"
            '  plugin_name: "gcp-finops-toolkit"\n'
            '  plugin_version: "1.0.0"\n'
            "---\n\n"
            f"# {skill_name}\n\nFollow the governed workflow.\n",
            encoding="utf-8",
        )

    # Antigravity rules
    (plugin_dir / "rules" / "AGENTS.md").write_text(
        "# FinOps Guardrails\n\nNever run destructive billing mutations without approval.\n",
        encoding="utf-8",
    )
    (plugin_dir / "rules" / "sql-guard.md").write_text(
        "---\n"
        "trigger: glob\n"
        'globs: "**/*.sql, **/*.tf"\n'
        "---\n\n"
        "Always check partition filters before running BigQuery SQL.\n",
        encoding="utf-8",
    )

    # Executable 0755 helper script & named hooks.json
    guard_script = plugin_dir / "scripts" / "policy_guard.sh"
    guard_script.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n", encoding="utf-8")
    guard_script.chmod(0o755)

    (plugin_dir / "hooks.json").write_text(
        json.dumps(
            {
                "policy-guard": {
                    "enabled": True,
                    "PreToolUse": [
                        {
                            "matcher": "mcp_finops-mcp-server_.*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "./scripts/policy_guard.sh",
                                    "timeout": 10,
                                }
                            ],
                        }
                    ],
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Subagent definition
    (plugin_dir / "agents" / "finops-advisor.md").write_text(
        "---\n"
        "name: finops-advisor\n"
        "description: Read-only FinOps cost anomaly investigator.\n"
        "subagent: true\n"
        "inheritMcp: true\n"
        "---\n\n"
        "Analyze billing anomalies and cite exact SKU deltas.\n",
        encoding="utf-8",
    )

    return plugin_dir


# ---------------------------------------------------------------------------
# 1. Foundation contract & schema integrity tests
# ---------------------------------------------------------------------------


def test_workshop_spec_contract() -> None:
    spec_path = REPO_ROOT / "WORKSHOP_SPEC.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    assert spec["project_defaults"] == {
        "project_id": "acme-agent-governance",
        "project_number": "123456789012",
        "location": "asia-southeast3",
        "fallback_region": "asia-southeast1",
        "skills_location": "global",
        "auth_provider_location": "asia-southeast1",
        "cmek_location": "us",
    }
    assert spec["ports"] == {
        "mock_registry": 8090,
        "sample_mcp_server": 8091,
        "sample_a2a_agent": 8092,
        "middleware": 8080,
    }
    sample = spec["sample_plugin"]
    assert sample["name"] == "gcp-finops-toolkit"
    assert sample["display_name"] == "GCP FinOps Toolkit"
    assert sample["version"] == "1.0.0"
    assert sample["skills"] == ["cost-anomaly-triage", "bq-query-optimizer"]
    assert sample["mcp_service_id"] == "finops-mcp-server"
    assert sample["agent_service_id"] == "finops-advisor-agent"
    assert sample["endpoint_service_id"] == "finops-billing-api"

    assert set(spec["middleware_endpoints"]) == {
        "/healthz",
        "/v1/catalog",
        "/v1/marketplace/claude.json",
        "/v1/marketplace/copilot.json",
        "/v1/marketplace/codex.json",
        "/v1/marketplace/antigravity.json",
        "/v1/plugins/{plugin_name}.zip",
        "/mcp",
    }
    assert spec["skill_metadata_keys"] == [
        "plugin_name",
        "plugin_display_name",
        "plugin_version",
        "source_repo",
        "git_commit",
        "mcp_service_id",
        "agent_service_id",
        "kms_signature",
        "kms_key_version",
    ]
    assert set(spec["env_vars"]) == {
        "AGENT_REGISTRY_BASE_URL",
        "GCP_PROJECT_ID",
        "GCP_LOCATION",
        "AGENT_REGISTRY_SKILLS_LOCATION",
        "MIDDLEWARE_PUBLIC_URL",
        "REQUIRE_KMS_SIGNATURE",
        "KMS_PUBLIC_KEY_PEM_PATH",
        "HUB_MIDDLEWARE_URL",
        "HUB_AUTH_TOKEN",
    }
    assert spec["env_vars"]["GCP_LOCATION"] == "asia-southeast3"
    assert spec["env_vars"]["AGENT_REGISTRY_SKILLS_LOCATION"] == "global"
    assert spec["env_vars"]["HUB_MIDDLEWARE_URL"] == "http://127.0.0.1:8080"
    assert ":locations:asia-southeast3:" in spec["urns"]["mcp_server"]
    assert "/locations/asia-southeast3/" in spec["resource_names"]["mcp_service"]


def test_pyproject_toml_valid() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "agent-registry-marketplace-workshop"
    assert data["project"]["requires-python"] == ">=3.12"
    deps_blob = " ".join(data["project"]["dependencies"]).lower()
    for pkg in (
        "fastapi",
        "uvicorn",
        "httpx",
        "pyyaml",
        "cryptography",
        "click",
        "jsonschema",
        "pytest",
    ):
        assert pkg in deps_blob
    assert data["tool"]["pytest"]["ini_options"]["pythonpath"] == ["."]


@pytest.mark.parametrize(
    "schema_file",
    [
        "antigravity_plugin_v1.schema.json",
        "agent_plugin_v1.schema.json",
        "agent_plugin_mcp_v1.schema.json",
    ],
)
def test_json_schemas_are_valid_draft_2020_12(schema_file: str) -> None:
    schema = load_schema(schema_file)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False


# ---------------------------------------------------------------------------
# 2. Antigravity bundle validator tests (valid + all 7 pre-publish pitfalls)
# ---------------------------------------------------------------------------


def test_antigravity_bundle_valid_multitool(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)
    ag_res = validate_antigravity_bundle(bundle)
    ap_res = validate_agent_plugin_bundle(bundle)

    assert ag_res.valid, f"Unexpected Antigravity errors: {ag_res.errors}"
    assert bool(ag_res) is True
    assert ap_res.valid, f"Unexpected Agent Plugins 1.0 errors: {ap_res.errors}"


def test_antigravity_bundle_pitfall1_naming_and_schema(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)
    manifest_path = bundle / ".antigravity-plugin" / "plugin.json"

    # Non-kebab-case + branded prefix + unknown property + >3 suggestedPrompts + http author.url
    bad_manifest = {
        "$schema": "https://antigravity.google/schemas/v1/plugin.json",
        "name": "Antigravity_FinOps",
        "displayName": "Gemini FinOps Toolkit",
        "logo": "assets/logo.svg",
        "suggestedPrompts": ["one", "two", "three", "four"],
        "author": {"name": "Acme", "url": "http://acme.example.com"},
        "sourcePath": "/legacy/path",
        "unexpectedField": True,
    }
    manifest_path.write_text(json.dumps(bad_manifest), encoding="utf-8")

    res = validate_antigravity_bundle(bundle)
    assert not res.valid
    joined = "\n".join(res.errors)
    assert "kebab-case" in joined
    assert "branded prefix 'antigravity'" in joined
    assert "branded prefix 'gemini'" in joined
    assert "unexpectedField" in joined
    assert "deprecated field 'sourcePath'" in joined
    assert "https://" in joined
    assert "suggestedPrompts" in joined


def test_antigravity_bundle_pitfall2_skill_folder_mismatch_and_metadata(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)
    skill_md = bundle / "skills" / "cost-anomaly-triage" / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: wrong-skill-name\n"
        "description: Mismatched skill name.\n"
        "metadata:\n"
        "  bad_nested: [1, 2]\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )

    res = validate_antigravity_bundle(bundle)
    assert not res.valid
    joined = "\n".join(res.errors)
    assert "must exactly match skill folder name" in joined
    assert "scalar string" in joined


def test_antigravity_bundle_pitfall3_rule_globs_must_be_string(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)
    rule_md = bundle / "rules" / "sql-guard.md"
    rule_md.write_text(
        "---\n"
        "trigger: glob\n"
        "globs:\n"
        '  - "**/*.sql"\n'
        '  - "**/*.tf"\n'
        "---\n\n"
        "Never use YAML array for globs.\n",
        encoding="utf-8",
    )

    res = validate_antigravity_bundle(bundle)
    assert not res.valid
    assert any("comma-separated string" in err for err in res.errors)


def test_antigravity_bundle_pitfall4_mcp_config_rules(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)
    mcp_cfg = bundle / "mcp_config.json"

    # Valid scoped variables inside server entry should pass
    mcp_cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "finops-mcp-server": {
                        "url": "http://127.0.0.1:8091/mcp",
                        "variables": [{"name": "TENANT_ID", "description": "Tenant identifier"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert validate_antigravity_bundle(bundle).valid

    # Invalid: root variables, dotted server key, PLUGIN_ROOT & PLUGIN_DATA in env
    mcp_cfg.write_text(
        json.dumps(
            {
                "variables": [{"name": "BAD_ROOT_VAR"}],
                "mcpServers": {
                    "finops.mcp.server": {
                        "command": "python3",
                        "env": {
                            "PLUGIN_ROOT": "/override",
                            "PLUGIN_DATA": "/data",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    res = validate_antigravity_bundle(bundle)
    assert not res.valid
    joined = "\n".join(res.errors)
    assert "top-level 'variables' is forbidden" in joined
    assert "finops.mcp.server" in joined
    assert "PLUGIN_ROOT" in joined
    assert "PLUGIN_DATA" in joined


def test_antigravity_bundle_pitfall5_hooks_named_group_and_relative_paths(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)
    hooks_json = bundle / "hooks.json"

    # Bare event key at top level
    hooks_json.write_text(
        json.dumps(
            {
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [{"type": "command", "command": "./scripts/policy_guard.sh"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    res_bare = validate_antigravity_bundle(bundle)
    assert not res_bare.valid
    assert any("named top-level group" in err for err in res_bare.errors)

    # Named group but uses ${PLUGIN_ROOT}, bare scripts/, and missing ./scripts/
    hooks_json.write_text(
        json.dumps(
            {
                "policy-guard": {
                    "enabled": True,
                    "PreToolUse": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {"type": "command", "command": "${PLUGIN_ROOT}/scripts/policy_guard.sh"},
                                {"type": "command", "command": "scripts/policy_guard.sh"},
                                {"type": "command", "command": "./scripts/does_not_exist.sh"},
                            ],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    res_paths = validate_antigravity_bundle(bundle)
    assert not res_paths.valid
    joined = "\n".join(res_paths.errors)
    assert "${PLUGIN_ROOT}" in joined
    assert "must start with './scripts/'" in joined
    assert "does_not_exist.sh" in joined


def test_antigravity_bundle_pitfall6_logo_exists_and_square(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)
    manifest_path = bundle / ".antigravity-plugin" / "plugin.json"

    # Valid 1:1 square PNG passes
    square_png = bundle / "assets" / "logo.png"
    square_png.write_bytes(_make_png_bytes(32, 32))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["logo"] = "assets/logo.png"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert validate_antigravity_bundle(bundle).valid

    # Non-square PNG fails
    square_png.write_bytes(_make_png_bytes(64, 32))
    res_rect_png = validate_antigravity_bundle(bundle)
    assert not res_rect_png.valid
    assert any("1:1 square" in err for err in res_rect_png.errors)

    # Non-square SVG fails
    manifest["logo"] = "assets/logo.svg"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (bundle / "assets" / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 60"></svg>',
        encoding="utf-8",
    )
    res_rect_svg = validate_antigravity_bundle(bundle)
    assert not res_rect_svg.valid
    assert any("1:1 square" in err for err in res_rect_svg.errors)

    # Missing logo file fails
    (bundle / "assets" / "logo.svg").unlink()
    res_missing = validate_antigravity_bundle(bundle)
    assert not res_missing.valid
    assert any("Logo file does not exist" in err for err in res_missing.errors)


def test_antigravity_bundle_pitfall7_scripts_executable_0755(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)
    script = bundle / "scripts" / "policy_guard.sh"
    script.chmod(0o644)

    res = validate_antigravity_bundle(bundle)
    assert not res.valid
    assert any("0755" in err for err in res.errors)


# ---------------------------------------------------------------------------
# 3. Agent Plugins 1.0 bundle validator tests
# ---------------------------------------------------------------------------


def test_agent_plugin_bundle_invalid_manifest_and_mcp_env_expansion(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)

    # Disallowed ${ENV_VAR} expansion in remote streamable-http headers & url
    (bundle / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "finops-mcp-server": {
                        "type": "streamable-http",
                        "url": "https://${HOST}/mcp",
                        "headers": {"Authorization": "Bearer ${HUB_AUTH_TOKEN}"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    res_mcp = validate_agent_plugin_bundle(bundle)
    assert not res_mcp.valid
    assert any("url must not use ${ENV_VAR}" in err for err in res_mcp.errors)
    assert any("header 'Authorization' must not use ${ENV_VAR}" in err for err in res_mcp.errors)

    # Invalid plugin.json missing $schema & bad name
    (bundle / "plugin.json").write_text(
        json.dumps({"name": "Bad--Name", "unknownKey": 123}),
        encoding="utf-8",
    )
    res_manifest = validate_agent_plugin_bundle(bundle)
    assert not res_manifest.valid
    joined = "\n".join(res_manifest.errors)
    assert "$schema" in joined
    assert "Bad--Name" in joined or "name" in joined
    assert "unknownKey" in joined


# ---------------------------------------------------------------------------
# 4. Marketplace catalog validator tests (Claude, Copilot, Codex, Antigravity)
# ---------------------------------------------------------------------------


def test_claude_marketplace_valid_and_invalid(tmp_path: Path) -> None:
    valid_path = tmp_path / "claude.json"
    valid_payload: dict[str, Any] = {
        "name": "acme-enterprise-marketplace",
        "owner": {
            "name": "Acme Platform Security",
            "email": "platform-security@acme.example.com",
        },
        "metadata": {
            "description": "Governed enterprise plugins",
            "version": "1.0.0",
            "pluginRoot": "./plugins",
        },
        "plugins": [
            {
                "name": "gcp-finops-toolkit",
                "description": "GCP FinOps toolkit",
                "version": "1.0.0",
                "source": "./plugins/gcp-finops-toolkit",
            }
        ],
    }
    valid_path.write_text(json.dumps(valid_payload), encoding="utf-8")

    assert validate_claude_marketplace(valid_path).valid
    # Relative source is forbidden for HTTP-served Claude marketplaces
    res_remote = validate_claude_marketplace(valid_path, is_remote_http=True)
    assert not res_remote.valid
    assert any("relative/local paths" in err for err in res_remote.errors)

    # Invalid missing owner and duplicate plugins
    bad_payload = {
        "name": "acme",
        "plugins": [
            {"name": "dup", "source": "https://example.com/a"},
            {"name": "dup", "source": ""},
        ],
    }
    res_bad = validate_claude_marketplace(bad_payload)
    assert not res_bad.valid
    joined = "\n".join(res_bad.errors)
    assert "owner" in joined
    assert "duplicate plugin name" in joined


def test_copilot_marketplace_valid_and_invalid(tmp_path: Path) -> None:
    valid_path = tmp_path / "copilot.json"
    valid_payload = {
        "name": "acme-enterprise-marketplace",
        "owner": {"name": "Acme Platform Security", "email": "sec@acme.example.com"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [
            {
                "name": "gcp-finops-toolkit",
                "description": "GCP FinOps toolkit",
                "version": "1.0.0",
                "source": "./plugins/gcp-finops-toolkit",
                "category": "Cloud Operations",
                "tags": ["gcp", "finops"],
            }
        ],
    }
    valid_path.write_text(json.dumps(valid_payload), encoding="utf-8")
    assert validate_copilot_marketplace(valid_path).valid

    bad_payload = {
        "name": "",
        "owner": {"email": "missing-name@acme.example.com"},
        "plugins": [{"name": "p1", "source": "./p1", "tags": "not-a-list"}],
    }
    res_bad = validate_copilot_marketplace(bad_payload)
    assert not res_bad.valid
    joined = "\n".join(res_bad.errors)
    assert "'name'" in joined
    assert "owner.name" in joined
    assert "tags" in joined


def test_codex_marketplace_valid_and_invalid(tmp_path: Path) -> None:
    valid_path = tmp_path / "codex.json"
    valid_payload = {
        "name": "acme-enterprise-marketplace",
        "interface": {"displayName": "Acme Enterprise Agent Marketplace"},
        "plugins": [
            {
                "name": "gcp-finops-toolkit",
                "source": {"source": "local", "path": "./plugins/gcp-finops-toolkit"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Cloud Operations",
            }
        ],
    }
    valid_path.write_text(json.dumps(valid_payload), encoding="utf-8")
    assert validate_codex_marketplace(valid_path).valid

    bad_payload = {
        "name": "acme",
        "interface": {},
        "plugins": [
            {
                "name": "gcp-finops-toolkit",
                "source": {"source": "unknown"},
                "policy": {"installation": "ALWAYS", "authentication": "NEVER"},
                "category": "",
            }
        ],
    }
    res_bad = validate_codex_marketplace(bad_payload)
    assert not res_bad.valid
    joined = "\n".join(res_bad.errors)
    assert "interface.displayName" in joined
    assert "source.source" in joined
    assert "policy.installation" in joined
    assert "policy.authentication" in joined
    assert "category" in joined


def test_antigravity_marketplace_valid_and_invalid(tmp_path: Path) -> None:
    valid_path = tmp_path / "antigravity.json"
    valid_payload = {
        "name": "acme-enterprise-marketplace",
        "description": "Governed enterprise plugins backed by Google Cloud Agent Registry.",
        "plugins": [
            {
                "name": "gcp-finops-toolkit",
                "displayName": "GCP FinOps Toolkit",
                "description": "FinOps skills and MCP tools.",
                "version": "1.0.0",
                "source": {
                    "type": "archive",
                    "url": "https://hub.acme.example.com/v1/plugins/gcp-finops-toolkit.zip",
                    "sha256": VALID_SHA256,
                },
            },
            {
                "name": "local-dev-helper",
                "displayName": "Local Dev Helper",
                "description": "Localhost archive mode check.",
                "version": "0.1.0",
                "source": {
                    "type": "archive",
                    "url": "http://127.0.0.1:8080/v1/plugins/local-dev-helper.zip",
                    "sha256": VALID_SHA256,
                },
            },
        ],
    }
    valid_path.write_text(json.dumps(valid_payload), encoding="utf-8")
    assert validate_antigravity_marketplace(valid_path).valid

    bad_payload = {
        "name": "acme",
        "description": "",
        "plugins": [
            {
                "name": "antigravity-bad",
                "displayName": "Claude Helper",
                "source": {
                    "type": "archive",
                    "url": "http://remote.example.com/plugin.tar.gz",
                    "sha256": "not-a-sha256",
                },
            }
        ],
    }
    res_bad = validate_antigravity_marketplace(bad_payload)
    assert not res_bad.valid
    joined = "\n".join(res_bad.errors)
    assert "description" in joined
    assert "branded prefix 'antigravity'" in joined
    assert "branded prefix 'claude'" in joined
    assert "https://" in joined
    assert "64-character lowercase hex" in joined


# ---------------------------------------------------------------------------
# 5. Regression tests for hooks.json flat vs matcher events, remote sources,
#    direct manifest["skills"], mcp_config enhancements, and 5-IDE bundles
# ---------------------------------------------------------------------------


def test_antigravity_hooks_flat_vs_matcher_events_and_prompt_handlers(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)
    stop_script = bundle / "scripts" / "stop.sh"
    stop_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    stop_script.chmod(0o755)
    hooks_json = bundle / "hooks.json"

    # Standard Antigravity hooks.json structure + SessionStart/PreInvocation/PostInvocation + prompt
    hooks_json.write_text(
        json.dumps(
            {
                "policy-guard": {
                    "enabled": True,
                    "PreToolUse": [
                        {
                            "matcher": "mcp_finops-mcp-server_.*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "./scripts/policy_guard.sh",
                                    "timeout": 10,
                                }
                            ],
                        }
                    ],
                    "PostToolUse": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {"type": "prompt", "prompt": "Summarize tool output safely."}
                            ],
                        }
                    ],
                    "Stop": [
                        {"type": "command", "command": "./scripts/stop.sh", "timeout": 15}
                    ],
                    "SessionStart": [
                        {"type": "prompt", "prompt": "Check FinOps budget guardrails."}
                    ],
                    "PreInvocation": [
                        {"type": "command", "command": "./scripts/policy_guard.sh"}
                    ],
                    "PostInvocation": [
                        {"type": "command", "command": "./scripts/stop.sh"}
                    ],
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    res_ok = validate_antigravity_bundle(bundle)
    assert res_ok.valid, f"Expected valid hooks.json, got errors: {res_ok.errors}"

    # Invalid wrapped Stop must FAIL
    hooks_json.write_text(
        json.dumps(
            {
                "policy-guard": {
                    "enabled": True,
                    "Stop": [
                        {
                            "matcher": ".*",
                            "hooks": [{"type": "command", "command": "./scripts/stop.sh"}],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    res_wrapped_stop = validate_antigravity_bundle(bundle)
    assert not res_wrapped_stop.valid
    assert any("must be a flat handler object" in err for err in res_wrapped_stop.errors)

    # Bare top-level PreInvocation / PostInvocation must FAIL
    hooks_json.write_text(
        json.dumps(
            {
                "PreInvocation": [{"type": "command", "command": "./scripts/policy_guard.sh"}],
                "PostInvocation": [{"type": "command", "command": "./scripts/stop.sh"}],
            }
        ),
        encoding="utf-8",
    )
    res_bare_inv = validate_antigravity_bundle(bundle)
    assert not res_bare_inv.valid
    joined_bare = "\n".join(res_bare_inv.errors)
    assert "PreInvocation" in joined_bare
    assert "PostInvocation" in joined_bare

    # Unknown event key, ${CLAUDE_PLUGIN_ROOT}, dual command+prompt, empty prompt, bad timeouts
    hooks_json.write_text(
        json.dumps(
            {
                "policy-guard": {
                    "enabled": True,
                    "UnknownEvent": [],
                    "Stop": [
                        {
                            "type": "command",
                            "command": "${CLAUDE_PLUGIN_ROOT}/scripts/stop.sh",
                            "timeout": True,
                        },
                        {
                            "type": "prompt",
                            "prompt": "Both set",
                            "command": "./scripts/stop.sh",
                            "timeout": 0,
                        },
                        {
                            "type": "prompt",
                            "prompt": "   ",
                            "timeout": -5,
                        },
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    res_bad = validate_antigravity_bundle(bundle)
    assert not res_bad.valid
    joined = "\n".join(res_bad.errors)
    assert "unknown event 'UnknownEvent'" in joined
    assert "${CLAUDE_PLUGIN_ROOT}" in joined
    assert "must not specify both 'command' and 'prompt'" in joined
    assert "missing non-empty 'prompt'" in joined
    assert joined.count("timeout must be a positive integer") == 3


def test_claude_and_copilot_remote_http_and_dict_sources() -> None:
    # Bare relative path must fail when is_remote_http=True for both Claude & Copilot
    bare_rel_payload = {
        "name": "acme-mkt",
        "owner": {"name": "Acme Sec"},
        "plugins": [{"name": "p1", "source": "plugins/p1"}],
    }
    res_claude_bare = validate_claude_marketplace(bare_rel_payload, is_remote_http=True)
    assert not res_claude_bare.valid
    assert any("http:// or https:// URL" in err for err in res_claude_bare.errors)

    res_copilot_bare = validate_copilot_marketplace(bare_rel_payload, is_remote_http=True)
    assert not res_copilot_bare.valid
    assert any("http:// or https:// URL" in err for err in res_copilot_bare.errors)

    # Valid HTTP URL string and complete dict sources pass when is_remote_http=True
    valid_remote = {
        "name": "acme-mkt",
        "owner": {"name": "Acme Sec"},
        "plugins": [
            {"name": "p-str", "source": "https://hub.acme.example.com/plugins/p1.zip"},
            {"name": "p-url", "source": {"source": "url", "url": "https://example.com/repo.git"}},
            {
                "name": "p-subdir",
                "source": {
                    "source": "git-subdir",
                    "url": "https://example.com/mono.git",
                    "path": "plugins/p1",
                },
            },
            {"name": "p-gh", "source": {"source": "github", "repo": "acme/finops-toolkit"}},
            {"name": "p-npm", "source": {"source": "npm", "package": "@acme/finops-plugin"}},
            {"name": "p-pip", "source": {"source": "pip", "package": "acme-finops-plugin"}},
        ],
    }
    assert validate_claude_marketplace(valid_remote, is_remote_http=True).valid
    assert validate_copilot_marketplace(valid_remote, is_remote_http=True).valid

    # Incomplete dict sources and local source in remote HTTP marketplace must FAIL
    incomplete_dicts = {
        "name": "acme-mkt",
        "owner": {"name": "Acme Sec"},
        "plugins": [
            {"name": "missing-url", "source": {"source": "url"}},
            {"name": "missing-repo", "source": {"source": "github"}},
            {"name": "bad-repo", "source": {"source": "github", "repo": "not-owner-repo"}},
            {"name": "missing-npm", "source": {"source": "npm"}},
            {"name": "missing-pip", "source": {"source": "pip"}},
            {"name": "missing-path", "source": {"source": "local"}},
            {"name": "local-in-remote", "source": {"source": "directory", "path": "./plugins/x"}},
            {"name": "unknown-type", "source": {"source": "s3", "url": "s3://bucket/a"}},
        ],
    }
    res_inc = validate_claude_marketplace(incomplete_dicts, is_remote_http=True)
    assert not res_inc.valid
    joined = "\n".join(res_inc.errors)
    assert "source.url must be a valid http(s) URL" in joined
    assert "source.repo must be a valid 'owner/repo'" in joined
    assert "source.package must be a non-empty string" in joined
    assert "source.path must be a non-empty string" in joined
    assert "type 'directory' is forbidden in an HTTP-served marketplace" in joined
    assert "'s3'" in joined

    # Local directory dict source passes when is_remote_http=False
    local_dict_ok = {
        "name": "acme-mkt",
        "owner": {"name": "Acme Sec"},
        "plugins": [{"name": "p-local", "source": {"source": "local", "path": "./plugins/p1"}}],
    }
    assert validate_claude_marketplace(local_dict_ok, is_remote_http=False).valid


def test_antigravity_direct_manifest_skills_and_mcp_server_url(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)
    extra_skill_dir = bundle / "extra-skill"
    extra_skill_dir.mkdir()
    (extra_skill_dir / "SKILL.md").write_text(
        "---\nname: extra-skill\ndescription: Extra standalone skill.\n---\nBody.\n",
        encoding="utf-8",
    )

    manifest_path = bundle / ".antigravity-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"] = "extra-skill"
    del manifest["version"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # Test serverUrl + valid authProviderType
    (bundle / "mcp_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "finops-mcp-server": {
                        "serverUrl": "https://finops-mcp.acme.example.com/mcp",
                        "authProviderType": "google_credentials",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    res_ok = validate_antigravity_bundle(bundle)
    assert res_ok.valid, f"Unexpected errors: {res_ok.errors}"
    assert any("missing a non-empty 'version'" in w for w in res_ok.warnings)

    # Mismatched frontmatter name in directly referenced manifest["skills"] dir must FAIL
    (extra_skill_dir / "SKILL.md").write_text(
        "---\nname: wrong-name\ndescription: Mismatched.\n---\nBody.\n",
        encoding="utf-8",
    )
    res_mismatch = validate_antigravity_bundle(bundle)
    assert not res_mismatch.valid
    assert any("must exactly match skill folder name" in err for err in res_mismatch.errors)

    # Restore valid extra skill, test invalid authProviderType and missing mcp_config warning
    (extra_skill_dir / "SKILL.md").write_text(
        "---\nname: extra-skill\ndescription: Extra standalone skill.\n---\nBody.\n",
        encoding="utf-8",
    )
    (bundle / "mcp_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "finops-mcp-server": {
                        "serverUrl": "https://finops-mcp.acme.example.com/mcp",
                        "authProviderType": "unsupported_provider",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    res_bad_auth = validate_antigravity_bundle(bundle)
    assert not res_bad_auth.valid
    assert any("google_credentials" in err for err in res_bad_auth.errors)

    # Removing mcp_config.json while mcp.json exists emits warning
    (bundle / "mcp_config.json").unlink()
    res_warn = validate_antigravity_bundle(bundle)
    assert res_warn.valid
    assert any("Antigravity ignores 'mcp.json' and '.mcp.json'" in w for w in res_warn.warnings)


def test_five_ide_plugin_bundle_validators(tmp_path: Path) -> None:
    bundle = _create_valid_multitool_bundle(tmp_path)

    # Add Claude Code manifest + .mcp.json
    (bundle / ".claude-plugin").mkdir()
    (bundle / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "gcp-finops-toolkit",
                "version": "1.0.0",
                "description": "GCP FinOps toolkit for Claude Code.",
                "author": {"name": "Acme Platform Security"},
            }
        ),
        encoding="utf-8",
    )
    (bundle / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "finops-mcp-server": {
                        "type": "streamable-http",
                        "url": "http://127.0.0.1:8091/mcp",
                        "headers": {"Authorization": "Bearer ${HUB_AUTH_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    # Add GitHub Copilot custom agent
    (bundle / "com.github.copilot" / "agents").mkdir(parents=True)
    (bundle / "com.github.copilot" / "agents" / "finops-advisor.agent.md").write_text(
        "---\n"
        "name: finops-advisor\n"
        "description: Read-only FinOps cost anomaly advisor.\n"
        "tools:\n"
        "  - finops-mcp-server/*\n"
        "---\n\nInvestigate cost spikes.\n",
        encoding="utf-8",
    )

    # Add OpenAI Codex manifest
    (bundle / ".codex-plugin").mkdir()
    (bundle / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "gcp-finops-toolkit",
                "version": "1.0.0",
                "description": "GCP FinOps toolkit for Codex.",
            }
        ),
        encoding="utf-8",
    )

    # All 5 IDE bundle validators pass on the unified bundle
    assert validate_antigravity_bundle(bundle).valid
    assert validate_agent_plugin_bundle(bundle).valid
    assert validate_claude_plugin_bundle(bundle).valid
    assert validate_copilot_plugin_bundle(bundle).valid
    assert validate_codex_plugin_bundle(bundle).valid

    # Invalid cases for Claude, Copilot, and Codex bundle validators
    (bundle / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"bad": {"type": "bogus"}}}), encoding="utf-8"
    )
    assert not validate_claude_plugin_bundle(bundle).valid

    (bundle / "com.github.copilot" / "agents" / "wrong-ext.md").write_text(
        "---\nname: x\ndescription: y\n---\n", encoding="utf-8"
    )
    res_cop = validate_copilot_plugin_bundle(bundle)
    assert not res_cop.valid
    assert any(".agent.md" in err for err in res_cop.errors)

    (bundle / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "different-name"}), encoding="utf-8"
    )
    res_codex = validate_codex_plugin_bundle(bundle)
    assert not res_codex.valid
    assert any("must match root plugin.json" in err for err in res_codex.errors)
