BEGIN;

ALTER TABLE watch_items
  ADD COLUMN IF NOT EXISTS spatial_target_geom geometry(Geometry,4326);

CREATE INDEX IF NOT EXISTS idx_watch_items_spatial_target_geom
  ON watch_items USING gist(spatial_target_geom);

COMMENT ON COLUMN watch_items.spatial_target_geom IS
  'Snapshot of the selected point, parcel, facility, corridor, or area before its operational buffer is applied.';

CREATE OR REPLACE FUNCTION gis_prepare_spatial_watch()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  reference_geom geometry(Geometry,4326);
  reference_parcel integer;
  prepared_target geometry(Geometry,4326);
BEGIN
  IF NEW.spatial_reference_entity_id IS NOT NULL THEN
    SELECT r.geom,r.parcel_objectid
      INTO reference_geom,reference_parcel
    FROM spatial_reference_entities r
    WHERE r.entity_id=NEW.spatial_reference_entity_id;

    IF reference_geom IS NULL THEN
      RAISE EXCEPTION 'spatial reference % was not found', NEW.spatial_reference_entity_id;
    END IF;
    prepared_target := reference_geom;
  ELSE
    prepared_target := coalesce(NEW.spatial_target_geom,NEW.geom);
  END IF;

  IF prepared_target IS NOT NULL THEN
    prepared_target := ST_Force2D(
      CASE WHEN ST_IsValid(prepared_target) THEN prepared_target ELSE ST_MakeValid(prepared_target) END
    );
    IF ST_IsEmpty(prepared_target) OR ST_SRID(prepared_target) <> 4326 THEN
      RAISE EXCEPTION 'spatial watch target must be a non-empty EPSG:4326 geometry';
    END IF;
    NEW.spatial_target_geom := prepared_target;
    NEW.geom := CASE
      WHEN ST_GeometryType(prepared_target)='ST_Point'
        THEN prepared_target::geometry(Point,4326)
      ELSE ST_PointOnSurface(prepared_target)::geometry(Point,4326)
    END;
    NEW.latitude := ST_Y(NEW.geom);
    NEW.longitude := ST_X(NEW.geom);
  END IF;

  IF NEW.nearby_enabled THEN
    IF prepared_target IS NULL THEN
      RAISE EXCEPTION 'spatial matching requires a target geometry';
    END IF;
    NEW.gis_enabled := true;
    NEW.radius_ft := greatest(1.0,least(coalesce(NEW.radius_ft,500.0),26400.0));
    NEW.spatial_scope := upper(coalesce(nullif(btrim(NEW.spatial_scope),''),'RADIUS'));
    NEW.spatial_geom := CASE NEW.spatial_scope
      WHEN 'RADIUS' THEN ST_Buffer(prepared_target::geography,NEW.radius_ft*0.3048)::geometry
      WHEN 'ADJOINING' THEN coalesce((
        SELECT ST_UnaryUnion(ST_Collect(p.geom))
        FROM (
          SELECT gp.geom
          FROM gis_parcels gp
          WHERE gp.objectid=reference_parcel
          UNION ALL
          SELECT adjacent.geom
          FROM gis_adjoining_parcels(reference_parcel,3.0,100) adjacent
        ) p
      ),prepared_target)
      ELSE prepared_target
    END;
  ELSIF NEW.spatial_reference_entity_id IS NULL THEN
    NEW.spatial_geom := NULL;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_gis_prepare_spatial_watch ON watch_items;
CREATE TRIGGER trg_gis_prepare_spatial_watch
BEFORE INSERT OR UPDATE OF nearby_enabled,radius_ft,spatial_scope,spatial_target_geom,
  spatial_reference_entity_id,geom
ON watch_items
FOR EACH ROW EXECUTE FUNCTION gis_prepare_spatial_watch();

UPDATE watch_items
SET spatial_target_geom=coalesce(spatial_target_geom,geom),
    updated_at=updated_at
WHERE nearby_enabled
  AND (spatial_reference_entity_id IS NOT NULL OR spatial_target_geom IS NOT NULL OR geom IS NOT NULL);

CREATE OR REPLACE FUNCTION gis_active_spatial_watch_matches(
  p_alert_id text,
  p_supplied_alert_geom geometry
)
RETURNS TABLE(
  watch_item_id uuid,
  match_type text,
  match_reason text,
  distance_ft double precision,
  radius_ft double precision,
  spatial_scope text
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
WITH target_alert AS (
  SELECT a.id,a.alert_id,a.source,a.category,a.priority,
         CASE
           WHEN a.geom IS NOT NULL THEN a.geom
           WHEN p_supplied_alert_geom IS NOT NULL
             AND ST_SRID(p_supplied_alert_geom)=4326
             AND ST_GeometryType(p_supplied_alert_geom)='ST_Point'
             AND NOT ST_IsEmpty(p_supplied_alert_geom)
             AND ST_IsValid(p_supplied_alert_geom)
             THEN p_supplied_alert_geom::geometry(Point,4326)
           ELSE NULL
         END AS geom
  FROM alerts a
  WHERE a.alert_id=p_alert_id
  LIMIT 1
), candidates AS (
  SELECT
    w.id,
    w.radius_ft,
    w.spatial_scope,
    w.spatial_geom,
    coalesce(w.spatial_target_geom,w.geom) AS target_geom,
    a.geom AS alert_geom
  FROM target_alert a
  JOIN watch_items w
    ON a.geom IS NOT NULL
   AND w.active=true
   AND w.nearby_enabled=true
   AND w.spatial_geom IS NOT NULL
   AND (w.starts_at IS NULL OR w.starts_at<=now())
   AND (w.expires_at IS NULL OR w.expires_at>now())
   AND a.priority>=w.min_priority
   AND (
     cardinality(w.source_filter)=0
     OR EXISTS (
       SELECT 1 FROM unnest(w.source_filter) value
       WHERE upper(btrim(value))=upper(btrim(a.source))
     )
   )
   AND (
     cardinality(w.alert_category_filter)=0
     OR EXISTS (
       SELECT 1 FROM unnest(w.alert_category_filter) value
       WHERE upper(btrim(value))=upper(btrim(a.category))
     )
   )
), matched AS (
  SELECT *,
    CASE upper(coalesce(spatial_scope,'RADIUS'))
      WHEN 'RADIUS' THEN ST_DWithin(
        alert_geom::geography,target_geom::geography,radius_ft*0.3048
      )
      ELSE ST_Intersects(alert_geom,spatial_geom)
    END AS is_match,
    ST_Distance(alert_geom::geography,target_geom::geography)/0.3048 AS measured_distance_ft
  FROM candidates
)
SELECT
  id,
  'PROXIMITY'::text,
  CASE upper(coalesce(spatial_scope,'RADIUS'))
    WHEN 'ADJOINING' THEN format(
      'PROXIMITY alert geometry matched the selected parcel or adjoining-parcel topology; %s ft from target',
      round(measured_distance_ft::numeric,1)
    )
    WHEN 'ENTITY' THEN format(
      'PROXIMITY alert geometry intersected the selected reference; %s ft from target',
      round(measured_distance_ft::numeric,1)
    )
    ELSE format(
      'PROXIMITY alert geometry is %s ft from target, inside %s ft buffer',
      round(measured_distance_ft::numeric,1),round(radius_ft::numeric,1)
    )
  END,
  measured_distance_ft,
  radius_ft,
  spatial_scope
FROM matched
WHERE is_match;
$$;

CREATE OR REPLACE FUNCTION gis_active_spatial_watch_matches(p_alert_id text)
RETURNS TABLE(
  watch_item_id uuid,
  match_type text,
  match_reason text,
  distance_ft double precision,
  radius_ft double precision,
  spatial_scope text
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
SELECT * FROM gis_active_spatial_watch_matches(p_alert_id,NULL::geometry);
$$;

CREATE OR REPLACE FUNCTION gis_spatial_history(
  p_geom geometry,
  p_radius_ft double precision DEFAULT 500.0,
  p_since interval DEFAULT interval '24 hours',
  p_source text DEFAULT NULL,
  p_category text DEFAULT NULL,
  p_min_priority integer DEFAULT 1
)
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
SELECT gis_spatial_impact_context(
  p_geom,
  greatest(1.0,least(coalesce(p_radius_ft,500.0),26400.0)),
  greatest(interval '1 minute',least(coalesce(p_since,interval '24 hours'),interval '365 days'))
) || jsonb_build_object(
  'filters',jsonb_build_object(
    'source',nullif(btrim(p_source),''),
    'category',nullif(btrim(p_category),''),
    'min_priority',greatest(1,least(coalesce(p_min_priority,1),5))
  ),
  'recent_alerts',coalesce((
    SELECT jsonb_agg(jsonb_build_object(
      'alert_id',a.alert_id,'source',a.source,'category',a.category,'subtype',a.subtype,
      'title',a.title,'priority',a.priority,'status',a.status,'received_at',a.received_at,
      'distance_ft',ST_Distance(a.geom::geography,p_geom::geography)/0.3048,
      'geometry',ST_AsGeoJSON(a.geom)::jsonb
    ) ORDER BY a.received_at DESC,a.priority DESC)
    FROM alerts a
    WHERE p_geom IS NOT NULL
      AND a.geom IS NOT NULL
      AND a.received_at>=now()-greatest(
        interval '1 minute',least(coalesce(p_since,interval '24 hours'),interval '365 days')
      )
      AND a.priority>=greatest(1,least(coalesce(p_min_priority,1),5))
      AND (nullif(btrim(p_source),'') IS NULL OR upper(a.source)=upper(btrim(p_source)))
      AND (nullif(btrim(p_category),'') IS NULL OR upper(a.category)=upper(btrim(p_category)))
      AND ST_DWithin(
        a.geom::geography,p_geom::geography,
        greatest(1.0,least(coalesce(p_radius_ft,500.0),26400.0))*0.3048
      )
  ),'[]'::jsonb)
);
$$;

GRANT EXECUTE ON FUNCTION gis_prepare_spatial_watch() TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_active_spatial_watch_matches(text) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_active_spatial_watch_matches(text,geometry) TO citymanager_app;
GRANT EXECUTE ON FUNCTION gis_spatial_history(geometry,double precision,interval,text,text,integer)
  TO citymanager_app;

COMMENT ON FUNCTION gis_active_spatial_watch_matches(text) IS
  'Returns one PostGIS-authoritative match per active, in-window regular Watchlist item for a precisely mapped alert.';
COMMENT ON FUNCTION gis_active_spatial_watch_matches(text,geometry) IS
  'Matches stored precise alert geometry or exact coordinates supplied by the shared Standard Alert payload.';
COMMENT ON FUNCTION gis_spatial_history(geometry,double precision,interval,text,text,integer) IS
  'Queries existing spatially resolved history and operational context without creating a parallel history store.';

COMMIT;
