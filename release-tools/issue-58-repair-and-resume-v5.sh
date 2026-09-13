#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

REPO="/opt/city-manager-os"
ACCEPTED_HEAD="aa19402d164b0fcc53891c873a534043fa50627b"
BOOTSTRAP_HEAD="bded35024236039f89cc34e2030570b12ce87777"
FAILED_RELEASE_HEAD="8cb53f440328678b2ffdd5f0d4528d1db884043a"
APPLICATION_TEST_FAILURE_HEAD="cff2737eba74bc9997d054c706a5496745e90a77"
PACKAGING_FAILURE_HEAD="45cc74598e077be05b4bee98f6b6763f13af6402"
ACCEPTANCE_FAILURE_HEAD="68934f25f0839ab14875c084f73bb39d5861cbe8"
REPORT_BRANCH="release-output/58"
RELEASE_ID="issue-58-regional-spatial-reference-v1"
REPAIR_ID="issue-58-permanent-transit-reference-repair-v5"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
STATE_DIR="/var/lib/city-manager-os/releases"
LOG_FILE="${LOG_DIR}/issue-58-repair-${RUN_ID}.log"
STATE_FILE="${STATE_DIR}/issue-58.state"
LOCK_FILE="/var/lock/cmos-issue-58-release.lock"
CURRENT_PHASE="startup"
FAIL_LINE=""
TARGET_HEAD=""
REPAIR_MODE=""
DATABASE_ACTION="not-started"
PRIVILEGE_ACTION="not-started"
CATALOG_REFRESH_ACTION="not-started"
CATALOG_FUNCTION_ACTION="not-started"
APPLICATION_ACTION="not-started"
E2E_ACTION="not-started"
FINAL_STATUS="FAIL"
ROLLBACK_DASHBOARD_TAG="dashboard-citymanager-dashboard:cmos58-v4-rollback-dashboard"
ROLLBACK_LATEST_TAG="dashboard-citymanager-dashboard:cmos58-v4-rollback-latest"
CANDIDATE_ACTIVE=0

INITIAL_RELEASE_PATHS=$'dashboard/map_app.py\ndashboard/phase3_app.py\ndashboard/spatial_reference_app.py\ndashboard/templates/nav.html\ndashboard/templates/spatial_reference.html\ndashboard/templates/spatial_reference_detail.html\ndashboard/tests/test_spatial_reference_catalog.py\ndeploy/gis/install_spatial_reference_catalog.sh\ndeploy/postgis/init/031_spatial_reference_catalog.sql\ndocs/SPATIAL_REFERENCE_CATALOG.md'
FINAL_RELEASE_PATHS=$'dashboard/Dockerfile\ndashboard/map_app.py\ndashboard/phase3_app.py\ndashboard/spatial_reference_app.py\ndashboard/templates/nav.html\ndashboard/templates/spatial_reference.html\ndashboard/templates/spatial_reference_detail.html\ndashboard/tests/test_spatial_reference_catalog.py\ndeploy/gis/install_spatial_reference_catalog.sh\ndeploy/postgis/init/031_spatial_reference_catalog.sql\ndocs/SPATIAL_REFERENCE_CATALOG.md'
REPAIR_PATHS=$'dashboard/tests/test_spatial_reference_catalog.py\ndeploy/gis/install_spatial_reference_catalog.sh\ndeploy/postgis/init/031_spatial_reference_catalog.sql'
FINAL_REPAIR_PATHS=$'dashboard/Dockerfile\ndashboard/tests/test_spatial_reference_catalog.py\ndeploy/gis/install_spatial_reference_catalog.sh\ndeploy/postgis/init/031_spatial_reference_catalog.sql\ndocs/SPATIAL_REFERENCE_CATALOG.md'
TEST_REPAIR_PATH="dashboard/tests/test_spatial_reference_catalog.py"
PACKAGING_REPAIR_PATH="dashboard/Dockerfile"
PERMANENT_REFERENCE_REPAIR_PATHS=$'dashboard/tests/test_spatial_reference_catalog.py\ndeploy/postgis/init/031_spatial_reference_catalog.sql\ndocs/SPATIAL_REFERENCE_CATALOG.md'

umask 077
[[ "$(id -u)" == 0 ]] || { printf 'ERROR: run the #58 repair as root\n' >&2; exit 1; }
mkdir -p "$LOG_DIR" "$STATE_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

redact(){
  sed -E \
    -e 's#(Bearer|Basic)[[:space:]]+[^[:space:]]+#\1 [REDACTED]#Ig' \
    -e 's#(https?://)[^/@[:space:]]+:[^/@[:space:]]+@#\1[REDACTED]@#Ig' \
    -e 's#(github_pat_|gh[pousr]_)[[:alnum:]_]+#[REDACTED_GITHUB_TOKEN]#g' \
    -e "s#((authorization|cookie|password|passwd|secret|token|api[_-]?key|ntfy[_-]?topic)[[:space:]\"']*[=:][[:space:]\"']*)[^,;[:space:]\"']+#\1[REDACTED]#Ig" \
    -e 's#[[:alnum:]._%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,}#[REDACTED_EMAIL]#g' \
    -e 's#(^|[^0-9])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9]|$)#\1[REDACTED_IP]\3#g'
}

safe_git_value(){
  local value=""
  if [[ -d "$REPO/.git" || -f "$REPO/.git" ]]; then
    value="$(git -C "$REPO" "$@" 2>/dev/null || true)"
  fi
  printf '%s' "${value:-unavailable}"
}

publish_report(){ (
  set +e
  local status="$1" rc="$2" report_parent="" report_worktree="" report_dir report_file latest_file
  local raw_sha head_sha origin_sha changed
  report_cleanup(){
    if [[ -n "$report_worktree" && "$report_worktree" == /tmp/cmos58-report.* ]]; then
      git -C "$REPO" worktree remove --force "$report_worktree" >/dev/null 2>&1 || true
    fi
    [[ -n "$report_parent" && "$report_parent" == /tmp/cmos58-report.* ]] \
      && rmdir "$report_parent" >/dev/null 2>&1 || true
  }
  trap report_cleanup EXIT
  [[ -d "$REPO/.git" || -f "$REPO/.git" ]] || return 1
  command -v git >/dev/null && command -v sha256sum >/dev/null || return 1
  git -C "$REPO" fetch -q origin "refs/heads/${REPORT_BRANCH}:refs/remotes/origin/${REPORT_BRANCH}" || return 1
  report_parent="$(mktemp -d /tmp/cmos58-report.XXXXXX)"
  report_worktree="$report_parent/worktree"
  git -C "$REPO" worktree add --detach "$report_worktree" "origin/${REPORT_BRANCH}" >/dev/null 2>&1 || return 1
  report_dir="$report_worktree/release-results/issue-58"
  mkdir -p "$report_dir"
  report_file="$report_dir/${RUN_ID}-${status,,}-repair.md"
  latest_file="$report_dir/latest.md"
  raw_sha="$(sha256sum "$LOG_FILE" | awk '{print $1}')"
  head_sha="$(safe_git_value rev-parse HEAD)"
  origin_sha="$(safe_git_value rev-parse origin/main)"
  changed="$(safe_git_value status --short | redact)"
  {
    printf '# City Manager OS issue #58 repair report\n\n'
    printf '> This repository is public. This report is intentionally redacted. The complete mode-600 log remains on the VPS.\n\n'
    printf '| Field | Value |\n|---|---|\n'
    printf '| Run | `%s` |\n' "$RUN_ID"
    printf '| Repair | `%s` |\n' "$REPAIR_ID"
    printf '| Status | **%s** |\n' "$status"
    printf '| Exit code | `%s` |\n' "$rc"
    printf '| Failed line | `%s` |\n' "${FAIL_LINE:-none}"
    printf '| Phase | `%s` |\n' "$CURRENT_PHASE"
    printf '| Repair mode | `%s` |\n' "${REPAIR_MODE:-unavailable}"
    printf '| Database action | `%s` |\n' "$DATABASE_ACTION"
    printf '| Privilege action | `%s` |\n' "$PRIVILEGE_ACTION"
    printf '| Catalog refresh | `%s` |\n' "$CATALOG_REFRESH_ACTION"
    printf '| Catalog function | `%s` |\n' "$CATALOG_FUNCTION_ACTION"
    printf '| Application action | `%s` |\n' "$APPLICATION_ACTION"
    printf '| E2E action | `%s` |\n' "$E2E_ACTION"
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '| Accepted base | `%s` |\n' "$ACCEPTED_HEAD"
    printf '| Bootstrap base | `%s` |\n' "$BOOTSTRAP_HEAD"
    printf '| Failed release | `%s` |\n' "$FAILED_RELEASE_HEAD"
    printf '| Application-test failure | `%s` |\n' "$APPLICATION_TEST_FAILURE_HEAD"
    printf '| Dashboard-packaging failure | `%s` |\n' "$PACKAGING_FAILURE_HEAD"
    printf '| Acceptance failure | `%s` |\n' "$ACCEPTANCE_FAILURE_HEAD"
    printf '| Target | `%s` |\n' "${TARGET_HEAD:-$head_sha}"
    printf '| Local HEAD | `%s` |\n' "$head_sha"
    printf '| Origin main | `%s` |\n' "$origin_sha"
    printf '| Full local log | `%s` |\n' "$LOG_FILE"
    printf '| Full log SHA-256 | `%s` |\n\n' "$raw_sha"
    printf '## Working tree\n\n```text\n%s\n```\n\n' "${changed:-clean}"
    printf '## Redacted diagnostic output\n\n```text\n'
    grep -Eai '(^|[[:space:]])(ERROR|FATAL|FAIL|FAILED|EXCEPTION|TRACEBACK|ASSERT|ASSERTIONERROR|SYNTAX|UNEXPECTED|MISMATCH|PASS|PLAN|READINESS|HEALTH|CATALOG|CONTEXT|FUNCTIONS|ROLLBACK)|^(LINE [0-9]+:|DETAIL:|HINT:|CONTEXT:|STATEMENT:|short test summary|E[[:space:]]|base=|target=|changed=|build=|services=|tests=|probes=|backup_required=|external=|full_e2e=|unknown=|DATABASE_|APPLICATION_|TARGET_HEAD=|#58)' "$LOG_FILE" \
      | grep -v '^FAILED_COMMAND=' | redact | tail -n 240 || true
    printf '```\n'
  } > "$report_file"
  chmod 600 "$report_file"
  install -m 600 "$report_file" "$latest_file"
  git -C "$report_worktree" add \
    "release-results/issue-58/$(basename "$report_file")" \
    "release-results/issue-58/$(basename "$latest_file")"
  git -C "$report_worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit \
    -m "Record #58 repair ${status,,} ${RUN_ID}" >/dev/null || return 1
  git -C "$report_worktree" push -q origin "HEAD:refs/heads/${REPORT_BRANCH}" || return 1
  log "REDACTED_GITHUB_REPORT=https://github.com/gioahmad/city-manager-os/blob/${REPORT_BRANCH}/release-results/issue-58/latest.md"
  return 0
) }

on_error(){
  local rc=$?
  FAIL_LINE="${BASH_LINENO[0]:-$LINENO}"
  log "ERROR: command failed rc=${rc} line=${FAIL_LINE} phase=${CURRENT_PHASE}"
  printf 'FAILED_COMMAND=%q\n' "$BASH_COMMAND"
  trap - ERR
  exit "$rc"
}

on_exit(){
  local rc=$?
  trap - ERR EXIT
  if (( rc != 0 && CANDIDATE_ACTIVE == 1 )) && declare -F restore_dashboard_candidate >/dev/null; then
    restore_dashboard_candidate || true
  fi
  if (( rc == 0 )); then FINAL_STATUS="PASS"; fi
  printf '\n============================================================\n'
  printf '#58 REPAIR: %s rc=%s phase=%s line=%s\n' "$FINAL_STATUS" "$rc" "$CURRENT_PHASE" "${FAIL_LINE:-none}"
  printf 'FULL_LOCAL_LOG=%s\n' "$LOG_FILE"
  printf 'FULL_LOCAL_LOG_MODE=600\n'
  printf 'GITHUB_REPORT=REDACTED\n'
  printf '============================================================\n'
  if ! publish_report "$FINAL_STATUS" "$rc"; then
    log "WARNING: redacted GitHub report could not be published; full local log retained at ${LOG_FILE}"
  fi
  exit "$rc"
}

trap on_error ERR
trap on_exit EXIT

state_set(){
  local key="$1" value="$2" temp="${STATE_FILE}.tmp"
  { [[ -f "$STATE_FILE" ]] && grep -v "^${key}=" "$STATE_FILE" || true; printf '%s=%s\n' "$key" "$value"; } > "$temp"
  mv -f "$temp" "$STATE_FILE"
  chmod 600 "$STATE_FILE"
}

db_at(){
  docker exec -i citymanager-postgis sh -lc \
    'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}

database_structure_ready(){
  [[ "$(db_at <<'SQL'
SELECT
  to_regclass('public.spatial_reference_entities') IS NOT NULL
  AND to_regclass('public.spatial_reference_catalog_status') IS NOT NULL
  AND (SELECT count(*)=3 FROM information_schema.columns
       WHERE table_schema='public' AND table_name='watch_items'
         AND column_name IN ('spatial_reference_entity_id','spatial_geom','spatial_scope'))
  AND to_regprocedure('public.gis_parcel_for_point(double precision,double precision,double precision)') IS NOT NULL
  AND to_regprocedure('public.gis_addresses_for_parcel(integer,double precision,integer)') IS NOT NULL
  AND to_regprocedure('public.gis_parcels_within_radius(integer,double precision,integer,double precision)') IS NOT NULL
  AND to_regprocedure('public.gis_parcels_within_radius(double precision,double precision,double precision,integer)') IS NOT NULL
  AND to_regprocedure('public.gis_adjoining_parcels(integer,double precision,integer)') IS NOT NULL
  AND to_regprocedure('public.gis_spatial_impact_context(geometry,double precision,interval)') IS NOT NULL
  AND to_regprocedure('public.gis_parcel_context(integer,double precision)') IS NOT NULL;
SQL
)" == t ]]
}

database_privileges_ready(){
  [[ "$(db_at <<'SQL'
SELECT
  has_table_privilege('citymanager_app','spatial_reference_entities','SELECT')
  AND has_table_privilege('citymanager_app','spatial_reference_entities','INSERT')
  AND has_table_privilege('citymanager_app','spatial_reference_entities','UPDATE')
  AND has_table_privilege('citymanager_app','spatial_reference_entities','DELETE')
  AND has_table_privilege('citymanager_app','spatial_reference_catalog_status','SELECT')
  AND has_function_privilege('citymanager_app','spatial_reference_refresh_local_sources()','EXECUTE')
  AND has_function_privilege('citymanager_app','gis_parcel_for_point(double precision,double precision,double precision)','EXECUTE')
  AND has_function_privilege('citymanager_app','gis_addresses_for_parcel(integer,double precision,integer)','EXECUTE')
  AND has_function_privilege('citymanager_app','gis_parcels_within_radius(integer,double precision,integer,double precision)','EXECUTE')
  AND has_function_privilege('citymanager_app','gis_parcels_within_radius(double precision,double precision,double precision,integer)','EXECUTE')
  AND has_function_privilege('citymanager_app','gis_adjoining_parcels(integer,double precision,integer)','EXECUTE')
  AND has_function_privilege('citymanager_app','gis_spatial_impact_context(geometry,double precision,interval)','EXECUTE')
  AND has_function_privilege('citymanager_app','gis_parcel_context(integer,double precision)','EXECUTE');
SQL
)" == t ]]
}

catalog_core_ready(){
  [[ "$(db_at <<'SQL'
SELECT
  EXISTS (SELECT 1 FROM spatial_reference_entities WHERE active=true)
  AND NOT EXISTS (
    SELECT 1 FROM spatial_reference_entities
    WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom) OR ST_SRID(geom)<>4326
  )
  AND NOT EXISTS (
    SELECT 1 FROM spatial_reference_entities
    WHERE source_record_id IS NOT NULL
    GROUP BY source_provider,source_record_id HAVING count(*)>1
  )
  AND (SELECT count(*) FROM spatial_reference_entities
       WHERE active=true AND source_provider='CMOS_TRANSIT_ASSET') =
      (SELECT count(*) FROM transit_assets
       WHERE active=true AND geom IS NOT NULL AND NOT ST_IsEmpty(geom)
         AND upper(coalesce(asset_type,''))<>'VEHICLE');
SQL
)" == t ]]
}

refresh_function_ready(){
  [[ "$(db_at <<'SQL'
SELECT coalesce((
  SELECT length(pg_get_functiondef(p.oid))-length(replace(pg_get_functiondef(p.oid),'VEHICLE','')) >= 14
  FROM pg_proc p
  WHERE p.oid=to_regprocedure('public.spatial_reference_refresh_local_sources()')
),false);
SQL
)" == t ]]
}

catalog_diagnostics(){
  db_at <<'SQL'
SELECT 'catalog_active=' || EXISTS (SELECT 1 FROM spatial_reference_entities WHERE active=true);
SELECT 'catalog_weehawken_parcel_link=' || EXISTS (
  SELECT 1 FROM spatial_reference_entities
  WHERE active=true AND upper(coalesce(municipality,'')) LIKE '%WEEHAWKEN%'
    AND parcel_objectid IS NOT NULL
);
SELECT 'catalog_geometry_valid=' || NOT EXISTS (
  SELECT 1 FROM spatial_reference_entities
  WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom) OR ST_SRID(geom)<>4326
);
SELECT 'catalog_sources_unique=' || NOT EXISTS (
  SELECT 1 FROM spatial_reference_entities
  WHERE source_record_id IS NOT NULL
  GROUP BY source_provider,source_record_id HAVING count(*)>1
);
SELECT 'catalog_transit_reconciled=' || (
  (SELECT count(*) FROM spatial_reference_entities
   WHERE active=true AND source_provider='CMOS_TRANSIT_ASSET') =
  (SELECT count(*) FROM transit_assets
   WHERE active=true AND geom IS NOT NULL AND NOT ST_IsEmpty(geom)
     AND upper(coalesce(asset_type,''))<>'VEHICLE')
);
SELECT 'catalog_counts=' || count(*) || '|' || count(*) FILTER (WHERE active) || '|' ||
       count(*) FILTER (WHERE parcel_objectid IS NOT NULL) || '|' ||
       count(*) FILTER (WHERE transit_asset_id IS NOT NULL)
FROM spatial_reference_entities;
SQL
}

parcel_context_ready(){
  database_structure_ready || return 1
  [[ "$(db_at <<'SQL'
SET ROLE citymanager_app;
WITH sample AS (
  SELECT objectid,ST_Y(ST_PointOnSurface(geom)) AS lat,ST_X(ST_PointOnSurface(geom)) AS lon
  FROM gis_parcels
  WHERE geom IS NOT NULL AND upper(coalesce(mun_name,'')) LIKE '%WEEHAWKEN%'
  ORDER BY objectid LIMIT 1
)
SELECT EXISTS (
  SELECT 1 FROM sample s
  WHERE gis_parcel_context(s.objectid,500.0) IS NOT NULL
    AND EXISTS (SELECT 1 FROM gis_parcel_for_point(s.lat,s.lon,3.0))
);
RESET ROLE;
SQL
)" == t ]]
}

database_ready(){
  database_structure_ready && database_privileges_ready && refresh_function_ready \
    && catalog_core_ready && parcel_context_ready
}

application_ready(){
  docker exec -i citymanager-dashboard python - "$RELEASE_ID" <<'PY' >/dev/null 2>&1
import json, os, sys, urllib.request
token = os.environ.get("CMOS_AUTOMATION_TOKEN", "").strip()
headers = {"X-CMOS-Automation-Key": token} if token else {}
request = urllib.request.Request("http://127.0.0.1:8000/api/spatial-reference/release", headers=headers)
with urllib.request.urlopen(request, timeout=10) as response:
    payload = json.load(response)
assert response.status == 200
assert payload.get("release_id") == sys.argv[1]
PY
}

wait_dashboard_ready(){
  local consecutive=0
  for _ in $(seq 1 30); do
    if [[ "$(docker inspect --format '{{.State.Running}}' citymanager-dashboard 2>/dev/null || true)" == true ]] \
      && docker exec citymanager-dashboard python -c \
        "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4); assert r.status==200" \
        >/dev/null 2>&1; then
      consecutive=$((consecutive+1))
      if (( consecutive >= 3 )); then
        log "READINESS PASS: citymanager-dashboard"
        return 0
      fi
    else
      consecutive=0
    fi
    sleep 2
  done
  return 1
}

restore_dashboard_candidate(){
  CANDIDATE_ACTIVE=0
  APPLICATION_ACTION="candidate-failed-dashboard-restored"
  (
    set +e
    log "ROLLBACK: restoring prior dashboard image"
    docker image tag "$ROLLBACK_DASHBOARD_TAG" dashboard-citymanager-dashboard:latest
    docker compose -f dashboard/docker-compose.yml up -d --no-deps --force-recreate citymanager-dashboard
    wait_dashboard_ready
    docker image tag "$ROLLBACK_LATEST_TAG" dashboard-citymanager-dashboard:latest
    ./deploy/cmos-health
    log "ROLLBACK: prior dashboard restored"
  )
  return 0
}

hash_is(){
  local path="$1" expected="$2"
  [[ -f "$path" && "$(sha256sum "$path" | awk '{print $1}')" == "$expected" ]]
}

section "#58 REGIONAL SPATIAL REFERENCE REPAIR AND RESUME"
printf 'REPAIR_ID=%s\nSTARTED_UTC=%s\n' "$REPAIR_ID" "$STARTED_UTC"

CURRENT_PHASE="preflight"
section "1. EXACT FAILED-RUN PREFLIGHT"
for cmd in git docker python3 bash sed grep install tee sha256sum mktemp awk tail sort flock seq sleep; do
  command -v "$cmd" >/dev/null || fail "$cmd is required"
done
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another #58 release runner is active"
cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "production repository must be clean"
git fetch -q origin main

HEAD_SHA="$(git rev-parse HEAD)"
ORIGIN_SHA="$(git rev-parse origin/main)"
[[ "$(git rev-parse "${FAILED_RELEASE_HEAD}^")" == "$BOOTSTRAP_HEAD" ]] \
  || fail "failed release parent no longer matches the known bootstrap"
[[ "$(git rev-parse "${BOOTSTRAP_HEAD}^")" == "$ACCEPTED_HEAD" ]] \
  || fail "bootstrap parent no longer matches the accepted production base"
[[ "$(git diff --name-only "$ACCEPTED_HEAD".."$BOOTSTRAP_HEAD" | sort)" == \
   "deploy/releases/issue-58-regional-spatial-reference.sh" ]] \
  || fail "bootstrap path manifest mismatch"
[[ "$(git diff --name-only "$BOOTSTRAP_HEAD".."$FAILED_RELEASE_HEAD" | sort)" == "$INITIAL_RELEASE_PATHS" ]] \
  || fail "failed release path manifest mismatch"
[[ "$(git rev-parse "${APPLICATION_TEST_FAILURE_HEAD}^")" == "$FAILED_RELEASE_HEAD" ]] \
  || fail "application-test repair parent no longer matches the failed release"
[[ "$(git diff --name-only "$FAILED_RELEASE_HEAD".."$APPLICATION_TEST_FAILURE_HEAD" | sort)" == "$REPAIR_PATHS" ]] \
  || fail "application-test repair path manifest mismatch"
[[ "$(git rev-parse "${PACKAGING_FAILURE_HEAD}^")" == "$APPLICATION_TEST_FAILURE_HEAD" ]] \
  || fail "dashboard-packaging failure parent no longer matches the application-test repair"
[[ "$(git diff --name-only "$APPLICATION_TEST_FAILURE_HEAD".."$PACKAGING_FAILURE_HEAD" | sort)" == "$TEST_REPAIR_PATH" ]] \
  || fail "dashboard-packaging failure path manifest mismatch"
[[ "$(git rev-parse "${ACCEPTANCE_FAILURE_HEAD}^")" == "$PACKAGING_FAILURE_HEAD" ]] \
  || fail "acceptance failure parent no longer matches the dashboard-packaging failure"
[[ "$(git diff --name-only "$PACKAGING_FAILURE_HEAD".."$ACCEPTANCE_FAILURE_HEAD" | sort)" == "$PACKAGING_REPAIR_PATH" ]] \
  || fail "acceptance failure path manifest mismatch"

if [[ "$HEAD_SHA" == "$ACCEPTANCE_FAILURE_HEAD" ]]; then
  REPAIR_MODE="permanent-transit-reference-build"
  [[ "$ORIGIN_SHA" == "$BOOTSTRAP_HEAD" ]] || fail "origin/main moved beyond the #58 bootstrap"
  hash_is dashboard/Dockerfile ad20c7de6c67f9951ce4fee96c41223260bedb04807f51e5cb6ac2b9c03849c4 \
    || fail "repaired dashboard image manifest checksum mismatch"
  hash_is deploy/postgis/init/031_spatial_reference_catalog.sql d5004eaf9320b29a12b93e806e76f4e2cd4895e2726975ff190bab88b6960837 \
    || fail "pre-permanent-reference SQL checksum mismatch"
  hash_is deploy/gis/install_spatial_reference_catalog.sh f49116d085069c977157f26976862d71e0a2607396b0d0ae13b02abe6064c846 \
    || fail "repaired installer checksum mismatch"
  hash_is dashboard/tests/test_spatial_reference_catalog.py db51f08ccc6ca40b62dc1d31ca1bce1cf6e8143b94c7e08351b00e88e722f4ef \
    || fail "pre-permanent-reference test checksum mismatch"
elif [[ "$(git rev-parse HEAD^)" == "$ACCEPTANCE_FAILURE_HEAD" ]] \
  && [[ "$(git diff --name-only "$ACCEPTANCE_FAILURE_HEAD"..HEAD | sort)" == "$PERMANENT_REFERENCE_REPAIR_PATHS" ]]; then
  REPAIR_MODE="permanent-transit-reference-resume"
  TARGET_HEAD="$HEAD_SHA"
  [[ "$ORIGIN_SHA" == "$BOOTSTRAP_HEAD" || "$ORIGIN_SHA" == "$TARGET_HEAD" ]] \
    || fail "origin/main is neither the #58 bootstrap nor this permanent-reference target"
else
  fail "local main is not the exact #58 acceptance failure or its permanent-reference repair"
fi
if [[ -f "$STATE_FILE" ]] && { \
  grep -Fxq "APPLICATION_TARGET=$ACCEPTANCE_FAILURE_HEAD" "$STATE_FILE" \
  || grep -Fxq "E2E_EVIDENCE_TARGET=$ACCEPTANCE_FAILURE_HEAD" "$STATE_FILE"; \
}; then
  E2E_ACTION="reused-v4-24-pass-runtime-evidence"
  state_set E2E_EVIDENCE_TARGET "$ACCEPTANCE_FAILURE_HEAD"
else
  E2E_ACTION="required-no-v4-runtime-state"
fi

REQUIRED_CONTAINERS=(citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis n8n ntfy)
for name in "${REQUIRED_CONTAINERS[@]}"; do
  [[ "$(docker inspect --format '{{.State.Running}}' "$name")" == true ]] || fail "required container is not running: $name"
done
database_structure_ready || fail "production database does not contain the committed #58 additive structure"
CATALOG_DIAGNOSTICS_BEFORE="$(catalog_diagnostics)"
printf '%s\n' "$CATALOG_DIAGNOSTICS_BEFORE"

BASE_COUNTS="$(db_at <<'SQL'
SELECT count(*) FROM watch_items;
SELECT count(*) FROM watch_item_recipients;
SELECT count(*) FROM subscribers;
SELECT count(*) FROM alerts;
SELECT count(*) FROM alert_watch_matches;
SELECT count(*) FROM deliveries;
SQL
)"
CATALOG_BEFORE="$(db_at <<'SQL'
SELECT count(*)||'|'||count(*) FILTER (WHERE active)||'|'||
       count(*) FILTER (WHERE parcel_objectid IS NOT NULL)||'|'||
       count(*) FILTER (WHERE transit_asset_id IS NOT NULL)
FROM spatial_reference_entities;
SQL
)"
STRICT_UNCHANGED_BEFORE="$(for name in citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis ntfy; do
  docker inspect --format '{{.Name}}|{{.Image}}|{{.State.StartedAt}}' "$name"
done)"
N8N_IMAGE_BEFORE="$(docker inspect --format '{{.Image}}' n8n)"
printf 'LOCAL_HEAD=%s\nORIGIN_MAIN=%s\nREPAIR_MODE=%s\nCATALOG_BASELINE=%s\n' \
  "$HEAD_SHA" "$ORIGIN_SHA" "$REPAIR_MODE" "$CATALOG_BEFORE"

CURRENT_PHASE="code"
section "2. GUARDED PERMANENT TRANSIT REFERENCE REPAIR"
if [[ "$REPAIR_MODE" == permanent-transit-reference-build ]]; then
  PERMANENT_REFERENCE_PATCH="$(cat <<'PATCH'
diff --git a/dashboard/tests/test_spatial_reference_catalog.py b/dashboard/tests/test_spatial_reference_catalog.py
index 6f954e2..1094797 100644
--- a/dashboard/tests/test_spatial_reference_catalog.py
+++ b/dashboard/tests/test_spatial_reference_catalog.py
@@ -33,6 +33,7 @@ def test_catalog_migration_is_additive_and_reuses_watchlist():
     assert "/0.3048" in sql
     assert "*0.3048" in sql
     assert "26400.0))*0.3048)),'[]'::jsonb" in sql
+    assert sql.count("upper(coalesce(ta.asset_type,'')) <> 'VEHICLE'") >= 2
     assert "CREATE TABLE IF NOT EXISTS watch_items" not in sql
     assert "CREATE TABLE IF NOT EXISTS subscribers" not in sql
     assert "CREATE TABLE IF NOT EXISTS deliveries" not in sql
diff --git a/deploy/postgis/init/031_spatial_reference_catalog.sql b/deploy/postgis/init/031_spatial_reference_catalog.sql
index c7cd87c..cc1395b 100644
--- a/deploy/postgis/init/031_spatial_reference_catalog.sql
+++ b/deploy/postgis/init/031_spatial_reference_catalog.sql
@@ -338,6 +338,7 @@ BEGIN
       SELECT ta.*,tp.provider_key,tp.name AS provider_name
       FROM transit_assets ta JOIN transit_providers tp ON tp.id=ta.provider_id
       WHERE ta.active AND ta.geom IS NOT NULL AND NOT ST_IsEmpty(ta.geom)
+        AND upper(coalesce(ta.asset_type,'')) <> 'VEHICLE'
     ), upserted AS (
       INSERT INTO spatial_reference_entities(
         entity_type,entity_subtype,canonical_name,aliases,municipality,county,state,
@@ -376,6 +377,7 @@ BEGIN
       AND NOT EXISTS (
         SELECT 1 FROM transit_assets ta
         WHERE ta.id::text=r.source_record_id AND ta.active AND ta.geom IS NOT NULL
+          AND upper(coalesce(ta.asset_type,'')) <> 'VEHICLE'
       );
     GET DIAGNOSTICS affected_rows = ROW_COUNT;
     retired_rows := retired_rows + affected_rows;
diff --git a/docs/SPATIAL_REFERENCE_CATALOG.md b/docs/SPATIAL_REFERENCE_CATALOG.md
index 3fb2365..caf8671 100644
--- a/docs/SPATIAL_REFERENCE_CATALOG.md
+++ b/docs/SPATIAL_REFERENCE_CATALOG.md
@@ -10,7 +10,9 @@ The first refresh reuses and links data already in PostGIS:
 
 - Hudson County named facilities from statewide `gis_parcels`
 - Hudson County landmark aliases from statewide `gis_landmark_aliases` and `gis_addresses`
-- all active mapped `transit_assets`, linked by their existing UUID rather than copied into another transit model
+- active mapped stationary `transit_assets` such as stops, stations, and terminals, linked by their existing UUID rather than copied into another transit model
+
+Live vehicle positions remain in the existing Transit system and are deliberately excluded from the permanent reference catalog. This prevents moving vehicles from creating catalog churn while preserving them for live transit observations and impact context.
 
 `spatial_reference_refresh_local_sources()` is idempotent. It upserts by stable source ID and retires missing linked records. Operators can run it from the Reference Catalog after an authoritative source promotion. A later lifecycle update can invoke the same function without creating another GIS refresh system.
 
PATCH
)"
  printf '%s\n' "$PERMANENT_REFERENCE_PATCH" | git apply --check
  printf '%s\n' "$PERMANENT_REFERENCE_PATCH" | git apply
  [[ "$(git diff --name-only | sort)" == "$PERMANENT_REFERENCE_REPAIR_PATHS" ]] \
    || fail "permanent-reference repair changed unexpected paths"
  git diff --check
  hash_is deploy/postgis/init/031_spatial_reference_catalog.sql 364f361d45078657129206d10e88f1b76ba1ce2495c17667455bc105b7182777 \
    || fail "permanent-reference SQL checksum mismatch"
  hash_is dashboard/tests/test_spatial_reference_catalog.py 4ef8ae995520538314ba704ec5c10020c5c6aaca0e36ec9e48eadb9daf336610 \
    || fail "permanent-reference regression test checksum mismatch"
  hash_is docs/SPATIAL_REFERENCE_CATALOG.md ebf2b06b07fe667d92c768a67047dee9f288778a3a0993f54731a7fdd21dcaac \
    || fail "permanent-reference documentation checksum mismatch"
  python3 -m py_compile dashboard/tests/test_spatial_reference_catalog.py
  python3 - <<'PY'
import importlib.util
path = "dashboard/tests/test_spatial_reference_catalog.py"
spec = importlib.util.spec_from_file_location("issue58_permanent_reference_static", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for name in sorted(item for item in dir(module) if item.startswith("test_")):
    getattr(module, name)()
    print("PERMANENT REFERENCE STATIC PASS", name)
PY
  git add -- $PERMANENT_REFERENCE_REPAIR_PATHS
  [[ "$(git diff --cached --name-only | sort)" == "$PERMANENT_REFERENCE_REPAIR_PATHS" ]] \
    || fail "staged permanent-reference paths differ from manifest"
  git diff --cached --check
  git -c user.name='City Manager OS Release' -c user.email='release@localhost' \
    commit -m "Keep #58 transit references permanent"
  TARGET_HEAD="$(git rev-parse HEAD)"
elif [[ "$REPAIR_MODE" == permanent-transit-reference-resume ]]; then
  hash_is deploy/postgis/init/031_spatial_reference_catalog.sql 364f361d45078657129206d10e88f1b76ba1ce2495c17667455bc105b7182777 \
    || fail "resumable permanent-reference SQL checksum mismatch"
  hash_is dashboard/tests/test_spatial_reference_catalog.py 4ef8ae995520538314ba704ec5c10020c5c6aaca0e36ec9e48eadb9daf336610 \
    || fail "resumable permanent-reference regression test checksum mismatch"
  hash_is docs/SPATIAL_REFERENCE_CATALOG.md ebf2b06b07fe667d92c768a67047dee9f288778a3a0993f54731a7fdd21dcaac \
    || fail "resumable permanent-reference documentation checksum mismatch"
fi
hash_is dashboard/Dockerfile ad20c7de6c67f9951ce4fee96c41223260bedb04807f51e5cb6ac2b9c03849c4 \
  || fail "final dashboard image manifest checksum mismatch"
hash_is deploy/gis/install_spatial_reference_catalog.sh f49116d085069c977157f26976862d71e0a2607396b0d0ae13b02abe6064c846 \
  || fail "final installer checksum mismatch"
git merge-base --is-ancestor "$ACCEPTANCE_FAILURE_HEAD" "$TARGET_HEAD" \
  || fail "permanent-reference target is not based on the acceptance failure"
[[ "$(git diff --name-only "$ACCEPTANCE_FAILURE_HEAD".."$TARGET_HEAD" | sort)" == "$PERMANENT_REFERENCE_REPAIR_PATHS" ]] \
  || fail "permanent-reference commit path manifest mismatch"
[[ "$(git diff --name-only "$FAILED_RELEASE_HEAD".."$TARGET_HEAD" | sort)" == "$FINAL_REPAIR_PATHS" ]] \
  || fail "coordinated repair path manifest mismatch"
[[ "$(git diff --name-only "$BOOTSTRAP_HEAD".."$TARGET_HEAD" | sort)" == "$FINAL_RELEASE_PATHS" ]] \
  || fail "coordinated release path manifest mismatch"
[[ -z "$(git status --porcelain)" ]] || fail "repository is not clean after permanent-reference repair"
printf 'TARGET_HEAD=%s\n' "$TARGET_HEAD"

CURRENT_PHASE="plan"
section "3. CHANGE-AWARE RELEASE PLAN"
PLAN_OUTPUT="$(./deploy/cmos-deploy plan --base "$BOOTSTRAP_HEAD" --target "$TARGET_HEAD")"
printf '%s\n' "$PLAN_OUTPUT"
grep -Fxq 'changed_count=11' <<< "$PLAN_OUTPUT"
grep -Fxq 'build=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'services=citymanager-dashboard,citymanager-integration-engine,citymanager-ops-engine,citymanager-staff' <<< "$PLAN_OUTPUT"
grep -Fxq 'backup_required=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'external=postgis-migration' <<< "$PLAN_OUTPUT"
grep -Fxq 'full_e2e=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'unknown=none' <<< "$PLAN_OUTPUT"
log "Only runtime-image diff is the validated dashboard route copy; SQL/test/docs repair does not expand restart scope"

CURRENT_PHASE="database"
section "4. MINIMAL TRANSACTIONAL DATABASE REPAIR"
./deploy/postgis/verify-backup.sh
if database_privileges_ready; then
  PRIVILEGE_ACTION="skipped-already-ready"
else
  PRIVILEGE_ACTION="transaction-started"
  db_at <<'SQL'
BEGIN;
GRANT SELECT,INSERT,UPDATE,DELETE ON spatial_reference_entities TO citymanager_app;
GRANT SELECT ON spatial_reference_catalog_status TO citymanager_app;
GRANT EXECUTE ON FUNCTION spatial_reference_refresh_local_sources() TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_parcel_for_point(double precision,double precision,double precision) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_addresses_for_parcel(integer,double precision,integer) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_parcels_within_radius(integer,double precision,integer,double precision) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_parcels_within_radius(double precision,double precision,double precision,integer) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_adjoining_parcels(integer,double precision,integer) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_spatial_impact_context(geometry,double precision,interval) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_parcel_context(integer,double precision) TO citymanager_app;
COMMIT;
SQL
  PRIVILEGE_ACTION="required-grants-reconciled"
  database_privileges_ready || fail "application database privileges remain incomplete"
fi
if parcel_context_ready 2>/dev/null; then
  DATABASE_ACTION="skipped-already-ready"
  log "Database already passes #58 context and topology readiness"
else
  DATABASE_ACTION="transaction-started"
  FUNCTION_SQL="$(sed -n '/^CREATE OR REPLACE FUNCTION gis_parcel_context(/,/^\$\$;$/p' \
    deploy/postgis/init/031_spatial_reference_catalog.sql)"
  [[ "$FUNCTION_SQL" == 'CREATE OR REPLACE FUNCTION gis_parcel_context('* ]] \
    || fail "could not extract corrected parcel-context function"
  [[ "$FUNCTION_SQL" == *'$$;' ]] || fail "corrected parcel-context function is incomplete"
  printf 'BEGIN;\nSET LOCAL check_function_bodies = on;\n%s\nGRANT EXECUTE ON FUNCTION gis_parcel_context(integer,double precision) TO citymanager_app;\nCOMMIT;\n' \
    "$FUNCTION_SQL" | docker exec -i citymanager-postgis sh -lc \
      'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
  DATABASE_ACTION="function-replaced-no-refresh"
  parcel_context_ready || fail "parcel context/topology failed after the transactional function repair"
fi
if refresh_function_ready; then
  CATALOG_FUNCTION_ACTION="skipped-already-permanent"
else
  CATALOG_FUNCTION_ACTION="transaction-started"
  REFRESH_FUNCTION_SQL="$(sed -n '/^CREATE OR REPLACE FUNCTION spatial_reference_refresh_local_sources(/,/^\$\$;$/p' \
    deploy/postgis/init/031_spatial_reference_catalog.sql)"
  [[ "$REFRESH_FUNCTION_SQL" == 'CREATE OR REPLACE FUNCTION spatial_reference_refresh_local_sources('* ]] \
    || fail "could not extract permanent-reference refresh function"
  [[ "$REFRESH_FUNCTION_SQL" == *'$$;' ]] || fail "permanent-reference refresh function is incomplete"
  printf 'BEGIN;\nSET LOCAL check_function_bodies = on;\n%s\nGRANT EXECUTE ON FUNCTION spatial_reference_refresh_local_sources() TO citymanager_app;\nCOMMIT;\n' \
    "$REFRESH_FUNCTION_SQL" | docker exec -i citymanager-postgis sh -lc \
      'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
  CATALOG_FUNCTION_ACTION="permanent-reference-filter-installed"
  refresh_function_ready || fail "permanent-reference refresh function validation failed"
fi
if [[ "$CATALOG_FUNCTION_ACTION" == "permanent-reference-filter-installed" ]] || ! catalog_core_ready; then
  CATALOG_REFRESH_ACTION="required-permanent-reconciliation-started"
  log "Reconciling stationary transit references and retiring mobile vehicle rows"
  db_at <<'SQL'
SELECT spatial_reference_refresh_local_sources() IS NOT NULL;
SQL
  CATALOG_REFRESH_ACTION="permanent-reference-reconciliation-completed"
  catalog_core_ready || fail "permanent reference catalog still fails after reconciliation"
else
  CATALOG_REFRESH_ACTION="skipped-core-ready"
  log "Permanent reference catalog already passes reconciliation; source refresh skipped"
fi
database_ready || fail "database does not satisfy final #58 readiness"
CATALOG_DIAGNOSTICS_AFTER="$(catalog_diagnostics)"
printf '%s\n' "$CATALOG_DIAGNOSTICS_AFTER"
state_set DATABASE_TARGET "$TARGET_HEAD"
printf 'DATABASE_ACTION=%s\n' "$DATABASE_ACTION"
printf 'PRIVILEGE_ACTION=%s\n' "$PRIVILEGE_ACTION"
printf 'CATALOG_FUNCTION_ACTION=%s\n' "$CATALOG_FUNCTION_ACTION"
printf 'CATALOG_REFRESH_ACTION=%s\n' "$CATALOG_REFRESH_ACTION"

CURRENT_PHASE="application"
section "5. CHANGE-AWARE DASHBOARD-ONLY DEPLOYMENT"
if application_ready && [[ -f "$STATE_FILE" ]] \
  && grep -Fxq "APPLICATION_TARGET=$TARGET_HEAD" "$STATE_FILE"; then
  APPLICATION_ACTION="skipped-already-ready"
  log "Dashboard already serves ${RELEASE_ID}"
else
  APPLICATION_ACTION="targeted-validation-started"
  docker image tag "$(docker inspect --format '{{.Image}}' citymanager-dashboard)" "$ROLLBACK_DASHBOARD_TAG"
  docker image tag "$(docker image inspect dashboard-citymanager-dashboard:latest --format '{{.Id}}')" "$ROLLBACK_LATEST_TAG"

  trap - ERR
  set +e
  (
    set -Eeuo pipefail
    trap 'rc=$?; printf "CANDIDATE_VALIDATION_FAILURE rc=%s line=%s command=%q\n" "$rc" "$LINENO" "$BASH_COMMAND"; exit "$rc"' ERR
    git diff --check "$BOOTSTRAP_HEAD...$TARGET_HEAD"
    python3 -m py_compile dashboard/spatial_reference_app.py dashboard/tests/test_spatial_reference_catalog.py
    docker compose -f dashboard/docker-compose.yml build citymanager-dashboard
    docker compose -f dashboard/docker-compose.yml run --rm --no-deps -T \
      --entrypoint python citymanager-dashboard - <<'PY'
import phase3_app
import spatial_reference_app
paths = {route.path for route in phase3_app.app.routes}
required = {
    "/api/spatial-reference/release",
    "/spatial-reference",
    "/api/parcel/for-point",
    "/api/parcels/within-radius",
    "/api/parcel/{parcel_objectid}/context",
}
missing = sorted(required - paths)
assert not missing, missing
print("DASHBOARD RUNTIME IMPORT PASS")
PY
  )
  PREDEPLOY_RC=$?
  set -e
  trap on_error ERR
  if (( PREDEPLOY_RC != 0 )); then
    docker image tag "$ROLLBACK_LATEST_TAG" dashboard-citymanager-dashboard:latest
    APPLICATION_ACTION="targeted-validation-failed-no-restart"
    fail "candidate validation failed before the dashboard restart"
  fi

  APPLICATION_ACTION="dashboard-only-deploy-started"
  CANDIDATE_ACTIVE=1
  trap - ERR
  set +e
  (
    set -Eeuo pipefail
    trap 'rc=$?; printf "CANDIDATE_LIVE_FAILURE rc=%s line=%s command=%q\n" "$rc" "$LINENO" "$BASH_COMMAND"; exit "$rc"' ERR
    docker compose -f dashboard/docker-compose.yml up -d --no-deps --force-recreate citymanager-dashboard
    wait_dashboard_ready
    docker exec -i citymanager-dashboard python - \
      /map /map/gis/status '/map/system/alerts.geojson?days=30&min_priority=1' <<'PY'
import json, os, sys, urllib.request
token = os.environ.get("CMOS_AUTOMATION_TOKEN", "").strip()
headers = {"X-CMOS-Automation-Key": token} if token else {}
for path in sys.argv[1:]:
    request = urllib.request.Request("http://127.0.0.1:8000" + path, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status == 200, (path, response.status)
        assert "/login" not in response.geturl(), (path, response.geturl())
        if path.split("?", 1)[0].endswith(".geojson") or path == "/map/gis/status":
            json.load(response)
    print("AUTHENTICATED PROBE PASS", path)
PY
    ./deploy/cmos-health
    if [[ "$E2E_ACTION" == "required-no-v4-runtime-state" ]]; then
      env CMOS_E2E_EXPECTED_HEAD="$TARGET_HEAD" ./deploy/cmos-e2e-secure
    else
      log "Reusing v4 evidence: identical runtime image passed all 24 controlled E2E checks"
    fi
    ./deploy/cmos-health
    application_ready
  )
  LIVE_RC=$?
  set -e
  trap on_error ERR
  if (( LIVE_RC != 0 )); then
    APPLICATION_ACTION="candidate-live-validation-failed"
    fail "candidate dashboard failed live validation"
  fi
  if [[ "$E2E_ACTION" == "required-no-v4-runtime-state" ]]; then
    E2E_ACTION="executed-24-check-controlled-e2e"
  fi
  APPLICATION_ACTION="dashboard-cached-build-runtime-validated-restarted"
fi
state_set APPLICATION_TARGET "$TARGET_HEAD"
printf 'APPLICATION_ACTION=%s\n' "$APPLICATION_ACTION"

CURRENT_PHASE="acceptance"
section "6. TARGETED #58 ACCEPTANCE"
SAMPLE="$(db_at <<'SQL'
WITH parcel AS (
  SELECT objectid,ST_Y(ST_PointOnSurface(geom)) AS lat,ST_X(ST_PointOnSurface(geom)) AS lon
  FROM gis_parcels WHERE geom IS NOT NULL AND upper(coalesce(mun_name,'')) LIKE '%WEEHAWKEN%'
  ORDER BY objectid LIMIT 1
), reference AS (
  SELECT entity_id FROM spatial_reference_entities
  WHERE active=true
  ORDER BY CASE
             WHEN upper(coalesce(municipality,'')) LIKE '%WEEHAWKEN%'
                  AND parcel_objectid IS NOT NULL THEN 0
             WHEN upper(coalesce(county,'')) LIKE '%HUDSON%' THEN 1
             ELSE 2
           END,
           importance_tier,canonical_name
  LIMIT 1
)
SELECT parcel.objectid||'|'||parcel.lat||'|'||parcel.lon||'|'||reference.entity_id
FROM parcel CROSS JOIN reference;
SQL
)"
[[ -n "$SAMPLE" ]] || fail "targeted acceptance could not find a Weehawken parcel and active reference"
IFS='|' read -r SAMPLE_PARCEL SAMPLE_LAT SAMPLE_LON SAMPLE_REFERENCE <<< "$SAMPLE"

docker exec -i citymanager-dashboard python - "$SAMPLE_PARCEL" "$SAMPLE_LAT" "$SAMPLE_LON" "$SAMPLE_REFERENCE" "$RELEASE_ID" <<'PY'
import json, os, sys, urllib.parse, urllib.request
parcel, lat, lon, reference, release_id = sys.argv[1:]
token = os.environ.get("CMOS_AUTOMATION_TOKEN", "").strip()
headers = {"X-CMOS-Automation-Key": token} if token else {}
base = "http://127.0.0.1:8000"

def fetch(path, kind="json"):
    request = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status == 200, (path, response.status)
        assert "/login" not in response.geturl(), (path, response.geturl())
        body = response.read()
    print("TARGETED PASS", path)
    return body if kind == "html" else json.loads(body)

release = fetch("/api/spatial-reference/release")
assert release["release_id"] == release_id
assert b"Regional Spatial Reference Catalog" in fetch("/spatial-reference", "html")
assert b"Watch This" in fetch(f"/spatial-reference/{reference}", "html")
search = fetch("/api/spatial-reference/search")
assert search["count"] > 0 and isinstance(search["items"], list)
point = fetch("/api/parcel/for-point?" + urllib.parse.urlencode({"lat": lat, "lon": lon, "tolerance_ft": 3}))
assert point["parcel_objectid"] == int(parcel) and point["relation_type"] == "SAME_PARCEL"
radius = fetch("/api/parcels/within-radius?" + urllib.parse.urlencode({"lat": lat, "lon": lon, "radius_ft": 500}))
assert radius["items"] and radius["items"][0]["relation_type"] == "SAME_PARCEL"
context = fetch(f"/api/parcel/{parcel}/context?radius_ft=500")
assert context["parcel"]["objectid"] == int(parcel)
for key in ("addresses", "adjoining_parcels", "nearby_parcels", "reference_entities"):
    assert isinstance(context[key], list), key
for key in ("active_watches", "open_issues", "recent_alerts", "events", "transit", "flood_zones"):
    assert isinstance(context["spatial_impact"][key], list), key
history = fetch(f"/api/spatial-reference/{reference}/nearby-history?radius_ft=500&days=30")
assert history["radius_ft"] == 500
buffered = fetch(f"/api/spatial-reference/{reference}/impact-buffer.geojson?radius_ft=500")
assert buffered["type"] == "FeatureCollection" and len(buffered["features"]) == 1
bbox = ",".join((str(float(lon)-.03), str(float(lat)-.03), str(float(lon)+.03), str(float(lat)+.03)))
layer = fetch("/map/system/spatial-references.geojson?bbox=" + urllib.parse.quote(bbox))
assert layer["type"] == "FeatureCollection" and isinstance(layer["features"], list)
PY

AUDIT_STATE="$(db_at <<'SQL'
SELECT 'catalog_active=' || (count(*)>0) FROM spatial_reference_entities WHERE active=true;
SELECT 'geometry_valid=' || (count(*)=0) FROM spatial_reference_entities
WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom) OR ST_SRID(geom)<>4326;
SELECT 'functions_ready=' || (count(DISTINCT p.proname)=7)
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='public' AND p.proname IN (
  'spatial_reference_refresh_local_sources','gis_parcel_for_point','gis_addresses_for_parcel',
  'gis_adjoining_parcels','gis_parcels_within_radius','gis_spatial_impact_context','gis_parcel_context'
);
SELECT 'sources_unique=' || (count(*)=0) FROM (
  SELECT source_provider,source_record_id FROM spatial_reference_entities
  WHERE source_record_id IS NOT NULL
  GROUP BY source_provider,source_record_id HAVING count(*)>1
) duplicate_sources;
SELECT 'weehawken_parcels=' || (count(*)>0) FROM gis_parcels
WHERE geom IS NOT NULL AND upper(coalesce(mun_name,'')) LIKE '%WEEHAWKEN%';
SELECT 'stationary_transit_reconciled=' || (count(*)=(
  SELECT count(*) FROM transit_assets
  WHERE active=true AND geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    AND upper(coalesce(asset_type,''))<>'VEHICLE'
)) FROM spatial_reference_entities WHERE active=true AND source_provider='CMOS_TRANSIT_ASSET';
SELECT 'mobile_transit_excluded=' || NOT EXISTS (
  SELECT 1 FROM spatial_reference_entities r
  JOIN transit_assets ta ON ta.id=r.transit_asset_id
  WHERE r.active=true AND r.source_provider='CMOS_TRANSIT_ASSET'
    AND upper(coalesce(ta.asset_type,''))='VEHICLE'
);
SELECT 'source_metadata_ready=' || coalesce(bool_and(source_reference IS NOT NULL AND refreshed_at IS NOT NULL),false)
FROM spatial_reference_entities WHERE active=true;
SQL
)"
EXPECTED_AUDIT=$'catalog_active=true\ngeometry_valid=true\nfunctions_ready=true\nsources_unique=true\nweehawken_parcels=true\nstationary_transit_reconciled=true\nmobile_transit_excluded=true\nsource_metadata_ready=true'
[[ "$AUDIT_STATE" == "$EXPECTED_AUDIT" ]] || fail "targeted database audit failed: ${AUDIT_STATE}"
log "TARGETED DATABASE AUDIT PASS"

docker exec -i citymanager-postgis sh -lc \
  'export PGOPTIONS="-c default_transaction_read_only=on"; psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT 'CATALOG' AS check,count(*) AS total,count(*) FILTER (WHERE active) AS active,
       count(*) FILTER (WHERE parcel_objectid IS NOT NULL) AS parcel_linked,
       count(*) FILTER (WHERE transit_asset_id IS NOT NULL) AS transit_linked
FROM spatial_reference_entities;
SELECT 'INVALID_GEOMETRY' AS check,count(*) AS rows FROM spatial_reference_entities
WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom) OR ST_SRID(geom)<>4326;
SELECT 'FUNCTIONS' AS check,count(DISTINCT p.proname) AS rows
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='public' AND p.proname IN (
  'spatial_reference_refresh_local_sources','gis_parcel_for_point','gis_addresses_for_parcel',
  'gis_adjoining_parcels','gis_parcels_within_radius','gis_spatial_impact_context','gis_parcel_context'
);
SELECT 'ACTIVE_MOBILE_TRANSIT_REFERENCES' AS check,count(*) AS rows
FROM spatial_reference_entities r JOIN transit_assets ta ON ta.id=r.transit_asset_id
WHERE r.active=true AND r.source_provider='CMOS_TRANSIT_ASSET'
  AND upper(coalesce(ta.asset_type,''))='VEHICLE';
SELECT 'REFERENCE_WATCHES' AS check,count(*) AS rows
FROM watch_items WHERE spatial_reference_entity_id IS NOT NULL;
SQL

FINAL_COUNTS="$(db_at <<'SQL'
SELECT count(*) FROM watch_items;
SELECT count(*) FROM watch_item_recipients;
SELECT count(*) FROM subscribers;
SELECT count(*) FROM alerts;
SELECT count(*) FROM alert_watch_matches;
SELECT count(*) FROM deliveries;
SQL
)"
[[ "$FINAL_COUNTS" == "$BASE_COUNTS" ]] || fail "watch, routing, alert, match, or delivery counts changed"
CATALOG_AFTER="$(db_at <<'SQL'
SELECT count(*)||'|'||count(*) FILTER (WHERE active)||'|'||
       count(*) FILTER (WHERE parcel_objectid IS NOT NULL)||'|'||
       count(*) FILTER (WHERE transit_asset_id IS NOT NULL)
FROM spatial_reference_entities;
SQL
)"
if [[ "$CATALOG_REFRESH_ACTION" == "skipped-core-ready" ]]; then
  [[ "$CATALOG_AFTER" == "$CATALOG_BEFORE" ]] || fail "catalog row/link counts changed without a source reconciliation"
fi
STRICT_UNCHANGED_AFTER="$(for name in citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis ntfy; do
  docker inspect --format '{{.Name}}|{{.Image}}|{{.State.StartedAt}}' "$name"
done)"
[[ "$STRICT_UNCHANGED_AFTER" == "$STRICT_UNCHANGED_BEFORE" ]] \
  || fail "a non-E2E out-of-scope container changed"
[[ "$(docker inspect --format '{{.Image}}' n8n)" == "$N8N_IMAGE_BEFORE" ]] \
  || fail "n8n image changed during controlled E2E"

CURRENT_PHASE="promotion"
section "7. PROMOTE VERIFIED MAIN"
[[ "$(git rev-parse HEAD)" == "$TARGET_HEAD" ]]
[[ -z "$(git status --porcelain)" ]]
git fetch -q origin main
REMOTE_NOW="$(git rev-parse origin/main)"
if [[ "$REMOTE_NOW" == "$BOOTSTRAP_HEAD" ]]; then
  git push origin main
elif [[ "$REMOTE_NOW" != "$TARGET_HEAD" ]]; then
  fail "origin/main changed during repair; verified target was not pushed"
fi
git fetch -q origin main
[[ "$(git rev-parse origin/main)" == "$TARGET_HEAD" ]] || fail "origin/main did not reach the verified target"
CANDIDATE_ACTIVE=0

CURRENT_PHASE="complete"
FINAL_STATUS="PASS"
section "#58 REPAIR AND RELEASE COMPLETE"
printf 'TARGET_HEAD=%s\n' "$TARGET_HEAD"
printf 'DATABASE_ACTION=%s\n' "$DATABASE_ACTION"
printf 'PRIVILEGE_ACTION=%s\n' "$PRIVILEGE_ACTION"
printf 'CATALOG_FUNCTION_ACTION=%s\n' "$CATALOG_FUNCTION_ACTION"
printf 'CATALOG_REFRESH_ACTION=%s\n' "$CATALOG_REFRESH_ACTION"
printf 'APPLICATION_ACTION=%s\n' "$APPLICATION_ACTION"
printf 'E2E_ACTION=%s\n' "$E2E_ACTION"
printf 'CATALOG=%s\n' "$CATALOG_AFTER"
printf 'NON_E2E_OUT_OF_SCOPE_CONTAINERS=UNCHANGED\n'
if [[ "$E2E_ACTION" == "reused-v4-24-pass-runtime-evidence" ]]; then
  printf 'N8N_IMAGE=UNCHANGED_NO_V5_RESTART\n'
else
  printf 'N8N_IMAGE=UNCHANGED_CONTROLLED_E2E_RESTART_ONLY\n'
fi
printf 'WATCH_ROUTING_DELIVERY_COUNTS=UNCHANGED\n'
printf 'ORIGIN_MAIN=%s\n' "$(git rev-parse origin/main)"
printf '#58 REPAIR: PASS\n'
