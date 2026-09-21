#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C GIT_TERMINAL_PROMPT=0

REPO="${CMOS_REPO:-/opt/city-manager-os}"
BASE="${1:?usage: bnn-map-recovery.sh BASE TARGET}"
TARGET="${2:?usage: bnn-map-recovery.sh BASE TARGET}"
ALERT_ID="${BNN_ACCEPTANCE_ALERT_ID:-BNN:d1468ab2}"
COMPOSE=(docker compose -f "$REPO/dashboard/docker-compose.yml")
INTEGRATION_SERVICE="citymanager-integration-engine"
INTEGRATION_STOPPED=0

log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }

resume_integration_engine(){
  [[ "$INTEGRATION_STOPPED" == 1 ]] || return 0
  log "Restarting the integration worker"
  "${COMPOSE[@]}" up -d --no-deps "$INTEGRATION_SERVICE"
  [[ "$(docker inspect -f '{{.State.Running}}' "$INTEGRATION_SERVICE" 2>/dev/null || true)" == true ]] || return 1
  INTEGRATION_STOPPED=0
}

cleanup(){
  local rc=$?
  trap - EXIT
  if [[ "$INTEGRATION_STOPPED" == 1 ]] && ! resume_integration_engine; then
    log "ERROR: integration worker restart failed"
    rc=1
  fi
  exit "$rc"
}
trap cleanup EXIT

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
[[ "$(git rev-parse HEAD)" == "$TARGET" ]] || fail "HEAD does not match target"
[[ "$(git rev-parse origin/main)" == "$TARGET" ]] || fail "origin/main does not match target"
git merge-base --is-ancestor "$BASE" "$TARGET" || fail "base is not an ancestor of target"

coverage(){
  docker exec -i citymanager-dashboard python - "$ALERT_ID" <<'PY'
import json,sys
from app import query_one

alert_id=sys.argv[1]
row=query_one(
    """
    SELECT count(*) AS total,
           count(*) FILTER (WHERE coalesce(a.geom,CASE WHEN r.status='RESOLVED' THEN r.geom END) IS NOT NULL) AS mapped,
           count(*) FILTER (WHERE coalesce(a.geom,CASE WHEN r.status='RESOLVED' THEN r.geom END) IS NULL) AS unmapped,
           count(*) FILTER (WHERE a.geom IS NOT NULL) AS precise,
           count(*) FILTER (WHERE a.geom IS NULL AND r.status='RESOLVED' AND r.geom IS NOT NULL) AS approximate,
           count(*) FILTER (WHERE r.status='UNRESOLVED') AS unresolved,
           count(*) FILTER (WHERE r.status='AMBIGUOUS') AS ambiguous,
           count(*) FILTER (WHERE r.entity_id IS NULL) AS pending,
           coalesce(bool_or(a.alert_id=%s AND coalesce(a.geom,CASE WHEN r.status='RESOLVED' THEN r.geom END) IS NOT NULL),false) AS target_mapped
    FROM alerts a
    LEFT JOIN geo_entity_resolutions r
      ON r.entity_type='ALERT' AND r.entity_id=a.id::text
    WHERE a.source='BNN'
    """,
    (alert_id,),
)
print(json.dumps(row,default=str,sort_keys=True))
PY
}

log "Capturing current BNN map coverage"
BEFORE="$(coverage)"
printf 'BNN_COVERAGE_BEFORE=%s\n' "$BEFORE"

log "Dry-running ${ALERT_ID} through the new resolver without writes"
DRY_RUN="$("${COMPOSE[@]}" run --rm --no-deps -T \
  -v "$REPO/dashboard:/src:ro" -w /src -e PYTHONPATH=/src:/app \
  --entrypoint python citymanager-dashboard - "$ALERT_ID" <<'PY'
import json,sys
from geo_resolver import _alert_payload,_connect,extract_location_candidates,resolve_payload

alert_id=sys.argv[1]
with _connect() as conn:
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(
            """
            SELECT a.id::text AS entity_id,a.alert_id,a.source,a.county,a.municipality,
                   a.title,a.message,a.location,a.metadata,a.raw_payload,a.updated_at,
                   CASE WHEN a.geom IS NOT NULL THEN ST_X(a.geom) END AS longitude,
                   CASE WHEN a.geom IS NOT NULL THEN ST_Y(a.geom) END AS latitude
            FROM alerts a WHERE a.alert_id=%s
            """,
            (alert_id,),
        )
        row=cur.fetchone()
    if not row:
        raise SystemExit(f"{alert_id} not found")
    payload=_alert_payload(row)
    result=resolve_payload(conn,payload,use_cache=False,persist=False)
    conn.rollback()
report={
    "alert_id":alert_id,
    "title":row.get("title"),
    "message":row.get("message"),
    "location":row.get("location"),
    "candidates":[item.as_dict() for item in extract_location_candidates(payload)],
    "result":result,
}
print(json.dumps(report,default=str,sort_keys=True))
if result.get("status")!="RESOLVED" or result.get("longitude") is None or result.get("latitude") is None:
    raise SystemExit(f"{alert_id} is still unresolved in the new resolver")
PY
)" || { printf 'BNN_TARGET_DRY_RUN=%s\n' "$DRY_RUN"; fail "target alert did not pass read-only preflight"; }
printf 'BNN_TARGET_DRY_RUN=%s\n' "$DRY_RUN"

log "Applying the focused dashboard and integration-engine release"
"$REPO/deploy/cmos-deploy" apply --base "$BASE" --target "$TARGET"

log "Stopping the integration worker for an exclusive resolver maintenance window"
INTEGRATION_STOPPED=1
"${COMPOSE[@]}" stop -t 30 "$INTEGRATION_SERVICE"

log "Reprocessing previously unmapped BNN alerts through resolver v5"
BACKFILL_RAW="$("${COMPOSE[@]}" run --rm --no-deps -T --entrypoint python \
  "$INTEGRATION_SERVICE" /app/geo_resolver.py \
  backfill --limit 10000 --since-days 3650 --source BNN)"
BACKFILL="$(printf '%s\n' "$BACKFILL_RAW" | tail -n 1)"
printf 'BNN_BACKFILL_SUMMARY=%s\n' "$BACKFILL"
python - "$BACKFILL" <<'PY'
import json,sys
summary=json.loads(sys.argv[1])
assert summary.get("locked") is False, summary
assert int(summary.get("errors") or 0)==0, summary
assert int(summary.get("processed") or 0)==int(summary.get("selected") or 0), summary
PY

log "Auditing every stored BNN alert without writes"
AUDIT="$("${COMPOSE[@]}" run --rm --no-deps -T --entrypoint python \
  "$INTEGRATION_SERVICE" /app/geo_resolver.py audit --source BNN)"
printf 'BNN_AUDIT_SUMMARY=%s\n' "$AUDIT"
python - "$AUDIT" <<'PY'
import json,sys
summary=json.loads(sys.argv[1])
assert summary.get("mode")=="READ_ONLY", summary
assert summary.get("complete") is True, summary
assert int(summary.get("error_count") or 0)==0, summary
assert int(summary.get("selected") or 0)==int(summary.get("total_available") or 0), summary
assert int(summary.get("mapped_count") or 0)>0, summary
PY

AFTER="$(coverage)"
printf 'BNN_COVERAGE_AFTER=%s\n' "$AFTER"
python - "$BEFORE" "$AFTER" "$ALERT_ID" <<'PY'
import json,sys
before,after=map(json.loads,sys.argv[1:3])
alert_id=sys.argv[3]
assert int(after["total"])==int(before["total"]), (before,after)
assert int(after["mapped"])>=int(before["mapped"]), (before,after)
assert after["target_mapped"] is True, {"alert_id":alert_id,"coverage":after}
if not before["target_mapped"]:
    assert int(after["mapped"])>int(before["mapped"]), (before,after)
PY

resume_integration_engine || fail "integration worker did not restart"
trap - EXIT
log "BNN MAP RECOVERY: PASS target=${ALERT_ID} full_e2e=NOT_RUN notifications=NONE"
