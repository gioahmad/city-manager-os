#!/usr/bin/env bash
set -Eeuo pipefail
REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED="${1:?usage: install_workspace.sh EXPECTED_COMMIT}"
cd "$REPO"
source "$REPO/deploy/progress.sh"
progress_step 0 8 "Checking release and current services"
[[ -z "$(git status --porcelain)" ]] || { echo 'Repository must be clean'; exit 1; }
[[ "$(git rev-parse HEAD)" == "$EXPECTED" ]] || { echo 'HEAD does not match expected commit'; exit 1; }
COMPOSE=(docker compose -f "$REPO/dashboard/docker-compose.yml")
OLD_IMAGE="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
OLD_IMAGE_NAME="$(docker inspect citymanager-dashboard --format '{{.Config.Image}}')"
ROLLBACK_IMAGE="citymanager-dashboard:rollback-${EXPECTED:0:12}"
docker image tag "$OLD_IMAGE" "$ROLLBACK_IMAGE"
PRESERVED=(citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis)
BEFORE="$(docker inspect --format '{{.Name}} {{.Id}} {{.Image}} {{.State.Running}}' "${PRESERVED[@]}")"

progress_step 1 8 "Recovery point"
PREVIOUS_RELEASE="${CMOS_PREVIOUS_RELEASE:-}"
bash "$REPO/deploy/postgis/ensure-deploy-backup.sh" "$PREVIOUS_RELEASE" "$EXPECTED"
progress_step 2 8 "Applying additive migrations"
for migration in 036_brain.sql 037_workspace.sql 038_workspace_calendar.sql 039_workspace_inbox.sql 040_executive_intake.sql 041_performance_indexes.sql; do
  run_with_progress "Migration $migration" "" docker exec -i citymanager-postgis sh -lc \
    'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$REPO/deploy/postgis/init/$migration"
done

# Only the dashboard image/service is built and restarted. Engines keep their running images.
progress_step 3 8 "Building dashboard image"
run_with_progress "Dashboard build" "" "${COMPOSE[@]}" build citymanager-dashboard
progress_step 4 8 "Running release tests"
run_with_progress "Release tests" "" "${COMPOSE[@]}" run --rm --no-deps -T \
  -v "$REPO/dashboard/tests:/app/tests:ro" -v "$REPO/deploy:/deploy:ro" \
  --entrypoint python citymanager-dashboard -m pytest -q \
  tests/test_workspace.py tests/test_workspace_calendar.py tests/test_workspace_hub.py tests/test_workspace_ingest.py \
  tests/test_brain.py tests/test_global_share.py \
  tests/test_contact_directory_share.py tests/test_navigation_and_map_sharing.py \
  tests/test_executive_workflow.py tests/test_today_board.py \
  tests/test_executive_intake_context_map.py
progress_step 5 8 "Checking database grants and private login"
run_with_progress "Database/login preflight" "" "${COMPOSE[@]}" run --rm --no-deps -T --entrypoint python citymanager-dashboard - <<'PY'
from app import db_conn
from private_auth import _accounts,env_bool
from workspace_app import config
assert env_bool('CMOS_AUTH_ENABLED'), 'Enable the existing private login first'
assert any(a.role=='EXECUTIVE' for a in _accounts().values()), 'An Executive account is required'
with db_conn() as c:
    for table in ('workspace_entities','workspace_relationships','workspace_dates','workspace_messages',
                  'workspace_personal_tasks','workspace_health','workspace_fasts','workspace_goals',
                  'workspace_calendar_auth','workspace_calendar_connections','workspace_calendar_events',
                  'workspace_config','workspace_portals','workspace_portal_messages','workspace_dismissed','brain_notes',
                  'workspace_documents','workspace_microsoft_mail','workspace_microsoft_contacts',
                  'workspace_inbox_handled','workspace_inbox_snoozed','workspace_context_links'):
        assert c.execute('SELECT to_regclass(%s) AS name',(table,)).fetchone()['name'], table
        assert c.execute('SELECT has_table_privilege(current_user,%s,%s) AS ok',
                         (table,'SELECT,INSERT,UPDATE,DELETE')).fetchone()['ok'], table
assert config()['timezone']
with db_conn() as c:
    cols={row['column_name'] for row in c.execute("""SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='workspace_calendar_events'""").fetchall()}
    assert 'id' in cols, 'Executive Intake calendar ID migration missing'
print('WORKSPACE DATABASE AND LOGIN: PASS')
PY
rollback(){
  trap - ERR
  docker image tag "$ROLLBACK_IMAGE" "$OLD_IMAGE_NAME"
  "${COMPOSE[@]}" up -d --no-deps --force-recreate citymanager-dashboard
  echo 'Workspace verification failed; previous dashboard restored. Additive tables retained.'
  exit 1
}
trap rollback ERR
progress_step 6 8 "Restarting dashboard only"
run_with_progress "Dashboard restart" "" "${COMPOSE[@]}" up -d --no-deps --force-recreate citymanager-dashboard
progress_step 7 8 "Verifying live workspace and protected services"
run_with_progress "Live workspace verification" "" docker exec -i citymanager-dashboard python - <<'PY'
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
for path in ('/','/map','/alerts?window=24h','/workspace','/workspace/display',
             '/workspace/api/state','/workspace/api/state?display=true',
             '/workspace/api/hub','/workspace/api/hub?view=library'):
    req=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers)
    with urllib.request.urlopen(req,timeout=30) as r:
        body=r.read()
        assert r.status==200
        if path.endswith('display=true'):
            data=json.loads(body)
            assert set(data)=={'config','alerts','health','work','refreshed_at'}
paths={getattr(route,'path','') for route in __import__('phase3_app').app.routes}
assert '/context/{item_kind}/{item_id}' in paths
assert '/records/{record_id}' in paths
print('WORKSPACE RELEASE: PASS — dashboard, map, Executive Intake and context routes verified')
PY
AFTER="$(docker inspect --format '{{.Name}} {{.Id}} {{.Image}} {{.State.Running}}' "${PRESERVED[@]}")"
[[ "$BEFORE" == "$AFTER" ]] || { echo 'Protected service changed unexpectedly'; false; }
trap - ERR
docker image rm "$ROLLBACK_IMAGE" >/dev/null 2>&1 || true
progress_step 8 8 "Workspace installation complete"
echo 'Existing staff, alert engines, integration engine, and database containers preserved.'
