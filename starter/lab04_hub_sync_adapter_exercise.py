"""Lab 04 Starter Exercise: Safe Ownership-Tracked IDE Config Hydration.

Goal:
    Enterprise developer workstation hydration MUST NEVER clobber a developer's
    personal IDE settings, must preserve user toggles like ``disabled: true`` or
    ``disabledTools`` on managed MCP servers, must write files atomically, and
    must ONLY prune removed enterprise entries when an upstream catalog fetch
    completed cleanly (never on partial failure!).

    In this exercise you will implement:
    1. ``atomic_write_json`` — writing JSON via ``tempfile.NamedTemporaryFile``
       in the target directory + ``os.fsync`` + ``os.replace``.
    2. ``safe_merge_antigravity_mcp_config`` — ownership-tracked merge for
       ``~/.gemini/config/mcp_config.json`` that:
       - NEVER overwrites pre-existing user-authored entries (keys not in
         ``previous_owned_keys``).
       - Preserves user-set ``disabled`` and ``disabledTools`` fields on
         hydrated entries across re-syncs.
       - Prunes previously owned keys no longer in ``desired_servers`` ONLY when
         ``catalog_complete=True``.

Reference Solution:
    ``tools/hub_sync/hydration.py`` and ``tools/hub_sync/adapters.py``
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

PRESERVED_USER_FIELDS: tuple[str, ...] = ("disabled", "disabledTools")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write ``payload`` as pretty JSON to ``path``.

    Requirements:
    1. Ensure ``path.parent`` exists (``mkdir(parents=True, exist_ok=True)``).
    2. Create a temporary file in ``path.parent`` (same filesystem!) using
       ``tempfile.NamedTemporaryFile(dir=path.parent, delete=False, ...)``.
    3. Write UTF-8 JSON, ``flush()``, and ``os.fsync(fh.fileno())``.
    4. Atomically swap into place with ``os.replace(tmp_path, path)``.
    5. Clean up the temp file if an exception occurs before ``os.replace``.
    """
    # WORKSHOP-TODO(lab-04): Implement atomic JSON write with tempfile + os.replace.
    raise NotImplementedError(
        "WORKSHOP-TODO(lab-04): Implement atomic_write_json()"
    )


def safe_merge_antigravity_mcp_config(
    existing_doc: dict[str, Any],
    desired_servers: dict[str, dict[str, Any]],
    previous_owned_keys: set[str],
    *,
    catalog_complete: bool,
) -> tuple[dict[str, Any], set[str]]:
    """Merge enterprise MCP servers into ``~/.gemini/config/mcp_config.json`` safely.

    Args:
        existing_doc: Current JSON content of ``mcp_config.json`` (may contain
            user-authored servers and other top-level keys).
        desired_servers: Sanitized enterprise MCP server definitions from the
            synced plugins/catalog.
        previous_owned_keys: Server keys owned by hub-sync during the prior run
            (loaded from ``.sync-manifest.json``).
        catalog_complete: ``True`` ONLY when the full catalog fetch and all
            plugin downloads succeeded cleanly.

    Returns:
        ``(updated_doc, new_owned_keys)`` where:
        - Pre-existing keys NOT in ``previous_owned_keys`` are untouched and
          NOT added to ``new_owned_keys``.
        - Owned keys get updated config while preserving ``disabled`` and
          ``disabledTools`` if the user set them on the existing entry.
        - Keys in ``previous_owned_keys - set(desired_servers)`` are deleted
          ONLY when ``catalog_complete`` is ``True``.
    """
    # WORKSHOP-TODO(lab-04): Implement ownership-tracked merge preserving user
    # entries, preserving `disabled` / `disabledTools`, and pruning only when
    # `catalog_complete` is True.
    raise NotImplementedError(
        "WORKSHOP-TODO(lab-04): Implement safe_merge_antigravity_mcp_config()"
    )


def _run_self_check() -> None:
    """Offline self-check harness verifying all safety invariants for Lab 04."""
    print("Lab 04 Self-Check")
    passed = 0
    total = 3

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "nested" / "mcp_config.json"
        try:
            atomic_write_json(target, {"mcpServers": {"ok": {"url": "http://127.0.0.1:8091/mcp"}}})
            assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["ok"]["url"]
            print("  [PASS] atomic_write_json")
            passed += 1
        except NotImplementedError as exc:
            print(f"  [TODO] {exc}")

    existing = {
        "mcpServers": {
            "my-personal-mcp": {"url": "http://localhost:9999/mcp"},
            "finops-mcp-server": {
                "url": "http://127.0.0.1:8091/old",
                "authProviderType": "google_credentials",
                "disabled": True,
                "disabledTools": ["delete_budget"],
            },
            "retired-enterprise-mcp": {"url": "http://127.0.0.1:8099/mcp"},
        }
    }
    desired = {
        "finops-mcp-server": {
            "url": "http://127.0.0.1:8091/mcp",
            "authProviderType": "google_credentials",
        },
        "my-personal-mcp": {
            "url": "http://127.0.0.1:8091/should-not-overwrite-user",
        },
    }
    prev_owned = {"finops-mcp-server", "retired-enterprise-mcp"}

    try:
        # 1. Partial failure (catalog_complete=False) MUST NOT prune retired-enterprise-mcp
        partial_doc, partial_owned = safe_merge_antigravity_mcp_config(
            existing, desired, prev_owned, catalog_complete=False
        )
        assert "retired-enterprise-mcp" in partial_doc["mcpServers"]
        assert "retired-enterprise-mcp" in partial_owned
        print("  [PASS] partial failure preserves existing installations")
        passed += 1
    except NotImplementedError as exc:
        print(f"  [TODO] {exc} (partial-failure preservation check)")

    try:
        # 2. Clean sync (catalog_complete=True) updates URL, preserves user server + disabled flags, prunes retired
        clean_doc, clean_owned = safe_merge_antigravity_mcp_config(
            existing, desired, prev_owned, catalog_complete=True
        )
        servers = clean_doc["mcpServers"]
        assert servers["my-personal-mcp"]["url"] == "http://localhost:9999/mcp"
        assert "my-personal-mcp" not in clean_owned
        assert servers["finops-mcp-server"]["url"] == "http://127.0.0.1:8091/mcp"
        assert servers["finops-mcp-server"]["disabled"] is True
        assert servers["finops-mcp-server"]["disabledTools"] == ["delete_budget"]
        assert "retired-enterprise-mcp" not in servers
        assert clean_owned == {"finops-mcp-server"}
        print("  [PASS] clean sync preserves user configs & prunes retired entries safely")
        passed += 1
    except NotImplementedError as exc:
        print(f"  [TODO] {exc} (clean-sync merge & prune check)")

    _ = (copy, os)
    if passed == total:
        print(f"  [DONE] Progress: {passed}/{total} checks passed — all Lab 04 checks passed!")
    else:
        print(f"  [INFO] Progress: {passed}/{total} checks passed")


if __name__ == "__main__":
    _run_self_check()
