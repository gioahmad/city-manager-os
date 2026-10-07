from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_watch_builder_is_three_choice_operator_flow():
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert "3 choices" in template
    assert "What should we watch for?" in template
    assert "Where should we watch?" in template
    assert "Who should be notified?" in template
    assert "data-simple-watch-form" in template
    assert "Choose a source, enter a topic, or choose a location." in template
    assert "Technical watch type" not in template.split('<article class="panel watch-saved-panel">', 1)[0]
    assert "Match field" not in template.split('<article class="panel watch-saved-panel">', 1)[0]


def test_saved_watch_list_does_not_render_every_editor_by_default():
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert '{% if focus_id and w.id == focus_id %}' in template
    assert 'href="/watchlist?focus={{ w.id }}#edit-watch">Edit</a>' in template
    assert "Bulk actions" in template
    assert "watch-summary-strip" in template
    assert "watch-rule-summary" in template


def test_source_only_watch_has_exact_source_rule():
    source = (ROOT / "spatial_watch_app.py").read_text()
    assert 'source_only = setup_mode == "TOPIC" and not topic and len(saved_source_filter) == 1' in source
    assert 'saved_watch_type = "SOURCE" if source_only' in source
    assert 'saved_match_mode = "FIELD" if source_only' in source
    assert 'saved_match_field = "source" if source_only' in source


def test_location_picker_is_optional_in_simple_flow():
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert "Optional. Leave blank for anywhere" in template
    assert "(not selected_label and not latitude)" in template


def test_spot_watches_keep_contextual_distance_controls():
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert "How far around this location?" in template
    for label in ("50 feet", "100 feet", "500 feet", "1,000 feet", "Half mile", "1 mile", "2 miles", "5 miles"):
        assert label in template
    assert "Use feet for a building, intersection, block, or corridor" in template
    assert "kind in ['MUNICIPALITY','COUNTY']" in template


def test_watch_name_is_optional_and_generated_from_rule():
    source = (ROOT / "spatial_watch_app.py").read_text()
    template = (ROOT / "templates" / "watchlist.html").read_text()
    assert "def _automatic_watch_name" in source
    assert 'display_name: str = Form("")' in source
    assert 'Created automatically from the rule' in template
    assert 'if not display_name:' in source


def test_simple_edit_resets_location_rules_when_becoming_topic_only():
    source = (ROOT / "spatial_watch_app.py").read_text()
    assert "LOCATION_RULE_WATCH_TYPES" in source
    assert "def _topic_rule_from_existing" in source
    assert 'candidate = "PHRASE"' in source
    assert 'mode = "CONTAINS"' in source
    assert 'field = None' in source
    assert 'current_type != "LOCATION_TOPIC"' in source
