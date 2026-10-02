#!/usr/bin/env bash
set -Eeuo pipefail
REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED="${1:?usage: install_intelligence.sh EXPECTED_COMMIT}"
cd "$REPO"
source "$REPO/deploy/progress.sh"
OLD_WORKER_IMAGE="$(docker inspect citymanager-intake --format '{{.Image}}' 2>/dev/null || true)"
# The workspace installer backs up, tests, and rolls back the dashboard on live failure.
bash "$REPO/deploy/workspace/install_workspace.sh" "$EXPECTED"
COMPOSE=(docker compose -p citymanager-intelligence -f "$REPO/deploy/intelligence/docker-compose.yml")
export CMOS_INTAKE_IMAGE
CMOS_INTAKE_IMAGE="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
restore_worker(){
  trap - ERR
  if [[ -n "$OLD_WORKER_IMAGE" ]]; then
    CMOS_INTAKE_IMAGE="$OLD_WORKER_IMAGE" "${COMPOSE[@]}" up -d --no-deps --force-recreate citymanager-intake
  else
    "${COMPOSE[@]}" rm -sf citymanager-intake
  fi
  echo 'Intake worker verification failed; its previous state was restored. The verified dashboard remains installed; files are retained.'
  exit 1
}
trap restore_worker ERR
progress_step 0 2 "Starting private document and Microsoft intake"
run_with_progress "Intake worker start" "" "${COMPOSE[@]}" up -d --no-deps --force-recreate citymanager-intake
progress_step 1 2 "Verifying intake worker"
run_with_progress "Intake worker verification" "" docker exec -i citymanager-intake python - <<'PY'
import time
from pathlib import Path
from app import db_conn
from workspace_ingest import extract
assert extract('smoke.csv',b'name,total\nExample,12\n')['profile']['rows']==1
for attempt in range(30):
    try:
        assert time.time()-float(Path('/tmp/workspace-intake-heartbeat').read_text())<300
        break
    except (OSError,ValueError,AssertionError):
        if attempt==29:raise
        time.sleep(1)
with db_conn() as c:
    for table in ('workspace_documents','workspace_microsoft_mail','workspace_microsoft_contacts'):
        c.execute('SELECT 1 FROM '+table+' LIMIT 1')
print('PRIVATE INTAKE: PASS — files process automatically; Microsoft sync runs every 15 minutes after sign-in.')
PY
trap - ERR
progress_step 2 2 "Inbox and intelligence intake installed"
echo 'Open /workspace#inbox and /workspace#library. Configure Microsoft 365 in Settings, then sign in once.'
