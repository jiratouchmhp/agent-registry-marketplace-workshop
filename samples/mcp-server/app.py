"""Runnable FastAPI Streamable HTTP MCP Server for the GCP FinOps Toolkit workshop.

Exposes JSON-RPC 2.0 over Streamable HTTP at POST /mcp with 3 FinOps tools:
  - get_billing_summary (readOnlyHint=True, destructiveHint=False, idempotentHint=True)
  - analyze_bq_query    (readOnlyHint=True, destructiveHint=False, idempotentHint=True)
  - delete_idle_dataset (readOnlyHint=False, destructiveHint=True, idempotentHint=False)
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

SERVER_NAME = "finops-mcp-server"
SERVER_VERSION = "1.0.0"
MCP_PROTOCOL_VERSION = "2025-03-26"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_billing_summary",
        "description": (
            "Return read-only Google Cloud Billing spend summary, baseline comparison, "
            "and top SKU deltas for a GCP project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Target GCP project ID (e.g. acme-agent-governance).",
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (1-90).",
                    "default": 7,
                    "minimum": 1,
                    "maximum": 90,
                },
            },
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Get Cloud Billing Summary",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "analyze_bq_query",
        "description": (
            "Inspect a BigQuery Standard SQL query for full-table scans, SELECT *, "
            "missing partition filters, and estimated on-demand bytes billed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "BigQuery Standard SQL query text to analyze.",
                },
                "project_id": {
                    "type": "string",
                    "description": "Optional GCP project ID owning the target datasets.",
                },
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Analyze BigQuery SQL Query",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "delete_idle_dataset",
        "description": (
            "Permanently delete an idle BigQuery dataset to reclaim storage spend. "
            "Destructive action intended to be gated by Agent Gateway + IAP v2 UAP."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "GCP project ID containing the dataset.",
                },
                "dataset_id": {
                    "type": "string",
                    "description": "BigQuery dataset ID to delete.",
                },
                "confirm_delete": {
                    "type": "boolean",
                    "description": "Must be true to execute deletion; false runs a dry-run check.",
                    "default": False,
                },
                "reason": {
                    "type": "string",
                    "description": "Audit justification / ticket reference for dataset removal.",
                },
            },
            "required": ["project_id", "dataset_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Delete Idle BigQuery Dataset",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
]

TOOL_INDEX: dict[str, dict[str, Any]] = {tool["name"]: tool for tool in TOOLS}

# In-memory simulated deletion log (used by workshop tests and IAP demo verification).
DELETED_DATASETS: list[dict[str, Any]] = []

app = FastAPI(
    title="GCP FinOps MCP Server",
    version=SERVER_VERSION,
    description="Streamable HTTP MCP server exposing governed FinOps tools.",
)


def _handle_get_billing_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = arguments.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("'project_id' must be a non-empty string")

    days = arguments.get("days", 7)
    if isinstance(days, bool) or not isinstance(days, int) or not (1 <= days <= 90):
        raise ValueError("'days' must be an integer between 1 and 90")

    scale = days / 7.0
    baseline_usd = round(4280.0 * scale, 2)
    observed_usd = round(6195.4 * scale, 2)
    delta_usd = round(observed_usd - baseline_usd, 2)
    delta_pct = round((delta_usd / baseline_usd) * 100.0, 2)

    return {
        "project_id": project_id.strip(),
        "window_days": days,
        "currency": "USD",
        "baseline_spend_usd": baseline_usd,
        "observed_spend_usd": observed_usd,
        "delta_usd": delta_usd,
        "delta_pct": delta_pct,
        "anomaly_detected": delta_pct >= 20.0,
        "top_skus": [
            {
                "service": "BigQuery",
                "sku": "Analysis (On-Demand TiB Scanned)",
                "spend_usd": round(3420.5 * scale, 2),
                "delta_pct": 68.4,
            },
            {
                "service": "BigQuery",
                "sku": "Active Logical Storage",
                "spend_usd": round(1125.9 * scale, 2),
                "delta_pct": 14.2,
            },
            {
                "service": "Compute Engine",
                "sku": "N2 Custom Instance Core running in Americas",
                "spend_usd": round(1040.0 * scale, 2),
                "delta_pct": 6.5,
            },
            {
                "service": "Cloud Storage",
                "sku": "Standard Storage US Multi-region",
                "spend_usd": round(609.0 * scale, 2),
                "delta_pct": 3.1,
            },
        ],
        "recommended_next_steps": [
            "Run analyze_bq_query on top scheduled BigQuery jobs for missing partition predicates.",
            "Review idle scratch datasets before requesting governed cleanup.",
        ],
    }


def _handle_analyze_bq_query(arguments: dict[str, Any]) -> dict[str, Any]:
    sql = arguments.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("'sql' must be a non-empty SQL string")

    project_id = arguments.get("project_id") or "acme-agent-governance"
    normalized = " ".join(sql.strip().split())
    upper = normalized.upper()

    findings: list[dict[str, Any]] = []
    estimated_gib = 12.5

    if re.search(r"\bSELECT\s+\*", upper):
        estimated_gib += 420.0
        findings.append(
            {
                "code": "BQ_SELECT_STAR",
                "severity": "HIGH",
                "message": "Query uses SELECT *, scanning every column in columnar BigQuery storage.",
                "recommendation": "Replace SELECT * with an explicit projection of required columns.",
            }
        )

    has_where = bool(re.search(r"\bWHERE\b", upper))
    has_partition_predicate = bool(
        re.search(r"(_PARTITIONDATE|_PARTITIONTIME|EVENT_DATE|CREATED_DATE|PARTITION_DATE)", upper)
    )
    if not has_where or not has_partition_predicate:
        estimated_gib += 680.0
        findings.append(
            {
                "code": "BQ_MISSING_PARTITION_FILTER",
                "severity": "HIGH",
                "message": "No partition column predicate detected; query may trigger a full table scan.",
                "recommendation": "Add a sargable partition filter (e.g. WHERE _PARTITIONDATE >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)).",
            }
        )

    if re.search(r"\bCROSS\s+JOIN\b", upper):
        estimated_gib += 350.0
        findings.append(
            {
                "code": "BQ_CROSS_JOIN",
                "severity": "MEDIUM",
                "message": "CROSS JOIN detected; Cartesian expansion can cause severe slot and shuffle spikes.",
                "recommendation": "Replace with an explicit keyed JOIN or pre-aggregated lookup.",
            }
        )

    if re.search(r"\bORDER\s+BY\b", upper) and not re.search(r"\bLIMIT\b", upper):
        findings.append(
            {
                "code": "BQ_UNBOUNDED_ORDER_BY",
                "severity": "LOW",
                "message": "ORDER BY without LIMIT forces a global sort across all output rows.",
                "recommendation": "Add a LIMIT clause or remove unnecessary sorting on intermediate results.",
            }
        )

    estimated_tib = estimated_gib / 1024.0
    # Standard BigQuery on-demand rate: $6.25 per TiB scanned.
    estimated_cost_usd = round(estimated_tib * 6.25, 4)
    savings_pct = 82 if len(findings) >= 2 else (45 if len(findings) == 1 else 0)

    return {
        "project_id": project_id,
        "estimated_bytes_scanned": int(estimated_gib * (1024**3)),
        "estimated_gib_scanned": round(estimated_gib, 2),
        "estimated_on_demand_cost_usd": estimated_cost_usd,
        "estimated_savings_pct": savings_pct,
        "anti_patterns_found": len(findings),
        "findings": findings,
        "safe_to_run": len(findings) == 0,
    }


def _handle_delete_idle_dataset(arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = arguments.get("project_id")
    dataset_id = arguments.get("dataset_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("'project_id' must be a non-empty string")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise ValueError("'dataset_id' must be a non-empty string")

    confirm_delete = arguments.get("confirm_delete", False)
    if not isinstance(confirm_delete, bool):
        raise ValueError("'confirm_delete' must be a boolean")
    reason = str(arguments.get("reason") or "Idle dataset cleanup (>90 days without queries)")

    if not confirm_delete:
        return {
            "status": "dry_run_confirmation_required",
            "project_id": project_id.strip(),
            "dataset_id": dataset_id.strip(),
            "destructive": True,
            "estimated_reclaimed_gb": 485.2,
            "estimated_monthly_savings_usd": 9.70,
            "message": (
                "Deletion NOT executed because confirm_delete=false. Re-run with "
                "confirm_delete=true after explicit human approval."
            ),
        }

    record = {
        "status": "deleted",
        "project_id": project_id.strip(),
        "dataset_id": dataset_id.strip(),
        "destructive": True,
        "reclaimed_gb": 485.2,
        "estimated_monthly_savings_usd": 9.70,
        "reason": reason,
        "deleted_at": datetime.now(timezone.utc).isoformat(),
    }
    DELETED_DATASETS.append(record)
    return record


TOOL_HANDLERS = {
    "get_billing_summary": _handle_get_billing_summary,
    "analyze_bq_query": _handle_analyze_bq_query,
    "delete_idle_dataset": _handle_delete_idle_dataset,
}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    error_obj: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error_obj["data"] = data
    return JSONResponse(
        content={"jsonrpc": "2.0", "id": req_id, "error": error_obj},
        status_code=200,
    )


def _jsonrpc_result(
    req_id: Any, result: dict[str, Any], headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        content={"jsonrpc": "2.0", "id": req_id, "result": result},
        status_code=200,
        headers=headers or {},
    )


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": SERVER_NAME,
        "version": SERVER_VERSION,
        "tool_count": len(TOOLS),
    }


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    """Handle MCP Streamable HTTP JSON-RPC 2.0 requests."""
    try:
        payload = await request.json()
    except Exception as exc:
        return _jsonrpc_error(None, -32700, f"Parse error: {exc}")

    if not isinstance(payload, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request: JSON-RPC payload must be an object")

    jsonrpc_ver = payload.get("jsonrpc")
    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") or {}

    if jsonrpc_ver != "2.0" or not isinstance(method, str):
        return _jsonrpc_error(req_id, -32600, "Invalid Request: expected jsonrpc='2.0' and method")

    # Notifications have no id and return HTTP 202 Accepted per Streamable HTTP spec.
    if method.startswith("notifications/"):
        return Response(status_code=202)

    if method == "initialize":
        session_id = request.headers.get("mcp-session-id") or f"finops-{uuid.uuid4().hex[:12]}"
        return _jsonrpc_result(
            req_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
            headers={"Mcp-Session-Id": session_id},
        )

    if method == "ping":
        return _jsonrpc_result(req_id, {})

    if method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        if not isinstance(params, dict):
            return _jsonrpc_error(req_id, -32602, "Invalid params: expected object")
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(tool_name, str) or tool_name not in TOOL_HANDLERS:
            return _jsonrpc_error(req_id, -32601, f"Unknown tool: {tool_name!r}")
        if not isinstance(arguments, dict):
            return _jsonrpc_error(req_id, -32602, "Tool 'arguments' must be a JSON object")

        try:
            output = TOOL_HANDLERS[tool_name](arguments)
        except ValueError as exc:
            return _jsonrpc_result(
                req_id,
                {
                    "content": [{"type": "text", "text": f"Invalid arguments: {exc}"}],
                    "isError": True,
                },
            )

        return _jsonrpc_result(
            req_id,
            {
                "content": [{"type": "text", "text": json.dumps(output, indent=2)}],
                "structuredContent": output,
                "isError": False,
            },
        )

    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8091"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
