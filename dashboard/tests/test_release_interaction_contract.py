from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text()


def test_all_dashboard_templates_compile():
    env = Environment(loader=FileSystemLoader(ROOT / "templates"))
    for path in sorted((ROOT / "templates").glob("*.html")):
        env.get_template(path.name)


def test_canonical_navigation_contract_has_no_legacy_workspace_hashes():
    core = "\n".join(
        read(path)
        for path in (
            "templates/nav.html",
            "templates/workspace.html",
            "templates/workspace_tools.html",
            "static/macro_layer.js",
            "static/workspace.js",
            "static/workspace_hub.js",
            "workspace_hub.py",
            "context_app.py",
        )
    )
    assert "/workspace#inbox" not in core
    assert "/workspace#today" not in core
    assert "/workspace#people" not in core
    assert "/workspace#settings" not in core
    assert 'href="/intake"' in read("templates/nav.html")
    assert "Executive Intake" in read("templates/nav.html")
    assert "Quick Capture Inbox" in read("templates/nav.html")
    assert "Workspace Today" in read("templates/workspace.html")
    assert "People, places &amp; projects" in read("templates/workspace.html")


def test_exact_record_drilldown_contract():
    context = read("context_app.py")
    workspace = read("static/workspace.js")
    hub = read("static/workspace_hub.js")
    search = read("operations_app.py")

    assert 'f"/issues?focus={record_id}&state=all"' in context
    assert 'f"/schedule?focus={record_id}&state=all"' in context
    assert 'f"/watchlist?focus={record_id}"' in context
    assert 'f"/intake?kind=MAIL&id={record_id}"' in context
    assert 'f"/intake?kind=CALENDAR&id={record_id}"' in context

    assert "'/issues?focus=' + encodeURIComponent(work.id)" in workspace
    assert "'/context/ALERT/' + encodeURIComponent(alert.id)" in workspace
    assert "'/intake?kind=CALENDAR&id='+encodeURIComponent(event.id)" in workspace

    assert "Open editable Work" in hub
    assert "Open editable Event" in hub
    assert "Open Alert controls" in hub
    assert "Open related Work" in hub

    assert 'return f"/context/ALERT/{result_id}"' in search
    assert "urlencode({'focus': result_id, 'state': 'all'})" in search
    assert "urlencode({'focus': result_id})" in search


def test_exact_focus_supported_by_operational_modules():
    issues = read("issues_app.py")
    schedule = read("schedule_app.py")
    watches = read("spatial_watch_app.py")
    routines = read("operations_routines_app.py")
    events = read("integrations_app.py")
    transit = read("transit_app.py")

    for source, phrase in (
        (issues, 'focus: str = ""'),
        (schedule, 'focus: str = ""'),
        (watches, 'focus: str = ""'),
        (routines, 'focus: str = ""'),
        (events, 'focus: str = ""'),
        (transit, 'focus: str = ""'),
    ):
        assert phrase in source


def test_my_day_composition_replaces_previous_get_routes_in_order():
    operations = read("operations_routines_app.py")
    today = read("today_board_app.py")
    executive = read("executive_workflow_app.py")

    assert 'remove_existing_get("/my-day")' in operations
    assert '_remove_get("/my-day")' in today
    assert '_remove_get("/my-day")' in executive

    assert "response = schedule_module.my_day(request)" in operations
    assert "response = routines_module.phase3_my_day(request)" in today
    assert "response = today_module.today_board_my_day(request)" in executive
    assert "executive_changes_strip.html" in executive


def test_my_day_uses_one_database_review_checkpoint():
    schedule = read("schedule_app.py")
    executive = read("executive_workflow_app.py")
    assert "executive_review_state" in schedule
    assert "executive_review_state" in executive
    assert "cmos_my_day_reviewed" not in schedule


def test_intake_promotions_are_idempotent_and_source_state_is_preserved():
    hub = read("workspace_hub.py")
    assert "This source is already linked to Command Center." in hub
    assert "This source is already linked to Events Center." in hub
    assert "workspace_inbox_handled" in hub
    assert "workspace_inbox_snoozed" in hub
    assert "inbox_state" in hub


def test_release_gate_runs_entire_dashboard_suite():
    installer = (ROOT.parent / "deploy" / "workspace" / "install_workspace.sh").read_text()
    assert "--entrypoint python citymanager-dashboard -m pytest -q tests" in installer
