import json
import hashlib
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
    assert "Open full record" in js
    assert "Microsoft calendar" in template
    assert "Microsoft email" in template
    assert "EXECUTIVE INTAKE" in template
    assert 'href="/intake"' in nav and "Executive Intake" in nav
    assert 'href="/inbox"' in nav and "Quick Capture Inbox" in nav


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
    assert "Save Selected Timeline" in template
    assert "function renderBulkTimeEditor()" in template
    assert "activity.name='activity_times'" in template
    assert "mode.name='received_modes'" in template
    assert "received.name='received_times'" in template
    assert "reason.name='reasons'" in template
    assert ".alert-bulk-edit-row" in css


def test_system_update_converges_app_services_without_pruning_data():
    deploy_root=ROOT.parent/"deploy"
    updater=(deploy_root/"cmos-system-update").read_text()
    maintenance=(deploy_root/"cmos-maintenance").read_text()

    assert 'CMOS_APP_IMAGE="$RELEASE_TAG"' in updater
    assert 'dashboard/docker-compose.yml' in updater
    for service in (
        "citymanager-dashboard",
        "citymanager-staff",
        "citymanager-ops-engine",
        "citymanager-integration-engine",
    ):
        assert service in updater
    assert "citymanager-intake" in updater
    assert 'RELEASE_TAG="citymanager-os-app:' in updater
    assert "IMAGE PARITY" in updater
    assert "AUTHENTICATED APPLICATION SMOKE" in updater
    assert "bash deploy/cmos-health" in updater
    assert "bash deploy/cmos-maintenance clean" in updater

    # Maintenance may remove disposable build artifacts, but never live data.
    assert "docker image prune -f" in maintenance
    assert "docker builder prune -f --filter until=168h" in maintenance
    assert "docker volume prune" not in maintenance
    assert "docker system prune" not in maintenance
    assert "docker container prune" not in maintenance
    assert "apt-get -s upgrade" in maintenance
    assert "apt-get upgrade" not in maintenance
    assert "No volumes, running containers, GIS artifacts, n8n data" in maintenance


def test_what_changed_is_an_exception_brief():
    source=(ROOT/"executive_workflow_app.py").read_text()
    page=(ROOT/"templates"/"executive_changes.html").read_text()
    strip=(ROOT/"templates"/"executive_changes_strip.html").read_text()
    assert "coalesce(observed_at,received_at) AS activity_at" in source
    assert "watch_matches = query_all(" in source
    assert "delivery_failures = query_all(" in source
    assert "overdue_work = query_all(" in source
    assert "NEEDS YOUR ATTENTION" in page
    assert "New Watch Matches" in page
    assert "Failed Notifications" in page
    assert "/context/WORK/" in page
    assert "/context/ALERT/" in page
    assert "Watch matches" in strip


def test_context_two_surfaces_metrics_evidence_and_project_actions():
    source=(ROOT/"context_app.py").read_text()
    page=(ROOT/"templates"/"context.html").read_text()
    css=(ROOT/"static"/"context.css").read_text()
    assert "def _context_insights(" in source
    assert "FROM alert_watch_matches" in source
    assert "FROM deliveries" in source
    assert "FROM issues WHERE id=%s" in source
    assert '("Search Everything"' in source
    assert '("Find Project Work"' in source
    assert "OPERATIONAL EVIDENCE" in page
    assert "context-metrics" in page
    assert "Technical details" in page
    assert ".context-metrics" in css


def test_polished_templates_compile():
    environment=Environment(loader=FileSystemLoader(ROOT/"templates"))
    environment.get_template("executive_changes.html")
    environment.get_template("executive_changes_strip.html")
    environment.get_template("context.html")


def test_workspace_polish_uses_current_assets_and_alert_windows():
    app=(ROOT/"workspace_app.py").read_text()
    page=(ROOT/"templates"/"workspace.html").read_text()
    nav=(ROOT/"templates"/"nav.html").read_text()
    context=(ROOT/"templates"/"context.html").read_text()

    for value in ("1h","2h","4h","6h","12h","24h","3d","7d","30d","all"):
        assert f"'{value}'" in app
        assert f'value="{value}"' in page
    assert "People, places &amp; projects" in page
    # ASSET_CONTENT_CONTRACT: real files plus content-derived cache revisions.
    for document, assets in (
        (page, ('workspace.js', 'workspace_hub.js', 'workspace.css')),
        (nav, ('macro_layer.css', 'macro_layer.js')),
        (context, ('context.css',)),
    ):
        for asset in assets:
            path = ROOT / 'static' / asset
            assert path.is_file(), asset
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            assert f'{asset}?v={digest}' in document, f'Stale or missing cache key: {asset}'



def test_alert_timeline_editor_supports_date_time_shift_sequence_and_sort():
    template=(ROOT/"templates"/"alerts.html").read_text()
    css=(ROOT/"static"/"macro_layer.css").read_text()

    assert "Repair Alert Timeline" in template
    assert 'id="alert-bulk-date-only"' in template
    assert 'id="alert-bulk-time-only"' in template
    assert 'id="alert-bulk-shift-days"' in template
    assert 'id="alert-bulk-shift-hours"' in template
    assert 'id="alert-bulk-shift-minutes"' in template
    assert 'id="alert-bulk-sequence-start"' in template
    assert 'id="alert-bulk-sequence-spacing"' in template
    assert 'id="alert-bulk-sort-oldest"' in template
    assert 'id="alert-bulk-sort-newest"' in template
    assert 'id="alert-bulk-reset"' in template
    assert "Set Date · Keep Times" in template
    assert "Set Time · Keep Dates" in template
    assert "Sequence Rows" in template
    assert "Save Selected Timeline" in template
    assert "function sortEditor(direction)" in template
    assert "dataset.originalValue" in template
    assert ".alert-bulk-tool-group" in css


def test_operational_health_and_delivery_pages_have_context_actions():
    source=(ROOT/"operations_app.py").read_text()
    health=(ROOT/"templates"/"source_health.html").read_text()
    deliveries=(ROOT/"templates"/"deliveries.html").read_text()

    assert '"healthy": sum(' in source
    assert '"attention": sum(' in source
    assert 'row["alerts_url"]' in source
    assert 'row["search_url"]' in source
    assert "delivery_counts = query_one(" in source
    assert 'a.id AS alert_uuid' in source
    assert 'row["alert_context_url"]' in source
    assert "Needs Attention" in health
    assert "Search Context" in health
    assert "Last 24h" in deliveries
    assert "Alert Context" in deliveries
    assert "Alert History" in deliveries


def test_product_docs_reflect_current_cohesion_phase():
    feature=(ROOT.parent/"docs"/"FEATURE_MATRIX.md").read_text()
    roadmap=(ROOT.parent/"docs"/"ROADMAP.md").read_text()
    readme=(ROOT.parent/"README.md").read_text()
    assert "October 4, 2026" in feature
    assert "| Executive Intake | Working" in feature
    assert "| Universal Context Inspector | Working" in feature
    assert "| Alert Timeline Repair | Working" in feature
    assert "Context Everywhere" in roadmap
    assert "Executive Exception Layer" in roadmap
    assert "Alert / Watch / Map Cohesion" in roadmap
    assert "Context Everywhere" in readme


def test_fast_release_backup_gate_and_runtime_performance_foundation():
    app=(ROOT/"app.py").read_text()
    requirements=(ROOT/"requirements.txt").read_text()
    installer=(ROOT.parent/"deploy"/"workspace"/"install_workspace.sh").read_text()
    updater=(ROOT.parent/"deploy"/"cmos-system-update").read_text()
    gate=(ROOT.parent/"deploy"/"postgis"/"ensure-deploy-backup.sh").read_text()
    migration=(ROOT.parent/"deploy"/"postgis"/"init"/"041_performance_indexes.sql").read_text()
    perf=(ROOT.parent/"deploy"/"cmos-performance-audit").read_text()

    assert "psycopg[binary,pool]" in requirements
    assert "ConnectionPool" in app
    assert "def _db_pool()" in app
    assert "X-CMOS-Response-Ms" in app
    assert "slow_request" in app

    assert "ensure-deploy-backup.sh" in installer
    assert "CMOS_PREVIOUS_RELEASE" in installer
    assert "041_performance_indexes.sql" in installer
    assert "last-successful-release" in updater
    assert "CMOS_PREVIOUS_RELEASE" in updater

    assert "verify-backup.sh" in gate
    assert "database/GIS-sensitive files changed" in gate
    assert "reusing recent validated recovery point" in gate
    assert "CMOS_FORCE_FULL_BACKUP" in gate

    assert "idx_alerts_activity_time" in migration
    assert "idx_alert_watch_matches_watch_recent" in migration
    assert "idx_deliveries_status_attempted_recent" in migration
    assert "idx_workspace_context_links_source_recent" in migration
    assert "idx_issues_open_follow_up" in migration

    assert "CITY MANAGER OS PERFORMANCE AUDIT" in perf
    assert "Workspace Intelligence" in perf
    assert "cache_hit_pct" in perf


def test_canonical_navigation_and_actionable_my_day_contract():
    nav=(ROOT/"templates"/"nav.html").read_text()
    workspace=(ROOT/"static"/"workspace.js").read_text()
    hub=(ROOT/"workspace_hub.py").read_text()
    hub_js=(ROOT/"static"/"workspace_hub.js").read_text()
    macro=(ROOT/"static"/"macro_layer.js").read_text()
    my_day=(ROOT/"templates"/"my_day.html").read_text()
    overview=(ROOT/"templates"/"index.html").read_text()
    ops_today=(ROOT/"templates"/"operations_my_day_insert.html").read_text()
    workspace_app=(ROOT/"workspace_app.py").read_text()

    assert "@app.get('/intake')" in workspace_app
    assert "@app.get('/library')" in workspace_app
    assert 'href="/intake"' in nav
    assert "Executive Intake" in nav
    assert "Quick Capture Inbox" in nav
    assert "/workspace#inbox" not in nav
    assert "/workspace#inbox" not in macro
    assert "/workspace#inbox" not in hub
    assert "/workspace#inbox" not in workspace
    assert "['▣','Executive Intake'" in macro
    assert "new URLSearchParams(location.search).get('view')" in workspace

    assert "('/intake?kind=MAIL&id='||m.id::text)" in hub
    assert "('/intake?kind=CALENDAR&id='||e.id::text)" in hub
    assert "('/context/WORK/'||i.id::text)" in hub
    assert "('/context/ALERT/'||r.id::text)" in hub
    assert "('/context/RECORD/'||e.id::text)" in hub
    assert "pendingOpen" in hub_js
    assert "async function openExact(kind,id)" in hub_js
    assert "Open full record" in hub_js

    assert '<div class="feed-row">' not in my_day
    assert "/context/WORK/" in my_day
    assert "/context/EVENT/" in my_day
    assert "/context/ALERT/" in my_day
    assert 'data-cmos-open="full"' in my_day
    assert 'data-cmos-open="full"' in ops_today
    assert "target.dataset.cmosOpen==='full'" in macro

    assert '/intake?kind={{ item.kind|urlencode }}&id={{ item.id }}' in overview
    assert 'data-cmos-context="/context/EVENT/{{ e.id }}"' in overview
    assert 'data-cmos-context="/context/WORK/{{ i.id }}"' in overview
    assert 'data-cmos-context="/context/ALERT/{{ a.id }}"' in overview


def test_intake_deep_link_does_not_close_when_source_is_outside_first_page():
    hub_js=(ROOT/"static"/"workspace_hub.js").read_text()
    pending=hub_js.index("if(!append&&pendingOpen)")
    selected=hub_js.index("if(selected&&!pendingOpen)")
    assert selected < pending
    assert "else await openExact(wanted.kind,wanted.id)" in hub_js


def test_intake_keeps_exact_record_in_url_and_preserves_deep_link_on_initial_load():
    hub_js=(ROOT/"static"/"workspace_hub.js").read_text()
    assert "function syncExactUrl(row)" in hub_js
    assert "params.set('kind',row.kind)" in hub_js
    assert "params.set('id',row.id)" in hub_js
    assert "closePreview(false)" in hub_js
    assert "syncExactUrl(selected)" in hub_js


def test_alert_place_filters_normalize_case_and_add_county():
    source=(ROOT/"operations_app.py").read_text()
    template=(ROOT/"templates"/"alerts.html").read_text()
    assert "county: str = \"\"" in source
    assert "(county, \"a.county\")" in source
    assert "[[:space:]]+" in source
    assert "GROUP BY lower(regexp_replace(trim(municipality)" in source
    assert "GROUP BY lower(regexp_replace(trim(county)" in source
    assert 'select name="county"' in template
    assert 'name="county" value="{{ county }}"' in template
    assert "case-insensitively" in template
    assert "def _alert_place_values" in source
    assert "municipality_values = _alert_place_values(municipality)" in source
    assert 'id="alerts-municipalities" multiple' in template
    assert 'id="alerts-county"' in template
    assert "data-county" in template
    assert "join('|')" in template
