#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED_BASE="2885db10df20707068dbbcb06b42f4f563138976"
REPORT_BRANCH="release-output/ops"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
LOG_FILE="$LOG_DIR/system-search-ntfy-clarity-$RUN_ID.log"
LOCK_FILE="/var/lock/cmos-system-search-ntfy-clarity.lock"
SENDER_BACKUP_DIR="/var/backups/city-manager-os/ntfy-match-explanations/$RUN_ID"
CURRENT_PHASE="startup"
FINAL_STATUS="FAIL"
FAIL_LINE="none"
TARGET_HEAD="unknown"
ORIGINAL_HEAD="unknown"
ARCHIVE_BRANCH="none"
REPOSITORY_ACTION="not-started"
DASHBOARD_ACTION="not-started"
SENDER_ACTION="not-started"
ACCEPTANCE_ACTION="not-started"
DASHBOARD_PREPARED=0
DASHBOARD_CHANGED=0
SENDER_CHANGED=0
DASHBOARD_ROLLBACK_TAG=""
ACCEPTANCE_RESULT='{}'

EXPECTED_PATHS=$'dashboard/map_app.py\ndashboard/operations_app.py\ndashboard/static/style.css\ndashboard/templates/deliveries.html\ndashboard/templates/map.html\ndashboard/templates/nav.html\ndashboard/templates/search.html\ndashboard/tests/test_global_search_match_explanations.py\ndeploy/n8n/install_ntfy_match_explanations.sh\ndeploy/releases/system-search-ntfy-clarity.sh\nworkflows/core/CORE_ntfy_Sender_v1.json'

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

dashboard_release_is_live(){
  docker exec -i citymanager-dashboard python - <<'PY' >/dev/null 2>&1
import os,urllib.request
token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: raise SystemExit(1)
headers={'X-CMOS-Automation-Key':token}
required={
  '/search':('Search Everything','does not create another database'),
  '/deliveries':('Why you received this','Notification History'),
  '/map':('initialMapQuery','FEMA Flood Zones'),
}
for path,markers in required.items():
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers)
    with urllib.request.urlopen(request,timeout=20) as response:
        body=response.read().decode(errors='replace')
        if response.status!=200 or '/login' in response.geturl(): raise SystemExit(1)
        if 'Internal Server Error' in body or not all(marker in body for marker in markers): raise SystemExit(1)
PY
}

sender_release_is_live(){
  local n8n_dir n8n_db
  n8n_dir="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"
  n8n_db="$n8n_dir/database.sqlite"
  [[ -f "$n8n_db" ]] || return 1
  python3 - "$n8n_db" <<'PY' >/dev/null 2>&1
import json,sqlite3,sys
con=sqlite3.connect(sys.argv[1]); con.row_factory=sqlite3.Row
rows=con.execute("SELECT active,activeVersionId,nodes FROM workflow_entity WHERE name='CORE - ntfy Sender v1'").fetchall()
if len(rows)!=1 or not rows[0]['active'] or not rows[0]['activeVersionId']: raise SystemExit(1)
nodes={n.get('name'):n for n in json.loads(rows[0]['nodes'])}
code=((nodes.get('Prepare ntfy Requests') or {}).get('parameters') or {}).get('jsCode','')
required=('function explainMatch','Why you received this:','payload.match_reasons','Watch center')
if not all(marker in code for marker in required): raise SystemExit(1)
con.close()
PY
}

rollback_dashboard(){
  [[ "$DASHBOARD_PREPARED" == 1 && -n "$DASHBOARD_ROLLBACK_TAG" ]] || return 0
  docker image inspect "$DASHBOARD_ROLLBACK_TAG" >/dev/null 2>&1 || return 1
  docker image tag "$DASHBOARD_ROLLBACK_TAG" dashboard-citymanager-dashboard:latest
  if [[ "$DASHBOARD_CHANGED" != 1 ]]; then
    DASHBOARD_ACTION="build-rolled-back-no-recreate"
    return 0
  fi
  log "ROLLBACK: restoring the prior dashboard image"
  docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate citymanager-dashboard
  for _ in $(seq 1 30); do
    if docker exec citymanager-dashboard python -c \
      "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200" \
      >/dev/null 2>&1; then
      DASHBOARD_ACTION="rolled-back"
      return 0
    fi
    sleep 2
  done
  return 1
}

rollback_sender(){
  [[ "$SENDER_CHANGED" == 1 ]] || return 0
  local backup="$SENDER_BACKUP_DIR/CORE_ntfy_Sender_previous.json" n8n_dir n8n_db sender_id tmp
  [[ -s "$backup" ]] || return 1
  n8n_dir="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
  n8n_db="$n8n_dir/database.sqlite"
  sender_id="$(python3 - "$n8n_db" <<'PY'
import sqlite3,sys
con=sqlite3.connect(sys.argv[1])
rows=con.execute("SELECT id FROM workflow_entity WHERE name='CORE - ntfy Sender v1'").fetchall()
if len(rows)!=1: raise SystemExit(1)
print(rows[0][0]); con.close()
PY
)" || return 1
  tmp="/tmp/CORE_ntfy_Sender_release_rollback_${RUN_ID}.json"
  log "ROLLBACK: restoring the prior ntfy sender"
  docker cp "$backup" "n8n:$tmp" >/dev/null
  docker exec -u root n8n chown node:node "$tmp"
  docker exec -u root n8n chmod 600 "$tmp"
  docker exec -u node n8n n8n import:workflow --input="$tmp" >/dev/null
  if docker exec -u node n8n n8n publish:workflow --help >/dev/null 2>&1; then
    docker exec -u node n8n n8n publish:workflow --id="$sender_id" >/dev/null
  else
    docker exec -u node n8n n8n update:workflow --id="$sender_id" --active=true >/dev/null
  fi
  docker restart n8n >/dev/null
  for _ in $(seq 1 60); do
    if docker exec n8n node -e \
      "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
      >/dev/null 2>&1; then
      SENDER_ACTION="rolled-back"
      docker exec n8n rm -f "$tmp" >/dev/null 2>&1 || true
      return 0
    fi
    sleep 2
  done
  return 1
}

restore_checkout(){
  [[ "$REPOSITORY_ACTION" == fast-forwarded || "$REPOSITORY_ACTION" == archived-and-realigned || "$REPOSITORY_ACTION" == archive-created ]] || return 0
  [[ "$ORIGINAL_HEAD" != unknown && "$TARGET_HEAD" != unknown ]] || return 0
  [[ -z "$(git -C "$REPO" status --porcelain)" ]] || return 1
  [[ "$(git -C "$REPO" rev-parse refs/heads/main)" == "$TARGET_HEAD" ]] || return 1
  git -C "$REPO" switch --detach "$TARGET_HEAD" >/dev/null
  git -C "$REPO" update-ref refs/heads/main "$ORIGINAL_HEAD" "$TARGET_HEAD"
  git -C "$REPO" switch main >/dev/null
  REPOSITORY_ACTION="restored-after-release-failure"
}

publish_report(){ (
  set +e
  local status="$1" rc="$2" parent="" worktree="" report_dir report_file
  cleanup_report(){
    [[ -z "$worktree" ]] || git -C "$REPO" worktree remove --force "$worktree" >/dev/null 2>&1 || true
    [[ -z "$parent" ]] || rmdir "$parent" >/dev/null 2>&1 || true
  }
  trap cleanup_report EXIT
  git -C "$REPO" fetch -q origin "+refs/heads/$REPORT_BRANCH:refs/remotes/origin/$REPORT_BRANCH" || return 1
  parent="$(mktemp -d /tmp/cmos-system-search-report.XXXXXX)"
  worktree="$parent/worktree"
  git -C "$REPO" worktree add --detach "$worktree" "origin/$REPORT_BRANCH" >/dev/null 2>&1 || return 1
  report_dir="$worktree/operation-results/system-search-ntfy-clarity"
  mkdir -p "$report_dir"
  report_file="$report_dir/$RUN_ID-${status,,}.md"
  {
    printf '# System search and Notification clarity\n\n'
    printf '| Field | Value |\n|---|---|\n'
    printf '| Run | `%s` |\n' "$RUN_ID"
    printf '| Status | **%s** |\n' "$status"
    printf '| Exit code | `%s` |\n' "$rc"
    printf '| Phase | `%s` |\n' "$CURRENT_PHASE"
    printf '| Failed line | `%s` |\n' "$FAIL_LINE"
    printf '| Target | `%s` |\n' "$TARGET_HEAD"
    printf '| Repository alignment | `%s` |\n' "$REPOSITORY_ACTION"
    printf '| Recovery branch | `%s` |\n' "$ARCHIVE_BRANCH"
    printf '| Dashboard | `%s` |\n' "$DASHBOARD_ACTION"
    printf '| ntfy sender | `%s` |\n' "$SENDER_ACTION"
    printf '| Focused acceptance | `%s` |\n' "$ACCEPTANCE_ACTION"
    printf '| Database/schema changes | `none` |\n'
    printf '| Stored record changes | `none` |\n'
    printf '| Test Notification sent | `no` |\n'
    printf '| Full E2E | `not run` |\n'
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '## Sanitized acceptance\n\n```json\n%s\n```\n\n' "$ACCEPTANCE_RESULT"
    printf 'No addresses, coordinates, search terms, alert contents, Recipient details, channel names, credentials, or private origins are included. '
    printf 'The private mode-600 log remains on the VPS at `%s`.\n' "$LOG_FILE"
  } > "$report_file"
  install -m 600 "$report_file" "$report_dir/latest.md"
  git -C "$worktree" add operation-results/system-search-ntfy-clarity
  git -C "$worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit \
    -m "Record system search and Notification clarity ${status,,} $RUN_ID" >/dev/null || return 1
  git -C "$worktree" push -q origin "HEAD:refs/heads/$REPORT_BRANCH" || return 1
  log "SANITIZED_REPORT=https://github.com/gioahmad/city-manager-os/blob/$REPORT_BRANCH/operation-results/system-search-ntfy-clarity/latest.md"
) }

on_error(){
  local rc=$?
  FAIL_LINE="${BASH_LINENO[0]:-$LINENO}"
  log "ERROR: release failed rc=$rc line=$FAIL_LINE phase=$CURRENT_PHASE"
  trap - ERR
  exit "$rc"
}

on_exit(){
  local rc=$?
  trap - ERR EXIT
  if (( rc == 0 )) && [[ "$CURRENT_PHASE" != complete ]]; then rc=1; FAIL_LINE="unexpected-eof"; fi
  if (( rc != 0 )); then
    rollback_dashboard || log "WARNING: dashboard rollback was not confirmed"
    rollback_sender || log "WARNING: ntfy sender rollback was not confirmed"
    restore_checkout || log "WARNING: repository restore was not confirmed"
  fi
  (( rc == 0 )) && FINAL_STATUS="PASS"
  section "SYSTEM SEARCH AND NTFY CLARITY: $FINAL_STATUS"
  printf 'STATUS=%s\nPHASE=%s\nTARGET=%s\nPRIVATE_LOG=%s\n' "$FINAL_STATUS" "$CURRENT_PHASE" "$TARGET_HEAD" "$LOG_FILE"
  publish_report "$FINAL_STATUS" "$rc" || log "WARNING: sanitized GitHub report could not be published"
  exit "$rc"
}

trap on_error ERR
trap on_exit EXIT

for command in git docker python3 flock mktemp install tee; do
  command -v "$command" >/dev/null || fail "$command is required"
done
mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another system-search release is already running"

CURRENT_PHASE="preflight"
section "SYSTEM SEARCH AND NTFY CLARITY"
cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "production repository must be clean"
git fetch -q origin main "+refs/heads/$REPORT_BRANCH:refs/remotes/origin/$REPORT_BRANCH"
TARGET_HEAD="$(git rev-parse origin/main)"
git merge-base --is-ancestor "$EXPECTED_BASE" "$TARGET_HEAD" || fail "origin/main does not descend from the accepted production release"
ACTUAL_PATHS="$(git diff --name-only "$EXPECTED_BASE...$TARGET_HEAD" | LC_ALL=C sort)"
[[ "$ACTUAL_PATHS" == "$EXPECTED_PATHS" ]] || {
  printf 'EXPECTED PATHS:\n%s\nACTUAL PATHS:\n%s\n' "$EXPECTED_PATHS" "$ACTUAL_PATHS"
  fail "release scope changed; refusing an unreviewed deployment"
}

ORIGINAL_HEAD="$(git rev-parse HEAD)"
if [[ "$ORIGINAL_HEAD" == "$TARGET_HEAD" ]]; then
  REPOSITORY_ACTION="already-aligned"
elif git merge-base --is-ancestor "$ORIGINAL_HEAD" "$TARGET_HEAD"; then
  git merge --ff-only origin/main
  REPOSITORY_ACTION="fast-forwarded"
else
  ARCHIVE_BRANCH="production-archive/system-search-$RUN_ID"
  git branch "$ARCHIVE_BRANCH" "$ORIGINAL_HEAD"
  REPOSITORY_ACTION="archive-created"
  git switch --detach "$ORIGINAL_HEAD" >/dev/null
  git update-ref refs/heads/main "$TARGET_HEAD" "$ORIGINAL_HEAD"
  git switch main >/dev/null
  git branch --set-upstream-to=origin/main main >/dev/null
  REPOSITORY_ACTION="archived-and-realigned"
fi
[[ "$(git rev-parse HEAD)" == "$TARGET_HEAD" ]] || fail "production checkout did not reach the release target"
[[ -z "$(git status --porcelain)" ]] || fail "production repository is not clean after alignment"
for container in citymanager-dashboard n8n citymanager-postgis; do
  [[ "$(docker inspect "$container" --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
    || fail "required container is not running: $container"
done

PLAN="$(python3 deploy/cmos-deploy plan --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null)"
printf '%s\n' "$PLAN"
grep -q '^changed_count=11$' <<<"$PLAN"
grep -q '^build=yes$' <<<"$PLAN"
grep -q '^services=citymanager-dashboard$' <<<"$PLAN"
grep -q '^tests=tests/test_attention_engine.py,tests/test_gis_import.py,tests/test_global_search_match_explanations.py$' <<<"$PLAN"
grep -q '^backup_required=no$' <<<"$PLAN"
grep -q '^external=n8n-workflow-publish$' <<<"$PLAN"
grep -q '^full_e2e=yes$' <<<"$PLAN"
grep -q '^unknown=none$' <<<"$PLAN"
log "PREFLIGHT PASS: specialized focused acceptance replaces the classifier's conservative full-E2E flag; no database or stored record changes"

CURRENT_PHASE="running-receipt"
publish_report "RUNNING" "0" || log "WARNING: start receipt could not be published"

CURRENT_PHASE="change-aware-verification"
if dashboard_release_is_live; then
  DASHBOARD_ACTION="already-current-no-build"
else
  old_image="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
  DASHBOARD_ROLLBACK_TAG="dashboard-citymanager-dashboard:cmos-system-search-rollback-$RUN_ID"
  docker image tag "$old_image" "$DASHBOARD_ROLLBACK_TAG"
  DASHBOARD_PREPARED=1
  python3 deploy/cmos-deploy verify --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null
  DASHBOARD_ACTION="verified-one-build"
fi

CURRENT_PHASE="search-database-contract"
if [[ "$DASHBOARD_ACTION" == verified-one-build ]]; then
  docker compose -f "$REPO/dashboard/docker-compose.yml" run --rm --no-deps -T \
    --entrypoint python citymanager-dashboard - <<'PY'
import time
from operations_app import _global_search_rows

started=time.monotonic()
rows=_global_search_rows('CMOS-RELEASE-CONTRACT-NO-MATCH','all')
elapsed_ms=round((time.monotonic()-started)*1000)
if rows: raise RuntimeError('synthetic no-match query unexpectedly returned records')
if elapsed_ms>12000: raise RuntimeError('system-wide search exceeded its protected ceiling')
print(f'SEARCH_DATABASE_CONTRACT=PASS elapsed_ms={elapsed_ms} result_count=0')
PY
fi

CURRENT_PHASE="ntfy-sender-publish"
if sender_release_is_live; then
  SENDER_ACTION="already-current-no-restart"
else
  CMOS_NTFY_BACKUP_DIR="$SENDER_BACKUP_DIR" \
    deploy/n8n/install_ntfy_match_explanations.sh "$TARGET_HEAD"
  SENDER_CHANGED=1
  sender_release_is_live || fail "plain-language ntfy sender contract is not live"
  SENDER_ACTION="published-one-restart"
fi

CURRENT_PHASE="dashboard-deploy"
if [[ "$DASHBOARD_ACTION" == verified-one-build ]]; then
  DASHBOARD_CHANGED=1
  docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate citymanager-dashboard
  for _ in $(seq 1 30); do
    if docker exec citymanager-dashboard python -c \
      "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200" \
      >/dev/null 2>&1; then break; fi
    sleep 2
  done
  dashboard_release_is_live || fail "system search and Notification explanation contract is not live"
  DASHBOARD_ACTION="deployed-one-build-one-recreate"
fi

CURRENT_PHASE="focused-acceptance"
ACCEPTANCE_RESULT="$(docker exec -i citymanager-dashboard python - <<'PY'
import json,os,time,urllib.parse,urllib.request
from operations_app import _humanize_match_reason

token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: raise RuntimeError('authenticated acceptance token is unavailable')
headers={'X-CMOS-Automation-Key':token}

def get(path,expect_json=False,timeout=20):
    started=time.monotonic()
    request=urllib.request.Request(
        'http://127.0.0.1:8000'+path,
        headers=headers,
        method='GET',
    )
    with urllib.request.urlopen(request,timeout=timeout) as response:
        body=response.read()
        elapsed_ms=round((time.monotonic()-started)*1000)
        if response.status!=200 or '/login' in response.geturl(): raise RuntimeError('authenticated acceptance failed')
        if b'Internal Server Error' in body: raise RuntimeError('an internal error was exposed')
        return json.loads(body) if expect_json else body.decode(errors='replace'),elapsed_ms

search_home,search_home_ms=get('/search')
contract_query='CMOS-RELEASE-CONTRACT-NO-MATCH'
search_results,search_results_ms=get('/search?'+urllib.parse.urlencode({'q':contract_query,'scope':'all'}))
notifications,notifications_ms=get('/deliveries')
mapping,mapping_ms=get('/map')
map_results,map_search_ms=get('/map/search?'+urllib.parse.urlencode({'q':contract_query}),True)

if search_home_ms>15000 or search_results_ms>15000 or mapping_ms>15000 or map_search_ms>15000:
    raise RuntimeError('a search or map page exceeded the focused 15-second ceiling')
for marker in ('Search Everything','does not create another database','alerts, work, Watches, Notifications'):
    if marker not in search_home: raise RuntimeError('system search instructions are incomplete')
if 'Search is temporarily unavailable' in search_results:
    raise RuntimeError('system-wide search reached its protected timeout')
if contract_query not in search_results or 'No records matched' not in search_results:
    raise RuntimeError('bounded system-wide no-match search failed')
if 'Why you received this' not in notifications:
    raise RuntimeError('Notification History explanation is missing')
if 'initialMapQuery' not in mapping or 'FEMA Flood Zones' not in mapping:
    raise RuntimeError('Mapping Center performance contract is missing')
if map_results.get('type')!='FeatureCollection' or not isinstance(map_results.get('features'),list):
    raise RuntimeError('indexed Location search response changed')

keyword=_humanize_match_reason('CONTAINS search_text matched search_term "CONTRACT KEYWORD"')
location=_humanize_match_reason('PROXIMITY alert geometry is 125.0 ft from target, inside 5280.0 ft buffer')
if keyword!='Keyword “CONTRACT KEYWORD” matched this alert': raise RuntimeError('keyword explanation contract failed')
if 'Watch center' not in location or '5,280-foot Distance' not in location: raise RuntimeError('Location explanation contract failed')
if any(term in keyword+location for term in ('search_text','search_term','geometry','buffer')):
    raise RuntimeError('technical match terms were exposed')

print(json.dumps({
    'system_search':'PASS',
    'searched_sections':8,
    'notification_history_reason':'PASS',
    'ntfy_reason_contract':'PASS',
    'map_startup_contract':'PASS',
    'location_search_contract':'PASS',
    'acceptance_requests':'GET-only',
    'write_requests_sent':0,
    'test_notification_sent':'NO',
    'timing_ms':{
      'search_home':search_home_ms,
      'search_all_no_match':search_results_ms,
      'notification_history':notifications_ms,
      'mapping_center':mapping_ms,
      'location_search_no_match':map_search_ms,
    },
},sort_keys=True))
PY
)"
printf '%s\n' "$ACCEPTANCE_RESULT" | python3 -m json.tool
sender_release_is_live || fail "ntfy sender failed final publication check"
ACCEPTANCE_ACTION="pass-search-eight-sections-reasons-map-performance-no-data-change"

CURRENT_PHASE="final-health"
docker exec citymanager-dashboard python -c "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=10).status==200"
docker exec n8n node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CURRENT_PHASE="complete"
section "RELEASE COMPLETE"
printf 'TARGET=%s\nDASHBOARD=%s\nNTFY_SENDER=%s\nDATABASE_SCHEMA_CHANGES=NONE\nSTORED_RECORD_CHANGES=NONE\nFULL_E2E=NOT_RUN\n' \
  "$TARGET_HEAD" "$DASHBOARD_ACTION" "$SENDER_ACTION"
