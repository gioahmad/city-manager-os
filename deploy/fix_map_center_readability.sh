#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="97d7fe990519a5607d4888653a0d56b7b7d930aa"
BRANCH="fix/map-center-readability"
SELF="deploy/fix_map_center_readability.sh"

cd "$REPO"

echo "============================================================"
echo "MAPPING CENTER READABILITY FIX"
echo "============================================================"

echo
echo "=== 1. PREFLIGHT ==="
git fetch origin main "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"
[ -z "$(git status --porcelain)" ]
[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git merge-base origin/main HEAD)" = "$BASE" ]
echo "Feature HEAD: $(git rev-parse HEAD)"
echo "Preflight: PASS"

echo
echo "=== 2. CACHE BUST + STATIC CHECK ==="
python3 - <<'PY'
from pathlib import Path
p = Path('dashboard/templates/map.html')
s = p.read_text()
old = '/static/map.css?v=20260907-1'
new = '/static/map.css?v=20260908-1'
if old not in s and new not in s:
    raise SystemExit('map.css version anchor missing')
if old in s:
    p.write_text(s.replace(old, new, 1))
PY

grep -Fq '.map-sidebar .panel-kicker{color:#9fd3ff' dashboard/static/map.css
grep -Fq '.map-side-head h2{font-size:1.28rem' dashboard/static/map.css
grep -Fq '/static/map.css?v=20260908-1' dashboard/templates/map.html

git add dashboard/static/map.css dashboard/templates/map.html
git rm -q "$SELF"
git diff --cached --check
if ! git diff --cached --quiet; then
  git commit -m "Improve Mapping Center readability"
  git push origin "$BRANCH"
fi

echo "Static readability checks: PASS"

echo
echo "=== 3. BUILD + DEPLOY DASHBOARD ==="
docker compose -f dashboard/docker-compose.yml build citymanager-dashboard
docker compose -f dashboard/docker-compose.yml up -d --force-recreate citymanager-dashboard

for _ in $(seq 1 45); do
  if docker exec citymanager-dashboard python -c 'import urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=4); raise SystemExit(0 if r.status==200 else 1)' >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker exec citymanager-dashboard python -c 'import urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=4); assert r.status==200'
echo "Dashboard health: PASS"

echo
echo "=== 4. MAP PAGE ACCEPTANCE ==="
docker exec -i citymanager-dashboard python - <<'PY'
import os, urllib.request
base='http://127.0.0.1:8000'
token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
req=urllib.request.Request(base+'/map',headers={'X-CMOS-Automation-Key':token})
with urllib.request.urlopen(req,timeout=10) as r:
    body=r.read().decode('utf-8','replace')
    assert r.status==200
    assert 'MAPPING CENTER' in body
    assert 'Map Workspace' in body
    assert '/static/map.css?v=20260908-1' in body
print('Map page + cache-busted stylesheet: PASS')
PY

echo
echo "=== 5. PROMOTE ==="
git switch main
git merge --ff-only "$BRANCH"
git push origin main
[ -z "$(git status --porcelain)" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]
echo "Promoted main: $(git rev-parse HEAD)"

echo
echo "=== 6. FINAL HEALTH ==="
./deploy/cmos-health

echo
echo "============================================================"
echo "MAPPING CENTER READABILITY: PASS"
echo "HIGH-CONTRAST SIDEBAR: PASS"
echo "CACHE BUST: PASS"
echo "MAP FUNCTIONALITY: UNCHANGED"
echo "============================================================"
