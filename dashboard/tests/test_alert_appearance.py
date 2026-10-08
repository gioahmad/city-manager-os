"""Appearance saves are scoped and validated without changing stored alert content."""
from copy import deepcopy
import json
import shutil
from pathlib import Path
import subprocess

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import alert_appearance as appearance


def test_backend_defaults_match_the_shared_renderer():
    if not shutil.which("node"):
        pytest.skip("node is required for shared browser/sender default parity")
    result = subprocess.run(
        ["node", "-e", "console.log(JSON.stringify(require('./static/alert_content.js').DEFAULTS))"],
        cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == appearance.APPEARANCE_DEFAULTS


class Database:
    def __init__(self):
        self.writes = []
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.writes.append((sql, params))

    def commit(self):
        self.commits += 1


def client_for(monkeypatch, database, role="EXECUTIVE"):
    monkeypatch.setattr(appearance, "_settings_and_sources", lambda: ({}, ["PSEG", "BNN", "ORU"]))
    monkeypatch.setattr(appearance, "db_conn", lambda: database)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.cmos_role = role
        return await call_next(request)

    app.add_api_route("/alerts/appearance", appearance.save_alert_appearance, methods=["POST"])
    return TestClient(app)


def test_defaults_and_source_channel_isolation():
    settings = {"alert_appearance": {"PSEG": {"dashboard": {
        "mapping_link": True, "explanation": "true", "source_link": False, "unknown": True,
    }}}}
    original = deepcopy(settings)
    dashboard = appearance.resolve_appearance(settings, "PSEG", "dashboard")
    assert dashboard["mapping_link"] is True and dashboard["source_link"] is False
    assert dashboard["explanation"] is False and "unknown" not in dashboard
    assert appearance.resolve_appearance(settings, "PSEG", "notification") == appearance.APPEARANCE_DEFAULTS
    assert appearance.resolve_appearance(settings, "BNN", "dashboard") == appearance.APPEARANCE_DEFAULTS
    assert settings == original


def test_save_serializes_both_channels_and_supports_native_and_json(monkeypatch):
    database = Database()
    with client_for(monkeypatch, database) as client:
        values = {"source": "PSEG", "dashboard.mapping_link": "true", "notification.source_link": "true"}
        response = client.post("/alerts/appearance", data=values, headers={"Accept": "application/json"})
        assert response.status_code == 200
        saved = response.json()["settings"]
        assert saved["dashboard"]["mapping_link"] is True
        assert saved["notification"]["mapping_link"] is False
        assert saved["notification"]["source_link"] is True
        assert saved["dashboard"]["source_link"] is False
        assert set(saved) == set(appearance.CHANNELS)
        assert all(set(channel) == set(appearance.APPEARANCE_DEFAULTS) for channel in saved.values())
        assert database.writes[0][1][0].obj == {"PSEG": saved}
        assert database.commits == 1
        response = client.post("/alerts/appearance", data={"source": "BNN"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].endswith("#alert-appearance")


@pytest.mark.parametrize("values", [
    {"source": "UNKNOWN"},
    {"source": "PSEG", "notification.unknown": "true"},
    {"source": "PSEG", "dashboard.mapping_link": "yes"},
    {"source": "PSEG", "dashboard.mapping_link": ["true", "false"]},
])
def test_invalid_choices_do_not_write(monkeypatch, values):
    database = Database()
    with client_for(monkeypatch, database) as client:
        assert client.post("/alerts/appearance", data=values).status_code == 400
    assert database.writes == [] and database.commits == 0


@pytest.mark.parametrize("role", ["READ_ONLY", "SUPERVISOR", ""])
def test_only_executive_can_save(monkeypatch, role):
    database = Database()
    with client_for(monkeypatch, database, role) as client:
        assert client.post("/alerts/appearance", data={"source": "PSEG"}).status_code == 403
    assert database.writes == []


def test_context_reuses_catalog_keeps_saved_sources_and_reports_missing_sample(monkeypatch):
    monkeypatch.setattr(appearance, "query_one", lambda *args: {
        "settings": {"alert_appearance": {"ARCHIVED": {"dashboard": {"source_link": False}}}}
    })
    calls = []

    def samples(sql, params):
        calls.append((sql, params))
        return [{"source": "BNN", "alert_id": "BNN:1", "message": "Stored incident", "metadata": {}}]

    monkeypatch.setattr(appearance, "query_all", samples)
    context = appearance.alert_appearance_context(source_rows=[{"source": "BNN"}, {"source": "ORU"}])
    assert context["appearance_sources"] == ["ARCHIVED", "BNN", "ORU", "PSEG"]
    assert set(context["appearance_samples"]) == {"BNN"}
    assert context["appearance_can_edit"] is False
    assert len(calls) == 1 and "LIMIT 1" in calls[0][0]
