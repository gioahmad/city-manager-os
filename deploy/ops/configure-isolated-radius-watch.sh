#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
REPORT_BRANCH="release-output/ops"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/operations"
STATE_DIR="/var/lib/city-manager-os/operations"
LOG_FILE="$LOG_DIR/isolated-radius-watch-$RUN_ID.log"
RECEIPT_FILE="$STATE_DIR/isolated-radius-watch-latest.json"
LOCK_FILE="/var/lock/cmos-isolated-radius-watch.lock"
CONTAINER_CONFIG="/tmp/cmos-private-radius-watch-$RUN_ID.json"
HOST_CONFIG=""
TRANSPORT_PAYLOAD=""
CURRENT_PHASE="startup"
FINAL_STATUS="FAIL"
FAIL_LINE=""
CONFIG_RESULT=""
TRANSPORT_RESULT="not-started"
REPORT_RESULT="not-started"

SUBSCRIBER_KEY="${CMOS_WATCH_SUBSCRIBER_KEY:-PRIVATE_RADIUS_PILOT}"
WATCH_KEY="${CMOS_WATCH_KEY:-W_PRIVATE_RADIUS_PILOT}"
WATCH_NAME="${CMOS_WATCH_NAME:-Private 1-Mile Pilot}"
RADIUS_FT="${CMOS_WATCH_RADIUS_FT:-5280}"
NTFY_PUBLISH_BASE="${CMOS_NTFY_PUBLISH_BASE:-http://100.94.203.47:8080}"
NTFY_SUBSCRIBE_BASE="${CMOS_NTFY_SUBSCRIBE_BASE:-https://ntfy.nhnj.us}"

usage(){
  cat <<'EOF'
Usage: sudo bash deploy/ops/configure-isolated-radius-watch.sh

Creates or updates one isolated private alert pilot using the existing City
Manager OS Watchlist, Geo Resolver, Subscribers, Routing, Delivery Guard and
ntfy architecture.

What it configures:
  - one permanent 5,280-foot spatial watch
  - all sources and alert categories, priority 1+
  - exact-address text fallback for alerts without trustworthy geometry
  - one random ntfy topic assigned only to this watch

What it verifies:
  - private address resolves through the local Geo Resolver
  - topic accepts one clearly labeled transport test
  - a synthetic point matches the watch and its single route inside a rollback
  - Dashboard spatial-watch health remains good

Privacy and change scope:
  - address input is hidden and remains only in production PostGIS
  - address, coordinates and topic are excluded from GitHub reports
  - no build, restart, schema change, workflow publish or full E2E

Rerunning is safe: the stable watch/subscriber are updated and the existing
private topic is preserved. Pause or edit the pilot later from Watchlists or
Subscribers in the private Dashboard.

Optional environment overrides:
  CMOS_PRIVATE_WATCH_ADDRESS  Noninteractive full address
  CMOS_WATCH_RADIUS_FT        Radius from 1 through 26400 (default 5280)
  CMOS_WATCH_NAME             Private Dashboard label
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

db_at(){
  docker exec -i citymanager-postgis sh -lc \
    'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}

safe_git_value(){
  local value=""
  value="$(git -C "$REPO" "$@" 2>/dev/null || true)"
  printf '%s' "${value:-unavailable}"
}

cleanup(){
  local rc=$?
  trap - EXIT
  [[ -z "$HOST_CONFIG" || ! -f "$HOST_CONFIG" ]] || rm -f "$HOST_CONFIG"
  [[ -z "$TRANSPORT_PAYLOAD" || ! -f "$TRANSPORT_PAYLOAD" ]] || rm -f "$TRANSPORT_PAYLOAD"
  docker exec citymanager-dashboard rm -f "$CONTAINER_CONFIG" >/dev/null 2>&1 || true
  exit "$rc"
}

publish_report(){ (
  set +e
  local status="$1" rc="$2" parent="" worktree="" report_dir report_file latest_file
  local result_block="No sanitized configuration result was produced."
  report_cleanup(){
    if [[ -n "$worktree" && "$worktree" == /tmp/cmos-watch-report.*/* ]]; then
      git -C "$REPO" worktree remove --force "$worktree" >/dev/null 2>&1 || true
    fi
    [[ -z "$parent" || "$parent" != /tmp/cmos-watch-report.* ]] || rmdir "$parent" >/dev/null 2>&1 || true
  }
  trap report_cleanup EXIT
  git -C "$REPO" fetch -q origin \
    "+refs/heads/$REPORT_BRANCH:refs/remotes/origin/$REPORT_BRANCH" || return 1
  parent="$(mktemp -d /tmp/cmos-watch-report.XXXXXX)"
  worktree="$parent/worktree"
  git -C "$REPO" worktree add --detach "$worktree" "origin/$REPORT_BRANCH" >/dev/null 2>&1 || return 1
  report_dir="$worktree/operation-results/isolated-radius-watch"
  mkdir -p "$report_dir"
  report_file="$report_dir/$RUN_ID-${status,,}.md"
  latest_file="$report_dir/latest.md"
  if [[ -n "$CONFIG_RESULT" ]]; then
    result_block="$CONFIG_RESULT"
  fi
  {
    printf '# Isolated radius watch operation\n\n'
    printf '> Public redacted summary. Address, coordinates, topic and credentials are intentionally excluded.\n\n'
    printf '| Field | Value |\n|---|---|\n'
    printf '| Run | `%s` |\n' "$RUN_ID"
    printf '| Status | **%s** |\n' "$status"
    printf '| Exit code | `%s` |\n' "$rc"
    printf '| Failed line | `%s` |\n' "${FAIL_LINE:-none}"
    printf '| Phase | `%s` |\n' "$CURRENT_PHASE"
    printf '| Radius | `%s ft` |\n' "$RADIUS_FT"
    printf '| Sources | `ANY` |\n'
    printf '| Categories | `ANY` |\n'
    printf '| Minimum priority | `1` |\n'
    printf '| Transport test | `%s` |\n' "$TRANSPORT_RESULT"
    printf '| Builds | `none` |\n'
    printf '| Restarts | `none` |\n'
    printf '| Schema changes | `none` |\n'
    printf '| Full E2E | `not run` |\n'
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '| Repository HEAD | `%s` |\n\n' "$(safe_git_value rev-parse HEAD)"
    printf '## Sanitized result\n\n```json\n%s\n```\n\n' "$result_block"
    printf '## Failure locator\n\n'
    printf 'The full mode-600 log remains on the VPS at `%s`.\n' "$LOG_FILE"
  } > "$report_file"
  install -m 600 "$report_file" "$latest_file"
  git -C "$worktree" add operation-results/isolated-radius-watch
  git -C "$worktree" -c user.name='City Manager OS Operations Runner' \
    -c user.email='operations-runner@localhost' commit \
    -m "Record isolated radius watch ${status,,} $RUN_ID" >/dev/null || return 1
  git -C "$worktree" push -q origin "HEAD:refs/heads/$REPORT_BRANCH" || return 1
  log "REDACTED_GITHUB_REPORT=https://github.com/gioahmad/city-manager-os/blob/$REPORT_BRANCH/operation-results/isolated-radius-watch/latest.md"
) }

on_error(){
  local rc=$?
  FAIL_LINE="${BASH_LINENO[0]:-$LINENO}"
  log "ERROR: operation failed rc=$rc line=$FAIL_LINE phase=$CURRENT_PHASE"
  trap - ERR
  exit "$rc"
}

on_exit(){
  local rc=$?
  trap - ERR EXIT
  (( rc == 0 )) && FINAL_STATUS="PASS"
  section "ISOLATED RADIUS WATCH: $FINAL_STATUS"
  printf 'STATUS=%s\nPHASE=%s\nFULL_LOCAL_LOG=%s\n' "$FINAL_STATUS" "$CURRENT_PHASE" "$LOG_FILE"
  if publish_report "$FINAL_STATUS" "$rc"; then
    REPORT_RESULT="published"
  else
    REPORT_RESULT="failed"
    log "WARNING: redacted GitHub report could not be published"
  fi
  cleanup
  exit "$rc"
}

trap on_error ERR
trap on_exit EXIT

for cmd in git docker python3 curl flock mktemp install tee; do
  command -v "$cmd" >/dev/null || fail "$cmd is required"
done

mkdir -p "$LOG_DIR" "$STATE_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

exec 9>"$LOCK_FILE"
flock -n 9 || fail "another isolated radius watch operation is active"

CURRENT_PHASE="preflight"
section "PRIVATE ONE-MILE ALERT PILOT"
cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "production repository must be clean"
git fetch -q origin main "+refs/heads/$REPORT_BRANCH:refs/remotes/origin/$REPORT_BRANCH"
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] \
  || fail "local main must match origin/main; run git pull --ff-only"

for name in citymanager-dashboard citymanager-postgis n8n ntfy; do
  [[ "$(docker inspect "$name" --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
    || fail "required container is not running: $name"
done

[[ "$RADIUS_FT" =~ ^[0-9]+$ ]] || fail "CMOS_WATCH_RADIUS_FT must be a whole number"
(( RADIUS_FT >= 1 && RADIUS_FT <= 26400 )) || fail "radius must be between 1 and 26400 feet"
[[ "$SUBSCRIBER_KEY" =~ ^[A-Z0-9_:-]{1,80}$ ]] || fail "invalid private subscriber key"
[[ "$WATCH_KEY" =~ ^[A-Z0-9_:-]{1,80}$ ]] || fail "invalid private watch key"

DB_READY="$(db_at <<'SQL'
SELECT
  to_regclass('public.watch_items') IS NOT NULL
  AND to_regclass('public.subscribers') IS NOT NULL
  AND to_regclass('public.watch_item_recipients') IS NOT NULL
  AND to_regprocedure('public.gis_active_spatial_watch_matches(text,geometry)') IS NOT NULL;
SQL
)"
[[ "$DB_READY" == t ]] || fail "the existing Watchlist spatial contract is not ready"
log "PREFLIGHT PASS: existing PostGIS, Watchlist, Routing, n8n and ntfy are running"

CURRENT_PHASE="private-input"
WATCH_ADDRESS="${CMOS_PRIVATE_WATCH_ADDRESS:-}"
if [[ -z "$WATCH_ADDRESS" ]]; then
  [[ -r /dev/tty ]] || fail "set CMOS_PRIVATE_WATCH_ADDRESS when no terminal is attached"
  printf 'Enter the full private address, including city/state/ZIP (input hidden): ' >/dev/tty
  IFS= read -r -s WATCH_ADDRESS </dev/tty
  printf '\n' >/dev/tty
fi
[[ -n "${WATCH_ADDRESS//[[:space:]]/}" ]] || fail "address is required"

EXISTING_TOPIC="$(db_at <<SQL
SELECT ntfy_topic FROM subscribers WHERE subscriber_id='$SUBSCRIBER_KEY' LIMIT 1;
SQL
)"
if [[ -n "$EXISTING_TOPIC" ]]; then
  NTFY_TOPIC="$EXISTING_TOPIC"
else
  NTFY_TOPIC="cmos-private-$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
fi

HOST_CONFIG="$(mktemp /tmp/cmos-private-radius-watch.XXXXXX.json)"
chmod 600 "$HOST_CONFIG"
printf '%s\0%s\0%s\0%s\0%s\0%s\0' \
  "$WATCH_ADDRESS" "$NTFY_TOPIC" "$SUBSCRIBER_KEY" "$WATCH_KEY" "$WATCH_NAME" "$RADIUS_FT" \
  | python3 -c 'import json,sys; p=sys.stdin.buffer.read().decode().split("\0"); json.dump(dict(address=p[0],topic=p[1],subscriber_key=p[2],watch_key=p[3],watch_name=p[4],radius_ft=int(p[5])),sys.stdout)' \
  > "$HOST_CONFIG"
docker cp "$HOST_CONFIG" "citymanager-dashboard:$CONTAINER_CONFIG"
docker exec citymanager-dashboard chmod 600 "$CONTAINER_CONFIG"

CURRENT_PHASE="transport-test"
TRANSPORT_PAYLOAD="$(mktemp /tmp/cmos-private-ntfy.XXXXXX.json)"
chmod 600 "$TRANSPORT_PAYLOAD"
printf '%s\0%s\0' "$NTFY_TOPIC" "$RUN_ID" \
  | python3 -c 'import json,sys; p=sys.stdin.buffer.read().decode().split("\0"); json.dump({"topic":p[0],"title":"City Manager OS isolated channel test","message":"Private one-mile pilot channel is reachable. This is the only test message.","priority":3,"tags":["test_tube","round_pushpin"]},sys.stdout)' \
  > "$TRANSPORT_PAYLOAD"
curl -fsS --max-time 20 -H 'Content-Type: application/json' \
  --data-binary "@$TRANSPORT_PAYLOAD" "${NTFY_PUBLISH_BASE%/}" >/dev/null
rm -f "$TRANSPORT_PAYLOAD"
TRANSPORT_PAYLOAD=""
TRANSPORT_RESULT="pass-one-isolated-test-message"
log "NTFY TRANSPORT PASS: one test message sent only to the new private topic"

CURRENT_PHASE="configure-watch"
CONFIG_RESULT="$(docker exec -i citymanager-dashboard python - "$CONTAINER_CONFIG" "$RECEIPT_FILE" <<'PY'
import json
import sys
import uuid

from app import db_conn
from geo_resolver import MIN_PRECISE_CONFIDENCE, resolve_payload

config_path, receipt_path = sys.argv[1:]
with open(config_path) as handle:
    config = json.load(handle)

address = str(config['address']).strip()
topic = str(config['topic']).strip()
subscriber_key = str(config['subscriber_key']).strip()
watch_key = str(config['watch_key']).strip()
watch_name = str(config['watch_name']).strip()
radius_ft = float(config['radius_ft'])

with db_conn() as conn:
    resolved = resolve_payload(conn, {'address': address, 'location': {}})
    confidence = float(resolved.get('confidence') or 0)
    if (
        resolved.get('status') != 'RESOLVED'
        or confidence < MIN_PRECISE_CONFIDENCE
        or resolved.get('latitude') is None
        or resolved.get('longitude') is None
    ):
        raise RuntimeError('private address did not resolve to trustworthy local point geometry')

    lat = float(resolved['latitude'])
    lon = float(resolved['longitude'])
    address_line = address.split(',', 1)[0].strip()
    aliases = []
    normalized_address = str(resolved.get('normalized_address') or '').strip()
    normalized_line = normalized_address.split(',', 1)[0].strip()
    if normalized_line and normalized_line.casefold() != address_line.casefold():
        aliases.append(normalized_line)

    with conn.cursor() as cur:
        cur.execute(
            'SELECT subscriber_id FROM subscribers WHERE ntfy_topic=%s AND subscriber_id<>%s',
            (topic, subscriber_key),
        )
        if cur.fetchone():
            raise RuntimeError('generated private topic is already assigned to another subscriber')

        cur.execute(
            '''
            INSERT INTO subscribers(subscriber_id,name,active,ntfy_topic,notes)
            VALUES(%s,%s,true,%s,%s)
            ON CONFLICT(subscriber_id) DO UPDATE SET
              name=EXCLUDED.name,active=true,ntfy_topic=EXCLUDED.ntfy_topic,
              notes=EXCLUDED.notes,updated_at=now()
            RETURNING id
            ''',
            (
                subscriber_key,
                f'{watch_name} Alerts',
                topic,
                'Isolated destination for the private radius pilot; no other watch is assigned by this runner.',
            ),
        )
        subscriber_uuid = cur.fetchone()['id']

        cur.execute(
            '''
            INSERT INTO watch_items(
              watch_id,active,watch_type,display_name,search_term,aliases,
              match_mode,match_field,category,tags,source_filter,alert_category_filter,
              min_priority,address,municipality,county,state,zip,block,lot,qualifier,parcel_id,
              gis_enabled,nearby_enabled,radius_ft,spatial_scope,source_notes,notes,
              geom,spatial_target_geom
            ) VALUES(
              %s,true,'ADDRESS',%s,%s,%s,'CONTAINS','search_text','PRIVATE_PILOT',
              ARRAY['private','radius','pilot'],ARRAY[]::text[],ARRAY[]::text[],1,
              %s,%s,%s,%s,%s,%s,%s,%s,%s,true,true,%s,'RADIUS',%s,%s,
              ST_SetSRID(ST_MakePoint(%s,%s),4326),
              ST_SetSRID(ST_MakePoint(%s,%s),4326)
            )
            ON CONFLICT(watch_id) DO UPDATE SET
              active=true,watch_type='ADDRESS',display_name=EXCLUDED.display_name,
              search_term=EXCLUDED.search_term,aliases=EXCLUDED.aliases,
              match_mode='CONTAINS',match_field='search_text',category='PRIVATE_PILOT',
              tags=EXCLUDED.tags,source_filter=ARRAY[]::text[],alert_category_filter=ARRAY[]::text[],
              min_priority=1,address=EXCLUDED.address,municipality=EXCLUDED.municipality,
              county=EXCLUDED.county,state=EXCLUDED.state,zip=EXCLUDED.zip,
              block=EXCLUDED.block,lot=EXCLUDED.lot,qualifier=EXCLUDED.qualifier,
              parcel_id=EXCLUDED.parcel_id,gis_enabled=true,nearby_enabled=true,
              radius_ft=EXCLUDED.radius_ft,spatial_scope='RADIUS',
              source_notes=EXCLUDED.source_notes,notes=EXCLUDED.notes,
              geom=EXCLUDED.geom,spatial_target_geom=EXCLUDED.spatial_target_geom,updated_at=now()
            RETURNING id
            ''',
            (
                watch_key,
                watch_name,
                address_line,
                aliases,
                address,
                resolved.get('municipality'),
                resolved.get('county'),
                resolved.get('state'),
                resolved.get('zip'),
                resolved.get('block'),
                resolved.get('lot'),
                resolved.get('qualifier'),
                resolved.get('parcel_id'),
                radius_ft,
                'Configured locally; private location is excluded from GitHub reports.',
                'All sources and categories, priority 1+, one-mile spatial match with exact-address text fallback.',
                lon,
                lat,
                lon,
                lat,
            ),
        )
        watch_uuid = cur.fetchone()['id']

        cur.execute(
            'UPDATE watch_item_recipients SET active=false WHERE watch_item_id=%s AND subscriber_id<>%s',
            (watch_uuid, subscriber_uuid),
        )
        cur.execute(
            '''
            INSERT INTO watch_item_recipients(watch_item_id,subscriber_id,active)
            VALUES(%s,%s,true)
            ON CONFLICT(watch_item_id,subscriber_id) DO UPDATE SET active=true
            ''',
            (watch_uuid, subscriber_uuid),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            '''
            SELECT count(*) AS n
            FROM watch_item_recipients r
            JOIN subscribers s ON s.id=r.subscriber_id
            WHERE r.watch_item_id=%s AND r.active AND s.active
            ''',
            (watch_uuid,),
        )
        if cur.fetchone()['n'] != 1:
            raise RuntimeError('private watch does not have exactly one active destination')

        test_alert_id = f'SYSTEM_TEST:PRIVATE_RADIUS:{uuid.uuid4()}'
        cur.execute('BEGIN')
        cur.execute(
            '''
            INSERT INTO alerts(
              alert_id,source,category,subtype,status,event_action,title,message,
              priority,location,tags,search_text,geom
            ) VALUES(
              %s,'SYSTEM_TEST','TEST','PRIVATE_RADIUS','ACTIVE','NEW',
              'Rolled-back private radius test','No notification is sent.',1,'{}'::jsonb,
              ARRAY['test'],%s,ST_SetSRID(ST_MakePoint(%s,%s),4326)
            )
            ''',
            (test_alert_id, address_line, lon, lat),
        )
        cur.execute(
            '''
            SELECT count(*) AS n
            FROM gis_active_spatial_watch_matches(%s,NULL::geometry) m
            WHERE m.watch_item_id=%s AND m.match_type='PROXIMITY'
            ''',
            (test_alert_id, watch_uuid),
        )
        spatial_test = cur.fetchone()['n'] == 1
        conn.rollback()
        if not spatial_test:
            raise RuntimeError('rolled-back spatial match test failed')

        cur.execute(
            '''
            SELECT
              count(*) FILTER (WHERE received_at>=now()-interval '30 days') AS alerts_30d,
              count(*) FILTER (
                WHERE received_at>=now()-interval '30 days' AND upper(category)='TRAFFIC'
              ) AS traffic_30d
            FROM alerts
            WHERE geom IS NOT NULL
              AND ST_DWithin(
                geom::geography,
                ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,
                %s*0.3048
              )
            ''',
            (lon, lat, radius_ft),
        )
        volume = cur.fetchone()

receipt = {
    'status': 'PASS',
    'watch_id': watch_key,
    'subscriber_id': subscriber_key,
    'radius_ft': int(radius_ft),
    'minimum_priority': 1,
    'source_filter': 'ANY',
    'category_filter': 'ANY',
    'exact_address_text_fallback': True,
    'isolated_active_destinations': 1,
    'resolution_method': resolved.get('resolution_method') or resolved.get('method') or 'local_geo_resolver',
    'confidence': round(confidence, 3),
    'rolled_back_spatial_match_test': 'PASS',
    'nearby_geocoded_alerts_30d': int(volume.get('alerts_30d') or 0),
    'nearby_traffic_alerts_30d': int(volume.get('traffic_30d') or 0),
    'private_fields_excluded': ['address', 'coordinates', 'ntfy_topic'],
}
print(json.dumps(receipt, sort_keys=True))
PY
)"
[[ -n "$CONFIG_RESULT" ]] || fail "configuration returned no sanitized receipt"
printf '%s\n' "$CONFIG_RESULT" | python3 -m json.tool > "$RECEIPT_FILE"
chmod 600 "$RECEIPT_FILE"
docker exec citymanager-dashboard rm -f "$CONTAINER_CONFIG"
rm -f "$HOST_CONFIG"
HOST_CONFIG=""
log "WATCH CONFIGURATION PASS: one isolated destination, one-mile radius, all sources/categories, priority 1+"
log "SPATIAL ROUTING TEST PASS: synthetic alert rolled back; no synthetic central notification sent"

CURRENT_PHASE="focused-health"
docker exec citymanager-dashboard python - <<'PY'
import json
import os
import urllib.request

token = os.environ.get('CMOS_AUTOMATION_TOKEN', '').strip()
headers = {'X-CMOS-Automation-Key': token} if token else {}
for path in ('/health', '/api/spatial-watch/release'):
    request = urllib.request.Request('http://127.0.0.1:8000' + path, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
        assert response.status == 200 and body
        if path.startswith('/api/'):
            payload = json.loads(body)
            assert payload['watch_source_of_truth'] == 'watch_items'
print('FOCUSED HEALTH PASS')
PY

CURRENT_PHASE="complete"
section "PRIVATE PILOT READY"
printf 'WATCH_ID=%s\nSUBSCRIBER_ID=%s\nRADIUS_FT=%s\n' "$WATCH_KEY" "$SUBSCRIBER_KEY" "$RADIUS_FT"
printf 'SOURCE_FILTER=ANY\nCATEGORY_FILTER=ANY\nMIN_PRIORITY=1\n'
printf 'BUILDS=NONE\nRESTARTS=NONE\nSCHEMA_CHANGES=NONE\nFULL_E2E=NOT_RUN\n'
if [[ -w /dev/tty ]]; then
  printf '\nPRIVATE NTFY TOPIC (not logged or uploaded): %s\n' "$NTFY_TOPIC" >/dev/tty
  printf 'SUBSCRIBE URL: %s/%s\n' "${NTFY_SUBSCRIBE_BASE%/}" "$NTFY_TOPIC" >/dev/tty
  printf 'A single test message should already be visible.\n' >/dev/tty
fi
