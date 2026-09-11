#!/usr/bin/env python3
"""Static safety contracts for the statewide GIS refresh lifecycle."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
refresh = (ROOT / "deploy/gis/refresh_statewide_gis.sh").read_text()
installer = (ROOT / "deploy/gis/install_statewide_gis_refresh_timer.sh").read_text()
migration = (ROOT / "deploy/postgis/init/029_statewide_gis_refresh_control.sql").read_text()
map_app = (ROOT / "dashboard/map_app.py").read_text()
template = (ROOT / "dashboard/templates/map.html").read_text()

assert refresh.index("deploy/postgis/backup.sh") < refresh.index("stage_sources()")
assert refresh.index("stage_sources \"$ADDRESS_ARCHIVE\"") < refresh.index("deploy/gis/import_statewide_gis.sh promote")
assert "--bootstrap-existing" in refresh
assert "${ADDRESS_ARCHIVE}.previous" in refresh
assert "activate_retained_source" in refresh
assert "record UNCHANGED" in refresh
assert "record FAILED" in refresh
assert "find /opt/citymanager-data/gis/incoming -maxdepth 1" in refresh

assert "OnCalendar=Sun *-*-01..07 03:15:00 America/New_York" in installer
assert "systemctl enable --now cmos-nj-statewide-gis-refresh.timer" in installer
assert "systemctl start cmos-nj-statewide-gis-refresh.service" not in installer
assert "systemctl disable --now cmos-hudson-gis-refresh.timer" in installer

assert "CREATE TABLE IF NOT EXISTS gis_refresh_runs" in migration
assert "'RUNNING','SUCCESS','FAILED','UNCHANGED'" in migration
assert '@app.get("/map/gis/status")' in map_app
assert "FROM pg_stat_progress_copy" in map_app
assert "setInterval(loadGisStatus,30000)" in template

print("CMOS STATEWIDE GIS LIFECYCLE CONTRACTS: PASS")
