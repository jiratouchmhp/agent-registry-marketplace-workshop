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
        "middleware_image",
        "sample_mcp_image",
        "sample_a2a_image",
        "enable_skills_bootstrap",
        "starter_plugin_dir",
        "allow_unauthenticated_workshop_demo",
    }
    missing = required_vars - set(var_blocks)
    assert not missing, f"Missing required variables in variables.tf: {sorted(missing)}"

    assert re.search(r'default\s*=\s*"us-central1"', var_blocks["location"])
    assert re.search(r"default\s*=\s*true\b", var_blocks["enable_skills_bootstrap"])
    assert re.search(r"default\s*=\s*false\b", var_blocks["allow_unauthenticated_workshop_demo"]), (
        "allow_unauthenticated_workshop_demo must default to false (secure by default)"
    )
    assert "starter-marketplace/plugins/gcp-finops-toolkit" in var_blocks["starter_plugin_dir"]


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

    ar_repos = _extract_hcl_blocks(iam_hcl, "resource", "google_artifact_registry_repository")
    assert len(ar_repos) == 1
    assert re.search(r'format\s*=\s*"DOCKER"', ar_repos[0][1])

    buckets = _extract_hcl_blocks(iam_hcl, "resource", "google_storage_bucket")
    assert len(buckets) == 1
    assert re.search(r"uniform_bucket_level_access\s*=\s*true", buckets[0][1])
    assert re.search(r'public_access_prevention\s*=\s*"enforced"', buckets[0][1])


def test_cloud_run_services_and_middleware_env_vars() -> None:
    cloud_run_hcl = (TF_CORE_DIR / "cloud_run.tf").read_text(encoding="utf-8")
    services = {
        labels[1]: body
        for labels, body in _extract_hcl_blocks(
            cloud_run_hcl, "resource", "google_cloud_run_v2_service"
        )
    }
    assert {"sample_mcp_server", "sample_a2a_agent", "polyglot_middleware"} == set(services)

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
        "AGENT_REGISTRY_BASE_URL",
        "MIDDLEWARE_PUBLIC_URL",
        "REQUIRE_KMS_SIGNATURE",
    ]:
        assert re.search(rf'name\s*=\s*"{env_name}"', mw_body), (
            f"polyglot_middleware missing env var {env_name}"
        )

    assert '"https://agentregistry.googleapis.com/v1"' in mw_body
    assert re.search(r'REQUIRE_KMS_SIGNATURE[\s\S]*?"false"', mw_body)

    run_iam_blocks = _extract_hcl_blocks(
        cloud_run_hcl, "resource", "google_cloud_run_v2_service_iam_member"
    )
    assert len(run_iam_blocks) >= 1
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
    assert "basename(google_agent_registry_service.finops_mcp.registry_resource)" in mcp_ds[0][1]
    assert not re.search(r"(?m)^\s*filter\s*=", mcp_ds[0][1])

    assert len(agent_ds) == 1
    assert (
        "basename(google_agent_registry_service.finops_a2a_agent.registry_resource)"
        in agent_ds[0][1]
    )
    assert not re.search(r"(?m)^\s*filter\s*=", agent_ds[0][1])

    assert len(ep_ds) == 1
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


def test_skills_bootstrap_uses_terraform_data_and_skills_only_runtime_smoke() -> None:
    skills_hcl = (TF_CORE_DIR / "skills_bootstrap.tf").read_text(encoding="utf-8")
    tf_data_blocks = _extract_hcl_blocks(skills_hcl, "resource", "terraform_data")
    assert len(tf_data_blocks) == 1

    _, body = tf_data_blocks[0]
    assert re.search(r"count\s*=\s*var\.enable_skills_bootstrap\s*\?\s*1\s*:\s*0", body)
    assert "triggers_replace" in body
    assert "filesha256(" in skills_hcl
    assert "fileset(" in skills_hcl
    assert 'repo_root = abspath("${path.module}/../..")' in skills_hcl
    assert re.search(r"working_dir\s*=\s*local\.repo_root", body), (
        "local-exec must set working_dir = local.repo_root to avoid ModuleNotFoundError"
    )
    assert re.search(r"PYTHONPATH\s*=\s*local\.repo_root", body), (
        "local-exec must set PYTHONPATH = local.repo_root to import tools.plugin_sync"
    )
    assert "probe_mcp=False" in body
    assert "plan.mcp_services.clear()" in body
    assert "plan.agent_services.clear()" in body
    assert "apply_sync_plan(" in body
    assert "google_agent_registry_skill" in skills_hcl
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

    # Smoke check 2: Execute the EXACT dedented local-exec heredoc from skills_bootstrap.tf in dry_run mode.
    heredoc_match = re.search(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT", body, re.DOTALL)
    assert heredoc_match is not None, "Could not locate <<-EOT command heredoc in skills_bootstrap.tf"
    rendered_cmd = textwrap.dedent(heredoc_match.group(1)).replace(
        "${local.repo_root}", str(REPO_ROOT)
    )
    assert "dry_run=False" in rendered_cmd
    dry_run_cmd = rendered_cmd.replace("dry_run=False", "dry_run=True")

    spec = json.loads(WORKSHOP_SPEC_PATH.read_text(encoding="utf-8"))
    run_env = {
        **env,
        "GCP_PROJECT_ID": spec["project_defaults"]["project_id"],
        "GCP_LOCATION": spec["project_defaults"]["location"],
        "AGENT_REGISTRY_BASE_URL": "https://agentregistry.googleapis.com/v1",
        "SKILL_STAGING_BUCKET": "acme-agent-governance-agent-registry-skills",
        "PLUGIN_DIR": str(STARTER_PLUGIN_DIR),
        "MCP_SERVICE_ID": spec["sample_plugin"]["mcp_service_id"],
        "AGENT_SERVICE_ID": spec["sample_plugin"]["agent_service_id"],
        "CUSTOM_BOOTSTRAP_CMD": "",
    }
    exec_proc = subprocess.run(
        ["/bin/bash", "-euo", "pipefail", "-c", dry_run_cmd],
        cwd=REPO_ROOT,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert exec_proc.returncode == 0, (
        f"skills_bootstrap.tf local-exec dry-run failed:\n"
        f"stderr: {exec_proc.stderr}\nstdout: {exec_proc.stdout}"
    )

    payload = json.loads(exec_proc.stdout)
    assert payload["mcp_services"] == [], (
        "skills_bootstrap.tf MUST NOT sync MCP services (owned by agent_registry_services.tf)"
    )
    assert payload["agent_services"] == [], (
        "skills_bootstrap.tf MUST NOT sync A2A agent services (owned by agent_registry_services.tf)"
    )
    skill_ids = [s["raw_skill_id"] for s in payload["skills"]]
    assert sorted(skill_ids) == sorted(spec["sample_plugin"]["skills"])
    for skill in payload["skills"]:
        assert skill["private_skill_id"] == f"private-{skill['raw_skill_id']}"
        meta = skill["frontmatter_metadata"]
        assert meta["mcp_service_id"] == spec["sample_plugin"]["mcp_service_id"]
        assert meta["agent_service_id"] == spec["sample_plugin"]["agent_service_id"]


def test_required_outputs_exported() -> None:
    outputs_hcl = (TF_CORE_DIR / "outputs.tf").read_text(encoding="utf-8")
    exported = {labels[0] for labels, _ in _extract_hcl_blocks(outputs_hcl, "output")}

    required_outputs = {
        "project_id",
        "project_number",
        "location",
        "middleware_url",
        "middleware_mcp_url",
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


def test_zero_references_to_security_governance() -> None:
    for path in sorted(TF_CORE_DIR.iterdir()):
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            assert "security_governance" not in content.lower(), (
                f"Workstream A Core file {path.name} must have ZERO references to security_governance"
            )
