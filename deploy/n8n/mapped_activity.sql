-- New or changed operational map records use the existing alert/matcher contract.
-- Reference layers are not activity. The activation cutoff prevents a historical send burst.
WITH records AS (
  SELECT 'WORK'::text AS kind,i.id::text AS record_id,
         'MAP_ACTIVITY:WORK:'||i.id AS alert_id,'OPERATIONS'::text AS source,
         'WORK'::text AS category,i.item_type AS subtype,i.title,
         concat_ws(' · ',nullif(i.description,''),nullif(i.next_action,'')) AS message,
         greatest(1,least(coalesce(i.priority,1),5)) AS priority,
         i.municipality,NULL::text AS county,
         coalesce(i.address,i.employee_location,a.fulladdr) AS address,
         coalesce(i.geom,a.geom) AS geom,i.updated_at AS changed_at,
         '/context/issue/'||i.id AS click_url,NULL::text AS source_url,
         ARRAY['mapped-activity','work']::text[] AS tags,NULL::timestamptz AS expires_at
  FROM issues i
  LEFT JOIN LATERAL (
    SELECT ga.geom,ga.fulladdr FROM gis_addresses ga
    WHERE nullif(btrim(coalesce(i.address,i.employee_location,'')),'') IS NOT NULL
      AND lower(btrim(ga.fulladdr))=lower(btrim(coalesce(i.address,i.employee_location,'')))
    ORDER BY CASE WHEN ga.status='A' THEN 0 ELSE 1 END,ga.objectid LIMIT 1
  ) a ON true
  WHERE i.status NOT IN ('RESOLVED','CLOSED')
  UNION ALL
  SELECT 'EVENT',e.id::text,'EVENT_INTEL:'||e.source_event_key,'EVENT_INTELLIGENCE',
         'EVENT',regexp_replace(upper(coalesce(e.event_type,'REGIONAL_EVENT')),'[^A-Z0-9]+','_','g'),e.title,
         concat_ws(' · ',nullif(e.impact_summary,''),nullif(e.road_impact,''),nullif(e.transit_impact,''),nullif(e.description,'')),
         CASE WHEN e.impact_score>=90 THEN 5 WHEN e.impact_level='ALERT' THEN 4 ELSE 1 END,
         e.municipality,e.county,e.address,
         coalesce(e.geom,CASE WHEN e.longitude BETWEEN -180 AND 180 AND e.latitude BETWEEN -90 AND 90
                              THEN ST_SetSRID(ST_MakePoint(e.longitude,e.latitude),4326) END),
         greatest(e.updated_at,e.last_changed_at),e.source_url,e.source_url,
         ARRAY['event-intelligence','regional-event']||CASE WHEN e.impact_level='ALERT' THEN ARRAY['high-impact'] ELSE ARRAY[]::text[] END,
         e.ends_at
  FROM event_intelligence e WHERE e.active
  UNION ALL
  SELECT 'MANAGED_EVENT',e.id::text,'MAP_ACTIVITY:MANAGED_EVENT:'||e.id,'MANAGED_EVENTS',
         'EVENT','MANAGED_EVENT',e.title,coalesce(e.notes,''),greatest(1,least(coalesce(e.priority,1),5)),
         e.municipality,NULL,e.address,a.geom,e.updated_at,'/events?focus='||e.id,NULL,
         ARRAY['mapped-activity','managed-event'],e.ends_at
  FROM operational_events e
  JOIN LATERAL (
    SELECT ga.geom FROM gis_addresses ga
    WHERE nullif(btrim(coalesce(e.address,'')),'') IS NOT NULL
      AND lower(btrim(ga.fulladdr))=lower(btrim(e.address))
    ORDER BY CASE WHEN ga.status='A' THEN 0 ELSE 1 END,ga.objectid LIMIT 1
  ) a ON true
  WHERE e.active AND e.event_status NOT IN ('COMPLETED','CANCELLED')
  UNION ALL
  SELECT 'TRANSIT',o.id::text,'TRANSIT_INTEL:'||p.provider_key||':'||coalesce(nullif(o.external_key,''),o.id::text),
         'TRANSIT_INTELLIGENCE','TRANSIT',regexp_replace(upper(p.provider_key||'_'||coalesce(o.mode,'TRANSIT')),'[^A-Z0-9]+','_','g'),o.title,
         concat_ws(' · ',nullif(o.description,''),nullif(o.route_name,''),nullif(o.asset_name,'')),
         CASE WHEN o.impact_score>=90 THEN 5 WHEN o.impact_level='ALERT' THEN 4 ELSE 1 END,
         o.municipality,o.county,NULL,o.geom,greatest(o.updated_at,o.last_changed_at),o.source_url,o.source_url,
         ARRAY['transit-intelligence',lower(p.provider_key),lower(coalesce(o.mode,'transit'))]||CASE WHEN o.impact_level='ALERT' THEN ARRAY['high-impact'] ELSE ARRAY[]::text[] END,
         o.ends_at
  FROM transit_observations o JOIN transit_providers p ON p.id=o.provider_id
  WHERE o.active AND o.impact_level IN ('WATCH','ALERT')
  UNION ALL
  SELECT 'VEHICLE',v.id::text,'MAP_ACTIVITY:VEHICLE:'||v.id,'TRANSIT_INTELLIGENCE',
         'TRANSIT','VEHICLE',v.name,concat_ws(' · ',v.mode,v.metadata->>'route_name'),1,v.municipality,v.county,NULL,
         v.geom,v.updated_at,'/transit',NULL,ARRAY['mapped-activity','vehicle'],NULL
  FROM transit_assets v
  WHERE v.active AND v.asset_type='VEHICLE' AND v.last_seen_at>=now()-interval '20 minutes'
), candidates AS (
  SELECT r.*,md5(jsonb_build_array(r.title,r.message,r.priority,r.tags,r.click_url,r.expires_at,ST_AsEWKT(r.geom))::text) AS fingerprint
  FROM records r
  WHERE r.geom IS NOT NULL AND r.changed_at>=TIMESTAMPTZ '__CMOS_ACTIVATED_AT__'
    AND (r.expires_at IS NULL OR r.expires_at>now())
    AND EXISTS (
      SELECT 1 FROM watch_items w
      WHERE w.active AND w.nearby_enabled AND w.spatial_geom IS NOT NULL
        AND (w.starts_at IS NULL OR w.starts_at<=now())
        AND (w.expires_at IS NULL OR w.expires_at>now())
        AND CASE WHEN w.spatial_scope='RADIUS'
          THEN ST_DWithin(r.geom::geography,coalesce(w.spatial_target_geom,w.geom)::geography,w.radius_ft*0.3048)
          ELSE ST_Intersects(r.geom,w.spatial_geom) END
    )
), pending AS (
  SELECT c.* FROM candidates c LEFT JOIN alerts a ON a.alert_id=c.alert_id
  WHERE coalesce(a.metadata#>>'{_cmos,mapped_activity_fingerprint}','')<>c.fingerprint
  ORDER BY c.changed_at,c.alert_id LIMIT 50
), saved AS (
  INSERT INTO alerts(alert_id,source,source_event_id,category,subtype,status,event_action,title,message,
                     priority,municipality,county,location,tags,click_url,source_url,observed_at,
                     source_updated_at,expires_at,metadata,search_text,geom)
  SELECT p.alert_id,p.source,coalesce(a.source_event_id,p.record_id),p.category,coalesce(nullif(p.subtype,''),p.kind),'ACTIVE',coalesce(a.event_action,'NEW'),
         p.title,coalesce(nullif(p.message,''),'Mapped '||lower(p.kind)||' activity.'),p.priority,p.municipality,p.county,
         jsonb_strip_nulls(jsonb_build_object('label',p.address,'address',p.address,
           'municipality',p.municipality,'county',p.county,'latitude',ST_Y(p.geom),'longitude',ST_X(p.geom))),
         p.tags,p.click_url,p.source_url,p.changed_at,p.changed_at,p.expires_at,
         coalesce(a.metadata,'{}'::jsonb)||jsonb_build_object('mapped_activity_only',true,
           'mapped_record_kind',p.kind,'mapped_record_id',p.record_id,'mapped_activity_candidate',p.fingerprint,
           'mapped_activity_queued_at',now()),
         concat_ws(' ',p.source,p.category,p.title,p.message,p.municipality,p.county,p.address),p.geom
  FROM pending p LEFT JOIN alerts a ON a.alert_id=p.alert_id
  ON CONFLICT(alert_id) DO UPDATE SET
    title=EXCLUDED.title,message=EXCLUDED.message,priority=EXCLUDED.priority,
    municipality=EXCLUDED.municipality,county=EXCLUDED.county,location=EXCLUDED.location,
    tags=EXCLUDED.tags,click_url=EXCLUDED.click_url,source_url=EXCLUDED.source_url,
    observed_at=EXCLUDED.observed_at,source_updated_at=EXCLUDED.source_updated_at,
    expires_at=EXCLUDED.expires_at,metadata=EXCLUDED.metadata,search_text=EXCLUDED.search_text,
    geom=EXCLUDED.geom,updated_at=now()
  RETURNING *
)
SELECT (to_jsonb(s)-'id'-'geom'-'raw_payload')||jsonb_build_object(
  'metadata',s.metadata,'location',s.location
) AS alert FROM saved s;
