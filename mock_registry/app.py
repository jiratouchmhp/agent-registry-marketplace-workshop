"""FastAPI simulator of the Google Cloud Agent Registry v1 REST API.

Faithfully implements:
- Services CRUD projecting onto read-only Agents, McpServers, and Endpoints
- A2A_AGENT_CARD top-level interfaces rejection & spec immutability
- Bindings CRUD + bindings:fetchAvailable with X-Mock-Allowed-Auth-Providers filtering
- Standalone Skills & SkillRevisions with automatic 'private-' prefixing
- Strict ZIP validation, simulated LLM/static secret & prompt-injection scanning
- Skill lifecycle state machine (DRAFT/ACTIVE/DISABLED/DEPRECATED/DECOMMISSIONED)
- ?alt=media 302 redirect to /scotty-signed/{token} with strict 403 header rejection
  if any x-goog-* or Authorization header is leaked across the redirect
- skills:search (KEYWORD & SEMANTIC + filter), findings, publishers, and operations
"""

from __future__ import annotations

import base64
import copy
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from mock_registry.models import (
    PRIVATE_SKILL_PREFIX,
    RESOURCE_ID_RE,
    SPEC_CONTENT_MAX_BYTES,
    TARGET_TO_STATE,
    BindingPayload,
    SearchAgentsPayload,
    SearchMcpServersPayload,
    SearchSkillsPayload,
    ServicePayload,
    SkillPayload,
    SkillRevisionPayload,
    make_google_error,
    normalize_state_filter_value,
    normalize_target_state,
    validate_skill_state_transition,
)
from mock_registry.scanner import (
    SkillArchiveValidationError,
    scan_skill_archive,
    validate_skill_zip,
)

DEFAULT_PROJECT_NUMBER = "123456789012"


class GoogleApiError(Exception):
    """Structured Google Cloud API error returned as JSON."""

    def __init__(self, code: int, status: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_resource_id(value: str | None, field_name: str) -> str:
    if not value or not RESOURCE_ID_RE.match(value):
        raise GoogleApiError(
            400,
            "INVALID_ARGUMENT",
            f"Query parameter '{field_name}' ({value!r}) must match "
            "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$.",
        )
    return value


async def _read_json_body(request: Request, *, allow_empty: bool = False) -> Any:
    """Read and parse a JSON request body, converting malformed JSON into 400 INVALID_ARGUMENT."""
    raw_bytes = await request.body()
    if not raw_bytes or not raw_bytes.strip():
        if allow_empty:
            return {}
        raise GoogleApiError(400, "INVALID_ARGUMENT", "Request body must be a non-empty JSON object.")
    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoogleApiError(400, "INVALID_ARGUMENT", f"Malformed JSON request body: {exc}") from exc


def _parse_spec_content(
    raw_content: dict[str, Any] | str | None,
    *,
    spec_label: str,
    allow_empty: bool,
) -> dict[str, Any]:
    """Validate Agent Registry v1 Struct (dict) or JSON string spec content and enforce 10 KB limit."""
    if raw_content is None:
        if allow_empty:
            return {}
        raise GoogleApiError(
            400,
            "INVALID_ARGUMENT",
            f"{spec_label}.content is required.",
        )

    if isinstance(raw_content, dict):
        if allow_empty:
            if raw_content:
                raise GoogleApiError(
                    400,
                    "INVALID_ARGUMENT",
                    f"{spec_label}.content must be empty when type is 'NO_SPEC'.",
                )
            return {}
        if not raw_content:
            raise GoogleApiError(
                400,
                "INVALID_ARGUMENT",
                f"{spec_label}.content must be a non-empty JSON object.",
            )
        encoded = json.dumps(raw_content, separators=(",", ":")).encode("utf-8")
        if len(encoded) > SPEC_CONTENT_MAX_BYTES:
            raise GoogleApiError(
                400,
                "INVALID_ARGUMENT",
                f"{spec_label}.content exceeds maximum size of {SPEC_CONTENT_MAX_BYTES} bytes.",
            )
        return copy.deepcopy(raw_content)

    # str
    if allow_empty:
        if raw_content.strip():
            raise GoogleApiError(
                400,
                "INVALID_ARGUMENT",
                f"{spec_label}.content must be empty when type is 'NO_SPEC'.",
            )
        return {}

    if not raw_content.strip():
        raise GoogleApiError(
            400,
            "INVALID_ARGUMENT",
            f"{spec_label}.content is required.",
        )
    if len(raw_content.encode("utf-8")) > SPEC_CONTENT_MAX_BYTES:
        raise GoogleApiError(
            400,
            "INVALID_ARGUMENT",
            f"{spec_label}.content exceeds maximum size of {SPEC_CONTENT_MAX_BYTES} bytes.",
        )
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise GoogleApiError(
            400,
            "INVALID_ARGUMENT",
            f"{spec_label}.content must be valid JSON: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise GoogleApiError(
            400,
            "INVALID_ARGUMENT",
            f"{spec_label}.content must decode to a JSON object.",
        )
    return parsed


def _parse_model(model_cls: type[Any], raw_body: Any) -> Any:
    if not isinstance(raw_body, dict):
        raise GoogleApiError(400, "INVALID_ARGUMENT", "Request body must be a JSON object.")
    if "labels" in raw_body:
        raise GoogleApiError(
            400,
            "INVALID_ARGUMENT",
            "Generic 'labels' map is not supported on Agent Registry v1 resources; "
            "use Skill frontmatter.metadata for custom key/value attributes.",
        )
    try:
        return model_cls.model_validate(raw_body)
    except ValidationError as exc:
        raise GoogleApiError(400, "INVALID_ARGUMENT", str(exc)) from exc


def _project_number_for(project: str) -> str:
    if project.isdigit():
        return project
    return DEFAULT_PROJECT_NUMBER


def _tokenize(text: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z0-9]+", text.lower()) if len(tok) >= 2}


def _evaluate_simple_filter(item: dict[str, Any], filter_expr: str) -> bool:
    """Evaluate simple AIP-160 filter clauses (AND/OR with =, !=, <, <=, >, >=, :)."""
    expr = filter_expr.strip()
    if not expr:
        return True

    or_branches = re.split(r"\s+OR\s+", expr)
    for branch in or_branches:
        and_clauses = re.split(r"\s+AND\s+", branch.strip())
        if all(_match_single_clause(item, clause.strip()) for clause in and_clauses if clause.strip()):
            return True
    return False


def _lookup_dotted_field(item: dict[str, Any], dotted_key: str) -> Any:
    current: Any = item
    for part in dotted_key.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _match_single_clause(item: dict[str, Any], clause: str) -> bool:
    cleaned = clause.strip().lstrip("(").rstrip(")").strip()
    if not cleaned:
        return True
    match = re.match(r'^([A-Za-z0-9_.]+)\s*(=|!=|<=|>=|<|>|:)\s*(.+)$', cleaned)
    if not match:
        return False
    field_name, op, raw_val = match.groups()
    expected = raw_val.strip().strip('"').strip("'")
    actual = _lookup_dotted_field(item, field_name)
    if actual is None:
        return False

    if field_name == "state":
        actual_norm = normalize_state_filter_value(str(actual))
        expected_norm = normalize_state_filter_value(expected)
        return (actual_norm == expected_norm) if op == "=" else (actual_norm != expected_norm)

    if field_name == "targetState":
        try:
            actual_norm = normalize_target_state(str(actual))
            expected_norm = normalize_target_state(expected)
        except ValueError:
            return False
        return (actual_norm == expected_norm) if op == "=" else (actual_norm != expected_norm)

    if op == ":":
        return expected.lower() in str(actual).lower()

    # Attempt numeric comparison when both values are numbers.
    try:
        actual_num = float(actual)
        expected_num = float(expected)
        if op == "=":
            return actual_num == expected_num
        if op == "!=":
            return actual_num != expected_num
        if op == "<":
            return actual_num < expected_num
        if op == "<=":
            return actual_num <= expected_num
        if op == ">":
            return actual_num > expected_num
        if op == ">=":
            return actual_num >= expected_num
    except (TypeError, ValueError):
        pass

    actual_str = str(actual)
    if op == "=":
        return actual_str == expected
    if op == "!=":
        return actual_str != expected
    if op == "<":
        return actual_str < expected
    if op == "<=":
        return actual_str <= expected
    if op == ">":
        return actual_str > expected
    if op == ">=":
        return actual_str >= expected
    return False


def _decode_revision_source(
    payload: SkillRevisionPayload,
    gcs_blobs: dict[str, bytes],
) -> bytes:
    has_upload = payload.archiveUploadSource is not None
    has_gcs = payload.gcsSource is not None
    if has_upload == has_gcs:
        raise GoogleApiError(
            400,
            "INVALID_ARGUMENT",
            "SkillRevision must specify exactly one of 'archiveUploadSource' or 'gcsSource'.",
        )
    if payload.archiveUploadSource is not None:
        try:
            return base64.b64decode(payload.archiveUploadSource.archiveContent, validate=True)
        except Exception as exc:
            raise GoogleApiError(
                400,
                "INVALID_ARGUMENT",
                f"Invalid base64 in archiveUploadSource.archiveContent: {exc}",
            ) from exc

    assert payload.gcsSource is not None
    uri = payload.gcsSource.uri
    if uri in gcs_blobs:
        return gcs_blobs[uri]
    if uri.startswith("file://"):
        local_path = Path(uri.removeprefix("file://"))
        if local_path.is_file():
            return local_path.read_bytes()
    raise GoogleApiError(
        404,
        "NOT_FOUND",
        f"GCS object not found in mock registry store: {uri}",
    )


@dataclass
class MockRegistryStore:
    """In-memory state store for the Mock Agent Registry v1 API."""

    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    endpoints: dict[str, dict[str, Any]] = field(default_factory=dict)
    bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    skills: dict[str, dict[str, Any]] = field(default_factory=dict)
    revisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    revision_blobs: dict[str, bytes] = field(default_factory=dict)
    revision_texts: dict[str, dict[str, str]] = field(default_factory=dict)
    findings: dict[str, dict[str, Any]] = field(default_factory=dict)
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    signed_downloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    gcs_blobs: dict[str, bytes] = field(default_factory=dict)

    def reset(self) -> None:
        self.services.clear()
        self.agents.clear()
        self.mcp_servers.clear()
        self.endpoints.clear()
        self.bindings.clear()
        self.skills.clear()
        self.revisions.clear()
        self.revision_blobs.clear()
        self.revision_texts.clear()
        self.findings.clear()
        self.operations.clear()
        self.signed_downloads.clear()
        self.gcs_blobs.clear()

    def create_operation(
        self,
        project: str,
        location: str,
        target: str,
        verb: str,
        response_obj: dict[str, Any],
    ) -> dict[str, Any]:
        op_id = f"op-{uuid.uuid4().hex[:12]}"
        op_name = f"projects/{project}/locations/{location}/operations/{op_id}"
        op = {
            "name": op_name,
            "done": True,
            "metadata": {
                "@type": "type.googleapis.com/google.cloud.agentregistry.v1.OperationMetadata",
                "target": target,
                "verb": verb,
                "createTime": _utc_now(),
            },
            "response": copy.deepcopy(response_obj),
        }
        self.operations[op_name] = op
        return op

    def list_publishers(self, project: str, location: str) -> list[dict[str, Any]]:
        prefix = f"projects/{project}/locations/{location}/publishers"
        return [
            {
                "name": f"{prefix}/cloud.google.com",
                "verifiedPrefix": "google-",
                "displayName": "Google Cloud",
                "publisherTier": "FIRST_PARTY",
                "documentationUri": "https://cloud.google.com/agent-registry/docs",
            },
            {
                "name": f"{prefix}/verified-partners",
                "verifiedPrefix": "partner-",
                "displayName": "Verified Enterprise Partners",
                "publisherTier": "THIRD_PARTY",
                "documentationUri": "https://cloud.google.com/agent-registry/docs/publishers",
            },
            {
                "name": f"{prefix}/private",
                "verifiedPrefix": PRIVATE_SKILL_PREFIX,
                "displayName": f"Private Publisher ({project})",
                "publisherTier": "PRIVATE",
            },
        ]

    def skill_revisions_for(self, skill_name: str) -> list[dict[str, Any]]:
        prefix = f"{skill_name}/revisions/"
        matched = [
            (name, rev)
            for name, rev in self.revisions.items()
            if name.startswith(prefix)
        ]
        matched.sort(key=lambda pair: (str(pair[1].get("createTime", "")), pair[0]))
        return [copy.deepcopy(rev) for _name, rev in matched]


def _validate_and_project_service(
    project: str,
    location: str,
    service_id: str,
    payload: ServicePayload,
    *,
    now: str,
    create_time: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Validate Service oneof spec constraints and build projected read-only resource."""
    specs_present = [
        ("agentSpec", payload.agentSpec),
        ("mcpServerSpec", payload.mcpServerSpec),
        ("endpointSpec", payload.endpointSpec),
    ]
    active_specs = [(k, v) for k, v in specs_present if v is not None]
    if len(active_specs) != 1:
        raise GoogleApiError(
            400,
            "INVALID_ARGUMENT",
            "Service must specify exactly one of 'agentSpec', 'mcpServerSpec', or 'endpointSpec'.",
        )

    spec_kind, _ = active_specs[0]
    service_name = f"projects/{project}/locations/{location}/services/{service_id}"
    proj_num = _project_number_for(project)
    interfaces_dicts = [iface.model_dump() for iface in payload.interfaces]

    service_doc: dict[str, Any] = {
        "name": service_name,
        "displayName": payload.displayName or service_id,
        "description": payload.description,
        "interfaces": interfaces_dicts,
        "createTime": create_time,
        "updateTime": now,
    }

    if spec_kind == "agentSpec":
        assert payload.agentSpec is not None
        spec = payload.agentSpec
        if spec.type == "A2A_AGENT_CARD":
            if interfaces_dicts:
                raise GoogleApiError(
                    400,
                    "INVALID_ARGUMENT",
                    "Top-level 'interfaces' MUST be empty when agentSpec.type is "
                    "'A2A_AGENT_CARD'; define endpoints inside agentSpec.content "
                    "'supportedInterfaces' instead.",
                )
            card = _parse_spec_content(spec.content, spec_label="agentSpec", allow_empty=False)
            supported = card.get("supportedInterfaces")
            if not isinstance(supported, list) or not supported:
                raise GoogleApiError(
                    400,
                    "INVALID_ARGUMENT",
                    "A2A_AGENT_CARD content must include a non-empty 'supportedInterfaces' list.",
                )
            caps = card.get("capabilities")
            if isinstance(caps, dict) and "stateTransitionHistory" in caps:
                raise GoogleApiError(
                    400,
                    "INVALID_ARGUMENT",
                    "Invalid A2A v1.0 Agent Card content: unknown capabilities field 'stateTransitionHistory'.",
                )
            projected_interfaces = []
            seen_urls: set[str] = set()
            for idx, item in enumerate(supported):
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("url"), str)
                    or not item["url"].strip()
                ):
                    raise GoogleApiError(
                        400,
                        "INVALID_ARGUMENT",
                        f"A2A_AGENT_CARD supportedInterfaces[{idx}].url must be a non-empty string.",
                    )
                iface_url = item["url"].strip()
                if iface_url in seen_urls:
                    raise GoogleApiError(
                        400,
                        "INVALID_ARGUMENT",
                        f"Duplicate interface url found in agent card: {iface_url}",
                    )
                seen_urls.add(iface_url)
                projected_interfaces.append(
                    {
                        "url": iface_url,
                        "protocolBinding": item.get("protocolBinding", "JSONRPC"),
                    }
                )
        else:
            # NO_SPEC
            _parse_spec_content(spec.content, spec_label="agentSpec", allow_empty=True)
            if not interfaces_dicts:
                raise GoogleApiError(
                    400,
                    "INVALID_ARGUMENT",
                    "Top-level 'interfaces' is required when agentSpec.type is 'NO_SPEC'.",
                )
            card = {}
            projected_interfaces = interfaces_dicts

        registry_resource = f"projects/{project}/locations/{location}/agents/{service_id}"
        service_doc["agentSpec"] = spec.model_dump(exclude_none=True)
        service_doc["registryResource"] = registry_resource
        projected = {
            "name": registry_resource,
            "agentId": (
                f"urn:agent:projects-{proj_num}:projects:{proj_num}:"
                f"locations:{location}:agentregistry:services:{service_id}"
            ),
            "displayName": service_doc["displayName"],
            "description": service_doc["description"] or card.get("description", ""),
            "version": str(card.get("version", "1.0.0")),
            "interfaces": projected_interfaces,
            "skills": card.get("skills", []),
            "capabilities": card.get("capabilities", {}),
            "defaultInputModes": card.get("defaultInputModes", ["text/plain"]),
            "defaultOutputModes": card.get("defaultOutputModes", ["text/plain"]),
            "card": card,
            "sourceService": service_name,
            "createTime": create_time,
            "updateTime": now,
        }
        return service_doc, "agents", projected

    if spec_kind == "mcpServerSpec":
        assert payload.mcpServerSpec is not None
        spec = payload.mcpServerSpec
        if not interfaces_dicts:
            raise GoogleApiError(
                400,
                "INVALID_ARGUMENT",
                "Service with 'mcpServerSpec' requires at least one entry in 'interfaces'.",
            )
        tools: list[dict[str, Any]] = []
        if spec.type == "TOOL_SPEC":
            parsed_spec = _parse_spec_content(
                spec.content, spec_label="mcpServerSpec", allow_empty=False
            )
            if not isinstance(parsed_spec.get("tools"), list):
                raise GoogleApiError(
                    400,
                    "INVALID_ARGUMENT",
                    "mcpServerSpec.content must be a JSON object containing a 'tools' list.",
                )
            tools = parsed_spec["tools"]
        else:
            _parse_spec_content(spec.content, spec_label="mcpServerSpec", allow_empty=True)

        registry_resource = f"projects/{project}/locations/{location}/mcpServers/{service_id}"
        service_doc["mcpServerSpec"] = spec.model_dump(exclude_none=True)
        service_doc["registryResource"] = registry_resource
        projected = {
            "name": registry_resource,
            "mcpServerId": (
                f"urn:mcp:projects-{proj_num}:projects:{proj_num}:"
                f"locations:{location}:agentregistry:services:{service_id}"
            ),
            "displayName": service_doc["displayName"],
            "description": service_doc["description"],
            "interfaces": interfaces_dicts,
            "tools": tools,
            "sourceService": service_name,
            "createTime": create_time,
            "updateTime": now,
        }
        return service_doc, "mcpServers", projected

    # endpointSpec
    assert payload.endpointSpec is not None
    _parse_spec_content(payload.endpointSpec.content, spec_label="endpointSpec", allow_empty=True)
    if not interfaces_dicts:
        raise GoogleApiError(
            400,
            "INVALID_ARGUMENT",
            "Service with 'endpointSpec' requires at least one entry in 'interfaces'.",
        )
    registry_resource = f"projects/{project}/locations/{location}/endpoints/{service_id}"
    service_doc["endpointSpec"] = payload.endpointSpec.model_dump(exclude_none=True)
    service_doc["registryResource"] = registry_resource
    projected = {
        "name": registry_resource,
        "endpointId": (
            f"urn:endpoint:projects-{proj_num}:projects:{proj_num}:"
            f"locations:{location}:agentregistry:services:{service_id}"
        ),
        "displayName": service_doc["displayName"],
        "description": service_doc["description"],
        "interfaces": interfaces_dicts,
        "sourceService": service_name,
        "createTime": create_time,
        "updateTime": now,
    }
    return service_doc, "endpoints", projected


def _ingest_skill_revision(
    store: MockRegistryStore,
    *,
    project: str,
    location: str,
    skill: dict[str, Any],
    revision_id: str,
    rev_payload: SkillRevisionPayload,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate ZIP, run scanner according to skill scanningConfig, and store SkillRevision."""
    skill_name = skill["name"]
    rev_name = f"{skill_name}/revisions/{revision_id}"
    if rev_name in store.revisions:
        raise GoogleApiError(
            409,
            "ALREADY_EXISTS",
            f"SkillRevision already exists: {rev_name}",
        )

    zip_bytes = _decode_revision_source(rev_payload, store.gcs_blobs)
    try:
        validated = validate_skill_zip(zip_bytes)
    except SkillArchiveValidationError as exc:
        raise GoogleApiError(400, "INVALID_ARGUMENT", str(exc)) from exc

    now = _utc_now()
    policy = skill.get("scanningConfig", {}).get("policy", "ENABLED_BLOCKING")

    findings: list[dict[str, Any]] = []
    if policy == "DISABLED":
        scan_result = {
            "scanState": "DISABLED",
            "scanTime": now,
            "findings": [],
        }
    else:
        findings = scan_skill_archive(
            validated.files_text,
            resource_name=rev_name,
            project=project,
            location=location,
        )
        for finding in findings:
            store.findings[finding["name"]] = finding
        scan_result = {
            "scanState": "SUCCEEDED",
            "scanTime": now,
            "findings": [f["name"] for f in findings],
        }

    revision_doc: dict[str, Any] = {
        "name": rev_name,
        "uid": str(uuid.uuid4()),
        "state": "ACTIVE",
        "sha256Hash": validated.sha256_hash,
        "sizeBytes": validated.size_bytes,
        "frontmatter": validated.frontmatter,
        "scanResult": scan_result,
        "createTime": now,
    }
    store.revisions[rev_name] = revision_doc
    store.revision_blobs[rev_name] = zip_bytes
    store.revision_texts[rev_name] = validated.files_text
    return revision_doc, findings


def create_app(store: MockRegistryStore | None = None) -> FastAPI:
    """Create a FastAPI Mock Agent Registry v1 instance backed by `store`."""
    registry_store = store if store is not None else MockRegistryStore()

    application = FastAPI(
        title="Mock Google Cloud Agent Registry (v1)",
        version="1.0.0",
        description="Local faithful simulator of https://agentregistry.googleapis.com/v1",
    )
    application.state.store = registry_store

    @application.exception_handler(GoogleApiError)
    async def _handle_google_error(_request: Request, exc: GoogleApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.code,
            content=make_google_error(exc.code, exc.status, exc.message),
        )

    @application.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "mock-agent-registry"}

    @application.post("/_mock/reset")
    async def mock_reset() -> dict[str, str]:
        registry_store.reset()
        return {"status": "reset"}

    # ------------------------------------------------------------------
    # Services (Writable Control Plane -> projects onto Agents/McpServers/Endpoints)
    # ------------------------------------------------------------------

    @application.post("/v1/projects/{project}/locations/{location}/services")
    async def create_service(
        project: str,
        location: str,
        request: Request,
        serviceId: str | None = Query(default=None),
    ) -> dict[str, Any]:
        sid = _validate_resource_id(serviceId, "serviceId")
        service_name = f"projects/{project}/locations/{location}/services/{sid}"
        if service_name in registry_store.services:
            raise GoogleApiError(409, "ALREADY_EXISTS", f"Service already exists: {service_name}")

        raw_body = await _read_json_body(request)
        payload = _parse_model(ServicePayload, raw_body)
        now = _utc_now()
        service_doc, bucket, projected = _validate_and_project_service(
            project, location, sid, payload, now=now, create_time=now
        )

        registry_store.services[service_name] = service_doc
        _get_projection_store(registry_store, bucket)[projected["name"]] = projected
        return registry_store.create_operation(project, location, service_name, "create", service_doc)

    @application.get("/v1/projects/{project}/locations/{location}/services")
    async def list_services(project: str, location: str) -> dict[str, Any]:
        prefix = f"projects/{project}/locations/{location}/services/"
        items = [
            copy.deepcopy(svc)
            for name, svc in sorted(registry_store.services.items())
            if name.startswith(prefix)
        ]
        return {"services": items}

    @application.get("/v1/projects/{project}/locations/{location}/services/{service_id}")
    async def get_service(project: str, location: str, service_id: str) -> dict[str, Any]:
        service_name = f"projects/{project}/locations/{location}/services/{service_id}"
        svc = registry_store.services.get(service_name)
        if svc is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Service not found: {service_name}")
        return copy.deepcopy(svc)

    @application.patch("/v1/projects/{project}/locations/{location}/services/{service_id}")
    async def patch_service(
        project: str,
        location: str,
        service_id: str,
        request: Request,
        updateMask: str | None = Query(default=None),
    ) -> dict[str, Any]:
        service_name = f"projects/{project}/locations/{location}/services/{service_id}"
        existing = registry_store.services.get(service_name)
        if existing is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Service not found: {service_name}")

        raw_body = await _read_json_body(request)
        if not isinstance(raw_body, dict):
            raise GoogleApiError(400, "INVALID_ARGUMENT", "Request body must be a JSON object.")
        if "labels" in raw_body:
            raise GoogleApiError(
                400,
                "INVALID_ARGUMENT",
                "Generic 'labels' map is not supported on Agent Registry v1 Service.",
            )

        mask_fields = (
            {m.strip() for m in updateMask.split(",") if m.strip()}
            if updateMask
            else set(raw_body.keys())
        )
        merged = {
            "displayName": existing.get("displayName", ""),
            "description": existing.get("description", ""),
            "interfaces": copy.deepcopy(existing.get("interfaces", [])),
        }
        for spec_key in ("agentSpec", "mcpServerSpec", "endpointSpec"):
            if spec_key in existing:
                merged[spec_key] = copy.deepcopy(existing[spec_key])

        existing_spec_key = next(
            k for k in ("agentSpec", "mcpServerSpec", "endpointSpec") if k in existing
        )
        existing_spec_type = existing[existing_spec_key]["type"]

        for key, value in raw_body.items():
            if key == "name":
                continue
            if key in mask_fields or any(m.startswith(f"{key}.") for m in mask_fields):
                if key in ("agentSpec", "mcpServerSpec", "endpointSpec") and key != existing_spec_key:
                    raise GoogleApiError(
                        400,
                        "INVALID_ARGUMENT",
                        f"Service spec type is immutable: cannot replace '{existing_spec_key}' "
                        f"with '{key}'.",
                    )
                if key == existing_spec_key and isinstance(value, dict):
                    new_type = value.get("type", existing_spec_type)
                    if new_type != existing_spec_type:
                        raise GoogleApiError(
                            400,
                            "INVALID_ARGUMENT",
                            f"{existing_spec_key}.type is immutable: cannot change "
                            f"{existing_spec_type!r} to {new_type!r}.",
                        )
                    merged_spec = dict(merged[existing_spec_key])
                    merged_spec.update(value)
                    merged[existing_spec_key] = merged_spec
                else:
                    merged[key] = value

        payload = _parse_model(ServicePayload, merged)
        service_doc, bucket, projected = _validate_and_project_service(
            project,
            location,
            service_id,
            payload,
            now=_utc_now(),
            create_time=existing["createTime"],
        )
        registry_store.services[service_name] = service_doc
        _get_projection_store(registry_store, bucket)[projected["name"]] = projected
        return registry_store.create_operation(project, location, service_name, "update", service_doc)

    @application.delete("/v1/projects/{project}/locations/{location}/services/{service_id}")
    async def delete_service(project: str, location: str, service_id: str) -> dict[str, Any]:
        service_name = f"projects/{project}/locations/{location}/services/{service_id}"
        existing = registry_store.services.pop(service_name, None)
        if existing is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Service not found: {service_name}")

        reg_res = existing.get("registryResource", "")
        registry_store.agents.pop(reg_res, None)
        registry_store.mcp_servers.pop(reg_res, None)
        registry_store.endpoints.pop(reg_res, None)
        return registry_store.create_operation(project, location, service_name, "delete", {})

    # ------------------------------------------------------------------
    # Read-Only Discovery Resources: Agents, McpServers, Endpoints
    # ------------------------------------------------------------------

    @application.get("/v1/projects/{project}/locations/{location}/agents")
    async def list_agents(
        project: str,
        location: str,
        filter: str = Query(default=""),
    ) -> dict[str, Any]:
        prefix = f"projects/{project}/locations/{location}/agents/"
        items = [
            copy.deepcopy(a)
            for name, a in sorted(registry_store.agents.items())
            if name.startswith(prefix) and _evaluate_simple_filter(a, filter)
        ]
        return {"agents": items}

    @application.get("/v1/projects/{project}/locations/{location}/agents/{agent_id}")
    async def get_agent(project: str, location: str, agent_id: str) -> dict[str, Any]:
        agent_name = f"projects/{project}/locations/{location}/agents/{agent_id}"
        agent = registry_store.agents.get(agent_name)
        if agent is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Agent not found: {agent_name}")
        return copy.deepcopy(agent)

    @application.post("/v1/projects/{project}/locations/{location}/agents:search")
    async def search_agents(project: str, location: str, request: Request) -> dict[str, Any]:
        raw_body = await _read_json_body(request, allow_empty=True)
        payload = _parse_model(SearchAgentsPayload, raw_body)
        prefix = f"projects/{project}/locations/{location}/agents/"
        needle = payload.searchString.strip().lower()
        results: list[dict[str, Any]] = []
        for name, agent in sorted(registry_store.agents.items()):
            if not name.startswith(prefix):
                continue
            if payload.filter and not _evaluate_simple_filter(agent, payload.filter):
                continue
            if needle:
                haystack = json.dumps(
                    {
                        "name": agent.get("name"),
                        "agentId": agent.get("agentId"),
                        "displayName": agent.get("displayName"),
                        "description": agent.get("description"),
                        "skills": agent.get("skills"),
                        "card": agent.get("card"),
                    }
                ).lower()
                if needle not in haystack:
                    continue
            results.append(copy.deepcopy(agent))
        return {"agents": results[: max(1, min(payload.pageSize, 100))]}

    @application.get("/v1/projects/{project}/locations/{location}/mcpServers")
    async def list_mcp_servers(
        project: str,
        location: str,
        filter: str = Query(default=""),
    ) -> dict[str, Any]:
        prefix = f"projects/{project}/locations/{location}/mcpServers/"
        items = [
            copy.deepcopy(s)
            for name, s in sorted(registry_store.mcp_servers.items())
            if name.startswith(prefix) and _evaluate_simple_filter(s, filter)
        ]
        return {"mcpServers": items}

    @application.get("/v1/projects/{project}/locations/{location}/mcpServers/{server_id}")
    async def get_mcp_server(project: str, location: str, server_id: str) -> dict[str, Any]:
        server_name = f"projects/{project}/locations/{location}/mcpServers/{server_id}"
        server = registry_store.mcp_servers.get(server_name)
        if server is None:
            raise GoogleApiError(404, "NOT_FOUND", f"McpServer not found: {server_name}")
        return copy.deepcopy(server)

    @application.post("/v1/projects/{project}/locations/{location}/mcpServers:search")
    async def search_mcp_servers(project: str, location: str, request: Request) -> dict[str, Any]:
        raw_body = await _read_json_body(request, allow_empty=True)
        payload = _parse_model(SearchMcpServersPayload, raw_body)
        prefix = f"projects/{project}/locations/{location}/mcpServers/"
        needle = payload.searchString.strip().lower()
        results: list[dict[str, Any]] = []
        for name, server in sorted(registry_store.mcp_servers.items()):
            if not name.startswith(prefix):
                continue
            if payload.filter and not _evaluate_simple_filter(server, payload.filter):
                continue
            if needle:
                haystack = json.dumps(
                    {
                        "name": server.get("name"),
                        "mcpServerId": server.get("mcpServerId"),
                        "displayName": server.get("displayName"),
                        "description": server.get("description"),
                        "tools": server.get("tools"),
                    }
                ).lower()
                if needle not in haystack:
                    continue
            results.append(copy.deepcopy(server))
        return {"mcpServers": results[: max(1, min(payload.pageSize, 100))]}

    @application.get("/v1/projects/{project}/locations/{location}/endpoints")
    async def list_endpoints(
        project: str,
        location: str,
        filter: str = Query(default=""),
    ) -> dict[str, Any]:
        prefix = f"projects/{project}/locations/{location}/endpoints/"
        items = [
            copy.deepcopy(ep)
            for name, ep in sorted(registry_store.endpoints.items())
            if name.startswith(prefix) and _evaluate_simple_filter(ep, filter)
        ]
        return {"endpoints": items}

    @application.get("/v1/projects/{project}/locations/{location}/endpoints/{endpoint_id}")
    async def get_endpoint(project: str, location: str, endpoint_id: str) -> dict[str, Any]:
        ep_name = f"projects/{project}/locations/{location}/endpoints/{endpoint_id}"
        ep = registry_store.endpoints.get(ep_name)
        if ep is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Endpoint not found: {ep_name}")
        return copy.deepcopy(ep)

    # ------------------------------------------------------------------
    # Bindings & bindings:fetchAvailable
    # ------------------------------------------------------------------

    @application.post("/v1/projects/{project}/locations/{location}/bindings")
    async def create_binding(
        project: str,
        location: str,
        request: Request,
        bindingId: str | None = Query(default=None),
    ) -> dict[str, Any]:
        bid = _validate_resource_id(bindingId, "bindingId")
        binding_name = f"projects/{project}/locations/{location}/bindings/{bid}"
        if binding_name in registry_store.bindings:
            raise GoogleApiError(409, "ALREADY_EXISTS", f"Binding already exists: {binding_name}")

        raw_body = await _read_json_body(request)
        payload = _parse_model(BindingPayload, raw_body)
        now = _utc_now()
        binding_doc: dict[str, Any] = {
            "name": binding_name,
            "displayName": payload.displayName or bid,
            "description": payload.description,
            "target": payload.target.model_dump(),
            "authProviderBinding": payload.authProviderBinding.model_dump(),
            "createTime": now,
            "updateTime": now,
        }
        if payload.source is not None:
            binding_doc["source"] = payload.source.model_dump()

        registry_store.bindings[binding_name] = binding_doc
        return registry_store.create_operation(project, location, binding_name, "create", binding_doc)

    @application.get("/v1/projects/{project}/locations/{location}/bindings")
    async def list_bindings(project: str, location: str) -> dict[str, Any]:
        prefix = f"projects/{project}/locations/{location}/bindings/"
        items = [
            copy.deepcopy(b)
            for name, b in sorted(registry_store.bindings.items())
            if name.startswith(prefix)
        ]
        return {"bindings": items}

    @application.get("/v1/projects/{project}/locations/{location}/bindings:fetchAvailable")
    async def fetch_available_bindings(
        project: str,
        location: str,
        request: Request,
        sourceIdentifier: str | None = Query(default=None),
        targetIdentifier: str | None = Query(default=None),
    ) -> dict[str, Any]:
        prefix = f"projects/{project}/locations/{location}/bindings/"
        allowed_hdr = request.headers.get("x-mock-allowed-auth-providers")
        allowed_providers: set[str] | None = None
        if allowed_hdr is not None:
            allowed_providers = {p.strip() for p in allowed_hdr.split(",") if p.strip()}

        matched: list[dict[str, Any]] = []
        for name, binding in sorted(registry_store.bindings.items()):
            if not name.startswith(prefix):
                continue
            if sourceIdentifier:
                src_id = (binding.get("source") or {}).get("identifier")
                if src_id and src_id != sourceIdentifier:
                    continue
            if targetIdentifier:
                tgt_id = (binding.get("target") or {}).get("identifier")
                if tgt_id != targetIdentifier:
                    continue
            if allowed_providers is not None:
                ap = (binding.get("authProviderBinding") or {}).get("authProvider", "")
                ap_basename = ap.rsplit("/", 1)[-1] if ap else ""
                if ap not in allowed_providers and ap_basename not in allowed_providers:
                    continue
            matched.append(copy.deepcopy(binding))
        return {"bindings": matched}

    @application.get("/v1/projects/{project}/locations/{location}/bindings/{binding_id}")
    async def get_binding(project: str, location: str, binding_id: str) -> dict[str, Any]:
        binding_name = f"projects/{project}/locations/{location}/bindings/{binding_id}"
        binding = registry_store.bindings.get(binding_name)
        if binding is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Binding not found: {binding_name}")
        return copy.deepcopy(binding)

    @application.patch("/v1/projects/{project}/locations/{location}/bindings/{binding_id}")
    async def patch_binding(
        project: str,
        location: str,
        binding_id: str,
        request: Request,
        updateMask: str | None = Query(default=None),
    ) -> dict[str, Any]:
        binding_name = f"projects/{project}/locations/{location}/bindings/{binding_id}"
        existing = registry_store.bindings.get(binding_name)
        if existing is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Binding not found: {binding_name}")

        raw_body = await _read_json_body(request)
        if not isinstance(raw_body, dict):
            raise GoogleApiError(400, "INVALID_ARGUMENT", "Request body must be a JSON object.")
        if "labels" in raw_body:
            raise GoogleApiError(
                400,
                "INVALID_ARGUMENT",
                "Generic 'labels' map is not supported on Agent Registry v1 Binding.",
            )

        mask_fields = (
            {m.strip() for m in updateMask.split(",") if m.strip()}
            if updateMask
            else set(raw_body.keys())
        )
        merged = {
            "displayName": existing.get("displayName", ""),
            "description": existing.get("description", ""),
            "target": copy.deepcopy(existing["target"]),
            "authProviderBinding": copy.deepcopy(existing["authProviderBinding"]),
        }
        if "source" in existing:
            merged["source"] = copy.deepcopy(existing["source"])

        for key, val in raw_body.items():
            if key == "name":
                continue
            if key in mask_fields or any(m.startswith(f"{key}.") for m in mask_fields):
                merged[key] = val

        payload = _parse_model(BindingPayload, merged)
        updated: dict[str, Any] = {
            "name": binding_name,
            "displayName": payload.displayName or binding_id,
            "description": payload.description,
            "target": payload.target.model_dump(),
            "authProviderBinding": payload.authProviderBinding.model_dump(),
            "createTime": existing["createTime"],
            "updateTime": _utc_now(),
        }
        if payload.source is not None:
            updated["source"] = payload.source.model_dump()
        registry_store.bindings[binding_name] = updated
        return registry_store.create_operation(project, location, binding_name, "update", updated)

    @application.delete("/v1/projects/{project}/locations/{location}/bindings/{binding_id}")
    async def delete_binding(project: str, location: str, binding_id: str) -> dict[str, Any]:
        binding_name = f"projects/{project}/locations/{location}/bindings/{binding_id}"
        if registry_store.bindings.pop(binding_name, None) is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Binding not found: {binding_name}")
        return registry_store.create_operation(project, location, binding_name, "delete", {})

    # ------------------------------------------------------------------
    # Standalone Skills & SkillRevisions
    # ------------------------------------------------------------------

    @application.post("/v1/projects/{project}/locations/{location}/skills")
    async def create_skill(
        project: str,
        location: str,
        request: Request,
        skillId: str | None = Query(default=None),
    ) -> dict[str, Any]:
        raw_sid = _validate_resource_id(skillId, "skillId")
        # Agent Registry auto-prepends 'private-' to user-supplied skillId under PRIVATE publisher.
        prefixed_sid = (
            raw_sid if raw_sid.startswith(PRIVATE_SKILL_PREFIX) else f"{PRIVATE_SKILL_PREFIX}{raw_sid}"
        )
        skill_name = f"projects/{project}/locations/{location}/skills/{prefixed_sid}"
        if skill_name in registry_store.skills:
            raise GoogleApiError(409, "ALREADY_EXISTS", f"Skill already exists: {skill_name}")

        raw_body = await _read_json_body(request)
        payload = _parse_model(SkillPayload, raw_body)

        if payload.type != "SIMPLE":
            raise GoogleApiError(
                400,
                "INVALID_ARGUMENT",
                f"Skill.type must be 'SIMPLE' in Agent Registry v1 (got {payload.type!r}).",
            )

        try:
            target_state = normalize_target_state(payload.targetState)
        except ValueError as exc:
            raise GoogleApiError(400, "INVALID_ARGUMENT", str(exc)) from exc

        if target_state == "TARGET_STATE_DECOMMISSIONED":
            raise GoogleApiError(
                400,
                "FAILED_PRECONDITION",
                "Cannot create a Skill directly in TARGET_STATE_DECOMMISSIONED.",
            )

        scanning_policy = (
            payload.scanningConfig.policy
            if payload.scanningConfig is not None
            else "ENABLED_BLOCKING"
        )
        now = _utc_now()
        proj_num = _project_number_for(project)
        skill_doc: dict[str, Any] = {
            "name": skill_name,
            "uid": str(uuid.uuid4()),
            "skillId": f"urn:skill:projects-{proj_num}:locations:{location}:{prefixed_sid}",
            "displayName": payload.displayName or raw_sid,
            "description": payload.description,
            "type": "SIMPLE",
            "publisher": payload.publisher
            or f"projects/{project}/locations/{location}/publishers/private",
            "targetState": target_state,
            "state": "STATE_DRAFT",
            "scanningConfig": {"policy": scanning_policy},
            "defaultRevision": "",
            "defaultRevisionFindingCount": 0,
            "createTime": now,
            "updateTime": now,
        }

        if payload.initialRevision is not None:
            rev_doc, findings = _ingest_skill_revision(
                registry_store,
                project=project,
                location=location,
                skill=skill_doc,
                revision_id="rev-1",
                rev_payload=payload.initialRevision,
            )
            finding_count = len(findings)
            skill_doc["frontmatter"] = copy.deepcopy(rev_doc["frontmatter"])
            skill_doc["defaultRevisionFindingCount"] = finding_count

            if scanning_policy == "ENABLED_BLOCKING" and finding_count > 0:
                # Auto-quarantine in STATE_DRAFT when blocking scan finds security issues.
                skill_doc["state"] = "STATE_DRAFT"
                skill_doc["defaultRevision"] = ""
            else:
                skill_doc["defaultRevision"] = rev_doc["name"]
                skill_doc["state"] = TARGET_TO_STATE[target_state]
        else:
            # Without a revision, skills remain STATE_DRAFT even if targetState=ACTIVE.
            if target_state in ("TARGET_STATE_DRAFT", "TARGET_STATE_DISABLED"):
                skill_doc["state"] = TARGET_TO_STATE[target_state]
            else:
                skill_doc["state"] = "STATE_DRAFT"

        registry_store.skills[skill_name] = skill_doc
        return registry_store.create_operation(project, location, skill_name, "create", skill_doc)

    @application.get("/v1/projects/{project}/locations/{location}/skills")
    async def list_skills(
        project: str,
        location: str,
        filter: str = Query(default=""),
    ) -> dict[str, Any]:
        prefix = f"projects/{project}/locations/{location}/skills/"
        items = [
            copy.deepcopy(skill)
            for name, skill in sorted(registry_store.skills.items())
            if name.startswith(prefix) and _evaluate_simple_filter(skill, filter)
        ]
        return {"skills": items}

    @application.post("/v1/projects/{project}/locations/{location}/skills:search")
    async def search_skills_post(project: str, location: str, request: Request) -> dict[str, Any]:
        raw_body = await _read_json_body(request, allow_empty=True)
        payload = _parse_model(SearchSkillsPayload, raw_body)
        return _execute_skill_search(registry_store, project, location, payload)

    @application.get("/v1/projects/{project}/locations/{location}/skills:search")
    async def search_skills_get(
        project: str,
        location: str,
        searchString: str = Query(default=""),
        searchType: str = Query(default="KEYWORD"),
        filter: str = Query(default=""),
        pageSize: int = Query(default=20),
    ) -> dict[str, Any]:
        payload = _parse_model(
            SearchSkillsPayload,
            {
                "searchString": searchString,
                "searchType": searchType,
                "filter": filter,
                "pageSize": pageSize,
            },
        )
        return _execute_skill_search(registry_store, project, location, payload)

    @application.get("/v1/projects/{project}/locations/{location}/skills/{skill_id}")
    async def get_skill(project: str, location: str, skill_id: str) -> dict[str, Any]:
        skill_name = f"projects/{project}/locations/{location}/skills/{skill_id}"
        skill = registry_store.skills.get(skill_name)
        if skill is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Skill not found: {skill_name}")
        return copy.deepcopy(skill)

    @application.patch("/v1/projects/{project}/locations/{location}/skills/{skill_id}")
    async def patch_skill(
        project: str,
        location: str,
        skill_id: str,
        request: Request,
        updateMask: str | None = Query(default=None),
    ) -> dict[str, Any]:
        skill_name = f"projects/{project}/locations/{location}/skills/{skill_id}"
        existing = registry_store.skills.get(skill_name)
        if existing is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Skill not found: {skill_name}")
        if existing.get("state") == "STATE_DECOMMISSIONED":
            raise GoogleApiError(
                400,
                "FAILED_PRECONDITION",
                f"Skill '{skill_name}' is STATE_DECOMMISSIONED, which is terminal and "
                "irreversible; no further updates are allowed.",
            )

        raw_body = await _read_json_body(request)
        if not isinstance(raw_body, dict):
            raise GoogleApiError(400, "INVALID_ARGUMENT", "Request body must be a JSON object.")
        if "labels" in raw_body:
            raise GoogleApiError(
                400,
                "INVALID_ARGUMENT",
                "Generic 'labels' map is not supported on Agent Registry v1 Skill.",
            )

        mask_fields = (
            {m.strip() for m in updateMask.split(",") if m.strip()}
            if updateMask
            else set(raw_body.keys())
        )
        updated = copy.deepcopy(existing)

        if "displayName" in raw_body and "displayName" in mask_fields:
            updated["displayName"] = str(raw_body["displayName"])
        if "description" in raw_body and "description" in mask_fields:
            updated["description"] = str(raw_body["description"])
        if "scanningConfig" in raw_body and (
            "scanningConfig" in mask_fields or "scanningConfig.policy" in mask_fields
        ):
            sc = raw_body["scanningConfig"]
            if not isinstance(sc, dict) or sc.get("policy") not in (
                "ENABLED_BLOCKING",
                "ENABLED_NONBLOCKING",
                "DISABLED",
            ):
                raise GoogleApiError(400, "INVALID_ARGUMENT", f"Invalid scanningConfig: {sc!r}")
            updated["scanningConfig"] = {"policy": sc["policy"]}

        policy = updated["scanningConfig"]["policy"]

        if "defaultRevision" in raw_body and "defaultRevision" in mask_fields:
            req_rev = str(raw_body["defaultRevision"])
            if req_rev and "/" not in req_rev:
                req_rev = f"{skill_name}/revisions/{req_rev}"
            if req_rev:
                rev_doc = registry_store.revisions.get(req_rev)
                if rev_doc is None or not req_rev.startswith(f"{skill_name}/revisions/"):
                    raise GoogleApiError(
                        404,
                        "NOT_FOUND",
                        f"SkillRevision not found for skill: {req_rev}",
                    )
                scan_res = rev_doc.get("scanResult", {})
                findings_list = scan_res.get("findings", [])
                if policy == "ENABLED_BLOCKING" and (
                    len(findings_list) > 0
                    or scan_res.get("scanState") not in ("SUCCEEDED", "DISABLED")
                ):
                    raise GoogleApiError(
                        400,
                        "FAILED_PRECONDITION",
                        f"Cannot promote revision '{req_rev}' to defaultRevision under "
                        f"ENABLED_BLOCKING policy: {len(findings_list)} security finding(s) found.",
                    )
                updated["defaultRevision"] = req_rev
                updated["frontmatter"] = copy.deepcopy(rev_doc["frontmatter"])
                updated["defaultRevisionFindingCount"] = len(findings_list)
            else:
                updated["defaultRevision"] = ""

        if "targetState" in raw_body and "targetState" in mask_fields:
            try:
                req_target = normalize_target_state(str(raw_body["targetState"]))
            except ValueError as exc:
                raise GoogleApiError(400, "INVALID_ARGUMENT", str(exc)) from exc

            try:
                validate_skill_state_transition(existing["state"], req_target)
            except ValueError as exc:
                raise GoogleApiError(400, "FAILED_PRECONDITION", str(exc)) from exc

            if req_target == "TARGET_STATE_ACTIVE":
                def_rev = updated.get("defaultRevision", "")
                if not def_rev:
                    # If no defaultRevision set yet, attempt to verify latest revision if any.
                    revs = registry_store.skill_revisions_for(skill_name)
                    if not revs:
                        raise GoogleApiError(
                            400,
                            "FAILED_PRECONDITION",
                            "Cannot activate Skill without a clean defaultRevision.",
                        )
                    latest = revs[-1]
                    scan_res = latest.get("scanResult", {})
                    if policy == "ENABLED_BLOCKING" and (
                        len(scan_res.get("findings", [])) > 0
                        or scan_res.get("scanState") not in ("SUCCEEDED", "DISABLED")
                    ):
                        raise GoogleApiError(
                            400,
                            "FAILED_PRECONDITION",
                            "Cannot activate Skill under ENABLED_BLOCKING policy while "
                            f"latest revision has scanState={scan_res.get('scanState')!r} or "
                            f"{len(scan_res.get('findings', []))} security finding(s).",
                        )
                    updated["defaultRevision"] = latest["name"]
                    updated["frontmatter"] = copy.deepcopy(latest["frontmatter"])
                    updated["defaultRevisionFindingCount"] = len(scan_res.get("findings", []))
                else:
                    rev_doc = registry_store.revisions.get(def_rev, {})
                    scan_res = rev_doc.get("scanResult", {})
                    if policy == "ENABLED_BLOCKING" and (
                        len(scan_res.get("findings", [])) > 0
                        or scan_res.get("scanState") not in ("SUCCEEDED", "DISABLED")
                    ):
                        raise GoogleApiError(
                            400,
                            "FAILED_PRECONDITION",
                            "Cannot activate Skill under ENABLED_BLOCKING policy while "
                            f"defaultRevision has scanState={scan_res.get('scanState')!r} or "
                            f"{len(scan_res.get('findings', []))} security finding(s).",
                        )

            updated["targetState"] = req_target
            updated["state"] = TARGET_TO_STATE[req_target]

        updated["updateTime"] = _utc_now()
        registry_store.skills[skill_name] = updated
        return registry_store.create_operation(project, location, skill_name, "update", updated)

    @application.delete("/v1/projects/{project}/locations/{location}/skills/{skill_id}")
    async def delete_skill(
        project: str,
        location: str,
        skill_id: str,
        force: bool = Query(default=False),
    ) -> dict[str, Any]:
        skill_name = f"projects/{project}/locations/{location}/skills/{skill_id}"
        if skill_name not in registry_store.skills:
            raise GoogleApiError(404, "NOT_FOUND", f"Skill not found: {skill_name}")

        revs = registry_store.skill_revisions_for(skill_name)
        if revs and not force:
            raise GoogleApiError(
                400,
                "FAILED_PRECONDITION",
                f"Skill '{skill_name}' still has {len(revs)} revision(s); pass '?force=true' "
                "to cascade delete the skill and all revisions.",
            )

        for rev in revs:
            rev_name = rev["name"]
            registry_store.revisions.pop(rev_name, None)
            registry_store.revision_blobs.pop(rev_name, None)
            registry_store.revision_texts.pop(rev_name, None)
            for fname in list(registry_store.findings.keys()):
                if registry_store.findings[fname].get("resource") == rev_name:
                    registry_store.findings.pop(fname, None)

        registry_store.skills.pop(skill_name, None)
        return registry_store.create_operation(project, location, skill_name, "delete", {})

    # ------------------------------------------------------------------
    # SkillRevisions + ?alt=media 302 Signed Redirect
    # ------------------------------------------------------------------

    @application.post("/v1/projects/{project}/locations/{location}/skills/{skill_id}/revisions")
    async def create_skill_revision(
        project: str,
        location: str,
        skill_id: str,
        request: Request,
        skillRevisionId: str | None = Query(default=None),
    ) -> dict[str, Any]:
        skill_name = f"projects/{project}/locations/{location}/skills/{skill_id}"
        skill = registry_store.skills.get(skill_name)
        if skill is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Skill not found: {skill_name}")
        if skill.get("state") == "STATE_DECOMMISSIONED":
            raise GoogleApiError(
                400,
                "FAILED_PRECONDITION",
                f"Skill '{skill_name}' is STATE_DECOMMISSIONED, which is terminal and "
                "irreversible; cannot create new revisions.",
            )

        rev_id = (
            _validate_resource_id(skillRevisionId, "skillRevisionId")
            if skillRevisionId
            else f"rev-{uuid.uuid4().hex[:8]}"
        )
        raw_body = await _read_json_body(request)
        payload = _parse_model(SkillRevisionPayload, raw_body)
        rev_doc, _findings = _ingest_skill_revision(
            registry_store,
            project=project,
            location=location,
            skill=skill,
            revision_id=rev_id,
            rev_payload=payload,
        )
        return registry_store.create_operation(project, location, rev_doc["name"], "create", rev_doc)

    @application.get("/v1/projects/{project}/locations/{location}/skills/{skill_id}/revisions")
    async def list_skill_revisions(project: str, location: str, skill_id: str) -> dict[str, Any]:
        skill_name = f"projects/{project}/locations/{location}/skills/{skill_id}"
        if skill_name not in registry_store.skills:
            raise GoogleApiError(404, "NOT_FOUND", f"Skill not found: {skill_name}")
        return {"skillRevisions": registry_store.skill_revisions_for(skill_name)}

    @application.get(
        "/v1/projects/{project}/locations/{location}/skills/{skill_id}/revisions/{revision_id}",
        response_model=None,
    )
    async def get_skill_revision(
        project: str,
        location: str,
        skill_id: str,
        revision_id: str,
        request: Request,
        alt: str | None = Query(default=None),
    ) -> Any:
        skill_name = f"projects/{project}/locations/{location}/skills/{skill_id}"
        skill = registry_store.skills.get(skill_name)
        if skill is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Skill not found: {skill_name}")

        rev_name = f"{skill_name}/revisions/{revision_id}"
        rev = registry_store.revisions.get(rev_name)
        if rev is None:
            raise GoogleApiError(404, "NOT_FOUND", f"SkillRevision not found: {rev_name}")

        if alt != "media":
            return copy.deepcopy(rev)

        # Enforce ENABLED_BLOCKING finding gate first so blocked scans report clearly.
        policy = skill.get("scanningConfig", {}).get("policy", "ENABLED_BLOCKING")
        scan_result = rev.get("scanResult", {})
        findings_list = scan_result.get("findings", [])
        if policy == "ENABLED_BLOCKING" and (
            len(findings_list) > 0 or scan_result.get("scanState") not in ("SUCCEEDED", "DISABLED")
        ):
            raise GoogleApiError(
                400,
                "FAILED_PRECONDITION",
                f"Download blocked by scanningConfig policy ENABLED_BLOCKING: "
                f"revision '{rev_name}' has {len(findings_list)} unresolved security finding(s).",
            )

        # Enforce downloadable lifecycle states: STATE_ACTIVE or STATE_DEPRECATED only.
        state = skill.get("state")
        if state not in ("STATE_ACTIVE", "STATE_DEPRECATED"):
            raise GoogleApiError(
                400,
                "FAILED_PRECONDITION",
                f"Cannot download media for Skill in state '{state}'; "
                "skill must be STATE_ACTIVE or STATE_DEPRECATED.",
            )

        token = secrets.token_urlsafe(24)
        registry_store.signed_downloads[token] = {
            "revision_name": rev_name,
            "sha256": rev["sha256Hash"],
        }
        base = str(request.base_url).rstrip("/")
        signed_url = f"{base}/scotty-signed/{token}"
        return RedirectResponse(url=signed_url, status_code=302)

    @application.delete(
        "/v1/projects/{project}/locations/{location}/skills/{skill_id}/revisions/{revision_id}"
    )
    async def delete_skill_revision(
        project: str,
        location: str,
        skill_id: str,
        revision_id: str,
    ) -> dict[str, Any]:
        skill_name = f"projects/{project}/locations/{location}/skills/{skill_id}"
        skill = registry_store.skills.get(skill_name)
        if skill is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Skill not found: {skill_name}")
        rev_name = f"{skill_name}/revisions/{revision_id}"
        if rev_name not in registry_store.revisions:
            raise GoogleApiError(404, "NOT_FOUND", f"SkillRevision not found: {rev_name}")
        if skill.get("defaultRevision") == rev_name:
            raise GoogleApiError(
                400,
                "FAILED_PRECONDITION",
                f"Cannot delete active defaultRevision '{rev_name}'; update defaultRevision first.",
            )
        registry_store.revisions.pop(rev_name, None)
        registry_store.revision_blobs.pop(rev_name, None)
        registry_store.revision_texts.pop(rev_name, None)
        return registry_store.create_operation(project, location, rev_name, "delete", {})

    @application.get("/scotty-signed/{token}")
    async def scotty_signed_download(token: str, request: Request) -> Response:
        """Simulated short-lived GCS signed URL endpoint.

        STRICTLY rejects (HTTP 403) if ANY x-goog-* or Authorization header is sent!
        """
        forbidden_headers = [
            name
            for name in request.headers.keys()
            if name.lower().startswith("x-goog-") or name.lower() == "authorization"
        ]
        if forbidden_headers:
            return JSONResponse(
                status_code=403,
                content=make_google_error(
                    403,
                    "PERMISSION_DENIED",
                    "Signed GCS URL rejected request containing forbidden Google/Auth headers: "
                    f"{', '.join(sorted(forbidden_headers))}. Strip all x-goog-* and "
                    "Authorization headers when following the ?alt=media 302 redirect.",
                ),
            )

        entry = registry_store.signed_downloads.get(token)
        if entry is None:
            raise GoogleApiError(404, "NOT_FOUND", "Signed download token not found or expired.")
        blob = registry_store.revision_blobs.get(entry["revision_name"])
        if blob is None:
            raise GoogleApiError(404, "NOT_FOUND", "Skill archive blob not found.")

        return Response(
            content=blob,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="skill.zip"',
                "ETag": f'"{entry["sha256"]}"',
            },
        )

    # ------------------------------------------------------------------
    # Findings, Publishers, Operations
    # ------------------------------------------------------------------

    @application.get("/v1/projects/{project}/locations/{location}/findings")
    async def list_findings(
        project: str,
        location: str,
        filter: str = Query(default=""),
    ) -> dict[str, Any]:
        prefix = f"projects/{project}/locations/{location}/findings/"
        items = [
            copy.deepcopy(f)
            for name, f in sorted(registry_store.findings.items())
            if name.startswith(prefix) and _evaluate_simple_filter(f, filter)
        ]
        return {"findings": items}

    @application.get("/v1/projects/{project}/locations/{location}/findings/{finding_id}")
    async def get_finding(project: str, location: str, finding_id: str) -> dict[str, Any]:
        fname = f"projects/{project}/locations/{location}/findings/{finding_id}"
        finding = registry_store.findings.get(fname)
        if finding is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Finding not found: {fname}")
        return copy.deepcopy(finding)

    @application.get("/v1/projects/{project}/locations/{location}/publishers")
    async def list_publishers(project: str, location: str) -> dict[str, Any]:
        return {"publishers": registry_store.list_publishers(project, location)}

    @application.get("/v1/projects/{project}/locations/{location}/operations/{op_id}")
    async def get_operation(project: str, location: str, op_id: str) -> dict[str, Any]:
        op_name = f"projects/{project}/locations/{location}/operations/{op_id}"
        op = registry_store.operations.get(op_name)
        if op is None:
            raise GoogleApiError(404, "NOT_FOUND", f"Operation not found: {op_name}")
        return copy.deepcopy(op)

    return application


def _get_projection_store(store: MockRegistryStore, bucket: str) -> dict[str, dict[str, Any]]:
    if bucket == "agents":
        return store.agents
    if bucket == "mcpServers":
        return store.mcp_servers
    return store.endpoints


def _execute_skill_search(
    store: MockRegistryStore,
    project: str,
    location: str,
    payload: SearchSkillsPayload,
) -> dict[str, Any]:
    prefix = f"projects/{project}/locations/{location}/skills/"
    query_str = (payload.searchString or payload.query).strip()
    scored: list[tuple[float, str, dict[str, Any]]] = []

    for name, skill in sorted(store.skills.items()):
        if not name.startswith(prefix):
            continue
        if payload.filter and not _evaluate_simple_filter(skill, payload.filter):
            continue

        def_rev = skill.get("defaultRevision", "")
        rev_texts = store.revision_texts.get(def_rev, {})
        combined_text = " ".join(
            [
                str(skill.get("name", "")),
                str(skill.get("skillId", "")),
                str(skill.get("displayName", "")),
                str(skill.get("description", "")),
                json.dumps(skill.get("frontmatter", {})),
                " ".join(rev_texts.values()),
            ]
        )

        if not query_str:
            scored.append((1.0, name, copy.deepcopy(skill)))
            continue

        if payload.searchType == "SEMANTIC":
            q_tokens = _tokenize(query_str)
            doc_tokens = _tokenize(combined_text)
            if not q_tokens:
                scored.append((1.0, name, copy.deepcopy(skill)))
                continue
            overlap = len(q_tokens & doc_tokens)
            # Also match prefix stems (e.g., "optimiz" matches "optimizer"/"optimize").
            stem_hits = sum(
                1
                for qt in q_tokens
                if qt not in doc_tokens
                and len(qt) >= 4
                and any(dt.startswith(qt[:4]) or qt.startswith(dt[:4]) for dt in doc_tokens if len(dt) >= 4)
            )
            score = (overlap + 0.5 * stem_hits) / len(q_tokens)
            if score > 0:
                scored.append((score, name, copy.deepcopy(skill)))
        else:
            # KEYWORD search: support field:value, field=value, wildcard *, or plain substring
            lowered = combined_text.lower()
            field_match = re.match(r'^([A-Za-z0-9_.]+)\s*([:=])\s*"?([^"]+)"?$', query_str)
            if field_match:
                f_key, f_op, f_val = field_match.groups()
                field_val = str(_lookup_dotted_field(skill, f_key) or "").lower()
                pat = f_val.lower().rstrip("*")
                hit = (
                    (field_val == pat)
                    if (f_op == "=" and not f_val.endswith("*"))
                    else (pat in field_val)
                )
                if hit:
                    scored.append((1.0, name, copy.deepcopy(skill)))
            else:
                terms = [t.rstrip("*").lower() for t in query_str.split() if t.strip()]
                if all(t in lowered for t in terms):
                    scored.append((1.0, name, copy.deepcopy(skill)))

    scored.sort(key=lambda item: (-item[0], item[1]))
    limit = max(1, min(payload.pageSize, 100))
    return {"skills": [item[2] for item in scored[:limit]]}


app = create_app()
