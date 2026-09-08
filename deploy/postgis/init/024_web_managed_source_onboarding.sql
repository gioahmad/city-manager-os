-- #47 Web-managed Source Onboarding
-- Additive control-plane metadata and activation safety. No raw secrets are stored.

ALTER TABLE integrations
  ADD COLUMN IF NOT EXISTS provider_template text NOT NULL DEFAULT 'CUSTOM',
  ADD COLUMN IF NOT EXISTS source_owner text,
  ADD COLUMN IF NOT EXISTS source_contact text,
  ADD COLUMN IF NOT EXISTS access_instructions text,
  ADD COLUMN IF NOT EXISTS geography_scope text,
  ADD COLUMN IF NOT EXISTS relevance_keywords text,
  ADD COLUMN IF NOT EXISTS attention_config jsonb NOT NULL
    DEFAULT '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":[]}'::jsonb,
  ADD COLUMN IF NOT EXISTS map_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS config_version bigint NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS last_test_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_test_ok boolean,
  ADD COLUMN IF NOT EXISTS last_test_config_version bigint,
  ADD COLUMN IF NOT EXISTS last_test_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS activation_override_at timestamptz,
  ADD COLUMN IF NOT EXISTS activation_override_by text,
  ADD COLUMN IF NOT EXISTS activation_override_reason text;

CREATE TABLE IF NOT EXISTS integration_activation_audit (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  integration_id uuid NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
  action text NOT NULL,
  actor text NOT NULL,
  reason text,
  config_version bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (action IN ('ACTIVATE','PAUSE','OVERRIDE_ACTIVATE'))
);

CREATE INDEX IF NOT EXISTS idx_integration_activation_audit_recent
  ON integration_activation_audit(integration_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_integrations_onboarding_test
  ON integrations(active, last_test_ok, last_test_at DESC);

CREATE OR REPLACE FUNCTION cmos_integration_onboarding_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  config_changed boolean;
  adapter text;
  parser text;
BEGIN
  config_changed :=
       NEW.adapter_type IS DISTINCT FROM OLD.adapter_type
    OR NEW.endpoint_url IS DISTINCT FROM OLD.endpoint_url
    OR NEW.method IS DISTINCT FROM OLD.method
    OR NEW.auth_type IS DISTINCT FROM OLD.auth_type
    OR NEW.auth_config IS DISTINCT FROM OLD.auth_config
    OR NEW.request_headers IS DISTINCT FROM OLD.request_headers
    OR NEW.request_query IS DISTINCT FROM OLD.request_query
    OR NEW.request_body IS DISTINCT FROM OLD.request_body
    OR NEW.parser_kind IS DISTINCT FROM OLD.parser_kind
    OR NEW.parser_config IS DISTINCT FROM OLD.parser_config
    OR NEW.timeout_seconds IS DISTINCT FROM OLD.timeout_seconds
    OR NEW.max_response_bytes IS DISTINCT FROM OLD.max_response_bytes
    OR NEW.allow_redirects IS DISTINCT FROM OLD.allow_redirects
    OR NEW.verify_tls IS DISTINCT FROM OLD.verify_tls;

  IF config_changed THEN
    NEW.config_version := COALESCE(OLD.config_version, 1) + 1;
    NEW.last_test_at := NULL;
    NEW.last_test_ok := NULL;
    NEW.last_test_config_version := NULL;
    NEW.last_test_summary := '{}'::jsonb;
    NEW.activation_override_at := NULL;
    NEW.activation_override_by := NULL;
    NEW.activation_override_reason := NULL;

    -- Editing live connection/parser settings is fail-safe: pause and retest.
    IF OLD.active THEN
      NEW.active := false;
    END IF;
  END IF;

  IF NEW.active AND NOT OLD.active THEN
    adapter := upper(COALESCE(NEW.adapter_type, 'HTTP'));
    parser := upper(COALESCE(NEW.parser_kind, 'NONE'));

    IF NULLIF(btrim(COALESCE(NEW.endpoint_url, '')), '') IS NULL THEN
      RAISE EXCEPTION 'Activation blocked: endpoint URL is not configured';
    END IF;

    IF adapter NOT IN (
      'HTTP',
      'NJT_BUSDATA',
      'NJT_RAILDATA',
      'NJT_BUS_GTFS',
      'NJT_RAIL_GTFS',
      'NJT_RSS',
      'PATH_REALTIME',
      'TRANSIT_GTFS_URL',
      'NYW_ADVISORIES'
    ) THEN
      RAISE EXCEPTION 'Activation blocked: unsupported adapter %', adapter;
    END IF;

    IF adapter = 'HTTP' AND parser = 'NONE' THEN
      RAISE EXCEPTION 'Activation blocked: HTTP sources require a supported parser';
    END IF;

    IF adapter = 'TRANSIT_GTFS_URL'
       AND NULLIF(btrim(COALESCE(NEW.parser_config->>'provider_key', '')), '') IS NULL THEN
      RAISE EXCEPTION 'Activation blocked: TRANSIT_GTFS_URL requires parser_config.provider_key';
    END IF;

    IF COALESCE(NEW.last_test_ok, false)
       AND NEW.last_test_config_version = NEW.config_version THEN
      RETURN NEW;
    END IF;

    IF NEW.activation_override_at IS NOT NULL
       AND NEW.activation_override_at >= now() - interval '5 minutes'
       AND NULLIF(btrim(COALESCE(NEW.activation_override_by, '')), '') IS NOT NULL
       AND length(btrim(COALESCE(NEW.activation_override_reason, ''))) >= 12 THEN
      RETURN NEW;
    END IF;

    RAISE EXCEPTION
      'Activation blocked: current configuration requires a successful TEST or recorded executive override';
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_integrations_onboarding_guard ON integrations;

CREATE TRIGGER trg_integrations_onboarding_guard
BEFORE UPDATE ON integrations
FOR EACH ROW
EXECUTE FUNCTION cmos_integration_onboarding_guard();

GRANT SELECT, INSERT, UPDATE, DELETE ON integration_activation_audit TO citymanager_app;
