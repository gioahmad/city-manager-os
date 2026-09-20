from pathlib import Path

from jinja2 import Environment, FileSystemLoader

import issues_app


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (DASHBOARD_ROOT / name).read_text()


def test_alert_builds_an_editable_work_item_draft_without_writing_data():
    source = _read("issues_app.py")
    helper = source.split("def _alert_work_prefill", 1)[1].split('@app.get("/issues"', 1)[0]

    assert "FROM alerts" in helper
    assert "WHERE alert_id=%s" in helper
    assert "Alert reference:" in helper
    assert "INSERT" not in helper
    assert "UPDATE" not in helper
    assert 'from_alert: str = ""' in source
    assert 'source not in {"MANUAL", "ALERT"}' in source


def test_alert_prefill_carries_operational_context(monkeypatch):
    def fake_query(sql, params):
        assert "WHERE alert_id=%s" in sql
        assert params == ("BNN:123",)
        return {
            "alert_id": "BNN:123",
            "title": "Working fire",
            "message": "Second alarm reported.",
            "source": "BNN",
            "category": "FIRE",
            "priority": 5,
            "municipality": "Union City",
            "address": "123 Bergenline Ave",
            "received_local": "09/20/2026 01:15 PM",
            "click_url": "https://example.test/incident/123",
        }

    monkeypatch.setattr(issues_app, "query_one", fake_query)
    prefill = issues_app._alert_work_prefill(" BNN:123 ")

    assert prefill["title"] == "Working fire"
    assert prefill["priority"] == 5
    assert prefill["municipality"] == "Union City"
    assert prefill["address"] == "123 Bergenline Ave"
    assert prefill["source"] == "ALERT"
    assert "Alert reference: BNN:123" in prefill["description"]
    assert "Second alarm reported." in prefill["description"]


def test_alert_map_and_notification_history_offer_the_same_tracking_action():
    operations = _read("operations_app.py")
    alerts = _read("templates/alerts.html")
    deliveries = _read("templates/deliveries.html")
    mapping = _read("templates/map.html")

    assert operations.count('row["track_alert_url"]') == 1
    assert 'alert["track_alert_url"]' in operations
    assert "Track This Alert" in alerts
    assert "Track This Alert" in deliveries
    assert "Track This Alert" in mapping
    assert "from_alert:merged.alert_id" in mapping


def test_command_center_reviews_prefill_before_normal_issue_submission():
    template = _read("templates/issues.html")

    assert "Track Alert as Work Item" in template
    assert "Nothing has been saved and the original Alert is unchanged." in template
    assert 'name="source"' in template
    assert 'action="/issues/create"' in template
    assert "Create Tracked Work Item" in template


def test_track_alert_templates_compile():
    environment = Environment(loader=FileSystemLoader(DASHBOARD_ROOT / "templates"))
    for template in ("issues.html", "alerts.html", "deliveries.html", "map.html"):
        environment.get_template(template)
