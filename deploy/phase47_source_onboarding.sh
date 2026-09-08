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
  --entrypoint pytest \
  citymanager-dashboard \
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

for _ in $(seq 1 45); do
  if curl -fsS http://127.0.0.1:8090/health >/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS http://127.0.0.1:8090/health >/dev/null
echo "Dashboard health endpoint: PASS"

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

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirect)

def http(path: str, data: dict[str, str] | None = None):
    body = None
    headers = {"X-CMOS-Automation-Key": TOKEN}
    method = "GET"
    if data is not None:
        method = "POST"
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")

conn = psycopg.connect(
    host=os.getenv("DB_HOST", "citymanager-postgis"),
    port=int(os.getenv("DB_PORT", "5432")),
    dbname=os.getenv("DB_NAME", "citymanager"),
    user=os.getenv("DB_USER", "citymanager_app"),
    password=os.environ["DB_PASSWORD"],
    row_factory=dict_row,
)

def row():
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM integrations WHERE integration_key=%s", (KEY,))
        return cur.fetchone()

def cleanup():
    with conn.cursor() as cur:
        cur.execute("DELETE FROM source_health WHERE source_id=%s", (f"INT:{KEY}",))
        cur.execute("DELETE FROM integrations WHERE integration_key=%s", (KEY,))
    conn.commit()

cleanup()

status, html = http("/integrations/onboarding")
assert status == 200, status
assert "Source Onboarding" in html
print("PASS  onboarding page is reachable through private automation auth")

create = {
    "provider_template": "CUSTOM",
    "name": "CMOS #47 E2E Placeholder",
    "integration_key": KEY,
    "category": "EVENTS",
    "adapter_type": "HTTP",
    "endpoint_url": "",
    "method": "GET",
    "auth_type": "NONE",
    "headers_json": "{}",
    "query_json": "{}",
    "request_body": "",
    "parser_kind": "NONE",
    "parser_config_json": "{}",
    "poll_seconds": "86400",
    "timeout_seconds": "15",
    "max_response_bytes": "1000000",
    "allow_redirects": "1",
    "verify_tls": "1",
    "source_owner": "SYSTEM TEST",
    "source_contact": "",
    "access_instructions": "Synthetic #47 acceptance record",
    "geography_scope": "TEST",
    "relevance_keywords": "Weehawken",
    "watch_threshold": "45",
    "alert_threshold": "75",
    "notes": "Synthetic #47 acceptance record",
}
status, _ = http("/integrations/onboarding/create", create)
assert status == 303, status

r = row()
assert r and r["active"] is False
assert (r["endpoint_url"] or "") == ""
assert r["last_test_at"] is None
print("PASS  browser created incomplete inactive placeholder")

status, _ = http(f"/integrations/onboarding/{r['id']}/activate", {})
assert status == 303, status
assert row()["active"] is False
print("PASS  activation blocked before setup/test")

parser_config = {
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
configured = dict(create)
configured.update({
    "provider_template": "SOCRATA",
    "name": "CMOS #47 E2E Test Source",
    "endpoint_url": "https://data.cityofnewyork.us/resource/tvpp-9vvx.json",
    "headers_json": json.dumps({"Accept": "application/json"}),
    "query_json": json.dumps({"$limit": 1}),
    "parser_kind": "JSON_EVENTS",
    "parser_config_json": json.dumps(parser_config),
})
configured.pop("integration_key", None)

before_version = int(r["config_version"])
status, _ = http(f"/integrations/onboarding/{r['id']}/update", configured)
assert status == 303, status
r = row()
assert int(r["config_version"]) == before_version + 1
assert r["last_test_at"] is None
assert r["active"] is False
print("PASS  configuration change incremented version and requires new TEST")

status, html = http(f"/integrations/onboarding/{r['id']}/test", {})
assert status == 200, status
assert "READ-ONLY TEST" in html
r = row()
assert r["last_test_ok"] is True, r["last_test_summary"]
assert int(r["last_test_config_version"]) == int(r["config_version"])
assert int((r["last_test_summary"] or {}).get("items_found") or 0) >= 1
with conn.cursor() as cur:
    cur.execute("SELECT count(*) AS n FROM event_intelligence WHERE source_integration_id=%s", (r["id"],))
    assert cur.fetchone()["n"] == 0
print("PASS  browser TEST fetched, parsed and previewed without storing production events")

status, _ = http(f"/integrations/onboarding/{r['id']}/activate", {})
assert status == 303, status
r = row()
assert r["active"] is True
with conn.cursor() as cur:
    cur.execute("SELECT count(*) AS n FROM integration_activation_audit WHERE integration_id=%s AND action='ACTIVATE'", (r["id"],))
    assert cur.fetchone()["n"] >= 1
print("PASS  tested current configuration activated with audit")

configured["query_json"] = json.dumps({"$limit": 2})
active_version = int(r["config_version"])
status, _ = http(f"/integrations/onboarding/{r['id']}/update", configured)
assert status == 303, status
r = row()
assert r["active"] is False
assert int(r["config_version"]) == active_version + 1
assert r["last_test_ok"] is None
assert r["last_test_config_version"] is None
print("PASS  editing live connection configuration auto-paused and invalidated TEST")

cleanup()
with conn.cursor() as cur:
    cur.execute("SELECT count(*) AS n FROM integrations WHERE integration_key=%s", (KEY,))
    assert cur.fetchone()["n"] == 0
    cur.execute("SELECT count(*) AS n FROM source_health WHERE source_id=%s", (f"INT:{KEY}",))
    assert cur.fetchone()["n"] == 0
print("PASS  synthetic #47 records cleaned")
conn.close()
PY

docker start citymanager-integration-engine >/dev/null
ENGINE_STOPPED=0
echo "#47 controlled web acceptance: PASS"

echo
echo "=== 10. PRE-PROMOTION HEALTH ==="
./deploy/cmos-health
echo "Pre-promotion health: PASS"

echo
echo "=== 11. PROMOTE FEATURE TO MAIN ==="
[ -z "$(git status --porcelain)" ]
FEATURE_HEAD="$(git rev-parse HEAD)"
git switch main
[ "$(git rev-parse HEAD)" = "$BASE" ]
git merge --ff-only "$FEATURE_HEAD"
git push origin main
PROMOTED=1

echo "Production commit: $(git rev-parse HEAD)"
echo "Promotion: PASS"

echo
echo "=== 12. SECURE FULL E2E ==="
./deploy/cmos-e2e-secure

echo
echo "=== 13. FINAL SAFETY / HEALTH ==="
./deploy/security/verify-db-credential-alignment.sh FINAL
./deploy/postgis/verify-backup.sh
./deploy/cmos-health

[ -z "$(git status --porcelain)" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]

trap - EXIT

echo
echo "============================================================"
echo "#47 WEB-MANAGED SOURCE ONBOARDING: PASS"
echo "PRODUCTION COMMIT: $(git rev-parse HEAD)"
echo "PLACEHOLDER CREATE: PASS"
echo "READ-ONLY TEST + NORMALIZED PREVIEW: PASS"
echo "ACTIVATION GATE + AUDIT: PASS"
echo "CONFIG-CHANGE AUTO-PAUSE: PASS"
echo "SECURE E2E: PASS"
echo "HEALTH: PASS"
echo "============================================================"
