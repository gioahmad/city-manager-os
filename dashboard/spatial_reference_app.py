import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import db_conn, query_all, query_one, templates
from map_app import app


ENTITY_TYPES = ("FACILITY", "VENUE", "CORRIDOR", "LANDMARK", "PARCEL_REFERENCE", "SERVICE_AREA", "OTHER")
SOURCE_KINDS = ("ADDRESS", "PARCEL", "LANDMARK", "TRANSIT_ASSET", "CUSTOM_FEATURE")
SPATIAL_SCOPES = ("ENTITY", "ADJOINING", "RADIUS")
LOCAL_ZONE = ZoneInfo("America/New_York")
RELEASE_ID = "issue-58-regional-spatial-reference-v1"


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _aliases(value: str):
    return list(dict.fromkeys(part.strip() for part in re.split(r"[,\n]", value or "") if part.strip()))


def _local_datetime(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, "Invalid date or time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_ZONE)
    return parsed.astimezone(timezone.utc)


def _datetime_local(value):
    if not value:
        return ""
    return value.astimezone(LOCAL_ZONE).strftime("%Y-%m-%dT%H:%M")


def _entity(entity_id: uuid.UUID):
    row = query_one(
        """
        SELECT r.*,ST_AsGeoJSON(r.geom)::json AS geometry,
               ST_X(r.centroid) AS longitude,ST_Y(r.centroid) AS latitude,
               w.id AS watch_item_id,w.watch_id,w.active AS watch_active,w.radius_ft AS watch_radius_ft,
               w.min_priority AS watch_min_priority,w.starts_at AS watch_starts_at,
               w.expires_at AS watch_expires_at,w.source_filter AS watch_source_filter,
               w.alert_category_filter AS watch_category_filter,w.spatial_scope AS watch_spatial_scope
        FROM spatial_reference_entities r
        LEFT JOIN watch_items w ON w.spatial_reference_entity_id=r.entity_id
        WHERE r.entity_id=%s
        """,
        (entity_id,),
    )
    if not row:
        raise HTTPException(404, "Spatial reference not found")
    return row


@app.get("/api/spatial-reference/release")
def spatial_reference_release():
    return {
        "release_id": RELEASE_ID,
        "architecture": "SEE IT -> TRACK IT -> TELL ME",
        "shared_systems": ["PostGIS", "Mapping Center", "Watchlist", "Subscribers", "Routing", "Delivery Guard", "ntfy"],
    }


@app.get("/spatial-reference", response_class=HTMLResponse)
def spatial_reference_index(request: Request, q: str = "", entity_type: str = "", msg: str = ""):
    q = q.strip()
    entity_type = entity_type.strip().upper()
    needle = f"%{q}%"
    rows = query_all(
        """
        SELECT r.entity_id,r.entity_type,r.entity_subtype,r.canonical_name,r.aliases,
               r.normalized_address,r.municipality,r.state,r.source_provider,r.authoritative,
               r.importance_tier,r.parcel_objectid,r.active,r.updated_at,
               ST_X(r.centroid) AS longitude,ST_Y(r.centroid) AS latitude,
               w.id AS watch_item_id,w.active AS watch_active
        FROM spatial_reference_entities r
        LEFT JOIN watch_items w ON w.spatial_reference_entity_id=r.entity_id
        WHERE (%s='' OR r.entity_type=%s)
          AND (%s='' OR r.canonical_name ILIKE %s OR coalesce(r.normalized_address,'') ILIKE %s
               OR EXISTS (SELECT 1 FROM unnest(r.aliases) a WHERE a ILIKE %s))
        ORDER BY r.active DESC,r.importance_tier,r.canonical_name
        LIMIT 250
        """,
        (entity_type, entity_type, q, needle, needle, needle),
    )
    counts = query_one(
        """SELECT count(*) AS total,count(*) FILTER (WHERE active) AS active,
                  count(*) FILTER (WHERE authoritative) AS authoritative,
                  count(*) FILTER (WHERE parcel_id IS NOT NULL OR parcel_objectid IS NOT NULL) AS parcel_linked,
                  count(*) FILTER (WHERE transit_asset_id IS NOT NULL) AS transit_linked,
                  max(refreshed_at) AS last_refreshed_at
           FROM spatial_reference_entities"""
    )
    sources = query_all(
        """SELECT source_provider,count(*) AS total,count(*) FILTER (WHERE active) AS active,
                  max(refreshed_at) AS last_refreshed_at
           FROM spatial_reference_entities GROUP BY source_provider ORDER BY source_provider"""
    )
    return templates.TemplateResponse(
        request=request,
        name="spatial_reference.html",
        context={"rows": rows, "counts": counts, "sources": sources, "q": q, "entity_type": entity_type,
                 "entity_types": ENTITY_TYPES, "source_kinds": SOURCE_KINDS, "msg": msg},
    )


@app.get("/spatial-reference/{entity_id}", response_class=HTMLResponse)
def spatial_reference_detail(
    request: Request, entity_id: uuid.UUID, radius_ft: float = 500.0,
    history_days: int = 30, msg: str = "",
):
    row = _entity(entity_id)
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    context = None
    if row.get("parcel_objectid"):
        context = query_one("SELECT gis_parcel_context(%s,%s) AS context", (row["parcel_objectid"], radius_ft)).get("context")
    history_days = max(1, min(int(history_days), 365))
    impact = context.get("spatial_impact") if context else query_one(
        """SELECT gis_spatial_impact_context(geom,%s,make_interval(days=>%s)) AS context
           FROM spatial_reference_entities WHERE entity_id=%s""",
        (radius_ft, history_days, entity_id),
    ).get("context")
    subscribers = query_all("SELECT id,name,subscriber_id FROM subscribers WHERE active=true ORDER BY name")
    selected = query_all(
        """SELECT subscriber_id FROM watch_item_recipients
           WHERE watch_item_id=%s AND active=true""",
        (row.get("watch_item_id"),),
    ) if row.get("watch_item_id") else []
    return templates.TemplateResponse(
        request=request,
        name="spatial_reference_detail.html",
        context={"entity": row, "context": context, "impact": impact, "radius_ft": radius_ft,
                 "history_days": history_days, "subscribers": subscribers,
                 "selected_subscribers": {str(item["subscriber_id"]) for item in selected},
                 "watch_starts_local": _datetime_local(row.get("watch_starts_at")),
                 "watch_expires_local": _datetime_local(row.get("watch_expires_at")),
                 "watch_duration": "CUSTOM" if row.get("watch_expires_at") else "ALWAYS",
                 "spatial_scopes": SPATIAL_SCOPES, "msg": msg},
    )


@app.get("/api/spatial-reference/search")
def spatial_reference_search(q: str = "", entity_type: str = ""):
    q = q.strip()
    entity_type = entity_type.strip().upper()
    needle = f"%{q}%"
    rows = query_all(
        """
        SELECT entity_id,entity_type,entity_subtype,canonical_name,aliases,normalized_address,
               municipality,county,state,postal_code,source_provider,source_record_id,source_reference,
               confidence,authoritative,importance_tier,parcel_id,parcel_objectid,transit_asset_id,
               default_buffer_ft,refreshed_at,active,
               ST_AsGeoJSON(geom)::json AS geometry
        FROM spatial_reference_entities
        WHERE (%s='' OR entity_type=%s)
          AND (%s='' OR canonical_name ILIKE %s OR coalesce(normalized_address,'') ILIKE %s
               OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE a ILIKE %s))
        ORDER BY active DESC,importance_tier,canonical_name LIMIT 100
        """,
        (entity_type, entity_type, q, needle, needle, needle),
    )
    return JSONResponse(_json_safe({"items": rows, "count": len(rows)}))


@app.get("/api/spatial-reference/source-search")
def spatial_reference_source_search(q: str):
    q = q.strip()
    if len(q) < 2:
        return {"items": []}
    needle = f"%{q}%"
    rows = query_all(
        """
        (SELECT 'ADDRESS'::text AS source_kind,a.objectid::text AS source_id,a.fulladdr::text AS label,
                concat_ws(' · ',a.post_comm,a.post_code) AS detail,ST_AsGeoJSON(a.geom)::json AS geometry
         FROM gis_addresses a WHERE a.geom IS NOT NULL AND coalesce(a.status,'A')='A' AND a.fulladdr ILIKE %s
         ORDER BY a.fulladdr LIMIT 10)
        UNION ALL
        (SELECT 'PARCEL',p.objectid::text,
                coalesce(nullif(p.prop_loc,''),'Block '||coalesce(p.pclblock,'?')||' Lot '||coalesce(p.pcllot,'?')),
                concat_ws(' · ',p.mun_name,'Block '||coalesce(p.pclblock,'?'),'Lot '||coalesce(p.pcllot,'?'),p.pams_pin),
                ST_AsGeoJSON(p.geom)::json
         FROM gis_parcels p WHERE p.geom IS NOT NULL
           AND (p.prop_loc ILIKE %s OR p.pams_pin ILIKE %s OR p.pclblock ILIKE %s OR p.pcllot ILIKE %s)
         ORDER BY p.prop_loc NULLS LAST LIMIT 10)
        UNION ALL
        (SELECT 'CUSTOM_FEATURE',f.id::text,coalesce(nullif(f.name,''),l.name),l.name,ST_AsGeoJSON(f.geom)::json
         FROM map_features f JOIN map_layers l ON l.id=f.layer_id
         WHERE f.active=true AND l.active=true AND (f.name ILIKE %s OR f.properties::text ILIKE %s)
         ORDER BY l.name,f.name NULLS LAST LIMIT 10)
        UNION ALL
        (SELECT 'LANDMARK',la.site_nguid || ':' || md5(lower(btrim(la.aclandmark))),
                btrim(la.aclandmark),concat_ws(' · ',a.fulladdr,a.post_comm,a.post_code),
                ST_AsGeoJSON(a.geom)::json
         FROM gis_landmark_aliases la JOIN gis_addresses a ON a.site_nguid=la.site_nguid
         WHERE a.geom IS NOT NULL AND coalesce(a.status,'A')='A'
           AND nullif(btrim(la.aclandmark),'') IS NOT NULL AND la.aclandmark ILIKE %s
         ORDER BY la.aclandmark,a.objectid LIMIT 10)
        UNION ALL
        (SELECT 'TRANSIT_ASSET',ta.id::text,ta.name,
                concat_ws(' · ',tp.name,ta.asset_type,ta.municipality,ta.state),ST_AsGeoJSON(ta.geom)::json
         FROM transit_assets ta JOIN transit_providers tp ON tp.id=ta.provider_id
         WHERE ta.active=true AND ta.geom IS NOT NULL
           AND (ta.name ILIKE %s OR coalesce(ta.short_name,'') ILIKE %s OR tp.name ILIKE %s)
         ORDER BY tp.name,ta.name LIMIT 10)
        """,
        (needle, needle, needle, needle, needle, needle, needle, needle, needle, needle, needle),
    )
    return JSONResponse(_json_safe({"items": rows}))


@app.post("/spatial-reference/adopt")
def spatial_reference_adopt(
    source_kind: str = Form(...), source_id: str = Form(...), canonical_name: str = Form(""),
    entity_type: str = Form("FACILITY"), entity_subtype: str = Form(""), aliases: str = Form(""),
    importance_tier: int = Form(3),
):
    source_kind = source_kind.strip().upper()
    entity_type = entity_type.strip().upper()
    canonical_name = canonical_name.strip()
    if source_kind not in SOURCE_KINDS or entity_type not in ENTITY_TYPES:
        raise HTTPException(400, "Invalid source or entity type")
    if importance_tier < 1 or importance_tier > 5:
        raise HTTPException(400, "Importance tier must be between 1 and 5")
    alias_values = _aliases(aliases)
    with db_conn() as conn, conn.cursor() as cur:
        if source_kind == "ADDRESS":
            try:
                objectid = int(source_id)
            except ValueError as exc:
                raise HTTPException(400, "Invalid address source ID") from exc
            cur.execute(
                """
                WITH source AS (
                  SELECT a.*,p.objectid AS parcel_objectid,p.pams_pin,p.pcl_guid AS parcel_guid,
                         p.county AS parcel_county
                  FROM gis_addresses a
                  LEFT JOIN LATERAL (
                    SELECT p.objectid,p.pams_pin,p.pcl_guid,p.county FROM gis_parcels p
                    WHERE p.geom IS NOT NULL AND (p.pcl_guid=a.pcl_guid OR ST_Covers(p.geom,a.geom))
                    ORDER BY CASE WHEN p.pcl_guid=a.pcl_guid THEN 0 ELSE 1 END LIMIT 1
                  ) p ON true
                  WHERE a.objectid=%s AND a.geom IS NOT NULL LIMIT 1
                )
                INSERT INTO spatial_reference_entities(
                  entity_type,entity_subtype,canonical_name,aliases,normalized_address,municipality,county,state,
                  postal_code,geom,centroid,source_provider,source_record_id,source_reference,provenance,
                  confidence,authoritative,importance_tier,default_buffer_ft,parcel_id,parcel_objectid,
                  verified_at,source_updated_at,refreshed_at,metadata
                )
                SELECT %s,nullif(%s,''),coalesce(nullif(%s,''),fulladdr),%s,fulladdr,post_comm,
                       coalesce(parcel_county,county),state,post_code,
                       geom,geom,'NJOGIS_NG911_ADDRESS',objectid::text,
                       'https://njogis-newjersey.opendata.arcgis.com/',
                       jsonb_build_object('dataset','gis_addresses','site_nguid',site_nguid),
                       1.0,true,%s,500.0,coalesce(parcel_guid,pcl_guid,pams_pin),parcel_objectid,now(),dateupdate,now(),
                       jsonb_build_object('source_kind','ADDRESS')
                FROM source
                ON CONFLICT (source_provider,source_record_id) WHERE source_record_id IS NOT NULL
                DO UPDATE SET entity_type=EXCLUDED.entity_type,entity_subtype=EXCLUDED.entity_subtype,
                  canonical_name=EXCLUDED.canonical_name,aliases=EXCLUDED.aliases,
                  normalized_address=EXCLUDED.normalized_address,municipality=EXCLUDED.municipality,
                  county=EXCLUDED.county,state=EXCLUDED.state,postal_code=EXCLUDED.postal_code,
                  geom=EXCLUDED.geom,provenance=EXCLUDED.provenance,confidence=EXCLUDED.confidence,
                  authoritative=EXCLUDED.authoritative,importance_tier=EXCLUDED.importance_tier,
                  parcel_id=EXCLUDED.parcel_id,parcel_objectid=EXCLUDED.parcel_objectid,
                  source_reference=EXCLUDED.source_reference,default_buffer_ft=EXCLUDED.default_buffer_ft,
                  verified_at=now(),source_updated_at=EXCLUDED.source_updated_at,refreshed_at=now(),
                  active=true,retired_at=NULL
                RETURNING entity_id
                """,
                (objectid, entity_type, entity_subtype, canonical_name, alias_values, importance_tier),
            )
        elif source_kind == "PARCEL":
            try:
                objectid = int(source_id)
            except ValueError as exc:
                raise HTTPException(400, "Invalid parcel source ID") from exc
            cur.execute(
                """
                INSERT INTO spatial_reference_entities(
                  entity_type,entity_subtype,canonical_name,aliases,normalized_address,municipality,county,state,
                  postal_code,geom,centroid,source_provider,source_record_id,source_reference,provenance,
                  confidence,authoritative,importance_tier,default_buffer_ft,parcel_id,parcel_objectid,
                  verified_at,source_updated_at,refreshed_at,metadata
                )
                SELECT %s,nullif(%s,''),coalesce(nullif(%s,''),nullif(prop_loc,''),'Block '||coalesce(pclblock,'?')||' Lot '||coalesce(pcllot,'?')),
                       %s,prop_loc,mun_name,county,'NJ',coalesce(zip5,zip_code),geom,ST_PointOnSurface(geom),
                       'NJOGIS_PARCEL',objectid::text,
                       'https://njogis-newjersey.opendata.arcgis.com/',
                       jsonb_build_object('dataset','gis_parcels','pams_pin',pams_pin,'pcl_guid',pcl_guid),
                       1.0,true,%s,500.0,coalesce(pcl_guid,pams_pin),objectid,now(),pcllastupd,now(),
                       jsonb_build_object('source_kind','PARCEL','block',pclblock,'lot',pcllot)
                FROM gis_parcels WHERE objectid=%s AND geom IS NOT NULL
                ON CONFLICT (source_provider,source_record_id) WHERE source_record_id IS NOT NULL
                DO UPDATE SET entity_type=EXCLUDED.entity_type,entity_subtype=EXCLUDED.entity_subtype,
                  canonical_name=EXCLUDED.canonical_name,aliases=EXCLUDED.aliases,
                  normalized_address=EXCLUDED.normalized_address,municipality=EXCLUDED.municipality,
                  county=EXCLUDED.county,state=EXCLUDED.state,postal_code=EXCLUDED.postal_code,
                  geom=EXCLUDED.geom,provenance=EXCLUDED.provenance,confidence=EXCLUDED.confidence,
                  authoritative=EXCLUDED.authoritative,importance_tier=EXCLUDED.importance_tier,
                  parcel_id=EXCLUDED.parcel_id,parcel_objectid=EXCLUDED.parcel_objectid,
                  source_reference=EXCLUDED.source_reference,default_buffer_ft=EXCLUDED.default_buffer_ft,
                  verified_at=now(),source_updated_at=EXCLUDED.source_updated_at,refreshed_at=now(),
                  active=true,retired_at=NULL
                RETURNING entity_id
                """,
                (entity_type, entity_subtype, canonical_name, alias_values, importance_tier, objectid),
            )
        elif source_kind == "LANDMARK":
            cur.execute(
                """
                WITH source AS (
                  SELECT DISTINCT ON (a.objectid) la.site_nguid,btrim(la.aclandmark) AS landmark_name,
                         a.objectid AS address_objectid,a.fulladdr,a.post_comm,a.post_code,a.state,a.dateupdate,a.geom,
                         p.objectid AS parcel_objectid,p.pcl_guid,p.pams_pin,p.mun_name,p.county
                  FROM gis_landmark_aliases la JOIN gis_addresses a ON a.site_nguid=la.site_nguid
                  LEFT JOIN LATERAL (
                    SELECT p.objectid,p.pcl_guid,p.pams_pin,p.mun_name,p.county
                    FROM gis_parcels p
                    WHERE p.geom IS NOT NULL
                      AND ((a.pcl_guid IS NOT NULL AND p.pcl_guid=a.pcl_guid) OR ST_Covers(p.geom,a.geom))
                    ORDER BY CASE WHEN a.pcl_guid IS NOT NULL AND p.pcl_guid=a.pcl_guid THEN 0 ELSE 1 END,p.objectid
                    LIMIT 1
                  ) p ON true
                  WHERE la.site_nguid || ':' || md5(lower(btrim(la.aclandmark)))=%s
                    AND a.geom IS NOT NULL AND coalesce(a.status,'A')='A'
                  ORDER BY a.objectid LIMIT 1
                )
                INSERT INTO spatial_reference_entities(
                  entity_type,entity_subtype,canonical_name,aliases,normalized_address,municipality,county,state,
                  postal_code,geom,centroid,source_provider,source_record_id,source_reference,provenance,
                  confidence,authoritative,importance_tier,default_buffer_ft,parcel_id,parcel_objectid,
                  verified_at,source_updated_at,refreshed_at,metadata
                )
                SELECT %s,nullif(%s,''),coalesce(nullif(%s,''),landmark_name),%s,fulladdr,
                       coalesce(mun_name,post_comm),county,coalesce(state,'NJ'),post_code,geom,geom,
                       'NJOGIS_LANDMARK_ALIAS',site_nguid || ':' || md5(lower(landmark_name)),
                       'https://njogis-newjersey.opendata.arcgis.com/',
                       jsonb_build_object('dataset','gis_landmark_aliases','site_nguid',site_nguid,
                                          'address_objectid',address_objectid),
                       1.0,true,%s,500.0,coalesce(pcl_guid,pams_pin),parcel_objectid,now(),dateupdate,now(),
                       jsonb_build_object('source_kind','LANDMARK_ALIAS')
                FROM source
                ON CONFLICT (source_provider,source_record_id) WHERE source_record_id IS NOT NULL
                DO UPDATE SET entity_type=EXCLUDED.entity_type,entity_subtype=EXCLUDED.entity_subtype,
                  canonical_name=EXCLUDED.canonical_name,aliases=EXCLUDED.aliases,
                  normalized_address=EXCLUDED.normalized_address,municipality=EXCLUDED.municipality,
                  county=EXCLUDED.county,state=EXCLUDED.state,postal_code=EXCLUDED.postal_code,
                  geom=EXCLUDED.geom,provenance=EXCLUDED.provenance,importance_tier=EXCLUDED.importance_tier,
                  parcel_id=EXCLUDED.parcel_id,parcel_objectid=EXCLUDED.parcel_objectid,
                  source_reference=EXCLUDED.source_reference,default_buffer_ft=EXCLUDED.default_buffer_ft,
                  verified_at=now(),source_updated_at=EXCLUDED.source_updated_at,refreshed_at=now(),
                  active=true,retired_at=NULL
                RETURNING entity_id
                """,
                (source_id, entity_type, entity_subtype, canonical_name, alias_values, importance_tier),
            )
        elif source_kind == "TRANSIT_ASSET":
            try:
                asset_id = uuid.UUID(source_id)
            except ValueError as exc:
                raise HTTPException(400, "Invalid transit asset source ID") from exc
            cur.execute(
                """
                INSERT INTO spatial_reference_entities(
                  entity_type,entity_subtype,canonical_name,aliases,municipality,county,state,geom,centroid,
                  source_provider,source_record_id,source_reference,provenance,confidence,authoritative,
                  importance_tier,default_buffer_ft,transit_asset_id,verified_at,source_updated_at,refreshed_at,metadata
                )
                SELECT %s,nullif(%s,''),coalesce(nullif(%s,''),ta.name),
                       CASE WHEN cardinality(%s::text[])>0 THEN %s::text[]
                            WHEN nullif(btrim(ta.short_name),'') IS NOT NULL THEN ARRAY[ta.short_name]
                            ELSE '{}'::text[] END,
                       ta.municipality,ta.county,ta.state,ta.geom,ta.geom,'CMOS_TRANSIT_ASSET',ta.id::text,
                       'City Manager OS transit_assets',
                       jsonb_build_object('dataset','transit_assets','provider',tp.provider_key,'asset_key',ta.asset_key),
                       1.0,true,%s,500.0,ta.id,now(),ta.updated_at,now(),
                       ta.metadata || jsonb_build_object('source_kind','TRANSIT_ASSET','provider_name',tp.name)
                FROM transit_assets ta JOIN transit_providers tp ON tp.id=ta.provider_id
                WHERE ta.id=%s AND ta.active=true AND ta.geom IS NOT NULL
                ON CONFLICT (source_provider,source_record_id) WHERE source_record_id IS NOT NULL
                DO UPDATE SET entity_type=EXCLUDED.entity_type,entity_subtype=EXCLUDED.entity_subtype,
                  canonical_name=EXCLUDED.canonical_name,aliases=EXCLUDED.aliases,
                  municipality=EXCLUDED.municipality,county=EXCLUDED.county,state=EXCLUDED.state,
                  geom=EXCLUDED.geom,provenance=EXCLUDED.provenance,importance_tier=EXCLUDED.importance_tier,
                  source_reference=EXCLUDED.source_reference,default_buffer_ft=EXCLUDED.default_buffer_ft,
                  transit_asset_id=EXCLUDED.transit_asset_id,verified_at=now(),
                  source_updated_at=EXCLUDED.source_updated_at,refreshed_at=now(),
                  metadata=EXCLUDED.metadata,active=true,retired_at=NULL
                RETURNING entity_id
                """,
                (entity_type, entity_subtype, canonical_name, alias_values, alias_values,
                 importance_tier, asset_id),
            )
        elif source_kind == "CUSTOM_FEATURE":
            try:
                feature_id = uuid.UUID(source_id)
            except ValueError as exc:
                raise HTTPException(400, "Invalid custom feature source ID") from exc
            cur.execute(
                """
                INSERT INTO spatial_reference_entities(
                  entity_type,entity_subtype,canonical_name,aliases,geom,centroid,source_provider,source_record_id,
                  source_reference,provenance,confidence,authoritative,importance_tier,default_buffer_ft,
                  verified_at,refreshed_at,metadata
                )
                SELECT %s,nullif(%s,''),coalesce(nullif(%s,''),nullif(f.name,''),l.name),%s,f.geom,ST_PointOnSurface(f.geom),
                       'MAP_FEATURE',f.id::text,'Mapping Center custom layer',
                       jsonb_build_object('layer_id',l.id,'layer_key',l.layer_key,'layer_name',l.name),
                       1.0,false,%s,500.0,now(),now(),f.properties||jsonb_build_object('source_kind','CUSTOM_FEATURE')
                FROM map_features f JOIN map_layers l ON l.id=f.layer_id
                WHERE f.id=%s AND f.active=true AND f.geom IS NOT NULL
                ON CONFLICT (source_provider,source_record_id) WHERE source_record_id IS NOT NULL
                DO UPDATE SET entity_type=EXCLUDED.entity_type,entity_subtype=EXCLUDED.entity_subtype,
                  canonical_name=EXCLUDED.canonical_name,aliases=EXCLUDED.aliases,geom=EXCLUDED.geom,
                  provenance=EXCLUDED.provenance,importance_tier=EXCLUDED.importance_tier,
                  source_reference=EXCLUDED.source_reference,default_buffer_ft=EXCLUDED.default_buffer_ft,
                  verified_at=now(),refreshed_at=now(),metadata=EXCLUDED.metadata,active=true,retired_at=NULL
                RETURNING entity_id
                """,
                (entity_type, entity_subtype, canonical_name, alias_values, importance_tier, feature_id),
            )
        created = cur.fetchone()
        if not created:
            raise HTTPException(404, "Source feature was not found")
        conn.commit()
    return RedirectResponse(f"/spatial-reference/{created['entity_id']}?msg=Reference+saved", status_code=303)


@app.post("/spatial-reference/refresh")
def spatial_reference_refresh():
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT spatial_reference_refresh_local_sources() AS result")
        result = cur.fetchone()["result"]
        conn.commit()
    message = (
        f"Catalog refreshed: {result.get('active_total', 0)} active, "
        f"{result.get('parcel_facilities_refreshed', 0)} parcel facilities, "
        f"{result.get('landmarks_refreshed', 0)} landmarks, "
        f"{result.get('transit_assets_refreshed', 0)} transit assets"
    )
    return RedirectResponse("/spatial-reference?" + urlencode({"msg": message}), status_code=303)


@app.post("/spatial-reference/{entity_id}/watch")
def spatial_reference_watch(
    entity_id: uuid.UUID, radius_ft: float = Form(500.0), min_priority: int = Form(1),
    spatial_scope: str = Form("RADIUS"), duration: str = Form("ALWAYS"),
    starts_at: str = Form(""), expires_at: str = Form(""), source_filter: str = Form(""),
    alert_category_filter: str = Form(""), subscriber_ids: list[uuid.UUID] = Form([]),
):
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    if min_priority < 1 or min_priority > 5:
        raise HTTPException(400, "Priority must be between 1 and 5")
    spatial_scope = spatial_scope.strip().upper()
    if spatial_scope not in SPATIAL_SCOPES:
        raise HTTPException(400, "Invalid spatial scope")
    duration = duration.strip().upper()
    if duration not in {"ALWAYS", "24_HOURS", "7_DAYS", "CUSTOM"}:
        raise HTTPException(400, "Invalid watch duration")
    start_value = _local_datetime(starts_at)
    expiry_value = _local_datetime(expires_at)
    if duration in {"24_HOURS", "7_DAYS"}:
        start_value = start_value or datetime.now(timezone.utc)
        expiry_value = start_value + timedelta(hours=24 if duration == "24_HOURS" else 168)
    elif duration == "ALWAYS":
        expiry_value = None
    elif expiry_value is None:
        raise HTTPException(400, "Custom duration requires an end date and time")
    if expiry_value and expiry_value <= (start_value or datetime.now(timezone.utc)):
        raise HTTPException(400, "Watch end must be after its start")
    source_values = _aliases(source_filter)
    category_values = _aliases(alert_category_filter)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,ST_X(r.centroid) AS longitude,ST_Y(r.centroid) AS latitude
               FROM spatial_reference_entities r
               WHERE r.entity_id=%s AND r.active=true FOR SHARE""",
            (entity_id,),
        )
        entity = cur.fetchone()
        if not entity:
            raise HTTPException(404, "Active spatial reference not found")
        if spatial_scope == "ADJOINING" and not entity.get("parcel_objectid"):
            raise HTTPException(400, "Adjoining-parcel scope requires a parcel-linked reference")
        for subscriber_id in subscriber_ids:
            cur.execute("SELECT id FROM subscribers WHERE id=%s AND active=true", (subscriber_id,))
            if not cur.fetchone():
                raise HTTPException(400, "One or more selected subscribers are inactive or missing")
        cur.execute("SELECT id FROM watch_items WHERE spatial_reference_entity_id=%s FOR UPDATE", (entity_id,))
        existing = cur.fetchone()
        if existing:
            watch_item_id = existing["id"]
            cur.execute(
                """
                WITH ref AS (
                  SELECT r.* FROM spatial_reference_entities r WHERE r.entity_id=%s
                ), prepared AS (
                  SELECT r.*,
                    CASE %s
                      WHEN 'RADIUS' THEN ST_Buffer(r.geom::geography,%s*0.3048)::geometry
                      WHEN 'ADJOINING' THEN coalesce((
                        SELECT ST_UnaryUnion(ST_Collect(g.geom)) FROM (
                          SELECT p.geom FROM gis_parcels p WHERE p.objectid=r.parcel_objectid
                          UNION ALL
                          SELECT p.geom FROM gis_adjoining_parcels(r.parcel_objectid,3.0,100) p
                        ) g
                      ),r.geom)
                      ELSE r.geom
                    END AS watch_geometry
                  FROM ref r
                )
                UPDATE watch_items w SET
                  active=true,watch_type=%s,display_name=p.canonical_name,search_term=p.canonical_name,
                  aliases=p.aliases,match_mode='CONTAINS',source_filter=%s,alert_category_filter=%s,
                  min_priority=%s,starts_at=%s,expires_at=%s,address=p.normalized_address,
                  municipality=p.municipality,county=p.county,state=p.state,parcel_id=p.parcel_id,
                  latitude=ST_Y(p.centroid),longitude=ST_X(p.centroid),gis_enabled=true,
                  gis_lookup=coalesce(p.normalized_address,p.canonical_name),nearby_enabled=true,
                  radius_ft=%s,geom=p.centroid,spatial_geom=p.watch_geometry,spatial_scope=%s,
                  source_notes=%s,notes=%s,updated_at=now()
                FROM prepared p WHERE w.id=%s
                """,
                (entity_id, spatial_scope, radius_ft,
                 "FACILITY" if entity["entity_type"] in {"FACILITY", "VENUE", "LANDMARK"} else "AREA",
                 source_values, category_values, min_priority, start_value, expiry_value, radius_ft,
                 spatial_scope, f"spatial_reference_entity:{entity_id}",
                 "Promoted from Regional Spatial Reference Catalog", watch_item_id),
            )
        else:
            watch_item_id = uuid.uuid4()
            cur.execute(
                """
                WITH ref AS (
                  SELECT r.* FROM spatial_reference_entities r WHERE r.entity_id=%s
                ), prepared AS (
                  SELECT r.*,
                    CASE %s
                      WHEN 'RADIUS' THEN ST_Buffer(r.geom::geography,%s*0.3048)::geometry
                      WHEN 'ADJOINING' THEN coalesce((
                        SELECT ST_UnaryUnion(ST_Collect(g.geom)) FROM (
                          SELECT p.geom FROM gis_parcels p WHERE p.objectid=r.parcel_objectid
                          UNION ALL
                          SELECT p.geom FROM gis_adjoining_parcels(r.parcel_objectid,3.0,100) p
                        ) g
                      ),r.geom)
                      ELSE r.geom
                    END AS watch_geometry
                  FROM ref r
                )
                INSERT INTO watch_items(
                  id,watch_id,active,watch_type,display_name,search_term,aliases,match_mode,
                  source_filter,alert_category_filter,min_priority,starts_at,expires_at,address,
                  municipality,county,state,parcel_id,latitude,longitude,gis_enabled,gis_lookup,
                  nearby_enabled,radius_ft,geom,spatial_geom,spatial_scope,source_notes,notes,
                  spatial_reference_entity_id
                )
                SELECT %s,%s,true,%s,p.canonical_name,p.canonical_name,p.aliases,'CONTAINS',%s,%s,%s,%s,%s,
                       p.normalized_address,p.municipality,p.county,p.state,p.parcel_id,
                       ST_Y(p.centroid),ST_X(p.centroid),true,coalesce(p.normalized_address,p.canonical_name),
                       true,%s,p.centroid,p.watch_geometry,%s,%s,%s,p.entity_id
                FROM prepared p
                """,
                (entity_id, spatial_scope, radius_ft, watch_item_id, f"REF_{entity_id.hex[:24].upper()}",
                 "FACILITY" if entity["entity_type"] in {"FACILITY", "VENUE", "LANDMARK"} else "AREA",
                 source_values, category_values, min_priority, start_value, expiry_value, radius_ft,
                 spatial_scope, f"spatial_reference_entity:{entity_id}",
                 "Promoted from Regional Spatial Reference Catalog"),
            )
        cur.execute("UPDATE watch_item_recipients SET active=false WHERE watch_item_id=%s", (watch_item_id,))
        for subscriber_id in subscriber_ids:
            cur.execute(
                """INSERT INTO watch_item_recipients(watch_item_id,subscriber_id,active)
                   VALUES(%s,%s,true) ON CONFLICT(watch_item_id,subscriber_id) DO UPDATE SET active=true""",
                (watch_item_id, subscriber_id),
            )
        conn.commit()
    return RedirectResponse(
        f"/spatial-reference/{entity_id}?" + urlencode({"msg": "Watch geography, schedule, filters, and routing saved"}),
        status_code=303,
    )


@app.get("/api/spatial-reference/{entity_id}/nearby-history")
def spatial_reference_history(
    entity_id: uuid.UUID, radius_ft: float = 500.0, days: int = 30,
):
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    days = max(1, min(int(days), 365))
    row = query_one(
        """SELECT gis_spatial_impact_context(geom,%s,make_interval(days=>%s)) AS context
           FROM spatial_reference_entities WHERE entity_id=%s AND active=true""",
        (radius_ft, days, entity_id),
    )
    if not row:
        raise HTTPException(404, "Active spatial reference not found")
    return JSONResponse(_json_safe(row["context"]))


@app.get("/api/spatial-reference/{entity_id}/impact-buffer.geojson")
def spatial_reference_impact_buffer(entity_id: uuid.UUID, radius_ft: float = 500.0):
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    row = query_one(
        """SELECT canonical_name,entity_type,
                  ST_AsGeoJSON(ST_Buffer(geom::geography,%s*0.3048)::geometry)::json AS geometry
           FROM spatial_reference_entities WHERE entity_id=%s AND active=true""",
        (radius_ft, entity_id),
    )
    if not row:
        raise HTTPException(404, "Active spatial reference not found")
    return JSONResponse({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "geometry": row["geometry"],
            "properties": {"entity_id": str(entity_id), "canonical_name": row["canonical_name"],
                           "entity_type": row["entity_type"], "radius_ft": radius_ft},
        }],
    }, media_type="application/geo+json")


@app.get("/api/parcel/for-point")
def parcel_for_point(lat: float, lon: float, tolerance_ft: float = 3.0):
    tolerance_ft = max(0.0, min(float(tolerance_ft), 25.0))
    row = query_one(
        """SELECT parcel_objectid,parcel_id,pams_pin,municipality,block,lot,qualifier,
                  property_location,relation_type,distance_ft,ST_AsGeoJSON(geom)::json AS geometry
           FROM gis_parcel_for_point(%s,%s,%s)""",
        (lat, lon, tolerance_ft),
    )
    if not row:
        raise HTTPException(404, "No parcel found at or within the requested tolerance")
    return JSONResponse(_json_safe(row))


@app.get("/api/parcels/within-radius")
def parcels_for_point(lat: float, lon: float, radius_ft: float = 500.0):
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    rows = query_all(
        """SELECT parcel_objectid,parcel_id,pams_pin,municipality,block,lot,property_location,
                  relation_type,distance_ft,shared_boundary_ft,separating_reference_id,
                  separating_reference_name,ST_AsGeoJSON(geom)::json AS geometry
           FROM gis_parcels_within_radius(%s,%s,%s,500)""",
        (lat, lon, radius_ft),
    )
    return JSONResponse(_json_safe({"items": rows, "count": len(rows), "radius_ft": radius_ft}))


@app.get("/api/parcel/{parcel_objectid}/context")
def parcel_context(parcel_objectid: int, radius_ft: float = 500.0):
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    value = query_one("SELECT gis_parcel_context(%s,%s) AS context", (parcel_objectid, radius_ft)).get("context")
    if value is None:
        raise HTTPException(404, "Parcel not found")
    return JSONResponse(_json_safe(value))


@app.get("/api/parcel/{parcel_objectid}/addresses")
def parcel_addresses(parcel_objectid: int):
    rows = query_all(
        """SELECT address_objectid,full_address,municipality,postal_code,parcel_id,link_method,
                  relation_type,distance_ft,ST_AsGeoJSON(geom)::json AS geometry
           FROM gis_addresses_for_parcel(%s,3.0,500)""",
        (parcel_objectid,),
    )
    return JSONResponse(_json_safe({"items": rows, "count": len(rows)}))


@app.get("/api/parcel/{parcel_objectid}/adjoining")
def parcel_adjoining(parcel_objectid: int):
    rows = query_all(
        """SELECT parcel_objectid,parcel_id,pams_pin,municipality,block,lot,property_location,
                  relation_type,distance_ft,shared_boundary_ft,ST_AsGeoJSON(geom)::json AS geometry
           FROM gis_adjoining_parcels(%s,3.0,250)""",
        (parcel_objectid,),
    )
    return JSONResponse(_json_safe({"items": rows, "count": len(rows)}))


@app.get("/api/parcel/{parcel_objectid}/nearby")
def parcel_nearby(parcel_objectid: int, radius_ft: float = 500.0):
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    rows = query_all(
        """SELECT parcel_objectid,parcel_id,pams_pin,municipality,block,lot,property_location,
                  relation_type,distance_ft,shared_boundary_ft,separating_reference_id,separating_reference_name,
                  ST_AsGeoJSON(geom)::json AS geometry
           FROM gis_parcels_within_radius(%s,%s,500,3.0)""",
        (parcel_objectid, radius_ft),
    )
    return JSONResponse(_json_safe({"items": rows, "count": len(rows), "radius_ft": radius_ft}))


@app.get("/map/system/spatial-references.geojson")
def spatial_reference_geojson(bbox: str | None = None):
    params = []
    where = ["active=true", "geom IS NOT NULL"]
    if bbox:
        try:
            values = [float(value) for value in bbox.split(",")]
        except ValueError as exc:
            raise HTTPException(400, "Invalid bbox") from exc
        if len(values) != 4 or values[0] >= values[2] or values[1] >= values[3]:
            raise HTTPException(400, "Invalid bbox")
        where.append("ST_Intersects(geom,ST_MakeEnvelope(%s,%s,%s,%s,4326))")
        params.extend(values)
    rows = query_all(
        f"""SELECT entity_id,entity_type,entity_subtype,canonical_name,normalized_address,
                   municipality,state,importance_tier,authoritative,parcel_objectid,
                   ST_AsGeoJSON(geom)::json AS geometry
            FROM spatial_reference_entities WHERE {' AND '.join(where)}
            ORDER BY importance_tier,canonical_name LIMIT 10000""",
        tuple(params),
    )
    features = [
        {"type": "Feature", "geometry": row.pop("geometry"), "properties": _json_safe(row)}
        for row in rows if row.get("geometry")
    ]
    return JSONResponse({"type": "FeatureCollection", "features": features})
