BEGIN;

CREATE TABLE IF NOT EXISTS geo_resolution_cache (
    cache_key text PRIMARY KEY,
    resolver_version integer NOT NULL,
    status text NOT NULL CHECK (status IN ('RESOLVED','AMBIGUOUS','UNRESOLVED','INVALID')),
    match_type text,
    normalized_input text NOT NULL DEFAULT '',
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    confidence numeric(5,4) NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    dataset_versions jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom geometry(Point,4326),
    hit_count bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_geo_resolution_cache_status
    ON geo_resolution_cache(status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_geo_resolution_cache_normalized
    ON geo_resolution_cache(normalized_input);
CREATE INDEX IF NOT EXISTS idx_geo_resolution_cache_geom
    ON geo_resolution_cache USING gist(geom);

CREATE TABLE IF NOT EXISTS geo_entity_resolutions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    cache_key text REFERENCES geo_resolution_cache(cache_key) ON DELETE SET NULL,
    status text NOT NULL CHECK (status IN ('RESOLVED','AMBIGUOUS','UNRESOLVED','INVALID')),
    match_type text,
    confidence numeric(5,4) NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
    resolved_label text,
    municipality text,
    county text,
    state text,
    postal_code text,
    parcel_id text,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom geometry(Point,4326),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(entity_type,entity_id)
);

CREATE INDEX IF NOT EXISTS idx_geo_entity_resolutions_status
    ON geo_entity_resolutions(entity_type,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_geo_entity_resolutions_parcel
    ON geo_entity_resolutions(parcel_id) WHERE parcel_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_geo_entity_resolutions_geom
    ON geo_entity_resolutions USING gist(geom);

CREATE OR REPLACE VIEW geo_resolution_coverage AS
SELECT entity_type,
       count(*) AS total,
       count(*) FILTER (WHERE status='RESOLVED') AS resolved,
       count(*) FILTER (WHERE status='AMBIGUOUS') AS ambiguous,
       count(*) FILTER (WHERE status='UNRESOLVED') AS unresolved,
       round(100.0 * count(*) FILTER (WHERE status='RESOLVED') / NULLIF(count(*),0),2) AS coverage_percent,
       round(avg(confidence),4) AS average_confidence,
       max(updated_at) AS last_updated_at
FROM geo_entity_resolutions
GROUP BY entity_type;

COMMENT ON TABLE geo_resolution_cache IS
'Shared local-only Geo Resolver cache. Remote services are dataset refresh sources, never runtime lookup dependencies.';

COMMENT ON TABLE geo_entity_resolutions IS
'Resolution state, confidence, geometry, parcel linkage and provenance for existing City Manager OS entities.';

COMMIT;
