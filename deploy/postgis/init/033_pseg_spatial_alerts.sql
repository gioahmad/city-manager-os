BEGIN;

CREATE TABLE IF NOT EXISTS pseg_alert_settings (
  settings_key text PRIMARY KEY DEFAULT 'DEFAULT',
  statewide_enabled boolean NOT NULL DEFAULT false,
  minimum_customers integer NOT NULL DEFAULT 500,
  minimum_percent numeric(7,4),
  counties text[] NOT NULL DEFAULT '{}',
  preset text NOT NULL DEFAULT 'STANDARD',
  material_increase_customers integer NOT NULL DEFAULT 250,
  restoration_notifications boolean NOT NULL DEFAULT true,
  reminder_minutes integer NOT NULL DEFAULT 0,
  mapping_enabled boolean NOT NULL DEFAULT true,
  poll_minutes integer NOT NULL DEFAULT 15,
  last_poll_started_at timestamptz,
  last_poll_success_at timestamptz,
  last_poll_error text,
  last_provider_generation text,
  updated_by text NOT NULL DEFAULT 'system',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (settings_key='DEFAULT'),
  CHECK (preset IN ('MAJOR','STANDARD','EXPANDED','CUSTOM')),
  CHECK (minimum_customers BETWEEN 1 AND 10000000),
  CHECK (minimum_percent IS NULL OR minimum_percent BETWEEN 0.0001 AND 100),
  CHECK (material_increase_customers BETWEEN 1 AND 10000000),
  CHECK (reminder_minutes BETWEEN 0 AND 10080),
  CHECK (poll_minutes BETWEEN 15 AND 60)
);

INSERT INTO pseg_alert_settings(settings_key)
VALUES ('DEFAULT')
ON CONFLICT (settings_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS pseg_outage_state (
  scope text NOT NULL,
  county text NOT NULL,
  municipality text NOT NULL,
  customers_out integer NOT NULL DEFAULT 0,
  previous_customers_out integer NOT NULL DEFAULT 0,
  customers_served integer,
  percent_out numeric(9,4),
  outage_count integer,
  etr text,
  started_at timestamptz,
  eligible boolean NOT NULL DEFAULT false,
  cycle_token text,
  current_hash text NOT NULL,
  representative_latitude double precision,
  representative_longitude double precision,
  location_context jsonb NOT NULL DEFAULT '{}'::jsonb,
  provider_record jsonb NOT NULL DEFAULT '{}'::jsonb,
  baseline_at timestamptz NOT NULL DEFAULT now(),
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  last_changed_at timestamptz NOT NULL DEFAULT now(),
  last_alert_created_at timestamptz,
  last_alert_reason text,
  material_baseline_customers_out integer,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (scope,county,municipality),
  CHECK (scope IN ('HUDSON','STATEWIDE')),
  CHECK (customers_out >= 0),
  CHECK (previous_customers_out >= 0),
  CHECK (customers_served IS NULL OR customers_served >= 0),
  CHECK (percent_out IS NULL OR percent_out >= 0),
  CHECK (outage_count IS NULL OR outage_count >= 0),
  CHECK (material_baseline_customers_out IS NULL OR material_baseline_customers_out >= 0),
  CHECK (representative_latitude IS NULL OR representative_latitude BETWEEN -90 AND 90),
  CHECK (representative_longitude IS NULL OR representative_longitude BETWEEN -180 AND 180)
);

ALTER TABLE pseg_outage_state
  ADD COLUMN IF NOT EXISTS material_baseline_customers_out integer;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='public.pseg_outage_state'::regclass
      AND conname='pseg_outage_state_material_baseline_customers_out_check'
  ) THEN
    ALTER TABLE pseg_outage_state
      ADD CONSTRAINT pseg_outage_state_material_baseline_customers_out_check
      CHECK (material_baseline_customers_out IS NULL OR material_baseline_customers_out >= 0);
  END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_pseg_outage_state_active
  ON pseg_outage_state(scope,eligible,customers_out DESC,county,municipality);

CREATE INDEX IF NOT EXISTS idx_alerts_pseg_route_pending
  ON alerts(received_at,alert_id)
  WHERE source='PSEG'
    AND metadata @> '{"_cmos":{"route_pending":true}}'::jsonb;

CREATE OR REPLACE FUNCTION pseg_touch_settings()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_pseg_touch_settings ON pseg_alert_settings;
CREATE TRIGGER trg_pseg_touch_settings
BEFORE UPDATE ON pseg_alert_settings
FOR EACH ROW EXECUTE FUNCTION pseg_touch_settings();

CREATE OR REPLACE VIEW pseg_alert_status AS
SELECT
  s.settings_key,
  s.statewide_enabled,
  s.minimum_customers,
  s.minimum_percent,
  s.counties,
  s.preset,
  s.material_increase_customers,
  s.restoration_notifications,
  s.reminder_minutes,
  s.mapping_enabled,
  s.poll_minutes,
  s.last_poll_started_at,
  s.last_poll_success_at,
  s.last_poll_error,
  s.last_provider_generation,
  o.hudson_active_municipalities,
  o.hudson_customers_out,
  o.statewide_eligible_municipalities,
  o.statewide_eligible_customers_out,
  a.pending_routes
FROM pseg_alert_settings s
CROSS JOIN LATERAL (
  SELECT
    count(*) FILTER (WHERE scope='HUDSON' AND customers_out>0) AS hudson_active_municipalities,
    coalesce(sum(customers_out) FILTER (WHERE scope='HUDSON'),0) AS hudson_customers_out,
    count(*) FILTER (WHERE scope='STATEWIDE' AND eligible) AS statewide_eligible_municipalities,
    coalesce(sum(customers_out) FILTER (WHERE scope='STATEWIDE' AND eligible),0) AS statewide_eligible_customers_out
  FROM pseg_outage_state
) o
CROSS JOIN LATERAL (
  SELECT count(*) AS pending_routes
  FROM alerts
  WHERE source='PSEG'
    AND metadata @> '{"_cmos":{"route_pending":true}}'::jsonb
) a;

GRANT SELECT,INSERT,UPDATE ON pseg_alert_settings TO citymanager_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON pseg_outage_state TO citymanager_app;
GRANT SELECT ON pseg_alert_status TO citymanager_app;
GRANT EXECUTE ON FUNCTION pseg_touch_settings() TO citymanager_app;
GRANT SELECT,INSERT,UPDATE ON alerts TO citymanager_app;
GRANT SELECT,INSERT,UPDATE ON source_health TO citymanager_app;

COMMENT ON TABLE pseg_alert_settings IS
  'Single web-managed PSEG policy. Statewide alerts always exclude Hudson County.';
COMMENT ON TABLE pseg_outage_state IS
  'Durable PSEG source cursor and change state feeding the existing alerts and central delivery path.';
COMMENT ON VIEW pseg_alert_status IS
  'Operational status for the consolidated PSEG spatial and statewide alert path.';

COMMIT;
