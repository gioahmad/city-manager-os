import hashlib
import hmac
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DB_PASSWORD','test')
import private_auth as auth
import workspace_app as ws
from workspace_engine import briefing_dates, derive_relationships, occurrence, suggestions, text_draft


def fact(a,kind,b):
    return {'id':str(uuid4()),'source_id':a,'relation':kind,'target_id':b}


def test_family_evidence_order_and_retraction():
    facts=[fact('mother','MOTHER_OF','child'),fact('aunt','SISTER_OF','mother')]
    a=derive_relationships(facts)
    assert a==derive_relationships(list(reversed(facts)))
    aunt=next(e for e in a if e['relation']=='AUNT_OF')
    assert (aunt['source_id'],aunt['target_id'])==('aunt','child')
    assert set(aunt['proof'])=={f['id'] for f in facts}
    assert derive_relationships(facts[1:])==[]
    assert derive_relationships([fact('aunt','AUNT_OF','child'),fact('mother','SISTER_OF','aunt')])==[]
    assert not any(e['relation']=='MOTHER_OF' for e in a)


def test_grandparents_cousins_and_no_self_connections():
    facts=[fact('g','PARENT_OF','m'),fact('m','PARENT_OF','c'),fact('a','BROTHER_OF','m'),fact('a','PARENT_OF','d')]
    derived=derive_relationships(facts)
    assert any(e['relation']=='GRANDPARENT_OF' and e['source_id']=='g' and e['target_id']=='c' for e in derived)
    assert any(e['relation']=='UNCLE_OF' and e['source_id']=='a' for e in derived)
    assert any(e['relation']=='COUSIN_OF' and e['source_id']=='c' and e['target_id']=='d' for e in derived)
    assert all(e['source_id']!=e['target_id'] for e in derived)


def test_suggestions_are_signals_not_facts_and_dismissal():
    people=[{'id':str(i),'name':name,'kind':'PERSON','attributes':attrs} for i,name,attrs in [
        (1,'Maria Rivera',{'address':'18 Example St.','unit':'1','phones':['+12015550101']}),
        (2,'Daniel Rivera',{'address':'18 Example Street','unit':'2'}),
        (3,'Lucia Santos',{'phones':['+12015550101']})]]
    rows=suggestions(people)
    assert len(rows)==2
    assert rows[0]['strength']=='Strong identity signal'
    assert any('family unknown' in reason for s in rows for reason in s['reasons'])
    assert all('relation' not in s for s in rows)
    assert len(suggestions(people,{r['fingerprint'] for r in rows}))==0
    assert suggestions([{'id':'x','name':'A','kind':'PERSON','attributes':{}},
                        {'id':'y','name':'B','kind':'PERSON','attributes':{}}])==[]


def test_anniversary_leap_day_lead_overdue_and_handled():
    row={'id':'a','name':'Maria Rivera','occasion':'ANNIVERSARY','label':'Anniversary',
         'event_date':date(2024,2,29),'annual':True,'lead_days':7}
    assert occurrence(row,date(2025,2,1))==date(2025,2,28)
    assert occurrence(row,date(2025,3,1))==date(2026,2,28)
    assert briefing_dates([row],date(2025,2,21),0)[0]['remind_on']==date(2025,2,21)
    assert not briefing_dates([{**row,'handled_occurrence':date(2025,2,28)}],date(2025,2,21),30)
    overdue={**row,'annual':False,'event_date':date(2025,1,1)}
    assert briefing_dates([overdue],date(2025,2,21),0)[0]['overdue']
    assert 'Happy anniversary' in text_draft(row)
    assert 'Happy' not in text_draft({**row,'occasion':'REMEMBRANCE'})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('CMOS_AUTH_ENABLED','true')
    monkeypatch.setenv('CMOS_SESSION_SECRET','workspace-test-secret-at-least-32-characters')
    for prefix,name in [('EXECUTIVE','Gio'),('READONLY','Reader')]:
        monkeypatch.setenv('CMOS_'+prefix+'_USERNAME',name)
        monkeypatch.setenv('CMOS_'+prefix+'_PASSWORD_HASH','unused-for-issued-session')
    app=FastAPI()
    app.router.routes.extend(r for r in ws.app.routes if getattr(r,'path','').startswith(('/workspace','/request-portal')))
    auth.configure_private_auth(app)
    with TestClient(app) as c: yield c


def login(client,role='EXECUTIVE',name='Gio'):
    cookie=auth._issue_session(auth.Account(name,role,''))
    client.cookies.set(auth.COOKIE_NAME,cookie)
    return hmac.new(auth._session_secret(),cookie.encode(),hashlib.sha256).hexdigest()


def test_auth_readonly_csrf_and_automation(client,monkeypatch):
    def unexpected(*a,**kw): pytest.fail('Unauthorized action reached storage')
    monkeypatch.setattr(ws,'db_conn',unexpected)
    assert client.get('/workspace',follow_redirects=False).status_code==303
    assert client.post('/workspace/api/action',json={'action':'HEALTH','csrf':'bad'}).status_code==401
    csrf=login(client,'READ_ONLY','Reader')
    assert client.post('/workspace/api/action',json={'action':'FAST_START','csrf':csrf}).status_code==403
    login(client)
    assert client.post('/workspace/api/action',json={'action':'FAST_START','csrf':'bad'}).status_code==403
    client.cookies.clear()
    monkeypatch.setenv('CMOS_AUTOMATION_TOKEN','automation-key')
    assert client.get('/workspace/api/state',headers={'x-cmos-automation-key':'automation-key'}).status_code==401


def test_display_does_not_query_or_return_private_tables(client,monkeypatch):
    login(client)
    queries=[]
    monkeypatch.setattr(ws,'query_one',lambda *_:{'settings':{}})
    def rows(sql,params=()):
        queries.append(sql)
        assert not any(t in sql for t in ['brain_notes','workspace_dates','workspace_health','workspace_entities','workspace_relationships','workspace_messages','workspace_personal_tasks'])
        return []
    monkeypatch.setattr(ws,'query_all',rows)
    response=client.get('/workspace/api/state?display=true')
    assert response.status_code==200
    assert set(response.json())=={'config','alerts','health','work','refreshed_at'}
    assert len(queries)==3


def test_private_data_scopes_and_hidden_endpoint_proofs(client,monkeypatch):
    login(client)
    a,b=str(uuid4()),str(uuid4())
    calls=[]
    monkeypatch.setattr(ws,'query_one',lambda sql,p=():{'settings':{}} if 'workspace_config' in sql else {})
    def rows(sql,params=()):
        calls.append((sql,params))
        if 'SELECT * FROM workspace_entities' in sql:
            return [{'id':a,'name':'Visible','kind':'PERSON','visibility':'WORK','owner_username':'gio','attributes':{}}]
        if 'FROM workspace_relationships' in sql:
            return [{**fact(a,'SISTER_OF',b),'visibility':'PRIVATE','owner_username':'gio'}]
        return []
    monkeypatch.setattr(ws,'query_all',rows)
    response=client.get('/workspace/api/state')
    assert response.status_code==200
    assert response.json()['facts']==[] and response.json()['derived']==[]
    for sql,params in calls:
        if any(t in sql for t in ['brain_notes','workspace_dates','workspace_health','workspace_fasts','workspace_personal_tasks','workspace_messages']):
            assert 'owner_username=%s' in sql and 'gio' in params


def test_public_portal_projection_expiry_and_csrf(client,monkeypatch):
    portal=str(uuid4());token='A'*43
    monkeypatch.setattr(ws,'query_one',lambda sql,p:{'id':portal,'title':'Road request','status':'OPEN','issue_id':str(uuid4())})
    monkeypatch.setattr(ws,'query_all',lambda *_:[{'author':'REQUESTER','body':'<script>bad</script>','created_at':datetime.now(timezone.utc)}])
    response=client.get('/request-portal/'+token)
    assert response.status_code==200
    assert '<script>bad</script>' not in response.text and '&lt;script&gt;' in response.text
    assert 'workspace/api' not in response.text
    assert client.post('/request-portal/'+token,data={'body':'hello','csrf':'bad'}).status_code==403
    monkeypatch.setattr(ws,'query_one',lambda *_:{})
    assert client.get('/request-portal/'+token).status_code==404
    assert client.get('/request-portal/short').status_code==404


def test_markdown_export_uses_only_signed_in_owner(client,monkeypatch):
    login(client)
    seen=[]
    def rows(sql,params): seen.append(params);return []
    monkeypatch.setattr(ws,'query_all',rows)
    r=client.get('/workspace/brain.md')
    assert r.status_code==200 and r.text.startswith('# My Brain')
    assert seen==[('gio',)]


@pytest.mark.parametrize('gateway_fails',[False,True])
def test_text_claim_prevents_retries_and_requires_saved_recipient(client,monkeypatch,gateway_fails):
    import operations_app
    csrf=login(client)
    person_id=uuid4();draft=uuid4();saved={};calls=[]
    person={'id':person_id,'kind':'PERSON','attributes':{'phones':['+12015550101']}}
    monkeypatch.setattr(ws,'entity',lambda *_:person)
    class Result:
        def __init__(self,row):self.row=row
        def fetchone(self):return self.row
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def commit(self):pass
        def execute(self,sql,params):
            if 'INSERT INTO workspace_messages' in sql:
                if params[0] in saved:return Result(None)
                saved[params[0]]='PENDING';return Result({'id':params[0]})
            if 'UPDATE workspace_messages' in sql:
                saved[params[0]]='UNKNOWN' if "'UNKNOWN'" in sql else 'ACCEPTED'
                return Result(None)
            assert 'SELECT status' in sql
            return Result({'status':saved[params[0]]})
    monkeypatch.setattr(ws,'db_conn',Connection)
    def send(phone,body):
        calls.append((phone,body))
        if gateway_fails:raise ValueError('No acceptance received')
    monkeypatch.setattr(operations_app,'_send_smsgate',send)
    data={'action':'SEND_TEXT','csrf':csrf,'entity_id':str(person_id),'message_id':str(draft),
          'phone':'+12015550101','body':'Thinking of you today.'}
    r=client.post('/workspace/api/action',json=data)
    assert r.status_code==(502 if gateway_fails else 200)
    assert saved[draft]==('UNKNOWN' if gateway_fails else 'ACCEPTED')
    r=client.post('/workspace/api/action',json=data)
    assert r.status_code==200 and 'No duplicate sent' in r.json()['message']
    assert calls==[('+12015550101','Thinking of you today.')]
    assert client.post('/workspace/api/action',json={**data,'message_id':str(uuid4()),'phone':'+12015550102'}).status_code==400
    assert len(calls)==1
