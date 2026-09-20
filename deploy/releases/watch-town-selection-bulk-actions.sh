#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C GIT_TERMINAL_PROMPT=0
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED_BASE="0586e8b602c88204e26b01df38298dd8be69c49f"
REPORT_BRANCH="release-output/ops"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
LOG_FILE="$LOG_DIR/watch-town-selection-bulk-actions-$RUN_ID.log"
LOCK_FILE="/var/lock/cmos-watch-town-selection-bulk-actions.lock"
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

EXPECTED_PATHS=$'dashboard/operations_app.py\ndashboard/spatial_watch_app.py\ndashboard/static/style.css\ndashboard/templates/alerts.html\ndashboard/templates/watchlist.html\ndashboard/tests/test_watch_selection_attribution_bulk_actions.py\ndashboard/tests/test_watchlist_reliability.py\ndeploy/releases/watch-town-selection-bulk-actions.sh'

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

dashboard_release_is_live(){
  docker exec -i citymanager-dashboard python - <<'PY'
import json,os,urllib.parse,urllib.request
from app import query_one

def fail(reason):
    print(f'DASHBOARD_CONTRACT=FAIL:{reason}')
    raise SystemExit(1)

token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: fail('automation-token-missing')
headers={'X-CMOS-Automation-Key':token}

def get(path):
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers)
    with urllib.request.urlopen(request,timeout=20) as response:
        body=response.read()
        if response.status!=200 or '/login' in response.geturl(): fail(f'{path}-authentication')
        if b'Internal Server Error' in body: fail(f'{path}-internal-server-error')
        return body

payload=json.loads(get('/api/spatial-watch/release'))
if payload.get('individual_location_selection') is not True: fail('individual-location-contract')
if payload.get('saved_keyword_switches') is not True: fail('keyword-switch-contract')
if payload.get('alert_match_explanations') is not True: fail('alert-explanation-contract')
if payload.get('bulk_watch_actions')!=['pause','activate','delete']: fail('watch-action-contract')
if payload.get('bulk_alert_actions')!=['resolve','delete']: fail('alert-action-contract')

get('/watchlist')
get('/alerts?window=24h')
layer=query_one("SELECT id::text AS id FROM map_layers WHERE layer_key='NJ_OFFICIAL_MUNICIPALITIES' AND active=true")
if not layer: fail('official-municipality-layer-missing')
bulk=get('/watchlist?'+urllib.parse.urlencode({'bulk_layer':layer['id']})).decode(errors='replace')
for marker in ('Choose individual Locations','name="feature_ids"','data-bulk-feature','Open a county to select only the towns you want'):
    if marker not in bulk: fail('town-picker-missing')
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
  parent="$(mktemp -d /tmp/cmos-watch-town-report.XXXXXX)"
  worktree="$parent/worktree"
  git -C "$REPO" worktree add --detach "$worktree" "origin/$REPORT_BRANCH" >/dev/null 2>&1 || return 1
  report_dir="$worktree/operation-results/watch-town-selection-bulk-actions"
  mkdir -p "$report_dir"
  report_file="$report_dir/$RUN_ID-${status,,}.md"
  {
    printf '# Watch town selection, explanations, and bulk actions\n\n'
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
    printf 'No addresses, coordinates, alert text, Watch names, Recipient details, channel names, credentials, or private origins are included. '
    printf 'The private mode-600 log remains on the VPS at `%s`.\n' "$LOG_FILE"
  } > "$report_file"
  install -m 600 "$report_file" "$report_dir/latest.md"
  git -C "$worktree" add operation-results/watch-town-selection-bulk-actions
  git -C "$worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit \
    -m "Record Watch town selection and bulk actions ${status,,} $RUN_ID" >/dev/null || return 1
  git -C "$worktree" push -q origin "HEAD:refs/heads/$REPORT_BRANCH" || return 1
  log "SANITIZED_REPORT=https://github.com/gioahmad/city-manager-os/blob/$REPORT_BRANCH/operation-results/watch-town-selection-bulk-actions/latest.md"
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
  section "WATCH TOWN SELECTION AND BULK ACTIONS: $FINAL_STATUS"
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
flock -n 9 || fail "another Watch town-selection release is already running"

CURRENT_PHASE="preflight"
section "WATCH TOWN SELECTION, ALERT EXPLANATIONS, AND BULK ACTIONS"
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
  ARCHIVE_BRANCH="production-archive/watch-town-selection-$RUN_ID"
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
grep -q '^changed_count=8$' <<<"$PLAN"
grep -q '^build=yes$' <<<"$PLAN"
grep -q '^services=citymanager-dashboard$' <<<"$PLAN"
grep -q '^tests=tests/test_attention_engine.py,tests/test_watch_selection_attribution_bulk_actions.py,tests/test_watchlist_reliability.py$' <<<"$PLAN"
grep -q '^backup_required=no$' <<<"$PLAN"
grep -q '^external=none$' <<<"$PLAN"
grep -q '^full_e2e=no$' <<<"$PLAN"
grep -q '^unknown=none$' <<<"$PLAN"
log "PREFLIGHT PASS: one dashboard build/recreate, no migration, no n8n change, no stored-record acceptance writes"

CURRENT_PHASE="running-receipt"
publish_report "RUNNING" "0" || log "WARNING: start receipt could not be published"

CURRENT_PHASE="change-aware-verification"
if dashboard_release_is_live; then
  DASHBOARD_ACTION="already-current-no-build"
else
  old_image="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
  DASHBOARD_ROLLBACK_TAG="dashboard-citymanager-dashboard:cmos-watch-town-rollback-$RUN_ID"
  docker image tag "$old_image" "$DASHBOARD_ROLLBACK_TAG"
  DASHBOARD_PREPARED=1
  python3 deploy/cmos-deploy verify --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null
  docker compose -f "$REPO/dashboard/docker-compose.yml" run --rm --no-deps -T \
    -v "$REPO/dashboard:/src:ro" -w /src -e PYTHONPATH=/src:/app \
    --entrypoint pytest citymanager-dashboard -p no:cacheprovider -q \
    tests/test_subscriber_watch_bulk_locations.py tests/test_global_search_match_explanations.py </dev/null
  docker compose -f "$REPO/dashboard/docker-compose.yml" run --rm --no-deps -T \
    -v "$REPO/dashboard:/src:ro" -w /src -e PYTHONPATH=/src:/app \
    --entrypoint python citymanager-dashboard - <<'PY'
from starlette.requests import Request
from app import query_one
from operations_app import alerts_page
from spatial_watch_app import spatial_watchlist

def request(path):
    return Request({
        'type':'http','http_version':'1.1','method':'GET','scheme':'http',
        'path':path,'raw_path':path.encode(),'query_string':b'',
        'headers':[],'client':('127.0.0.1',0),'server':('localhost',8000),
        'root_path':'',
    })

alerts=alerts_page(request('/alerts'),window='24h')
if alerts.status_code!=200 or b'Internal Server Error' in alerts.body:
    raise RuntimeError('new Alert query or template failed against production data')
layer=query_one("SELECT id::text AS id FROM map_layers WHERE layer_key='NJ_OFFICIAL_MUNICIPALITIES' AND active=true")
if not layer: raise RuntimeError('official municipality layer is unavailable')
watches=spatial_watchlist(request('/watchlist'),bulk_layer=layer['id'])
if watches.status_code!=200 or b'name="feature_ids"' not in watches.body:
    raise RuntimeError('individual town picker failed against production data')
access=query_one("""
  SELECT has_table_privilege(current_user,'alerts','DELETE') AS alerts_delete,
         has_table_privilege(current_user,'watch_items','DELETE') AS watches_delete,
         has_table_privilege(current_user,'geo_entity_resolutions','DELETE') AS resolutions_delete,
         (SELECT count(*) FROM pg_constraint
           WHERE conrelid=ANY(ARRAY['deliveries'::regclass,'alert_watch_matches'::regclass])
             AND confrelid='alerts'::regclass AND confdeltype='c') AS alert_cascades
""")
if not all(access.get(key) for key in ('alerts_delete','watches_delete','resolutions_delete')):
    raise RuntimeError('existing application role cannot perform a requested guarded delete')
if int(access.get('alert_cascades') or 0)!=2:
    raise RuntimeError('existing alert deletion cascade contract is incomplete')
print('READ_ONLY_PRODUCTION_DATA_SMOKE=PASS')
PY
  DASHBOARD_ACTION="verified-one-build"
fi

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
  dashboard_release_is_live || fail "town selection, explanations, and bulk actions are not live"
  DASHBOARD_ACTION="deployed-one-build-one-recreate"
fi

CURRENT_PHASE="focused-read-only-acceptance"
ACCEPTANCE_RESULT="$(docker exec -i citymanager-dashboard python - <<'PY'
import json,os,time,urllib.parse,urllib.request,uuid
from app import query_one
from spatial_watch_app import _bulk_location_context,_selected_bulk_feature_ids

token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: raise RuntimeError('authenticated acceptance token is unavailable')
headers={'X-CMOS-Automation-Key':token}
timing={}

def get(path,key):
    started=time.monotonic()
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers,method='GET')
    with urllib.request.urlopen(request,timeout=20) as response:
        body=response.read()
        timing[key]=round((time.monotonic()-started)*1000)
        if response.status!=200 or '/login' in response.geturl(): raise RuntimeError('authenticated acceptance failed')
        if b'Internal Server Error' in body: raise RuntimeError('an internal error was exposed')
        return body.decode(errors='replace')

layer=query_one("SELECT id::text AS id FROM map_layers WHERE layer_key='NJ_OFFICIAL_MUNICIPALITIES' AND active=true")
if not layer: raise RuntimeError('official municipality layer is missing')
context=_bulk_location_context(uuid.UUID(layer['id']))
groups=[group for group in context['groups'] if group['count']>1]
if not groups: raise RuntimeError('municipalities are not grouped into counties')
sample=groups[0]
if len(sample['features'])!=sample['count']: raise RuntimeError('individual town choices are incomplete')
selected=_selected_bulk_feature_ids(context,group_tokens=[],feature_ids=[uuid.UUID(sample['features'][0]['id'])])
if len(selected)!=1: raise RuntimeError('individual town selection expanded unexpectedly')

bulk=get('/watchlist?'+urllib.parse.urlencode({'bulk_layer':layer['id']}),'town_picker')
for marker in ('Choose individual Locations','name="feature_ids"','data-bulk-feature','Open a county to select only the towns you want'):
    if marker not in bulk: raise RuntimeError('individual town controls are incomplete')
watchlist=get('/watchlist','watchlist')
alerts=get('/alerts?window=24h','alerts')
release=json.loads(get('/api/spatial-watch/release','release_api'))
if release.get('bulk_watch_actions')!=['pause','activate','delete']: raise RuntimeError('bulk Watch action contract failed')
if release.get('bulk_alert_actions')!=['resolve','delete']: raise RuntimeError('bulk alert action contract failed')
if release.get('saved_keyword_switches') is not True: raise RuntimeError('keyword switch contract failed')
if 'data-watch-bulk-form' not in watchlist: raise RuntimeError('bulk Watch controls are missing')

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
if int(counts.get('alerts') or 0)>0:
    for marker in ('Why you received this','data-alert-bulk-form','Permanent deletion also removes their Match and Notification evidence'):
        if marker not in alerts: raise RuntimeError('alert explanation or bulk controls are missing')
if any(value>20000 for value in timing.values()): raise RuntimeError('a focused page exceeded the 20-second ceiling')

print(json.dumps({
  'individual_town_selection':'PASS',
  'official_municipality_locations':int(context['feature_count']),
  'county_groups_with_multiple_towns':len(groups),
  'individual_selection_expands_to':len(selected),
  'saved_keyword_switches':'PASS',
  'alert_match_explanations':'PASS',
  'bulk_watch_actions':'PASS',
  'bulk_alert_actions':'PASS',
  'alert_delete_confirmation':'REQUIRED',
  'watch_count':int(counts.get('watches') or 0),
  'active_watches_needing_recipient':int(counts.get('active_without_recipient') or 0),
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
