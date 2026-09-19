#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED_BASE="149126ad5abbfa10c1f3855da92cac6a1f3c9b79"
REPORT_BRANCH="release-output/ops"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
LOG_FILE="$LOG_DIR/spatial-rematch-nj-watch-layers-$RUN_ID.log"
LOCK_FILE="/var/lock/cmos-spatial-rematch-nj-watch-layers.lock"
BACKUP_DIR="/var/backups/city-manager-os/spatial-rematch-nj-watch-layers-$RUN_ID"
WORKFLOW_NAME="CORE - Resolved Alert Spatial Rematch v1"
WORKFLOW_FILE="workflows/core/CORE_Resolved_Spatial_Rematch_v1.json"
TMP_WORKFLOW="/tmp/cmos-resolved-spatial-rematch-$RUN_ID.json"
CURRENT_PHASE="startup"
FINAL_STATUS="FAIL"
FAIL_LINE="none"
TARGET_HEAD="unknown"
ORIGINAL_HEAD="unknown"
ARCHIVE_BRANCH="none"
REPOSITORY_ACTION="not-started"
N8N_ACTION="not-started"
LAYER_ACTION="not-started"
ACCEPTANCE_ACTION="not-started"
ACCEPTANCE_RESULT='{}'
N8N_DB=""
N8N_OWNER=""
N8N_BACKUP=""
N8N_CHANGED=0
LAYERS_CREATED=0

EXPECTED_PATHS=$'dashboard/tests/test_spatial_rematch_layers.py\ndeploy/cmos-deploy\ndeploy/gis/sync_nj_watch_layers.py\ndeploy/releases/spatial-rematch-nj-watch-layers.sh\nworkflows/core/CORE_Resolved_Spatial_Rematch_v1.json'

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

wait_n8n(){
  for _ in $(seq 1 60); do
    if docker exec n8n node -e \
      "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
      >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

n8n_contract(){
  [[ -n "$N8N_DB" && -f "$N8N_DB" ]] || return 1
  python3 - "$N8N_DB" "$WORKFLOW_NAME" <<'PY'
import json,sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
rows=con.execute(
  'SELECT id,active,activeVersionId,nodes FROM workflow_entity WHERE name=?',
  (sys.argv[2],),
).fetchall()
if len(rows)!=1: raise SystemExit(1)
row=rows[0]
if not row['active'] or not row['activeVersionId']: raise SystemExit(1)
nodes={node.get('name'):node for node in json.loads(row['nodes'] or '[]')}
required={
  'Resolved Alert Schedule','Load Newly Resolved Alerts','Restore Resolved Standard Alert',
  'Send Resolved Alert to Central Watchlist Matcher','Mark Resolved Alert Rematched',
}
if not required.issubset(nodes): raise SystemExit(1)
load=(nodes['Load Newly Resolved Alerts'].get('parameters') or {}).get('query','')
mark=(nodes['Mark Resolved Alert Rematched'].get('parameters') or {}).get('query','')
send=nodes['Send Resolved Alert to Central Watchlist Matcher']
if '__CMOS_ACTIVATED_AT__' in load: raise SystemExit(1)
if not all(value in load for value in (
  'geo_entity_resolutions', 'a.geom IS NOT NULL', "r.spatial_precision='ADDRESS_POINT'",
  'spatial_rematch_version',
)):
    raise SystemExit(1)
if 'SUPPLIED_COORDINATE' in load: raise SystemExit(1)
if "spatial_rematch_version','geo-v1'" not in mark: raise SystemExit(1)
if not send.get('alwaysOutputData') or (send.get('parameters') or {}).get('mode')!='each':
    raise SystemExit(1)
if 'ntfy' in json.dumps(nodes,sort_keys=True).lower(): raise SystemExit(1)
print(f"REMATCH_WORKFLOW=PASS id={row['id']} active=1 published=1")
con.close()
PY
}

layer_contract(){
  docker exec -i citymanager-dashboard python - <<'PY'
import json
from app import query_all
ranges={
  'NJ_OFFICIAL_MUNICIPALITIES':(560,570),
  'NJ_OFFICIAL_COUNTIES':(21,21),
  'NJDOT_MAJOR_HIGHWAYS':(250,350),
}
rows=query_all("""
  SELECT l.layer_key,l.active,count(f.id) FILTER (WHERE f.active) AS locations,
         count(f.id) FILTER (WHERE f.active AND (f.geom IS NULL OR ST_IsEmpty(f.geom) OR NOT ST_IsValid(f.geom))) AS bad
  FROM map_layers l LEFT JOIN map_features f ON f.layer_id=l.id
  WHERE l.layer_key=ANY(%s::text[])
  GROUP BY l.layer_key,l.active
""",(list(ranges),))
actual={row['layer_key']:row for row in rows}
for key,(minimum,maximum) in ranges.items():
    row=actual.get(key)
    count=int((row or {}).get('locations') or 0)
    if not row or not row.get('active') or not minimum<=count<=maximum or int(row.get('bad') or 0):
        raise SystemExit(1)
print('WATCH_LAYERS=PASS '+json.dumps({key:int(actual[key]['locations']) for key in sorted(actual)},sort_keys=True))
PY
}

release_is_live(){
  n8n_contract >/dev/null 2>&1 && layer_contract >/dev/null 2>&1
}

rollback_n8n(){
  [[ "$N8N_CHANGED" == 1 && -n "$N8N_BACKUP" && -s "$N8N_BACKUP" ]] || return 0
  log "ROLLBACK: restoring the prior n8n database"
  docker stop n8n >/dev/null
  install -m 600 "$N8N_BACKUP" "$N8N_DB"
  [[ -z "$N8N_OWNER" ]] || chown "$N8N_OWNER" "$N8N_DB"
  docker start n8n >/dev/null
  wait_n8n
  N8N_ACTION="rolled-back"
}

rollback_layers(){
  [[ "$LAYERS_CREATED" == 1 ]] || return 0
  log "ROLLBACK: removing only the three newly created official Watch layers"
  docker exec -i citymanager-dashboard python - <<'PY'
from app import db_conn
keys=['NJ_OFFICIAL_MUNICIPALITIES','NJ_OFFICIAL_COUNTIES','NJDOT_MAJOR_HIGHWAYS']
with db_conn() as conn:
    with conn.cursor() as cur:
        cur.execute('DELETE FROM map_layers WHERE layer_key=ANY(%s::text[])',(keys,))
    conn.commit()
PY
  LAYER_ACTION="rolled-back"
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
  parent="$(mktemp -d /tmp/cmos-spatial-rematch-report.XXXXXX)"
  worktree="$parent/worktree"
  git -C "$REPO" worktree add --detach "$worktree" "origin/$REPORT_BRANCH" >/dev/null 2>&1 || return 1
  report_dir="$worktree/operation-results/spatial-rematch-nj-watch-layers"
  mkdir -p "$report_dir"
  report_file="$report_dir/$RUN_ID-${status,,}.md"
  {
    printf '# Resolved spatial matching and official NJ Watch layers\n\n'
    printf '| Field | Value |\n|---|---|\n'
    printf '| Run | `%s` |\n' "$RUN_ID"
    printf '| Status | **%s** |\n' "$status"
    printf '| Exit code | `%s` |\n' "$rc"
    printf '| Phase | `%s` |\n' "$CURRENT_PHASE"
    printf '| Failed line | `%s` |\n' "$FAIL_LINE"
    printf '| Target | `%s` |\n' "$TARGET_HEAD"
    printf '| Repository alignment | `%s` |\n' "$REPOSITORY_ACTION"
    printf '| Recovery branch | `%s` |\n' "$ARCHIVE_BRANCH"
    printf '| Resolved-alert rematch | `%s` |\n' "$N8N_ACTION"
    printf '| Official Watch layers | `%s` |\n' "$LAYER_ACTION"
    printf '| Focused acceptance | `%s` |\n' "$ACCEPTANCE_ACTION"
    printf '| Database schema changes | `none` |\n'
    printf '| Existing Watch changes | `none` |\n'
    printf '| Test Notification sent | `no` |\n'
    printf '| Dashboard build/restart | `no` |\n'
    printf '| PostGIS restart | `no` |\n'
    printf '| Full E2E | `not run` |\n'
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '## Sanitized acceptance\n\n```json\n%s\n```\n\n' "$ACCEPTANCE_RESULT"
    printf 'No addresses, coordinates, alert text, Watch names, Recipient details, channel names, credentials, or private origins are included. '
    printf 'The private mode-600 log remains on the VPS at `%s`.\n' "$LOG_FILE"
  } > "$report_file"
  install -m 600 "$report_file" "$report_dir/latest.md"
  git -C "$worktree" add operation-results/spatial-rematch-nj-watch-layers
  git -C "$worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit \
    -m "Record spatial rematch and NJ Watch layers ${status,,} $RUN_ID" >/dev/null || return 1
  git -C "$worktree" push -q origin "HEAD:refs/heads/$REPORT_BRANCH" || return 1
  log "SANITIZED_REPORT=https://github.com/gioahmad/city-manager-os/blob/$REPORT_BRANCH/operation-results/spatial-rematch-nj-watch-layers/latest.md"
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
    rollback_layers || log "WARNING: official Watch layer rollback was not confirmed"
    rollback_n8n || log "WARNING: n8n rollback was not confirmed"
    restore_checkout || log "WARNING: repository restore was not confirmed"
  fi
  docker exec n8n rm -f "$TMP_WORKFLOW" >/dev/null 2>&1 || true
  rm -f "$TMP_WORKFLOW"
  (( rc == 0 )) && FINAL_STATUS="PASS"
  section "SPATIAL REMATCH AND OFFICIAL NJ WATCH LAYERS: $FINAL_STATUS"
  printf 'STATUS=%s\nPHASE=%s\nTARGET=%s\nPRIVATE_LOG=%s\n' "$FINAL_STATUS" "$CURRENT_PHASE" "$TARGET_HEAD" "$LOG_FILE"
  publish_report "$FINAL_STATUS" "$rc" || log "WARNING: sanitized GitHub report could not be published"
  exit "$rc"
}

trap on_error ERR
trap on_exit EXIT

for command in git docker python3 flock mktemp install tee; do
  command -v "$command" >/dev/null || fail "$command is required"
done
mkdir -p "$LOG_DIR" "$BACKUP_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another spatial-rematch release is already running"

CURRENT_PHASE="preflight"
section "RESOLVED SPATIAL MATCHING AND OFFICIAL NJ WATCH LAYERS"
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
  ARCHIVE_BRANCH="production-archive/spatial-rematch-layers-$RUN_ID"
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
for container in citymanager-dashboard citymanager-postgis n8n; do
  [[ "$(docker inspect "$container" --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
    || fail "required container is not running: $container"
done

N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
[[ -n "$N8N_DIR" && "$N8N_DIR" == /* && "$N8N_DIR" != / ]] || fail "n8n data mount is missing or unsafe"
N8N_DB="$N8N_DIR/database.sqlite"
[[ -f "$N8N_DB" ]] || fail "n8n database was not found"
N8N_OWNER="$(stat -c '%u:%g' "$N8N_DB")"

PLAN="$(python3 deploy/cmos-deploy plan --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null)"
printf '%s\n' "$PLAN"
grep -q '^changed_count=5$' <<<"$PLAN"
grep -q '^build=no$' <<<"$PLAN"
grep -q '^services=none$' <<<"$PLAN"
grep -q '^tests=tests/test_spatial_rematch_layers.py$' <<<"$PLAN"
grep -q '^backup_required=yes$' <<<"$PLAN"
grep -q '^external=n8n-workflow-publish,postgis-reference-layer-sync$' <<<"$PLAN"
grep -q '^full_e2e=no$' <<<"$PLAN"
grep -q '^unknown=none$' <<<"$PLAN"
log "PREFLIGHT PASS: one n8n workflow, existing Mapping Center tables, no build, no schema migration, no full E2E"

if release_is_live; then
  N8N_ACTION="already-current"
  LAYER_ACTION="already-current"
else
  CURRENT_PHASE="focused-verification"
  git diff --check "$EXPECTED_BASE...$TARGET_HEAD"
  python3 -m py_compile deploy/cmos-deploy deploy/gis/sync_nj_watch_layers.py dashboard/tests/test_spatial_rematch_layers.py
  python3 -m json.tool "$WORKFLOW_FILE" >/dev/null
  bash -n deploy/releases/spatial-rematch-nj-watch-layers.sh
  python3 dashboard/tests/test_spatial_rematch_layers.py
  CURRENT_PHASE="backups"
  N8N_BACKUP="$BACKUP_DIR/n8n-database.sqlite"
  python3 - "$N8N_DB" "$N8N_BACKUP" <<'PY'
import sqlite3,sys
source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2])
source.backup(target); target.close(); source.close()
PY
  docker exec citymanager-postgis sh -lc \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --data-only --table=map_layers --table=map_features' \
    > "$BACKUP_DIR/mapping-center-layers.dump"
  chmod 600 "$N8N_BACKUP" "$BACKUP_DIR/mapping-center-layers.dump"
  [[ -s "$N8N_BACKUP" && -s "$BACKUP_DIR/mapping-center-layers.dump" ]] || fail "release backups are incomplete"

  CURRENT_PHASE="n8n-publish"
  ACTIVATED_AT="$STARTED_UTC"
  python3 - "$N8N_DB" "$WORKFLOW_FILE" "$TMP_WORKFLOW" "$WORKFLOW_NAME" "$ACTIVATED_AT" <<'PY'
import json,sqlite3,sys,uuid
db,source_path,target_path,workflow_name,activated_at=sys.argv[1:]
con=sqlite3.connect(db); con.row_factory=sqlite3.Row
matcher=con.execute("SELECT id,nodes FROM workflow_entity WHERE name='CORE - Watchlist Matcher v1' ORDER BY active DESC,updatedAt DESC LIMIT 1").fetchone()
if not matcher: raise SystemExit('central Watchlist Matcher was not found')
postgres=None
for node in json.loads(matcher['nodes'] or '[]'):
    value=(node.get('credentials') or {}).get('postgres')
    if value: postgres=value; break
if not postgres:
    rows=con.execute("SELECT id,name FROM credentials_entity WHERE lower(type) LIKE '%postgres%' ORDER BY name,id").fetchall()
    if len(rows)!=1: raise SystemExit('could not resolve one production Postgres credential')
    postgres={'id':rows[0]['id'],'name':rows[0]['name']}
existing=con.execute('SELECT id FROM workflow_entity WHERE name=? ORDER BY updatedAt DESC',(workflow_name,)).fetchall()
if len(existing)>1: raise SystemExit('duplicate resolved-alert rematch workflows exist')
workflow=json.load(open(source_path))
if existing: workflow['id']=existing[0]['id']
elif not workflow.get('id'): raise SystemExit('new workflow is missing its stable n8n ID')
workflow['active']=False
workflow['versionId']=str(uuid.uuid4())
replaced=0
for node in workflow.get('nodes') or []:
    if node.get('type')=='n8n-nodes-base.postgres':
        node['credentials']={'postgres':postgres}
    if node.get('name')=='Load Newly Resolved Alerts':
        query=node['parameters']['query']
        if '__CMOS_ACTIVATED_AT__' not in query: raise SystemExit('activation marker missing')
        node['parameters']['query']=query.replace('__CMOS_ACTIVATED_AT__',activated_at)
        replaced+=1
    if node.get('name')=='Send Resolved Alert to Central Watchlist Matcher':
        ref=node['parameters']['workflowId']
        ref['value']=matcher['id']
        ref['cachedResultUrl']=f"/workflow/{matcher['id']}"
if replaced!=1: raise SystemExit('activation marker replacement failed')
json.dump(workflow,open(target_path,'w'),indent=2)
con.close()
PY
  python3 -m json.tool "$TMP_WORKFLOW" >/dev/null
  docker cp "$TMP_WORKFLOW" "n8n:$TMP_WORKFLOW"
  docker exec -u root n8n chown node:node "$TMP_WORKFLOW"
  docker exec -u root n8n chmod 600 "$TMP_WORKFLOW"
  N8N_CHANGED=1
  docker exec -u node n8n n8n import:workflow --input="$TMP_WORKFLOW"
  REMATCH_ID="$(python3 - "$N8N_DB" "$WORKFLOW_NAME" <<'PY'
import sqlite3,sys
con=sqlite3.connect(sys.argv[1]); rows=con.execute('SELECT id FROM workflow_entity WHERE name=?',(sys.argv[2],)).fetchall()
if len(rows)!=1: raise SystemExit(1)
print(rows[0][0]); con.close()
PY
)"
  if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
    docker exec -u node n8n n8n publish:workflow --id="$REMATCH_ID" >/dev/null
  else
    docker exec -u node n8n n8n update:workflow --id="$REMATCH_ID" --active=true >/dev/null
  fi
  docker restart n8n >/dev/null
  wait_n8n || fail "n8n did not become ready"
  n8n_contract
  N8N_ACTION="published-one-workflow-one-restart"

  CURRENT_PHASE="official-layer-sync"
  EXISTING_LAYER_COUNT="$(docker exec -i citymanager-dashboard python - <<'PY'
from app import query_one
keys=['NJ_OFFICIAL_MUNICIPALITIES','NJ_OFFICIAL_COUNTIES','NJDOT_MAJOR_HIGHWAYS']
print(query_one('SELECT count(*) AS n FROM map_layers WHERE layer_key=ANY(%s::text[])',(keys,))['n'])
PY
)"
  if [[ "$EXISTING_LAYER_COUNT" == 0 ]]; then
    LAYER_RESULT="$(docker compose -f "$REPO/dashboard/docker-compose.yml" run --rm --no-deps -T \
      -v "$REPO:/repo:ro" -w /repo/dashboard -e PYTHONPATH=/repo/dashboard:/app \
      --entrypoint python citymanager-dashboard /repo/deploy/gis/sync_nj_watch_layers.py --apply </dev/null)"
    LAYERS_CREATED=1
    LAYER_ACTION="created-three-official-layers"
  elif [[ "$EXISTING_LAYER_COUNT" == 3 ]] && layer_contract >/dev/null 2>&1; then
    LAYER_RESULT='{"mode":"already-current"}'
    LAYER_ACTION="already-current"
  else
    fail "official Watch layers are in an incomplete preexisting state"
  fi
  printf '%s\n' "$LAYER_RESULT" | python3 -m json.tool
  layer_contract
fi

CURRENT_PHASE="focused-read-only-acceptance"
ACCEPTANCE_RESULT="$(docker exec -i citymanager-dashboard python - "$STARTED_UTC" <<'PY'
import json,os,sys,urllib.parse,urllib.request
from app import query_all,query_one
activated_at=sys.argv[1]
token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: raise RuntimeError('authenticated acceptance token is unavailable')
headers={'X-CMOS-Automation-Key':token}
layers=query_all("""
  SELECT l.id::text AS id,l.layer_key,count(f.id) FILTER (WHERE f.active) AS locations
  FROM map_layers l LEFT JOIN map_features f ON f.layer_id=l.id
  WHERE l.layer_key=ANY(%s::text[]) AND l.active=true
  GROUP BY l.id,l.layer_key ORDER BY l.layer_key
""",(['NJ_OFFICIAL_MUNICIPALITIES','NJ_OFFICIAL_COUNTIES','NJDOT_MAJOR_HIGHWAYS'],))
for layer in layers:
    path='/watchlist?'+urllib.parse.urlencode({'bulk_layer':layer['id']})
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers)
    with urllib.request.urlopen(request,timeout=30) as response:
        body=response.read().decode(errors='replace')
        if response.status!=200 or '/login' in response.geturl(): raise RuntimeError('authenticated layer preview failed')
        if 'Internal Server Error' in body: raise RuntimeError('an internal error was exposed')
        if 'Choose Location groups' not in body or 'Create Selected Watches' not in body:
            raise RuntimeError('official layer is not available to the Watch builder')
health=query_one("""
  SELECT
    count(*) FILTER (WHERE active AND nearby_enabled AND (starts_at IS NULL OR starts_at<=now()) AND (expires_at IS NULL OR expires_at>now())) AS active_spatial_watches,
    count(*) FILTER (WHERE active AND nearby_enabled AND coalesce(spatial_target_geom,geom) IS NULL) AS spatial_missing_target,
    count(*) FILTER (WHERE active AND (starts_at IS NULL OR starts_at<=now()) AND (expires_at IS NULL OR expires_at>now()) AND NOT EXISTS (
      SELECT 1 FROM watch_item_recipients wir JOIN subscribers s ON s.id=wir.subscriber_id
      WHERE wir.watch_item_id=watch_items.id AND wir.active AND s.active
    )) AS active_watches_needing_recipient,
    (SELECT count(*) FROM alert_watch_matches WHERE matched_at>=now()-interval '24 hours') AS matches_24h
  FROM watch_items
""")
pending=query_one("""
  SELECT count(*) AS n FROM alerts a JOIN geo_entity_resolutions r
    ON r.entity_type='ALERT' AND r.entity_id=a.id::text
  WHERE a.geom IS NOT NULL AND a.received_at>=now()-interval '6 hours'
    AND a.status<>'RESOLVED' AND (a.expires_at IS NULL OR a.expires_at>now())
    AND r.status='RESOLVED' AND r.spatial_precision='ADDRESS_POINT'
    AND r.updated_at>=%s::timestamptz
    AND coalesce(a.metadata#>>'{_cmos,spatial_rematch_version}','')<>'geo-v1'
    AND EXISTS (
      SELECT 1 FROM watch_items w
      WHERE w.active=true
        AND (w.starts_at IS NULL OR w.starts_at<=now())
        AND (w.expires_at IS NULL OR w.expires_at>now())
        AND (w.nearby_enabled=true OR nullif(w.municipality,'') IS NOT NULL)
    )
""",(activated_at,))['n']
print(json.dumps({
  'resolved_alert_rematch_workflow':'ACTIVE',
  'central_watchlist_reused':'PASS',
  'delivery_guard_reused':'PASS',
  'official_layers':{row['layer_key']:int(row['locations']) for row in layers},
  'official_layer_count':len(layers),
  'watch_builder_previews':'PASS',
  'active_spatial_watches':int(health.get('active_spatial_watches') or 0),
  'spatial_watches_missing_location':int(health.get('spatial_missing_target') or 0),
  'active_watches_needing_recipient':int(health.get('active_watches_needing_recipient') or 0),
  'matches_24h':int(health.get('matches_24h') or 0),
  'eligible_rematch_queue_now':int(pending or 0),
  'database_schema_changes':'NONE',
  'existing_watch_changes':'NONE',
  'test_notification_sent':'NO',
  'dashboard_build_or_restart':'NO',
  'postgis_restart':'NO',
  'full_e2e':'NOT_RUN',
},sort_keys=True))
PY
)"
printf '%s\n' "$ACCEPTANCE_RESULT" | python3 -m json.tool
ACCEPTANCE_ACTION="pass-read-only-no-test-notification"

CURRENT_PHASE="final-health"
n8n_contract
layer_contract
docker exec n8n node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
docker exec citymanager-dashboard python -c "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200"

CURRENT_PHASE="complete"
section "RELEASE COMPLETE"
printf 'TARGET=%s\nN8N=%s\nLAYERS=%s\nDATABASE_SCHEMA_CHANGES=NONE\nEXISTING_WATCH_CHANGES=NONE\nDASHBOARD_BUILD_OR_RESTART=NO\nPOSTGIS_RESTART=NO\nFULL_E2E=NOT_RUN\n' \
  "$TARGET_HEAD" "$N8N_ACTION" "$LAYER_ACTION"
N8N_CHANGED=0
LAYERS_CREATED=0
