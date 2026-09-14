# City Manager OS issue #56 release report

> This repository is public. This report is intentionally redacted. The complete mode-600 log remains on the VPS.

| Field | Value |
|---|---|
| Run | `20260914T004454Z-2858741` |
| Release | `issue-56-unified-spatial-watch-pack-v1` |
| Status | **FAIL** |
| Exit code | `1` |
| Failed line | `406` |
| Phase | `targeted-build-test` |
| Database | `not-started` |
| Central matcher | `not-started` |
| Dashboard | `not-started` |
| Secure E2E | `not-started` |
| Started UTC | `2026-09-14T00:44:54Z` |
| Finished UTC | `2026-09-14T00:45:05Z` |
| Accepted base | `c68b88461578d135db114681d0a4bdae76a4b3ee` |
| Candidate | `26a76d501b1a959f66983976ac803d11da2319de` |
| Local HEAD | `26a76d501b1a959f66983976ac803d11da2319de` |
| Origin main | `c68b88461578d135db114681d0a4bdae76a4b3ee` |
| Full local log | `/var/log/city-manager-os/releases/issue-56-20260914T004454Z-2858741.log` |
| Full log SHA-256 | `4d6a0bdb0b4331644a8078470fc841865c891009e05c42c93f9f32b56bd1a81b` |

## Working tree

```text
unavailable
```

## Redacted diagnostic output

```text
#56 UNIFIED SPATIAL WATCH PACK
 dashboard/Dockerfile                               |   1 +
 dashboard/map_app.py                               |  23 +-
 dashboard/phase3_app.py                            |   2 +
 dashboard/spatial_watch_app.py                     | 508 +++++++++++++++++++++
 dashboard/templates/map.html                       |  17 +-
 dashboard/templates/watchlist.html                 | 164 ++++++-
 dashboard/tests/test_spatial_watch_pack.py         | 123 +++++
 create mode 100644 dashboard/spatial_watch_app.py
 create mode 100644 dashboard/tests/test_spatial_watch_pack.py
BASE=c68b88461578d135db114681d0a4bdae76a4b3ee
1. PINNED CANDIDATE AND CHANGE-AWARE PLAN
STATIC PASS test_application_composition_includes_spatial_watch_module
STATIC PASS test_browser_uses_existing_watchlist_resolver_subscribers_and_routing
STATIC PASS test_central_matcher_adds_spatial_match_without_parallel_delivery
STATIC PASS test_guarded_database_installer_covers_point_corridor_and_no_writes
STATIC PASS test_mapping_center_previews_and_reuses_canonical_references
STATIC PASS test_spatial_watch_migration_reuses_canonical_systems
STATIC PASS targeted shared-image delta
CMOS CHANGE-AWARE DEPLOYMENT PLAN
base=c68b88461578d135db114681d0a4bdae76a4b3ee
target=26a76d501b1a959f66983976ac803d11da2319de
changed=dashboard/Dockerfile
changed=dashboard/map_app.py
changed=dashboard/phase3_app.py
changed=dashboard/spatial_watch_app.py
changed=dashboard/templates/map.html
changed=dashboard/templates/watchlist.html
changed=dashboard/tests/test_spatial_watch_pack.py
changed=deploy/gis/install_spatial_watch_pack.sh
changed=deploy/n8n/install_spatial_watch_matcher.sh
changed=deploy/postgis/init/032_unified_spatial_watch_pack.sql
changed=docs/UNIFIED_SPATIAL_WATCH_PACK.md
changed=modules/ALERT_ROUTER.md
changed=modules/WATCHLIST.md
changed=workflows/core/CORE_Watchlist_Matcher_v1.json
changed=workflows/live/CORE_Watchlist_Matcher_live.json
build=yes
services=citymanager-dashboard,citymanager-integration-engine,citymanager-ops-engine,citymanager-staff
tests=tests/test_attention_engine.py,tests/test_event_source_pack.py,tests/test_executive_workflow.py,tests/test_gis_import.py,tests/test_source_onboarding.py,tests/test_spatial_watch_pack.py,tests/test_today_board.py
probes=/map,/map/gis/status,/map/system/alerts.geojson?days=30&min_priority=1
backup_required=yes
external=n8n-workflow-publish,postgis-migration
full_e2e=yes
unknown=none
[2026-09-14T00:44:56Z] CHANGE-AWARE ACTION: one cached build, Dashboard-only recreation, one n8n restart, no other service restart
2. ONE TARGETED BUILD AND TEST PASS
 Image dashboard-citymanager-dashboard Building 
#30 [25/40] COPY spatial_reference_app.py .
#31 [26/40] COPY spatial_watch_app.py .
 Image dashboard-citymanager-dashboard Built 
 Container dashboard-citymanager-dashboard-run-15b931d27177 Creating 
 Container dashboard-citymanager-dashboard-run-15b931d27177 Created 
=================================== FAILURES ===================================
        assert "import spatial_reference_app" in phase3
        assert '"key": "spatial-references"' in map_app
        assert "SELECT 'REFERENCE' AS result_type" in map_app
>       assert "coalesce(spatial_geom,geom)" in map_app.lower()
E       assert 'coalesce(spatial_geom,geom)' in 'import json\nimport re\nimport uuid\nfrom datetime import date, datetime, timedelta\nfrom decimal import decimal\nfro...er by e.starts_at,e.priority desc limit 2000\n        """\n    )\n    return jsonresponse(_feature_collection(rows))\n'
tests/test_spatial_reference_catalog.py:82: AssertionError
FAILED tests/test_spatial_reference_catalog.py::test_composition_and_mapping_layer
1 failed, 23 passed in 0.46s
[2026-09-14T00:45:05Z] ERROR: command failed rc=1 line=406 phase=targeted-build-test
#56 RELEASE: FAIL rc=1 phase=targeted-build-test line=406
```
