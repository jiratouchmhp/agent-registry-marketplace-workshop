#!/usr/bin/env bash
# Apply IAM v3 Unified Access Policy (UAP) rules for Agent Gateway + IAP tool governance.
#
# Creates (or updates) an IAM AccessPolicy using `mcp-tool-annotations-uap.json`
# and binds it to either a specific Network Services Agent Gateway or the entire
# project via `gcloud iam policy-bindings create`.
#
# Supports `--dry-run` to validate JSON and preview exact gcloud commands without
# mutating Google Cloud resources.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_ID="${GCP_PROJECT_ID:-acme-agent-governance}"
LOCATION="${GCP_LOCATION:-us-central1}"
POLICY_ID="${UAP_POLICY_ID:-finops-mcp-tool-annotations}"
BINDING_ID="${UAP_BINDING_ID:-finops-mcp-tool-annotations-binding}"
GATEWAY_NAME="${AGENT_GATEWAY_NAME:-agent-marketplace-gateway}"
BIND_SCOPE="gateway" # "gateway" or "project"
PRINCIPAL="${AGENT_PRINCIPAL:-}"
RULES_FILE="${SCRIPT_DIR}/mcp-tool-annotations-uap.json"
DRY_RUN="false"
VALIDATE_ONLY="false"

usage() {
  cat <<'EOF'
Usage: apply-uap.sh [OPTIONS]

Apply IAM v3 Unified Access Policy (UAP) rules enforcing MCP tool annotations
(`read_only_hint` ALLOW, `destructive_hint` DENY) on Agent Gateway + IAP.

Options:
  --project ID           Google Cloud project ID (default: $GCP_PROJECT_ID or acme-agent-governance)
  --location REGION      Agent Gateway location (default: $GCP_LOCATION or us-central1)
  --policy-id ID         IAM AccessPolicy ID (default: finops-mcp-tool-annotations)
  --binding-id ID        IAM PolicyBinding ID (default: finops-mcp-tool-annotations-binding)
  --gateway-name NAME    Agent Gateway name when --bind-scope=gateway (default: agent-marketplace-gateway)
  --bind-scope SCOPE     Binding target: "gateway" or "project" (default: gateway)
  --principal PRINCIPAL  Override principal identifier in rules JSON (e.g., principal://agents.global...)
  --rules-file PATH      Path to UAP rules JSON file (default: ./mcp-tool-annotations-uap.json)
  --dry-run              Validate rules JSON and print exact gcloud commands without executing them
  --validate-only        Pass --validate-only to gcloud beta iam access-policies create
  -h, --help             Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT_ID="$2"
      shift 2
      ;;
    --location)
      LOCATION="$2"
      shift 2
      ;;
    --policy-id)
      POLICY_ID="$2"
      shift 2
      ;;
    --binding-id)
      BINDING_ID="$2"
      shift 2
      ;;
    --gateway-name)
      GATEWAY_NAME="$2"
      shift 2
      ;;
    --bind-scope)
      BIND_SCOPE="$2"
      shift 2
      ;;
    --principal)
      PRINCIPAL="$2"
      shift 2
      ;;
    --rules-file)
      RULES_FILE="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    --validate-only)
      VALIDATE_ONLY="true"
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

if [[ ! -f "${RULES_FILE}" ]]; then
  echo "ERROR: Rules file not found: ${RULES_FILE}" >&2
  exit 1
fi

if [[ "${BIND_SCOPE}" == "gateway" ]]; then
  TARGET_RESOURCE="//networkservices.googleapis.com/projects/${PROJECT_ID}/locations/${LOCATION}/agentGateways/${GATEWAY_NAME}"
elif [[ "${BIND_SCOPE}" == "project" ]]; then
  TARGET_RESOURCE="//cloudresourcemanager.googleapis.com/projects/${PROJECT_ID}"
else
  echo "ERROR: --bind-scope must be 'gateway' or 'project' (got '${BIND_SCOPE}')" >&2
  exit 2
fi

RENDERED_RULES="$(mktemp /tmp/mcp-tool-annotations-uap.XXXXXX.json)"
trap 'rm -f "${RENDERED_RULES}"' EXIT

PYTHON_BIN="python3"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "ERROR: python3 is required to validate and render UAP rules JSON." >&2
  exit 1
fi

"${PYTHON_BIN}" - "${RULES_FILE}" "${RENDERED_RULES}" "${PRINCIPAL}" <<'PYEOF'
import json
import sys
from pathlib import Path

src_path = Path(sys.argv[1])
dst_path = Path(sys.argv[2])
principal_override = sys.argv[3].strip()

rules = json.loads(src_path.read_text(encoding="utf-8"))
if not isinstance(rules, list) or not rules:
    raise SystemExit("UAP rules JSON must be a non-empty JSON array of rule objects.")

for idx, rule in enumerate(rules):
    if rule.get("effect") not in ("ALLOW", "DENY"):
        raise SystemExit(f"Rule[{idx}] has invalid effect: {rule.get('effect')!r}")
    perms = rule.get("operation", {}).get("permissions", [])
    if "iap.googleapis.com/resources.egressViaIAP" not in perms:
        raise SystemExit(
            f"Rule[{idx}] must target permission 'iap.googleapis.com/resources.egressViaIAP'."
        )
    if principal_override:
        rule["principals"] = [principal_override]

dst_path.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
PYEOF

POLICY_RESOURCE="projects/${PROJECT_ID}/locations/global/accessPolicies/${POLICY_ID}"

CREATE_CMD=(
  gcloud beta iam access-policies create "${POLICY_ID}"
  --project="${PROJECT_ID}"
  --location="global"
  --display-name="FinOps MCP Tool Annotation Governance"
  --details-rules="${RENDERED_RULES}"
)
if [[ "${VALIDATE_ONLY}" == "true" ]]; then
  CREATE_CMD+=(--validate-only)
fi

BIND_CMD=(
  gcloud iam policy-bindings create "${BINDING_ID}"
  --project="${PROJECT_ID}"
  --location="global"
  --policy="${POLICY_RESOURCE}"
  --policy-kind="access"
  --target-resource="${TARGET_RESOURCE}"
  --display-name="Bind FinOps MCP Tool UAP"
)

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "[DRY-RUN] Validated UAP rules JSON (${RULES_FILE}):"
  cat "${RENDERED_RULES}"
  echo ""
  echo "[DRY-RUN] Step 1 — Create IAM AccessPolicy:"
  printf ' '
  printf ' %q' "${CREATE_CMD[@]}"
  echo ""
  echo "[DRY-RUN] Step 2 — Bind AccessPolicy to ${TARGET_RESOURCE}:"
  printf ' '
  printf ' %q' "${BIND_CMD[@]}"
  echo ""
  exit 0
fi

echo "Applying IAM v3 AccessPolicy '${POLICY_ID}' in project '${PROJECT_ID}'..."
if gcloud beta iam access-policies describe "${POLICY_ID}" \
  --project="${PROJECT_ID}" \
  --location="global" >/dev/null 2>&1; then
  echo "AccessPolicy '${POLICY_ID}' already exists; updating rules..."
  gcloud beta iam access-policies update "${POLICY_ID}" \
    --project="${PROJECT_ID}" \
    --location="global" \
    --details-rules="${RENDERED_RULES}"
else
  "${CREATE_CMD[@]}"
fi

if [[ "${VALIDATE_ONLY}" == "true" ]]; then
  echo "Validation succeeded (--validate-only); skipping policy binding creation."
  exit 0
fi

if gcloud iam policy-bindings describe "${BINDING_ID}" \
  --project="${PROJECT_ID}" \
  --location="global" >/dev/null 2>&1; then
  echo "PolicyBinding '${BINDING_ID}' already exists for target '${TARGET_RESOURCE}'."
else
  echo "Binding AccessPolicy '${POLICY_RESOURCE}' to '${TARGET_RESOURCE}'..."
  "${BIND_CMD[@]}"
fi

echo "Done. Verify decisions in Cloud Logging with:"
echo '  protoPayload.serviceName="iap.googleapis.com" AND protoPayload.authorizationInfo.permission="iap.resources.egressViaIAP"'
