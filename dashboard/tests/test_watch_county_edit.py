"""Editing county + topic Watches preserves their saved county and recipients."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def watch_app(monkeypatch):
    dashboard = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(dashboard))
    monkeypatch.chdir(dashboard)
    import spatial_watch_app
    return spatial_watch_app


def county_topic(**changes):
    return {
        "id": uuid4(), "active": True, "active_recipient_count": 1,
        "had_active_recipient": True, "watch_type": "LOCATION_TOPIC",
        "display_name": "Hudson fire", "search_term": "working fire",
        "match_mode": "CONTAINS", "match_field": "search_text",
        "county": "Hudson", "state": "NJ", "municipality": None,
        "target_wkt": None, "spatial_target_type": None, "address": None,
        "nearby_enabled": False, **changes,
    }


def test_edit_metadata_recognizes_county_without_overriding_city_or_target(watch_app, monkeypatch):
    reference_id = uuid4()
    cases = [
        (county_topic(), "COUNTY", "Hudson"),
        (county_topic(watch_type="COUNTY"), "COUNTY", "Hudson"),
        (county_topic(municipality="Union City"), "MUNICIPALITY", "Union City"),
        (county_topic(spatial_target_type="ST_Point"), "EXISTING", ""),
        (county_topic(spatial_reference_entity_id=reference_id), "REFERENCE", str(reference_id)),
        (county_topic(watch_type="PHRASE"), "TYPED_ADDRESS", ""),
    ]
    monkeypatch.setattr(watch_app, "query_all", lambda sql, *args: [dict(row) for row, _, _ in cases]
                        if "FROM watch_items w" in sql else [])
    monkeypatch.setattr(watch_app, "query_one", lambda *args: {})
    monkeypatch.setattr(watch_app, "_watch_health", lambda: {})
    monkeypatch.setattr(watch_app, "templates", SimpleNamespace(
        TemplateResponse=lambda **kwargs: kwargs["context"]))
    page = watch_app.spatial_watchlist(
        SimpleNamespace(state=SimpleNamespace()), selected_keywords=[], radius_ft=5280)
    for row, (_, expected_kind, expected_id) in zip(page["items"], cases, strict=True):
        assert (row["location_kind"], row["location_id"]) == (expected_kind, expected_id)


@pytest.mark.parametrize("changes,kind,watch_type", [
    ({}, "COUNTY", "COUNTY"),
    ({"watch_type": "COUNTY"}, "COUNTY", "COUNTY"),
    ({"municipality": "Union City"}, "MUNICIPALITY", "TOWN"),
    ({"target_wkt": "SRID=4326;POINT(-74.03 40.76)"}, "EXISTING", "AREA"),
    ({"watch_type": "POINT", "target_wkt": "SRID=4326;POINT(-74.03 40.76)"}, "EXISTING", "POINT"),
])
def test_existing_location_keeps_county_city_and_geometry_precedence(watch_app, changes, kind, watch_type):
    current = county_topic(**changes)
    target = watch_app._selected_location(
        None, kind="EXISTING", source_id="", location_query="", latitude="",
        longitude="", municipality="", current=current, scope="EXISTING")
    assert (target["kind"], target["watch_type"]) == (kind, watch_type)
    assert (target["county"], target["state"]) == ("Hudson", "NJ")
    assert target["replace_target"] is False
    assert target.get("target_wkt") == current.get("target_wkt")
    assert target["spatial"] is bool(current.get("target_wkt"))


def test_unrelated_topic_county_metadata_does_not_become_a_location(watch_app):
    with pytest.raises(HTTPException) as error:
        watch_app._selected_location(
            None, kind="EXISTING", source_id="", location_query="", latitude="",
            longitude="", municipality="", current=county_topic(watch_type="PHRASE"))
    assert error.value.status_code == 400


@pytest.mark.parametrize("scope", ["EXISTING", "COUNTY"])
def test_county_topic_name_and_recipient_edits_save_without_geometry(watch_app, monkeypatch, scope):
    current, recipient_id = county_topic(), uuid4()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = [current, {"id": recipient_id}, {"total": 0}]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    monkeypatch.setattr(watch_app, "db_conn", lambda: nullcontext(conn))
    application = FastAPI()
    application.add_api_route("/watchlist/{item_id}/update", watch_app.spatial_watch_update, methods=["POST"])
    with TestClient(application) as client:
        response = client.post(f"/watchlist/{current['id']}/update", data={
            "display_name": "Hudson working fires", "setup_mode": "LOCATION_TOPIC",
            "search_term": current["search_term"], "match_mode": "CONTAINS",
            "match_field": "search_text", "location_kind": scope, "location_scope": scope,
            "location_id": "Hudson" if scope == "COUNTY" else "",
            "keep_state": "1", "subscriber_ids": str(recipient_id),
        }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/watchlist?msg=Watch+updated"
    calls = [call.args for call in cursor.execute.call_args_list]
    update = next(params for sql, params in calls if "UPDATE watch_items SET" in sql)
    assert update[0:4] == (True, "LOCATION_TOPIC", "Hudson working fires", "working fire")
    assert update[5:7] == ("CONTAINS", "search_text")
    assert update[11:14] == (None, "Hudson", "NJ")
    assert update[23:25] == (False, False)
    assert update[31:33] == (None, None)
    assert any("INSERT INTO watch_item_recipients" in sql and params == (current["id"], recipient_id)
               for sql, params in calls)
    conn.commit.assert_called_once()
