from pathlib import Path

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader

from operations_app import require_watch_recipients


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]


class _Cursor:
    def __init__(self, total):
        self.total = total
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchone(self):
        return {"total": self.total}


def _read(name):
    return (DASHBOARD_ROOT / name).read_text()


def test_active_watch_recipient_guard_is_set_based_and_clear():
    cursor = _Cursor(0)
    require_watch_recipients(cursor, ["first", "first", "second"])
    sql, params = cursor.calls[0]
    assert "id=ANY(%s::uuid[])" in sql
    assert "w.expires_at IS NULL OR w.expires_at>now()" in sql
    assert "wir.active=true AND s.active=true" in sql
    assert params == (["first", "second"],)

    with pytest.raises(HTTPException, match="active Recipient"):
        require_watch_recipients(_Cursor(2), ["first", "second"])


def test_standard_watch_activation_and_repair_share_existing_routing_tables():
    source = _read("spatial_watch_app.py")
    template = _read("templates/watchlist.html")
    assert '@app.post("/watchlist/repair-unrouted")' in source
    repair = source.split("def spatial_watch_repair_unrouted", 1)[1].split(
        "def _insert_watch_item", 1
    )[0]
    assert "INSERT INTO watch_item_recipients" in repair
    assert "NOT EXISTS" in repair
    assert "INSERT INTO watch_items" not in repair
    assert "INSERT INTO deliveries" not in repair
    assert 'activation: str = Form("on")' in source
    assert '"active": turn_on' in source
    assert source.count("require_watch_recipients(cur,") >= 4
    assert 'action="/watchlist/repair-unrouted"' in template
    assert 'name="activation" value="on"' in template
    assert 'name="activation" value="paused"' in template


def test_every_browser_watch_activation_path_uses_the_same_guard():
    expected = {
        "alert_admin_v2.py": 2,
        "integrations_app.py": 1,
        "operations_app.py": 5,
        "rules_app.py": 3,
        "spatial_reference_app.py": 1,
    }
    for filename, minimum in expected.items():
        assert _read(filename).count("require_watch_recipients(cur,") >= minimum


def test_scheduled_watch_without_recipient_is_not_reported_ready():
    source = _read("spatial_watch_app.py")
    state_helper = source.split("def _watch_state", 1)[1].split("for route_path", 1)[0]
    assert state_helper.index('return "Needs Recipient"') < state_helper.index(
        'return "Watching", "waiting"'
    )


def test_watch_templates_compile_after_readiness_controls():
    environment = Environment(loader=FileSystemLoader(DASHBOARD_ROOT / "templates"))
    for template in ("watchlist.html", "spatial_reference_detail.html"):
        environment.get_template(template)
