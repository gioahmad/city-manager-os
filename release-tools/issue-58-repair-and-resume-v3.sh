#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

REPO="/opt/city-manager-os"
ACCEPTED_HEAD="aa19402d164b0fcc53891c873a534043fa50627b"
BOOTSTRAP_HEAD="bded35024236039f89cc34e2030570b12ce87777"
FAILED_RELEASE_HEAD="8cb53f440328678b2ffdd5f0d4528d1db884043a"
APPLICATION_TEST_FAILURE_HEAD="cff2737eba74bc9997d054c706a5496745e90a77"
REPORT_BRANCH="release-output/58"
RELEASE_ID="issue-58-regional-spatial-reference-v1"
REPAIR_ID="issue-58-parcel-context-repair-v3"
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
APPLICATION_ACTION="not-started"
FINAL_STATUS="FAIL"

RELEASE_PATHS=$'dashboard/map_app.py\ndashboard/phase3_app.py\ndashboard/spatial_reference_app.py\ndashboard/templates/nav.html\ndashboard/templates/spatial_reference.html\ndashboard/templates/spatial_reference_detail.html\ndashboard/tests/test_spatial_reference_catalog.py\ndeploy/gis/install_spatial_reference_catalog.sh\ndeploy/postgis/init/031_spatial_reference_catalog.sql\ndocs/SPATIAL_REFERENCE_CATALOG.md'
REPAIR_PATHS=$'dashboard/tests/test_spatial_reference_catalog.py\ndeploy/gis/install_spatial_reference_catalog.sh\ndeploy/postgis/init/031_spatial_reference_catalog.sql'
TEST_REPAIR_PATH="dashboard/tests/test_spatial_reference_catalog.py"

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
    printf '| Application action | `%s` |\n' "$APPLICATION_ACTION"
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '| Accepted base | `%s` |\n' "$ACCEPTED_HEAD"
    printf '| Bootstrap base | `%s` |\n' "$BOOTSTRAP_HEAD"
    printf '| Failed release | `%s` |\n' "$FAILED_RELEASE_HEAD"
    printf '| Application-test failure | `%s` |\n' "$APPLICATION_TEST_FAILURE_HEAD"
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
       WHERE active=true AND geom IS NOT NULL AND NOT ST_IsEmpty(geom));
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
   WHERE active=true AND geom IS NOT NULL AND NOT ST_IsEmpty(geom))
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
  database_structure_ready && database_privileges_ready && catalog_core_ready && parcel_context_ready
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

hash_is(){
  local path="$1" expected="$2"
  [[ -f "$path" && "$(sha256sum "$path" | awk '{print $1}')" == "$expected" ]]
}

section "#58 REGIONAL SPATIAL REFERENCE REPAIR AND RESUME"
printf 'REPAIR_ID=%s\nSTARTED_UTC=%s\n' "$REPAIR_ID" "$STARTED_UTC"

CURRENT_PHASE="preflight"
section "1. EXACT FAILED-RUN PREFLIGHT"
for cmd in git docker python3 bash sed grep install tee sha256sum mktemp awk tail sort flock; do
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
[[ "$(git diff --name-only "$BOOTSTRAP_HEAD".."$FAILED_RELEASE_HEAD" | sort)" == "$RELEASE_PATHS" ]] \
  || fail "failed release path manifest mismatch"
[[ "$(git rev-parse "${APPLICATION_TEST_FAILURE_HEAD}^")" == "$FAILED_RELEASE_HEAD" ]] \
  || fail "application-test repair parent no longer matches the failed release"
[[ "$(git diff --name-only "$FAILED_RELEASE_HEAD".."$APPLICATION_TEST_FAILURE_HEAD" | sort)" == "$REPAIR_PATHS" ]] \
  || fail "application-test repair path manifest mismatch"

if [[ "$HEAD_SHA" == "$APPLICATION_TEST_FAILURE_HEAD" ]]; then
  REPAIR_MODE="test-portability-build"
  [[ "$ORIGIN_SHA" == "$BOOTSTRAP_HEAD" ]] || fail "origin/main moved beyond the #58 bootstrap"
  hash_is deploy/postgis/init/031_spatial_reference_catalog.sql d5004eaf9320b29a12b93e806e76f4e2cd4895e2726975ff190bab88b6960837 \
    || fail "repaired SQL checksum mismatch"
  hash_is deploy/gis/install_spatial_reference_catalog.sh f49116d085069c977157f26976862d71e0a2607396b0d0ae13b02abe6064c846 \
    || fail "repaired installer checksum mismatch"
  hash_is dashboard/tests/test_spatial_reference_catalog.py bb52c4bbff76e1946131295c6ab73ecab4901184560c21aa288b662fe1330fa4 \
    || fail "failed container-path test checksum mismatch"
elif [[ "$(git rev-parse HEAD^)" == "$APPLICATION_TEST_FAILURE_HEAD" ]] \
  && [[ "$(git diff --name-only "$APPLICATION_TEST_FAILURE_HEAD"..HEAD | sort)" == "$TEST_REPAIR_PATH" ]]; then
  REPAIR_MODE="test-portability-resume"
  TARGET_HEAD="$HEAD_SHA"
  [[ "$ORIGIN_SHA" == "$BOOTSTRAP_HEAD" || "$ORIGIN_SHA" == "$TARGET_HEAD" ]] \
    || fail "origin/main is neither the #58 bootstrap nor this portability target"
else
  fail "local main is not the exact #58 application-test failure or its one-file portability repair"
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
UNCHANGED_BEFORE="$(for name in citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis n8n ntfy; do
  docker inspect --format '{{.Name}}|{{.Image}}|{{.State.StartedAt}}' "$name"
done)"
printf 'LOCAL_HEAD=%s\nORIGIN_MAIN=%s\nREPAIR_MODE=%s\nCATALOG_BASELINE=%s\n' \
  "$HEAD_SHA" "$ORIGIN_SHA" "$REPAIR_MODE" "$CATALOG_BEFORE"

CURRENT_PHASE="code"
section "2. GUARDED ONE-FILE TEST PORTABILITY REPAIR"
if [[ "$REPAIR_MODE" == test-portability-build ]]; then
  TEST_PATCH="$(cat <<'PATCH'
diff --git a/dashboard/tests/test_spatial_reference_catalog.py b/dashboard/tests/test_spatial_reference_catalog.py
index 9f14ee6..6f954e2 100644
--- a/dashboard/tests/test_spatial_reference_catalog.py
+++ b/dashboard/tests/test_spatial_reference_catalog.py
@@ -1,11 +1,20 @@
 from pathlib import Path
+from unittest import SkipTest
 
 
-ROOT = Path(__file__).resolve().parents[2]
+DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
+REPOSITORY_ROOT = DASHBOARD_ROOT.parent
+
+
+def _deployment_file(relative_path):
+    path = REPOSITORY_ROOT / relative_path
+    if not path.is_file():
+        raise SkipTest("deployment source is outside the dashboard-only test mount")
+    return path
 
 
 def test_catalog_migration_is_additive_and_reuses_watchlist():
-    sql = (ROOT / "deploy/postgis/init/031_spatial_reference_catalog.sql").read_text()
+    sql = _deployment_file("deploy/postgis/init/031_spatial_reference_catalog.sql").read_text()
     assert "CREATE TABLE IF NOT EXISTS spatial_reference_entities" in sql
     assert "ALTER TABLE watch_items" in sql
     assert "spatial_reference_entity_id" in sql
@@ -30,7 +39,7 @@ def test_catalog_migration_is_additive_and_reuses_watchlist():
 
 
 def test_installer_validates_function_sql_before_refreshing_sources():
-    installer = (ROOT / "deploy/gis/install_spatial_reference_catalog.sh").read_text()
+    installer = _deployment_file("deploy/gis/install_spatial_reference_catalog.sh").read_text()
     validation = "SELECT gis_parcel_context(NULL::integer,500.0) IS NULL;"
     refresh = "SELECT spatial_reference_refresh_local_sources() AS refresh_result;"
     assert validation in installer
@@ -39,7 +48,7 @@ def test_installer_validates_function_sql_before_refreshing_sources():
 
 
 def test_catalog_routes_and_normal_watch_promotion():
-    source = (ROOT / "dashboard/spatial_reference_app.py").read_text()
+    source = (DASHBOARD_ROOT / "spatial_reference_app.py").read_text()
     assert '@app.get("/spatial-reference"' in source
     assert '@app.get("/api/spatial-reference/release")' in source
     assert '@app.post("/spatial-reference/adopt")' in source
@@ -63,9 +72,9 @@ def test_catalog_routes_and_normal_watch_promotion():
 
 
 def test_composition_and_mapping_layer():
-    phase3 = (ROOT / "dashboard/phase3_app.py").read_text()
-    map_app = (ROOT / "dashboard/map_app.py").read_text()
-    nav = (ROOT / "dashboard/templates/nav.html").read_text()
+    phase3 = (DASHBOARD_ROOT / "phase3_app.py").read_text()
+    map_app = (DASHBOARD_ROOT / "map_app.py").read_text()
+    nav = (DASHBOARD_ROOT / "templates/nav.html").read_text()
     assert "import spatial_reference_app" in phase3
     assert '"key": "spatial-references"' in map_app
     assert "SELECT 'REFERENCE' AS result_type" in map_app
PATCH
)"
  printf '%s\n' "$TEST_PATCH" | git apply --check
  printf '%s\n' "$TEST_PATCH" | git apply
  [[ "$(git diff --name-only)" == "$TEST_REPAIR_PATH" ]] || fail "test portability repair changed an unexpected path"
  git diff --check
  hash_is dashboard/tests/test_spatial_reference_catalog.py db51f08ccc6ca40b62dc1d31ca1bce1cf6e8143b94c7e08351b00e88e722f4ef \
    || fail "portable regression test checksum mismatch"
  python3 -m py_compile dashboard/tests/test_spatial_reference_catalog.py
  python3 - <<'PY'
import importlib.util
path = "dashboard/tests/test_spatial_reference_catalog.py"
spec = importlib.util.spec_from_file_location("issue58_portable_static", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for name in sorted(item for item in dir(module) if item.startswith("test_")):
    getattr(module, name)()
    print("PORTABILITY STATIC PASS", name)
PY
  git add -- "$TEST_REPAIR_PATH"
  [[ "$(git diff --cached --name-only)" == "$TEST_REPAIR_PATH" ]] || fail "staged portability path differs from manifest"
  git diff --cached --check
  git -c user.name='City Manager OS Release' -c user.email='release@localhost' \
    commit -m "Make #58 contract tests dashboard-mount aware"
  TARGET_HEAD="$(git rev-parse HEAD)"
elif [[ "$REPAIR_MODE" == test-portability-resume ]]; then
  hash_is deploy/postgis/init/031_spatial_reference_catalog.sql d5004eaf9320b29a12b93e806e76f4e2cd4895e2726975ff190bab88b6960837 \
    || fail "resumable SQL checksum mismatch"
  hash_is deploy/gis/install_spatial_reference_catalog.sh f49116d085069c977157f26976862d71e0a2607396b0d0ae13b02abe6064c846 \
    || fail "resumable installer checksum mismatch"
  hash_is dashboard/tests/test_spatial_reference_catalog.py db51f08ccc6ca40b62dc1d31ca1bce1cf6e8143b94c7e08351b00e88e722f4ef \
    || fail "resumable portable regression test checksum mismatch"
fi
git merge-base --is-ancestor "$FAILED_RELEASE_HEAD" "$TARGET_HEAD" || fail "repair target is not based on the failed release"
[[ "$(git diff --name-only "$FAILED_RELEASE_HEAD".."$TARGET_HEAD" | sort)" == "$REPAIR_PATHS" ]] \
  || fail "repair commit path manifest mismatch"
[[ "$(git diff --name-only "$BOOTSTRAP_HEAD".."$TARGET_HEAD" | sort)" == "$RELEASE_PATHS" ]] \
  || fail "coordinated release path manifest mismatch"
[[ -z "$(git status --porcelain)" ]] || fail "repository is not clean after code repair"
printf 'TARGET_HEAD=%s\n' "$TARGET_HEAD"

CURRENT_PHASE="plan"
section "3. CHANGE-AWARE RELEASE PLAN"
PLAN_OUTPUT="$(./deploy/cmos-deploy plan --base "$BOOTSTRAP_HEAD" --target "$TARGET_HEAD")"
printf '%s\n' "$PLAN_OUTPUT"
grep -Fxq 'changed_count=10' <<< "$PLAN_OUTPUT"
grep -Fxq 'build=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'services=citymanager-dashboard' <<< "$PLAN_OUTPUT"
grep -Fxq 'backup_required=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'external=postgis-migration' <<< "$PLAN_OUTPUT"
grep -Fxq 'full_e2e=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'unknown=none' <<< "$PLAN_OUTPUT"

CURRENT_PHASE="database"
section "4. MINIMAL TRANSACTIONAL DATABASE REPAIR"
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
if catalog_core_ready; then
  CATALOG_REFRESH_ACTION="skipped-core-ready"
  log "Completed catalog data already passes core reconciliation; source refresh skipped"
else
  CATALOG_REFRESH_ACTION="required-reconciliation-started"
  log "Catalog core reconciliation is incomplete; running one idempotent local-source reconciliation"
  db_at <<'SQL'
SELECT spatial_reference_refresh_local_sources() IS NOT NULL;
SQL
  CATALOG_REFRESH_ACTION="one-required-reconciliation-completed"
  catalog_core_ready || fail "catalog core reconciliation still fails after its required refresh"
fi
database_ready || fail "database does not satisfy final #58 readiness"
CATALOG_DIAGNOSTICS_AFTER="$(catalog_diagnostics)"
printf '%s\n' "$CATALOG_DIAGNOSTICS_AFTER"
state_set DATABASE_TARGET "$TARGET_HEAD"
printf 'DATABASE_ACTION=%s\n' "$DATABASE_ACTION"
printf 'PRIVILEGE_ACTION=%s\n' "$PRIVILEGE_ACTION"
printf 'CATALOG_REFRESH_ACTION=%s\n' "$CATALOG_REFRESH_ACTION"

CURRENT_PHASE="application"
section "5. CHANGE-AWARE DASHBOARD DEPLOYMENT"
if application_ready; then
  APPLICATION_ACTION="skipped-already-ready"
  log "Dashboard already serves ${RELEASE_ID}"
else
  APPLICATION_ACTION="change-aware-apply-started"
  ./deploy/cmos-deploy apply --base "$BOOTSTRAP_HEAD" --target "$TARGET_HEAD" --external-applied
  APPLICATION_ACTION="dashboard-built-tested-restarted"
  application_ready || fail "dashboard release marker is unavailable after deployment"
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
SELECT count(*)>0 FROM gis_parcels
WHERE geom IS NOT NULL AND upper(coalesce(mun_name,'')) LIKE '%WEEHAWKEN%';
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
UNCHANGED_AFTER="$(for name in citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis n8n ntfy; do
  docker inspect --format '{{.Name}}|{{.Image}}|{{.State.StartedAt}}' "$name"
done)"
[[ "$UNCHANGED_AFTER" == "$UNCHANGED_BEFORE" ]] || fail "an out-of-scope container changed"

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

CURRENT_PHASE="complete"
FINAL_STATUS="PASS"
section "#58 REPAIR AND RELEASE COMPLETE"
printf 'TARGET_HEAD=%s\n' "$TARGET_HEAD"
printf 'DATABASE_ACTION=%s\n' "$DATABASE_ACTION"
printf 'PRIVILEGE_ACTION=%s\n' "$PRIVILEGE_ACTION"
printf 'CATALOG_REFRESH_ACTION=%s\n' "$CATALOG_REFRESH_ACTION"
printf 'APPLICATION_ACTION=%s\n' "$APPLICATION_ACTION"
printf 'CATALOG=%s\n' "$CATALOG_AFTER"
printf 'OUT_OF_SCOPE_CONTAINERS=UNCHANGED\n'
printf 'WATCH_ROUTING_DELIVERY_COUNTS=UNCHANGED\n'
printf 'ORIGIN_MAIN=%s\n' "$(git rev-parse origin/main)"
printf '#58 REPAIR: PASS\n'
