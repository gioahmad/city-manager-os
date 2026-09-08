#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="ae07c387d7029c6b3bb833d56e7c4a3107a43ebc"
BRANCH="feature/executive-workflow-pack"
MIGRATION="deploy/postgis/init/027_executive_workflow_pack.sql"

cd "$REPO"

PRODUCTION_TOUCHED=0
PROMOTED=0

cleanup_on_exit() {
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$PROMOTED" = "0" ] && [ "$PRODUCTION_TOUCHED" = "1" ]; then
    echo
    echo "=== #55 FAILURE: RESTORE ACCEPTED MAIN APP ==="
    git switch main >/dev/null 2>&1 || true
    git reset --hard origin/main >/dev/null 2>&1 || true
    docker compose -f dashboard/docker-compose.yml build citymanager-dashboard || true
    docker compose -f dashboard/docker-compose.yml up -d --force-recreate \
      citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine || true
    echo "Additive executive_review_state table may remain; accepted main does not depend on it."
  fi
  if [ "$rc" -ne 0 ]; then
    echo
    echo "============================================================"
    echo "#55 EXECUTIVE WORKFLOW PACK: FAIL rc=$rc"
    echo "============================================================"
  fi
  exit "$rc"
}
trap cleanup_on_exit EXIT

health_dashboard() {
  docker exec citymanager-dashboard python -c 'import urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=4); raise SystemExit(0 if r.status==200 else 1)' >/dev/null 2>&1
}

echo "============================================================"
echo "#55 EXECUTIVE WORKFLOW PACK"
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
  dashboard/executive_workflow_app.py \
  dashboard/today_board_app.py \
  dashboard/phase3_app.py
bash -n "$0"
grep -Fq 'import executive_workflow_app' dashboard/phase3_app.py
grep -Fq 'COPY executive_workflow_app.py' dashboard/Dockerfile
grep -Fq 'quick_capture_global.html' dashboard/templates/nav.html
grep -Fq '/inbox' dashboard/templates/nav.html
grep -Fq '/what-changed' dashboard/templates/nav.html
grep -Fq 'quick-waiting' dashboard/templates/issues.html
grep -Fq 'executive_review_state' "$MIGRATION"
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
for name in (
    "quick_capture_global.html",
    "executive_inbox.html",
    "executive_changes.html",
    "executive_changes_strip.html",
    "today_board_wizard.html",
    "issues.html",
    "my_day.html",
    "nav.html",
):
    env.get_template(name)
print("Jinja templates: PASS")
PY

docker compose -f dashboard/docker-compose.yml run --rm --no-deps \
  -v "$REPO/dashboard:/src:ro" -w /src -e PYTHONPATH=/src:/app \
  --entrypoint pytest citymanager-dashboard -p no:cacheprovider -q \
  tests/test_executive_workflow.py \
  tests/test_today_board.py \
  tests/test_attention_engine.py \
  tests/test_event_source_pack.py \
  tests/test_source_onboarding.py

echo "Targeted tests: PASS"

echo
echo "=== 5. BACKUP SAFETY GATE ==="
./deploy/postgis/backup.sh
./deploy/postgis/verify-backup.sh
echo "Backup safety gate: PASS"

echo
echo "=== 6. APPLY ADDITIVE EXECUTIVE WORKFLOW MIGRATION ==="
PRODUCTION_TOUCHED=1
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$MIGRATION"

echo
echo "=== 7. VERIFY STORAGE ==="
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT to_regclass('public.executive_review_state') AS review_state_table;
SELECT indexname FROM pg_indexes WHERE indexname='idx_issues_quick_capture_inbox';
SQL
TABLE_OK="$(docker exec -i citymanager-postgis sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT CASE WHEN to_regclass('public.executive_review_state') IS NULL THEN 0 ELSE 1 END;
SQL
)"
[ "$TABLE_OK" = "1" ]
echo "Executive review state: PASS"

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
set +e
docker exec -i citymanager-dashboard python - <<'PY'
from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

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
    method = "GET"
    if data is not None:
        payload = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST"
    req = urllib.request.Request(BASE + path, data=payload, headers=headers, method=method)
    try:
        with opener.open(req, timeout=12) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")

def one(sql, params=()):
    with psycopg.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()

def scalar(sql, params=()):
    row = one(sql, params)
    return int(next(iter(row.values()))) if row else 0

marker = "CMOS55 E2E quick capture"
before_issues = scalar("SELECT count(*) AS n FROM issues")
before_alerts = scalar("SELECT count(*) AS n FROM alerts")

# Render all new surfaces.
checks = {
    "/my-day": ["What Changed", "+ Quick Capture"],
    "/inbox": ["Triage Queue", "+ Quick Capture"],
    "/what-changed": ["Mark Everything Reviewed Now", "What Changed"],
    "/today-board/setup": ["Setup Wizard", "Nothing here is guessed"],
}
for path, markers in checks.items():
    status, body = request(path)
    assert status == 200, (path, status, body[:1000])
    for expected in markers:
        assert expected in body, (path, expected)
    print("PASS render", path)

# Global Quick Capture creates one existing issue row in Inbox source-state.
status, body = request(
    "/quick-capture",
    {
        "raw_text": marker + "\nPreserve this raw wording for acceptance.",
        "item_type": "INBOX",
        "priority": "3",
        "return_to": "/inbox",
    },
)
assert status == 303, (status, body[:500])
row = one(
    "SELECT id::text AS id,item_type,source,description FROM issues WHERE title=%s ORDER BY created_at DESC LIMIT 1",
    (marker,),
)
assert row and row["item_type"] == "IDEA" and row["source"] == "QUICK_CAPTURE_INBOX", row
assert "Preserve this raw wording" in (row["description"] or "")
issue_id = row["id"]
print("PASS Quick Capture -> existing issues row -> Inbox source-state")

status, body = request("/inbox")
assert status == 200 and marker in body
print("PASS Inbox shows same capture")

follow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
status, body = request(
    f"/inbox/{issue_id}/triage",
    {
        "title": marker,
        "item_type": "FOLLOW_UP",
        "priority": "4",
        "category": "CMOS55_TEST",
        "assigned_to": "CMOS55 Tester",
        "next_action": "Check acceptance",
        "waiting_on": "CMOS55 Agency",
        "follow_up_at": follow,
    },
)
assert status == 303, (status, body[:500])
row = one(
    "SELECT id::text AS id,item_type,priority,category,assigned_to,next_action,waiting_on,follow_up_at FROM issues WHERE id=%s::uuid",
    (issue_id,),
)
assert row["id"] == issue_id and row["item_type"] == "FOLLOW_UP" and row["priority"] == 4, row
assert row["waiting_on"] == "CMOS55 Agency"
print("PASS Inbox triage updates same issue row")

# Fast Waiting On uses the same issue and existing fields.
follow2 = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
status, body = request(
    f"/issues/{issue_id}/quick-waiting",
    {"waiting_on": "CMOS55 External", "follow_up_at": follow2, "return_to": "/issues"},
)
assert status == 303, (status, body[:500])
row = one("SELECT waiting_on,waiting_on_since,follow_up_at FROM issues WHERE id=%s::uuid", (issue_id,))
assert row["waiting_on"] == "CMOS55 External" and row["waiting_on_since"] is not None
print("PASS Fast Waiting On updates existing issue")

# What Changed sees the synthetic change before checkpointing.
status, body = request("/what-changed")
assert status == 200 and marker in body
print("PASS What Changed sees updated issue")

status, body = request("/what-changed/mark-reviewed", {})
assert status == 303, (status, body[:500])
review = one("SELECT username,last_reviewed_at FROM executive_review_state WHERE username='automation'")
assert review and review["last_reviewed_at"] is not None
print("PASS per-user review checkpoint")

# Clean synthetic state.
with psycopg.connect(**DB) as conn, conn.cursor() as cur:
    cur.execute("DELETE FROM issues WHERE id=%s::uuid", (issue_id,))
    cur.execute("DELETE FROM executive_review_state WHERE username='automation'")
    conn.commit()

assert scalar("SELECT count(*) AS n FROM issues") == before_issues
assert scalar("SELECT count(*) AS n FROM alerts") == before_alerts
assert one("SELECT id FROM issues WHERE id=%s::uuid", (issue_id,)) is None
print("PASS synthetic cleanup + no alert/task duplication")
print("EXECUTIVE WORKFLOW CONTROLLED ACCEPTANCE: PASS")
PY
ACCEPT_RC=$?
set -e
if [ "$ACCEPT_RC" -ne 0 ]; then
  echo
  echo "=== CONTROLLED ACCEPTANCE FAILURE - DASHBOARD TRACEBACK ==="
  docker logs --since 10m citymanager-dashboard 2>&1 | tail -n 220 || true
  exit "$ACCEPT_RC"
fi

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
echo "#55 EXECUTIVE WORKFLOW PACK: PASS"
echo "GLOBAL QUICK CAPTURE: PASS"
echo "INBOX TRIAGE: PASS"
echo "WHAT CHANGED: PASS"
echo "TODAY BOARD SETUP WIZARD: PASS"
echo "FAST WAITING ON: PASS"
echo "NO PARALLEL TASK SYSTEM: PASS"
echo "SECURE E2E: PASS"
echo "HEALTH: PASS"
echo "============================================================"
