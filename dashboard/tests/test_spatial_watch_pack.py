import json
from pathlib import Path
from unittest import SkipTest


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = DASHBOARD_ROOT.parent


def _repository_file(relative_path: str) -> Path:
    path = REPOSITORY_ROOT / relative_path
    if not path.is_file():
        raise SkipTest("deployment source is outside the dashboard-only test mount")
    return path


def _workflow_nodes(relative_path: str) -> dict:
    payload = json.loads(_repository_file(relative_path).read_text())
    workflow = payload[0] if isinstance(payload, list) else payload
    return {node["name"]: node for node in workflow["nodes"]}


def test_spatial_watch_migration_reuses_canonical_systems():
    sql = _repository_file("deploy/postgis/init/032_unified_spatial_watch_pack.sql").read_text()
    assert "ALTER TABLE watch_items" in sql
    assert "spatial_target_geom geometry(Geometry,4326)" in sql
    assert "trg_gis_prepare_spatial_watch" in sql
    assert "gis_active_spatial_watch_matches" in sql
    assert "p_supplied_alert_geom geometry" in sql
    assert "ST_GeometryType(p_supplied_alert_geom)='ST_Point'" in sql
    assert "LEFT JOIN geo_entity_resolutions r" in sql
    assert "r.status='RESOLVED'" in sql
    assert "resolver point" in sql
    assert "coalesce(a.geom,r.geom)" in sql
    assert "gis_spatial_history" in sql
    assert "ST_DWithin(" in sql
    assert "ST_Intersects(alert_geom,spatial_geom)" in sql
    assert "a.geom IS NOT NULL" in sql
    assert "w.starts_at IS NULL OR w.starts_at<=now()" in sql
    assert "w.expires_at IS NULL OR w.expires_at>now()" in sql
    assert "w.source_filter" in sql
    assert "w.alert_category_filter" in sql
    assert "a.priority>=w.min_priority" in sql
    assert "upper(coalesce(w.watch_type,''))<>'LOCATION_TOPIC'" in sql
    assert "gis_spatial_impact_context" in sql
    assert "CREATE TABLE" not in sql
    assert "INSERT INTO deliveries" not in sql
    assert "INSERT INTO subscribers" not in sql


def test_guarded_database_installer_covers_point_corridor_and_no_writes():
    installer = _repository_file("deploy/gis/install_spatial_watch_pack.sh").read_text()
    assert "pg_dump" in installer
    assert installer.index("pg_dump") < installer.index("032_unified_spatial_watch_pack.sql")
    assert "CMOS56:IN" in installer
    assert "CMOS56:OUT" in installer
    assert "CMOS56:UNRESOLVED" in installer
    assert "CMOS56:RESOLVER_IN" in installer
    assert "APPROXIMATE_COUNTY" in installer
    assert "CMOS56:CORRIDOR_IN" in installer
    assert "CMOS56:CORRIDOR_OUT" in installer
    assert installer.count("watch_item_id BETWEEN") == 5
    assert installer.count("watch_item_id='56000000-0000-0000-0000-000000000005'::uuid") == 2
    assert "ST_LineString" in installer
    assert "BEGIN;" in installer and "ROLLBACK;" in installer
    assert "INSERT INTO deliveries" not in installer
    assert "ntfy" not in installer.lower()
    matcher_installer = _repository_file("deploy/n8n/install_spatial_watch_matcher.sh").read_text()
    assert "chown node:node" in matcher_installer
    assert 'prepare_node_file "$TMP_CONTRACT"' in matcher_installer
    assert 'prepare_node_file "$TMP_TARGET"' in matcher_installer


def test_effective_geometry_release_is_bounded_and_recoverable():
    release = _repository_file("deploy/releases/spatial-watch-effective-geometry.sh").read_text()
    assert "rollback_dashboard" in release
    assert "rollback_n8n" in release
    assert "rollback_database" in release
    assert release.index('CURRENT_PHASE="backups"') < release.index(
        'CURRENT_PHASE="database-install"'
    )
    assert "gis_active_spatial_watch_matches(a.alert_id)" in release
    assert "INSERT INTO alert_watch_matches" in release
    assert "interval '24 hours'" in release
    assert "RETROSPECTIVE_NOTIFICATION_SENT=NO" in release
    assert "INSERT INTO deliveries" not in release
    assert "cmos-e2e" not in release
    assert "tests/test_watchlist_reliability.py </dev/null" in release
    assert "match_reason LIKE '%%resolver point%%'" in release
    assert '[[ -z "$ACCEPTANCE_RESULT"' in release


def test_browser_uses_existing_watchlist_resolver_subscribers_and_routing():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    assert '@app.get("/watchlist"' in source
    assert '@app.post("/watchlist/create")' in source
    assert '@app.post("/watchlist/{item_id}/update")' in source
    assert '@app.post("/watchlist/{item_id}/toggle")' in source
    assert '@app.post("/watchlist/{item_id}/delete")' in source
    assert '@app.post("/api/watchlist/test-notification")' in source
    assert '@app.get("/api/watch-locations/search")' in source
    assert '@app.get("/api/spatial-watch-point/nearby-history")' in source
    assert '@app.get("/api/alerts/{alert_id}/spatial-impact")' in source
    assert '@app.get("/api/alerts/{alert_id}/impact-buffer.geojson")' in source
    assert "MIN_PRECISE_CONFIDENCE" in source and "resolve_payload" in source
    assert 'SETUP_MODES = {"LOCATION", "TOPIC", "LOCATION_TOPIC"}' in source
    assert "spatial_requested = bool(target and target.get(\"spatial\"))" in source
    assert "radius_ft: float = Form(5280.0)" in source
    assert 'saved_watch_type = "LOCATION_TOPIC"' in source
    assert "INSERT INTO watch_items" in source
    assert "INSERT INTO watch_item_recipients" in source
    assert "FROM subscribers WHERE active=true" in source
    for preset in ("1_HOUR", "4_HOURS", "8_HOURS", "12_HOURS", "24_HOURS", "48_HOURS", "3_DAYS", "7_DAYS"):
        assert preset in source
    assert "INSERT INTO deliveries" not in source
    assert "INSERT INTO alerts" not in source
    assert "requests." not in source
    test_endpoint = source.split('def watchlist_test_notification', 1)[1].split('@app.get("/api/spatial-watch', 1)[0]
    assert "urllib_request.urlopen" in test_endpoint
    assert "INSERT INTO" not in test_endpoint
    assert "UPDATE " not in test_endpoint


def test_mapping_center_previews_and_reuses_canonical_references():
    map_app = (DASHBOARD_ROOT / "map_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/map.html").read_text()
    watchlist = (DASHBOARD_ROOT / "templates/watchlist.html").read_text()
    assert "intended_recipients" in map_app
    assert "watch_state" in map_app
    assert "coalesce(w.spatial_geom,w.geom)" in map_app
    assert "Pick Watch Point" in template
    assert "Watch This Corridor" in template
    assert "showImpactArea" in template
    assert "watch-preview" in template
    assert "watchCenterMarker" in template
    assert "Watch center" in template
    assert "pane:'overlayPane'" in template
    assert "interactive:false,bubblingMouseEvents:false" in template
    assert "L.featureGroup([previewArea,previewCenter])" in template
    assert "if(key==='watchlist')addWatchCenter(feature,layer)" in template
    assert "if(key==='watchlist')removeLayer('watch-centers')" in template
    assert "/api/spatial-watch-point/nearby-history" in template
    assert "Create One-Mile Watch" in template
    assert "Search Visible Area" in template
    assert "View nearby alert history data" in watchlist
    assert "Only these alert sources" in watchlist
    assert "Only these alert categories" in watchlist
    assert "Recipients" in watchlist


def test_central_matcher_adds_spatial_match_without_parallel_delivery():
    shared_matcher = (DASHBOARD_ROOT / "static/watch_matcher.js").read_text().rstrip()
    for path in (
        "workflows/core/CORE_Watchlist_Matcher_v1.json",
        "workflows/live/CORE_Watchlist_Matcher_live.json",
    ):
        nodes = _workflow_nodes(path)
        loader = nodes["Load Active Watchlist + Recipients"]["parameters"]
        matcher = nodes["Match + Resolve Recipients"]["parameters"]["jsCode"]
        assert "$1::jsonb" in loader["query"]
        assert "gis_active_spatial_watch_matches" in loader["query"]
        assert "supplied_alert_geom" in loader["query"]
        assert "jsonb_typeof(alert#>'{location,longitude}')='number'" in loader["query"]
        assert "queryReplacement" in loader["options"]
        assert matcher.startswith(shared_matcher + "\n\n")
        assert "watch.spatial_match_type" in matcher
        assert "result.match_type || row.match_mode" in matcher
        assert "locationPlusTopic" in matcher
        assert "municipalityMatch" in matcher
        assert "const locationMatched = spatialMatch" in matcher
        assert "locationPlusTopic ? 'LOCATION_TOPIC'" in matcher
        assert "recipientMap" in matcher
        assert "matched_watch_ids.includes(row.watch_id)" in matcher


def test_matcher_installer_verifies_location_plus_topic_as_and():
    installer = _repository_file("deploy/n8n/install_spatial_watch_matcher.sh").read_text()
    assert "location_plus_topic=AND" in installer
    assert "municipality_plus_topic=AND" in installer
    assert "Location plus topic matched outside the Location" in installer
    assert "Location plus topic matched without the topic" in installer


def test_application_composition_includes_spatial_watch_module():
    phase3 = (DASHBOARD_ROOT / "phase3_app.py").read_text()
    dockerfile = (DASHBOARD_ROOT / "Dockerfile").read_text()
    assert "import spatial_reference_app" in phase3
    assert "import spatial_watch_app" in phase3
    assert "COPY spatial_watch_app.py ." in dockerfile
