#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="4657e98c3dd6bdf037ae3d98c8ce8b8a1d684bcb"
BRANCH="feature/web-managed-source-onboarding"
MIGRATION="deploy/postgis/init/024_web_managed_source_onboarding.sql"

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
    echo "=== #47 FAILURE: RESTORING MAIN APPLICATION ==="
    docker exec -i citymanager-postgis sh -lc \
      'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL' || true
DROP TRIGGER IF EXISTS trg_integrations_onboarding_guard ON integrations;
DROP FUNCTION IF EXISTS cmos_integration_onboarding_guard();
SQL
    git switch main >/dev/null 2>&1 || true
    git reset --hard origin/main >/dev/null 2>&1 || true
    docker compose -f dashboard/docker-compose.yml build citymanager-dashboard >/dev/null 2>&1 || true
    docker compose -f dashboard/docker-compose.yml up -d --force-recreate \
      citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine >/dev/null 2>&1 || true
    echo "Rollback attempt complete. Existing additive #47 columns/table were left in place, but the activation trigger was removed."
  fi

  if [ "$rc" -ne 0 ]; then
    echo
    echo "============================================================"
    echo "#47 WEB-MANAGED SOURCE ONBOARDING: FAIL rc=$rc"
    echo "============================================================"
  fi
  exit "$rc"
}
trap cleanup_on_exit EXIT

echo "============================================================"
echo "#47 WEB-MANAGED SOURCE ONBOARDING"
echo "============================================================"

echo
echo "=== 1. PREFLIGHT ==="
git fetch origin main "$BRANCH"

[ -z "$(git status --porcelain)" ]
[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]
[ "$(git merge-base origin/main HEAD)" = "$BASE" ]
[ "$(git branch --show-current)" = "$BRANCH" ]

echo "Branch: $(git branch --show-current)"
echo "Feature HEAD: $(git rev-parse HEAD)"
echo "Base main: $(git rev-parse origin/main)"
echo "Preflight: PASS"

echo
echo "=== 2. STATIC VALIDATION ==="
python3 -m py_compile \
  dashboard/source_onboarding.py \
  dashboard/phase3_app.py

python3 - <<'PY'
from pathlib import Path

required = [
    "provider_template",
    "last_test_ok",
    "last_test_config_version",
    "integration_activation_audit",
    "trg_integrations_onboarding_guard",
]
sql = Path("deploy/postgis/init/024_web_managed_source_onboarding.sql").read_text()
missing = [item for item in required if item not in sql]
if missing:
    raise SystemExit("Migration validation missing: " + ", ".join(missing))

print("Python/migration static validation: PASS")
PY

echo
echo "=== 3. BUILD FEATURE IMAGE ==="
docker compose -f dashboard/docker-compose.yml build citymanager-dashboard

echo
echo "=== 4. TARGETED TESTS ==="
docker compose -f dashboard/docker-compose.yml run --rm --no-deps \
  -v "$REPO/dashboard:/src:ro" \
  -w /src \
  --entrypoint python \
  citymanager-dashboard \
  -c 'from jinja2 import Environment,FileSystemLoader; Environment(loader=FileSystemLoader("templates")).get_template("source_onboarding.html"); print("Jinja template validation: PASS")'

docker compose -f dashboard/docker-compose.yml run --rm --no-deps \
  -v "$REPO/dashboard:/src:ro" \
  -w /src \
  -e PYTHONPATH=/src:/app \
  --entrypoint pytest \
  citymanager-dashboard \
  -p no:cacheprovider \
  -q \
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
echo "=== 6. APPLY ADDITIVE #47 MIGRATION ==="
PRODUCTION_TOUCHED=1
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < "$MIGRATION"

echo
echo "=== 7. VERIFY #47 DATABASE OBJECTS ==="
COLUMN_COUNT="$(
  docker exec -i citymanager-postgis sh -lc \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT count(*)
FROM information_schema.columns
WHERE table_schema='public'
  AND table_name='integrations'
  AND column_name IN (
    'provider_template',
    'source_owner',
    'source_contact',
    'access_instructions',
    'geography_scope',
    'relevance_keywords',
    'attention_config',
    'map_config',
    'config_version',
    'last_test_at',
    'last_test_ok',
    'last_test_config_version',
    'last_test_summary',
    'activation_override_at',
    'activation_override_by',
    'activation_override_reason'
  );
SQL
)"
[ "$COLUMN_COUNT" = "16" ]

AUDIT_TABLE="$(
  docker exec -i citymanager-postgis sh -lc \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT to_regclass('public.integration_activation_audit') IS NOT NULL;
SQL
)"
[ "$AUDIT_TABLE" = "t" ]

TRIGGER_COUNT="$(
  docker exec -i citymanager-postgis sh -lc \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT count(*)
FROM pg_trigger
WHERE tgname='trg_integrations_onboarding_guard'
  AND NOT tgisinternal;
SQL
)"
[ "$TRIGGER_COUNT" = "1" ]

echo "Database columns: $COLUMN_COUNT/16"
echo "Activation audit table: PASS"
echo "Activation guard trigger: PASS"

echo
echo "=== 8. DEPLOY FEATURE BUILD ==="
docker compose -f dashboard/docker-compose.yml up -d --force-recreate \
  citymanager-dashboard \
  citymanager-staff \
  citymanager-ops-engine \
  citymanager-integration-engine

dashboard_health() {
  docker exec citymanager-dashboard python -c 'import urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=4); raise SystemExit(0 if r.status == 200 else 1)' >/dev/null 2>&1
}

for _ in $(seq 1 45); do
  if dashboard_health; then
    break
  fi
  sleep 2
done
dashboard_health
echo "Dashboard internal health endpoint: PASS"

echo
echo "=== 9. #47 CONTROLLED WEB ACCEPTANCE ==="
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

BASE = "http://127.0.0.1:8000"
KEY = "CMOS47_E2E"
TOKEN = os.environ.get("CMOS_AUTOMATION_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("CMOS_AUTOMATION_TOKEN is required for controlled web acceptance")

DB = dict(
    host=os.getenv("DB_HOST", "citymanager-postgis"),
    port=int(os.getenv("DB_PORT", "5432")),
    dbname=os.getenv("DB_NAME", "citymanager"),
    user=os.getenv("DB_USER", "citymanager_app"),
    password=os.environ["DB_PASSWORD"],
    row_factory=dict_row,
)


def request(path: str, data: dict[str, str] | None = None):
    headers = {"X-CMOS-Automation-Key": TOKEN}
    body = None
    method = "GET"
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.geturl(), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.geturl(), exc.read().decode("utf-8", errors="replace")


def one(sql: str, params=()):
    with psycopg.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params=()):
    with psycopg.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()


execute("DELETE FROM integrations WHERE integration_key=%s", (KEY,))

status, _, _ = request(
    "/integrations/onboarding/create",
    {
        "name": "CMOS #47 E2E Placeholder",
        "integration_key": KEY,
        "provider_template": "GENERIC_JSON",
        "category": "EVENTS",
        "endpoint_url": "",
        "method": "GET",
        "auth_type": "NONE",
        "parser_kind": "NONE",
        "headers_json": "{}",
        "query_json": "{}",
        "request_body": "",
        "parser_config_json": "{}",
        "poll_seconds": "900",
        "timeout_seconds": "15",
        "max_response_bytes": "1000000",
        "allow_redirects": "1",
        "verify_tls": "1",
        "source_owner": "System Test",
        "source_contact": "",
        "access_instructions": "",
        "geography_scope": "Weehawken",
        "relevance_keywords": "Weehawken, Lincoln Tunnel",
        "attention_config_json": "{}",
        "map_config_json": "{}",
        "notes": "Synthetic #47 acceptance source",
    },
)
assert status == 200, status
row = one("SELECT * FROM integrations WHERE integration_key=%s", (KEY,))
assert row and not row["active"] and row["endpoint_url"] == ""
print("PASS placeholder source created without endpoint")

status, _, _ = request(f"/integrations/onboarding/{row['id']}/activate", {})
assert status == 200, status
row = one("SELECT * FROM integrations WHERE integration_key=%s", (KEY,))
assert not row["active"]
print("PASS incomplete placeholder activation blocked")

status, _, _ = request(
    f"/integrations/onboarding/{row['id']}/update",
    {
        "name": row["name"],
        "integration_key": KEY,
        "provider_template": "SOCRATA",
        "category": "EVENTS",
        "adapter_type": "HTTP",
        "endpoint_url": "https://data.cityofnewyork.us/resource/tvpp-9vvx.json",
        "method": "GET",
        "auth_type": "NONE",
        "username_env": "",
        "password_env": "",
        "token_env": "",
        "key_env": "",
        "key_name": "",
        "headers_json": '{"Accept":"application/json"}',
        "query_json": '{"$limit":2}',
        "request_body": "",
        "parser_kind": "JSON_EVENTS",
        "parser_config_json": json.dumps(
            {
                "list_path": "",
                "mapping": {
                    "id": "event_id",
                    "title": "event_name",
                    "start": "start_date_time",
                    "end": "end_date_time",
                    "event_type": "event_type",
                    "venue": "event_location",
                    "municipality": "event_borough",
                    "road_impact": "street_closure_type",
                },
                "defaults": {
                    "municipality": "Manhattan",
                    "state": "NY",
                    "default_timezone": "America/New_York",
                },
            }
        ),
        "poll_seconds": "3600",
        "timeout_seconds": "20",
        "max_response_bytes": "1000000",
        "allow_redirects": "1",
        "verify_tls": "1",
        "source_owner": "System Test",
        "source_contact": "",
        "access_instructions": "",
        "geography_scope": "NYC / Metro",
        "relevance_keywords": "Manhattan, Lincoln Tunnel",
        "attention_config_json": '{"minimum_score":45}',
        "map_config_json": '{"map_capable":true,"default_layer":"events"}',
        "notes": "Synthetic #47 acceptance source",
    },
)
assert status == 200, status
row = one("SELECT * FROM integrations WHERE integration_key=%s", (KEY,))
assert row["config_version"] >= 2 and row["last_test_ok"] is None
print("PASS source configured and old test state invalidated")

before_events = one("SELECT count(*) AS n FROM event_intelligence WHERE source_integration_id=%s", (row["id"],))["n"]
status, _, body = request(f"/integrations/onboarding/{row['id']}/test", {})
assert status == 200, status
assert "TEST RESULT" in body and "Normalized preview" in body
row = one("SELECT * FROM integrations WHERE integration_key=%s", (KEY,))
after_events = one("SELECT count(*) AS n FROM event_intelligence WHERE source_integration_id=%s", (row["id"],))["n"]
assert row["last_test_ok"] is True
assert row["last_test_config_version"] == row["config_version"]
assert before_events == after_events == 0
print("PASS read-only TEST saved current success and stored no production events")

status, _, _ = request(f"/integrations/onboarding/{row['id']}/activate", {})
assert status == 200, status
row = one("SELECT * FROM integrations WHERE integration_key=%s", (KEY,))
assert row["active"] is True
assert one("SELECT count(*) AS n FROM integration_activation_audit WHERE integration_id=%s AND action='ACTIVATE'", (row["id"],))["n"] >= 1
print("PASS successful current TEST unlocked audited activation")

status, _, _ = request(
    f"/integrations/onboarding/{row['id']}/update",
    {
        "name": row["name"],
        "integration_key": KEY,
        "provider_template": "SOCRATA",
        "category": "EVENTS",
        "adapter_type": "HTTP",
        "endpoint_url": "https://data.cityofnewyork.us/resource/tvpp-9vvx.json",
        "method": "GET",
        "auth_type": "NONE",
        "username_env": "",
        "password_env": "",
        "token_env": "",
        "key_env": "",
        "key_name": "",
        "headers_json": '{"Accept":"application/json"}',
        "query_json": '{"$limit":1}',
        "request_body": "",
        "parser_kind": "JSON_EVENTS",
        "parser_config_json": json.dumps(row["parser_config"]),
        "poll_seconds": "3600",
        "timeout_seconds": "20",
        "max_response_bytes": "1000000",
        "allow_redirects": "1",
        "verify_tls": "1",
        "source_owner": "System Test",
        "source_contact": "",
        "access_instructions": "",
        "geography_scope": "NYC / Metro",
        "relevance_keywords": "Manhattan, Lincoln Tunnel",
        "attention_config_json": json.dumps(row["attention_config"]),
        "map_config_json": json.dumps(row["map_config"]),
        "notes": "Synthetic #47 acceptance source",
    },
)
assert status == 200, status
row = one("SELECT * FROM integrations WHERE integration_key=%s", (KEY,))
assert row["active"] is False
assert row["last_test_ok"] is None
print("PASS config change auto-paused source and invalidated test")

status, _, _ = request(
    f"/integrations/onboarding/{row['id']}/activate-override",
    {"reason": "CMOS #47 controlled acceptance override"},
)
assert status == 200, status
row = one("SELECT * FROM integrations WHERE integration_key=%s", (KEY,))
assert row["active"] is True
assert row["activation_override_reason"] == "CMOS #47 controlled acceptance override"
assert one("SELECT count(*) AS n FROM integration_activation_audit WHERE integration_id=%s AND action='OVERRIDE_ACTIVATE'", (row["id"],))["n"] >= 1
print("PASS executive override recorded with reason")

execute("DELETE FROM integrations WHERE integration_key=%s", (KEY,))
assert one("SELECT count(*) AS n FROM integrations WHERE integration_key=%s", (KEY,))["n"] == 0
print("PASS synthetic #47 records cleaned")
PY

docker start citymanager-integration-engine >/dev/null
ENGINE_STOPPED=0

echo
echo "=== 10. FEATURE HEALTH ==="
./deploy/cmos-health

echo
echo "=== 11. PROMOTE VERIFIED FEATURE ==="
git switch main
git merge --ff-only "$BRANCH"
git push origin main
PROMOTED=1

echo "Promoted main: $(git rev-parse HEAD)"

echo
echo "=== 12. POST-PROMOTION SECURE E2E ==="
./deploy/cmos-e2e-secure
./deploy/security/verify-db-credential-alignment.sh FINAL
./deploy/postgis/verify-backup.sh
./deploy/cmos-health

echo
echo "============================================================"
echo "#47 WEB-MANAGED SOURCE ONBOARDING: PASS"
echo "PLACEHOLDER CREATE: PASS"
echo "READ-ONLY TEST + NORMALIZED PREVIEW: PASS"
echo "ACTIVATION GATE + AUDIT: PASS"
echo "CONFIG-CHANGE AUTO-PAUSE: PASS"
echo "SECURE E2E: PASS"
echo "HEALTH: PASS"
echo "============================================================"
