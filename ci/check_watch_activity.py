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
        return c.execute(query, params).fetchall()

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
