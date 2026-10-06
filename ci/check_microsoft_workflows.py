"""Actual SQL write/reopen, ownership, permissions and exact-once-attempt checks.
Only Microsoft HTTP responses are substituted. Every internal mutation is real.
"""
from microsoft_fixture import setup
from uuid import uuid4
from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from fastapi import HTTPException
import json
application,core,ms,calendar,hub,auth,provider,owner,mail_id,cookie,csrf=setup()
count=0
def passed(message):
    global count;count+=1;print('MICROSOFT WORKFLOW PASS:',message,flush=True)
def error(fn,status):
    try:fn()
    except HTTPException as e:assert e.status_code==status,(e.status_code,e.detail)
    else:raise AssertionError('Expected rejection '+str(status))
with TestClient(application) as client:
    assert client.get('/email',follow_redirects=False).status_code==303
    assert client.post('/workspace/api/microsoft/execute',json={}).status_code==401
    client.cookies.set(auth.COOKIE_NAME,cookie)
    assert client.post('/workspace/api/microsoft/capture',json={'csrf':'wrong'}).status_code==403
    for path in ['/email','/calendar','/important']:
        r=client.get(path);assert r.status_code==200 and r.text.count('class="cmos-rail"')==1
        assert r.headers['x-frame-options']=='DENY' and "frame-ancestors 'none'" in r.headers['content-security-policy']
    client.cookies.set(auth.COOKIE_NAME,auth._issue_session(auth.Account('Reader','READ_ONLY','')))
    assert client.post('/workspace/api/microsoft/prepare',json={'csrf':csrf}).status_code==403
    client.cookies.set(auth.COOKIE_NAME,cookie)
    passed('Real authentication, CSRF, read-only role and all three shared-navigation pages')
    detail=client.get('/workspace/api/hub/detail/MAIL/'+mail_id)
    assert detail.status_code==200 and detail.json()['item']['id']==mail_id
    assert detail.json()['item']['title']=='Fixture contractor email'
    assert client.get('/workspace/api/microsoft/status').status_code==200
    passed('Actual source-detail query and connection status work against real contact and workspace schemas')
    base={'kind':'MAIL','id':mail_id,'request_id':str(uuid4()),'destination':'TASK','title':'Reviewed private follow-up','due_date':'2026-12-01'}
    first=ms.capture(owner,base);second=ms.capture(owner,base);assert first==second
    row=hub.find(owner,'TASK',first['id']);assert row['title']==base['title'] and row['visibility']=='PRIVATE'
    error(lambda:hub.find('other-owner','TASK',first['id']),404)
    error(lambda:ms.capture('other-owner',{**base,'request_id':str(uuid4())}),404)
    error(lambda:ms.capture(owner,{**base,'title':'Changed payload'}),409)
    passed('Private task saved, reopened through real source query, retry deduplicated, other owner blocked')
    work={**base,'request_id':str(uuid4()),'destination':'WORK','body':'Only reviewed shared content.','share_confirmed':True,'priority':4,'assigned_to':'Fixture Responsible','next_action':'Arrange visit.'}
    saved=ms.capture(owner,work)
    with core.db_conn() as c:
        row=c.execute('SELECT * FROM issues WHERE id=%s',(saved['id'],)).fetchone()
        assert row['description']==work['body'] and row['assigned_to']=='Fixture Responsible' and row['priority']==4 and row['due_at'].hour==22
    assert hub.find(owner,'WORK',saved['id'])['title']==work['title']
    assert ms.capture(owner,{**work,'request_id':str(uuid4())})['id']==saved['id']
    error(lambda:ms.capture(owner,{**work,'request_id':str(uuid4()),'share_confirmed':False}),400)
    passed('Shared work requires review; owner, due date, priority, content and source link persist; source promotion not duplicated')
    event={**base,'request_id':str(uuid4()),'destination':'EVENT','title':'Reviewed site appointment','body':'Reviewed event notes','share_confirmed':True,'starts_at':'2026-12-01T10:00','ends_at':'2026-12-01T11:00','timezone':'America/New_York','location':'Test site'}
    ev=ms.capture(owner,event);reopened=hub.find(owner,'EVENT',ev['id']);assert reopened['title']==event['title'] and 'Reviewed event notes' in reopened['body'] and datetime.fromisoformat(reopened['metadata']['starts_at']).hour==15
    error(lambda:ms.capture(owner,{**event,'request_id':str(uuid4()),'ends_at':'2026-11-01T09:00'}),400)
    passed('Internal event validates times and reopens with reviewed title, notes and time zone')
    concurrent={**base,'request_id':str(uuid4()),'title':'Concurrent follow-up'}
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:ms.capture(owner,concurrent),range(2)))
    assert results[0]['id']==results[1]['id']
    passed('Concurrent internal save uses one durable receipt and one record')
    # Real marking and filtering; clearing an empty import snapshot must retain important mail.
    assert client.post('/workspace/api/microsoft/important',json={'csrf':csrf,'kind':'MAIL','id':mail_id,'important':True}).status_code==200
    marked=client.get('/workspace/api/microsoft/items?section=important').json();assert any(r['id']==mail_id for r in marked['items'])
    with core.db_conn() as c:calendar.replace_mail(c,owner,[])
    assert hub.find(owner,'MAIL',mail_id)['id']
    passed('Important flag persists and protects a retained source from rolling Inbox pruning')
    # Provider-owned calendar selection; no database substitution.
    now=datetime.now(timezone.utc)
    window=ms.calendar_window(owner,'project-calendar',(now-timedelta(days=1)).isoformat(),(now+timedelta(days=2)).isoformat())
    assert window['fresh'] and window['items'] and len(window['calendars'])==2
    cal_id=str(window['items'][0]['id']);assert hub.find(owner,'CALENDAR',cal_id)['metadata']['calendar_key']=='project-calendar'
    calendar.sync(owner);assert hub.find(owner,'CALENDAR',cal_id)['id']
    provider.read_failure=True
    stale=ms.calendar_window(owner,'project-calendar',(now-timedelta(days=1)).isoformat(),(now+timedelta(days=2)).isoformat());provider.read_failure=False
    assert not stale['fresh'] and stale['items']
    passed('Additional owned calendar imported with stable local ID; worker preserves it; outage explicitly reports stale data')
    send={'operation':'MAIL_SEND','request_id':str(uuid4()),'mode':'new','to':'recipient@example.com','cc':'copy@example.com','bcc':'blind@example.com','title':'Fixture only','body':'Reviewed exact message','source_kind':'MAIL','source_id':mail_id}
    # Rights enforced before even a provider write.
    with core.db_conn() as c:c.execute('UPDATE workspace_calendar_connections SET scopes=%s WHERE owner_username=%s',(calendar.SCOPE,owner))
    error(lambda:ms.prepare(owner,send),403);assert len(provider.writes)==0
    with core.db_conn() as c:c.execute('UPDATE workspace_calendar_connections SET scopes=%s WHERE owner_username=%s',(calendar.WRITE_SCOPE,owner))
    op=ms.prepare(owner,send);assert op['status']=='REVIEW' and not provider.writes
    assert op['review']['to']==['recipient@example.com'] and op['review']['body']==send['body']
    error(lambda:ms.execute_operation(owner,{'operation_id':op['id']}),400)
    error(lambda:ms.execute_operation('other-owner',{'operation_id':op['id'],'confirmed':True}),404)
    result=ms.execute_operation(owner,{'operation_id':op['id'],'confirmed':True})
    assert result['status']=='SUCCEEDED' and result['result']['accepted'] and len(provider.writes)==1
    assert provider.writes[-1]['body']['message']['body']['content']==send['body']
    ms.execute_operation(owner,{'operation_id':op['id'],'confirmed':True});assert len(provider.writes)==1
    passed('Read-only consent cannot send; exact review and explicit owner confirmation precede one accepted send, never a delivery claim')
    reply=ms.prepare(owner,{**send,'request_id':str(uuid4()),'mode':'reply','to':'ignored@example.com','operation':'MAIL_DRAFT'})
    assert reply['review']['to']==['reply-desk@example.com']
    result=ms.execute_operation(owner,{'operation_id':reply['id'],'confirmed':True});assert result['status']=='SUCCEEDED' and 'Not sent' in result['result']['message']
    assert provider.writes[-1]['path'].endswith('/createReply')
    passed('Reply draft respects verified Microsoft Reply-To and saves without sending')
    provider.outcome='timeout';unknown=ms.prepare(owner,{**send,'request_id':str(uuid4())});before=len(provider.writes)
    result=ms.execute_operation(owner,{'operation_id':unknown['id'],'confirmed':True});assert result['status']=='UNKNOWN'
    ms.execute_operation(owner,{'operation_id':unknown['id'],'confirmed':True});assert len(provider.writes)==before+1;provider.outcome='ok'
    passed('Timeout after possible acceptance is UNKNOWN; replaying the same operation cannot resend')
    race=ms.prepare(owner,{**send,'request_id':str(uuid4())});before=len(provider.writes)
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:ms.execute_operation(owner,{'operation_id':race['id'],'confirmed':True}),range(2)))
    assert len(provider.writes)==before+1
    passed('Two simultaneous confirmations produce at most one Microsoft write attempt')
    event_data={**event,'operation':'CALENDAR_CREATE','source_kind':'EVENT','source_id':ev['id'],'request_id':str(uuid4()),'calendar_key':'project-calendar','attendees':'guest@example.com','send_invitations':False}
    error(lambda:ms.prepare(owner,event_data),400)
    event_data['send_invitations']=True;op=ms.prepare(owner,event_data)
    assert op['review']['calendar']=='Project calendar' and op['review']['attendees']==['guest@example.com']
    result=ms.execute_operation(owner,{'operation_id':op['id'],'confirmed':True});assert result['status']=='SUCCEEDED'
    assert provider.writes[-1]['body']['transactionId']==event_data['request_id'] and provider.writes[-1]['body']['start']['timeZone']=='UTC'
    error(lambda:ms.prepare(owner,{**event_data,'request_id':str(uuid4()),'source_kind':'CALENDAR','source_id':cal_id}),409)
    passed('Selected-calendar create requires invitation decision, uses transaction ID, and rejects copying an appointment to its existing calendar')
    changed=ms.prepare(owner,{**send,'request_id':str(uuid4())});before=len(provider.writes);provider.mailbox='different@example.com'
    result=ms.execute_operation(owner,{'operation_id':changed['id'],'confirmed':True});assert result['status']=='FAILED' and len(provider.writes)==before;provider.mailbox='fixture@example.com'
    passed('Mailbox identity change after review blocks sending against the wrong account')
    print(f'MICROSOFT WORKFLOWS: PASS ({count} checks; real PostgreSQL storage, simulated Microsoft only)',flush=True)
