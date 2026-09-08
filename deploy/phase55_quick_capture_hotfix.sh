#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="ae07c387d7029c6b3bb833d56e7c4a3107a43ebc"
BRANCH="feature/executive-workflow-pack"

cd "$REPO"

echo "============================================================"
echo "#55 QUICK CAPTURE HOTFIX"
echo "============================================================"

git fetch origin main "$BRANCH"
[ "$(git branch --show-current)" = "$BRANCH" ]
[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]
[ "$(git merge-base origin/main HEAD)" = "$BASE" ]
[ -z "$(git status --porcelain)" ]

echo
echo "=== 1. INSPECT CURRENT ISSUES CONSTRAINTS ==="
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid='public.issues'::regclass
ORDER BY conname;
SQL

echo
echo "=== 2. PATCH INBOX TO USE SOURCE STATE ==="
python3 - <<'PY'
from pathlib import Path

p = Path("dashboard/executive_workflow_app.py")
s = p.read_text()

old = '''CAPTURE_TYPES = {\n    "INBOX",\n    "ISSUE",\n    "TASK",\n    "FOLLOW_UP",\n    "DECISION",\n    "COMMITMENT",\n    "COMMUNICATION",\n    "IDEA",\n}\n'''
new = '''CAPTURE_TYPES = {\n    "ISSUE",\n    "TASK",\n    "FOLLOW_UP",\n    "DECISION",\n    "COMMITMENT",\n    "COMMUNICATION",\n    "IDEA",\n}\n'''
assert old in s
s = s.replace(old, new, 1)

old = '''    requested_type = str(form.get("item_type") or "INBOX").strip().upper()\n    item_type = requested_type if requested_type in CAPTURE_TYPES else "INBOX"\n'''
new = '''    requested_type = str(form.get("item_type") or "INBOX").strip().upper()\n    is_inbox = requested_type == "INBOX"\n    item_type = "IDEA" if is_inbox else (requested_type if requested_type in CAPTURE_TYPES else "IDEA")\n    source = "QUICK_CAPTURE_INBOX" if is_inbox else "QUICK_CAPTURE"\n'''
assert old in s
s = s.replace(old, new, 1)

old = '''          %s,%s,%s,%s,'OPEN','QUICK_CAPTURE','Weehawken',%s,%s,%s,%s,\n'''
new = '''          %s,%s,%s,%s,'OPEN',%s,'Weehawken',%s,%s,%s,%s,\n'''
assert old in s
s = s.replace(old, new, 1)

old = '''            priority,\n            assigned_to,\n            item_type,\n'''
new = '''            priority,\n            source,\n            assigned_to,\n            item_type,\n'''
assert old in s
s = s.replace(old, new, 1)

old = '''        WHERE source='QUICK_CAPTURE'\n          AND item_type='INBOX'\n          AND status NOT IN ('RESOLVED','CLOSED')\n'''
new = '''        WHERE source='QUICK_CAPTURE_INBOX'\n          AND status NOT IN ('RESOLVED','CLOSED')\n'''
assert old in s
s = s.replace(old, new, 1)

old = '''        SET title=%s,item_type=%s,category=%s,priority=%s,assigned_to=%s,next_action=%s,\n'''
new = '''        SET title=%s,item_type=%s,source='QUICK_CAPTURE',category=%s,priority=%s,assigned_to=%s,next_action=%s,\n'''
assert old in s
s = s.replace(old, new, 1)

old = '''        WHERE id=%s AND source='QUICK_CAPTURE' AND item_type='INBOX'\n'''
new = '''        WHERE id=%s AND source='QUICK_CAPTURE_INBOX'\n'''
assert s.count(old) == 2
s = s.replace(old, new, 2)

p.write_text(s)

p = Path("deploy/phase55_executive_workflow_pack.sh")
s = p.read_text()
s = s.replace(
    '# Global Quick Capture creates one existing issue row, initially INBOX.',
    '# Global Quick Capture creates one existing issue row in Inbox source-state.',
)
s = s.replace(
    '"SELECT id::text AS id,item_type,source,description FROM issues WHERE title=%s ORDER BY created_at DESC LIMIT 1",',
    '"SELECT id::text AS id,item_type,source,description FROM issues WHERE title=%s ORDER BY created_at DESC LIMIT 1",',
)
s = s.replace(
    'assert row and row["item_type"] == "INBOX" and row["source"] == "QUICK_CAPTURE", row',
    'assert row and row["item_type"] == "IDEA" and row["source"] == "QUICK_CAPTURE_INBOX", row',
)
s = s.replace(
    'print("PASS Quick Capture -> existing issues row -> INBOX")',
    'print("PASS Quick Capture -> existing issues row -> Inbox source-state")',
)
p.write_text(s)
PY

python3 -m py_compile dashboard/executive_workflow_app.py
bash -n deploy/phase55_executive_workflow_pack.sh

grep -Fq 'QUICK_CAPTURE_INBOX' dashboard/executive_workflow_app.py
grep -Fq 'item_type = "IDEA" if is_inbox' dashboard/executive_workflow_app.py
grep -Fq 'source=.QUICK_CAPTURE_INBOX.' dashboard/executive_workflow_app.py || true

echo "Inbox source-state patch: PASS"

echo
echo "=== 3. COMMIT REVIEWED HOTFIX ==="
git add dashboard/executive_workflow_app.py deploy/phase55_executive_workflow_pack.sh deploy/phase55_quick_capture_hotfix.sh
git commit -m "Fix Quick Capture inbox source state"
git push origin "$BRANCH"

echo "Hotfix feature HEAD: $(git rev-parse HEAD)"

echo
echo "=== 4. EXECUTE FULL #55 ACCEPTANCE AGAIN ==="
bash deploy/phase55_executive_workflow_pack.sh
