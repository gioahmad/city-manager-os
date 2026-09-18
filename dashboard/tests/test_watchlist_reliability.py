import ast
from pathlib import Path


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = DASHBOARD_ROOT.parent


def _watch_item_write_calls():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "execute":
            continue
        sql_node, params_node = node.args[:2]
        if not isinstance(sql_node, ast.Constant) or not isinstance(sql_node.value, str):
            continue
        sql = sql_node.value
        if "watch_items" not in sql or not (
            "INSERT INTO watch_items" in sql or "UPDATE watch_items SET" in sql
        ):
            continue
        assert isinstance(params_node, ast.Tuple)
        yield sql, params_node


def test_watch_writes_have_typed_geometry_and_matching_parameters():
    calls = list(_watch_item_write_calls())
    assert len(calls) == 2
    for sql, params in calls:
        assert sql.count("%s") == len(params.elts)
        assert "%s IS NULL" not in sql
        assert "%s::double precision IS NULL" in sql
        assert "ST_MakePoint(%s::double precision,%s::double precision)" in sql


def test_watchlist_has_simple_modes_health_and_friendly_errors():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/watchlist.html").read_text()
    assert 'SETUP_MODES = {"NEARBY", "KEYWORD"}' in source
    assert 'setup_mode: str = Form("NEARBY")' in source
    assert '@app.get("/api/watchlist/health")' in source
    assert "@_friendly_watch_errors" in source
    assert "full street address with municipality and state" in source
    assert 'value="NEARBY"' in template
    assert 'value="KEYWORD"' in template
    assert "Watchlist Health" in template
    assert "Examples and setup tips" in template
    assert "Send alerts to" in template


def test_private_watch_runner_splits_full_address_for_local_resolver():
    runner = (
        REPOSITORY_ROOT / "deploy/ops/configure-isolated-radius-watch.sh"
    ).read_text()
    assert "address_parts =" in runner
    assert "'address': address_line" in runner
    assert "'municipality': municipality_hint" in runner
    assert "resolved.get('postal_code')" in runner

