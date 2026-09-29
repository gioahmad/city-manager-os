#!/usr/bin/env bash
set -Eeuo pipefail
REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED="${1:?usage: install_brain.sh EXPECTED_COMMIT}"
cd "$REPO"
[[ -z "$(git status --porcelain)" ]] || { echo 'Repository must be clean'; exit 1; }
[[ "$(git rev-parse HEAD)" == "$EXPECTED" ]] || { echo 'HEAD does not match expected commit'; exit 1; }
COMPOSE=(docker compose -f "$REPO/dashboard/docker-compose.yml")

# Additive migration: existing alerts, watches and engines do not change.
bash "$REPO/deploy/postgis/backup.sh"
docker exec -i citymanager-postgis sh -lc \
  'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < "$REPO/deploy/postgis/init/036_brain.sql"

# Build and test before replacing the running dashboard.
"${COMPOSE[@]}" build citymanager-dashboard
"${COMPOSE[@]}" run --rm --no-deps -T \
  -v "$REPO/dashboard/tests:/app/tests:ro" --entrypoint python \
  citymanager-dashboard -m pytest -q tests/test_brain.py tests/test_global_share.py tests/test_navigation_and_map_sharing.py
"${COMPOSE[@]}" run --rm --no-deps -T --entrypoint python citymanager-dashboard - <<'PY'
from app import db_conn
from private_auth import _accounts, env_bool
assert env_bool('CMOS_AUTH_ENABLED'), 'Brain requires the existing private login to be enabled'
assert _accounts(), 'No private login accounts configured'
assert any(a.role=='EXECUTIVE' for a in _accounts().values()), 'An Executive login is required for SMS setup'
with db_conn() as conn:
    for table in ('brain_notes','brain_attachments','brain_sms_settings'):
        assert conn.execute('SELECT to_regclass(%s) AS name',(table,)).fetchone()['name'], table
    assert conn.execute("SELECT has_table_privilege(current_user,'brain_notes','SELECT,INSERT,UPDATE,DELETE') AS ok").fetchone()['ok']
print('BRAIN DATABASE AND LOGIN: PASS')
PY

OLD_IMAGE="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
OLD_IMAGE_NAME="$(docker inspect citymanager-dashboard --format '{{.Config.Image}}')"
rollback(){
  trap - ERR
  docker image tag "$OLD_IMAGE" "$OLD_IMAGE_NAME"
  "${COMPOSE[@]}" up -d --no-deps --force-recreate citymanager-dashboard
  echo 'Brain dashboard verification failed; previous image restored. Additive Brain tables retained.'
  exit 1
}
trap rollback ERR
"${COMPOSE[@]}" up -d --no-deps --force-recreate citymanager-dashboard
docker exec -i citymanager-dashboard python - <<'PY'
import time,urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5) as r:
            assert r.status==200
        break
    except Exception:
        if attempt==29: raise
        time.sleep(1)
from phase3_app import app
assert any(getattr(r,'path','')=='/brain' for r in app.routes)
from private_auth import COOKIE_NAME,_accounts,_issue_session
account=next(a for a in _accounts().values() if a.role=='EXECUTIVE')
request=urllib.request.Request('http://127.0.0.1:8000/brain',headers={
    'Cookie':COOKIE_NAME+'='+_issue_session(account)})
with urllib.request.urlopen(request,timeout=15) as r:
    assert r.status==200 and b'Capture it now.' in r.read()
print('BRAIN RELEASE: PASS — sign in and open /brain')
PY
trap - ERR
