"""Owner-private Microsoft 365 imports using delegated, read-only Graph permissions."""
import base64
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from psycopg import Error as DatabaseError

from app import app, db_conn, query_one
from brain_app import _owner
from private_auth import COOKIE_NAME

CALENDAR_SCOPE='offline_access https://graph.microsoft.com/Calendars.ReadBasic'
SCOPE=CALENDAR_SCOPE+' https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Contacts.Read'
WRITE_PERMISSIONS={'Mail.ReadWrite','Mail.Send','Calendars.ReadWrite'}
WRITE_SCOPE=SCOPE+' '+' '.join('https://graph.microsoft.com/'+s for s in sorted(WRITE_PERMISSIONS))
GRAPH='https://graph.microsoft.com/v1.0/'


def permissions(scopes):
    canonical={s.casefold():s for s in ('offline_access','Calendars.ReadBasic','Mail.Read','Contacts.Read','Mail.ReadWrite','Mail.Send','Calendars.ReadWrite')}
    return {canonical.get(s.rsplit('/',1)[-1].casefold(),s.rsplit('/',1)[-1]) for s in str(scopes).split()}


def validated_scopes(scopes):
    allowed={'offline_access','Calendars.ReadBasic','Mail.Read','Contacts.Read'} | WRITE_PERMISSIONS
    names=permissions(scopes)&allowed
    if not names & {'Calendars.ReadBasic','Calendars.ReadWrite'}:raise ValueError('Calendar consent missing')
    return 'offline_access '+' '.join('https://graph.microsoft.com/'+s for s in sorted(names-{'offline_access'}))


def safe_outlook_url(value):
    parsed=urlparse(str(value or ''))
    return str(value) if parsed.scheme=='https' and parsed.hostname in {'outlook.office.com','outlook.office365.com','outlook.live.com'} and not parsed.username else None


def plain_body(value):
    content=str((value or {}).get('content') or '')[:100000]
    if str((value or {}).get('contentType','text')).lower()=='html':
        from html.parser import HTMLParser
        class Text(HTMLParser):
            def __init__(self):super().__init__();self.parts=[];self.skip=0
            def handle_starttag(self,tag,attrs):
                if tag in {'script','style'}:self.skip+=1
                if tag in {'p','br','div','tr','li'}:self.parts.append('\n')
            def handle_endtag(self,tag):
                if tag in {'script','style'}:self.skip=max(0,self.skip-1)
            def handle_data(self,data):
                if not self.skip:self.parts.append(data)
        parser=Text();parser.feed(content);content=''.join(parser.parts)
    return content.replace('\x00','')[:20000]


def mail_row(owner,item):
    sender=(item.get('from') or {}).get('emailAddress') or {}
    received=datetime.fromisoformat(item['receivedDateTime'].replace('Z','+00:00'))
    if not received.tzinfo:raise ValueError('Mail date lacks timezone')
    recipients=[{'name':str((r.get('emailAddress') or {}).get('name') or '')[:200],
                 'email':str((r.get('emailAddress') or {}).get('address') or '')[:320]} for r in item.get('toRecipients',[])[:100]]
    return (owner,str(item['id'])[:2000],str(item.get('subject') or '(No subject)')[:500],plain_body(item.get('body')),
            str(sender.get('name') or '')[:200],str(sender.get('address') or '')[:320],json.dumps(recipients),
            str(item.get('conversationId') or '')[:2000],received,bool(item.get('isRead')),safe_outlook_url(item.get('webLink')))


def contact_row(owner,item):
    address=item.get('businessAddress') or item.get('homeAddress') or {}
    phones=[item.get('mobilePhone'),*(item.get('businessPhones') or []),*(item.get('homePhones') or [])]
    attrs={'emails':[str(r.get('address'))[:320] for r in (item.get('emailAddresses') or [])[:20] if r.get('address')],
           'phones':[str(p)[:80] for p in phones[:20] if p], 'organization':str(item.get('companyName') or '')[:200],
           'title':str(item.get('jobTitle') or '')[:200],
           'address':', '.join(str(address.get(k) or '') for k in ('street','city','state','postalCode') if address.get(k))[:1000],
           'birthday':str(item.get('birthday') or '')[:40], 'tags':['Microsoft 365']}
    name=item.get('displayName') or ' '.join(str(item.get(k) or '') for k in ('givenName','surname')).strip() or 'Unnamed contact'
    return (owner,str(item['id'])[:2000],str(name)[:200],json.dumps(attrs))


def replace_mail(c,owner,rows):
    for row in rows:
        c.execute('''INSERT INTO workspace_microsoft_mail(owner_username,provider_key,title,body,sender_name,sender_email,
            recipients,conversation_key,received_at,is_read,outlook_url) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
            ON CONFLICT(owner_username,provider_key) DO UPDATE SET title=EXCLUDED.title,body=EXCLUDED.body,
            sender_name=EXCLUDED.sender_name,sender_email=EXCLUDED.sender_email,recipients=EXCLUDED.recipients,
            conversation_key=EXCLUDED.conversation_key,received_at=EXCLUDED.received_at,is_read=EXCLUDED.is_read,
            outlook_url=EXCLUDED.outlook_url''',row)
    c.execute('''DELETE FROM workspace_microsoft_mail m WHERE owner_username=%s AND NOT(provider_key=ANY(%s::text[]))
        AND NOT EXISTS(SELECT 1 FROM workspace_important f WHERE f.owner_username=m.owner_username AND f.kind='MAIL' AND f.item_id=m.id)
        AND NOT EXISTS(SELECT 1 FROM workspace_context_links l WHERE l.owner_username=m.owner_username
            AND ((l.source_kind='MAIL' AND l.source_id=m.id) OR (l.target_kind='MAIL' AND l.target_id=m.id)))''',
              (owner,[r[1] for r in rows]))


def replace_contacts(c,owner,rows):
    for row in rows:
        c.execute('''INSERT INTO workspace_microsoft_contacts(owner_username,provider_key,name,attributes)
            VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT(owner_username,provider_key) DO UPDATE SET
            name=EXCLUDED.name,attributes=EXCLUDED.attributes,updated_at=now()''',row)
    c.execute('''DELETE FROM workspace_microsoft_contacts WHERE owner_username=%s AND NOT(provider_key=ANY(%s::text[]))
        AND imported_entity_id IS NULL''',
              (owner,[r[1] for r in rows]))


def settings():
    origin=os.getenv('CMOS_PUBLIC_ORIGIN','').rstrip('/')
    tenant=os.getenv('CMOS_MICROSOFT_TENANT','organizations')
    client=os.getenv('CMOS_MICROSOFT_CLIENT_ID','')
    secret=os.getenv('CMOS_MICROSOFT_CLIENT_SECRET','')
    key=os.getenv('CMOS_CALENDAR_KEY','')
    url=urlparse(origin)
    ready=bool(client and secret and key and url.scheme=='https' and url.netloc and not url.path and not url.query and not url.fragment
               and re.fullmatch(r'[A-Za-z0-9.-]+',tenant))
    try:Fernet(key.encode())
    except (ValueError,TypeError):ready=False
    return {'ready':ready,'client':client,'secret':secret,'key':key,'tenant':tenant,
            'redirect':origin+'/workspace/calendar/microsoft/callback'}


def cipher():
    cfg=settings()
    if not cfg['ready']:raise HTTPException(400,'Microsoft 365 setup is needed. Follow the connection instructions in Settings.')
    return Fernet(cfg['key'].encode())


def status(owner,lookup=query_one):
    row=lookup('''SELECT connected_at,last_sync_at,sync_error,scopes,
        (SELECT count(*) FROM workspace_microsoft_mail WHERE owner_username=%s) AS mail_count,
        (SELECT count(*) FROM workspace_microsoft_contacts WHERE owner_username=%s) AS contact_count
        FROM workspace_calendar_connections WHERE owner_username=%s''',(owner,owner,owner))
    scopes=permissions((row or {}).get('scopes',CALENDAR_SCOPE))
    return {'ready':settings()['ready'],'connected':bool(row),'mail_enabled':bool(scopes & {'Mail.Read','Mail.ReadWrite'}),
            'contacts_enabled':'Contacts.Read' in scopes,**{k:v for k,v in (row or {}).items() if k!='scopes'}}


def begin(owner,request,*,enable_write=False):
    requested=WRITE_SCOPE if enable_write else SCOPE
    cfg=settings();encrypt=cipher();state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48)
    with db_conn() as c:
        c.execute('DELETE FROM workspace_calendar_auth WHERE expires_at<now() OR owner_username=%s',(owner,))
        c.execute('INSERT INTO workspace_calendar_auth(state_hash,owner_username,session_hash,verifier,requested_scopes) VALUES(%s,%s,%s,%s,%s)',
                  (hashlib.sha256(state.encode()).hexdigest(),owner,hashlib.sha256(request.cookies[COOKIE_NAME].encode()).hexdigest(),encrypt.encrypt(verifier.encode()).decode(),requested))
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    params={'client_id':cfg['client'],'response_type':'code','redirect_uri':cfg['redirect'],'scope':requested,
            'state':state,'code_challenge':challenge,'code_challenge_method':'S256','prompt':'select_account'}
    return {'redirect_url':f"https://login.microsoftonline.com/{cfg['tenant']}/oauth2/v2.0/authorize?"+urlencode(params)}


def token_request(client,data):
    cfg=settings()
    response=client.post(f"https://login.microsoftonline.com/{cfg['tenant']}/oauth2/v2.0/token",
        data={'client_id':cfg['client'],'client_secret':cfg['secret'],'scope':SCOPE,**data})
    response.raise_for_status();result=response.json()
    if not isinstance(result,dict) or not result.get('access_token'):raise ValueError('Missing token')
    return result


@app.get('/workspace/calendar/microsoft/callback')
def callback(request: Request, state: str='', code: str='', error: str=''):
    owner=_owner(request,write=True)
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}',state):raise HTTPException(400,'Calendar sign-in expired. Start again.')
    cfg=settings();encrypt=cipher()
    with db_conn() as c:
        pending=c.execute('''DELETE FROM workspace_calendar_auth WHERE state_hash=%s AND owner_username=%s
            AND session_hash=%s AND expires_at>now() RETURNING verifier,requested_scopes''',
            (hashlib.sha256(state.encode()).hexdigest(),owner,hashlib.sha256(request.cookies[COOKIE_NAME].encode()).hexdigest())).fetchone()
    if not pending:raise HTTPException(400,'Calendar sign-in expired. Start again.')
    if error:return RedirectResponse('/workspace?view=settings',status_code=303)
    if not code or len(code)>10000:raise HTTPException(400,'Calendar sign-in did not return a code')
    try:
        verifier=encrypt.decrypt(pending['verifier'].encode()).decode()
        with httpx.Client(timeout=20,follow_redirects=False) as client:
            requested=pending.get('requested_scopes') or SCOPE
            tokens=token_request(client,{'grant_type':'authorization_code','code':code,'redirect_uri':cfg['redirect'],'code_verifier':verifier,'scope':requested})
            # Never silently promote a read connection or mix two mailbox identities.
            actual=tokens.get('scope')
            if not actual:raise ValueError('Microsoft did not return granted permissions')
            scopes=validated_scopes(actual)
            if (permissions(scopes) & WRITE_PERMISSIONS) - permissions(requested):
                raise ValueError('Unrequested write permissions')
            from microsoft_workspace import connection_identity
            account_email=connection_identity(owner,client,tokens['access_token'])
        if not tokens.get('refresh_token'):raise ValueError('Missing refresh token')
        encrypted=encrypt.encrypt(json.dumps({'owner':owner,'refresh':tokens['refresh_token']}).encode()).decode()
        with db_conn() as c:
            c.execute('''INSERT INTO workspace_calendar_connections(owner_username,tokens,scopes,account_email) VALUES(%s,%s,%s,%s)
                ON CONFLICT(owner_username) DO UPDATE SET tokens=EXCLUDED.tokens,scopes=EXCLUDED.scopes,account_email=EXCLUDED.account_email,sync_error=false''',(owner,encrypted,scopes,account_email))
        sync(owner)
    except (httpx.HTTPError,ValueError,InvalidToken,HTTPException):
        # Keep provider response and token contents out of user-facing errors.
        return RedirectResponse('/workspace?view=settings',status_code=303)
    return RedirectResponse('/email',status_code=303)


def graph_pages(client,url,access,*,limit=1000,truncate=False,mail=False,deadline=None):
    rows=[]
    for page in range(10):
        if deadline is not None and time.monotonic()>deadline:raise ValueError('Microsoft refresh time limit reached')
        parsed=urlparse(url)
        if parsed.scheme!='https' or parsed.netloc!='graph.microsoft.com' or not parsed.path.startswith('/v1.0/'):
            raise ValueError('Unexpected pagination target')
        response=client.get(url,headers={'Authorization':'Bearer '+access,'Prefer':('outlook.body-content-type="text", IdType="ImmutableId"' if mail else 'outlook.timezone="UTC"')})
        response.raise_for_status();body=response.json()
        if not isinstance(body,dict) or not isinstance(body.get('value'),list):raise ValueError('Missing Microsoft results')
        rows.extend(body['value']);url=body.get('@odata.nextLink')
        if truncate and len(rows)>=limit:return rows[:limit]
        if len(rows)>limit:raise ValueError('Microsoft result limit reached')
        if not url:return rows
    raise ValueError('Calendar page limit reached')


def event_row(owner,event):
    if event.get('isCancelled'):return None
    def timestamp(key):
        value=event[key]
        if value.get('timeZone') not in {'UTC','Etc/UTC'}:raise ValueError('Calendar must return UTC')
        result=datetime.fromisoformat(value['dateTime'].replace('Z','+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    link=safe_outlook_url(event.get('webLink'))
    start,end=timestamp('start'),timestamp('end')
    if end<start:raise ValueError('Invalid calendar range')
    return (owner,str(event['id'])[:2000],str(event.get('subject') or 'Appointment')[:500],start,end,
            str((event.get('location') or {}).get('displayName') or '')[:1000],bool(event.get('isAllDay')),link)


def sync(owner):
    encrypt=cipher()
    with db_conn() as c:
        connection=c.execute('SELECT tokens,scopes FROM workspace_calendar_connections WHERE owner_username=%s FOR UPDATE',(owner,)).fetchone()
        if not connection:raise HTTPException(400,'Connect your Microsoft account first')
        snapshot=False
        try:
            saved=json.loads(encrypt.decrypt(connection['tokens'].encode()))
            if saved['owner']!=owner:raise ValueError('Calendar owner mismatch')
            with httpx.Client(timeout=20,follow_redirects=False) as client:
                scopes=connection.get('scopes',CALENDAR_SCOPE)
                tokens=token_request(client,{'grant_type':'refresh_token','refresh_token':saved['refresh'],'scope':scopes})
                saved['refresh']=tokens.get('refresh_token') or saved['refresh']
                c.execute('UPDATE workspace_calendar_connections SET tokens=%s WHERE owner_username=%s',
                          (encrypt.encrypt(json.dumps(saved).encode()).decode(),owner))
                now=datetime.now(timezone.utc)
                url=GRAPH+'me/calendarView?'+urlencode({'startDateTime':now.isoformat(),'endDateTime':(now+timedelta(days=30)).isoformat(),
                    '$top':'100','$select':'id,subject,start,end,location,isAllDay,isCancelled,webLink'})
                rows=[r for e in graph_pages(client,url,tokens['access_token']) if (r:=event_row(owner,e))]
                granted=permissions(scopes);mails=contacts=None;deadline=time.monotonic()+75
                if granted & {'Mail.Read','Mail.ReadWrite'}:
                    url=GRAPH+'me/mailFolders/inbox/messages?'+urlencode({'$top':'50',
                        '$filter':'receivedDateTime ge '+(now-timedelta(days=30)).isoformat(),
                        '$orderby':'receivedDateTime desc',
                        '$select':'id,subject,body,from,toRecipients,receivedDateTime,isRead,webLink,conversationId'})
                    mails=[mail_row(owner,e) for e in graph_pages(client,url,tokens['access_token'],limit=250,truncate=True,mail=True,deadline=deadline)]
                if 'Contacts.Read' in granted:
                    url=GRAPH+'me/contacts?'+urlencode({'$top':'100',
                        '$select':'id,displayName,givenName,surname,emailAddresses,mobilePhone,businessPhones,homePhones,businessAddress,homeAddress,companyName,jobTitle,birthday'})
                    contacts=[contact_row(owner,e) for e in graph_pages(client,url,tokens['access_token'],limit=500,truncate=True,deadline=deadline)]
            c.execute('SAVEPOINT microsoft_snapshot',());snapshot=True
            calendar_keys=[]
            for row in rows:
                calendar_keys.append(row[1])
                c.execute('''INSERT INTO workspace_calendar_events(owner_username,event_key,title,starts_at,ends_at,location,all_day,outlook_url)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(owner_username,event_key) DO UPDATE SET
                      title=EXCLUDED.title,starts_at=EXCLUDED.starts_at,ends_at=EXCLUDED.ends_at,
                      location=EXCLUDED.location,all_day=EXCLUDED.all_day,outlook_url=EXCLUDED.outlook_url''',row)
            c.execute('''DELETE FROM workspace_calendar_events e
                WHERE e.owner_username=%s AND NOT(e.event_key=ANY(%s::text[])) AND e.calendar_key='primary'
                  AND NOT EXISTS(SELECT 1 FROM workspace_important f WHERE f.owner_username=e.owner_username AND f.kind='CALENDAR' AND f.item_id=e.id)
                  AND NOT EXISTS(
                    SELECT 1 FROM workspace_context_links l
                    WHERE l.owner_username=e.owner_username
                      AND ((l.source_kind='CALENDAR' AND l.source_id=e.id)
                        OR (l.target_kind='CALENDAR' AND l.target_id=e.id))
                  )''',(owner,calendar_keys))
            if mails is not None:
                replace_mail(c,owner,mails)
            if contacts is not None:
                replace_contacts(c,owner,contacts)
            c.execute('UPDATE workspace_calendar_connections SET last_sync_at=now(),sync_error=false WHERE owner_username=%s',(owner,))
        except (httpx.HTTPError,InvalidToken,ValueError,KeyError,TypeError,DatabaseError):
            if snapshot:c.execute('ROLLBACK TO SAVEPOINT microsoft_snapshot',())
            c.execute('UPDATE workspace_calendar_connections SET sync_error=true WHERE owner_username=%s',(owner,))
            c.commit()  # Preserve rotated refresh token and the last successful event snapshot.
            raise HTTPException(502,'Microsoft 365 could not refresh. Your previous imports are retained; check Settings or reconnect.')
    return {'message':'Outlook refreshed. Email, contacts, and appointments stay private to your account.'}


def disconnect(owner):
    with db_conn() as c:
        c.execute("UPDATE workspace_microsoft_operations SET status='CANCELLED',updated_at=now() WHERE owner_username=%s AND status='REVIEW'",(owner,))
        c.execute("DELETE FROM workspace_important WHERE owner_username=%s AND kind IN ('MAIL','CALENDAR','CONTACT')",(owner,))
        c.execute('DELETE FROM workspace_calendar_connections WHERE owner_username=%s',(owner,))
        c.execute('DELETE FROM workspace_calendar_auth WHERE owner_username=%s',(owner,))
    return {'message':'Microsoft 365 disconnected. Imported email, contact previews, and appointments removed. People and tasks you created are retained.'}
