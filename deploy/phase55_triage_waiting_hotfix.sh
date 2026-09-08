#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="ae07c387d7029c6b3bb833d56e7c4a3107a43ebc"
BRANCH="feature/executive-workflow-pack"

cd "$REPO"

echo "============================================================"
echo "#55 TRIAGE + WAITING STATE SQL HOTFIX"
echo "============================================================"

git fetch origin main "$BRANCH"
[ "$(git branch --show-current)" = "$BRANCH" ]
[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]
[ "$(git merge-base origin/main HEAD)" = "$BASE" ]
[ -z "$(git status --porcelain)" ]

echo
echo "=== 1. PATCH TRIAGE + FAST WAITING SQL ==="
python3 - <<'PY'
from pathlib import Path

p = Path("dashboard/executive_workflow_app.py")
s = p.read_text()

old = '''        UPDATE issues\n        SET title=%s,item_type=%s,source='QUICK_CAPTURE',category=%s,priority=%s,assigned_to=%s,next_action=%s,\n            waiting_on=%s,\n            waiting_on_since=CASE\n              WHEN %s IS NULL THEN NULL\n              WHEN waiting_on_since IS NULL OR waiting_on IS DISTINCT FROM %s THEN now()\n              ELSE waiting_on_since\n            END,\n            follow_up_at=NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',\n            updated_at=now()\n        WHERE id=%s AND source='QUICK_CAPTURE_INBOX'\n'''
new = '''        UPDATE issues\n        SET title=%s,item_type=%s,source='QUICK_CAPTURE',category=%s,priority=%s,assigned_to=%s,next_action=%s,\n            waiting_on=%s,\n            follow_up_at=NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',\n            updated_at=now()\n        WHERE id=%s AND source='QUICK_CAPTURE_INBOX'\n'''
assert old in s, "triage waiting-state SQL block not found"
s = s.replace(old, new, 1)

old = '''            next_action,\n            waiting_on,\n            waiting_on,\n            waiting_on,\n            follow_up_at,\n            issue_id,\n'''
new = '''            next_action,\n            waiting_on,\n            follow_up_at,\n            issue_id,\n'''
assert old in s, "triage duplicate waiting_on bind block not found"
s = s.replace(old, new, 1)

old = '''        UPDATE issues\n        SET waiting_on=%s,\n            waiting_on_since=CASE WHEN waiting_on IS DISTINCT FROM %s OR waiting_on_since IS NULL THEN now() ELSE waiting_on_since END,\n            follow_up_at=NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',\n            updated_at=now()\n        WHERE id=%s AND status NOT IN ('RESOLVED','CLOSED')\n        """,\n        (waiting_on, waiting_on, follow_up_at, issue_id),\n'''
new = '''        UPDATE issues\n        SET waiting_on=%s,\n            follow_up_at=NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',\n            updated_at=now()\n        WHERE id=%s AND status NOT IN ('RESOLVED','CLOSED')\n        """,\n        (waiting_on, follow_up_at, issue_id),\n'''
assert old in s, "fast waiting SQL block not found"
s = s.replace(old, new, 1)

p.write_text(s)
PY

python3 -m py_compile dashboard/executive_workflow_app.py

if grep -Fq 'WHEN %s IS NULL THEN NULL' dashboard/executive_workflow_app.py; then
  echo "ERROR: untyped CASE bind still present"
  exit 1
fi

if grep -Fq 'waiting_on_since=CASE' dashboard/executive_workflow_app.py; then
  echo "ERROR: duplicate waiting_on_since maintenance still present"
  exit 1
fi

echo "Triage + Fast Waiting SQL: PASS"

echo
echo "=== 2. COMMIT + PUSH HOTFIX ==="
git add dashboard/executive_workflow_app.py deploy/phase55_triage_waiting_hotfix.sh
git commit -m "Use waiting trigger for executive workflow updates"
git push origin "$BRANCH"

echo "Triage hotfix feature HEAD: $(git rev-parse HEAD)"

echo
echo "=== 3. RUN FULL #55 ACCEPTANCE ==="
bash deploy/phase55_executive_workflow_pack.sh
