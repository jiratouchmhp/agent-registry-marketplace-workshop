"""Static validation, workload alignment, and security invariant tests for Workstream A (terraform/core/)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TF_CORE_DIR = REPO_ROOT / "terraform" / "core"
WORKSHOP_SPEC_PATH = REPO_ROOT / "WORKSHOP_SPEC.json"
SAMPLES_MCP_APP = REPO_ROOT / "samples" / "mcp-server" / "app.py"
SAMPLES_A2A_APP = REPO_ROOT / "samples" / "a2a-agent" / "app.py"
STARTER_PLUGIN_DIR = REPO_ROOT / "starter-marketplace" / "plugins" / "gcp-finops-toolkit"

MAX_AGENT_REGISTRY_SPEC_BYTES = 10 * 1024

EXPECTED_TF_FILES = [
    "versions.tf",
    "variables.tf",
    "apis.tf",
    "iam.tf",
    "cloud_run.tf",
    "agent_registry_services.tf",
    "skills_bootstrap.tf",
    "outputs.tf",
]


def _strip_hcl_comments(text: str) -> str:
    """Remove # and // line comments and /* ... */ block comments outside quotes."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def _extract_hcl_blocks(
    text: str, kind: str, block_type: str | None = None
) -> list[tuple[list[str], str]]:
    """Extract top-level brace-balanced HCL blocks of a given kind (resource, data, variable, output)."""
    cleaned = _strip_hcl_comments(text)
    if block_type is not None:
        header_re = re.compile(
            rf'(?m)^\s*{re.escape(kind)}\s+"{re.escape(block_type)}"(?:\s+"([^"]+)")?\s*\{{'
        )
    else:
        header_re = re.compile(
            rf'(?m)^\s*{re.escape(kind)}(?:\s+"([^"]+)")?(?:\s+"([^"]+)")?\s*\{{'
        )

    blocks: list[tuple[list[str], str]] = []
    for match in header_re.finditer(cleaned):
        labels = [g for g in match.groups() if g is not None]
        if block_type is not None:
            labels = [block_type, *labels]

        start = match.end()
        depth = 1
        i = start
        n = len(cleaned)
        in_str = False
        while i < n and depth > 0:
            c = cleaned[i]
            if in_str:
                if c == "\\" and i + 1 < n:
                    i += 2
                    continue
                if c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
            i += 1
        body = cleaned[start : i - 1]
        blocks.append((labels, body))
    return blocks


def _extract_jsonencode_arg(block_body: str) -> str:
    """Extract the balanced parenthesized argument passed to jsonencode(...)."""
    idx = block_body.find("jsonencode(")
    assert idx != -1, "Expected jsonencode(...) call in block body"
    start = idx + len("jsonencode(")
    depth = 1
    i = start
    n = len(block_body)
    in_str = False
    while i < n and depth > 0:
        c = block_body[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
        i += 1
    assert depth == 0, "Unbalanced parentheses in jsonencode(...)"
    return block_body[start : i - 1].strip()


class _HclLiteralParser:
    """Minimal recursive-descent parser for HCL object/list/primitive literals inside jsonencode(...)."""

    def __init__(self, src: str, interpolations: dict[str, str] | None = None) -> None:
        self.src = src
        self.n = len(src)
        self.pos = 0
        self.interpolations = interpolations or {}

    def _skip_ws(self) -> None:
        while self.pos < self.n and self.src[self.pos] in " \t\r\n,":
            self.pos += 1

    def parse(self) -> Any:
        self._skip_ws()
        val = self._parse_value()
        self._skip_ws()
        return val

    def _parse_value(self) -> Any:
        self._skip_ws()
        ch = self.src[self.pos]
        if ch == "{":
            return self._parse_object()
        if ch == "[":
            return self._parse_list()
        if ch == '"':
            return self._parse_string()
        if self.src.startswith("true", self.pos):
            self.pos += 4
            return True
        if self.src.startswith("false", self.pos):
            self.pos += 5
            return False
        if self.src.startswith("null", self.pos):
            self.pos += 4
            return None
        num_match = re.match(r"-?\d+(?:\.\d+)?", self.src[self.pos :])
        if num_match:
            self.pos += num_match.end()
            raw = num_match.group(0)
            return float(raw) if "." in raw else int(raw)
        raise ValueError(f"Unexpected HCL token at offset {self.pos}: {self.src[self.pos : self.pos + 30]!r}")

    def _parse_string(self) -> str:
        assert self.src[self.pos] == '"'
        self.pos += 1
        chars: list[str] = []
        while self.pos < self.n:
            c = self.src[self.pos]
            if c == "\\":
                self.pos += 1
                esc = self.src[self.pos]
                chars.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(esc, esc))
                self.pos += 1
                continue
            if c == '"':
                self.pos += 1
                raw = "".join(chars)
                for expr, replacement in self.interpolations.items():
                    raw = raw.replace(expr, replacement)
                return raw
            chars.append(c)
            self.pos += 1
        raise ValueError("Unterminated HCL string literal")

    def _parse_list(self) -> list[Any]:
        assert self.src[self.pos] == "["
        self.pos += 1
        items: list[Any] = []
        while True:
            self._skip_ws()
            if self.src[self.pos] == "]":
                self.pos += 1
                return items
            items.append(self._parse_value())

    def _parse_object(self) -> dict[str, Any]:
        assert self.src[self.pos] == "{"
        self.pos += 1
        obj: dict[str, Any] = {}
        while True:
            self._skip_ws()
            if self.src[self.pos] == "}":
                self.pos += 1
                return obj
            if self.src[self.pos] == '"':
                key = self._parse_string()
            else:
                key_match = re.match(r"[A-Za-z_][A-Za-z0-9_-]*", self.src[self.pos :])
                if not key_match:
                    raise ValueError(
                        f"Expected object key at offset {self.pos}: {self.src[self.pos : self.pos + 30]!r}"
                    )
                key = key_match.group(0)
                self.pos += key_match.end()
            self._skip_ws()
            assert self.src[self.pos] in ("=", ":"), f"Expected '=' after key {key!r}"
            self.pos += 1
            obj[key] = self._parse_value()


def _load_python_module(module_name: str, file_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _all_tf_texts() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(TF_CORE_DIR.glob("*.tf"))}


def test_required_files_exist() -> None:
    for filename in [*EXPECTED_TF_FILES, "terraform.tfvars.example"]:
        path = TF_CORE_DIR / filename
        assert path.is_file(), f"Missing expected Terraform Core file: {path}"


def test_hclfmt_check_all_tf_files_and_tfvars_example() -> None:
    hcl_files = [*sorted(TF_CORE_DIR.glob("*.tf")), TF_CORE_DIR / "terraform.tfvars.example"]
    assert len(hcl_files) >= len(EXPECTED_TF_FILES) + 1

    terraform_bin = shutil.which("terraform")
    hclfmt_bin = os.environ.get("HCLFMT") or shutil.which("hclfmt")
    if terraform_bin:
        result = subprocess.run(
            [terraform_bin, "fmt", "-check", "-diff", str(TF_CORE_DIR)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"terraform fmt check failed for {TF_CORE_DIR}:\n"
            f"stderr: {result.stderr}\nstdout: {result.stdout[:500]}"
        )
        return

    if not hclfmt_bin or not Path(hclfmt_bin).is_file():
        pytest.skip("Neither terraform nor hclfmt found on PATH (set HCLFMT env var to run).")

    for hcl_file in hcl_files:
        result = subprocess.run(
            [hclfmt_bin, "-check", "-require-no-change", str(hcl_file)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"hclfmt check failed for {hcl_file.name}:\n"
            f"stderr: {result.stderr}\nstdout: {result.stdout[:500]}"
        )


def test_versions_ga_provider_and_no_beta() -> None:
    versions_hcl = _strip_hcl_comments((TF_CORE_DIR / "versions.tf").read_text(encoding="utf-8"))
    assert re.search(r'required_version\s*=\s*">=\s*1\.6\.0"', versions_hcl), (
        "versions.tf must require Terraform >= 1.6.0"
    )
    assert re.search(r'source\s*=\s*"hashicorp/google"', versions_hcl), (
        "versions.tf must declare hashicorp/google"
    )
    assert re.search(r'version\s*=\s*">=\s*7\.39\.0"', versions_hcl), (
        "versions.tf must pin hashicorp/google >= 7.39.0"
    )

    for name, content in _all_tf_texts().items():
        cleaned = _strip_hcl_comments(content)
        assert "google-beta" not in cleaned, (
            f"{name} must use GA hashicorp/google provider only (found google-beta)"
        )


def test_variables_and_secure_defaults() -> None:
    variables_hcl = (TF_CORE_DIR / "variables.tf").read_text(encoding="utf-8")
    var_blocks = {labels[0]: body for labels, body in _extract_hcl_blocks(variables_hcl, "variable")}

    required_vars = {
        "project_id",
        "location",
        "secondary_location",
        "enable_secondary_region",
        "agent_registry_location",
        "skills_location",
        "force_destroy_staging_bucket",
        "middleware_image",
        "sample_mcp_image",
        "sample_a2a_image",
        "secondary_middleware_image",
        "secondary_sample_mcp_image",
        "secondary_sample_a2a_image",
        "enable_skills_bootstrap",
        "starter_plugin_dir",
        "allow_unauthenticated_workshop_demo",
    }
    missing = required_vars - set(var_blocks)
    assert not missing, f"Missing required variables in variables.tf: {sorted(missing)}"

    assert re.search(r'default\s*=\s*"asia-southeast3"', var_blocks["location"]), (
        "location must default to Bangkok primary (asia-southeast3)"
    )
    assert re.search(r'default\s*=\s*"asia-southeast1"', var_blocks["secondary_location"]), (
        "secondary_location must default to Singapore secondary (asia-southeast1)"
    )
    assert "validation" in var_blocks["secondary_location"], (
        "secondary_location must include a non-empty validation block"
    )
    assert re.search(r"default\s*=\s*false\b", var_blocks["enable_secondary_region"]), (
        "enable_secondary_region must default to false"
    )
    assert re.search(r'default\s*=\s*""', var_blocks["agent_registry_location"]), (
        "agent_registry_location must default to empty string (inherits var.location)"
    )
    assert re.search(r'default\s*=\s*"global"', var_blocks["skills_location"]), (
        'skills_location must default to "global"'
    )
    assert re.search(
        r'contains\(\[\s*"global"\s*,\s*"us"\s*,\s*"eu"\s*\]\s*,\s*var\.skills_location\s*\)',
        var_blocks["skills_location"],
    ), 'skills_location validation must enforce contains(["global", "us", "eu"], var.skills_location)'
    assert re.search(r"default\s*=\s*false\b", var_blocks["force_destroy_staging_bucket"]), (
        "force_destroy_staging_bucket must default to false (safe default)"
    )
    for sec_img_var in (
        "secondary_middleware_image",
        "secondary_sample_mcp_image",
        "secondary_sample_a2a_image",
    ):
        assert re.search(r'default\s*=\s*""', var_blocks[sec_img_var]), (
            f"{sec_img_var} must default to empty string"
        )
    assert re.search(r"default\s*=\s*true\b", var_blocks["enable_skills_bootstrap"])
    assert re.search(r"default\s*=\s*false\b", var_blocks["allow_unauthenticated_workshop_demo"]), (
        "allow_unauthenticated_workshop_demo must default to false (secure by default)"
    )
    assert "starter-marketplace/plugins/gcp-finops-toolkit" in var_blocks["starter_plugin_dir"]


def test_dual_source_skills_variables_and_validations() -> None:
    variables_hcl = (TF_CORE_DIR / "variables.tf").read_text(encoding="utf-8")
    var_blocks = {labels[0]: body for labels, body in _extract_hcl_blocks(variables_hcl, "variable")}

    dual_source_vars = {
        "skills_source_mode",
        "enable_local_standalone_skills",
        "local_skills_dir",
        "local_skills_plugin_name",
        "skills_repo_url",
        "skills_repo_ref",
        "skills_repo_subdir",
        "skills_repo_token_env",
        "skills_repo_plugin_name",
        "skills_per_skill_plugin",
    }
    missing = dual_source_vars - set(var_blocks)
    assert not missing, f"Missing dual-source skills variables in variables.tf: {sorted(missing)}"

    # skills_source_mode must default to "local" and strictly allow only "local" or "git".
    mode_body = var_blocks["skills_source_mode"]
    assert re.search(r'default\s*=\s*"local"', mode_body), (
        'skills_source_mode must default to "local"'
    )
    assert re.search(
        r'contains\(\[\s*"local"\s*,\s*"git"\s*\]\s*,\s*var\.skills_source_mode\s*\)',
        mode_body,
    ), 'skills_source_mode validation must enforce contains(["local", "git"], var.skills_source_mode)'

    assert re.search(r"default\s*=\s*true\b", var_blocks["enable_local_standalone_skills"])
    assert re.search(r'default\s*=\s*"\.\./\.\./local-skills"', var_blocks["local_skills_dir"])
    assert re.search(
        r'default\s*=\s*"local-ops-toolkit"', var_blocks["local_skills_plugin_name"]
    )
    assert re.search(r'default\s*=\s*""', var_blocks["skills_repo_url"])
    assert re.search(r'default\s*=\s*"main"', var_blocks["skills_repo_ref"])
    assert re.search(r'default\s*=\s*""', var_blocks["skills_repo_subdir"])
    assert re.search(r'default\s*=\s*""', var_blocks["skills_repo_token_env"])
    assert re.search(
        r'default\s*=\s*"central-repo-skills"', var_blocks["skills_repo_plugin_name"]
    )
    assert re.search(r"default\s*=\s*false\b", var_blocks["skills_per_skill_plugin"])

    # Security guardrail: skills_repo_token_env accepts only an env var name, never raw tokens.
    assert "validation" in var_blocks["skills_repo_token_env"]
    assert "never" in var_blocks["skills_repo_token_env"].lower()
    assert "validation" in var_blocks["skills_repo_subdir"]


def test_apis_enabled() -> None:
    apis_hcl = _strip_hcl_comments((TF_CORE_DIR / "apis.tf").read_text(encoding="utf-8"))
    expected_apis = [
        "agentregistry.googleapis.com",
        "run.googleapis.com",
        "artifactregistry.googleapis.com",
        "storage.googleapis.com",
        "cloudbuild.googleapis.com",
        "iam.googleapis.com",
    ]
    assert 'resource "google_project_service"' in apis_hcl
    for api in expected_apis:
        assert f'"{api}"' in apis_hcl, f"Missing required API in apis.tf: {api}"


def test_iam_least_privilege_and_p4sa_bucket_access() -> None:
    all_texts = _all_tf_texts()
    iam_hcl = all_texts["iam.tf"]
    iam_cleaned = _strip_hcl_comments(iam_hcl)

    p4sa_local_match = re.search(r'agent_registry_p4sa\s*=\s*"([^"]+)"', iam_cleaned)
    assert p4sa_local_match is not None, "iam.tf must define local.agent_registry_p4sa"
    p4sa_value = p4sa_local_match.group(1)
    assert (
        p4sa_value
        == "service-${data.google_project.current.number}@gcp-sa-agentregistry.iam.gserviceaccount.com"
    )

    sa_blocks = {
        labels[1]: body
        for labels, body in _extract_hcl_blocks(iam_hcl, "resource", "google_service_account")
    }
    assert {"middleware_sa", "ci_publisher_sa", "skill_author_sa"}.issubset(sa_blocks)

    iam_resource_types = [
        "google_project_iam_member",
        "google_project_iam_binding",
        "google_project_iam_policy",
        "google_storage_bucket_iam_member",
        "google_storage_bucket_iam_binding",
        "google_artifact_registry_repository_iam_member",
        "google_cloud_run_v2_service_iam_member",
    ]

    middleware_roles: list[str] = []
    ci_agentregistry_roles: list[str] = []
    author_agentregistry_roles: list[str] = []
    p4sa_bucket_roles: list[str] = []

    for content in all_texts.values():
        for res_type in iam_resource_types:
            for _, body in _extract_hcl_blocks(content, "resource", res_type):
                role_match = re.search(r'role\s*=\s*"([^"]+)"', body)
                role = role_match.group(1) if role_match else ""
                resolved_body = body.replace("${local.agent_registry_p4sa}", p4sa_value)

                if "middleware_sa" in resolved_body:
                    middleware_roles.append(role)
                if "ci_publisher_sa" in resolved_body and role.startswith("roles/agentregistry."):
                    ci_agentregistry_roles.append(role)
                if "skill_author_sa" in resolved_body and role.startswith("roles/agentregistry."):
                    author_agentregistry_roles.append(role)
                if (
                    res_type == "google_storage_bucket_iam_member"
                    and "gcp-sa-agentregistry.iam.gserviceaccount.com" in resolved_body
                ):
                    p4sa_bucket_roles.append(role)
                    assert "local.agent_registry_p4sa" in body, (
                        "P4SA bucket IAM binding should reuse local.agent_registry_p4sa"
                    )

    assert middleware_roles == ["roles/agentregistry.viewer"], (
        f"middleware_sa must receive ONLY roles/agentregistry.viewer, got: {middleware_roles}"
    )
    assert ci_agentregistry_roles == ["roles/agentregistry.editor"], (
        f"ci_publisher_sa must receive ONLY roles/agentregistry.editor, got: {ci_agentregistry_roles}"
    )
    assert author_agentregistry_roles == ["roles/agentregistry.user"], (
        f"skill_author_sa must receive ONLY roles/agentregistry.user, got: {author_agentregistry_roles}"
    )
    assert p4sa_bucket_roles == ["roles/storage.objectViewer"], (
        f"Agent Registry P4SA must receive roles/storage.objectViewer on staging bucket, got: {p4sa_bucket_roles}"
    )

    ar_repos = {
        labels[1]: body
        for labels, body in _extract_hcl_blocks(
            iam_hcl, "resource", "google_artifact_registry_repository"
        )
    }
    assert {"workshop_images", "workshop_images_secondary"} == set(ar_repos)
    assert re.search(r'format\s*=\s*"DOCKER"', ar_repos["workshop_images"])
    assert re.search(r"location\s*=\s*var\.location\b", ar_repos["workshop_images"])
    assert re.search(
        r"count\s*=\s*var\.enable_secondary_region\s*\?\s*1\s*:\s*0",
        ar_repos["workshop_images_secondary"],
    )
    assert re.search(
        r"location\s*=\s*var\.secondary_location\b", ar_repos["workshop_images_secondary"]
    )
    assert re.search(r'format\s*=\s*"DOCKER"', ar_repos["workshop_images_secondary"])

    ar_iam_members = {
        labels[1]: body
        for labels, body in _extract_hcl_blocks(
            iam_hcl, "resource", "google_artifact_registry_repository_iam_member"
        )
    }
    assert "ci_publisher_ar_writer_secondary" in ar_iam_members
    assert re.search(
        r"count\s*=\s*var\.enable_secondary_region\s*\?\s*1\s*:\s*0",
        ar_iam_members["ci_publisher_ar_writer_secondary"],
    )
    assert re.search(
        r'role\s*=\s*"roles/artifactregistry\.writer"',
        ar_iam_members["ci_publisher_ar_writer_secondary"],
    )

    buckets = _extract_hcl_blocks(iam_hcl, "resource", "google_storage_bucket")
    assert len(buckets) == 1
    assert re.search(r"uniform_bucket_level_access\s*=\s*true", buckets[0][1])
    assert re.search(r'public_access_prevention\s*=\s*"enforced"', buckets[0][1])
    assert re.search(r"force_destroy\s*=\s*var\.force_destroy_staging_bucket\b", buckets[0][1])


def test_cloud_run_services_and_middleware_env_vars() -> None:
    cloud_run_hcl = (TF_CORE_DIR / "cloud_run.tf").read_text(encoding="utf-8")
    services = {
        labels[1]: body
        for labels, body in _extract_hcl_blocks(
            cloud_run_hcl, "resource", "google_cloud_run_v2_service"
        )
    }
    assert {
        "sample_mcp_server",
        "sample_a2a_agent",
        "polyglot_middleware",
        "sample_mcp_server_secondary",
        "sample_a2a_agent_secondary",
        "polyglot_middleware_secondary",
    } == set(services)

    assert re.search(
        r'agent_registry_location\s*=\s*var\.agent_registry_location\s*!=\s*""\s*\?\s*var\.agent_registry_location\s*:\s*var\.location',
        cloud_run_hcl,
    ), "cloud_run.tf must define local.agent_registry_location defaulting to var.location"

    assert re.search(r'name\s*=\s*"sample-mcp-server"', services["sample_mcp_server"])
    assert re.search(r'name\s*=\s*"sample-a2a-agent"', services["sample_a2a_agent"])
    assert re.search(r'name\s*=\s*"polyglot-middleware"', services["polyglot_middleware"])

    # sample_a2a_agent must advertise its deterministic HTTPS Cloud Run URL via A2A_PUBLIC_URL.
    assert re.search(r'name\s*=\s*"A2A_PUBLIC_URL"', services["sample_a2a_agent"])
    assert "https://sample-a2a-agent-${data.google_project.current.number}.${var.location}.run.app" in cloud_run_hcl
    assert "A2A_PUBLIC_URL" not in services["sample_mcp_server"]
    assert "A2A_PUBLIC_URL" not in services["polyglot_middleware"]

    mw_body = services["polyglot_middleware"]
    assert "google_service_account.middleware_sa.email" in mw_body
    for env_name in [
        "GCP_PROJECT_ID",
        "GCP_LOCATION",
        "GCP_SECONDARY_LOCATION",
        "AGENT_REGISTRY_LOCATION",
        "AGENT_REGISTRY_SKILLS_LOCATION",
        "AGENT_REGISTRY_BASE_URL",
        "MIDDLEWARE_PUBLIC_URL",
        "REQUIRE_KMS_SIGNATURE",
    ]:
        assert re.search(rf'name\s*=\s*"{env_name}"', mw_body), (
            f"polyglot_middleware missing env var {env_name}"
        )

    assert re.search(r'GCP_SECONDARY_LOCATION[\s\S]*?var\.secondary_location', mw_body)
    assert re.search(r'AGENT_REGISTRY_LOCATION[\s\S]*?local\.agent_registry_location', mw_body)
    assert re.search(r'AGENT_REGISTRY_SKILLS_LOCATION[\s\S]*?var\.skills_location', mw_body)
    assert '"https://agentregistry.googleapis.com/v1"' in mw_body
    assert re.search(r'REQUIRE_KMS_SIGNATURE[\s\S]*?"false"', mw_body)

    # Secondary-region Cloud Run v2 services (Singapore active-standby, opt-in via enable_secondary_region)
    for sec_svc in (
        "sample_mcp_server_secondary",
        "sample_a2a_agent_secondary",
        "polyglot_middleware_secondary",
    ):
        sec_body = services[sec_svc]
        assert re.search(
            r"count\s*=\s*var\.enable_secondary_region\s*\?\s*1\s*:\s*0", sec_body
        ), f"{sec_svc} must be gated by count = var.enable_secondary_region ? 1 : 0"
        assert re.search(r"location\s*=\s*var\.secondary_location\b", sec_body), (
            f"{sec_svc} must deploy to var.secondary_location"
        )

    assert "google_service_account.middleware_sa.email" in services["polyglot_middleware_secondary"]
    assert re.search(
        r'AGENT_REGISTRY_LOCATION[\s\S]*?local\.agent_registry_location',
        services["polyglot_middleware_secondary"],
    )
    assert re.search(
        r'AGENT_REGISTRY_SKILLS_LOCATION[\s\S]*?var\.skills_location',
        services["polyglot_middleware_secondary"],
    )
    assert re.search(r'name\s*=\s*"A2A_PUBLIC_URL"', services["sample_a2a_agent_secondary"])
    assert (
        "https://sample-a2a-agent-${data.google_project.current.number}.${var.secondary_location}.run.app"
        in cloud_run_hcl
    )

    run_iam_blocks = _extract_hcl_blocks(
        cloud_run_hcl, "resource", "google_cloud_run_v2_service_iam_member"
    )
    run_iam_by_name = {labels[1]: body for labels, body in run_iam_blocks}
    assert len(run_iam_blocks) == 6
    for sec_iam in (
        "middleware_secondary_unauthenticated_demo",
        "sample_mcp_secondary_unauthenticated_demo",
        "sample_a2a_secondary_unauthenticated_demo",
    ):
        assert sec_iam in run_iam_by_name
        assert re.search(
            r"count\s*=\s*var\.enable_secondary_region\s*&&\s*var\.allow_unauthenticated_workshop_demo\s*\?\s*1\s*:\s*0",
            run_iam_by_name[sec_iam],
        ), f"{sec_iam} must require both enable_secondary_region and allow_unauthenticated_workshop_demo"
    for labels, body in run_iam_blocks:
        if "allUsers" in body:
            assert "var.allow_unauthenticated_workshop_demo" in body, (
                f"{labels[1]} grants allUsers without allow_unauthenticated_workshop_demo guard"
            )


def test_agent_registry_services_and_a2a_no_top_level_interfaces() -> None:
    spec = json.loads(WORKSHOP_SPEC_PATH.read_text(encoding="utf-8"))
    ar_hcl = (TF_CORE_DIR / "agent_registry_services.tf").read_text(encoding="utf-8")

    services = {
        labels[1]: body
        for labels, body in _extract_hcl_blocks(
            ar_hcl, "resource", "google_agent_registry_service"
        )
    }
    assert {"finops_mcp", "finops_a2a_agent", "finops_endpoint"} == set(services)

    for name, body in services.items():
        assert not re.search(r"(?m)^\s*labels\s*=", body), (
            f"google_agent_registry_service.{name} must not define unsupported `labels` attribute"
        )
        assert re.search(r"location\s*=\s*local\.agent_registry_location\b", body), (
            f"google_agent_registry_service.{name} must use local.agent_registry_location"
        )

    # 1. MCP Server service: TOOL_SPEC + JSONRPC interfaces
    mcp_body = services["finops_mcp"]
    assert re.search(rf'service_id\s*=\s*"{spec["sample_plugin"]["mcp_service_id"]}"', mcp_body)
    assert re.search(r"mcp_server_spec\s*\{", mcp_body)
    assert re.search(r'type\s*=\s*"TOOL_SPEC"', mcp_body)
    assert "jsonencode(" in mcp_body
    assert re.search(r"(?m)^\s*interfaces\s*\{", mcp_body)
    assert re.search(r'protocol_binding\s*=\s*"JSONRPC"', mcp_body)
    assert "reservation" not in mcp_body.lower(), (
        "finops_mcp description/spec should describe idle BigQuery dataset deletion, not reservations"
    )

    # 2. A2A Agent service: A2A_AGENT_CARD and STRICTLY NO top-level `interfaces` block!
    a2a_body = services["finops_a2a_agent"]
    assert re.search(rf'service_id\s*=\s*"{spec["sample_plugin"]["agent_service_id"]}"', a2a_body)
    assert re.search(r"agent_spec\s*\{", a2a_body)
    assert re.search(r'type\s*=\s*"A2A_AGENT_CARD"', a2a_body)
    assert "jsonencode(" in a2a_body
    assert "supportedInterfaces" in a2a_body
    assert "defaultInputModes" in a2a_body
    assert "defaultOutputModes" in a2a_body
    assert not re.search(r"(?m)^\s*interfaces\s*\{", a2a_body), (
        "google_agent_registry_service.finops_a2a_agent with A2A_AGENT_CARD MUST NOT "
        "declare a top-level `interfaces` block"
    )

    # 3. Endpoint service: NO_SPEC + HTTP_JSON interfaces
    ep_body = services["finops_endpoint"]
    assert re.search(rf'service_id\s*=\s*"{spec["sample_plugin"]["endpoint_service_id"]}"', ep_body)
    assert re.search(r"endpoint_spec\s*\{", ep_body)
    assert re.search(r'type\s*=\s*"NO_SPEC"', ep_body)
    assert re.search(r"(?m)^\s*interfaces\s*\{", ep_body)
    assert re.search(r'protocol_binding\s*=\s*"HTTP_JSON"', ep_body)

    # 4. Data sources resolve directly via basename(google_agent_registry_service.<r>.registry_resource)
    mcp_ds = _extract_hcl_blocks(ar_hcl, "data", "google_agent_registry_mcp_server")
    agent_ds = _extract_hcl_blocks(ar_hcl, "data", "google_agent_registry_agent")
    ep_ds = _extract_hcl_blocks(ar_hcl, "data", "google_agent_registry_endpoint")

    assert len(mcp_ds) == 1
    assert re.search(r"location\s*=\s*local\.agent_registry_location\b", mcp_ds[0][1])
    assert "basename(google_agent_registry_service.finops_mcp.registry_resource)" in mcp_ds[0][1]
    assert not re.search(r"(?m)^\s*filter\s*=", mcp_ds[0][1])

    assert len(agent_ds) == 1
    assert re.search(r"location\s*=\s*local\.agent_registry_location\b", agent_ds[0][1])
    assert (
        "basename(google_agent_registry_service.finops_a2a_agent.registry_resource)"
        in agent_ds[0][1]
    )
    assert not re.search(r"(?m)^\s*filter\s*=", agent_ds[0][1])

    assert len(ep_ds) == 1
    assert re.search(r"location\s*=\s*local\.agent_registry_location\b", ep_ds[0][1])
    assert "basename(google_agent_registry_service.finops_endpoint.registry_resource)" in ep_ds[0][1]
    assert not re.search(r"(?m)^\s*filter\s*=", ep_ds[0][1])


def test_agent_registry_specs_match_sample_workloads_and_size_limits() -> None:
    ar_hcl = (TF_CORE_DIR / "agent_registry_services.tf").read_text(encoding="utf-8")
    services = {
        labels[1]: body
        for labels, body in _extract_hcl_blocks(
            ar_hcl, "resource", "google_agent_registry_service"
        )
    }

    # Parse MCP TOOL_SPEC from Terraform and compare verbatim with samples/mcp-server/app.py TOOLS.
    mcp_spec_raw = _extract_jsonencode_arg(services["finops_mcp"])
    parsed_mcp_spec = _HclLiteralParser(mcp_spec_raw).parse()
    mcp_spec_bytes = json.dumps(parsed_mcp_spec, separators=(",", ":")).encode("utf-8")
    assert len(mcp_spec_bytes) <= MAX_AGENT_REGISTRY_SPEC_BYTES, (
        f"MCP TOOL_SPEC exceeds 10 KB Agent Registry limit ({len(mcp_spec_bytes)} bytes)"
    )

    sample_mcp_mod = _load_python_module("sample_mcp_server_app", SAMPLES_MCP_APP)
    assert parsed_mcp_spec == {"tools": sample_mcp_mod.TOOLS}, (
        "Terraform finops_mcp TOOL_SPEC must match samples/mcp-server/app.py TOOLS verbatim"
    )

    # Parse A2A_AGENT_CARD from Terraform and compare verbatim with samples/a2a-agent/app.py.
    fake_a2a_base = "https://sample-a2a-agent-123456789012.us-central1.run.app"
    a2a_card_raw = _extract_jsonencode_arg(services["finops_a2a_agent"])
    parsed_a2a_card = _HclLiteralParser(
        a2a_card_raw,
        interpolations={"${google_cloud_run_v2_service.sample_a2a_agent.uri}": fake_a2a_base},
    ).parse()
    a2a_card_bytes = json.dumps(parsed_a2a_card, separators=(",", ":")).encode("utf-8")
    assert len(a2a_card_bytes) <= MAX_AGENT_REGISTRY_SPEC_BYTES, (
        f"A2A_AGENT_CARD exceeds 10 KB Agent Registry limit ({len(a2a_card_bytes)} bytes)"
    )

    sample_a2a_mod = _load_python_module("sample_a2a_agent_app", SAMPLES_A2A_APP)
    assert parsed_a2a_card == sample_a2a_mod.build_agent_card(fake_a2a_base), (
        "Terraform finops_a2a_agent A2A_AGENT_CARD must match samples/a2a-agent/app.py build_agent_card()"
    )


def _assert_no_services_in_payload(obj: Any) -> None:
    """Recursively verify any sync-plan JSON payload contains zero MCP/A2A services."""
    if isinstance(obj, dict):
        if "mcp_services" in obj:
            assert obj["mcp_services"] == [], (
                "skills_bootstrap.tf MUST NOT sync MCP services (owned by agent_registry_services.tf)"
            )
        if "agent_services" in obj:
            assert obj["agent_services"] == [], (
                "skills_bootstrap.tf MUST NOT sync A2A agent services (owned by agent_registry_services.tf)"
            )
        for value in obj.values():
            _assert_no_services_in_payload(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_services_in_payload(item)


def test_skills_bootstrap_uses_terraform_data_and_skills_only_runtime_smoke(
    tmp_path: Path,
) -> None:
    skills_hcl = (TF_CORE_DIR / "skills_bootstrap.tf").read_text(encoding="utf-8")
    tf_data_blocks = _extract_hcl_blocks(skills_hcl, "resource", "terraform_data")
    assert len(tf_data_blocks) == 1

    labels, body = tf_data_blocks[0]
    assert labels == ["terraform_data", "bootstrap_starter_skills"]
    assert re.search(r"count\s*=\s*var\.enable_skills_bootstrap\s*\?\s*1\s*:\s*0", body)
    assert "triggers_replace" in body
    for trigger_key in [
        "skills_location",
        "skills_source_mode",
        "local_skills_dir",
        "local_skills_content_hash",
        "skills_repo_url",
        "skills_repo_ref",
        "skills_repo_subdir",
        "skills_repo_plugin_name",
        "skills_per_skill_plugin",
    ]:
        assert re.search(rf"(?m)^\s*{re.escape(trigger_key)}\s*=", body), (
            f"triggers_replace in skills_bootstrap.tf missing {trigger_key}"
        )

    assert "filesha256(" in skills_hcl
    assert "try(fileset(" in skills_hcl, (
        "skills_bootstrap.tf must guard fileset(...) with try(...) so git mode never fails if local dirs are absent"
    )
    assert 'repo_root = abspath("${path.module}/../..")' in skills_hcl
    assert re.search(r"working_dir\s*=\s*local\.repo_root", body), (
        "local-exec must set working_dir = local.repo_root to avoid ModuleNotFoundError"
    )
    assert re.search(r"PYTHONPATH\s*=\s*local\.repo_root", body), (
        "local-exec must set PYTHONPATH = local.repo_root to import tools.plugin_sync"
    )
    assert "precondition" in body, (
        "terraform_data.bootstrap_starter_skills should declare a lifecycle precondition for git mode skills_repo_url"
    )
    assert re.search(r"location\s*=\s*local\.agent_registry_location\b", body), (
        "skills_bootstrap.tf must target local.agent_registry_location"
    )
    assert re.search(r"skills_location\s*=\s*var\.skills_location\b", body), (
        "skills_bootstrap.tf must track var.skills_location in triggers_replace"
    )
    assert re.search(r"GCP_LOCATION\s*=\s*local\.agent_registry_location\b", body), (
        "skills_bootstrap.tf local-exec must pass local.agent_registry_location"
    )
    assert re.search(r"AGENT_REGISTRY_SKILLS_LOCATION\s*=\s*var\.skills_location\b", body), (
        "skills_bootstrap.tf local-exec must pass var.skills_location via AGENT_REGISTRY_SKILLS_LOCATION"
    )
    assert "-m tools.plugin_sync ingest" in body
    assert "--wait-for-scan" in body, (
        "skills_bootstrap.tf must pass --wait-for-scan so ENABLED_BLOCKING scans settle during apply"
    )
    assert "--only-skills" in body
    assert "--skip-mcp-probe" in body
    assert "google_agent_registry_skill" in skills_hcl
    assert "google_agent_registry_service" in skills_hcl
    assert "agent_registry_services.tf" in skills_hcl
    assert "plugin-sync" in skills_hcl
    assert "gcloud" in skills_hcl
    assert "starter-marketplace/plugins/gcp-finops-toolkit" in skills_hcl

    # Smoke check 1: `python -m tools.plugin_sync --help` succeeds with the configured cwd/PYTHONPATH.
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    help_proc = subprocess.run(
        [sys.executable, "-m", "tools.plugin_sync", "--help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0, f"tools.plugin_sync --help failed: {help_proc.stderr}"

    # Smoke check 2: Execute the EXACT dedented local-exec heredoc from skills_bootstrap.tf
    # across Option 1 ("local") and Option 2 ("git") branches using a stubbed `.venv/bin/python`
    # that records every CLI invocation verbatim.
    heredoc_match = re.search(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT", body, re.DOTALL)
    assert heredoc_match is not None, "Could not locate <<-EOT command heredoc in skills_bootstrap.tf"
    fake_repo_root = tmp_path / "repo"
    stub_python = fake_repo_root / ".venv" / "bin" / "python"
    stub_python.parent.mkdir(parents=True, exist_ok=True)
    stub_python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['STUB_LOG_FILE'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    stub_python.chmod(0o755)

    rendered_cmd = (
        textwrap.dedent(heredoc_match.group(1))
        .replace("${local.repo_root}", str(fake_repo_root))
        .replace("$${", "${")
    )
    spec = json.loads(WORKSHOP_SPEC_PATH.read_text(encoding="utf-8"))
    fake_local_skills = tmp_path / "local-skills"
    fake_local_skills.mkdir()

    def _run_bootstrap(overrides: dict[str, str], log_name: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        log_file = tmp_path / f"{log_name}.jsonl"
        run_env = {
            **env,
            "STUB_LOG_FILE": str(log_file),
            "GCP_PROJECT_ID": spec["project_defaults"]["project_id"],
            "GCP_LOCATION": spec["project_defaults"]["location"],
            "AGENT_REGISTRY_SKILLS_LOCATION": spec["project_defaults"]["skills_location"],
            "AGENT_REGISTRY_BASE_URL": "https://agentregistry.googleapis.com/v1",
            "SKILL_STAGING_BUCKET": "acme-agent-governance-agent-registry-skills",
            "SKILLS_SOURCE_MODE": "local",
            "PLUGIN_DIR": str(STARTER_PLUGIN_DIR),
            "ENABLE_LOCAL_STANDALONE_SKILLS": "true",
            "LOCAL_SKILLS_DIR": str(fake_local_skills),
            "LOCAL_SKILLS_PLUGIN_NAME": "local-ops-toolkit",
            "SKILLS_REPO_URL": "",
            "SKILLS_REPO_REF": "main",
            "SKILLS_REPO_SUBDIR": "",
            "SKILLS_REPO_TOKEN_ENV": "",
            "SKILLS_REPO_PLUGIN_NAME": "central-repo-skills",
            "SKILLS_PER_SKILL_PLUGIN": "false",
            "MCP_SERVICE_ID": spec["sample_plugin"]["mcp_service_id"],
            "AGENT_SERVICE_ID": spec["sample_plugin"]["agent_service_id"],
            "CUSTOM_BOOTSTRAP_CMD": "",
            **overrides,
        }
        proc = subprocess.run(
            ["/bin/bash", "-euo", "pipefail", "-c", rendered_cmd],
            cwd=REPO_ROOT,
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
        )
        calls: list[list[str]] = []
        if log_file.exists():
            calls = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line]
        return proc, calls

    # 2a. Option 1 (local): starter plugin + standalone local skills (grouped under local-ops-toolkit).
    proc_local, calls_local = _run_bootstrap({}, "local_both")
    assert proc_local.returncode == 0, f"local mode failed: {proc_local.stderr}"
    assert len(calls_local) == 2
    for call in calls_local:
        assert call[:3] == ["-m", "tools.plugin_sync", "ingest"]
        assert "--source-type" in call and call[call.index("--source-type") + 1] == "local"
        assert "--only-skills" in call, "Every local-mode ingest call MUST pass --only-skills"
        assert "--skip-mcp-probe" in call, "Every local-mode ingest call MUST pass --skip-mcp-probe"
        assert "--wait-for-scan" in call, "Every local-mode ingest call MUST pass --wait-for-scan"
        assert call[call.index("--location") + 1] == "global"
        assert call[call.index("--skills-location") + 1] == "global"
        assert "--target-state" in call and call[call.index("--target-state") + 1] == "TARGET_STATE_ACTIVE"
        assert "--scanning-policy" in call and call[call.index("--scanning-policy") + 1] == "ENABLED_BLOCKING"
    assert calls_local[0][calls_local[0].index("--path") + 1] == str(STARTER_PLUGIN_DIR)
    assert calls_local[1][calls_local[1].index("--path") + 1] == str(fake_local_skills)
    assert calls_local[1][calls_local[1].index("--plugin-name") + 1] == "local-ops-toolkit"
    assert "--per-skill-plugin" not in calls_local[1]

    # 2b. Option 1 (local) with --per-skill-plugin enabled.
    proc_psp, calls_psp = _run_bootstrap({"SKILLS_PER_SKILL_PLUGIN": "true"}, "local_per_skill")
    assert proc_psp.returncode == 0, f"local per-skill mode failed: {proc_psp.stderr}"
    assert len(calls_psp) == 2
    assert "--per-skill-plugin" in calls_psp[1]
    assert "--only-skills" in calls_psp[1]

    # 2c. Option 1 (local) with standalone skills disabled or directory absent.
    proc_no_standalone, calls_no_standalone = _run_bootstrap(
        {"ENABLE_LOCAL_STANDALONE_SKILLS": "false"}, "local_disabled_standalone"
    )
    assert proc_no_standalone.returncode == 0
    assert len(calls_no_standalone) == 1

    proc_missing_dir, calls_missing_dir = _run_bootstrap(
        {"LOCAL_SKILLS_DIR": str(tmp_path / "does-not-exist")}, "local_missing_dir"
    )
    assert proc_missing_dir.returncode == 0
    assert len(calls_missing_dir) == 1

    # 2d. Option 2 (git): MUST fail fast when SKILLS_REPO_URL is empty or whitespace.
    for case_name, bad_url in [("git_empty_url", ""), ("git_ws_url", "   ")]:
        proc_bad, calls_bad = _run_bootstrap(
            {"SKILLS_SOURCE_MODE": "git", "SKILLS_REPO_URL": bad_url}, case_name
        )
        assert proc_bad.returncode != 0, f"git mode must fail fast for SKILLS_REPO_URL={bad_url!r}"
        assert calls_bad == []
        assert "skills_repo_url" in proc_bad.stderr.lower()

    # 2e. Option 2 (git): full arguments including --ref, --subdir, --git-token-env, --per-skill-plugin.
    repo_url = "https://gitlab.example.com/ai-platform/agent-marketplace.git"
    proc_git_full, calls_git_full = _run_bootstrap(
        {
            "SKILLS_SOURCE_MODE": "git",
            "SKILLS_REPO_URL": repo_url,
            "SKILLS_REPO_REF": "release/v1",
            "SKILLS_REPO_SUBDIR": "plugins",
            "SKILLS_REPO_TOKEN_ENV": "GITLAB_TOKEN",
            "SKILLS_REPO_PLUGIN_NAME": "central-repo-skills",
            "SKILLS_PER_SKILL_PLUGIN": "true",
        },
        "git_full",
    )
    assert proc_git_full.returncode == 0, f"git full mode failed: {proc_git_full.stderr}"
    assert len(calls_git_full) == 1
    git_call = calls_git_full[0]
    assert git_call[:3] == ["-m", "tools.plugin_sync", "ingest"]
    assert git_call[git_call.index("--source-type") + 1] == "git"
    assert git_call[git_call.index("--repo-url") + 1] == repo_url
    assert git_call[git_call.index("--ref") + 1] == "release/v1"
    assert git_call[git_call.index("--subdir") + 1] == "plugins"
    assert git_call[git_call.index("--git-token-env") + 1] == "GITLAB_TOKEN"
    assert git_call[git_call.index("--plugin-name") + 1] == "central-repo-skills"
    assert "--per-skill-plugin" in git_call
    assert "--only-skills" in git_call, "git-mode ingest call MUST pass --only-skills"
    assert "--skip-mcp-probe" in git_call, "git-mode ingest call MUST pass --skip-mcp-probe"
    assert "--wait-for-scan" in git_call, "git-mode ingest call MUST pass --wait-for-scan"

    # 2f. Option 2 (git): minimal optional args omitted cleanly.
    proc_git_min, calls_git_min = _run_bootstrap(
        {"SKILLS_SOURCE_MODE": "git", "SKILLS_REPO_URL": repo_url}, "git_minimal"
    )
    assert proc_git_min.returncode == 0, f"git minimal mode failed: {proc_git_min.stderr}"
    assert len(calls_git_min) == 1
    assert "--subdir" not in calls_git_min[0]
    assert "--git-token-env" not in calls_git_min[0]
    assert "--per-skill-plugin" not in calls_git_min[0]
    assert "--only-skills" in calls_git_min[0]
    assert "--skip-mcp-probe" in calls_git_min[0]
    assert "--wait-for-scan" in calls_git_min[0]

    # 2g. Custom command override bypasses default plugin_sync invocations.
    proc_custom, calls_custom = _run_bootstrap(
        {"CUSTOM_BOOTSTRAP_CMD": "echo custom-ok"}, "custom_cmd"
    )
    assert proc_custom.returncode == 0
    assert proc_custom.stdout.strip() == "custom-ok"
    assert calls_custom == []

    # 2h. Unsupported mode fails fast.
    proc_invalid_mode, calls_invalid_mode = _run_bootstrap(
        {"SKILLS_SOURCE_MODE": "invalid"}, "invalid_mode"
    )
    assert proc_invalid_mode.returncode != 0
    assert calls_invalid_mode == []

    # Smoke check 3: When `tools.plugin_sync ingest` is registered, execute a real `--dry-run`
    # with the exact Terraform flags to confirm CLI flag alignment and zero Service mutations.
    ingest_help = subprocess.run(
        [sys.executable, "-m", "tools.plugin_sync", "ingest", "--help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if ingest_help.returncode == 0:
        dry_run_cmd = [
            sys.executable,
            "-m",
            "tools.plugin_sync",
            "ingest",
            "--source-type",
            "local",
            "--path",
            str(STARTER_PLUGIN_DIR),
            "--registry-url",
            "https://agentregistry.googleapis.com/v1",
            "--project",
            spec["project_defaults"]["project_id"],
            "--location",
            spec["project_defaults"]["location"],
            "--target-state",
            "TARGET_STATE_ACTIVE",
            "--scanning-policy",
            "ENABLED_BLOCKING",
            "--only-skills",
            "--skip-mcp-probe",
            "--dry-run",
        ]
        if "--wait-for-scan" in ingest_help.stdout:
            dry_run_cmd.append("--wait-for-scan")

        real_dry_run = subprocess.run(
            dry_run_cmd,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert real_dry_run.returncode == 0, (
            f"Real `tools.plugin_sync ingest --only-skills --dry-run` failed:\n"
            f"stderr: {real_dry_run.stderr}\nstdout: {real_dry_run.stdout}"
        )
        payload = json.loads(real_dry_run.stdout)
        _assert_no_services_in_payload(payload)


def test_tfvars_example_documents_both_skill_source_options() -> None:
    tfvars_text = (TF_CORE_DIR / "terraform.tfvars.example").read_text(encoding="utf-8")
    uncommented = _strip_hcl_comments(tfvars_text)

    # Bangkok primary + Singapore secondary multi-region defaults + two-stage Cloud Build guide.
    assert re.search(r'(?m)^\s*location\s*=\s*"asia-southeast3"', uncommented), (
        'terraform.tfvars.example must default location to "asia-southeast3" (Bangkok)'
    )
    assert re.search(r'(?m)^\s*secondary_location\s*=\s*"asia-southeast1"', uncommented), (
        'terraform.tfvars.example must default secondary_location to "asia-southeast1" (Singapore)'
    )
    assert re.search(r'(?m)^\s*skills_location\s*=\s*"global"', uncommented), (
        'terraform.tfvars.example must default skills_location to "global"'
    )
    assert re.search(r"(?m)^\s*enable_secondary_region\s*=\s*false\b", uncommented), (
        "terraform.tfvars.example must set enable_secondary_region = false by default"
    )
    assert re.search(r"(?m)^\s*force_destroy_staging_bucket\s*=\s*false\b", uncommented)
    assert "scripts/build_images_cloudbuild.sh" in tfvars_text, (
        "terraform.tfvars.example must document two-stage Cloud Build image script"
    )
    assert "asia-southeast3-docker.pkg.dev" in uncommented

    # Option 1 must be active by default.
    assert re.search(r'(?m)^\s*skills_source_mode\s*=\s*"local"', uncommented), (
        'terraform.tfvars.example must set skills_source_mode = "local" active by default'
    )
    assert re.search(r"(?m)^\s*enable_local_standalone_skills\s*=\s*true\b", uncommented)
    assert re.search(r'(?m)^\s*local_skills_dir\s*=\s*"\.\./\.\./local-skills"', uncommented)
    assert re.search(r'(?m)^\s*local_skills_plugin_name\s*=\s*"local-ops-toolkit"', uncommented)

    # Option 2 must be clearly documented as a commented example (not overriding Option 1).
    assert not re.search(r'(?m)^\s*skills_source_mode\s*=\s*"git"', uncommented), (
        'Option 2 (skills_source_mode = "git") should be commented out by default in terraform.tfvars.example'
    )
    for opt2_field in [
        'skills_source_mode      = "git"',
        "skills_repo_url",
        "skills_repo_ref",
        "skills_repo_subdir",
        "skills_repo_token_env",
        "skills_repo_plugin_name",
    ]:
        assert opt2_field in tfvars_text, (
            f"terraform.tfvars.example must document Option 2 setting: {opt2_field}"
        )
    assert "Option 1" in tfvars_text
    assert "Option 2" in tfvars_text
    assert "NEVER put raw tokens in .tfvars" in tfvars_text


def test_required_outputs_exported() -> None:
    outputs_hcl = (TF_CORE_DIR / "outputs.tf").read_text(encoding="utf-8")
    output_blocks = {labels[0]: body for labels, body in _extract_hcl_blocks(outputs_hcl, "output")}
    exported = set(output_blocks)

    required_outputs = {
        "project_id",
        "project_number",
        "location",
        "secondary_location",
        "agent_registry_location",
        "skills_location",
        "middleware_url",
        "middleware_mcp_url",
        "secondary_middleware_url",
        "secondary_middleware_mcp_url",
        "secondary_artifact_registry_repository_url",
        "middleware_sa_email",
        "mcp_server_resource_id",
        "mcp_server_urn",
        "a2a_agent_resource_id",
        "a2a_agent_urn",
        "endpoint_resource_id",
        "agent_registry_p4sa",
        "mcp_service_name",
        "mcp_registry_resource",
        "agent_service_name",
        "agent_registry_resource",
        "endpoint_service_name",
        "endpoint_registry_resource",
    }
    missing = required_outputs - exported
    assert not missing, f"Missing required outputs in outputs.tf: {sorted(missing)}"

    assert (
        'try(google_cloud_run_v2_service.polyglot_middleware_secondary[0].uri, "")'
        in output_blocks["secondary_middleware_url"]
    )
    assert "try(" in output_blocks["secondary_middleware_mcp_url"]
    assert "try(" in output_blocks["secondary_artifact_registry_repository_url"]
    assert "local.agent_registry_location" in output_blocks["agent_registry_location"]


def test_zero_references_to_security_governance() -> None:
    for path in sorted(TF_CORE_DIR.iterdir()):
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            assert "security_governance" not in content.lower(), (
                f"Workstream A Core file {path.name} must have ZERO references to security_governance"
            )
