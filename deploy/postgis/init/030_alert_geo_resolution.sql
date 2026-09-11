BEGIN;

ALTER TABLE geo_entity_resolutions
  ADD COLUMN IF NOT EXISTS spatial_precision text,
  ADD COLUMN IF NOT EXISTS resolver_version integer NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS last_attempt_at timestamptz,
  ADD COLUMN IF NOT EXISTS attempt_count bigint NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_geo_entity_resolution_alert_state
  ON geo_entity_resolutions(entity_type,status,confidence DESC,updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_alerts_geo_pending
  ON alerts(priority DESC,received_at DESC)
  WHERE geom IS NULL;

CREATE OR REPLACE VIEW alert_geo_coverage AS
SELECT
  a.source,
  count(*) AS total,
  count(*) FILTER (WHERE a.geom IS NOT NULL) AS precise,
  count(*) FILTER (WHERE a.geom IS NULL AND r.geom IS NOT NULL) AS approximate,
  count(*) FILTER (WHERE coalesce(a.geom,r.geom) IS NOT NULL) AS visible,
  count(*) FILTER (WHERE r.status='AMBIGUOUS') AS ambiguous,
  count(*) FILTER (WHERE r.status='UNRESOLVED') AS unresolved,
  count(*) FILTER (WHERE r.id IS NULL) AS pending,
  round(
    100.0 * count(*) FILTER (WHERE coalesce(a.geom,r.geom) IS NOT NULL)
    / nullif(count(*),0),
    2
  ) AS visible_percent,
  round(avg(r.confidence),4) AS average_confidence,
  max(r.updated_at) AS last_resolution_at
FROM alerts a
LEFT JOIN geo_entity_resolutions r
  ON r.entity_type='ALERT' AND r.entity_id=a.id::text
GROUP BY a.source;

GRANT SELECT,INSERT,UPDATE,DELETE ON geo_resolution_cache TO citymanager_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON geo_entity_resolutions TO citymanager_app;
GRANT SELECT ON geo_resolution_coverage,alert_geo_coverage TO citymanager_app;
GRANT SELECT,UPDATE ON alerts TO citymanager_app;
GRANT SELECT,INSERT,UPDATE ON source_health TO citymanager_app;

COMMENT ON VIEW alert_geo_coverage IS
'Per-source precise, approximate, unresolved and pending spatial coverage for existing alerts.';

COMMIT;
