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

schema="$($HARNESS plan --files deploy/postgis/init/028_geo_resolution.sql)"
assert_line "$schema" "build=no"
assert_line "$schema" "backup_required=yes"
assert_line "$schema" "external=postgis-migration"
assert_line "$schema" "full_e2e=yes"

workflow="$($HARNESS plan --files workflows/core/CORE_Watchlist_Matcher_v1.json)"
assert_line "$workflow" "external=n8n-workflow-publish"
assert_line "$workflow" "full_e2e=yes"

docs="$($HARNESS plan --files docs/CHANGE_AWARE_DEPLOYMENT.md README.md)"
assert_line "$docs" "build=no"
assert_line "$docs" "services=none"
assert_line "$docs" "full_e2e=no"

statewide="$($HARNESS plan --files deploy/gis/import_statewide_gis.sh deploy/gis/statewide_archive.py)"
assert_line "$statewide" "build=no"
assert_line "$statewide" "services=none"
assert_line "$statewide" "backup_required=yes"
assert_line "$statewide" "external=postgis-statewide-import"
assert_line "$statewide" "full_e2e=no"

set +e
unknown="$($HARNESS plan --files unexplained/runtime.bin 2>&1)"
rc=$?
set -e
[ "$rc" -eq 2 ]
assert_line "$unknown" "unknown=unexplained/runtime.bin"

printf 'CMOS CHANGE-AWARE CLASSIFICATION TESTS: PASS\n'
