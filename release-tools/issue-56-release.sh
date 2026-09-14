#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

REPO="/opt/city-manager-os"
EXPECTED_BASE="c68b88461578d135db114681d0a4bdae76a4b3ee"
CANDIDATE_SHA="c02fbde1c6bda440e2076400d2ed3371c30f09fa"
SUPERSEDED_CANDIDATE="26a76d501b1a959f66983976ac803d11da2319de"
CANDIDATE_BRANCH="release-candidate/56"
REPORT_BRANCH="release-output/56"
RELEASE_ID="issue-56-unified-spatial-watch-pack-v1"
MATCHER_ID="ESH9c2pZ8QfkMosO"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
STATE_DIR="/var/lib/city-manager-os/releases"
LOG_FILE="${LOG_DIR}/issue-56-${RUN_ID}.log"
STATE_FILE="${STATE_DIR}/issue-56.state"
LOCK_FILE="/var/lock/cmos-issue-56-release.lock"
N8N_ROLLBACK_FILE="${STATE_DIR}/issue-56-pre-matcher-${RUN_ID}.json"
CURRENT_PHASE="startup"
FAIL_LINE=""
FAIL_COMMAND=""
FINAL_STATUS="FAIL"
DATABASE_ACTION="not-started"
MATCHER_ACTION="not-started"
APPLICATION_ACTION="not-started"
E2E_ACTION="not-started"
IMAGE_BUILT=0
DASHBOARD_CHANGED=0
MATCHER_CHANGED=0
OLD_DASH_IMAGE=""
OLD_LATEST_IMAGE=""
N8N_DIR=""
N8N_DB=""

EXPECTED_PATHS=$'dashboard/Dockerfile\ndashboard/map_app.py\ndashboard/phase3_app.py\ndashboard/spatial_watch_app.py\ndashboard/templates/map.html\ndashboard/templates/watchlist.html\ndashboard/tests/test_spatial_reference_catalog.py\ndashboard/tests/test_spatial_watch_pack.py\ndeploy/gis/install_spatial_watch_pack.sh\ndeploy/n8n/install_spatial_watch_matcher.sh\ndeploy/postgis/init/032_unified_spatial_watch_pack.sql\ndocs/UNIFIED_SPATIAL_WATCH_PACK.md\nmodules/ALERT_ROUTER.md\nmodules/WATCHLIST.md\nworkflows/core/CORE_Watchlist_Matcher_v1.json\nworkflows/live/CORE_Watchlist_Matcher_live.json'

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

state_get(){
  local key="$1"
  [[ -f "$STATE_FILE" ]] || return 0
  sed -n "s/^${key}=//p" "$STATE_FILE" | tail -n 1
}

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

wait_dashboard(){
  local ready=0
  for _ in $(seq 1 45); do
    if [[ "$(docker inspect citymanager-dashboard --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
      && docker exec citymanager-dashboard python -c \
        "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4); assert r.status==200" \
        >/dev/null 2>&1; then
      ready=$((ready+1))
      (( ready >= 3 )) && { log "READINESS PASS: citymanager-dashboard"; return 0; }
    else
      ready=0
    fi
    sleep 2
  done
  return 1
}

wait_n8n(){
  for _ in $(seq 1 60); do
    if docker exec n8n node -e \
      "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
      >/dev/null 2>&1; then
      log "READINESS PASS: n8n"
      return 0
    fi
    sleep 2
  done
  return 1
}

publish_matcher(){
  if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
    docker exec -u node n8n n8n publish:workflow --id="$MATCHER_ID" >/dev/null
  else
    docker exec -u node n8n n8n update:workflow --id="$MATCHER_ID" --active=true >/dev/null
  fi
}

database_ready(){
  [[ "$(db_at <<'SQL'
SELECT
  EXISTS (SELECT 1 FROM information_schema.columns
          WHERE table_schema='public' AND table_name='watch_items'
            AND column_name='spatial_target_geom')
  AND EXISTS (SELECT 1 FROM pg_trigger
              WHERE tgrelid='public.watch_items'::regclass
                AND tgname='trg_gis_prepare_spatial_watch' AND NOT tgisinternal)
  AND to_regprocedure('public.gis_active_spatial_watch_matches(text)') IS NOT NULL
  AND to_regprocedure('public.gis_active_spatial_watch_matches(text,geometry)') IS NOT NULL
  AND to_regprocedure('public.gis_spatial_history(geometry,double precision,interval,text,text,integer)') IS NOT NULL
  AND position('ST_DWithin' IN pg_get_functiondef(
        to_regprocedure('public.gis_active_spatial_watch_matches(text,geometry)')
      ))>0
  AND position('p_supplied_alert_geom' IN pg_get_functiondef(
        to_regprocedure('public.gis_active_spatial_watch_matches(text,geometry)')
      ))>0
  AND NOT EXISTS (
    SELECT 1 FROM watch_items
    WHERE nearby_enabled AND coalesce(spatial_target_geom,geom) IS NOT NULL
      AND spatial_geom IS NULL
  )
  AND has_function_privilege(
    'citymanager_app','gis_active_spatial_watch_matches(text,geometry)','EXECUTE'
  )
  AND has_function_privilege(
    'citymanager_app',
    'gis_spatial_history(geometry,double precision,interval,text,text,integer)','EXECUTE'
  );
SQL
)" == t ]]
}

matcher_ready(){
  python3 - "$N8N_DB" "$MATCHER_ID" <<'PY' >/dev/null
import json,sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
row=con.execute('SELECT active,activeVersionId,nodes FROM workflow_entity WHERE id=?',(sys.argv[2],)).fetchone()
if not row or not row['active'] or not row['activeVersionId']:
    raise SystemExit(1)
nodes={node.get('name'):node for node in json.loads(row['nodes'])}
load=(nodes.get('Load Active Watchlist + Recipients') or {}).get('parameters') or {}
match=(nodes.get('Match + Resolve Recipients') or {}).get('parameters') or {}
query=load.get('query',''); options=load.get('options') or {}; code=match.get('jsCode','')
required=('gis_active_spatial_watch_matches','supplied_alert_geom','spatial_match_reason')
if not all(value in query for value in required): raise SystemExit(1)
if 'queryReplacement' not in options: raise SystemExit(1)
if 'row.spatial_match_type' not in code or 'result.match_type || row.match_mode' not in code: raise SystemExit(1)
con.close()
PY
}

application_ready(){
  docker exec -i citymanager-dashboard python - "$RELEASE_ID" <<'PY' >/dev/null 2>&1
import json,os,sys,urllib.request
token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
headers={'X-CMOS-Automation-Key':token} if token else {}
request=urllib.request.Request('http://127.0.0.1:8000/api/spatial-watch/release',headers=headers)
with urllib.request.urlopen(request,timeout=10) as response:
    payload=json.load(response)
assert response.status==200 and payload.get('release_id')==sys.argv[1]
PY
}

rollback_runtime(){
  set +e
  trap - ERR
  if (( MATCHER_CHANGED == 1 )) && [[ -s "$N8N_ROLLBACK_FILE" ]]; then
    log "ROLLBACK: restoring the prior central Watchlist Matcher"
    local restore_path="/tmp/issue-56-matcher-restore-${RUN_ID}.json"
    docker cp "$N8N_ROLLBACK_FILE" "n8n:$restore_path" >/dev/null 2>&1
    docker exec -u node n8n n8n import:workflow --input="$restore_path" >/dev/null 2>&1
    publish_matcher >/dev/null 2>&1
    docker restart n8n >/dev/null 2>&1
    wait_n8n >/dev/null 2>&1
    docker exec n8n rm -f "$restore_path" >/dev/null 2>&1
    MATCHER_CHANGED=0
    log "ROLLBACK: prior matcher restored"
  fi
  if (( DASHBOARD_CHANGED == 1 )) && [[ -n "$OLD_DASH_IMAGE" ]]; then
    log "ROLLBACK: restoring the prior Dashboard image"
    docker image tag "$OLD_DASH_IMAGE" dashboard-citymanager-dashboard:latest >/dev/null 2>&1
    docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate citymanager-dashboard >/dev/null 2>&1
    wait_dashboard >/dev/null 2>&1
    DASHBOARD_CHANGED=0
    log "ROLLBACK: prior Dashboard restored"
  fi
  if (( IMAGE_BUILT == 1 )) && [[ -n "$OLD_LATEST_IMAGE" ]]; then
    docker image tag "$OLD_LATEST_IMAGE" dashboard-citymanager-dashboard:latest >/dev/null 2>&1
  fi
  set -e
}

publish_report(){ (
  set +e
  local status="$1" rc="$2" parent="" worktree="" report_dir report_file latest_file raw_sha changed
  report_cleanup(){
    if [[ -n "$worktree" && "$worktree" == /tmp/cmos56-report.*/* ]]; then
      git -C "$REPO" worktree remove --force "$worktree" >/dev/null 2>&1 || true
    fi
    if [[ -n "$parent" && "$parent" == /tmp/cmos56-report.* ]]; then
      rmdir "$parent" >/dev/null 2>&1 || true
    fi
  }
  trap report_cleanup EXIT
  [[ -d "$REPO/.git" || -f "$REPO/.git" ]] || return 1
  git -C "$REPO" fetch -q origin "refs/heads/${REPORT_BRANCH}:refs/remotes/origin/${REPORT_BRANCH}" || return 1
  parent="$(mktemp -d /tmp/cmos56-report.XXXXXX)"
  worktree="$parent/worktree"
  git -C "$REPO" worktree add --detach "$worktree" "origin/${REPORT_BRANCH}" >/dev/null 2>&1 || return 1
  report_dir="$worktree/release-results/issue-56"
  mkdir -p "$report_dir"
  report_file="$report_dir/${RUN_ID}-${status,,}.md"
  latest_file="$report_dir/latest.md"
  raw_sha="$(sha256sum "$LOG_FILE" | awk '{print $1}')"
  changed="$(safe_git_value status --short | redact)"
  {
    printf '# City Manager OS issue #56 release report\n\n'
    printf '> This repository is public. This report is intentionally redacted. The complete mode-600 log remains on the VPS.\n\n'
    printf '| Field | Value |\n|---|---|\n'
    printf '| Run | `%s` |\n' "$RUN_ID"
    printf '| Release | `%s` |\n' "$RELEASE_ID"
    printf '| Status | **%s** |\n' "$status"
    printf '| Exit code | `%s` |\n' "$rc"
    printf '| Failed line | `%s` |\n' "${FAIL_LINE:-none}"
    printf '| Phase | `%s` |\n' "$CURRENT_PHASE"
    printf '| Database | `%s` |\n' "$DATABASE_ACTION"
    printf '| Central matcher | `%s` |\n' "$MATCHER_ACTION"
    printf '| Dashboard | `%s` |\n' "$APPLICATION_ACTION"
    printf '| Secure E2E | `%s` |\n' "$E2E_ACTION"
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '| Accepted base | `%s` |\n' "$EXPECTED_BASE"
    printf '| Candidate | `%s` |\n' "$CANDIDATE_SHA"
    printf '| Local HEAD | `%s` |\n' "$(safe_git_value rev-parse HEAD)"
    printf '| Origin main | `%s` |\n' "$(safe_git_value rev-parse origin/main)"
    printf '| Full local log | `%s` |\n' "$LOG_FILE"
    printf '| Full log SHA-256 | `%s` |\n\n' "$raw_sha"
    printf '## Working tree\n\n```text\n%s\n```\n\n' "${changed:-clean}"
    printf '## Redacted diagnostic output\n\n```text\n'
    grep -Eai '(^|[[:space:]])(ERROR|FATAL|FAIL|FAILED|EXCEPTION|TRACEBACK|ASSERT|SYNTAX|UNEXPECTED|MISMATCH|PASS|PLAN|READINESS|HEALTH|SPATIAL|MATCHER|DATABASE|DASHBOARD|E2E|ROLLBACK)|^(base|target|changed|build|services|tests|probes|backup_required|external|full_e2e|unknown)=' "$LOG_FILE" \
      | grep -v '^FAILED_COMMAND=' | redact | tail -n 240 || true
    printf '```\n'
  } > "$report_file"
  install -m 600 "$report_file" "$latest_file"
  git -C "$worktree" add "release-results/issue-56/$(basename "$report_file")" "release-results/issue-56/latest.md"
  git -C "$worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit -m "Record #56 release ${status,,} ${RUN_ID}" >/dev/null || return 1
  git -C "$worktree" push -q origin "HEAD:refs/heads/${REPORT_BRANCH}" || return 1
  log "REDACTED_GITHUB_REPORT=https://github.com/gioahmad/city-manager-os/blob/${REPORT_BRANCH}/release-results/issue-56/latest.md"
) }

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
  if (( rc == 0 )); then
    FINAL_STATUS="PASS"
  else
    rollback_runtime || true
  fi
  printf '\n============================================================\n'
  printf '#56 RELEASE: %s rc=%s phase=%s line=%s\n' "$FINAL_STATUS" "$rc" "$CURRENT_PHASE" "${FAIL_LINE:-none}"
  printf 'FULL_LOCAL_LOG=%s\nGITHUB_REPORT=REDACTED\n' "$LOG_FILE"
  printf '============================================================\n'
  if ! publish_report "$FINAL_STATUS" "$rc"; then
    log "WARNING: redacted GitHub report could not be published; full local log remains at ${LOG_FILE}"
  fi
  exit "$rc"
}

trap on_error ERR
trap on_exit EXIT

CURRENT_PHASE="preflight"
section "#56 UNIFIED SPATIAL WATCH PACK"
for cmd in git docker python3 grep install tee sha256sum mktemp awk sed tail sort flock; do
  command -v "$cmd" >/dev/null || fail "$cmd is required"
done
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another #56 release runner is active"
cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "production repository must be clean"

git fetch -q origin main \
  "+refs/heads/${CANDIDATE_BRANCH}:refs/remotes/origin/${CANDIDATE_BRANCH}" \
  "+refs/heads/${REPORT_BRANCH}:refs/remotes/origin/${REPORT_BRANCH}"
[[ "$(git rev-parse "origin/${CANDIDATE_BRANCH}")" == "$CANDIDATE_SHA" ]] \
  || fail "published #56 candidate moved unexpectedly"
[[ "$(git rev-parse "${CANDIDATE_SHA}^")" == "$SUPERSEDED_CANDIDATE" ]] \
  || fail "#56 repaired candidate is not based on the inspected failed candidate"
[[ "$(git rev-parse "${SUPERSEDED_CANDIDATE}^")" == "$EXPECTED_BASE" ]] \
  || fail "#56 candidate chain is not based on the accepted production commit"
[[ "$(git diff --name-only "$EXPECTED_BASE".."$CANDIDATE_SHA" | sort)" == "$EXPECTED_PATHS" ]] \
  || fail "#56 candidate path manifest mismatch"

HEAD_SHA="$(git rev-parse HEAD)"
ORIGIN_SHA="$(git rev-parse origin/main)"
if [[ "$ORIGIN_SHA" == "$EXPECTED_BASE" ]]; then
  [[ "$HEAD_SHA" == "$EXPECTED_BASE" || "$HEAD_SHA" == "$SUPERSEDED_CANDIDATE" || "$HEAD_SHA" == "$CANDIDATE_SHA" ]] \
    || fail "local main is not the accepted base or exact resumable #56 candidate"
elif [[ "$ORIGIN_SHA" == "$CANDIDATE_SHA" ]]; then
  [[ "$HEAD_SHA" == "$EXPECTED_BASE" || "$HEAD_SHA" == "$SUPERSEDED_CANDIDATE" || "$HEAD_SHA" == "$CANDIDATE_SHA" ]] \
    || fail "local main is not compatible with accepted #56 production"
else
  fail "origin/main moved beyond the accepted #56 release boundary"
fi
if [[ "$HEAD_SHA" != "$CANDIDATE_SHA" ]]; then
  git merge --ff-only "$CANDIDATE_SHA"
fi
[[ "$(git rev-parse HEAD)" == "$CANDIDATE_SHA" ]] || fail "local main did not reach the exact #56 candidate"
[[ -z "$(git status --porcelain)" ]] || fail "repository changed during candidate preparation"

REQUIRED_CONTAINERS=(citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis n8n ntfy)
for name in "${REQUIRED_CONTAINERS[@]}"; do
  [[ "$(docker inspect "$name" --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
    || fail "required container is not running: $name"
done
N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
N8N_DB="$N8N_DIR/database.sqlite"
[[ -f "$N8N_DB" ]] || fail "n8n database was not found"
UNCHANGED_BEFORE="$(for name in citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis ntfy; do docker inspect "$name" --format '{{.Name}}|{{.Image}}|{{.State.StartedAt}}'; done)"
BASE_COUNTS="$(db_at <<'SQL'
SELECT count(*)||'|'||(SELECT count(*) FROM watch_item_recipients)||'|'||
       (SELECT count(*) FROM subscribers)||'|'||(SELECT count(*) FROM alerts)||'|'||
       (SELECT count(*) FROM alert_watch_matches)||'|'||(SELECT count(*) FROM deliveries)
FROM watch_items;
SQL
)"
printf 'BASE=%s\nCANDIDATE=%s\nBASE_COUNTS=%s\n' "$EXPECTED_BASE" "$CANDIDATE_SHA" "$BASE_COUNTS"

CURRENT_PHASE="static-validation"
section "1. PINNED CANDIDATE AND CHANGE-AWARE PLAN"
git diff --check "$EXPECTED_BASE".."$CANDIDATE_SHA"
python3 -m py_compile dashboard/spatial_watch_app.py dashboard/map_app.py dashboard/phase3_app.py dashboard/tests/test_spatial_watch_pack.py
bash -n deploy/gis/install_spatial_watch_pack.sh deploy/n8n/install_spatial_watch_matcher.sh
python3 -m json.tool workflows/core/CORE_Watchlist_Matcher_v1.json >/dev/null
python3 -m json.tool workflows/live/CORE_Watchlist_Matcher_live.json >/dev/null
python3 - <<'PY'
import importlib.util
path='dashboard/tests/test_spatial_watch_pack.py'
spec=importlib.util.spec_from_file_location('issue56_static',path)
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
for name in sorted(item for item in dir(module) if item.startswith('test_')):
    getattr(module,name)()
    print('STATIC PASS',name)
PY
python3 - "$EXPECTED_BASE" "$CANDIDATE_SHA" <<'PY'
import subprocess,sys
base,target=sys.argv[1:]
old=subprocess.check_output(['git','show',f'{base}:dashboard/Dockerfile'],text=True).splitlines()
new=subprocess.check_output(['git','show',f'{target}:dashboard/Dockerfile'],text=True).splitlines()
expected=old.copy(); expected.insert(expected.index('COPY spatial_reference_app.py .')+1,'COPY spatial_watch_app.py .')
assert new==expected,'Dockerfile delta is not the single #56 module copy'
print('STATIC PASS targeted shared-image delta')
PY
PLAN_OUTPUT="$(./deploy/cmos-deploy plan --base "$EXPECTED_BASE" --target "$CANDIDATE_SHA")"
printf '%s\n' "$PLAN_OUTPUT"
grep -Fxq 'build=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'services=citymanager-dashboard,citymanager-integration-engine,citymanager-ops-engine,citymanager-staff' <<< "$PLAN_OUTPUT"
grep -Fxq 'backup_required=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'external=n8n-workflow-publish,postgis-migration' <<< "$PLAN_OUTPUT"
grep -Fxq 'full_e2e=yes' <<< "$PLAN_OUTPUT"
grep -Fxq 'unknown=none' <<< "$PLAN_OUTPUT"
log "CHANGE-AWARE ACTION: one cached build, Dashboard-only recreation, one n8n restart, no other service restart"

APP_ALREADY_READY=0
application_ready && APP_ALREADY_READY=1
if (( APP_ALREADY_READY == 0 )); then
  CURRENT_PHASE="targeted-build-test"
  section "2. ONE TARGETED BUILD AND TEST PASS"
  OLD_DASH_IMAGE="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
  OLD_LATEST_IMAGE="$(docker image inspect dashboard-citymanager-dashboard:latest --format '{{.Id}}')"
  docker compose -f dashboard/docker-compose.yml build citymanager-dashboard
  IMAGE_BUILT=1
  docker compose -f dashboard/docker-compose.yml run --rm --no-deps -T \
    -v "$REPO:/repo:ro" -w /repo/dashboard -e PYTHONPATH=/repo/dashboard:/app \
    --entrypoint pytest citymanager-dashboard -p no:cacheprovider -q \
    tests/test_spatial_watch_pack.py tests/test_spatial_reference_catalog.py \
    tests/test_alert_geo_resolution.py tests/test_gis_import.py
  docker compose -f dashboard/docker-compose.yml run --rm --no-deps -T \
    -v "$REPO:/repo:ro" -w /repo/dashboard -e PYTHONPATH=/repo/dashboard:/app \
    --entrypoint python citymanager-dashboard - <<'PY'
from jinja2 import Environment,FileSystemLoader
env=Environment(loader=FileSystemLoader('/repo/dashboard/templates'))
for name in ('map.html','watchlist.html'):
    env.get_template(name)
print('JINJA TARGETED VALIDATION: PASS')
import phase3_app
paths={(route.path,tuple(sorted(route.methods or ()))) for route in phase3_app.app.routes}
assert any(path=='/watchlist' for path,_ in paths)
assert any(path=='/api/spatial-watch/release' for path,_ in paths)
print('DASHBOARD RUNTIME IMPORT: PASS')
PY
else
  APPLICATION_ACTION="skipped-already-ready"
  log "Dashboard already serves the exact #56 release; build and restart will be skipped"
fi

CURRENT_PHASE="backups"
section "3. GUARDED BACKUPS"
./deploy/postgis/verify-backup.sh
if ! matcher_ready; then
  rollback_tmp="/tmp/issue-56-pre-matcher-${RUN_ID}.json"
  docker exec -u node n8n n8n export:workflow --id="$MATCHER_ID" --output="$rollback_tmp" >/dev/null
  docker cp "n8n:$rollback_tmp" "$N8N_ROLLBACK_FILE"
  docker exec n8n rm -f "$rollback_tmp" >/dev/null 2>&1 || true
  chmod 600 "$N8N_ROLLBACK_FILE"
  [[ -s "$N8N_ROLLBACK_FILE" ]] || fail "central matcher rollback export is empty"
  log "N8N MATCHER BACKUP: PASS"
else
  log "N8N MATCHER BACKUP: skipped, exact matcher already active"
fi

CURRENT_PHASE="database"
section "4. ADDITIVE POSTGIS SPATIAL MATCHING"
if database_ready; then
  DATABASE_ACTION="skipped-already-ready"
  log "Database already satisfies the #56 contract"
else
  ./deploy/gis/install_spatial_watch_pack.sh "$CANDIDATE_SHA"
  database_ready || fail "database did not satisfy #56 readiness after installation"
  DATABASE_ACTION="additive-migration-and-transactional-tests"
fi
state_set DATABASE_TARGET "$CANDIDATE_SHA"

CURRENT_PHASE="matcher"
section "5. EXISTING CENTRAL MATCHER UPGRADE"
if matcher_ready; then
  MATCHER_ACTION="skipped-already-ready"
  log "The active central matcher already satisfies #56"
else
  ./deploy/n8n/install_spatial_watch_matcher.sh "$CANDIDATE_SHA"
  MATCHER_CHANGED=1
  matcher_ready || fail "central matcher did not satisfy #56 after publication"
  MATCHER_ACTION="published-existing-matcher-and-restarted-n8n"
fi
state_set MATCHER_TARGET "$CANDIDATE_SHA"

CURRENT_PHASE="application"
section "6. TARGETED DASHBOARD RECREATION"
if application_ready; then
  [[ "$APPLICATION_ACTION" != not-started ]] || APPLICATION_ACTION="skipped-already-ready"
else
  DASHBOARD_CHANGED=1
  docker compose -f dashboard/docker-compose.yml up -d --no-deps --force-recreate citymanager-dashboard
  wait_dashboard || fail "Dashboard did not become ready"
  application_ready || fail "Dashboard does not serve the #56 release marker"
  APPLICATION_ACTION="one-cached-build-dashboard-only-restart"
fi
state_set APPLICATION_TARGET "$CANDIDATE_SHA"

CURRENT_PHASE="targeted-acceptance"
section "7. TARGETED READ-ONLY PRODUCTION ACCEPTANCE"
SAMPLE_POINT="$(db_at <<'SQL'
SELECT ST_Y(ST_PointOnSurface(geom))||'|'||ST_X(ST_PointOnSurface(geom))
FROM gis_parcels WHERE geom IS NOT NULL
ORDER BY CASE WHEN upper(coalesce(mun_name,''))='WEEHAWKEN' THEN 0 ELSE 1 END,objectid
LIMIT 1;
SQL
)"
[[ -n "$SAMPLE_POINT" ]] || fail "no parcel point is available for read-only spatial acceptance"
IFS='|' read -r SAMPLE_LAT SAMPLE_LON <<< "$SAMPLE_POINT"
SAMPLE_ALERT="$(db_at <<'SQL'
SELECT alert_id FROM alerts
WHERE geom IS NOT NULL AND position('/' IN alert_id)=0
ORDER BY received_at DESC LIMIT 1;
SQL
)"

docker exec -i citymanager-dashboard python - "$RELEASE_ID" "$SAMPLE_LAT" "$SAMPLE_LON" "$SAMPLE_ALERT" <<'PY'
import json,os,sys,urllib.parse,urllib.request
release_id,lat,lon,alert_id=sys.argv[1:]
token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
headers={'X-CMOS-Automation-Key':token} if token else {}
base='http://127.0.0.1:8000'
def fetch(path,kind='json'):
    request=urllib.request.Request(base+path,headers=headers)
    with urllib.request.urlopen(request,timeout=60) as response:
        assert response.status==200,(path,response.status)
        assert '/login' not in response.geturl(),(path,response.geturl())
        body=response.read()
    print('TARGETED PASS',path.split('?',1)[0])
    return body if kind=='html' else json.loads(body)
release=fetch('/api/spatial-watch/release')
assert release['release_id']==release_id
assert release['watch_source_of_truth']=='watch_items'
watchlist=fetch('/watchlist?state=spatial','html')
assert b'Location / Proximity' in watchlist and b'Subscribers' in watchlist
mapping=fetch('/map','html')
assert b'Pick Watch Point' in mapping and b'Show Impact Area' in mapping
params=urllib.parse.urlencode({'lat':lat,'lon':lon,'radius_ft':500,'hours':24})
history=fetch('/api/spatial-watch-point/nearby-history?'+params)
for key in ('references','active_watches','open_issues','recent_alerts','events','transit','flood_zones'):
    assert isinstance(history[key],list),key
layer=fetch('/map/system/watchlist.geojson')
assert layer['type']=='FeatureCollection' and isinstance(layer['features'],list)
if alert_id:
    encoded=urllib.parse.quote(alert_id,safe='')
    impact=fetch('/api/alerts/'+encoded+'/spatial-impact?radius_ft=500&hours=24')
    assert isinstance(impact['recent_alerts'],list)
    buffered=fetch('/api/alerts/'+encoded+'/impact-buffer.geojson?radius_ft=500')
    assert buffered['type']=='FeatureCollection' and len(buffered['features'])==1
else:
    print('TARGETED PASS alert impact route code and SQL validated; no stored precise alert sample available')
PY

AUDIT_STATE="$(db_at <<'SQL'
SELECT to_regprocedure('public.gis_active_spatial_watch_matches(text,geometry)') IS NOT NULL;
SELECT to_regprocedure('public.gis_spatial_history(geometry,double precision,interval,text,text,integer)') IS NOT NULL;
SELECT NOT EXISTS (
  SELECT 1 FROM watch_items
  WHERE nearby_enabled AND coalesce(spatial_target_geom,geom) IS NOT NULL AND spatial_geom IS NULL
);
SELECT NOT EXISTS (
  SELECT 1 FROM watch_items
  WHERE spatial_target_geom IS NOT NULL
    AND (ST_IsEmpty(spatial_target_geom) OR NOT ST_IsValid(spatial_target_geom) OR ST_SRID(spatial_target_geom)<>4326)
);
SELECT NOT EXISTS (SELECT 1 FROM watch_items WHERE left(watch_id,7)='CMOS56_');
SELECT NOT EXISTS (SELECT 1 FROM alerts WHERE alert_id LIKE 'CMOS56:%');
SQL
)"
[[ "$AUDIT_STATE" == $'t\nt\nt\nt\nt\nt' ]] || fail "targeted database audit failed: ${AUDIT_STATE}"
matcher_ready || fail "central matcher audit failed"
database_ready || fail "database contract audit failed"
application_ready || fail "Dashboard contract audit failed"
log "TARGETED SPATIAL ACCEPTANCE: PASS"

CURRENT_PHASE="e2e"
section "8. ONE REQUIRED SECURE END-TO-END ACCEPTANCE"
if [[ "$(state_get E2E_TARGET)" == "$CANDIDATE_SHA" ]]; then
  E2E_ACTION="reused-identical-candidate-pass"
  log "Reusing secure E2E evidence for the identical candidate"
else
  env CMOS_E2E_EXPECTED_HEAD="$CANDIDATE_SHA" ./deploy/cmos-e2e-secure
  state_set E2E_TARGET "$CANDIDATE_SHA"
  E2E_ACTION="one-full-secure-e2e-pass"
fi
./deploy/cmos-health

CURRENT_PHASE="final-audit"
section "9. FINAL CHANGE-SCOPE AUDIT"
UNCHANGED_AFTER="$(for name in citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis ntfy; do docker inspect "$name" --format '{{.Name}}|{{.Image}}|{{.State.StartedAt}}'; done)"
[[ "$UNCHANGED_AFTER" == "$UNCHANGED_BEFORE" ]] || fail "an out-of-scope container changed"
[[ -z "$(git status --porcelain)" ]] || fail "repository is not clean"
FINAL_COUNTS="$(db_at <<'SQL'
SELECT count(*)||'|'||(SELECT count(*) FROM watch_item_recipients)||'|'||
       (SELECT count(*) FROM subscribers)||'|'||(SELECT count(*) FROM alerts)||'|'||
       (SELECT count(*) FROM alert_watch_matches)||'|'||(SELECT count(*) FROM deliveries)
FROM watch_items;
SQL
)"
printf 'FINAL_COUNTS=%s\n' "$FINAL_COUNTS"
printf 'UNCHANGED_SERVICES=citymanager-staff,citymanager-ops-engine,citymanager-integration-engine,citymanager-postgis,ntfy\n'
log "FINAL SCOPE AUDIT: PASS"

CURRENT_PHASE="promotion"
section "10. PROMOTE ACCEPTED PRODUCTION MAIN"
git fetch -q origin main
REMOTE_NOW="$(git rev-parse origin/main)"
if [[ "$REMOTE_NOW" == "$EXPECTED_BASE" ]]; then
  git push origin "$CANDIDATE_SHA:refs/heads/main"
elif [[ "$REMOTE_NOW" != "$CANDIDATE_SHA" ]]; then
  fail "origin/main changed during #56 deployment"
fi
git fetch -q origin main
[[ "$(git rev-parse origin/main)" == "$CANDIDATE_SHA" ]] || fail "origin/main did not reach the accepted candidate"

CURRENT_PHASE="complete"
FINAL_STATUS="PASS"
RESTARTED_SERVICES="none"
if (( DASHBOARD_CHANGED == 1 && MATCHER_CHANGED == 1 )); then
  RESTARTED_SERVICES="citymanager-dashboard,n8n"
elif (( DASHBOARD_CHANGED == 1 )); then
  RESTARTED_SERVICES="citymanager-dashboard"
elif (( MATCHER_CHANGED == 1 )); then
  RESTARTED_SERVICES="n8n"
fi
printf '\n============================================================\n'
printf '#56 UNIFIED SPATIAL WATCH PACK: PASS\n'
printf 'BASE=%s\nTARGET=%s\n' "$EXPECTED_BASE" "$CANDIDATE_SHA"
printf 'DATABASE=%s\nMATCHER=%s\nDASHBOARD=%s\nE2E=%s\n' \
  "$DATABASE_ACTION" "$MATCHER_ACTION" "$APPLICATION_ACTION" "$E2E_ACTION"
printf 'ARCHITECTURE=SEE IT -> TRACK IT -> TELL ME\n'
printf 'TRACKING=existing watch_items,watch_item_recipients,subscribers\n'
printf 'NOTIFICATIONS=existing Routing,Delivery Guard,ntfy\n'
printf 'RESTARTED_SERVICES=%s\n' "$RESTARTED_SERVICES"
printf '============================================================\n'
