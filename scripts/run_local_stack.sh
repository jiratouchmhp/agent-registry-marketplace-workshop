#!/usr/bin/env bash
# Single-command Zero-Docker local workshop stack launcher.
#
# Starts all 4 local FastAPI services in parallel:
#   1. Mock Agent Registry v1  (default :8090)
#   2. Sample FinOps MCP Server (default :8091)
#   3. Sample FinOps A2A Agent  (default :8092)
#   4. Polyglot Middleware      (default :8080)
#
# Waits for all 4 /healthz endpoints to return HTTP 200, prints URLs and
# attendee next-step commands, and cleanly terminates all child PIDs on exit.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"

PORT_OFFSET=0
MOCK_PORT_OVERRIDE="${MOCK_PORT:-}"
MCP_PORT_OVERRIDE="${MCP_PORT:-}"
A2A_PORT_OVERRIDE="${A2A_PORT:-}"
MIDDLEWARE_PORT_OVERRIDE="${MIDDLEWARE_PORT:-}"

GCP_PROJECT_ID="${GCP_PROJECT_ID:-acme-agent-governance}"
GCP_LOCATION="${GCP_LOCATION:-asia-southeast3}"
GCP_SECONDARY_LOCATION="${GCP_SECONDARY_LOCATION:-asia-southeast1}"
REQUIRE_KMS_SIGNATURE="${REQUIRE_KMS_SIGNATURE:-false}"
KMS_PUBLIC_KEY_PEM_PATH="${KMS_PUBLIC_KEY_PEM_PATH:-}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-20}"
LOG_DIR=""

usage() {
  cat <<'EOF'
Usage: scripts/run_local_stack.sh [OPTIONS]

Launch the full 4-service Enterprise Agent Marketplace stack locally with zero
Docker required, wait for all /healthz endpoints, and clean up on Ctrl-C.

Options:
  --port-offset N        Add N to default ports (8090/8091/8092/8080)
  --project ID           GCP_PROJECT_ID passed to middleware (default: acme-agent-governance)
  --location REGION      GCP_LOCATION passed to middleware (default: asia-southeast3)
  --timeout SECONDS      Max seconds to wait for /healthz readiness (default: 20)
  --log-dir PATH         Directory to write service logs (default: temp dir)
  -h, --help             Show this help message and exit

Environment Overrides:
  MOCK_PORT              Override mock_registry port (default: 8090 + offset)
  MCP_PORT               Override sample-mcp-server port (default: 8091 + offset)
  A2A_PORT               Override sample-a2a-agent port (default: 8092 + offset)
  MIDDLEWARE_PORT        Override middleware port (default: 8080 + offset)
  GCP_LOCATION           Override region (default: asia-southeast3)
  REQUIRE_KMS_SIGNATURE  true|false (default: false)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port-offset)
      PORT_OFFSET="$2"
      shift 2
      ;;
    --project)
      GCP_PROJECT_ID="$2"
      shift 2
      ;;
    --location)
      GCP_LOCATION="$2"
      shift 2
      ;;
    --timeout)
      HEALTH_TIMEOUT="$2"
      shift 2
      ;;
    --log-dir)
      LOG_DIR="$2"
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

MOCK_PORT="${MOCK_PORT_OVERRIDE:-$((8090 + PORT_OFFSET))}"
MCP_PORT="${MCP_PORT_OVERRIDE:-$((8091 + PORT_OFFSET))}"
A2A_PORT="${A2A_PORT_OVERRIDE:-$((8092 + PORT_OFFSET))}"
MIDDLEWARE_PORT="${MIDDLEWARE_PORT_OVERRIDE:-$((8080 + PORT_OFFSET))}"

CREATED_TEMP_LOG_DIR=false
if [[ -z "${LOG_DIR}" ]]; then
  LOG_DIR="$(mktemp -d /tmp/workshop-local-stack.XXXXXX)"
  CREATED_TEMP_LOG_DIR=true
else
  mkdir -p "${LOG_DIR}"
fi

PIDS=()

cleanup() {
  local exit_code=$?
  trap - INT TERM EXIT
  if [[ ${#PIDS[@]} -gt 0 ]]; then
    echo ""
    echo "Stopping local workshop stack..."
    for pid in "${PIDS[@]}"; do
      if kill -0 "${pid}" 2>/dev/null; then
        kill "${pid}" 2>/dev/null || true
      fi
    done
    for pid in "${PIDS[@]}"; do
      wait "${pid}" 2>/dev/null || true
    done
  fi
  if [[ "${CREATED_TEMP_LOG_DIR}" == "true" && -d "${LOG_DIR}" ]]; then
    rm -rf "${LOG_DIR}"
  fi
  exit "${exit_code}"
}

trap cleanup INT TERM EXIT

# Quick port availability check via Python socket probe
"${PYTHON_BIN}" - "${MOCK_PORT}" "${MCP_PORT}" "${A2A_PORT}" "${MIDDLEWARE_PORT}" <<'PYEOF'
import socket, sys
for raw in sys.argv[1:]:
    port = int(raw)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            raise SystemExit(
                f"ERROR: Port 127.0.0.1:{port} is already in use. "
                "Stop the conflicting process or pass --port-offset N."
            )
PYEOF

cd "${REPO_ROOT}"

echo "Starting Zero-Docker Local Workshop Stack..."
echo "  Project  : ${GCP_PROJECT_ID}"
echo "  Location : ${GCP_LOCATION} (secondary: ${GCP_SECONDARY_LOCATION})"
echo "  Logs     : ${LOG_DIR}"
echo ""

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

AGENT_REGISTRY_BASE_URL="http://127.0.0.1:${MOCK_PORT}/v1" \
GCP_PROJECT_ID="${GCP_PROJECT_ID}" \
GCP_LOCATION="${GCP_LOCATION}" \
GCP_SECONDARY_LOCATION="${GCP_SECONDARY_LOCATION}" \
MIDDLEWARE_PUBLIC_URL="http://127.0.0.1:${MIDDLEWARE_PORT}" \
REQUIRE_KMS_SIGNATURE="${REQUIRE_KMS_SIGNATURE}" \
KMS_PUBLIC_KEY_PEM_PATH="${KMS_PUBLIC_KEY_PEM_PATH}" \
"${PYTHON_BIN}" -m uvicorn middleware.app.main:app \
  --host 127.0.0.1 --port "${MIDDLEWARE_PORT}" \
  >"${LOG_DIR}/middleware.log" 2>&1 &
PID_MW=$!
PIDS+=("${PID_MW}")

wait_for_health() {
  local name="$1"
  local url="$2"
  local pid="$3"
  local log_file="$4"
  local deadline=$((SECONDS + HEALTH_TIMEOUT))

  while (( SECONDS < deadline )); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      echo "ERROR: ${name} exited unexpectedly before becoming healthy." >&2
      echo "--- ${log_file} ---" >&2
      cat "${log_file}" >&2 || true
      exit 1
    fi
    if curl -fsS --max-time 2 "${url}" >/dev/null 2>&1; then
      printf "  [READY] %-20s %s (PID %s)\n" "${name}" "${url}" "${pid}"
      return 0
    fi
    sleep 0.25
  done

  echo "ERROR: Timed out after ${HEALTH_TIMEOUT}s waiting for ${name} at ${url}." >&2
  echo "--- ${log_file} ---" >&2
  cat "${log_file}" >&2 || true
  exit 1
}

wait_for_health "mock-registry" "http://127.0.0.1:${MOCK_PORT}/healthz" "${PID_MOCK}" "${LOG_DIR}/mock_registry.log"
wait_for_health "sample-mcp-server" "http://127.0.0.1:${MCP_PORT}/healthz" "${PID_MCP}" "${LOG_DIR}/sample_mcp_server.log"
wait_for_health "sample-a2a-agent" "http://127.0.0.1:${A2A_PORT}/healthz" "${PID_A2A}" "${LOG_DIR}/sample_a2a_agent.log"
wait_for_health "middleware" "http://127.0.0.1:${MIDDLEWARE_PORT}/healthz" "${PID_MW}" "${LOG_DIR}/middleware.log"

echo ""
echo "========================================================================"
echo "All 4 Local Services Are Running!"
echo "========================================================================"
echo "  Mock Agent Registry v1 : http://127.0.0.1:${MOCK_PORT}/v1"
echo "  Sample FinOps MCP      : http://127.0.0.1:${MCP_PORT}/mcp"
echo "  Sample FinOps A2A      : http://127.0.0.1:${A2A_PORT}/a2a"
echo "  Polyglot Middleware    : http://127.0.0.1:${MIDDLEWARE_PORT}"
echo "  Middleware MCP Gateway : http://127.0.0.1:${MIDDLEWARE_PORT}/mcp"
echo ""
echo "Next Steps (open a second terminal in this repo):"
echo "  1. Ingest Option 1 local plugin + local-skills/:"
echo "     make demo-sync-local AGENT_REGISTRY_BASE_URL=http://127.0.0.1:${MOCK_PORT}/v1 GCP_LOCATION=${GCP_LOCATION}"
echo "  2. Inspect live verified catalog:"
echo "     curl -s http://127.0.0.1:${MIDDLEWARE_PORT}/v1/catalog | python3 -m json.tool"
echo "  3. Hydrate all 4 IDEs into a safe demo HOME:"
echo "     make demo-hub-sync HUB_MIDDLEWARE_URL=http://127.0.0.1:${MIDDLEWARE_PORT}"
echo ""
echo "Press Ctrl-C to stop all 4 services cleanly."

# Wait until any child exits or user interrupts
wait
