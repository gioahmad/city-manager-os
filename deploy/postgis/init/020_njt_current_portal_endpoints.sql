-- Align NJ TRANSIT Phase 1 connector metadata with the current Developer Portal.
-- This migration does not activate any connector and stores no credentials.

UPDATE integrations
SET endpoint_url='https://pcsdata.njtransit.com/api/BUSDV2/getVehicleLocations',
    auth_config='{"username_env":"NJT_USERNAME","password_env":"NJT_PASSWORD","api_family":"BUSDV2"}'::jsonb,
    notes='Current token-based BUSDV2 adapter with legacy BUSDATA fallback. Captures regional BUS and HBLR vehicle context without alerting per vehicle.',
    updated_at=now()
WHERE integration_key='NJT_BUS_LIVE';

UPDATE integrations
SET endpoint_url='https://raildata.njtransit.com/api/TrainData/getStationMSG',
    auth_config='{"username_env":"NJT_USERNAME","password_env":"NJT_PASSWORD","api_family":"TrainData"}'::jsonb,
    notes='Current RailData production host. Cached token resolution supports product-specific and portal-guide token endpoints.',
    updated_at=now()
WHERE integration_key='NJT_RAIL_LIVE';

UPDATE integrations
SET endpoint_url='https://pcsdata.njtransit.com/api/GTFSG2/getGTFS',
    auth_config='{"username_env":"NJT_USERNAME","password_env":"NJT_PASSWORD","api_family":"GTFSG2"}'::jsonb,
    notes='Token-based NJ TRANSIT bus GTFS adapter. Tries GTFSG2 then GTFS using cached authentication; no direct download URL is stored.',
    updated_at=now()
WHERE integration_key='NJT_BUS_GTFS';

UPDATE integrations
SET endpoint_url='https://raildata.njtransit.com/api/GTFSRT/getGTFS',
    auth_config='{"username_env":"NJT_USERNAME","password_env":"NJT_PASSWORD","api_family":"GTFSRT"}'::jsonb,
    notes='Current rail GTFS production host. Token resolution supports GTFSRT, TrainData and portal-guide generic token endpoints.',
    updated_at=now()
WHERE integration_key='NJT_RAIL_GTFS';

UPDATE integrations
SET active=false,updated_at=now()
WHERE integration_key IN (
  'NJT_BUS_LIVE',
  'NJT_RAIL_LIVE',
  'NJT_BUS_GTFS',
  'NJT_RAIL_GTFS'
);
