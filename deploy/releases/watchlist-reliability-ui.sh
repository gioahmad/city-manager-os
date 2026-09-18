#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
umask 077

REPO="${CMOS_REPO:-/opt/city-manager-os}"
EXPECTED_BASE="e3ff9897ca3ec9375c4a2305e06fea1a674249dd"
RELEASE_ID="watchlist-reliability-ui-v1"
REPORT_BRANCH="release-output/ops"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="/var/log/city-manager-os/releases"
LOG_FILE="$LOG_DIR/watchlist-reliability-$RUN_ID.log"
LOCK_FILE="/var/lock/cmos-watchlist-reliability.lock"
PROBE_PREFIX="CMOS Watch Acceptance $RUN_ID"
CURRENT_PHASE="startup"
FINAL_STATUS="FAIL"
FAIL_LINE="none"
TARGET_HEAD="unknown"
ORIGINAL_HEAD="unknown"
ARCHIVE_BRANCH="none"
REPOSITORY_ACTION="not-started"
LOCAL_ONLY_COMMITS="unknown"
REMOTE_ONLY_COMMITS="unknown"
DEPLOYMENT_ACTION="not-started"
ACCEPTANCE_ACTION="not-started"
PROBE_CLEANUP_ACTION="not-needed"
HEALTH_RESULT='{}'

EXPECTED_PATHS=$'dashboard/spatial_watch_app.py\ndashboard/static/style.css\ndashboard/templates/watchlist.html\ndashboard/tests/test_spatial_watch_pack.py\ndashboard/tests/test_watchlist_reliability.py\ndeploy/ops/configure-isolated-radius-watch.sh\ndeploy/releases/watchlist-reliability-ui.sh'

section(){ printf '\n============================================================\n%s\n============================================================\n' "$1"; }
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail(){ log "ERROR: $*"; return 1; }

dashboard_release_is_live(){
  docker exec -i citymanager-dashboard python - "$RELEASE_ID" <<'PY' >/dev/null 2>&1
import json
import os
import sys
import urllib.request

token = os.environ.get("CMOS_AUTOMATION_TOKEN", "").strip()
if not token:
    raise SystemExit(1)
request = urllib.request.Request(
    "http://127.0.0.1:8000/api/spatial-watch/release",
    headers={"X-CMOS-Automation-Key": token},
)
with urllib.request.urlopen(request, timeout=10) as response:
    payload = json.load(response)
if payload.get("release_id") != sys.argv[1]:
    raise SystemExit(1)
PY
}

safe_git_value(){
  local value=""
  value="$(git -C "$REPO" "$@" 2>/dev/null || true)"
  printf '%s' "${value:-unavailable}"
}

cleanup_probe(){
  docker inspect citymanager-dashboard >/dev/null 2>&1 || return 0
  if docker exec -i citymanager-dashboard python - "$PROBE_PREFIX" <<'PY' >/dev/null 2>&1
import sys
from app import db_conn

with db_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM watch_items WHERE display_name=%s", (sys.argv[1],))
    conn.commit()
PY
  then
    PROBE_CLEANUP_ACTION="verified-clean"
  else
    PROBE_CLEANUP_ACTION="failed"
  fi
}

rollback_dashboard(){
  local rollback_tag="dashboard-citymanager-dashboard:cmos-deploy-rollback-citymanager-dashboard"
  docker image inspect "$rollback_tag" >/dev/null 2>&1 || {
    log "WARNING: dashboard rollback image is unavailable"
    return 1
  }
  log "ROLLBACK: restoring the pre-release dashboard image"
  docker image tag "$rollback_tag" dashboard-citymanager-dashboard:latest
  docker compose -f "$REPO/dashboard/docker-compose.yml" up -d --no-deps --force-recreate citymanager-dashboard
  for _ in $(seq 1 30); do
    if docker exec citymanager-dashboard python -c \
      "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200" \
      >/dev/null 2>&1; then
      DEPLOYMENT_ACTION="rolled-back-after-acceptance-failure"
      log "ROLLBACK PASS: pre-release dashboard restored"
      return 0
    fi
    sleep 2
  done
  log "WARNING: dashboard rollback did not reach ready state"
  return 1
}

restore_checkout(){
  [[ "$REPOSITORY_ACTION" == archived-and-realigned || "$REPOSITORY_ACTION" == archive-created ]] || return 0
  [[ "$ORIGINAL_HEAD" != unknown && "$TARGET_HEAD" != unknown ]] || return 0
  [[ -z "$(git -C "$REPO" status --porcelain)" ]] || {
    log "WARNING: repository changed after alignment; leaving recovery branch and reviewed main intact"
    return 1
  }
  local current_main
  current_main="$(git -C "$REPO" rev-parse refs/heads/main)"
  if [[ "$current_main" == "$ORIGINAL_HEAD" ]]; then
    REPOSITORY_ACTION="original-main-retained"
    return 0
  fi
  [[ "$current_main" == "$TARGET_HEAD" ]] || {
    log "WARNING: main moved after alignment; refusing an automatic repository restore"
    return 1
  }
  log "REPOSITORY RESTORE: returning main to the pre-release commit"
  git -C "$REPO" switch --detach "$TARGET_HEAD" >/dev/null
  git -C "$REPO" update-ref refs/heads/main "$ORIGINAL_HEAD" "$TARGET_HEAD"
  git -C "$REPO" switch main >/dev/null
  REPOSITORY_ACTION="restored-after-release-failure"
  log "REPOSITORY RESTORE PASS: original main restored; archive retained at $ARCHIVE_BRANCH"
}

publish_report(){ (
  set +e
  local status="$1" rc="$2" parent="" worktree="" report_dir report_file
  cleanup_report(){
    if [[ -n "$worktree" && "$worktree" == /tmp/cmos-watchlist-report.*/* ]]; then
      git -C "$REPO" worktree remove --force "$worktree" >/dev/null 2>&1 || true
    fi
    [[ -z "$parent" || "$parent" != /tmp/cmos-watchlist-report.* ]] || rmdir "$parent" >/dev/null 2>&1 || true
  }
  trap cleanup_report EXIT
  git -C "$REPO" fetch -q origin "+refs/heads/$REPORT_BRANCH:refs/remotes/origin/$REPORT_BRANCH" || return 1
  parent="$(mktemp -d /tmp/cmos-watchlist-report.XXXXXX)"
  worktree="$parent/worktree"
  git -C "$REPO" worktree add --detach "$worktree" "origin/$REPORT_BRANCH" >/dev/null 2>&1 || return 1
  report_dir="$worktree/operation-results/watchlist-reliability"
  mkdir -p "$report_dir"
  report_file="$report_dir/$RUN_ID-${status,,}.md"
  {
    printf '# Watchlist reliability release\n\n'
    printf '| Field | Value |\n|---|---|\n'
    printf '| Run | `%s` |\n' "$RUN_ID"
    printf '| Status | **%s** |\n' "$status"
    printf '| Exit code | `%s` |\n' "$rc"
    printf '| Phase | `%s` |\n' "$CURRENT_PHASE"
    printf '| Failed line | `%s` |\n' "$FAIL_LINE"
    printf '| Target | `%s` |\n' "$TARGET_HEAD"
    printf '| Original VPS head | `%s` |\n' "$ORIGINAL_HEAD"
    printf '| Repository alignment | `%s` |\n' "$REPOSITORY_ACTION"
    printf '| Recovery branch | `%s` |\n' "$ARCHIVE_BRANCH"
    printf '| Original-only commits | `%s` |\n' "$LOCAL_ONLY_COMMITS"
    printf '| Release-only commits | `%s` |\n' "$REMOTE_ONLY_COMMITS"
    printf '| Dashboard deployment | `%s` |\n' "$DEPLOYMENT_ACTION"
    printf '| Create, update and routing probe | `%s` |\n' "$ACCEPTANCE_ACTION"
    printf '| Temporary probe row | `%s` |\n' "$PROBE_CLEANUP_ACTION"
    printf '| Schema changes | `none` |\n'
    printf '| n8n changes | `none` |\n'
    printf '| Full E2E | `not run` |\n'
    printf '| Started UTC | `%s` |\n' "$STARTED_UTC"
    printf '| Finished UTC | `%s` |\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '## Sanitized watch health\n\n```json\n%s\n```\n\n' "$HEALTH_RESULT"
    printf 'The private mode-600 log remains on the VPS at `%s`.\n' "$LOG_FILE"
  } > "$report_file"
  install -m 600 "$report_file" "$report_dir/latest.md"
  git -C "$worktree" add operation-results/watchlist-reliability
  git -C "$worktree" -c user.name='City Manager OS Release Runner' \
    -c user.email='release-runner@localhost' commit \
    -m "Record watchlist reliability ${status,,} $RUN_ID" >/dev/null || return 1
  git -C "$worktree" push -q origin "HEAD:refs/heads/$REPORT_BRANCH" || return 1
  log "REDACTED_REPORT=https://github.com/gioahmad/city-manager-os/blob/$REPORT_BRANCH/operation-results/watchlist-reliability/latest.md"
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
  if (( rc == 0 )) && [[ "$CURRENT_PHASE" != "complete" ]]; then
    rc=1
    FAIL_LINE="unexpected-eof"
    log "ERROR: release ended before focused acceptance completed"
  fi
  cleanup_probe
  if (( rc != 0 )) && [[ "$DEPLOYMENT_ACTION" == "dashboard-only-pass" ]]; then
    rollback_dashboard || true
  fi
  if (( rc != 0 )); then
    restore_checkout || true
  fi
  (( rc == 0 )) && FINAL_STATUS="PASS"
  section "WATCHLIST RELIABILITY RELEASE: $FINAL_STATUS"
  printf 'STATUS=%s\nPHASE=%s\nTARGET=%s\nFULL_LOG=%s\n' \
    "$FINAL_STATUS" "$CURRENT_PHASE" "$TARGET_HEAD" "$LOG_FILE"
  publish_report "$FINAL_STATUS" "$rc" || log "WARNING: redacted GitHub report could not be published"
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
flock -n 9 || fail "another watchlist release is already running"

CURRENT_PHASE="preflight"
section "WATCHLIST RELIABILITY + SIMPLE SETUP"
cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "production repository must be clean"
git fetch -q origin main "+refs/heads/$REPORT_BRANCH:refs/remotes/origin/$REPORT_BRANCH"
TARGET_HEAD="$(git rev-parse origin/main)"
git merge-base --is-ancestor "$EXPECTED_BASE" "$TARGET_HEAD" \
  || fail "origin/main does not descend from the accepted watchlist base"

ACTUAL_PATHS="$(git diff --name-only "$EXPECTED_BASE...$TARGET_HEAD" | LC_ALL=C sort)"
[[ "$ACTUAL_PATHS" == "$EXPECTED_PATHS" ]] || {
  printf 'EXPECTED PATHS:\n%s\nACTUAL PATHS:\n%s\n' "$EXPECTED_PATHS" "$ACTUAL_PATHS"
  fail "release scope changed; refusing an unreviewed deployment"
}

ORIGINAL_HEAD="$(git rev-parse HEAD)"
LOCAL_ONLY_COMMITS="$(git rev-list --count "$TARGET_HEAD..$ORIGINAL_HEAD")"
REMOTE_ONLY_COMMITS="$(git rev-list --count "$ORIGINAL_HEAD..$TARGET_HEAD")"
if [[ "$ORIGINAL_HEAD" == "$TARGET_HEAD" ]]; then
  REPOSITORY_ACTION="already-aligned"
elif git merge-base --is-ancestor "$ORIGINAL_HEAD" "$TARGET_HEAD"; then
  git merge --ff-only origin/main
  REPOSITORY_ACTION="fast-forwarded"
else
  ARCHIVE_BRANCH="production-archive/watchlist-$RUN_ID"
  git branch "$ARCHIVE_BRANCH" "$ORIGINAL_HEAD"
  git show-ref --verify --quiet "refs/heads/$ARCHIVE_BRANCH" \
    || fail "could not preserve the original production commit"
  REPOSITORY_ACTION="archive-created"
  git switch --detach "$ORIGINAL_HEAD" >/dev/null
  git update-ref refs/heads/main "$TARGET_HEAD" "$ORIGINAL_HEAD"
  git switch main >/dev/null
  git branch --set-upstream-to=origin/main main >/dev/null
  REPOSITORY_ACTION="archived-and-realigned"
  log "REPOSITORY ALIGNMENT: preserved $ORIGINAL_HEAD as $ARCHIVE_BRANCH"
fi
[[ "$(git rev-parse HEAD)" == "$TARGET_HEAD" ]] || fail "production checkout did not reach release target"
[[ -z "$(git status --porcelain)" ]] || fail "production repository is not clean after alignment"

for name in citymanager-dashboard citymanager-postgis n8n ntfy; do
  [[ "$(docker inspect "$name" --format '{{.State.Running}}' 2>/dev/null || true)" == true ]] \
    || fail "required container is not running: $name"
done

PLAN="$(python3 deploy/cmos-deploy plan --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null)"
printf '%s\n' "$PLAN"
grep -q '^build=yes$' <<<"$PLAN"
grep -q '^services=citymanager-dashboard$' <<<"$PLAN"
grep -q '^backup_required=no$' <<<"$PLAN"
grep -q '^external=none$' <<<"$PLAN"
grep -q '^full_e2e=no$' <<<"$PLAN"
grep -q '^unknown=none$' <<<"$PLAN"
log "PREFLIGHT PASS: dashboard-only change, no schema/workflow/full-E2E action"

CURRENT_PHASE="deployment-receipt"
publish_report "RUNNING" "0" || log "WARNING: start receipt could not be published"

CURRENT_PHASE="change-aware-deploy"
if dashboard_release_is_live; then
  DEPLOYMENT_ACTION="already-current-no-build"
  log "DEPLOYMENT SKIP: expected watchlist release is already live"
else
  python3 deploy/cmos-deploy apply --base "$EXPECTED_BASE" --target "$TARGET_HEAD" </dev/null
  DEPLOYMENT_ACTION="dashboard-only-pass"
fi

CURRENT_PHASE="focused-acceptance"
HEALTH_RESULT="$(docker exec -i citymanager-dashboard python - "$RELEASE_ID" "$PROBE_PREFIX" <<'PY'
import json
import os
import sys
import urllib.parse
import urllib.request

from app import db_conn

release_id, probe_name = sys.argv[1:]
token = os.environ.get('CMOS_AUTOMATION_TOKEN', '').strip()
if not token:
    raise RuntimeError('CMOS_AUTOMATION_TOKEN is required for authenticated acceptance')
headers = {'X-CMOS-Automation-Key': token}

def get_json(path):
    request = urllib.request.Request('http://127.0.0.1:8000' + path, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200 or '/login' in response.geturl():
            raise RuntimeError(f'authenticated GET failed: {path}')
        return json.load(response)

def post_form(path, values):
    request = urllib.request.Request(
        'http://127.0.0.1:8000' + path,
        data=urllib.parse.urlencode(values).encode(),
        headers={**headers, 'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode(errors='replace')
        if response.status != 200 or '/login' in response.geturl() or 'Could not save:' in body:
            raise RuntimeError(f'authenticated form acceptance failed: {path}')

release = get_json('/api/spatial-watch/release')
if release.get('release_id') != release_id:
    raise RuntimeError('dashboard is not running the expected release')

try:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id::text FROM subscribers WHERE active ORDER BY created_at LIMIT 1")
            subscriber = cur.fetchone()
    if not subscriber:
        raise RuntimeError('no active subscriber is available to verify routing')

    post_form('/watchlist/create', {
        'display_name': probe_name,
        'setup_mode': 'KEYWORD',
        'search_term': 'CMOS_NONMATCHING_ACCEPTANCE_TOKEN',
        'match_mode': 'CONTAINS',
        'min_priority': '1',
        'duration': 'PERMANENT',
        'subscriber_ids': subscriber['id'],
    })

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text,watch_id FROM watch_items WHERE display_name=%s",
                (probe_name,),
            )
            watch = cur.fetchone()
    if not watch:
        raise RuntimeError('create probe did not persist its temporary watch')

    post_form(f"/watchlist/{watch['id']}/update", {
        'display_name': probe_name,
        'watch_type': 'PHRASE',
        'search_term': 'CMOS_NONMATCHING_ACCEPTANCE_TOKEN_UPDATED',
        'match_mode': 'CONTAINS',
        'min_priority': '1',
        'duration': 'PERMANENT',
        'radius_ft': '500',
        'active': '1',
        'subscriber_ids': subscriber['id'],
    })

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.search_term,w.nearby_enabled,
                       count(*) FILTER (WHERE wir.active AND s.active) AS active_routes
                FROM watch_items w
                LEFT JOIN watch_item_recipients wir ON wir.watch_item_id=w.id
                LEFT JOIN subscribers s ON s.id=wir.subscriber_id
                WHERE w.id=%s
                GROUP BY w.id
                """,
                (watch['id'],),
            )
            verified = cur.fetchone()
    if (
        not verified
        or verified['search_term'] != 'CMOS_NONMATCHING_ACCEPTANCE_TOKEN_UPDATED'
        or verified['nearby_enabled']
        or verified['active_routes'] != 1
    ):
        raise RuntimeError('create/update/routing acceptance did not match the expected state')
finally:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watch_items WHERE display_name=%s", (probe_name,))
        conn.commit()

health = get_json('/api/watchlist/health')
for key in ('spatial_missing_target', 'invalid_schedule', 'routes_to_inactive_subscribers'):
    if int(health.get(key) or 0) != 0:
        raise RuntimeError(f'watchlist health blocker: {key}={health.get(key)}')
if not health.get('spatial_matcher_ready'):
    raise RuntimeError('spatial matcher function is unavailable')

safe = {
    'status': health.get('status'),
    'active_now': int(health.get('active_now') or 0),
    'routed_now': int(health.get('routed_now') or 0),
    'unrouted_now': int(health.get('unrouted_now') or 0),
    'matches_7d': int(health.get('matches_7d') or 0),
    'sent_24h': int(health.get('sent_24h') or 0),
    'failed_24h': int(health.get('failed_24h') or 0),
    'create_update_routing_probe': 'PASS',
    'temporary_probe_removed': True,
}
print(json.dumps(safe, sort_keys=True))
PY
)"
printf '%s\n' "$HEALTH_RESULT" | python3 -m json.tool
ACCEPTANCE_ACTION="pass-temporary-row-removed"

CURRENT_PHASE="complete"
section "WATCHLIST RELEASE COMPLETE"
printf 'TARGET=%s\nDEPLOYED_SERVICE=citymanager-dashboard\n' "$TARGET_HEAD"
printf 'DATABASE_SCHEMA_CHANGES=NONE\nN8N_CHANGES=NONE\nFULL_E2E=NOT_RUN\n'
printf 'CREATE_UPDATE_ROUTING_PROBE=PASS\nTEMPORARY_PROBE=REMOVED\n'
