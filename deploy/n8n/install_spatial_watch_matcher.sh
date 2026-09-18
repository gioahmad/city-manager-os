#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED_TARGET="${1:-}"
MATCHER_ID="ESH9c2pZ8QfkMosO"
MATCHER_FILE="$REPO/workflows/live/CORE_Watchlist_Matcher_live.json"
BACKUP_ROOT="/var/backups/city-manager-os/spatial-watch-matcher"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${CMOS_MATCHER_BACKUP_DIR:-$BACKUP_ROOT/$STAMP}"
TMP_TARGET="/tmp/CORE_Watchlist_Matcher_spatial_${STAMP}.json"
TMP_BACKUP="/tmp/CORE_Watchlist_Matcher_pre56_${STAMP}.json"
TMP_CONTRACT="/tmp/CORE_Watchlist_Matcher_contract_${STAMP}.js"
PUBLISHED=0

log(){ printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }

wait_ready(){
  local ready=0
  for _ in $(seq 1 60); do
    if docker exec n8n node -e \
      "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
      >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 2
  done
  (( ready == 1 )) || return 1
}

publish_matcher(){
  if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
    docker exec -u node n8n n8n publish:workflow --id="$MATCHER_ID" >/dev/null
  else
    docker exec -u node n8n n8n update:workflow --id="$MATCHER_ID" --active=true >/dev/null
  fi
}

prepare_node_file(){
  docker exec -u root n8n chown node:node "$1"
  docker exec -u root n8n chmod 600 "$1"
}

restore_matcher(){
  local rc=$?
  trap - ERR
  if (( PUBLISHED == 1 )) && [[ -s "$BACKUP_DIR/CORE_Watchlist_Matcher_pre56.json" ]]; then
    log "ROLLBACK: restoring the prior central matcher"
    docker cp "$BACKUP_DIR/CORE_Watchlist_Matcher_pre56.json" "n8n:$TMP_BACKUP" >/dev/null 2>&1 || true
    prepare_node_file "$TMP_BACKUP" >/dev/null 2>&1 || true
    docker exec -u node n8n n8n import:workflow --input="$TMP_BACKUP" >/dev/null 2>&1 || true
    publish_matcher >/dev/null 2>&1 || true
    docker restart n8n >/dev/null 2>&1 || true
    wait_ready >/dev/null 2>&1 || true
  fi
  log "#56 MATCHER INSTALL FAILED rc=${rc} line=${LINENO}"
  exit "$rc"
}
trap restore_matcher ERR

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
[[ -z "$EXPECTED_TARGET" || "$(git rev-parse HEAD)" == "$EXPECTED_TARGET" ]] \
  || fail "HEAD does not match expected target"
[[ -s "$MATCHER_FILE" ]] || fail "matcher definition is missing"
for container in n8n citymanager-postgis; do
  [[ "$(docker inspect "$container" --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
    || fail "required container is not running: $container"
done

N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
N8N_DB="$N8N_DIR/database.sqlite"
[[ -f "$N8N_DB" ]] || fail "n8n database was not found"
install -d -m 700 "$BACKUP_DIR"

log "Backing up n8n and the currently published central matcher"
python3 - "$N8N_DB" "$BACKUP_DIR/database.sqlite" <<'PY'
import sqlite3,sys
source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2])
source.backup(target); target.close(); source.close()
PY
docker exec -u node n8n n8n export:workflow --id="$MATCHER_ID" --output="$TMP_BACKUP" >/dev/null
docker cp "n8n:$TMP_BACKUP" "$BACKUP_DIR/CORE_Watchlist_Matcher_pre56.json"
chmod 600 "$BACKUP_DIR/database.sqlite" "$BACKUP_DIR/CORE_Watchlist_Matcher_pre56.json"

log "Preparing and validating the spatial matcher definition"
python3 - "$MATCHER_FILE" "$TMP_TARGET" "$MATCHER_ID" <<'PY'
import json,sys,uuid
source,target,workflow_id=sys.argv[1:]
payload=json.load(open(source))
items=payload if isinstance(payload,list) else [payload]
workflow=next((item for item in items if item.get('id')==workflow_id),None)
if not workflow: raise SystemExit('central matcher ID missing')
nodes={node.get('name'):node for node in workflow.get('nodes') or []}
load=nodes.get('Load Active Watchlist + Recipients') or {}
match=nodes.get('Match + Resolve Recipients') or {}
query=(load.get('parameters') or {}).get('query','')
code=(match.get('parameters') or {}).get('jsCode','')
required_query=('gis_active_spatial_watch_matches','supplied_alert_geom','spatial_match_reason','queryReplacement')
if not all(value in (query + json.dumps(load.get('parameters') or {})) for value in required_query):
    raise SystemExit('spatial loader contract missing')
required_code=('row.spatial_match_type','result.match_type || row.match_mode',
               'locationPlusTopic','LOCATION_TOPIC','municipalityLocationMatch')
if not all(value in code for value in required_code):
    raise SystemExit('spatial deduplication contract missing')
workflow['active']=False
workflow['versionId']=str(uuid.uuid4())
json.dump(items if isinstance(payload,list) else workflow,open(target,'w'),indent=2)
PY
python3 -m json.tool "$TMP_TARGET" >/dev/null

log "Running isolated spatial and Location-plus-topic contracts"
python3 - "$TMP_TARGET" "$TMP_CONTRACT" <<'PY'
import json,sys
payload=json.load(open(sys.argv[1]))
workflow=(payload if isinstance(payload,list) else [payload])[0]
nodes={node.get('name'):node for node in workflow.get('nodes') or []}
code=(nodes['Match + Resolve Recipients'].get('parameters') or {})['jsCode']
script=f'''const run=new Function('$','$input',{json.dumps(code)});
const alert={{alert_id:'CMOS56:CONTRACT',priority:4,source:'SYSTEM_TEST',category:'TEST',municipality:'WEEHAWKEN',search_text:'PARK AVENUE ROAD CLOSURE'}};
const recipient={{subscriber_uuid:'56000000-0000-0000-0000-000000000099',subscriber_id:'CMOS56_TEST',name:'Contract',ntfy_topic:'contract'}};
const row={{watch_item_uuid:'56000000-0000-0000-0000-000000000098',watch_id:'CMOS56_DEDUP',display_name:'Contract',watch_type:'CORRIDOR',search_term:'PARK AVENUE',aliases:[],match_mode:'CONTAINS',match_field:'search_text',min_priority:1,source_filter:[],alert_category_filter:[],spatial_match_type:'PROXIMITY',spatial_match_reason:'trusted geometry inside corridor buffer',spatial_distance_ft:125,recipients:[recipient,recipient]}};
const evaluate=(caseAlert,caseRow)=>run(()=>({{first:()=>({{json:caseAlert}})}}),{{all:()=>[{{json:caseRow}}]}})[0].json;
const output=evaluate(alert,row);
if(output.match_count!==1)throw new Error('text plus spatial produced duplicate watch matches');
if(output.matches[0].match_mode!=='PROXIMITY')throw new Error('spatial match did not take precedence');
if(output.recipient_count!==1||output.delivery_payloads.length!==1)throw new Error('recipient delivery was not deduplicated');
if(output.matched_watch_ids.length!==1)throw new Error('watch ID was duplicated');
const locationTopic={{...row,watch_id:'CMOS_LOCATION_TOPIC',watch_type:'LOCATION_TOPIC',search_term:'ROAD CLOSURE',recipients:[recipient]}};
const inside=evaluate(alert,locationTopic);
if(inside.match_count!==1||inside.matches[0].match_mode!=='LOCATION_TOPIC')throw new Error('Location plus topic did not require and record both conditions');
const outside=evaluate(alert,{{...locationTopic,spatial_match_type:null,spatial_match_reason:null}});
if(outside.match_count!==0)throw new Error('Location plus topic matched outside the Location');
const wrongTopic=evaluate({{...alert,search_text:'PARK AVENUE WATER MAIN'}},locationTopic);
if(wrongTopic.match_count!==0)throw new Error('Location plus topic matched without the topic');
const municipality=evaluate(alert,{{...locationTopic,nearby_enabled:false,municipality:'Weehawken',spatial_match_type:null,spatial_match_reason:null}});
if(municipality.match_count!==1)throw new Error('municipality plus topic did not match both conditions');
console.log('MATCHER CONTRACT spatial_dedup=ONE location_plus_topic=AND municipality_plus_topic=AND recipient_delivery=ONE');
'''
open(sys.argv[2],'w').write(script)
PY
docker cp "$TMP_CONTRACT" "n8n:$TMP_CONTRACT"
prepare_node_file "$TMP_CONTRACT"
docker exec -u node n8n node "$TMP_CONTRACT"

log "Importing and publishing the central matcher"
docker cp "$TMP_TARGET" "n8n:$TMP_TARGET"
prepare_node_file "$TMP_TARGET"
docker exec -u node n8n n8n import:workflow --input="$TMP_TARGET" >/dev/null
PUBLISHED=1
publish_matcher
docker restart n8n >/dev/null
wait_ready || fail "n8n did not become ready"

log "Verifying the active published workflow contract"
python3 - "$N8N_DB" "$MATCHER_ID" <<'PY'
import json,sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
row=con.execute('SELECT active,activeVersionId,nodes FROM workflow_entity WHERE id=?',(sys.argv[2],)).fetchone()
if not row or not row['active'] or not row['activeVersionId']:
    raise SystemExit('matcher is not active and published')
nodes={node.get('name'):node for node in json.loads(row['nodes'])}
load=nodes.get('Load Active Watchlist + Recipients') or {}
match=nodes.get('Match + Resolve Recipients') or {}
query=(load.get('parameters') or {}).get('query','')
options=(load.get('parameters') or {}).get('options') or {}
code=(match.get('parameters') or {}).get('jsCode','')
if 'gis_active_spatial_watch_matches' not in query:
    raise SystemExit('published loader does not call PostGIS spatial matching')
if 'supplied_alert_geom' not in query:
    raise SystemExit('published loader does not preserve exact Standard Alert coordinates')
if 'queryReplacement' not in options:
    raise SystemExit('published loader does not bind the normalized alert safely')
required=('row.spatial_match_type','result.match_type || row.match_mode',
          'locationPlusTopic','LOCATION_TOPIC','municipalityLocationMatch')
if not all(value in code for value in required):
    raise SystemExit('published matcher is missing spatial or Location-plus-topic behavior')
print('MATCHER active=1 published=1 postgis_spatial=YES location_plus_topic=AND deduplication=YES')
con.close()
PY

PUBLISHED=0
trap - ERR
rm -f "$TMP_TARGET" "$TMP_CONTRACT"
docker exec n8n rm -f "$TMP_TARGET" "$TMP_BACKUP" "$TMP_CONTRACT" >/dev/null 2>&1 || true
log "#56 MATCHER INSTALL: PASS"
log "Backup retained at: $BACKUP_DIR"
log "The existing matcher, Subscribers, Routing, Delivery Guard, and ntfy path remains authoritative"
