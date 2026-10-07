from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MATCHER = ROOT / "static" / "watch_matcher.js"


def _evaluate(alert: dict, watch: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = """
const matcher = require(process.argv[1]);
const alert = JSON.parse(process.argv[2]);
const watch = JSON.parse(process.argv[3]);
console.log(JSON.stringify(matcher.evaluateWatch(alert, watch, {now: '2026-10-06T12:00:00Z'})));
"""
    result = subprocess.run(
        [node, "-e", script, str(MATCHER), json.dumps(alert), json.dumps(watch)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _alert(message: str) -> dict:
    return {
        "alert_id": "BNN:GEO-TEST",
        "source": "BNN",
        "category": "PUBLIC_SAFETY",
        "subtype": "INCIDENT",
        "status": "ACTIVE",
        "title": "BNN Incident",
        "message": message,
        "priority": 4,
        "county": "",
        "municipality": "",
        "location": {},
        "tags": ["bnn"],
    }


def _watch(kind: str, *, municipality: str = "", county: str = "", term: str = "") -> dict:
    return {
        "active": True,
        "watch_type": kind,
        "display_name": "BNN geography test",
        "search_term": term,
        "aliases": [],
        "match_mode": "FIELD",
        "match_field": "municipality" if kind == "TOWN" else "county",
        "min_priority": 1,
        "municipality": municipality,
        "county": county,
        "source_filter": ["BNN"],
        "alert_category_filter": [],
        "nearby_enabled": False,
        "recipients": [{"subscriber_id": "TEST"}],
    }


def test_bnn_town_watch_matches_exact_pipe_segment_when_structured_city_is_blank():
    alert = _alert("NJ | Hudson | Jersey City | FIRE | 123 Newark Ave | BNN123")
    result = _evaluate(alert, _watch("TOWN", municipality="Jersey City"))
    assert result["matched"] is True
    assert result["match_field"] == "municipality"


def test_bnn_town_watch_does_not_confuse_another_pipe_segment():
    alert = _alert("NJ | Hudson | Jersey City | FIRE | 123 Newark Ave | BNN123")
    result = _evaluate(alert, _watch("TOWN", municipality="Union City"))
    assert result["matched"] is False


def test_bnn_county_watch_accepts_hudson_or_hudson_county():
    alert = _alert("NJ | Hudson | Jersey City | FIRE | 123 Newark Ave | BNN123")
    for county in ("Hudson", "Hudson County"):
        result = _evaluate(alert, _watch("COUNTY", county=county))
        assert result["matched"] is True
        assert result["match_field"] == "county"


def test_bnn_field_watch_uses_exact_pipe_geography_fallback():
    alert = _alert("NJ | Hudson | Jersey City | FIRE | 123 Newark Ave | BNN123")
    watch = _watch("PHRASE", term="Hudson County")
    watch["match_field"] = "county"
    result = _evaluate(alert, watch)
    assert result["matched"] is True


def test_bnn_county_plus_topic_watch_requires_both_conditions():
    alert = _alert("NJ | Hudson | Jersey City | WORKING FIRE | 123 Newark Ave | BNN123")
    watch = _watch("LOCATION_TOPIC", county="Hudson", term="WORKING FIRE")
    watch["match_field"] = "search_text"
    result = _evaluate(alert, watch)
    assert result["matched"] is True
    wrong_county = _watch("LOCATION_TOPIC", county="Bergen", term="WORKING FIRE")
    wrong_county["match_field"] = "search_text"
    assert _evaluate(alert, wrong_county)["matched"] is False
