from __future__ import annotations

from bnn_reparser import classify, search_text


def _municipalities():
    return {
        "JERSEY CITY": "Jersey City",
        "WEEHAWKEN": "Weehawken",
        "UNION CITY": "Union City",
        "HOBOKEN": "Hoboken",
    }


def test_classify_bnn_pipe_by_semantic_value_not_position():
    row = {
        "message": "FIRE | Jersey City | NJ | BNN123 | Hudson County | 123 Newark Ave",
        "county": "",
        "municipality": "",
        "location": {},
        "metadata": {"bnn_source_payload": {}},
        "raw_payload": {},
        "resolution": {},
    }
    parsed = classify(row, _municipalities())
    assert parsed["state"] == "NJ"
    assert parsed["county"] == "Hudson"
    assert parsed["county_source"] == "pipe_segment"
    assert parsed["municipality"] == "Jersey City"
    assert parsed["municipality_source"] == "pipe_segment"


def test_explicit_or_resolved_geography_beats_pipe_fallback():
    row = {
        "message": "NJ | Hudson | Jersey City | FIRE",
        "county": "Hudson",
        "municipality": "",
        "location": {},
        "metadata": {"bnn_source_payload": {"city": "Weehawken"}},
        "raw_payload": {},
        "resolution": {"status": "RESOLVED", "municipality": "Union City", "county": "Hudson"},
    }
    parsed = classify(row, _municipalities())
    assert parsed["municipality"] == "Weehawken"
    assert parsed["municipality_source"] == "source_payload"


def test_search_text_includes_corrected_geography():
    row = {
        "source": "BNN", "category": "PUBLIC_SAFETY", "subtype": "INCIDENT", "status": "ACTIVE",
        "title": "BNN Incident", "message": "NJ | Hudson | Jersey City | FIRE",
        "location": {"label": "123 Newark Ave"}, "tags": ["bnn"],
    }
    value = search_text(row, {"state": "NJ", "county": "Hudson", "municipality": "Jersey City"})
    assert "Hudson County" in value
    assert "Jersey City" in value
