"""Execute the production provider query; do not only search source strings."""
import ast
import os
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def provider_sql():
    tree = ast.parse((ROOT / 'transit_app.py').read_text())
    return next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'TRANSIT_PROVIDER_SUMMARY_SQL'
                        for target in node.targets))


@pytest.fixture
def database():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.executescript('''
        CREATE TABLE transit_providers(id INTEGER PRIMARY KEY,name TEXT,phase INTEGER);
        CREATE TABLE transit_assets(id INTEGER PRIMARY KEY,provider_id INTEGER,active BOOLEAN);
        CREATE TABLE transit_observations(id INTEGER PRIMARY KEY,provider_id INTEGER,active BOOLEAN,impact_level TEXT);
        INSERT INTO transit_providers VALUES(1,'Mixed',1),(2,'No assets',2),(3,'No observations',3),(4,'Empty',4);
        INSERT INTO transit_assets VALUES(1,1,true),(2,1,true),(3,1,false),(4,3,true);
        INSERT INTO transit_observations VALUES
            (1,1,true,'WATCH'),(2,1,true,'ALERT'),(3,1,true,'AWARENESS'),
            (4,1,false,'ALERT'),(5,2,true,'ALERT');
    ''')
    yield connection
    connection.close()


def test_provider_counts_preserve_original_semantics(database):
    original = '''
        SELECT p.*,
               count(DISTINCT a.id) FILTER (WHERE a.active) AS asset_count,
               count(DISTINCT o.id) FILTER (WHERE o.active AND o.impact_level IN ('WATCH','ALERT')) AS active_impact_count
        FROM transit_providers p
        LEFT JOIN transit_assets a ON a.provider_id=p.id
        LEFT JOIN transit_observations o ON o.provider_id=p.id
        GROUP BY p.id ORDER BY p.phase,p.name
    '''
    actual = [dict(row) for row in database.execute(provider_sql())]
    assert actual == [dict(row) for row in database.execute(original)]
    assert [(row['asset_count'], row['active_impact_count']) for row in actual] == [(2,2),(0,1),(1,0),(0,0)]


def test_provider_query_does_not_multiply_large_child_tables(database):
    database.executemany('INSERT INTO transit_assets VALUES(?,?,true)', ((i,1) for i in range(10,5010)))
    database.executemany("INSERT INTO transit_observations VALUES(?,?,true,'WATCH')", ((i,1) for i in range(10,9010)))
    # A deterministic SQL instruction budget catches the old 45-million-row join
    # without waiting for a wall-clock timeout on slower test machines.
    calls = 0
    def limit_work():
        nonlocal calls
        calls += 1
        return int(calls > 2000)
    database.set_progress_handler(limit_work,1000)
    try:
        rows = list(database.execute(provider_sql()))
    finally:
        database.set_progress_handler(None,0)
    assert rows[0]['asset_count'] == 5002
    assert rows[0]['active_impact_count'] == 9002


@pytest.fixture
def transit(monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv('DB_PASSWORD','test')
    import transit_app
    return transit_app


def test_transit_page_uses_the_tested_summary_query(transit,monkeypatch):
    calls = []
    def query(sql,params=()):
        calls.append(sql)
        return []
    monkeypatch.setattr(transit,'query_all',query)
    monkeypatch.setattr(transit,'query_one',lambda *args: {'n':0})
    monkeypatch.setattr(transit,'templates',SimpleNamespace(TemplateResponse=lambda **kwargs: kwargs['context']))
    page = transit.transit_center(SimpleNamespace())
    assert provider_sql() in calls
    assert page['providers'] == []
    assert page['observations'] == []


def test_create_action_redirects_to_exact_work(transit,monkeypatch):
    work_id = uuid.uuid4()
    writes = []
    monkeypatch.setattr(transit,'execute',lambda sql,params: writes.append((sql,params)))
    monkeypatch.setattr(transit,'query_one',lambda *args: {'id':work_id})
    response = transit.transit_create_action(uuid.uuid4())
    assert response.status_code == 303
    assert len(writes) == 1
    location = urlsplit(response.headers['location'])
    assert location.path == '/issues'
    assert parse_qs(location.query)['focus'] == [str(work_id)]
    assert parse_qs(location.query)['state'] == ['all']


def test_create_action_reports_missing_work(transit,monkeypatch):
    monkeypatch.setattr(transit,'execute',lambda *args: None)
    monkeypatch.setattr(transit,'query_one',lambda *args: None)
    with pytest.raises(transit.HTTPException) as error:
        transit.transit_create_action(uuid.uuid4())
    assert error.value.status_code == 404
