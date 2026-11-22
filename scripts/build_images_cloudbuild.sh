#!/usr/bin/env bash
# Zero-Docker container image builder using Google Cloud Build & Artifact Registry.
#
# Submits checked-in cloudbuild/*.yaml manifests from the repository root so
# attendees never need a local Docker daemon. Supports primary region
# (default: asia-southeast3) and optional secondary region (e.g. asia-southeast1).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_ID="${GCP_PROJECT_ID:-}"
REGION="${GCP_LOCATION:-asia-southeast3}"
SECONDARY_REGION=""
REPO="${ARTIFACT_REGISTRY_REPO:-agent-marketplace}"
TAG="${IMAGE_TAG:-1.0.0}"
ONLY_MIDDLEWARE="false"
DRY_RUN="false"

usage() {
  cat <<'EOF'
Usage: scripts/build_images_cloudbuild.sh [OPTIONS]

Build and push workshop container images to Artifact Registry using Cloud Build
with zero local Docker required.

Options:
  --project ID               Google Cloud project ID (required, or $GCP_PROJECT_ID)
  --region REGION            Primary GCP region (default: asia-southeast3)
  --secondary-region REGION  Optional secondary GCP region (e.g. asia-southeast1)
  --repo REPO                Artifact Registry repository ID (default: agent-marketplace)
  --tag TAG                  Container image tag (default: 1.0.0)
  --only-middleware          Build only polyglot-middleware (used by --deploy-mode source)
  --dry-run                  Print exact gcloud commands without executing them
  -h, --help                 Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT_ID="$2"
      shift 2
      ;;
    --region)
      REGION="$2"
      shift 2
      ;;
    --secondary-region)
      SECONDARY_REGION="$2"
      shift 2
      ;;
    --repo)
      REPO="$2"
      shift 2
      ;;
    --tag)
      TAG="$2"
      shift 2
      ;;
    --only-middleware)
      ONLY_MIDDLEWARE="true"
      shift
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

if [[ -z "${PROJECT_ID}" ]]; then
  if [[ "${DRY_RUN}" == "false" ]]; then
    PROJECT_ID="$("${GCLOUD}" config get-value project --quiet 2>/dev/null || true)"
    if [[ "${PROJECT_ID}" == "(unset)" ]]; then
      PROJECT_ID=""
    fi
  fi
fi

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: --project PROJECT_ID (or \$GCP_PROJECT_ID) is required." >&2
  exit 2
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

ensure_ar_repo() {
  local target_region="$1"
  echo "Ensuring Artifact Registry Docker repository '${REPO}' in ${target_region}..."
  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[DRY-RUN] Ensure Artifact Registry repo exists:"
    run_cmd "${GCLOUD}" artifacts repositories describe "${REPO}" \
      --project="${PROJECT_ID}" \
      --location="${target_region}" \
      --quiet
    run_cmd "${GCLOUD}" artifacts repositories create "${REPO}" \
      --project="${PROJECT_ID}" \
      --location="${target_region}" \
      --repository-format=docker \
      --description="Enterprise Agent Marketplace workshop images" \
      --quiet
    return 0
  fi

  if "${GCLOUD}" artifacts repositories describe "${REPO}" \
    --project="${PROJECT_ID}" \
    --location="${target_region}" \
    --quiet >/dev/null 2>&1; then
    echo "  [OK] Repository '${REPO}' already exists in ${target_region}."
  else
    "${GCLOUD}" artifacts repositories create "${REPO}" \
      --project="${PROJECT_ID}" \
      --location="${target_region}" \
      --repository-format=docker \
      --description="Enterprise Agent Marketplace workshop images" \
      --quiet
    echo "  [OK] Created Artifact Registry repository '${REPO}' in ${target_region}."
  fi
}

build_region_images() {
  local target_region="$1"
  local prefix="${target_region}-docker.pkg.dev/${PROJECT_ID}/${REPO}"

  ensure_ar_repo "${target_region}"

  local specs=(
    "polyglot-middleware:cloudbuild/middleware.yaml"
  )
  if [[ "${ONLY_MIDDLEWARE}" != "true" ]]; then
    specs+=(
      "sample-mcp-server:cloudbuild/sample-mcp-server.yaml"
      "sample-a2a-agent:cloudbuild/sample-a2a-agent.yaml"
    )
  fi

  for entry in "${specs[@]}"; do
    local img_name="${entry%%:*}"
    local cb_config="${entry#*:}"
    local image_uri="${prefix}/${img_name}:${TAG}"
    echo ""
    echo "Building ${image_uri} via Cloud Build (${cb_config}) in ${target_region}..."
    if [[ "${DRY_RUN}" == "true" ]]; then
      printf '[DRY-RUN] CLOUDSDK_CORE_INTERACTIVE_UX_STYLE=OFF'
      printf ' %q' \
        "${GCLOUD}" builds submit \
        --project="${PROJECT_ID}" \
        --region="${target_region}" \
        --default-buckets-behavior=regional-user-owned-bucket \
        --config="${cb_config}" \
        --substitutions="_IMAGE_URI=${image_uri}" \
        . \
        --quiet
      printf '\n'
    else
      CLOUDSDK_CORE_INTERACTIVE_UX_STYLE=OFF "${GCLOUD}" builds submit \
        --project="${PROJECT_ID}" \
        --region="${target_region}" \
        --default-buckets-behavior=regional-user-owned-bucket \
        --config="${cb_config}" \
        --substitutions="_IMAGE_URI=${image_uri}" \
        . \
        --quiet
      echo "  [OK] Built & pushed ${image_uri}"
    fi
  done
}

cd "${REPO_ROOT}"

echo "========================================================================"
echo "Zero-Docker Cloud Build Image Builder"
echo "========================================================================"
echo "Project          : ${PROJECT_ID}"
echo "Primary Region   : ${REGION}"
if [[ -n "${SECONDARY_REGION}" ]]; then
  echo "Secondary Region : ${SECONDARY_REGION}"
fi
echo "Repository / Tag : ${REPO} / ${TAG}"
echo "Only Middleware  : ${ONLY_MIDDLEWARE}"
echo "Dry Run          : ${DRY_RUN}"
echo ""

build_region_images "${REGION}"

if [[ -n "${SECONDARY_REGION}" && "${SECONDARY_REGION}" != "${REGION}" ]]; then
  echo ""
  echo "--- Building Secondary Region Images (${SECONDARY_REGION}) ---"
  build_region_images "${SECONDARY_REGION}"
fi

echo ""
echo "Cloud Build image build step complete."
