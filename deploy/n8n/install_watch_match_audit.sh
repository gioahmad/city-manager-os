#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
MATCHER_ID="ESH9c2pZ8QfkMosO"
MATCHER_FILE="$REPO/workflows/live/CORE_Watchlist_Matcher_live.json"
BACKUP_ROOT="/var/backups/city-manager-os/watch-match-audit"
STAMP="$(date '+%Y%m%d-%H%M%S')"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"
TMP_JSON="/tmp/CORE_Watchlist_Matcher_with_audit_${STAMP}.json"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }

for cmd in git docker python3; do
  command -v "$cmd" >/dev/null 2>&1 || fail "$cmd is required"
done

cd "$REPO"
[[ "$(git branch --show-current)" == "main" ]] || fail "Production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "Repository must be clean before matcher update"

git fetch origin main >/dev/null 2>&1 || fail "Could not fetch origin/main"
git pull --ff-only origin main >/dev/null || fail "Could not fast-forward main"
[[ -s "$MATCHER_FILE" ]] || fail "Matcher export missing: $MATCHER_FILE"

for c in n8n citymanager-postgis; do
  [[ "$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null || true)" == "running" ]] \
    || fail "Required container not running: $c"
done

N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
N8N_DB="$N8N_DIR/database.sqlite"
[[ -f "$N8N_DB" ]] || fail "n8n database not found"
mkdir -p "$BACKUP_DIR"

log "Creating online n8n SQLite backup"
python3 - "$N8N_DB" "$BACKUP_DIR/database.sqlite" <<'PY'
import sqlite3,sys
src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
cp -a "$MATCHER_FILE" "$BACKUP_DIR/CORE_Watchlist_Matcher_live.json"
log "Backup: $BACKUP_DIR"

log "Building updated matcher definition"
python3 - "$MATCHER_FILE" "$TMP_JSON" "$MATCHER_ID" <<'PY'
import json,sys,uuid
src,out,wid=sys.argv[1:4]
data=json.load(open(src))
workflows=data if isinstance(data,list) else [data]
wf=next((x for x in workflows if x.get('id')==wid),None)
if not wf:
    raise SystemExit(f'workflow {wid} not found in export')

nodes=wf.get('nodes') or []
connections=wf.get('connections') or {}
by_name={n.get('name'):n for n in nodes}

match_name='Match + Resolve Recipients'
delivery_name='Send to Central Delivery Guard'
persist_name='Persist Watch Matches'
restore_name='Restore Matcher Output After Audit'

if match_name not in by_name or delivery_name not in by_name:
    raise SystemExit('required matcher nodes not found')

if persist_name not in by_name:
    load=by_name.get('Load Active Watchlist + Recipients') or {}
    creds=load.get('credentials') or {
        'postgres': {'id':'ypC5byxSvG6uOccJ','name':'Postgres account'}
    }
    mx,my=by_name[match_name].get('position',[896,96])
    dx,dy=by_name[delivery_name].get('position',[1120,96])

    persist={
      'parameters': {
        'operation':'executeQuery',
        'query': """WITH input AS (\n  SELECT $1::jsonb AS p\n),\nresolved AS (\n  SELECT\n    a.id AS alert_uuid,\n    (m->>'watch_item_uuid')::uuid AS watch_item_uuid,\n    COALESCE(NULLIF(m->>'match_mode',''),'MATCH') AS match_type,\n    NULLIF(m->>'match_reason','') AS match_reason\n  FROM input i\n  JOIN alerts a\n    ON a.alert_id=i.p->'alert'->>'alert_id'\n  CROSS JOIN LATERAL jsonb_array_elements(\n    COALESCE(i.p->'matches','[]'::jsonb)\n  ) m\n  WHERE NULLIF(m->>'watch_item_uuid','') IS NOT NULL\n),\nupserted AS (\n  INSERT INTO alert_watch_matches(\n    alert_id,watch_item_id,match_type,match_reason,matched_at\n  )\n  SELECT alert_uuid,watch_item_uuid,match_type,match_reason,NOW()\n  FROM resolved\n  ON CONFLICT(alert_id,watch_item_id,match_type) DO UPDATE\n  SET match_reason=EXCLUDED.match_reason,\n      matched_at=NOW()\n  RETURNING id\n)\nSELECT i.p AS matcher_output,\n       (SELECT count(*) FROM upserted) AS persisted_match_count\nFROM input i;""",
        'options': {'queryReplacement':'={{ JSON.stringify($json) }}'}
      },
      'type':'n8n-nodes-base.postgres',
      'typeVersion':2.6,
      'position':[mx+240,my],
      'id':'cmos-core-persist-watch-matches',
      'name':persist_name,
      'alwaysOutputData':True,
      'credentials':creds
    }
    restore={
      'parameters': {
        'jsCode': "return $input.all().map((item) => ({ json: item.json.matcher_output || {} }));"
      },
      'type':'n8n-nodes-base.code',
      'typeVersion':2,
      'position':[mx+480,my],
      'id':'cmos-core-restore-after-match-audit',
      'name':restore_name
    }
    nodes.extend([persist,restore])
    by_name[persist_name]=persist
    by_name[restore_name]=restore

# Rewire only the single central path. Preserve any other outputs if present.
main=(connections.get(match_name) or {}).get('main') or [[]]
while len(main)<1: main.append([])
out0=main[0]
replaced=False
new_out=[]
for edge in out0:
    if edge.get('node')==delivery_name:
        if not replaced:
            new_out.append({'node':persist_name,'type':'main','index':0})
            replaced=True
    else:
        new_out.append(edge)
if not replaced and not any(x.get('node')==persist_name for x in new_out):
    new_out.append({'node':persist_name,'type':'main','index':0})
connections[match_name]={'main':[new_out]}
connections[persist_name]={'main':[[{'node':restore_name,'type':'main','index':0}]]}
connections[restore_name]={'main':[[{'node':delivery_name,'type':'main','index':0}]]}

# Move the existing delivery node to keep the workflow visually readable.
by_name[delivery_name]['position']=[by_name[match_name].get('position',[896,96])[0]+720, by_name[match_name].get('position',[896,96])[1]]

wf['nodes']=nodes
wf['connections']=connections
wf['active']=False
wf['versionId']=str(uuid.uuid4())

json.dump(workflows if isinstance(data,list) else wf,open(out,'w'),indent=2)
print('nodes=',len(nodes))
print('persist_node=',persist_name in {n.get('name') for n in nodes})
PY
python3 -m json.tool "$TMP_JSON" >/dev/null || fail "Generated matcher JSON is invalid"

log "Importing updated matcher through n8n CLI"
docker cp "$TMP_JSON" n8n:/tmp/CORE_Watchlist_Matcher_with_audit.json
docker exec -u node n8n n8n import:workflow --input=/tmp/CORE_Watchlist_Matcher_with_audit.json >/dev/null \
  || fail "n8n workflow import failed"

log "Publishing updated central matcher"
if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
  docker exec -u node n8n n8n publish:workflow --id="$MATCHER_ID" >/dev/null \
    || fail "matcher publish failed"
else
  docker exec -u node n8n n8n update:workflow --id="$MATCHER_ID" --active=true >/dev/null \
    || fail "matcher activation failed"
fi

log "Restarting n8n to load published matcher"
docker restart n8n >/dev/null

log "Waiting for n8n HTTP listener"
ready=0
for i in $(seq 1 60); do
  if docker exec n8n node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 2
done
(( ready == 1 )) || fail "n8n HTTP listener did not become ready"

log "Verifying matcher publication and audit nodes"
python3 - "$N8N_DB" "$MATCHER_ID" <<'PY'
import sqlite3,sys,json
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
r=con.execute('SELECT id,name,active,activeVersionId,nodes,connections FROM workflow_entity WHERE id=?',(sys.argv[2],)).fetchone()
if not r: raise SystemExit('matcher missing')
if not r['active'] or not r['activeVersionId']: raise SystemExit('matcher not active/published')
names={n.get('name') for n in json.loads(r['nodes'])}
for name in ('Persist Watch Matches','Restore Matcher Output After Audit'):
    if name not in names: raise SystemExit(f'missing node: {name}')
c=json.loads(r['connections'])
edge=((c.get('Match + Resolve Recipients') or {}).get('main') or [[]])[0]
if not any(x.get('node')=='Persist Watch Matches' for x in edge):
    raise SystemExit('matcher not rewired through audit persistence')
print('MATCHER active=1 published=1 match_audit=YES')
con.close()
PY

log "Checking match-audit table write permission"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL' >/dev/null
SELECT to_regclass('public.alert_watch_matches');
SQL

log "Exporting published matcher back to repository"
if docker exec -u node n8n n8n export:workflow --help >/dev/null 2>&1; then
  docker exec -u node n8n n8n export:workflow --id="$MATCHER_ID" --output=/tmp/CORE_Watchlist_Matcher_live.json >/dev/null
  docker cp n8n:/tmp/CORE_Watchlist_Matcher_live.json "$MATCHER_FILE"
else
  python3 - "$TMP_JSON" "$MATCHER_FILE" <<'PY'
import json,sys
p=json.load(open(sys.argv[1]))
items=p if isinstance(p,list) else [p]
for x in items: x['active']=True
json.dump(items if isinstance(p,list) else items[0],open(sys.argv[2],'w'),indent=2)
PY
fi
python3 -m json.tool "$MATCHER_FILE" >/dev/null || fail "Repository matcher export became invalid"

git add workflows/live/CORE_Watchlist_Matcher_live.json
if git diff --cached --quiet; then
  log "Matcher export already matched repository"
else
  git commit -m "Persist central watchlist match audit records" >/dev/null
  git push origin main >/dev/null
  log "Updated matcher export committed and pushed: $(git rev-parse HEAD)"
fi

if [[ -x deploy/cmos-health ]]; then
  deploy/cmos-health >/tmp/cmos-watch-match-audit-health.log 2>&1 || {
    cat /tmp/cmos-watch-match-audit-health.log
    fail "cmos-health failed after matcher update"
  }
fi

log "WATCH MATCH AUDIT PERSISTENCE PASSED"
log "Existing central delivery path preserved; alert_watch_matches is now populated by the matcher."
log "Backup retained at: $BACKUP_DIR"
