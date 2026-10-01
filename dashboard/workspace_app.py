"""Unified workspace; existing alert and integration modules remain independent."""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
from datetime import date, datetime, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Form, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from psycopg.types.json import Jsonb

from app import app, db_conn, query_all, query_one, templates
from brain_app import _owner, _csrf, _write
from workspace_engine import FAMILY, RELATIONS, briefing_dates, derive_relationships, suggestions, text_draft, occurrence

KINDS={'PERSON','ORGANIZATION','BUILDING','STREET','PROJECT','DOCUMENT'}
DEFAULT_CONFIG={'name':'City Manager OS','organization':'Weehawken','timezone':'America/New_York',
                'template':'CITY','personal':True,'water_ml':None,'protein_g':None}
WINDOWS={'6h':'6 hours','12h':'12 hours','24h':'24 hours','7d':'7 days','30d':'30 days','all':None}


def config():
    row=query_one('SELECT settings FROM workspace_config WHERE singleton=true')
    return {**DEFAULT_CONFIG,**(row.get('settings') or {})}


def visible(row, owner, display=False):
    return row['visibility']=='WORK' or (not display and row['owner_username']==owner)


def entity(entity_id, owner):
    row=query_one('''SELECT * FROM workspace_entities e WHERE id=%s AND (contact_id IS NULL OR EXISTS
        (SELECT 1 FROM contacts c WHERE c.id=e.contact_id AND c.active AND c.visibility='ALL'))''', (uid(entity_id),))
    if not row or not visible(row,owner):
        raise HTTPException(404,'Record not found')
    return row


def uid(value):
    try:
        return UUID(str(value))
    except (ValueError,TypeError,AttributeError):
        raise HTTPException(400,'Invalid record identifier')


def text(value, limit=200, required=False):
    value=str(value or '').strip()
    if len(value)>limit or (required and not value):
        raise HTTPException(400,f'Enter {"1 to " if required else "up to "}{limit} characters')
    return value


def attributes(data):
    from operations_app import _contact_phones, _contact_emails, _contact_values
    result={k:text(data.get(k),1000 if k in {'address','notes'} else 200)
            for k in ('address','unit','organization','title','notes')}
    try:
        result['phones']=_contact_phones(text(data.get('phones'),500))
        result['emails']=_contact_emails(text(data.get('emails'),500))
        result['tags']=_contact_values(text(data.get('tags'),500))[:30]
        result['aliases']=_contact_values(text(data.get('aliases'),500))[:20]
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    return result


def intelligence(window='24h'):
    if window not in WINDOWS:
        raise HTTPException(400,'Choose a valid time window')
    where='TRUE' if window=='all' else 'received_at >= now()-%s::interval'
    rows=query_all(f'''SELECT id,alert_id,title,source,municipality,priority,status,received_at
        FROM alerts WHERE {where} ORDER BY received_at DESC LIMIT 80''',
        () if window=='all' else (WINDOWS[window],))
    health=query_all('''SELECT source_id,status,last_success_at,last_event_at FROM source_health
        ORDER BY source_id LIMIT 100''')
    return rows,health


@app.get('/workspace',response_class=HTMLResponse)
@app.get('/workspace/display',response_class=HTMLResponse)
def workspace_page(request: Request):
    _owner(request)
    return templates.TemplateResponse(request=request,name='workspace.html',context={
        'csrf':_csrf(request),'display':request.url.path.endswith('/display'),
        'readonly':request.state.cmos_role=='READ_ONLY','username':request.state.cmos_account.username})


@app.get('/workspace/api/state')
def workspace_state(request: Request, display: bool=False, period: str='day', window: str='24h', view: str='all'):
    owner=_owner(request)
    if view not in {'all','today','intelligence','work','brain','people','dates','settings'}:
        raise HTTPException(400,'Choose a valid workspace section')
    cfg=config()
    today=datetime.now(ZoneInfo(cfg['timezone'])).date()
    data={'config':cfg,'today':today,'refreshed_at':datetime.now(timezone.utc)}
    wants=lambda *views: view=='all' or view in views
    if display or wants('intelligence','today'):
        alerts,health=intelligence(window)
        data.update(alerts=alerts,health=health)
    if display or wants('work','today'):
        data['work']=query_all("""SELECT id,title,status,priority,address,next_action,assigned_to,updated_at
            FROM issues WHERE status NOT IN ('RESOLVED','CLOSED') ORDER BY updated_at DESC LIMIT 80""")
    # Display never queries personal tables, regardless of the requested section.
    if display:
        data.pop('today')
        return JSONResponse(jsonable_encoder(data),headers={'Cache-Control':'no-store'})
    if wants('people','dates'):
        entities=query_all('''SELECT * FROM workspace_entities e WHERE (visibility='WORK' OR owner_username=%s)
            AND (contact_id IS NULL OR EXISTS(SELECT 1 FROM contacts c WHERE c.id=e.contact_id AND c.active AND c.visibility='ALL'))
            ORDER BY name,id LIMIT 501''',(owner,))
        limited=len(entities)>500
        entities=entities[:500]
        ids={str(e['id']) for e in entities}
        data.update(entities=entities,directory_limited=limited)
        if wants('people'):
            facts=query_all('''SELECT * FROM workspace_relationships WHERE active
                AND (visibility='WORK' OR owner_username=%s) ORDER BY created_at LIMIT 2000''',(owner,))
            facts=[f for f in facts if str(f['source_id']) in ids and str(f['target_id']) in ids]
            dismissed={r['fingerprint'] for r in query_all('SELECT fingerprint FROM workspace_dismissed WHERE owner_username=%s',(owner,))}
            data.update(entities=entities,facts=facts,derived=derive_relationships(facts),
                        suggestions=suggestions(entities,dismissed),directory_limited=limited)
    if wants('work'):
        portals=query_all('''SELECT p.id,p.issue_id,i.title,i.status,p.expires_at,p.revoked,
            coalesce((SELECT jsonb_agg(jsonb_build_object('author',m.author,'body',m.body,'created_at',m.created_at)
            ORDER BY m.created_at) FROM (SELECT author,body,created_at FROM workspace_portal_messages WHERE portal_id=p.id
                ORDER BY created_at DESC LIMIT 100) m),'[]'::jsonb) AS messages
            FROM workspace_portals p JOIN issues i ON i.id=p.issue_id ORDER BY p.created_at DESC LIMIT 50''')
        data['portals']=portals
    # Dates and messages always belong to the signed-in user, including Executive accounts.
    if wants('today','dates'):
        dates=query_all('''SELECT d.*,e.name,e.attributes FROM workspace_dates d JOIN workspace_entities e ON e.id=d.entity_id
            WHERE d.owner_username=%s AND d.active AND (e.visibility='WORK' OR e.owner_username=%s)
            AND (e.contact_id IS NULL OR EXISTS(SELECT 1 FROM contacts c WHERE c.id=e.contact_id AND c.active AND c.visibility='ALL'))''',(owner,owner))
        days={'day':0,'week':6,'month':29}.get(period)
        if days is None:
            raise HTTPException(400,'Choose daily, weekly, or monthly')
        reminders=briefing_dates(dates,today,days)
        for r in reminders:
            r['draft']=text_draft(r)
        data.update(dates=dates,reminders=reminders,today=today)
    if wants('today'):
        personal=query_all('SELECT * FROM workspace_personal_tasks WHERE owner_username=%s ORDER BY done,due_date NULLS LAST,created_at DESC LIMIT 100',(owner,))
        logs=query_all('''SELECT kind,sum(amount) AS amount,(logged_at AT TIME ZONE %s)::date AS day
            FROM workspace_health WHERE owner_username=%s AND logged_at>=now()-interval '31 days'
            GROUP BY kind,day ORDER BY day DESC''',(cfg['timezone'],owner))
        fast=query_one('SELECT * FROM workspace_fasts WHERE owner_username=%s AND ended_at IS NULL',(owner,))
        history=query_all('SELECT * FROM workspace_fasts WHERE owner_username=%s AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 30',(owner,))
        goals=query_one('SELECT water_ml,protein_g FROM workspace_goals WHERE owner_username=%s',(owner,))
        data.update(personal=personal,health_logs=logs,fast=fast,fast_history=history,goals=goals)
    if wants('brain'):
        notes=query_all('''SELECT id,left(body,1200) AS body,char_length(body)>1200 AS truncated,kind,tags,created_at FROM brain_notes WHERE owner_username=%s
            AND deleted_at IS NULL ORDER BY pinned DESC,created_at DESC LIMIT 30''',(owner,))
        data['notes']=notes
    if wants('today','settings'):
        from workspace_calendar import status
        data['calendar']=status(owner,lookup=query_one)
    if wants('today'):
        data['appointments']=query_all("""SELECT title,starts_at,ends_at,location,all_day,outlook_url
            FROM workspace_calendar_events WHERE owner_username=%s AND ends_at>=now()
            ORDER BY starts_at LIMIT 12""",(owner,))
    if wants('intelligence'):
        data['notices']=query_all("""SELECT title,municipality,source_name,starts_at,source_url,impact_summary
            FROM event_intelligence WHERE active AND status NOT IN ('CANCELLED','COMPLETED')
            AND ((starts_at>=now()-interval '12 hours' AND starts_at<now()+interval '30 days')
                OR (starts_at IS NULL AND last_seen_at>=now()-interval '3 days'))
            ORDER BY starts_at NULLS LAST,last_seen_at DESC LIMIT 40""")
    if wants('settings'):
        data['connections']=query_all("""SELECT i.name,i.active,i.parser_kind,i.geography_scope,
            sh.status,sh.last_success_at FROM integrations i LEFT JOIN source_health sh
            ON sh.source_id='INT:'||i.integration_key ORDER BY i.name LIMIT 100""")
    if view=='all':
        data['message_history']=query_all('''SELECT entity_id,status,created_at,phone FROM workspace_messages
            WHERE owner_username=%s ORDER BY created_at DESC LIMIT 50''',(owner,))
    return JSONResponse(jsonable_encoder(data),headers={'Cache-Control':'no-store'})


@app.get('/workspace/brain.md')
def brain_markdown(request: Request):
    owner=_owner(request)
    notes=query_all('''SELECT id,body,kind,tags,created_at FROM brain_notes WHERE owner_username=%s
        AND deleted_at IS NULL ORDER BY created_at,id''',(owner,))
    parts=['# My Brain\n']
    for n in notes:
        parts.append(f"\n## {n['kind'].title()} · {n['created_at'].isoformat()}\n\nRecord: {n['id']}\n\n{n['body']}\n\n"+
                     ' '.join('#'+t for t in n['tags'])+'\n')
    return Response('\n'.join(parts),media_type='text/markdown',headers={'Content-Disposition':'attachment; filename="brain.md"'})


@app.post('/workspace/api/action')
async def workspace_action(request: Request):
    length=request.headers.get('content-length','0')
    if not length.isdigit() or int(length)>40000:
        raise HTTPException(413,'Request too large')
    raw=await request.body()
    if len(raw)>40000:
        raise HTTPException(413,'Request too large')
    try:
        data=json.loads(raw)
    except (ValueError,UnicodeDecodeError):
        raise HTTPException(400,'Invalid request')
    if not isinstance(data,dict) or not isinstance(data.get('csrf'),str):
        raise HTTPException(400,'Refresh the page and try again')
    owner=_write(request,data['csrf'])
    try:
        result=await run_in_threadpool(action,owner,data,request)
    except (ValueError,TypeError,KeyError,ZoneInfoNotFoundError):
        raise HTTPException(400,'Check the values and try again')
    return JSONResponse(jsonable_encoder({'ok':True,**(result or {})}))


def action(owner,d,request):
    kind=d.get('action')
    if kind in {'MS_CALENDAR_CONNECT','MS_CALENDAR_SYNC','MS_CALENDAR_DISCONNECT'}:
        from workspace_calendar import begin,sync,disconnect
        if kind=='MS_CALENDAR_CONNECT':return begin(owner,request)
        if kind=='MS_CALENDAR_SYNC':return sync(owner)
        return disconnect(owner)
    with db_conn() as conn:
        if kind=='ENTITY':
            k=d.get('kind','PERSON'); vis=d.get('visibility','PRIVATE')
            if k not in KINDS or vis not in {'PRIVATE','WORK'}:
                raise HTTPException(400,'Invalid record type or visibility')
            attrs=attributes(d)
            if d.get('issue_id'):
                issue=query_one('SELECT id FROM issues WHERE id=%s',(uid(d['issue_id']),))
                if not issue: raise HTTPException(404,'Work item not found')
                attrs['issue_id']=str(issue['id'])
            if d.get('id'):
                record=entity(d['id'],owner)
                if record.get('contact_id'):
                    raise HTTPException(400,'Edit linked directory contacts in Contacts, then refresh the import')
                if k!=record['kind']: raise HTTPException(400,'Record type cannot be changed')
                if vis!=record['visibility'] and record['owner_username']!=owner:
                    raise HTTPException(403,'Only the record owner can change visibility')
                conn.execute('UPDATE workspace_entities SET name=%s,attributes=%s,visibility=%s,updated_at=now() WHERE id=%s',
                             (text(d['name'],200,True),Jsonb(attrs),vis,record['id']))
            else:
                row=conn.execute('''INSERT INTO workspace_entities(kind,name,visibility,owner_username,attributes)
                    VALUES(%s,%s,%s,%s,%s) RETURNING id''',(k,text(d['name'],200,True),vis,owner,Jsonb(attrs))).fetchone()
                return {'id':row['id']}
        elif kind=='IMPORT_CONTACTS':
            # Explicit reference to shared canonical contacts; private directory rows stay private.
            conn.execute('''INSERT INTO workspace_entities(kind,name,visibility,owner_username,attributes,contact_id)
                SELECT 'PERSON',name,'WORK',%s,jsonb_build_object('phones',phones,'emails',emails,'address',address,
                    'organization',organization,'title',title,'tags',tags,'notes',notes),id
                FROM contacts WHERE active AND visibility='ALL'
                ON CONFLICT(contact_id) DO UPDATE SET name=EXCLUDED.name,attributes=EXCLUDED.attributes,updated_at=now()''',(owner,))
        elif kind=='RELATIONSHIP':
            a,b=entity(d['source_id'],owner),entity(d['target_id'],owner)
            relation=d['relation']
            if relation not in RELATIONS or a['id']==b['id']:
                raise HTTPException(400,'Choose a valid relationship between different records')
            if relation in FAMILY and (a['kind']!='PERSON' or b['kind']!='PERSON'):
                raise HTTPException(400,'Family relationships require two people')
            visibility='PRIVATE' if relation in FAMILY or a['visibility']=='PRIVATE' or b['visibility']=='PRIVATE' or d.get('visibility')!='WORK' else 'WORK'
            conn.execute('''INSERT INTO workspace_relationships(source_id,target_id,relation,visibility,owner_username,evidence)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(source_id,target_id,relation,owner_username)
                DO UPDATE SET active=true,evidence=EXCLUDED.evidence,visibility=EXCLUDED.visibility''',
                (a['id'],b['id'],relation,visibility,owner,text(d.get('evidence'),2000)))
        elif kind=='REMOVE_RELATIONSHIP':
            row=conn.execute('UPDATE workspace_relationships SET active=false WHERE id=%s AND owner_username=%s RETURNING id',
                             (uid(d['id']),owner)).fetchone()
            if not row: raise HTTPException(404,'Relationship not found')
        elif kind=='DISMISS':
            if not re.fullmatch('[a-f0-9]{64}',d.get('fingerprint','')): raise HTTPException(400,'Invalid suggestion')
            conn.execute('INSERT INTO workspace_dismissed VALUES(%s,%s) ON CONFLICT DO NOTHING',(owner,d['fingerprint']))
        elif kind=='CAPTURE':
            body=text(d.get('body'),20000,True)
            destination=d.get('destination')
            if destination=='BRAIN':
                from brain_app import _values,_insert
                _insert(owner,*_values(body))
            elif destination=='PERSONAL':
                conn.execute('INSERT INTO workspace_personal_tasks(owner_username,title,due_date) VALUES(%s,%s,%s)',
                             (owner,text(body,500,True),date.fromisoformat(d['due_date']) if d.get('due_date') else None))
            elif destination=='WORK':
                row=conn.execute("INSERT INTO issues(title,description,source,status,priority,item_type) VALUES(%s,%s,'WORKSPACE','OPEN',3,'TASK') RETURNING id",
                                 (body[:180],body)).fetchone()
                return {'issue_id':row['id']}
            else: raise HTTPException(400,'Choose a capture destination')
        elif kind=='TASK':
            row=conn.execute('UPDATE workspace_personal_tasks SET done=%s WHERE id=%s AND owner_username=%s RETURNING id',
                             (bool(d.get('done')),uid(d['id']),owner)).fetchone()
            if not row: raise HTTPException(404,'Task not found')
        elif kind=='HEALTH':
            amount=float(d['amount']); metric=d['metric']
            if metric not in {'WATER','PROTEIN'} or not math.isfinite(amount) or not 0<amount<=(5000 if metric=='WATER' else 300):
                raise HTTPException(400,'Enter a valid amount in mL or grams')
            conn.execute('INSERT INTO workspace_health(owner_username,kind,amount) VALUES(%s,%s,%s)',(owner,metric,amount))
        elif kind=='GOALS':
            water=float(d['water_ml']) if d.get('water_ml') else None
            protein=float(d['protein_g']) if d.get('protein_g') else None
            if any(v is not None and (not math.isfinite(v) or not 1<=v<=limit) for v,limit in [(water,10000),(protein,1000)]):
                raise HTTPException(400,'Check your goals')
            conn.execute('''INSERT INTO workspace_goals(owner_username,water_ml,protein_g) VALUES(%s,%s,%s)
                ON CONFLICT(owner_username) DO UPDATE SET water_ml=EXCLUDED.water_ml,protein_g=EXCLUDED.protein_g''',(owner,water,protein))
        elif kind=='FAST_START':
            conn.execute('INSERT INTO workspace_fasts(owner_username) VALUES(%s) ON CONFLICT DO NOTHING',(owner,))
        elif kind=='FAST_END':
            conn.execute('UPDATE workspace_fasts SET ended_at=now() WHERE owner_username=%s AND ended_at IS NULL',(owner,))
        elif kind=='DATE':
            person=entity(d['entity_id'],owner)
            if person['kind']!='PERSON': raise HTTPException(400,'Choose a person')
            event=date.fromisoformat(d['event_date']); lead=int(d.get('lead_days',0)); occasion=d['occasion']
            if occasion not in {'BIRTHDAY','ANNIVERSARY','REMEMBRANCE','FOLLOW_UP','OTHER'} or not 0<=lead<=365:
                raise HTTPException(400,'Check the occasion and notice period')
            conn.execute('''INSERT INTO workspace_dates(entity_id,owner_username,occasion,label,event_date,annual,lead_days,context)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',(person['id'],owner,occasion,text(d.get('label') or occasion.replace('_',' ').title(),200,True),event,bool(d.get('annual')),lead,text(d.get('context'),2000)))
        elif kind in {'DATE_DONE','DATE_REMOVE'}:
            row=query_one('SELECT * FROM workspace_dates WHERE id=%s AND owner_username=%s',(uid(d['id']),owner))
            if not row: raise HTTPException(404,'Date not found')
            if kind=='DATE_REMOVE':
                conn.execute('UPDATE workspace_dates SET active=false WHERE id=%s',(row['id'],))
            else:
                event=occurrence(row,datetime.now(ZoneInfo(config()['timezone'])).date())
                conn.execute('UPDATE workspace_dates SET handled_occurrence=%s WHERE id=%s',(event,row['id']))
        elif kind=='SEND_TEXT':
            # Claim before the network call; one deliberate click cannot send twice on retry.
            person=entity(d['entity_id'],owner)
            if person['kind']!='PERSON': raise HTTPException(400,'Choose a person')
            from operations_app import _phone_numbers, _send_smsgate
            phones=_phone_numbers(text(d['phone'],40,True))
            if len(phones)!=1: raise HTTPException(400,'Choose one verified recipient')
            if phones[0] not in (person['attributes'].get('phones') or []):
                raise HTTPException(400,'Save and verify this number on the person record first')
            body=text(d['body'],4000,True); message_id=uid(d['message_id'])
            reminder=None; event=None
            if d.get('date_id'):
                reminder=query_one('SELECT * FROM workspace_dates WHERE id=%s AND entity_id=%s AND owner_username=%s AND active',
                                   (uid(d['date_id']),person['id'],owner))
                if not reminder: raise HTTPException(404,'Date not found')
                event=occurrence(reminder,datetime.now(ZoneInfo(config()['timezone'])).date())
            claimed=conn.execute('''INSERT INTO workspace_messages(id,owner_username,entity_id,date_id,occurrence,phone,body,status)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'PENDING') ON CONFLICT(id) DO NOTHING RETURNING id''',
                (message_id,owner,person['id'],reminder['id'] if reminder else None,event,phones[0],body)).fetchone()
            if not claimed:
                old=conn.execute('SELECT status FROM workspace_messages WHERE id=%s AND owner_username=%s',(message_id,owner)).fetchone()
                if not old: raise HTTPException(409,'Use a new message draft')
                return {'message':'Previous attempt: '+old['status']+'. No duplicate sent.'}
            conn.commit()
            try:
                _send_smsgate(phones[0],body)
            except ValueError:
                conn.execute("UPDATE workspace_messages SET status='UNKNOWN' WHERE id=%s",(message_id,))
                conn.commit()
                raise HTTPException(502,'Gateway did not confirm acceptance. Check SMSGate before trying a new draft; delivery may be uncertain.')
            conn.execute("UPDATE workspace_messages SET status='ACCEPTED' WHERE id=%s",(message_id,))
            if reminder:
                conn.execute('UPDATE workspace_dates SET handled_occurrence=%s WHERE id=%s',(event,reminder['id']))
            return {'message':'SMSGate accepted the text. This is not a delivery receipt.'}
        elif kind=='PORTAL':
            issue=query_one('SELECT id FROM issues WHERE id=%s',(uid(d['issue_id']),))
            if not issue: raise HTTPException(404,'Work item not found')
            token=secrets.token_urlsafe(32)
            conn.execute('INSERT INTO workspace_portals(issue_id,token_hash,owner_username) VALUES(%s,%s,%s)',
                         (issue['id'],hashlib.sha256(token.encode()).hexdigest(),owner))
            origin=os.getenv('CMOS_PUBLIC_ORIGIN','').rstrip('/')
            return {'portal_url':origin+'/request-portal/'+token}
        elif kind in {'PORTAL_REPLY','PORTAL_REVOKE'}:
            portal=query_one('SELECT id FROM workspace_portals WHERE id=%s',(uid(d['id']),))
            if not portal: raise HTTPException(404,'Request link not found')
            if kind=='PORTAL_REVOKE':
                conn.execute('UPDATE workspace_portals SET revoked=true WHERE id=%s',(portal['id'],))
            else:
                conn.execute("INSERT INTO workspace_portal_messages(portal_id,author,body) VALUES(%s,'STAFF',%s)",
                             (portal['id'],text(d['body'],4000,True)))
        elif kind=='CONFIG':
            if request.state.cmos_role!='EXECUTIVE': raise HTTPException(403,'Executive settings only')
            zone=text(d.get('timezone'),100,True); ZoneInfo(zone)
            template=d.get('template','CITY')
            if template not in {'CITY','BUSINESS','PERSONAL'}: raise HTTPException(400,'Choose a template')
            settings={'name':text(d.get('name'),60,True),'organization':text(d.get('organization'),100,True),
                      'timezone':zone,'template':template,'personal':bool(d.get('personal',True))}
            conn.execute('UPDATE workspace_config SET settings=%s WHERE singleton=true',(Jsonb(settings),))
        else: raise HTTPException(400,'Unknown workspace action')


def portal_record(token):
    if not re.fullmatch('[A-Za-z0-9_-]{43}',token): raise HTTPException(404,'Request link not found')
    row=query_one('''SELECT p.id,p.issue_id,i.title,i.status FROM workspace_portals p
        JOIN issues i ON i.id=p.issue_id WHERE token_hash=%s AND NOT revoked AND expires_at>now()''',
        (hashlib.sha256(token.encode()).hexdigest(),))
    if not row: raise HTTPException(404,'Request link expired or unavailable')
    return row


def portal_csrf(token):
    from private_auth import _session_secret
    return hmac.new(_session_secret(),('request-portal:'+token).encode(),hashlib.sha256).hexdigest()


@app.get('/request-portal/{token}',response_class=HTMLResponse)
def portal_page(token: str, request: Request):
    row=portal_record(token)
    messages=query_all('SELECT author,body,created_at FROM workspace_portal_messages WHERE portal_id=%s ORDER BY created_at LIMIT 500',(row['id'],))
    return templates.TemplateResponse(request=request,name='workspace_portal.html',context={
        'record':row,'messages':messages,'csrf':portal_csrf(token)})


@app.post('/request-portal/{token}')
def portal_post(token: str, request: Request, body: str=Form(...), csrf: str=Form(...)):
    row=portal_record(token)
    if not hmac.compare_digest(portal_csrf(token),csrf): raise HTTPException(403,'Refresh this request link')
    with db_conn() as conn:
        # Per-link cap bounds abuse without introducing another service.
        active=conn.execute('SELECT id FROM workspace_portals WHERE id=%s AND NOT revoked AND expires_at>now() FOR UPDATE',(row['id'],)).fetchone()
        if not active: raise HTTPException(404,'Request link expired or unavailable')
        count=conn.execute("SELECT count(*) AS n FROM workspace_portal_messages WHERE portal_id=%s AND author='REQUESTER' AND created_at>now()-interval '1 hour'",(row['id'],)).fetchone()['n']
        if count>=20: raise HTTPException(429,'Please wait before adding more messages')
        conn.execute("INSERT INTO workspace_portal_messages(portal_id,author,body) VALUES(%s,'REQUESTER',%s)",(row['id'],text(body,4000,True)))
    return RedirectResponse('/request-portal/'+token,status_code=303)
