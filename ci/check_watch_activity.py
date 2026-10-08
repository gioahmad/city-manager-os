"""Real API writes, PostGIS geometry, tracked n8n code, and delivery reservations.
Runs only on the CI-local database; never publishes or sends a notification.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import urlopen
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT/'browser-check-results'
ARTIFACTS.mkdir(exist_ok=True)
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
import map_app
import operations_app
for route in core.app.routes:
    if getattr(route, 'path', '') in {'/watchlist','/watch-preview','/map','/api/watch-lab/evaluate','/alerts','/alerts/appearance'} or getattr(route, 'path', '').startswith('/watchlist/'):
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

# Per-source appearance changes presentation while the actual matcher keeps its rules/evidence.
from alert_appearance import APPEARANCE_DEFAULTS
from pseg_engine import build_hudson_alert
sender = {n['name']:n for n in json.loads((ROOT/'workflows/core/CORE_ntfy_Sender_v1.json').read_text())['nodes']}
original_settings = sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']
def appearance_form(source, dashboard, notification):
    return {'source':source, **{channel+'.'+key:str(value).lower()
        for channel,options in [('dashboard',dashboard),('notification',notification)]
        for key,value in options.items()}}
all_off = dict.fromkeys(APPEARANCE_DEFAULTS,False)
all_on = dict.fromkeys(APPEARANCE_DEFAULTS,True)
bnn_options = {**APPEARANCE_DEFAULTS,'mapping_link':True}
post('/alerts/appearance',appearance_form('BNN',bnn_options,bnn_options))
bnn_saved = sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']['alert_appearance']['BNN']
appearance_payloads = []
appearance_matches = []
appearance_ids = []
def appearance_match(payload):
    normalized = js(matcher['Normalize Alert']['parameters']['jsCode'],payload,[])
    rows = sql(matcher['Load Active Watchlist + Recipients']['parameters']['query'],(Jsonb(normalized),))
    return js(matcher['Match + Resolve Recipients']['parameters']['jsCode'],normalized,rows)
for structured in (False,True):
    payload = build_hudson_alert(
        {'municipality':'WEEHAWKEN','customers_out':700,'customers_served':2000,
         'outage_count':3,'jobs_working':2,'circuits':1,'confirmed_poles':1},
        {'customers_out':600},
        {'label':'Weehawken','latitude':40.77,'longitude':-74.02,
         'areas':[{'label':'working fire area'}]},str(uuid4()),datetime.now(timezone.utc))
    if not structured:
        payload['metadata'] = {}
    ident = sql('''INSERT INTO alerts(alert_id,source,category,subtype,status,event_action,title,message,
        priority,county,municipality,click_url,search_text,location,metadata,raw_payload,geom)
        VALUES(%s,'PSEG','UTILITY','POWER_OUTAGE','ACTIVE','UPDATE',%s,%s,4,'Hudson','Weehawken',
        %s,%s,%s,%s,%s,ST_GeomFromEWKT(%s)) RETURNING id''',
        (payload['alert_id'],payload['title'],payload['message'],payload['click_url'],payload['search_text'],
         Jsonb(payload['location']),Jsonb(payload['metadata']),Jsonb(payload),inside))[0]['id']
    result = appearance_match(payload)
    assert term_watch['watch_id'] in result['matched_watch_ids'],result
    sql(matcher['Persist Watch Matches']['parameters']['query'],(Jsonb(result),))
    delivery = next(p for p in result['delivery_payloads'] if term_watch['watch_id'] in p['matched_watch_ids'])
    assert 'source' not in delivery and 'metadata' not in delivery, delivery
    appearance_payloads.append(payload)
    appearance_matches.append(result)
    appearance_ids.append(ident)
appearance_raw = sql('SELECT id,message,search_text,metadata,raw_payload FROM alerts WHERE id=ANY(%s) ORDER BY id',(appearance_ids,))
def appearance_counts():
    return sql('''SELECT (SELECT count(*) FROM watch_items) AS watches,
        (SELECT count(*) FROM alert_watch_matches) AS matches,
        (SELECT count(*) FROM deliveries) AS deliveries''')[0]
appearance_before = appearance_counts()
sender_code = sender['Prepare ntfy Requests']['parameters']['jsCode']
sender_script = "const d=JSON.parse(require('fs').readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(new Function('$input',d.code)({first:()=>({json:d.input})})));"
for dashboard_on in (False,True):
    dashboard_options,notification_options = (all_on,all_off) if dashboard_on else (all_off,all_on)
    post('/alerts/appearance',appearance_form('PSEG',dashboard_options,notification_options))
    settings = sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']
    assert settings['alert_appearance']['PSEG']=={'dashboard':dashboard_options,'notification':notification_options}
    assert settings['alert_appearance']['BNN']==bnn_saved
    assert {k:v for k,v in settings.items() if k!='alert_appearance'}=={k:v for k,v in original_settings.items() if k!='alert_appearance'}
    bundle = {'delivery_payloads':[
        next(p for p in result['delivery_payloads'] if term_watch['watch_id'] in p['matched_watch_ids'])
        for result in appearance_matches] + [{'source':'BNN','ntfy_topic':'ci-only-bnn',
        'message':'Original BNN body\nMapping Center: https://fixture.example.com/map'}]}
    loaded=sql(sender['Load Notification Options']['parameters']['query'],(Jsonb(bundle),))[0]
    for index,payload in enumerate(appearance_payloads):
        enriched = loaded['sender_input']['delivery_payloads'][index]
        assert enriched['source']=='PSEG' and enriched['metadata']==payload['metadata']
    output=json.loads(subprocess.run(['node','-e',sender_script],input=json.dumps({'code':sender_code,'input':loaded}),text=True,capture_output=True,check=True).stdout)
    assert len(output)==3
    for index,item in enumerate(output[:2]):
        rendered = item['json']['ntfy_body']
        for optional in ('ETR ','Started ','Jobs 3','Damage: Pole 1','Change +100',
                         'Approximate outage area:','Mapping Center:','Why you received this:',
                         'Matched Watches:','Matched keywords:'):
            assert (optional in rendered['message']) is (not dashboard_on),(optional,rendered)
        assert bool(rendered.get('click')) is (not dashboard_on),rendered
        assert item['json']['message']==appearance_payloads[index]['message']
        assert item['json']['match_reasons']==bundle['delivery_payloads'][index]['match_reasons']
        assert item['json']['matched_watch_ids']==bundle['delivery_payloads'][index]['matched_watch_ids']
        if dashboard_on:
            assert rendered['message']=='700 of 2,000 customers out in Weehawken.'
        assert appearance_match(appearance_payloads[index])==appearance_matches[index]
    assert output[2]['json']['ntfy_body']['message']==bundle['delivery_payloads'][2]['message']
os.environ.update(CMOS_READONLY_USERNAME='ReadOnly',CMOS_READONLY_PASSWORD_HASH='unused-test-hash')
readonly = auth._issue_session(auth.Account('ReadOnly','READ_ONLY',''))
appearance_saved = sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']
for data,request_headers,code in [
    ({'source':'PSEG'}, {**headers,'Cookie':auth.COOKIE_NAME+'='+readonly},403),
    ({'source':'PSEG'}, {'Origin':'https://unrelated.example.com'},403),
    ({'source':'NOT_A_REAL_SOURCE'}, headers,400),
    ({'source':'PSEG','dashboard.unknown':'true'}, headers,400),
    ({'source':'PSEG','notification.mapping_link':'not-a-boolean'}, headers,400),
    ({'source':['PSEG','BNN']}, headers,400),
]:
    denied = client.post('/alerts/appearance',data=data,headers=request_headers,follow_redirects=False)
    assert denied.status_code==code,(denied.status_code,denied.text)
    assert sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']==appearance_saved
assert appearance_counts()==appearance_before
assert sql('SELECT id,message,search_text,metadata,raw_payload FROM alerts WHERE id=ANY(%s) ORDER BY id',(appearance_ids,))==appearance_raw
print('ALERT APPEARANCE API/SENDER PASS: real source/channel config, legacy and sectioned PSEG enriched by alert_id, actual matcher and keyword evidence retained, BNN isolated, invalid/origin/read-only saves rejected before writes; no send')

# A historical preview uses saved rules or a point directly, independently of prior Matches.
history_tag = str(uuid4())
history_titles = {}
def history_alert(key, geometry=inside, source='BNN', category='INCIDENT', priority=4, words='working fire', old=False, resolution=None):
    title = 'History '+history_tag+' '+key+' '+words
    row = sql('''INSERT INTO alerts(alert_id,source,category,subtype,status,event_action,title,message,
        priority,county,municipality,geom,observed_at,received_at)
        VALUES(%s,%s,%s,'TEST','ACTIVE','NEW',%s,'',%s,'Hudson','Weehawken',ST_GeomFromEWKT(%s),
        now()-CASE WHEN %s THEN interval '45 days' ELSE interval '0' END,
        now()-CASE WHEN %s THEN interval '45 days' ELSE interval '0' END) RETURNING id''',
        ('HISTORY:'+history_tag+':'+key,source,category,title,priority,geometry,old,old))[0]
    history_titles[key] = title
    if resolution:
        sql('''INSERT INTO geo_entity_resolutions(entity_type,entity_id,status,geom)
            VALUES('ALERT',%s,%s,ST_GeomFromEWKT(%s)) RETURNING id''',(str(row['id']),resolution,inside))
    return row['id']
old_area_point = sql("SELECT ST_AsEWKT(ST_Project(ST_SetSRID(ST_MakePoint(-74.02,40.77),4326)::geography,800*0.3048,0)::geometry) AS geom")[0]['geom']
history_alert('old-area',geometry=old_area_point,priority=1,words='quiet',old=True)
history_alert('fire',old=True)
history_alert('tier',source='OPERATIONS',category='WORK',words='tier 3')
history_alert('wrong-source',source='OTHER')
history_alert('wrong-category',category='TEST')
history_alert('low-priority',priority=1)
history_alert('no-words',words='quiet')
history_alert('outside',geometry=outside)
history_alert('resolved',geometry=None,resolution='RESOLVED')
history_alert('stored-wins',geometry=outside,resolution='RESOLVED')
history_alert('unresolved',geometry=None,resolution='UNRESOLVED')
history_alert('unmapped',geometry=None)
full_rule = sql('''INSERT INTO watch_items(watch_id,display_name,watch_type,search_term,aliases,
    match_field,match_mode,source_filter,alert_category_filter,min_priority,nearby_enabled,
    spatial_target_geom,spatial_scope,radius_ft,active,starts_at,expires_at)
    VALUES(%s,'Historical full radius rule','LOCATION_TOPIC','working fire',ARRAY['TIER \\d'],
    'search_text','WORD',ARRAY['BNN','OPERATIONS'],ARRAY['INCIDENT','WORK'],3,true,
    ST_SetSRID(ST_MakePoint(-74.02,40.77),4326),'RADIUS',1000,false,now()-interval '10 days',now()-interval '1 day') RETURNING *''',
    ('HISTORY_RULE_'+history_tag,))[0]
boundary_rule = sql('''INSERT INTO watch_items(watch_id,display_name,watch_type,search_term,match_field,match_mode,nearby_enabled,
    spatial_target_geom,spatial_scope,radius_ft,active)
    VALUES(%s,'Historical saved boundary','AREA','','search_text','CONTAINS',true,ST_GeomFromEWKT(%s),'ENTITY',50,false) RETURNING *''',
    ('HISTORY_BOUNDARY_'+history_tag,'SRID=4326;POLYGON((-74.025 40.765,-74.015 40.765,-74.015 40.775,-74.025 40.775,-74.025 40.765))'))[0]
def history_counts():
    return sql('''SELECT (SELECT count(*) FROM watch_items) AS watches,
        (SELECT count(*) FROM alert_watch_matches) AS matches,
        (SELECT count(*) FROM deliveries) AS deliveries''')[0]
history_before = history_counts()
area_params = {'latitude':40.77,'longitude':-74.02,'radius_ft':1000}
area_preview = client.get('/watch-preview',params=area_params)
assert area_preview.status_code==200,area_preview.text[:1000]
assert '<option value="all" selected>' in area_preview.text
for key in ('old-area','fire','tier','wrong-source','wrong-category','low-priority','no-words','resolved'):
    assert history_titles[key] in area_preview.text,key
for key in ('outside','stored-wins','unresolved','unmapped'):
    assert history_titles[key] not in area_preview.text,key
assert history_titles['old-area'] not in client.get('/watch-preview',params={**area_params,'window':'24h'}).text
assert history_titles['fire'] not in client.get('/watch-preview',params={**area_params,'radius_ft':500}).text
saved_preview = client.get('/watch-preview',params={'watch_item_id':str(full_rule['id']),'window':'all',
    'source':'OTHER','min_priority':1,'radius_ft':20000})
assert saved_preview.status_code==200,saved_preview.text[:1000]
for key in ('fire','tier','resolved'):
    assert history_titles[key] in saved_preview.text,key
for key in ('old-area','wrong-source','wrong-category','low-priority','no-words','outside','stored-wins','unresolved','unmapped'):
    assert history_titles[key] not in saved_preview.text,key
boundary_preview = client.get('/watch-preview',params={'watch_item_id':str(boundary_rule['id']),'window':'all'})
assert boundary_preview.status_code==200,boundary_preview.text[:1000]
assert history_titles['outside'] in boundary_preview.text and history_titles['stored-wins'] in boundary_preview.text
watch_page = client.get('/watchlist',params={'focus':str(watch['id'])})
assert '/watch-preview?watch_item_id='+str(watch['id']) in watch_page.text
evidence_page = client.get('/watchlist',params={'evidence':str(watch['id'])})
assert 'Preview History' in evidence_page.text and 'watch_item_id='+str(watch['id']) in evidence_page.text
for params,code in [({'latitude':40.77},400),({**area_params,'latitude':91},422),
                    ({**area_params,'radius_ft':0},422),({**area_params,'radius_ft':26401},422),
                    ({**area_params,'latitude':'nan'},422),({'watch_item_id':'bad'},422),
                    ({**area_params,'watch_item_id':str(watch['id'])},400),({'watch_item_id':str(uuid4())},404)]:
    assert client.get('/watch-preview',params=params).status_code==code,params
assert history_counts()==history_before
print('HISTORY PREVIEW PASS: all periods, no existing Watch/Match required, 999.9/1000.1 ft boundary, resolved/stored geometry precedence, full saved multi-source/category/word/priority rule, paused/expired and ENTITY area, invalid requests rejected; zero writes')

# The All History count must not silently stop at the old 25,000-candidate cap.
with core.db_conn() as c:
    c.execute('''INSERT INTO alerts(alert_id,source,category,subtype,status,event_action,title,message,
        priority,geom,received_at) SELECT 'HISTORY_SCALE:'||%s||':'||n,'CI_HISTORY_SCALE','TEST','TEST',
        'ACTIVE','NEW','History scale','','1',ST_SetSRID(ST_MakePoint(-75,41.5),4326),
        now()-interval '90 days' FROM generate_series(1,25001) n''',(history_tag,))
scale_preview = client.get('/watch-preview',params={'latitude':41.5,'longitude':-75,'radius_ft':1})
assert scale_preview.status_code==200,scale_preview.text[:500]
assert '<strong>25001</strong>' in scale_preview.text and 'Showing 1–100 of 25001 matches' in scale_preview.text
next_href = re.search(r'href="([^"]+)">Next 100</a>',scale_preview.text).group(1)
scale_second = client.get(unescape(next_href))
assert scale_second.status_code==200 and 'Showing 101–200 of 25001 matches' in scale_second.text
assert 'Previous 100' in scale_second.text and 'Next 100' in scale_second.text
links = lambda response: set(re.findall(r'href="/alerts\?q=([^&"]+)',response.text))
assert len(links(scale_preview))==len(links(scale_second))==100 and not links(scale_preview)&links(scale_second)
scale_last = client.get('/watch-preview',params={'latitude':41.5,'longitude':-75,'radius_ft':1,'history_page':251})
assert scale_last.status_code==200 and 'Showing 25001–25001 of 25001 matches' in scale_last.text
assert len(links(scale_last))==1 and 'Next 100' not in scale_last.text
assert history_counts()==history_before
with core.db_conn() as c:
    c.execute("DELETE FROM alerts WHERE source='CI_HISTORY_SCALE'")
print('HISTORY SCALE PASS: every one of 25,001 older alerts counted; pages 1/2/251 give distinct 100/100/1 results; no Watch/Match/delivery writes')

# These isolated records distinguish enabled Watches from delivery evidence; no sender runs.
status_alert = sql('''INSERT INTO alerts(alert_id,source,category,subtype,status,event_action,title,
    message,priority,county) VALUES(%s,'BNN','INCIDENT','TEST','ACTIVE','NEW','Working fire','',4,'Hudson')
    RETURNING id,alert_id''', ('CI_STATUS:'+str(uuid4()),))[0]
status_watches = {}
for delivery_label in ('No delivery recorded','Queued'):
    status_name = 'CI delivery status '+str(uuid4())
    post('/watchlist/create', {'display_name':status_name,'setup_mode':'LOCATION_TOPIC',
        'search_term':'working fire','location_scope':'COUNTY','location_kind':'COUNTY',
        'location_id':'Hudson','subscriber_ids':str(subscribers[0])})
    status_watch = sql('SELECT * FROM watch_items WHERE display_name=%s',(status_name,))[0]
    status_watches[delivery_label] = status_watch
    sql('''INSERT INTO alert_watch_matches(alert_id,watch_item_id,match_type,match_reason)
        VALUES(%s,%s,'LOCATION_TOPIC','CI status evidence') RETURNING id''',
        (status_alert['id'],status_watch['id']))
    if delivery_label == 'Queued':
        sql('''INSERT INTO deliveries(delivery_key,alert_id,subscriber_id,ntfy_topic,status,matched_watch_ids)
            VALUES(%s,%s,%s,'ci-status-only','PENDING',%s) RETURNING id''',
            ('CI_STATUS:'+str(uuid4()),status_alert['id'],subscribers[0],Jsonb([status_watch['watch_id']])))

lab_before = history_counts()
lab_response = client.post('/api/watch-lab/evaluate', data={
    'watch_item_id':str(status_watches['No delivery recorded']['id']),
    'alert_id':status_alert['alert_id']}, headers=headers)
assert lab_response.status_code == 200, (lab_response.status_code,lab_response.text)
lab = lab_response.json()
assert (lab['watch']['county'],lab['watch']['state']) == ('Hudson','NJ'), lab
assert not lab['watch']['nearby_enabled'] and not lab['watch']['watch_target_ready']
script = """const d=JSON.parse(require('fs').readFileSync(0,'utf8'));
const m=require(process.argv[1]);
const decision=m.evaluateWatch(d.alert,d.watch);
if(!decision.matched)throw Error('Lab county/topic did not match');
if(decision.gates.filter(g=>['alert_geometry','watch_target'].includes(g.key))
    .some(g=>g.status!=='NOT_APPLICABLE'))throw Error('Lab county/topic falsely requires geometry');
if(m.evaluateWatch({...d.alert,county:'Bergen'},d.watch).matched)throw Error('Lab ignored county');
if(m.evaluateWatch(d.alert,{...d.watch,search_term:'road closure'}).matched)throw Error('Lab ignored topic');"""
subprocess.run(['node','-e',script,str(ROOT/'dashboard/static/watch_matcher.js')],
    input=json.dumps(lab),text=True,check=True)
assert history_counts() == lab_before
print('WATCH LAB API PASS: real PostgreSQL projection retains county/state; real matcher requires county and topic without geometry; no writes')

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
# Keep the canonical HTTPS origin while the browser uses the direct HTTP address.
assert os.environ['CMOS_PUBLIC_ORIGIN'] == 'https://fixture.example.com'
server = uvicorn.Server(uvicorn.Config(application,host='127.0.0.1',port=port,log_level='error'))
thread = threading.Thread(target=server.run,daemon=True)
thread.start()
for _ in range(100):
    if server.started:
        break
    time.sleep(.05)
assert server.started
# Load the same pinned Leaflet assets as production once; basemap tiles are not test data.
map_assets = {}
for asset in ('leaflet@1.9.4/dist/leaflet.js','leaflet@1.9.4/dist/leaflet.css',
              'leaflet-draw@1.0.4/dist/leaflet.draw.js','leaflet-draw@1.0.4/dist/leaflet.draw.css',
              'leaflet.heat@0.2.0/dist/leaflet-heat.js'):
    url='https://unpkg.com/'+asset
    with urlopen(url,timeout=30) as response:
        map_assets[url]=response.read()
try:
    with sync_playwright() as browsers:
        for engine in ('firefox','chromium'):
            browser = getattr(browsers,engine).launch()
            context = browser.new_context(viewport={'width':1000,'height':900})
            context.add_cookies([{'name':auth.COOKIE_NAME,'value':cookie,'url':base}])
            page = context.new_page()
            errors=[]
            page.on('pageerror',lambda error: errors.append(str(error)))
            for delivery_label,status_watch in status_watches.items():
                page.goto(base+'/watchlist?'+urlencode({'q':status_watch['display_name']}))
                card = page.locator('.watch-card').filter(has_text=status_watch['display_name'])
                assert card.locator('[data-watch-lifecycle]').inner_text().strip() == 'On'
                assert card.locator('[data-watch-delivery]').inner_text().strip() == delivery_label
                details = card.locator('.watch-card-more')
                assert details.locator('summary').is_visible()
                details.locator('summary').click()
                explanation = details.locator('.watch-state-explanation')
                assert explanation.is_visible()
                assert 'The Watch is enabled and within its scheduled time window.' in explanation.inner_text()
                card.screenshot(path=str(ARTIFACTS/(
                    engine+'-watch-status-'+delivery_label.lower().replace(' ','-')+'.png')))
            print('WATCH STATUS BROWSER PASS:',engine,'both enabled Watches show On; only an actual PENDING delivery shows Queued; a match alone shows No delivery recorded')
            # Choose multiple sources, save, then edit through the real checkbox UI.
            page.goto(base+'/watchlist?'+urlencode({'create':'1','setup_mode':'TOPIC',
                'source_filter':'CI_RETIRED_SOURCE'}))
            form = page.locator('form[action="/watchlist/create"]')
            picker = form.locator('[data-source-picker]')
            picker.locator('summary').click()
            retired = picker.locator('[data-source-choice][value="CI_RETIRED_SOURCE"]')
            assert retired.is_visible() and retired.is_checked()
            assert form.locator('[name="source_filter"]').input_value()=='CI_RETIRED_SOURCE'
            picker.locator('[data-source-all]').check()
            assert form.locator('[name="source_filter"]').input_value()==''
            assert not retired.is_checked()
            for source in ('BNN','OPERATIONS'):
                picker.locator('[data-source-choice][value="'+source+'"]').check()
            assert not picker.locator('[data-source-all]').is_checked()
            assert {value.strip() for value in form.locator('[name="source_filter"]').input_value().split(',')}=={'BNN','OPERATIONS'}
            page.screenshot(path=str(ARTIFACTS/(engine+'-watch-sources.png')))
            picker.locator('summary').click()
            form.get_by_text('More options',exact=True).click()
            source_name = engine+' source picker '+str(uuid4())
            form.locator('[name="display_name"]').fill(source_name)
            with page.expect_response(lambda response: response.request.method=='POST'
                                      and response.url==base+'/watchlist/create') as native_submit:
                form.get_by_role('button',name='Save Paused',exact=True).click()
            response = native_submit.value
            assert response.status==303, (response.status,response.text())
            native_headers = response.request.all_headers()
            assert native_headers.get('origin')==base, native_headers.get('origin')
            assert native_headers.get('host')==urlsplit(base).netloc, native_headers.get('host')
            page.wait_for_url('**/watchlist?msg=*')
            selected = sql('SELECT * FROM watch_items WHERE display_name=%s',(source_name,))[0]
            assert set(selected['source_filter'])=={'BNN','OPERATIONS'} and selected['watch_type']=='SOURCE'
            page.locator('.watch-card').filter(has_text=source_name).screenshot(path=str(ARTIFACTS/(engine+'-watch-card.png')))
            page.goto(base+'/watchlist?focus='+str(selected['id']))
            edit = page.locator('form[action="/watchlist/'+str(selected['id'])+'/update"]')
            picker = edit.locator('[data-source-picker]')
            picker.locator('summary').click()
            assert picker.locator('[data-source-choice][value="BNN"]').is_checked()
            assert picker.locator('[data-source-choice][value="OPERATIONS"]').is_checked()
            picker.locator('[data-source-choice][value="BNN"]').uncheck()
            assert edit.locator('[name="source_filter"]').input_value()=='OPERATIONS'
            with page.expect_response(lambda response: response.request.method=='POST'
                                      and response.url==base+'/watchlist/'+str(selected['id'])+'/update') as native_submit:
                edit.get_by_role('button',name='Save Changes',exact=True).click()
            response = native_submit.value
            assert response.status==303, (response.status,response.text())
            native_headers = response.request.all_headers()
            assert native_headers.get('origin')==base, native_headers.get('origin')
            assert native_headers.get('host')==urlsplit(base).netloc, native_headers.get('host')
            page.wait_for_url('**/watchlist?msg=*')
            assert sql('SELECT source_filter FROM watch_items WHERE id=%s',(selected['id'],))[0]['source_filter']==['OPERATIONS']
            page.goto(base+'/watchlist?focus='+str(selected['id']))
            edit = page.locator('form[action="/watchlist/'+str(selected['id'])+'/update"]')
            picker = edit.locator('[data-source-picker]')
            picker.locator('summary').click()
            assert picker.locator('[data-source-choice][value="OPERATIONS"]').is_checked()
            assert not picker.locator('[data-source-choice][value="BNN"]').is_checked()
            picker.locator('[data-source-all]').check()
            assert picker.locator('[data-source-choice]:checked').count()==0
            assert edit.locator('[name="source_filter"]').input_value()==''
            picker.locator('summary').click()
            edit.locator('[name="search_term"]').fill('mayday')
            edit.locator('[name="word_mode"]').select_option('CONTAINS')
            edit.get_by_role('button',name='Save Changes',exact=True).click()
            page.wait_for_url('**/watchlist?msg=*')
            selected = sql('SELECT * FROM watch_items WHERE id=%s',(selected['id'],))[0]
            assert selected['source_filter']==[] and selected['search_term']=='mayday' and not selected['active']
            print('SOURCE PICKER BROWSER PASS:',engine,'native direct HTTP Origin/Host and 303 with separate canonical HTTPS origin; unknown prefill preserved; two sources saved/reopened; one removed; All sources clears restrictions')

            # Existing SOURCE rules predate source_filter and may have no alert history.
            legacy_name = engine+' legacy source '+str(uuid4())
            legacy = sql('''INSERT INTO watch_items(watch_id,display_name,watch_type,search_term,aliases,
                match_mode,match_field,source_filter,active,min_priority,municipality)
                VALUES(%s,%s,'SOURCE','EXEC_ASSISTANT',ARRAY['CI_RETIRED_SOURCE'],
                'FIELD','source',ARRAY[]::text[],false,4,'Weehawken') RETURNING *''',
                ('CI_LEGACY_'+str(uuid4()),legacy_name))[0]
            sql('INSERT INTO watch_item_recipients(watch_item_id,subscriber_id,active) VALUES(%s,%s,true) RETURNING id',
                (legacy['id'],subscribers[0]))
            page.goto(base+'/watchlist?focus='+str(legacy['id']))
            edit = page.locator('form[action="/watchlist/'+str(legacy['id'])+'/update"]')
            assert set(edit.locator('[name="source_filter"]').input_value().split(', '))=={'EXEC_ASSISTANT','CI_RETIRED_SOURCE'}
            assert edit.locator('[name="search_term"]').input_value()==''
            assert edit.locator('[name="location_scope"]').input_value()=='ANYWHERE'
            edit.get_by_text('More options',exact=True).click()
            edit.locator('[name="display_name"]').fill(legacy_name+' edited')
            edit.locator('[name="notes"]').fill('Saved without rebuilding this rule')
            edit.locator('[name="subscriber_ids"][value="'+str(subscribers[0])+'"]').uncheck()
            edit.locator('[name="subscriber_ids"][value="'+str(subscribers[1])+'"]').check()
            with page.expect_response(lambda response: response.request.method=='POST'
                                      and response.url==base+'/watchlist/'+str(legacy['id'])+'/update') as saved_response:
                edit.get_by_role('button',name='Save Changes',exact=True).click()
            assert saved_response.value.status==303, saved_response.value.text()
            page.wait_for_url('**/watchlist?msg=*')
            saved = sql('SELECT * FROM watch_items WHERE id=%s',(legacy['id'],))[0]
            assert saved['display_name']==legacy_name+' edited' and saved['notes']=='Saved without rebuilding this rule'
            assert (saved['watch_type'],saved['match_mode'],saved['match_field'])==('SOURCE','FIELD','source')
            assert set([saved['search_term'],*saved['aliases']])==set(saved['source_filter'])=={'EXEC_ASSISTANT','CI_RETIRED_SOURCE'}
            assert not saved['active'] and saved['min_priority']==4 and saved['municipality']=='Weehawken'
            assert [row['subscriber_id'] for row in sql('SELECT subscriber_id FROM watch_item_recipients WHERE watch_item_id=%s AND active',(legacy['id'],))]==[subscribers[1]]
            page.goto(base+'/watchlist?focus='+str(legacy['id']))
            edit = page.locator('form[action="/watchlist/'+str(legacy['id'])+'/update"]')
            assert set(edit.locator('[name="source_filter"]').input_value().split(', '))=={'EXEC_ASSISTANT','CI_RETIRED_SOURCE'}
            edit.locator('[data-source-picker] summary').click()
            edit.locator('[data-source-all]').check()
            edit.locator('[data-source-picker] summary').click()
            rejected_posts=[]
            def capture_empty_rule(request):
                if request.method=='POST' and request.url==base+'/watchlist/'+str(legacy['id'])+'/update':
                    rejected_posts.append(request.url)
            page.on('request',capture_empty_rule)
            edit.get_by_role('button',name='Save Changes',exact=True).click()
            assert edit.locator('[data-simple-watch-message]').is_visible()
            assert edit.locator('[data-simple-watch-message]').inner_text()=='Choose a source, enter a topic, or choose a location.'
            assert not rejected_posts
            page.remove_listener('request',capture_empty_rule)

            # County + keyword watches must retain their county when only the name changes.
            county_name = engine+' county edit '+str(uuid4())
            post('/watchlist/create',{'display_name':county_name,'setup_mode':'LOCATION_TOPIC',
                'search_term':'working fire','source_filter':'BNN','location_scope':'COUNTY',
                'location_kind':'COUNTY','location_id':'Hudson','location_query':'Hudson',
                'activation':'paused','subscriber_ids':str(subscribers[0])})
            county_watch=sql('SELECT * FROM watch_items WHERE display_name=%s',(county_name,))[0]
            page.goto(base+'/watchlist?focus='+str(county_watch['id']))
            edit=page.locator('form[action="/watchlist/'+str(county_watch['id'])+'/update"]')
            assert edit.locator('[name="location_scope"]').input_value()=='COUNTY'
            assert edit.locator('[name="location_id"]').input_value()=='Hudson'
            assert edit.locator('[data-county-choice]').input_value()=='Hudson'
            edit.get_by_text('More options',exact=True).click()
            edit.locator('[name="display_name"]').fill(county_name+' edited')
            with page.expect_response(lambda response: response.request.method=='POST'
                                      and response.url==base+'/watchlist/'+str(county_watch['id'])+'/update') as saved_response:
                edit.get_by_role('button',name='Save Changes',exact=True).click()
            assert saved_response.value.status==303, saved_response.value.text()
            page.wait_for_url('**/watchlist?msg=*')
            saved=sql('SELECT * FROM watch_items WHERE id=%s',(county_watch['id'],))[0]
            assert saved['display_name']==county_name+' edited'
            assert saved['watch_type']=='LOCATION_TOPIC' and saved['county']=='Hudson' and saved['state']=='NJ'
            assert saved['search_term']=='working fire' and saved['source_filter']==['BNN'] and not saved['active']
            assert not saved['nearby_enabled'] and saved['spatial_target_geom'] is None
            print('EXISTING WATCH EDIT BROWSER PASS:',engine,'legacy exact sources and retired source preserved; name/notes/recipients saved; empty rule explains blocked save; county/topic name edit keeps location')

            page.goto(base+'/watchlist?from_record=EVENT_INTELLIGENCE&record_id='+str(event_id))
            form = page.locator('form[action="/watchlist/create"]')
            assert form.is_visible()
            original_lat = form.locator('[name="latitude"]').input_value()
            original_lon = form.locator('[name="longitude"]').input_value()
            form.get_by_text('More options',exact=True).click()
            form.locator('[data-source-picker] summary').click()
            form.locator('[data-source-choice][value="BNN"]').check()
            form.locator('[data-source-picker] summary').click()
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
            edit.locator('[data-source-picker] summary').click()
            edit.locator('[data-source-all]').check()
            edit.locator('[data-source-picker] summary').click()
            edit.locator('[name="search_term"]').fill('mayday\ntier \\d')
            edit.locator('[name="word_mode"]').select_option('CONTAINS')
            edit.locator('[name="subscriber_ids"][value="'+str(subscribers[1])+'"]').check()
            edit.get_by_role('button',name='Save Changes',exact=True).click()
            page.wait_for_url('**/watchlist?msg=*')
            modified = sql('SELECT * FROM watch_items WHERE id=%s',(draft_watch['id'],))[0]
            assert modified['source_filter']==[] and modified['search_term']=='mayday'
            assert modified['aliases']==[r'tier \d'] and modified['match_mode']=='CONTAINS' and not modified['active']
            page.locator('#notification-options summary').click()
            assert page.locator('form[action="/watchlist/notification-settings"]').count()==0
            page.get_by_role('link',name='Set alert appearance',exact=True).click()
            page.wait_for_url('**/alerts#alert-appearance')
            assert page.locator('#alert-appearance').get_attribute('open') is not None

            # Both panels render stored examples; only Save persists source-specific choices.
            post('/alerts/appearance',appearance_form('PSEG',all_on,all_off))
            page.goto(base+'/alerts?source=PSEG#alert-appearance')
            options = page.locator('form[data-alert-appearance]')
            source_picker = options.locator('[data-appearance-source]')
            assert source_picker.input_value()=='PSEG'
            pseg_card = page.locator('[data-alert-id="'+str(appearance_ids[1])+'"]')
            card_message = pseg_card.locator('[data-alert-content-message]')
            assert 'Mapping Center:' in card_message.inner_text()
            assert 'Matched keywords:' in card_message.inner_text()
            source_picker.select_option('BNN')
            bnn_checkbox = options.locator('[name="dashboard.mapping_link"]')
            assert bnn_checkbox.is_checked()
            bnn_checkbox.uncheck()
            source_picker.select_option('PSEG')
            assert options.locator('[name="dashboard.mapping_link"]').is_checked()
            source_picker.select_option('BNN')
            assert not bnn_checkbox.is_checked(), 'Changing sources must retain unsaved choices'
            source_picker.select_option('PSEG')
            preview_before = history_counts()
            settings_before_preview = sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']
            for channel,enabled in [('dashboard',False),('notification',True)]:
                for checkbox in options.locator('[data-appearance-channel="'+channel+'"]').all():
                    checkbox.set_checked(enabled)
                options.locator('[data-appearance-preview="'+channel+'"]').click()
                preview = options.locator('[data-appearance-message="'+channel+'"]').inner_text()
                for optional in ('Mapping Center:','ETR ','Damage:','Matched keywords:','Why you received this:'):
                    assert (optional in preview) is enabled,(channel,optional,preview)
            assert options.locator('[data-appearance-message="dashboard"]').inner_text()=='700 of 2,000 customers out in Weehawken.'
            assert history_counts()==preview_before
            assert sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']==settings_before_preview
            with page.expect_response(lambda response: response.request.method=='POST'
                                      and response.url==base+'/alerts/appearance') as saved_response:
                options.get_by_role('button',name='Save appearance',exact=True).click()
            assert saved_response.value.status==200,saved_response.value.text()
            page.wait_for_function("document.querySelector('[data-alert-appearance] [type=submit]').disabled===false")
            saved_appearance = sql('SELECT settings FROM workspace_config WHERE singleton=true')[0]['settings']['alert_appearance']
            assert saved_appearance['PSEG']=={'dashboard':all_off,'notification':all_on}
            assert saved_appearance['BNN']==bnn_saved
            assert card_message.inner_text()=='700 of 2,000 customers out in Weehawken.'
            assert not pseg_card.locator('[data-alert-source-link]').is_visible()
            assert history_counts()==preview_before
            assert term_watch['watch_id'] in appearance_match(appearance_payloads[1])['matched_watch_ids']
            assert sql('SELECT id,message,search_text,metadata,raw_payload FROM alerts WHERE id=ANY(%s) ORDER BY id',(appearance_ids,))==appearance_raw
            page.reload()
            options = page.locator('form[data-alert-appearance]')
            assert not options.locator('[name="dashboard.mapping_link"]').is_checked()
            assert options.locator('[name="notification.mapping_link"]').is_checked()
            assert not options.locator('[name="dashboard.keywords"]').is_checked()
            assert page.locator('[data-alert-id="'+str(appearance_ids[1])+'"] [data-alert-content-message]').inner_text()=='700 of 2,000 customers out in Weehawken.'
            options.screenshot(path=str(ARTIFACTS/(engine+'-alert-appearance.png')))
            fresh = browser.new_context(viewport={'width':390,'height':844})
            fresh.add_cookies([{'name':auth.COOKIE_NAME,'value':cookie,'url':base}])
            fresh_page = fresh.new_page()
            fresh_page.on('pageerror',lambda error: errors.append(str(error)))
            fresh_page.goto(base+'/alerts?source=PSEG#alert-appearance')
            fresh_options = fresh_page.locator('form[data-alert-appearance]')
            assert not fresh_options.locator('[name="dashboard.mapping_link"]').is_checked()
            assert fresh_options.locator('[name="notification.mapping_link"]').is_checked()
            assert not fresh_options.locator('[name="dashboard.keywords"]').is_checked()
            fresh_page.screenshot(path=str(ARTIFACTS/(engine+'-alert-appearance-phone.png')),full_page=True)
            fresh.close()
            assert history_counts()==preview_before
            print('ALERT APPEARANCE BROWSER PASS:',engine,'source drafts retained; opposite dashboard/notification previews; explicit save; BNN isolated; card Mapping Center and keyword display removed; real matching/raw evidence retained; reload and fresh phone context persist; previews make zero writes')
            history_snapshot = history_counts()
            page.goto(base+'/watchlist?focus='+str(watch['id']))
            card=page.locator('.watch-card').filter(has=page.locator('a[href*="watch_item_id='+str(watch['id'])+'"]')).first
            card.get_by_role('link',name='Preview History',exact=True).click()
            page.wait_for_url('**/watch-preview?watch_item_id=*')
            assert page.locator('#history-preview-controls [name="window"]').input_value()=='all'
            assert page.get_by_text(history_titles['old-area'],exact=True).is_visible()
            page.locator('[name="window"]').select_option('24h')
            page.get_by_role('button',name='Preview History',exact=True).click()
            page.wait_for_url('**/watch-preview?*window=24h*')
            assert page.get_by_text(history_titles['old-area'],exact=True).count()==0
            assert page.locator('[name="watch_item_id"]').input_value()==str(watch['id'])
            # Only external assets and map layers are mocked. Actual Map JS/Leaflet/forms and history SQL run.
            context.route('https://**/*',lambda route: route.fulfill(status=200,body=map_assets.get(route.request.url,b''),
                content_type='text/css' if route.request.url.endswith('.css') else 'application/javascript'))
            context.route('**/map/gis/status',lambda route: route.fulfill(json={}))
            map_url=base+'/map?map_view=1&lat=40.77&lng=-74.02&zoom=16&layers=&tab=search'
            page.goto(map_url)
            page.wait_for_function("typeof map!=='undefined' && typeof showSelectedLocation==='function'")
            box=page.locator('#city-map').bounding_box()
            page.mouse.click(box['x']+box['width']/2,box['y']+box['height']/2)
            popup=page.locator('.leaflet-popup-content')
            radius=popup.locator('[name="radius_ft"]')
            assert radius.input_value()=='1000'
            radius.fill('1200')
            assert page.locator('#map-selected-details [name="radius_ft"]').input_value()=='1200'
            assert abs(page.evaluate("groups['watch-preview'].getLayers()[0].getRadius()")-1200*.3048)<1e-9
            radius.fill('1000')
            popup.get_by_role('button',name='Preview History Here',exact=True).click()
            page.wait_for_url('**/watch-preview?*')
            params=dict(parse_qsl(urlsplit(page.url).query))
            assert params['window']=='all' and params['radius_ft']=='1000' and 'watch_item_id' not in params
            assert abs(float(params['latitude'])-40.77)<1e-4 and abs(float(params['longitude'])+74.02)<1e-4
            assert page.get_by_text(history_titles['old-area'],exact=True).is_visible()
            page.locator('[name="radius_ft"]').fill('500')
            page.get_by_role('button',name='Preview History',exact=True).click()
            page.wait_for_url('**/watch-preview?*radius_ft=500*')
            assert page.get_by_text(history_titles['old-area'],exact=True).count()==0
            page.get_by_role('link',name='Create Watch From This Area',exact=True).click()
            page.wait_for_url('**/watchlist?*')
            draft=page.locator('form[action="/watchlist/create"]')
            assert draft.locator('[name="radius_ft"]').input_value()=='500'
            assert abs(float(draft.locator('[name="latitude"]').input_value())-float(params['latitude']))<1e-9
            assert draft.locator('[name="match_selection"]').input_value()=='ANY'
            page.goto(map_url)
            page.wait_for_function("typeof map!=='undefined' && typeof geoOptions==='function'")
            # A real clickable polygon must not steal the picker click or substitute its center.
            page.evaluate("""L.geoJSON({type:'Feature',properties:{name:'Picker coverage'},geometry:{type:'Polygon',
                coordinates:[[[-74.021,40.769],[-74.015,40.769],[-74.015,40.775],[-74.021,40.775],[-74.021,40.769]]]}},
                geoOptions('parcels','#7fb3d5','Parcels')).addTo(map)""")
            page.get_by_role('button',name='Tools',exact=True).click()
            page.get_by_role('button',name='Pick Watch Point / Search History',exact=True).click()
            box=page.locator('#city-map').bounding_box()
            page.mouse.click(box['x']+box['width']/2,box['y']+box['height']/2)
            popup=page.locator('.leaflet-popup-content')
            assert popup.locator('[name="radius_ft"]').input_value()=='1000'
            assert abs(float(popup.locator('[name="latitude"]').input_value())-40.77)<1e-4
            assert abs(float(popup.locator('[name="longitude"]').input_value())+74.02)<1e-4
            assert history_counts()==history_snapshot
            print('MAP HISTORY BROWSER PASS:',engine,'click actual map, redraw radius, all-history native GET, older un-matched alerts, re-preview radius, optional Watch draft keeps point/radius, picker works through polygon overlays; no writes')
            assert not errors,errors
            print('WATCH BROWSER PASS:',engine,'Intel/Map/Alert drafts, all activity, selectable 34 terms, source/location/recipient controls, paused save/edit, per-source alert appearance')
            context.close()
            browser.close()
finally:
    server.should_exit=True
    thread.join(timeout=5)

