#!/usr/bin/env bash
# Self-contained Zero-Docker end-to-end local attendee smoke test.
#
# Launches all 4 workshop services on isolated ports (default 18090/18091/18092/18080)
# with a temporary working directory and automatic EXIT cleanup, then validates:
#   1. Option 1 local skills ingestion (gcp-finops-toolkit + local-skills/)
#   2. Option 2 offline Git repo shallow-clone & ingest (verifying 40-char git_commit)
#   3. Middleware endpoints (/healthz, /v1/catalog, 4x /v1/marketplace/*.json,
#      plugin ZIPs + X-Bundle-Sha256, POST /mcp initialize/tools/list/
#      load_verified_skill/call_remote_a2a_agent)
#   4. tools.hub_sync sync (full, --bootstrap-mcp-only, safe merge preservation,
#      and --fallback-middleware-url failover)
#   5. platform_agent.adk_orchestrator (--mode rest discovery + ?alt=media,
#      --mode adk --print-config)
#   6. Workstream B offline checks (KMS keygen/sign/verify/tamper-reject,
#      IAP UAP --dry-run, policy_guard.py PreToolUse deny + PostToolUse audit)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"

PORT_OFFSET=0
MOCK_PORT_OVERRIDE="${MOCK_PORT:-}"
MCP_PORT_OVERRIDE="${MCP_PORT:-}"
A2A_PORT_OVERRIDE="${A2A_PORT:-}"
MIDDLEWARE_PORT_OVERRIDE="${MIDDLEWARE_PORT:-}"
PROJECT_ID="${GCP_PROJECT_ID:-acme-agent-governance}"
LOCATION="${GCP_LOCATION:-asia-southeast3}"
SECONDARY_LOCATION="${GCP_SECONDARY_LOCATION:-asia-southeast1}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-20}"

usage() {
  cat <<'EOF'
Usage: scripts/local_smoke_test.sh [OPTIONS]

Run a fully self-contained Zero-Docker local smoke test covering every major
workshop workflow (Workstream A + Workstream B offline checks) on isolated ports.

Options:
  --port-offset N      Add N to default isolated ports (18090/18091/18092/18080)
  --project ID         Project ID for local mock registry (default: acme-agent-governance)
  --location REGION    Location for local mock registry (default: asia-southeast3)
  --timeout SECONDS    Service startup readiness timeout (default: 20)
  -h, --help           Show this help message and exit

Environment Overrides:
  MOCK_PORT, MCP_PORT, A2A_PORT, MIDDLEWARE_PORT
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port-offset)
      PORT_OFFSET="$2"
      shift 2
      ;;
    --project)
      PROJECT_ID="$2"
      shift 2
      ;;
    --location)
      LOCATION="$2"
      shift 2
      ;;
    --timeout)
      HEALTH_TIMEOUT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "${PORT_OFFSET}" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --port-offset must be a non-negative integer (got '${PORT_OFFSET}')." >&2
  exit 2
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: Python virtualenv not found at '${PYTHON_BIN}'." >&2
  echo "Run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

MOCK_PORT="${MOCK_PORT_OVERRIDE:-$((18090 + PORT_OFFSET))}"
MCP_PORT="${MCP_PORT_OVERRIDE:-$((18091 + PORT_OFFSET))}"
A2A_PORT="${A2A_PORT_OVERRIDE:-$((18092 + PORT_OFFSET))}"
MIDDLEWARE_PORT="${MIDDLEWARE_PORT_OVERRIDE:-$((18080 + PORT_OFFSET))}"

REGISTRY_URL="http://127.0.0.1:${MOCK_PORT}/v1"
MIDDLEWARE_URL="http://127.0.0.1:${MIDDLEWARE_PORT}"

TMP_DIR="$(mktemp -d /tmp/workshop-local-smoke.XXXXXX)"
LOG_DIR="${TMP_DIR}/logs"
mkdir -p "${LOG_DIR}"
PIDS=()
PASSED_STEPS=()

cleanup() {
  local exit_code=$?
  trap - INT TERM EXIT
  for pid in "${PIDS[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
  for pid in "${PIDS[@]}"; do
    wait "${pid}" 2>/dev/null || true
  done
  if [[ "${exit_code}" -ne 0 && -d "${LOG_DIR}" ]]; then
    echo "" >&2
    echo "Smoke test failed — recent service logs:" >&2
    for lf in "${LOG_DIR}"/*.log; do
      if [[ -f "${lf}" ]]; then
        echo "--- $(basename "${lf}") ---" >&2
        tail -n 30 "${lf}" >&2 || true
      fi
    done
  fi
  rm -rf "${TMP_DIR}"
  exit "${exit_code}"
}

trap cleanup INT TERM EXIT

record_pass() {
  PASSED_STEPS+=("$1")
  printf "  [PASS] %s\n" "$1"
}

cd "${REPO_ROOT}"

echo "========================================================================"
echo "Enterprise Agent Marketplace Workshop — Zero-Docker Local Smoke Test"
echo "========================================================================"
echo "Isolated ports : mock=${MOCK_PORT} mcp=${MCP_PORT} a2a=${A2A_PORT} middleware=${MIDDLEWARE_PORT}"
echo "Project/Region : ${PROJECT_ID} / ${LOCATION}"
echo "Temp workspace : ${TMP_DIR}"
echo ""

# Launch 4 local services
"${PYTHON_BIN}" -m uvicorn mock_registry.app:app \
  --host 127.0.0.1 --port "${MOCK_PORT}" \
  >"${LOG_DIR}/mock_registry.log" 2>&1 &
PID_MOCK=$!
PIDS+=("${PID_MOCK}")

"${PYTHON_BIN}" -m uvicorn --app-dir samples/mcp-server app:app \
  --host 127.0.0.1 --port "${MCP_PORT}" \
  >"${LOG_DIR}/sample_mcp_server.log" 2>&1 &
PID_MCP=$!
PIDS+=("${PID_MCP}")

A2A_PUBLIC_URL="http://127.0.0.1:${A2A_PORT}" \
"${PYTHON_BIN}" -m uvicorn --app-dir samples/a2a-agent app:app \
  --host 127.0.0.1 --port "${A2A_PORT}" \
  >"${LOG_DIR}/sample_a2a_agent.log" 2>&1 &
PID_A2A=$!
PIDS+=("${PID_A2A}")

AGENT_REGISTRY_BASE_URL="${REGISTRY_URL}" \
GCP_PROJECT_ID="${PROJECT_ID}" \
GCP_LOCATION="${LOCATION}" \
GCP_SECONDARY_LOCATION="${SECONDARY_LOCATION}" \
MIDDLEWARE_PUBLIC_URL="${MIDDLEWARE_URL}" \
REQUIRE_KMS_SIGNATURE="false" \
"${PYTHON_BIN}" -m uvicorn middleware.app.main:app \
  --host 127.0.0.1 --port "${MIDDLEWARE_PORT}" \
  >"${LOG_DIR}/middleware.log" 2>&1 &
PID_MW=$!
PIDS+=("${PID_MW}")

wait_health() {
  local name="$1" url="$2" pid="$3"
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  while (( SECONDS < deadline )); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      echo "ERROR: ${name} terminated prematurely." >&2
      exit 1
    fi
    if curl -fsS --max-time 2 "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  echo "ERROR: Timed out waiting for ${name} at ${url}." >&2
  exit 1
}

wait_health "mock-registry" "http://127.0.0.1:${MOCK_PORT}/healthz" "${PID_MOCK}"
wait_health "sample-mcp-server" "http://127.0.0.1:${MCP_PORT}/healthz" "${PID_MCP}"
wait_health "sample-a2a-agent" "http://127.0.0.1:${A2A_PORT}/healthz" "${PID_A2A}"
wait_health "middleware" "${MIDDLEWARE_URL}/healthz" "${PID_MW}"
record_pass "Started all 4 isolated local services cleanly"

# ----------------------------------------------------------------------------
# Step 1: Option 1 Local Skills Ingestion (plugin + standalone local-skills/)
# ----------------------------------------------------------------------------
echo ""
echo "[1/6] Option 1: Local plugin & local-skills/ ingestion"

STAGED_PLUGIN="${TMP_DIR}/gcp-finops-toolkit"
cp -R "${REPO_ROOT}/starter-marketplace/plugins/gcp-finops-toolkit" "${STAGED_PLUGIN}"

# Point copied starter plugin to our isolated sample MCP & A2A ports so live
# MCP tools/list probing and remote A2A invocation exercise our isolated servers.
"${PYTHON_BIN}" - "${STAGED_PLUGIN}" "${MCP_PORT}" "${A2A_PORT}" <<'PYEOF'
import sys
from pathlib import Path

root = Path(sys.argv[1])
mcp_port = sys.argv[2]
a2a_port = sys.argv[3]

for rel in (
    "mcp.json",
    ".mcp.json",
    "mcp_config.json",
    "agents/finops-advisor/agent-card.json",
):
    p = root / rel
    if p.is_file():
        text = p.read_text(encoding="utf-8")
        text = text.replace("127.0.0.1:8091", f"127.0.0.1:{mcp_port}")
        text = text.replace("127.0.0.1:8092", f"127.0.0.1:{a2a_port}")
        p.write_text(text, encoding="utf-8")
PYEOF

"${PYTHON_BIN}" -m tools.plugin_sync ingest \
  --source-type local \
  --path "${STAGED_PLUGIN}" \
  --registry-url "${REGISTRY_URL}" \
  --project "${PROJECT_ID}" \
  --location "${LOCATION}" \
  --target-state TARGET_STATE_ACTIVE \
  --scanning-policy ENABLED_BLOCKING \
  >"${TMP_DIR}/ingest_finops.json"

"${PYTHON_BIN}" -m tools.plugin_sync ingest \
  --source-type local \
  --path "${REPO_ROOT}/local-skills" \
  --plugin-name local-ops-toolkit \
  --registry-url "${REGISTRY_URL}" \
  --project "${PROJECT_ID}" \
  --location "${LOCATION}" \
  --target-state TARGET_STATE_ACTIVE \
  --scanning-policy ENABLED_BLOCKING \
  --skip-mcp-probe \
  >"${TMP_DIR}/ingest_local_skills.json"

record_pass "Option 1 local ingestion (gcp-finops-toolkit + local-ops-toolkit)"

# ----------------------------------------------------------------------------
# Step 2: Option 2 Offline Git Repo Clone & Ingest (40-char git_commit check)
# ----------------------------------------------------------------------------
echo ""
echo "[2/6] Option 2: Offline Git repository shallow clone & ingest"

GIT_REPO="${TMP_DIR}/offline-git-skills"
mkdir -p "${GIT_REPO}/git-smoke-skill"
cat >"${GIT_REPO}/git-smoke-skill/SKILL.md" <<'EOF'
---
name: git-smoke-skill
description: Offline Git repository smoke-test skill verifying shallow-clone provenance.
---

# Git Smoke Skill

Use this runbook to verify Git-backed ingestion provenance in Agent Registry v1.
EOF

git -C "${GIT_REPO}" init -q -b main
git -C "${GIT_REPO}" config user.name "Workshop Smoke Test"
git -C "${GIT_REPO}" config user.email "smoke-test@acme.example.com"
git -C "${GIT_REPO}" add .
git -C "${GIT_REPO}" commit -q -m "Add git-smoke-skill for offline Option 2 test"
EXPECTED_GIT_SHA="$(git -C "${GIT_REPO}" rev-parse HEAD)"

"${PYTHON_BIN}" -m tools.plugin_sync ingest \
  --source-type git \
  --repo-url "${GIT_REPO}" \
  --ref main \
  --plugin-name git-smoke-toolkit \
  --registry-url "${REGISTRY_URL}" \
  --project "${PROJECT_ID}" \
  --location "${LOCATION}" \
  --target-state TARGET_STATE_ACTIVE \
  --scanning-policy ENABLED_BLOCKING \
  --skip-mcp-probe \
  >"${TMP_DIR}/ingest_git.json"

"${PYTHON_BIN}" - "${TMP_DIR}/ingest_git.json" "${EXPECTED_GIT_SHA}" <<'PYEOF'
import json, re, sys
from pathlib import Path

doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_sha = sys.argv[2].strip()
assert re.fullmatch(r"[0-9a-f]{40}", expected_sha), f"Invalid expected SHA: {expected_sha}"

serialized = json.dumps(doc)
assert expected_sha in serialized, f"40-char git_commit {expected_sha} not found in ingest output"
PYEOF

record_pass "Option 2 offline Git ingest verified 40-char git_commit (${EXPECTED_GIT_SHA:0:12}...)"

# ----------------------------------------------------------------------------
# Step 3: Middleware Verification (/healthz, /v1/catalog, 4x manifests, ZIPs, /mcp)
# ----------------------------------------------------------------------------
echo ""
echo "[3/6] Middleware API, Polyglot manifests, ZIP SHA-256, and MCP Gateway"

"${PYTHON_BIN}" - "${MIDDLEWARE_URL}" "${LOCATION}" "${TMP_DIR}" <<'PYEOF'
import hashlib, io, json, sys, zipfile
from pathlib import Path
import httpx

mw_url = sys.argv[1].rstrip("/")
expected_region = sys.argv[2]
tmp_dir = Path(sys.argv[3])

with httpx.Client(timeout=20.0) as client:
    # 1. /healthz (checks region == expected_region; accepts location fallback if region key absent)
    r = client.get(f"{mw_url}/healthz")
    assert r.status_code == 200, f"/healthz failed: {r.status_code} {r.text}"
    health = r.json()
    assert health.get("status") == "ok", f"Unexpected /healthz: {health}"
    assert health.get("region") == expected_region, (
        f"Expected region={expected_region!r}, got {health!r}"
    )

    # 2. /v1/catalog
    r = client.get(f"{mw_url}/v1/catalog")
    assert r.status_code == 200, f"/v1/catalog failed: {r.status_code} {r.text}"
    catalog = r.json()
    plugin_names = {p["name"] for p in catalog.get("plugins", [])}
    for expected_plugin in ("gcp-finops-toolkit", "local-ops-toolkit", "git-smoke-toolkit"):
        assert expected_plugin in plugin_names, f"Missing plugin {expected_plugin} in {plugin_names}"
    assert len(catalog.get("skills", [])) >= 6, f"Expected >=6 verified skills, got {len(catalog.get('skills', []))}"
    assert not catalog.get("rejectedSkills"), f"Unexpected rejectedSkills: {catalog.get('rejectedSkills')}"

    # 3. All 4 IDE marketplace manifests
    for ide in ("claude", "copilot", "codex", "antigravity"):
        mr = client.get(f"{mw_url}/v1/marketplace/{ide}.json")
        assert mr.status_code == 200, f"/v1/marketplace/{ide}.json failed: {mr.status_code}"
        mdoc = mr.json()
        m_plugins = {p["name"] for p in mdoc.get("plugins", [])}
        assert "gcp-finops-toolkit" in m_plugins, f"{ide}.json missing gcp-finops-toolkit"
        assert "local-ops-toolkit" in m_plugins, f"{ide}.json missing local-ops-toolkit"

    # 4. Plugin ZIPs + X-Bundle-Sha256 header integrity check
    for pname in ("gcp-finops-toolkit", "local-ops-toolkit"):
        zr = client.get(f"{mw_url}/v1/plugins/{pname}.zip")
        assert zr.status_code == 200, f"GET /v1/plugins/{pname}.zip failed: {zr.status_code}"
        hdr_sha = (zr.headers.get("X-Bundle-Sha256") or "").strip().lower()
        actual_sha = hashlib.sha256(zr.content).hexdigest()
        assert hdr_sha == actual_sha, f"{pname}.zip X-Bundle-Sha256 mismatch: {hdr_sha} != {actual_sha}"
        with zipfile.ZipFile(io.BytesIO(zr.content)) as zf:
            names = set(zf.namelist())
            assert "plugin.json" in names, f"{pname}.zip missing plugin.json"
            assert ".antigravity-plugin/plugin.json" in names, f"{pname}.zip missing .antigravity-plugin/plugin.json"

    # 5. POST /mcp — initialize, tools/list, load_verified_skill, call_remote_a2a_agent
    init_resp = client.post(
        f"{mw_url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
    )
    assert init_resp.status_code == 200
    assert init_resp.headers.get("Mcp-Session-Id"), "Missing Mcp-Session-Id header on initialize"
    assert "result" in init_resp.json()

    list_resp = client.post(
        f"{mw_url}/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert list_resp.status_code == 200
    tool_names = {t["name"] for t in list_resp.json()["result"]["tools"]}
    for req_tool in ("search_catalog", "load_verified_skill", "list_governed_mcp_servers", "call_remote_a2a_agent"):
        assert req_tool in tool_names, f"Missing MCP gateway tool: {req_tool}"

    skill_resp = client.post(
        f"{mw_url}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "load_verified_skill", "arguments": {"skill_name": "cost-anomaly-triage"}},
        },
    )
    assert skill_resp.status_code == 200
    skill_res = skill_resp.json()["result"]
    assert skill_res.get("isError") is False, f"load_verified_skill failed: {skill_res}"
    assert skill_res["structuredContent"]["instructions"], "Empty skill instructions"

    a2a_resp = client.post(
        f"{mw_url}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "call_remote_a2a_agent",
                "arguments": {
                    "agent_name": "finops-advisor-agent",
                    "task": "Why did BigQuery spend in acme-agent-governance spike yesterday?",
                },
            },
        },
    )
    assert a2a_resp.status_code == 200
    a2a_res = a2a_resp.json()["result"]
    assert a2a_res.get("isError") is False, f"call_remote_a2a_agent failed: {a2a_res}"
    assert a2a_res["structuredContent"]["response"]["result"]["status"]["state"] == "completed"
PYEOF

record_pass "Middleware /healthz (${LOCATION}), /v1/catalog, 4x manifests, ZIP SHA-256, and /mcp gateway"

# ----------------------------------------------------------------------------
# Step 4: tools.hub_sync (full, bootstrap-mcp-only, safe-merge, failover)
# ----------------------------------------------------------------------------
echo ""
echo "[4/6] IDE Hydration CLI (full sync, bootstrap-mcp-only, safe merge, failover)"

DEMO_HOME="${TMP_DIR}/demo-home"
DEMO_HOME_MCP="${TMP_DIR}/demo-home-mcp-only"
DEMO_HOME_FAILOVER="${TMP_DIR}/demo-home-failover"
mkdir -p "${DEMO_HOME}" "${DEMO_HOME_MCP}" "${DEMO_HOME_FAILOVER}"

# 4a. Full sync
"${PYTHON_BIN}" -m tools.hub_sync sync \
  --middleware-url "${MIDDLEWARE_URL}" \
  --home "${DEMO_HOME}" \
  --tools all \
  --no-exec \
  >"${TMP_DIR}/hub_sync_full.out"

"${PYTHON_BIN}" -m tools.hub_sync status --home "${DEMO_HOME}" --json >"${TMP_DIR}/hub_status.json"

# 4b. --bootstrap-mcp-only
"${PYTHON_BIN}" -m tools.hub_sync sync \
  --middleware-url "${MIDDLEWARE_URL}" \
  --home "${DEMO_HOME_MCP}" \
  --tools all \
  --bootstrap-mcp-only \
  --no-exec \
  >"${TMP_DIR}/hub_sync_bootstrap.out"

# 4c. Ownership-tracked safe merge preservation test (personal MCP + disabledTools)
"${PYTHON_BIN}" - "${DEMO_HOME}" <<'PYEOF'
import json, sys
from pathlib import Path

mcp_path = Path(sys.argv[1]) / ".gemini" / "config" / "mcp_config.json"
assert mcp_path.is_file(), f"Missing {mcp_path}"
doc = json.loads(mcp_path.read_text(encoding="utf-8"))
servers = doc.setdefault("mcpServers", {})
servers["my-personal-mcp"] = {"url": "http://127.0.0.1:9999/mcp"}
assert "finops-mcp-server" in servers, f"Expected finops-mcp-server in {servers.keys()}"
servers["finops-mcp-server"]["disabledTools"] = ["delete_idle_dataset"]
mcp_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
PYEOF

"${PYTHON_BIN}" -m tools.hub_sync sync \
  --middleware-url "${MIDDLEWARE_URL}" \
  --home "${DEMO_HOME}" \
  --tools antigravity \
  --no-exec \
  >"${TMP_DIR}/hub_sync_resync.out"

"${PYTHON_BIN}" - "${DEMO_HOME}" "${DEMO_HOME_MCP}" "${TMP_DIR}/hub_status.json" <<'PYEOF'
import json, sys
from pathlib import Path

demo_home = Path(sys.argv[1])
demo_mcp = Path(sys.argv[2])
status_doc = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))

assert status_doc["synced"] is True
assert "gcp-finops-toolkit" in status_doc["plugins"]
assert "local-ops-toolkit" in status_doc["plugins"]

# Verify personal MCP & disabledTools survived re-sync
mcp_doc = json.loads((demo_home / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8"))
servers = mcp_doc["mcpServers"]
assert servers["my-personal-mcp"]["url"] == "http://127.0.0.1:9999/mcp", "Personal MCP server clobbered!"
assert servers["finops-mcp-server"].get("disabledTools") == ["delete_idle_dataset"], "User disabledTools lost!"

# Verify --bootstrap-mcp-only wired MCP config without hydrating plugin directories
assert (demo_mcp / ".gemini" / "config" / "mcp_config.json").is_file()
plugins_dir = demo_mcp / ".acme-agent-hub" / "plugins"
assert not plugins_dir.exists() or not any(plugins_dir.iterdir()), "bootstrap-mcp-only downloaded plugins!"
PYEOF

# 4d. --fallback-middleware-url failover test (dead primary -> live local fallback)
echo "  [INFO] Simulating intentional Bangkok primary outage (http://127.0.0.1:19999) to verify regional failover..."
"${PYTHON_BIN}" -m tools.hub_sync sync \
  --middleware-url "http://127.0.0.1:19999" \
  --fallback-middleware-url "${MIDDLEWARE_URL}" \
  --home "${DEMO_HOME_FAILOVER}" \
  --tools all \
  --no-exec \
  >"${TMP_DIR}/hub_sync_failover.out"

"${PYTHON_BIN}" - "${DEMO_HOME_FAILOVER}" <<'PYEOF'
import json, sys
from pathlib import Path

manifest_path = Path(sys.argv[1]) / ".acme-agent-hub" / ".sync-manifest.json"
assert manifest_path.is_file(), "Failover sync did not write .sync-manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
assert "gcp-finops-toolkit" in manifest.get("owned_plugins", {}), "Failover sync missed gcp-finops-toolkit"
PYEOF

record_pass "tools.hub_sync full sync, --bootstrap-mcp-only, safe-merge preservation, and --fallback-middleware-url failover"

# ----------------------------------------------------------------------------
# Step 5: Platform Agent Orchestrator (--mode rest + ?alt=media & --mode adk)
# ----------------------------------------------------------------------------
echo ""
echo "[5/6] Platform Agent Orchestrator (REST discovery + ?alt=media & ADK config)"

"${PYTHON_BIN}" -m platform_agent.adk_orchestrator \
  --mode rest \
  --project-id "${PROJECT_ID}" \
  --location "${LOCATION}" \
  --base-url "${REGISTRY_URL}" \
  >"${TMP_DIR}/platform_rest.json"

"${PYTHON_BIN}" -m platform_agent.adk_orchestrator \
  --mode adk \
  --project-id "${PROJECT_ID}" \
  --location "${LOCATION}" \
  --print-config \
  >"${TMP_DIR}/platform_adk_config.json"

"${PYTHON_BIN}" - "${TMP_DIR}/platform_rest.json" "${TMP_DIR}/platform_adk_config.json" "${LOCATION}" <<'PYEOF'
import json, sys
from pathlib import Path

rest_doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
adk_doc = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
expected_loc = sys.argv[3]

assert len(rest_doc.get("skills", [])) >= 1, f"Expected discovered skills, got {rest_doc}"
assert len(rest_doc.get("extracted_skills", [])) >= 1, f"Expected ?alt=media downloaded skills in extracted_skills, got {rest_doc}"
assert all(s.get("sha256_hash") for s in rest_doc["extracted_skills"]), "Missing sha256_hash on downloaded skills"
assert len(rest_doc.get("mcp_servers", [])) >= 1, f"Expected >=1 MCP server in {rest_doc}"
assert len(rest_doc.get("agents", [])) >= 1, f"Expected >=1 A2A agent in {rest_doc}"

assert adk_doc["registry"]["location"] == expected_loc
assert "GcpAuthProvider" in adk_doc["auth_manager"]["registration_call"]
PYEOF

record_pass "platform_agent.adk_orchestrator --mode rest (?alt=media) and --mode adk --print-config"

# ----------------------------------------------------------------------------
# Step 6: Workstream B Offline Security & Governance Checks
# ----------------------------------------------------------------------------
echo ""
echo "[6/6] Workstream B offline checks (KMS signing, IAP UAP dry-run, Policy Guard hook)"

KMS_DIR="${TMP_DIR}/kms"
mkdir -p "${KMS_DIR}"

"${PYTHON_BIN}" "${REPO_ROOT}/security-governance/kms/sign_and_verify.py" keygen \
  --private-out "${KMS_DIR}/signing-private.pem" \
  --public-out "${KMS_DIR}/signing-public.pem" \
  >"${KMS_DIR}/keygen.json"

# Create a fresh unsigned skill ZIP from local-skills/cloud-run-debugger
"${PYTHON_BIN}" - "${REPO_ROOT}/local-skills/cloud-run-debugger" "${KMS_DIR}/unsigned.zip" <<'PYEOF'
import sys, zipfile
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for path in sorted(src.rglob("*")):
        if path.is_file():
            zf.write(path, arcname=f"{src.name}/{path.relative_to(src).as_posix()}")
PYEOF

"${PYTHON_BIN}" "${REPO_ROOT}/security-governance/kms/sign_and_verify.py" sign \
  --input "${KMS_DIR}/unsigned.zip" \
  --output "${KMS_DIR}/signed.zip" \
  --private-key "${KMS_DIR}/signing-private.pem" \
  >"${KMS_DIR}/sign.json"

SIGNED_SHA="$("${PYTHON_BIN}" -c "import json; print(json.load(open('${KMS_DIR}/sign.json'))['sha256'])")"

"${PYTHON_BIN}" "${REPO_ROOT}/security-governance/kms/sign_and_verify.py" verify \
  --input "${KMS_DIR}/signed.zip" \
  --public-key "${KMS_DIR}/signing-public.pem" \
  --expected-sha256 "${SIGNED_SHA}" \
  >"${KMS_DIR}/verify_ok.json"

# Tamper with signed ZIP (modify SKILL.md body without re-signing) and confirm rejection
"${PYTHON_BIN}" - "${KMS_DIR}/signed.zip" "${KMS_DIR}/tampered.zip" <<'PYEOF'
import sys, zipfile
from pathlib import Path

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zout:
    for info in zin.infolist():
        data = zin.read(info)
        if info.filename.endswith("SKILL.md"):
            data += b"\n<!-- tampered instruction -->\n"
        zout.writestr(info, data)
PYEOF

if "${PYTHON_BIN}" "${REPO_ROOT}/security-governance/kms/sign_and_verify.py" verify \
  --input "${KMS_DIR}/tampered.zip" \
  --public-key "${KMS_DIR}/signing-public.pem" \
  >"${KMS_DIR}/verify_tampered.json" 2>&1; then
  echo "ERROR: Expected tampered ZIP signature verification to fail, but it succeeded!" >&2
  exit 1
fi

# IAP UAP --dry-run check (Agent Gateway + IAP v2 UAP is supported in us-central1 / europe-west1)
bash "${REPO_ROOT}/security-governance/iap-uap/apply-uap.sh" \
  --project "${PROJECT_ID}" \
  --location "us-central1" \
  --dry-run \
  >"${TMP_DIR}/uap_dry_run.out"
grep -q "egressViaIAP" "${TMP_DIR}/uap_dry_run.out"

# Policy Guard PreToolUse deny & PostToolUse audit check
AUDIT_LOG="${TMP_DIR}/policy-guard-audit.jsonl"

PRE_DENY="$(printf '%s' '{"tool_call":{"name":"mcp_finops-mcp-server_delete_idle_dataset","args":{"dataset_id":"scratch"},"annotations":{"destructiveHint":true}}}' \
  | "${PYTHON_BIN}" "${REPO_ROOT}/security-governance/hooks/scripts/policy_guard.py" --event PreToolUse)"

PRE_ALLOW="$(printf '%s' '{"tool_call":{"name":"mcp_finops-mcp-server_get_billing_summary","args":{"project_id":"acme"},"annotations":{"readOnlyHint":true}}}' \
  | "${PYTHON_BIN}" "${REPO_ROOT}/security-governance/hooks/scripts/policy_guard.py" --event PreToolUse)"

POST_OUT="$(printf '%s' '{"conversation_id":"smoke-1","tool_call":{"name":"get_billing_summary","args":{"project_id":"acme","api_secret":"SUPER_SECRET_123"}},"result":{"ok":true}}' \
  | POLICY_GUARD_AUDIT_LOG="${AUDIT_LOG}" "${PYTHON_BIN}" "${REPO_ROOT}/security-governance/hooks/scripts/policy_guard.py" --event PostToolUse)"

"${PYTHON_BIN}" - "${PRE_DENY}" "${PRE_ALLOW}" "${POST_OUT}" "${AUDIT_LOG}" <<'PYEOF'
import json, sys
from pathlib import Path

deny_doc = json.loads(sys.argv[1])
allow_doc = json.loads(sys.argv[2])
post_doc = json.loads(sys.argv[3])
audit_path = Path(sys.argv[4])

assert deny_doc.get("decision") == "deny", f"Expected PreToolUse deny, got: {deny_doc}"
assert allow_doc == {}, f"Expected PreToolUse allow {{}}, got: {allow_doc}"
assert post_doc == {}, f"Expected PostToolUse {{}}, got: {post_doc}"
assert audit_path.is_file(), "PostToolUse did not write audit log"
audit_text = audit_path.read_text(encoding="utf-8")
assert "SUPER_SECRET_123" not in audit_text, "Policy Guard leaked raw secret value into audit log!"
entry = json.loads(audit_text.strip().splitlines()[-1])
assert entry["tool_name"] == "get_billing_summary"
assert "api_secret" in entry["arg_keys"]
PYEOF

record_pass "Workstream B offline checks (KMS sign/verify/tamper-reject, IAP UAP --dry-run, Policy Guard hook)"

echo ""
echo "========================================================================"
echo "LOCAL ZERO-DOCKER SMOKE TEST PASSED (${#PASSED_STEPS[@]} checks)"
echo "========================================================================"
for step in "${PASSED_STEPS[@]}"; do
  echo "  [OK] ${step}"
done
