"""Data models, constants, and state-machine rules for the Mock Agent Registry v1 API."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PRIVATE_SKILL_PREFIX = "private-"

SKILL_MAX_ZIP_BYTES = 1 * 1024 * 1024  # 1 MB compressed
SKILL_MAX_UNCOMPRESSED_BYTES = 10 * 1024 * 1024  # 10 MB total uncompressed
SKILL_MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MB individual file uncompressed
SKILL_MAX_NESTING_DEPTH = 8
SPEC_CONTENT_MAX_BYTES = 10 * 1024  # 10 KB for agentSpec / mcpServerSpec content

RESOURCE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

ProtocolBinding = Literal["JSONRPC", "HTTP_JSON", "GRPC"]
AgentSpecType = Literal["A2A_AGENT_CARD", "NO_SPEC"]
McpServerSpecType = Literal["TOOL_SPEC", "NO_SPEC"]
EndpointSpecType = Literal["NO_SPEC"]
SkillType = Literal["SIMPLE"]
ScanningPolicy = Literal["ENABLED_BLOCKING", "ENABLED_NONBLOCKING", "DISABLED"]
ScanState = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "DISABLED", "UNSUPPORTED_FORMAT"]
SearchType = Literal["KEYWORD", "SEMANTIC"]

SkillTargetState = Literal[
    "TARGET_STATE_DRAFT",
    "TARGET_STATE_ACTIVE",
    "TARGET_STATE_DISABLED",
    "TARGET_STATE_DEPRECATED",
    "TARGET_STATE_DECOMMISSIONED",
]

SkillState = Literal[
    "STATE_CREATING",
    "STATE_DRAFT",
    "STATE_ACTIVE",
    "STATE_DISABLED",
    "STATE_DEPRECATED",
    "STATE_DECOMMISSIONED",
    "STATE_DELETING",
]

TARGET_TO_STATE: dict[str, str] = {
    "TARGET_STATE_DRAFT": "STATE_DRAFT",
    "TARGET_STATE_ACTIVE": "STATE_ACTIVE",
    "TARGET_STATE_DISABLED": "STATE_DISABLED",
    "TARGET_STATE_DEPRECATED": "STATE_DEPRECATED",
    "TARGET_STATE_DECOMMISSIONED": "STATE_DECOMMISSIONED",
}

SHORT_TO_TARGET_STATE: dict[str, str] = {
    "DRAFT": "TARGET_STATE_DRAFT",
    "ACTIVE": "TARGET_STATE_ACTIVE",
    "DISABLED": "TARGET_STATE_DISABLED",
    "DEPRECATED": "TARGET_STATE_DEPRECATED",
    "DECOMMISSIONED": "TARGET_STATE_DECOMMISSIONED",
    "STATE_DRAFT": "TARGET_STATE_DRAFT",
    "STATE_ACTIVE": "TARGET_STATE_ACTIVE",
    "STATE_DISABLED": "TARGET_STATE_DISABLED",
    "STATE_DEPRECATED": "TARGET_STATE_DEPRECATED",
    "STATE_DECOMMISSIONED": "TARGET_STATE_DECOMMISSIONED",
    "TARGET_STATE_DRAFT": "TARGET_STATE_DRAFT",
    "TARGET_STATE_ACTIVE": "TARGET_STATE_ACTIVE",
    "TARGET_STATE_DISABLED": "TARGET_STATE_DISABLED",
    "TARGET_STATE_DEPRECATED": "TARGET_STATE_DEPRECATED",
    "TARGET_STATE_DECOMMISSIONED": "TARGET_STATE_DECOMMISSIONED",
}

SHORT_TO_STATE: dict[str, str] = {
    "CREATING": "STATE_CREATING",
    "DRAFT": "STATE_DRAFT",
    "ACTIVE": "STATE_ACTIVE",
    "DISABLED": "STATE_DISABLED",
    "DEPRECATED": "STATE_DEPRECATED",
    "DECOMMISSIONED": "STATE_DECOMMISSIONED",
    "DELETING": "STATE_DELETING",
    "STATE_CREATING": "STATE_CREATING",
    "STATE_DRAFT": "STATE_DRAFT",
    "STATE_ACTIVE": "STATE_ACTIVE",
    "STATE_DISABLED": "STATE_DISABLED",
    "STATE_DEPRECATED": "STATE_DEPRECATED",
    "STATE_DECOMMISSIONED": "STATE_DECOMMISSIONED",
    "STATE_DELETING": "STATE_DELETING",
}


def normalize_target_state(value: str | None) -> str:
    """Normalize a targetState value to canonical TARGET_STATE_* form."""
    if not value:
        return "TARGET_STATE_DRAFT"
    key = value.strip().upper()
    if key not in SHORT_TO_TARGET_STATE:
        raise ValueError(
            f"Invalid targetState {value!r}; expected one of {sorted(TARGET_TO_STATE)}"
        )
    return SHORT_TO_TARGET_STATE[key]


def normalize_state_filter_value(value: str) -> str:
    """Normalize filter values like 'ACTIVE' or 'STATE_ACTIVE' to canonical STATE_*."""
    key = value.strip().strip('"').strip("'").upper()
    return SHORT_TO_STATE.get(key, key)


def validate_skill_state_transition(current_state: str, requested_target_state: str) -> None:
    """Enforce the Agent Registry v1 Skill lifecycle state machine.

    Rules:
    - STATE_DECOMMISSIONED is terminal and irreversible.
    - TARGET_STATE_DECOMMISSIONED is ONLY allowed from STATE_DEPRECATED
      (cannot decommission directly from DRAFT, ACTIVE, or DISABLED).
    """
    if current_state == "STATE_DECOMMISSIONED":
        if requested_target_state != "TARGET_STATE_DECOMMISSIONED":
            raise ValueError(
                "Skill is STATE_DECOMMISSIONED, which is terminal and irreversible; "
                f"cannot transition to {requested_target_state}"
            )
        return

    if requested_target_state == "TARGET_STATE_DECOMMISSIONED":
        if current_state != "STATE_DEPRECATED":
            raise ValueError(
                "Cannot transition directly to TARGET_STATE_DECOMMISSIONED from "
                f"{current_state}; skill must be transitioned to STATE_DEPRECATED first"
            )


class InterfaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    protocolBinding: ProtocolBinding


class AgentSpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: AgentSpecType
    content: dict[str, Any] | str | None = None


class McpServerSpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: McpServerSpecType
    content: dict[str, Any] | str | None = None


class EndpointSpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EndpointSpecType = "NO_SPEC"
    content: dict[str, Any] | str | None = None


class ServicePayload(BaseModel):
    """Writable Service request payload (no generic labels map in v1)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    displayName: str = ""
    description: str = ""
    interfaces: list[InterfaceModel] = Field(default_factory=list)
    agentSpec: AgentSpecModel | None = None
    mcpServerSpec: McpServerSpecModel | None = None
    endpointSpec: EndpointSpecModel | None = None


class ResourceIdentifierModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str


class AuthProviderBindingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authProvider: str
    scopes: list[str] = Field(default_factory=list)
    continueUri: str = ""


class BindingPayload(BaseModel):
    """Writable Binding request payload (no generic labels map in v1)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    displayName: str = ""
    description: str = ""
    source: ResourceIdentifierModel | None = None
    target: ResourceIdentifierModel
    authProviderBinding: AuthProviderBindingModel


class ScanningConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: ScanningPolicy = "ENABLED_BLOCKING"


class ArchiveUploadSourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archiveContent: str


class GcsSourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    generation: int | None = None


class SkillRevisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    archiveUploadSource: ArchiveUploadSourceModel | None = None
    gcsSource: GcsSourceModel | None = None


class SkillPayload(BaseModel):
    """Writable Skill request payload (no generic labels map in v1)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    displayName: str = ""
    description: str = ""
    type: str = "SIMPLE"
    targetState: str | None = None
    defaultRevision: str | None = None
    publisher: str | None = None
    scanningConfig: ScanningConfigModel | None = None
    initialRevision: SkillRevisionPayload | None = None


class SearchAgentsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    searchString: str = ""
    filter: str = ""
    pageSize: int = 20
    pageToken: str = ""


class SearchMcpServersPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    searchString: str = ""
    filter: str = ""
    pageSize: int = 20
    pageToken: str = ""


class SearchSkillsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    searchString: str = ""
    query: str = ""
    searchType: SearchType = "KEYWORD"
    filter: str = ""
    pageSize: int = 20
    pageToken: str = ""


def make_google_error(code: int, status: str, message: str) -> dict[str, Any]:
    """Return a Google API-style error envelope."""
    return {
        "error": {
            "code": code,
            "message": message,
            "status": status,
        }
    }
