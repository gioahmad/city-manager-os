BEGIN;

-- Statewide parcel/address tables are promoted after a fresh PostGIS bootstrap.
-- Defer SQL-function relation checks so this numbered migration remains safe for
-- both an existing production database and a future empty-database bootstrap.
SET LOCAL check_function_bodies = off;

CREATE TABLE IF NOT EXISTS spatial_reference_entities (
  entity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type text NOT NULL,
  entity_subtype text,
  canonical_name text NOT NULL,
  aliases text[] NOT NULL DEFAULT '{}',
  normalized_address text,
  municipality text,
  county text,
  state text,
  postal_code text,
  geom geometry(Geometry,4326) NOT NULL,
  centroid geometry(Point,4326) NOT NULL,
  source_provider text NOT NULL,
  source_record_id text,
  source_reference text,
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
  confidence numeric(5,4) NOT NULL DEFAULT 1.0,
  authoritative boolean NOT NULL DEFAULT false,
  importance_tier smallint NOT NULL DEFAULT 3,
  parcel_id text,
  parcel_objectid integer,
  transit_asset_id uuid REFERENCES transit_assets(id) ON DELETE SET NULL,
  default_buffer_ft double precision,
  active boolean NOT NULL DEFAULT true,
  retired_at timestamptz,
  verified_at timestamptz,
  source_updated_at timestamptz,
  refreshed_at timestamptz NOT NULL DEFAULT now(),
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (entity_type IN ('FACILITY','VENUE','CORRIDOR','LANDMARK','PARCEL_REFERENCE','SERVICE_AREA','OTHER')),
  CHECK (btrim(canonical_name) <> ''),
  CHECK (btrim(source_provider) <> ''),
  CHECK (confidence >= 0 AND confidence <= 1),
  CHECK (importance_tier BETWEEN 1 AND 5),
  CHECK (default_buffer_ft IS NULL OR default_buffer_ft BETWEEN 1 AND 26400),
  CHECK (ST_SRID(geom) = 4326),
  CHECK (ST_SRID(centroid) = 4326),
  CHECK (active OR retired_at IS NOT NULL)
);

CREATE OR REPLACE FUNCTION spatial_reference_prepare_entity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.geom IS NULL OR ST_IsEmpty(NEW.geom) THEN
    RAISE EXCEPTION 'spatial reference geometry is required';
  END IF;
  IF ST_SRID(NEW.geom) <> 4326 THEN
    RAISE EXCEPTION 'spatial reference geometry must use EPSG:4326';
  END IF;
  NEW.geom := ST_Force2D(CASE WHEN ST_IsValid(NEW.geom) THEN NEW.geom ELSE ST_MakeValid(NEW.geom) END);
  IF ST_IsEmpty(NEW.geom) THEN
    RAISE EXCEPTION 'spatial reference geometry is empty after validation';
  END IF;
  NEW.centroid := CASE
    WHEN ST_GeometryType(NEW.geom) = 'ST_Point' THEN NEW.geom::geometry(Point,4326)
    ELSE ST_PointOnSurface(NEW.geom)::geometry(Point,4326)
  END;
  NEW.entity_type := upper(btrim(NEW.entity_type));
  NEW.entity_subtype := nullif(upper(btrim(NEW.entity_subtype)), '');
  NEW.canonical_name := btrim(NEW.canonical_name);
  NEW.source_provider := upper(btrim(NEW.source_provider));
  NEW.updated_at := now();
  IF NEW.active THEN
    NEW.retired_at := NULL;
  ELSIF NEW.retired_at IS NULL THEN
    NEW.retired_at := now();
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_spatial_reference_prepare_entity ON spatial_reference_entities;
CREATE TRIGGER trg_spatial_reference_prepare_entity
BEFORE INSERT OR UPDATE ON spatial_reference_entities
FOR EACH ROW EXECUTE FUNCTION spatial_reference_prepare_entity();

CREATE UNIQUE INDEX IF NOT EXISTS idx_spatial_reference_source_unique
  ON spatial_reference_entities(source_provider,source_record_id)
  WHERE source_record_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_spatial_reference_type_active
  ON spatial_reference_entities(entity_type,entity_subtype,active,importance_tier,canonical_name);
CREATE INDEX IF NOT EXISTS idx_spatial_reference_name
  ON spatial_reference_entities(lower(canonical_name));
CREATE INDEX IF NOT EXISTS idx_spatial_reference_aliases
  ON spatial_reference_entities USING gin(aliases);
CREATE INDEX IF NOT EXISTS idx_spatial_reference_parcel
  ON spatial_reference_entities(parcel_id,parcel_objectid)
  WHERE parcel_id IS NOT NULL OR parcel_objectid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_spatial_reference_transit_asset
  ON spatial_reference_entities(transit_asset_id)
  WHERE transit_asset_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_spatial_reference_geom
  ON spatial_reference_entities USING gist(geom);
CREATE INDEX IF NOT EXISTS idx_spatial_reference_centroid
  ON spatial_reference_entities USING gist(centroid);

ALTER TABLE watch_items
  ADD COLUMN IF NOT EXISTS spatial_reference_entity_id uuid
  REFERENCES spatial_reference_entities(entity_id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS spatial_geom geometry(Geometry,4326),
  ADD COLUMN IF NOT EXISTS spatial_scope text NOT NULL DEFAULT 'ENTITY';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='public.watch_items'::regclass
      AND conname='watch_items_spatial_scope_check'
  ) THEN
    ALTER TABLE watch_items ADD CONSTRAINT watch_items_spatial_scope_check
      CHECK (spatial_scope IN ('ENTITY','ADJOINING','RADIUS'));
  END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_items_spatial_reference
  ON watch_items(spatial_reference_entity_id)
  WHERE spatial_reference_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_watch_items_spatial_geom
  ON watch_items USING gist(spatial_geom);

CREATE OR REPLACE FUNCTION spatial_reference_classify_name(p_name text)
RETURNS TABLE(entity_type text,entity_subtype text,importance_tier smallint)
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
SELECT
  CASE
    WHEN coalesce(p_name,'') ~* '\m(ARENA|STADIUM|CONVENTION|EVENT[[:space:]]+CENTER|EVENT[[:space:]]+SPACE|VENUE)\M'
      THEN 'VENUE'
    WHEN coalesce(p_name,'') ~* '\m(PARK|PLAZA|PUBLIC[[:space:]]+SPACE|LANDMARK)\M'
      THEN 'LANDMARK'
    ELSE 'FACILITY'
  END,
  CASE
    WHEN coalesce(p_name,'') ~* '\m(HOSPITAL|MEDICAL[[:space:]]+CENTER|ACUTE[[:space:]]+CARE)\M' THEN 'HOSPITAL'
    WHEN coalesce(p_name,'') ~* '\m(URGENT[[:space:]]+CARE)\M' THEN 'URGENT_CARE'
    WHEN coalesce(p_name,'') ~* '\m(HEALTH[[:space:]]+CENTER|HEALTHCARE|CLINIC)\M' THEN 'HEALTH_CENTER'
    WHEN coalesce(p_name,'') ~* '\m(EMS|AMBULANCE|RESCUE[[:space:]]+SQUAD)\M' THEN 'EMS'
    WHEN coalesce(p_name,'') ~* '\m(FIRE|FIREHOUSE)\M' THEN 'FIRE'
    WHEN coalesce(p_name,'') ~* '\m(POLICE|SHERIFF)\M' THEN 'POLICE'
    WHEN coalesce(p_name,'') ~* '\m(EMERGENCY[[:space:]]+MANAGEMENT|OEM)\M' THEN 'OEM'
    WHEN coalesce(p_name,'') ~* '\m(UNIVERSITY)\M' THEN 'UNIVERSITY'
    WHEN coalesce(p_name,'') ~* '\m(COLLEGE)\M' THEN 'COLLEGE'
    WHEN coalesce(p_name,'') ~* '\m(CHILD[[:space:]]*CARE|DAY[[:space:]]*CARE|PRESCHOOL)\M' THEN 'CHILDCARE'
    WHEN coalesce(p_name,'') ~* '\m(SCHOOL|ACADEMY)\M' THEN 'SCHOOL'
    WHEN coalesce(p_name,'') ~* '\m(NURSING|ASSISTED[[:space:]]+LIVING|SENIOR[[:space:]]+CARE)\M' THEN 'SENIOR_CARE'
    WHEN coalesce(p_name,'') ~* '\m(COURT|COURTHOUSE)\M' THEN 'COURT'
    WHEN coalesce(p_name,'') ~* '\m(TOWN[[:space:]]+HALL|CITY[[:space:]]+HALL|BOROUGH[[:space:]]+HALL|MUNICIPAL|PUBLIC[[:space:]]+BUILDING)\M' THEN 'GOVERNMENT'
    WHEN coalesce(p_name,'') ~* '\m(FERRY[[:space:]]+TERMINAL)\M' THEN 'FERRY_TERMINAL'
    WHEN coalesce(p_name,'') ~* '\m(BUS[[:space:]]+TERMINAL)\M' THEN 'BUS_TERMINAL'
    WHEN coalesce(p_name,'') ~* '\m(TRANSIT|TRAIN|RAIL|STATION|TERMINAL)\M' THEN 'TRANSIT_STATION'
    WHEN coalesce(p_name,'') ~* '\m(AIRPORT)\M' THEN 'AIRPORT'
    WHEN coalesce(p_name,'') ~* '\m(HELIPORT)\M' THEN 'HELIPORT'
    WHEN coalesce(p_name,'') ~* '\m(ARENA)\M' THEN 'ARENA'
    WHEN coalesce(p_name,'') ~* '\m(STADIUM)\M' THEN 'STADIUM'
    WHEN coalesce(p_name,'') ~* '\m(CONVENTION)\M' THEN 'CONVENTION_CENTER'
    WHEN coalesce(p_name,'') ~* '\m(SHELTER|COOLING[[:space:]]+CENTER|WARMING[[:space:]]+CENTER)\M' THEN 'EMERGENCY_RESOURCE'
    WHEN coalesce(p_name,'') ~* '\m(PUMP[[:space:]]+STATION|SUBSTATION|WATER[[:space:]]+TREATMENT|SEWER|UTILITY)\M' THEN 'UTILITY'
    WHEN coalesce(p_name,'') ~* '\m(PARK)\M' THEN 'PARK'
    ELSE 'LANDMARK'
  END,
  CASE
    WHEN coalesce(p_name,'') ~* '\m(HOSPITAL|EMS|AMBULANCE|FIRE|FIREHOUSE|POLICE|SHERIFF|EMERGENCY[[:space:]]+MANAGEMENT|OEM|SHELTER|PUMP[[:space:]]+STATION|SUBSTATION)\M' THEN 1
    WHEN coalesce(p_name,'') ~* '\m(URGENT[[:space:]]+CARE|HEALTH[[:space:]]+CENTER|UNIVERSITY|COLLEGE|SCHOOL|ACADEMY|NURSING|ASSISTED[[:space:]]+LIVING|TERMINAL|STATION|AIRPORT|HELIPORT|ARENA|STADIUM|CONVENTION|COURT)\M' THEN 2
    ELSE 3
  END::smallint;
$$;

CREATE OR REPLACE FUNCTION spatial_reference_refresh_local_sources()
RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
  parcel_rows bigint := 0;
  landmark_rows bigint := 0;
  transit_rows bigint := 0;
  retired_rows bigint := 0;
  affected_rows bigint := 0;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('city-manager-os:spatial-reference-refresh'));

  IF to_regclass('public.gis_parcels') IS NOT NULL THEN
    WITH candidates AS (
      SELECT p.objectid,p.pcl_guid,p.pams_pin,p.mun_name,p.county,p.prop_loc,p.zip5,
             p.fac_name,p.pcllastupd,p.geom,k.entity_type,k.entity_subtype,k.importance_tier
      FROM gis_parcels p
      CROSS JOIN LATERAL spatial_reference_classify_name(p.fac_name) k
      WHERE p.geom IS NOT NULL AND NOT ST_IsEmpty(p.geom)
        AND nullif(btrim(p.fac_name),'') IS NOT NULL
        AND upper(trim(p.county))='HUDSON'
    ), upserted AS (
      INSERT INTO spatial_reference_entities(
        entity_type,entity_subtype,canonical_name,normalized_address,municipality,county,state,
        postal_code,geom,centroid,source_provider,source_record_id,source_reference,provenance,confidence,
        authoritative,importance_tier,parcel_id,parcel_objectid,verified_at,source_updated_at,
        refreshed_at,metadata
      )
      SELECT c.entity_type,c.entity_subtype,btrim(c.fac_name),nullif(btrim(c.prop_loc),''),
             nullif(btrim(c.mun_name),''),nullif(btrim(c.county),''),'NJ',nullif(btrim(c.zip5),''),
             c.geom,ST_PointOnSurface(c.geom),'NJOGIS_PARCEL_FACILITY',c.objectid::text,
             'https://njogis-newjersey.opendata.arcgis.com/',
             jsonb_build_object('dataset','gis_parcels','objectid',c.objectid,'pams_pin',c.pams_pin,
                                'pcl_guid',c.pcl_guid),1.0,true,c.importance_tier,
             coalesce(c.pcl_guid,c.pams_pin),c.objectid,now(),c.pcllastupd,now(),
             jsonb_build_object('source_kind','PARCEL_FACILITY')
      FROM candidates c
      ON CONFLICT (source_provider,source_record_id) WHERE source_record_id IS NOT NULL
      DO UPDATE SET entity_type=EXCLUDED.entity_type,entity_subtype=EXCLUDED.entity_subtype,
                    canonical_name=EXCLUDED.canonical_name,importance_tier=EXCLUDED.importance_tier,
                    source_reference=EXCLUDED.source_reference,normalized_address=EXCLUDED.normalized_address,
                    municipality=EXCLUDED.municipality,county=EXCLUDED.county,state=EXCLUDED.state,
                    postal_code=EXCLUDED.postal_code,geom=EXCLUDED.geom,
                    provenance=EXCLUDED.provenance,parcel_id=EXCLUDED.parcel_id,
                    parcel_objectid=EXCLUDED.parcel_objectid,verified_at=now(),
                    source_updated_at=EXCLUDED.source_updated_at,refreshed_at=now(),
                    metadata=spatial_reference_entities.metadata || EXCLUDED.metadata,
                    active=true,retired_at=NULL
      RETURNING 1
    )
    SELECT count(*) INTO parcel_rows FROM upserted;

    UPDATE spatial_reference_entities r
    SET active=false,retired_at=coalesce(r.retired_at,now()),refreshed_at=now()
    WHERE r.source_provider='NJOGIS_PARCEL_FACILITY' AND r.active
      AND NOT EXISTS (
        SELECT 1 FROM gis_parcels p
        WHERE p.objectid::text=r.source_record_id AND p.geom IS NOT NULL
          AND NOT ST_IsEmpty(p.geom) AND nullif(btrim(p.fac_name),'') IS NOT NULL
          AND upper(trim(p.county))='HUDSON'
      );
    GET DIAGNOSTICS retired_rows = ROW_COUNT;
  END IF;

  IF to_regclass('public.gis_landmark_aliases') IS NOT NULL
     AND to_regclass('public.gis_addresses') IS NOT NULL THEN
    WITH candidates AS (
      SELECT DISTINCT ON (la.site_nguid,lower(btrim(la.aclandmark)))
             la.site_nguid,btrim(la.aclandmark) AS landmark_name,
             a.objectid AS address_objectid,a.fulladdr,a.post_comm,a.post_code,a.state,a.dateupdate,a.geom,
             p.objectid AS parcel_objectid,p.pcl_guid,p.pams_pin,p.mun_name,p.county,
             k.entity_type,k.entity_subtype,k.importance_tier
      FROM gis_landmark_aliases la
      JOIN gis_addresses a ON a.site_nguid=la.site_nguid
                            AND a.geom IS NOT NULL AND coalesce(a.status,'A')='A'
      LEFT JOIN LATERAL (
        SELECT p.objectid,p.pcl_guid,p.pams_pin,p.mun_name,p.county
        FROM gis_parcels p
        WHERE p.geom IS NOT NULL
          AND ((a.pcl_guid IS NOT NULL AND p.pcl_guid=a.pcl_guid) OR ST_Covers(p.geom,a.geom))
        ORDER BY CASE WHEN a.pcl_guid IS NOT NULL AND p.pcl_guid=a.pcl_guid THEN 0 ELSE 1 END,p.objectid
        LIMIT 1
      ) p ON true
      CROSS JOIN LATERAL spatial_reference_classify_name(la.aclandmark) k
      WHERE nullif(btrim(la.site_nguid),'') IS NOT NULL
        AND nullif(btrim(la.aclandmark),'') IS NOT NULL
        AND (
          upper(trim(a.county))='882278'
          OR EXISTS (
            SELECT 1 FROM spatial_reference_entities existing
            WHERE existing.source_provider='NJOGIS_LANDMARK_ALIAS'
              AND existing.source_record_id=la.site_nguid || ':' || md5(lower(btrim(la.aclandmark)))
          )
        )
      ORDER BY la.site_nguid,lower(btrim(la.aclandmark)),a.objectid
    ), filtered AS (
      SELECT c.* FROM candidates c
      WHERE NOT EXISTS (
        SELECT 1 FROM spatial_reference_entities r
        WHERE r.source_provider='NJOGIS_PARCEL_FACILITY' AND r.active
          AND r.parcel_objectid=c.parcel_objectid
          AND lower(regexp_replace(r.canonical_name,'[^a-zA-Z0-9]+','','g'))=
              lower(regexp_replace(c.landmark_name,'[^a-zA-Z0-9]+','','g'))
      )
    ), upserted AS (
      INSERT INTO spatial_reference_entities(
        entity_type,entity_subtype,canonical_name,normalized_address,municipality,county,state,
        postal_code,geom,centroid,source_provider,source_record_id,source_reference,provenance,confidence,
        authoritative,importance_tier,parcel_id,parcel_objectid,verified_at,source_updated_at,
        refreshed_at,metadata
      )
      SELECT c.entity_type,c.entity_subtype,c.landmark_name,nullif(btrim(c.fulladdr),''),
             coalesce(nullif(btrim(c.mun_name),''),nullif(btrim(c.post_comm),'')),
             nullif(btrim(c.county),''),coalesce(nullif(btrim(c.state),''),'NJ'),
             nullif(btrim(c.post_code),''),c.geom,c.geom,'NJOGIS_LANDMARK_ALIAS',
             c.site_nguid || ':' || md5(lower(c.landmark_name)),
             'https://njogis-newjersey.opendata.arcgis.com/',
             jsonb_build_object('dataset','gis_landmark_aliases','site_nguid',c.site_nguid,
                                'address_objectid',c.address_objectid),1.0,true,c.importance_tier,
             coalesce(c.pcl_guid,c.pams_pin),c.parcel_objectid,now(),c.dateupdate,now(),
             jsonb_build_object('source_kind','LANDMARK_ALIAS')
      FROM filtered c
      ON CONFLICT (source_provider,source_record_id) WHERE source_record_id IS NOT NULL
      DO UPDATE SET entity_type=EXCLUDED.entity_type,entity_subtype=EXCLUDED.entity_subtype,
                    canonical_name=EXCLUDED.canonical_name,importance_tier=EXCLUDED.importance_tier,
                    source_reference=EXCLUDED.source_reference,normalized_address=EXCLUDED.normalized_address,
                    municipality=EXCLUDED.municipality,county=EXCLUDED.county,state=EXCLUDED.state,
                    postal_code=EXCLUDED.postal_code,geom=EXCLUDED.geom,
                    provenance=EXCLUDED.provenance,parcel_id=EXCLUDED.parcel_id,
                    parcel_objectid=EXCLUDED.parcel_objectid,verified_at=now(),
                    source_updated_at=EXCLUDED.source_updated_at,refreshed_at=now(),
                    metadata=spatial_reference_entities.metadata || EXCLUDED.metadata,
                    active=true,retired_at=NULL
      RETURNING 1
    )
    SELECT count(*) INTO landmark_rows FROM upserted;

    UPDATE spatial_reference_entities r
    SET active=false,retired_at=coalesce(r.retired_at,now()),refreshed_at=now()
    WHERE r.source_provider='NJOGIS_LANDMARK_ALIAS' AND r.active
      AND NOT EXISTS (
        SELECT 1 FROM gis_landmark_aliases la
        JOIN gis_addresses a ON a.site_nguid=la.site_nguid
        WHERE la.site_nguid || ':' || md5(lower(btrim(la.aclandmark)))=r.source_record_id
          AND nullif(btrim(la.aclandmark),'') IS NOT NULL
          AND a.geom IS NOT NULL AND coalesce(a.status,'A')='A'
      );
    GET DIAGNOSTICS affected_rows = ROW_COUNT;
    retired_rows := retired_rows + affected_rows;
  END IF;

  IF to_regclass('public.transit_assets') IS NOT NULL THEN
    WITH candidates AS (
      SELECT ta.*,tp.provider_key,tp.name AS provider_name
      FROM transit_assets ta JOIN transit_providers tp ON tp.id=ta.provider_id
      WHERE ta.active AND ta.geom IS NOT NULL AND NOT ST_IsEmpty(ta.geom)
    ), upserted AS (
      INSERT INTO spatial_reference_entities(
        entity_type,entity_subtype,canonical_name,aliases,municipality,county,state,
        geom,centroid,source_provider,source_record_id,source_reference,provenance,confidence,authoritative,
        importance_tier,transit_asset_id,verified_at,source_updated_at,refreshed_at,metadata
      )
      SELECT 'FACILITY','TRANSIT_' || upper(coalesce(nullif(btrim(c.asset_type),''),
                                                  nullif(btrim(c.mode),''),'ASSET')),
             c.name,CASE WHEN nullif(btrim(c.short_name),'') IS NOT NULL AND c.short_name<>c.name
                         THEN ARRAY[c.short_name] ELSE '{}'::text[] END,
             c.municipality,c.county,c.state,c.geom,c.geom,'CMOS_TRANSIT_ASSET',c.id::text,
             'City Manager OS transit_assets',
             jsonb_build_object('dataset','transit_assets','provider',c.provider_key,
                                'asset_key',c.asset_key),1.0,true,
             CASE WHEN upper(coalesce(c.asset_type,'')) ~ '(TERMINAL|STATION|HUB)' THEN 2 ELSE 3 END,
             c.id,now(),c.updated_at,now(),
             c.metadata || jsonb_build_object('source_kind','TRANSIT_ASSET','provider_name',c.provider_name)
      FROM candidates c
      ON CONFLICT (source_provider,source_record_id) WHERE source_record_id IS NOT NULL
      DO UPDATE SET entity_type=EXCLUDED.entity_type,entity_subtype=EXCLUDED.entity_subtype,
                    canonical_name=EXCLUDED.canonical_name,aliases=EXCLUDED.aliases,
                    source_reference=EXCLUDED.source_reference,importance_tier=EXCLUDED.importance_tier,
                    municipality=EXCLUDED.municipality,
                    county=EXCLUDED.county,state=EXCLUDED.state,geom=EXCLUDED.geom,
                    provenance=EXCLUDED.provenance,transit_asset_id=EXCLUDED.transit_asset_id,
                    verified_at=now(),source_updated_at=EXCLUDED.source_updated_at,
                    refreshed_at=now(),metadata=spatial_reference_entities.metadata || EXCLUDED.metadata,
                    active=true,retired_at=NULL
      RETURNING 1
    )
    SELECT count(*) INTO transit_rows FROM upserted;

    UPDATE spatial_reference_entities r
    SET active=false,retired_at=coalesce(r.retired_at,now()),refreshed_at=now()
    WHERE r.source_provider='CMOS_TRANSIT_ASSET' AND r.active
      AND NOT EXISTS (
        SELECT 1 FROM transit_assets ta
        WHERE ta.id::text=r.source_record_id AND ta.active AND ta.geom IS NOT NULL
      );
    GET DIAGNOSTICS affected_rows = ROW_COUNT;
    retired_rows := retired_rows + affected_rows;
  END IF;

  RETURN jsonb_build_object(
    'parcel_facilities_refreshed',parcel_rows,
    'landmarks_refreshed',landmark_rows,
    'transit_assets_refreshed',transit_rows,
    'retired',retired_rows,
    'active_total',(SELECT count(*) FROM spatial_reference_entities WHERE active),
    'refreshed_at',now()
  );
END;
$$;

CREATE OR REPLACE FUNCTION gis_parcel_for_point(
  p_lat double precision,
  p_lon double precision,
  p_tolerance_ft double precision DEFAULT 3.0
)
RETURNS TABLE (
  parcel_objectid integer,
  parcel_id text,
  pams_pin text,
  municipality text,
  block text,
  lot text,
  qualifier text,
  property_location text,
  relation_type text,
  distance_ft double precision,
  geom geometry(MultiPolygon,4326)
)
LANGUAGE sql
STABLE
AS $$
WITH origin AS (
  SELECT ST_SetSRID(ST_MakePoint(p_lon,p_lat),4326) AS geom,
         greatest(0.0,least(coalesce(p_tolerance_ft,3.0),25.0)) AS tolerance_ft
)
SELECT p.objectid,p.pcl_guid::text,p.pams_pin::text,p.mun_name::text,p.pclblock::text,
       p.pcllot::text,p.pclqcode::text,p.prop_loc::text,
       CASE WHEN ST_Covers(p.geom,o.geom) THEN 'SAME_PARCEL' ELSE 'FUZZY_BOUNDARY' END,
       ST_Distance(p.geom::geography,o.geom::geography)/0.3048,p.geom
FROM origin o JOIN gis_parcels p
  ON p.geom && ST_Expand(o.geom,o.tolerance_ft/200000.0)
 AND (ST_Covers(p.geom,o.geom)
      OR ST_DWithin(p.geom::geography,o.geom::geography,o.tolerance_ft*0.3048))
WHERE p.geom IS NOT NULL
ORDER BY CASE WHEN ST_Covers(p.geom,o.geom) THEN 0 ELSE 1 END,
         ST_Distance(p.geom::geography,o.geom::geography),p.objectid
LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION gis_addresses_for_parcel(
  p_parcel_objectid integer,
  p_tolerance_ft double precision DEFAULT 3.0,
  p_limit integer DEFAULT 250
)
RETURNS TABLE (
  address_objectid integer,
  full_address text,
  municipality text,
  postal_code text,
  parcel_id text,
  link_method text,
  relation_type text,
  distance_ft double precision,
  geom geometry(Point,4326)
)
LANGUAGE sql
STABLE
AS $$
WITH target AS (
  SELECT objectid,pcl_guid,geom
  FROM gis_parcels
  WHERE objectid=p_parcel_objectid AND geom IS NOT NULL
  LIMIT 1
), linked AS (
  SELECT a.objectid,a.fulladdr::text,a.post_comm::text,a.post_code::text,a.pcl_guid::text,
         'PCL_GUID'::text AS link_method,
         ST_Distance(a.geom::geography,t.geom::geography)/0.3048 AS distance_ft,
         a.geom
  FROM target t
  JOIN gis_addresses a
    ON t.pcl_guid IS NOT NULL AND a.pcl_guid=t.pcl_guid
  WHERE a.geom IS NOT NULL AND coalesce(a.status,'A')='A'
), spatial AS (
  SELECT a.objectid,a.fulladdr::text,a.post_comm::text,a.post_code::text,a.pcl_guid::text,
         CASE WHEN ST_Covers(t.geom,a.geom) THEN 'POINT_IN_PARCEL' ELSE 'FUZZY_BOUNDARY' END::text,
         ST_Distance(a.geom::geography,t.geom::geography)/0.3048 AS distance_ft,
         a.geom
  FROM target t
  JOIN gis_addresses a
    ON a.geom && ST_Expand(t.geom,greatest(0.0,least(coalesce(p_tolerance_ft,3.0),25.0))/250000.0)
   AND ST_DWithin(a.geom::geography,t.geom::geography,
                  greatest(0.0,least(coalesce(p_tolerance_ft,3.0),25.0))*0.3048)
  WHERE a.geom IS NOT NULL AND coalesce(a.status,'A')='A'
    AND (t.pcl_guid IS NULL OR a.pcl_guid IS DISTINCT FROM t.pcl_guid)
), combined AS (
  SELECT * FROM linked
  UNION ALL
  SELECT * FROM spatial
)
SELECT c.objectid,c.fulladdr,c.post_comm,c.post_code,c.pcl_guid,c.link_method,
       CASE WHEN c.link_method='FUZZY_BOUNDARY' THEN 'FUZZY_BOUNDARY' ELSE 'SAME_PARCEL' END,
       c.distance_ft,c.geom
FROM combined c
ORDER BY CASE c.link_method WHEN 'PCL_GUID' THEN 0 WHEN 'POINT_IN_PARCEL' THEN 1 ELSE 2 END,
         c.fulladdr NULLS LAST,c.objectid
LIMIT greatest(1,least(coalesce(p_limit,250),1000));
$$;

CREATE OR REPLACE FUNCTION gis_parcels_within_radius(
  p_parcel_objectid integer,
  p_radius_ft double precision DEFAULT 500.0,
  p_limit integer DEFAULT 250,
  p_adjoining_tolerance_ft double precision DEFAULT 3.0
)
RETURNS TABLE (
  parcel_objectid integer,
  parcel_id text,
  pams_pin text,
  municipality text,
  block text,
  lot text,
  property_location text,
  relation_type text,
  distance_ft double precision,
  shared_boundary_ft double precision,
  separating_reference_id uuid,
  separating_reference_name text,
  geom geometry(MultiPolygon,4326)
)
LANGUAGE sql
STABLE
AS $$
WITH target AS (
  SELECT objectid,pcl_guid,geom
  FROM gis_parcels
  WHERE objectid=p_parcel_objectid AND geom IS NOT NULL
  LIMIT 1
), candidates AS (
  SELECT p.*,t.geom AS target_geom,
         ST_Distance(p.geom::geography,t.geom::geography)/0.3048 AS distance_ft,
         CASE WHEN ST_Touches(p.geom,t.geom)
              THEN ST_Length(ST_CollectionExtract(ST_Intersection(ST_Boundary(p.geom),ST_Boundary(t.geom)),2)::geography)/0.3048
              ELSE 0.0 END AS shared_boundary_ft,
         ST_ShortestLine(t.geom,p.geom) AS connector
  FROM target t
  JOIN gis_parcels p
    ON p.objectid<>t.objectid
   AND p.geom && ST_Expand(t.geom,greatest(1.0,least(coalesce(p_radius_ft,500.0),26400.0))/250000.0)
   AND ST_DWithin(p.geom::geography,t.geom::geography,
                  greatest(1.0,least(coalesce(p_radius_ft,500.0),26400.0))*0.3048)
  WHERE p.geom IS NOT NULL
), classified AS (
  SELECT c.*,sep.entity_id AS separating_reference_id,
         sep.canonical_name AS separating_reference_name,
         CASE
           WHEN sep.entity_id IS NOT NULL AND sep.entity_subtype='STREET' THEN 'ACROSS_STREET'
           WHEN sep.entity_id IS NOT NULL THEN 'ACROSS_CORRIDOR'
           WHEN ST_Touches(c.geom,c.target_geom)
             OR c.distance_ft<=greatest(0.0,least(coalesce(p_adjoining_tolerance_ft,3.0),25.0))
             THEN 'ADJOINING_BOUNDARY'
           ELSE 'NEARBY_PARCEL'
         END AS relation_type
  FROM candidates c
  LEFT JOIN LATERAL (
    SELECT r.entity_id,r.canonical_name,r.entity_subtype
    FROM spatial_reference_entities r
    WHERE r.active=true AND r.entity_type='CORRIDOR'
      AND ST_Intersects(r.geom,c.connector)
    ORDER BY r.importance_tier,ST_Distance(r.centroid::geography,ST_Centroid(c.connector)::geography)
    LIMIT 1
  ) sep ON true
)
SELECT c.objectid,c.pcl_guid::text,c.pams_pin::text,c.mun_name::text,c.pclblock::text,c.pcllot::text,
       c.prop_loc::text,c.relation_type,c.distance_ft,c.shared_boundary_ft,
       c.separating_reference_id,c.separating_reference_name,c.geom
FROM classified c
ORDER BY c.distance_ft,c.objectid
LIMIT greatest(1,least(coalesce(p_limit,250),1000));
$$;

CREATE OR REPLACE FUNCTION gis_parcels_within_radius(
  p_lat double precision,
  p_lon double precision,
  p_radius_ft double precision DEFAULT 500.0,
  p_limit integer DEFAULT 250
)
RETURNS TABLE (
  parcel_objectid integer,
  parcel_id text,
  pams_pin text,
  municipality text,
  block text,
  lot text,
  property_location text,
  relation_type text,
  distance_ft double precision,
  shared_boundary_ft double precision,
  separating_reference_id uuid,
  separating_reference_name text,
  geom geometry(MultiPolygon,4326)
)
LANGUAGE sql
STABLE
AS $$
WITH target AS (
  SELECT p.parcel_objectid,p.parcel_id,p.pams_pin,p.municipality,p.block,p.lot,
         p.property_location,p.geom
  FROM gis_parcel_for_point(p_lat,p_lon,3.0) p
), combined AS (
  SELECT t.parcel_objectid,t.parcel_id,t.pams_pin,t.municipality,t.block,t.lot,
         t.property_location,'SAME_PARCEL'::text AS relation_type,0.0::double precision AS distance_ft,
         0.0::double precision AS shared_boundary_ft,NULL::uuid AS separating_reference_id,
         NULL::text AS separating_reference_name,t.geom
  FROM target t
  UNION ALL
  SELECT p.parcel_objectid,p.parcel_id,p.pams_pin,p.municipality,p.block,p.lot,
         p.property_location,p.relation_type,p.distance_ft,p.shared_boundary_ft,
         p.separating_reference_id,p.separating_reference_name,p.geom
  FROM target t
  CROSS JOIN LATERAL gis_parcels_within_radius(
    t.parcel_objectid,p_radius_ft,greatest(1,least(coalesce(p_limit,250),1000)),3.0
  ) p
)
SELECT c.* FROM combined c
ORDER BY CASE c.relation_type WHEN 'SAME_PARCEL' THEN 0 WHEN 'ADJOINING_BOUNDARY' THEN 1 ELSE 2 END,
         c.distance_ft,c.parcel_objectid
LIMIT greatest(1,least(coalesce(p_limit,250),1000));
$$;

CREATE OR REPLACE FUNCTION gis_adjoining_parcels(
  p_parcel_objectid integer,
  p_tolerance_ft double precision DEFAULT 3.0,
  p_limit integer DEFAULT 100
)
RETURNS TABLE (
  parcel_objectid integer,
  parcel_id text,
  pams_pin text,
  municipality text,
  block text,
  lot text,
  property_location text,
  relation_type text,
  distance_ft double precision,
  shared_boundary_ft double precision,
  geom geometry(MultiPolygon,4326)
)
LANGUAGE sql
STABLE
AS $$
SELECT p.parcel_objectid,p.parcel_id,p.pams_pin,p.municipality,p.block,p.lot,p.property_location,
       p.relation_type,p.distance_ft,p.shared_boundary_ft,p.geom
FROM gis_parcels_within_radius(
  p_parcel_objectid,
  greatest(1.0,least(coalesce(p_tolerance_ft,3.0),25.0)),
  greatest(1,least(coalesce(p_limit,100),500)),
  p_tolerance_ft
) p
WHERE p.relation_type='ADJOINING_BOUNDARY'
ORDER BY p.distance_ft,p.parcel_objectid;
$$;

CREATE OR REPLACE FUNCTION gis_spatial_impact_context(
  p_geom geometry,
  p_radius_ft double precision DEFAULT 500.0,
  p_since interval DEFAULT interval '30 days'
)
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
WITH settings AS (
  SELECT ST_Force2D(p_geom) AS geom,
         greatest(1.0,least(coalesce(p_radius_ft,500.0),26400.0)) AS radius_ft,
         greatest(interval '1 hour',least(coalesce(p_since,interval '30 days'),interval '365 days')) AS since
), nearby_references AS (
  SELECT r.entity_id,r.entity_type,r.entity_subtype,r.canonical_name,r.importance_tier,
         ST_Distance(r.geom::geography,s.geom::geography)/0.3048 AS distance_ft
  FROM settings s JOIN spatial_reference_entities r
    ON r.active AND r.geom IS NOT NULL
   AND ST_DWithin(r.geom::geography,s.geom::geography,s.radius_ft*0.3048)
  ORDER BY r.importance_tier,distance_ft,r.canonical_name LIMIT 100
), nearby_watches AS (
  SELECT w.watch_id,w.display_name,w.watch_type,w.min_priority,w.radius_ft,w.spatial_scope,
         ST_Distance(coalesce(w.spatial_geom,w.geom)::geography,s.geom::geography)/0.3048 AS distance_ft
  FROM settings s JOIN watch_items w
    ON w.active AND coalesce(w.spatial_geom,w.geom) IS NOT NULL
   AND (w.starts_at IS NULL OR w.starts_at<=now())
   AND (w.expires_at IS NULL OR w.expires_at>now())
   AND ST_DWithin(coalesce(w.spatial_geom,w.geom)::geography,s.geom::geography,s.radius_ft*0.3048)
  ORDER BY distance_ft,w.display_name LIMIT 100
), nearby_issues AS (
  SELECT i.id,i.title,i.category,i.priority,i.status,i.updated_at,
         ST_Distance(i.geom::geography,s.geom::geography)/0.3048 AS distance_ft
  FROM settings s JOIN issues i
    ON i.geom IS NOT NULL AND i.status NOT IN ('RESOLVED','CLOSED')
   AND ST_DWithin(i.geom::geography,s.geom::geography,s.radius_ft*0.3048)
  ORDER BY i.priority DESC,i.updated_at DESC LIMIT 100
), recent_alerts AS (
  SELECT a.id,a.alert_id,a.source,a.category,a.subtype,a.status,a.title,a.priority,a.received_at,
         ST_Distance(coalesce(a.geom,g.geom)::geography,s.geom::geography)/0.3048 AS distance_ft
  FROM settings s JOIN alerts a ON a.received_at>=now()-s.since
  LEFT JOIN geo_entity_resolutions g ON g.entity_type='ALERT' AND g.entity_id=a.id::text
  WHERE coalesce(a.geom,g.geom) IS NOT NULL
    AND ST_DWithin(coalesce(a.geom,g.geom)::geography,s.geom::geography,s.radius_ft*0.3048)
  ORDER BY a.received_at DESC,a.priority DESC LIMIT 100
), nearby_events AS (
  SELECT e.id,e.title,e.event_type,e.venue,e.status,e.impact_level,e.impact_score,
         e.starts_at,e.ends_at,
         ST_Distance(e.geom::geography,s.geom::geography)/0.3048 AS distance_ft
  FROM settings s JOIN event_intelligence e
    ON e.active AND e.geom IS NOT NULL
   AND coalesce(e.ends_at,e.starts_at,e.last_seen_at)>=now()-s.since
   AND ST_DWithin(e.geom::geography,s.geom::geography,s.radius_ft*0.3048)
  ORDER BY e.starts_at DESC NULLS LAST,e.impact_score DESC LIMIT 100
), nearby_transit AS (
  SELECT o.id,o.title,o.mode,o.route_name,o.asset_name,o.status,o.impact_level,o.impact_score,
         o.starts_at,o.ends_at,o.last_seen_at,
         ST_Distance(o.geom::geography,s.geom::geography)/0.3048 AS distance_ft
  FROM settings s JOIN transit_observations o
    ON o.active AND o.geom IS NOT NULL AND o.last_seen_at>=now()-s.since
   AND ST_DWithin(o.geom::geography,s.geom::geography,s.radius_ft*0.3048)
  ORDER BY o.last_seen_at DESC,o.impact_score DESC LIMIT 100
), flood_context AS (
  SELECT z.id,z.fld_zone,z.zone_subty,z.sfha_tf,z.static_bfe
  FROM settings s JOIN gis_flood_zones z
    ON z.geom IS NOT NULL
   AND ST_DWithin(z.geom::geography,s.geom::geography,s.radius_ft*0.3048)
  ORDER BY CASE WHEN z.sfha_tf='T' THEN 0 ELSE 1 END,z.fld_zone,z.id LIMIT 100
)
SELECT jsonb_build_object(
  'radius_ft',s.radius_ft,'since',s.since,'generated_at',now(),
  'references',coalesce((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.importance_tier,x.distance_ft,x.canonical_name) FROM nearby_references x),'[]'::jsonb),
  'active_watches',coalesce((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.distance_ft,x.display_name) FROM nearby_watches x),'[]'::jsonb),
  'open_issues',coalesce((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.priority DESC,x.updated_at DESC) FROM nearby_issues x),'[]'::jsonb),
  'recent_alerts',coalesce((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.received_at DESC,x.priority DESC) FROM recent_alerts x),'[]'::jsonb),
  'events',coalesce((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.starts_at DESC NULLS LAST) FROM nearby_events x),'[]'::jsonb),
  'transit',coalesce((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.last_seen_at DESC) FROM nearby_transit x),'[]'::jsonb),
  'flood_zones',coalesce((SELECT jsonb_agg(to_jsonb(x)) FROM flood_context x),'[]'::jsonb)
)
FROM settings s;
$$;

CREATE OR REPLACE FUNCTION gis_parcel_context(
  p_parcel_objectid integer,
  p_radius_ft double precision DEFAULT 500.0
)
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
WITH target AS (
  SELECT objectid,pcl_guid,pams_pin,mun_name,pclblock,pcllot,prop_loc,pcllastupd,geom
  FROM gis_parcels
  WHERE objectid=p_parcel_objectid AND geom IS NOT NULL
  LIMIT 1
)
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM target) THEN NULL ELSE jsonb_build_object(
  'parcel',(SELECT jsonb_build_object(
    'objectid',objectid,'parcel_id',pcl_guid,'pams_pin',pams_pin,'municipality',mun_name,
    'block',pclblock,'lot',pcllot,'property_location',prop_loc,'source_updated_at',pcllastupd,
    'geometry',ST_AsGeoJSON(geom)::jsonb
  ) FROM target),
  'addresses',coalesce((SELECT jsonb_agg(
    (to_jsonb(a)-'geom') || jsonb_build_object('geometry',ST_AsGeoJSON(a.geom)::jsonb)
    ORDER BY a.full_address,a.address_objectid
  ) FROM gis_addresses_for_parcel(p_parcel_objectid,3.0,250) a),'[]'::jsonb),
  'adjoining_parcels',coalesce((SELECT jsonb_agg(
    (to_jsonb(p)-'geom') || jsonb_build_object('geometry',ST_AsGeoJSON(p.geom)::jsonb)
    ORDER BY p.distance_ft,p.parcel_objectid
  ) FROM gis_adjoining_parcels(p_parcel_objectid,3.0,100) p),'[]'::jsonb),
  'nearby_parcels',coalesce((SELECT jsonb_agg(
    (to_jsonb(p)-'geom') || jsonb_build_object('geometry',ST_AsGeoJSON(p.geom)::jsonb)
    ORDER BY p.distance_ft,p.parcel_objectid
  ) FROM gis_parcels_within_radius(p_parcel_objectid,p_radius_ft,250,3.0) p
    WHERE p.relation_type<>'ADJOINING_BOUNDARY'),'[]'::jsonb),
  'reference_entities',coalesce((SELECT jsonb_agg(jsonb_build_object(
    'entity_id',r.entity_id,'entity_type',r.entity_type,'entity_subtype',r.entity_subtype,
    'canonical_name',r.canonical_name,'distance_ft',ST_Distance(r.centroid::geography,t.geom::geography)/0.3048
  ) ORDER BY r.importance_tier,r.canonical_name)
  FROM target t JOIN spatial_reference_entities r
    ON r.active=true
   AND ST_DWithin(r.centroid::geography,t.geom::geography,
                  greatest(1.0,least(coalesce(p_radius_ft,500.0),26400.0))*0.3048))),'[]'::jsonb),
  'spatial_impact',(SELECT gis_spatial_impact_context(
    t.geom,greatest(1.0,least(coalesce(p_radius_ft,500.0),26400.0)),interval '30 days'
  ) FROM target t),
  'radius_ft',greatest(1.0,least(coalesce(p_radius_ft,500.0),26400.0)),
  'generated_at',now()
) END;
$$;

CREATE OR REPLACE VIEW spatial_reference_catalog_status AS
SELECT entity_type,coalesce(entity_subtype,'') AS entity_subtype,
       count(*) AS total,count(*) FILTER (WHERE active) AS active,
       count(*) FILTER (WHERE authoritative) AS authoritative,
       count(*) FILTER (WHERE parcel_id IS NOT NULL OR parcel_objectid IS NOT NULL) AS parcel_linked,
       max(refreshed_at) AS last_refreshed_at,max(updated_at) AS last_updated_at
FROM spatial_reference_entities
GROUP BY entity_type,coalesce(entity_subtype,'');

GRANT SELECT,INSERT,UPDATE,DELETE ON spatial_reference_entities TO citymanager_app;
GRANT SELECT ON spatial_reference_catalog_status TO citymanager_app;
GRANT EXECUTE ON FUNCTION spatial_reference_refresh_local_sources() TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_parcel_for_point(double precision,double precision,double precision) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_addresses_for_parcel(integer,double precision,integer) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_parcels_within_radius(integer,double precision,integer,double precision) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_parcels_within_radius(double precision,double precision,double precision,integer) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_adjoining_parcels(integer,double precision,integer) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_spatial_impact_context(geometry,double precision,interval) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_parcel_context(integer,double precision) TO citymanager_app;

COMMENT ON TABLE spatial_reference_entities IS
  'Canonical reusable reference geography. References become normal watch_items only through Watch This.';
COMMENT ON COLUMN watch_items.spatial_geom IS
  'Exact reference, adjoining-parcel union, or buffered impact geometry retained for Mapping Center and #56 spatial matching.';
COMMENT ON FUNCTION gis_spatial_impact_context(geometry,double precision,interval) IS
  'Read-only context over the existing Watchlist, Command Center, Alerts, Events, Transit, and flood layers.';

COMMIT;
