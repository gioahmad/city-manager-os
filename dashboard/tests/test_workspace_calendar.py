import base64
import hashlib
from datetime import datetime,timezone
from types import SimpleNamespace
from urllib.parse import parse_qs,urlparse

from cryptography.fernet import Fernet
import httpx
import pytest
from fastapi import HTTPException
import workspace_calendar as calendar
from private_auth import COOKIE_NAME

@pytest.fixture
def configured(monkeypatch):
    for name,value in {'CMOS_PUBLIC_ORIGIN':'https://workspace.example.com','CMOS_MICROSOFT_CLIENT_ID':'test-client',
                       'CMOS_MICROSOFT_CLIENT_SECRET':'test-secret','CMOS_CALENDAR_KEY':Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(name,value)
    return calendar.settings()


def test_connection_requires_valid_secrets_and_https(monkeypatch,configured):
    assert calendar.settings()['ready']
    monkeypatch.setenv('CMOS_PUBLIC_ORIGIN','http://workspace.example.com')
    assert not calendar.settings()['ready']
    monkeypatch.setenv('CMOS_PUBLIC_ORIGIN','https://workspace.example.com')
    monkeypatch.setenv('CMOS_CALENDAR_KEY','bad-key')
    assert not calendar.settings()['ready']


def test_oauth_state_is_session_owned_pkce_and_encrypted(monkeypatch,configured):
    saved=[]
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,sql,params):saved.append((sql,params))
    monkeypatch.setattr(calendar,'db_conn',Connection)
    request=SimpleNamespace(cookies={COOKIE_NAME:'my-session'})
    url=calendar.begin('gio',request)['redirect_url'];params=parse_qs(urlparse(url).query)
    row=saved[1][1]
    assert row[0]==hashlib.sha256(params['state'][0].encode()).hexdigest()
    assert row[1:3]==('gio',hashlib.sha256(b'my-session').hexdigest())
    verifier=calendar.cipher().decrypt(row[3].encode()).decode()
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    assert params['code_challenge']==[challenge] and params['code_challenge_method']==['S256']
    assert 'Calendars.ReadBasic' in params['scope'][0] and 'Mail.' not in params['scope'][0]
    assert configured['secret'] not in url and verifier not in url


def test_callback_rejects_expired_wrong_session_or_replayed_state(monkeypatch,configured):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    app=FastAPI();app.router.routes.extend(r for r in calendar.app.routes if getattr(r,'path','').endswith('/microsoft/callback'))
    monkeypatch.setattr(calendar,'_owner',lambda *a,**kw:'gio')
    class Result:
        def fetchone(self):return None
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,sql,params):
            assert 'DELETE' in sql and 'expires_at>now()' in sql and 'session_hash=%s' in sql
            assert params[1]=='gio'
            return Result()
    monkeypatch.setattr(calendar,'db_conn',Connection)
    monkeypatch.setattr(calendar,'token_request',lambda *a:pytest.fail('Invalid callback reached Microsoft'))
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME,'wrong-session')
        assert client.get('/workspace/calendar/microsoft/callback?state='+('A'*43)+'&code=test').status_code==400


def test_graph_pagination_never_sends_token_to_another_host():
    client=SimpleNamespace(get=lambda *a,**kw:pytest.fail('Token would leave Microsoft Graph'))
    with pytest.raises(ValueError):calendar.graph_pages(client,'https://evil.example/v1.0/events','secret')


def test_calendar_pages_and_utc_rows_are_bounded_and_links_checked():
    calls=[]
    def get(url,headers):
        calls.append(url);assert headers['Prefer']=='outlook.timezone="UTC"'
        request=httpx.Request('GET',url)
        payload={'value':[{'id':'one'}],'@odata.nextLink':'https://graph.microsoft.com/v1.0/page2'} if len(calls)==1 else {'value':[{'id':'two'}]}
        return httpx.Response(200,json=payload,request=request)
    assert calendar.graph_pages(SimpleNamespace(get=get),calendar.GRAPH+'me/calendarView','access')==[{'id':'one'},{'id':'two'}]
    row={'id':'event','subject':'Meeting','start':{'dateTime':'2026-10-01T14:00:00.0000000','timeZone':'UTC'},
         'end':{'dateTime':'2026-10-01T15:00:00','timeZone':'UTC'},'webLink':'javascript:bad'}
    saved=calendar.event_row('gio',row)
    assert saved[0]=='gio' and saved[3]==datetime(2026,10,1,14,tzinfo=timezone.utc) and saved[-1] is None
    assert calendar.event_row('gio',{**row,'isCancelled':True}) is None


def test_sync_failure_retains_snapshot_and_marks_owner_only(monkeypatch,configured):
    import json
    statements=[]
    encrypted=calendar.cipher().encrypt(json.dumps({'owner':'gio','refresh':'old-token'}).encode()).decode()
    class Result:
        def fetchone(self):return {'tokens':encrypted}
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,sql,params):statements.append((sql,params));return Result()
        def commit(self):statements.append(('COMMIT',()))
    monkeypatch.setattr(calendar,'db_conn',Connection)
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
    monkeypatch.setattr(calendar.httpx,'Client',Client)
    monkeypatch.setattr(calendar,'token_request',lambda *a:{'access_token':'access','refresh_token':'rotated-token'})
    monkeypatch.setattr(calendar,'graph_pages',lambda *a:(_ for _ in ()).throw(ValueError('unavailable')))
    with pytest.raises(HTTPException) as error:calendar.sync('gio')
    assert error.value.status_code==502 and 'old-token' not in error.value.detail
    assert not any('DELETE FROM workspace_calendar_events' in s for s,p in statements)
    assert statements[-1]==('COMMIT',())
    token_update=next(p[0] for s,p in statements if 'SET tokens=' in s)
    assert json.loads(calendar.cipher().decrypt(token_update.encode()))['refresh']=='rotated-token'
    assert any('sync_error=true' in s and p==('gio',) for s,p in statements)


def test_sync_success_replaces_only_the_owner_snapshot(monkeypatch,configured):
    import json
    statements=[]
    encrypted=calendar.cipher().encrypt(json.dumps({'owner':'gio','refresh':'old-token'}).encode()).decode()
    class Result:
        def fetchone(self):return {'tokens':encrypted}
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,sql,params):statements.append((sql,params));return Result()
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
    monkeypatch.setattr(calendar,'db_conn',Connection);monkeypatch.setattr(calendar.httpx,'Client',Client)
    monkeypatch.setattr(calendar,'token_request',lambda *a:{'access_token':'access','refresh_token':'rotated-token'})
    event={'id':'meeting','subject':'Appointment','start':{'dateTime':'2026-10-02T14:00:00','timeZone':'UTC'},
           'end':{'dateTime':'2026-10-02T15:00:00','timeZone':'UTC'}}
    def pages(client,url,access):
        assert access=='access' and '/me/calendarView?' in url and 'endDateTime=' in url
        return [event,{**event,'id':'cancelled','isCancelled':True}]
    monkeypatch.setattr(calendar,'graph_pages',pages)
    assert calendar.sync('gio')['message'].startswith('Outlook refreshed')
    deletes=[p for s,p in statements if s.startswith('DELETE FROM workspace_calendar_events')]
    assert deletes==[('gio',)]
    inserts=[p for s,p in statements if 'INSERT INTO workspace_calendar_events' in s]
    assert len(inserts)==1 and inserts[0][:3]==('gio','meeting','Appointment')
    assert 'sync_error=false' in statements[-1][0] and statements[-1][1]==('gio',)
