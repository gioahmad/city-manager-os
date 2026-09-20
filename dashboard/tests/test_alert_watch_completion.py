from pathlib import Path

from jinja2 import Environment, FileSystemLoader


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (DASHBOARD_ROOT / name).read_text()


def test_watchlist_uses_set_based_evidence_queries_for_speed():
    source = _read("spatial_watch_app.py")
    assert "WITH recipient_rollup AS" in source
    assert "match_rollup AS" in source
    assert "delivery_rollup AS" in source
    assert "LEFT JOIN recipient_rollup" in source
    assert "LEFT JOIN match_rollup" in source
    assert "LEFT JOIN delivery_rollup" in source
    watch_query = source.split("WITH recipient_rollup AS", 1)[1].split("for row in all_items", 1)[0]
    assert "WHERE wir.watch_item_id=w.id AND wir.active" not in watch_query
    assert "SELECT count(*) FROM alert_watch_matches awm" not in watch_query


def test_county_layer_links_directly_to_filtered_individual_towns():
    source = _read("spatial_watch_app.py")
    template = _read("templates/watchlist.html")
    assert '"NJ_OFFICIAL_MUNICIPALITIES"' in source
    assert '"NJ_OFFICIAL_COUNTIES"' in source
    assert "bulk_group_filter" in source
    assert 'group["town_picker_url"]' in source
    assert "Choose individual towns in {{ group.label }} County" in template
    assert "Show every county" in template
    assert 'name="feature_ids"' in template


def test_alert_card_chooses_mode_and_live_keywords_before_watch_setup():
    watch_source = _read("spatial_watch_app.py")
    alert_source = _read("operations_app.py")
    alert_template = _read("templates/alerts.html")
    watch_template = _read("templates/watchlist.html")
    assert "def alert_keyword_choices" in alert_source
    assert "fixed incident dictionary" in alert_source
    assert "alert_keyword_choices(alert)" in alert_source
    assert "selected_keywords: list[str] = Query(default=[])" in watch_source
    assert "Create Watch From Alert" in alert_template
    assert "This Location plus selected topics" in alert_template
    assert "Selected topics anywhere" in alert_template
    assert "Anything near this Location" in alert_template
    assert "params.append('selected_keywords', choice.value)" in alert_template
    assert "keyword in prefill.selected_keywords" in watch_template
    assert '"alert_watch_prefill_query"' in watch_source
    assert '"county_to_town_selection": True' in watch_source


def test_visible_area_search_covers_alerts_events_work_and_watches():
    source = _read("map_app.py")
    template = _read("templates/map.html")
    for function in (
        "map_watchlist_geojson(bbox: str | None = None, q: str = \"\")",
        "map_issues_geojson(bbox: str | None = None, q: str = \"\")",
        "map_system_events(bbox: str | None = None, q: str = \"\")",
    ):
        assert function in source
    assert source.count("ST_MakeEnvelope(%s,%s,%s,%s,4326)") >= 6
    for key in ("alerts", "event-intelligence", "operations", "watchlist"):
        assert f'data-area-record="{key}"' in template
    assert "function searchVisibleRecords" in template
    assert "Search follows the current map view" in template
    assert '"visible_area_search"' in _read("spatial_watch_app.py")


def test_everything_search_includes_safe_operational_catalogs():
    source = _read("operations_app.py")
    for scope in ("people", "operations", "configuration", "conditions"):
        assert f'"{scope}"' in source
    for table in (
        "subscribers",
        "staff_employees",
        "operations_routines",
        "staff_locations",
        "rule_subsections",
        "map_layers",
        "flood_observations",
        "pseg_outage_state",
    ):
        assert f"FROM {table}" in source or f"JOIN {table}" in source
    assert "pin_hash" not in source


def test_filtered_bulk_alert_action_reuses_exact_search_contract_and_is_guarded():
    source = _read("operations_app.py")
    template = _read("templates/alerts.html")
    assert "def _alert_filter(" in source
    assert source.count("_alert_filter(") >= 3
    assert "ALERT_FILTERED_BULK_LIMIT = 5000" in source
    assert "Narrow the search before changing all matching alerts" in source
    assert '"DELETE ALL" if action == "delete" else "APPLY ALL"' in source
    assert 'name="selection_scope"' in template
    assert "All {{ result_total }} matching results" in template


def test_completion_templates_compile():
    environment = Environment(loader=FileSystemLoader(DASHBOARD_ROOT / "templates"))
    for template in ("watchlist.html", "alerts.html", "map.html", "search.html"):
        environment.get_template(template)
