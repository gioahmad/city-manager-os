#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
EXPECTED_TARGET="${1:-}"
WORKFLOW_FILE="$REPO/workflows/sources/PSEG_Unified_Spatial_Statewide_v2.json"
BACKUP_ROOT="/var/backups/city-manager-os/pseg-workflows"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"
TMP_TARGET="/tmp/PSEG_Unified_Spatial_Statewide_${STAMP}.json"
MANIFEST="$BACKUP_DIR/workflows.tsv"
PUBLISHED=0
DB_OWNER=""

log(){ printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }

wait_ready(){
  for _ in $(seq 1 60); do
    if docker exec n8n node -e \
      "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
      >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

restore_n8n(){
  local rc=$?
  trap - ERR
  if (( PUBLISHED == 1 )) && [[ -s "$BACKUP_DIR/database.sqlite" ]]; then
    log "ROLLBACK: restoring the pre-#60 n8n database"
    docker stop n8n >/dev/null 2>&1 || true
    install -m 600 "$BACKUP_DIR/database.sqlite" "$N8N_DB" || true
    [[ -n "$DB_OWNER" ]] && chown "$DB_OWNER" "$N8N_DB" >/dev/null 2>&1 || true
    docker start n8n >/dev/null 2>&1 || true
    wait_ready >/dev/null 2>&1 || true
  fi
  docker exec n8n rm -f "$TMP_TARGET" >/dev/null 2>&1 || true
  rm -f "$TMP_TARGET"
  log "#60 PSEG WORKFLOW INSTALL FAILED rc=${rc} line=${LINENO}"
  exit "$rc"
}
trap restore_n8n ERR

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
[[ -z "$EXPECTED_TARGET" || "$(git rev-parse HEAD)" == "$EXPECTED_TARGET" ]] \
  || fail "HEAD does not match expected target"
[[ -s "$WORKFLOW_FILE" ]] || fail "unified PSEG workflow definition is missing"
for container in n8n citymanager-postgis; do
  [[ "$(docker inspect "$container" --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
    || fail "required container is not running: $container"
done

N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
[[ -n "$N8N_DIR" && "$N8N_DIR" == /* && "$N8N_DIR" != / ]] \
  || fail "n8n data mount is missing or unsafe"
N8N_DB="$N8N_DIR/database.sqlite"
[[ -f "$N8N_DB" ]] || fail "n8n database was not found"
DB_OWNER="$(stat -c '%u:%g' "$N8N_DB")"
install -d -m 700 "$BACKUP_DIR"

log "Capturing every live PSEG workflow before consolidation"
python3 - "$N8N_DB" "$BACKUP_DIR/database.sqlite" "$MANIFEST" <<'PY'
import re,sqlite3,sys
source=sqlite3.connect(sys.argv[1]); source.row_factory=sqlite3.Row
target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close()
rows=source.execute("""
  SELECT id,name,active FROM workflow_entity
  WHERE upper(name) LIKE '%PSEG%' OR upper(name) LIKE '%PSE&G%'
  ORDER BY name,id
""").fetchall()
if not rows: raise SystemExit('no existing PSEG workflow was found')
with open(sys.argv[3],'w') as handle:
    for row in rows:
        workflow_id=str(row['id'])
        if not re.fullmatch(r'[A-Za-z0-9_-]+',workflow_id):
            raise SystemExit('unsafe workflow ID in n8n database')
        name=str(row['name']).replace('\t',' ').replace('\n',' ')
        handle.write(f"{workflow_id}\t{1 if row['active'] else 0}\t{name}\n")
source.close()
PY
chmod 600 "$BACKUP_DIR/database.sqlite" "$MANIFEST"
while IFS=$'\t' read -r workflow_id active workflow_name; do
  export_path="/tmp/pseg-pre60-${workflow_id}-${STAMP}.json"
  docker exec -u node n8n n8n export:workflow --id="$workflow_id" --output="$export_path" >/dev/null
  docker cp "n8n:$export_path" "$BACKUP_DIR/${workflow_id}.json"
  docker exec n8n rm -f "$export_path" >/dev/null 2>&1 || true
  chmod 600 "$BACKUP_DIR/${workflow_id}.json"
  log "BACKUP PSEG workflow id=${workflow_id} active=${active} name=${workflow_name}"
done < "$MANIFEST"

log "Preparing one central PSEG router with existing production credentials"
python3 - "$N8N_DB" "$WORKFLOW_FILE" "$TMP_TARGET" <<'PY'
import json,sqlite3,sys,uuid
db,source_path,target_path=sys.argv[1:]
con=sqlite3.connect(db); con.row_factory=sqlite3.Row
rows=con.execute("""
  SELECT id,name,nodes FROM workflow_entity
  WHERE upper(name) LIKE '%PSEG%' OR upper(name) LIKE '%PSE&G%'
  ORDER BY CASE WHEN name='PSEG Hudson - City Manager OS v1' THEN 0 ELSE 1 END,
           active DESC,updatedAt DESC
""").fetchall()
if not rows: raise SystemExit('existing PSEG workflow not found')
primary=rows[0]
matcher=con.execute("SELECT id FROM workflow_entity WHERE name='CORE - Watchlist Matcher v1' ORDER BY active DESC LIMIT 1").fetchone()
if not matcher: raise SystemExit('central Watchlist Matcher not found')
postgres=None
for row in rows:
    for node in json.loads(row['nodes'] or '[]'):
        value=(node.get('credentials') or {}).get('postgres')
        if value:
            postgres=value; break
    if postgres: break
if not postgres:
    candidates=con.execute("SELECT id,name FROM credentials_entity WHERE lower(type) LIKE '%postgres%' ORDER BY name,id").fetchall()
    if len(candidates)!=1:
        raise SystemExit('could not resolve one production Postgres credential')
    postgres={'id':candidates[0]['id'],'name':candidates[0]['name']}
workflow=json.load(open(source_path))
workflow['id']=primary['id']
workflow['active']=False
workflow['versionId']=str(uuid.uuid4())
for node in workflow.get('nodes') or []:
    if node.get('type')=='n8n-nodes-base.postgres':
        node['credentials']={'postgres':postgres}
    if node.get('name')=='Send to Central Watchlist Matcher':
        ref=node['parameters']['workflowId']
        ref['value']=matcher['id']
        ref['cachedResultUrl']=f"/workflow/{matcher['id']}"
json.dump(workflow,open(target_path,'w'),indent=2)
print(f"TARGET_ID={primary['id']}")
print(f"MATCHER_ID={matcher['id']}")
con.close()
PY
python3 -m json.tool "$TMP_TARGET" >/dev/null

docker cp "$TMP_TARGET" "n8n:$TMP_TARGET"
docker exec -u root n8n chown node:node "$TMP_TARGET"
docker exec -u root n8n chmod 600 "$TMP_TARGET"
docker exec -u node n8n n8n import:workflow --input="$TMP_TARGET" >/dev/null
PUBLISHED=1
TARGET_ID="$(python3 - "$TMP_TARGET" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['id'])
PY
)"
if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
  docker exec -u node n8n n8n publish:workflow --id="$TARGET_ID" >/dev/null
else
  docker exec -u node n8n n8n update:workflow --id="$TARGET_ID" --active=true >/dev/null
fi

while IFS=$'\t' read -r workflow_id active workflow_name; do
  [[ "$workflow_id" == "$TARGET_ID" ]] && continue
  docker exec -u node n8n n8n update:workflow --id="$workflow_id" --active=false >/dev/null
  log "CONSOLIDATED legacy workflow id=${workflow_id} name=${workflow_name}"
done < "$MANIFEST"

docker restart n8n >/dev/null
wait_ready || fail "n8n did not become ready"

python3 - "$N8N_DB" "$TARGET_ID" <<'PY'
import json,sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
row=con.execute('SELECT name,active,activeVersionId,nodes FROM workflow_entity WHERE id=?',(sys.argv[2],)).fetchone()
if not row or not row['active'] or not row['activeVersionId']:
    raise SystemExit('unified PSEG workflow is not active and published')
nodes={node.get('name'):node for node in json.loads(row['nodes'] or '[]')}
required={'Load Pending PSEG Alerts','Restore Standard PSEG Alert','Send to Central Watchlist Matcher','Mark PSEG Alert Routed'}
if not required.issubset(nodes): raise SystemExit('unified PSEG workflow contract is incomplete')
load=(nodes['Load Pending PSEG Alerts'].get('parameters') or {}).get('query','')
mark=(nodes['Mark PSEG Alert Routed'].get('parameters') or {}).get('query','')
replacement=((nodes['Mark PSEG Alert Routed'].get('parameters') or {}).get('options') or {}).get('queryReplacement','')
if 'route_pending' not in load or 'route_pending' not in mark:
    raise SystemExit('durable route handoff is missing')
if not nodes['Send to Central Watchlist Matcher'].get('alwaysOutputData'):
    raise SystemExit('matcher must return an acknowledgement item even when no watch matches')
if 'Restore Standard PSEG Alert' not in replacement or "d.p->>'alert_id'" not in mark:
    raise SystemExit('route acknowledgement is not pinned to the original Standard Alert')
if 'ntfy' in json.dumps(list(nodes.values()),sort_keys=True).lower():
    raise SystemExit('direct ntfy reference found in consolidated workflow')
active_legacy=con.execute("""
  SELECT count(*) AS n FROM workflow_entity
  WHERE id<>? AND active=1 AND (upper(name) LIKE '%PSEG%' OR upper(name) LIKE '%PSE&G%')
""",(sys.argv[2],)).fetchone()['n']
if active_legacy: raise SystemExit('a legacy PSEG workflow remains active')
print('PSEG WORKFLOW active=1 published=1 legacy_active=0 direct_ntfy=NO')
con.close()
PY

PUBLISHED=0
trap - ERR
docker exec n8n rm -f "$TMP_TARGET" >/dev/null 2>&1 || true
rm -f "$TMP_TARGET"
log "#60 PSEG WORKFLOW INSTALL: PASS"
log "All prior PSEG definitions retained in mode-600 backup: $BACKUP_DIR"
