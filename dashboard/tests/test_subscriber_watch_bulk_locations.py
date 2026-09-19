import ast
import re
from pathlib import Path


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]


def _load_pure(names: set[str]):
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    tree = ast.parse(source)
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id in names for target in targets):
                selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            selected.append(node)
    namespace = {"re": re}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "spatial_watch_helpers", "exec"), namespace)
    return namespace


def test_alert_keyword_choices_are_derived_from_each_alert():
    namespace = _load_pure({"ALERT_KEYWORD_STOPWORDS", "_alert_keyword_choices"})
    choices = namespace["_alert_keyword_choices"](
        {
            "title": "BNN - Example location",
            "message": "Working fire reported near a school with road closure",
            "subtype": "INCIDENT",
            "category": "PUBLIC_SAFETY",
            "tags": ["dispatch"],
        }
    )
    normalized = {value.casefold() for value in choices}
    assert "working fire" in normalized
    assert "road closure" in normalized
    assert "incident" not in normalized
    assert len(choices) <= 12


def test_layer_field_inference_uses_imported_metadata():
    namespace = _load_pure({"_normalized_property_key", "_infer_property_key"})
    infer = namespace["_infer_property_key"]
    keys = ["STATE_NAME", "COUNTY_NAM", "MUN_NAME"]
    assert infer(keys, ("state_name",)) == "STATE_NAME"
    assert infer(keys, ("county_nam", "county")) == "COUNTY_NAM"
    assert infer(keys, ("mun_name",), {"STATE_NAME"}) == "MUN_NAME"


def test_recipient_page_assigns_existing_watches_without_new_routing_model():
    source = (DASHBOARD_ROOT / "operations_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/subscribers.html").read_text()
    assert '@app.post("/subscribers/{subscriber_uuid}/watches")' in source
    assert "WHERE subscriber_id=%s AND watch_item_id=ANY(%s::uuid[])" in source
    assert "ON CONFLICT(watch_item_id,subscriber_id) DO UPDATE SET active=true" in source
    assert 'name="watch_item_ids"' in template
    assert 'name="visible_watch_item_ids"' in template
    assert "Save Watch Choices" in template
    assert "Choose Existing Watches" in template
    assert 'row["manage_watches"]' in source
    assert "CREATE TABLE" not in source


def test_bulk_builder_reuses_mapping_center_and_existing_watch_tables():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    watch_template = (DASHBOARD_ROOT / "templates/watchlist.html").read_text()
    map_template = (DASHBOARD_ROOT / "templates/map.html").read_text()
    assert '@app.post("/watchlist/bulk-create")' in source
    assert "FROM map_features f" in source
    assert "BULK_WATCH_LIMIT = 250" in source
    assert '"CORRIDOR"' in source
    assert 'radius_ft: float = Form(50.0)' in source
    assert "Create several Watches from a map layer" in watch_template
    assert "Inside boundary" in watch_template
    assert "50 feet · road or corridor" in watch_template
    assert "Build Watches" in map_template
    assert "CREATE TABLE" not in source


def test_alert_watch_picker_saves_any_selected_phrase_as_existing_aliases():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/watchlist.html").read_text()
    assert '"keyword_choices": keyword_choices' in source
    assert "data-alert-keyword" in template
    assert "These choices come from this alert, not a fixed list" in template
    assert 'name="aliases"' in template
    assert "Keywords:" in template


def test_streamed_release_keeps_docker_from_consuming_the_script():
    runner = (DASHBOARD_ROOT.parent / "deploy/releases/subscriber-watch-spatial-layers.sh").read_text()
    assert "tests/test_spatial_watch_pack.py tests/test_watchlist_reliability.py </dev/null" in runner
