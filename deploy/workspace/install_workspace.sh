#!/usr/bin/env bash
set -Eeuo pipefail
REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED="${1:?usage: install_workspace.sh EXPECTED_COMMIT}"
cd "$REPO"
[[ -z "$(git status --porcelain)" ]] || { echo 'Repository must be clean'; exit 1; }
[[ "$(git rev-parse HEAD)" == "$EXPECTED" ]] || { echo 'HEAD does not match expected commit'; exit 1; }
COMPOSE=(docker compose -f "$REPO/dashboard/docker-compose.yml")
OLD_IMAGE="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
OLD_IMAGE_NAME="$(docker inspect citymanager-dashboard --format '{{.Config.Image}}')"
PRESERVED=(citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis)
BEFORE="$(docker inspect --format '{{.Name}} {{.Id}} {{.Image}} {{.State.Running}}' "${PRESERVED[@]}")"

bash "$REPO/deploy/postgis/backup.sh"
for migration in 036_brain.sql 037_workspace.sql; do
  docker exec -i citymanager-postgis sh -lc \
    'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$REPO/deploy/postgis/init/$migration"
done

# Only the dashboard image/service is built and restarted. Engines keep their running images.
"${COMPOSE[@]}" build citymanager-dashboard
"${COMPOSE[@]}" run --rm --no-deps -T -v "$REPO/dashboard/tests:/app/tests:ro" \
  --entrypoint python citymanager-dashboard -m pytest -q \
  tests/test_workspace.py tests/test_brain.py tests/test_global_share.py \
  tests/test_contact_directory_share.py tests/test_navigation_and_map_sharing.py \
  tests/test_executive_workflow.py tests/test_today_board.py
"${COMPOSE[@]}" run --rm --no-deps -T --entrypoint python citymanager-dashboard - <<'PY'
from app import db_conn
from private_auth import _accounts,env_bool
from workspace_app import config
assert env_bool('CMOS_AUTH_ENABLED'), 'Enable the existing private login first'
assert any(a.role=='EXECUTIVE' for a in _accounts().values()), 'An Executive account is required'
with db_conn() as c:
    for table in ('workspace_entities','workspace_relationships','workspace_dates','workspace_messages',
                  'workspace_personal_tasks','workspace_health','workspace_fasts','workspace_goals',
                  'workspace_config','workspace_portals','workspace_portal_messages','workspace_dismissed','brain_notes'):
        assert c.execute('SELECT to_regclass(%s) AS name',(table,)).fetchone()['name'], table
        assert c.execute('SELECT has_table_privilege(current_user,%s,%s) AS ok',
                         (table,'SELECT,INSERT,UPDATE,DELETE')).fetchone()['ok'], table
assert config()['timezone']
print('WORKSPACE DATABASE AND LOGIN: PASS')
PY
rollback(){
  trap - ERR
  docker image tag "$OLD_IMAGE" "$OLD_IMAGE_NAME"
  "${COMPOSE[@]}" up -d --no-deps --force-recreate citymanager-dashboard
  echo 'Workspace verification failed; previous dashboard restored. Additive tables retained.'
  exit 1
}
trap rollback ERR
"${COMPOSE[@]}" up -d --no-deps --force-recreate citymanager-dashboard
docker exec -i citymanager-dashboard python - <<'PY'
import json,time,urllib.request
from private_auth import COOKIE_NAME,_accounts,_issue_session
account=next(a for a in _accounts().values() if a.role=='EXECUTIVE')
headers={'Cookie':COOKIE_NAME+'='+_issue_session(account)}
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5) as r: assert r.status==200
        break
    except Exception:
        if attempt==29: raise
        time.sleep(1)
for path in ('/workspace','/workspace/display','/workspace/api/state','/workspace/api/state?display=true'):
    req=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers)
    with urllib.request.urlopen(req,timeout=30) as r:
        body=r.read()
        assert r.status==200
        if path.endswith('display=true'):
            data=json.loads(body)
            assert set(data)=={'config','alerts','health','work','refreshed_at'}
print('WORKSPACE RELEASE: PASS — open /workspace; PC/TV view at /workspace/display')
PY
AFTER="$(docker inspect --format '{{.Name}} {{.Id}} {{.Image}} {{.State.Running}}' "${PRESERVED[@]}")"
[[ "$BEFORE" == "$AFTER" ]] || { echo 'Protected service changed unexpectedly'; false; }
trap - ERR
echo 'Existing staff, alert engines, integration engine, and database containers preserved.'
