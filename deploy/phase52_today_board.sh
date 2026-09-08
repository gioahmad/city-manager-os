#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="0e0bb3147f196f9c440017eae635a0bda988877b"
BRANCH="feature/daily-constants-today-board"
MIGRATION="deploy/postgis/init/026_daily_constants_today_board.sql"

cd "$REPO"

PRODUCTION_TOUCHED=0
PROMOTED=0

cleanup_on_exit() {
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$PROMOTED" = "0" ] && [ "$PRODUCTION_TOUCHED" = "1" ]; then
    echo
    echo "=== #52 FAILURE: RESTORE ACCEPTED MAIN APP ==="
    git switch main >/dev/null 2>&1 || true
    git reset --hard origin/main >/dev/null 2>&1 || true
    docker compose -f dashboard/docker-compose.yml build citymanager-dashboard || true
    docker compose -f dashboard/docker-compose.yml up -d --force-recreate \
      citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine || true
    echo "Additive #52 tables may remain; accepted main does not reference them."
  fi
  if [ "$rc" -ne 0 ]; then
    echo
    echo "============================================================"
    echo "#52 DAILY CONSTANTS / TODAY BOARD: FAIL rc=$rc"
    echo "============================================================"
  fi
  exit "$rc"
}
trap cleanup_on_exit EXIT

health_dashboard() {
  docker exec citymanager-dashboard python -c 'import urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=4); raise SystemExit(0 if r.status==200 else 1)' >/dev/null 2>&1
}

echo "============================================================"
echo "#52 DAILY CONSTANTS / TODAY BOARD"
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
  dashboard/today_board_engine.py \
  dashboard/today_board_app.py \
  dashboard/phase3_app.py
bash -n "$0"
grep -Fq 'import today_board_app' dashboard/phase3_app.py
grep -Fq 'COPY today_board_app.py' dashboard/Dockerfile
grep -Fq 'COPY today_board_engine.py' dashboard/Dockerfile
grep -Fq '"/today-board"' dashboard/private_auth.py
grep -Fq 'Daily Constants' dashboard/templates/today_board.html
grep -Fq 'EMS_STAFFING' "$MIGRATION"
echo "Static validation: PASS"

echo
echo "=== 3. BUILD FEATURE IMAGE ==="
docker compose -f dashboard/docker-compose.yml build citymanager-dashboard

echo
echo "=== 4. JINJA + TARGETED TESTS ==="
docker compose -f dashboard/docker-compose.yml run --rm --no-deps \
  -v "$REPO/dashboard:/src:ro" -w /src -e PYTHONPATH=/src:/app \
  --entrypoint python citymanager-dashboard - <<'PY'
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader("templates"))
for name in ("today_board.html", "today_board_strip.html", "my_day.html", "index.html", "nav.html"):
    env.get_template(name)
print("Jinja templates: PASS")
PY

docker compose -f dashboard/docker-compose.yml run --rm --no-deps \
  -v "$REPO/dashboard:/src:ro" -w /src -e PYTHONPATH=/src:/app \
  --entrypoint pytest citymanager-dashboard -p no:cacheprovider -q \
  tests/test_today_board.py \
  tests/test_event_source_pack.py \
  tests/test_source_onboarding.py \
  tests/test_attention_engine.py

echo "Targeted tests: PASS"

echo
echo "=== 5. BACKUP SAFETY GATE ==="
./deploy/postgis/backup.sh
./deploy/postgis/verify-backup.sh
echo "Backup safety gate: PASS"

echo
echo "=== 6. APPLY ADDITIVE TODAY BOARD MIGRATION ==="
PRODUCTION_TOUCHED=1
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$MIGRATION"

echo
echo "=== 7. VERIFY STORAGE + STARTER CARDS ==="
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT rule_key,name,category,rule_type,active,pinned,sort_order
FROM daily_constant_rules
ORDER BY pinned DESC,sort_order,name;
SELECT count(*) AS starter_rules FROM daily_constant_rules
WHERE rule_key IN (
  'FIRE_DUTY_GROUP','POLICE_DUTY_SQUADS','EMS_STAFFING','GARBAGE_TODAY',
  'RECYCLING_TODAY','REGIONAL_EVENTS_TODAY','EVENT_WATCHES_TODAY','OPERATIONS_EXCEPTIONS_TODAY'
);
SQL
COUNT="$(docker exec -i citymanager-postgis sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT count(*) FROM daily_constant_rules
WHERE rule_key IN (
  'FIRE_DUTY_GROUP','POLICE_DUTY_SQUADS','EMS_STAFFING','GARBAGE_TODAY',
  'RECYCLING_TODAY','REGIONAL_EVENTS_TODAY','EVENT_WATCHES_TODAY','OPERATIONS_EXCEPTIONS_TODAY'
);
SQL
)"
[ "$COUNT" = "8" ]
echo "Starter cards: 8/8 PASS"

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
echo "=== 9. CONTROLLED BROWSER ACCEPTANCE ==="
docker exec -i citymanager-dashboard python - <<'PY'
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

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

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirect())

def request(path, data=None):
    headers = {"X-CMOS-Automation-Key": TOKEN}
    payload = None
    if data is not None:
        payload = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(BASE + path, data=payload, headers=headers, method="POST" if data is not None else "GET")
    try:
        with opener.open(req, timeout=10) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")

def one(sql, params=()):
    with psycopg.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()

def scalar(sql):
    row = one(sql)
    return int(next(iter(row.values())))

before_alerts = scalar("SELECT count(*) AS n FROM alerts")
before_issues = scalar("SELECT count(*) AS n FROM issues")

status, body = request("/today-board")
assert status == 200, (status, body[:800])
for marker in ("Daily Constants", "Fire Duty Group", "Police Duty Squads", "EMS Staffing", "TEST ANY DATE"):
    assert marker in body, marker
print("PASS Today Board admin renders starter cards")

status, body = request("/my-day")
assert status == 200, (status, body[:800])
for marker in ("WHAT IS TRUE TODAY?", "Fire Duty Group", "Police Duty Squads", "EMS Staffing", "Tomorrow"):
    assert marker in body, marker
print("PASS My Day renders Today + Tomorrow constants")

status, body = request("/")
assert status == 200, (status, body[:800])
assert "WHAT IS TRUE TODAY?" in body and "Fire Duty Group" in body
print("PASS Overview renders compact Today Board")

status, body = request("/today-board/api/summary")
assert status == 200, (status, body[:800])
summary = json.loads(body)
keys = {x["key"] for x in summary["today_items"]}
assert {"FIRE_DUTY_GROUP", "POLICE_DUTY_SQUADS", "EMS_STAFFING"}.issubset(keys)
print("PASS summary API exposes Today Board state")

# Browser-managed synthetic rule.
status, body = request(
    "/today-board/save",
    {
        "rule_key": "CMOS52_E2E",
        "name": "CMOS52 E2E Constant",
        "category": "OTHER",
        "rule_type": "MANUAL",
        "manual_value": "BASE",
        "sort_order": "9999",
        "active": "on",
        "pinned": "on",
        "day_boundary": "00:00",
    },
)
assert status == 303, (status, body[:800])
row = one("SELECT id::text AS id FROM daily_constant_rules WHERE rule_key='CMOS52_E2E'")
assert row and row["id"]
print("PASS browser rule create")

now = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
starts = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
ends = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
status, body = request(
    f"/today-board/{row['id']}/override",
    {"starts_at": starts, "ends_at": ends, "value": "OVERRIDE", "detail": "Acceptance", "reason": "Controlled E2E"},
)
assert status == 303, (status, body[:800])
status, body = request("/today-board/api/summary")
assert status == 200
summary = json.loads(body)
item = next(x for x in summary["today_items"] if x["key"] == "CMOS52_E2E")
assert item["value"] == "OVERRIDE" and item["override"] is True, item
print("PASS temporary override resolves over base rule")

with psycopg.connect(**DB) as conn, conn.cursor() as cur:
    cur.execute("DELETE FROM daily_constant_rules WHERE rule_key='CMOS52_E2E'")
    conn.commit()

assert one("SELECT id FROM daily_constant_rules WHERE rule_key='CMOS52_E2E'") is None
assert scalar("SELECT count(*) AS n FROM alerts") == before_alerts
assert scalar("SELECT count(*) AS n FROM issues") == before_issues
print("PASS synthetic cleanup + no task/alert side effects")
print("TODAY BOARD CONTROLLED ACCEPTANCE: PASS")
PY

echo
echo "=== 10. FEATURE HEALTH ==="
./deploy/cmos-health

echo
echo "=== 11. PROMOTE VERIFIED FEATURE ==="
git switch main
git merge --ff-only "$BRANCH"
git push origin main
PROMOTED=1
[ -z "$(git status --porcelain)" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]
echo "Promoted main: $(git rev-parse HEAD)"

echo
echo "=== 12. POST-PROMOTION REGRESSION ==="
./deploy/cmos-e2e-secure
./deploy/security/verify-db-credential-alignment.sh
./deploy/postgis/verify-backup.sh
./deploy/cmos-health

echo
echo "============================================================"
echo "#52 DAILY CONSTANTS / TODAY BOARD: PASS"
echo "RULE ENGINE: PASS"
echo "TODAY + TOMORROW: PASS"
echo "FIRE / POLICE / EMS READY: PASS"
echo "TEMPORARY OVERRIDES: PASS"
echo "BROWSER ADMIN: PASS"
echo "NO PARALLEL TASK SYSTEM: PASS"
echo "SECURE E2E: PASS"
echo "HEALTH: PASS"
echo "============================================================"
