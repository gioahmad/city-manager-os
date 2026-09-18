#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED_BASE="c99a5abc38503089d535c90b8f3996cccf9713ad"
REPORT_BRANCH="release-output/ops"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
LOG_FILE="$LOG_DIR/spatial-watch-center-marker-$RUN_ID.log"
LOCK_FILE="/var/lock/cmos-spatial-watch-center-marker.lock"
CURRENT_PHASE="startup"
FINAL_STATUS="FAIL"
FAIL_LINE="none"
TARGET_HEAD="unknown"
ORIGINAL_HEAD="unknown"
ARCHIVE_BRANCH="none"
REPOSITORY_ACTION="not-started"
DASHBOARD_ACTION="not-started"
ACCEPTANCE_ACTION="not-started"
DASHBOARD_PREPARED=0
DASHBOARD_CHANGED=0
DASHBOARD_ROLLBACK_TAG=""
ACCEPTANCE_RESULT='{}'

EXPECTED_PATHS=$'dashboard/templates/map.html\ndashboard/tests/test_spatial_watch_pack.py\ndeploy/releases/spatial-watch-center-marker.sh'

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

dashboard_release_is_live(){
  docker exec -i citymanager-dashboard python - <<'PY' >/dev/null 2>&1
import os,urllib.request

token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: raise SystemExit(1)
request=urllib.request.Request(
    'http://127.0.0.1:8000/map',
    headers={'X-CMOS-Automation-Key':token},
)
with urllib.request.urlopen(request,timeout=20) as response:
    body=response.read().decode(errors='replace')
    if response.status!=200 or response.headers.get('Referrer-Policy')!='no-referrer': raise SystemExit(1)
    required=(
        'function watchCenterMarker',
        "bindTooltip('Watch center'",
        "pane:'markerPane'",
        "if(key==='watchlist')addWatchCenter(feature,layer)",
        "if(key==='watchlist')removeLayer('watch-centers')",
        'L.featureGroup([previewArea,previewCenter])',
        'One-mile Watch preview · center marked',
    )
    if not all(marker in body for marker in required): raise SystemExit(1)
    if 'Internal Server Error' in body: raise SystemExit(1)
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
  parent="$(mktemp -d /tmp/cmos-watch-center-report.XXXXXX)"
  worktree="$parent/worktree"
  git -C "$REPO" worktree add --detach "$worktree" "origin/$REPORT_BRANCH" >/dev/null 2>&1 || return 1
  report_dir="$worktree/operation-results/spatial-watch-center-marker"
  mkdir -p "$report_dir"
  report_file="$report_dir/$RUN_ID-${status,,}.md"
  {
    printf '# Spatial Watch center marker\n\n'
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
    printf '| Focused acceptance | `%s` |\n' "$ACCEPTANCE_ACTION"
    printf '| Database/schema changes | `none` |\n'
    printf '| Watch/Recipient/Notification changes | `none` |\n'
    printf '| Full E2E | `not run` |\n'
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '## Sanitized acceptance\n\n```json\n%s\n```\n\n' "$ACCEPTANCE_RESULT"
    printf 'No addresses, coordinates, Recipient details, Watch payloads, credentials, or private origins are included. '
    printf 'The private mode-600 log remains on the VPS at `%s`.\n' "$LOG_FILE"
  } > "$report_file"
  install -m 600 "$report_file" "$report_dir/latest.md"
  git -C "$worktree" add operation-results/spatial-watch-center-marker
  git -C "$worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit \
    -m "Record Watch center marker ${status,,} $RUN_ID" >/dev/null || return 1
  git -C "$worktree" push -q origin "HEAD:refs/heads/$REPORT_BRANCH" || return 1
  log "SANITIZED_REPORT=https://github.com/gioahmad/city-manager-os/blob/$REPORT_BRANCH/operation-results/spatial-watch-center-marker/latest.md"
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
    restore_checkout || log "WARNING: repository restore was not confirmed"
  fi
  (( rc == 0 )) && FINAL_STATUS="PASS"
  section "SPATIAL WATCH CENTER MARKER: $FINAL_STATUS"
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
flock -n 9 || fail "another Watch center marker release is already running"

CURRENT_PHASE="preflight"
section "SPATIAL WATCH CENTER MARKER"
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
  ARCHIVE_BRANCH="production-archive/watch-center-$RUN_ID"
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
[[ "$(docker inspect citymanager-dashboard --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] || fail "required container is not running: citymanager-dashboard"

PLAN="$(python3 deploy/cmos-deploy plan --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null)"
printf '%s\n' "$PLAN"
grep -q '^changed_count=3$' <<<"$PLAN"
grep -q '^build=yes$' <<<"$PLAN"
grep -q '^services=citymanager-dashboard$' <<<"$PLAN"
grep -q '^tests=tests/test_gis_import.py,tests/test_spatial_watch_pack.py$' <<<"$PLAN"
grep -q '^backup_required=no$' <<<"$PLAN"
grep -q '^external=none$' <<<"$PLAN"
grep -q '^full_e2e=no$' <<<"$PLAN"
grep -q '^unknown=none$' <<<"$PLAN"
log "PREFLIGHT PASS: presentation-only center marker; no database, Watch, Recipient, routing, or Notification changes"

CURRENT_PHASE="running-receipt"
publish_report "RUNNING" "0" || log "WARNING: start receipt could not be published"

CURRENT_PHASE="change-aware-verification"
if dashboard_release_is_live; then
  DASHBOARD_ACTION="already-current-no-build"
else
  old_image="$(docker inspect citymanager-dashboard --format '{{.Image}}')"
  DASHBOARD_ROLLBACK_TAG="dashboard-citymanager-dashboard:cmos-watch-center-rollback-$RUN_ID"
  docker image tag "$old_image" "$DASHBOARD_ROLLBACK_TAG"
  DASHBOARD_PREPARED=1
  python3 deploy/cmos-deploy verify --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null
  DASHBOARD_ACTION="verified-one-build"
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
  dashboard_release_is_live || fail "Watch center marker contract is not live"
  DASHBOARD_ACTION="deployed-one-build-one-recreate"
fi

CURRENT_PHASE="focused-acceptance"
ACCEPTANCE_RESULT="$(docker exec -i citymanager-dashboard python - <<'PY'
import json,os,urllib.request
from app import db_conn

token=os.environ.get('CMOS_AUTOMATION_TOKEN','').strip()
if not token: raise RuntimeError('authenticated acceptance token is unavailable')
headers={'X-CMOS-Automation-Key':token}

def counts():
    with db_conn() as conn,conn.cursor() as cur:
        cur.execute('''SELECT
          (SELECT count(*) FROM alerts) AS alerts,
          (SELECT count(*) FROM watch_items) AS watches,
          (SELECT count(*) FROM map_layers) AS layers,
          (SELECT count(*) FROM map_features) AS features''')
        return dict(cur.fetchone())

def get(path,expect_json=False):
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers=headers)
    with urllib.request.urlopen(request,timeout=30) as response:
        body=response.read()
        if response.status!=200 or '/login' in response.geturl(): raise RuntimeError('authenticated map acceptance failed')
        if b'Internal Server Error' in body: raise RuntimeError('an internal error was exposed')
        return response.headers, json.loads(body) if expect_json else body.decode(errors='replace')

before=counts()
map_headers,map_page=get('/map')
_,watch_geojson=get('/map/system/watchlist.geojson',True)
after=counts()
if before!=after: raise RuntimeError('read-only map acceptance changed stored data')
if map_headers.get('Referrer-Policy')!='no-referrer': raise RuntimeError('private document referrer policy changed')
if watch_geojson.get('type')!='FeatureCollection' or not isinstance(watch_geojson.get('features'),list):
    raise RuntimeError('Watch map layer contract changed')
for marker in (
    'function watchCenterMarker',
    "bindTooltip('Watch center'",
    "pane:'markerPane'",
    "if(key==='watchlist')addWatchCenter(feature,layer)",
    "if(key==='watchlist')removeLayer('watch-centers')",
    'L.featureGroup([previewArea,previewCenter])',
    'One-mile Watch preview · center marked',
):
    if marker not in map_page: raise RuntimeError('Watch center marker is missing')

print(json.dumps({
    'dashboard_health':'PASS',
    'mapping_center':'PASS',
    'saved_watch_centers':'PASS',
    'one_mile_preview_center':'PASS',
    'layer_cleanup':'PASS',
    'private_document_policy':'PASS',
    'stored_data_unchanged':'PASS',
},sort_keys=True))
PY
)"
printf '%s\n' "$ACCEPTANCE_RESULT" | python3 -m json.tool
ACCEPTANCE_ACTION="pass-map-saved-preview-cleanup-no-data-change"

CURRENT_PHASE="final-health"
docker exec citymanager-dashboard python -c "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=10).status==200"

CURRENT_PHASE="complete"
section "RELEASE COMPLETE"
printf 'TARGET=%s\nDASHBOARD=%s\nDATABASE_SCHEMA_CHANGES=NONE\nFULL_E2E=NOT_RUN\n' "$TARGET_HEAD" "$DASHBOARD_ACTION"
