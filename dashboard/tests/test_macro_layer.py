from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_shared_nav_loads_preserve_first_macro_layer():
    nav = (ROOT / "templates" / "nav.html").read_text()
    assert "/static/macro_layer.css" in nav
    assert "/static/macro_layer.js" in nav
    # Existing destinations remain present; the layer augments instead of replacing them.
    for route in (
        "/workspace", "/my-day", "/issues", "/inbox", "/brain", "/map",
        "/alerts", "/search", "/share", "/staff-admin", "/schedule",
        "/event-intelligence", "/today-board", "/transit", "/watchlist",
        "/subscribers", "/contacts", "/integrations", "/database",
        "/spatial-reference", "/source-health", "/deliveries", "/api-lab",
    ):
        assert route in nav


def test_macro_layer_has_command_palette_and_quicklook():
    js = (ROOT / "static" / "macro_layer.js").read_text()
    css = (ROOT / "static" / "macro_layer.css").read_text()
    assert "cmos-command-dialog" in js
    assert "cmos-sidecar" in js
    assert "openSidecar" in js
    assert "data-cmos-context" in js
    assert "embed" in js
    assert ".cmos-command-dialog" in css
    assert ".cmos-sidecar" in css
    assert "body.cmos-embedded" in css


def test_dashboard_is_drillable_without_losing_original_sections():
    page = (ROOT / "templates" / "index.html").read_text()
    for section in (
        "Operational Schedule", "Active Alerts", "Command Center",
        "Intelligence Feed", "Source Health", "Recent Deliveries",
    ):
        assert section in page
    assert page.count("data-cmos-context") >= 8
    assert "/watchlist" in page
    assert "/issues?state=open" in page
    assert "/source-health" in page
    assert "/deliveries" in page


def test_map_keeps_existing_power_and_opens_actions_in_context():
    page = (ROOT / "templates" / "map.html").read_text()
    for label in ("Search", "Layers", "Import", "Draw", "Tools"):
        assert f">{label}<" in page
    for phrase in (
        "Address / Parcel Search",
        "Alert Layer Filters",
        "Import Data",
        "Draw on Map",
        "Saved Map Views",
        "Build Current View Brief",
        "Create One-Mile Watch",
        "Create Watch From Alert",
    ):
        assert phrase in page
    assert "dataset.cmosQuicklook" in page


def test_dashboard_spatial_operating_picture():
    operations=(ROOT/"operations_app.py").read_text()
    page=(ROOT/"templates"/"index.html").read_text()
    assert "spatial_status = query_one(" in operations
    for field in (
        "mapped_alerts_24h","total_alerts_24h","active_spatial_watches",
        "active_references","active_map_layers","parcels_ready",
        "addresses_ready","flood_ready",
    ):
        assert field in operations
        assert field in page
    assert "Mapping &amp; Spatial Intelligence" in page
    assert 'data-cmos-quicklook data-cmos-title="Mapping Center"' in page


def test_persistent_rail_replaces_mixed_nav():
    nav=(ROOT/"templates"/"nav.html").read_text()
    css=(ROOT/"static"/"macro_layer.css").read_text()
    js=(ROOT/"static"/"macro_layer.js").read_text()
    assert 'class="cmos-rail"' in nav
    assert 'class="cmos-global-bar"' in nav
    assert 'data-cmos-command-trigger' in nav
    for label in ("Overview","My Day","Command","Inbox","Brain","Map","Alerts","Watches","Staff","Events","Event Intelligence","Transit","Places / References"):
        assert label in nav
    assert ".cmos-rail{" in css
    assert ".cmos-global-bar{" in css
    assert "cmos-mobile-nav-open" in js


def test_product_cohesion_pass_v2():
    css=(ROOT/"static"/"macro_layer.css").read_text()
    for token in (
        "Product cohesion pass v2",
        "--cmos-rail-w:190px",
        ".cmos-global-bar",
        ".cmos-sidecar{width:min(640px,46vw)",
        "body.cmos-embedded>main",
        ".cmos-spatial-panel",
    ):
        assert token in css


def test_primary_pages_are_task_first():
    js=(ROOT/"static"/"macro_layer.js").read_text()
    css=(ROOT/"static"/"macro_layer.css").read_text()
    for path in ("'/issues'","'/watchlist'","'/alerts'","'/spatial-reference'"):
        assert path in js
    for phrase in (
        "Review work first",
        "Create or manage a Watch",
        "Find and review alerts",
        "Find a place, then act on it",
        "Advanced Tools",
    ):
        assert phrase in js
    for token in (
        ".cmos-flow-header",
        ".cmos-advanced-tools",
        ".cmos-inline-advanced",
        ".cmos-secondary-workflow",
    ):
        assert token in css


def test_executive_action_center_and_quick_actions():
    operations=(ROOT/"operations_app.py").read_text()
    page=(ROOT/"templates"/"index.html").read_text()
    js=(ROOT/"static"/"macro_layer.js").read_text()
    css=(ROOT/"static"/"macro_layer.css").read_text()
    assert "action_center = query_all(" in operations
    assert "recent_activity = query_all(" in operations
    assert "What needs me now" in page
    assert "Activity" in page
    for phrase in ("New Work","New Watch","Quick Capture","Open Map","Search Records"):
        assert phrase in js
    assert "location.hash==='#new-work'" in js
    assert "location.hash==='#new-watch'" in js
    assert ".cmos-action-grid" in css


def test_dashboard_sql_avoids_percent_placeholder_collisions():
    operations=(ROOT/"operations_app.py").read_text()
    assert "%20" not in operations
    assert "chr(37)||'20'" in operations
