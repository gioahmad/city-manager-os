import ast
from pathlib import Path
from unittest import SkipTest


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = DASHBOARD_ROOT.parent


def _watch_item_write_calls():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "execute":
            continue
        sql_node, params_node = node.args[:2]
        if not isinstance(sql_node, ast.Constant) or not isinstance(sql_node.value, str):
            continue
        sql = sql_node.value
        if "watch_items" not in sql or not (
            "INSERT INTO watch_items" in sql
            or ("UPDATE watch_items SET" in sql and "spatial_target_geom" in sql)
        ):
            continue
        assert isinstance(params_node, ast.Tuple)
        yield sql, params_node


def test_watch_writes_have_typed_geometry_and_matching_parameters():
    calls = list(_watch_item_write_calls())
    assert len(calls) == 2
    for sql, params in calls:
        assert sql.count("%s") == len(params.elts)
        assert "%s IS NULL" not in sql
        assert "ST_GeomFromEWKT(%s::text)" in sql
        assert "spatial_target_geom" in sql


def test_watchlist_has_simple_modes_health_and_friendly_errors():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/watchlist.html").read_text()
    assert 'SETUP_MODES = {"LOCATION", "TOPIC", "LOCATION_TOPIC"}' in source
    assert 'setup_mode: str = Form("LOCATION")' in source
    assert '"NEARBY": "LOCATION"' in source
    assert '"KEYWORD": "TOPIC"' in source
    assert "radius_ft: float = Form(5280.0)" in source
    assert '@app.get("/api/watchlist/health")' in source
    assert '@app.get("/api/watch-locations/search")' in source
    assert '@app.post("/api/watch-locations/resolve")' in source
    assert '@app.post("/api/watchlist/test-notification")' in source
    assert '@app.post("/watchlist/{item_id}/delete")' in source
    assert "@_friendly_watch_errors" in source
    assert "full street address with municipality and state" in source
    for value in ("LOCATION", "TOPIC", "LOCATION_TOPIC"):
        assert f'value="{value}"' in template
    for label in (
        "What should we watch for?",
        "Where should we watch?",
        "Who should be notified?",
        "Test it",
        "Turn it on",
        "Watching",
        "Paused",
        "Expired",
        "Needs Recipient",
        "Matching",
        "Delivery Problem",
    ):
        assert label in template
    assert "1 mile · recommended" in template
    assert "Send Test Notification" in template
    assert "does not create an alert, Match, or Watch" in template
    assert "Verify full address" in template
    assert "Address, parcel, street, municipality, or saved map area" in template


def test_normal_watch_setup_hides_technical_nomenclature_until_advanced():
    template = (DASHBOARD_ROOT / "templates/watchlist.html").read_text()
    normal_setup, advanced = template.split('<details class="advanced-options">', 1)
    visible_normal = normal_setup.lower()
    for forbidden in (
        "spatial geometry",
        "gis enabled",
        "nearby enabled",
        "subscriber id",
        "match mode",
    ):
        assert forbidden not in visible_normal
    assert "match mode" in advanced.lower()


def test_watch_name_cannot_be_saved_as_an_unknown_alert_filter():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/watchlist.html").read_text()
    assert "def _validate_alert_filters" in source
    assert source.count("_validate_alert_filters(cur, saved_source_filter, saved_category_filter)") == 2
    assert "Unknown alert {label}" in source
    assert "The Watch name never" in template
    assert template.count('autocomplete="off"') >= 4


def test_alert_spatial_endpoints_use_the_mapping_center_point():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    map_source = (DASHBOARD_ROOT / "map_app.py").read_text()
    assert 'GEOMETRY_RELEASE_ID = "spatial-watch-effective-geometry-v1"' in source
    assert '"geometry_release_id": GEOMETRY_RELEASE_ID' in source
    impact = source.split("def alert_spatial_impact", 1)[1].split(
        '@app.get("/api/alerts/{alert_id}/impact-buffer.geojson")', 1
    )[0]
    buffer = source.split("def alert_impact_buffer", 1)[1]
    for endpoint in (impact, buffer):
        assert "geo_entity_resolutions" in endpoint
        assert "coalesce(a.geom,r.geom)" in endpoint
    assert map_source.count("r.entity_id=a.id::text AND r.status='RESOLVED'") >= 1


def test_alert_map_restores_full_history_choices_without_destructive_actions():
    map_source = (DASHBOARD_ROOT / "map_app.py").read_text()
    map_template = (DASHBOARD_ROOT / "templates/map.html").read_text()
    map_styles = (DASHBOARD_ROOT / "static/map.css").read_text()
    alerts_source = (DASHBOARD_ROOT / "operations_app.py").read_text()
    alerts_template = (DASHBOARD_ROOT / "templates/alerts.html").read_text()
    assert '"endpoint": "/map/system/alerts.geojson?hours=12"' in map_source
    assert "min(int(hours or 12), 168)" in map_source
    assert "from operations_app import ALERT_WINDOWS" in map_source
    assert '"30d": 720' in alerts_source
    assert '"all": None' in alerts_source
    assert "if window_hours is not None:" in map_source
    assert "a.received_at >= now()-(%s * interval '1 hour')" in map_source
    for value in ('value="6h"', 'value="12h"', 'value="24h"', 'value="7d"', 'value="30d"', 'value="all"'):
        assert value in map_template
    visible_controls = map_template.split('<details open>', 1)[0]
    assert 'id="alert-window"' in visible_controls
    assert "<details open>" in map_template
    assert "All mapped history" in map_template
    assert "addEventListener('change',()=>refreshAlertLayer(true))" in map_template
    assert "Search Visible Area" in map_template
    assert "Search all alerts" in map_template
    assert "Search all operational records" in map_template
    assert "only controls what is displayed. Alert history is kept" in map_template
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in map_styles
    assert "@media(max-width:1050px){.map-alert-filter-grid{grid-template-columns:1fr}}" in map_styles
    assert ".map-alert-controls>*{min-width:0}" in map_styles
    assert '"all": None' in alerts_source
    assert "a.location::text ILIKE" in alerts_source
    assert "array_to_string(a.tags,' ') ILIKE" in alerts_source
    assert "All stored history" in alerts_template
    assert "Search the complete alert database" in alerts_template
    assert "DELETE FROM alerts" not in map_source
    assert '@app.post("/alerts/bulk-action")' in alerts_source
    assert "Type DELETE to permanently delete the selected alerts" in alerts_source


def test_map_area_record_checkboxes_keep_their_text_inside_the_panel():
    map_template = (DASHBOARD_ROOT / "templates/map.html").read_text()
    map_styles = (DASHBOARD_ROOT / "static/map.css").read_text()
    assert '.map-alert-controls input:not([type="checkbox"]),.map-alert-controls select' in map_styles
    assert ".map-check input{width:auto}" in map_styles
    assert 'href="/static/map.css?v=' in map_template


def test_leaflet_draw_buttons_keep_their_plugin_icons():
    map_template = (DASHBOARD_ROOT / "templates/map.html").read_text()
    map_styles = (DASHBOARD_ROOT / "static/map.css").read_text()
    assert ".leaflet-control-layers,.leaflet-bar a{background-color:#fff" in map_styles
    assert ".leaflet-control-layers,.leaflet-bar a{background:#fff" not in map_styles
    assert 'href="/static/map.css?v=' in map_template


def test_mapping_center_deduplicates_cancels_and_reports_layer_refreshes():
    map_source = (DASHBOARD_ROOT / "map_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/map.html").read_text()

    assert "const layerLoads={};" in template
    assert "const loadedSignatures={};" in template
    assert "const layerMetrics={};" in template
    assert "loadedSignatures[key]===signature" in template
    assert "layerLoads[key]?.signature===signature" in template
    assert template.index("layerLoads[key]?.signature===signature") < template.index("loadedSignatures[key]===signature")
    assert "new AbortController()" in template
    assert "signal:controller.signal" in template
    assert "previous data kept" in template
    assert "refreshed '+refreshClock()" in template
    assert "syncSystemLayer(toggle,true)" in template
    assert "Promise.allSettled(jobs)" in template
    assert "},600);" in template
    assert "setInterval(loadGisStatus,30000)" not in template
    assert "gisStatusTimer=setTimeout(loadGisStatus,active?30000:300000)" in template
    assert 'data-layer-runtime="{{ layer.key }}"' in template
    assert "left(a.message,600) AS message" in map_source


def test_alert_can_start_an_editable_watch_draft_without_writing_data():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    operations_source = (DASHBOARD_ROOT / "operations_app.py").read_text()
    watch_template = (DASHBOARD_ROOT / "templates/watchlist.html").read_text()
    alert_template = (DASHBOARD_ROOT / "templates/alerts.html").read_text()
    map_template = (DASHBOARD_ROOT / "templates/map.html").read_text()

    helper = source.split("def _watch_prefill_from_alert", 1)[1].split("def _json_safe", 1)[0]
    assert "FROM alerts a" in helper
    assert "geo_entity_resolutions" in helper
    assert "ST_PointOnSurface" in helper
    assert "INSERT" not in helper
    assert "UPDATE" not in helper
    assert 'from_alert: str = ""' in source
    assert 'action="/watchlist/create"' in watch_template
    assert "Create a Watch from this alert" not in watch_template
    assert "STARTED FROM ALERT" in watch_template
    assert "What about this alert matters?" in watch_template
    assert "topic, the Location, or both" in watch_template
    assert "Nothing is saved until you choose Turn On Watch." in watch_template
    assert 'data-alert-filter="source_filter"' in watch_template
    assert 'data-alert-filter="alert_category_filter"' in watch_template
    assert "watch_from_alert_url" in operations_source
    assert "Create Watch From Alert" in alert_template
    assert "Create Watch From Alert" in map_template


def test_private_watch_runner_splits_full_address_for_local_resolver():
    runner_path = REPOSITORY_ROOT / "deploy/ops/configure-isolated-radius-watch.sh"
    if not runner_path.is_file():
        raise SkipTest("deployment source is outside the dashboard-only test mount")
    runner = runner_path.read_text()
    assert "address_parts =" in runner
    assert "'address': address_line" in runner
    assert "'municipality': municipality_hint" in runner
    assert "resolved.get('postal_code')" in runner
