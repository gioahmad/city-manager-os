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


def test_alert_map_shelf_life_and_global_search_are_bounded_and_non_destructive():
    map_source = (DASHBOARD_ROOT / "map_app.py").read_text()
    map_template = (DASHBOARD_ROOT / "templates/map.html").read_text()
    alerts_source = (DASHBOARD_ROOT / "operations_app.py").read_text()
    alerts_template = (DASHBOARD_ROOT / "templates/alerts.html").read_text()
    assert '"endpoint": "/map/system/alerts.geojson?hours=12"' in map_source
    assert "min(int(hours or 12), 168)" in map_source
    assert "a.received_at >= now()-(%s * interval '1 hour')" in map_source
    for value in ('value="6"', 'value="12"', 'value="24"', 'value="168"'):
        assert value in map_template
    assert "Search Visible Area" in map_template
    assert "only controls what is displayed. Alert history is kept" in map_template
    assert '"all": None' in alerts_source
    assert "a.location::text ILIKE" in alerts_source
    assert "array_to_string(a.tags,' ') ILIKE" in alerts_source
    assert "All stored history" in alerts_template
    assert "Search the complete alert database" in alerts_template
    assert "DELETE FROM alerts" not in map_source
    assert '@app.post("/alerts/bulk-action")' in alerts_source
    assert "Type DELETE to permanently delete the selected alerts" in alerts_source


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
