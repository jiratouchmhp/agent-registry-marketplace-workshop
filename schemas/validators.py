"""Reusable schema & policy validators for multi-IDE plugin bundles and marketplaces.

Implements:
- Official Draft 2020-12 JSON Schema validation for Antigravity v1 and Agent Plugins 1.0
- All 7 Antigravity Pre-Publish Checklist checks
- Agent Skills (agentskills.io) SKILL.md frontmatter checks
- Marketplace catalog validators for Claude Code, VS Code/GitHub Copilot, OpenAI Codex,
  and Google Antigravity
"""

from __future__ import annotations

import json
import re
import shlex
import stat
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from jsonschema import Draft202012Validator

SCHEMAS_DIR = Path(__file__).resolve().parent

KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
ANTIGRAVITY_MCP_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
ENV_INTERPOLATION_RE = re.compile(r"\$\{[^}]+\}")

DISALLOWED_BRAND_PREFIXES = ("antigravity", "gemini", "claude", "cursor")
DEPRECATED_ANTIGRAVITY_FIELDS = ("source", "sourcePath", "source_path")
RESERVED_MCP_ENV_KEYS = ("PLUGIN_ROOT", "PLUGIN_DATA")
VALID_RULE_TRIGGERS = ("always_on", "model_decision", "glob", "manual")
MATCHER_HOOK_EVENTS = frozenset({"PreToolUse", "PostToolUse"})
FLAT_HOOK_EVENTS = frozenset({"Stop", "SessionStart", "PreInvocation", "PostInvocation"})
VALID_ANTIGRAVITY_HOOK_EVENTS = MATCHER_HOOK_EVENTS | FLAT_HOOK_EVENTS
HOOK_EVENT_NAMES = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SubagentStop",
        "Notification",
        "SessionStart",
        "PreInvocation",
        "PostInvocation",
        "UserPromptSubmit",
        "hooks",
    }
)
CLAUDE_COPILOT_SOURCE_TYPES = frozenset(
    {"url", "git-subdir", "archive", "git", "github", "npm", "pip", "local", "directory"}
)
CODEX_INSTALL_POLICIES = frozenset({"AVAILABLE", "INSTALLED_BY_DEFAULT", "NOT_AVAILABLE"})
CODEX_AUTH_POLICIES = frozenset({"ON_INSTALL", "ON_USE"})
NON_EXECUTABLE_SCRIPT_EXTS = frozenset({".md", ".txt", ".json", ".yaml", ".yml", ".pyc"})

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


@dataclass
class ValidationResult:
    """Outcome of a bundle or marketplace validation check."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0

    def __bool__(self) -> bool:
        return self.valid

    def extend(self, other: ValidationResult) -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)

    def raise_for_errors(self) -> None:
        if self.errors:
            formatted = "\n".join(f"- {err}" for err in self.errors)
            raise ValueError(f"Validation failed with {len(self.errors)} error(s):\n{formatted}")


def load_schema(filename: str) -> dict[str, Any]:
    """Load and cache a JSON schema stored in this package."""
    if filename not in _SCHEMA_CACHE:
        schema_path = SCHEMAS_DIR / filename
        with schema_path.open("r", encoding="utf-8") as fh:
            _SCHEMA_CACHE[filename] = json.load(fh)
    return _SCHEMA_CACHE[filename]


def _validate_against_schema(
    instance: Any, schema_filename: str, context: str
) -> list[str]:
    schema = load_schema(schema_filename)
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(part) for part in err.absolute_path) or "<root>"
        errors.append(f"{context} schema error at '{path}': {err.message}")
    return errors


def _load_json_input(
    path_or_data: str | Path | dict[str, Any], label: str
) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(path_or_data, dict):
        return path_or_data, None
    file_path = Path(path_or_data)
    if not file_path.is_file():
        return None, f"{label} file not found: {file_path}"
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"{label} is not valid JSON ({file_path}): {exc}"
    if not isinstance(data, dict):
        return None, f"{label} root must be a JSON object ({file_path})"
    return data, None


def parse_frontmatter(markdown_text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    """Parse YAML frontmatter delimited by leading '---' markers."""
    lines = markdown_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, markdown_text, "Missing YAML frontmatter opening '---' delimiter on line 1"
    closing_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            closing_idx = idx
            break
    if closing_idx is None:
        return None, markdown_text, "Missing YAML frontmatter closing '---' delimiter"
    yaml_block = "\n".join(lines[1:closing_idx])
    body = "\n".join(lines[closing_idx + 1 :])
    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        return None, body, f"Invalid YAML frontmatter: {exc}"
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        return None, body, "YAML frontmatter must be a mapping/object"
    return parsed, body, None


def _has_disallowed_brand_prefix(value: str) -> str | None:
    lowered = value.strip().lower()
    for prefix in DISALLOWED_BRAND_PREFIXES:
        if lowered.startswith(prefix):
            return prefix
    return None


def _resolve_contained_path(root: Path, rel_path: str) -> Path | None:
    if not rel_path or rel_path.startswith("/") or Path(rel_path).is_absolute():
        return None
    root_resolved = root.resolve()
    candidate = (root_resolved / rel_path).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    return candidate


def _parse_svg_number(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"(px|pt|em|rem|%)$", "", raw.strip())
    try:
        return float(cleaned)
    except ValueError:
        return None


def _check_logo_square(logo_path: Path) -> str | None:
    """Verify logo file is non-empty and 1:1 square when dimensions are inspectable."""
    data = logo_path.read_bytes()
    if not data:
        return f"Logo file is empty: {logo_path.name}"

    suffix = logo_path.suffix.lower()
    if suffix == ".png":
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
            return f"Logo PNG header is invalid: {logo_path.name}"
        width, height = struct.unpack(">II", data[16:24])
        if width <= 0 or height <= 0 or width != height:
            return (
                f"Logo PNG must have a 1:1 square aspect ratio "
                f"(got {width}x{height} in {logo_path.name})"
            )
    elif suffix == ".svg":
        try:
            root = ET.fromstring(data.decode("utf-8"))
        except (UnicodeDecodeError, ET.ParseError) as exc:
            return f"Logo SVG is not valid XML ({logo_path.name}): {exc}"
        view_box = root.attrib.get("viewBox")
        if view_box:
            parts = re.split(r"[\s,]+", view_box.strip())
            if len(parts) == 4:
                try:
                    vb_w = float(parts[2])
                    vb_h = float(parts[3])
                except ValueError:
                    return f"Logo SVG has non-numeric viewBox '{view_box}' ({logo_path.name})"
                if vb_w <= 0 or vb_h <= 0 or abs(vb_w - vb_h) > 1e-6:
                    return (
                        f"Logo SVG viewBox must have a 1:1 square aspect ratio "
                        f"(got {vb_w:g}x{vb_h:g} in {logo_path.name})"
                    )
                return None
        width = _parse_svg_number(root.attrib.get("width"))
        height = _parse_svg_number(root.attrib.get("height"))
        if width is not None and height is not None:
            if width <= 0 or height <= 0 or abs(width - height) > 1e-6:
                return (
                    f"Logo SVG must have a 1:1 square aspect ratio "
                    f"(got {width:g}x{height:g} in {logo_path.name})"
                )
    return None


def validate_skill_md(
    skill_md_path: str | Path, expected_name: str | None = None
) -> ValidationResult:
    """Validate an Agent Skills (agentskills.io) SKILL.md file."""
    result = ValidationResult()
    path = Path(skill_md_path)
    if not path.is_file():
        result.errors.append(f"SKILL.md file not found: {path}")
        return result
    if path.name != "SKILL.md":
        result.errors.append(f"Skill file must be named uppercase 'SKILL.md', got '{path.name}'")

    text = path.read_text(encoding="utf-8")
    frontmatter, _body, fm_err = parse_frontmatter(text)
    if fm_err or frontmatter is None:
        result.errors.append(f"{path}: {fm_err}")
        return result

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name:
        result.errors.append(f"{path}: frontmatter missing required non-empty string 'name'")
    else:
        if not SKILL_NAME_RE.match(name) or "--" in name:
            result.errors.append(
                f"{path}: frontmatter 'name' ({name!r}) must be 1-64 chars lowercase [a-z0-9-] "
                f"with no leading, trailing, or consecutive hyphens"
            )
        if expected_name is not None and name != expected_name:
            result.errors.append(
                f"{path}: frontmatter 'name' ({name!r}) must exactly match skill folder name "
                f"({expected_name!r})"
            )

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        result.errors.append(f"{path}: frontmatter missing required non-empty string 'description'")
    elif len(description) > 1024:
        result.errors.append(
            f"{path}: frontmatter 'description' exceeds 1024 chars ({len(description)})"
        )

    metadata = frontmatter.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            result.errors.append(f"{path}: frontmatter 'metadata' must be a map<string, string>")
        else:
            for k, v in metadata.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    result.errors.append(
                        f"{path}: frontmatter metadata[{k!r}] must have scalar string key and "
                        f"value (got {type(v).__name__})"
                    )

    return result


def _validate_skills_dir(skills_root: Path, result: ValidationResult) -> None:
    if not skills_root.exists():
        return
    if not skills_root.is_dir():
        result.errors.append(f"Skills path is not a directory: {skills_root}")
        return

    for entry in sorted(skills_root.iterdir()):
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            lowercase = entry / "skill.md"
            if lowercase.is_file():
                result.errors.append(
                    f"Skill directory '{entry.name}' contains lowercase 'skill.md'; "
                    f"must be uppercase 'SKILL.md'"
                )
            else:
                result.errors.append(
                    f"Skill directory '{entry.name}' is missing required 'SKILL.md'"
                )
            continue
        result.extend(validate_skill_md(skill_md, expected_name=entry.name))


def _validate_antigravity_rules(plugin_root: Path, result: ValidationResult) -> None:
    if (plugin_root / "AGENTS.md").is_file():
        result.warnings.append(
            "Root 'AGENTS.md' is ignored by Antigravity; place always-on instructions in "
            "'rules/AGENTS.md'."
        )

    rules_dir = plugin_root / "rules"
    if not rules_dir.is_dir():
        return

    for rule_file in sorted(rules_dir.rglob("*.md")):
        if rule_file.name == "AGENTS.md" and rule_file.parent == rules_dir:
            continue
        text = rule_file.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        frontmatter, _body, fm_err = parse_frontmatter(text)
        if fm_err or frontmatter is None:
            result.errors.append(f"{rule_file}: {fm_err}")
            continue

        trigger = frontmatter.get("trigger")
        if trigger is not None and trigger not in VALID_RULE_TRIGGERS:
            result.errors.append(
                f"{rule_file}: invalid rule trigger {trigger!r}; expected one of "
                f"{', '.join(VALID_RULE_TRIGGERS)}"
            )

        if "globs" in frontmatter:
            globs = frontmatter["globs"]
            if not isinstance(globs, str):
                result.errors.append(
                    f"{rule_file}: rule 'globs' MUST be a comma-separated string "
                    f"(e.g. '**/*.sql, **/*.tf'), never a {type(globs).__name__}"
                )
            elif not globs.strip():
                result.errors.append(f"{rule_file}: rule 'globs' string must not be empty")
        elif trigger == "glob":
            result.errors.append(
                f"{rule_file}: rule with trigger='glob' must specify a comma-separated 'globs' string"
            )


def _validate_antigravity_mcp_config(plugin_root: Path, result: ValidationResult) -> None:
    mcp_config_path = plugin_root / "mcp_config.json"
    if not mcp_config_path.is_file():
        if (plugin_root / "mcp.json").is_file() or (plugin_root / ".mcp.json").is_file():
            result.warnings.append(
                "Found 'mcp.json'/'.mcp.json' without 'mcp_config.json'; Antigravity ignores "
                "'mcp.json' and '.mcp.json' and only loads 'mcp_config.json'."
            )
        return

    data, err = _load_json_input(mcp_config_path, "mcp_config.json")
    if err or data is None:
        result.errors.append(err or "Failed to load mcp_config.json")
        return

    if "variables" in data:
        result.errors.append(
            "mcp_config.json: top-level 'variables' is forbidden; declare 'variables' inside "
            "each 'mcpServers.<server>' entry instead"
        )

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        result.errors.append("mcp_config.json: 'mcpServers' must be an object")
        return

    for server_key, server_cfg in servers.items():
        if not isinstance(server_key, str) or not ANTIGRAVITY_MCP_KEY_RE.match(server_key):
            result.errors.append(
                f"mcp_config.json: MCP server key {server_key!r} must match "
                f"^[a-zA-Z0-9_-]{{1,64}}$ (no dots or special characters)"
            )
        elif len(server_key) > 20:
            result.warnings.append(
                f"mcp_config.json: MCP server key {server_key!r} exceeds recommended 20 chars "
                f"for Antigravity tool prefixing"
            )

        if not isinstance(server_cfg, dict):
            result.errors.append(
                f"mcp_config.json: server '{server_key}' configuration must be an object"
            )
            continue

        has_url = (
            (isinstance(server_cfg.get("url"), str) and bool(server_cfg["url"].strip()))
            or (
                isinstance(server_cfg.get("serverUrl"), str)
                and bool(server_cfg["serverUrl"].strip())
            )
        )
        has_cmd = isinstance(server_cfg.get("command"), str) and bool(server_cfg["command"].strip())
        if not has_url and not has_cmd:
            result.errors.append(
                f"mcp_config.json: server '{server_key}' must define 'url', 'serverUrl', or "
                f"'command'"
            )

        if "authProviderType" in server_cfg:
            auth_provider = server_cfg["authProviderType"]
            if auth_provider != "google_credentials":
                result.errors.append(
                    f"mcp_config.json: server '{server_key}' 'authProviderType' must be "
                    f"'google_credentials' (got {auth_provider!r})"
                )

        env = server_cfg.get("env")
        if env is not None:
            if not isinstance(env, dict):
                result.errors.append(
                    f"mcp_config.json: server '{server_key}' 'env' must be an object"
                )
            else:
                for reserved in RESERVED_MCP_ENV_KEYS:
                    if reserved in env:
                        result.errors.append(
                            f"mcp_config.json: server '{server_key}' must not declare reserved "
                            f"env key '{reserved}'"
                        )

        variables = server_cfg.get("variables")
        if variables is not None:
            if not isinstance(variables, list):
                result.errors.append(
                    f"mcp_config.json: server '{server_key}' 'variables' must be a list"
                )
            else:
                for idx, var_item in enumerate(variables):
                    if not isinstance(var_item, dict) or not isinstance(var_item.get("name"), str):
                        result.errors.append(
                            f"mcp_config.json: server '{server_key}' variables[{idx}] must be an "
                            f"object with a string 'name'"
                        )


def _validate_hook_handler(
    hook: Any, loc: str, plugin_root: Path, result: ValidationResult
) -> None:
    if not isinstance(hook, dict):
        result.errors.append(f"hooks.json: {loc} must be an object")
        return

    if "timeout" in hook:
        timeout = hook["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            result.errors.append(
                f"hooks.json: {loc}.timeout must be a positive integer (got {timeout!r})"
            )

    hook_type = hook.get("type")
    if hook_type not in ("command", "prompt"):
        result.errors.append(
            f"hooks.json: {loc}.type must be 'command' or 'prompt' (got {hook_type!r})"
        )
        return

    has_command = "command" in hook
    has_prompt = "prompt" in hook
    if has_command and has_prompt:
        result.errors.append(f"hooks.json: {loc} must not specify both 'command' and 'prompt'")
        return

    if hook_type == "prompt":
        prompt = hook.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            result.errors.append(f"hooks.json: {loc} missing non-empty 'prompt'")
        return

    command = hook.get("command")
    if not isinstance(command, str) or not command.strip():
        result.errors.append(f"hooks.json: {loc} missing non-empty 'command'")
        return

    if ENV_INTERPOLATION_RE.search(command):
        result.errors.append(
            f"hooks.json: {loc} command {command!r} must use relative './scripts/...' paths "
            f"without ${{...}} variable interpolation (e.g. ${{PLUGIN_ROOT}}, "
            f"${{CLAUDE_PLUGIN_ROOT}})"
        )
        return

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    if not tokens:
        return

    for token in tokens:
        if token.startswith("/") and (token == tokens[0] or "/scripts/" in token):
            result.errors.append(
                f"hooks.json: {loc} command {command!r} must use relative './scripts/...' "
                f"paths, not an absolute path ({token!r})"
            )
        elif token.startswith("scripts/"):
            result.errors.append(
                f"hooks.json: {loc} script path {token!r} must start with './scripts/' "
                f"relative prefix"
            )
        elif token.startswith("./scripts/"):
            resolved = _resolve_contained_path(plugin_root, token)
            if resolved is None or not resolved.is_file():
                result.errors.append(
                    f"hooks.json: {loc} referenced hook script does not exist: {token}"
                )


def _validate_antigravity_hooks(plugin_root: Path, result: ValidationResult) -> None:
    hooks_path = plugin_root / "hooks.json"
    if not hooks_path.is_file():
        return

    data, err = _load_json_input(hooks_path, "hooks.json")
    if err or data is None:
        result.errors.append(err or "Failed to load hooks.json")
        return

    if not data:
        result.errors.append("hooks.json: must define at least one named hook group")
        return

    for group_name, group_cfg in data.items():
        if group_name in HOOK_EVENT_NAMES:
            result.errors.append(
                f"hooks.json: top-level key {group_name!r} is a bare hook event/wrapper name; "
                f"wrap hooks in a named top-level group (e.g. 'policy-guard')"
            )
            continue
        if not isinstance(group_cfg, dict):
            result.errors.append(
                f"hooks.json: hook group {group_name!r} must be an object"
            )
            continue
        if "enabled" in group_cfg and not isinstance(group_cfg["enabled"], bool):
            result.errors.append(
                f"hooks.json: hook group {group_name!r} 'enabled' must be a boolean"
            )
        if "description" in group_cfg and not isinstance(group_cfg["description"], str):
            result.errors.append(
                f"hooks.json: hook group {group_name!r} 'description' must be a string"
            )

        for event_name, event_entries in group_cfg.items():
            if event_name in ("enabled", "description"):
                continue

            if event_name not in VALID_ANTIGRAVITY_HOOK_EVENTS:
                result.errors.append(
                    f"hooks.json: hook group {group_name!r} contains unknown event "
                    f"{event_name!r}; expected one of {sorted(VALID_ANTIGRAVITY_HOOK_EVENTS)}"
                )
                continue

            if event_name in MATCHER_HOOK_EVENTS:
                if not isinstance(event_entries, list):
                    result.errors.append(
                        f"hooks.json: {group_name}.{event_name} must be a list of matcher objects"
                    )
                    continue
                for entry_idx, entry in enumerate(event_entries):
                    if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                        result.errors.append(
                            f"hooks.json: {group_name}.{event_name}[{entry_idx}] must be an "
                            f"object containing a 'hooks' list"
                        )
                        continue
                    if "matcher" in entry and not isinstance(entry["matcher"], str):
                        result.errors.append(
                            f"hooks.json: {group_name}.{event_name}[{entry_idx}].matcher must be "
                            f"a string"
                        )
                    for hook_idx, hook in enumerate(entry["hooks"]):
                        _validate_hook_handler(
                            hook,
                            f"{group_name}.{event_name}[{entry_idx}].hooks[{hook_idx}]",
                            plugin_root,
                            result,
                        )
            else:
                # Flat-handler events: Stop, SessionStart, PreInvocation, PostInvocation
                if not isinstance(event_entries, list):
                    result.errors.append(
                        f"hooks.json: {group_name}.{event_name} must be a flat list of handler "
                        f"objects"
                    )
                    continue
                for entry_idx, entry in enumerate(event_entries):
                    loc = f"{group_name}.{event_name}[{entry_idx}]"
                    if isinstance(entry, dict) and ("hooks" in entry or "matcher" in entry):
                        result.errors.append(
                            f"hooks.json: {loc} must be a flat handler object (do not wrap "
                            f"'{event_name}' handlers in 'matcher'/'hooks')"
                        )
                        continue
                    _validate_hook_handler(entry, loc, plugin_root, result)


def _validate_scripts_executable(plugin_root: Path, result: ValidationResult) -> None:
    scripts_dir = plugin_root / "scripts"
    if not scripts_dir.is_dir():
        return

    for script_path in sorted(scripts_dir.rglob("*")):
        if not script_path.is_file():
            continue
        if "__pycache__" in script_path.parts or script_path.name.startswith("."):
            continue
        if script_path.suffix.lower() in NON_EXECUTABLE_SCRIPT_EXTS:
            continue
        mode = stat.S_IMODE(script_path.stat().st_mode)
        if (mode & 0o755) != 0o755:
            rel = script_path.relative_to(plugin_root)
            result.errors.append(
                f"Script '{rel}' must have executable 0755 permissions (got {oct(mode)})"
            )


def _validate_antigravity_agents(plugin_root: Path, result: ValidationResult) -> None:
    agents_dir = plugin_root / "agents"
    if not agents_dir.is_dir():
        return
    for agent_file in sorted(agents_dir.glob("*.md")):
        frontmatter, _body, fm_err = parse_frontmatter(agent_file.read_text(encoding="utf-8"))
        if fm_err or frontmatter is None:
            result.errors.append(f"{agent_file}: {fm_err}")
            continue
        if not isinstance(frontmatter.get("name"), str) or not frontmatter["name"].strip():
            result.errors.append(f"{agent_file}: agent frontmatter missing non-empty 'name'")
        if (
            not isinstance(frontmatter.get("description"), str)
            or not frontmatter["description"].strip()
        ):
            result.errors.append(
                f"{agent_file}: agent frontmatter missing non-empty 'description'"
            )


def validate_antigravity_bundle(plugin_dir: str | Path) -> ValidationResult:
    """Validate a plugin directory against Antigravity v1 schema and all 7 pre-publish checks."""
    result = ValidationResult()
    plugin_root = Path(plugin_dir)
    if not plugin_root.is_dir():
        result.errors.append(f"Plugin directory not found: {plugin_root}")
        return result

    ag_manifest_path = plugin_root / ".antigravity-plugin" / "plugin.json"
    root_manifest_path = plugin_root / "plugin.json"
    if ag_manifest_path.is_file():
        manifest_path = ag_manifest_path
        if root_manifest_path.is_file():
            result.warnings.append(
                "Multi-tool layout detected: '.antigravity-plugin/plugin.json' takes precedence "
                "over root 'plugin.json'."
            )
    elif root_manifest_path.is_file():
        manifest_path = root_manifest_path
    else:
        result.errors.append(
            f"Missing Antigravity manifest ('.antigravity-plugin/plugin.json' or 'plugin.json') "
            f"in {plugin_root}"
        )
        return result

    manifest, err = _load_json_input(manifest_path, str(manifest_path.relative_to(plugin_root)))
    if err or manifest is None:
        result.errors.append(err or "Failed to parse Antigravity manifest")
        return result

    # 1a. Official Draft 2020-12 JSON Schema check
    result.errors.extend(
        _validate_against_schema(
            manifest,
            "antigravity_plugin_v1.schema.json",
            str(manifest_path.relative_to(plugin_root)),
        )
    )

    # 1b. Pre-publish checklist #1: kebab-case + non-branded naming, no deprecated keys, https author URL
    for deprecated_key in DEPRECATED_ANTIGRAVITY_FIELDS:
        if deprecated_key in manifest:
            result.errors.append(
                f"Manifest uses deprecated field '{deprecated_key}'; remove it before publishing"
            )

    name = manifest.get("name")
    if isinstance(name, str):
        if not KEBAB_CASE_RE.match(name) or len(name) > 64:
            result.errors.append(
                f"Plugin 'name' ({name!r}) must be lowercase kebab-case matching "
                f"^[a-z0-9]+(-[a-z0-9]+)*$ (max 64 chars)"
            )
        brand_hit = _has_disallowed_brand_prefix(name)
        if brand_hit:
            result.errors.append(
                f"Plugin 'name' ({name!r}) must not start with branded prefix '{brand_hit}'"
            )

    for display_key in ("displayName", "display_name"):
        display_val = manifest.get(display_key)
        if isinstance(display_val, str) and display_val.strip():
            brand_hit = _has_disallowed_brand_prefix(display_val)
            if brand_hit:
                result.errors.append(
                    f"Plugin '{display_key}' ({display_val!r}) must not start with branded "
                    f"prefix '{brand_hit}'"
                )

    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        result.warnings.append(
            "Manifest is missing a non-empty 'version' (recommended semver e.g. '1.0.0')."
        )

    author = manifest.get("author")
    if isinstance(author, dict) and "url" in author:
        author_url = author["url"]
        if isinstance(author_url, str) and not author_url.startswith("https://"):
            result.errors.append(
                f"Manifest 'author.url' ({author_url!r}) must use an 'https://' URL"
            )

    # Pre-publish checklist #6: local 1:1 logo file exists
    logo = manifest.get("logo")
    if not isinstance(logo, str) or not logo.strip():
        result.errors.append(
            "Manifest is missing required local 'logo' (e.g. 'assets/logo.svg', 1:1 square)"
        )
    elif logo.startswith(("http://", "https://")):
        result.errors.append(
            f"Manifest 'logo' ({logo!r}) must be a local relative file inside the plugin bundle, "
            f"not a remote URL"
        )
    else:
        logo_resolved = _resolve_contained_path(plugin_root, logo)
        if logo_resolved is None or not logo_resolved.is_file():
            result.errors.append(f"Logo file does not exist inside plugin directory: '{logo}'")
        else:
            logo_err = _check_logo_square(logo_resolved)
            if logo_err:
                result.errors.append(logo_err)

    # Pre-publish checklist #2: skills/<name>/SKILL.md folder == frontmatter name
    _validate_skills_dir(plugin_root / "skills", result)
    extra_skills = manifest.get("skills")
    if isinstance(extra_skills, str):
        extra_paths = [extra_skills]
    elif isinstance(extra_skills, list):
        extra_paths = [p for p in extra_skills if isinstance(p, str)]
    else:
        extra_paths = []
    for extra in extra_paths:
        resolved = _resolve_contained_path(plugin_root, extra)
        if resolved is None or not resolved.is_dir():
            result.errors.append(f"Manifest 'skills' entry directory not found: '{extra}'")
        elif (resolved / "SKILL.md").is_file():
            result.extend(validate_skill_md(resolved / "SKILL.md", expected_name=resolved.name))
        elif (resolved / "skill.md").is_file():
            result.errors.append(
                f"Skill directory '{resolved.name}' contains lowercase 'skill.md'; "
                f"must be uppercase 'SKILL.md'"
            )
        else:
            _validate_skills_dir(resolved, result)

    # Pre-publish checklist #3: rules & comma-separated globs string
    _validate_antigravity_rules(plugin_root, result)

    # Pre-publish checklist #4: mcp_config.json scoped variables, sanitized keys, no reserved env
    _validate_antigravity_mcp_config(plugin_root, result)

    # Pre-publish checklist #5: named hooks.json w/ relative ./scripts/... paths
    _validate_antigravity_hooks(plugin_root, result)

    # Pre-publish checklist #7: scripts/ files have executable 0755 bit
    _validate_scripts_executable(plugin_root, result)

    # Optional Antigravity subagents check
    _validate_antigravity_agents(plugin_root, result)

    return result


def validate_agent_plugin_bundle(plugin_dir: str | Path) -> ValidationResult:
    """Validate a plugin directory against the Agent Plugins 1.0 specification."""
    result = ValidationResult()
    plugin_root = Path(plugin_dir)
    if not plugin_root.is_dir():
        result.errors.append(f"Plugin directory not found: {plugin_root}")
        return result

    manifest_path = plugin_root / "plugin.json"
    manifest, err = _load_json_input(manifest_path, "plugin.json")
    if err or manifest is None:
        result.errors.append(err or "Missing root plugin.json")
        return result

    result.errors.extend(
        _validate_against_schema(manifest, "agent_plugin_v1.schema.json", "plugin.json")
    )

    mcp_path = plugin_root / "mcp.json"
    if mcp_path.is_file():
        mcp_data, mcp_err = _load_json_input(mcp_path, "mcp.json")
        if mcp_err or mcp_data is None:
            result.errors.append(mcp_err or "Invalid mcp.json")
        else:
            result.errors.extend(
                _validate_against_schema(mcp_data, "agent_plugin_mcp_v1.schema.json", "mcp.json")
            )
            servers = mcp_data.get("mcpServers")
            if isinstance(servers, dict):
                for server_name, cfg in servers.items():
                    if not isinstance(cfg, dict):
                        continue
                    if cfg.get("type") in ("streamable-http", "sse"):
                        url = cfg.get("url")
                        if isinstance(url, str) and ENV_INTERPOLATION_RE.search(url):
                            result.errors.append(
                                f"mcp.json: remote server '{server_name}' url must not use "
                                f"${{ENV_VAR}} expansion in Agent Plugins 1.0"
                            )
                        headers = cfg.get("headers")
                        if isinstance(headers, dict):
                            for hdr_name, hdr_val in headers.items():
                                if isinstance(hdr_val, str) and ENV_INTERPOLATION_RE.search(
                                    hdr_val
                                ):
                                    result.errors.append(
                                        f"mcp.json: remote server '{server_name}' header "
                                        f"'{hdr_name}' must not use ${{ENV_VAR}} expansion in "
                                        f"Agent Plugins 1.0"
                                    )

    _validate_skills_dir(plugin_root / "skills", result)
    return result


def _validate_marketplace_owner(
    data: dict[str, Any], label: str, result: ValidationResult
) -> None:
    owner = data.get("owner")
    if not isinstance(owner, dict):
        result.errors.append(f"{label}: missing required object 'owner'")
        return
    if not isinstance(owner.get("name"), str) or not owner["name"].strip():
        result.errors.append(f"{label}: 'owner.name' must be a non-empty string")
    if "email" in owner and not isinstance(owner["email"], str):
        result.errors.append(f"{label}: 'owner.email' must be a string")


def _validate_marketplace_metadata(
    data: dict[str, Any], label: str, result: ValidationResult
) -> None:
    metadata = data.get("metadata")
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        result.errors.append(f"{label}: 'metadata' must be an object")
        return
    for key in ("description", "version", "pluginRoot"):
        if key in metadata and not isinstance(metadata[key], str):
            result.errors.append(f"{label}: 'metadata.{key}' must be a string")


def _is_http_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _validate_claude_or_copilot_source(
    source: Any,
    label: str,
    idx: int,
    result: ValidationResult,
    *,
    is_remote_http: bool = False,
) -> None:
    if isinstance(source, str):
        if not source.strip():
            result.errors.append(f"{label}: plugins[{idx}].source must not be empty")
        elif is_remote_http and not _is_http_url(source):
            result.errors.append(
                f"{label}: plugins[{idx}].source ({source!r}) must be an http:// or https:// URL "
                f"in an HTTP-served marketplace (relative/local paths are forbidden)"
            )
    elif isinstance(source, dict):
        src_type = source.get("source")
        if not isinstance(src_type, str) or not src_type.strip():
            result.errors.append(
                f"{label}: plugins[{idx}].source object must specify a non-empty 'source' type"
            )
            return
        if src_type not in CLAUDE_COPILOT_SOURCE_TYPES:
            result.errors.append(
                f"{label}: plugins[{idx}].source.source ({src_type!r}) must be one of "
                f"{sorted(CLAUDE_COPILOT_SOURCE_TYPES)}"
            )
            return

        if src_type in ("url", "git-subdir", "archive", "git"):
            if not _is_http_url(source.get("url")):
                result.errors.append(
                    f"{label}: plugins[{idx}].source.url must be a valid http(s) URL for "
                    f"source type '{src_type}'"
                )
            if src_type == "git-subdir" and "path" in source:
                if not isinstance(source["path"], str) or not source["path"].strip():
                    result.errors.append(
                        f"{label}: plugins[{idx}].source.path must be a non-empty string for "
                        f"'git-subdir'"
                    )
        elif src_type == "github":
            repo = source.get("repo")
            if (
                not isinstance(repo, str)
                or not repo.strip()
                or len(repo.strip().split("/")) != 2
                or not all(part.strip() for part in repo.strip().split("/"))
            ):
                result.errors.append(
                    f"{label}: plugins[{idx}].source.repo must be a valid 'owner/repo' string "
                    f"for source type 'github'"
                )
        elif src_type in ("npm", "pip"):
            pkg = source.get("package")
            if not isinstance(pkg, str) or not pkg.strip():
                result.errors.append(
                    f"{label}: plugins[{idx}].source.package must be a non-empty string for "
                    f"source type '{src_type}'"
                )
        elif src_type in ("local", "directory"):
            if is_remote_http:
                result.errors.append(
                    f"{label}: plugins[{idx}].source type '{src_type}' is forbidden in an "
                    f"HTTP-served marketplace"
                )
            src_path = source.get("path")
            if not isinstance(src_path, str) or not src_path.strip():
                result.errors.append(
                    f"{label}: plugins[{idx}].source.path must be a non-empty string for "
                    f"source type '{src_type}'"
                )
    else:
        result.errors.append(
            f"{label}: plugins[{idx}].source must be a non-empty string or source object"
        )


def validate_claude_marketplace(
    path: str | Path | dict[str, Any], *, is_remote_http: bool = False
) -> ValidationResult:
    """Validate a Claude Code marketplace.json catalog."""
    result = ValidationResult()
    label = "Claude marketplace"
    data, err = _load_json_input(path, label)
    if err or data is None:
        result.errors.append(err or f"Failed to load {label}")
        return result

    if not isinstance(data.get("name"), str) or not data["name"].strip():
        result.errors.append(f"{label}: missing required non-empty string 'name'")

    _validate_marketplace_owner(data, label, result)
    _validate_marketplace_metadata(data, label, result)

    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        result.errors.append(f"{label}: missing required list 'plugins'")
        return result

    seen_names: set[str] = set()
    for idx, plugin in enumerate(plugins):
        if not isinstance(plugin, dict):
            result.errors.append(f"{label}: plugins[{idx}] must be an object")
            continue
        pname = plugin.get("name")
        if not isinstance(pname, str) or not pname.strip():
            result.errors.append(f"{label}: plugins[{idx}] missing required non-empty 'name'")
        elif pname in seen_names:
            result.errors.append(f"{label}: duplicate plugin name '{pname}'")
        else:
            seen_names.add(pname)

        _validate_claude_or_copilot_source(
            plugin.get("source"), label, idx, result, is_remote_http=is_remote_http
        )

        for opt_key in ("description", "version"):
            if opt_key in plugin and not isinstance(plugin[opt_key], str):
                result.errors.append(f"{label}: plugins[{idx}].{opt_key} must be a string")

    return result


def validate_copilot_marketplace(
    path: str | Path | dict[str, Any], *, is_remote_http: bool = False
) -> ValidationResult:
    """Validate a VS Code / GitHub Copilot marketplace.json catalog."""
    result = ValidationResult()
    label = "Copilot marketplace"
    data, err = _load_json_input(path, label)
    if err or data is None:
        result.errors.append(err or f"Failed to load {label}")
        return result

    if not isinstance(data.get("name"), str) or not data["name"].strip():
        result.errors.append(f"{label}: missing required non-empty string 'name'")

    _validate_marketplace_owner(data, label, result)
    _validate_marketplace_metadata(data, label, result)

    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        result.errors.append(f"{label}: missing required list 'plugins'")
        return result

    seen_names: set[str] = set()
    for idx, plugin in enumerate(plugins):
        if not isinstance(plugin, dict):
            result.errors.append(f"{label}: plugins[{idx}] must be an object")
            continue
        pname = plugin.get("name")
        if not isinstance(pname, str) or not pname.strip():
            result.errors.append(f"{label}: plugins[{idx}] missing required non-empty 'name'")
        elif pname in seen_names:
            result.errors.append(f"{label}: duplicate plugin name '{pname}'")
        else:
            seen_names.add(pname)

        _validate_claude_or_copilot_source(
            plugin.get("source"), label, idx, result, is_remote_http=is_remote_http
        )

        for opt_key in ("description", "version", "category"):
            if opt_key in plugin and not isinstance(plugin[opt_key], str):
                result.errors.append(f"{label}: plugins[{idx}].{opt_key} must be a string")
        if "tags" in plugin and (
            not isinstance(plugin["tags"], list)
            or not all(isinstance(t, str) for t in plugin["tags"])
        ):
            result.errors.append(f"{label}: plugins[{idx}].tags must be a list of strings")

    return result


def validate_codex_marketplace(path: str | Path | dict[str, Any]) -> ValidationResult:
    """Validate an OpenAI Codex marketplace.json catalog."""
    result = ValidationResult()
    label = "Codex marketplace"
    data, err = _load_json_input(path, label)
    if err or data is None:
        result.errors.append(err or f"Failed to load {label}")
        return result

    if not isinstance(data.get("name"), str) or not data["name"].strip():
        result.errors.append(f"{label}: missing required non-empty string 'name'")

    interface = data.get("interface")
    if not isinstance(interface, dict):
        result.errors.append(f"{label}: missing required object 'interface'")
    elif not isinstance(interface.get("displayName"), str) or not interface["displayName"].strip():
        result.errors.append(f"{label}: 'interface.displayName' must be a non-empty string")

    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        result.errors.append(f"{label}: missing required list 'plugins'")
        return result

    seen_names: set[str] = set()
    for idx, plugin in enumerate(plugins):
        if not isinstance(plugin, dict):
            result.errors.append(f"{label}: plugins[{idx}] must be an object")
            continue

        pname = plugin.get("name")
        if not isinstance(pname, str) or not pname.strip():
            result.errors.append(f"{label}: plugins[{idx}] missing required non-empty 'name'")
        elif pname in seen_names:
            result.errors.append(f"{label}: duplicate plugin name '{pname}'")
        else:
            seen_names.add(pname)

        source = plugin.get("source")
        if not isinstance(source, dict):
            result.errors.append(f"{label}: plugins[{idx}].source must be an object")
        else:
            src_kind = source.get("source")
            if src_kind == "local":
                src_path = source.get("path")
                if not isinstance(src_path, str) or not src_path.strip():
                    result.errors.append(
                        f"{label}: plugins[{idx}].source.path must be a non-empty string for "
                        f"local source"
                    )
            elif src_kind in ("url", "archive", "git"):
                src_url = source.get("url")
                if not isinstance(src_url, str) or not src_url.startswith(("http://", "https://")):
                    result.errors.append(
                        f"{label}: plugins[{idx}].source.url must be an http(s) URL for "
                        f"'{src_kind}' source"
                    )
            else:
                result.errors.append(
                    f"{label}: plugins[{idx}].source.source must be one of "
                    f"'local', 'url', 'archive', 'git' (got {src_kind!r})"
                )

        policy = plugin.get("policy")
        if not isinstance(policy, dict):
            result.errors.append(f"{label}: plugins[{idx}].policy must be an object")
        else:
            installation = policy.get("installation")
            if installation not in CODEX_INSTALL_POLICIES:
                result.errors.append(
                    f"{label}: plugins[{idx}].policy.installation ({installation!r}) must be one "
                    f"of {sorted(CODEX_INSTALL_POLICIES)}"
                )
            auth = policy.get("authentication")
            if auth not in CODEX_AUTH_POLICIES:
                result.errors.append(
                    f"{label}: plugins[{idx}].policy.authentication ({auth!r}) must be one "
                    f"of {sorted(CODEX_AUTH_POLICIES)}"
                )

        category = plugin.get("category")
        if not isinstance(category, str) or not category.strip():
            result.errors.append(f"{label}: plugins[{idx}].category must be a non-empty string")

    return result


def _is_valid_archive_url(url: str) -> tuple[bool, str | None]:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        return False, f"archive URL must use https:// (got scheme {parsed.scheme!r})"
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        return (
            False,
            f"archive URL must use https:// for non-localhost host ({parsed.hostname!r})",
        )
    if not parsed.netloc:
        return False, "archive URL is missing host"
    if not parsed.path.lower().endswith(".zip"):
        return False, f"archive URL path must end with '.zip' (got {parsed.path!r})"
    return True, None


def validate_antigravity_marketplace(path: str | Path | dict[str, Any]) -> ValidationResult:
    """Validate a Google Antigravity marketplace.json catalog (archive mode)."""
    result = ValidationResult()
    label = "Antigravity marketplace"
    data, err = _load_json_input(path, label)
    if err or data is None:
        result.errors.append(err or f"Failed to load {label}")
        return result

    if not isinstance(data.get("name"), str) or not data["name"].strip():
        result.errors.append(f"{label}: missing required non-empty string 'name'")
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        result.errors.append(f"{label}: missing required non-empty string 'description'")

    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        result.errors.append(f"{label}: missing required list 'plugins'")
        return result

    seen_names: set[str] = set()
    for idx, plugin in enumerate(plugins):
        if not isinstance(plugin, dict):
            result.errors.append(f"{label}: plugins[{idx}] must be an object")
            continue

        pname = plugin.get("name")
        if not isinstance(pname, str) or not pname.strip():
            result.errors.append(f"{label}: plugins[{idx}] missing required non-empty 'name'")
        else:
            if not KEBAB_CASE_RE.match(pname) or len(pname) > 64:
                result.errors.append(
                    f"{label}: plugins[{idx}].name ({pname!r}) must be lowercase kebab-case"
                )
            brand_hit = _has_disallowed_brand_prefix(pname)
            if brand_hit:
                result.errors.append(
                    f"{label}: plugins[{idx}].name ({pname!r}) must not start with branded "
                    f"prefix '{brand_hit}'"
                )
            if pname in seen_names:
                result.errors.append(f"{label}: duplicate plugin name '{pname}'")
            seen_names.add(pname)

        display_name = plugin.get("displayName")
        if display_name is not None:
            if not isinstance(display_name, str) or not display_name.strip():
                result.errors.append(
                    f"{label}: plugins[{idx}].displayName must be a non-empty string"
                )
            else:
                brand_hit = _has_disallowed_brand_prefix(display_name)
                if brand_hit:
                    result.errors.append(
                        f"{label}: plugins[{idx}].displayName ({display_name!r}) must not start "
                        f"with branded prefix '{brand_hit}'"
                    )

        for opt_key in ("description", "version"):
            if opt_key in plugin and not isinstance(plugin[opt_key], str):
                result.errors.append(f"{label}: plugins[{idx}].{opt_key} must be a string")

        source = plugin.get("source")
        if not isinstance(source, dict):
            result.errors.append(f"{label}: plugins[{idx}].source must be an object")
            continue

        src_type = source.get("type")
        if src_type == "archive":
            url = source.get("url")
            if not isinstance(url, str) or not url.strip():
                result.errors.append(
                    f"{label}: plugins[{idx}].source.url must be a non-empty string"
                )
            else:
                ok, url_err = _is_valid_archive_url(url)
                if not ok and url_err:
                    result.errors.append(f"{label}: plugins[{idx}].source.url: {url_err}")

            sha256 = source.get("sha256")
            if not isinstance(sha256, str) or not SHA256_HEX_RE.match(sha256):
                result.errors.append(
                    f"{label}: plugins[{idx}].source.sha256 must be a 64-character lowercase "
                    f"hex SHA-256 digest"
                )
        elif src_type == "local":
            if not isinstance(source.get("path"), str) or not source["path"].strip():
                result.errors.append(
                    f"{label}: plugins[{idx}].source.path must be a non-empty string"
                )
        elif src_type in ("git", "github"):
            if not isinstance(source.get("url"), str) and not isinstance(source.get("repo"), str):
                result.errors.append(
                    f"{label}: plugins[{idx}].source must specify 'url' or 'repo' for '{src_type}'"
                )
        else:
            result.errors.append(
                f"{label}: plugins[{idx}].source.type ({src_type!r}) must be 'archive', "
                f"'local', 'git', or 'github'"
            )

    return result


def _validate_simple_plugin_manifest(
    manifest: dict[str, Any], label: str, result: ValidationResult
) -> None:
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        result.errors.append(f"{label}: missing required non-empty string 'name'")
    for key in ("version", "description"):
        if key in manifest and not isinstance(manifest[key], str):
            result.errors.append(f"{label}: '{key}' must be a string")
    if "author" in manifest:
        author = manifest["author"]
        if isinstance(author, dict):
            if "name" in author and not isinstance(author["name"], str):
                result.errors.append(f"{label}: 'author.name' must be a string")
            if "email" in author and not isinstance(author["email"], str):
                result.errors.append(f"{label}: 'author.email' must be a string")
            if "url" in author and not isinstance(author["url"], str):
                result.errors.append(f"{label}: 'author.url' must be a string")
        elif not isinstance(author, str):
            result.errors.append(f"{label}: 'author' must be a string or object")


def validate_claude_plugin_bundle(plugin_dir: str | Path) -> ValidationResult:
    """Validate a Claude Code plugin bundle ('.claude-plugin/plugin.json', '.mcp.json', agents, skills)."""
    result = ValidationResult()
    plugin_root = Path(plugin_dir)
    if not plugin_root.is_dir():
        result.errors.append(f"Plugin directory not found: {plugin_root}")
        return result

    manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
    manifest, err = _load_json_input(manifest_path, ".claude-plugin/plugin.json")
    if err or manifest is None:
        result.errors.append(err or "Missing .claude-plugin/plugin.json")
    else:
        _validate_simple_plugin_manifest(manifest, ".claude-plugin/plugin.json", result)

    mcp_path = plugin_root / ".mcp.json"
    if mcp_path.is_file():
        mcp_data, mcp_err = _load_json_input(mcp_path, ".mcp.json")
        if mcp_err or mcp_data is None:
            result.errors.append(mcp_err or "Invalid .mcp.json")
        else:
            servers = mcp_data.get("mcpServers")
            if not isinstance(servers, dict):
                result.errors.append(".mcp.json: 'mcpServers' must be an object")
            else:
                for server_name, cfg in servers.items():
                    if not isinstance(cfg, dict):
                        result.errors.append(
                            f".mcp.json: server '{server_name}' must be an object"
                        )
                        continue
                    srv_type = cfg.get("type", "stdio" if "command" in cfg else "http")
                    if srv_type not in ("streamable-http", "http", "sse", "stdio"):
                        result.errors.append(
                            f".mcp.json: server '{server_name}' invalid type {srv_type!r}"
                        )
                    elif srv_type == "stdio":
                        if not isinstance(cfg.get("command"), str) or not cfg["command"].strip():
                            result.errors.append(
                                f".mcp.json: stdio server '{server_name}' missing non-empty "
                                f"'command'"
                            )
                    else:
                        if not isinstance(cfg.get("url"), str) or not cfg["url"].strip():
                            result.errors.append(
                                f".mcp.json: remote server '{server_name}' missing non-empty 'url'"
                            )
                    if "headers" in cfg and not isinstance(cfg["headers"], dict):
                        result.errors.append(
                            f".mcp.json: server '{server_name}' 'headers' must be an object"
                        )

    _validate_antigravity_agents(plugin_root, result)
    _validate_skills_dir(plugin_root / "skills", result)
    return result


def validate_copilot_plugin_bundle(plugin_dir: str | Path) -> ValidationResult:
    """Validate a VS Code / GitHub Copilot plugin bundle (Agent Plugins 1.0 + Copilot agents)."""
    result = validate_agent_plugin_bundle(plugin_dir)
    plugin_root = Path(plugin_dir)
    if not plugin_root.is_dir():
        return result

    copilot_agents_dir = plugin_root / "com.github.copilot" / "agents"
    if copilot_agents_dir.is_dir():
        for md_file in sorted(copilot_agents_dir.glob("*.md")):
            if not md_file.name.endswith(".agent.md"):
                result.errors.append(
                    f"{md_file}: Copilot custom agent files must end with '.agent.md'"
                )
                continue
            frontmatter, _body, fm_err = parse_frontmatter(md_file.read_text(encoding="utf-8"))
            if fm_err or frontmatter is None:
                result.errors.append(f"{md_file}: {fm_err}")
                continue
            if not isinstance(frontmatter.get("name"), str) or not frontmatter["name"].strip():
                result.errors.append(f"{md_file}: Copilot agent missing non-empty 'name'")
            if (
                not isinstance(frontmatter.get("description"), str)
                or not frontmatter["description"].strip()
            ):
                result.errors.append(f"{md_file}: Copilot agent missing non-empty 'description'")
            if "tools" in frontmatter:
                tools = frontmatter["tools"]
                valid_tools = (
                    isinstance(tools, list) and all(isinstance(t, str) for t in tools)
                ) or isinstance(tools, str)
                if not valid_tools:
                    result.errors.append(
                        f"{md_file}: Copilot agent 'tools' must be a list of strings or string"
                    )

    return result


def validate_codex_plugin_bundle(plugin_dir: str | Path) -> ValidationResult:
    """Validate an OpenAI Codex plugin bundle (root plugin.json + .codex-plugin/plugin.json)."""
    result = ValidationResult()
    plugin_root = Path(plugin_dir)
    if not plugin_root.is_dir():
        result.errors.append(f"Plugin directory not found: {plugin_root}")
        return result

    root_manifest_path = plugin_root / "plugin.json"
    codex_manifest_path = plugin_root / ".codex-plugin" / "plugin.json"

    root_name: str | None = None
    if root_manifest_path.is_file():
        result.extend(validate_agent_plugin_bundle(plugin_root))
        root_data, _ = _load_json_input(root_manifest_path, "plugin.json")
        if isinstance(root_data, dict):
            if isinstance(root_data.get("name"), str):
                root_name = root_data["name"]
            extensions = root_data.get("extensions")
            if not isinstance(extensions, dict) or "com.openai" not in extensions:
                result.warnings.append(
                    "Root 'plugin.json' has no 'extensions[\"com.openai\"]' section for Codex."
                )
    else:
        result.errors.append(f"Missing root 'plugin.json' in {plugin_root}")

    codex_manifest, codex_err = _load_json_input(codex_manifest_path, ".codex-plugin/plugin.json")
    if codex_err or codex_manifest is None:
        result.errors.append(codex_err or "Missing .codex-plugin/plugin.json")
    else:
        _validate_simple_plugin_manifest(codex_manifest, ".codex-plugin/plugin.json", result)
        codex_name = codex_manifest.get("name")
        if root_name and isinstance(codex_name, str) and codex_name != root_name:
            result.errors.append(
                f".codex-plugin/plugin.json 'name' ({codex_name!r}) must match root "
                f"plugin.json 'name' ({root_name!r})"
            )

    return result
