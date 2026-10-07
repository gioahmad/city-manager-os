#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED="${1:-}"
WORKFLOW_ID="CmosGeoRematch01"
SOURCE="$REPO/workflows/core/CORE_Resolved_Spatial_Rematch_v1.json"
BACKUP_ROOT="/var/backups/city-manager-os/resolved-rematch"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"
TMP="/tmp/CORE_Resolved_Spatial_Rematch_${STAMP}.json"
TMP_BACKUP="/tmp/CORE_Resolved_Spatial_Rematch_pre_${STAMP}.json"

log(){ printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }

cd "$REPO"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
if [[ -n "$EXPECTED" ]]; then
  [[ "$(git rev-parse HEAD)" == "$EXPECTED" ]] || fail "HEAD does not match expected target"
fi
[[ -s "$SOURCE" ]] || fail "resolved rematch workflow definition missing"

N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
N8N_DB="$N8N_DIR/database.sqlite"
[[ -f "$N8N_DB" ]] || fail "n8n database not found"

if python3 - "$N8N_DB" "$WORKFLOW_ID" <<'PY'
import json,sqlite3,sys
db,wid=sys.argv[1:]
con=sqlite3.connect(db); con.row_factory=sqlite3.Row
row=con.execute("SELECT active,nodes FROM workflow_entity WHERE id=?",(wid,)).fetchone()
con.close()
if not row or not row["active"]:
    raise SystemExit(1)
nodes={n.get("name"):n for n in json.loads(row["nodes"])}
q=((nodes.get("Load Newly Resolved Alerts") or {}).get("parameters") or {}).get("query","")
m=((nodes.get("Mark Resolved Alert Rematched") or {}).get("parameters") or {}).get("query","")
if "nullif(w.county,'') IS NOT NULL" not in q or "geo-v3" not in q or "geo-v3" not in m:
    raise SystemExit(1)
print("RESOLVED REMATCH already current")
PY
then
  log "Resolved-alert rematch already current — no backup or n8n restart required"
  exit 0
fi

install -d -m 700 "$BACKUP_DIR"
python3 - "$N8N_DB" "$BACKUP_DIR/database.sqlite" <<'PY'
import sqlite3,sys
source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2])
source.backup(target); target.close(); source.close()
PY

if docker exec -u node n8n n8n export:workflow --id="$WORKFLOW_ID" --output="$TMP_BACKUP" >/dev/null 2>&1; then
  docker cp "n8n:$TMP_BACKUP" "$BACKUP_DIR/workflow.json" >/dev/null
  chmod 600 "$BACKUP_DIR/workflow.json"
fi

ACTIVATED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
python3 - "$SOURCE" "$TMP" "$ACTIVATED_AT" <<'PY'
import json,sys,uuid
source,target,activated=sys.argv[1:]
wf=json.load(open(source))
for node in wf.get("nodes") or []:
    params=node.get("parameters") or {}
    if isinstance(params.get("query"),str):
        params["query"]=params["query"].replace("__CMOS_ACTIVATED_AT__",activated)
wf["active"]=False
wf["versionId"]=str(uuid.uuid4())
json.dump(wf,open(target,"w"),indent=2)
PY

docker cp "$TMP" "n8n:$TMP"
docker exec -u root n8n chown node:node "$TMP"
docker exec -u root n8n chmod 600 "$TMP"
docker exec -u node n8n n8n import:workflow --input="$TMP" >/dev/null

if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
  docker exec -u node n8n n8n publish:workflow --id="$WORKFLOW_ID" >/dev/null
else
  docker exec -u node n8n n8n update:workflow --id="$WORKFLOW_ID" --active=true >/dev/null
fi

docker restart n8n >/dev/null
for _ in $(seq 1 60); do
  if docker exec n8n node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

python3 - "$N8N_DB" "$WORKFLOW_ID" <<'PY'
import json,sqlite3,sys
db,wid=sys.argv[1:]
con=sqlite3.connect(db); con.row_factory=sqlite3.Row
row=con.execute("SELECT active,activeVersionId,nodes FROM workflow_entity WHERE id=?",(wid,)).fetchone()
con.close()
assert row and row["active"] and row["activeVersionId"], row
nodes={n.get("name"):n for n in json.loads(row["nodes"])}
q=((nodes["Load Newly Resolved Alerts"].get("parameters") or {}).get("query") or "")
m=((nodes["Mark Resolved Alert Rematched"].get("parameters") or {}).get("query") or "")
assert "nullif(w.county,'') IS NOT NULL" in q
assert "geo-v3" in q and "geo-v3" in m
print("RESOLVED REMATCH: PASS county_watch=YES version=geo-v3")
PY

rm -f "$TMP"
docker exec n8n rm -f "$TMP" "$TMP_BACKUP" >/dev/null 2>&1 || true

mapfile -t stale < <(
  find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null |
    sort -nr | awk 'NR>3 {sub(/^[^ ]+ /,""); print}'
)
for path in "${stale[@]}"; do rm -rf -- "$path"; done

log "Resolved-alert rematch installed"
