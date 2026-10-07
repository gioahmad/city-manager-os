"""Literal term lists and digit placeholders agree in preview and the live matcher."""
import ast
import json
import re
import subprocess
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERMS = r"2nd alarm|3rd alarm|4th alarm|mass casualty|mci|active shooter|fatal|doa|critical condition|trapped|entrapment|hazmat|mayday|officer down|shots fired|burn center|extrication|tier \d|10-75|10 75|10-13|10 13|10-77|10 77|10-45|10 45|level mobilization|foot pursuit|armed robbery|barricaded edp|working fire|all hands|high rise fire|overturned auto"


def helpers():
    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code, self.detail = status_code, detail
    tree = ast.parse((ROOT / 'watch_preview_app.py').read_text())
    names = {'watch_terms', '_normalize', '_phrase_pattern', '_matches', '_prepared', '_pipe_fields', '_county_name', '_geography_key'}
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    scope = {'re': re, 'unicodedata': unicodedata, 'HTTPException': HTTPException,
             'STATE_ALIASES': {}, 'COUNTIES': {'NJ': set(), 'NY': set()}}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(ROOT / 'watch_preview_app.py'), 'exec'), scope)
    return scope


def test_term_list_and_live_preview_parity():
    scope = helpers()
    terms = scope['watch_terms'](TERMS + '\nWORKING FIRE\n""', '  Mayday  ')
    assert len(terms) == 34 and terms[-1] == 'overturned auto'
    assert scope['watch_terms']('fire\nroad closure, flood|hazmat', 'FIRE') == ['fire', 'road closure', 'flood', 'hazmat']
    try:
        scope['watch_terms']('|'.join('term ' + str(i) for i in range(201)))
        raise AssertionError('oversize list accepted')
    except scope['HTTPException'] as exc:
        assert exc.status_code == 400
    cases = []
    rule = {'source': 'BNN', 'category': '', 'min_priority': 1, 'mode': 'WORD',
            'field': 'search_text', 'term': terms[0], 'aliases': terms[1:]}
    for term in terms:
        text = term.replace(r'\d', '2')
        for message in (text.lower(), text.upper()):
            cases.append({'source': 'BNN', 'message': 'Units report ' + message + '.', 'priority': 1})
    cases += [{'source': 'OTHER', 'message': 'working fire', 'priority': 5},
              {'source': 'BNN', 'message': 'Routine inspection', 'priority': 5},
              {'source': 'BNN', 'message': 'tier x', 'priority': 1},
              {'source': 'BNN', 'message': 'mcity', 'priority': 1}]
    expected = [True] * (len(terms) * 2) + [False] * 4
    assert [scope['_matches'](alert, rule)[0] for alert in cases] == expected
    watch = {'active': True, 'watch_type': 'PHRASE', 'search_term': terms[0], 'aliases': terms[1:],
             'match_mode': 'WORD', 'source_filter': ['BNN'], 'min_priority': 1}
    script = "const d=JSON.parse(require('fs').readFileSync(0,'utf8'));const m=require(process.argv[1]);process.stdout.write(JSON.stringify(d.cases.map(a=>m.evaluateWatch(a,d.watch).matched)));"
    result = subprocess.run(['node', '-e', script, str(ROOT / 'static/watch_matcher.js')],
                            input=json.dumps({'cases': cases, 'watch': watch}), text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == expected
    wildcard = {**rule, 'term': r'tier \d', 'aliases': [], 'mode': 'CONTAINS'}
    assert scope['_matches']({'source': 'BNN', 'priority': 1, 'message': 'TIER 12'}, wildcard)[0]
    assert not scope['_matches']({'source': 'BNN', 'priority': 1, 'message': 'TIER X'}, wildcard)[0]


if __name__ == '__main__':
    test_term_list_and_live_preview_parity()
    print('WATCH TERMS: all 34 terms, both cases, source gate, word boundaries and numeric wildcard PASS')
