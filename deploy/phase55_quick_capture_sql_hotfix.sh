#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="ae07c387d7029c6b3bb833d56e7c4a3107a43ebc"
BRANCH="feature/executive-workflow-pack"

cd "$REPO"

echo "============================================================"
echo "#55 QUICK CAPTURE SQL HOTFIX"
echo "============================================================"

git fetch origin main "$BRANCH"
[ "$(git branch --show-current)" = "$BRANCH" ]
[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]
[ "$(git merge-base origin/main HEAD)" = "$BASE" ]
[ -z "$(git status --porcelain)" ]


echo
echo "=== 1. REMOVE DUPLICATE WAITING_ON_SINCE SQL ==="
python3 - <<'PY'
from pathlib import Path

p = Path("dashboard/executive_workflow_app.py")
s = p.read_text()

old = '''        INSERT INTO issues(\n          title,description,category,priority,status,source,municipality,assigned_to,\n          item_type,next_action,waiting_on,waiting_on_since,follow_up_at\n        ) VALUES(\n          %s,%s,%s,%s,'OPEN',%s,'Weehawken',%s,%s,%s,%s,\n          CASE WHEN %s IS NULL THEN NULL ELSE now() END,\n          NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York'\n        )\n'''
new = '''        INSERT INTO issues(\n          title,description,category,priority,status,source,municipality,assigned_to,\n          item_type,next_action,waiting_on,follow_up_at\n        ) VALUES(\n          %s,%s,%s,%s,'OPEN',%s,'Weehawken',%s,%s,%s,%s,\n          NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York'\n        )\n'''
assert old in s, "expected Quick Capture INSERT block not found"
s = s.replace(old, new, 1)

old = '''            next_action,\n            waiting_on,\n            waiting_on,\n            follow_up_at,\n'''
new = '''            next_action,\n            waiting_on,\n            follow_up_at,\n'''
assert old in s, "expected duplicate waiting_on bind block not found"
s = s.replace(old, new, 1)

p.write_text(s)
PY

python3 -m py_compile dashboard/executive_workflow_app.py

grep -Fq 'item_type,next_action,waiting_on,follow_up_at' dashboard/executive_workflow_app.py
if grep -Fq 'CASE WHEN %s IS NULL THEN NULL ELSE now() END' dashboard/executive_workflow_app.py; then
    echo "ERROR: untyped waiting_on CASE still present"
    exit 1
fi

echo "Quick Capture SQL: PASS"


echo
echo "=== 2. HARDEN ACCEPTANCE ERROR REPORTING ==="
python3 - <<'PY'
from pathlib import Path

p = Path("deploy/phase55_executive_workflow_pack.sh")
s = p.read_text()

needle = '''echo\necho "=== 9. CONTROLLED BROWSER ACCEPTANCE ==="\ndocker exec -i citymanager-dashboard python - <<'PY'\n'''
replacement = '''echo\necho "=== 9. CONTROLLED BROWSER ACCEPTANCE ==="\nset +e\ndocker exec -i citymanager-dashboard python - <<'PY'\n'''
assert needle in s, "acceptance start marker not found"
s = s.replace(needle, replacement, 1)

needle = '''print("EXECUTIVE WORKFLOW CONTROLLED ACCEPTANCE: PASS")\nPY\n\necho\necho "=== 10. FEATURE HEALTH ==="\n'''
replacement = '''print("EXECUTIVE WORKFLOW CONTROLLED ACCEPTANCE: PASS")\nPY\nACCEPT_RC=$?\nset -e\nif [ "$ACCEPT_RC" -ne 0 ]; then\n  echo\n  echo "=== CONTROLLED ACCEPTANCE FAILURE - DASHBOARD TRACEBACK ==="\n  docker logs --since 10m citymanager-dashboard 2>&1 | tail -n 220 || true\n  exit "$ACCEPT_RC"\nfi\n\necho\necho "=== 10. FEATURE HEALTH ==="\n'''
assert needle in s, "acceptance end marker not found"
s = s.replace(needle, replacement, 1)

p.write_text(s)
PY

bash -n deploy/phase55_executive_workflow_pack.sh
grep -Fq 'CONTROLLED ACCEPTANCE FAILURE - DASHBOARD TRACEBACK' deploy/phase55_executive_workflow_pack.sh

echo "Acceptance traceback capture: PASS"


echo
echo "=== 3. COMMIT + PUSH HOTFIX ==="
git add \
  dashboard/executive_workflow_app.py \
  deploy/phase55_executive_workflow_pack.sh \
  deploy/phase55_quick_capture_sql_hotfix.sh

git commit -m "Fix Quick Capture waiting state insert"
git push origin "$BRANCH"

echo "SQL hotfix feature HEAD: $(git rev-parse HEAD)"


echo
echo "=== 4. RUN FULL #55 ACCEPTANCE ==="
bash deploy/phase55_executive_workflow_pack.sh
