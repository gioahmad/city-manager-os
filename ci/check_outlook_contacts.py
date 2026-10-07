"""Real SQL and authenticated API checks; only Microsoft HTTP is replaced."""
from microsoft_fixture import setup
from uuid import uuid4
import json
from fastapi.testclient import TestClient
import microsoft_contacts as contacts

application,core,ms,calendar,hub,auth,provider,owner,mail_id,cookie,csrf=setup()
base='/workspace/api/microsoft/'
with TestClient(application,base_url='https://fixture.example.com') as client:
    client.cookies.set(auth.COOKIE_NAME,cookie)
    assert 'Find a contact in Outlook' in client.get('/contacts').text
    assert client.post(base+'contacts/preview',json={'provider_key':'contact-jane'}).status_code==403
    listed=client.get(base+'contacts?live=true&q=Jane').json()
    assert listed['items'][0]['name']=='Jane Fixture'
    assert client.get(base+'contacts?live=true&cursor=https://evil.example/v1.0/me/contacts').status_code==400
    preview=client.post(base+'contacts/preview',json={'csrf':csrf,'provider_key':'contact-jane'}).json()
    fields={k:'\n'.join(v) if isinstance(v,list) else v for k,v in preview['fields'].items()}
    fields['name']='Jane Imported Fixture'
    data={'csrf':csrf,'provider_key':'contact-jane','version':preview['version'],'fields':fields,'create_separate':True}
    response=client.post(base+'contacts/import',json=data);assert response.status_code==200,response.text
    contact_id=response.json()['contact_id']
    retry=client.post(base+'contacts/import',json=data);assert retry.json()['contact_id']==contact_id
    with core.db_conn() as c:
        local=c.execute('SELECT * FROM contacts WHERE id=%s',(contact_id,)).fetchone()
        assert local['visibility']=='PRIVATE' and local['owner_username']==owner
        assert local['phones']==['+12015551234'] and local['emails']==['jane@example.com']
        c.execute('UPDATE contacts SET title=%s WHERE id=%s',('Director',contact_id))
    def prepare():
        r=client.post(base+'prepare',json={'csrf':csrf,'operation':'CONTACT_UPDATE','contact_id':contact_id,'request_id':str(uuid4())})
        assert r.status_code==200,r.text
        return r.json()
    before=len(provider.writes)
    op=prepare();assert len(provider.writes)==before
    changes={r['field']:r for r in op['review']['changes']}
    assert changes['jobTitle']['before']=='Coordinator' and changes['jobTitle']['after']=='Director'
    assert 'businessAddress' not in changes and 'mobilePhone' not in changes
    bad=client.post(base+'execute',json={'csrf':csrf,'operation_id':op['id']});assert bad.status_code==400
    result=client.post(base+'execute',json={'csrf':csrf,'operation_id':op['id'],'confirmed':True})
    assert result.json()['status']=='SUCCEEDED',result.text
    assert len(provider.writes)==before+1 and provider.contacts['contact-jane']['jobTitle']=='Director'
    assert provider.contacts['contact-jane']['mobilePhone']=='201-555-1234'
    assert client.post(base+'execute',json={'csrf':csrf,'operation_id':op['id'],'confirmed':True}).json()['status']=='SUCCEEDED'
    assert len(provider.writes)==before+1
    with core.db_conn() as c:c.execute('UPDATE contacts SET title=%s WHERE id=%s',('Deputy',contact_id))
    op=prepare();provider.contacts['contact-jane']['changeKey']='external-change'
    with core.db_conn() as c:
        stored=c.execute('SELECT payload FROM workspace_microsoft_operations WHERE id=%s',(op['id'],)).fetchone()
    executable=json.loads(calendar.cipher().decrypt(stored['payload'].encode()))
    assert executable['json']['displayName']==provider.contacts['contact-jane']['displayName']
    result=client.post(base+'execute',json={'csrf':csrf,'operation_id':op['id'],'confirmed':True}).json()
    assert result['status']=='FAILED' and len(provider.writes)==before+1
    op=prepare()
    with core.db_conn() as c:c.execute('UPDATE contacts SET title=%s WHERE id=%s',('Chief',contact_id))
    result=client.post(base+'execute',json={'csrf':csrf,'operation_id':op['id'],'confirmed':True}).json()
    assert result['status']=='FAILED' and len(provider.writes)==before+1
    op=prepare();provider.outcome='timeout'
    result=client.post(base+'execute',json={'csrf':csrf,'operation_id':op['id'],'confirmed':True}).json()
    assert result['status']=='UNKNOWN' and len(provider.writes)==before+2
    client.post(base+'execute',json={'csrf':csrf,'operation_id':op['id'],'confirmed':True})
    assert len(provider.writes)==before+2;provider.outcome='ok'
    provider.contacts['contact-copy']={**provider.contacts['contact-jane'],'id':'contact-copy'}
    duplicate=client.post(base+'contacts/preview',json={'csrf':csrf,'provider_key':'contact-copy'}).json()
    assert duplicate['matches']
    duplicate_data={**data,'provider_key':'contact-copy','version':duplicate['version'],'create_separate':False}
    assert client.post(base+'contacts/import',json=duplicate_data).status_code==409
    # Link survives disconnect, but cannot be used under a different connected mailbox.
    with core.db_conn() as c:
        c.execute('UPDATE workspace_calendar_connections SET account_email=%s WHERE owner_username=%s',('different@example.com',owner))
    assert client.get(base+'contacts/'+contact_id+'/sync').status_code==404
    with core.db_conn() as c:
        c.execute('UPDATE workspace_calendar_connections SET account_email=%s,scopes=%s WHERE owner_username=%s',(provider.mailbox,calendar.SCOPE,owner))
    assert client.post(base+'prepare',json={'csrf':csrf,'operation':'CONTACT_UPDATE','contact_id':contact_id,'request_id':str(uuid4())}).status_code==403
    client.cookies.set(auth.COOKIE_NAME,auth._issue_session(auth.Account('Reader','READ_ONLY','')))
    assert client.post(base+'contacts/import',json=data).status_code==403
    assert client.get(base+'contacts/'+contact_id+'/sync').status_code==404
print('OUTLOOK CONTACTS PASS: private import, cleanup, duplicate match, exact review, single PATCH, stale data, account pinning, CSRF, permissions and uncertain outcome')
