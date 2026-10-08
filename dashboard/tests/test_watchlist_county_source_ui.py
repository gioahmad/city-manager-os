from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_normal_watch_location_search_includes_counties():
    source = (ROOT / "spatial_watch_app.py").read_text()
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert '{"kind": "COUNTY", "source_id": name.title()' in source
    assert 'county_needle = re.sub(r"\\s+County$"' in source
    assert 'for name in sorted(COUNTIES["NJ"])' in source
    assert '"detail": "Alerts labeled for this county"' in source
    assert 'data-county-choice' in template and 'county_choices' in template
    assert "distanceField.hidden = ['ANYWHERE', 'COUNTY', 'MUNICIPALITY'].includes(type)" in template


def test_source_filter_is_visible_in_normal_create_and_edit_flow():
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert "{% macro source_picker(" in template
    assert "name=\"source_filter\"" in template
    assert 'data-source-choice' in template and 'data-source-all' in template
    assert template.count('{{ source_picker(') == 2
