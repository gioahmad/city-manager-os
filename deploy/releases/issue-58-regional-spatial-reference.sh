#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

REPO="/opt/city-manager-os"
EXPECTED_HEAD="aa19402d164b0fcc53891c873a534043fa50627b"
RUNNER_PATH="deploy/releases/issue-58-regional-spatial-reference.sh"
REPORT_BRANCH="release-output/58"
RELEASE_ID="issue-58-regional-spatial-reference-v1"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
STATE_DIR="/var/lib/city-manager-os/releases"
LOG_FILE="${LOG_DIR}/issue-58-${RUN_ID}.log"
STATE_FILE="${STATE_DIR}/issue-58.state"
LOCK_FILE="/var/lock/cmos-issue-58-release.lock"
PAYLOAD_DIR=""
REPORT_WORKTREE=""
CURRENT_PHASE="startup"
FAIL_LINE=""
FAIL_COMMAND=""
RELEASE_BASE=""
TARGET_HEAD=""
FINAL_STATUS="FAIL"

umask 077
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
  local status="$1" rc="$2" report_dir report_file latest_file raw_sha head_sha origin_sha changed report_parent="" report_worktree=""
  report_cleanup(){
    if [[ -n "$report_worktree" && "$report_worktree" == /tmp/cmos58-report.* ]]; then
      git -C "$REPO" worktree remove --force "$report_worktree" >/dev/null 2>&1 || true
    fi
    [[ -n "$report_parent" && "$report_parent" == /tmp/cmos58-report.* ]] && rmdir "$report_parent" >/dev/null 2>&1 || true
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
  report_file="$report_dir/${RUN_ID}-${status,,}.md"
  latest_file="$report_dir/latest.md"
  raw_sha="$(sha256sum "$LOG_FILE" | awk '{print $1}')"
  head_sha="$(safe_git_value rev-parse HEAD)"
  origin_sha="$(safe_git_value rev-parse origin/main)"
  changed="$(safe_git_value status --short | redact)"
  {
    printf '# City Manager OS issue #58 release report\n\n'
    printf '> This repository is public. This report is intentionally redacted. The complete mode-600 log remains on the VPS.\n\n'
    printf '| Field | Value |\n|---|---|\n'
    printf '| Run | `%s` |\n' "$RUN_ID"
    printf '| Status | **%s** |\n' "$status"
    printf '| Exit code | `%s` |\n' "$rc"
    printf '| Failed line | `%s` |\n' "${FAIL_LINE:-none}"
    printf '| Phase | `%s` |\n' "$CURRENT_PHASE"
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '| Accepted base | `%s` |\n' "$EXPECTED_HEAD"
    printf '| Release base | `%s` |\n' "${RELEASE_BASE:-unavailable}"
    printf '| Target | `%s` |\n' "${TARGET_HEAD:-$head_sha}"
    printf '| Local HEAD | `%s` |\n' "$head_sha"
    printf '| Origin main | `%s` |\n' "$origin_sha"
    printf '| Full local log | `%s` |\n' "$LOG_FILE"
    printf '| Full log SHA-256 | `%s` |\n\n' "$raw_sha"
    printf '## Working tree\n\n```text\n%s\n```\n\n' "${changed:-clean}"
    printf '## Redacted diagnostic output\n\n```text\n'
    grep -Eai '(^|[[:space:]])(ERROR|FATAL|FAIL|FAILED|EXCEPTION|TRACEBACK|ASSERT|SYNTAX|UNEXPECTED|MISMATCH|PASS|PLAN|READINESS|HEALTH|CATALOG|CONTEXT|FUNCTIONS)|^(base|target|changed|build|services|tests|probes|backup_required|external|full_e2e|unknown)=' "$LOG_FILE" \
      | grep -v '^FAILED_COMMAND=' | redact | tail -n 180 || true
    printf '```\n'
  } > "$report_file"
  chmod 600 "$report_file"
  install -m 600 "$report_file" "$latest_file"
  git -C "$report_worktree" add \
    "release-results/issue-58/$(basename "$report_file")" \
    "release-results/issue-58/$(basename "$latest_file")"
  git -C "$report_worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit -m "Record #58 release ${status,,} ${RUN_ID}" >/dev/null || return 1
  git -C "$report_worktree" push -q origin "HEAD:refs/heads/${REPORT_BRANCH}" || return 1
  log "REDACTED_GITHUB_REPORT=https://github.com/gioahmad/city-manager-os/blob/${REPORT_BRANCH}/release-results/issue-58/latest.md"
  return 0
) }

cleanup(){
  set +e
  if [[ -n "$REPORT_WORKTREE" && "$REPORT_WORKTREE" == /tmp/cmos58-report.* ]]; then
    git -C "$REPO" worktree remove --force "$REPORT_WORKTREE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$PAYLOAD_DIR" && "$PAYLOAD_DIR" == /tmp/cmos58-payload.* ]]; then
    rm -rf -- "$PAYLOAD_DIR"
  fi
}

on_error(){
  local rc=$?
  FAIL_LINE="${BASH_LINENO[0]:-$LINENO}"
  FAIL_COMMAND="$BASH_COMMAND"
  log "ERROR: command failed rc=${rc} line=${FAIL_LINE} phase=${CURRENT_PHASE}"
  printf 'FAILED_COMMAND=%q\n' "$FAIL_COMMAND"
  trap - ERR
  exit "$rc"
}

on_exit(){
  local rc=$?
  trap - ERR EXIT
  if (( rc == 0 )); then FINAL_STATUS="PASS"; fi
  printf '\n============================================================\n'
  printf '#58 RELEASE: %s rc=%s phase=%s line=%s\n' "$FINAL_STATUS" "$rc" "$CURRENT_PHASE" "${FAIL_LINE:-none}"
  printf 'FULL_LOCAL_LOG=%s\n' "$LOG_FILE"
  printf 'FULL_LOCAL_LOG_MODE=600\n'
  printf 'GITHUB_REPORT=REDACTED\n'
  printf '============================================================\n'
  if ! publish_report "$FINAL_STATUS" "$rc"; then
    log "WARNING: redacted GitHub report could not be published; full local log retained at ${LOG_FILE}"
  fi
  cleanup
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

database_ready(){
  local structure
  structure="$(db_at <<'SQL'
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
)"
  [[ "$structure" == t ]] || return 1
  [[ "$(db_at <<'SQL'
SELECT
  EXISTS (SELECT 1 FROM spatial_reference_entities WHERE active=true)
  AND EXISTS (
    SELECT 1 FROM spatial_reference_entities
    WHERE active=true AND upper(coalesce(municipality,'')) LIKE '%WEEHAWKEN%'
      AND parcel_objectid IS NOT NULL
  )
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
       WHERE active=true AND geom IS NOT NULL AND NOT ST_IsEmpty(geom));
SQL
)" == t ]] || return 1
  [[ "$(db_at <<'SQL'
SELECT has_table_privilege('citymanager_app','spatial_reference_entities','SELECT')
  AND has_table_privilege('citymanager_app','spatial_reference_catalog_status','SELECT')
  AND has_function_privilege(
    'citymanager_app',
    'gis_parcel_context(integer,double precision)',
    'EXECUTE'
  );
SQL
)" == t ]]
}

application_ready(){
  docker exec -i citymanager-dashboard python - "$RELEASE_ID" <<'PY' >/dev/null 2>&1
import json, os, sys, urllib.request
token=os.environ.get("CMOS_AUTOMATION_TOKEN","").strip()
headers={"X-CMOS-Automation-Key":token} if token else {}
request=urllib.request.Request("http://127.0.0.1:8000/api/spatial-reference/release",headers=headers)
with urllib.request.urlopen(request,timeout=10) as response:
    payload=json.load(response)
assert response.status == 200
assert payload.get("release_id") == sys.argv[1]
PY
}

CURRENT_PHASE="preflight"
section "1. ACCEPTED BASELINE AND RELEASE PREFLIGHT"
for cmd in git docker python3 base64 gzip tar grep find install tee sha256sum mktemp cmp awk sed tail sort flock; do
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
EXPECTED_RELEASE_PATHS=$'dashboard/map_app.py\ndashboard/phase3_app.py\ndashboard/spatial_reference_app.py\ndashboard/templates/nav.html\ndashboard/templates/spatial_reference.html\ndashboard/templates/spatial_reference_detail.html\ndashboard/tests/test_spatial_reference_catalog.py\ndeploy/gis/install_spatial_reference_catalog.sh\ndeploy/postgis/init/031_spatial_reference_catalog.sql\ndocs/SPATIAL_REFERENCE_CATALOG.md'

if [[ "$HEAD_SHA" == "$EXPECTED_HEAD" ]]; then
  RELEASE_BASE="$EXPECTED_HEAD"
  RELEASE_MODE="build"
  [[ "$ORIGIN_SHA" == "$EXPECTED_HEAD" ]] || fail "origin/main moved beyond the accepted base"
elif [[ "$(git rev-parse HEAD^)" == "$EXPECTED_HEAD" ]] \
  && [[ "$(git diff --name-only "$EXPECTED_HEAD"..HEAD | sort)" == "$RUNNER_PATH" ]]; then
  RELEASE_BASE="$HEAD_SHA"
  RELEASE_MODE="build"
  [[ "$ORIGIN_SHA" == "$HEAD_SHA" ]] || fail "origin/main does not match the pulled #58 bootstrap"
elif [[ "$(git rev-parse HEAD^^)" == "$EXPECTED_HEAD" ]] \
  && [[ "$(git diff --name-only "$EXPECTED_HEAD"..HEAD^ | sort)" == "$RUNNER_PATH" ]] \
  && [[ "$(git diff --name-only HEAD^..HEAD | sort)" == "$EXPECTED_RELEASE_PATHS" ]]; then
  RELEASE_BASE="$(git rev-parse HEAD^)"
  TARGET_HEAD="$HEAD_SHA"
  RELEASE_MODE="resume"
  [[ "$ORIGIN_SHA" == "$RELEASE_BASE" || "$ORIGIN_SHA" == "$TARGET_HEAD" ]] \
    || fail "origin/main moved to an unexpected commit during #58"
else
  fail "HEAD is not the accepted base, the published #58 bootstrap, or its exact resumable release commit"
fi

REQUIRED_CONTAINERS=(citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis n8n ntfy)
for name in "${REQUIRED_CONTAINERS[@]}"; do
  [[ "$(docker inspect --format '{{.State.Running}}' "$name")" == true ]] || fail "required container is not running: $name"
done

BASE_COUNTS="$(db_at <<'SQL'
SELECT count(*) FROM watch_items;
SELECT count(*) FROM watch_item_recipients;
SELECT count(*) FROM subscribers;
SELECT count(*) FROM alerts;
SELECT count(*) FROM alert_watch_matches;
SELECT count(*) FROM deliveries;
SQL
)"
UNCHANGED_BEFORE="$(for name in citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis n8n ntfy; do
  docker inspect --format '{{.Name}}|{{.Image}}|{{.State.StartedAt}}' "$name"
done)"
printf 'ACCEPTED_HEAD=%s\nRELEASE_BASE=%s\nMODE=%s\n' "$EXPECTED_HEAD" "$RELEASE_BASE" "$RELEASE_MODE"

CURRENT_PHASE="payload"
section "2. VERIFIED SELF-CONTAINED PAYLOAD"
PAYLOAD_DIR="$(mktemp -d /tmp/cmos58-payload.XXXXXX)"
base64 -d > "$PAYLOAD_DIR/payload.tar.gz" <<'CMOS58_PAYLOAD'
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
CMOS58_PAYLOAD
[[ "$(sha256sum "$PAYLOAD_DIR/payload.tar.gz" | awk '{print $1}')" == "a0a42a068546e5d00c3427eb0b2f28308fda55a27227d2882f1e5e60f6104792" ]] \
  || fail "embedded payload checksum mismatch"
gzip -t "$PAYLOAD_DIR/payload.tar.gz"
ACTUAL_PAYLOAD="$(tar -tzf "$PAYLOAD_DIR/payload.tar.gz" | sort)"
[[ "$ACTUAL_PAYLOAD" == "$EXPECTED_RELEASE_PATHS" ]] || fail "unexpected embedded payload paths"
tar -xzf "$PAYLOAD_DIR/payload.tar.gz" -C "$PAYLOAD_DIR"

CURRENT_PHASE="code"
section "3. IDEMPOTENT RELEASE CODE PREPARATION"
if [[ "$RELEASE_MODE" == build ]]; then
  NEW_PATHS=$'dashboard/spatial_reference_app.py\ndashboard/templates/spatial_reference.html\ndashboard/templates/spatial_reference_detail.html\ndashboard/tests/test_spatial_reference_catalog.py\ndeploy/gis/install_spatial_reference_catalog.sh\ndeploy/postgis/init/031_spatial_reference_catalog.sql\ndocs/SPATIAL_REFERENCE_CATALOG.md'
  while IFS= read -r path; do [[ ! -e "$path" ]] || fail "new release path already exists: $path"; done <<< "$NEW_PATHS"
  while IFS= read -r path; do
    mode=0644
    [[ "$path" == *.sh ]] && mode=0755
    install -D -m "$mode" "$PAYLOAD_DIR/$path" "$path"
  done <<< "$EXPECTED_RELEASE_PATHS"
  [[ "$( { git diff --name-only; git ls-files --others --exclude-standard; } | sort -u)" == "$EXPECTED_RELEASE_PATHS" ]] \
    || fail "release payload changed an unexpected path"
  git diff --check
  python3 -m py_compile dashboard/spatial_reference_app.py dashboard/map_app.py dashboard/phase3_app.py dashboard/tests/test_spatial_reference_catalog.py
  bash -n deploy/gis/install_spatial_reference_catalog.sh
  python3 - <<'PY'
import importlib.util
path="dashboard/tests/test_spatial_reference_catalog.py"
spec=importlib.util.spec_from_file_location("issue58_static",path)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for name in sorted(item for item in dir(module) if item.startswith("test_")):
    getattr(module,name)()
    print("STATIC PASS",name)
PY
  git add -- $EXPECTED_RELEASE_PATHS
  [[ "$(git diff --cached --name-only | sort)" == "$EXPECTED_RELEASE_PATHS" ]] || fail "staged release paths differ from manifest"
  git diff --cached --check
  git -c user.name='City Manager OS Release' -c user.email='release@localhost' \
    commit -m "Implement #58 regional spatial reference catalog"
  TARGET_HEAD="$(git rev-parse HEAD)"
else
  while IFS= read -r path; do
    cmp -s "$PAYLOAD_DIR/$path" "$path" || fail "resumable release file differs from signed payload: $path"
  done <<< "$EXPECTED_RELEASE_PATHS"
fi
[[ "$(git rev-parse HEAD^)" == "$RELEASE_BASE" ]] || fail "release target parent mismatch"
[[ "$(git diff --name-only "$RELEASE_BASE".."$TARGET_HEAD" | sort)" == "$EXPECTED_RELEASE_PATHS" ]] \
  || fail "release commit path manifest mismatch"
[[ -z "$(git status --porcelain)" ]] || fail "repository is not clean after code preparation"
printf 'TARGET_HEAD=%s\n' "$TARGET_HEAD"

CURRENT_PHASE="plan"
section "4. CHANGE-AWARE DEPLOYMENT PLAN"
PLAN_OUTPUT="$(./deploy/cmos-deploy plan --base "$RELEASE_BASE" --target "$TARGET_HEAD")"
printf '%s\n' "$PLAN_OUTPUT"
grep -Fxq 'build=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'services=citymanager-dashboard' <<< "$PLAN_OUTPUT"
grep -Fxq 'backup_required=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'external=postgis-migration' <<< "$PLAN_OUTPUT"
grep -Fxq 'full_e2e=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'unknown=none' <<< "$PLAN_OUTPUT"

CURRENT_PHASE="database"
section "5. GUARDED POSTGIS CATALOG AND TOPOLOGY INSTALL"
if database_ready; then
  log "Database already satisfies ${RELEASE_ID}; additive migration and source refresh skipped"
else
  ./deploy/gis/install_spatial_reference_catalog.sh "$TARGET_HEAD"
  database_ready || fail "database did not satisfy #58 readiness after installer"
fi
state_set DATABASE_TARGET "$TARGET_HEAD"

CURRENT_PHASE="application"
section "6. CHANGE-AWARE DASHBOARD DEPLOYMENT"
if application_ready; then
  log "Dashboard already serves ${RELEASE_ID}; build and restart skipped"
else
  ./deploy/cmos-deploy apply --base "$RELEASE_BASE" --target "$TARGET_HEAD" --external-applied
  application_ready || fail "dashboard release marker is unavailable after change-aware deployment"
fi
state_set APPLICATION_TARGET "$TARGET_HEAD"

CURRENT_PHASE="acceptance"
section "7. TARGETED #58 ACCEPTANCE"
SAMPLE="$(db_at <<'SQL'
WITH parcel AS (
  SELECT objectid,ST_Y(ST_PointOnSurface(geom)) AS lat,ST_X(ST_PointOnSurface(geom)) AS lon
  FROM gis_parcels WHERE geom IS NOT NULL AND upper(coalesce(mun_name,'')) LIKE '%WEEHAWKEN%'
  ORDER BY objectid LIMIT 1
), reference AS (
  SELECT entity_id FROM spatial_reference_entities
  WHERE active=true AND upper(coalesce(municipality,'')) LIKE '%WEEHAWKEN%'
    AND parcel_objectid IS NOT NULL
  ORDER BY importance_tier,canonical_name LIMIT 1
)
SELECT parcel.objectid||'|'||parcel.lat||'|'||parcel.lon||'|'||reference.entity_id
FROM parcel CROSS JOIN reference;
SQL
)"
[[ -n "$SAMPLE" ]] || fail "targeted acceptance could not find a Weehawken parcel and active reference"
IFS='|' read -r SAMPLE_PARCEL SAMPLE_LAT SAMPLE_LON SAMPLE_REFERENCE <<< "$SAMPLE"

docker exec -i citymanager-dashboard python - "$SAMPLE_PARCEL" "$SAMPLE_LAT" "$SAMPLE_LON" "$SAMPLE_REFERENCE" "$RELEASE_ID" <<'PY'
import json, os, sys, urllib.parse, urllib.request
parcel,lat,lon,reference,release_id=sys.argv[1:]
token=os.environ.get("CMOS_AUTOMATION_TOKEN","").strip()
headers={"X-CMOS-Automation-Key":token} if token else {}
base="http://127.0.0.1:8000"

def fetch(path, kind="json"):
    request=urllib.request.Request(base+path,headers=headers)
    with urllib.request.urlopen(request,timeout=60) as response:
        assert response.status == 200,(path,response.status)
        assert "/login" not in response.geturl(),(path,response.geturl())
        body=response.read()
    print("TARGETED PASS",path)
    return body if kind == "html" else json.loads(body)

release=fetch("/api/spatial-reference/release")
assert release["release_id"] == release_id
assert b"Regional Spatial Reference Catalog" in fetch("/spatial-reference","html")
assert b"Watch This" in fetch(f"/spatial-reference/{reference}","html")
search=fetch("/api/spatial-reference/search")
assert search["count"] > 0 and isinstance(search["items"],list)
point=fetch("/api/parcel/for-point?"+urllib.parse.urlencode({"lat":lat,"lon":lon,"tolerance_ft":3}))
assert point["parcel_objectid"] == int(parcel) and point["relation_type"] == "SAME_PARCEL"
radius=fetch("/api/parcels/within-radius?"+urllib.parse.urlencode({"lat":lat,"lon":lon,"radius_ft":500}))
assert radius["items"] and radius["items"][0]["relation_type"] == "SAME_PARCEL"
context=fetch(f"/api/parcel/{parcel}/context?radius_ft=500")
assert context["parcel"]["objectid"] == int(parcel)
for key in ("addresses","adjoining_parcels","nearby_parcels","reference_entities"):
    assert isinstance(context[key],list),key
for key in ("active_watches","open_issues","recent_alerts","events","transit","flood_zones"):
    assert isinstance(context["spatial_impact"][key],list),key
history=fetch(f"/api/spatial-reference/{reference}/nearby-history?radius_ft=500&days=30")
assert history["radius_ft"] == 500
buffered=fetch(f"/api/spatial-reference/{reference}/impact-buffer.geojson?radius_ft=500")
assert buffered["type"] == "FeatureCollection" and len(buffered["features"]) == 1
bbox=','.join((str(float(lon)-.03),str(float(lat)-.03),str(float(lon)+.03),str(float(lat)+.03)))
layer=fetch("/map/system/spatial-references.geojson?bbox="+urllib.parse.quote(bbox))
assert layer["type"] == "FeatureCollection" and isinstance(layer["features"],list)
PY

AUDIT_STATE="$(db_at <<'SQL'
SELECT count(*)>0 FROM spatial_reference_entities WHERE active=true;
SELECT count(*)=0 FROM spatial_reference_entities
WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom) OR ST_SRID(geom)<>4326;
SELECT count(DISTINCT p.proname)=7
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='public' AND p.proname IN (
  'spatial_reference_refresh_local_sources','gis_parcel_for_point','gis_addresses_for_parcel',
  'gis_adjoining_parcels','gis_parcels_within_radius','gis_spatial_impact_context','gis_parcel_context'
);
SELECT count(*)=0 FROM (
  SELECT source_provider,source_record_id FROM spatial_reference_entities
  WHERE source_record_id IS NOT NULL
  GROUP BY source_provider,source_record_id HAVING count(*)>1
) duplicate_sources;
SELECT count(*)>0 FROM spatial_reference_entities
WHERE active=true AND upper(coalesce(municipality,'')) LIKE '%WEEHAWKEN%'
  AND parcel_objectid IS NOT NULL;
SELECT count(*)=(
  SELECT count(*) FROM transit_assets WHERE active=true AND geom IS NOT NULL AND NOT ST_IsEmpty(geom)
) FROM spatial_reference_entities WHERE active=true AND source_provider='CMOS_TRANSIT_ASSET';
SELECT coalesce(bool_and(source_reference IS NOT NULL AND refreshed_at IS NOT NULL),false)
FROM spatial_reference_entities WHERE active=true;
SQL
)"
EXPECTED_AUDIT=$'t\nt\nt\nt\nt\nt\nt'
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
[[ "$FINAL_COUNTS" == "$BASE_COUNTS" ]] || fail "watch, routing, alert, match, or delivery counts changed during installation"
UNCHANGED_AFTER="$(for name in citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis n8n ntfy; do
  docker inspect --format '{{.Name}}|{{.Image}}|{{.State.StartedAt}}' "$name"
done)"
[[ "$UNCHANGED_AFTER" == "$UNCHANGED_BEFORE" ]] || fail "an out-of-scope container changed"

CURRENT_PHASE="promotion"
section "8. PROMOTE ACCEPTED MAIN"
[[ "$(git rev-parse HEAD)" == "$TARGET_HEAD" ]]
[[ -z "$(git status --porcelain)" ]]
git fetch -q origin main
REMOTE_NOW="$(git rev-parse origin/main)"
if [[ "$REMOTE_NOW" == "$RELEASE_BASE" ]]; then
  git push origin main
elif [[ "$REMOTE_NOW" != "$TARGET_HEAD" ]]; then
  fail "origin/main changed during deployment; production target was not pushed"
fi
git fetch -q origin main
[[ "$(git rev-parse origin/main)" == "$TARGET_HEAD" ]] || fail "origin/main did not reach the accepted target"

CATALOG_SUMMARY="$(db_at <<'SQL'
SELECT count(*)||' total, '||count(*) FILTER (WHERE active)||' active, '||
       count(*) FILTER (WHERE parcel_objectid IS NOT NULL)||' parcel-linked, '||
       count(*) FILTER (WHERE transit_asset_id IS NOT NULL)||' transit-linked'
FROM spatial_reference_entities;
SQL
)"
CURRENT_PHASE="complete"
FINAL_STATUS="PASS"
printf '\n============================================================\n'
printf '#58 REGIONAL SPATIAL REFERENCE CATALOG + PARCEL TOPOLOGY: PASS\n'
printf 'BASE=%s\nTARGET=%s\n' "$EXPECTED_HEAD" "$TARGET_HEAD"
printf 'CATALOG=%s\n' "$CATALOG_SUMMARY"
printf 'DEPLOYED_SERVICES=citymanager-dashboard\n'
printf 'UNCHANGED_SERVICES=citymanager-staff,citymanager-ops-engine,citymanager-integration-engine,citymanager-postgis,n8n,ntfy\n'
printf 'DATABASE=existing citymanager PostGIS\n'
printf 'TRACKING=existing watch_items,watch_item_recipients,subscribers\n'
printf 'NOTIFICATIONS=existing Routing,Delivery Guard,ntfy\n'
printf 'LIVE_SPATIAL_MATCHER=unchanged;tracked separately in #56\n'
printf 'FULL_LOCAL_LOG=%s\n' "$LOG_FILE"
printf '============================================================\n'
