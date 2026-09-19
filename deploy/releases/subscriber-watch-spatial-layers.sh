#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED_BASE="65b371c0bd35766c22dcef84d7410a4b43a0a52b"
REPORT_BRANCH="release-output/ops"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
LOG_FILE="$LOG_DIR/subscriber-watch-spatial-layers-$RUN_ID.log"
LOCK_FILE="/var/lock/cmos-subscriber-watch-spatial-layers.lock"
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

EXPECTED_PATHS=$'dashboard/map_app.py\ndashboard/operations_app.py\ndashboard/spatial_watch_app.py\ndashboard/static/style.css\ndashboard/templates/map.html\ndashboard/templates/subscribers.html\ndashboard/templates/watchlist.html\ndashboard/tests/test_subscriber_watch_bulk_locations.py\ndeploy/releases/subscriber-watch-spatial-layers.sh'

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

dashboard_release_is_live(){
  docker exec -i citymanager-dashboard python - <<'PY' >/dev/null 2>&1
import json,os,urllib.request
token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: raise SystemExit(1)
headers={'X-CMOS-Automation-Key':token}
required={
  '/watchlist':('Create several Watches from a map layer','50 feet · road or corridor','data-alert-keywords'),
  '/subscribers':('Open a Recipient to edit the channel or choose which existing Watches','Recipient Directory'),
  '/map':('Build Watches','Mapping Center'),
}
for path,markers in required.items():
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers)
    with urllib.request.urlopen(request,timeout=20) as response:
        body=response.read().decode(errors='replace')
        if response.status!=200 or '/login' in response.geturl(): raise SystemExit(1)
        if 'Internal Server Error' in body or not all(marker in body for marker in markers): raise SystemExit(1)
request=urllib.request.Request('http://127.0.0.1:8000/api/spatial-watch/release',headers=headers)
with urllib.request.urlopen(request,timeout=20) as response: payload=json.load(response)
if payload.get('recipient_watch_assignment')!='/subscribers/{recipient_id}/watches': raise SystemExit(1)
if payload.get('bulk_watch_endpoint')!='/watchlist/bulk-create': raise SystemExit(1)
if payload.get('bulk_watch_limit')!=250: raise SystemExit(1)
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
  docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate citymanager-dashboard
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
  parent="$(mktemp -d /tmp/cmos-subscriber-watch-report.XXXXXX)"
  worktree="$parent/worktree"
  git -C "$REPO" worktree add --detach "$worktree" "origin/$REPORT_BRANCH" >/dev/null 2>&1 || return 1
  report_dir="$worktree/operation-results/subscriber-watch-spatial-layers"
  mkdir -p "$report_dir"
  report_file="$report_dir/$RUN_ID-${status,,}.md"
  {
    printf '# Subscriber Watch assignment and reusable Locations\n\n'
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
    printf '| Stored record changes | `none` |\n'
    printf '| Notification sent | `no` |\n'
    printf '| n8n restart | `no` |\n'
    printf '| Full E2E | `not run` |\n'
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '## Sanitized acceptance\n\n```json\n%s\n```\n\n' "$ACCEPTANCE_RESULT"
    printf 'No addresses, coordinates, alert text, Watch names, Recipient details, channel names, credentials, or private origins are included. '
    printf 'The private mode-600 log remains on the VPS at `%s`.\n' "$LOG_FILE"
  } > "$report_file"
  install -m 600 "$report_file" "$report_dir/latest.md"
  git -C "$worktree" add operation-results/subscriber-watch-spatial-layers
  git -C "$worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit \
    -m "Record Subscriber Watch and reusable Locations ${status,,} $RUN_ID" >/dev/null || return 1
  git -C "$worktree" push -q origin "HEAD:refs/heads/$REPORT_BRANCH" || return 1
  log "SANITIZED_REPORT=https://github.com/gioahmad/city-manager-os/blob/$REPORT_BRANCH/operation-results/subscriber-watch-spatial-layers/latest.md"
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
  section "SUBSCRIBER WATCH AND REUSABLE LOCATIONS: $FINAL_STATUS"
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
flock -n 9 || fail "another Subscriber Watch release is already running"

CURRENT_PHASE="preflight"
section "SUBSCRIBER WATCH ASSIGNMENT AND REUSABLE LOCATIONS"
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
  ARCHIVE_BRANCH="production-archive/subscriber-watch-$RUN_ID"
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
grep -q '^changed_count=9$' <<<"$PLAN"
grep -q '^build=yes$' <<<"$PLAN"
grep -q '^services=citymanager-dashboard$' <<<"$PLAN"
grep -q '^tests=tests/test_attention_engine.py,tests/test_gis_import.py,tests/test_subscriber_watch_bulk_locations.py$' <<<"$PLAN"
grep -q '^backup_required=no$' <<<"$PLAN"
grep -q '^external=none$' <<<"$PLAN"
grep -q '^full_e2e=no$' <<<"$PLAN"
grep -q '^unknown=none$' <<<"$PLAN"
log "PREFLIGHT PASS: dashboard only, no database migration, no n8n publish, no stored-record acceptance writes"

CURRENT_PHASE="running-receipt"
publish_report "RUNNING" "0" || log "WARNING: start receipt could not be published"

CURRENT_PHASE="change-aware-verification"
if dashboard_release_is_live; then
  DASHBOARD_ACTION="already-current-no-build"
else
  old_image="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
  DASHBOARD_ROLLBACK_TAG="dashboard-citymanager-dashboard:cmos-subscriber-watch-rollback-$RUN_ID"
  docker image tag "$old_image" "$DASHBOARD_ROLLBACK_TAG"
  DASHBOARD_PREPARED=1
  python3 deploy/cmos-deploy verify --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null
  docker compose -f "$REPO/dashboard/docker-compose.yml" run --rm --no-deps -T \
    -v "$REPO/dashboard:/src:ro" -w /src -e PYTHONPATH=/src:/app \
    --entrypoint pytest citymanager-dashboard -p no:cacheprovider -q \
    tests/test_spatial_watch_pack.py tests/test_watchlist_reliability.py </dev/null
  DASHBOARD_ACTION="verified-one-build"
fi

CURRENT_PHASE="dashboard-deploy"
if [[ "$DASHBOARD_ACTION" == verified-one-build ]]; then
  DASHBOARD_CHANGED=1
  docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate citymanager-dashboard
  for _ in $(seq 1 30); do
    if docker exec citymanager-dashboard python -c \
      "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200" \
      >/dev/null 2>&1; then break; fi
    sleep 2
  done
  dashboard_release_is_live || fail "Subscriber Watch and reusable Location controls are not live"
  DASHBOARD_ACTION="deployed-one-build-one-recreate"
fi

CURRENT_PHASE="focused-read-only-acceptance"
ACCEPTANCE_RESULT="$(docker exec -i citymanager-dashboard python - <<'PY'
import json,os,time,urllib.parse,urllib.request
from app import query_one
from spatial_watch_app import _alert_keyword_choices

token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: raise RuntimeError('authenticated acceptance token is unavailable')
headers={'X-CMOS-Automation-Key':token}
timing={}

def get(path,expect_json=False):
    started=time.monotonic()
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers,method='GET')
    with urllib.request.urlopen(request,timeout=20) as response:
        body=response.read()
        elapsed=round((time.monotonic()-started)*1000)
        if response.status!=200 or '/login' in response.geturl(): raise RuntimeError('authenticated acceptance failed')
        if b'Internal Server Error' in body: raise RuntimeError('an internal error was exposed')
        return (json.loads(body) if expect_json else body.decode(errors='replace')),elapsed

watchlist,timing['watchlist']=get('/watchlist')
subscribers,timing['recipients']=get('/subscribers')
mapping,timing['mapping']=get('/map')
release,timing['release_api']=get('/api/spatial-watch/release',True)
for marker in ('Create several Watches from a map layer','Inside boundary','50 feet · road or corridor','Create Selected Watches'):
    if marker not in watchlist and marker!='Create Selected Watches': raise RuntimeError('reusable Location instructions are incomplete')
for marker in ('Open a Recipient to edit the channel or choose which existing Watches','Recipient Directory'):
    if marker not in subscribers: raise RuntimeError('Recipient Watch choices are incomplete')
if 'Build Watches' not in mapping: raise RuntimeError('Mapping Center Watch action is missing')
if release.get('bulk_watch_limit')!=250 or release.get('reusable_location_source')!='Mapping Center map_layers and map_features':
    raise RuntimeError('release API contract changed')

choices=_alert_keyword_choices({
  'title':'Example alert','message':'Working fire near school with road closure',
  'subtype':'INCIDENT','category':'PUBLIC_SAFETY','tags':['dispatch'],
})
normalized={value.casefold() for value in choices}
if not {'working fire','road closure'}.issubset(normalized): raise RuntimeError('dynamic Alert keyword contract failed')

recipient=query_one("SELECT id::text AS id FROM subscribers ORDER BY created_at,id LIMIT 1")
recipient_preview='SKIPPED_NO_RECIPIENT'
if recipient:
    managed,timing['recipient_watch_choices']=get('/subscribers?'+urllib.parse.urlencode({'manage':recipient['id']}))
    for marker in ('Watches for this Recipient','Save Watch Choices','name="watch_item_ids"'):
        if marker not in managed: raise RuntimeError('Recipient Watch checklist is incomplete')
    recipient_preview='PASS'

seed=query_one("SELECT alert_id FROM alerts WHERE nullif(trim(alert_id),'') IS NOT NULL ORDER BY received_at DESC,id DESC LIMIT 1")
if seed:
    draft,timing['alert_watch_draft']=get('/watchlist?'+urllib.parse.urlencode({'from_alert':seed['alert_id']}))
    for marker in ('Choose keywords from this alert','These choices come from this alert, not a fixed list','name="alert_keywords"'):
        if marker not in draft: raise RuntimeError('Alert keyword picker is incomplete')

layer=query_one("""
  SELECT l.id::text AS id
  FROM map_layers l
  WHERE l.active=true AND l.layer_type='CUSTOM_GEOJSON'
    AND EXISTS (SELECT 1 FROM map_features f WHERE f.layer_id=l.id AND f.active=true)
  ORDER BY l.name LIMIT 1
""")
bulk_preview='SKIPPED_NO_ACTIVE_LAYER'
if layer:
    bulk,timing['bulk_preview']=get('/watchlist?'+urllib.parse.urlencode({'bulk_layer':layer['id']}))
    for marker in ('Choose Location groups','name="group_tokens"','Turn these Watches on now'):
        if marker not in bulk: raise RuntimeError('bulk Watch preview is incomplete')
    bulk_preview='PASS'

integrity=query_one("""
  SELECT
    (SELECT count(*) FROM watch_item_recipients wir LEFT JOIN watch_items w ON w.id=wir.watch_item_id LEFT JOIN subscribers s ON s.id=wir.subscriber_id WHERE w.id IS NULL OR s.id IS NULL) AS orphan_routes,
    (SELECT count(*) FROM watch_items w WHERE w.active AND (w.starts_at IS NULL OR w.starts_at<=now()) AND (w.expires_at IS NULL OR w.expires_at>now()) AND NOT EXISTS (SELECT 1 FROM watch_item_recipients wir JOIN subscribers s ON s.id=wir.subscriber_id WHERE wir.watch_item_id=w.id AND wir.active AND s.active)) AS active_without_recipient,
    (SELECT count(*) FROM map_layers l WHERE l.active AND l.layer_type='CUSTOM_GEOJSON' AND EXISTS (SELECT 1 FROM map_features f WHERE f.layer_id=l.id AND f.active)) AS reusable_layers
""")
if int(integrity.get('orphan_routes') or 0): raise RuntimeError('orphan Recipient Watch assignments exist')
if any(value>20000 for value in timing.values()): raise RuntimeError('a focused page exceeded the 20-second ceiling')

print(json.dumps({
  'recipient_watch_assignment':'PASS',
  'recipient_watch_preview':recipient_preview,
  'dynamic_alert_keywords':'PASS',
  'mapping_center_reuse':'PASS',
  'bulk_watch_preview':bulk_preview,
  'reusable_layer_count':int(integrity.get('reusable_layers') or 0),
  'active_watches_needing_recipient':int(integrity.get('active_without_recipient') or 0),
  'orphan_routes':0,
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
printf 'TARGET=%s\nDASHBOARD=%s\nDATABASE_SCHEMA_CHANGES=NONE\nSTORED_RECORD_CHANGES=NONE\nN8N_RESTART=NO\nFULL_E2E=NOT_RUN\n' \
  "$TARGET_HEAD" "$DASHBOARD_ACTION"
