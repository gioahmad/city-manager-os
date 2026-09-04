#!/usr/bin/env bash
set -Eeuo pipefail

RESULT="${1:-}"
MESSAGE="${2:-}"
RUN_ID="${3:-$(date '+%Y%m%d%H%M%S')}"

case "$RESULT" in
  SUCCESS)
    TITLE="City Manager OS GIS Refresh Complete"
    PRIORITY="3"
    ALERT_PRIORITY="2"
    TAGS="white_check_mark,map"
    ;;
  FAILURE)
    TITLE="City Manager OS GIS Refresh Failed"
    PRIORITY="5"
    ALERT_PRIORITY="5"
    TAGS="warning,map"
    ;;
  *)
    echo "Usage: notify_gis_refresh.sh SUCCESS|FAILURE message [run_id]" >&2
    exit 2
    ;;
esac

[[ -n "$MESSAGE" ]] || MESSAGE="Hudson GIS refresh ${RESULT,,}."

for cmd in docker curl; do
  command -v "$cmd" >/dev/null || { echo "ERROR: $cmd is required" >&2; exit 1; }
done

ROUTE="$(docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qAt -F "|"' <<'SQL'
SELECT id::text, ntfy_topic
FROM subscribers
WHERE subscriber_id='GIO_CATCHALL'
  AND active=true
  AND nullif(trim(ntfy_topic),'') IS NOT NULL
LIMIT 1;
SQL
)"

[[ -n "$ROUTE" ]] || { echo "ERROR: active GIO_CATCHALL subscriber route not found" >&2; exit 1; }
IFS='|' read -r SUBSCRIBER_UUID NTFY_TOPIC <<< "$ROUTE"

ALERT_KEY="GIS_REFRESH:${RUN_ID}:${RESULT}"
DELIVERY_KEY="${ALERT_KEY}:${SUBSCRIBER_UUID}"

ALERT_UUID="$(docker exec -i \
  -e GIS_ALERT_KEY="$ALERT_KEY" \
  -e GIS_RUN_ID="$RUN_ID" \
  -e GIS_RESULT="$RESULT" \
  -e GIS_TITLE="$TITLE" \
  -e GIS_MESSAGE="$MESSAGE" \
  -e GIS_ALERT_PRIORITY="$ALERT_PRIORITY" \
  citymanager-postgis sh -lc '
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v alert_key="$GIS_ALERT_KEY" \
  -v run_id="$GIS_RUN_ID" \
  -v result="$GIS_RESULT" \
  -v title="$GIS_TITLE" \
  -v message="$GIS_MESSAGE" \
  -v priority="$GIS_ALERT_PRIORITY" -qAt
' <<'SQL'
INSERT INTO alerts(
  alert_id,source,source_event_id,category,subtype,status,event_action,
  title,message,priority,municipality,location,tags,observed_at,received_at,
  metadata,search_text
)
VALUES (
  :'alert_key','GIS_REFRESH',:'run_id','SYSTEM','GIS_REFRESH','ACTIVE','UPDATE',
  :'title',:'message',:'priority'::integer,'Weehawken','{}'::jsonb,
  ARRAY['gis','refresh','system'],now(),now(),
  jsonb_build_object('run_id',:'run_id','result',:'result'),
  'GIS_REFRESH SYSTEM GIS REFRESH ' || :'result' || ' ' || :'title' || ' ' || :'message'
)
ON CONFLICT (alert_id) DO UPDATE
SET title=EXCLUDED.title,
    message=EXCLUDED.message,
    priority=EXCLUDED.priority,
    received_at=now(),
    metadata=EXCLUDED.metadata,
    search_text=EXCLUDED.search_text,
    updated_at=now()
RETURNING id::text;
SQL
)"

[[ -n "$ALERT_UUID" ]] || { echo "ERROR: unable to create GIS refresh alert record" >&2; exit 1; }

DELIVERY_UUID="$(docker exec -i \
  -e GIS_DELIVERY_KEY="$DELIVERY_KEY" \
  -e GIS_ALERT_UUID="$ALERT_UUID" \
  -e GIS_SUBSCRIBER_UUID="$SUBSCRIBER_UUID" \
  -e GIS_NTFY_TOPIC="$NTFY_TOPIC" \
  citymanager-postgis sh -lc '
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v delivery_key="$GIS_DELIVERY_KEY" \
  -v alert_uuid="$GIS_ALERT_UUID" \
  -v subscriber_uuid="$GIS_SUBSCRIBER_UUID" \
  -v ntfy_topic="$GIS_NTFY_TOPIC" -qAt
' <<'SQL'
INSERT INTO deliveries(
  delivery_key,alert_id,subscriber_id,ntfy_topic,status,attempted_at,
  matched_watch_ids,match_reasons
)
VALUES (
  :'delivery_key',:'alert_uuid'::uuid,:'subscriber_uuid'::uuid,:'ntfy_topic',
  'PENDING',now(),'[]'::jsonb,'["GIS_REFRESH_SYSTEM"]'::jsonb
)
ON CONFLICT (delivery_key) DO NOTHING
RETURNING id::text;
SQL
)"

if [[ -z "$DELIVERY_UUID" ]]; then
  echo "GIS refresh notification already delivered or attempted for ${ALERT_KEY}; deduplicated."
  exit 0
fi

NTFY_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{if .IPAddress}}{{.IPAddress}} {{end}}{{end}}' ntfy 2>/dev/null | awk '{print $1}')"
[[ -n "$NTFY_IP" ]] || { echo "ERROR: unable to resolve ntfy container IP" >&2; exit 1; }

set +e
NTFY_RESPONSE="$(curl -fsS --max-time 30 \
  -H "Title: ${TITLE}" \
  -H "Priority: ${PRIORITY}" \
  -H "Tags: ${TAGS}" \
  --data-binary "$MESSAGE" \
  "http://${NTFY_IP}:80/${NTFY_TOPIC}" 2>&1)"
RC=$?
set -e

if (( RC == 0 )); then
  docker exec -i -e GIS_DELIVERY_UUID="$DELIVERY_UUID" citymanager-postgis sh -lc '
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v delivery_uuid="$GIS_DELIVERY_UUID"
' <<'SQL' >/dev/null
UPDATE deliveries
SET status='SENT',sent_at=now(),error_message=NULL
WHERE id=:'delivery_uuid'::uuid;
SQL
  echo "GIS refresh notification sent through dynamic subscriber topic ${NTFY_TOPIC}."
else
  docker exec -i \
    -e GIS_DELIVERY_UUID="$DELIVERY_UUID" \
    -e GIS_DELIVERY_ERROR="$NTFY_RESPONSE" \
    citymanager-postgis sh -lc '
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v delivery_uuid="$GIS_DELIVERY_UUID" -v delivery_error="$GIS_DELIVERY_ERROR"
' <<'SQL' >/dev/null || true
UPDATE deliveries
SET status='FAILED',error_message=left(:'delivery_error',1000)
WHERE id=:'delivery_uuid'::uuid;
SQL
  echo "ERROR: ntfy delivery failed: ${NTFY_RESPONSE}" >&2
  exit "$RC"
fi
