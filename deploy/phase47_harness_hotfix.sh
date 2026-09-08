#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="4657e98c3dd6bdf037ae3d98c8ce8b8a1d684bcb"
BRANCH="feature/web-managed-source-onboarding"
TARGET="deploy/phase47_source_onboarding.sh"
SELF="deploy/phase47_harness_hotfix.sh"

cd "$REPO"

echo "============================================================"
echo "#47 DEPLOYMENT HARNESS HOTFIX"
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

old_health = '''for _ in $(seq 1 45); do
  if curl -fsS http://127.0.0.1:8090/health >/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS http://127.0.0.1:8090/health >/dev/null
echo "Dashboard health endpoint: PASS"
'''

new_health = '''dashboard_health() {
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
'''

if old_health not in s:
    raise SystemExit("Expected host-bound health block not found")
s = s.replace(old_health, new_health, 1)

s = s.replace('"/source-onboarding', '"/integrations/onboarding')

old_normal = 'request(f"/integrations/onboarding/{row[\'id\']}/activate", {"override_reason": ""})'
s = s.replace(old_normal, 'request(f"/integrations/onboarding/{row[\'id\']}/activate", {})')

old_override = '''status, _, _ = request(
    f"/integrations/onboarding/{row['id']}/activate",
    {"override_reason": "CMOS #47 controlled acceptance override"},
)'''
new_override = '''status, _, _ = request(
    f"/integrations/onboarding/{row['id']}/activate-override",
    {"reason": "CMOS #47 controlled acceptance override"},
)'''
if old_override not in s:
    raise SystemExit("Expected executive override acceptance block not found")
s = s.replace(old_override, new_override, 1)

path.write_text(s)
PY

bash -n "$TARGET"

if grep -Fq 'http://127.0.0.1:8090/health' "$TARGET"; then
  echo "ERROR: host-bound dashboard health check still exists"
  exit 21
fi

if grep -Fq '"/source-onboarding' "$TARGET"; then
  echo "ERROR: stale onboarding route still exists"
  exit 22
fi

grep -Fq '/integrations/onboarding/{row['"'"'id'"'"']}/activate-override' "$TARGET"
grep -Fq '{"reason": "CMOS #47 controlled acceptance override"}' "$TARGET"
grep -Fq 'Dashboard internal health endpoint: PASS' "$TARGET"

echo "Harness replacements: PASS"

git add "$TARGET"
git rm -q "$SELF"

git diff --cached --check

git commit -m "Fix #47 deployment health and acceptance routes"
git push origin "$BRANCH"

[ -z "$(git status --porcelain)" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]

echo "Corrected feature HEAD: $(git rev-parse HEAD)"
echo "Permanent harness fix committed: PASS"

echo
exec bash "$TARGET"
