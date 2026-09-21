#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${CMOS_REPO:-/opt/city-manager-os}"
BASE="${1:?usage: watch-lab.sh BASE TARGET}"
TARGET="${2:?usage: watch-lab.sh BASE TARGET}"
MATCHER_ID="ESH9c2pZ8QfkMosO"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/var/backups/city-manager-os/watch-lab/$STAMP"
RESTORE_FILE="/tmp/CORE_Watchlist_Matcher_watch_lab_restore_${STAMP}.json"
N8N_CHANGED=0
DASHBOARD_CHANGED=0

log(){ printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }

wait_n8n(){
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

publish_matcher(){
  if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
    docker exec -u node n8n n8n publish:workflow --id="$MATCHER_ID" >/dev/null
  else
    docker exec -u node n8n n8n update:workflow --id="$MATCHER_ID" --active=true >/dev/null
  fi
}

rollback_release(){
  local rc=$?
  trap - ERR
  if (( DASHBOARD_CHANGED == 1 )); then
    log "FAILURE DIAGNOSTIC: dashboard logs before rollback"
    docker logs --tail 120 citymanager-dashboard >&2 || true
  fi
  if (( DASHBOARD_CHANGED == 1 )) && docker image inspect \
    dashboard-citymanager-dashboard:cmos-deploy-rollback-citymanager-dashboard >/dev/null 2>&1; then
    log "ROLLBACK: restoring the prior dashboard image"
    docker image tag \
      dashboard-citymanager-dashboard:cmos-deploy-rollback-citymanager-dashboard \
      dashboard-citymanager-dashboard:latest >/dev/null 2>&1 || true
    docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate \
      citymanager-dashboard >/dev/null 2>&1 || true
  fi
  if (( N8N_CHANGED == 1 )) && [[ -s "$BACKUP_DIR/CORE_Watchlist_Matcher_pre56.json" ]]; then
    log "ROLLBACK: restoring the prior central Watch matcher"
    docker cp "$BACKUP_DIR/CORE_Watchlist_Matcher_pre56.json" "n8n:$RESTORE_FILE" >/dev/null 2>&1 || true
    docker exec -u root n8n chown node:node "$RESTORE_FILE" >/dev/null 2>&1 || true
    docker exec -u root n8n chmod 600 "$RESTORE_FILE" >/dev/null 2>&1 || true
    docker exec -u node n8n n8n import:workflow --input="$RESTORE_FILE" >/dev/null 2>&1 || true
    publish_matcher >/dev/null 2>&1 || true
    docker restart n8n >/dev/null 2>&1 || true
    wait_n8n >/dev/null 2>&1 || true
  fi
  log "WATCH LAB RELEASE: FAIL rc=$rc"
  log "Dashboard runtime and central matcher were restored when their backups were available"
  exit "$rc"
}
trap rollback_release ERR

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "production repository must be clean"
[[ "$(git rev-parse HEAD)" == "$TARGET" ]] || fail "HEAD does not match the approved target"
git merge-base --is-ancestor "$BASE" "$TARGET" || fail "base is not an ancestor of target"

for container in citymanager-dashboard citymanager-postgis n8n; do
  [[ "$(docker inspect "$container" --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
    || fail "required container is not running: $container"
done

PLAN="$(./deploy/cmos-deploy plan --base "$BASE" --target "$TARGET")"
printf '%s\n' "$PLAN"
grep -Fqx 'services=citymanager-dashboard' <<<"$PLAN" || fail "release must recreate only the dashboard"
grep -Fqx 'external=n8n-workflow-publish' <<<"$PLAN" || fail "release must publish only the guarded n8n matcher"
grep -Fqx 'full_e2e=no' <<<"$PLAN" || fail "full E2E must remain disabled"
grep -Fqx 'unknown=none' <<<"$PLAN" || fail "release contains an unclassified path"

log "Publishing the existing central matcher with backup and isolated contracts"
CMOS_MATCHER_BACKUP_DIR="$BACKUP_DIR" \
  ./deploy/n8n/install_spatial_watch_matcher.sh "$TARGET"
N8N_CHANGED=1

log "Rebuilding and recreating only the dashboard with focused Watch verification"
./deploy/cmos-deploy apply \
  --base "$BASE" \
  --target "$TARGET" \
  --external-applied
DASHBOARD_CHANGED=1

log "Running read-only Valley Hospital acceptance with zero Match or delivery writes"
docker exec -i citymanager-dashboard python - <<'PY'
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from app import query_one

alert_id = "ORU:eb5707b3"
watch = query_one(
    "SELECT id::text AS id FROM watch_items WHERE display_name ILIKE %s ORDER BY updated_at DESC LIMIT 1",
    ("%Valley Hospital%",),
)
assert watch, "Valley Hospital Watch not found"
before = query_one(
    """
    SELECT
      (SELECT count(*) FROM alert_watch_matches) AS matches,
      (SELECT count(*) FROM deliveries) AS deliveries
    """
)
token = os.environ.get("CMOS_AUTOMATION_TOKEN", "").strip()
headers = {"Content-Type": "application/x-www-form-urlencoded"}
if token:
    headers["X-CMOS-Automation-Key"] = token

def evaluate(point_mode):
    body = urllib.parse.urlencode({
        "watch_item_id": watch["id"],
        "alert_id": alert_id,
        "point_mode": point_mode,
    }).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:8000/api/watch-lab/evaluate",
        data=body,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == 200
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Watch Lab {point_mode} returned HTTP {exc.code}: {body}") from exc

saved = evaluate("ALERT")
center = evaluate("WATCH_CENTER")
after = query_one(
    """
    SELECT
      (SELECT count(*) FROM alert_watch_matches) AS matches,
      (SELECT count(*) FROM deliveries) AS deliveries
    """
)
assert saved["read_only"] is True
assert center["read_only"] is True
assert center["evidence"]["point_inside_watch"] is True, center
assert before == after, {"before": before, "after": after}
print(json.dumps({
    "status": "PASS",
    "alert_id": alert_id,
    "watch_id": center["watch"]["watch_id"],
    "saved_point_inside": saved["evidence"]["point_inside_watch"],
    "center_point_inside": center["evidence"]["point_inside_watch"],
    "writes": 0,
}, sort_keys=True))
PY

N8N_CHANGED=0
DASHBOARD_CHANGED=0
trap - ERR
docker exec n8n rm -f "$RESTORE_FILE" >/dev/null 2>&1 || true
log "WATCH LAB RELEASE: PASS"
log "FULL_E2E=NOT_RUN"
log "Backup retained at: $BACKUP_DIR"
