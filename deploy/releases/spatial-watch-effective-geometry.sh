#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C GIT_TERMINAL_PROMPT=0
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED_BASE="5d2f826f688ecc704044e54cb3650d9aa6934f42"
REPLAY_ALERT_ID="ORU:eb5707b3"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
LOG_FILE="$LOG_DIR/spatial-watch-effective-geometry-$RUN_ID.log"
LOCK_FILE="/var/lock/cmos-spatial-watch-effective-geometry.lock"
BACKUP_DIR="/var/backups/city-manager-os/spatial-watch-effective-geometry-$RUN_ID"
WORKFLOW_NAME="CORE - Resolved Alert Spatial Rematch v1"
WORKFLOW_FILE="workflows/core/CORE_Resolved_Spatial_Rematch_v1.json"
TMP_WORKFLOW="/tmp/cmos-spatial-rematch-$RUN_ID.json"
CURRENT_PHASE="startup"
FINAL_STATUS="FAIL"
FAIL_LINE="none"
TARGET_HEAD="unknown"
ORIGINAL_HEAD="unknown"
ARCHIVE_BRANCH="none"
REPOSITORY_ACTION="not-started"
DASHBOARD_ACTION="not-started"
DATABASE_ACTION="not-started"
N8N_ACTION="not-started"
ACCEPTANCE_RESULT='{}'
DASHBOARD_PREPARED=0
DASHBOARD_CHANGED=0
DASHBOARD_ROLLBACK_TAG=""
DATABASE_CHANGED=0
N8N_CHANGED=0
N8N_DB=""
N8N_OWNER=""
BACKFILL_IDS=""

EXPECTED_PATHS=$'dashboard/map_app.py\ndashboard/spatial_watch_app.py\ndashboard/templates/watchlist.html\ndashboard/tests/test_spatial_rematch_layers.py\ndashboard/tests/test_spatial_watch_pack.py\ndashboard/tests/test_watchlist_reliability.py\ndeploy/gis/install_spatial_watch_pack.sh\ndeploy/postgis/init/032_unified_spatial_watch_pack.sql\ndeploy/releases/spatial-watch-effective-geometry.sh\nworkflows/core/CORE_Resolved_Spatial_Rematch_v1.json'

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

wait_dashboard(){
  for _ in $(seq 1 30); do
    if docker exec citymanager-dashboard python -c \
      "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200" \
      >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

wait_n8n(){
  for _ in $(seq 1 30); do
    if docker exec n8n node -e \
      "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
      >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

publish_workflow(){
  local workflow_id="$1"
  if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
    docker exec -u node n8n n8n publish:workflow --id="$workflow_id" >/dev/null
  else
    docker exec -u node n8n n8n update:workflow --id="$workflow_id" --active=true >/dev/null
  fi
}

rollback_dashboard(){
  [[ "$DASHBOARD_PREPARED" == 1 && -n "$DASHBOARD_ROLLBACK_TAG" ]] || return 0
  docker image inspect "$DASHBOARD_ROLLBACK_TAG" >/dev/null 2>&1 || return 1
  docker image tag "$DASHBOARD_ROLLBACK_TAG" dashboard-citymanager-dashboard:latest
  if [[ "$DASHBOARD_CHANGED" == 1 ]]; then
    log "ROLLBACK: restoring prior dashboard image"
    docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate citymanager-dashboard </dev/null
    wait_dashboard || return 1
  fi
  DASHBOARD_ACTION="rolled-back"
}

rollback_n8n(){
  [[ "$N8N_CHANGED" == 1 && -s "$BACKUP_DIR/n8n-database.sqlite" ]] || return 0
  log "ROLLBACK: restoring prior n8n database"
  docker stop n8n >/dev/null
  install -m 600 "$BACKUP_DIR/n8n-database.sqlite" "$N8N_DB"
  [[ -z "$N8N_OWNER" ]] || chown "$N8N_OWNER" "$N8N_DB"
  docker start n8n >/dev/null
  wait_n8n || return 1
  N8N_ACTION="rolled-back"
}

rollback_database(){
  [[ "$DATABASE_CHANGED" == 1 ]] || return 0
  log "ROLLBACK: restoring prior spatial functions and Watch filters"
  if [[ -n "$BACKFILL_IDS" ]]; then
    docker exec -i citymanager-postgis sh -lc \
      'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
DELETE FROM alert_watch_matches
WHERE id=ANY(string_to_array('$BACKFILL_IDS',',')::uuid[]);
SQL
  fi
  docker exec -i citymanager-postgis sh -lc \
    'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$BACKUP_DIR/prior-spatial-functions.sql"
  docker exec -i citymanager-postgis sh -lc \
    'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$BACKUP_DIR/prior-watch-filters.sql"
  DATABASE_ACTION="rolled-back"
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
    rollback_n8n || log "WARNING: n8n rollback was not confirmed"
    rollback_database || log "WARNING: database rollback was not confirmed"
    restore_checkout || log "WARNING: repository restore was not confirmed"
  fi
  docker exec n8n rm -f "$TMP_WORKFLOW" >/dev/null 2>&1 || true
  rm -f "$TMP_WORKFLOW"
  (( rc == 0 )) && FINAL_STATUS="PASS"
  section "SPATIAL WATCH EFFECTIVE GEOMETRY: $FINAL_STATUS"
  printf 'STATUS=%s\nPHASE=%s\nTARGET=%s\nDASHBOARD=%s\nDATABASE=%s\nN8N=%s\nFULL_E2E=NOT_RUN\nPRIVATE_LOG=%s\n' \
    "$FINAL_STATUS" "$CURRENT_PHASE" "$TARGET_HEAD" "$DASHBOARD_ACTION" "$DATABASE_ACTION" "$N8N_ACTION" "$LOG_FILE"
  [[ "$ACCEPTANCE_RESULT" == '{}' ]] || printf '%s\n' "$ACCEPTANCE_RESULT" | python3 -m json.tool || true
  exit "$rc"
}

trap on_error ERR
trap on_exit EXIT

for command in git docker python3 flock install tee; do
  command -v "$command" >/dev/null || fail "$command is required"
done
mkdir -p "$LOG_DIR" "$BACKUP_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another spatial Watch release is already running"

CURRENT_PHASE="preflight"
section "SPATIAL WATCH EFFECTIVE GEOMETRY"
cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "production repository must be clean"
git fetch -q origin "+refs/heads/main:refs/remotes/origin/main"
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
  ARCHIVE_BRANCH="production-archive/spatial-watch-geometry-$RUN_ID"
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

PLAN="$(python3 deploy/cmos-deploy plan --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null)"
printf '%s\n' "$PLAN"
grep -q '^changed_count=10$' <<<"$PLAN"
grep -q '^build=yes$' <<<"$PLAN"
grep -q '^services=citymanager-dashboard$' <<<"$PLAN"
grep -q '^backup_required=yes$' <<<"$PLAN"
grep -q '^external=n8n-workflow-publish,postgis-migration$' <<<"$PLAN"
grep -q '^unknown=none$' <<<"$PLAN"
log "PREFLIGHT PASS: one dashboard recreate, one function migration, one existing n8n workflow publish"
log "Full E2E is intentionally replaced by focused spatial contracts and all-Watch production acceptance"

N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
[[ -n "$N8N_DIR" && "$N8N_DIR" == /* && "$N8N_DIR" != / ]] || fail "n8n data mount is missing or unsafe"
N8N_DB="$N8N_DIR/database.sqlite"
[[ -f "$N8N_DB" ]] || fail "n8n database was not found"
N8N_OWNER="$(stat -c '%u:%g' "$N8N_DB")"

CURRENT_PHASE="focused-verification"
git diff --check "$EXPECTED_BASE...$TARGET_HEAD"
python3 -m py_compile dashboard/map_app.py dashboard/spatial_watch_app.py \
  dashboard/tests/test_spatial_watch_pack.py dashboard/tests/test_spatial_rematch_layers.py \
  dashboard/tests/test_watchlist_reliability.py
python3 -m json.tool "$WORKFLOW_FILE" >/dev/null
bash -n deploy/gis/install_spatial_watch_pack.sh
bash -n deploy/releases/spatial-watch-effective-geometry.sh

OLD_IMAGE="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
DASHBOARD_ROLLBACK_TAG="dashboard-citymanager-dashboard:cmos-spatial-geometry-rollback-$RUN_ID"
docker image tag "$OLD_IMAGE" "$DASHBOARD_ROLLBACK_TAG"
DASHBOARD_PREPARED=1
docker compose -f "$REPO/dashboard/docker-compose.yml" build citymanager-dashboard </dev/null
DASHBOARD_ACTION="built-and-focused-tested"
docker compose -f "$REPO/dashboard/docker-compose.yml" run --rm --no-deps -T \
  -v "$REPO:/repo:ro" -w /repo/dashboard -e PYTHONPATH=/repo/dashboard:/app \
  --entrypoint pytest citymanager-dashboard -p no:cacheprovider -q \
  tests/test_attention_engine.py tests/test_gis_import.py tests/test_spatial_watch_pack.py \
  tests/test_spatial_rematch_layers.py tests/test_watchlist_reliability.py
docker compose -f "$REPO/dashboard/docker-compose.yml" run --rm --no-deps -T \
  -v "$REPO/dashboard:/src:ro" --entrypoint python citymanager-dashboard - <<'PY'
from jinja2 import Environment,FileSystemLoader
Environment(loader=FileSystemLoader('/src/templates')).get_template('watchlist.html')
print('JINJA_TARGETED_VALIDATION=PASS')
PY

CURRENT_PHASE="backups"
python3 - "$N8N_DB" "$BACKUP_DIR/n8n-database.sqlite" <<'PY'
import sqlite3,sys
source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2])
source.backup(target); target.close(); source.close()
PY
docker exec -i citymanager-postgis sh -lc \
  'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > "$BACKUP_DIR/prior-spatial-functions.sql" <<'SQL'
SELECT pg_get_functiondef('gis_active_spatial_watch_matches(text,geometry)'::regprocedure) || E';\n';
SELECT pg_get_functiondef('gis_spatial_history(geometry,double precision,interval,text,text,integer)'::regprocedure) || E';\n';
SQL
docker exec -i citymanager-postgis sh -lc \
  'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > "$BACKUP_DIR/prior-watch-filters.sql" <<'SQL'
SELECT format(
  'UPDATE watch_items SET source_filter=%L::text[],alert_category_filter=%L::text[],updated_at=%L::timestamptz WHERE id=%L::uuid;',
  source_filter::text,alert_category_filter::text,updated_at::text,id::text
)
FROM watch_items
WHERE nearby_enabled
  AND upper(coalesce(watch_type,''))<>'LOCATION_TOPIC'
  AND (cardinality(source_filter)>0 OR cardinality(alert_category_filter)>0);
SQL
chmod 600 "$BACKUP_DIR"/*
[[ -s "$BACKUP_DIR/n8n-database.sqlite" && -s "$BACKUP_DIR/prior-spatial-functions.sql" ]] \
  || fail "release backups are incomplete"

CURRENT_PHASE="database-install"
DATABASE_CHANGED=1
deploy/gis/install_spatial_watch_pack.sh "$TARGET_HEAD"
DATABASE_ACTION="effective-geometry-functions-installed-and-legacy-filters-repaired"

CURRENT_PHASE="n8n-publish"
python3 - "$N8N_DB" "$WORKFLOW_FILE" "$TMP_WORKFLOW" "$WORKFLOW_NAME" "$STARTED_UTC" <<'PY'
import json,sqlite3,sys,uuid
db,source_path,target_path,workflow_name,activated_at=sys.argv[1:]
con=sqlite3.connect(db); con.row_factory=sqlite3.Row
matcher=con.execute(
    "SELECT id,nodes FROM workflow_entity WHERE name='CORE - Watchlist Matcher v1' ORDER BY active DESC,updatedAt DESC LIMIT 1"
).fetchone()
if not matcher: raise SystemExit('central Watchlist Matcher was not found')
postgres=None
for node in json.loads(matcher['nodes'] or '[]'):
    value=(node.get('credentials') or {}).get('postgres')
    if value: postgres=value; break
if not postgres: raise SystemExit('central matcher Postgres credential was not found')
rows=con.execute('SELECT id FROM workflow_entity WHERE name=? ORDER BY updatedAt DESC',(workflow_name,)).fetchall()
if len(rows)!=1: raise SystemExit('expected one existing resolved spatial rematch workflow')
workflow=json.load(open(source_path))
workflow['id']=rows[0]['id']
workflow['active']=False
workflow['versionId']=str(uuid.uuid4())
replaced=0
for node in workflow.get('nodes') or []:
    if node.get('type')=='n8n-nodes-base.postgres': node['credentials']={'postgres':postgres}
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
docker exec -u node n8n n8n import:workflow --input="$TMP_WORKFLOW" >/dev/null
REMATCH_ID="$(python3 - "$N8N_DB" "$WORKFLOW_NAME" <<'PY'
import sqlite3,sys
con=sqlite3.connect(sys.argv[1]); rows=con.execute('SELECT id FROM workflow_entity WHERE name=?',(sys.argv[2],)).fetchall()
if len(rows)!=1: raise SystemExit(1)
print(rows[0][0]); con.close()
PY
)"
publish_workflow "$REMATCH_ID"
docker restart n8n >/dev/null
wait_n8n || fail "n8n did not become ready"
python3 - "$N8N_DB" "$WORKFLOW_NAME" <<'PY'
import json,sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
row=con.execute('SELECT active,activeVersionId,nodes FROM workflow_entity WHERE name=?',(sys.argv[2],)).fetchone()
if not row or not row['active'] or not row['activeVersionId']: raise SystemExit('rematch workflow is not published')
nodes={node.get('name'):node for node in json.loads(row['nodes'] or '[]')}
load=nodes['Load Newly Resolved Alerts']['parameters']['query']
mark=nodes['Mark Resolved Alert Rematched']['parameters']['query']
if '__CMOS_ACTIVATED_AT__' in load: raise SystemExit('activation marker was not replaced')
if 'coalesce(a.geom,r.geom) IS NOT NULL' not in load: raise SystemExit('effective geometry query missing')
if "spatial_rematch_version','geo-v2'" not in mark: raise SystemExit('geo-v2 marker missing')
if "r.spatial_precision='ADDRESS_POINT'" in load or 'r.confidence>=0.85' in load:
    raise SystemExit('old precise-only restriction remains')
print('N8N_REMATCH_CONTRACT=PASS')
con.close()
PY
N8N_ACTION="published-existing-rematch-workflow-one-restart"

CURRENT_PHASE="dashboard-deploy"
DASHBOARD_CHANGED=1
docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate citymanager-dashboard </dev/null
wait_dashboard || fail "dashboard did not become ready"
docker exec -i citymanager-dashboard python - <<'PY'
from spatial_watch_app import GEOMETRY_RELEASE_ID
assert GEOMETRY_RELEASE_ID=='spatial-watch-effective-geometry-v1'
print('DASHBOARD_GEOMETRY_CONTRACT=PASS')
PY
DASHBOARD_ACTION="deployed-one-build-one-recreate"

CURRENT_PHASE="all-watch-read-only-acceptance"
ACCEPTANCE_RESULT="$(docker exec -i citymanager-dashboard python - "$REPLAY_ALERT_ID" <<'PY'
import json,sys
from app import query_one
from spatial_watch_app import _watch_health
alert_id=sys.argv[1]
result=query_one("""
WITH effective_alerts AS (
  SELECT a.id,a.alert_id,a.source,a.category,a.priority,coalesce(a.geom,r.geom) AS geom
  FROM alerts a
  LEFT JOIN geo_entity_resolutions r
    ON r.entity_type='ALERT' AND r.entity_id=a.id::text AND r.status='RESOLVED'
  WHERE a.received_at>=now()-interval '24 hours' AND coalesce(a.geom,r.geom) IS NOT NULL
), expected AS (
  SELECT a.alert_id,w.id AS watch_item_id
  FROM effective_alerts a
  JOIN watch_items w
    ON w.active AND w.nearby_enabled AND w.spatial_geom IS NOT NULL
   AND (w.starts_at IS NULL OR w.starts_at<=now())
   AND (w.expires_at IS NULL OR w.expires_at>now())
   AND a.priority>=w.min_priority
   AND (cardinality(w.source_filter)=0 OR EXISTS (
     SELECT 1 FROM unnest(w.source_filter) v WHERE upper(btrim(v))=upper(btrim(a.source))
   ))
   AND (cardinality(w.alert_category_filter)=0 OR EXISTS (
     SELECT 1 FROM unnest(w.alert_category_filter) v WHERE upper(btrim(v))=upper(btrim(a.category))
   ))
   AND CASE upper(coalesce(w.spatial_scope,'RADIUS'))
     WHEN 'RADIUS' THEN ST_DWithin(
       a.geom::geography,coalesce(w.spatial_target_geom,w.geom)::geography,w.radius_ft*0.3048
     )
     ELSE ST_Intersects(a.geom,w.spatial_geom)
   END
), missing AS (
  SELECT e.* FROM expected e
  WHERE NOT EXISTS (
    SELECT 1 FROM gis_active_spatial_watch_matches(e.alert_id) m
    WHERE m.watch_item_id=e.watch_item_id
  )
)
SELECT
  (SELECT count(*) FROM effective_alerts) AS mapped_alerts_24h,
  (SELECT count(*) FROM watch_items w WHERE w.active AND w.nearby_enabled
    AND (w.starts_at IS NULL OR w.starts_at<=now()) AND (w.expires_at IS NULL OR w.expires_at>now())) AS active_spatial_watches,
  (SELECT count(*) FROM expected) AS expected_spatial_matches,
  (SELECT count(*) FROM missing) AS missing_spatial_matches,
  (SELECT count(*) FROM watch_items w
    WHERE w.nearby_enabled AND upper(coalesce(w.watch_type,''))<>'LOCATION_TOPIC'
      AND (
        EXISTS (SELECT 1 FROM unnest(w.source_filter) v WHERE NOT EXISTS (
          SELECT 1 FROM alerts a WHERE upper(btrim(a.source))=upper(btrim(v))
        ))
        OR EXISTS (SELECT 1 FROM unnest(w.alert_category_filter) v WHERE NOT EXISTS (
          SELECT 1 FROM alerts a WHERE upper(btrim(a.category))=upper(btrim(v))
        ))
      )) AS location_watches_with_invalid_filters,
  (SELECT count(*) FROM gis_active_spatial_watch_matches(alert_id)
    WHERE match_reason LIKE '%resolver point%') AS replay_resolver_matches,
  (SELECT count(*) FROM gis_active_spatial_watch_matches(alert_id) m
    JOIN watch_items w ON w.id=m.watch_item_id
    WHERE w.watch_id='W_VALLEY_HOSPITAL_012431') AS valley_match
FROM (SELECT %s::text AS alert_id) requested
""",(alert_id,))
health=_watch_health()
payload={
  'mapped_alerts_24h':int(result['mapped_alerts_24h']),
  'active_spatial_watches':int(result['active_spatial_watches']),
  'expected_spatial_matches':int(result['expected_spatial_matches']),
  'missing_spatial_matches':int(result['missing_spatial_matches']),
  'location_watches_with_invalid_filters':int(result['location_watches_with_invalid_filters']),
  'replay_resolver_matches':int(result['replay_resolver_matches']),
  'valley_match':int(result['valley_match']),
  'watch_health':health.get('status'),
  'spatial_matcher_ready':bool(health.get('spatial_matcher_ready')),
  'retrospective_notification_sent':'NO',
  'full_e2e':'NOT_RUN',
}
if payload['missing_spatial_matches']!=0: raise RuntimeError(payload)
if payload['location_watches_with_invalid_filters']!=0: raise RuntimeError(payload)
if payload['replay_resolver_matches']<1 or payload['valley_match']!=1: raise RuntimeError(payload)
if payload['watch_health']!='PASS' or not payload['spatial_matcher_ready']: raise RuntimeError(payload)
print(json.dumps(payload,sort_keys=True))
PY
)"
printf '%s\n' "$ACCEPTANCE_RESULT" | python3 -m json.tool

CURRENT_PHASE="bounded-spatial-match-backfill"
BACKFILL_IDS="$(docker exec -i citymanager-postgis sh -lc \
  'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
WITH recent_mapped AS (
  SELECT a.id,a.alert_id
  FROM alerts a
  LEFT JOIN geo_entity_resolutions r
    ON r.entity_type='ALERT' AND r.entity_id=a.id::text AND r.status='RESOLVED'
  WHERE a.received_at>=now()-interval '24 hours'
    AND a.status<>'RESOLVED'
    AND (a.expires_at IS NULL OR a.expires_at>now())
    AND coalesce(a.geom,r.geom) IS NOT NULL
), inserted AS (
  INSERT INTO alert_watch_matches(alert_id,watch_item_id,match_type,match_reason,matched_at)
  SELECT a.id,m.watch_item_id,m.match_type,m.match_reason,now()
  FROM recent_mapped a
  CROSS JOIN LATERAL gis_active_spatial_watch_matches(a.alert_id) m
  ON CONFLICT(alert_id,watch_item_id,match_type) DO NOTHING
  RETURNING id
)
SELECT coalesce(string_agg(id::text,','),'') FROM inserted;
SQL
)"
if [[ -n "$BACKFILL_IDS" ]]; then
  BACKFILLED_MATCHES="$(( $(tr -cd ',' <<<"$BACKFILL_IDS" | wc -c) + 1 ))"
else
  BACKFILLED_MATCHES=0
fi
PERSISTED_MATCHES="$(docker exec -i citymanager-postgis sh -lc \
  'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
SELECT count(*)
FROM alert_watch_matches awm
JOIN alerts a ON a.id=awm.alert_id
JOIN watch_items w ON w.id=awm.watch_item_id
WHERE a.alert_id='$REPLAY_ALERT_ID'
  AND w.watch_id='W_VALLEY_HOSPITAL_012431'
  AND awm.match_type='PROXIMITY';
SQL
)"
[[ "$PERSISTED_MATCHES" == 1 ]] || fail "the targeted Valley Hospital Match was not persisted"
MISSING_PERSISTED="$(docker exec -i citymanager-postgis sh -lc \
  'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
WITH recent_mapped AS (
  SELECT a.id,a.alert_id
  FROM alerts a
  LEFT JOIN geo_entity_resolutions r
    ON r.entity_type='ALERT' AND r.entity_id=a.id::text AND r.status='RESOLVED'
  WHERE a.received_at>=now()-interval '24 hours'
    AND a.status<>'RESOLVED'
    AND (a.expires_at IS NULL OR a.expires_at>now())
    AND coalesce(a.geom,r.geom) IS NOT NULL
), expected AS (
  SELECT a.id AS alert_id,m.watch_item_id,m.match_type
  FROM recent_mapped a
  CROSS JOIN LATERAL gis_active_spatial_watch_matches(a.alert_id) m
)
SELECT count(*)
FROM expected e
WHERE NOT EXISTS (
  SELECT 1 FROM alert_watch_matches awm
  WHERE awm.alert_id=e.alert_id AND awm.watch_item_id=e.watch_item_id AND awm.match_type=e.match_type
);
SQL
)"
[[ "$MISSING_PERSISTED" == 0 ]] || fail "one or more recent spatial Matches were not backfilled"

CURRENT_PHASE="final-health"
wait_dashboard || fail "dashboard final health failed"
wait_n8n || fail "n8n final health failed"
docker exec -i citymanager-dashboard python - <<'PY'
from spatial_watch_app import _watch_health
h=_watch_health()
assert h.get('status')=='PASS',h
assert h.get('spatial_matcher_ready') is True,h
print('FINAL_WATCH_HEALTH=PASS')
PY

CURRENT_PHASE="complete"
N8N_CHANGED=0
DATABASE_CHANGED=0
section "RELEASE COMPLETE"
printf 'TARGET=%s\nBACKFILLED_SPATIAL_MATCHES=%s\nORU_VALLEY_MATCH=PERSISTED\nRETROSPECTIVE_NOTIFICATION_SENT=NO\nFULL_E2E=NOT_RUN\n' \
  "$TARGET_HEAD" "$BACKFILLED_MATCHES"
