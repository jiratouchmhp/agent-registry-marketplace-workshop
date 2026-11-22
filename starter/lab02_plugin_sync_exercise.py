"""Lab 02 Starter Exercise: Decomposing Agent Plugins 1.0 -> Agent Registry v1 Skills.

Goal:
    Google Cloud Agent Registry (v1) stores standalone ``Skill`` resources in
    ``agentskills.io`` format (strictly ONE uppercase ``SKILL.md`` per ZIP,
    <= 1 MB compressed, scalar string ``metadata`` map) — NOT multi-skill
    ``agent-plugins.org`` plugin archives.

    In this exercise you will implement:
    1. ``canonical_private_skill_id`` — handling Agent Registry's automatic
       ``private-`` ID prefix for PRIVATE-publisher skills.
    2. ``enrich_skill_frontmatter`` — injecting plugin provenance & KMS signing
       keys into ``SKILL.md`` frontmatter ``metadata`` (strictly ``map<string, string>``).
    3. ``build_single_skill_zip`` — packaging a single skill directory into an
       ``agentskills.io``-compliant ZIP archive (single uppercase ``SKILL.md``,
       no symlinks, no ``..`` traversal, <= 1 MB compressed).
    4. ``register_skill_revision`` — calling ``POST /v1/projects/{p}/locations/{l}/skills?skillId={id}``
       (or creating a new revision on ``skills/private-{id}/revisions`` if the skill
       already exists) with ``scanningConfig.policy = "ENABLED_BLOCKING"``.

Reference Solution:
    ``tools/plugin_sync/``
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx
import yaml

from schemas.validators import parse_frontmatter, validate_skill_md

PRIVATE_SKILL_PREFIX = "private-"
MAX_SKILL_ZIP_BYTES = 1_048_576  # 1 MiB Agent Registry hard limit
RECOMMENDED_INLINE_UPLOAD_BYTES = 512_000  # 500 KiB advised for inline archiveUploadSource

REQUIRED_METADATA_KEYS: tuple[str, ...] = (
    "plugin_name",
    "plugin_display_name",
    "plugin_version",
    "source_repo",
    "git_commit",
    "mcp_service_id",
    "agent_service_id",
)


@dataclass
class SkillArchiveArtifact:
    """In-memory packaged ``agentskills.io`` single-skill ZIP ready for upload."""

    skill_id: str
    canonical_skill_id: str
    zip_bytes: bytes
    sha256_hex: str


def canonical_private_skill_id(raw_skill_id: str) -> str:
    """Return the canonical ``private-{skillId}`` resource ID used after creation.

    Remember from ``docs/architecture.md`` and ``docs/labs/02-register-agent-registry.md``:
    - Creating ``POST .../skills?skillId=cost-anomaly-triage`` creates
      ``projects/{p}/locations/{l}/skills/private-cost-anomaly-triage``.
    - All subsequent ``GET``, ``PATCH``, ``DELETE``, and ``revisions`` calls
      MUST use ``private-cost-anomaly-triage`` (avoiding double-prefixing
      ``private-private-...``!).
    """
    # WORKSHOP-TODO(lab-02): Strip whitespace, validate non-empty, and ensure the
    # returned ID starts with PRIVATE_SKILL_PREFIX ("private-") exactly once.
    raise NotImplementedError(
        "WORKSHOP-TODO(lab-02): Implement canonical_private_skill_id()"
    )


def enrich_skill_frontmatter(
    skill_md_text: str,
    plugin_metadata: dict[str, str],
) -> str:
    """Inject scalar string provenance fields into ``SKILL.md`` YAML frontmatter.

    Requirements:
    - Parse existing YAML frontmatter with ``parse_frontmatter(skill_md_text)``.
    - Merge ``plugin_metadata`` into ``frontmatter["metadata"]``.
    - Ensure EVERY key and value in ``frontmatter["metadata"]`` is a plain ``str``
      (Agent Registry v1 rejects non-string or nested YAML metadata values).
    - Re-serialize ``---\\n<yaml>---\\n<body>`` cleanly.
    """
    # WORKSHOP-TODO(lab-02): Parse frontmatter, validate/coerce metadata values
    # as scalar strings (rejecting lists/dicts), merge plugin_metadata, and
    # return the updated SKILL.md text.
    raise NotImplementedError(
        "WORKSHOP-TODO(lab-02): Implement enrich_skill_frontmatter()"
    )


def build_single_skill_zip(
    skill_dir: Path,
    plugin_metadata: dict[str, str],
) -> SkillArchiveArtifact:
    """Package a single ``skills/<skill-name>/`` folder into an ``agentskills.io`` ZIP.

    Requirements:
    - Validate ``skill_dir / "SKILL.md"`` via ``validate_skill_md(skill_md, expected_name=skill_dir.name)``.
    - Ensure the resulting ZIP contains **strictly ONE uppercase ``SKILL.md``**
      at ``<skill-name>/SKILL.md``.
    - Reject symlinks and ``..`` path components.
    - Enforce ``len(zip_bytes) <= MAX_SKILL_ZIP_BYTES`` (1 MB).
    - Compute ``hashlib.sha256(zip_bytes).hexdigest()`` and return a
      ``SkillArchiveArtifact`` with ``canonical_skill_id = canonical_private_skill_id(skill_dir.name)``.
    """
    # WORKSHOP-TODO(lab-02): Validate SKILL.md, enrich frontmatter metadata,
    # build deterministic ZIP under `<skill_dir.name>/`, check 1 MB cap, and
    # return SkillArchiveArtifact.
    raise NotImplementedError(
        "WORKSHOP-TODO(lab-02): Implement build_single_skill_zip()"
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
    """Register or update a standalone ``Skill`` in Google Cloud Agent Registry ``v1``.

    Hints:
    - Base collection URL:
      ``f"{registry_base_url.rstrip('/')}/projects/{project_id}/locations/{location}/skills"``
    - First check if the skill already exists using the **canonical** ID:
      ``GET .../skills/{artifact.canonical_skill_id}`` (i.e. ``private-<id>``!).
    - If 404 (new skill):
      ``POST .../skills?skillId={artifact.skill_id}`` with JSON body:
      ``{
          "displayName": display_name,
          "description": description,
          "type": "SIMPLE",
          "targetState": "TARGET_STATE_ACTIVE",
          "scanningConfig": {"policy": scanning_policy},
          "initialRevision": {
              "archiveUploadSource": {
                  "archiveContent": base64.b64encode(artifact.zip_bytes).decode("ascii")
              }
          },
      }``
    - If 200 (existing skill):
      1. Create a new immutable revision:
         ``POST .../skills/{artifact.canonical_skill_id}/revisions?skillRevisionId={revision_id}``
         with ``{"archiveUploadSource": {"archiveContent": ...}}``.
      2. Once scanning passes with 0 findings, promote it to active default via:
         ``PATCH .../skills/{artifact.canonical_skill_id}?updateMask=defaultRevision``
         with ``{"defaultRevision": "projects/{project_id}/locations/{location}/skills/{artifact.canonical_skill_id}/revisions/{revision_id}"}``.
    """
    # WORKSHOP-TODO(lab-02): Implement idempotent create-or-new-revision call
    # using `artifact.skill_id` on POST create and `artifact.canonical_skill_id`
    # (`private-...`) on GET/revision creation.
    raise NotImplementedError(
        "WORKSHOP-TODO(lab-02): Implement register_skill_revision()"
    )


def _run_self_check() -> None:
    """Quick local self-check harness for attendees completing Lab 02."""
    sample_skill_dir = (
        Path(__file__).resolve().parent / "lab01_plugin_skeleton" / "skills" / "hello-finops"
    )
    print("Lab 02 Self-Check")
    passed = 0
    total = 4

    try:
        cid = canonical_private_skill_id("cost-anomaly-triage")
        assert cid == "private-cost-anomaly-triage", f"Expected private-cost-anomaly-triage, got {cid}"
        assert canonical_private_skill_id("private-cost-anomaly-triage") == "private-cost-anomaly-triage"
        print("  [PASS] canonical_private_skill_id")
        passed += 1
    except NotImplementedError as exc:
        print(f"  [TODO] {exc}")

    meta = {
        "plugin_name": "hello-finops-plugin",
        "plugin_display_name": "Hello FinOps Plugin",
        "plugin_version": "0.1.0",
        "source_repo": "https://github.com/acme/agent-plugins",
        "git_commit": "abc1234",
        "mcp_service_id": "finops-mcp-server",
        "agent_service_id": "finops-advisor-agent",
    }

    try:
        raw_skill_md = (sample_skill_dir / "SKILL.md").read_text(encoding="utf-8")
        enriched_md = enrich_skill_frontmatter(raw_skill_md, meta)
        enriched_fm, _, enrich_err = parse_frontmatter(enriched_md)
        assert enrich_err is None and enriched_fm is not None
        assert enriched_fm["metadata"]["plugin_name"] == "hello-finops-plugin"
        assert all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in enriched_fm["metadata"].items()
        ), "All metadata keys and values must be scalar strings"
        print("  [PASS] enrich_skill_frontmatter")
        passed += 1
    except NotImplementedError as exc:
        print(f"  [TODO] {exc}")

    try:
        artifact = build_single_skill_zip(sample_skill_dir, meta)
        assert artifact.canonical_skill_id == "private-hello-finops"
        assert len(artifact.zip_bytes) <= MAX_SKILL_ZIP_BYTES
        with zipfile.ZipFile(io.BytesIO(artifact.zip_bytes)) as zf:
            skill_mds = [n for n in zf.namelist() if n.endswith("SKILL.md")]
            assert len(skill_mds) == 1, f"Expected strictly 1 SKILL.md, got {skill_mds}"
            parsed_fm, _, err = parse_frontmatter(zf.read(skill_mds[0]).decode("utf-8"))
            assert err is None and parsed_fm is not None
            assert parsed_fm["metadata"]["plugin_name"] == "hello-finops-plugin"
        print(f"  [PASS] build_single_skill_zip (sha256={artifact.sha256_hex[:12]}...)")
        passed += 1
    except NotImplementedError as exc:
        print(f"  [TODO] {exc}")

    try:
        sample_zip_bytes = b"PK\x05\x06" + b"\x00" * 18
        synthetic_artifact = SkillArchiveArtifact(
            skill_id="hello-finops",
            canonical_skill_id="private-hello-finops",
            zip_bytes=sample_zip_bytes,
            sha256_hex=hashlib.sha256(sample_zip_bytes).hexdigest(),
        )
        created = {"exists": False}

        def _mock_registry(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and path.endswith("/skills/private-hello-finops"):
                if not created["exists"]:
                    return httpx.Response(404, json={"error": {"code": 404}})
                return httpx.Response(200, json={"name": path.lstrip("/")})
            if request.method == "POST" and path.endswith("/skills"):
                assert request.url.params.get("skillId") == "hello-finops"
                created["exists"] = True
                return httpx.Response(
                    200,
                    json={"name": f"{path.lstrip('/')}/private-hello-finops", "done": True},
                )
            if request.method == "POST" and path.endswith("/skills/private-hello-finops/revisions"):
                return httpx.Response(200, json={"name": f"{path.lstrip('/')}/rev-2"})
            if request.method == "PATCH" and path.endswith("/skills/private-hello-finops"):
                return httpx.Response(
                    200, json={"name": path.lstrip("/"), "defaultRevision": "rev-2"}
                )
            return httpx.Response(400, text=f"Unexpected {request.method} {request.url}")

        with httpx.Client(transport=httpx.MockTransport(_mock_registry)) as client:
            res_create = register_skill_revision(
                client,
                registry_base_url="http://127.0.0.1:8090/v1",
                project_id="acme-agent-governance",
                location="asia-southeast3",
                artifact=synthetic_artifact,
                display_name="Hello FinOps",
                description="Starter skill",
            )
            assert "private-hello-finops" in str(res_create)
            res_update = register_skill_revision(
                client,
                registry_base_url="http://127.0.0.1:8090/v1",
                project_id="acme-agent-governance",
                location="asia-southeast3",
                artifact=synthetic_artifact,
                display_name="Hello FinOps",
                description="Starter skill",
                revision_id="rev-2",
            )
            assert res_update.get("defaultRevision") == "rev-2"
        print("  [PASS] register_skill_revision (create + new revision promotion)")
        passed += 1
    except NotImplementedError as exc:
        print(f"  [TODO] {exc}")

    # Sanity-check validate_skill_md & json/yaml/base64/httpx imports are usable
    assert validate_skill_md(sample_skill_dir / "SKILL.md", expected_name="hello-finops").valid
    _ = (base64, hashlib, json, yaml, httpx)
    if passed == total:
        print(f"  [DONE] Progress: {passed}/{total} checks passed — all Lab 02 checks passed!")
    else:
        print(f"  [INFO] Progress: {passed}/{total} checks passed")


if __name__ == "__main__":
    _run_self_check()
