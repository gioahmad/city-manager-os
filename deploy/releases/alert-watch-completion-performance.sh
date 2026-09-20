#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C GIT_TERMINAL_PROMPT=0
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED_BASE="956241541799cc96727eef2117e188ad2c3662e7"
REPORT_BRANCH="release-output/ops"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
LOG_FILE="$LOG_DIR/alert-watch-completion-performance-$RUN_ID.log"
LOCK_FILE="/var/lock/cmos-alert-watch-completion-performance.lock"
CURRENT_PHASE="startup"
FINAL_STATUS="FAIL"
FAIL_LINE="none"
TARGET_HEAD="unknown"
ORIGINAL_HEAD="unknown"
ARCHIVE_BRANCH="none"
REPOSITORY_ACTION="not-started"
DASHBOARD_ACTION="not-started"
ACCEPTANCE_ACTION="not-started"
DASHBOARD_PREPARED=0
DASHBOARD_CHANGED=0
DASHBOARD_ROLLBACK_TAG=""
ACCEPTANCE_RESULT='{}'

EXPECTED_PATHS=$'dashboard/map_app.py\ndashboard/operations_app.py\ndashboard/spatial_watch_app.py\ndashboard/templates/alerts.html\ndashboard/templates/map.html\ndashboard/templates/search.html\ndashboard/templates/watchlist.html\ndashboard/tests/test_alert_watch_completion.py\ndashboard/tests/test_global_search_match_explanations.py\ndashboard/tests/test_subscriber_watch_bulk_locations.py\ndashboard/tests/test_watch_selection_attribution_bulk_actions.py\ndeploy/releases/alert-watch-completion-performance.sh'

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

dashboard_release_is_live(){
  docker exec -i citymanager-dashboard python - <<'PY'
import os,urllib.parse,urllib.request

def fail(reason):
    print(f'DASHBOARD_CONTRACT=FAIL:{reason}')
    raise SystemExit(1)

try:
    from operations_app import ALERT_FILTERED_BULK_LIMIT
    from spatial_watch_app import COMPLETION_RELEASE_ID, spatial_watchlist
    from map_app import map_system_events, map_issues_geojson, map_watchlist_geojson
    from app import query_one
except (ImportError,AttributeError):
    fail('release-not-installed')

if COMPLETION_RELEASE_ID != 'alert-watch-completion-performance-v1': fail('completion-release-contract')
if ALERT_FILTERED_BULK_LIMIT != 5000: fail('filtered-bulk-contract')
for value in (spatial_watchlist,map_system_events,map_issues_geojson,map_watchlist_geojson):
    if not callable(value): fail('route-contract')
token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: fail('automation-token-missing')
headers={'X-CMOS-Automation-Key':token}

def get(path):
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers)
    with urllib.request.urlopen(request,timeout=20) as response:
        body=response.read()
        if response.status!=200 or '/login' in response.geturl(): fail(f'{path}-authentication')
        if b'Internal Server Error' in body: fail(f'{path}-internal-server-error')
        return body.decode(errors='replace')

mapping=get('/map')
for marker in ('Search records in this map area','data-area-record="alerts"','data-area-record="event-intelligence"','data-area-record="operations"','data-area-record="watchlist"'):
    if marker not in mapping: fail('map-area-search-contract')
alerts=get('/alerts?window=24h')
for marker in ('Create Watch From Alert','name="selection_scope"','All '):
    if marker not in alerts: fail('alert-watch-or-bulk-contract')
get('/search?q=CMOS_RELEASE_PROBE')
county=query_one("SELECT id::text AS id FROM map_layers WHERE layer_key='NJ_OFFICIAL_COUNTIES' AND active=true")
towns=query_one("SELECT id::text AS id FROM map_layers WHERE layer_key='NJ_OFFICIAL_MUNICIPALITIES' AND active=true")
if not county or not towns: fail('official-layer-missing')
county_page=get('/watchlist?'+urllib.parse.urlencode({'bulk_layer':county['id']}))
if 'Choose individual towns in' not in county_page: fail('county-to-town-link')
town_page=get('/watchlist?'+urllib.parse.urlencode({'bulk_layer':towns['id'],'bulk_group_filter':'Bergen'}))
for marker in ('Bergen County towns','Show every county','name="feature_ids"'):
    if marker not in town_page: fail('filtered-town-picker')
print('DASHBOARD_CONTRACT=PASS')
PY
}

rollback_dashboard(){
  [[ "$DASHBOARD_PREPARED" == 1 && -n "$DASHBOARD_ROLLBACK_TAG" ]] || return 0
  docker image inspect "$DASHBOARD_ROLLBACK_TAG" >/dev/null 2>&1 || return 1
  docker image tag "$DASHBOARD_ROLLBACK_TAG" dashboard-citymanager-dashboard:latest
  if [[ "$DASHBOARD_CHANGED" != 1 ]]; then
    DASHBOARD_ACTION="build-rolled-back-no-recreate"
    return 0
  fi
  log "ROLLBACK: restoring the prior dashboard image"
  docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate citymanager-dashboard </dev/null
  for _ in $(seq 1 30); do
    if docker exec citymanager-dashboard python -c \
      "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200" \
      >/dev/null 2>&1; then
      DASHBOARD_ACTION="rolled-back"
      return 0
    fi
    sleep 2
  done
  return 1
}

restore_checkout(){
  [[ "$REPOSITORY_ACTION" == fast-forwarded || "$REPOSITORY_ACTION" == archived-and-realigned || "$REPOSITORY_ACTION" == archive-created ]] || return 0
  [[ "$ORIGINAL_HEAD" != unknown && "$TARGET_HEAD" != unknown ]] || return 0
  [[ -z "$(git -C "$REPO" status --porcelain)" ]] || return 1
  [[ "$(git -C "$REPO" rev-parse refs/heads/main)" == "$TARGET_HEAD" ]] || return 1
  git -C "$REPO" switch --detach "$TARGET_HEAD" >/dev/null
  git -C "$REPO" update-ref refs/heads/main "$ORIGINAL_HEAD" "$TARGET_HEAD"
  git -C "$REPO" switch main >/dev/null
  REPOSITORY_ACTION="restored-after-release-failure"
}

publish_report(){ (
  set +e
  local status="$1" rc="$2" parent="" worktree="" report_dir report_file
  cleanup_report(){
    [[ -z "$worktree" ]] || git -C "$REPO" worktree remove --force "$worktree" >/dev/null 2>&1 || true
    [[ -z "$parent" ]] || rmdir "$parent" >/dev/null 2>&1 || true
  }
  trap cleanup_report EXIT
  git -C "$REPO" fetch -q origin "+refs/heads/$REPORT_BRANCH:refs/remotes/origin/$REPORT_BRANCH" || return 1
  parent="$(mktemp -d /tmp/cmos-alert-watch-report.XXXXXX)"
  worktree="$parent/worktree"
  git -C "$REPO" worktree add --detach "$worktree" "origin/$REPORT_BRANCH" >/dev/null 2>&1 || return 1
  report_dir="$worktree/operation-results/alert-watch-completion-performance"
  mkdir -p "$report_dir"
  report_file="$report_dir/$RUN_ID-${status,,}.md"
  {
    printf '# Alert and Watch completion performance\n\n'
    printf '| Field | Value |\n|---|---|\n'
    printf '| Run | `%s` |\n' "$RUN_ID"
    printf '| Status | **%s** |\n' "$status"
    printf '| Exit code | `%s` |\n' "$rc"
    printf '| Phase | `%s` |\n' "$CURRENT_PHASE"
    printf '| Failed line | `%s` |\n' "$FAIL_LINE"
    printf '| Target | `%s` |\n' "$TARGET_HEAD"
    printf '| Repository alignment | `%s` |\n' "$REPOSITORY_ACTION"
    printf '| Recovery branch | `%s` |\n' "$ARCHIVE_BRANCH"
    printf '| Dashboard | `%s` |\n' "$DASHBOARD_ACTION"
    printf '| Focused acceptance | `%s` |\n' "$ACCEPTANCE_ACTION"
    printf '| Database/schema changes | `none` |\n'
    printf '| Stored record changes by release | `none` |\n'
    printf '| Notification sent | `no` |\n'
    printf '| n8n restart | `no` |\n'
    printf '| PostGIS restart | `no` |\n'
    printf '| Full E2E | `not run` |\n'
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '## Sanitized acceptance\n\n```json\n%s\n```\n\n' "$ACCEPTANCE_RESULT"
    printf 'No addresses, coordinates, alert text, Watch names, staff names, Recipient details, channel names, credentials, or private origins are included. '
    printf 'The private mode-600 log remains on the VPS at `%s`.\n' "$LOG_FILE"
  } > "$report_file"
  install -m 600 "$report_file" "$report_dir/latest.md"
  git -C "$worktree" add operation-results/alert-watch-completion-performance
  git -C "$worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit \
    -m "Record Alert and Watch completion ${status,,} $RUN_ID" >/dev/null || return 1
  git -C "$worktree" push -q origin "HEAD:refs/heads/$REPORT_BRANCH" || return 1
  log "SANITIZED_REPORT=https://github.com/gioahmad/city-manager-os/blob/$REPORT_BRANCH/operation-results/alert-watch-completion-performance/latest.md"
) }

on_error(){
  local rc=$?
  FAIL_LINE="${BASH_LINENO[0]:-$LINENO}"
  log "ERROR: release failed rc=$rc line=$FAIL_LINE phase=$CURRENT_PHASE"
  trap - ERR
  exit "$rc"
}

on_exit(){
  local rc=$?
  trap - ERR EXIT
  if (( rc == 0 )) && [[ "$CURRENT_PHASE" != complete ]]; then rc=1; FAIL_LINE="unexpected-eof"; fi
  if (( rc != 0 )); then
    rollback_dashboard || log "WARNING: dashboard rollback was not confirmed"
    restore_checkout || log "WARNING: repository restore was not confirmed"
  fi
  (( rc == 0 )) && FINAL_STATUS="PASS"
  section "ALERT AND WATCH COMPLETION PERFORMANCE: $FINAL_STATUS"
  printf 'STATUS=%s\nPHASE=%s\nTARGET=%s\nPRIVATE_LOG=%s\n' "$FINAL_STATUS" "$CURRENT_PHASE" "$TARGET_HEAD" "$LOG_FILE"
  publish_report "$FINAL_STATUS" "$rc" || log "WARNING: sanitized GitHub report could not be published"
  exit "$rc"
}

trap on_error ERR
trap on_exit EXIT

for command in git docker python3 flock mktemp install tee; do
  command -v "$command" >/dev/null || fail "$command is required"
done
mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another Alert and Watch completion release is already running"

CURRENT_PHASE="preflight"
section "ALERT AND WATCH COMPLETION PERFORMANCE"
cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "production repository must be clean"
git fetch -q origin "+refs/heads/main:refs/remotes/origin/main" "+refs/heads/$REPORT_BRANCH:refs/remotes/origin/$REPORT_BRANCH"
TARGET_HEAD="$(git rev-parse origin/main)"
git merge-base --is-ancestor "$EXPECTED_BASE" "$TARGET_HEAD" || fail "origin/main does not descend from the accepted production release"
ACTUAL_PATHS="$(git diff --name-only "$EXPECTED_BASE...$TARGET_HEAD" | LC_ALL=C sort)"
[[ "$ACTUAL_PATHS" == "$EXPECTED_PATHS" ]] || {
  printf 'EXPECTED PATHS:\n%s\nACTUAL PATHS:\n%s\n' "$EXPECTED_PATHS" "$ACTUAL_PATHS"
  fail "release scope changed; refusing an unreviewed deployment"
}

ORIGINAL_HEAD="$(git rev-parse HEAD)"
if [[ "$ORIGINAL_HEAD" == "$TARGET_HEAD" ]]; then
  REPOSITORY_ACTION="already-aligned"
elif git merge-base --is-ancestor "$ORIGINAL_HEAD" "$TARGET_HEAD"; then
  git merge --ff-only origin/main
  REPOSITORY_ACTION="fast-forwarded"
else
  ARCHIVE_BRANCH="production-archive/alert-watch-completion-$RUN_ID"
  git branch "$ARCHIVE_BRANCH" "$ORIGINAL_HEAD"
  REPOSITORY_ACTION="archive-created"
  git switch --detach "$ORIGINAL_HEAD" >/dev/null
  git update-ref refs/heads/main "$TARGET_HEAD" "$ORIGINAL_HEAD"
  git switch main >/dev/null
  git branch --set-upstream-to=origin/main main >/dev/null
  REPOSITORY_ACTION="archived-and-realigned"
fi
[[ "$(git rev-parse HEAD)" == "$TARGET_HEAD" ]] || fail "production checkout did not reach the release target"
[[ -z "$(git status --porcelain)" ]] || fail "production repository is not clean after alignment"
for container in citymanager-dashboard n8n citymanager-postgis; do
  [[ "$(docker inspect "$container" --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
    || fail "required container is not running: $container"
done

PLAN="$(python3 deploy/cmos-deploy plan --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null)"
printf '%s\n' "$PLAN"
grep -q '^changed_count=12$' <<<"$PLAN"
grep -q '^build=yes$' <<<"$PLAN"
grep -q '^services=citymanager-dashboard$' <<<"$PLAN"
grep -q '^tests=tests/test_alert_watch_completion.py,tests/test_attention_engine.py,tests/test_gis_import.py,tests/test_global_search_match_explanations.py,tests/test_subscriber_watch_bulk_locations.py,tests/test_watch_selection_attribution_bulk_actions.py$' <<<"$PLAN"
grep -q '^backup_required=no$' <<<"$PLAN"
grep -q '^external=none$' <<<"$PLAN"
grep -q '^full_e2e=no$' <<<"$PLAN"
grep -q '^unknown=none$' <<<"$PLAN"
log "PREFLIGHT PASS: one dashboard build/recreate, no migration, no workflow change, no stored-record acceptance writes"

CURRENT_PHASE="running-receipt"
publish_report "RUNNING" "0" || log "WARNING: start receipt could not be published"

CURRENT_PHASE="change-aware-verification"
if dashboard_release_is_live; then
  DASHBOARD_ACTION="already-current-no-build"
else
  old_image="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
  DASHBOARD_ROLLBACK_TAG="dashboard-citymanager-dashboard:cmos-alert-watch-rollback-$RUN_ID"
  docker image tag "$old_image" "$DASHBOARD_ROLLBACK_TAG"
  DASHBOARD_PREPARED=1
  python3 deploy/cmos-deploy verify --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null
  DASHBOARD_ACTION="verified-one-build"
fi

CURRENT_PHASE="read-only-production-smoke"
SMOKE_RESULT="$(docker compose -f "$REPO/dashboard/docker-compose.yml" run --rm --no-deps -T \
  --entrypoint python citymanager-dashboard - <<'PY'
import json,time,uuid
from starlette.requests import Request
from app import query_one
from map_app import map_issues_geojson,map_system_events,map_watchlist_geojson
from operations_app import _global_search_rows,alerts_page
from spatial_watch_app import _bulk_location_context,spatial_watchlist

def request(path):
    return Request({
        'type':'http','http_version':'1.1','method':'GET','scheme':'http',
        'path':path,'raw_path':path.encode(),'query_string':b'',
        'headers':[],'client':('127.0.0.1',0),'server':('localhost',8000),'root_path':'',
    })

timing={}
def timed(name,call,ceiling):
    started=time.monotonic()
    value=call()
    timing[name]=round((time.monotonic()-started)*1000)
    if timing[name]>ceiling: raise RuntimeError(f'{name} exceeded its performance ceiling')
    return value

watches=timed('watchlist',lambda:spatial_watchlist(request('/watchlist')),6000)
if watches.status_code!=200 or b'Internal Server Error' in watches.body: raise RuntimeError('Watchlist failed')
alerts=timed('alerts',lambda:alerts_page(request('/alerts'),window='24h'),8000)
if alerts.status_code!=200 or b'Internal Server Error' in alerts.body: raise RuntimeError('Alert page failed')
timed('search_everything',lambda:_global_search_rows('CMOS_RELEASE_PROBE','all'),12000)
box='-75.6,38.8,-73.8,41.4'
for name,call in (
    ('map_watches',lambda:map_watchlist_geojson(bbox=box,q='CMOS_RELEASE_PROBE')),
    ('map_work',lambda:map_issues_geojson(bbox=box,q='CMOS_RELEASE_PROBE')),
    ('map_events',lambda:map_system_events(bbox=box,q='CMOS_RELEASE_PROBE')),
):
    response=timed(name,call,8000)
    if response.status_code!=200: raise RuntimeError(f'{name} failed')
county=query_one("SELECT id::text AS id FROM map_layers WHERE layer_key='NJ_OFFICIAL_COUNTIES' AND active=true")
towns=query_one("SELECT id::text AS id FROM map_layers WHERE layer_key='NJ_OFFICIAL_MUNICIPALITIES' AND active=true")
if not county or not towns: raise RuntimeError('official NJ Watch layers are missing')
county_context=timed('county_picker',lambda:_bulk_location_context(uuid.UUID(county['id'])),6000)
town_context=timed('town_picker',lambda:_bulk_location_context(uuid.UUID(towns['id']),group_filter='Bergen'),6000)
if not county_context['groups'] or town_context['visible_feature_count']<2: raise RuntimeError('county-to-town picker failed')
print(json.dumps({'status':'PASS','timing_ms':timing,'bergen_town_choices':town_context['visible_feature_count']},sort_keys=True))
PY
)"
printf '%s\n' "$SMOKE_RESULT" | tail -n 1 | python3 -m json.tool

CURRENT_PHASE="dashboard-deploy"
if [[ "$DASHBOARD_ACTION" == verified-one-build ]]; then
  DASHBOARD_CHANGED=1
  docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate citymanager-dashboard </dev/null
  for _ in $(seq 1 30); do
    if docker exec citymanager-dashboard python -c \
      "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200" \
      >/dev/null 2>&1; then break; fi
    sleep 2
  done
  dashboard_release_is_live || fail "Alert and Watch completion contract is not live"
  DASHBOARD_ACTION="deployed-one-build-one-recreate"
fi

CURRENT_PHASE="focused-read-only-acceptance"
ACCEPTANCE_RESULT="$(docker exec -i citymanager-dashboard python - <<'PY'
import json,os,time,urllib.parse,urllib.request
from app import query_one

token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: raise RuntimeError('authenticated acceptance token is unavailable')
headers={'X-CMOS-Automation-Key':token}
timing={}

def get(path,key,ceiling=12000):
    started=time.monotonic()
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers)
    with urllib.request.urlopen(request,timeout=20) as response:
        body=response.read()
        timing[key]=round((time.monotonic()-started)*1000)
        if response.status!=200 or '/login' in response.geturl(): raise RuntimeError('authenticated acceptance failed')
        if b'Internal Server Error' in body: raise RuntimeError('an internal error was exposed')
        if timing[key]>ceiling: raise RuntimeError(f'{key} exceeded its performance ceiling')
        return body.decode(errors='replace')

watchlist=get('/watchlist','watchlist',6000)
alerts=get('/alerts?window=24h','alerts',8000)
mapping=get('/map','map',8000)
search=get('/search?q=CMOS_RELEASE_PROBE','search_everything',12000)
county=query_one("SELECT id::text AS id FROM map_layers WHERE layer_key='NJ_OFFICIAL_COUNTIES' AND active=true")
towns=query_one("SELECT id::text AS id FROM map_layers WHERE layer_key='NJ_OFFICIAL_MUNICIPALITIES' AND active=true")
county_page=get('/watchlist?'+urllib.parse.urlencode({'bulk_layer':county['id']}),'county_picker',6000)
town_page=get('/watchlist?'+urllib.parse.urlencode({'bulk_layer':towns['id'],'bulk_group_filter':'Bergen'}),'town_picker',6000)
for body,markers in (
    (watchlist,('Five simple steps','data-watch-bulk-form')),
    (alerts,('Create Watch From Alert','name="selection_scope"','Why you received this')),
    (mapping,('Search records in this map area','Search Visible Area','Search all operational records')),
    (search,('Search Everything','Recipients &amp; Staff','Flood &amp; Utility State')),
    (county_page,('Choose individual towns in','Watch an entire county boundary')),
    (town_page,('Bergen County towns','Show every county','name="feature_ids"')),
):
    if any(marker not in body for marker in markers): raise RuntimeError('a completion control is missing')
counts=query_one("""
  SELECT
    (SELECT count(*) FROM watch_items) AS watches,
    (SELECT count(*) FROM watch_items w
      WHERE w.active AND (w.starts_at IS NULL OR w.starts_at<=now())
        AND (w.expires_at IS NULL OR w.expires_at>now())
        AND NOT EXISTS (
          SELECT 1 FROM watch_item_recipients wir JOIN subscribers s ON s.id=wir.subscriber_id
          WHERE wir.watch_item_id=w.id AND wir.active AND s.active
        )) AS active_without_recipient,
    (SELECT count(*) FROM alerts) AS alerts,
    (SELECT count(*) FROM alert_watch_matches WHERE nullif(trim(match_reason),'') IS NOT NULL) AS explained_matches
""")
print(json.dumps({
  'watchlist_performance':'PASS',
  'county_to_individual_towns':'PASS',
  'inline_alert_watch_choices':'PASS',
  'visible_area_record_search':'PASS',
  'expanded_operational_search':'PASS',
  'filtered_bulk_alert_actions':'PASS',
  'watch_count':int(counts.get('watches') or 0),
  'active_watches_needing_recipient':int(counts.get('active_without_recipient') or 0),
  'alert_count':int(counts.get('alerts') or 0),
  'explained_match_count':int(counts.get('explained_matches') or 0),
  'acceptance_requests':'GET-only',
  'write_requests_sent':0,
  'notification_sent':'NO',
  'database_schema_changes':'NONE',
  'timing_ms':timing,
},sort_keys=True))
PY
)"
printf '%s\n' "$ACCEPTANCE_RESULT" | python3 -m json.tool
ACCEPTANCE_ACTION="pass-read-only-no-data-change"

CURRENT_PHASE="final-health"
dashboard_release_is_live || fail "dashboard contract failed final health check"
docker exec n8n node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CURRENT_PHASE="complete"
section "RELEASE COMPLETE"
printf 'TARGET=%s\nDASHBOARD=%s\nDATABASE_SCHEMA_CHANGES=NONE\nSTORED_RECORD_CHANGES=NONE\nN8N_RESTART=NO\nPOSTGIS_RESTART=NO\nFULL_E2E=NOT_RUN\n' \
  "$TARGET_HEAD" "$DASHBOARD_ACTION"
