from pathlib import Path

from jinja2 import Environment, FileSystemLoader


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = DASHBOARD_ROOT / "templates"


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
        "/rules",
        "/subscribers",
        "/routing",
        "/spatial-reference",
        "/source-health",
        "/deliveries",
        "/api-lab",
    ):
        assert f'href="{route}"' in nav

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
    assert "localStorage" not in template
    assert ".map-share-status" in styles


def test_map_sharing_adds_no_server_side_state_or_parallel_map():
    source = (DASHBOARD_ROOT / "map_app.py").read_text()
    template = (TEMPLATES / "map.html").read_text()

    assert "Copy Operational Map Link" in template
    assert "fetch('/map/share" not in template
    assert "/map/saved" not in source
    assert "CREATE TABLE" not in source
    assert "MapLibre" not in template
    assert "Cesium" not in template
