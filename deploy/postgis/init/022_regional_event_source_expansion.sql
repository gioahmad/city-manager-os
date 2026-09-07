-- Regional Event Intelligence source expansion.
-- New municipal sources begin paused for controlled
-- live acceptance and silent baseline seeding.

UPDATE integrations
SET active=false,
    updated_at=now()
WHERE integration_key='NYC_PERMITTED_EVENTS';

UPDATE integrations
SET request_query = jsonb_build_object(
      '$limit', 3000,
      '$where',
        'event_borough = ''Manhattan'' AND start_date_time >= ''{today_local}T00:00:00''',
      '$order',
        'start_date_time ASC'
    ),
    max_response_bytes=5000000,
    notes=
      'Authoritative NYC Open Data permitted-events feed. Dynamic current-local-date filtering returns current/future Manhattan events first.',
    updated_at=now()
WHERE integration_key='NYC_PERMITTED_EVENTS';


INSERT INTO integrations (
  integration_key,
  name,
  active,
  category,
  adapter_type,
  endpoint_url,
  method,
  auth_type,
  auth_config,
  request_headers,
  request_query,
  parser_kind,
  parser_config,
  poll_seconds,
  timeout_seconds,
  max_response_bytes,
  allow_redirects,
  verify_tls,
  notes
)
VALUES
(
  'WNY_OFFICIAL_EVENTS',
  'West New York Official Calendar',
  false,
  'EVENTS',
  'HTTP',
  'https://www.westnewyorknj.org/calendar/?ical=1',
  'GET',
  'NONE',
  '{}'::jsonb,
  '{"User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,
  'ICS_EVENTS',
  jsonb_build_object(
    'defaults',
    jsonb_build_object(
      'event_type','MUNICIPAL_EVENT',
      'municipality','West New York',
      'county','Hudson',
      'state','NJ',
      'default_timezone','America/New_York'
    )
  ),
  1800,
  20,
  5000000,
  true,
  true,
  'Official Town of West New York public calendar ICS feed.'
),
(
  'SECAUCUS_OFFICIAL_EVENTS',
  'Secaucus Official Events Calendar',
  false,
  'EVENTS',
  'HTTP',
  'https://secaucusnj.gov/index.php?id=92&option=com_dpcalendar&task=ical.download',
  'GET',
  'NONE',
  '{}'::jsonb,
  '{"User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,
  'ICS_EVENTS',
  jsonb_build_object(
    'defaults',
    jsonb_build_object(
      'event_type','MUNICIPAL_EVENT',
      'municipality','Secaucus',
      'county','Hudson',
      'state','NJ',
      'default_timezone','America/New_York'
    )
  ),
  1800,
  20,
  5000000,
  true,
  true,
  'Official Town of Secaucus Events calendar ICS feed.'
),
(
  'JERSEY_CITY_OFFICIAL_EVENTS',
  'Jersey City Official Calendar',
  false,
  'EVENTS',
  'HTTP',
  'https://www.jerseycitynj.gov/ICalendarHandler?calendarId=12409811',
  'GET',
  'NONE',
  '{}'::jsonb,
  '{"User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,
  'ICS_EVENTS',
  jsonb_build_object(
    'defaults',
    jsonb_build_object(
      'event_type','MUNICIPAL_EVENT',
      'municipality','Jersey City',
      'county','Hudson',
      'state','NJ',
      'default_timezone','America/New_York'
    )
  ),
  1800,
  20,
  5000000,
  true,
  true,
  'Official City of Jersey City main calendar ICS feed.'
)
ON CONFLICT (integration_key)
DO UPDATE SET
  name=EXCLUDED.name,
  active=false,
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
  updated_at=now();
