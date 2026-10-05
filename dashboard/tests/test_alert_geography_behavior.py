"""Exercise real filter functions without starting workers or a database."""
import ast
import re
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def alert_filter():
    tree = ast.parse((ROOT/'operations_app.py').read_text())
    names = {'_alert_place_values', '_alert_filter'}
    nodes = [n for n in tree.body if
             isinstance(n, ast.FunctionDef) and n.name in names or
             isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'ALERT_WINDOWS' for t in n.targets)]
    namespace = {'re': re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT/'operations_app.py'), 'exec'), namespace)
    return namespace['_alert_filter']

@pytest.mark.parametrize('municipality,expected', [
    ('WEEHAWKEN', ['weehawken']),
    (' Weehawken | UNION   CITY ', ['weehawken', 'union city']),
    ('North\tBergen|Guttenberg', ['north bergen', 'guttenberg']),
])
def test_multiple_municipalities_are_case_and_space_insensitive(alert_filter, municipality, expected):
    sql, params, filters = alert_filter(municipality=municipality, county='Hudson', window='all')
    assert '= ANY(%s)' in sql
    assert expected in params
    assert 'Hudson' in params
    assert 'a.county' in sql
    assert filters['county'] == 'Hudson'
    assert filters['municipality'] == municipality.strip()

def test_place_text_stays_in_parameters_not_sql(alert_filter):
    value = "Town'); DELETE FROM alerts; --"
    sql, params, _ = alert_filter(municipality=value, window='all')
    assert 'DELETE FROM' not in sql
    assert [value.casefold()] in params

def test_empty_geography_does_not_filter_out_records(alert_filter):
    sql, params, filters = alert_filter(municipality=' | ', county='', window='all')
    assert 'ANY' not in sql
    assert params == []
    assert filters['state'] == 'all'
