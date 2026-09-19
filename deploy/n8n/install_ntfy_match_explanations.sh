#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED_TARGET="${1:-}"
SOURCE_FILE="$REPO/workflows/core/CORE_ntfy_Sender_v1.json"
WORKFLOW_NAME="CORE - ntfy Sender v1"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="/var/backups/city-manager-os/ntfy-match-explanations"
BACKUP_DIR="${CMOS_NTFY_BACKUP_DIR:-$BACKUP_ROOT/$STAMP}"
TMP_TARGET="/tmp/CORE_ntfy_Sender_explanations_${STAMP}.json"
TMP_BACKUP="/tmp/CORE_ntfy_Sender_before_explanations_${STAMP}.json"
TMP_CONTRACT="/tmp/CORE_ntfy_Sender_explanations_contract_${STAMP}.js"
SENDER_ID=""
MUTATED=0

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
  (( ready == 1 ))
}

publish_sender(){
  if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
    docker exec -u node n8n n8n publish:workflow --id="$SENDER_ID" >/dev/null
  else
    docker exec -u node n8n n8n update:workflow --id="$SENDER_ID" --active=true >/dev/null
  fi
}

prepare_node_file(){
  docker exec -u root n8n chown node:node "$1"
  docker exec -u root n8n chmod 600 "$1"
}

rollback_sender(){
  local rc=$?
  trap - ERR
  if (( MUTATED == 1 )) && [[ -s "$BACKUP_DIR/CORE_ntfy_Sender_previous.json" ]]; then
    log "ROLLBACK: restoring the prior ntfy sender"
    docker cp "$BACKUP_DIR/CORE_ntfy_Sender_previous.json" "n8n:$TMP_BACKUP" >/dev/null 2>&1 || true
    prepare_node_file "$TMP_BACKUP" >/dev/null 2>&1 || true
    docker exec -u node n8n n8n import:workflow --input="$TMP_BACKUP" >/dev/null 2>&1 || true
    publish_sender >/dev/null 2>&1 || true
    docker restart n8n >/dev/null 2>&1 || true
    wait_ready >/dev/null 2>&1 || true
  fi
  log "NTFY MATCH EXPLANATION INSTALL FAILED rc=${rc} line=${LINENO}"
  exit "$rc"
}
trap rollback_sender ERR

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
[[ -z "$EXPECTED_TARGET" || "$(git rev-parse HEAD)" == "$EXPECTED_TARGET" ]] \
  || fail "HEAD does not match expected target"
[[ -s "$SOURCE_FILE" ]] || fail "ntfy sender definition is missing"
[[ "$(docker inspect n8n --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
  || fail "required container is not running: n8n"

N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
N8N_DB="$N8N_DIR/database.sqlite"
[[ -f "$N8N_DB" ]] || fail "n8n database was not found"

SENDER_ID="$(python3 - "$N8N_DB" "$WORKFLOW_NAME" <<'PY'
import sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
rows=con.execute('SELECT id,active,activeVersionId FROM workflow_entity WHERE name=?',(sys.argv[2],)).fetchall()
if len(rows)!=1: raise SystemExit(f'expected one exact ntfy sender, found {len(rows)}')
row=rows[0]
if not row['active'] or not row['activeVersionId']: raise SystemExit('existing ntfy sender is not active and published')
print(row['id'])
con.close()
PY
)"
[[ -n "$SENDER_ID" ]] || fail "published ntfy sender was not found"

install -d -m 700 "$BACKUP_DIR"
log "Backing up n8n and the published ntfy sender"
python3 - "$N8N_DB" "$BACKUP_DIR/database.sqlite" <<'PY'
import sqlite3,sys
source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2])
source.backup(target); target.close(); source.close()
PY
docker exec -u node n8n n8n export:workflow --id="$SENDER_ID" --output="$TMP_BACKUP" >/dev/null
docker cp "n8n:$TMP_BACKUP" "$BACKUP_DIR/CORE_ntfy_Sender_previous.json"
chmod 600 "$BACKUP_DIR/database.sqlite" "$BACKUP_DIR/CORE_ntfy_Sender_previous.json"

log "Preparing and validating the ntfy sender"
python3 - "$SOURCE_FILE" "$TMP_TARGET" "$SENDER_ID" <<'PY'
import json,sys,uuid
source,target,workflow_id=sys.argv[1:]
workflow=json.load(open(source))
if isinstance(workflow,list):
    if len(workflow)!=1: raise SystemExit('ntfy sender source must contain exactly one workflow')
    workflow=workflow[0]
if workflow.get('name')!='CORE - ntfy Sender v1': raise SystemExit('unexpected workflow name')
nodes={node.get('name'):node for node in workflow.get('nodes') or []}
prepare=nodes.get('Prepare ntfy Requests') or {}
code=(prepare.get('parameters') or {}).get('jsCode','')
required=(
    'function explainMatch',
    'Why you received this:',
    'Keyword',
    'Watch center',
    'payload.match_reasons',
    'const message = explanations.length',
)
if not all(marker in code for marker in required):
    raise SystemExit('plain-language match explanation contract is incomplete')
if 'Publish to ntfy' not in nodes or 'Return Send Results' not in nodes:
    raise SystemExit('central ntfy sender path is incomplete')
workflow['id']=workflow_id
workflow['active']=False
workflow['versionId']=str(uuid.uuid4())
json.dump(workflow,open(target,'w'),indent=2,ensure_ascii=False)
PY
python3 -m json.tool "$TMP_TARGET" >/dev/null

log "Running isolated ntfy message contracts without sending a Notification"
python3 - "$TMP_TARGET" "$TMP_CONTRACT" <<'PY'
import json,sys
workflow=json.load(open(sys.argv[1]))
nodes={node.get('name'):node for node in workflow.get('nodes') or []}
code=(nodes['Prepare ntfy Requests'].get('parameters') or {})['jsCode']
script=f'''const run=new Function('$input',{json.dumps(code)});
const prepare=(payload)=>run({{first:()=>({{json:{{delivery_payloads:[payload]}}}})}})[0].json.ntfy_body.message;
const base={{ntfy_topic:'contract-only',title:'Contract',message:'Original alert',priority:3,tags:[]}};
const keyword=prepare({{...base,match_reasons:['CONTAINS search_text matched search_term "CONTRACT KEYWORD"']}});
if(!keyword.includes('Why you received this:')||!keyword.includes('Keyword “CONTRACT KEYWORD” matched this alert'))throw new Error('keyword explanation missing');
if(keyword.includes('search_text')||keyword.includes('search_term'))throw new Error('technical keyword terms leaked');
const location=prepare({{...base,match_reasons:['PROXIMITY alert geometry is 125.0 ft from target, inside 5280.0 ft buffer']}});
if(!location.includes('Watch center')||!location.includes('5,280-foot Distance'))throw new Error('Location explanation missing');
if(location.includes('geometry')||location.includes('buffer'))throw new Error('technical Location terms leaked');
if(prepare(base)!=='Original alert')throw new Error('message without reasons changed');
console.log('NTFY_EXPLANATION_CONTRACT keyword=PASS location=PASS unchanged=PASS notification_sent=NO');
'''
open(sys.argv[2],'w').write(script)
PY
docker cp "$TMP_CONTRACT" "n8n:$TMP_CONTRACT"
prepare_node_file "$TMP_CONTRACT"
docker exec -u node n8n node "$TMP_CONTRACT"

log "Importing and publishing the ntfy sender"
docker cp "$TMP_TARGET" "n8n:$TMP_TARGET"
prepare_node_file "$TMP_TARGET"
docker exec -u node n8n n8n import:workflow --input="$TMP_TARGET" >/dev/null
MUTATED=1
publish_sender
docker restart n8n >/dev/null
wait_ready || fail "n8n did not become ready"

log "Verifying the published ntfy sender"
python3 - "$N8N_DB" "$SENDER_ID" <<'PY'
import json,sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
row=con.execute('SELECT name,active,activeVersionId,nodes FROM workflow_entity WHERE id=?',(sys.argv[2],)).fetchone()
if not row or row['name']!='CORE - ntfy Sender v1': raise SystemExit('ntfy sender identity changed')
if not row['active'] or not row['activeVersionId']: raise SystemExit('ntfy sender is not active and published')
nodes={node.get('name'):node for node in json.loads(row['nodes'])}
code=((nodes.get('Prepare ntfy Requests') or {}).get('parameters') or {}).get('jsCode','')
required=('function explainMatch','Why you received this:','payload.match_reasons','Watch center')
if not all(marker in code for marker in required): raise SystemExit('published sender explanation contract is incomplete')
print('NTFY_SENDER active=1 published=1 plain_language_reason=YES')
con.close()
PY

MUTATED=0
trap - ERR
rm -f "$TMP_TARGET" "$TMP_CONTRACT"
docker exec n8n rm -f "$TMP_TARGET" "$TMP_BACKUP" "$TMP_CONTRACT" >/dev/null 2>&1 || true
log "NTFY MATCH EXPLANATION INSTALL: PASS"
log "Backup retained at: $BACKUP_DIR"
