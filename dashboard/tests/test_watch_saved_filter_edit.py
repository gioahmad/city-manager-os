"""Saved rules remain editable when their historical source/category rows disappear."""
import re
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import spatial_watch_app as watches


class _Database:
    def __init__(self, current=None):
        self.current = current or {}
        self.unknown = []
        self.saved = None
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params):
        if "FROM unnest" in sql:
            known = {"bnn"} if "a.source" in sql else {"public_safety"}
            self.unknown = [{"filter_value": value} for value in params[0]
                            if value.strip().casefold() not in known]
        elif "UPDATE watch_items SET" in sql:
            columns = re.findall(r"(\w+)=%s", sql.split("spatial_scope=", 1)[0])
            self.saved = dict(zip(columns, params))
        else:
            assert "FROM watch_items WHERE id=%s FOR UPDATE" in sql

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.unknown

    def commit(self):
        self.commits += 1


def test_new_rules_still_require_known_sources_and_categories():
    watches._validate_alert_filters(_Database(), ["BNN"], ["PUBLIC_SAFETY"])
    for sources, categories in ((["ARCHIVED_SOURCE"], []), ([], ["ARCHIVED_CATEGORY"])):
        with pytest.raises(HTTPException, match="Unknown alert"):
            watches._validate_alert_filters(_Database(), sources, categories)


@pytest.mark.parametrize("legacy", [False, True])
def test_untouched_update_retains_archived_filters_and_rejects_unknown_additions(monkeypatch, legacy):
    item_id = uuid4()
    current = {
        "id": item_id, "active": True, "watch_type": "SOURCE" if legacy else "PHRASE",
        "search_term": "ARCHIVED_SOURCE" if legacy else "fire", "aliases": ["ARCHIVED_ALIAS"],
        "match_mode": "FIELD" if legacy else "CONTAINS",
        "match_field": "source" if legacy else "search_text", "nearby_enabled": False,
        "source_filter": [] if legacy else ["ARCHIVED_SOURCE", "ARCHIVED_ALIAS"],
        "alert_category_filter": ["ARCHIVED_CATEGORY"], "had_active_recipient": True,
    }
    database = _Database(current)
    monkeypatch.setattr(watches, "db_conn", lambda: database)
    monkeypatch.setattr(watches, "_save_recipients", lambda *args: None)
    monkeypatch.setattr(watches, "require_watch_recipients", lambda *args: None)
    application = FastAPI()
    application.add_api_route("/watchlist/{item_id}/update", watches.spatial_watch_update, methods=["POST"])
    values = {
        "display_name": "Archived rule", "setup_mode": "TOPIC", "location_scope": "ANYWHERE",
        "keep_state": "1", "watch_type": current["watch_type"], "match_mode": current["match_mode"],
        "match_field": current["match_field"], "search_term": "" if legacy else "fire",
        "source_filter": "archived_source, ARCHIVED_ALIAS", "alert_category_filter": "archived_category",
        "min_priority": "4",
    }
    with TestClient(application) as client:
        response = client.post(f"/watchlist/{item_id}/update", data=values, follow_redirects=False)
        assert response.status_code == 303 and "error=" not in response.headers["location"], response.headers
        assert database.saved["source_filter"] == ["archived_source", "ARCHIVED_ALIAS"]
        assert database.saved["alert_category_filter"] == ["archived_category"]
        assert database.saved["min_priority"] == 4
        if legacy:
            assert database.saved["watch_type"] == "SOURCE"
            assert database.saved["match_mode"] == "FIELD" and database.saved["match_field"] == "source"
            assert database.saved["search_term"] == "archived_source"
            assert database.saved["aliases"] == ["ARCHIVED_ALIAS"]
        for field in ("source_filter", "alert_category_filter"):
            database.saved = None
            response = client.post(f"/watchlist/{item_id}/update", data={
                **values, field: values[field] + ", UNKNOWN_ADDITION",
            }, follow_redirects=False)
            assert response.status_code == 303 and "Unknown+alert" in response.headers["location"]
            assert database.saved is None and database.commits == 1


def test_only_exact_legacy_source_rules_can_supply_retained_source_candidates():
    legacy = {"watch_type": "SOURCE", "match_mode": "FIELD", "match_field": "source",
              "search_term": "ARCHIVED_SOURCE", "aliases": ["ARCHIVED_ALIAS"]}
    for change in ({"watch_type": "PHRASE"}, {"match_mode": "CONTAINS"},
                   {"match_field": "title"}, {"nearby_enabled": True}, {"source_filter": ["BNN"]}):
        with pytest.raises(HTTPException, match="Unknown alert source"):
            watches._validate_alert_filters(_Database(), ["ARCHIVED_ALIAS"], [], existing={**legacy, **change})
