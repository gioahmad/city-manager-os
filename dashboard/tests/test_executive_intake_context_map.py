import json
import os
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]


def test_executive_intake_is_first_class_and_selective():
    hub = (ROOT / "workspace_hub.py").read_text()
    template = (ROOT / "templates" / "workspace_hub.html").read_text()
    js = (ROOT / "static" / "workspace_hub.js").read_text()
    nav = (ROOT / "templates" / "nav.html").read_text()

    assert "'CALENDAR'" in hub and "'EVENT'" in hub
    assert "workspace_calendar_events" in hub
    assert "Bring into Work" in js
    assert "Add to Brain" in js
    assert "Bring into Events" in js
    assert "Create Event" in js
    assert "Mark handled" in js
    assert "Snooze" in js
    assert "Open connected context" in js
    assert "Microsoft calendar" in template
    assert "Microsoft email" in template
    assert "EXECUTIVE INTAKE" in template
    assert "/workspace#inbox" in nav and "Intake" in nav


def test_calendar_refresh_preserves_linkable_ids():
    migration = (ROOT.parent / "deploy" / "postgis" / "init" / "040_executive_intake.sql").read_text()
    calendar = (ROOT / "workspace_calendar.py").read_text()
    installer = (ROOT.parent / "deploy" / "workspace" / "install_workspace.sh").read_text()

    assert "ADD COLUMN IF NOT EXISTS id uuid" in migration
    assert "workspace_calendar_events_id_unique" in migration
    assert "workspace_inbox_snoozed" in migration
    assert "ON CONFLICT(owner_username,event_key) DO UPDATE" in calendar
    assert "workspace_context_links" in calendar
    assert "040_executive_intake.sql" in installer


def test_universal_context_inspector_is_wired():
    phase = (ROOT / "phase3_app.py").read_text()
    app = (ROOT / "context_app.py").read_text()
    template = (ROOT / "templates" / "context.html").read_text()
    mapping = (ROOT / "templates" / "map.html").read_text()

    assert "import context_app" in phase
    assert '@app.get("/context/{item_kind}/{item_id}"' in app
    assert '@app.get("/records/{record_id}")' in app
    assert "Linked context" in template
    assert "/context/ALERT/" in mapping
    assert "/context/WATCH/" in mapping
    assert "/context/WORK/" in mapping
    assert "/context/EVENT/" in mapping
    assert "/context/REFERENCE/" in mapping


def test_mapping_center_activity_defaults_and_controls_are_preserve_first():
    source = (ROOT / "map_app.py").read_text()
    template = (ROOT / "templates" / "map.html").read_text()
    styles = (ROOT / "static" / "map.css").read_text()

    watch = next(line for line in source.splitlines() if '"key": "watchlist"' in line)
    alerts = next(line for line in source.splitlines() if '"key": "alerts"' in line)
    refs = next(line for line in source.splitlines() if '"key": "spatial-references"' in line)
    work = next(line for line in source.splitlines() if '"key": "operations"' in line)
    assert '"default_visible": True' in watch
    assert '"default_visible": True' in alerts
    assert '"default_visible": False' in refs
    assert '"default_visible": False' in work
    assert "local_bounds" in source and "WEEHAWKEN" in source

    for value in ("1h", "2h", "4h", "6h", "12h", "24h", "3d", "7d", "30d", "custom", "all"):
        assert f'value="{value}"' in template
    assert 'id="alert-custom-value"' in template
    assert 'data-alert-source' in template
    assert 'id="alert-sources-all"' in template
    assert 'id="alert-sources-none"' in template
    assert 'data-map-visual="heat"' in template
    assert 'data-map-visual="both"' in template
    assert 'id="alert-playback"' in template
    assert "leaflet.heat" in template
    assert ".map-source-grid" in styles
    assert ".map-segmented" in styles


def test_map_alert_endpoint_supports_multiple_feeds_and_custom_hours(monkeypatch):
    monkeypatch.chdir(ROOT)
    os.environ.setdefault("DB_PASSWORD", "test")
    import map_app

    captured = {}

    def fake_query_all(sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(map_app, "query_all", fake_query_all)
    response = map_app.map_alerts_geojson(
        bbox="-74.04,40.75,-74.00,40.79",
        window="custom",
        custom_hours=5,
        min_priority=2,
        active_only=True,
        sources="BNN,NHRFR_DISP,WPD_RADIO",
    )
    assert response.status_code == 200
    assert json.loads(response.body)["features"] == []
    assert "upper(a.source)=ANY(%s)" in captured["sql"]
    assert captured["params"][0] == 5
    assert captured["params"][1] == 2
    assert captured["params"][2] == ["BNN", "NHRFR_DISP", "WPD_RADIO"]
    assert "a.status <> 'RESOLVED'" in captured["sql"]


def test_dashboard_surfaces_intake_and_existing_spatial_operating_picture():
    operations = (ROOT / "operations_app.py").read_text()
    page = (ROOT / "templates" / "index.html").read_text()
    assert "intake_summary" in operations
    assert "workspace_microsoft_mail" in operations
    assert "workspace_calendar_events" in operations
    assert "Executive Intake" in page
    assert "Mapping &amp; Spatial Intelligence" in page


def test_new_templates_compile():
    environment = Environment(loader=FileSystemLoader(ROOT / "templates"))
    environment.get_template("context.html")
    environment.get_template("workspace_hub.html")
    environment.get_template("map.html")


def test_command_palette_routes_intent_without_writing():
    js=(ROOT/"static"/"macro_layer.js").read_text()
    assert "function smartCommandRoute" in js
    assert "intake_source:'MAIL'" in js
    assert "intake_source:'CALENDAR'" in js
    assert "state:'waiting'" in js
    assert "setup_mode:'TOPIC'" in js
    assert "custom_hours" in js
    # Smart commands only route or prefill. They do not POST or create records.
    smart=js.split("function smartCommandRoute",1)[1].split("function use(",1)[0]
    assert "fetch(" not in smart
    assert "POST" not in smart


def test_full_alert_page_has_fixed_and_custom_time_windows():
    source=(ROOT/"operations_app.py").read_text()
    template=(ROOT/"templates"/"alerts.html").read_text()
    assert 'window == "custom"' in source
    assert "custom_hours" in source
    for value in ("1h","2h","4h","6h","12h","24h","3d","7d","30d","custom","all"):
        assert f"('{value}'," in template


def test_local_map_extent_avoids_psycopg_percent_placeholders():
    source=(ROOT/"map_app.py").read_text()
    local=source.split("local_bounds = query_one(",1)[1].split("custom_layers = query_all(",1)[0]
    assert "WEEHAWKEN%" not in local
    assert "position('WEEHAWKEN'" in local


def test_release_installer_pins_a_real_rollback_image():
    installer=(ROOT.parent/"deploy"/"workspace"/"install_workspace.sh").read_text()
    assert 'ROLLBACK_IMAGE="citymanager-dashboard:rollback-' in installer
    assert 'docker image tag "$OLD_IMAGE" "$ROLLBACK_IMAGE"' in installer
    assert 'docker image tag "$ROLLBACK_IMAGE" "$OLD_IMAGE_NAME"' in installer


def test_alert_time_corrections_are_audited_and_bulk_capable():
    source=(ROOT/"operations_app.py").read_text()
    template=(ROOT/"templates"/"alerts.html").read_text()
    assert '@app.post("/alerts/{alert_uuid}/time-correction")' in source
    assert "time_corrections" in source
    assert "prior_observed_at" in source
    assert "prior_received_at" in source
    assert "Only an Executive user can correct stored Alert times" in source
    assert 'action == "shift_time"' in source
    assert 'action == "set_time"' in source
    assert "Bulk exact time correction" in source
    assert "Shift selected activity times" in template
    assert "Set selected activity time" in template
    assert 'name="bulk_activity_at"' in template
    assert 'name="bulk_received_at"' in template
    assert "Correct Alert Time" in template
    assert "Activity" in template and "Received" in template


def test_filter_select_edit_alert_timeline_workflow():
    source=(ROOT/"operations_app.py").read_text()
    template=(ROOT/"templates"/"alerts.html").read_text()
    css=(ROOT/"static"/"macro_layer.css").read_text()
    assert '@app.post("/alerts/bulk-time-edit")' in source
    assert "activity_times: list[str]" in source
    assert "received_modes: list[str]" in source
    assert "Bulk selected alert time correction" in source
    assert 'id="alert-bulk-edit-selected"' in template
    assert 'id="alert-bulk-edit-dialog"' in template
    assert "Edit Selected" in template
    assert "Save All Time Corrections" in template
    assert "function renderBulkTimeEditor()" in template
    assert 'name="activity_times"' in template
    assert 'name="received_modes"' in template
    assert ".alert-bulk-edit-row" in css
