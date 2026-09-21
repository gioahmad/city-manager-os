from pathlib import Path


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
TIMEZONE_SQL_FILES = (
    "executive_workflow_app.py",
    "flood_app.py",
    "integrations_app.py",
    "issues_app.py",
    "operations_app.py",
    "operations_occurrence_controls.py",
    "operations_routines_app.py",
    "schedule_app.py",
    "today_board_app.py",
    "transit_app.py",
)


def test_runtime_sql_uses_the_configured_postgres_timezone():
    for name in TIMEZONE_SQL_FILES:
        source = (DASHBOARD_ROOT / name).read_text()
        assert "AT TIME ZONE 'America/New_York'" not in source, name
        assert "AT TIME ZONE current_setting('TimeZone')" in source, name

    compose = (DASHBOARD_ROOT / "docker-compose.yml").read_text()
    assert compose.count('PGTZ: "${APP_TIMEZONE:-America/New_York}"') == 4


def test_admin_tools_exposes_timezone_and_connection_pressure_without_new_storage():
    source = (DASHBOARD_ROOT / "integrations_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates" / "admin_tools.html").read_text()

    assert "current_setting('TimeZone') AS application_timezone" in source
    assert "FROM pg_stat_activity" in source
    assert "datname=current_database() AND backend_type='client backend'" in source
    assert "current_setting('max_connections')::integer" in source
    assert "Database Sessions" in template
    assert "Current use / PostgreSQL limit" in template
    assert "Local Time Zone" in template
    assert "Used by deadlines, events and routines" in template
    assert "CREATE TABLE" not in source
