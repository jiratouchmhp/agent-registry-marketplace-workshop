"""Runnable FastAPI A2A v1.0 Agent for the GCP FinOps Toolkit workshop.

Exposes:
  - GET  /healthz
  - GET  /.well-known/agent-card.json  (A2A v1.0 Agent Card <= 10 KB)
  - POST /a2a                          (JSON-RPC 2.0 and HTTP+JSON task execution)
  - GET  /a2a/tasks/{task_id}          (Retrieve executed task state)
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

AGENT_NAME = "GCP FinOps Advisor"
AGENT_VERSION = "1.0.0"

app = FastAPI(
    title=AGENT_NAME,
    version=AGENT_VERSION,
    description="A2A v1.0 FinOps specialist providing structured GCP cost advice.",
)

# In-memory store for task state transitions.
TASKS: dict[str, dict[str, Any]] = {}


def build_agent_card(base_url: str | None = None) -> dict[str, Any]:
    """Build a valid A2A v1.0 Agent Card (well under Agent Registry's 10 KB limit)."""
    public_base = (base_url or os.environ.get("A2A_PUBLIC_URL") or "http://127.0.0.1:8092").rstrip(
        "/"
    )
    a2a_url = f"{public_base}/a2a"
    return {
        "name": AGENT_NAME,
        "description": (
            "Enterprise A2A v1.0 FinOps specialist that analyzes GCP Cloud Billing anomalies, "
            "BigQuery query efficiency, and safe cost-remediation plans."
        ),
        "version": AGENT_VERSION,
        "provider": {
            "organization": "Acme Platform Security",
            "url": "https://acme.example.com/platform",
        },
        "supportedInterfaces": [
            {
                "url": a2a_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            },
            {
                "url": a2a_url,
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0",
            },
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "cost-anomaly-triage",
                "name": "Cost Anomaly Triage",
                "description": (
                    "Diagnose unexpected GCP Cloud Billing spend spikes by service, SKU, "
                    "and project labels without making destructive changes."
                ),
                "tags": ["finops", "billing", "anomaly", "gcp"],
                "examples": [
                    "Why did BigQuery spend in acme-analytics jump 45% yesterday?",
                    "Triage the top 3 SKUs contributing to our weekly Cloud Billing alert.",
                ],
            },
            {
                "id": "bq-query-optimization",
                "name": "BigQuery Query Optimization",
                "description": (
                    "Inspect BigQuery Standard SQL queries for full table scans, missing "
                    "partition filters, and SELECT * anti-patterns, then propose safe rewrites."
                ),
                "tags": ["finops", "bigquery", "sql", "cost-optimization"],
                "examples": [
                    "Optimize SELECT * FROM analytics.events for lower bytes scanned.",
                    "Estimate on-demand savings from adding a _PARTITIONDATE filter.",
                ],
            },
        ],
    }


def _extract_prompt_text(body: dict[str, Any]) -> str:
    """Extract user prompt text from A2A JSON-RPC or HTTP+JSON request payloads."""
    params = body.get("params") if isinstance(body.get("params"), dict) else body
    message = params.get("message") if isinstance(params, dict) else None
    if isinstance(message, dict):
        parts = message.get("parts")
        if isinstance(parts, list):
            texts: list[str] = []
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
            if texts:
                return "\n".join(texts).strip()
        if isinstance(message.get("text"), str):
            return message["text"].strip()
    if isinstance(params, dict) and isinstance(params.get("prompt"), str):
        return params["prompt"].strip()
    if isinstance(body.get("prompt"), str):
        return body["prompt"].strip()
    return ""


def _generate_finops_advice(prompt: str) -> dict[str, Any]:
    """Generate structured, deterministic FinOps guidance for the prompt."""
    lower = prompt.lower()

    if any(tok in lower for tok in ("select ", "bigquery", "sql", "query", "partition", "bytes")):
        category = "bq-query-optimization"
        summary = (
            "BigQuery SQL optimization plan: eliminate SELECT *, add a sargable partition "
            "predicate, and set maximum_bytes_billed before execution."
        )
        recommendations = [
            {
                "priority": "P0",
                "action": "Project only required columns instead of SELECT *",
                "estimated_savings_pct": 55,
                "risk": "low",
            },
            {
                "priority": "P0",
                "action": "Filter on _PARTITIONDATE or the table's partition column",
                "estimated_savings_pct": 30,
                "risk": "low",
            },
            {
                "priority": "P1",
                "action": "Validate query cost with bq query --dry_run and maximum_bytes_billed",
                "estimated_savings_pct": 10,
                "risk": "low",
            },
        ]
        suggested_tools = ["analyze_bq_query"]
    elif any(tok in lower for tok in ("delete", "idle", "dataset", "cleanup", "remove")):
        category = "idle-resource-governance"
        summary = (
            "Idle dataset cleanup requires human review and Agent Gateway + IAP v2 UAP "
            "authorization because delete_idle_dataset carries destructiveHint=true."
        )
        recommendations = [
            {
                "priority": "P0",
                "action": "Verify zero queries over the last 90 days via INFORMATION_SCHEMA.JOBS",
                "estimated_savings_pct": 15,
                "risk": "low",
            },
            {
                "priority": "P1",
                "action": "Run delete_idle_dataset with confirm_delete=false (dry-run) first",
                "estimated_savings_pct": 15,
                "risk": "low",
            },
            {
                "priority": "P2",
                "action": "Obtain FinOps owner sign-off before authorized destructive execution",
                "estimated_savings_pct": 100,
                "risk": "high",
            },
        ]
        suggested_tools = ["get_billing_summary", "delete_idle_dataset"]
    else:
        category = "cost-anomaly-triage"
        summary = (
            "Cloud Billing anomaly triage plan: establish trailing 7d/28d baseline, isolate top "
            "BigQuery and Compute Engine SKU deltas, and apply non-destructive budget caps."
        )
        recommendations = [
            {
                "priority": "P0",
                "action": "Inspect 7-day vs 28-day SKU spend delta using get_billing_summary",
                "estimated_savings_pct": 25,
                "risk": "low",
            },
            {
                "priority": "P1",
                "action": "Audit top BigQuery Analysis jobs via analyze_bq_query",
                "estimated_savings_pct": 40,
                "risk": "low",
            },
            {
                "priority": "P2",
                "action": "Enforce cost_center/owner labels and budget threshold alerts in Terraform",
                "estimated_savings_pct": 15,
                "risk": "low",
            },
        ]
        suggested_tools = ["get_billing_summary", "analyze_bq_query"]

    project_match = re.search(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b", lower)
    detected_project = project_match.group(1) if project_match else "acme-agent-governance"

    return {
        "category": category,
        "project_id": detected_project,
        "summary": summary,
        "read_only_first": True,
        "recommended_mcp_tools": suggested_tools,
        "recommendations": recommendations,
    }


def _create_completed_task(prompt: str, context_id: str | None = None) -> dict[str, Any]:
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    ctx_id = context_id or f"ctx-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc).isoformat()
    advice = _generate_finops_advice(prompt)

    rec_lines = "\n".join(
        f"- [{r['priority']}] {r['action']} (est. savings: {r['estimated_savings_pct']}%, risk: {r['risk']})"
        for r in advice["recommendations"]
    )
    rendered_text = (
        f"FinOps Advice ({advice['category']})\n"
        f"Summary: {advice['summary']}\n"
        f"Recommended Tools: {', '.join(advice['recommended_mcp_tools'])}\n"
        f"Recommendations:\n{rec_lines}"
    )

    task: dict[str, Any] = {
        "id": task_id,
        "contextId": ctx_id,
        "status": {
            "state": "completed",
            "timestamp": now,
            "message": {
                "role": "agent",
                "parts": [{"kind": "text", "text": rendered_text}],
            },
        },
        "artifacts": [
            {
                "artifactId": f"art-{uuid.uuid4().hex[:8]}",
                "name": "finops-advice",
                "parts": [
                    {"kind": "text", "text": rendered_text},
                    {"kind": "data", "data": advice},
                ],
            }
        ],
        "history": [
            {
                "role": "user",
                "parts": [{"kind": "text", "text": prompt}],
            },
            {
                "role": "agent",
                "parts": [{"kind": "text", "text": rendered_text}],
            },
        ],
    }
    TASKS[task_id] = task
    return task


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "agent": "finops-advisor-agent", "version": AGENT_VERSION}


@app.get("/.well-known/agent-card.json")
async def well_known_agent_card(request: Request) -> JSONResponse:
    base_url = os.environ.get("A2A_PUBLIC_URL") or str(request.base_url).rstrip("/")
    return JSONResponse(content=build_agent_card(base_url))


@app.get("/a2a/tasks/{task_id}")
async def get_task_http(task_id: str) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return task


@app.post("/a2a")
async def a2a_endpoint(request: Request) -> JSONResponse:
    """Execute A2A v1.0 requests over JSON-RPC 2.0 or HTTP+JSON."""
    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid JSON payload: {exc}"},
        )

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content={"error": "Request body must be a JSON object"},
        )

    # JSON-RPC 2.0 binding
    if body.get("jsonrpc") == "2.0":
        req_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}

        if method in ("message/send", "tasks/send", "SendMessage"):
            prompt = _extract_prompt_text(body)
            if not prompt:
                return JSONResponse(
                    content={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32602,
                            "message": "Missing prompt text in params.message.parts",
                        },
                    }
                )
            context_id = params.get("contextId") if isinstance(params, dict) else None
            task = _create_completed_task(prompt, context_id=context_id)
            return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": task})

        if method in ("tasks/get", "GetTask"):
            task_id = params.get("id") if isinstance(params, dict) else None
            task = TASKS.get(task_id) if isinstance(task_id, str) else None
            if task is None:
                return JSONResponse(
                    content={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32001, "message": f"Task not found: {task_id!r}"},
                    }
                )
            return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": task})

        if method in ("tasks/cancel", "CancelTask"):
            task_id = params.get("id") if isinstance(params, dict) else None
            task = TASKS.get(task_id) if isinstance(task_id, str) else None
            if task is None:
                return JSONResponse(
                    content={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32001, "message": f"Task not found: {task_id!r}"},
                    }
                )
            task["status"]["state"] = "canceled"
            task["status"]["timestamp"] = datetime.now(timezone.utc).isoformat()
            return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": task})

        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unsupported A2A method: {method!r}"},
            }
        )

    # HTTP+JSON binding
    prompt = _extract_prompt_text(body)
    if not prompt:
        return JSONResponse(
            status_code=400,
            content={"error": "Provide 'message.parts[].text' or 'prompt' for HTTP+JSON execution"},
        )
    context_id = body.get("contextId") if isinstance(body.get("contextId"), str) else None
    task = _create_completed_task(prompt, context_id=context_id)
    return JSONResponse(content=task)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8092"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
