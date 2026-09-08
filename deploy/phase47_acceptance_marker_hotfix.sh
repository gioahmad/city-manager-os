#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="4657e98c3dd6bdf037ae3d98c8ce8b8a1d684bcb"
BRANCH="feature/web-managed-source-onboarding"
TARGET="deploy/phase47_source_onboarding.sh"
SELF="deploy/phase47_acceptance_marker_hotfix.sh"

cd "$REPO"

echo "============================================================"
echo "#47 ACCEPTANCE MARKER HOTFIX"
echo "============================================================"

[ "$(git branch --show-current)" = "$BRANCH" ]
[ -z "$(git status --porcelain)" ]
git fetch origin main "$BRANCH"
[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]

python3 - <<'PY'
from pathlib import Path

path = Path("deploy/phase47_source_onboarding.sh")
s = path.read_text()

old = '''status, _, body = request(f"/integrations/onboarding/{row['id']}/test", {})
assert status == 200, status
assert "TEST RESULT" in body and "Normalized preview" in body
row = one("SELECT * FROM integrations WHERE integration_key=%s", (KEY,))
after_events = one("SELECT count(*) AS n FROM event_intelligence WHERE source_integration_id=%s", (row["id"],))["n"]
assert row["last_test_ok"] is True
assert row["last_test_config_version"] == row["config_version"]
assert before_events == after_events == 0
print("PASS read-only TEST saved current success and stored no production events")
'''

new = '''status, _, body = request(f"/integrations/onboarding/{row['id']}/test", {})
assert status == 200, ("TEST HTTP status", status, body[:1200])
assert "READ-ONLY TEST" in body, ("TEST result panel missing", body[:1600])
assert "Normalized item preview" in body, ("Normalized preview missing", body[:2400])
row = one("SELECT * FROM integrations WHERE integration_key=%s", (KEY,))
after_events = one("SELECT count(*) AS n FROM event_intelligence WHERE source_integration_id=%s", (row["id"],))["n"]
assert row["last_test_ok"] is True, ("last_test_ok", row["last_test_ok"], row["last_test_summary"])
assert row["last_test_config_version"] == row["config_version"], ("test/config version mismatch", row["last_test_config_version"], row["config_version"])
assert before_events == after_events == 0, ("TEST stored production events", before_events, after_events)
print("PASS read-only TEST rendered normalized preview, saved current success and stored no production events")
'''

if old not in s:
    raise SystemExit("Expected stale TEST acceptance block not found")

s = s.replace(old, new, 1)
path.write_text(s)
PY

bash -n "$TARGET"

grep -Fq 'assert "READ-ONLY TEST" in body' "$TARGET"
grep -Fq 'assert "Normalized item preview" in body' "$TARGET"
if grep -Fq 'assert "TEST RESULT" in body' "$TARGET"; then
  echo "ERROR: stale TEST RESULT assertion still present"
  exit 31
fi

echo "Acceptance marker replacement: PASS"

git add "$TARGET"
git rm -q "$SELF"
git diff --cached --check

git commit -m "Fix #47 read-only test acceptance markers"
git push origin "$BRANCH"

[ -z "$(git status --porcelain)" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]

echo "Corrected feature HEAD: $(git rev-parse HEAD)"
echo "Acceptance harness fix committed: PASS"

echo
exec bash "$TARGET"
