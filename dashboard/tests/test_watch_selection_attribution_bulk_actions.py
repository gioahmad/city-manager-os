import ast
import uuid
from pathlib import Path

from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]


def _load_bulk_selection():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_selected_bulk_feature_ids"
    )
    namespace = {"HTTPException": HTTPException, "uuid": uuid}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "bulk_selection", "exec"), namespace)
    return namespace["_selected_bulk_feature_ids"]


def test_county_group_allows_individual_town_selection():
    select = _load_bulk_selection()
    first, second = uuid.uuid4(), uuid.uuid4()
    context = {
        "features_by_id": {str(first): {}, str(second): {}},
        "groups": [{"token": "bergen", "feature_ids": [str(first), str(second)]}],
    }

    assert select(context, group_tokens=[], feature_ids=[second]) == [str(second)]
    assert select(context, group_tokens=["bergen"], feature_ids=[]) == [str(first), str(second)]

    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/watchlist.html").read_text()
    assert '"features": []' in source
    assert 'name="feature_ids"' in template
    assert "Open a county to select only the towns you want" in template
    assert "data-bulk-group" in template
    assert "data-bulk-feature" in template


def test_saved_keywords_and_watch_bulk_actions_use_existing_watch_rows():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/watchlist.html").read_text()

    assert 'row["keyword_choices"]' in source
    assert "Keywords currently on" in template
    assert "not a hardcoded list" in template
    assert template.count("data-primary-keyword") >= 2
    assert template.count("data-other-keywords") >= 2
    assert '@app.post("/watchlist/bulk-action")' in source
    assert 'action not in {"pause", "activate", "delete"}' in source
    assert "Type DELETE to permanently delete the selected Watches" in source
    assert 'name="watch_item_ids"' in template
    assert "Select all Watches shown" in template
    assert "CREATE TABLE" not in source


def test_alerts_explain_matches_and_guard_bulk_deletion():
    source = (DASHBOARD_ROOT / "operations_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/alerts.html").read_text()

    assert "awm.match_reason" in source
    assert 'coalesce(wm.watch_evidence,\'[]\'::jsonb) AS watch_evidence' in source
    assert "_humanize_match_reason(item.get(\"match_reason\"))" in source
    assert "Why you received this" in template
    assert "item.watch_name" in template
    assert "item.reason" in template
    assert '@app.post("/alerts/bulk-action")' in source
    assert 'action not in {"resolve", "delete"}' in source
    assert "Only an Executive user can permanently delete alerts" in source
    assert "Type DELETE to permanently delete the selected alerts" in source
    assert "DELETE FROM geo_entity_resolutions WHERE entity_type='ALERT'" in source
    assert "Permanent deletion also removes their Match and Notification evidence" in template
    assert "Mark selected resolved · keep history" in template


def test_changed_templates_compile():
    environment = Environment(loader=FileSystemLoader(DASHBOARD_ROOT / "templates"))
    environment.get_template("watchlist.html")
    environment.get_template("alerts.html")
