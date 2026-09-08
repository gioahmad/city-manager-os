-- #35 Regional Event Source Pack 1
-- Register broad public event sources without requiring provider-specific
-- alert systems. New sources start paused and are activated only after TEST.

ALTER TABLE integrations
  DROP CONSTRAINT IF EXISTS integrations_parser_kind_check;

ALTER TABLE integrations
  ADD CONSTRAINT integrations_parser_kind_check
  CHECK (parser_kind IN (
    'NONE','JSON_EVENTS','RSS_EVENTS','ATOM_EVENTS','ICS_EVENTS','JSONLD_EVENTS'
  ));

INSERT INTO integrations (
  integration_key,name,active,category,adapter_type,endpoint_url,method,
  auth_type,auth_config,request_headers,request_query,parser_kind,parser_config,
  poll_seconds,timeout_seconds,max_response_bytes,allow_redirects,verify_tls,
  notes,provider_template,source_owner,access_instructions,geography_scope,
  relevance_keywords,attention_config,map_config
)
VALUES
(
  'HUDSON_COUNTY_EVENTS','Hudson County / Visit Hudson Events',false,'EVENTS','HTTP',
  'https://www.visithudson.org/calendar/','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"COUNTY_EVENT","county":"Hudson","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,3000000,true,true,
  'Official Hudson County tourism/cultural events page. Uses generic schema.org Event JSON-LD when exposed by the public page.',
  'JSONLD','Hudson County Office of Cultural and Heritage Affairs / Tourism Development',
  'Public source; no account required. Leave paused if the site stops exposing parseable Event JSON-LD.',
  'Hudson County, NJ','Hudson County, Weehawken, Hoboken, Jersey City, Union City, North Bergen, West New York, Secaucus',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Hudson County","Weehawken","Hoboken","Jersey City","Union City","North Bergen","West New York","Secaucus"]}'::jsonb,
  '{}'::jsonb
),
(
  'BERGEN_COUNTY_EVENTS','Bergen County Official Events',false,'EVENTS','HTTP',
  'https://bergencountynj.gov/events/','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"COUNTY_EVENT","county":"Bergen","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,3000000,true,true,
  'Official Bergen County events archive. Uses generic schema.org Event JSON-LD when exposed by the public page.',
  'JSONLD','Bergen County',
  'Public source; no account required. Leave paused if the archive does not expose parseable Event JSON-LD.',
  'Bergen County, NJ / Meadowlands corridor','Bergen County, East Rutherford, Meadowlands, MetLife Stadium, Overpeck',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Bergen County","East Rutherford","Meadowlands","MetLife Stadium","Overpeck"]}'::jsonb,
  '{}'::jsonb
),
(
  'JERSEY_CITY_CULTURAL_EVENTS','Jersey City Cultural Affairs Events',false,'EVENTS','HTTP',
  'https://jerseycityculture.org/events/?ical=1','GET','NONE','{}'::jsonb,
  '{"Accept":"text/calendar, text/plain","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'ICS_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"MUNICIPAL_EVENT","municipality":"Jersey City","county":"Hudson","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,5000000,true,true,
  'Official Jersey City Office of Cultural Affairs public events calendar.',
  'ICS','Jersey City Office of Cultural Affairs',
  'Public iCalendar source; no account required.',
  'Jersey City, NJ','Jersey City, Liberty State Park, waterfront, festival, parade, concert',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Jersey City","Liberty State Park","waterfront","festival","parade","concert"]}'::jsonb,
  '{}'::jsonb
),
(
  'METLIFE_OFFICIAL_EVENTS','MetLife Stadium Official Events',false,'EVENTS','HTTP',
  'https://www.metlifestadium.com/events/','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"VENUE_EVENT","venue":"MetLife Stadium","municipality":"East Rutherford","county":"Bergen","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,3000000,true,true,
  'Official MetLife Stadium event calendar. Ticketmaster remains the structured metro discovery source when its API key is available.',
  'JSONLD','MetLife Stadium','Public venue source; no account required.',
  'East Rutherford / Meadowlands','MetLife Stadium, Meadowlands, Giants, Jets, concert, Route 3, NJ Transit',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["MetLife Stadium","Meadowlands","Giants","Jets","concert","Route 3","NJ Transit"]}'::jsonb,
  '{}'::jsonb
),
(
  'JAVITS_OFFICIAL_EVENTS','Javits Center Official Calendar',false,'EVENTS','HTTP',
  'https://www.javitscenter.com/calendar/','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"VENUE_EVENT","venue":"Javits Center","municipality":"Manhattan","state":"NY","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,5000000,true,true,
  'Official Javits Center public calendar for major conventions and West Side demand.',
  'JSONLD','Javits Center','Public venue source; no account required.',
  'Manhattan West Side / Lincoln Tunnel approach','Javits Center, convention, expo, Comic Con, West Side, Lincoln Tunnel',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Javits Center","convention","expo","Comic Con","West Side","Lincoln Tunnel"]}'::jsonb,
  '{}'::jsonb
),
(
  'PRUDENTIAL_OFFICIAL_EVENTS','Prudential Center Official Events',false,'EVENTS','HTTP',
  'https://www.prucenter.com/events','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"VENUE_EVENT","venue":"Prudential Center","municipality":"Newark","county":"Essex","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,5000000,true,true,
  'Official Prudential Center public event calendar; many events are also covered by Ticketmaster.',
  'JSONLD','Prudential Center','Public venue source; no account required.',
  'Newark, NJ','Prudential Center, Newark, Devils, Seton Hall, concert',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Prudential Center","Newark","Devils","Seton Hall","concert"]}'::jsonb,
  '{}'::jsonb
),
(
  'NJPAC_OFFICIAL_EVENTS','NJPAC Official Events',false,'EVENTS','HTTP',
  'https://www.njpac.org/tickets-events/','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"VENUE_EVENT","venue":"NJPAC","municipality":"Newark","county":"Essex","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,5000000,true,true,
  'Official New Jersey Performing Arts Center public event calendar.',
  'JSONLD','NJPAC','Public venue source; no account required.',
  'Newark, NJ','NJPAC, Newark, concert, performance',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["NJPAC","Newark","concert","performance"]}'::jsonb,
  '{}'::jsonb
),
(
  'EVENTBRITE_DISCOVERY_PLACEHOLDER','Eventbrite Discovery Placeholder',false,'EVENTS','HTTP',
  '','GET','NONE','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'NONE','{}'::jsonb,
  3600,15,1000000,true,true,
  'Placeholder only. Eventbrite broad public event search was deprecated/shut down; use organizer- or venue-specific access only if a future supported source is identified.',
  'CUSTOM','Eventbrite',
  'Do not activate as broad discovery. Add a supported organizer/venue endpoint later through Source Onboarding if useful.',
  'Regional','Eventbrite',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Eventbrite"]}'::jsonb,
  '{}'::jsonb
)
ON CONFLICT (integration_key) DO UPDATE SET
  name=EXCLUDED.name,
  active=integrations.active,
  category=EXCLUDED.category,
  adapter_type=EXCLUDED.adapter_type,
  endpoint_url=EXCLUDED.endpoint_url,
  method=EXCLUDED.method,
  auth_type=EXCLUDED.auth_type,
  auth_config=EXCLUDED.auth_config,
  request_headers=EXCLUDED.request_headers,
  request_query=EXCLUDED.request_query,
  parser_kind=EXCLUDED.parser_kind,
  parser_config=EXCLUDED.parser_config,
  poll_seconds=EXCLUDED.poll_seconds,
  timeout_seconds=EXCLUDED.timeout_seconds,
  max_response_bytes=EXCLUDED.max_response_bytes,
  allow_redirects=EXCLUDED.allow_redirects,
  verify_tls=EXCLUDED.verify_tls,
  notes=EXCLUDED.notes,
  provider_template=EXCLUDED.provider_template,
  source_owner=EXCLUDED.source_owner,
  access_instructions=EXCLUDED.access_instructions,
  geography_scope=EXCLUDED.geography_scope,
  relevance_keywords=EXCLUDED.relevance_keywords,
  attention_config=EXCLUDED.attention_config,
  map_config=EXCLUDED.map_config,
  updated_at=now();

UPDATE integrations
SET provider_template='GENERIC_JSON',
    source_owner='Ticketmaster',
    access_instructions='Add TICKETMASTER_API_KEY to the dashboard/integration-engine environment, run browser TEST, seed silently, then activate.',
    geography_scope='25-mile Weehawken metro',
    relevance_keywords='MetLife Stadium, Meadowlands, Madison Square Garden, Prudential Center, Barclays Center, Yankee Stadium, Citi Field, concert, sports',
    attention_config='{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["MetLife Stadium","Meadowlands","Madison Square Garden","Prudential Center","Barclays Center","Yankee Stadium","Citi Field","concert","sports"]}'::jsonb,
    notes='High-value optional metro discovery source. Requires TICKETMASTER_API_KEY. Discovery API covers Ticketmaster and additional Ticketmaster discovery platforms.',
    updated_at=now()
WHERE integration_key='TICKETMASTER_METRO_EVENTS';
