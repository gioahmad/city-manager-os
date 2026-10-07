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
    def __init__(self):
        self.writes=[];self.outcome='ok';self.mailbox='fixture@example.com';self.read_failure=False;self.token_count=0
        self.photo_reads=[];self.photo_failure=''
        self.photos={
            'site-photo': {'id':'site-photo','name':'site.jpg','contentType':'image/jpeg','size':12,'isInline':False,'@odata.type':'#microsoft.graph.fileAttachment'},
            'inline-photo': {'id':'inline-photo','name':'embedded.png','contentType':'image/png','size':10,'isInline':True,'@odata.type':'#microsoft.graph.fileAttachment'},
            'unused-photo': {'id':'unused-photo','name':'not-needed.jpg','contentType':'image/jpeg','size':9,'isInline':False,'@odata.type':'#microsoft.graph.fileAttachment'},
            'pdf-file': {'id':'pdf-file','name':'notice.pdf','contentType':'application/pdf','size':20,'isInline':False,'@odata.type':'#microsoft.graph.fileAttachment'},
        }
        self.contacts={'contact-jane': {'id':'contact-jane','displayName':'Jane Fixture','companyName':'Test Town','jobTitle':'Coordinator',
            'emailAddresses':[{'address':'Jane@Example.com','name':'Jane'}], 'mobilePhone':'201-555-1234', 'businessPhones':[], 'homePhones':[],
            'businessAddress':{'street':'1 Main St','city':'Weehawken','state':'NJ','postalCode':'07086','countryOrRegion':'US'},
            'changeKey':'v1','@odata.etag':'W/"v1"'}}
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
            if path=='/v1.0/me/contacts':return httpx.Response(200,json={'value':list(self.contacts.values())})
            if path.startswith('/v1.0/me/contacts/'):
                contact=self.contacts.get(path.rsplit('/',1)[-1])
                return httpx.Response(200,json=contact) if contact else httpx.Response(404,json={})
            if path.startswith('/v1.0/me/messages/'):
                assert 'ImmutableId' in request.headers.get('Prefer','')
                if path.endswith('/attachments'):
                    return httpx.Response(200,json={'value':list(self.photos.values())})
                if '/attachments/' in path:
                    attachment_id=path.split('/attachments/',1)[1].removesuffix('/$value')
                    if attachment_id==self.photo_failure:return httpx.Response(503,json={})
                    photo=self.photos.get(attachment_id)
                    if not photo:return httpx.Response(404,json={})
                    if path.endswith('/$value'):
                        self.photo_reads.append(attachment_id)
                        return httpx.Response(200,content=('original-'+attachment_id).encode())
                    return httpx.Response(200,json=photo)
                return httpx.Response(200,json={'from':{'emailAddress':{'address':'sender@example.com'}},'replyTo':[{'emailAddress':{'address':'reply-desk@example.com'}}]})
        if request.method=='PATCH' and path.startswith('/v1.0/me/contacts/'):
            contact=self.contacts.get(path.rsplit('/',1)[-1])
            if not contact:return httpx.Response(404,json={})
            if request.headers.get('If-Match')!=contact['@odata.etag']:return httpx.Response(412,json={})
            self.writes.append({'path':path,'body':json.loads(request.content),'method':'PATCH'})
            if self.outcome=='timeout':raise httpx.ReadTimeout('fixture contact timeout',request=request)
            if self.outcome=='rejected':return httpx.Response(403,json={})
            contact.update(json.loads(request.content));contact['changeKey']='v'+str(len(self.writes)+1);contact['@odata.etag']='W/"'+contact['changeKey']+'"'
            return httpx.Response(200,json=contact)
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
        for prefix in ['001_','002_','003_','015_','017_','035_','036_','037_','038_','039_','040_','042_','043_']:
            path=next((ROOT/'deploy/postgis/init').glob(prefix+'*.sql'));c.execute(path.read_text())
        # Repeat the new migration to prove additive/idempotent installation.
        c.execute((ROOT/'deploy/postgis/init/042_microsoft_workspace.sql').read_text())
        c.execute((ROOT/'deploy/postgis/init/043_outlook_contacts.sql').read_text())
    import app as core
    import workspace_calendar as calendar
    import microsoft_workspace as ms
    import microsoft_contacts
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
        if getattr(route,'path','') in {'/contacts','/email','/calendar','/important','/appearance'} or getattr(route,'path','').startswith(('/contacts/','/workspace/api/microsoft/','/workspace/api/hub','/workspace/calendar/microsoft/','/workspace/documents/')):application.router.routes.append(route)
    auth.configure_private_auth(application)
    cookie=auth._issue_session(auth.Account('MSTest','EXECUTIVE',''))
    csrf=hmac.new(auth._session_secret(),cookie.encode(),hashlib.sha256).hexdigest()
    return application,core,ms,calendar,hub,auth,provider,owner,mail_id,cookie,csrf
