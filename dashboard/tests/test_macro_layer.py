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
