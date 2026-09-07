-- Transit Intelligence Phase 2.
-- New connectors are created PAUSED. Existing working NJT rail connectors are untouched.

UPDATE integrations
SET endpoint_url='https://pcsdata.njtransit.com/api/BUSDV2/getVehicleLocations',
    poll_seconds=120,
    timeout_seconds=30,
    max_response_bytes=25000000,
    notes='Regional NJ TRANSIT bus vehicles from BUSDV2. Radius is feet; City Manager OS defaults to 30,000 feet. HBLR vehicles are not inferred from BUSDV2 mode because live acceptance showed BUS/HBLR/ALL returning the same set.',
    updated_at=now()
WHERE integration_key='NJT_BUS_LIVE';

UPDATE integrations
SET endpoint_url='https://pcsdata.njtransit.com/api/GTFS/getGTFS',
    auth_config='{"username_env":"NJT_USERNAME","password_env":"NJT_PASSWORD","api_family":"GTFS"}'::jsonb,
    poll_seconds=86400,
    timeout_seconds=90,
    max_response_bytes=90000000,
    notes='NJ TRANSIT static Bus GTFS. /api/GTFS is preferred because live 2026 acceptance returned a complete ~59 MB ZIP. Mixed-mode GTFS relationships derive HBLR routes/stops safely.',
    updated_at=now()
WHERE integration_key='NJT_BUS_GTFS';

INSERT INTO integrations(
  integration_key,name,active,category,adapter_type,endpoint_url,
  method,auth_type,auth_config,request_headers,request_query,request_body,
  parser_kind,parser_config,poll_seconds,timeout_seconds,max_response_bytes,
  allow_redirects,verify_tls,notes
)
VALUES
('NJT_BUS_ALERTS','NJ TRANSIT Bus Advisories',false,'TRANSIT','NJT_RSS','https://www.njtransit.com/rss/BusAdvisories_feed.xml','GET','NONE','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,NULL,'NONE','{"provider_key":"NJ_TRANSIT","mode":"BUS"}'::jsonb,180,30,4000000,true,true,'Official NJ TRANSIT bus advisory RSS. Regional watch matching prevents statewide advisory noise.'),
('NJT_LIGHT_RAIL_ALERTS','NJ TRANSIT Light Rail Advisories',false,'TRANSIT','NJT_RSS','https://www.njtransit.com/rss/LightRailAdvisories_feed.xml','GET','NONE','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,NULL,'NONE','{"provider_key":"NJ_TRANSIT","mode":"LIGHT_RAIL","include_terms":["HBLR","HUDSON-BERGEN","BERGENLINE","PORT IMPERIAL","LINCOLN HARBOR","TONNELLE","HOBOKEN"]}'::jsonb,180,30,4000000,true,true,'Official NJ TRANSIT Light Rail advisory RSS, filtered to Hudson-Bergen Light Rail and nearby transfer points.'),
('PATH_GTFS','PATH Static GTFS',false,'TRANSIT','TRANSIT_GTFS_URL','https://data.trilliumtransit.com/gtfs/path-nj-us/path-nj-us.zip','GET','NONE','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,NULL,'NONE','{"provider_key":"PATH","fallback_mode":"RAIL","force_mode":"RAIL"}'::jsonb,86400,60,25000000,true,true,'PATH static GTFS reference data for station/route map context.'),
('PATH_REALTIME','PATH Realtime Arrivals',false,'TRANSIT','PATH_REALTIME','https://www.panynj.gov/bin/portauthority/ridepath.json','GET','NONE','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,NULL,'NONE','{"provider_key":"PATH","station_codes":["HOB","NEW","EXP","JSQ","WTC","33S"]}'::jsonb,60,30,4000000,true,true,'Official Port Authority RidePATH realtime JSON. Shows regional arrival context and raises watches when PATH marks an arrival delayed.'),
('NYW_GTFS','NY Waterway Static GTFS',false,'TRANSIT','TRANSIT_GTFS_URL','https://data.trilliumtransit.com/gtfs/nywaterway-nj-us/nywaterway-nj-us.zip','GET','NONE','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,NULL,'NONE','{"provider_key":"NY_WATERWAY","fallback_mode":"FERRY","allowed_modes":["FERRY"]}'::jsonb,86400,60,25000000,true,true,'NY Waterway GTFS reference feed. Ferry routes/stops only; shuttle-bus routes are excluded.'),
('NYW_ADVISORIES','NY Waterway Port Imperial Advisories',false,'TRANSIT','NYW_ADVISORIES','https://www.nywaterway.com/advisoriesalerts.aspx','GET','NONE','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,NULL,'NONE','{"provider_key":"NY_WATERWAY"}'::jsonb,300,30,4000000,true,true,'Official NY Waterway advisories. Advisory details are checked and only items mentioning Port Imperial are stored.')
ON CONFLICT(integration_key) DO UPDATE
SET name=EXCLUDED.name,category=EXCLUDED.category,adapter_type=EXCLUDED.adapter_type,
    endpoint_url=EXCLUDED.endpoint_url,method=EXCLUDED.method,auth_type=EXCLUDED.auth_type,
    auth_config=EXCLUDED.auth_config,parser_kind=EXCLUDED.parser_kind,parser_config=EXCLUDED.parser_config,
    poll_seconds=EXCLUDED.poll_seconds,timeout_seconds=EXCLUDED.timeout_seconds,
    max_response_bytes=EXCLUDED.max_response_bytes,allow_redirects=EXCLUDED.allow_redirects,
    verify_tls=EXCLUDED.verify_tls,notes=EXCLUDED.notes,updated_at=now();

UPDATE transit_providers
SET notes=CASE provider_key
  WHEN 'NJ_TRANSIT' THEN 'Phase 1/2 provider: rail, regional buses, HBLR reference data and official bus/light-rail advisories.'
  WHEN 'PATH' THEN 'Phase 2 provider: Port Authority RidePATH realtime context plus static PATH station/route reference data.'
  WHEN 'NY_WATERWAY' THEN 'Phase 2 provider: Port Imperial ferry route reference data and official advisories.'
  ELSE notes END,
updated_at=now()
WHERE provider_key IN ('NJ_TRANSIT','PATH','NY_WATERWAY');

INSERT INTO transit_watch_config(provider_id,active,target_type,target_key,display_name,municipality,corridor,min_impact_level,notes)
SELECT p.id,true,v.target_type,v.target_key,v.display_name,v.municipality,v.corridor,v.min_level,v.notes
FROM transit_providers p
CROSS JOIN (VALUES
('ROUTE','126','NJ TRANSIT Route 126','Weehawken','Lincoln Tunnel / PABT','WATCH','Cross-Hudson bus relevance.'),
('ROUTE','128','NJ TRANSIT Route 128','Weehawken','Lincoln Tunnel / PABT','WATCH','Cross-Hudson bus relevance.'),
('ROUTE','156','NJ TRANSIT Route 156','Weehawken','Boulevard East / Lincoln Tunnel','WATCH','Weehawken/Boulevard East relevance.'),
('ROUTE','158','NJ TRANSIT Route 158','Weehawken','Port Imperial / Boulevard East / PABT','WATCH','Port Imperial/Weehawken relevance.'),
('ROUTE','159','NJ TRANSIT Route 159','Weehawken','Boulevard East / PABT','WATCH','Weehawken/Boulevard East relevance.'),
('LINE','HBLR','Hudson-Bergen Light Rail','Weehawken','HBLR','WATCH','Regional light rail line serving Port Imperial/Lincoln Harbor/Bergenline.'))
AS v(target_type,target_key,display_name,municipality,corridor,min_level,notes)
WHERE p.provider_key='NJ_TRANSIT'
ON CONFLICT(provider_id,target_type,target_key) DO UPDATE
SET active=true,display_name=EXCLUDED.display_name,municipality=EXCLUDED.municipality,corridor=EXCLUDED.corridor,min_impact_level=EXCLUDED.min_impact_level,notes=EXCLUDED.notes,updated_at=now();

INSERT INTO transit_watch_config(provider_id,active,target_type,target_key,display_name,municipality,corridor,min_impact_level,notes)
SELECT p.id,true,'STATION',v.target_key,v.display_name,v.municipality,'PATH',v.min_level,v.notes
FROM transit_providers p
CROSS JOIN (VALUES
('HOB','PATH Hoboken','Hoboken','WATCH','Major local cross-Hudson transfer.'),
('NEW','PATH Newport','Jersey City','WATCH','Nearby waterfront PATH connection.'),
('EXP','PATH Exchange Place','Jersey City','WATCH','Downtown cross-Hudson PATH connection.'),
('JSQ','PATH Journal Square','Jersey City','WATCH','Major PATH transfer point.'),
('WTC','PATH World Trade Center','New York','WATCH','Downtown Manhattan connection.'),
('33S','PATH 33rd Street','New York','WATCH','Midtown Manhattan connection.'))
AS v(target_key,display_name,municipality,min_level,notes)
WHERE p.provider_key='PATH'
ON CONFLICT(provider_id,target_type,target_key) DO UPDATE
SET active=true,display_name=EXCLUDED.display_name,municipality=EXCLUDED.municipality,corridor=EXCLUDED.corridor,min_impact_level=EXCLUDED.min_impact_level,notes=EXCLUDED.notes,updated_at=now();

INSERT INTO transit_watch_config(provider_id,active,target_type,target_key,display_name,municipality,corridor,min_impact_level,notes)
SELECT p.id,true,v.target_type,v.target_key,v.display_name,'Weehawken','Port Imperial / Hudson River',v.min_level,v.notes
FROM transit_providers p
CROSS JOIN (VALUES
('TERMINAL','PORT IMPERIAL','Port Imperial / Weehawken','WATCH','Highest-priority local ferry terminal.'),
('ROUTE','PORT IMPERIAL MIDTOWN','Port Imperial - Midtown / W39','WATCH','Primary Midtown ferry connection.'),
('ROUTE','PORT IMPERIAL PIER 11','Port Imperial - Pier 11 / Wall St','WATCH','Downtown Wall Street ferry connection.'),
('ROUTE','PORT IMPERIAL BROOKFIELD','Port Imperial - Brookfield Place','WATCH','Downtown Brookfield ferry connection.'))
AS v(target_type,target_key,display_name,min_level,notes)
WHERE p.provider_key='NY_WATERWAY'
ON CONFLICT(provider_id,target_type,target_key) DO UPDATE
SET active=true,display_name=EXCLUDED.display_name,municipality=EXCLUDED.municipality,corridor=EXCLUDED.corridor,min_impact_level=EXCLUDED.min_impact_level,notes=EXCLUDED.notes,updated_at=now();
