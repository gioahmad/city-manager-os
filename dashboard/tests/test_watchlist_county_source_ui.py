from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_normal_watch_location_search_includes_counties():
    source = (ROOT / "spatial_watch_app.py").read_text()
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert "SELECT 'COUNTY' AS kind" in source
    assert 'county_needle = re.sub(r"\\s+County$"' in source
    assert "(county_like,)" in source
    assert "'Any alert labeled for this county' AS detail" in source
    assert "Counties such as Hudson County are supported too." in template
    assert "['MUNICIPALITY', 'COUNTY'].includes(item.kind)" in template


def test_source_filter_is_visible_in_normal_create_and_edit_flow():
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert "<label>Source" in template
    assert "name=\"source_filter\"" in template
    assert "Example: BNN. Leave blank for every source." in template
    assert template.count('name="source_filter"') >= 2
