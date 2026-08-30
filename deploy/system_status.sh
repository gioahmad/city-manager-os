#!/usr/bin/env bash

# City Manager OS - single-shot operational status report
# Safe to paste into support: does not print database passwords or API keys.

set -u

ROOT="/opt/city-manager-os"
DASH_URL="${DASHBOARD_URL:-http://100.94.203.47:8090}"

section() {
  printf '\n============================================================\n'
  printf '%s\n' "$1"
  printf '============================================================\n'
}

section "CITY MANAGER OS STATUS"
date
hostname

section "GIT"
cd "$ROOT" 2>/dev/null || { echo "ERROR: $ROOT not found"; exit 1; }
printf 'Branch: '
git branch --show-current 2>/dev/null || true
printf 'Commit: '
git rev-parse --short HEAD 2>/dev/null || true
printf 'Origin/main: '
git rev-parse --short origin/main 2>/dev/null || echo "not fetched"
echo "Working tree:"
git status --short 2>/dev/null || true

section "CORE CONTAINERS"
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' \
  | { head -n 1; grep -E 'citymanager|n8n|ntfy' || true; }

section "DASHBOARD ROUTES"
for p in \
  / \
  /schedule \
  /alerts \
  /modules \
  /issues \
  /watchlist \
  /subscribers \
  /routing \
  /source-health \
  /deliveries
  do
    code=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "$DASH_URL$p" 2>/dev/null || echo ERR)
    printf '%-20s %s\n' "$p" "$code"
  done

section "DATABASE OBJECTS + COUNTS"
ENV_FILE="$ROOT/deploy/postgis/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a

  docker exec citymanager-postgis \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -At -F ' | ' -c "
WITH wanted(name) AS (
  VALUES
    ('alerts'),
    ('watch_items'),
    ('subscribers'),
    ('watch_item_recipients'),
    ('deliveries'),
    ('source_health'),
    ('issues'),
    ('operational_events')
)
SELECT name,
       CASE WHEN to_regclass('public.' || name) IS NULL THEN 'MISSING' ELSE 'PRESENT' END
FROM wanted
ORDER BY name;
" 2>/dev/null || echo "ERROR: database object check failed"

  echo
  docker exec citymanager-postgis \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
SELECT
  (SELECT count(*) FROM alerts) AS alerts,
  (SELECT count(*) FROM watch_items WHERE active) AS active_watch_items,
  (SELECT count(*) FROM subscribers WHERE active) AS active_subscribers,
  (SELECT count(*) FROM watch_item_recipients WHERE active) AS active_routes,
  (SELECT count(*) FROM deliveries) AS deliveries,
  (SELECT count(*) FROM issues WHERE status NOT IN ('RESOLVED','CLOSED')) AS open_issues;
" 2>/dev/null || echo "ERROR: core count query failed"

  echo
  echo "Source health:"
  docker exec citymanager-postgis \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
SELECT source_id,
       status,
       last_success_at AT TIME ZONE 'America/New_York' AS last_success_et,
       last_event_at AT TIME ZONE 'America/New_York' AS last_event_et,
       CASE WHEN last_error IS NULL OR last_error = '' THEN '' ELSE left(last_error,120) END AS last_error
FROM source_health
ORDER BY source_id;
" 2>/dev/null || echo "ERROR: source_health query failed"

  echo
  echo "Subscribers + active route counts:"
  docker exec citymanager-postgis \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
SELECT s.name,
       s.active,
       s.ntfy_topic,
       count(wir.id) FILTER (WHERE wir.active) AS active_routes
FROM subscribers s
LEFT JOIN watch_item_recipients wir ON wir.subscriber_id = s.id
GROUP BY s.id, s.name, s.active, s.ntfy_topic
ORDER BY s.active DESC, s.name;
" 2>/dev/null || echo "ERROR: subscriber query failed"

  echo
  echo "Recent alerts:"
  docker exec citymanager-postgis \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
SELECT source, event_action, municipality, title,
       received_at AT TIME ZONE 'America/New_York' AS received_et
FROM alerts
ORDER BY received_at DESC
LIMIT 10;
" 2>/dev/null || echo "ERROR: alert query failed"
else
  echo "ERROR: $ENV_FILE not found"
fi

section "ACTIVE N8N WORKFLOWS"
N8N_DB=$(docker inspect n8n \
  --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)/database.sqlite

if [ -r "$N8N_DB" ]; then
  python3 - "$N8N_DB" <<'PY'
import sqlite3, sys
p = sys.argv[1]
con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
try:
    rows = con.execute("""
        SELECT id, name, active, updatedAt
        FROM workflow_entity
        WHERE active = 1
        ORDER BY name
    """).fetchall()
    print(f"Active workflows: {len(rows)}")
    for r in rows:
        print(f"{r['id']} | {r['name']} | updated {r['updatedAt']}")
finally:
    con.close()
PY
else
  echo "ERROR: could not locate readable n8n database"
fi

section "CANONICAL CITY MANAGER OS WORKFLOWS"
find "$ROOT/workflows" -maxdepth 2 -type f -name '*.json' -printf '%P\n' 2>/dev/null | sort

section "STATUS COMPLETE"
echo "Paste this entire report back into ChatGPT."
