from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_saved_watches_expose_match_notification_and_historical_evidence():
    source = (ROOT / "spatial_watch_app.py").read_text()
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert "def _watch_evidence" in source
    assert "FROM alert_watch_matches awm" in source
    assert "FROM deliveries d" in source
    assert "matched_watch_ids ? %s" in source
    assert "def _saved_watch_preview_url" in source
    assert "Preview History" in template
    assert "Matched Alerts" in template
    assert "Notifications" in template
    assert "Search Evidence" in template
    assert "creates no Match or Notification" in template


def test_county_is_treated_as_a_saved_location_watch():
    source = (ROOT / "spatial_watch_app.py").read_text()
    assert 'row.get("watch_type") in {"TOWN", "COUNTY"}' in source
