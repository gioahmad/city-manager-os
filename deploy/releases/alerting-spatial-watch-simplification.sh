#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED_BASE="0b3a096328bcf7fc8c70cc91e12d006328a01496"
RELEASE_ID="alerting-spatial-watch-simplification-v1"
REPORT_BRANCH="release-output/ops"
MATCHER_ID="ESH9c2pZ8QfkMosO"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
LOG_FILE="$LOG_DIR/alerting-spatial-watch-simplification-$RUN_ID.log"
LOCK_FILE="/var/lock/cmos-alerting-spatial-watch-simplification.lock"
PROBE_NAME="CMOS Alerting Acceptance $RUN_ID"
CURRENT_PHASE="startup"
FINAL_STATUS="FAIL"
FAIL_LINE="none"
TARGET_HEAD="unknown"
ORIGINAL_HEAD="unknown"
ARCHIVE_BRANCH="none"
REPOSITORY_ACTION="not-started"
DASHBOARD_ACTION="not-started"
MATCHER_ACTION="not-started"
ACCEPTANCE_ACTION="not-started"
PROBE_CLEANUP_ACTION="not-needed"
DASHBOARD_CHANGED=0
DASHBOARD_PREPARED=0
MATCHER_CHANGED=0
DASHBOARD_ROLLBACK_TAG=""
MATCHER_BACKUP_DIR=""
HEALTH_RESULT='{}'
UNROUTED_SAFE='[]'
UNROUTED_COUNT="unknown"

EXPECTED_PATHS=$'dashboard/alert_admin_v2.py\ndashboard/map_app.py\ndashboard/operations_app.py\ndashboard/spatial_watch_app.py\ndashboard/static/map.css\ndashboard/static/style.css\ndashboard/templates/admin_tools.html\ndashboard/templates/alert_admin.html\ndashboard/templates/alerts.html\ndashboard/templates/deliveries.html\ndashboard/templates/index.html\ndashboard/templates/map.html\ndashboard/templates/modules.html\ndashboard/templates/nav.html\ndashboard/templates/operator_help.html\ndashboard/templates/routing.html\ndashboard/templates/spatial_reference_detail.html\ndashboard/templates/subscribers.html\ndashboard/templates/watchlist.html\ndashboard/tests/test_spatial_watch_pack.py\ndashboard/tests/test_watchlist_reliability.py\ndeploy/n8n/install_spatial_watch_matcher.sh\ndeploy/releases/alerting-spatial-watch-simplification.sh\nworkflows/core/CORE_Watchlist_Matcher_v1.json\nworkflows/live/CORE_Watchlist_Matcher_live.json'

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

dashboard_release_is_live(){
  docker exec -i citymanager-dashboard python - "$RELEASE_ID" <<'PY' >/dev/null 2>&1
import json,os,sys,urllib.request
token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: raise SystemExit(1)
request=urllib.request.Request(
    'http://127.0.0.1:8000/api/spatial-watch/release',
    headers={'X-CMOS-Automation-Key':token},
)
with urllib.request.urlopen(request,timeout=10) as response: payload=json.load(response)
if payload.get('release_id')!=sys.argv[1]: raise SystemExit(1)
PY
}

n8n_database(){
  local n8n_dir
  n8n_dir="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
  [[ -n "$n8n_dir" && -f "$n8n_dir/database.sqlite" ]] || return 1
  printf '%s/database.sqlite' "$n8n_dir"
}

matcher_release_is_live(){
  local database
  database="$(n8n_database)" || return 1
  python3 - "$database" "$MATCHER_ID" <<'PY' >/dev/null 2>&1
import json,sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
row=con.execute('SELECT active,activeVersionId,nodes FROM workflow_entity WHERE id=?',(sys.argv[2],)).fetchone()
if not row or not row['active'] or not row['activeVersionId']: raise SystemExit(1)
nodes={node.get('name'):node for node in json.loads(row['nodes'])}
code=((nodes.get('Match + Resolve Recipients') or {}).get('parameters') or {}).get('jsCode','')
required=('locationPlusTopic','LOCATION_TOPIC','municipalityLocationMatch','locationPlusTopic && !locationMatched')
if not all(value in code for value in required): raise SystemExit(1)
PY
}

cleanup_probe(){
  docker inspect citymanager-dashboard >/dev/null 2>&1 || return 0
  if docker exec -i citymanager-dashboard python - "$PROBE_NAME" <<'PY' >/dev/null 2>&1
import sys
from app import db_conn
with db_conn() as conn:
    with conn.cursor() as cur:
        cur.execute('DELETE FROM watch_items WHERE display_name=%s',(sys.argv[1],))
    conn.commit()
PY
  then PROBE_CLEANUP_ACTION="verified-clean"; else PROBE_CLEANUP_ACTION="failed"; fi
}

rollback_dashboard(){
  [[ "$DASHBOARD_PREPARED" == 1 && -n "$DASHBOARD_ROLLBACK_TAG" ]] || return 0
  docker image inspect "$DASHBOARD_ROLLBACK_TAG" >/dev/null 2>&1 || return 1
  if [[ "$DASHBOARD_CHANGED" != 1 ]]; then
    docker image tag "$DASHBOARD_ROLLBACK_TAG" dashboard-citymanager-dashboard:latest
    DASHBOARD_ACTION="build-rolled-back-no-recreate"
    return 0
  fi
  log "ROLLBACK: restoring the previous dashboard image"
  docker image tag "$DASHBOARD_ROLLBACK_TAG" dashboard-citymanager-dashboard:latest
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

rollback_matcher(){
  [[ "$MATCHER_CHANGED" == 1 && -n "$MATCHER_BACKUP_DIR" ]] || return 0
  local backup="$MATCHER_BACKUP_DIR/CORE_Watchlist_Matcher_pre56.json"
  local target="/tmp/CORE_Watchlist_Matcher_alerting_rollback_${RUN_ID}.json"
  [[ -s "$backup" ]] || return 1
  log "ROLLBACK: restoring the previous central matcher"
  docker cp "$backup" "n8n:$target" >/dev/null
  docker exec -u root n8n chown node:node "$target"
  docker exec -u root n8n chmod 600 "$target"
  docker exec -u node n8n n8n import:workflow --input="$target" >/dev/null
  if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
    docker exec -u node n8n n8n publish:workflow --id="$MATCHER_ID" >/dev/null
  else
    docker exec -u node n8n n8n update:workflow --id="$MATCHER_ID" --active=true >/dev/null
  fi
  docker restart n8n >/dev/null
  for _ in $(seq 1 30); do
    if docker exec n8n node -e \
      "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
      >/dev/null 2>&1; then MATCHER_ACTION="rolled-back"; return 0; fi
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
  parent="$(mktemp -d /tmp/cmos-alerting-report.XXXXXX)"
  worktree="$parent/worktree"
  git -C "$REPO" worktree add --detach "$worktree" "origin/$REPORT_BRANCH" >/dev/null 2>&1 || return 1
  report_dir="$worktree/operation-results/alerting-spatial-watch-simplification"
  mkdir -p "$report_dir"
  report_file="$report_dir/$RUN_ID-${status,,}.md"
  {
    printf '# Alerting and spatial Watch simplification\n\n'
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
    printf '| Central matcher | `%s` |\n' "$MATCHER_ACTION"
    printf '| Focused acceptance | `%s` |\n' "$ACCEPTANCE_ACTION"
    printf '| Temporary Watch | `%s` |\n' "$PROBE_CLEANUP_ACTION"
    printf '| Active Watches needing Recipient | `%s` |\n' "$UNROUTED_COUNT"
    printf '| Schema changes | `none` |\n'
    printf '| New database/GIS/Watch/Notification systems | `none` |\n'
    printf '| Full E2E | `not run` |\n'
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '## Sanitized health and acceptance\n\n```json\n%s\n```\n\n' "$HEALTH_RESULT"
    printf '## Sanitized Needs Recipient references\n\n```json\n%s\n```\n\n' "$UNROUTED_SAFE"
    printf 'Names, addresses, Recipient details, Notification channels, and payloads are intentionally excluded. '
    printf 'The private mode-600 log remains on the VPS at `%s`.\n' "$LOG_FILE"
  } > "$report_file"
  install -m 600 "$report_file" "$report_dir/latest.md"
  git -C "$worktree" add operation-results/alerting-spatial-watch-simplification
  git -C "$worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit \
    -m "Record alerting simplification ${status,,} $RUN_ID" >/dev/null || return 1
  git -C "$worktree" push -q origin "HEAD:refs/heads/$REPORT_BRANCH" || return 1
  log "SANITIZED_REPORT=https://github.com/gioahmad/city-manager-os/blob/$REPORT_BRANCH/operation-results/alerting-spatial-watch-simplification/latest.md"
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
    rollback_matcher || log "WARNING: matcher rollback was not confirmed"
    cleanup_probe
    restore_checkout || log "WARNING: repository restore was not confirmed"
  else
    cleanup_probe
  fi
  (( rc == 0 )) && FINAL_STATUS="PASS"
  section "ALERTING + SPATIAL WATCH SIMPLIFICATION: $FINAL_STATUS"
  printf 'STATUS=%s\nPHASE=%s\nTARGET=%s\nPRIVATE_LOG=%s\n' "$FINAL_STATUS" "$CURRENT_PHASE" "$TARGET_HEAD" "$LOG_FILE"
  publish_report "$FINAL_STATUS" "$rc" || log "WARNING: sanitized GitHub report could not be published"
  exit "$rc"
}

trap on_error ERR
trap on_exit EXIT

for command in git docker python3 flock mktemp install tee sha256sum; do
  command -v "$command" >/dev/null || fail "$command is required"
done
mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another alerting simplification release is already running"

CURRENT_PHASE="preflight"
section "ALERTING + SPATIAL WATCH SIMPLIFICATION"
cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "production repository must be clean"
git fetch -q origin main "+refs/heads/$REPORT_BRANCH:refs/remotes/origin/$REPORT_BRANCH"
TARGET_HEAD="$(git rev-parse origin/main)"
git merge-base --is-ancestor "$EXPECTED_BASE" "$TARGET_HEAD" || fail "origin/main does not descend from the accepted production base"
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
  ARCHIVE_BRANCH="production-archive/alerting-simplification-$RUN_ID"
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

for name in citymanager-dashboard citymanager-postgis n8n ntfy; do
  [[ "$(docker inspect "$name" --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] || fail "required container is not running: $name"
done

PLAN="$(python3 deploy/cmos-deploy plan --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null)"
printf '%s\n' "$PLAN"
grep -q '^build=yes$' <<<"$PLAN"
grep -q '^services=citymanager-dashboard$' <<<"$PLAN"
grep -q '^backup_required=no$' <<<"$PLAN"
grep -q '^external=n8n-workflow-publish$' <<<"$PLAN"
grep -q '^unknown=none$' <<<"$PLAN"
log "PREFLIGHT PASS: one dashboard image and one guarded matcher publish; no schema, GIS import, or full E2E"

section "PRIVATE NEEDS RECIPIENT IDENTIFICATION"
PRIVATE_UNROUTED="$(docker exec -i citymanager-dashboard python - <<'PY'
from app import db_conn
with db_conn() as conn,conn.cursor() as cur:
    cur.execute('''
      SELECT w.id::text,w.display_name
      FROM watch_items w
      WHERE w.active
        AND (w.starts_at IS NULL OR w.starts_at<=now())
        AND (w.expires_at IS NULL OR w.expires_at>now())
        AND NOT EXISTS (
          SELECT 1 FROM watch_item_recipients wir
          JOIN subscribers s ON s.id=wir.subscriber_id
          WHERE wir.watch_item_id=w.id AND wir.active AND s.active
        )
      ORDER BY w.display_name
    ''')
    rows=cur.fetchall()
print(f'count={len(rows)}')
for row in rows: print(f"- {row['display_name']} [private-ref {row['id']}]")
PY
)"
printf '%s\n' "$PRIVATE_UNROUTED"
UNROUTED_COUNT="$(sed -n 's/^count=//p' <<<"$PRIVATE_UNROUTED")"
UNROUTED_SAFE="$(docker exec -i citymanager-dashboard python - "$RUN_ID" <<'PY'
import hashlib,json,sys
from app import db_conn
with db_conn() as conn,conn.cursor() as cur:
    cur.execute('''
      SELECT w.id::text FROM watch_items w
      WHERE w.active AND (w.starts_at IS NULL OR w.starts_at<=now())
        AND (w.expires_at IS NULL OR w.expires_at>now())
        AND NOT EXISTS (SELECT 1 FROM watch_item_recipients wir JOIN subscribers s ON s.id=wir.subscriber_id WHERE wir.watch_item_id=w.id AND wir.active AND s.active)
      ORDER BY w.id
    ''')
    refs=[hashlib.sha256(f'{sys.argv[1]}:{row["id"]}'.encode()).hexdigest()[:12] for row in cur.fetchall()]
print(json.dumps(refs))
PY
)"

CURRENT_PHASE="running-receipt"
publish_report "RUNNING" "0" || log "WARNING: start receipt could not be published"

CURRENT_PHASE="change-aware-verification"
if dashboard_release_is_live; then
  DASHBOARD_ACTION="already-current-no-build"
else
  old_image="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
  DASHBOARD_ROLLBACK_TAG="dashboard-citymanager-dashboard:cmos-alerting-rollback-$RUN_ID"
  docker image tag "$old_image" "$DASHBOARD_ROLLBACK_TAG"
  DASHBOARD_PREPARED=1
  python3 deploy/cmos-deploy verify --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null
  DASHBOARD_ACTION="verified-one-build"
fi

CURRENT_PHASE="guarded-matcher-publish"
if matcher_release_is_live; then
  MATCHER_ACTION="already-current-no-restart"
else
  MATCHER_BACKUP_DIR="/var/backups/city-manager-os/spatial-watch-matcher/$RUN_ID"
  CMOS_MATCHER_BACKUP_DIR="$MATCHER_BACKUP_DIR" bash deploy/n8n/install_spatial_watch_matcher.sh "$TARGET_HEAD"
  MATCHER_CHANGED=1
  [[ -s "$MATCHER_BACKUP_DIR/CORE_Watchlist_Matcher_pre56.json" ]] || fail "matcher rollback artifact is missing"
  MATCHER_ACTION="published-one-restart"
  matcher_release_is_live || fail "published matcher contract is not live"
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
  dashboard_release_is_live || fail "dashboard release contract is not live"
  DASHBOARD_ACTION="deployed-one-build-one-recreate"
fi

CURRENT_PHASE="focused-acceptance"
HEALTH_RESULT="$(docker exec -i citymanager-dashboard python - "$RELEASE_ID" "$PROBE_NAME" <<'PY'
import json,os,sys,urllib.parse,urllib.request
from app import db_conn

release_id,probe_name=sys.argv[1:]
token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: raise RuntimeError('authenticated acceptance token is unavailable')
headers={'X-CMOS-Automation-Key':token}

def get(path,expect_json=False):
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers)
    with urllib.request.urlopen(request,timeout=30) as response:
        body=response.read()
        if response.status!=200 or '/login' in response.geturl(): raise RuntimeError('authenticated page acceptance failed')
        if b'Internal Server Error' in body: raise RuntimeError('an internal error was exposed')
        return json.loads(body) if expect_json else body.decode(errors='replace')

def post(path,values,expect_json=False):
    request=urllib.request.Request(
        'http://127.0.0.1:8000'+path,
        data=urllib.parse.urlencode(values,doseq=True).encode(),
        headers={**headers,'Content-Type':'application/x-www-form-urlencoded'},method='POST')
    with urllib.request.urlopen(request,timeout=30) as response:
        body=response.read()
        if response.status!=200 or '/login' in response.geturl(): raise RuntimeError('authenticated action acceptance failed')
        if b'Internal Server Error' in body or b'Could not complete that change' in body: raise RuntimeError('a friendly action failed')
        return json.loads(body) if expect_json else body.decode(errors='replace')

release=get('/api/spatial-watch/release',True)
if release.get('release_id')!=release_id or release.get('default_distance_ft')!=5280: raise RuntimeError('release contract mismatch')
watch_page=get('/watchlist')
for label in ('What should we watch for?','Where should we watch?','Who should be notified?','Test it','Turn it on','Needs Recipient','Delivery Problem'):
    if label not in watch_page: raise RuntimeError('Watch interface contract mismatch')
map_page=get('/map')
for label in ('Last 6 hours','Last 12 hours','Last 24 hours','Last week','Search Visible Area'):
    if label not in map_page: raise RuntimeError('map alert window contract mismatch')
get('/map/system/alerts.geojson?hours=6&min_priority=1',True)
alerts_page=get('/alerts?window=all&state=all')
if 'All stored history' not in alerts_page or 'Search the complete alert database' not in alerts_page: raise RuntimeError('global alert search contract mismatch')

with db_conn() as conn,conn.cursor() as cur:
    cur.execute('''
      SELECT a.id::text AS alert_uuid,a.alert_id,a.source,g.objectid::text AS address_objectid
      FROM alerts a
      JOIN LATERAL (
        SELECT ga.objectid,ga.geom FROM gis_addresses ga
        WHERE ga.geom IS NOT NULL ORDER BY ga.geom <-> a.geom LIMIT 1
      ) g ON ST_DWithin(g.geom::geography,a.geom::geography,1609.344)
      WHERE a.geom IS NOT NULL AND nullif(trim(a.source),'') IS NOT NULL
      ORDER BY a.received_at DESC LIMIT 1
    ''')
    target=cur.fetchone()
    cur.execute('SELECT id::text FROM subscribers WHERE active ORDER BY created_at LIMIT 1')
    recipient=cur.fetchone()
    cur.execute('''
      SELECT count(*) AS total
      FROM deliveries d
      WHERE d.status='SENT'
        AND EXISTS (
          SELECT 1
          FROM watch_item_recipients wir
          JOIN watch_items w ON w.id=wir.watch_item_id
          WHERE wir.subscriber_id=d.subscriber_id
            AND d.matched_watch_ids ? w.watch_id
        )
    ''')
    delivered_route=cur.fetchone()
if not target: raise RuntimeError('no existing mapped alert and local address are available for spatial acceptance')
if not recipient: raise RuntimeError('no active Recipient is available for Notification acceptance')
if not delivered_route or int(delivered_route['total'])<1: raise RuntimeError('no existing routed Notification delivery evidence is available')

try:
    with db_conn() as conn,conn.cursor() as cur:
        cur.execute('SELECT (SELECT count(*) FROM alerts) AS alerts,(SELECT count(*) FROM alert_watch_matches) AS matches,(SELECT count(*) FROM watch_item_recipients) AS recipients,(SELECT count(*) FROM deliveries) AS deliveries')
        before_test=cur.fetchone()
    test_result=post('/api/watchlist/test-notification',{'recipient_id':recipient['id']},True)
    if not test_result.get('ok') or not test_result.get('isolated'): raise RuntimeError('isolated Test Notification was not accepted')
    with db_conn() as conn,conn.cursor() as cur:
        cur.execute('SELECT (SELECT count(*) FROM alerts) AS alerts,(SELECT count(*) FROM alert_watch_matches) AS matches,(SELECT count(*) FROM watch_item_recipients) AS recipients,(SELECT count(*) FROM deliveries) AS deliveries')
        after_test=cur.fetchone()
    if before_test!=after_test: raise RuntimeError('Test Notification wrote to alert, Match, Recipient, or delivery history')

    post('/watchlist/create',{
      'display_name':probe_name,'setup_mode':'LOCATION','location_kind':'ADDRESS',
      'location_id':target['address_objectid'],'radius_ft':'5280','duration':'PERMANENT',
      'min_priority':'1','subscriber_ids':recipient['id']})
    with db_conn() as conn,conn.cursor() as cur:
        cur.execute('''SELECT w.id::text,w.watch_id,w.radius_ft,w.nearby_enabled,
          ST_GeometryType(w.spatial_target_geom) AS target_type,ST_GeometryType(w.spatial_geom) AS area_type,
          count(*) FILTER (WHERE wir.active AND s.active) AS recipient_count
          FROM watch_items w LEFT JOIN watch_item_recipients wir ON wir.watch_item_id=w.id
          LEFT JOIN subscribers s ON s.id=wir.subscriber_id WHERE w.display_name=%s GROUP BY w.id''',(probe_name,))
        watch=cur.fetchone()
    if not watch or not watch['nearby_enabled'] or float(watch['radius_ft'])!=5280 or not watch['target_type'] or not watch['area_type'] or int(watch['recipient_count'])!=1:
        raise RuntimeError('one-mile Location Watch creation or Recipient connection failed')

    post(f"/watchlist/{watch['id']}/update",{
      'display_name':probe_name,'setup_mode':'LOCATION_TOPIC','location_kind':'EXISTING',
      'search_term':target['source'],'match_mode':'CONTAINS','min_priority':'1','radius_ft':'5280',
      'duration':'PERMANENT','keep_state':'1','subscriber_ids':recipient['id']})
    with db_conn() as conn,conn.cursor() as cur:
        cur.execute('SELECT watch_type,nearby_enabled,radius_ft,spatial_target_geom IS NOT NULL AS has_target FROM watch_items WHERE id=%s',(watch['id'],))
        edited=cur.fetchone()
    if not edited or edited['watch_type']!='LOCATION_TOPIC' or not edited['nearby_enabled'] or not edited['has_target'] or float(edited['radius_ft'])!=5280:
        raise RuntimeError('Location plus topic edit did not preserve the selected Location')

    post(f"/watchlist/{watch['id']}/toggle",{'action':'pause'})
    with db_conn() as conn,conn.cursor() as cur:
        cur.execute('SELECT active FROM watch_items WHERE id=%s',(watch['id'],)); paused=cur.fetchone()
    if not paused or paused['active']: raise RuntimeError('pause failed')
    post(f"/watchlist/{watch['id']}/toggle",{'action':'reactivate'})
    with db_conn() as conn,conn.cursor() as cur:
        cur.execute('SELECT active FROM watch_items WHERE id=%s',(watch['id'],)); active=cur.fetchone()
    if not active or not active['active']: raise RuntimeError('reactivation failed')

    with db_conn() as conn,conn.cursor() as cur:
        cur.execute('SELECT count(*) AS total FROM gis_active_spatial_watch_matches(%s) WHERE watch_item_id=%s',(target['alert_id'],watch['id']))
        spatial_match=cur.fetchone()
        if int(spatial_match['total'])!=1: raise RuntimeError('PostGIS did not match the existing alert inside the one-mile Watch')
        cur.execute('''INSERT INTO alert_watch_matches(alert_id,watch_item_id,match_type,match_reason)
          VALUES(%s,%s,'RELEASE_ACCEPTANCE','Existing alert inside one-mile Watch; isolated release verification')
          ON CONFLICT(alert_id,watch_item_id,match_type) DO NOTHING''',(target['alert_uuid'],watch['id']))
        conn.commit()
    evidence=get('/watchlist')
    if probe_name not in evidence or 'Latest Match:' not in evidence: raise RuntimeError('Watch Match evidence is not visible')

    post(f"/watchlist/{watch['id']}/delete",{'confirm_delete':'DELETE'})
    with db_conn() as conn,conn.cursor() as cur:
        cur.execute('SELECT count(*) AS total FROM watch_items WHERE id=%s',(watch['id'],)); remaining=cur.fetchone()
        cur.execute('SELECT count(*) AS total FROM watch_item_recipients WHERE watch_item_id=%s',(watch['id'],)); routes=cur.fetchone()
        cur.execute('SELECT count(*) AS total FROM alert_watch_matches WHERE watch_item_id=%s',(watch['id'],)); matches=cur.fetchone()
    if any(int(row['total']) for row in (remaining,routes,matches)): raise RuntimeError('temporary Watch deletion did not finish cleanly')
finally:
    with db_conn() as conn,conn.cursor() as cur:
        cur.execute('DELETE FROM watch_items WHERE display_name=%s',(probe_name,))
        conn.commit()

health=get('/api/watchlist/health',True)
for key in ('spatial_missing_target','invalid_schedule','routes_to_inactive_subscribers'):
    if int(health.get(key) or 0)!=0: raise RuntimeError('Watch health blocker remains')
safe={
  'status':health.get('status'),'active_now':int(health.get('active_now') or 0),
  'routed_now':int(health.get('routed_now') or 0),'unrouted_now':int(health.get('unrouted_now') or 0),
  'matches_7d':int(health.get('matches_7d') or 0),'sent_24h':int(health.get('sent_24h') or 0),
  'failed_24h':int(health.get('failed_24h') or 0),
  'one_mile_location_watch':'PASS','edit_location_plus_topic':'PASS','pause_reactivate':'PASS',
  'postgis_match':'PASS','recipient_connection':'PASS','isolated_test_notification':'PASS',
  'existing_route_delivery_evidence':'PASS',
  'test_notification_database_writes':0,'delete_and_cleanup':'PASS','map_windows':'PASS','global_search':'PASS',
}
print(json.dumps(safe,sort_keys=True))
PY
)"
printf '%s\n' "$HEALTH_RESULT" | python3 -m json.tool
ACCEPTANCE_ACTION="pass-create-edit-pause-activate-match-route-test-deliver-delete"
PROBE_CLEANUP_ACTION="verified-clean"

CURRENT_PHASE="final-health"
docker exec citymanager-dashboard python -c "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=10).status==200"
docker exec n8n node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
[[ "$(docker inspect citymanager-postgis --format '{{.State.Running}}')" == true ]]
[[ "$(docker inspect ntfy --format '{{.State.Running}}')" == true ]]

CURRENT_PHASE="complete"
section "RELEASE COMPLETE"
printf 'TARGET=%s\nDASHBOARD=%s\nMATCHER=%s\n' "$TARGET_HEAD" "$DASHBOARD_ACTION" "$MATCHER_ACTION"
printf 'DATABASE_SCHEMA_CHANGES=NONE\nFULL_E2E=NOT_RUN\nTEMPORARY_WATCH=REMOVED\n'
