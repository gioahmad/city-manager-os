from pathlib import Path
import asyncio
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

import pytest
from jinja2 import Environment, FileSystemLoader


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = DASHBOARD_ROOT / "templates"
sys.path.insert(0, str(DASHBOARD_ROOT))


def test_navigation_is_task_grouped_without_removing_destinations():
    nav = (TEMPLATES / "nav.html").read_text()
    styles = (DASHBOARD_ROOT / "static" / "command_nav.css").read_text()

    assert "Admin / More" not in nav
    assert "<summary>Operations</summary>" in nav
    assert "<summary>Control Center</summary>" in nav
    assert 'class="nav-group nav-operations' in nav
    assert 'class="nav-group nav-control' in nav
    assert nav.count('href="/integrations"') == 2

    for route in (
        "/my-day",
        "/issues",
        "/inbox",
        "/map",
        "/alerts",
        "/search",
        "/staff-admin",
        "/schedule",
        "/event-intelligence",
        "/today-board",
        "/today-board/setup",
        "/transit",
        "/watchlist",
        "/subscribers",
        "/spatial-reference",
        "/source-health",
        "/deliveries",
        "/api-lab",
    ):
        assert f'href="{route}"' in nav

    for retired_route in ("/modules", "/rules", "/routing", "/alert-admin"):
        assert f'href="{retired_route}"' not in nav

    assert ".desktop-nav .nav-control{margin-left:auto}" in styles
    assert ".mobile-menu{max-height:calc(100vh - 96px);overflow-y:auto}" in styles


def test_navigation_template_compiles():
    environment = Environment(loader=FileSystemLoader(TEMPLATES))
    environment.get_template("nav.html")


def test_operational_map_link_round_trips_existing_map_state():
    template = (TEMPLATES / "map.html").read_text()
    styles = (DASHBOARD_ROOT / "static" / "map.css").read_text()

    assert 'id="copy-map-link"' in template
    assert "MAP_SHARE_VERSION='1'" in template
    assert "function operationalMapUrl()" in template
    assert "function restoreMapControls()" in template
    assert "function restoreSharedSelection" in template
    assert "function featureIdentity" in template
    assert "navigator.clipboard?.writeText" in template

    for parameter in (
        "map_view",
        "lat",
        "lng",
        "zoom",
        "layers",
        "records",
        "tab",
        "window",
        "area_q",
        "source",
        "category",
        "priority",
        "alert_active",
        "selected_layer",
        "selected_id",
    ):
        assert f"'{parameter}'" in template

    assert "custom-'+el.dataset.id" in template
    assert "runMapSearch(initialMapQuery,sharedMapView)" in template
    assert "SAVED_MAP_VIEWS_KEY='cmos.map.savedViews.v1'" in template
    assert "localStorage.setItem(SAVED_MAP_VIEWS_KEY" in template
    assert ".map-share-status" in styles


def test_alert_category_filter_lives_with_layers_and_refreshes_the_existing_layer():
    template = (TEMPLATES / "map.html").read_text()
    source = (DASHBOARD_ROOT / "map_app.py").read_text()
    search_panel = template.split('data-panel-name="search"', 1)[1].split(
        'data-panel-name="layers"', 1
    )[0]
    layers_panel = template.split('data-panel-name="layers"', 1)[1].split(
        'data-panel-name="import"', 1
    )[0]

    assert 'id="alert-map-category"' not in search_panel
    assert 'id="alert-map-category"' in layers_panel
    assert 'id="apply-alert-filters"' in layers_panel
    assert 'id="reset-alert-filters"' in layers_panel
    assert 'id="alert-filter-status"' in layers_panel
    assert "Category uses the category supplied by each source, including BNN." in layers_panel
    assert "['alert-window','alert-map-source','alert-map-category','alert-map-priority','alert-map-active'].forEach" in template
    assert "addEventListener('change',()=>refreshAlertLayer(true))" in template
    assert "showAppliedAlertCount(count)" in template
    assert "No mapped alerts match " in template
    assert "if(category)params.set('category',category)" in template
    assert "upper(a.category)=upper(%s)" in source
    assert "WHEN a.geom IS NULL" in source


def test_alert_layer_endpoint_applies_every_display_filter(monkeypatch):
    monkeypatch.chdir(DASHBOARD_ROOT)
    os.environ.setdefault("DB_PASSWORD", "test")
    import map_app

    captured = {}

    def fake_query_all(sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(map_app, "query_all", fake_query_all)
    response = map_app.map_alerts_geojson(
        bbox="-75,40,-73,42",
        window="30d",
        min_priority=4,
        active_only=True,
        q="bridge",
        source="BNN",
        category="FIRE",
    )

    assert response.status_code == 200
    assert json.loads(response.body)["features"] == []
    assert "a.received_at >= now()-(%s * interval '1 hour')" in captured["sql"]
    assert "upper(a.source)=upper(%s)" in captured["sql"]
    assert "upper(a.category)=upper(%s)" in captured["sql"]
    assert "a.status <> 'RESOLVED'" in captured["sql"]
    assert "ST_Intersects(coalesce(a.geom,r.geom)" in captured["sql"]
    assert captured["params"][:4] == (720, 4, "BNN", "FIRE")
    assert captured["params"][4:11] == ("%bridge%",) * 7
    assert captured["params"][-4:] == (-75.0, 40.0, -73.0, 42.0)


def test_alert_location_correction_accepts_one_coordinate_pair_or_google_maps_link(monkeypatch):
    monkeypatch.chdir(DASHBOARD_ROOT)
    os.environ.setdefault("DB_PASSWORD", "test")
    from fastapi import HTTPException
    from map_app import _coordinate_pair

    assert _coordinate_pair("40.765123, -74.021456") == (40.765123, -74.021456)
    assert _coordinate_pair(
        "https://www.google.com/maps/place/Valley/@40.965353,-74.072017,17z"
    ) == (40.965353, -74.072017)
    assert _coordinate_pair("https://maps.google.com/?q=40.765123%2C-74.021456") == (
        40.765123,
        -74.021456,
    )
    with pytest.raises(HTTPException, match="latitude, longitude"):
        _coordinate_pair("40.765123")
    with pytest.raises(HTTPException, match="outside valid"):
        _coordinate_pair("140.765123, -274.021456")


def test_alert_location_correction_reuses_map_alert_and_resolution_systems():
    template = (TEMPLATES / "map.html").read_text()
    source = (DASHBOARD_ROOT / "map_app.py").read_text()
    styles = (DASHBOARD_ROOT / "static" / "map.css").read_text()

    assert '@app.post("/map/alerts/{alert_id}/location")' in source
    assert "FOR UPDATE OF a" in source
    assert "MANUAL_COORDINATE_CORRECTION" in source
    assert '"location_corrections": corrections' in source
    assert '"location_corrections": resolution_corrections' in source
    assert "ST_SetSRID(ST_MakePoint(%s,%s),4326)" in source
    assert "FROM gis_addresses ga CROSS JOIN point" in source
    assert "ST_DWithin(ga.geom::geography,point.geom::geography,500)" in source
    assert "match_type IN ('PROXIMITY','LOCATION_TOPIC')" in source
    assert '"spatial_rematch_version": "manual-location-pending-v1"' in source
    assert "CREATE TABLE" not in source

    assert "Correct Alert Location" in template
    assert "Coordinates or Google Maps link" in template
    assert "draggable:true" in template
    assert "Pick on Map" in template
    assert "Save Correction" in template
    assert "'/map/alerts/'+encodeURIComponent(alertId)+'/location'" in template
    assert "await refreshAlertLayer(true)" in template
    assert ".map-location-editor" in styles
    assert 'href="/static/map.css?v=' in template


def test_alert_location_correction_executes_one_atomic_existing_table_update(monkeypatch):
    monkeypatch.chdir(DASHBOARD_ROOT)
    os.environ.setdefault("DB_PASSWORD", "test")
    import map_app
    from starlette.requests import Request

    class Cursor:
        rowcount = 0

        def __init__(self):
            self.queries = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            assert query.count("%s") == len(params)
            self.queries.append((query, params))
            self.rowcount = 2 if "DELETE FROM alert_watch_matches" in query else 1

        def fetchone(self):
            return {
                "id": "00000000-0000-0000-0000-000000000001",
                "alert_id": "BNN:test",
                "title": "BNN incident",
                "location": {"label": "Old location"},
                "metadata": {},
                "municipality": "Weehawken",
                "county": "Hudson",
                "state": "NJ",
                "resolved_label": "Old location",
                "prior_match_type": "LOCAL_NEAREST_ADDRESS",
                "prior_resolution_provenance": {},
                "prior_latitude": 40.76,
                "prior_longitude": -74.03,
                "rematch_eligible": True,
            }

    class Connection:
        def __init__(self):
            self.cursor_value = Cursor()
            self.commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return self.cursor_value

        def commit(self):
            self.commits += 1

    connection = Connection()
    monkeypatch.setattr(map_app, "db_conn", lambda: connection)
    body = json.dumps(
        {
            "coordinates": "40.765123, -74.021456",
            "label": "Verified location",
            "reason": "Checked against incident map",
        }
    ).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/map/alerts/BNN:test/location",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
        },
        receive,
    )
    request.state.cmos_user = "operator"
    request.state.cmos_role = "SUPERVISOR"

    response = asyncio.run(map_app.map_alert_location_correct("BNN:test", request))
    result = json.loads(response.body)

    assert response.status_code == 200
    assert result["latitude"] == 40.765123
    assert result["longitude"] == -74.021456
    assert result["spatial_rematch"] == "QUEUED"
    assert result["cleared_spatial_matches"] == 2
    assert connection.commits == 1
    sql = "\n".join(query for query, _ in connection.cursor_value.queries)
    assert "UPDATE alerts" in sql
    assert "INSERT INTO geo_entity_resolutions" in sql
    assert "DELETE FROM alert_watch_matches" in sql


def test_map_sharing_adds_no_server_side_state_or_parallel_map():
    source = (DASHBOARD_ROOT / "map_app.py").read_text()
    template = (TEMPLATES / "map.html").read_text()

    assert "Copy Operational Map Link" in template
    assert "fetch('/map/share" not in template
    assert "/map/saved" not in source
    assert "CREATE TABLE" not in source
    assert "MapLibre" not in template
    assert "Cesium" not in template


def test_saved_views_measurements_and_current_view_brief_stay_browser_native():
    template = (TEMPLATES / "map.html").read_text()
    styles = (DASHBOARD_ROOT / "static" / "map.css").read_text()

    assert 'id="saved-map-view-name"' in template
    assert 'id="saved-map-views"' in template
    assert "views.unshift({name,url:operationalMapUrl()" in template
    assert "window.location.assign(url)" in template
    assert "fetch('/map/saved" not in template

    assert '<option value="">Measure only · do not save</option>' in template
    assert 'id="map-measure-result"' in template
    assert "function measurementText(layer,type)" in template
    assert "L.GeometryUtil.geodesicArea(points)" in template
    draw_handler = template.split("const drawLayerSelect", 1)[1]
    measure_only = "if(!layerId){showMeasurement(ev.layer,ev.layerType);return;}"
    assert measure_only in draw_handler
    assert draw_handler.index(measure_only) < draw_handler.index("fetch('/map/layer/'")

    assert 'id="build-current-view-brief"' in template
    assert 'id="current-view-brief"' in template
    assert "function layerFeaturesInView(group)" in template
    assert "function buildCurrentViewBrief()" in template
    assert "Loaded data only · built " in template
    assert ".map-saved-actions" in styles
    assert ".map-measure-label" in styles


def test_selected_features_show_cross_layer_context_from_loaded_map_data():
    template = (TEMPLATES / "map.html").read_text()
    styles = (DASHBOARD_ROOT / "static" / "map.css").read_text()

    assert "CONTEXT_RADIUS_METERS=1609.344" in template
    for key in (
        "alerts",
        "watchlist",
        "operations",
        "event-intelligence",
        "managed-events",
        "transit-intelligence",
        "spatial-references",
    ):
        assert f"'{key}'" in template.split("const CONTEXT_LAYER_KEYS=", 1)[1].split("];", 1)[0]
    assert "function featureContainsPoint(feature,point)" in template
    assert "featureContainsPoint(feature,candidateCenter)||featureContainsPoint(candidateFeature,center)" in template
    assert "function nearbyLoadedRecords(feature,layer,key)" in template
    assert "candidateLayer===layer" in template
    assert "!map.hasLayer(group)" in template
    assert "map.distance(center,candidateCenter)" in template
    assert ".sort((a,b)=>a.distance-b.distance).slice(0,8)" in template
    assert "Inside the selected area or within 1 mile. Uses currently enabled layers only." in template
    assert "selectedFeatureContext(feature,layer,key,layerName)" in template
    assert "fetch('/map/context" not in template
    assert ".map-context-facts" in styles
    assert ".map-context-record" in styles
    assert 'href="/static/map.css?v=' in template


def test_map_template_compiles_after_browser_native_tools():
    environment = Environment(loader=FileSystemLoader(TEMPLATES))
    environment.get_template("map.html")


def test_alert_history_opens_or_places_one_alert_in_the_existing_map(monkeypatch):
    monkeypatch.chdir(DASHBOARD_ROOT)
    os.environ.setdefault("DB_PASSWORD", "test")
    import operations_app

    mapped = parse_qs(
        urlparse(
            operations_app.alert_map_url(
                {
                    "alert_id": "BNN:123",
                    "map_latitude": 40.765123,
                    "map_longitude": -74.021456,
                }
            )
        ).query
    )
    assert mapped["focus"] == ["alert"]
    assert mapped["area_q"] == ["BNN:123"]
    assert mapped["selected_layer"] == ["alerts"]
    assert mapped["selected_id"] == ["BNN:123"]
    assert mapped["lat"] == ["40.765123"]
    assert mapped["lng"] == ["-74.021456"]

    unmapped = parse_qs(
        urlparse(operations_app.alert_map_url({"alert_id": "BNN:missing"})).query
    )
    assert unmapped["edit_alert"] == ["BNN:missing"]
    assert "selected_id" not in unmapped

    source = (DASHBOARD_ROOT / "operations_app.py").read_text()
    alerts = (TEMPLATES / "alerts.html").read_text()
    mapping = (TEMPLATES / "map.html").read_text()
    assert "ST_Y(coalesce(a.geom,r.geom)) AS map_latitude" in source
    assert "View on Map" in alerts
    assert "Place on Map" in alerts
    assert "openRequestedAlertEditor" in mapping
    assert "if(current)setMarker(current)" in mapping
    assert "map.setView(latlng,Math.max(map.getZoom(),16));await refreshAlertLayer(true)" in mapping
