#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

REPO="/opt/city-manager-os"
EXPECTED_BASE="7bd25e3e36ebc5151d5ed9381d000257f7b722f1"
CANDIDATE_BRANCH="release-candidate/60"
REPORT_BRANCH="release-output/60"
RELEASE_ID="issue-60-pseg-spatial-alerts-v1"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
STATE_DIR="/var/lib/city-manager-os/releases"
LOG_FILE="$LOG_DIR/issue-60-$RUN_ID.log"
STATE_FILE="$STATE_DIR/issue-60.state"
LOCK_FILE="/var/lock/cmos-issue-60-release.lock"
N8N_ROLLBACK_FILE="$STATE_DIR/issue-60-pre-n8n-$RUN_ID.sqlite"
WORKFLOW_INVENTORY="$STATE_DIR/issue-60-pre-consolidation-$RUN_ID.json"
CURRENT_PHASE="startup"
FINAL_STATUS="FAIL"
FAIL_LINE=""
FAIL_COMMAND=""
TARGET_HEAD=""
DATABASE_ACTION="not-started"
APPLICATION_ACTION="not-started"
WORKFLOW_ACTION="not-started"
PSEG_POLL_ACTION="not-started"
TEST_ACTION="not-started"
IMAGE_BUILT=0
APPLICATION_CHANGED=0
WORKFLOW_CHANGED=0
OLD_DASH_IMAGE=""
OLD_LATEST_IMAGE=""
N8N_DB=""
N8N_DB_OWNER=""

EXPECTED_PATHS=$'dashboard/Dockerfile\ndashboard/geo_resolver.py\ndashboard/integration_worker.py\ndashboard/integrations_app.py\ndashboard/pseg_engine.py\ndashboard/templates/integrations.html\ndashboard/templates/pseg_settings.html\ndashboard/tests/test_pseg_spatial_alerts.py\ndeploy/n8n/install_pseg_unified.sh\ndeploy/postgis/init/033_pseg_spatial_alerts.sql\ndeploy/pseg/install_pseg_spatial_alerts.sh\ndeploy/releases/issue-60-pseg-spatial-alerts.sh\ndocs/PSEG_SPATIAL_ALERTS.md\nworkflows/sources/PSEG_Unified_Spatial_Statewide_v2.json'

umask 077
mkdir -p "$LOG_DIR" "$STATE_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

redact(){
  sed -E \
    -e 's#(Bearer|Basic)[[:space:]]+[^[:space:]]+#\1 [REDACTED]#Ig' \
    -e 's#(https?://)[^/@[:space:]]+:[^/@[:space:]]+@#\1[REDACTED]@#Ig' \
    -e 's#(github_pat_|gh[pousr]_)[[:alnum:]_]+#[REDACTED_GITHUB_TOKEN]#g' \
    -e "s#((authorization|cookie|password|passwd|secret|token|api[_-]?key|ntfy[_-]?topic|topic)[[:space:]\"']*[=:][[:space:]\"']*)[^,;[:space:]\"']+#\1[REDACTED]#Ig" \
    -e 's#[[:alnum:]._%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,}#[REDACTED_EMAIL]#g' \
    -e 's#(^|[^0-9])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9]|$)#\1[REDACTED_IP]\3#g'
}

safe_git_value(){
  local value=""
  value="$(git -C "$REPO" "$@" 2>/dev/null || true)"
  printf '%s' "${value:-unavailable}"
}

state_get(){
  local key="$1"
  [[ -f "$STATE_FILE" ]] || return 0
  sed -n "s/^${key}=//p" "$STATE_FILE" | tail -n 1
}

state_set(){
  local key="$1" value="$2" temp="$STATE_FILE.tmp"
  { [[ -f "$STATE_FILE" ]] && grep -v "^${key}=" "$STATE_FILE" || true; printf '%s=%s\n' "$key" "$value"; } > "$temp"
  mv -f "$temp" "$STATE_FILE"
  chmod 600 "$STATE_FILE"
}

db_at(){
  docker exec -i citymanager-postgis sh -lc \
    'psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}

wait_dashboard(){
  local ready=0
  for _ in $(seq 1 45); do
    if [[ "$(docker inspect citymanager-dashboard --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
      && docker exec citymanager-dashboard python -c \
        "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4); assert r.status==200" \
        >/dev/null 2>&1; then
      ready=$((ready+1))
      (( ready >= 3 )) && { log "READINESS PASS: citymanager-dashboard"; return 0; }
    else
      ready=0
    fi
    sleep 2
  done
  return 1
}

wait_n8n(){
  for _ in $(seq 1 60); do
    if docker exec n8n node -e \
      "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
      >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

database_ready(){
  local objects
  objects="$(db_at <<'SQL'
SELECT
  to_regclass('public.pseg_alert_settings') IS NOT NULL
  AND to_regclass('public.pseg_outage_state') IS NOT NULL
  AND to_regclass('public.pseg_alert_status') IS NOT NULL
  AND EXISTS(
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='pseg_outage_state'
      AND column_name='material_baseline_customers_out'
  );
SQL
)"
  [[ "$objects" == t ]] || return 1
  [[ "$(db_at <<'SQL'
SELECT
  (SELECT count(*)=1 FROM pseg_alert_settings WHERE settings_key='DEFAULT')
  AND (SELECT minimum_customers BETWEEN 1 AND 10000000 AND poll_minutes=15
       FROM pseg_alert_settings WHERE settings_key='DEFAULT')
  AND has_table_privilege('citymanager_app','pseg_alert_settings','SELECT,UPDATE')
  AND has_table_privilege('citymanager_app','pseg_outage_state','SELECT,INSERT,UPDATE,DELETE');
SQL
)" == t ]]
}

application_ready(){
  if ! docker exec -i citymanager-dashboard python - "$RELEASE_ID" <<'PY' >/dev/null 2>&1
import json,os,sys,urllib.request
token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
headers={'X-CMOS-Automation-Key':token} if token else {}
request=urllib.request.Request('http://127.0.0.1:8000/api/pseg/release',headers=headers)
with urllib.request.urlopen(request,timeout=10) as response:
    payload=json.load(response)
assert response.status==200 and payload.get('release_id')==sys.argv[1]
assert payload.get('poll_minutes')==15 and payload.get('hudson_excluded_from_statewide') is True
PY
  then
    return 1
  fi
  local dashboard_image integration_image
  dashboard_image="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
  integration_image="$(docker inspect citymanager-integration-engine --format '{{.Image}}')"
  [[ "$dashboard_image" == "$integration_image" ]] || return 1
  docker exec citymanager-integration-engine python -c \
    "import pseg_engine; assert pseg_engine.RELEASE_ID == '$RELEASE_ID'" >/dev/null 2>&1
}

image_ready(){
  docker image inspect dashboard-citymanager-dashboard:latest >/dev/null 2>&1 || return 1
  docker run --rm --network none --entrypoint python dashboard-citymanager-dashboard:latest \
    -c "import pseg_engine; assert pseg_engine.RELEASE_ID == '$RELEASE_ID'" >/dev/null 2>&1
}

workflow_ready(){
  python3 - "$N8N_DB" <<'PY' >/dev/null
import json,sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
row=con.execute("""
  SELECT id,active,activeVersionId,nodes FROM workflow_entity
  WHERE name='PSEG Hudson + NJ Statewide - City Manager OS v2'
  ORDER BY active DESC LIMIT 1
""").fetchone()
if not row or not row['active'] or not row['activeVersionId']: raise SystemExit(1)
nodes={node.get('name'):node for node in json.loads(row['nodes'] or '[]')}
required={'Load Pending PSEG Alerts','Restore Standard PSEG Alert','Send to Central Watchlist Matcher','Mark PSEG Alert Routed'}
if not required.issubset(nodes): raise SystemExit(1)
load=(nodes['Load Pending PSEG Alerts'].get('parameters') or {}).get('query','')
mark=(nodes['Mark PSEG Alert Routed'].get('parameters') or {}).get('query','')
replacement=((nodes['Mark PSEG Alert Routed'].get('parameters') or {}).get('options') or {}).get('queryReplacement','')
if 'route_pending' not in load or 'route_pending' not in mark: raise SystemExit(1)
if not nodes['Send to Central Watchlist Matcher'].get('alwaysOutputData'): raise SystemExit(1)
if 'Restore Standard PSEG Alert' not in replacement or "d.p->>'alert_id'" not in mark: raise SystemExit(1)
if 'ntfy' in json.dumps(list(nodes.values()),sort_keys=True).lower(): raise SystemExit(1)
legacy=con.execute("""
  SELECT count(*) AS n FROM workflow_entity
  WHERE id<>? AND active=1 AND (upper(name) LIKE '%PSEG%' OR upper(name) LIKE '%PSE&G%')
""",(row['id'],)).fetchone()['n']
if legacy: raise SystemExit(1)
con.close()
PY
}

pseg_poll_ready(){
  [[ "$(db_at <<SQL
SELECT EXISTS(
  SELECT 1 FROM source_health
  WHERE source_id='PSEG' AND status='OK'
    AND metadata->>'release_id'='$RELEASE_ID'
    AND last_success_at IS NOT NULL
);
SQL
)" == t ]]
}

capture_workflow_inventory(){
  python3 - "$N8N_DB" "$WORKFLOW_INVENTORY" <<'PY'
import hashlib,json,re,sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
rows=con.execute("""
  SELECT id,name,active,nodes,connections,settings FROM workflow_entity
  WHERE upper(name) LIKE '%PSEG%' OR upper(name) LIKE '%PSE&G%'
  ORDER BY name,id
""").fetchall()

secret_key=re.compile(r'(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|ntfy[_-]?topic|^topic$)',re.I)
def scrub_text(value):
    value=re.sub(r'(?i)\b(Bearer|Basic)\s+[^\s"\']+',r'\1 [REDACTED]',value)
    value=re.sub(r'(?i)(https?://)[^/@\s:]+:[^/@\s]+@',r'\1[REDACTED]@',value)
    value=re.sub(r'(?i)([?&](?:api[_-]?key|token|secret|password)=)[^&\s"\']+',r'\1[REDACTED]',value)
    value=re.sub(r'(?i)(https?://[^/\s"\']*ntfy[^/\s"\']*)/[^\s"\']+',r'\1/[REDACTED_TOPIC]',value)
    value=re.sub(
      r'(?i)\b(password|passwd|secret|token|api[_-]?key|ntfy[_-]?topic|topic)\b(\s*[:=]\s*["\'])[^"\']+(["\'])',
      r'\1\2[REDACTED]\3',value,
    )
    value=re.sub(r'\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b','[REDACTED_EMAIL]',value)
    value=re.sub(r'(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)','[REDACTED_IP]',value)
    return value
def scrub(value,key=''):
    if key == 'credentials' and isinstance(value,dict):
        return {kind:{'id':'[REDACTED_CREDENTIAL_ID]','name':str((ref or {}).get('name') or kind)}
                for kind,ref in value.items()}
    if secret_key.search(key) and key.lower() not in {'authentication'}:
        return '[REDACTED]'
    if isinstance(value,dict):
        result={k:scrub(v,str(k)) for k,v in value.items()}
        if secret_key.search(str(value.get('name') or '')) and 'value' in result:
            result['value']='[REDACTED]'
        return result
    if isinstance(value,list): return [scrub(v,key) for v in value]
    if isinstance(value,str): return scrub_text(value)
    return value
result=[]
for row in rows:
    nodes=json.loads(row['nodes'] or '[]')
    connections=json.loads(row['connections'] or '{}')
    settings=json.loads(row['settings'] or '{}')
    material=json.dumps({'nodes':nodes,'connections':connections,'settings':settings},sort_keys=True,separators=(',',':'))
    raw_text=json.dumps(nodes,sort_keys=True).lower()
    result.append({
      'id':row['id'],'name':row['name'],'active':bool(row['active']),
      'definition_sha256':hashlib.sha256(material.encode()).hexdigest(),
      'direct_ntfy_reference':'ntfy' in raw_text,
      'reviewed_definition':scrub({
        'name':row['name'],'active':bool(row['active']),'nodes':nodes,
        'connections':connections,'settings':settings,
      }),
    })
with open(sys.argv[2],'w') as handle:
    json.dump({
      'captured_before_issue_60':True,
      'redacted':True,
      'runtime_state_and_credentials_omitted':True,
      'workflows':result,
    },handle,indent=2)
con.close()
PY
  chmod 600 "$WORKFLOW_INVENTORY"
  log "PSEG WORKFLOW REVIEW: captured $(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["workflows"]))' "$WORKFLOW_INVENTORY") redacted definitions"
}

backup_n8n(){
  python3 - "$N8N_DB" "$N8N_ROLLBACK_FILE" <<'PY'
import sqlite3,sys
source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2])
source.backup(target); target.close(); source.close()
PY
  chmod 600 "$N8N_ROLLBACK_FILE"
  [[ -s "$N8N_ROLLBACK_FILE" ]] || fail "n8n rollback backup is empty"
}

rollback_runtime(){
  set +e
  trap - ERR
  if (( WORKFLOW_CHANGED == 1 )) && [[ -s "$N8N_ROLLBACK_FILE" ]]; then
    log "ROLLBACK: restoring pre-#60 n8n state"
    docker stop n8n >/dev/null 2>&1
    install -m 600 "$N8N_ROLLBACK_FILE" "$N8N_DB"
    [[ -n "$N8N_DB_OWNER" ]] && chown "$N8N_DB_OWNER" "$N8N_DB"
    docker start n8n >/dev/null 2>&1
    wait_n8n >/dev/null 2>&1
    WORKFLOW_CHANGED=0
  fi
  if (( APPLICATION_CHANGED == 1 )) && [[ -n "$OLD_DASH_IMAGE" ]]; then
    log "ROLLBACK: restoring prior Dashboard and Integration Engine image"
    docker image tag "$OLD_DASH_IMAGE" dashboard-citymanager-dashboard:latest >/dev/null 2>&1
    docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate \
      citymanager-dashboard citymanager-integration-engine >/dev/null 2>&1
    wait_dashboard >/dev/null 2>&1
    APPLICATION_CHANGED=0
  elif (( IMAGE_BUILT == 1 )) && [[ -n "$OLD_LATEST_IMAGE" ]]; then
    docker image tag "$OLD_LATEST_IMAGE" dashboard-citymanager-dashboard:latest >/dev/null 2>&1
  fi
  set -e
}

publish_report(){ (
  set +e
  local status="$1" rc="$2" parent="" worktree="" report_dir report_file latest_file raw_sha changed
  report_cleanup(){
    if [[ -n "$worktree" && "$worktree" == /tmp/cmos60-report.*/* ]]; then
      git -C "$REPO" worktree remove --force "$worktree" >/dev/null 2>&1 || true
    fi
    [[ -n "$parent" && "$parent" == /tmp/cmos60-report.* ]] && rmdir "$parent" >/dev/null 2>&1 || true
  }
  trap report_cleanup EXIT
  git -C "$REPO" fetch -q origin "+refs/heads/$REPORT_BRANCH:refs/remotes/origin/$REPORT_BRANCH" || return 1
  parent="$(mktemp -d /tmp/cmos60-report.XXXXXX)"
  worktree="$parent/worktree"
  git -C "$REPO" worktree add --detach "$worktree" "origin/$REPORT_BRANCH" >/dev/null 2>&1 || return 1
  report_dir="$worktree/release-results/issue-60"
  mkdir -p "$report_dir"
  report_file="$report_dir/$RUN_ID-${status,,}.md"
  latest_file="$report_dir/latest.md"
  raw_sha="$(sha256sum "$LOG_FILE" | awk '{print $1}')"
  changed="$(safe_git_value status --short | redact)"
  {
    printf '# City Manager OS issue #60 release report\n\n'
    printf '> Public, intentionally redacted summary. The complete mode-600 log remains on the VPS.\n\n'
    printf '| Field | Value |\n|---|---|\n'
    printf '| Run | `%s` |\n' "$RUN_ID"
    printf '| Release | `%s` |\n' "$RELEASE_ID"
    printf '| Status | **%s** |\n' "$status"
    printf '| Exit code | `%s` |\n' "$rc"
    printf '| Failed line | `%s` |\n' "${FAIL_LINE:-none}"
    printf '| Phase | `%s` |\n' "$CURRENT_PHASE"
    printf '| Database | `%s` |\n' "$DATABASE_ACTION"
    printf '| Dashboard + Integration Engine | `%s` |\n' "$APPLICATION_ACTION"
    printf '| PSEG n8n consolidation | `%s` |\n' "$WORKFLOW_ACTION"
    printf '| PSEG source poll | `%s` |\n' "$PSEG_POLL_ACTION"
    printf '| Focused tests | `%s` |\n' "$TEST_ACTION"
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '| Accepted base | `%s` |\n' "$EXPECTED_BASE"
    printf '| Candidate | `%s` |\n' "${TARGET_HEAD:-unavailable}"
    printf '| Local HEAD | `%s` |\n' "$(safe_git_value rev-parse HEAD)"
    printf '| Origin main | `%s` |\n' "$(safe_git_value rev-parse origin/main)"
    printf '| Full local log | `%s` |\n' "$LOG_FILE"
    printf '| Full log SHA-256 | `%s` |\n\n' "$raw_sha"
    printf '## Working tree\n\n```text\n%s\n```\n\n' "${changed:-clean}"
    printf '## Redacted diagnostic output\n\n```text\n'
    grep -Eai '(^|[[:space:]])(ERROR|FATAL|FAIL|FAILED|EXCEPTION|TRACEBACK|ASSERT|SYNTAX|UNEXPECTED|MISMATCH|PASS|PLAN|READINESS|HEALTH|PSEG|WORKFLOW|DATABASE|DASHBOARD|ROUTE|ROLLBACK)|^(base|target|changed|build|services|tests|probes|backup_required|external|full_e2e|unknown)=' "$LOG_FILE" \
      | grep -v '^FAILED_COMMAND=' | redact | tail -n 260 || true
    printf '```\n'
  } > "$report_file"
  install -m 600 "$report_file" "$latest_file"
  if [[ -s "$WORKFLOW_INVENTORY" ]]; then
    install -m 600 "$WORKFLOW_INVENTORY" "$report_dir/pre-consolidation-reviewed-workflows.json"
  fi
  git -C "$worktree" add "release-results/issue-60"
  git -C "$worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit -m "Record #60 release ${status,,} $RUN_ID" >/dev/null || return 1
  git -C "$worktree" push -q origin "HEAD:refs/heads/$REPORT_BRANCH" || return 1
  log "REDACTED_GITHUB_REPORT=https://github.com/gioahmad/city-manager-os/blob/$REPORT_BRANCH/release-results/issue-60/latest.md"
) }

on_error(){
  local rc=$?
  FAIL_LINE="${BASH_LINENO[0]:-$LINENO}"
  FAIL_COMMAND="$BASH_COMMAND"
  log "ERROR: command failed rc=$rc line=$FAIL_LINE phase=$CURRENT_PHASE"
  printf 'FAILED_COMMAND=%q\n' "$FAIL_COMMAND"
  trap - ERR
  exit "$rc"
}

on_exit(){
  local rc=$?
  trap - ERR EXIT
  if (( rc == 0 )); then FINAL_STATUS="PASS"; else rollback_runtime || true; fi
  printf '\n============================================================\n'
  printf '#60 RELEASE: %s rc=%s phase=%s line=%s\n' "$FINAL_STATUS" "$rc" "$CURRENT_PHASE" "${FAIL_LINE:-none}"
  printf 'FULL_LOCAL_LOG=%s\nFULL_LOCAL_LOG_MODE=600\nGITHUB_REPORT=REDACTED\n' "$LOG_FILE"
  printf '============================================================\n'
  publish_report "$FINAL_STATUS" "$rc" || log "WARNING: redacted GitHub report could not be published"
  exit "$rc"
}

trap on_error ERR
trap on_exit EXIT

CURRENT_PHASE="preflight"
section "#60 PSEG SPATIAL + ADJUSTABLE NJ STATEWIDE ALERTS"
for cmd in git docker python3 grep install tee sha256sum mktemp awk sed tail sort flock stat chown; do
  command -v "$cmd" >/dev/null || fail "$cmd is required"
done
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another #60 release runner is active"
cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "production repository must be clean"
git fetch -q origin main \
  "+refs/heads/$CANDIDATE_BRANCH:refs/remotes/origin/$CANDIDATE_BRANCH" \
  "+refs/heads/$REPORT_BRANCH:refs/remotes/origin/$REPORT_BRANCH"
TARGET_HEAD="$(git rev-parse "origin/$CANDIDATE_BRANCH")"
[[ "$(git rev-parse "$TARGET_HEAD^")" == "$EXPECTED_BASE" ]] || fail "#60 candidate is not one commit above the accepted base"
[[ "$(git diff --name-only "$EXPECTED_BASE".."$TARGET_HEAD" | sort)" == "$EXPECTED_PATHS" ]] \
  || fail "#60 candidate path manifest mismatch"

HEAD_SHA="$(git rev-parse HEAD)"
ORIGIN_SHA="$(git rev-parse origin/main)"
[[ "$ORIGIN_SHA" == "$EXPECTED_BASE" || "$ORIGIN_SHA" == "$TARGET_HEAD" ]] \
  || fail "origin/main moved beyond the #60 release boundary"
[[ "$HEAD_SHA" == "$EXPECTED_BASE" || "$HEAD_SHA" == "$TARGET_HEAD" ]] \
  || fail "local main is outside the resumable #60 release boundary"
if [[ "$HEAD_SHA" != "$TARGET_HEAD" ]]; then git merge --ff-only "$TARGET_HEAD"; fi
[[ "$(git rev-parse HEAD)" == "$TARGET_HEAD" ]] || fail "local main did not reach the exact candidate"
[[ -z "$(git status --porcelain)" ]] || fail "repository changed during candidate preparation"

REQUIRED_CONTAINERS=(citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine citymanager-postgis n8n ntfy)
for name in "${REQUIRED_CONTAINERS[@]}"; do
  [[ "$(docker inspect "$name" --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
    || fail "required container is not running: $name"
done
N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
[[ -n "$N8N_DIR" && "$N8N_DIR" == /* && "$N8N_DIR" != / ]] \
  || fail "n8n data mount is missing or unsafe"
N8N_DB="$N8N_DIR/database.sqlite"
[[ -f "$N8N_DB" ]] || fail "n8n database was not found"
N8N_DB_OWNER="$(stat -c '%u:%g' "$N8N_DB")"
UNCHANGED_BEFORE="$(for name in citymanager-staff citymanager-ops-engine citymanager-postgis ntfy; do
  docker inspect "$name" --format '{{.Name}}|{{.Image}}|{{.State.StartedAt}}'
done)"
printf 'BASE=%s\nTARGET=%s\n' "$EXPECTED_BASE" "$TARGET_HEAD"

CURRENT_PHASE="static-validation"
section "1. PINNED CHANGESET AND FOCUSED CONTRACTS"
git diff --check "$EXPECTED_BASE".."$TARGET_HEAD"
python3 -m py_compile dashboard/pseg_engine.py dashboard/geo_resolver.py \
  dashboard/integration_worker.py dashboard/integrations_app.py dashboard/tests/test_pseg_spatial_alerts.py
python3 -m json.tool workflows/sources/PSEG_Unified_Spatial_Statewide_v2.json >/dev/null
bash -n \
  deploy/n8n/install_pseg_unified.sh \
  deploy/pseg/install_pseg_spatial_alerts.sh \
  deploy/releases/issue-60-pseg-spatial-alerts.sh
PLAN_OUTPUT="$(./deploy/cmos-deploy plan --base "$EXPECTED_BASE" --target "$TARGET_HEAD")"
printf '%s\n' "$PLAN_OUTPUT"
grep -Fq 'postgis-migration' <<< "$PLAN_OUTPUT" || fail "change planner did not see the PSEG migration"
grep -Fq 'n8n-workflow-publish' <<< "$PLAN_OUTPUT" || fail "change planner did not see the PSEG workflow"
log "BOUNDED PLAN: one cached image build; Dashboard + Integration Engine only; one n8n publish; additive migration; focused acceptance; no full E2E"

CURRENT_PHASE="focused-build-test"
section "2. ONE CACHED BUILD AND FOCUSED TEST PASS"
OLD_DASH_IMAGE="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
OLD_LATEST_IMAGE="$(docker image inspect dashboard-citymanager-dashboard:latest --format '{{.Id}}' 2>/dev/null || true)"
if ! image_ready; then
  docker compose -f dashboard/docker-compose.yml build citymanager-dashboard
  IMAGE_BUILT=1
fi
if [[ "$(state_get TEST_TARGET)" != "$TARGET_HEAD" ]]; then
  docker compose -f dashboard/docker-compose.yml run --rm --no-deps -T \
    -v "$REPO:/repo:ro" -w /repo/dashboard -e PYTHONPATH=/repo/dashboard:/app \
    --entrypoint pytest citymanager-dashboard -p no:cacheprovider -q \
    tests/test_pseg_spatial_alerts.py tests/test_alert_geo_resolution.py
  docker compose -f dashboard/docker-compose.yml run --rm --no-deps -T \
    -v "$REPO:/repo:ro" -w /repo/dashboard -e PYTHONPATH=/repo/dashboard:/app \
    --entrypoint python citymanager-dashboard - <<'PY'
from jinja2 import Environment,FileSystemLoader
env=Environment(loader=FileSystemLoader('/repo/dashboard/templates'))
for name in ('integrations.html','pseg_settings.html'):
    env.get_template(name)
import phase3_app
paths={getattr(route,'path',None) for route in phase3_app.app.routes}
assert '/integrations/pseg' in paths and '/integrations/pseg/settings' in paths
assert '/api/pseg/release' in paths
print('PSEG DASHBOARD IMPORT + JINJA: PASS')
PY
  TEST_ACTION="focused-unit-contracts-pass"
  state_set TEST_TARGET "$TARGET_HEAD"
else
  TEST_ACTION="skipped-already-passed-for-candidate"
  log "Focused tests already passed for the exact candidate"
fi

CURRENT_PHASE="database"
section "3. ADDITIVE PSEG POLICY AND SOURCE STATE"
if database_ready; then
  DATABASE_ACTION="skipped-already-ready"
  log "PSEG database contract already installed"
else
  ./deploy/postgis/verify-backup.sh
  ./deploy/pseg/install_pseg_spatial_alerts.sh "$TARGET_HEAD"
  database_ready || fail "PSEG database readiness failed after migration"
  DATABASE_ACTION="verified-backup-and-additive-migration"
fi
state_set DATABASE_TARGET "$TARGET_HEAD"

CURRENT_PHASE="application"
section "4. DASHBOARD + EXISTING INTEGRATION ENGINE"
if application_ready; then
  APPLICATION_ACTION="skipped-already-ready"
  log "Dashboard and shared image already expose the exact #60 release"
else
  [[ -n "$OLD_DASH_IMAGE" ]] || OLD_DASH_IMAGE="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
  APPLICATION_CHANGED=1
  docker compose -f dashboard/docker-compose.yml up -d --no-deps --force-recreate \
    citymanager-dashboard citymanager-integration-engine
  wait_dashboard || fail "Dashboard did not become ready"
  [[ "$(docker inspect citymanager-integration-engine --format '{{.State.Running}}')" == true ]] \
    || fail "Integration Engine did not remain running"
  application_ready || fail "Dashboard does not expose the #60 release marker"
  APPLICATION_ACTION="one-image-dashboard-and-integration-engine-restart"
fi
state_set APPLICATION_TARGET "$TARGET_HEAD"

CURRENT_PHASE="workflow"
section "5. ONE CENTRAL PSEG ROUTER; LEGACY DIRECT PATHS OFF"
if workflow_ready; then
  WORKFLOW_ACTION="skipped-already-ready"
  log "Unified PSEG router already active and legacy paths inactive"
else
  capture_workflow_inventory
  backup_n8n
  publish_report "PRECHANGE-REVIEW" 0 \
    || fail "reviewed PSEG workflow definitions could not be published before consolidation"
  ./deploy/n8n/install_pseg_unified.sh "$TARGET_HEAD"
  WORKFLOW_CHANGED=1
  workflow_ready || fail "PSEG workflow consolidation readiness failed"
  WORKFLOW_ACTION="captured-all-pseg-published-one-router-restarted-n8n"
fi
state_set WORKFLOW_TARGET "$TARGET_HEAD"

CURRENT_PHASE="source-readiness"
section "6. SAFE PROVIDER BASELINE AND LOCAL SPATIAL RESOLUTION"
for attempt in $(seq 1 12); do
  if pseg_poll_ready; then break; fi
  log "Waiting for the existing Integration Engine PSEG poll ($attempt/12)"
  sleep 5
done
if ! pseg_poll_ready; then
  log "Automatic poll not ready; running one locked forced acceptance poll"
  docker exec -i citymanager-integration-engine python - <<'PY'
import json
from pseg_engine import run_due_pseg
result=run_due_pseg(force=True)
print(json.dumps(result,sort_keys=True,default=str))
if not result.get('ok'): raise SystemExit(1)
PY
  PSEG_POLL_ACTION="one-forced-safe-acceptance-poll"
else
  PSEG_POLL_ACTION="automatic-15-minute-engine-poll-ready"
fi
for attempt in $(seq 1 12); do
  if pseg_poll_ready; then break; fi
  log "Waiting for the in-flight PSEG acceptance poll ($attempt/12)"
  sleep 5
done
pseg_poll_ready || fail "PSEG source did not reach healthy #60 readiness"

docker exec citymanager-dashboard python geo_resolver.py backfill --limit 500 --since-days 30 --source PSEG

CURRENT_PHASE="focused-production-acceptance"
section "7. READ-ONLY FOCUSED PROBES; NO SYNTHETIC NOTIFICATION"
docker exec -i citymanager-dashboard python - "$RELEASE_ID" <<'PY'
import json,os,sys,urllib.request
token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
assert token,'CMOS_AUTOMATION_TOKEN is required for authenticated probes'
headers={'X-CMOS-Automation-Key':token}
for path in ('/api/pseg/release','/integrations/pseg','/map/system/alerts.geojson?days=30'):
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers)
    with urllib.request.urlopen(request,timeout=60) as response:
        body=response.read()
        assert response.status==200 and body
        if path.startswith('/api/pseg'):
            payload=json.loads(body); assert payload['release_id']==sys.argv[1]
print('AUTHENTICATED PSEG + MAPPING PROBES: PASS')
PY

DB_ACCEPTANCE="$(db_at <<'SQL'
SELECT
  NOT EXISTS(SELECT 1 FROM pseg_outage_state WHERE scope='STATEWIDE' AND county='HUDSON')
  AND NOT EXISTS(
    SELECT 1 FROM alerts
    WHERE source='PSEG' AND metadata->>'location_approximate'='true' AND geom IS NOT NULL
  )
  AND EXISTS(
    SELECT 1 FROM source_health
    WHERE source_id='PSEG' AND status='OK'
      AND metadata->>'release_id'='issue-60-pseg-spatial-alerts-v1'
  );
SQL
)"
[[ "$DB_ACCEPTANCE" == t ]] || fail "PSEG production data contract failed"
workflow_ready || fail "unified PSEG workflow lost readiness"
application_ready || fail "PSEG application lost readiness"

UNCHANGED_AFTER="$(for name in citymanager-staff citymanager-ops-engine citymanager-postgis ntfy; do
  docker inspect "$name" --format '{{.Name}}|{{.Image}}|{{.State.StartedAt}}'
done)"
[[ "$UNCHANGED_AFTER" == "$UNCHANGED_BEFORE" ]] \
  || fail "a protected service was rebuilt or restarted"
log "PROTECTED SERVICES UNCHANGED: Staff, Operations Engine, PostGIS, ntfy"
log "FULL E2E: intentionally not run; focused #60 contracts and authenticated production probes passed"

CURRENT_PHASE="promotion"
section "8. PROMOTE EXACT ACCEPTED CANDIDATE"
git fetch -q origin main
REMOTE_NOW="$(git rev-parse origin/main)"
if [[ "$REMOTE_NOW" == "$EXPECTED_BASE" ]]; then
  git push -q origin "$TARGET_HEAD:refs/heads/main"
elif [[ "$REMOTE_NOW" != "$TARGET_HEAD" ]]; then
  fail "origin/main changed during #60; candidate was not pushed"
fi
git fetch -q origin main
[[ "$(git rev-parse origin/main)" == "$TARGET_HEAD" ]] || fail "origin/main did not reach the candidate"
[[ -z "$(git status --porcelain)" ]] || fail "production repository is not clean"
state_set ACCEPTED_TARGET "$TARGET_HEAD"
APPLICATION_CHANGED=0
WORKFLOW_CHANGED=0
CURRENT_PHASE="complete"
section "#60 RELEASE ACCEPTED"
printf 'BASE=%s\nTARGET=%s\nDATABASE=%s\nAPPLICATION=%s\nWORKFLOW=%s\nPSEG_POLL=%s\n' \
  "$EXPECTED_BASE" "$TARGET_HEAD" "$DATABASE_ACTION" "$APPLICATION_ACTION" "$WORKFLOW_ACTION" "$PSEG_POLL_ACTION"
printf 'FULL_E2E=NOT_RUN\nSYNTHETIC_NOTIFICATIONS=NONE\nPROTECTED_SERVICE_RESTARTS=NONE\n'
