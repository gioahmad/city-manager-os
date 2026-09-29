import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DB_PASSWORD', 'test')
import brain_app as brain
import private_auth as auth


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('CMOS_AUTH_ENABLED', 'true')
    monkeypatch.setenv('CMOS_SESSION_SECRET', 'test-session-secret-at-least-32-characters')
    monkeypatch.setenv('CMOS_EXECUTIVE_USERNAME', 'Gio')
    monkeypatch.setenv('CMOS_EXECUTIVE_PASSWORD_HASH', 'not-used-for-issued-session')
    monkeypatch.setenv('CMOS_READONLY_USERNAME', 'Reader')
    monkeypatch.setenv('CMOS_READONLY_PASSWORD_HASH', 'not-used-for-issued-session')
    app = FastAPI()
    app.router.routes.extend(r for r in brain.app.routes if getattr(r, 'path', '').startswith('/brain'))
    auth.configure_private_auth(app)
    with TestClient(app) as client:
        yield client


def login(client, role='EXECUTIVE', username='Gio'):
    client.cookies.set(auth.COOKIE_NAME, auth._issue_session(auth.Account(username, role, '')))
    return hmac.new(auth._session_secret(), client.cookies.get(auth.COOKIE_NAME).encode(), hashlib.sha256).hexdigest()


def signed(data, timestamp=None):
    raw = json.dumps(data).encode()
    timestamp = str(timestamp or int(time.time()))
    signature = hmac.new(b'signing-key-for-test', raw + timestamp.encode(), hashlib.sha256).hexdigest()
    return {'content': raw, 'headers': {'X-Timestamp': timestamp, 'X-Signature': signature, 'Content-Type': 'application/json'}}


def test_sms_auth_sender_and_retries(client, monkeypatch):
    settings = {'owner_username': 'gio', 'senders': ['+12015551234'], 'signing_key': 'signing-key-for-test'}
    monkeypatch.setattr(brain, 'query_one', lambda *_: settings)
    seen = set()
    saved = []
    def insert(owner, body, kind, tags, **kwargs):
        assert owner == 'gio'
        key = kwargs['source_id']
        if key in seen:
            return False
        seen.add(key)
        saved.append((body, kind, tags))
        return True
    monkeypatch.setattr(brain, '_insert', insert)
    event = {'id': 'event-1', 'event': 'sms:received', 'payload': {'sender': '+12015551234', 'message': 'Idea: delivery #NHCAC'}}
    assert client.post('/brain/webhooks/sms', json=event).status_code == 401
    assert client.post('/brain/webhooks/sms', **signed(event, int(time.time())-600)).status_code == 401
    assert client.post('/brain/webhooks/sms', **signed(event)).json() == {'ok': True, 'saved': True}
    assert client.post('/brain/webhooks/sms', **signed(event)).json() == {'ok': True, 'saved': False}
    assert saved == [('Idea: delivery #NHCAC', 'IDEA', ['nhcac'])]
    event['payload']['sender'] = '+12015559999'
    assert client.post('/brain/webhooks/sms', **signed(event)).status_code == 403
    event['event'] = 'sms:delivered'
    assert client.post('/brain/webhooks/sms', **signed(event)).json()['saved'] is False
    assert client.post('/brain/webhooks/sms', **signed([])).status_code == 400
    assert not brain._verify_sms(b'{}', '9'*10000, '', 'key')


def test_private_routes_readonly_and_csrf(client, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('Unauthorized request reached the database')
    monkeypatch.setattr(brain, 'db_conn', unexpected)
    assert client.get('/brain/export', follow_redirects=False).status_code == 303
    assert client.post('/brain/save', data={'body':'private', 'csrf':'bad'}).status_code == 401
    login(client, 'READ_ONLY', 'Reader')
    assert client.post('/brain/save', data={'body':'private','csrf':'bad'}).status_code == 403
    login(client)
    assert client.post('/brain/save', data={'body':'private','csrf':'bad'}).status_code == 403


def test_page_escaping_owner_search_and_export(client, monkeypatch):
    csrf = login(client)
    queries = []
    def rows(sql, params):
        queries.append((sql, params))
        return [{'id':uuid4(), 'body':'<script>alert(1)</script> https://example.com', 'kind':'NOTE',
                 'tags':['nhcac'], 'source':'WEB', 'pinned':False, 'attachments':[],
                 'created_at':datetime.now(timezone.utc)}]
    monkeypatch.setattr(brain, 'query_all', rows)
    monkeypatch.setattr(brain, 'query_one', lambda *_: {})
    response = client.get('/brain?q=%25&view=pinned')
    assert response.status_code == 200
    assert '<script>alert(1)</script>' not in response.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
    assert csrf in response.text
    assert 'href="https://example.com"' in response.text
    assert queries[0][1][0] == 'gio'
    assert queries[0][1][7] == '%\\%%'
    export = client.get('/brain/export')
    assert export.status_code == 200
    assert queries[-1][1] == ('gio',)
    assert export.headers['content-disposition'].endswith('"brain-notes.json"')


def test_write_and_file_ownership_and_transaction(client, monkeypatch):
    csrf = login(client)
    calls = []
    note_id = uuid4()
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params):
            calls.append((sql,params))
            return SimpleNamespace(fetchone=lambda: {'id':note_id})
    monkeypatch.setattr(brain, 'db_conn', Conn)
    response = client.post('/brain/save', data={'body':'Call Mike #work','csrf':csrf},
                           files={'attachment':('../../photo.jpg', b'photo', 'image/jpeg')}, follow_redirects=False)
    assert response.status_code == 303
    assert calls[0][1][0] == 'gio'
    assert calls[1][1] == (note_id,'photo.jpg',b'photo')
    response = client.post(f'/brain/{note_id}/update', data={'body':'Updated','csrf':csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert calls[-1][1][-1] == 'gio' and 'owner_username=%s' in calls[-1][0]
    response = client.post(f'/brain/{note_id}/action', data={'action':'trash','csrf':csrf}, follow_redirects=False)
    assert response.status_code == 303 and calls[-1][1][-1] == 'gio'
    def no_file(sql, params):
        assert params[-1] == 'gio' and 'n.owner_username=%s' in sql
        return {}
    monkeypatch.setattr(brain, 'query_one', no_file)
    assert client.get(f'/brain/files/{uuid4()}').status_code == 404
    assert client.post('/brain/save', data={'body':'Too large','csrf':csrf},
                       files={'attachment':('large.bin', b'x'*(brain.MAX_ATTACHMENT+1))}).status_code == 413


def test_input_limits_and_duplicate_insert(monkeypatch):
    assert brain._values('https://example.com #Work')[1:] == ('LINK',['work'])
    assert brain._phone('(201) 555-1234') == '+12015551234'
    for text in ('', 'x'*20001):
        with pytest.raises(HTTPException): brain._values(text)
    with pytest.raises(HTTPException): brain._values('note','INVALID')
    calls=[]
    class Conn:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql,params):
            calls.append(sql)
            return SimpleNamespace(fetchone=lambda: None)
    monkeypatch.setattr(brain,'db_conn',Conn)
    assert not brain._insert('gio','note','NOTE',[],source='SMS',source_id='repeat',attachment=('a',b'a'))
    assert len(calls)==1 and 'ON CONFLICT(source,source_id) DO NOTHING' in calls[0]


def test_connect_reuses_gateway_without_duplicate_registration(client,monkeypatch):
    import operations_app
    import integration_runtime
    csrf=login(client)
    monkeypatch.setenv('CMOS_PUBLIC_ORIGIN','https://city.example')
    monkeypatch.setattr(brain,'query_one',lambda *_:{'owner_username':'gio'})
    monkeypatch.setattr(operations_app,'_smsgate_settings',lambda:{
        'url':'https://api.sms-gate.app/3rdparty/v1/messages','username':'saved-user','password':'saved-password'})
    calls=[]
    existing=[]
    def request(**kwargs):
        calls.append(kwargs)
        if kwargs['method']=='POST': existing.append(json.loads(kwargs['body']))
        return SimpleNamespace(ok=True,body_text=json.dumps(existing))
    monkeypatch.setattr(integration_runtime,'perform_http_request',request)
    assert client.post('/brain/sms/connect',data={'csrf':csrf},follow_redirects=False).status_code==303
    assert [c['method'] for c in calls]==['GET','POST']
    assert existing==[{'url':'https://city.example/brain/webhooks/sms','event':'sms:received'}]
    assert calls[-1]['url']=='https://api.sms-gate.app/3rdparty/v1/webhooks'
    assert calls[-1]['headers']['Authorization'].startswith('Basic ')
    assert client.post('/brain/sms/connect',data={'csrf':csrf},follow_redirects=False).status_code==303
    assert [c['method'] for c in calls]==['GET','POST','GET']
    monkeypatch.setenv('CMOS_PUBLIC_ORIGIN','http://unsafe.example')
    assert client.post('/brain/sms/connect',data={'csrf':csrf}).status_code==400
    assert len(calls)==3
