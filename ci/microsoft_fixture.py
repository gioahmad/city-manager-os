"""Isolated real database + real application handlers; Microsoft HTTP only is mocked.
Requires a fresh CI-local cmos_tests database. Never accepts a production endpoint.
"""
from pathlib import Path
import os,sys,json,hashlib,hmac
from datetime import datetime,timezone,timedelta
from urllib.parse import urlsplit
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'dashboard'));os.chdir(ROOT/'dashboard')
import psycopg
from psycopg.rows import dict_row
from cryptography.fernet import Fernet
import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
REAL_CLIENT=httpx.Client

class Provider:
    def __init__(self):self.writes=[];self.outcome='ok';self.mailbox='fixture@example.com';self.read_failure=False;self.token_count=0
    def __call__(self,request):
        host,path=request.url.host,request.url.path
        if host=='login.microsoftonline.com' and path.endswith('/token'):
            self.token_count+=1
            return httpx.Response(200,json={'access_token':'fixture-token','refresh_token':'rotated-fixture-'+str(self.token_count)})
        if host!='graph.microsoft.com':raise AssertionError('Unexpected external host; real network is prohibited')
        if request.method=='GET':
            if self.read_failure:return httpx.Response(503,json={'error':{'message':'fixture outage'}})
            if path=='/v1.0/me/calendar':return httpx.Response(200,json={'id':'primary-id','name':'Main calendar','owner':{'address':self.mailbox},'canEdit':True})
            if path=='/v1.0/me/calendars':return httpx.Response(200,json={'value':[
                {'id':'primary-id','name':'Main calendar','owner':{'address':self.mailbox},'canEdit':True},
                {'id':'project-calendar','name':'Project calendar','owner':{'address':self.mailbox},'canEdit':True},
                {'id':'shared-calendar','name':'Other mailbox','owner':{'address':'someone-else@example.com'},'canEdit':True}]})
            if '/calendarView' in path:
                from zoneinfo import ZoneInfo
                today=datetime.now(ZoneInfo('America/New_York')).date()
                start=datetime.fromisoformat(str(today)+'T14:00:00+00:00')
                return httpx.Response(200,json={'value':[{'id':'event-'+('project' if 'project-calendar' in path else 'primary'),'subject':'Fixture site visit',
                    'start':{'dateTime':start.replace(tzinfo=None).isoformat(),'timeZone':'UTC'},'end':{'dateTime':(start+timedelta(hours=1)).replace(tzinfo=None).isoformat(),'timeZone':'UTC'},
                    'location':{'displayName':'Test site'},'webLink':'https://outlook.office.com/calendar/item/fixture','isAllDay':False}]})
            if path=='/v1.0/me/mailFolders/inbox/messages':return httpx.Response(200,json={'value':[]})
            if path=='/v1.0/me/contacts':return httpx.Response(200,json={'value':[]})
            if path.startswith('/v1.0/me/messages/'):
                assert 'ImmutableId' in request.headers.get('Prefer','')
                return httpx.Response(200,json={'from':{'emailAddress':{'address':'sender@example.com'}},'replyTo':[{'emailAddress':{'address':'reply-desk@example.com'}}]})
        if request.method=='POST':
            self.writes.append({'path':path,'body':json.loads(request.content)})
            if self.outcome=='timeout':raise httpx.ReadTimeout('fixture timeout after acceptance',request=request)
            if self.outcome=='rejected':return httpx.Response(403,json={'error':{'message':'fixture reject'}})
            if self.outcome=='server-error':return httpx.Response(503,json={})
            sending=path.endswith('/sendMail') or path.endswith('/reply')
            if sending:return httpx.Response(202)
            return httpx.Response(201,json={'id':'created-'+str(len(self.writes)),'webLink':'https://outlook.office.com/mail/item/fixture'})
        raise AssertionError('Unmodeled Graph operation: '+request.method+' '+path)


def setup():
    dsn=os.environ['CMOS_TEST_POSTGRES_DSN'];url=urlsplit(dsn)
    assert url.hostname in {'127.0.0.1','localhost'} and url.path=='/cmos_tests','CI-local cmos_tests is required'
    os.environ.update(DB_HOST=url.hostname,DB_PORT=str(url.port or 5432),DB_NAME='cmos_tests',DB_USER=url.username or 'postgres',DB_PASSWORD=url.password or '',
        CMOS_AUTH_ENABLED='true',CMOS_AUTH_SECURE_COOKIES='false',CMOS_PUBLIC_ORIGIN='https://fixture.example.com',
        CMOS_SESSION_SECRET='microsoft-fixture-session-secret-over-32-chars',CMOS_EXECUTIVE_USERNAME='MSTest',CMOS_EXECUTIVE_PASSWORD_HASH='unused',
        CMOS_READONLY_USERNAME='Reader',CMOS_READONLY_PASSWORD_HASH='unused',CMOS_MICROSOFT_CLIENT_ID='fixture-client',CMOS_MICROSOFT_CLIENT_SECRET='fixture-secret',
        CMOS_MICROSOFT_TENANT='organizations',CMOS_CALENDAR_KEY=Fernet.generate_key().decode())
    with psycopg.connect(dsn,autocommit=True) as c:
        c.execute("DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='citymanager_app') THEN CREATE ROLE citymanager_app; END IF; END $$")
        for prefix in ['001_','002_','003_','015_','017_','035_','036_','037_','038_','039_','040_','042_']:
            path=next((ROOT/'deploy/postgis/init').glob(prefix+'*.sql'));c.execute(path.read_text())
        # Repeat the new migration to prove additive/idempotent installation.
        c.execute((ROOT/'deploy/postgis/init/042_microsoft_workspace.sql').read_text())
    import app as core
    import workspace_calendar as calendar
    import microsoft_workspace as ms
    import workspace_hub as hub
    import private_auth as auth
    if core._POOL:core._POOL.close();core._POOL=None
    provider=Provider()
    httpx.Client=lambda *a,**kw:REAL_CLIENT(*a,**{**kw,'transport':httpx.MockTransport(provider)})
    owner='mstest';mail_id=str(uuid4())
    tokens=calendar.cipher().encrypt(json.dumps({'owner':owner,'refresh':'fixture-refresh'}).encode()).decode()
    with core.db_conn() as c:
        c.execute("DELETE FROM workspace_microsoft_operations WHERE owner_username=%s",(owner,))
        c.execute('INSERT INTO workspace_calendar_connections(owner_username,tokens,scopes,account_email,last_sync_at) VALUES(%s,%s,%s,%s,now()) ON CONFLICT(owner_username) DO UPDATE SET tokens=EXCLUDED.tokens,scopes=EXCLUDED.scopes,account_email=EXCLUDED.account_email',(owner,tokens,calendar.WRITE_SCOPE,provider.mailbox))
        c.execute('INSERT INTO workspace_microsoft_mail(id,owner_username,provider_key,title,body,sender_email,received_at) VALUES(%s,%s,%s,%s,%s,%s,now())',(mail_id,owner,'mail-'+mail_id,'Fixture contractor email','Review the site visit. Private source body.','sender@example.com'))
    application=FastAPI();application.mount('/static',StaticFiles(directory=str(ROOT/'dashboard/static')),name='static')
    for route in list(core.app.routes):
        if getattr(route,'path','') in {'/email','/calendar','/important','/appearance'} or getattr(route,'path','').startswith(('/workspace/api/microsoft/','/workspace/api/hub','/workspace/calendar/microsoft/')):application.router.routes.append(route)
    auth.configure_private_auth(application)
    cookie=auth._issue_session(auth.Account('MSTest','EXECUTIVE',''))
    csrf=hmac.new(auth._session_secret(),cookie.encode(),hashlib.sha256).hexdigest()
    return application,core,ms,calendar,hub,auth,provider,owner,mail_id,cookie,csrf
