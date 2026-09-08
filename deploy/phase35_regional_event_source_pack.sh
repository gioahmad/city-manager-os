#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="36833baf9e115d97a4a3fbab8eb08ff0eacaeddd"
BRANCH="feature/regional-event-source-pack-1"
MIGRATION="deploy/postgis/init/025_regional_event_source_pack.sql"

cd "$REPO"

PRODUCTION_TOUCHED=0
PROMOTED=0
ENGINE_STOPPED=0

cleanup_on_exit() {
  rc=$?
  if [ "$ENGINE_STOPPED" = "1" ]; then
    docker start citymanager-integration-engine >/dev/null 2>&1 || true
  fi

  if [ "$rc" -ne 0 ] && [ "$PROMOTED" = "0" ] && [ "$PRODUCTION_TOUCHED" = "1" ]; then
    echo
    echo "=== #35 FAILURE: DEACTIVATE NEW PACK + RESTORE MAIN APP ==="
    docker exec -i citymanager-postgis sh -lc \
      'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL' || true
UPDATE integrations
SET active=false
WHERE integration_key IN (
  'HUDSON_COUNTY_EVENTS','BERGEN_COUNTY_EVENTS','JERSEY_CITY_CULTURAL_EVENTS',
  'METLIFE_OFFICIAL_EVENTS','JAVITS_OFFICIAL_EVENTS','PRUDENTIAL_OFFICIAL_EVENTS',
  'NJPAC_OFFICIAL_EVENTS','TICKETMASTER_METRO_EVENTS','EVENTBRITE_DISCOVERY_PLACEHOLDER'
);
SQL
    git switch main >/dev/null 2>&1 || true
    git reset --hard origin/main >/dev/null 2>&1 || true
    docker compose -f dashboard/docker-compose.yml build citymanager-dashboard || true
    docker compose -f dashboard/docker-compose.yml up -d --force-recreate \
      citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine || true
  fi

  if [ "$rc" -ne 0 ]; then
    echo
    echo "============================================================"
    echo "#35 REGIONAL EVENT SOURCE PACK: FAIL rc=$rc"
    echo "============================================================"
  fi
  exit "$rc"
}
trap cleanup_on_exit EXIT

health_dashboard() {
  docker exec citymanager-dashboard python -c 'import urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=4); raise SystemExit(0 if r.status==200 else 1)' >/dev/null 2>&1
}

echo "============================================================"
echo "#35 REGIONAL EVENT SOURCE PACK 1"
echo "============================================================"

echo
echo "=== 1. PREFLIGHT ==="
git fetch origin main "$BRANCH"
[ -z "$(git status --porcelain)" ]
[ "$(git branch --show-current)" = "$BRANCH" ]
[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]
[ "$(git merge-base origin/main HEAD)" = "$BASE" ]
echo "Feature HEAD: $(git rev-parse HEAD)"
echo "Base main: $BASE"
echo "Preflight: PASS"

echo
echo "=== 2. STATIC VALIDATION ==="
python3 -m py_compile \
  dashboard/integration_runtime.py \
  dashboard/integration_engine.py \
  dashboard/source_onboarding.py \
  dashboard/integrations_app.py
bash -n "$0"
grep -Fq 'JSONLD_EVENTS' dashboard/integration_runtime.py
grep -Fq 'JSONLD_EVENTS' "$MIGRATION"
grep -Fq 'HUDSON_COUNTY_EVENTS' "$MIGRATION"
grep -Fq 'EVENTBRITE_DISCOVERY_PLACEHOLDER' "$MIGRATION"
echo "Static validation: PASS"

echo
echo "=== 3. BUILD FEATURE IMAGE ==="
docker compose -f dashboard/docker-compose.yml build citymanager-dashboard

echo
echo "=== 4. TARGETED TESTS ==="
docker compose -f dashboard/docker-compose.yml run --rm --no-deps \
  -v "$REPO/dashboard:/src:ro" -w /src -e PYTHONPATH=/src:/app \
  --entrypoint pytest citymanager-dashboard -p no:cacheprovider -q \
  tests/test_event_source_pack.py \
  tests/test_source_onboarding.py \
  tests/test_attention_engine.py \
  tests/test_gis_import.py

echo "Targeted tests: PASS"

echo
echo "=== 5. BACKUP SAFETY GATE ==="
./deploy/postgis/backup.sh
./deploy/postgis/verify-backup.sh
echo "Backup safety gate: PASS"

echo
echo "=== 6. APPLY ADDITIVE SOURCE PACK MIGRATION ==="
PRODUCTION_TOUCHED=1
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$MIGRATION"

echo
echo "=== 7. VERIFY SOURCE REGISTRY ==="
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT integration_key,active,parser_kind,provider_template,endpoint_url
FROM integrations
WHERE integration_key IN (
  'HUDSON_COUNTY_EVENTS','BERGEN_COUNTY_EVENTS','JERSEY_CITY_CULTURAL_EVENTS',
  'METLIFE_OFFICIAL_EVENTS','JAVITS_OFFICIAL_EVENTS','PRUDENTIAL_OFFICIAL_EVENTS',
  'NJPAC_OFFICIAL_EVENTS','TICKETMASTER_METRO_EVENTS','EVENTBRITE_DISCOVERY_PLACEHOLDER'
)
ORDER BY integration_key;

SELECT conname,pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid='integrations'::regclass
  AND conname='integrations_parser_kind_check';
SQL

COUNT="$(docker exec -i citymanager-postgis sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT count(*)
FROM integrations
WHERE integration_key IN (
  'HUDSON_COUNTY_EVENTS','BERGEN_COUNTY_EVENTS','JERSEY_CITY_CULTURAL_EVENTS',
  'METLIFE_OFFICIAL_EVENTS','JAVITS_OFFICIAL_EVENTS','PRUDENTIAL_OFFICIAL_EVENTS',
  'NJPAC_OFFICIAL_EVENTS','TICKETMASTER_METRO_EVENTS','EVENTBRITE_DISCOVERY_PLACEHOLDER'
);
SQL
)"
[ "$COUNT" = "9" ]
echo "Source registry: 9/9 PASS"

echo
echo "=== 8. DEPLOY FEATURE BUILD ==="
docker compose -f dashboard/docker-compose.yml up -d --force-recreate \
  citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine
for _ in $(seq 1 45); do
  health_dashboard && break
  sleep 2
done
health_dashboard
echo "Dashboard internal health: PASS"

echo
echo "=== 9. CONTROLLED READ-ONLY TEST + SILENT BASELINE ==="
docker stop citymanager-integration-engine >/dev/null
ENGINE_STOPPED=1

docker exec -i citymanager-dashboard python - <<'PY'
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

import psycopg
from psycopg.rows import dict_row

from integration_engine import load_integration, run_integration

BASE = "http://127.0.0.1:8000"
TOKEN = os.environ.get("CMOS_AUTOMATION_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("CMOS_AUTOMATION_TOKEN is required")

DB = dict(
    host=os.getenv("DB_HOST", "citymanager-postgis"),
    port=int(os.getenv("DB_PORT", "5432")),
    dbname=os.getenv("DB_NAME", "citymanager"),
    user=os.getenv("DB_USER", "citymanager_app"),
    password=os.environ["DB_PASSWORD"],
    row_factory=dict_row,
)

CANDIDATES = [
    "HUDSON_COUNTY_EVENTS",
    "BERGEN_COUNTY_EVENTS",
    "JERSEY_CITY_CULTURAL_EVENTS",
    "METLIFE_OFFICIAL_EVENTS",
    "JAVITS_OFFICIAL_EVENTS",
    "PRUDENTIAL_OFFICIAL_EVENTS",
    "NJPAC_OFFICIAL_EVENTS",
]


def one(sql: str, params=()):
    with psycopg.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def request(path: str, data: dict[str, str] | None = None):
    headers = {"X-CMOS-Automation-Key": TOKEN}
    payload = None
    method = "GET"
    if data is not None:
        payload = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST"
    req = urllib.request.Request(BASE + path, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def event_count(source_id):
    return one("SELECT count(*) AS n FROM event_intelligence WHERE source_integration_id=%s", (source_id,))["n"]


def test_and_maybe_activate(key: str):
    row = one("SELECT * FROM integrations WHERE integration_key=%s", (key,))
    if not row:
        raise AssertionError(f"missing source {key}")

    before = event_count(row["id"])
    status, body = request(f"/integrations/onboarding/{row['id']}/test", {})
    if status != 200:
        print(f"{key}: TEST HTTP {status}; LEFT PAUSED")
        return

    row = one("SELECT * FROM integrations WHERE integration_key=%s", (key,))
    after = event_count(row["id"])
    if after != before:
        raise AssertionError(f"{key}: browser TEST stored production events")

    summary = row.get("last_test_summary") or {}
    if isinstance(summary, str):
        summary = json.loads(summary)
    items = int(summary.get("items_found") or 0)
    print(
        f"{key}: test_ok={row['last_test_ok']} http={summary.get('http_status')} "
        f"items={items} bytes={summary.get('response_bytes')} error={summary.get('error')}"
    )

    if not row["last_test_ok"] or items <= 0:
        print(f"{key}: LEFT PAUSED (no parseable current events)")
        return

    integration = load_integration(integration_key=key)
    outcome = run_integration(
        integration,
        run_type="MANUAL",
        parse_and_store=True,
        suppress_alerts=True,
    )
    if not outcome.get("ok"):
        raise AssertionError(f"{key}: silent baseline failed: {outcome.get('error')}")

    pending = one(
        "SELECT count(*) AS n FROM event_intelligence WHERE source_integration_id=%s AND alert_pending=true",
        (row["id"],),
    )["n"]
    if pending != 0:
        raise AssertionError(f"{key}: silent baseline produced pending alerts")

    status, _ = request(f"/integrations/onboarding/{row['id']}/activate", {})
    if status != 200:
        raise AssertionError(f"{key}: activation HTTP {status}")
    active = one("SELECT active FROM integrations WHERE id=%s", (row["id"],))["active"]
    if not active:
        raise AssertionError(f"{key}: activation did not persist")
    print(f"{key}: SILENT BASELINE + ACTIVE")


for key in CANDIDATES:
    test_and_maybe_activate(key)

# Ticketmaster is valuable but must remain NEEDS SETUP until its secret exists.
tm = one("SELECT * FROM integrations WHERE integration_key='TICKETMASTER_METRO_EVENTS'")
if not tm:
    raise AssertionError("Ticketmaster source template missing")
if os.getenv("TICKETMASTER_API_KEY", "").strip():
    test_and_maybe_activate("TICKETMASTER_METRO_EVENTS")
else:
    print("TICKETMASTER_METRO_EVENTS: NEEDS SETUP (TICKETMASTER_API_KEY missing)")
    if tm["active"]:
        raise AssertionError("Ticketmaster active without required key")

# Eventbrite broad discovery is intentionally a documented placeholder.
eb = one("SELECT * FROM integrations WHERE integration_key='EVENTBRITE_DISCOVERY_PLACEHOLDER'")
assert eb and not eb["active"] and eb["parser_kind"] == "NONE"
print("EVENTBRITE_DISCOVERY_PLACEHOLDER: REGISTERED / PAUSED BY DESIGN")

print("SOURCE PACK CONTROLLED ACCEPTANCE: PASS")
PY

docker start citymanager-integration-engine >/dev/null
ENGINE_STOPPED=0

echo
echo "=== 10. POST-SEED SOURCE STATE ==="
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT integration_key,active,parser_kind,last_test_ok,last_test_at,
       COALESCE(last_test_summary->>'items_found','') AS test_items,
       COALESCE(last_test_summary->>'error','') AS test_error
FROM integrations
WHERE integration_key IN (
  'HUDSON_COUNTY_EVENTS','BERGEN_COUNTY_EVENTS','JERSEY_CITY_CULTURAL_EVENTS',
  'METLIFE_OFFICIAL_EVENTS','JAVITS_OFFICIAL_EVENTS','PRUDENTIAL_OFFICIAL_EVENTS',
  'NJPAC_OFFICIAL_EVENTS','TICKETMASTER_METRO_EVENTS','EVENTBRITE_DISCOVERY_PLACEHOLDER'
)
ORDER BY integration_key;

SELECT i.integration_key,
       count(e.id) AS stored,
       count(e.id) FILTER (WHERE e.active) AS active_events,
       count(e.id) FILTER (WHERE e.alert_pending) AS pending_alerts,
       min(e.starts_at) FILTER (WHERE e.active) AS next_start,
       max(e.starts_at) FILTER (WHERE e.active) AS latest_start
FROM integrations i
LEFT JOIN event_intelligence e ON e.source_integration_id=i.id
WHERE i.integration_key IN (
  'HUDSON_COUNTY_EVENTS','BERGEN_COUNTY_EVENTS','JERSEY_CITY_CULTURAL_EVENTS',
  'METLIFE_OFFICIAL_EVENTS','JAVITS_OFFICIAL_EVENTS','PRUDENTIAL_OFFICIAL_EVENTS',
  'NJPAC_OFFICIAL_EVENTS','TICKETMASTER_METRO_EVENTS'
)
GROUP BY i.integration_key
ORDER BY i.integration_key;
SQL

echo
echo "=== 11. FEATURE HEALTH ==="
./deploy/cmos-health

echo
echo "=== 12. PROMOTE VERIFIED FEATURE ==="
git switch main
git merge --ff-only "$BRANCH"
git push origin main
PROMOTED=1
echo "Promoted main: $(git rev-parse HEAD)"

echo
echo "=== 13. POST-PROMOTION REGRESSION ==="
./deploy/cmos-e2e-secure
./deploy/security/verify-db-credential-alignment.sh FINAL
./deploy/postgis/verify-backup.sh
./deploy/cmos-health

echo
echo "============================================================"
echo "#35 REGIONAL EVENT SOURCE PACK 1: PASS"
echo "SOURCE REGISTRY: 9/9 PASS"
echo "JSON-LD PARSER: PASS"
echo "READ-ONLY TEST GATE: PASS"
echo "SILENT BASELINE: PASS"
echo "CROSS-SOURCE ALERT DEDUPE: INSTALLED"
echo "SECURE E2E: PASS"
echo "HEALTH: PASS"
echo "============================================================"
