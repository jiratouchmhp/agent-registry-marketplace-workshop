#!/usr/bin/env python3
"""Starter Exercise Verification Harness for Labs 01-04.

Default mode:
  1. Validates ``starter/lab01_plugin_skeleton`` against both Agent Plugins 1.0
     (must pass) and Google Antigravity v1 (prints clear attendee TODO items).
  2. Executes ``starter/lab02_plugin_sync_exercise.py``,
     ``starter/lab03_middleware_redirect_exercise.py``, and
     ``starter/lab04_hub_sync_adapter_exercise.py`` to confirm clean execution
     with zero syntax or import errors.

``--verify-reference-solutions`` mode:
  Copies ``starter/`` into a temporary directory (leaving the repository
  completely untouched), supplies reference implementations for Labs 01-04,
  and proves that ALL 4 starter self-check harnesses reach ``[PASS]`` / ``[DONE]``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx

from schemas.validators import validate_agent_plugin_bundle, validate_antigravity_bundle

STARTER_DIR = REPO_ROOT / "starter"
EXERCISE_FILES: tuple[tuple[str, str], ...] = (
    ("Lab 02", "lab02_plugin_sync_exercise.py"),
    ("Lab 03", "lab03_middleware_redirect_exercise.py"),
    ("Lab 04", "lab04_hub_sync_adapter_exercise.py"),
)

# -----------------------------------------------------------------------------
# Reference Solutions (applied ONLY to temporary copies in --verify-reference-solutions)
# -----------------------------------------------------------------------------

LAB02_REFERENCE_IMPL = r'''
# --- Reference implementations injected into temporary copy for verification ---

def canonical_private_skill_id(raw_skill_id: str) -> str:
    cleaned = raw_skill_id.strip()
    if not cleaned:
        raise ValueError("raw_skill_id must not be empty")
    if cleaned.startswith(PRIVATE_SKILL_PREFIX):
        return cleaned
    return f"{PRIVATE_SKILL_PREFIX}{cleaned}"


def enrich_skill_frontmatter(
    skill_md_text: str,
    plugin_metadata: dict[str, str],
) -> str:
    frontmatter, body, err = parse_frontmatter(skill_md_text)
    if err or frontmatter is None:
        raise ValueError(f"Invalid SKILL.md frontmatter: {err}")

    existing_meta = frontmatter.get("metadata") or {}
    if not isinstance(existing_meta, dict):
        raise ValueError("Existing metadata must be a mapping")

    merged_meta: dict[str, str] = {}
    for k, v in existing_meta.items():
        if isinstance(v, (dict, list)):
            raise ValueError(f"Nested metadata value not allowed for key {k!r}")
        merged_meta[str(k)] = str(v)

    for k, v in plugin_metadata.items():
        if isinstance(v, (dict, list)):
            raise ValueError(f"Nested metadata value not allowed for key {k!r}")
        merged_meta[str(k)] = str(v)

    frontmatter["metadata"] = merged_meta
    yaml_block = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    clean_body = body.lstrip("\n")
    return f"---\n{yaml_block}\n---\n\n{clean_body}\n"


def build_single_skill_zip(
    skill_dir: Path,
    plugin_metadata: dict[str, str],
) -> SkillArchiveArtifact:
    skill_md_path = skill_dir / "SKILL.md"
    validation = validate_skill_md(skill_md_path, expected_name=skill_dir.name)
    if not validation.valid:
        raise ValueError(f"Invalid SKILL.md: {validation.errors}")

    enriched_md = enrich_skill_frontmatter(
        skill_md_path.read_text(encoding="utf-8"),
        plugin_metadata,
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(skill_dir.rglob("*")):
            if item.is_symlink():
                raise ValueError(f"Symlink forbidden in skill archive: {item}")
            if item.is_dir():
                continue
            rel = item.relative_to(skill_dir)
            if ".." in rel.parts:
                raise ValueError(f"Path traversal forbidden: {rel}")
            arcname = f"{skill_dir.name}/{rel.as_posix()}"
            raw = enriched_md.encode("utf-8") if item == skill_md_path else item.read_bytes()
            info = zipfile.ZipInfo(filename=arcname, date_time=(2025, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, raw)

    zip_bytes = buf.getvalue()
    if len(zip_bytes) > MAX_SKILL_ZIP_BYTES:
        raise ValueError(f"Skill ZIP exceeds {MAX_SKILL_ZIP_BYTES} bytes: {len(zip_bytes)}")

    return SkillArchiveArtifact(
        skill_id=skill_dir.name,
        canonical_skill_id=canonical_private_skill_id(skill_dir.name),
        zip_bytes=zip_bytes,
        sha256_hex=hashlib.sha256(zip_bytes).hexdigest(),
    )


def register_skill_revision(
    client: httpx.Client,
    *,
    registry_base_url: str,
    project_id: str,
    location: str,
    artifact: SkillArchiveArtifact,
    display_name: str,
    description: str,
    revision_id: str = "rev-1",
    scanning_policy: str = "ENABLED_BLOCKING",
) -> dict[str, Any]:
    base = registry_base_url.rstrip("/")
    parent = f"projects/{project_id}/locations/{location}"
    skills_url = f"{base}/{parent}/skills"
    skill_resource = f"{parent}/skills/{artifact.canonical_skill_id}"
    encoded = base64.b64encode(artifact.zip_bytes).decode("ascii")

    get_resp = client.get(f"{base}/{skill_resource}")
    if get_resp.status_code == 404:
        create_resp = client.post(
            skills_url,
            params={"skillId": artifact.skill_id},
            json={
                "displayName": display_name,
                "description": description,
                "type": "SIMPLE",
                "targetState": "TARGET_STATE_ACTIVE",
                "scanningConfig": {"policy": scanning_policy},
                "initialRevision": {
                    "archiveUploadSource": {"archiveContent": encoded},
                },
            },
        )
        create_resp.raise_for_status()
        return create_resp.json()

    get_resp.raise_for_status()
    rev_resp = client.post(
        f"{base}/{skill_resource}/revisions",
        params={"skillRevisionId": revision_id},
        json={"archiveUploadSource": {"archiveContent": encoded}},
    )
    rev_resp.raise_for_status()

    patch_resp = client.patch(
        f"{base}/{skill_resource}",
        params={"updateMask": "defaultRevision"},
        json={"defaultRevision": f"{skill_resource}/revisions/{revision_id}"},
    )
    patch_resp.raise_for_status()
    return patch_resp.json()
'''

LAB03_REFERENCE_IMPL = r'''
# --- Reference implementations injected into temporary copy for verification ---

def strip_headers_for_cross_origin_redirect(
    headers: dict[str, str],
    source_url: str,
    target_url: str,
) -> dict[str, str]:
    src = urlparse(source_url)
    dst = urlparse(target_url)
    if (src.scheme.lower(), src.netloc.lower()) == (dst.scheme.lower(), dst.netloc.lower()):
        return dict(headers)
    return {
        k: v
        for k, v in headers.items()
        if k.lower() != "authorization" and not k.lower().startswith("x-goog-")
    }


def download_skill_revision_media(
    client: httpx.Client,
    *,
    media_url: str,
    auth_token: str,
    quota_project: str,
    expected_sha256: str,
    max_redirects: int = 5,
) -> bytes:
    current_url = media_url
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "x-goog-user-project": quota_project,
        "x-goog-api-client": "acme-marketplace-middleware/1.0",
    }
    resp = client.get(current_url, headers=headers, follow_redirects=False)
    hops = 0
    while resp.status_code in (301, 302, 303, 307, 308):
        hops += 1
        if hops > max_redirects:
            raise RuntimeError(f"Exceeded max_redirects={max_redirects}")
        loc = resp.headers.get("Location") or resp.headers.get("location")
        if not loc:
            raise RuntimeError("Redirect response missing Location header")
        next_url = urljoin(current_url, loc)
        headers = strip_headers_for_cross_origin_redirect(headers, current_url, next_url)
        current_url = next_url
        resp = client.get(current_url, headers=headers, follow_redirects=False)

    if resp.status_code != 200:
        raise RuntimeError(f"Expected HTTP 200 downloading media, got {resp.status_code}: {resp.text}")

    data = resp.content
    actual_sha = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_sha.lower(), expected_sha256.strip().lower()):
        raise RuntimeError(f"SHA-256 mismatch: expected {expected_sha256}, got {actual_sha}")
    return data


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
    logo_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
        '<rect width="64" height="64" rx="12" fill="#1a73e8"/>'
        '<text x="32" y="40" text-anchor="middle" fill="#ffffff" font-size="24">AG</text>'
        "</svg>\n"
    )
    files: dict[str, str] = {
        "plugin.json": json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": plugin_name,
                "version": version,
                "description": description,
            },
            indent=2,
        ),
        ".antigravity-plugin/plugin.json": json.dumps(
            {
                "$schema": "https://antigravity.google/schemas/v1/plugin.json",
                "name": plugin_name,
                "displayName": display_name,
                "version": version,
                "description": description,
                "logo": "assets/logo.svg",
            },
            indent=2,
        ),
        ".claude-plugin/plugin.json": json.dumps(
            {"name": plugin_name, "version": version, "description": description},
            indent=2,
        ),
        ".codex-plugin/plugin.json": json.dumps(
            {
                "name": plugin_name,
                "version": version,
                "description": description,
                "interface": {"displayName": display_name},
            },
            indent=2,
        ),
        "mcp.json": json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    mcp_server_key: {"type": "streamable-http", "url": mcp_server_url}
                },
            },
            indent=2,
        ),
        ".mcp.json": json.dumps(
            {"mcpServers": {mcp_server_key: {"type": "http", "url": mcp_server_url}}},
            indent=2,
        ),
        "mcp_config.json": json.dumps(
            {
                "mcpServers": {
                    mcp_server_key: {
                        "url": mcp_server_url,
                        "authProviderType": "google_credentials",
                    }
                }
            },
            indent=2,
        ),
        "assets/logo.svg": logo_svg,
    }
    for skill_name, md in skill_markdowns.items():
        files[f"skills/{skill_name}/SKILL.md"] = md

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname in sorted(files):
            zf.writestr(arcname, files[arcname].encode("utf-8"))
    zip_bytes = buf.getvalue()
    return zip_bytes, hashlib.sha256(zip_bytes).hexdigest()
'''

LAB04_REFERENCE_IMPL = r'''
# --- Reference implementations injected into temporary copy for verification ---

def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_name = fh.name
            json.dump(payload, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def safe_merge_antigravity_mcp_config(
    existing_doc: dict[str, Any],
    desired_servers: dict[str, dict[str, Any]],
    previous_owned_keys: set[str],
    *,
    catalog_complete: bool,
) -> tuple[dict[str, Any], set[str]]:
    updated = copy.deepcopy(existing_doc)
    servers = updated.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        updated["mcpServers"] = servers

    new_owned: set[str] = set()

    for key, desired_cfg in desired_servers.items():
        if key in servers and key not in previous_owned_keys:
            # Never overwrite user-authored server definitions
            continue
        merged_cfg = copy.deepcopy(desired_cfg)
        if key in servers and isinstance(servers[key], dict):
            for field in PRESERVED_USER_FIELDS:
                if field in servers[key]:
                    merged_cfg[field] = copy.deepcopy(servers[key][field])
        servers[key] = merged_cfg
        new_owned.add(key)

    retired_keys = set(previous_owned_keys) - set(desired_servers.keys())
    if catalog_complete:
        for retired in retired_keys:
            servers.pop(retired, None)
    else:
        for retired in retired_keys:
            if retired in servers:
                new_owned.add(retired)

    return updated, new_owned
'''


def _run_python_script(script_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing_pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{existing_pypath}" if existing_pypath else str(REPO_ROOT)
    )
    return subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _inject_reference_impl(script_path: Path, impl_code: str) -> None:
    text = script_path.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    if marker not in text:
        raise RuntimeError(f"Expected {marker!r} in {script_path}")
    patched = text.replace(marker, f"{impl_code}\n\n{marker}", 1)
    script_path.write_text(patched, encoding="utf-8")


def _complete_lab01_reference(plugin_dir: Path) -> None:
    """Add all required Antigravity pre-publish files to a temp copy of Lab 01 skeleton."""
    (plugin_dir / ".antigravity-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".antigravity-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://antigravity.google/schemas/v1/plugin.json",
                "name": "hello-finops-plugin",
                "displayName": "Hello FinOps Plugin",
                "description": "Completed Lab 01 reference bundle passing all Antigravity checks.",
                "version": "1.0.0",
                "logo": "assets/logo.svg",
                "author": {
                    "name": "Acme Platform Security",
                    "email": "platform-security@acme.example.com",
                    "url": "https://acme.example.com/platform",
                },
                "license": "Apache-2.0",
                "keywords": ["finops", "workshop"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (plugin_dir / "assets").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "assets" / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
        '<rect width="64" height="64" rx="12" fill="#1a73e8"/>'
        "</svg>\n",
        encoding="utf-8",
    )

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
        )
        + "\n",
        encoding="utf-8",
    )

    (plugin_dir / "rules").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "rules" / "AGENTS.md").write_text(
        "# FinOps Always-On Rules\n\nAlways inspect read-only billing metrics before changes.\n",
        encoding="utf-8",
    )
    (plugin_dir / "rules" / "finops-guardrails.md").write_text(
        "---\ntrigger: glob\nglobs: \"**/*.sql, **/*.tf\"\n---\n\nAvoid SELECT * in BigQuery.\n",
        encoding="utf-8",
    )

    (plugin_dir / "scripts").mkdir(parents=True, exist_ok=True)
    hook_script = plugin_dir / "scripts" / "finops-guard.sh"
    hook_script.write_text("#!/usr/bin/env bash\nprintf '{}'\n", encoding="utf-8")
    os.chmod(hook_script, 0o755)

    (plugin_dir / "hooks.json").write_text(
        json.dumps(
            {
                "policy-guard": {
                    "enabled": True,
                    "PreToolUse": [
                        {
                            "matcher": "mcp_finops-mcp-server_delete_.*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "./scripts/finops-guard.sh",
                                    "timeout": 10,
                                }
                            ],
                        }
                    ],
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _verify_lab02_register_skill_revision(lab02_path: Path) -> None:
    """Import patched Lab 02 module and verify register_skill_revision() via MockTransport."""
    spec = importlib.util.spec_from_file_location("lab02_ref_verify", lab02_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import patched Lab 02 module from {lab02_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    skill_dir = lab02_path.parent / "lab01_plugin_skeleton" / "skills" / "hello-finops"
    artifact = mod.build_single_skill_zip(
        skill_dir,
        {
            "plugin_name": "hello-finops-plugin",
            "plugin_display_name": "Hello FinOps Plugin",
            "plugin_version": "0.1.0",
            "source_repo": "https://github.com/acme/agent-plugins",
            "git_commit": "abc1234",
            "mcp_service_id": "finops-mcp-server",
            "agent_service_id": "finops-advisor-agent",
        },
    )

    created = {"exists": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/skills/private-hello-finops"):
            if not created["exists"]:
                return httpx.Response(404, json={"error": {"code": 404}})
            return httpx.Response(200, json={"name": path.lstrip("/")})
        if request.method == "POST" and path.endswith("/skills"):
            assert request.url.params.get("skillId") == "hello-finops"
            created["exists"] = True
            return httpx.Response(
                200, json={"name": f"{path.lstrip('/')}/private-hello-finops", "done": True}
            )
        if request.method == "POST" and path.endswith("/skills/private-hello-finops/revisions"):
            return httpx.Response(200, json={"name": f"{path.lstrip('/')}/rev-2"})
        if request.method == "PATCH" and path.endswith("/skills/private-hello-finops"):
            return httpx.Response(200, json={"name": path.lstrip("/"), "defaultRevision": "rev-2"})
        return httpx.Response(400, text=f"Unexpected {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        res_create = mod.register_skill_revision(
            client,
            registry_base_url="https://agentregistry.googleapis.com/v1",
            project_id="acme-agent-governance",
            location="asia-southeast3",
            artifact=artifact,
            display_name="Hello FinOps",
            description="Sample starter skill",
        )
        assert "private-hello-finops" in str(res_create)

        res_update = mod.register_skill_revision(
            client,
            registry_base_url="https://agentregistry.googleapis.com/v1",
            project_id="acme-agent-governance",
            location="asia-southeast3",
            artifact=artifact,
            display_name="Hello FinOps",
            description="Sample starter skill",
            revision_id="rev-2",
        )
        assert res_update.get("defaultRevision") == "rev-2"


def _evaluate_lab01_coexistence_checklist(plugin_dir: Path) -> list[tuple[bool, str]]:
    """Inspect all 6 Lab 01 Antigravity coexistence deliverables beyond schema-only checks."""
    rules_dir = plugin_dir / "rules"
    glob_rules = (
        [p for p in rules_dir.glob("*.md") if p.name != "AGENTS.md"]
        if rules_dir.is_dir()
        else []
    )
    scripts_dir = plugin_dir / "scripts"
    exec_scripts = (
        [
            p
            for p in scripts_dir.iterdir()
            if p.is_file() and os.access(p, os.X_OK)
        ]
        if scripts_dir.is_dir()
        else []
    )
    return [
        (
            (plugin_dir / ".antigravity-plugin" / "plugin.json").is_file(),
            ".antigravity-plugin/plugin.json (Antigravity v1 manifest with logo & displayName)",
        ),
        (
            (plugin_dir / "assets" / "logo.svg").is_file(),
            "assets/logo.svg (1:1 square local SVG logo)",
        ),
        (
            (plugin_dir / "mcp_config.json").is_file(),
            "mcp_config.json (Antigravity MCP config with authProviderType: google_credentials)",
        ),
        (
            (rules_dir / "AGENTS.md").is_file() and bool(glob_rules),
            "rules/AGENTS.md + glob rule (e.g. rules/finops-guardrails.md with comma-separated globs)",
        ),
        (
            (plugin_dir / "hooks.json").is_file(),
            "hooks.json (named top-level group using relative ./scripts/... paths)",
        ),
        (
            bool(exec_scripts),
            "scripts/*.sh with executable permissions (chmod 0755)",
        ),
    ]


def check_current_starter() -> int:
    """Validate starter skeleton and execute Labs 02-04 starter scripts as-is."""
    print("========================================================================")
    print("Workshop Starter Exercises Check (Labs 01-04)")
    print("========================================================================")

    # Lab 01
    lab01_dir = STARTER_DIR / "lab01_plugin_skeleton"
    ap_res = validate_agent_plugin_bundle(lab01_dir)
    ag_res = validate_antigravity_bundle(lab01_dir)
    coexistence_items = _evaluate_lab01_coexistence_checklist(lab01_dir)
    coex_passed = sum(1 for ok, _ in coexistence_items if ok)

    print("\n[Lab 01] starter/lab01_plugin_skeleton")
    if ap_res.valid:
        print("  [PASS] Agent Plugins 1.0 bundle validation passed")
    else:
        print("  [FAIL] Agent Plugins 1.0 validation failed:")
        for err in ap_res.errors:
            print(f"         - {err}")
        return 1

    if ag_res.valid:
        print("  [PASS] Google Antigravity v1 schema validator passed")
    else:
        print(f"  [TODO] Antigravity v1 schema validator items remaining ({len(ag_res.errors)}):")
        for err in ag_res.errors:
            print(f"         - {err}")

    print(
        f"  [INFO] Lab 01 Antigravity Coexistence Checklist ({coex_passed}/{len(coexistence_items)} present):"
    )
    for ok, desc in coexistence_items:
        marker = "[PASS]" if ok else "[TODO]"
        print(f"         {marker} {desc}")

    # Labs 02-04
    for label, filename in EXERCISE_FILES:
        script_path = STARTER_DIR / filename
        print(f"\n[{label}] starter/{filename}")
        proc = _run_python_script(script_path)
        if proc.stdout.strip():
            for line in proc.stdout.strip().splitlines():
                print(f"  {line}")
        if proc.returncode != 0:
            print(f"  [FAIL] Exited with code {proc.returncode}")
            if proc.stderr.strip():
                print(proc.stderr.strip())
            return 1
        print(f"  [OK]   {filename} ran cleanly without syntax/import errors")

    print("\nAll starter skeletons and self-check harnesses executed cleanly.")
    print("Tip: Run with --verify-reference-solutions to verify all 4 harnesses reach [PASS]/[DONE].")
    return 0


def verify_reference_solutions() -> int:
    """Copy starter/ to temp dir, apply reference implementations, and prove all 4 pass."""
    print("========================================================================")
    print("Verifying Reference Solutions Against Starter Harnesses (Temp Sandbox)")
    print("========================================================================")

    with tempfile.TemporaryDirectory(prefix="workshop-starter-ref-") as tmp:
        tmp_starter = Path(tmp) / "starter"
        shutil.copytree(STARTER_DIR, tmp_starter)

        # Lab 01 reference solution
        lab01_copy = tmp_starter / "lab01_plugin_skeleton"
        _complete_lab01_reference(lab01_copy)
        ap_res = validate_agent_plugin_bundle(lab01_copy)
        ag_res = validate_antigravity_bundle(lab01_copy)
        coex_items = _evaluate_lab01_coexistence_checklist(lab01_copy)
        missing_coex = [desc for ok, desc in coex_items if not ok]
        if not ap_res.valid or not ag_res.valid or missing_coex:
            print("[FAIL] Lab 01 reference solution failed validation:")
            for err in ap_res.errors + ag_res.errors + missing_coex:
                print(f"  - {err}")
            return 1
        print(
            f"  [PASS] Lab 01: Agent Plugins 1.0 + Antigravity v1 ({len(coex_items)}/{len(coex_items)} coexistence items + all 7 pre-publish checks)"
        )

        # Lab 02 reference solution
        lab02_copy = tmp_starter / "lab02_plugin_sync_exercise.py"
        _inject_reference_impl(lab02_copy, LAB02_REFERENCE_IMPL)
        proc2 = _run_python_script(lab02_copy)
        if (
            proc2.returncode != 0
            or "[TODO]" in proc2.stdout
            or proc2.stdout.count("[PASS]") < 4
            or "[DONE]" not in proc2.stdout
        ):
            print(f"[FAIL] Lab 02 reference verification failed (rc={proc2.returncode}):")
            print(proc2.stdout)
            print(proc2.stderr)
            return 1
        _verify_lab02_register_skill_revision(lab02_copy)
        print("  [PASS] Lab 02: All 4/4 starter checks reached [DONE]")

        # Lab 03 reference solution
        lab03_copy = tmp_starter / "lab03_middleware_redirect_exercise.py"
        _inject_reference_impl(lab03_copy, LAB03_REFERENCE_IMPL)
        proc3 = _run_python_script(lab03_copy)
        if proc3.returncode != 0 or "[TODO]" in proc3.stdout or "[DONE]" not in proc3.stdout:
            print(f"[FAIL] Lab 03 reference verification failed (rc={proc3.returncode}):")
            print(proc3.stdout)
            print(proc3.stderr)
            return 1
        print("  [PASS] Lab 03: All starter checks reached [DONE]")

        # Lab 04 reference solution
        lab04_copy = tmp_starter / "lab04_hub_sync_adapter_exercise.py"
        _inject_reference_impl(lab04_copy, LAB04_REFERENCE_IMPL)
        proc4 = _run_python_script(lab04_copy)
        if proc4.returncode != 0 or "[TODO]" in proc4.stdout or "[DONE]" not in proc4.stdout:
            print(f"[FAIL] Lab 04 reference verification failed (rc={proc4.returncode}):")
            print(proc4.stdout)
            print(proc4.stderr)
            return 1
        print("  [PASS] Lab 04: All starter checks reached [DONE]")

    print("\n[DONE] Reference solutions passed ALL 4 starter harnesses cleanly!")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate starter exercises (Labs 01-04) and optionally verify reference solutions."
    )
    parser.add_argument(
        "--verify-reference-solutions",
        action="store_true",
        help=(
            "Copy starter/ into a temporary directory, apply reference implementations for "
            "Labs 01-04, and prove all 4 harnesses pass without modifying repository files."
        ),
    )
    args = parser.parse_args(argv)

    rc = check_current_starter()
    if rc != 0:
        return rc
    if args.verify_reference_solutions:
        print("")
        return verify_reference_solutions()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
