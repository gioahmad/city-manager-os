from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_normal_watch_location_search_includes_counties():
    source = (ROOT / "spatial_watch_app.py").read_text()
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert "SELECT 'COUNTY' AS kind" in source
    assert "'Any alert labeled for this county' AS detail" in source
    assert "Address, county, city/town" in template
    assert "['MUNICIPALITY', 'COUNTY'].includes(item.kind)" in template


def test_source_filter_is_visible_in_normal_create_and_edit_flow():
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert template.count("Only from this alert source (optional)") >= 2
    assert "For “any BNN alert in Hudson County,” choose BNN here and Hudson County under Location." in template
