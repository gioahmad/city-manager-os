-- Ensure the private dashboard's application role can administer Mapping Center layers.
-- This is intentionally idempotent and repairs drift where map tables exist but
-- write privileges were not preserved.

GRANT SELECT, INSERT, UPDATE, DELETE
ON TABLE map_layers, map_features
TO citymanager_app;
