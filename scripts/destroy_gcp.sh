#!/usr/bin/env bash
# Surgical, Complete Teardown + Post-Teardown Zero-Leftover Audit for Workshop GCP Resources.
#
# Cleans up primary (asia-southeast3) and secondary (asia-southeast1, when
# --enable-secondary is set OR workshop resources exist there):
#   1. Cloud Run services (polyglot-middleware, sample-a2a-agent, sample-mcp-server)
#   2. Agent Registry Skills via REST DELETE ...?force=true (polling LRO)
#   3. Agent Registry Services (finops-mcp-server, finops-advisor-agent, finops-billing-api)
#   4. Artifact Registry Docker repository
#   5. Optional GCS staging bucket (gs://<project>-agent-registry-skills)
#   6. Workshop Service Accounts + roles/agentregistry.viewer project IAM binding
#   7. Post-teardown bounded list (--limit=50 --quiet) zero-leftover audit!

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"

PROJECT_ID="${GCP_PROJECT_ID:-}"
PRIMARY_REGION="${GCP_LOCATION:-asia-southeast3}"
SECONDARY_REGION="${GCP_SECONDARY_LOCATION:-asia-southeast1}"
SKILLS_LOCATION="${AGENT_REGISTRY_SKILLS_LOCATION:-global}"
SKILLS_LOCATION_EXPLICIT="false"
ENABLE_SECONDARY="false"
REPO="${ARTIFACT_REGISTRY_REPO:-agent-marketplace}"
REPO_EXPLICIT="false"
NAME_PREFIX=""
DELETE_SAS="true"
DELETE_STAGING_BUCKET="true"
DELETE_SOURCE_DEPLOY_REPO="false"
STATE_FILE=""
DRY_RUN="false"

usage() {
  cat <<'EOF'
Usage: scripts/destroy_gcp.sh [OPTIONS]

Surgically tear down all Enterprise Agent Marketplace workshop GCP resources
and run a post-teardown zero-leftover audit.

Options:
  --project ID               Google Cloud project ID (required, or $GCP_PROJECT_ID)
  --primary-region REGION    Primary GCP region (default: asia-southeast3)
  --secondary-region REGION  Secondary GCP region (default: asia-southeast1)
  --skills-location LOC      Agent Registry Skills location: global|us|eu (default: global)
  --enable-secondary         Include secondary region in teardown & audit
  --repo REPO                Artifact Registry repository ID (default: agent-marketplace)
  --name-prefix PREFIX       Optional resource name prefix used during deploy
  --delete-sas               Delete workshop SAs & IAM bindings (default: true)
  --no-delete-sas            Keep workshop service accounts
  --delete-staging-bucket      Delete gs://<project>-agent-registry-skills if present (default: true)
  --no-delete-staging-bucket   Keep GCS staging bucket
  --delete-source-deploy-repo  Also delete Cloud Run's auto-created 'cloud-run-source-deploy' AR repo
  --state-file PATH            Deployment manifest JSON from deploy_gcp.sh
  --dry-run                    Print teardown actions without deleting GCP resources
  -h, --help                   Show this help message and exit
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
      SKILLS_LOCATION_EXPLICIT="true"
      shift 2
      ;;
    --enable-secondary)
      ENABLE_SECONDARY="true"
      shift
      ;;
    --repo)
      REPO="$2"
      REPO_EXPLICIT="true"
      shift 2
      ;;
    --name-prefix)
      NAME_PREFIX="$2"
      shift 2
      ;;
    --delete-sas)
      DELETE_SAS="true"
      shift
      ;;
    --no-delete-sas)
      DELETE_SAS="false"
      shift
      ;;
    --delete-staging-bucket)
      DELETE_STAGING_BUCKET="true"
      shift
      ;;
    --no-delete-staging-bucket)
      DELETE_STAGING_BUCKET="false"
      shift
      ;;
    --delete-source-deploy-repo)
      DELETE_SOURCE_DEPLOY_REPO="true"
      shift
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

if [[ -z "${PROJECT_ID}" && -n "${STATE_FILE}" && -f "${STATE_FILE}" && -x "${PYTHON_BIN}" ]]; then
  PROJECT_ID="$("${PYTHON_BIN}" -c "import json; print(json.load(open('${STATE_FILE}')).get('project_id',''))")"
fi
if [[ -z "${PROJECT_ID}" && "${DRY_RUN}" != "true" ]]; then
  PROJECT_ID="$("${GCLOUD}" config get-value project --quiet 2>/dev/null || true)"
  [[ "${PROJECT_ID}" == "(unset)" ]] && PROJECT_ID=""
fi
if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: --project PROJECT_ID (or \$GCP_PROJECT_ID) is required." >&2
  exit 2
fi

if [[ -z "${STATE_FILE}" ]]; then
  DEFAULT_STATE="/tmp/workshop-gcp-deploy-${PROJECT_ID}.json"
  if [[ -f "${DEFAULT_STATE}" ]]; then
    STATE_FILE="${DEFAULT_STATE}"
  fi
fi

# Collect extra metadata from state file if present
EXTRA_SKILLS=()
EXTRA_AR_SERVICES=()
EXTRA_RUN_SERVICES=()
if [[ -n "${STATE_FILE}" && -f "${STATE_FILE}" && -x "${PYTHON_BIN}" ]]; then
  eval "$("${PYTHON_BIN}" - "${STATE_FILE}" <<'PYEOF'
import json, shlex, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
skills, ar_svcs, run_svcs = set(), set(), set()
skills.update(doc.get("agent_registry_skills", []))
for reg_info in doc.get("regions", {}).values():
    skills.update(reg_info.get("agent_registry_skills", []))
    ar_svcs.update(reg_info.get("agent_registry_services", []))
    for cr in reg_info.get("cloud_run", {}).values():
        if cr.get("name"):
            run_svcs.add(cr["name"])
if doc.get("enable_secondary"):
    print("STATE_HAD_SECONDARY=true")
if doc.get("skills_location"):
    print(f"STATE_SKILLS_LOC={shlex.quote(doc['skills_location'])}")
if doc.get("artifact_registry_repo"):
    print(f"STATE_REPO={shlex.quote(doc['artifact_registry_repo'])}")
if doc.get("name_prefix"):
    print(f"STATE_PREFIX={shlex.quote(doc['name_prefix'])}")
print("EXTRA_SKILLS=(" + " ".join(shlex.quote(x) for x in sorted(skills)) + ")")
print("EXTRA_AR_SERVICES=(" + " ".join(shlex.quote(x) for x in sorted(ar_svcs)) + ")")
print("EXTRA_RUN_SERVICES=(" + " ".join(shlex.quote(x) for x in sorted(run_svcs)) + ")")
PYEOF
)"
  [[ "${STATE_HAD_SECONDARY:-false}" == "true" ]] && ENABLE_SECONDARY="true"
  [[ "${SKILLS_LOCATION_EXPLICIT}" == "false" && -n "${STATE_SKILLS_LOC:-}" ]] && SKILLS_LOCATION="${STATE_SKILLS_LOC}"
  [[ "${REPO_EXPLICIT}" == "false" && -n "${STATE_REPO:-}" ]] && REPO="${STATE_REPO}"
  [[ -z "${NAME_PREFIX}" && -n "${STATE_PREFIX:-}" ]] && NAME_PREFIX="${STATE_PREFIX}"
fi

PREFIX_DASH=""
[[ -n "${NAME_PREFIX}" ]] && PREFIX_DASH="${NAME_PREFIX%-}-"

RUN_SERVICES=(
  "${PREFIX_DASH}polyglot-middleware"
  "${PREFIX_DASH}sample-a2a-agent"
  "${PREFIX_DASH}sample-mcp-server"
)
for extra in "${EXTRA_RUN_SERVICES[@]}"; do
  [[ " ${RUN_SERVICES[*]} " == *" ${extra} "* ]] || RUN_SERVICES+=("${extra}")
done

AR_SERVICES=(
  "${PREFIX_DASH}finops-mcp-server"
  "${PREFIX_DASH}finops-advisor-agent"
  "${PREFIX_DASH}finops-billing-api"
)
for extra in "${EXTRA_AR_SERVICES[@]}"; do
  [[ " ${AR_SERVICES[*]} " == *" ${extra} "* ]] || AR_SERVICES+=("${extra}")
done

WORKSHOP_SKILLS=(
  "private-cost-anomaly-triage"
  "private-bq-query-optimizer"
  "private-cloud-run-debugger"
  "private-terraform-iam-auditor"
  "private-incident-postmortem-writer"
)
for extra in "${EXTRA_SKILLS[@]}"; do
  [[ " ${WORKSHOP_SKILLS[*]} " == *" ${extra} "* ]] || WORKSHOP_SKILLS+=("${extra}")
done

MW_SA_EMAIL="agent-marketplace-middleware@${PROJECT_ID}.iam.gserviceaccount.com"
WORKLOADS_SA_EMAIL="finops-sample-workloads@${PROJECT_ID}.iam.gserviceaccount.com"
STAGING_BUCKET="${PROJECT_ID}-agent-registry-skills"

# Determine target regions (primary + secondary if enabled OR resources detected in secondary)
TARGET_REGIONS=("${PRIMARY_REGION}")
if [[ -n "${SECONDARY_REGION}" && "${SECONDARY_REGION}" != "${PRIMARY_REGION}" ]]; then
  if [[ "${ENABLE_SECONDARY}" == "true" ]]; then
    TARGET_REGIONS+=("${SECONDARY_REGION}")
  elif [[ "${DRY_RUN}" == "false" ]]; then
    # Auto-detect whether any workshop resource exists in secondary region
    SEC_FOUND="false"
    if "${GCLOUD}" artifacts repositories describe "${REPO}" --project="${PROJECT_ID}" --location="${SECONDARY_REGION}" --quiet >/dev/null 2>&1; then
      SEC_FOUND="true"
    fi
    if [[ "${SEC_FOUND}" == "false" ]]; then
      SEC_RUN="$("${GCLOUD}" run services list --project="${PROJECT_ID}" --region="${SECONDARY_REGION}" --format="value(metadata.name)" --limit=50 --quiet 2>/dev/null || true)"
      for svc in "${RUN_SERVICES[@]}"; do
        if grep -Fxq "${svc}" <<<"${SEC_RUN}"; then
          SEC_FOUND="true"
          break
        fi
      done
    fi
    if [[ "${SEC_FOUND}" == "false" ]]; then
      SEC_AR="$("${GCLOUD}" agent-registry services list --project="${PROJECT_ID}" --location="${SECONDARY_REGION}" --format="value(name)" --limit=50 --quiet 2>/dev/null || true)"
      for svc in "${AR_SERVICES[@]}"; do
        if grep -Eq "(^|/)${svc}$" <<<"${SEC_AR}"; then
          SEC_FOUND="true"
          break
        fi
      done
    fi
    if [[ "${SEC_FOUND}" == "true" ]]; then
      echo "[INFO] Detected workshop resources in secondary region '${SECONDARY_REGION}'; adding to teardown."
      TARGET_REGIONS+=("${SECONDARY_REGION}")
    fi
  fi
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

echo "========================================================================"
echo "Enterprise Agent Marketplace — Live GCP Teardown & Zero-Leftover Audit"
echo "========================================================================"
echo "Project              : ${PROJECT_ID}"
echo "Regions              : ${TARGET_REGIONS[*]}"
echo "Skills Location      : ${SKILLS_LOCATION}"
echo "Cloud Run Services   : ${RUN_SERVICES[*]}"
echo "Agent Registry Svcs  : ${AR_SERVICES[*]}"
echo "Agent Registry Skills: ${WORKSHOP_SKILLS[*]} (in ${SKILLS_LOCATION})"
echo "Artifact Registry    : ${REPO}"
echo "Delete SAs           : ${DELETE_SAS}"
echo "Delete Staging Bucket: ${DELETE_STAGING_BUCKET} (gs://${STAGING_BUCKET})"
echo "Delete Source AR Repo: ${DELETE_SOURCE_DEPLOY_REPO} (cloud-run-source-deploy)"
echo "Dry Run              : ${DRY_RUN}"
echo ""

ACCESS_TOKEN=""
if [[ "${DRY_RUN}" == "false" ]]; then
  ACCESS_TOKEN="$("${GCLOUD}" auth print-access-token --quiet)"
fi

for region in "${TARGET_REGIONS[@]}"; do
  echo "--- Teardown in region: ${region} ---"

  # 1. Delete Cloud Run services
  echo "[1/6] Deleting Cloud Run services in ${region}..."
  for svc in "${RUN_SERVICES[@]}"; do
    if [[ "${DRY_RUN}" == "true" ]]; then
      run_cmd "${GCLOUD}" run services delete "${svc}" \
        --project="${PROJECT_ID}" \
        --region="${region}" \
        --quiet
    else
      if "${GCLOUD}" run services describe "${svc}" \
        --project="${PROJECT_ID}" --region="${region}" --quiet >/dev/null 2>&1; then
        "${GCLOUD}" run services delete "${svc}" \
          --project="${PROJECT_ID}" \
          --region="${region}" \
          --quiet
        echo "  [OK] Deleted Cloud Run service: ${svc} (${region})"
      else
        echo "  [SKIP] Cloud Run service not found: ${svc} (${region})"
      fi
    fi
  done

  # 2. Delete Agent Registry Services (regional)
  echo "[2/6] Deleting Agent Registry Services in ${region}..."
  for svc in "${AR_SERVICES[@]}"; do
    if [[ "${DRY_RUN}" == "true" ]]; then
      run_cmd "${GCLOUD}" agent-registry services delete "${svc}" \
        --project="${PROJECT_ID}" \
        --location="${region}" \
        --quiet
    else
      if "${GCLOUD}" agent-registry services describe "${svc}" \
        --project="${PROJECT_ID}" --location="${region}" --quiet >/dev/null 2>&1; then
        "${GCLOUD}" agent-registry services delete "${svc}" \
          --project="${PROJECT_ID}" \
          --location="${region}" \
          --quiet
        echo "  [OK] Deleted Agent Registry Service: ${svc} (${region})"
      else
        echo "  [SKIP] Agent Registry Service not found: ${svc} (${region})"
      fi
    fi
  done

  # 3. Delete Artifact Registry repository
  echo "[3/6] Deleting Artifact Registry repository '${REPO}' in ${region}..."
  if [[ "${DRY_RUN}" == "true" ]]; then
    run_cmd "${GCLOUD}" artifacts repositories delete "${REPO}" \
      --project="${PROJECT_ID}" \
      --location="${region}" \
      --quiet
  else
    if "${GCLOUD}" artifacts repositories describe "${REPO}" \
      --project="${PROJECT_ID}" --location="${region}" --quiet >/dev/null 2>&1; then
      "${GCLOUD}" artifacts repositories delete "${REPO}" \
        --project="${PROJECT_ID}" \
        --location="${region}" \
        --quiet
      echo "  [OK] Deleted Artifact Registry repository: ${REPO} (${region})"
    else
      echo "  [SKIP] Artifact Registry repository not found: ${REPO} (${region})"
    fi
  fi

  if [[ "${DELETE_SOURCE_DEPLOY_REPO}" == "true" ]]; then
    if [[ "${DRY_RUN}" == "true" ]]; then
      run_cmd "${GCLOUD}" artifacts repositories delete "cloud-run-source-deploy" \
        --project="${PROJECT_ID}" \
        --location="${region}" \
        --quiet
    else
      if "${GCLOUD}" artifacts repositories describe "cloud-run-source-deploy" \
        --project="${PROJECT_ID}" --location="${region}" --quiet >/dev/null 2>&1; then
        "${GCLOUD}" artifacts repositories delete "cloud-run-source-deploy" \
          --project="${PROJECT_ID}" \
          --location="${region}" \
          --quiet
        echo "  [OK] Deleted Cloud Run source-deploy AR repository: cloud-run-source-deploy (${region})"
      else
        echo "  [SKIP] Cloud Run source-deploy AR repository not found: cloud-run-source-deploy (${region})"
      fi
    fi
  fi
done

# 4. Delete standalone Agent Registry Skills ONCE in ${SKILLS_LOCATION} (global/us/eu) via REST DELETE ?force=true
echo ""
echo "[4/6] Deleting standalone Agent Registry Skills in ${SKILLS_LOCATION} via REST DELETE ?force=true..."
if [[ "${DRY_RUN}" == "true" ]]; then
  for sk in "${WORKSHOP_SKILLS[@]}"; do
    echo "[DRY-RUN] REST DELETE https://agentregistry.googleapis.com/v1/projects/${PROJECT_ID}/locations/${SKILLS_LOCATION}/skills/${sk}?force=true"
  done
else
  "${PYTHON_BIN}" - "${PROJECT_ID}" "${SKILLS_LOCATION}" "${ACCESS_TOKEN}" "${WORKSHOP_SKILLS[@]}" <<'PYEOF'
import sys, time
import httpx

project_id, location, token = sys.argv[1:4]
skill_ids = sys.argv[4:]
base = "https://agentregistry.googleapis.com/v1"
headers = {
    "Authorization": f"Bearer {token}",
    "x-goog-user-project": project_id,
    "Accept": "application/json",
}

def poll_lro(client: httpx.Client, op: dict) -> None:
    if not isinstance(op, dict):
        return
    if "done" not in op and "/operations/" not in str(op.get("name", "")):
        return
    deadline = time.monotonic() + 120.0
    while not op.get("done", False):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for delete LRO: {op.get('name')}")
        time.sleep(1.5)
        r = client.get(f"{base}/{op['name'].lstrip('/')}", headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"Failed polling delete LRO ({r.status_code}): {r.text}")
        op = r.json()
    if op.get("error"):
        raise RuntimeError(f"Skill delete LRO failed: {op['error']}")

with httpx.Client(timeout=60.0) as client:
    for sk in skill_ids:
        url = f"{base}/projects/{project_id}/locations/{location}/skills/{sk}"
        resp = client.delete(url, params={"force": "true"}, headers=headers)
        if resp.status_code == 404:
            print(f"  [SKIP] Skill not found: {sk} ({location})")
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"Failed deleting skill '{sk}' ({resp.status_code}): {resp.text}")
        if resp.content:
            poll_lro(client, resp.json())
        print(f"  [OK] Deleted Agent Registry Skill (force=true): {sk} ({location})")
PYEOF
fi

# 5. Optional GCS staging bucket cleanup
echo ""
echo "[5/6] Checking optional GCS staging bucket (gs://${STAGING_BUCKET})..."
if [[ "${DELETE_STAGING_BUCKET}" == "true" ]]; then
  if [[ "${DRY_RUN}" == "true" ]]; then
    run_cmd "${GCLOUD}" storage rm --recursive --all-versions "gs://${STAGING_BUCKET}/**" --project="${PROJECT_ID}" --quiet
    run_cmd "${GCLOUD}" storage buckets delete "gs://${STAGING_BUCKET}" --project="${PROJECT_ID}" --quiet
  else
    if "${GCLOUD}" storage buckets describe "gs://${STAGING_BUCKET}" --project="${PROJECT_ID}" --quiet >/dev/null 2>&1; then
      "${GCLOUD}" storage rm --recursive --all-versions "gs://${STAGING_BUCKET}/**" --project="${PROJECT_ID}" --quiet 2>/dev/null || true
      "${GCLOUD}" storage buckets delete "gs://${STAGING_BUCKET}" --project="${PROJECT_ID}" --quiet
      echo "  [OK] Deleted GCS staging bucket: gs://${STAGING_BUCKET}"
    else
      echo "  [SKIP] GCS staging bucket not found: gs://${STAGING_BUCKET}"
    fi
  fi
else
  echo "  [SKIP] Keeping GCS staging bucket (--no-delete-staging-bucket)."
fi

# 6. Workshop Service Accounts & IAM binding cleanup
echo ""
echo "[6/6] Cleaning up workshop Service Accounts & IAM bindings..."
if [[ "${DELETE_SAS}" == "true" ]]; then
  if [[ "${DRY_RUN}" == "true" ]]; then
    run_cmd "${GCLOUD}" projects remove-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${MW_SA_EMAIL}" \
      --role="roles/agentregistry.viewer" \
      --condition=None \
      --quiet
    run_cmd "${GCLOUD}" iam service-accounts delete "${MW_SA_EMAIL}" --project="${PROJECT_ID}" --quiet
    run_cmd "${GCLOUD}" iam service-accounts delete "${WORKLOADS_SA_EMAIL}" --project="${PROJECT_ID}" --quiet
  else
    # Pass --condition=None (matching deploy_gcp.sh) so IAM policy v3 projects
    # with unrelated conditional bindings do not reject the removal, and also
    # sweep any deleted:serviceAccount:<email>?uid=... tombstones on re-runs.
    POLICY_JSON="$("${GCLOUD}" projects get-iam-policy "${PROJECT_ID}" --format=json --quiet 2>/dev/null || true)"
    MATCHING_VIEWER_MEMBERS=""
    if [[ -n "${POLICY_JSON}" && -x "${PYTHON_BIN}" ]]; then
      MATCHING_VIEWER_MEMBERS="$("${PYTHON_BIN}" - "${POLICY_JSON}" "${MW_SA_EMAIL}" "${WORKLOADS_SA_EMAIL}" <<'PYEOF'
import json, sys

policy = json.loads(sys.argv[1])
targets = tuple(sys.argv[2:])
seen: list[str] = []
for binding in policy.get("bindings", []):
    if binding.get("role") != "roles/agentregistry.viewer":
        continue
    if binding.get("condition"):
        continue
    for member in binding.get("members", []):
        if any(
            member == f"serviceAccount:{sa}" or member.startswith(f"deleted:serviceAccount:{sa}?")
            for sa in targets
        ):
            if member not in seen:
                seen.append(member)
for m in seen:
    print(m)
PYEOF
)"
    fi

    if [[ -n "${MATCHING_VIEWER_MEMBERS}" ]]; then
      while IFS= read -r member; do
        [[ -z "${member}" ]] && continue
        "${GCLOUD}" projects remove-iam-policy-binding "${PROJECT_ID}" \
          --member="${member}" \
          --role="roles/agentregistry.viewer" \
          --condition=None \
          --quiet >/dev/null
        echo "  [OK] Removed roles/agentregistry.viewer binding for: ${member}"
      done <<<"${MATCHING_VIEWER_MEMBERS}"
    else
      echo "  [SKIP] No roles/agentregistry.viewer binding found for workshop SAs."
    fi

    for sa_email in "${MW_SA_EMAIL}" "${WORKLOADS_SA_EMAIL}"; do
      if "${GCLOUD}" iam service-accounts describe "${sa_email}" --project="${PROJECT_ID}" --quiet >/dev/null 2>&1; then
        "${GCLOUD}" iam service-accounts delete "${sa_email}" --project="${PROJECT_ID}" --quiet
        echo "  [OK] Deleted service account: ${sa_email}"
      else
        echo "  [SKIP] Service account not found: ${sa_email}"
      fi
    done
  fi
else
  echo "  [SKIP] Keeping workshop service accounts (--no-delete-sas)."
fi

# 7. Post-Teardown Zero-Leftover Audit
echo ""
echo "========================================================================"
echo "Post-Teardown Zero-Leftover Audit (--limit=50 --quiet)"
echo "========================================================================"

if [[ "${DRY_RUN}" == "true" ]]; then
  for region in "${TARGET_REGIONS[@]}"; do
    run_cmd "${GCLOUD}" run services list --project="${PROJECT_ID}" --region="${region}" --limit=50 --quiet
    run_cmd "${GCLOUD}" artifacts repositories list --project="${PROJECT_ID}" --location="${region}" --limit=50 --quiet
    run_cmd "${GCLOUD}" agent-registry services list --project="${PROJECT_ID}" --location="${region}" --limit=50 --quiet
  done
  for sk in "${WORKSHOP_SKILLS[@]}"; do
    echo "[DRY-RUN] REST GET (expect 404) https://agentregistry.googleapis.com/v1/projects/${PROJECT_ID}/locations/${SKILLS_LOCATION}/skills/${sk}"
  done
  echo ""
  echo "[NOTE] Shared GCP build artifacts left untouched by default:"
  echo "  - If you deployed with '--deploy-mode source', Cloud Run auto-creates an Artifact Registry"
  echo "    repository named 'cloud-run-source-deploy' in each region. Pass --delete-source-deploy-repo"
  echo "    to remove it if no other Cloud Run source deployments share this project."
  echo "  - Cloud Build regional staging buckets (created by --default-buckets-behavior=regional-user-owned-bucket)"
  echo "    are retained to avoid disrupting other builds in ${PROJECT_ID}."
  echo "[DRY-RUN] Teardown & zero-leftover audit plan complete."
  exit 0
fi

LEFTOVERS=()

for region in "${TARGET_REGIONS[@]}"; do
  RUN_LIST="$("${GCLOUD}" run services list --project="${PROJECT_ID}" --region="${region}" --format="value(metadata.name)" --limit=50 --quiet 2>/dev/null || true)"
  for svc in "${RUN_SERVICES[@]}"; do
    if grep -Fxq "${svc}" <<<"${RUN_LIST}"; then
      LEFTOVERS+=("Cloud Run (${region}): ${svc}")
    fi
  done

  AR_REPO_LIST="$("${GCLOUD}" artifacts repositories list --project="${PROJECT_ID}" --location="${region}" --format="value(name)" --limit=50 --quiet 2>/dev/null || true)"
  if grep -Eq "(^|/)${REPO}$" <<<"${AR_REPO_LIST}"; then
    LEFTOVERS+=("Artifact Registry (${region}): ${REPO}")
  fi

  AR_SVC_LIST="$("${GCLOUD}" agent-registry services list --project="${PROJECT_ID}" --location="${region}" --format="value(name)" --limit=50 --quiet 2>/dev/null || true)"
  for svc in "${AR_SERVICES[@]}"; do
    if grep -Eq "(^|/)${svc}$" <<<"${AR_SVC_LIST}"; then
      LEFTOVERS+=("Agent Registry Service (${region}): ${svc}")
    fi
  done
done

# Verify every workshop Skill returns 404 in ${SKILLS_LOCATION} (REST GET avoids
# gcloud skills list --limit=50 pagination truncation behind 100+ built-in Google skills).
REMAINING_SKILLS="$("${PYTHON_BIN}" - "${PROJECT_ID}" "${SKILLS_LOCATION}" "${ACCESS_TOKEN}" "${WORKSHOP_SKILLS[@]}" <<'PYEOF'
import sys
import httpx

project_id, location, token = sys.argv[1:4]
skill_ids = sys.argv[4:]
base = "https://agentregistry.googleapis.com/v1"
headers = {
    "Authorization": f"Bearer {token}",
    "x-goog-user-project": project_id,
    "Accept": "application/json",
}

leftovers: list[str] = []
with httpx.Client(timeout=30.0) as client:
    for sk in skill_ids:
        url = f"{base}/projects/{project_id}/locations/{location}/skills/{sk}"
        resp = client.get(url, headers=headers)
        if resp.status_code == 404:
            continue
        if resp.status_code == 200:
            leftovers.append(sk)
        else:
            raise RuntimeError(
                f"Unexpected HTTP {resp.status_code} auditing skill '{sk}' in {location}: {resp.text}"
            )
for sk in leftovers:
    print(sk)
PYEOF
)"

if [[ -n "${REMAINING_SKILLS}" ]]; then
  while IFS= read -r sk; do
    [[ -n "${sk}" ]] && LEFTOVERS+=("Agent Registry Skill (${SKILLS_LOCATION}): ${sk}")
  done <<<"${REMAINING_SKILLS}"
fi

if [[ "${DELETE_SAS}" == "true" ]]; then
  for sa_email in "${MW_SA_EMAIL}" "${WORKLOADS_SA_EMAIL}"; do
    if "${GCLOUD}" iam service-accounts describe "${sa_email}" --project="${PROJECT_ID}" --quiet >/dev/null 2>&1; then
      LEFTOVERS+=("IAM Service Account: ${sa_email}")
    fi
  done

  AUDIT_POLICY_JSON="$("${GCLOUD}" projects get-iam-policy "${PROJECT_ID}" --format=json --quiet 2>/dev/null || true)"
  if [[ -n "${AUDIT_POLICY_JSON}" && -x "${PYTHON_BIN}" ]]; then
    REMAINING_IAM_BINDINGS="$("${PYTHON_BIN}" - "${AUDIT_POLICY_JSON}" "${MW_SA_EMAIL}" "${WORKLOADS_SA_EMAIL}" <<'PYEOF'
import json, sys

policy = json.loads(sys.argv[1])
targets = tuple(sys.argv[2:])
for binding in policy.get("bindings", []):
    role = binding.get("role", "")
    for member in binding.get("members", []):
        if any(
            member == f"serviceAccount:{sa}" or member.startswith(f"deleted:serviceAccount:{sa}?")
            for sa in targets
        ):
            print(f"{role} -> {member}")
PYEOF
)"
    if [[ -n "${REMAINING_IAM_BINDINGS}" ]]; then
      while IFS= read -r b; do
        [[ -n "${b}" ]] && LEFTOVERS+=("Project IAM Binding: ${b}")
      done <<<"${REMAINING_IAM_BINDINGS}"
    fi
  fi
fi

if [[ ${#LEFTOVERS[@]} -gt 0 ]]; then
  echo "[FAIL] Post-teardown audit found ${#LEFTOVERS[@]} remaining workshop resource(s):" >&2
  for item in "${LEFTOVERS[@]}"; do
    echo "  - ${item}" >&2
  done
  exit 1
fi

if [[ -n "${STATE_FILE}" && -f "${STATE_FILE}" ]]; then
  rm -f "${STATE_FILE}"
fi

echo ""
echo "[NOTE] Shared GCP build artifacts (left untouched by default unless opted in):"
echo "  - If you used '--deploy-mode source', Cloud Run auto-creates an Artifact Registry repository"
echo "    named 'cloud-run-source-deploy' in each region. Re-run with --delete-source-deploy-repo"
echo "    to remove it if this is a dedicated workshop sandbox."
echo "  - Regional Cloud Build user-owned staging buckets (--default-buckets-behavior=regional-user-owned-bucket)"
echo "    are retained by default; inspect via 'gcloud storage buckets list --project=${PROJECT_ID} --limit=50 --quiet'."
echo ""
echo "[PASS] Zero-leftover audit confirmed all workshop resources removed in: ${TARGET_REGIONS[*]} (skills: ${SKILLS_LOCATION})"
