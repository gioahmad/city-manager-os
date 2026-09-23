#!/usr/bin/env bash
set -Eeuo pipefail

HARNESS="$(cd "$(dirname "$0")" && pwd)/cmos-deploy"

assert_line() {
  local output="$1"
  local expected="$2"
  grep -Fqx "$expected" <<<"$output" || {
    printf 'MISSING: %s\n' "$expected"
    printf '%s\n' "$output"
    return 1
  }
}

dashboard="$($HARNESS plan --files dashboard/templates/map.html dashboard/map_app.py)"
assert_line "$dashboard" "build=yes"
assert_line "$dashboard" "services=citymanager-dashboard"
assert_line "$dashboard" "probes=/map,/map/gis/status,/map/system/alerts.geojson?days=30&min_priority=1"
assert_line "$dashboard" "full_e2e=no"

composition="$($HARNESS plan --files dashboard/phase3_app.py)"
assert_line "$composition" "services=citymanager-dashboard"
assert_line "$composition" "full_e2e=yes"

ops="$($HARNESS plan --files dashboard/operations_engine.py)"
assert_line "$ops" "services=citymanager-ops-engine"
assert_line "$ops" "full_e2e=yes"

integration="$($HARNESS plan --files dashboard/integration_runtime.py)"
assert_line "$integration" "services=citymanager-dashboard,citymanager-integration-engine"
assert_line "$integration" "full_e2e=yes"

integrations_ui="$($HARNESS plan --files dashboard/integrations_app.py)"
assert_line "$integrations_ui" "services=citymanager-dashboard"
assert_line "$integrations_ui" "probes=/admin-tools,/event-intelligence,/integrations"
assert_line "$integrations_ui" "full_e2e=no"

configured_time="$($HARNESS plan --files dashboard/executive_workflow_app.py dashboard/flood_app.py dashboard/integrations_app.py dashboard/issues_app.py dashboard/operations_app.py dashboard/operations_occurrence_controls.py dashboard/operations_routines_app.py dashboard/schedule_app.py dashboard/today_board_app.py dashboard/transit_app.py)"
assert_line "$configured_time" "services=citymanager-dashboard"
assert_line "$configured_time" "probes=/admin-tools,/alerts,/event-intelligence,/flood,/inbox,/integrations,/issues,/my-day,/operations-routines,/schedule,/search,/source-health,/staff-admin,/today-board,/transit,/what-changed"
assert_line "$configured_time" "full_e2e=no"

geo="$($HARNESS plan --files dashboard/geo_resolver.py dashboard/integration_worker.py dashboard/tests/test_alert_geo_resolution.py)"
assert_line "$geo" "services=citymanager-dashboard,citymanager-integration-engine"
assert_line "$geo" "probes=/api/watchlist/health,/map/gis/status,/map/system/alerts.geojson?days=30&min_priority=1"
assert_line "$geo" "tests=tests/test_alert_geo_resolution.py,tests/test_event_source_pack.py,tests/test_gis_import.py,tests/test_source_onboarding.py"
assert_line "$geo" "full_e2e=no"

schema="$($HARNESS plan --files deploy/postgis/init/028_geo_resolution.sql)"
assert_line "$schema" "build=no"
assert_line "$schema" "backup_required=yes"
assert_line "$schema" "external=postgis-migration"
assert_line "$schema" "full_e2e=yes"

workflow="$($HARNESS plan --files workflows/core/CORE_Watchlist_Matcher_v1.json)"
assert_line "$workflow" "external=n8n-workflow-publish"
assert_line "$workflow" "tests=tests/test_spatial_watch_pack.py,tests/test_watch_lab.py"
assert_line "$workflow" "full_e2e=no"

watch_lab="$($HARNESS plan --files dashboard/spatial_watch_app.py dashboard/templates/watchlist.html dashboard/static/watch_matcher.js)"
assert_line "$watch_lab" "services=citymanager-dashboard"
assert_line "$watch_lab" "probes=/api/spatial-watch/release,/api/watchlist/health,/watchlist"
assert_line "$watch_lab" "tests=tests/test_spatial_watch_pack.py,tests/test_watch_lab.py,tests/test_watchlist_reliability.py"
assert_line "$watch_lab" "full_e2e=no"

docs="$($HARNESS plan --files docs/CHANGE_AWARE_DEPLOYMENT.md README.md)"
assert_line "$docs" "build=no"
assert_line "$docs" "services=none"
assert_line "$docs" "probes=none"
assert_line "$docs" "full_e2e=no"

statewide="$($HARNESS plan --files deploy/gis/import_statewide_gis.sh deploy/gis/statewide_archive.py)"
assert_line "$statewide" "build=no"
assert_line "$statewide" "services=none"
assert_line "$statewide" "backup_required=yes"
assert_line "$statewide" "external=postgis-statewide-import"
assert_line "$statewide" "full_e2e=no"

lifecycle="$($HARNESS plan --files deploy/gis/refresh_statewide_gis.sh deploy/gis/statewide_bulk_refresh.py deploy/gis/install_statewide_gis_refresh_timer.sh)"
assert_line "$lifecycle" "build=no"
assert_line "$lifecycle" "services=none"
assert_line "$lifecycle" "backup_required=yes"
assert_line "$lifecycle" "external=postgis-statewide-import"
assert_line "$lifecycle" "full_e2e=no"

nyc="$($HARNESS plan --files deploy/gis/refresh_nyc_addresses.sh)"
assert_line "$nyc" "build=no"
assert_line "$nyc" "services=none"
assert_line "$nyc" "backup_required=yes"
assert_line "$nyc" "external=postgis-nyc-address-import"
assert_line "$nyc" "tests=tests/test_alert_geo_resolution.py"
assert_line "$nyc" "full_e2e=no"

set +e
unknown="$($HARNESS plan --files unexplained/runtime.bin 2>&1)"
rc=$?
set -e
[ "$rc" -eq 2 ]
assert_line "$unknown" "unknown=unexplained/runtime.bin"

printf 'CMOS CHANGE-AWARE CLASSIFICATION TESTS: PASS\n'
