"""Real API writes, PostGIS geometry, tracked n8n code, and delivery reservations.
Runs only on the CI-local database; never publishes or sends a notification.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'ci'))
application, core, ms, calendar, hub, auth, provider, owner, mail_id, cookie, csrf = __import__('microsoft_fixture').setup()
with core.db_conn() as c:
    # GIS datasets are external imports; this small fixture has the same relevant columns.
    c.execute('''CREATE TABLE IF NOT EXISTS gis_addresses(
      objectid integer PRIMARY KEY,fulladdr text,post_comm text,inc_muni text,post_code text,
      pcl_guid text,county text,state text,status text,primarypt text,geom geometry(Point,4326));
      CREATE TABLE IF NOT EXISTS gis_nyc_addresses(LIKE gis_addresses INCLUDING ALL);
      CREATE TABLE IF NOT EXISTS gis_parcels(objectid integer PRIMARY KEY,pcl_guid text,pams_pin text,
      mun_name text,pclblock text,pcllot text,prop_loc text,geom geometry(MultiPolygon,4326));
      ALTER TABLE issues ADD COLUMN IF NOT EXISTS employee_location text;''')
    for prefix in ('014_', '018_', '028_', '030_', '031_', '032_'):
        c.execute(next((ROOT/'deploy/postgis/init').glob(prefix+'*.sql')).read_text())
    c.commit()
import spatial_watch_app as spatial
for route in core.app.routes:
    if getattr(route, 'path', '') == '/watchlist' or getattr(route, 'path', '').startswith('/watchlist/'):
        application.router.routes.append(route)
client = TestClient(application, base_url='https://fixture.example.com')
client.cookies.set(auth.COOKIE_NAME, cookie)
headers = {'Origin': 'https://fixture.example.com'}

def sql(query, params=()):
    with core.db_conn() as c:
        return c.execute(query.replace('$1', '%s'), params).fetchall()

subscribers = [uuid4(), uuid4()]
for index, subscriber in enumerate(subscribers):
    sql('INSERT INTO subscribers(id,subscriber_id,name,ntfy_topic) VALUES(%s,%s,%s,%s) RETURNING id',
        (subscriber, 'MAP_TEST_'+str(subscriber), 'Map Test '+str(index), 'ci-map-'+str(subscriber)))
def post(path, values):
    r = client.post(path, data=values, headers=headers, follow_redirects=False)
    assert r.status_code == 303 and 'error=' not in r.headers['location'], (r.status_code, r.headers, r.text)
    return r
name = 'Any mapped activity '+str(uuid4())
post('/watchlist/create', {'display_name': name, 'match_selection': 'ANY', 'setup_mode': 'TOPIC',
     'location_scope': 'MAP_POINT', 'location_kind': 'MAP_POINT', 'latitude': '40.77', 'longitude': '-74.02',
     'radius_ft': '1000', 'source_filter': 'unknown-source', 'alert_category_filter': 'unknown-category',
     'search_term': 'fire', 'aliases': 'alarm', 'min_priority': '5', 'subscriber_ids': [str(x) for x in subscribers]})
watch = sql('SELECT *,ST_AsEWKT(spatial_target_geom) AS target FROM watch_items WHERE display_name=%s', (name,))[0]
assert watch['nearby_enabled'] and watch['radius_ft'] == 1000 and watch['min_priority'] == 1
assert watch['source_filter'] == watch['alert_category_filter'] == watch['aliases'] == []
assert watch['watch_type'] == 'POINT'
post('/watchlist/'+str(watch['id'])+'/update', {'display_name': name, 'match_selection': 'ANY',
     'setup_mode': 'LOCATION_TOPIC', 'search_term': 'fire', 'aliases': 'alarm', 'source_filter': 'BNN',
     'alert_category_filter': 'TEST', 'min_priority': '5', 'location_scope': 'EXISTING',
     'location_kind': 'EXISTING', 'radius_ft': '1000', 'keep_state': '1',
     'subscriber_ids': [str(x) for x in subscribers]})
saved = sql('SELECT *,ST_AsEWKT(spatial_target_geom) AS target FROM watch_items WHERE id=%s', (watch['id'],))[0]
assert saved['target'] == watch['target'] and saved['radius_ft'] == 1000 and saved['active']
assert saved['source_filter'] == saved['alert_category_filter'] == saved['aliases'] == [] and saved['min_priority'] == 1
bad = client.post('/watchlist/'+str(watch['id'])+'/update', data={
    'display_name': name, 'match_selection': 'ANY', 'location_scope': 'ANYWHERE'}, headers=headers, follow_redirects=False)
assert bad.status_code == 303 and 'error=' in bad.headers['location']
assert sql('SELECT ST_AsEWKT(spatial_target_geom) AS target FROM watch_items WHERE id=%s', (watch['id'],))[0]['target'] == watch['target']
print('WATCH API PASS: create and edit clear stale filters; target, radius, recipients and active state preserved')

# A second area watch reaches the same recipient; the adapter must deduplicate it.
post('/watchlist/create', {'display_name': name+' second', 'match_selection': 'ANY',
     'location_scope': 'MAP_POINT', 'latitude': '40.77', 'longitude': '-74.02', 'radius_ft': '1000',
     'subscriber_ids': str(subscribers[0])})
inside = sql("SELECT ST_AsEWKT(ST_Project(ST_SetSRID(ST_MakePoint(-74.02,40.77),4326)::geography,999.9*0.3048,0)::geometry) AS geom")[0]['geom']
outside = sql("SELECT ST_AsEWKT(ST_Project(ST_SetSRID(ST_MakePoint(-74.02,40.77),4326)::geography,1000.1*0.3048,0)::geometry) AS geom")[0]['geom']
issue_id, outside_id, old_id, unmapped_id = [uuid4() for _ in range(4)]
for item_id, geometry, old in ((issue_id,inside,False),(outside_id,outside,False),(old_id,inside,True),(unmapped_id,None,False)):
    sql('''INSERT INTO issues(id,title,priority,geom,updated_at) VALUES(%s,'Low priority work',1,
       ST_GeomFromEWKT(%s),now()-CASE WHEN %s THEN interval '1 day' ELSE interval '0' END) RETURNING id''',
        (item_id,geometry,old))
event_id, transit_id, managed_id, vehicle_id = [uuid4() for _ in range(4)]
sql('''INSERT INTO event_intelligence(id,source_event_key,fingerprint,title,description,change_hash,
       impact_level,geom) VALUES(%s,%s,%s,'Awareness event','All events count','fixture','AWARENESS',ST_GeomFromEWKT(%s)) RETURNING id''',
    (event_id,str(event_id),str(event_id),inside))
integration = sql('SELECT id FROM integrations LIMIT 1')[0]['id']
provider_id = sql("SELECT id FROM transit_providers WHERE provider_key='NJ_TRANSIT'")[0]['id']
sql('''INSERT INTO transit_observations(id,source_integration_id,provider_id,external_key,fingerprint,
       title,change_hash,impact_level,mode,geom) VALUES(%s,%s,%s,%s,%s,'Transit watch','fixture','WATCH','BUS',ST_GeomFromEWKT(%s)) RETURNING id''',
    (transit_id,integration,provider_id,str(transit_id),str(transit_id),inside))
address = 'CI managed event '+str(managed_id)
sql("INSERT INTO gis_addresses(objectid,fulladdr,status,geom) VALUES(9999001,%s,'A',ST_GeomFromEWKT(%s)) ON CONFLICT(objectid) DO UPDATE SET fulladdr=EXCLUDED.fulladdr,geom=EXCLUDED.geom RETURNING objectid",(address,inside))
sql('''INSERT INTO operational_events(id,title,starts_at,address) VALUES(%s,'Managed event',now(),%s) RETURNING id''', (managed_id,address))
sql('''INSERT INTO transit_assets(id,provider_id,asset_key,asset_type,name,mode,geom)
       VALUES(%s,%s,%s,'VEHICLE','Live bus','BUS',ST_GeomFromEWKT(%s)) RETURNING id''', (vehicle_id,provider_id,str(vehicle_id),inside))
before = sql('SELECT count(*) AS n FROM alerts')[0]['n']
for kind, ident in [('EVENT_INTELLIGENCE',event_id),('TRANSIT',transit_id)]:
    r = client.get('/watchlist', params={'from_record':kind,'record_id':str(ident)})
    assert r.status_code == 200 and '<details class="panel watch-create-panel" open>' in r.text, r.text[:500]
    assert 'value="40.' in r.text and 'STARTED FROM INTELLIGENCE' in r.text
assert sql('SELECT count(*) AS n FROM alerts')[0]['n'] == before
r = client.get('/watchlist', params={'latitude':'40.77','longitude':'-74.02','setup_mode':'LOCATION'})
assert r.status_code == 200 and '<details class="panel watch-create-panel" open>' in r.text
assert '<option value="ANY" selected>Any mapped activity' in r.text
print('WATCH ENTRY PASS: Intel without an alert and exact-point Map links open editable drafts without writes')

rematch = json.loads((ROOT/'workflows/core/CORE_Resolved_Spatial_Rematch_v1.json').read_text())
nodes = {n['name']:n for n in rematch['nodes']}
load_activity = nodes['Load Mapped Activity']['parameters']['query']
assert load_activity == (ROOT/'deploy/n8n/mapped_activity.sql').read_text()
activation = (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
load_activity = load_activity.replace('__CMOS_ACTIVATED_AT__',activation)
mark = nodes['Mark Mapped Activity']['parameters']['query']
payloads = [row['alert'] for row in sql(load_activity)]
by_id = {p['metadata']['mapped_record_id']:p for p in payloads}
expected = {str(x) for x in [issue_id,event_id,transit_id,managed_id,vehicle_id]}
assert expected <= by_id.keys(), (expected,by_id.keys())
assert not {str(outside_id),str(old_id),str(unmapped_id)} & by_id.keys()
print('MAPPED SQL PASS: 999.9 ft inside; 1000.1 ft outside; P1 work, Awareness events, managed events, transit and live vehicles included; old/unmapped records excluded')

workflow = json.loads((ROOT/'workflows/live/CORE_Watchlist_Matcher_live.json').read_text())
workflow = workflow[0] if isinstance(workflow,list) else workflow
matcher = {n['name']:n for n in workflow['nodes']}
guard = json.loads((ROOT/'workflows/core/CORE_Delivery_Guard_Logger_v1.json').read_text())
guard = guard[0] if isinstance(guard,list) else guard
reserve = next(n for n in guard['nodes'] if n['name']=='Reserve Delivery')['parameters']['query']
def js(code, alert, rows):
    script = "const d=JSON.parse(require('fs').readFileSync(0,'utf8'));const run=new Function('$','$input',d.code);const out=run(()=>({first:()=>({json:d.alert})}),{all:()=>d.rows.map(json=>({json})),first:()=>({json:d.alert})});process.stdout.write(JSON.stringify(out));"
    result = subprocess.run(['node','-e',script], input=json.dumps({'code':code,'alert':alert,'rows':rows}),text=True,capture_output=True,check=True)
    return json.loads(result.stdout)[0]['json']
for payload in [by_id[x] for x in expected]:
    normalized = js(matcher['Normalize Alert']['parameters']['jsCode'],payload,[])
    rows = sql(matcher['Load Active Watchlist + Recipients']['parameters']['query'], (Jsonb(normalized),))
    result = js(matcher['Match + Resolve Recipients']['parameters']['jsCode'],normalized,rows)
    assert watch['watch_id'] in result['matched_watch_ids'], result
    deliveries = [p for p in result['delivery_payloads'] if p['subscriber_id'].startswith('MAP_TEST_')]
    assert len(deliveries) == 2, deliveries
    persisted = sql(matcher['Persist Watch Matches']['parameters']['query'], (Jsonb(result),))[0]
    assert persisted['persisted_match_count'] >= 2
    for delivery in deliveries:
        delivery.update(event_action=normalized['event_action'],alert_status=normalized['status'])
        reservation = sql(reserve,(Jsonb(delivery),))[0]
        assert reservation['should_send']
        retry = sql(reserve,(Jsonb(delivery),))[0]
        assert not retry['should_send'], retry
    assert not sql(mark,(Jsonb(payload),)), 'Pending sends must not be marked completed'
    sql("UPDATE deliveries SET status='FAILED' WHERE alert_id=(SELECT id FROM alerts WHERE alert_id=%s) RETURNING id",(payload['alert_id'],))
    assert not sql(mark,(Jsonb(payload),)), 'Failed sends must remain retryable'
    for delivery in deliveries:
        assert sql(reserve,(Jsonb(delivery),))[0]['should_send']
    sql("UPDATE deliveries SET status='SENT',sent_at=now() WHERE alert_id=(SELECT id FROM alerts WHERE alert_id=%s) RETURNING id",(payload['alert_id'],))
    assert sql(mark,(Jsonb(payload),))
    assert not sql(mark,(Jsonb({**payload,'metadata':{**payload['metadata'],'mapped_activity_candidate':'stale'}}),))
assert not expected & {p['alert']['metadata']['mapped_record_id'] for p in sql(load_activity)}
sql("UPDATE issues SET title='Meaningful work update',updated_at=now() WHERE id=%s RETURNING id",(issue_id,))
assert str(issue_id) in {p['alert']['metadata']['mapped_record_id'] for p in sql(load_activity)}
print('WATCH PIPELINE PASS: actual Normalize/SQL/matcher/audit/Delivery Guard; two watches deduplicate two recipients, pending sends suppress retries, failed sends retry, completed unchanged records stay quiet, meaningful changes re-evaluate')

# Existing alerts newly mapped later are eligible even if originally received days ago.
late_id = 'CI:LATE:'+str(uuid4())
sql("INSERT INTO alerts(alert_id,source,category,subtype,status,event_action,title,message,priority,received_at) VALUES(%s,'BNN','INCIDENT','TEST','ACTIVE','NEW','Old alert mapped now','Mapped later',1,now()-interval '3 days') RETURNING id",(late_id,))
sql("INSERT INTO geo_entity_resolutions(entity_type,entity_id,status,geom) SELECT 'ALERT',id::text,'RESOLVED',ST_GeomFromEWKT(%s) FROM alerts WHERE alert_id=%s RETURNING id",(inside,late_id))
late_query = nodes['Load Newly Resolved Alerts']['parameters']['query'].replace('__CMOS_ACTIVATED_AT__',activation)
late_payload = next(r['alert'] for r in sql(late_query) if r['alert']['alert_id']==late_id)
late_mark = nodes['Mark Resolved Alert Rematched']['parameters']['query']
sql(late_mark,(Jsonb(late_payload),))
assert late_id not in [r['alert']['alert_id'] for r in sql(late_query)]
sql("UPDATE geo_entity_resolutions SET geom=ST_GeomFromEWKT(%s),updated_at=now() WHERE entity_id=(SELECT id::text FROM alerts WHERE alert_id=%s) RETURNING id",(outside,late_id))
assert late_id in [r['alert']['alert_id'] for r in sql(late_query)]
print('LATE MAPPING PASS: old alerts enter matching when mapped; changed geometry is reconsidered; unchanged geometry is not repeatedly rematched')

# Selectable multi-term rules use the same Watch rows, with no special BNN matcher.
sys.path.insert(0,str(ROOT/'dashboard/tests'))
from test_watch_terms import TERMS
term_name = 'Selectable BNN terms '+str(uuid4())
post('/watchlist/create', {'display_name':term_name,'setup_mode':'TOPIC','location_scope':'ANYWHERE',
    'source_filter':'BNN','search_term':TERMS,'word_mode':'WORD','term_list':'1',
    'subscriber_ids':str(subscribers[0])})
term_watch = sql('SELECT * FROM watch_items WHERE display_name=%s',(term_name,))[0]
assert term_watch['match_mode']=='WORD' and term_watch['source_filter']==['BNN']
assert len(term_watch['aliases'])==33 and term_watch['aliases'][-1]=='overturned auto'
post('/watchlist/'+str(term_watch['id'])+'/update',{'display_name':term_name,'setup_mode':'LOCATION_TOPIC',
    'source_filter':'','location_scope':'MAP_POINT','latitude':'40.77','longitude':'-74.02',
    'search_term':'working fire\nTIER \\d','word_mode':'WORD','term_list':'1','keep_state':'1',
    'subscriber_ids':str(subscribers[1])})
changed = sql('SELECT * FROM watch_items WHERE id=%s',(term_watch['id'],))[0]
assert changed['watch_type']=='LOCATION_TOPIC' and changed['match_mode']=='WORD'
assert changed['aliases']==[r'TIER \d'] and changed['source_filter']==[]
recipients = sql('SELECT subscriber_id FROM watch_item_recipients WHERE watch_item_id=%s AND active',(term_watch['id'],))
assert [r['subscriber_id'] for r in recipients]==[subscribers[1]]
print('WATCH TERMS API PASS: all terms retained; source, words, matching mode, area and recipient freely editable')
multi_name = 'Multiple sources only '+str(uuid4())
post('/watchlist/create',{'display_name':multi_name,'setup_mode':'TOPIC','location_scope':'ANYWHERE',
    'source_filter':'BNN,OPERATIONS','subscriber_ids':str(subscribers[0])})
multi = sql('SELECT * FROM watch_items WHERE display_name=%s',(multi_name,))[0]
assert multi['watch_type']=='SOURCE' and multi['match_mode']=='FIELD' and multi['aliases']==['OPERATIONS']
small = {k:multi[k] for k in ('watch_type','search_term','aliases','match_mode','match_field','source_filter','min_priority')}
script="const d=JSON.parse(require('fs').readFileSync(0,'utf8'));const m=require(process.argv[1]);for(const source of ['BNN','OPERATIONS','OTHER']){if(m.evaluateWatch({source,priority:1},d).matched!==(source!=='OTHER'))throw Error(source);}"
subprocess.run(['node','-e',script,str(ROOT/'dashboard/static/watch_matcher.js')],input=json.dumps(small),text=True,check=True)
print('MULTIPLE SOURCES PASS: source-only Watch accepts each selected source and rejects other sources')

# The central sender reads one dynamic global setting and preserves all audit data.
sender = {n['name']:n for n in json.loads((ROOT/'workflows/core/CORE_ntfy_Sender_v1.json').read_text())['nodes']}
original_settings = sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']
for show in (False,True):
    response = client.post('/watchlist/notification-settings',data={'explanations':'on'} if show else {},headers=headers,follow_redirects=False)
    assert response.status_code==303
    settings = sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']
    assert settings=={**original_settings,'notification_explanations':show}
    bundle={'delivery_payloads':[{'ntfy_topic':'ci-only-'+source,'subscriber_id':source,'source':source,
        'message':'Original body','match_reasons':['CONTAINS search_text matched search_term "working fire"']}
        for source in ('BNN','OTHER')]}
    loaded=sql(sender['Load Notification Options']['parameters']['query'],(Jsonb(bundle),))[0]
    code=sender['Prepare ntfy Requests']['parameters']['jsCode']
    script="const d=JSON.parse(require('fs').readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(new Function('$input',d.code)({first:()=>({json:d.input})})));"
    output=json.loads(subprocess.run(['node','-e',script],input=json.dumps({'code':code,'input':loaded}),text=True,capture_output=True,check=True).stdout)
    assert len(output)==2
    for item in output:
        assert ('Why you received this:' in item['json']['ntfy_body']['message']) is show
        assert item['json']['match_reasons']==bundle['delivery_payloads'][0]['match_reasons']
        if not show:assert item['json']['ntfy_body']['message']=='Original body'
os.environ.update(CMOS_READONLY_USERNAME='ReadOnly',CMOS_READONLY_PASSWORD_HASH='unused-test-hash')
readonly = auth._issue_session(auth.Account('ReadOnly','READ_ONLY',''))
denied = client.post('/watchlist/notification-settings',data={'explanations':'off'},headers={**headers,'Cookie':auth.COOKIE_NAME+'='+readonly},follow_redirects=False)
assert denied.status_code==403,(denied.status_code,denied.text)
assert sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']['notification_explanations'] is True
print('GLOBAL NOTIFICATION OPTIONS PASS: real settings + central sender; all sources/recipients on/off, original body and evidence retained, read-only denied, no send')

# Exercise the actual new form behavior in both supported browser engines.
import socket
import threading
import time
import uvicorn
from playwright.sync_api import sync_playwright

with socket.socket() as sock:
    sock.bind(('127.0.0.1',0))
    port = sock.getsockname()[1]
base = 'http://127.0.0.1:'+str(port)
os.environ['CMOS_PUBLIC_ORIGIN'] = base
server = uvicorn.Server(uvicorn.Config(application,host='127.0.0.1',port=port,log_level='error'))
thread = threading.Thread(target=server.run,daemon=True)
thread.start()
for _ in range(100):
    if server.started:
        break
    time.sleep(.05)
assert server.started
try:
    with sync_playwright() as browsers:
        for engine in ('firefox','chromium'):
            browser = getattr(browsers,engine).launch()
            context = browser.new_context(viewport={'width':1000,'height':900})
            context.add_cookies([{'name':auth.COOKIE_NAME,'value':cookie,'url':base}])
            page = context.new_page()
            errors=[]
            page.on('pageerror',lambda error: errors.append(str(error)))
            page.goto(base+'/watchlist?from_record=EVENT_INTELLIGENCE&record_id='+str(event_id))
            form = page.locator('form[action="/watchlist/create"]')
            assert form.is_visible()
            original_lat = form.locator('[name="latitude"]').input_value()
            original_lon = form.locator('[name="longitude"]').input_value()
            form.get_by_text('More options',exact=True).click()
            form.locator('[name="source_filter"]').fill('stale-source')
            form.locator('[name="search_term"]').fill('stale-topic')
            form.locator('[name="aliases"]').fill('stale-alias')
            form.locator('[name="alert_category_filter"]').fill('stale-category')
            form.locator('[name="min_priority"]').select_option('5')
            form.locator('[name="match_selection"]').select_option('ANY')
            assert form.locator('[name="search_term"]').is_disabled()
            assert not form.locator('[name="search_term"]').is_visible()
            assert form.locator('[name="min_priority"]').is_disabled()
            form.locator('[name="radius_ft"]').select_option('1000')
            browser_name = engine+' area Watch '+str(uuid4())
            form.locator('[name="display_name"]').fill(browser_name)
            for sub in subscribers:
                form.locator('[name="subscriber_ids"][value="'+str(sub)+'"]').check()
            form.get_by_role('button',name='Turn On Watch',exact=True).click()
            page.wait_for_url('**/watchlist?msg=*')
            created = sql('SELECT *,ST_AsEWKT(spatial_target_geom) AS target FROM watch_items WHERE display_name=%s',(browser_name,))[0]
            assert created['min_priority']==1 and created['source_filter']==created['alert_category_filter']==created['aliases']==[]
            assert abs(created['latitude']-float(original_lat))<1e-9 and abs(created['longitude']-float(original_lon))<1e-9
            page.goto(base+'/watchlist?focus='+str(created['id']))
            edit = page.locator('form[action="/watchlist/'+str(created['id'])+'/update"]')
            assert edit.locator('[name="match_selection"]').input_value()=='ANY'
            edit.locator('[name="radius_ft"]').select_option('500')
            edit.get_by_role('button',name='Save Changes',exact=True).click()
            page.wait_for_url('**/watchlist?msg=*')
            changed = sql('SELECT *,ST_AsEWKT(spatial_target_geom) AS target FROM watch_items WHERE id=%s',(created['id'],))[0]
            assert changed['target']==created['target'] and changed['radius_ft']==500 and changed['active']
            page.goto(base+'/watchlist?from_alert='+late_id+'&setup_mode=LOCATION')
            form = page.locator('form[action="/watchlist/create"]')
            assert form.locator('[name="match_selection"]').input_value()=='ANY'
            assert form.locator('[name="search_term"]').input_value()==''
            page.goto(base+'/watchlist?previewed=1&setup_mode=LOCATION&location_kind=COUNTY&location_id=Hudson&location_query=Hudson&source_filter=BNN&min_priority=4&match_mode=FIELD&match_field=county')
            form = page.locator('form[action="/watchlist/create"]')
            assert form.locator('[name="match_selection"]').input_value()=='FILTERED'
            assert form.locator('[name="source_filter"]').input_value()=='BNN'
            assert form.locator('[name="min_priority"]').input_value()=='4'
            assert form.locator('[name="match_mode"]').input_value()=='FIELD'
            from urllib.parse import urlencode
            draft = urlencode({'create':'1','setup_mode':'TOPIC','source_filter':'BNN',
                'search_term':TERMS,'match_mode':'WORD'})
            page.goto(base+'/watchlist?'+draft)
            form = page.locator('form[action="/watchlist/create"]')
            assert form.locator('[name="word_mode"]').input_value()=='WORD'
            assert form.locator('[name="location_scope"]').input_value()=='ANYWHERE'
            assert form.locator('[name="subscriber_ids"]:checked').count()==0
            form.get_by_text('More options',exact=True).click()
            draft_name = engine+' selectable terms '+str(uuid4())
            form.locator('[name="display_name"]').fill(draft_name)
            form.get_by_role('button',name='Save Paused',exact=True).click()
            page.wait_for_url('**/watchlist?msg=*')
            draft_watch = sql('SELECT * FROM watch_items WHERE display_name=%s',(draft_name,))[0]
            assert not draft_watch['active'] and len(draft_watch['aliases'])==33
            page.goto(base+'/watchlist?focus='+str(draft_watch['id']))
            edit = page.locator('form[action="/watchlist/'+str(draft_watch['id'])+'/update"]')
            assert len(edit.locator('[name="search_term"]').input_value().splitlines())==34
            edit.locator('[name="source_filter"]').fill('')
            edit.locator('[name="search_term"]').fill('mayday\ntier \\d')
            edit.locator('[name="word_mode"]').select_option('CONTAINS')
            edit.locator('[name="subscriber_ids"][value="'+str(subscribers[1])+'"]').check()
            edit.get_by_role('button',name='Save Changes',exact=True).click()
            page.wait_for_url('**/watchlist?msg=*')
            modified = sql('SELECT * FROM watch_items WHERE id=%s',(draft_watch['id'],))[0]
            assert modified['source_filter']==[] and modified['search_term']=='mayday'
            assert modified['aliases']==[r'tier \d'] and modified['match_mode']=='CONTAINS' and not modified['active']
            page.locator('#notification-options summary').click()
            options = page.locator('form[action="/watchlist/notification-settings"]')
            options.locator('[name="explanations"]').uncheck()
            options.get_by_role('button',name='Save notification options',exact=True).click()
            page.wait_for_url('**/watchlist?msg=*')
            assert sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']['notification_explanations'] is False
            page.locator('#notification-options summary').click()
            options = page.locator('form[action="/watchlist/notification-settings"]')
            options.locator('[name="explanations"]').check()
            options.get_by_role('button',name='Save notification options',exact=True).click()
            page.wait_for_url('**/watchlist?msg=*')
            assert sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']['notification_explanations'] is True
            assert not errors,errors
            print('WATCH BROWSER PASS:',engine,'Intel/Map/Alert drafts, all activity, selectable 34 terms, source/location/recipient controls, paused save/edit, global explanation on/off')
            context.close()
            browser.close()
finally:
    server.should_exit=True
    thread.join(timeout=5)
