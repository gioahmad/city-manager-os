from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import integration_engine
import integration_runtime as rt
import source_onboarding as so


def test_jsonld_event_parser_extracts_schema_event():
    html = '''
    <html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
      {"@type":"Event","@id":"event-1","name":"MetLife Test Concert",
       "startDate":"2099-09-11T20:00:00-04:00",
       "endDate":"2099-09-11T23:00:00-04:00",
       "url":"https://example.test/event-1",
       "location":{"@type":"Place","name":"MetLife Stadium",
         "address":{"streetAddress":"1 Stadium Dr","addressLocality":"East Rutherford","addressRegion":"NJ","postalCode":"07073"},
         "geo":{"@type":"GeoCoordinates","latitude":40.8,"longitude":-74.0}}}
    ]}
    </script></head></html>
    '''
    events = rt.parse_events(
        html,
        "JSONLD_EVENTS",
        {"future_only": True, "defaults": {"default_timezone": "America/New_York"}},
    )
    assert len(events) == 1
    event = events[0]
    assert event["title"] == "MetLife Test Concert"
    assert event["venue"] == "MetLife Stadium"
    assert event["municipality"] == "East Rutherford"
    assert event["state"] == "NJ"
    assert event["latitude"] == 40.8
    assert event["longitude"] == -74.0
    assert event["impact_level"] in {"WATCH", "ALERT"}


def test_future_only_window_drops_historical_events():
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    body = f'''[
      {{"id":"old","title":"Old","start":"{old}"}},
      {{"id":"future","title":"Future","start":"{future}"}}
    ]'''
    events = rt.parse_events(
        body,
        "JSON_EVENTS",
        {
            "future_only": True,
            "past_grace_hours": 1,
            "mapping": {"id": "id", "title": "title", "start": "start"},
            "defaults": {"default_timezone": "UTC"},
        },
    )
    assert [e["title"] for e in events] == ["Future"]


def test_source_onboarding_exposes_jsonld_template():
    assert "JSONLD_EVENTS" in so.PARSER_KINDS
    assert so.SOURCE_TEMPLATES["JSONLD"]["parser_kind"] == "JSONLD_EVENTS"


def test_run_integration_supports_silent_baseline():
    assert "suppress_alerts" in inspect.signature(integration_engine.run_integration).parameters
