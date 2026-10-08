"""Preview entry is useful without filters and still rejects invalid requests."""
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_preview_entry_needs_criteria_without_querying_history(monkeypatch):
    dashboard = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(dashboard))
    monkeypatch.chdir(dashboard)
    import watch_preview_app as preview

    def unexpected_history_query():
        raise AssertionError("Empty or invalid preview requests must not scan history")

    monkeypatch.setattr(preview, "db_conn", unexpected_history_query)
    monkeypatch.setattr(preview, "query_one", lambda *args, **kwargs: None)
    application = FastAPI()
    application.add_api_route("/watch-preview", preview.watch_preview, methods=["GET"])
    with TestClient(application) as client:
        for params in ({}, {"q": "", "source": "", "category": "", "municipality": "",
                            "county": "", "window": "24h", "custom_hours": 720, "min_priority": 1}):
            response = client.get("/watch-preview", params=params)
            assert response.status_code == 200, response.text
            assert "Choose what to preview" in response.text
            assert 'id="history-results"' not in response.text

        for params, status in (({"latitude": 40.77}, 400),
                               ({"watch_item_id": "invalid"}, 422),
                               ({"radius_ft": 0}, 422),
                               ({"watch_item_id": str(uuid4())}, 404)):
            response = client.get("/watch-preview", params=params)
            assert response.status_code == status, response.text
