#!/usr/bin/env bash
# Workshop environment preflight check (Zero-Docker & Zero-Terraform friendly).
#
# Checks Python 3.12+, local .venv & dependencies, local port readiness
# (8080/8090/8091/8092), git, curl, jq (optional), gcloud CLI (+ PATH check,
# active auth, ADC, project, agent-registry CLI), docker (optional),
# terraform (optional), and agy (optional), then prints an Attendee Readiness Summary.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"

usage() {
  cat <<'EOF'
Usage: scripts/preflight_check.sh [OPTIONS]

Verify local and Google Cloud prerequisites for the Enterprise Agent Marketplace
Workshop. Works cleanly with zero Docker and zero Terraform installed.

Options:
  -h, --help    Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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

REQUIRED_FAILURES=0
PYTHON_OK=false
VENV_OK=false
PORTS_OK=false
GCLOUD_BIN=""
GCLOUD_ON_PATH=false
GCLOUD_AUTH_OK=false
GCLOUD_ADC_OK=false
GCLOUD_AR_CLI_OK=false
GCLOUD_ACCOUNT=""
GCLOUD_PROJECT=""
DOCKER_OK=false
TERRAFORM_OK=false
AGY_OK=false

print_ok()   { printf "  [OK]   %-18s %s\n" "$1" "$2"; }
print_warn() { printf "  [WARN] %-18s %s\n" "$1" "$2"; }
print_info() { printf "  [INFO] %-18s %s\n" "$1" "$2"; }
print_fail() { printf "  [FAIL] %-18s %s\n" "$1" "$2"; }

echo "========================================================================"
echo "Enterprise Agent Marketplace Workshop — Preflight Check"
echo "========================================================================"
echo "Repository : ${REPO_ROOT}"
echo ""
echo "1. Core Local Tools & Port Readiness"

# Check python3 >= 3.12
if command -v python3 >/dev/null 2>&1; then
  PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
  if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    print_ok "python3" "${PY_VER} (>= 3.12)"
    PYTHON_OK=true
  else
    print_fail "python3" "${PY_VER} found, but Python 3.12+ is required"
    REQUIRED_FAILURES=$((REQUIRED_FAILURES + 1))
  fi
else
  print_fail "python3" "Not found on PATH (Python 3.12+ is required)"
  REQUIRED_FAILURES=$((REQUIRED_FAILURES + 1))
fi

# Check .venv readiness and installed Python dependencies
if [[ -x "${VENV_PYTHON}" ]]; then
  VENV_VER="$("${VENV_PYTHON}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
  if "${VENV_PYTHON}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    MISSING_PKGS="$("${VENV_PYTHON}" - <<'PYEOF'
import importlib
required = ("fastapi", "uvicorn", "httpx", "yaml", "cryptography", "click", "jsonschema", "pytest")
missing = []
for mod in required:
    try:
        importlib.import_module(mod)
    except Exception:
        missing.append(mod)
print(",".join(missing))
PYEOF
)"
    if [[ -z "${MISSING_PKGS}" ]]; then
      print_ok ".venv" "Python ${VENV_VER} with all workshop dependencies installed"
      VENV_OK=true
    else
      print_warn ".venv" "Python ${VENV_VER} missing packages: ${MISSING_PKGS} (run: .venv/bin/pip install -e .)"
    fi
  else
    print_warn ".venv" "Python ${VENV_VER} is older than 3.12 (recreate with python3 -m venv .venv)"
  fi
else
  print_warn ".venv" "Not created yet (run: python3 -m venv .venv && .venv/bin/pip install -e .)"
fi

# Check git
if command -v git >/dev/null 2>&1; then
  print_ok "git" "$(git --version)"
else
  print_fail "git" "Not found on PATH (required for Option 2 Git ingestion & labs)"
  REQUIRED_FAILURES=$((REQUIRED_FAILURES + 1))
fi

# Check curl
if command -v curl >/dev/null 2>&1; then
  CURL_VER="$(curl --version | head -n 1)"
  print_ok "curl" "${CURL_VER}"
else
  print_fail "curl" "Not found on PATH (required for REST & MCP verification)"
  REQUIRED_FAILURES=$((REQUIRED_FAILURES + 1))
fi

# Check jq (optional)
if command -v jq >/dev/null 2>&1; then
  print_ok "jq" "$(jq --version) (optional)"
else
  print_info "jq" "Not installed (optional — scripts use Python for JSON parsing)"
fi

# Check local workshop port readiness (8080, 8090, 8091, 8092)
PROBE_PY=""
if [[ -x "${VENV_PYTHON}" ]]; then
  PROBE_PY="${VENV_PYTHON}"
elif command -v python3 >/dev/null 2>&1; then
  PROBE_PY="$(command -v python3)"
fi

if [[ -n "${PROBE_PY}" ]]; then
  BUSY_PORTS="$("${PROBE_PY}" - <<'PYEOF'
import socket
busy = []
for port in (8080, 8090, 8091, 8092):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            busy.append(str(port))
print(",".join(busy))
PYEOF
)"
  if [[ -z "${BUSY_PORTS}" ]]; then
    print_ok "local ports" "127.0.0.1 ports 8080, 8090, 8091, 8092 are available"
    PORTS_OK=true
  else
    print_warn "local ports" "Port(s) in use: ${BUSY_PORTS} (stop existing stack, pass --port-offset N, or run 'make smoke-local' on isolated 18080-18092)"
  fi
fi

echo ""
echo "2. Google Cloud CLI (Optional for Track 1 Local, Required for Tracks 2 & 3 Live GCP)"

if command -v gcloud >/dev/null 2>&1; then
  GCLOUD_BIN="$(command -v gcloud)"
  GCLOUD_ON_PATH=true
elif [[ -x "${HOME}/.local/bin/gcloud" ]]; then
  GCLOUD_BIN="${HOME}/.local/bin/gcloud"
  GCLOUD_ON_PATH=false
fi

if [[ -n "${GCLOUD_BIN}" ]]; then
  GCLOUD_VER="$("${GCLOUD_BIN}" version --quiet 2>/dev/null | head -n 1 || echo "installed")"
  print_ok "gcloud" "${GCLOUD_VER} (${GCLOUD_BIN})"

  if [[ "${GCLOUD_ON_PATH}" == "false" ]]; then
    print_warn "gcloud PATH" "Found only at ${GCLOUD_BIN} (not on \$PATH) — run: export PATH=\"\$HOME/.local/bin:\$PATH\" before running bare 'gcloud' commands"
  fi

  GCLOUD_ACCOUNT="$("${GCLOUD_BIN}" auth list --filter="status:ACTIVE" --format="value(account)" --limit=50 --quiet 2>/dev/null | head -n 1 || true)"
  if [[ -n "${GCLOUD_ACCOUNT}" ]]; then
    print_ok "gcloud auth" "Active account: ${GCLOUD_ACCOUNT}"
    GCLOUD_AUTH_OK=true
  else
    print_warn "gcloud auth" "No active account (run: gcloud auth login --quiet && gcloud auth application-default login --quiet)"
  fi

  ADC_FILE="${GOOGLE_APPLICATION_CREDENTIALS:-${CLOUDSDK_CONFIG:-${HOME}/.config/gcloud}/application_default_credentials.json}"
  if [[ -f "${ADC_FILE}" ]]; then
    if "${GCLOUD_BIN}" auth application-default print-access-token --quiet >/dev/null 2>&1; then
      print_ok "gcloud ADC" "Application Default Credentials valid (${ADC_FILE})"
      GCLOUD_ADC_OK=true
    else
      print_warn "gcloud ADC" "ADC file found (${ADC_FILE}) but token refresh failed (run: gcloud auth application-default login --quiet)"
    fi
  else
    print_warn "gcloud ADC" "Not configured (needed for Track 3 Terraform / ADK; run: gcloud auth application-default login --quiet)"
  fi

  if "${GCLOUD_BIN}" agent-registry --help --quiet >/dev/null 2>&1; then
    print_ok "agent-registry" "'gcloud agent-registry' command group available"
    GCLOUD_AR_CLI_OK=true
  else
    print_warn "agent-registry" "'gcloud agent-registry' not found — run 'gcloud components update --quiet' for Agent Registry v1 CLI commands"
  fi

  GCLOUD_PROJECT="${GCP_PROJECT_ID:-$("${GCLOUD_BIN}" config get-value project --quiet 2>/dev/null || true)}"
  if [[ -n "${GCLOUD_PROJECT}" && "${GCLOUD_PROJECT}" != "(unset)" ]]; then
    print_ok "gcloud project" "${GCLOUD_PROJECT} (confirm this is a disposable workshop sandbox before deploying)"
  else
    GCLOUD_PROJECT=""
    print_warn "gcloud project" "Not set (export GCP_PROJECT_ID=<your-sandbox-project> or run: gcloud config set project <id> --quiet)"
  fi
else
  print_info "gcloud" "Not found on PATH or \$HOME/.local/bin/gcloud (optional for Track 1 offline local mode)"
fi

echo ""
echo "3. Optional Tools (Zero-Docker & Zero-Terraform Fully Supported)"

if command -v docker >/dev/null 2>&1; then
  DOCKER_VER="$(docker --version 2>/dev/null || echo "installed")"
  print_ok "docker" "${DOCKER_VER} (optional — Cloud Build zero-Docker paths also available)"
  DOCKER_OK=true
else
  print_info "docker" "Not installed — ZERO-DOCKER paths fully supported!"
fi

if command -v terraform >/dev/null 2>&1; then
  TF_VER="$(terraform version 2>/dev/null | head -n 1 || echo "installed")"
  print_ok "terraform" "${TF_VER} (optional — Track 3 IaC available)"
  TERRAFORM_OK=true
else
  print_info "terraform" "Not installed — Track 1 Local & Track 2 zero-Terraform gcloud deploy fully supported!"
fi

if command -v agy >/dev/null 2>&1; then
  print_ok "agy" "Installed at $(command -v agy) (optional Antigravity CLI)"
  AGY_OK=true
else
  print_info "agy" "Not installed (optional — tools.hub_sync supports --no-exec for offline IDE wiring)"
fi

echo ""
echo "========================================================================"
echo "Attendee Readiness Summary"
echo "========================================================================"

if [[ "${REQUIRED_FAILURES}" -gt 0 ]]; then
  echo "Status: MISSING CORE PREREQUISITES (${REQUIRED_FAILURES} failure(s) above)."
  echo "Install Python 3.12+, git, and curl before proceeding."
  exit 1
fi

if [[ "${VENV_OK}" == "false" ]]; then
  echo "Next Step (1-time setup required before running workshop targets):"
  echo "  python3 -m venv .venv && .venv/bin/pip install -e ."
  echo ""
fi

if [[ -n "${GCLOUD_BIN}" && "${GCLOUD_ON_PATH}" == "false" ]]; then
  echo "Shell PATH Tip (so bare 'gcloud' commands in Labs 00-08 work directly):"
  echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo ""
fi

echo "Recommended Workshop Tracks (Zero Local Docker Required):"
if [[ "${VENV_OK}" == "true" ]]; then
  echo "  Track 1 — Offline Local Mode (READY):"
else
  echo "  Track 1 — Offline Local Mode (NEEDS .venv — run 1-time setup above first):"
fi
echo "     make smoke-local         # Automated end-to-end validation on isolated ports"
echo "     make run-local-stack     # Interactive 4-service stack (:8090/:8091/:8092/:8080)"
echo "     make starter-check       # Validate starter exercises for Labs 01-04"
echo ""

if [[ -n "${GCLOUD_BIN}" && "${GCLOUD_AUTH_OK}" == "true" && "${VENV_OK}" == "true" ]]; then
  PROJ_HINT="${GCLOUD_PROJECT:-<your-sandbox-project-id>}"
  echo "  Track 2 — Live GCP Zero-Docker / Zero-Terraform (READY):"
  echo "     [SAFETY] Confirm GCP_PROJECT_ID (${PROJ_HINT}) is a disposable workshop sandbox before deploying!"
  echo "     make gcp-deploy GCP_PROJECT_ID=${PROJ_HINT} GCP_LOCATION=asia-southeast3 [ENABLE_SECONDARY=true]"
  echo "     make gcp-validate GCP_PROJECT_ID=${PROJ_HINT} GCP_LOCATION=asia-southeast3 [ENABLE_SECONDARY=true]"
  echo "     make gcp-destroy GCP_PROJECT_ID=${PROJ_HINT} GCP_LOCATION=asia-southeast3 [ENABLE_SECONDARY=true]"
elif [[ -n "${GCLOUD_BIN}" && "${GCLOUD_AUTH_OK}" == "true" ]]; then
  echo "  Track 2 — Live GCP Zero-Docker / Zero-Terraform (NEEDS .venv):"
  echo "     Create .venv first, confirm your sandbox project, then run: make gcp-deploy GCP_PROJECT_ID=<your-sandbox-project-id>"
else
  echo "  Track 2 — Live GCP Zero-Docker / Zero-Terraform (NEEDS GCLOUD AUTH):"
  echo "     Install gcloud CLI, run 'gcloud auth login --quiet' + 'gcloud auth application-default login --quiet',"
  echo "     then run: make gcp-deploy GCP_PROJECT_ID=<your-sandbox-project-id>"
fi
echo ""

if [[ "${TERRAFORM_OK}" == "true" && -n "${GCLOUD_BIN}" && "${GCLOUD_AUTH_OK}" == "true" ]]; then
  echo "  Track 3 — Live GCP Full Terraform IaC (READY):"
  echo "     make validate-hcl"
  echo "     make tf-core-plan"
  echo "     make tf-sec-plan"
else
  echo "  Track 3 — Live GCP Full Terraform IaC (OPTIONAL — use Track 2 'make gcp-deploy' if skipping Terraform):"
  echo "     Install Terraform >= 1.6.0 to run 'make validate-hcl', 'make tf-core-plan', and 'make tf-sec-plan'."
fi
echo ""
echo "Summary flags: python=${PYTHON_OK} venv=${VENV_OK} ports_free=${PORTS_OK} gcloud_on_path=${GCLOUD_ON_PATH} gcloud_auth=${GCLOUD_AUTH_OK} gcloud_adc=${GCLOUD_ADC_OK} agent_registry_cli=${GCLOUD_AR_CLI_OK} docker=${DOCKER_OK} terraform=${TERRAFORM_OK} agy=${AGY_OK}"
