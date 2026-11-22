#!/usr/bin/env bash
# Zero-Docker, Zero-Terraform live Google Cloud deployer for the Enterprise Agent
# Marketplace Workshop (Bangkok asia-southeast3 primary + optional Singapore
# asia-southeast1 secondary failover).
#
# Every gcloud command uses --quiet and explicit --project / --region / --location.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"

PROJECT_ID="${GCP_PROJECT_ID:-}"
PRIMARY_REGION="${GCP_LOCATION:-asia-southeast3}"
SECONDARY_REGION="${GCP_SECONDARY_LOCATION:-asia-southeast1}"
SKILLS_LOCATION="${AGENT_REGISTRY_SKILLS_LOCATION:-global}"
ENABLE_SECONDARY="false"
REPO="${ARTIFACT_REGISTRY_REPO:-agent-marketplace}"
TAG="${IMAGE_TAG:-1.0.0}"
DEPLOY_MODE="cloudbuild"
SKILLS_SOURCE="local"
SKILLS_REPO_URL="${SKILLS_REPO_URL:-https://github.com/jiratouchmhp/agent-registry-marketplace-workshop.git}"
SKILLS_REPO_REF="${SKILLS_REPO_REF:-main}"
SKILLS_REPO_SUBDIR="${SKILLS_REPO_SUBDIR:-local-skills}"
SKILLS_REPO_TOKEN_ENV="${SKILLS_REPO_TOKEN_ENV:-}"
ALLOW_UNAUTH_DEMO="false"
SKIP_BUILD="false"
SKIP_API_ENABLE="false"
NAME_PREFIX=""
STATE_FILE=""
DRY_RUN="false"

usage() {
  cat <<'EOF'
Usage: scripts/deploy_gcp.sh [OPTIONS]

Deploy the Enterprise Agent Marketplace stack to Google Cloud with zero local
Docker and zero Terraform required.

Options:
  --project ID                  Google Cloud project ID (required, or $GCP_PROJECT_ID)
  --primary-region REGION       Primary GCP region (default: asia-southeast3)
  --secondary-region REGION     Secondary GCP region (default: asia-southeast1)
  --skills-location LOC         Agent Registry Skills location: global|us|eu (default: global)
  --enable-secondary            Also deploy full secondary stack in --secondary-region
  --repo REPO                   Artifact Registry repo ID (default: agent-marketplace)
  --tag TAG                     Container image tag (default: 1.0.0)
  --deploy-mode MODE            cloudbuild|source (default: cloudbuild)
  --skills-source MODE          local|git (default: local)
  --skills-repo-url URL         Git repo URL when --skills-source git
  --skills-repo-ref REF         Git branch/tag/SHA when --skills-source git (default: main)
  --skills-repo-subdir PATH     Subdir inside Git repo (default: local-skills)
  --skills-repo-token-env VAR   Env var holding private Git token (optional)
  --allow-unauthenticated-demo  Allow unauthenticated Cloud Run invocations (default: false)
  --skip-build                  Skip Cloud Build image build step
  --skip-api-enable             Skip gcloud services enable step
  --name-prefix PREFIX          Optional name prefix for isolated test deployments
  --state-file PATH             Manifest output path (default: /tmp/workshop-gcp-deploy-<project>.json)
  --dry-run                     Print planned commands without mutating GCP resources
  -h, --help                    Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT_ID="$2"
      shift 2
      ;;
    --primary-region)
      PRIMARY_REGION="$2"
      shift 2
      ;;
    --secondary-region)
      SECONDARY_REGION="$2"
      shift 2
      ;;
    --skills-location)
      SKILLS_LOCATION="$2"
      shift 2
      ;;
    --enable-secondary)
      ENABLE_SECONDARY="true"
      shift
      ;;
    --repo)
      REPO="$2"
      shift 2
      ;;
    --tag)
      TAG="$2"
      shift 2
      ;;
    --deploy-mode)
      DEPLOY_MODE="$2"
      shift 2
      ;;
    --skills-source)
      SKILLS_SOURCE="$2"
      shift 2
      ;;
    --skills-repo-url)
      SKILLS_REPO_URL="$2"
      shift 2
      ;;
    --skills-repo-ref)
      SKILLS_REPO_REF="$2"
      shift 2
      ;;
    --skills-repo-subdir)
      SKILLS_REPO_SUBDIR="$2"
      shift 2
      ;;
    --skills-repo-token-env)
      SKILLS_REPO_TOKEN_ENV="$2"
      shift 2
      ;;
    --allow-unauthenticated-demo)
      ALLOW_UNAUTH_DEMO="true"
      shift
      ;;
    --skip-build)
      SKIP_BUILD="true"
      shift
      ;;
    --skip-api-enable)
      SKIP_API_ENABLE="true"
      shift
      ;;
    --name-prefix)
      NAME_PREFIX="$2"
      shift 2
      ;;
    --state-file)
      STATE_FILE="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
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

if [[ "${DEPLOY_MODE}" != "cloudbuild" && "${DEPLOY_MODE}" != "source" ]]; then
  echo "ERROR: --deploy-mode must be 'cloudbuild' or 'source' (got '${DEPLOY_MODE}')." >&2
  exit 2
fi
if [[ "${SKILLS_SOURCE}" != "local" && "${SKILLS_SOURCE}" != "git" ]]; then
  echo "ERROR: --skills-source must be 'local' or 'git' (got '${SKILLS_SOURCE}')." >&2
  exit 2
fi
if [[ "${SKILLS_LOCATION}" != "global" && "${SKILLS_LOCATION}" != "us" && "${SKILLS_LOCATION}" != "eu" ]]; then
  echo "ERROR: --skills-location must be 'global', 'us', or 'eu' for live Agent Registry v1 (got '${SKILLS_LOCATION}')." >&2
  exit 2
fi

resolve_gcloud() {
  if command -v gcloud >/dev/null 2>&1; then
    command -v gcloud
  elif [[ -x "${HOME}/.local/bin/gcloud" ]]; then
    echo "${HOME}/.local/bin/gcloud"
  else
    echo ""
  fi
}

GCLOUD="$(resolve_gcloud)"
if [[ -z "${GCLOUD}" ]]; then
  if [[ "${DRY_RUN}" == "true" ]]; then
    GCLOUD="gcloud"
  else
    echo "ERROR: gcloud CLI not found on PATH or at \$HOME/.local/bin/gcloud." >&2
    exit 1
  fi
fi

if [[ -z "${PROJECT_ID}" && "${DRY_RUN}" == "false" ]]; then
  PROJECT_ID="$("${GCLOUD}" config get-value project --quiet 2>/dev/null || true)"
  [[ "${PROJECT_ID}" == "(unset)" ]] && PROJECT_ID=""
fi

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: --project PROJECT_ID (or \$GCP_PROJECT_ID) is required." >&2
  exit 2
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: Python virtualenv not found at '${PYTHON_BIN}'." >&2
  echo "Run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

if [[ -z "${STATE_FILE}" ]]; then
  STATE_FILE="/tmp/workshop-gcp-deploy-${PROJECT_ID}.json"
fi

# Normalize optional prefix with trailing hyphen
PREFIX_DASH=""
if [[ -n "${NAME_PREFIX}" ]]; then
  PREFIX_DASH="${NAME_PREFIX%-}-"
fi

MCP_RUN_SVC="${PREFIX_DASH}sample-mcp-server"
A2A_RUN_SVC="${PREFIX_DASH}sample-a2a-agent"
MW_RUN_SVC="${PREFIX_DASH}polyglot-middleware"

AR_MCP_SVC_ID="${PREFIX_DASH}finops-mcp-server"
AR_A2A_SVC_ID="${PREFIX_DASH}finops-advisor-agent"
AR_ENDPOINT_SVC_ID="${PREFIX_DASH}finops-billing-api"

MW_SA_ID="agent-marketplace-middleware"
WORKLOADS_SA_ID="finops-sample-workloads"
MW_SA_EMAIL="${MW_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
WORKLOADS_SA_EMAIL="${WORKLOADS_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"

AUTH_FLAG="--no-allow-unauthenticated"
if [[ "${ALLOW_UNAUTH_DEMO}" == "true" ]]; then
  AUTH_FLAG="--allow-unauthenticated"
fi

TARGET_REGIONS=("${PRIMARY_REGION}")
if [[ "${ENABLE_SECONDARY}" == "true" && -n "${SECONDARY_REGION}" && "${SECONDARY_REGION}" != "${PRIMARY_REGION}" ]]; then
  TARGET_REGIONS+=("${SECONDARY_REGION}")
fi

run_cmd() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    printf '[DRY-RUN]'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

cd "${REPO_ROOT}"

echo "========================================================================"
echo "Enterprise Agent Marketplace — Live GCP Deploy (Zero-Docker / Zero-TF)"
echo "========================================================================"
echo "Project           : ${PROJECT_ID}"
echo "Regions           : ${TARGET_REGIONS[*]} (secondary_enabled=${ENABLE_SECONDARY})"
echo "Skills Location   : ${SKILLS_LOCATION}"
echo "Deploy Mode       : ${DEPLOY_MODE}"
echo "Skills Source     : ${SKILLS_SOURCE}"
echo "Cloud Run Auth    : ${AUTH_FLAG}"
echo "Service Names     : ${MCP_RUN_SVC}, ${A2A_RUN_SVC}, ${MW_RUN_SVC}"
echo "State Manifest    : ${STATE_FILE}"
echo "Dry Run           : ${DRY_RUN}"
echo ""

# ----------------------------------------------------------------------------
# Step 0: Enable Required APIs
# ----------------------------------------------------------------------------
if [[ "${SKIP_API_ENABLE}" == "false" ]]; then
  echo "[Step 0/7] Enabling required Google Cloud APIs..."
  run_cmd "${GCLOUD}" services enable \
    agentregistry.googleapis.com \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    storage.googleapis.com \
    iam.googleapis.com \
    --project="${PROJECT_ID}" \
    --quiet
else
  echo "[Step 0/7] Skipping API enablement (--skip-api-enable)."
fi

# ----------------------------------------------------------------------------
# Step 1: Least-Privilege Service Accounts & IAM Binding
# ----------------------------------------------------------------------------
echo ""
echo "[Step 1/7] Creating least-privilege Service Accounts & Agent Registry viewer IAM..."

ensure_sa() {
  local sa_id="$1"
  local sa_email="$2"
  local display_name="$3"
  if [[ "${DRY_RUN}" == "true" ]]; then
    run_cmd "${GCLOUD}" iam service-accounts create "${sa_id}" \
      --project="${PROJECT_ID}" \
      --display-name="${display_name}" \
      --quiet
    return 0
  fi
  if "${GCLOUD}" iam service-accounts describe "${sa_email}" \
    --project="${PROJECT_ID}" --quiet >/dev/null 2>&1; then
    echo "  [OK] Service account already exists: ${sa_email}"
  else
    "${GCLOUD}" iam service-accounts create "${sa_id}" \
      --project="${PROJECT_ID}" \
      --display-name="${display_name}" \
      --quiet
    echo "  [OK] Created service account: ${sa_email}"
  fi
}

ensure_sa "${MW_SA_ID}" "${MW_SA_EMAIL}" "Polyglot Agent Marketplace Middleware"
ensure_sa "${WORKLOADS_SA_ID}" "${WORKLOADS_SA_EMAIL}" "FinOps Sample MCP and A2A Workloads"

# Bind roles/agentregistry.viewer ONLY to agent-marketplace-middleware
run_cmd "${GCLOUD}" projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${MW_SA_EMAIL}" \
  --role="roles/agentregistry.viewer" \
  --condition=None \
  --quiet >/dev/null

echo "  [OK] Bound roles/agentregistry.viewer ONLY to ${MW_SA_EMAIL}"

# ----------------------------------------------------------------------------
# Step 2: Build Container Images via Zero-Docker Cloud Build
# ----------------------------------------------------------------------------
echo ""
echo "[Step 2/7] Building container images (mode=${DEPLOY_MODE}, skip_build=${SKIP_BUILD})..."

if [[ "${SKIP_BUILD}" == "false" ]]; then
  BUILD_ARGS=(
    --project "${PROJECT_ID}"
    --region "${PRIMARY_REGION}"
    --repo "${REPO}"
    --tag "${TAG}"
  )
  if [[ "${ENABLE_SECONDARY}" == "true" && -n "${SECONDARY_REGION}" && "${SECONDARY_REGION}" != "${PRIMARY_REGION}" ]]; then
    BUILD_ARGS+=(--secondary-region "${SECONDARY_REGION}")
  fi
  if [[ "${DEPLOY_MODE}" == "source" ]]; then
    BUILD_ARGS+=(--only-middleware)
  fi
  if [[ "${DRY_RUN}" == "true" ]]; then
    BUILD_ARGS+=(--dry-run)
  fi
  bash "${SCRIPT_DIR}/build_images_cloudbuild.sh" "${BUILD_ARGS[@]}"
else
  echo "  [INFO] Skipping Cloud Build step (--skip-build)."
fi

# Resolve current active gcloud user for Cloud Run invoker grant
ACTIVE_ACCOUNT=""
if [[ "${DRY_RUN}" == "false" ]]; then
  ACTIVE_ACCOUNT="$("${GCLOUD}" auth list --filter="status:ACTIVE" --format="value(account)" --limit=50 --quiet 2>/dev/null | head -n 1 || true)"
else
  ACTIVE_ACCOUNT="attendee@example.com"
fi

# Determine IAM member prefix (serviceAccount vs user)
invoker_member_for() {
  local acct="$1"
  if [[ "${acct}" == *"gserviceaccount.com" ]]; then
    echo "serviceAccount:${acct}"
  else
    echo "user:${acct}"
  fi
}

# Temp directory to capture per-region deployment metadata for manifest assembly
WORK_TMP="$(mktemp -d /tmp/workshop-deploy-work.XXXXXX)"
trap 'rm -rf "${WORK_TMP}"' EXIT

deploy_and_register_region() {
  local region="$1"
  local peer_region="$2"
  local ar_prefix="${region}-docker.pkg.dev/${PROJECT_ID}/${REPO}"

  echo ""
  echo "========================================================================"
  echo "Deploying Region: ${region} (secondary reference: ${peer_region})"
  echo "========================================================================"

  # Step 3: Deploy Cloud Run Services
  echo "[Step 3/7] Deploying Cloud Run services in ${region}..."

  if [[ "${DEPLOY_MODE}" == "source" ]]; then
    run_cmd "${GCLOUD}" run deploy "${MCP_RUN_SVC}" \
      --project="${PROJECT_ID}" \
      --region="${region}" \
      --source="samples/mcp-server" \
      --service-account="${WORKLOADS_SA_EMAIL}" \
      --port=8091 \
      --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=${region}" \
      "${AUTH_FLAG}" \
      --quiet

    run_cmd "${GCLOUD}" run deploy "${A2A_RUN_SVC}" \
      --project="${PROJECT_ID}" \
      --region="${region}" \
      --source="samples/a2a-agent" \
      --service-account="${WORKLOADS_SA_EMAIL}" \
      --port=8092 \
      --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=${region}" \
      "${AUTH_FLAG}" \
      --quiet
  else
    run_cmd "${GCLOUD}" run deploy "${MCP_RUN_SVC}" \
      --project="${PROJECT_ID}" \
      --region="${region}" \
      --image="${ar_prefix}/sample-mcp-server:${TAG}" \
      --service-account="${WORKLOADS_SA_EMAIL}" \
      --port=8091 \
      --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=${region}" \
      "${AUTH_FLAG}" \
      --quiet

    run_cmd "${GCLOUD}" run deploy "${A2A_RUN_SVC}" \
      --project="${PROJECT_ID}" \
      --region="${region}" \
      --image="${ar_prefix}/sample-a2a-agent:${TAG}" \
      --service-account="${WORKLOADS_SA_EMAIL}" \
      --port=8092 \
      --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=${region}" \
      "${AUTH_FLAG}" \
      --quiet
  fi

  local mcp_url a2a_url mw_url
  if [[ "${DRY_RUN}" == "true" ]]; then
    mcp_url="https://${MCP_RUN_SVC}-dryrun.${region}.run.app"
    a2a_url="https://${A2A_RUN_SVC}-dryrun.${region}.run.app"
    mw_url="https://${MW_RUN_SVC}-dryrun.${region}.run.app"
  else
    mcp_url="$("${GCLOUD}" run services describe "${MCP_RUN_SVC}" --project="${PROJECT_ID}" --region="${region}" --format="value(status.url)" --quiet)"
    a2a_url="$("${GCLOUD}" run services describe "${A2A_RUN_SVC}" --project="${PROJECT_ID}" --region="${region}" --format="value(status.url)" --quiet)"
  fi

  # Update A2A_PUBLIC_URL on sample-a2a-agent to its real Cloud Run HTTPS URL
  run_cmd "${GCLOUD}" run services update "${A2A_RUN_SVC}" \
    --project="${PROJECT_ID}" \
    --region="${region}" \
    --update-env-vars="A2A_PUBLIC_URL=${a2a_url}" \
    --quiet

  # Deploy polyglot-middleware with least-privilege middleware SA
  run_cmd "${GCLOUD}" run deploy "${MW_RUN_SVC}" \
    --project="${PROJECT_ID}" \
    --region="${region}" \
    --image="${ar_prefix}/polyglot-middleware:${TAG}" \
    --service-account="${MW_SA_EMAIL}" \
    --port=8080 \
    --set-env-vars="AGENT_REGISTRY_BASE_URL=https://agentregistry.googleapis.com/v1,GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=${region},GCP_SECONDARY_LOCATION=${peer_region},AGENT_REGISTRY_SKILLS_LOCATION=${SKILLS_LOCATION},REQUIRE_KMS_SIGNATURE=false" \
    "${AUTH_FLAG}" \
    --quiet

  if [[ "${DRY_RUN}" == "false" ]]; then
    mw_url="$("${GCLOUD}" run services describe "${MW_RUN_SVC}" --project="${PROJECT_ID}" --region="${region}" --format="value(status.url)" --quiet)"
  fi

  # Update MIDDLEWARE_PUBLIC_URL to its real Cloud Run HTTPS URL
  run_cmd "${GCLOUD}" run services update "${MW_RUN_SVC}" \
    --project="${PROJECT_ID}" \
    --region="${region}" \
    --update-env-vars="MIDDLEWARE_PUBLIC_URL=${mw_url}" \
    --quiet

  echo "  [OK] Cloud Run URLs (${region}):"
  echo "       MCP        : ${mcp_url}"
  echo "       A2A        : ${a2a_url}"
  echo "       Middleware : ${mw_url}"

  # Step 4: Grant current authenticated user roles/run.invoker (unless unauthenticated demo)
  echo ""
  echo "[Step 4/7] Configuring Cloud Run invoker IAM in ${region}..."
  if [[ "${ALLOW_UNAUTH_DEMO}" == "false" && -n "${ACTIVE_ACCOUNT}" ]]; then
    local invoker_member
    invoker_member="$(invoker_member_for "${ACTIVE_ACCOUNT}")"
    for svc in "${MCP_RUN_SVC}" "${A2A_RUN_SVC}" "${MW_RUN_SVC}"; do
      run_cmd "${GCLOUD}" run services add-iam-policy-binding "${svc}" \
        --project="${PROJECT_ID}" \
        --region="${region}" \
        --member="${invoker_member}" \
        --role="roles/run.invoker" \
        --quiet >/dev/null
    done
    # Also allow middleware SA to invoke sample services
    for svc in "${MCP_RUN_SVC}" "${A2A_RUN_SVC}"; do
      run_cmd "${GCLOUD}" run services add-iam-policy-binding "${svc}" \
        --project="${PROJECT_ID}" \
        --region="${region}" \
        --member="serviceAccount:${MW_SA_EMAIL}" \
        --role="roles/run.invoker" \
        --quiet >/dev/null
    done
    echo "  [OK] Granted roles/run.invoker to ${invoker_member} on all 3 services in ${region}"
  else
    echo "  [INFO] Unauthenticated demo mode enabled (skipping per-user invoker binding)."
  fi

  # Step 5: Register Agent Registry v1 Services pointing to real live Cloud Run URLs
  echo ""
  echo "[Step 5/7] Registering Agent Registry v1 Services in ${region} via REST v1..."
  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[DRY-RUN] Register Agent Registry v1 Services in projects/${PROJECT_ID}/locations/${region}:"
    echo "  - ${AR_MCP_SVC_ID} -> ${mcp_url}/mcp"
    echo "  - ${AR_A2A_SVC_ID} -> ${a2a_url}/a2a (A2A_AGENT_CARD, no top-level interfaces)"
    echo "  - ${AR_ENDPOINT_SVC_ID} -> https://cloudbilling.googleapis.com/v1"
  else
    local access_token
    access_token="$("${GCLOUD}" auth print-access-token --quiet)"
    "${PYTHON_BIN}" - "${PROJECT_ID}" "${region}" "${access_token}" "${mcp_url}" "${a2a_url}" "${AR_MCP_SVC_ID}" "${AR_A2A_SVC_ID}" "${AR_ENDPOINT_SVC_ID}" <<'PYEOF'
import json
import sys
import time
import httpx

project_id, location, token, mcp_url, a2a_url, mcp_svc_id, a2a_svc_id, ep_svc_id = sys.argv[1:9]
base = "https://agentregistry.googleapis.com/v1"
parent = f"projects/{project_id}/locations/{location}"
headers = {
    "Authorization": f"Bearer {token}",
    "x-goog-user-project": project_id,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

mcp_tool_spec = {
    "tools": [
        {
            "name": "get_billing_summary",
            "description": "Return read-only Google Cloud Billing spend summary, baseline comparison, and top SKU deltas for a GCP project.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Target GCP project ID."},
                    "days": {"type": "integer", "description": "Lookback window in days (1-90).", "default": 7},
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
            "description": "Inspect a BigQuery Standard SQL query for full-table scans, SELECT *, and missing partition filters.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "BigQuery Standard SQL query text to analyze."},
                    "project_id": {"type": "string", "description": "Optional GCP project ID."},
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
            "description": "Permanently delete an idle BigQuery dataset to reclaim storage spend.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "GCP project ID containing the dataset."},
                    "dataset_id": {"type": "string", "description": "BigQuery dataset ID to delete."},
                    "confirm_delete": {"type": "boolean", "default": False},
                    "reason": {"type": "string"},
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
}

a2a_card = {
    "name": "GCP FinOps Advisor",
    "description": "Enterprise A2A v1.0 FinOps specialist that analyzes GCP Cloud Billing anomalies, BigQuery query efficiency, and safe cost-remediation plans.",
    "version": "1.0.0",
    "provider": {"organization": "Acme Platform Security", "url": "https://acme.example.com/platform"},
    "supportedInterfaces": [
        {"url": f"{a2a_url.rstrip('/')}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        {"url": f"{a2a_url.rstrip('/')}/a2a/http", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
    ],
    "capabilities": {"streaming": False, "pushNotifications": False},
    "defaultInputModes": ["text/plain", "application/json"],
    "defaultOutputModes": ["text/plain", "application/json"],
    "skills": [
        {
            "id": "cost-anomaly-triage",
            "name": "Cost Anomaly Triage",
            "description": "Diagnose unexpected GCP Cloud Billing spend spikes by service and SKU.",
            "tags": ["finops", "billing", "anomaly", "gcp"],
        },
        {
            "id": "bq-query-optimization",
            "name": "BigQuery Query Optimization",
            "description": "Inspect BigQuery Standard SQL queries for full table scans and missing partition filters.",
            "tags": ["finops", "bigquery", "sql", "cost-optimization"],
        },
    ],
}

services = [
    (
        mcp_svc_id,
        {
            "displayName": "FinOps Cost & BigQuery MCP Server",
            "description": "Governed FinOps MCP server exposing read-only billing/BigQuery tools and a destructive dataset deletion tool.",
            "interfaces": [{"url": f"{mcp_url.rstrip('/')}/mcp", "protocolBinding": "JSONRPC"}],
            "mcpServerSpec": {"type": "TOOL_SPEC", "content": mcp_tool_spec},
        },
        "displayName,description,interfaces,mcpServerSpec.content",
    ),
    (
        a2a_svc_id,
        {
            "displayName": "GCP FinOps Advisor",
            "description": "Enterprise A2A v1.0 FinOps specialist advisor.",
            "agentSpec": {"type": "A2A_AGENT_CARD", "content": a2a_card},
        },
        "displayName,description,agentSpec.content",
    ),
    (
        ep_svc_id,
        {
            "displayName": "FinOps Cloud Billing API Endpoint",
            "description": "External Cloud Billing REST API endpoint registered for governed agent egress policies.",
            "interfaces": [{"url": "https://cloudbilling.googleapis.com/v1", "protocolBinding": "HTTP_JSON"}],
            "endpointSpec": {"type": "NO_SPEC"},
        },
        "displayName,description,interfaces",
    ),
]


def poll_lro(client: httpx.Client, body: dict) -> dict:
    if not isinstance(body, dict):
        return {}
    if "done" not in body and "/operations/" not in str(body.get("name", "")):
        return body
    op = body
    deadline = time.monotonic() + 120.0
    while not op.get("done", False):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for LRO: {op.get('name')}")
        time.sleep(1.5)
        r = client.get(f"{base}/{op['name'].lstrip('/')}", headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"LRO poll failed ({r.status_code}): {r.text}")
        op = r.json()
    if op.get("error"):
        raise RuntimeError(f"LRO failed: {op['error']}")
    return op.get("response") or op


with httpx.Client(timeout=60.0) as client:
    for svc_id, payload, update_mask in services:
        svc_path = f"{parent}/services/{svc_id}"
        get_r = client.get(f"{base}/{svc_path}", headers=headers)
        if get_r.status_code == 404:
            post_r = client.post(
                f"{base}/{parent}/services",
                params={"serviceId": svc_id},
                json=payload,
                headers=headers,
            )
            if post_r.status_code >= 400:
                raise RuntimeError(f"Create service '{svc_id}' failed ({post_r.status_code}): {post_r.text}")
            poll_lro(client, post_r.json())
            print(f"  [OK] Created Agent Registry Service: {svc_path}")
        elif get_r.status_code == 200:
            patch_r = client.patch(
                f"{base}/{svc_path}",
                params={"updateMask": update_mask},
                json=payload,
                headers=headers,
            )
            if patch_r.status_code >= 400:
                raise RuntimeError(f"Update service '{svc_id}' failed ({patch_r.status_code}): {patch_r.text}")
            poll_lro(client, patch_r.json())
            print(f"  [OK] Updated Agent Registry Service: {svc_path}")
        else:
            raise RuntimeError(f"Get service '{svc_id}' failed ({get_r.status_code}): {get_r.text}")
PYEOF
  fi

  # Write per-region metadata fragment
  "${PYTHON_BIN}" - "${WORK_TMP}/region_${region}.json" "${region}" \
    "${MCP_RUN_SVC}" "${mcp_url}" "${A2A_RUN_SVC}" "${a2a_url}" "${MW_RUN_SVC}" "${mw_url}" \
    "${AR_MCP_SVC_ID}" "${AR_A2A_SVC_ID}" "${AR_ENDPOINT_SVC_ID}" \
    "${REPO}" <<'PYEOF'
import json, sys
from pathlib import Path

out_file = Path(sys.argv[1])
region = sys.argv[2]
mcp_name, mcp_url, a2a_name, a2a_url, mw_name, mw_url = sys.argv[3:9]
ar_mcp, ar_a2a, ar_ep, repo = sys.argv[9:13]

data = {
    "region": region,
    "artifact_registry_repo": repo,
    "cloud_run": {
        "sample_mcp_server": {"name": mcp_name, "url": mcp_url},
        "sample_a2a_agent": {"name": a2a_name, "url": a2a_url},
        "polyglot_middleware": {"name": mw_name, "url": mw_url},
    },
    "agent_registry_services": [ar_mcp, ar_a2a, ar_ep],
}
out_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF
}

for reg in "${TARGET_REGIONS[@]}"; do
  peer="${SECONDARY_REGION}"
  if [[ "${reg}" == "${SECONDARY_REGION}" ]]; then
    peer="${PRIMARY_REGION}"
  fi
  deploy_and_register_region "${reg}" "${peer}"
done

# ----------------------------------------------------------------------------
# Step 6: Ingest Standalone Skills ONCE into multi-region location (global/us/eu)
# ----------------------------------------------------------------------------
echo ""
echo "[Step 6/7] Ingesting standalone Skills ONCE in ${SKILLS_LOCATION} (source=${SKILLS_SOURCE})..."

INGEST_OUT_1="${WORK_TMP}/ingest_skills_1.json"
INGEST_OUT_2="${WORK_TMP}/ingest_skills_2.json"

COMMON_INGEST_ARGS=(
  --only-skills
  --skip-mcp-probe
  --wait-for-scan
  --registry-url "https://agentregistry.googleapis.com/v1"
  --project "${PROJECT_ID}"
  --location "${SKILLS_LOCATION}"
  --skills-location "${SKILLS_LOCATION}"
  --target-state TARGET_STATE_ACTIVE
  --scanning-policy ENABLED_BLOCKING
)

if [[ "${SKILLS_SOURCE}" == "local" ]]; then
  if [[ "${DRY_RUN}" == "true" ]]; then
    run_cmd "${PYTHON_BIN}" -m tools.plugin_sync ingest \
      --source-type local --path "starter-marketplace/plugins/gcp-finops-toolkit" \
      "${COMMON_INGEST_ARGS[@]}" --dry-run
    run_cmd "${PYTHON_BIN}" -m tools.plugin_sync ingest \
      --source-type local --path "local-skills" --plugin-name "local-ops-toolkit" \
      "${COMMON_INGEST_ARGS[@]}" --dry-run
  else
    SKILLS_ACCESS_TOKEN="$("${GCLOUD}" auth print-access-token --quiet)"
    AGENT_REGISTRY_AUTH_TOKEN="${SKILLS_ACCESS_TOKEN}" "${PYTHON_BIN}" -m tools.plugin_sync ingest \
      --source-type local --path "starter-marketplace/plugins/gcp-finops-toolkit" \
      "${COMMON_INGEST_ARGS[@]}" >"${INGEST_OUT_1}"
    echo "  [OK] Ingested gcp-finops-toolkit skills in ${SKILLS_LOCATION}"

    AGENT_REGISTRY_AUTH_TOKEN="${SKILLS_ACCESS_TOKEN}" "${PYTHON_BIN}" -m tools.plugin_sync ingest \
      --source-type local --path "local-skills" --plugin-name "local-ops-toolkit" \
      "${COMMON_INGEST_ARGS[@]}" >"${INGEST_OUT_2}"
    echo "  [OK] Ingested local-ops-toolkit skills in ${SKILLS_LOCATION}"
  fi
else
  GIT_ARGS=(
    --source-type git
    --repo-url "${SKILLS_REPO_URL}"
    --plugin-name "local-ops-toolkit"
  )
  if [[ -n "${SKILLS_REPO_REF}" ]]; then
    GIT_ARGS+=(--ref "${SKILLS_REPO_REF}")
  fi
  if [[ -n "${SKILLS_REPO_SUBDIR}" ]]; then
    GIT_ARGS+=(--subdir "${SKILLS_REPO_SUBDIR}")
  fi
  if [[ -n "${SKILLS_REPO_TOKEN_ENV}" ]]; then
    GIT_ARGS+=(--git-token-env "${SKILLS_REPO_TOKEN_ENV}")
  fi

  if [[ "${DRY_RUN}" == "true" ]]; then
    run_cmd "${PYTHON_BIN}" -m tools.plugin_sync ingest \
      --source-type local --path "starter-marketplace/plugins/gcp-finops-toolkit" \
      "${COMMON_INGEST_ARGS[@]}" --dry-run
    run_cmd "${PYTHON_BIN}" -m tools.plugin_sync ingest \
      "${GIT_ARGS[@]}" "${COMMON_INGEST_ARGS[@]}" --dry-run
  else
    SKILLS_ACCESS_TOKEN="$("${GCLOUD}" auth print-access-token --quiet)"
    AGENT_REGISTRY_AUTH_TOKEN="${SKILLS_ACCESS_TOKEN}" "${PYTHON_BIN}" -m tools.plugin_sync ingest \
      --source-type local --path "starter-marketplace/plugins/gcp-finops-toolkit" \
      "${COMMON_INGEST_ARGS[@]}" >"${INGEST_OUT_1}"
    echo "  [OK] Ingested gcp-finops-toolkit skills in ${SKILLS_LOCATION}"

    AGENT_REGISTRY_AUTH_TOKEN="${SKILLS_ACCESS_TOKEN}" "${PYTHON_BIN}" -m tools.plugin_sync ingest \
      "${GIT_ARGS[@]}" "${COMMON_INGEST_ARGS[@]}" >"${INGEST_OUT_2}"
    echo "  [OK] Ingested Git repository skills (${SKILLS_REPO_URL}) in ${SKILLS_LOCATION}"
  fi
fi

# ----------------------------------------------------------------------------
# Step 7: Write Structured Deployment Manifest JSON
# ----------------------------------------------------------------------------
echo ""
echo "[Step 7/7] Writing deployment manifest JSON..."

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "[DRY-RUN] Would write deployment manifest to ${STATE_FILE}"
else
  "${PYTHON_BIN}" - "${STATE_FILE}" "${WORK_TMP}" "${PROJECT_ID}" "${PRIMARY_REGION}" \
    "${SECONDARY_REGION}" "${SKILLS_LOCATION}" "${ENABLE_SECONDARY}" "${REPO}" "${TAG}" "${NAME_PREFIX}" \
    "${DEPLOY_MODE}" "${SKILLS_SOURCE}" "${ALLOW_UNAUTH_DEMO}" \
    "${MW_SA_EMAIL}" "${WORKLOADS_SA_EMAIL}" "${INGEST_OUT_1}" "${INGEST_OUT_2}" "${TARGET_REGIONS[@]}" <<'PYEOF'
import datetime as dt
import json
import sys
from pathlib import Path

state_path = Path(sys.argv[1])
work_tmp = Path(sys.argv[2])
project_id = sys.argv[3]
primary_region = sys.argv[4]
secondary_region = sys.argv[5]
skills_location = sys.argv[6]
enable_secondary = sys.argv[7] == "true"
repo = sys.argv[8]
tag = sys.argv[9]
name_prefix = sys.argv[10]
deploy_mode = sys.argv[11]
skills_source = sys.argv[12]
allow_unauth = sys.argv[13] == "true"
mw_sa = sys.argv[14]
workloads_sa = sys.argv[15]
ingest_files = [Path(sys.argv[16]), Path(sys.argv[17])]
regions_list = sys.argv[18:]

skill_ids: list[str] = []
for f in ingest_files:
    if f.is_file():
        doc = json.loads(f.read_text(encoding="utf-8"))
        for plan in doc.get("plans", [doc]):
            for sk in plan.get("skills", []):
                pid = sk.get("private_skill_id")
                if pid and pid not in skill_ids:
                    skill_ids.append(pid)

if not skill_ids:
    skill_ids = [
        "private-cost-anomaly-triage",
        "private-bq-query-optimizer",
        "private-cloud-run-debugger",
        "private-terraform-iam-auditor",
        "private-incident-postmortem-writer",
    ]

regions_map = {}
for reg in regions_list:
    frag = work_tmp / f"region_{reg}.json"
    if frag.is_file():
        regions_map[reg] = json.loads(frag.read_text(encoding="utf-8"))

manifest = {
    "schema_version": "1.0.0",
    "deployed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "project_id": project_id,
    "primary_region": primary_region,
    "secondary_region": secondary_region,
    "skills_location": skills_location,
    "enable_secondary": enable_secondary,
    "artifact_registry_repo": repo,
    "image_tag": tag,
    "name_prefix": name_prefix,
    "deploy_mode": deploy_mode,
    "skills_source": skills_source,
    "allow_unauthenticated_demo": allow_unauth,
    "agent_registry_skills": skill_ids,
    "service_accounts": {
        "middleware": mw_sa,
        "sample_workloads": workloads_sa,
    },
    "regions": regions_map,
}
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(f"  [OK] Wrote deployment manifest: {state_path}")
PYEOF
fi

echo ""
echo "========================================================================"
echo "Deployment Complete!"
echo "========================================================================"
echo "Next Steps:"
echo "  1. Validate end-to-end live GCP deployment:"
if [[ "${ENABLE_SECONDARY}" == "true" ]]; then
  echo "     scripts/validate_live_gcp.sh --project ${PROJECT_ID} --primary-region ${PRIMARY_REGION} --secondary-region ${SECONDARY_REGION} --skills-location ${SKILLS_LOCATION} --enable-secondary --state-file ${STATE_FILE}"
else
  echo "     scripts/validate_live_gcp.sh --project ${PROJECT_ID} --primary-region ${PRIMARY_REGION} --skills-location ${SKILLS_LOCATION} --state-file ${STATE_FILE}"
fi
echo "  2. Clean up all workshop GCP resources when finished:"
if [[ "${ENABLE_SECONDARY}" == "true" ]]; then
  echo "     scripts/destroy_gcp.sh --project ${PROJECT_ID} --primary-region ${PRIMARY_REGION} --secondary-region ${SECONDARY_REGION} --skills-location ${SKILLS_LOCATION} --enable-secondary --state-file ${STATE_FILE}"
else
  echo "     scripts/destroy_gcp.sh --project ${PROJECT_ID} --primary-region ${PRIMARY_REGION} --skills-location ${SKILLS_LOCATION} --state-file ${STATE_FILE}"
fi
