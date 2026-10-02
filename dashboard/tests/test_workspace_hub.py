import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
import private_auth as auth
import workspace_hub as hub


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('CMOS_AUTH_ENABLED','true');monkeypatch.setenv('CMOS_SESSION_SECRET','hub-secret-at-least-32-characters')
    for prefix,name in [('EXECUTIVE','Gio'),('READONLY','Reader')]:
        monkeypatch.setenv('CMOS_'+prefix+'_USERNAME',name);monkeypatch.setenv('CMOS_'+prefix+'_PASSWORD_HASH','unused')
    app=FastAPI();app.router.routes.extend(r for r in hub.app.routes if getattr(r,'path','').startswith(('/workspace/api/hub','/workspace/documents')))
    auth.configure_private_auth(app)
    with TestClient(app) as c:yield c


def login(client,role='EXECUTIVE',name='Gio'):
    client.cookies.set(auth.COOKIE_NAME,auth._issue_session(auth.Account(name,role,'')))
    return hmac.new(auth._session_secret(),client.cookies.get(auth.COOKIE_NAME).encode(),hashlib.sha256).hexdigest()


def test_auth_readonly_csrf_and_invalid_input(client,monkeypatch):
    monkeypatch.setattr(hub,'db_conn',lambda:pytest.fail('Unauthorized write reached the database'))
    monkeypatch.setattr(hub,'query_all',lambda *a:pytest.fail('Unauthorized read reached the database'))
    assert client.get('/workspace/api/hub',follow_redirects=False).status_code==303
    assert client.post('/workspace/api/hub/action',json={}).status_code==401
    csrf=login(client,'READ_ONLY','Reader')
    assert client.post('/workspace/api/hub/action',json={'csrf':csrf}).status_code==403
    assert client.post('/workspace/api/hub/upload',data={'csrf':csrf},files={'files':('memo.txt',b'private')}).status_code==403
    login(client)
    assert client.post('/workspace/api/hub/action',json={'csrf':'wrong'}).status_code==403
    assert client.post('/workspace/api/hub/action',content='[]').status_code==400
    assert client.post('/workspace/api/hub/answer',content='[]').status_code==400
    assert client.get('/workspace/api/hub?scope=unknown').status_code==400


def test_pagination_literal_search_and_owner_binding(monkeypatch):
    statements=[];items=[{'id':uuid4(),'title':'Example'} for _ in range(61)]
    monkeypatch.setattr(hub,'query_all',lambda sql,p:statements.append((sql,p)) or items)
    monkeypatch.setattr(hub,'config',lambda:{'name':'Workspace'})
    data=hub.list_items('gio',q='100%_budget',scope='personal',source='MAIL')
    assert len(data['items'])==60 and data['has_more']
    sql,p=statements[0];assert p[:2]==('gio','gio') and p[2:4]==('PRIVATE','MAIL')
    assert p[4:6]==('%100\\%\\_budget%',)*2 and p[-1]==0
    assert 'h.owner_username=%s' in sql and 'm.owner_username=a.owner' in sql


def test_missing_and_other_owners_records_never_produce_preview(client,monkeypatch):
    login(client);seen=[]
    monkeypatch.setattr(hub,'query_one',lambda sql,p:seen.append(p) or {})
    assert client.get('/workspace/api/hub/detail/MAIL/'+str(uuid4())).status_code==404
    assert seen[0][0]=='gio'
    assert client.get('/workspace/api/hub/detail/UNKNOWN/'+str(uuid4())).status_code==400


def test_context_rechecks_both_ends_and_uses_evidence_only(monkeypatch):
    source={'kind':'MAIL','id':uuid4(),'title':'Site meeting','body':'Maria Rivera at 18 Example Street','metadata':{'sender_email':'maria@example.com'}}
    available={'id':uuid4(),'name':'Maria Rivera','attributes':{'emails':['maria@example.com']},'visibility':'PRIVATE'}
    vanished=uuid4();link_id=uuid4()
    def rows(sql,p):
        if 'workspace_context_links' in sql:return [{'id':link_id,'source_kind':'MAIL','source_id':source['id'],'target_kind':'RECORD','target_id':vanished}]
        assert p==('gio',) and "c.visibility='ALL'" in sql
        return [available]
    monkeypatch.setattr(hub,'query_all',rows)
    monkeypatch.setattr(hub,'find',lambda *a,**kw:(_ for _ in ()).throw(HTTPException(404)))
    links,suggestions=hub.context('gio',source)
    assert not links and suggestions[0]['evidence']=='Sender email matches this record.'
    assert 'relation' not in suggestions[0]


def test_upload_is_all_or_nothing_and_keeps_original(client,monkeypatch):
    csrf=login(client);saved=[]
    class Result:
        def fetchone(self):return {'id':uuid4()}
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,sql,p):saved.append(p);return Result()
    monkeypatch.setattr(hub,'db_conn',Connection)
    response=client.post('/workspace/api/hub/upload',data={'csrf':csrf},files=[('files',('memo.txt',b'private memo','text/plain')),('files',('bad.exe',b'program','application/octet-stream'))])
    assert response.status_code==400 and not saved
    response=client.post('/workspace/api/hub/upload',data={'csrf':csrf},files=[('files',('../../memo.txt',b'private memo','text/plain'))])
    assert response.status_code==200 and len(response.json()['ids'])==1
    assert saved[0]==('gio','memo.txt','text/plain',b'private memo')


def test_contact_import_stays_private_is_idempotent_and_never_changes_recipients(monkeypatch):
    source_id=uuid4();entity_id=uuid4();statements=[]
    source={'kind':'CONTACT','id':source_id,'title':'Maria','metadata':{}}
    contact={'attributes':{'phones':['+12015550101','extension 22'],'emails':['maria@example.com'],'organization':'Town','birthday':'1980-03-12T00:00:00Z'},'name':'Maria','imported_entity_id':None}
    monkeypatch.setattr(hub,'find',lambda *a,**kw:source)
    class Result:
        def __init__(self,row):self.row=row
        def fetchone(self):return self.row
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,sql,p):
            statements.append((sql,p))
            if 'SELECT * FROM workspace_microsoft_contacts' in sql:return Result(contact)
            if 'INSERT INTO workspace_entities' in sql:return Result({'id':entity_id})
            return Result(None)
    monkeypatch.setattr(hub,'db_conn',Connection)
    assert hub.action('gio',{'action':'IMPORT_CONTACT','kind':'CONTACT','id':str(source_id)})['id']==entity_id
    sql,p=next((s,p) for s,p in statements if 'INSERT INTO workspace_entities' in s)
    attrs=json.loads(p[-1]);assert "'PRIVATE'" in sql and p[1]=='gio'
    assert attrs['phones']==['+12015550101'] and attrs['source_phones']==['+12015550101','extension 22']
    assert any('workspace_dates' in s for s,p in statements)
    assert not any('INSERT INTO contacts' in s or 'subscribers' in s or 'watch_items' in s for s,p in statements)
    contact['imported_entity_id']=entity_id;statements.clear()
    hub.action('gio',{'action':'IMPORT_CONTACT','kind':'CONTACT','id':str(source_id)})
    assert not any('INSERT' in s for s,p in statements)


def test_answers_use_owned_sources_and_no_provider_by_default(client,monkeypatch):
    csrf=login(client);monkeypatch.delenv('CMOS_OLLAMA_URL',raising=False)
    sources=[{'kind':'BRAIN','id':uuid4(),'title':'Waterfront','excerpt':'Site walk Friday','visibility':'PRIVATE','route':'/brain'}]
    monkeypatch.setattr(hub,'evidence',lambda owner,q:sources if owner=='gio' else [])
    monkeypatch.setattr(hub.httpx,'AsyncClient',lambda **kw:pytest.fail('Unconfigured answer contacted a model'))
    data=client.post('/workspace/api/hub/answer',json={'csrf':csrf,'question':'Waterfront next steps?'}).json()
    assert data['answer'] is None and len(data['sources'])==1 and 'local model' in data['message']
    monkeypatch.setenv('CMOS_OLLAMA_MODEL','model');monkeypatch.setenv('CMOS_OLLAMA_URL','https://public.example.com')
    assert hub.ollama_settings() is None
    monkeypatch.setenv('CMOS_OLLAMA_URL','http://citymanager-ollama:11434')
    assert hub.ollama_settings()==('http://citymanager-ollama:11434','model')
