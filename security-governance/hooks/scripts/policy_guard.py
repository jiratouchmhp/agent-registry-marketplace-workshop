#!/usr/bin/env python3
"""Antigravity / Jetski Policy Guard Hook (PreToolUse + PostToolUse).

Client-side defense-in-depth guardrail complementing server-side Agent Gateway +
IAP UAP policies:
  - PreToolUse: Blocks destructive MCP tools (by annotation or name) and
    destructive shell commands unless explicitly approved via
    `POLICY_GUARD_APPROVE_DESTRUCTIVE=true`. Always exits 0 and emits a JSON
    decision on stdout (`{"decision": "deny", "reason": "..."}` or `{}`).
  - PostToolUse: Appends a structured JSONL audit entry (redacting raw argument
    values to avoid logging secrets) to `POLICY_GUARD_AUDIT_LOG` (defaulting to
    the OS temp directory), prints `{}`, and exits 0.

Uses strictly the Python standard library so it runs without a virtualenv.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

DESTRUCTIVE_TOOL_NAME_RE = re.compile(
    r"(?:^|[_\.\-/])"
    r"(delete|destroy|drop|purge|wipe|truncate|terminate|decommission|revoke|remove|rm|kill)"
    r"(?:$|[_\.\-/])",
    re.IGNORECASE,
)

SHELL_TOOL_NAMES = frozenset(
    {
        "run_command",
        "bash",
        "shell",
        "terminal",
        "exec",
        "execute_command",
        "run_shell_command",
    }
)

DANGEROUS_SHELL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("recursive force delete (rm -rf)", re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b")),
    ("recursive force delete (rm -fr)", re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\b")),
    ("terraform destroy", re.compile(r"\bterraform\s+destroy\b", re.IGNORECASE)),
    ("gcloud delete", re.compile(r"\bgcloud\b.*\bdelete\b", re.IGNORECASE)),
    ("SQL DROP statement", re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA|DATASET)\b", re.IGNORECASE)),
    ("BigQuery rm", re.compile(r"\bbq\s+rm\b", re.IGNORECASE)),
    ("Cloud Storage recursive rm", re.compile(r"\b(gsutil|gcloud\s+storage)\s+rm\b", re.IGNORECASE)),
    ("kubectl delete", re.compile(r"\bkubectl\s+delete\b", re.IGNORECASE)),
)


def _extract_tool_details(payload: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Extract (tool_name, tool_args, annotations) from Jetski/Antigravity or generic hook JSON."""
    tool_call = payload.get("tool_call") or payload.get("toolCall") or {}
    if not isinstance(tool_call, dict):
        tool_call = {}

    tool_name = str(
        tool_call.get("name")
        or payload.get("tool_name")
        or payload.get("toolName")
        or payload.get("tool")
        or ""
    ).strip()

    raw_args = (
        tool_call.get("args")
        if "args" in tool_call
        else (
            tool_call.get("arguments")
            if "arguments" in tool_call
            else (
                payload.get("tool_input")
                or payload.get("toolInput")
                or payload.get("args")
                or payload.get("arguments")
                or {}
            )
        )
    )
    tool_args = raw_args if isinstance(raw_args, dict) else {"_raw": str(raw_args)}

    raw_annotations = (
        tool_call.get("annotations")
        or payload.get("annotations")
        or (payload.get("tool_metadata") or {}).get("annotations")
        or {}
    )
    annotations = raw_annotations if isinstance(raw_annotations, dict) else {}
    return tool_name, tool_args, annotations


def _is_truthy_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() == "true"
    return False


def evaluate_pre_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a PreToolUse hook payload and return `{}` (allow) or `{"decision": "deny", "reason": ...}`."""
    tool_name, tool_args, annotations = _extract_tool_details(payload)

    if os.environ.get("POLICY_GUARD_APPROVE_DESTRUCTIVE", "").strip().lower() == "true":
        return {}

    # 1. Explicit MCP tool annotation (`destructiveHint` / `destructive_hint`).
    if _is_truthy_bool(annotations.get("destructiveHint")) or _is_truthy_bool(
        annotations.get("destructive_hint")
    ):
        return {
            "decision": "deny",
            "reason": (
                f"Policy Guard blocked tool '{tool_name}': annotated with destructiveHint=true. "
                "Set POLICY_GUARD_APPROVE_DESTRUCTIVE=true after explicit human approval."
            ),
        }

    # 2. Explicit read-only annotation short-circuit (only when not a shell runner).
    normalized_name = tool_name.lower()
    if (
        normalized_name not in SHELL_TOOL_NAMES
        and (
            _is_truthy_bool(annotations.get("readOnlyHint"))
            or _is_truthy_bool(annotations.get("read_only_hint"))
        )
    ):
        return {}

    # 3. Destructive tool name pattern check (covers MCP tools like
    #    `mcp_finops-mcp-server_delete_budget`, `delete_billing_export`, etc.).
    if tool_name and DESTRUCTIVE_TOOL_NAME_RE.search(tool_name):
        return {
            "decision": "deny",
            "reason": (
                f"Policy Guard blocked destructive tool '{tool_name}'. "
                "Destructive operations require explicit human approval "
                "(set POLICY_GUARD_APPROVE_DESTRUCTIVE=true) or an authorized IAP UAP exception."
            ),
        }

    # 4. Dangerous shell command check on command-execution tools.
    if normalized_name in SHELL_TOOL_NAMES:
        cmd_text = str(
            tool_args.get("CommandLine")
            or tool_args.get("command")
            or tool_args.get("cmd")
            or ""
        )
        for label, pattern in DANGEROUS_SHELL_PATTERNS:
            if pattern.search(cmd_text):
                return {
                    "decision": "deny",
                    "reason": (
                        f"Policy Guard blocked shell command matching '{label}' inside '{tool_name}'. "
                        "Set POLICY_GUARD_APPROVE_DESTRUCTIVE=true after human review."
                    ),
                }

    return {}


def record_post_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    """Append a structured JSONL audit event for PostToolUse and return `{}`."""
    tool_name, tool_args, _annotations = _extract_tool_details(payload)
    default_log = Path(tempfile.gettempdir()) / "agent-policy-guard-audit.jsonl"
    log_path = Path(os.environ.get("POLICY_GUARD_AUDIT_LOG", str(default_log)))

    result_val = payload.get("result")
    result_bytes = len(result_val.encode("utf-8")) if isinstance(result_val, str) else len(
        json.dumps(result_val, default=str).encode("utf-8")
    ) if result_val is not None else 0

    entry = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event": "PostToolUse",
        "conversation_id": str(payload.get("conversation_id") or payload.get("conversationId") or ""),
        "agent_name": str(payload.get("agent_name") or payload.get("agentName") or ""),
        "step_idx": payload.get("step_idx", payload.get("stepIdx")),
        "tool_name": tool_name,
        "arg_keys": sorted(str(k) for k in tool_args.keys()),
        "had_error": bool(payload.get("error")),
        "result_bytes": result_bytes,
    }

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as exc:
        # Never break the agent loop on audit I/O errors; emit diagnostic to stderr only.
        print(f"[policy-guard] audit log write warning: {exc}", file=sys.stderr)

    return {}


def _detect_event(cli_event: str | None, payload: dict[str, Any]) -> str:
    if cli_event:
        return cli_event
    for key in ("hook_event_name", "hookEventName", "event"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    if "result" in payload and "decision" not in payload:
        return "PostToolUse"
    return "PreToolUse"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Antigravity / Jetski policy-guard hook for PreToolUse and PostToolUse."
    )
    parser.add_argument(
        "--event",
        choices=["PreToolUse", "PostToolUse"],
        default=None,
        help="Hook lifecycle event (defaults to payload hint or PreToolUse).",
    )
    args = parser.parse_args(argv)

    raw_stdin = sys.stdin.read().strip()
    if not raw_stdin:
        payload: dict[str, Any] = {}
    else:
        try:
            parsed = json.loads(raw_stdin)
            payload = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError as exc:
            # Fail-closed on malformed JSON in PreToolUse so invalid payloads cannot bypass policy.
            if (args.event or "PreToolUse") == "PreToolUse":
                print(
                    json.dumps(
                        {
                            "decision": "deny",
                            "reason": f"Policy Guard received malformed hook JSON on stdin: {exc}",
                        }
                    )
                )
                return 0
            print("{}")
            return 0

    event = _detect_event(args.event, payload)
    if event == "PostToolUse":
        output = record_post_tool_use(payload)
    else:
        output = evaluate_pre_tool_use(payload)

    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
