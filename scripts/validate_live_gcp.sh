#!/usr/bin/env bash
# Live Google Cloud End-to-End Attendee Validation Script.
#
# Validates a live primary Bangkok (asia-southeast3) deployment plus optional
# secondary Singapore (asia-southeast1) failover deployment:
#   1. Primary /health, /v1/catalog, 4x /v1/marketplace/*.json, plugin ZIPs +
#      X-Bundle-Sha256, and POST /mcp (initialize, tools/list, load_verified_skill,
#      call_remote_a2a_agent)
#   2. Direct Agent Registry v1 ?alt=media HTTP 302 redirect header-handling check
#   3. tools.hub_sync sync against live Cloud Run URL (with HUB_AUTH_TOKEN)
#   4. platform_agent.adk_orchestrator --mode rest against live Agent Registry v1
#   5. Optional secondary region /health, /v1/catalog, and hub_sync failover test

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
STATE_FILE=""
MIDDLEWARE_URL=""
SECONDARY_MIDDLEWARE_URL=""
NAME_PREFIX=""
DRY_RUN="false"

usage() {
  cat <<'EOF'
Usage: scripts/validate_live_gcp.sh [OPTIONS]

Validate a live Enterprise Agent Marketplace GCP deployment end-to-end using
authenticated gcloud identity and OAuth access tokens.

Options:
  --project ID                      Google Cloud project ID (or $GCP_PROJECT_ID)
  --primary-region REGION           Primary GCP region (default: asia-southeast3)
  --secondary-region REGION         Secondary GCP region (default: asia-southeast1)
  --skills-location LOC             Agent Registry Skills location: global|us|eu (default: global)
  --enable-secondary                Validate secondary region + hub_sync failover
  --state-file PATH                 Deployment manifest JSON from deploy_gcp.sh
  --middleware-url URL              Override primary Middleware HTTPS URL
  --secondary-middleware-url URL    Override secondary Middleware HTTPS URL
  --name-prefix PREFIX              Optional service name prefix (when not using state file)
  --dry-run                         Print planned checks without calling live endpoints
  -h, --help                        Show this help message and exit
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
    --state-file)
      STATE_FILE="$2"
      shift 2
      ;;
    --middleware-url)
      MIDDLEWARE_URL="$2"
      shift 2
      ;;
    --secondary-middleware-url)
      SECONDARY_MIDDLEWARE_URL="$2"
      ENABLE_SECONDARY="true"
      shift 2
      ;;
    --name-prefix)
      NAME_PREFIX="$2"
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
if [[ -z "${GCLOUD}" && "${DRY_RUN}" != "true" ]]; then
  echo "ERROR: gcloud CLI not found on PATH or at \$HOME/.local/bin/gcloud." >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: Python virtualenv not found at '${PYTHON_BIN}'." >&2
  exit 1
fi

if [[ -z "${PROJECT_ID}" && -n "${STATE_FILE}" && -f "${STATE_FILE}" ]]; then
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

A2A_PRIMARY_URL=""
if [[ -n "${STATE_FILE}" && -f "${STATE_FILE}" ]]; then
  eval "$("${PYTHON_BIN}" - "${STATE_FILE}" "${PRIMARY_REGION}" "${SECONDARY_REGION}" <<'PYEOF'
import json, shlex, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
pri, sec = sys.argv[2], sys.argv[3]
regions = doc.get("regions", {})
pri_mw = regions.get(pri, {}).get("cloud_run", {}).get("polyglot_middleware", {}).get("url", "")
pri_a2a = regions.get(pri, {}).get("cloud_run", {}).get("sample_a2a_agent", {}).get("url", "")
sec_mw = regions.get(sec, {}).get("cloud_run", {}).get("polyglot_middleware", {}).get("url", "")
sec_en = "true" if doc.get("enable_secondary") else "false"
skills_loc = doc.get("skills_location", "")
print(f"STATE_MW_URL={shlex.quote(pri_mw)}")
print(f"STATE_A2A_URL={shlex.quote(pri_a2a)}")
print(f"STATE_SEC_MW_URL={shlex.quote(sec_mw)}")
print(f"STATE_ENABLE_SEC={shlex.quote(sec_en)}")
print(f"STATE_SKILLS_LOC={shlex.quote(skills_loc)}")
PYEOF
)"
  [[ -z "${MIDDLEWARE_URL}" && -n "${STATE_MW_URL:-}" ]] && MIDDLEWARE_URL="${STATE_MW_URL}"
  [[ -n "${STATE_A2A_URL:-}" ]] && A2A_PRIMARY_URL="${STATE_A2A_URL}"
  [[ -z "${SECONDARY_MIDDLEWARE_URL}" && -n "${STATE_SEC_MW_URL:-}" ]] && SECONDARY_MIDDLEWARE_URL="${STATE_SEC_MW_URL}"
  [[ "${ENABLE_SECONDARY}" == "false" && "${STATE_ENABLE_SEC:-false}" == "true" ]] && ENABLE_SECONDARY="true"
  [[ "${SKILLS_LOCATION_EXPLICIT}" == "false" && -n "${STATE_SKILLS_LOC:-}" ]] && SKILLS_LOCATION="${STATE_SKILLS_LOC}"
fi

PREFIX_DASH=""
[[ -n "${NAME_PREFIX}" ]] && PREFIX_DASH="${NAME_PREFIX%-}-"
MW_SVC_NAME="${PREFIX_DASH}polyglot-middleware"
A2A_SVC_NAME="${PREFIX_DASH}sample-a2a-agent"

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "========================================================================"
  echo "[DRY-RUN] Live GCP End-to-End Validation Plan"
  echo "========================================================================"
  echo "Project          : ${PROJECT_ID}"
  echo "Primary Region   : ${PRIMARY_REGION}"
  echo "Secondary Region : ${SECONDARY_REGION} (enabled=${ENABLE_SECONDARY})"
  echo "Skills Location  : ${SKILLS_LOCATION}"
  echo "Checks:"
  echo "  1. Primary ${PRIMARY_REGION} /health, /v1/catalog (shared ${SKILLS_LOCATION} skills + ${PRIMARY_REGION} MCP/A2A), 4x /v1/marketplace/*.json, plugin ZIP SHA-256, POST /mcp"
  echo "  2. Direct Agent Registry v1 ?alt=media 302 redirect header-handling & SHA-256 check (locations/${SKILLS_LOCATION}/skills)"
  echo "  3. tools.hub_sync sync against live primary Middleware with HUB_AUTH_TOKEN into isolated temp HOME"
  echo "  4. platform_agent.adk_orchestrator --mode rest against https://agentregistry.googleapis.com/v1 (${PRIMARY_REGION}, skills=${SKILLS_LOCATION})"
  if [[ "${ENABLE_SECONDARY}" == "true" ]]; then
    echo "  5. Secondary ${SECONDARY_REGION} /health, /v1/catalog (shared ${SKILLS_LOCATION} skills + ${SECONDARY_REGION} MCP/A2A), and tools.hub_sync --fallback-middleware-url failover"
  fi
  exit 0
fi

if [[ -z "${MIDDLEWARE_URL}" ]]; then
  MIDDLEWARE_URL="$("${GCLOUD}" run services describe "${MW_SVC_NAME}" --project="${PROJECT_ID}" --region="${PRIMARY_REGION}" --format="value(status.url)" --quiet)"
fi
if [[ -z "${A2A_PRIMARY_URL}" ]]; then
  A2A_PRIMARY_URL="$("${GCLOUD}" run services describe "${A2A_SVC_NAME}" --project="${PROJECT_ID}" --region="${PRIMARY_REGION}" --format="value(status.url)" --quiet 2>/dev/null || true)"
fi
if [[ "${ENABLE_SECONDARY}" == "true" && -z "${SECONDARY_MIDDLEWARE_URL}" ]]; then
  SECONDARY_MIDDLEWARE_URL="$("${GCLOUD}" run services describe "${MW_SVC_NAME}" --project="${PROJECT_ID}" --region="${SECONDARY_REGION}" --format="value(status.url)" --quiet)"
fi

if [[ -z "${MIDDLEWARE_URL}" ]]; then
  echo "ERROR: Could not resolve primary Middleware URL. Pass --middleware-url or --state-file." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d /tmp/workshop-live-validate.XXXXXX)"
trap 'rm -rf "${TMP_DIR}"' EXIT

ID_TOKEN="$("${GCLOUD}" auth print-identity-token --quiet)"
ACCESS_TOKEN="$("${GCLOUD}" auth print-access-token --quiet)"

cd "${REPO_ROOT}"

echo "========================================================================"
echo "Enterprise Agent Marketplace — Live GCP End-to-End Validation"
echo "========================================================================"
echo "Project             : ${PROJECT_ID}"
echo "Primary Middleware  : ${MIDDLEWARE_URL} (${PRIMARY_REGION})"
echo "Skills Location     : ${SKILLS_LOCATION}"
if [[ "${ENABLE_SECONDARY}" == "true" ]]; then
  echo "Secondary Middleware: ${SECONDARY_MIDDLEWARE_URL} (${SECONDARY_REGION})"
fi
echo ""

# ----------------------------------------------------------------------------
# Check 1: Primary Middleware endpoints (/health, /v1/catalog, 4x manifests, ZIPs, /mcp)
# ----------------------------------------------------------------------------
echo "[1/5] Validating Primary Middleware (${PRIMARY_REGION})..."

"${PYTHON_BIN}" - "${MIDDLEWARE_URL}" "${PRIMARY_REGION}" "${SKILLS_LOCATION}" "${ID_TOKEN}" "${A2A_PRIMARY_URL}" <<'PYEOF'
import hashlib, io, sys, zipfile
import httpx

mw_url = sys.argv[1].rstrip("/")
expected_region = sys.argv[2]
expected_skills_loc = sys.argv[3]
id_token = sys.argv[4]
a2a_url = sys.argv[5].rstrip("/")
headers = {"Authorization": f"Bearer {id_token}"}

with httpx.Client(timeout=60.0, headers=headers) as client:
    # Note: Google Frontend (GFE) reserves /healthz on *.run.app; use /health externally.
    r = client.get(f"{mw_url}/health")
    assert r.status_code == 200, f"/health failed ({r.status_code}): {r.text}"
    health = r.json()
    assert health.get("status") == "ok", f"Unexpected /health: {health}"
    assert (health.get("region") or health.get("location")) == expected_region, (
        f"Expected region={expected_region!r}, got {health}"
    )
    assert health.get("skillsLocation") == expected_skills_loc, (
        f"Expected skillsLocation={expected_skills_loc!r}, got {health}"
    )
    print(
        f"  [PASS] GET /health -> region={health.get('region', health.get('location'))}, "
        f"skillsLocation={health.get('skillsLocation')}"
    )

    r = client.get(f"{mw_url}/v1/catalog")
    assert r.status_code == 200, f"/v1/catalog failed ({r.status_code}): {r.text}"
    cat = r.json()
    assert cat.get("marketplace", {}).get("skillsLocation") == expected_skills_loc, (
        f"Expected marketplace.skillsLocation={expected_skills_loc!r}, got {cat.get('marketplace')}"
    )
    plugins = cat.get("plugins", [])
    skills = cat.get("skills", [])
    mcp_servers = cat.get("mcpServers", [])
    agents = cat.get("agents", [])
    assert len(plugins) > 0, f"Expected >0 verified plugins, got {cat}"
    assert len(skills) > 0, f"Expected >0 verified skills, got {cat}"
    assert all(f"/locations/{expected_skills_loc}/skills/private-" in str(s.get("name", "")) for s in skills), (
        f"Expected all catalog skills to be private skills in {expected_skills_loc}: {[s.get('name') for s in skills]}"
    )
    assert len(mcp_servers) > 0 and all(f"/locations/{expected_region}/" in str(m.get("name", "")) for m in mcp_servers), (
        f"Expected regional MCP servers in {expected_region}: {[m.get('name') for m in mcp_servers]}"
    )
    assert len(agents) > 0 and all(f"/locations/{expected_region}/" in str(a.get("name", "")) for a in agents), (
        f"Expected regional A2A agents in {expected_region}: {[a.get('name') for a in agents]}"
    )
    finops_plugin = next((p for p in plugins if p.get("name") == "gcp-finops-toolkit"), None)
    if finops_plugin is not None:
        assert any(f"/locations/{expected_region}/" in str(m) for m in finops_plugin.get("mcpServers", [])), (
            f"Expected gcp-finops-toolkit to link {expected_region} MCP server: {finops_plugin}"
        )
        assert any(f"/locations/{expected_region}/" in str(a) for a in finops_plugin.get("agents", [])), (
            f"Expected gcp-finops-toolkit to link {expected_region} A2A agent: {finops_plugin}"
        )
    plugin_names = [p["name"] for p in plugins]
    print(
        f"  [PASS] GET /v1/catalog -> {len(plugins)} plugins ({plugin_names}), "
        f"{len(skills)} verified skills ({expected_skills_loc}), "
        f"{len(mcp_servers)} MCP + {len(agents)} A2A ({expected_region})"
    )

    for ide in ("claude", "copilot", "codex", "antigravity"):
        mr = client.get(f"{mw_url}/v1/marketplace/{ide}.json")
        assert mr.status_code == 200, f"/v1/marketplace/{ide}.json failed ({mr.status_code})"
        assert len(mr.json().get("plugins", [])) > 0, f"{ide}.json returned 0 plugins"
    print("  [PASS] GET /v1/marketplace/{claude,copilot,codex,antigravity}.json")

    zip_targets = [p for p in ("gcp-finops-toolkit", "local-ops-toolkit") if p in plugin_names]
    assert zip_targets, f"Expected gcp-finops-toolkit or local-ops-toolkit in {plugin_names}"
    for pname in zip_targets:
        zr = client.get(f"{mw_url}/v1/plugins/{pname}.zip")
        assert zr.status_code == 200, f"/v1/plugins/{pname}.zip failed ({zr.status_code})"
        hdr_sha = (zr.headers.get("X-Bundle-Sha256") or "").strip().lower()
        actual_sha = hashlib.sha256(zr.content).hexdigest()
        assert hdr_sha == actual_sha, f"X-Bundle-Sha256 mismatch for {pname}: {hdr_sha} != {actual_sha}"
        with zipfile.ZipFile(io.BytesIO(zr.content)) as zf:
            assert "plugin.json" in zf.namelist()
    print(f"  [PASS] Verified plugin ZIPs & X-Bundle-Sha256: {', '.join(zip_targets)}")

    # MCP Gateway checks
    ir = client.post(f"{mw_url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
    assert ir.status_code == 200 and "result" in ir.json()

    lr = client.post(f"{mw_url}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert lr.status_code == 200
    t_names = {t["name"] for t in lr.json()["result"]["tools"]}
    for req in ("search_catalog", "load_verified_skill", "list_governed_mcp_servers", "call_remote_a2a_agent"):
        assert req in t_names, f"Missing MCP tool {req}"

    workshop_skill = next(
        (
            s.get("frontmatter", {}).get("name")
            for s in skills
            if s.get("frontmatter", {}).get("name") == "cost-anomaly-triage"
        ),
        None,
    )
    first_skill = workshop_skill or skills[0].get("frontmatter", {}).get("name") or "cost-anomaly-triage"
    sr = client.post(
        f"{mw_url}/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "load_verified_skill", "arguments": {"skill_name": first_skill}}},
    )
    assert sr.status_code == 200
    sres = sr.json()["result"]
    assert sres.get("isError") is False, f"load_verified_skill error: {sres}"
    assert sres["structuredContent"]["instructions"]
    print(f"  [PASS] POST /mcp initialize, tools/list, load_verified_skill({first_skill})")

    agents = cat.get("agents", [])
    if agents:
        finops_agent = next(
            (
                a
                for a in agents
                if str(a.get("agentId", "")).endswith(":finops-advisor-agent")
                or a.get("displayName") == "GCP FinOps Advisor"
                or "finops-advisor-agent" in str(a.get("name", ""))
            ),
            None,
        )
        agent_target = (finops_agent or agents[0]).get("name") or "finops-advisor-agent"
        ar = client.post(
            f"{mw_url}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "call_remote_a2a_agent",
                    "arguments": {"agent_name": agent_target, "task": "Summarize FinOps cost anomaly triage steps."},
                },
            },
        )
        assert ar.status_code == 200
        ares = ar.json()["result"]
        assert ares.get("isError") is False, (
            f"POST /mcp call_remote_a2a_agent({agent_target}) failed (ensure middleware SA "
            f"has roles/run.invoker and OIDC ID token auth is enabled): {ares}"
        )
        a2a_state = (
            ares.get("structuredContent", {})
            .get("response", {})
            .get("result", {})
            .get("status", {})
            .get("state")
        )
        assert a2a_state == "completed", f"Unexpected A2A response state: {ares}"
        print(f"  [PASS] POST /mcp call_remote_a2a_agent({agent_target}) -> completed (Cloud Run OIDC authenticated)")
PYEOF

# ----------------------------------------------------------------------------
# Check 2: Direct Agent Registry v1 ?alt=media HTTP 302 header-stripping check
# ----------------------------------------------------------------------------
echo ""
echo "[2/5] Verifying direct Agent Registry v1 ?alt=media 302 redirect header stripping (locations/${SKILLS_LOCATION}/skills)..."

"${PYTHON_BIN}" - "${PROJECT_ID}" "${SKILLS_LOCATION}" "${ACCESS_TOKEN}" <<'PYEOF'
import hashlib, hmac, sys
from urllib.parse import urljoin
import httpx

project_id, skills_location, token = sys.argv[1:4]
base = "https://agentregistry.googleapis.com/v1"
api_headers = {
    "Authorization": f"Bearer {token}",
    "x-goog-user-project": project_id,
    "x-goog-api-client": "workshop-live-validator/1.0",
    "Accept": "application/json",
}

with httpx.Client(timeout=30.0, follow_redirects=False) as client:
    all_skills = []
    page_token = ""
    for _ in range(20):
        params = {"pageSize": "200"}
        if page_token:
            params["pageToken"] = page_token
        lr = client.get(
            f"{base}/projects/{project_id}/locations/{skills_location}/skills",
            params=params,
            headers=api_headers,
        )
        assert lr.status_code == 200, f"list_skills failed ({lr.status_code}): {lr.text}"
        payload = lr.json()
        all_skills.extend(payload.get("skills", []))
        next_token = str(payload.get("nextPageToken") or "").strip()
        if not next_token or next_token == page_token:
            break
        page_token = next_token

    active = [
        s
        for s in all_skills
        if "/skills/private-" in str(s.get("name", ""))
        and s.get("state") in ("STATE_ACTIVE", "ACTIVE")
        and s.get("defaultRevision")
    ]
    assert active, (
        f"No private ACTIVE skill with defaultRevision found in locations/{skills_location}/skills "
        f"(scanned {len(all_skills)} total skills)"
    )
    target_rev = active[0]["defaultRevision"]

    rr = client.get(f"{base}/{target_rev.lstrip('/')}", headers=api_headers)
    assert rr.status_code == 200, f"get_skill_revision failed ({rr.status_code}): {rr.text}"
    expected_sha = str(rr.json().get("sha256Hash", "")).strip().lower()
    if expected_sha.startswith("sha256:"):
        expected_sha = expected_sha[7:]
    assert len(expected_sha) == 64, f"Unexpected sha256Hash: {expected_sha!r}"

    media_url = f"{base}/{target_rev.lstrip('/')}?alt=media"
    media_headers = dict(api_headers)
    media_headers["Accept"] = "application/zip, application/octet-stream, */*"
    r = client.get(media_url, headers=media_headers)
    assert r.status_code in (301, 302, 303, 307, 308), (
        f"Expected 302 redirect from ?alt=media, got HTTP {r.status_code}"
    )

    def _origin(u: httpx.URL) -> tuple[str, str, int | None]:
        scheme = u.scheme.lower()
        port = u.port or (443 if scheme == "https" else (80 if scheme == "http" else None))
        return (scheme, (u.host or "").lower(), port)

    initial_url = httpx.URL(str(r.request.url))
    hops = 0
    hop_notes = []
    cur_headers = dict(media_headers)
    while r.status_code in (301, 302, 303, 307, 308):
        hops += 1
        assert hops <= 5, f"Exceeded max redirects ({hops})"
        loc = r.headers.get("location")
        assert loc, "Redirect response missing Location header"
        next_url_str = urljoin(str(r.request.url), loc)
        next_u = httpx.URL(next_url_str)
        qkeys = {k.lower() for k in next_u.params.keys()}
        same_origin_download = (
            _origin(initial_url) == _origin(next_u)
            and (next_u.path or "").lower().startswith("/download/")
            and "/scotty-signed/" not in (next_u.path or "").lower()
            and not (qkeys & {"x-goog-signature", "signature", "googleaccessid", "x-goog-credential"})
        )
        if same_origin_download:
            next_headers = dict(cur_headers)
            hop_notes.append("same-origin /download (auth preserved)")
        else:
            next_headers = {
                k: v for k, v in cur_headers.items()
                if k.lower() != "authorization" and not k.lower().startswith("x-goog-")
            }
            hop_notes.append("cross-origin/signed (auth & x-goog-* stripped)")
        r = client.get(next_url_str, headers=next_headers)
        cur_headers = next_headers

    assert r.status_code == 200, f"?alt=media redirect download failed: HTTP {r.status_code}: {r.text}"
    actual_sha = hashlib.sha256(r.content).hexdigest()
    assert hmac.compare_digest(actual_sha, expected_sha), f"SHA-256 mismatch: {actual_sha} != {expected_sha}"
    print(f"  [PASS] ?alt=media 302 redirect ({'; '.join(hop_notes)}) + SHA-256 verified ({actual_sha[:12]}...)")
PYEOF

# ----------------------------------------------------------------------------
# Check 3: tools.hub_sync sync against live primary Middleware
# ----------------------------------------------------------------------------
echo ""
echo "[3/5] Running tools.hub_sync sync against live primary Middleware..."

LIVE_HOME="${TMP_DIR}/hub-primary"
mkdir -p "${LIVE_HOME}"

HUB_AUTH_TOKEN="${ID_TOKEN}" "${PYTHON_BIN}" -m tools.hub_sync sync \
  --middleware-url "${MIDDLEWARE_URL}" \
  --home "${LIVE_HOME}" \
  --tools all \
  --no-exec \
  >"${TMP_DIR}/hub_primary.out"

"${PYTHON_BIN}" -m tools.hub_sync status --home "${LIVE_HOME}" --json >"${TMP_DIR}/hub_primary_status.json"
"${PYTHON_BIN}" - "${TMP_DIR}/hub_primary_status.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
assert doc["synced"] is True
assert len(doc.get("plugins", {})) > 0, f"Expected synced plugins, got {doc}"
print(f"  [PASS] tools.hub_sync hydrated {len(doc['plugins'])} plugin(s) into isolated temp HOME")
PYEOF

# ----------------------------------------------------------------------------
# Check 4: platform_agent.adk_orchestrator --mode rest against live Agent Registry v1
# ----------------------------------------------------------------------------
echo ""
echo "[4/5] Running platform_agent.adk_orchestrator --mode rest against live Agent Registry v1..."

AGENT_REGISTRY_ACCESS_TOKEN="${ACCESS_TOKEN}" "${PYTHON_BIN}" -m platform_agent.adk_orchestrator \
  --mode rest \
  --project-id "${PROJECT_ID}" \
  --location "${PRIMARY_REGION}" \
  --skills-location "${SKILLS_LOCATION}" \
  --base-url "https://agentregistry.googleapis.com/v1" \
  >"${TMP_DIR}/platform_live.json"

"${PYTHON_BIN}" - "${TMP_DIR}/platform_live.json" "${SKILLS_LOCATION}" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
expected_skills_loc = sys.argv[2]
assert len(doc.get("skills", [])) > 0, f"Expected >=1 discovered skill, got {doc}"
assert all(
    f"/locations/{expected_skills_loc}/skills/private-" in str(s.get("name", ""))
    for s in doc["skills"]
), f"Expected private skills in {expected_skills_loc}, got {[s.get('name') for s in doc['skills']]}"
assert len(doc.get("extracted_skills", [])) > 0, f"Expected >=1 ?alt=media extracted skill, got {doc}"
print(
    f"  [PASS] platform_agent discovered {len(doc['skills'])} private skill(s) ({expected_skills_loc}), "
    f"downloaded {len(doc['extracted_skills'])}, {len(doc.get('mcp_servers', []))} MCP server(s), "
    f"{len(doc.get('agents', []))} A2A agent(s)"
)
PYEOF

# ----------------------------------------------------------------------------
# Check 5: Optional Secondary Region Validation + Regional Failover Test
# ----------------------------------------------------------------------------
echo ""
if [[ "${ENABLE_SECONDARY}" == "true" ]]; then
  echo "[5/5] Validating Secondary Region (${SECONDARY_REGION}) & Regional Failover..."

  "${PYTHON_BIN}" - "${SECONDARY_MIDDLEWARE_URL}" "${SECONDARY_REGION}" "${SKILLS_LOCATION}" "${ID_TOKEN}" <<'PYEOF'
import sys, httpx

sec_url = sys.argv[1].rstrip("/")
expected_region = sys.argv[2]
expected_skills_loc = sys.argv[3]
id_token = sys.argv[4]
headers = {"Authorization": f"Bearer {id_token}"}

with httpx.Client(timeout=60.0, headers=headers) as client:
    r = client.get(f"{sec_url}/health")
    assert r.status_code == 200, f"Secondary /health failed ({r.status_code}): {r.text}"
    health = r.json()
    assert (health.get("region") or health.get("location")) == expected_region, (
        f"Expected secondary region={expected_region!r}, got {health}"
    )
    assert health.get("skillsLocation") == expected_skills_loc, (
        f"Expected secondary skillsLocation={expected_skills_loc!r}, got {health}"
    )
    c = client.get(f"{sec_url}/v1/catalog")
    assert c.status_code == 200, f"Secondary /v1/catalog failed ({c.status_code}): {c.text}"
    cat = c.json()
    assert cat.get("marketplace", {}).get("skillsLocation") == expected_skills_loc, (
        f"Expected secondary marketplace.skillsLocation={expected_skills_loc!r}, got {cat.get('marketplace')}"
    )
    skills = cat.get("skills", [])
    mcp_servers = cat.get("mcpServers", [])
    agents = cat.get("agents", [])
    assert len(skills) > 0, f"Expected >0 skills in secondary region, got {cat}"
    assert all(f"/locations/{expected_skills_loc}/skills/private-" in str(s.get("name", "")) for s in skills), (
        f"Expected shared private skills in {expected_skills_loc}, got {[s.get('name') for s in skills]}"
    )
    assert len(mcp_servers) > 0 and all(f"/locations/{expected_region}/" in str(m.get("name", "")) for m in mcp_servers), (
        f"Expected secondary MCP servers in {expected_region}: {[m.get('name') for m in mcp_servers]}"
    )
    assert len(agents) > 0 and all(f"/locations/{expected_region}/" in str(a.get("name", "")) for a in agents), (
        f"Expected secondary A2A agents in {expected_region}: {[a.get('name') for a in agents]}"
    )
    finops_plugin = next((p for p in cat.get("plugins", []) if p.get("name") == "gcp-finops-toolkit"), None)
    if finops_plugin is not None:
        assert any(f"/locations/{expected_region}/" in str(m) for m in finops_plugin.get("mcpServers", [])), (
            f"Expected secondary gcp-finops-toolkit to link {expected_region} MCP server: {finops_plugin}"
        )
        assert any(f"/locations/{expected_region}/" in str(a) for a in finops_plugin.get("agents", [])), (
            f"Expected secondary gcp-finops-toolkit to link {expected_region} A2A agent: {finops_plugin}"
        )
    print(
        f"  [PASS] Secondary {expected_region} /health & /v1/catalog "
        f"({len(skills)} shared {expected_skills_loc} skills + {len(mcp_servers)} MCP + {len(agents)} A2A in {expected_region})"
    )
PYEOF

  FAILOVER_HOME="${TMP_DIR}/hub-failover"
  mkdir -p "${FAILOVER_HOME}"
  HUB_AUTH_TOKEN="${ID_TOKEN}" "${PYTHON_BIN}" -m tools.hub_sync sync \
    --middleware-url "https://invalid.local" \
    --fallback-middleware-url "${SECONDARY_MIDDLEWARE_URL}" \
    --home "${FAILOVER_HOME}" \
    --tools all \
    --no-exec \
    >"${TMP_DIR}/hub_failover.out"

  "${PYTHON_BIN}" - "${FAILOVER_HOME}" "${SECONDARY_MIDDLEWARE_URL}" <<'PYEOF'
import json, sys
from pathlib import Path
manifest = json.loads((Path(sys.argv[1]) / ".acme-agent-hub" / ".sync-manifest.json").read_text(encoding="utf-8"))
expected_url = sys.argv[2].rstrip("/")
assert manifest.get("middleware_url", "").rstrip("/") == expected_url, f"Failover manifest mismatch: {manifest}"
assert len(manifest.get("owned_plugins", {})) > 0
print(f"  [PASS] tools.hub_sync failed over seamlessly from https://invalid.local -> {expected_url}")
PYEOF
else
  echo "[5/5] Secondary region failover check skipped (pass --enable-secondary to include ${SECONDARY_REGION})."
fi

echo ""
echo "========================================================================"
echo "LIVE GCP VALIDATION PASSED!"
echo "========================================================================"
