# City Manager OS issue #60 release report

> Public, intentionally redacted summary. The complete mode-600 log remains on the VPS.

| Field | Value |
|---|---|
| Run | `20260915T203351Z-3328437` |
| Release | `issue-60-pseg-spatial-alerts-v1` |
| Status | **PRECHANGE-REVIEW** |
| Exit code | `0` |
| Failed line | `none` |
| Phase | `workflow` |
| Database | `verified-backup-and-additive-migration` |
| Dashboard + Integration Engine | `one-image-dashboard-and-integration-engine-restart` |
| PSEG n8n consolidation | `not-started` |
| PSEG source poll | `not-started` |
| Focused tests | `focused-unit-contracts-pass` |
| Started UTC | `2026-09-15T20:33:51Z` |
| Finished UTC | `2026-09-15T20:34:55Z` |
| Accepted base | `7bd25e3e36ebc5151d5ed9381d000257f7b722f1` |
| Candidate | `0a9d0960cb74651dac3dafe389fe9fc04d8aa892` |
| Local HEAD | `0a9d0960cb74651dac3dafe389fe9fc04d8aa892` |
| Origin main | `7bd25e3e36ebc5151d5ed9381d000257f7b722f1` |
| Full local log | `/var/log/city-manager-os/releases/issue-60-20260915T203351Z-3328437.log` |
| Full log SHA-256 | `1de633e0fe1ad86ba610bfa91997535bd4fbb93497a935a283c5757c61aa1f5f` |

## Working tree

```text
unavailable
```

## Redacted diagnostic output

```text
#60 PSEG SPATIAL + ADJUSTABLE NJ STATEWIDE ALERTS
 dashboard/Dockerfile                               |    1 +
 dashboard/geo_resolver.py                          |   18 +-
 dashboard/integration_worker.py                    |   13 +
 dashboard/integrations_app.py                      |  139 +++
 dashboard/pseg_engine.py                           | 1206 ++++++++++++++++++++
 dashboard/templates/integrations.html              |    5 +
 dashboard/templates/pseg_settings.html             |   99 ++
 dashboard/tests/test_pseg_spatial_alerts.py        |  247 ++++
 create mode 100644 dashboard/pseg_engine.py
 create mode 100644 dashboard/templates/pseg_settings.html
 create mode 100644 dashboard/tests/test_pseg_spatial_alerts.py
 create mode 100644 workflows/sources/PSEG_Unified_Spatial_Statewide_v2.json
BASE=7bd25e3e36ebc5151d5ed9381d000257f7b722f1
TARGET=0a9d0960cb74651dac3dafe389fe9fc04d8aa892
CMOS CHANGE-AWARE DEPLOYMENT PLAN
base=7bd25e3e36ebc5151d5ed9381d000257f7b722f1
target=0a9d0960cb74651dac3dafe389fe9fc04d8aa892
changed=dashboard/Dockerfile
changed=dashboard/geo_resolver.py
changed=dashboard/integration_worker.py
changed=dashboard/integrations_app.py
changed=dashboard/pseg_engine.py
changed=dashboard/templates/integrations.html
changed=dashboard/templates/pseg_settings.html
changed=dashboard/tests/test_pseg_spatial_alerts.py
changed=deploy/n8n/install_pseg_unified.sh
changed=deploy/postgis/init/033_pseg_spatial_alerts.sql
changed=deploy/pseg/install_pseg_spatial_alerts.sh
changed=deploy/releases/issue-60-pseg-spatial-alerts.sh
changed=docs/PSEG_SPATIAL_ALERTS.md
changed=workflows/sources/PSEG_Unified_Spatial_Statewide_v2.json
build=yes
services=citymanager-dashboard,citymanager-integration-engine,citymanager-ops-engine,citymanager-staff
tests=tests/test_alert_geo_resolution.py,tests/test_attention_engine.py,tests/test_event_source_pack.py,tests/test_executive_workflow.py,tests/test_gis_import.py,tests/test_pseg_spatial_alerts.py,tests/test_source_onboarding.py,tests/test_today_board.py
probes=none
backup_required=yes
external=n8n-workflow-publish,postgis-migration
full_e2e=yes
unknown=none
reasons=dashboard application,dashboard presentation,database migration requires guarded feature installer,deployment/tooling only,documentation/tooling only,integration worker runtime,n8n workflow requires guarded publish,shared application image definition,shared local geo resolver runtime,test-only change
[2026-09-15T20:33:53Z] BOUNDED PLAN: one cached image build; Dashboard + Integration Engine only; one n8n publish; additive migration; focused acceptance; no full E2E
2. ONE CACHED BUILD AND FOCUSED TEST PASS
 Image dashboard-citymanager-dashboard Building 
#35 [30/41] COPY pseg_engine.py .
 Image dashboard-citymanager-dashboard Built 
 Container dashboard-citymanager-dashboard-run-35d5c0067a1e Creating 
 Container dashboard-citymanager-dashboard-run-35d5c0067a1e Created 
18 passed in 0.70s
 Container dashboard-citymanager-dashboard-run-0fd75ecbf8d2 Creating 
 Container dashboard-citymanager-dashboard-run-0fd75ecbf8d2 Created 
PSEG DASHBOARD IMPORT + JINJA: PASS
3. ADDITIVE PSEG POLICY AND SOURCE STATE
BACKUP VERIFY: PASS
[2026-09-15T20:34:26Z] Applying additive PSEG settings and durable source-state migration
[2026-09-15T20:34:27Z] #60 PSEG DATABASE INSTALL: PASS
4. DASHBOARD + EXISTING INTEGRATION ENGINE
[2026-09-15T20:34:49Z] READINESS PASS: citymanager-dashboard
5. ONE CENTRAL PSEG ROUTER; LEGACY DIRECT PATHS OFF
[2026-09-15T20:34:51Z] PSEG WORKFLOW REVIEW: captured 10 redacted definitions
```
