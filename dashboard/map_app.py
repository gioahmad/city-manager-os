import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import unquote, urlparse

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from schedule_app import app
from app import db_conn, execute, query_all, query_one, templates
from gis_import import MAX_UPLOAD_BYTES, read_import
from geo_resolver import RESOLVER_VERSION, resolve_payload
from operations_app import ALERT_WINDOWS


SYSTEM_LAYERS = [
    {"key": "flood", "name": "FEMA Flood Zones", "endpoint": "/map/system/flood.geojson", "default_visible": False, "style": {"color": "#e26d6d"}},
    {"key": "parcels", "name": "Parcels", "endpoint": "/map/system/parcels.geojson", "default_visible": False, "style": {"color": "#7fb3d5"}, "viewport": True},
    {"key": "addresses", "name": "NG911 Addresses", "endpoint": "/map/system/addresses.geojson", "default_visible": False, "point": True, "viewport": True},
    {"key": "watchlist", "name": "Watch Locations", "endpoint": "/map/system/watchlist.geojson", "default_visible": True, "point": True, "viewport": True},
    {"key": "spatial-references", "name": "Regional References", "endpoint": "/map/system/spatial-references.geojson", "default_visible": True, "style": {"color": "#9b7ede"}, "viewport": True},
    {"key": "alerts", "name": "Alerts", "endpoint": "/map/system/alerts.geojson?hours=12", "default_visible": True, "point": True, "viewport": True},
    {"key": "operations", "name": "Operations / Work Items", "endpoint": "/map/system/issues.geojson", "default_visible": True, "point": True, "viewport": True},
    {"key": "event-intelligence", "name": "Event Intelligence", "endpoint": "/map/system/events.geojson", "default_visible": False, "point": True, "viewport": True},
    {"key": "managed-events", "name": "Managed Events", "endpoint": "/map/system/managed-events.geojson", "default_visible": False, "point": True},
    {"key": "transit-intelligence", "name": "Transit Intelligence", "endpoint": "/map/system/transit.geojson", "default_visible": False, "point": True},
]

def _layer_key(name: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:40] or "LAYER"
    return f"CUSTOM_{slug}_{uuid.uuid4().hex[:6].upper()}"


def _bbox(value: str | None):
    if not value:
        return None
    try:
        vals = [float(x) for x in value.split(",")]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid bbox") from exc
    if len(vals) != 4 or vals[0] >= vals[2] or vals[1] >= vals[3]:
        raise HTTPException(status_code=400, detail="Invalid bbox")
    return tuple(vals)


def _coordinate_pair(value: Any) -> tuple[float, float]:
    """Parse latitude/longitude text, including a copied Google Maps URL."""
    text = unquote(str(value or "").strip())
    match = re.search(
        r"@\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)",
        text,
    ) or re.search(
        r"(?<![\d.])(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)(?![\d.])",
        text,
    )
    if not match:
        raise HTTPException(
            status_code=400,
            detail="Paste coordinates as latitude, longitude or paste a Google Maps link.",
        )
    latitude, longitude = (float(match.group(1)), float(match.group(2)))
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise HTTPException(status_code=400, detail="Coordinates are outside valid latitude/longitude ranges.")
    return latitude, longitude


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _feature_collection(rows, geometry_field="geometry"):
    features = []
    for row in rows:
        geom = row.get(geometry_field)
        if not geom:
            continue
        props = {k: v for k, v in row.items() if k != geometry_field}
        for key, value in list(props.items()):
            if isinstance(value, uuid.UUID):
                props[key] = str(value)
            elif isinstance(value, (datetime, date)):
                props[key] = value.isoformat()
            elif isinstance(value, Decimal):
                props[key] = float(value)
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": features}


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date, timedelta, Decimal, uuid.UUID)):
        return str(value)
    return value


def _feature_name(properties: dict) -> str | None:
    if not isinstance(properties, dict):
        return None
    lowered = {str(key).lower(): value for key, value in properties.items()}
    for key in (
        "name", "title", "label", "municipality", "mun_name", "munname", "city", "town",
        "county", "county_name", "route_name", "route", "highway", "road_name", "road",
        "address", "fulladdr",
    ):
        value = lowered.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _store_imported_features(layer_id: uuid.UUID, features: list[dict], *, replace: bool = False, import_type: str = "", filename: str = "") -> int:
    inserted = 0
    with db_conn() as conn:
        with conn.cursor() as cur:
            if replace:
                cur.execute("UPDATE map_features SET active=false,updated_at=now() WHERE layer_id=%s AND active=true", (layer_id,))
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                geom = feature.get("geometry")
                if not isinstance(geom, dict):
                    continue
                props = feature.get("properties") or {}
                if not isinstance(props, dict):
                    props = {"value": str(props)}
                props = dict(props)
                props.setdefault("_import_type", import_type)
                props.setdefault("_import_file", filename)
                name = _feature_name(props)
                cur.execute(
                    """
                    WITH g AS (
                      SELECT ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) AS geom
                    ), cleaned AS (
                      SELECT CASE WHEN ST_IsValid(geom) THEN geom ELSE ST_MakeValid(geom) END AS geom FROM g
                    )
                    INSERT INTO map_features(layer_id,name,properties,geom)
                    SELECT %s,%s,%s::jsonb,geom
                    FROM cleaned
                    WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
                    """,
                    (json.dumps(geom), layer_id, name, json.dumps(props, default=str)),
                )
                inserted += cur.rowcount
        conn.commit()
    return inserted


@app.get("/map", response_class=HTMLResponse)
def mapping_center(request: Request, msg: str = ""):
    bounds = query_one(
        """
        WITH e AS (
          SELECT coalesce(
            ST_EstimatedExtent('public','gis_parcels','geom'),
            (SELECT ST_Extent(geom) FROM gis_parcels WHERE geom IS NOT NULL)
          ) AS b
        )
        SELECT ST_XMin(b) AS minx,ST_YMin(b) AS miny,ST_XMax(b) AS maxx,ST_YMax(b) AS maxy
        FROM e WHERE b IS NOT NULL
        """
    )
    custom_layers = query_all(
        """
        SELECT l.id,l.layer_key,l.name,l.layer_type,l.source_url,l.attribution,
               l.style,l.active,l.default_visible,l.sort_order,l.updated_at,
               count(f.id) FILTER (WHERE f.active=true) AS feature_count,
               max(f.updated_at) FILTER (WHERE f.active=true) AS last_feature_at
        FROM map_layers l
        LEFT JOIN map_features f ON f.layer_id=l.id
        GROUP BY l.id
        ORDER BY l.sort_order,l.name
        """
    )
    editable_layers = [x for x in custom_layers if x["layer_type"] == "CUSTOM_GEOJSON" and x["active"]]
    alert_sources = query_all(
        "SELECT DISTINCT source FROM alerts WHERE nullif(trim(source),'') IS NOT NULL ORDER BY source"
    )
    alert_categories = query_all(
        "SELECT DISTINCT category FROM alerts WHERE nullif(trim(category),'') IS NOT NULL ORDER BY category"
    )
    return templates.TemplateResponse(
        request=request,
        name="map.html",
        context={
            "page": "map",
            "msg": msg,
            "bounds": bounds,
            "system_layers": SYSTEM_LAYERS,
            "custom_layers": custom_layers,
            "editable_layers": editable_layers,
            "alert_sources": alert_sources,
            "alert_categories": alert_categories,
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        },
    )


@app.get("/map/search")
def map_search(q: str = ""):
    needle = q.strip()
    if len(needle) < 2:
        return JSONResponse({"type": "FeatureCollection", "features": []})
    like = f"%{needle}%"
    prefix_end = f"{needle}\U0010ffff"
    block_lot = re.fullmatch(
        r"block\s+([A-Za-z0-9.-]+)\s+lot\s+([A-Za-z0-9.-]+)",
        needle,
        flags=re.IGNORECASE,
    )
    simple_parcel_number = bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[A-Za-z]?", needle))
    parcel_identifier_sql = ""
    parcel_identifier_params = []
    if block_lot:
        parcel_identifier_sql = " OR (pclblock=%s AND pcllot=%s)"
        parcel_identifier_params = [block_lot.group(1), block_lot.group(2)]
    elif simple_parcel_number:
        parcel_identifier_sql = " OR pclblock=%s OR pcllot=%s"
        parcel_identifier_params = [needle, needle]
    rows = []
    rows.extend(query_all(
        """
        SELECT 'ADDRESS' AS result_type,fulladdr AS label,
               concat_ws(' · ',post_comm,post_code) AS detail,
               objectid::text AS source_id,ST_AsGeoJSON(geom)::json AS geometry
        FROM gis_addresses
        WHERE lower(fulladdr)>=lower(%s) AND lower(fulladdr)<lower(%s) AND geom IS NOT NULL
        ORDER BY CASE WHEN status='A' THEN 0 ELSE 1 END,fulladdr LIMIT 8
        """, (needle, prefix_end)
    ))
    rows.extend(query_all(
        f"""
        SELECT 'PARCEL' AS result_type,
               coalesce(nullif(prop_loc,''),'Block ' || coalesce(pclblock,'?') || ' Lot ' || coalesce(pcllot,'?')) AS label,
               concat_ws(' · ',mun_name,'Block ' || coalesce(pclblock,'?'),'Lot ' || coalesce(pcllot,'?'),nullif(pams_pin,'')) AS detail,
               objectid::text AS source_id,ST_AsGeoJSON(geom)::json AS geometry
        FROM gis_parcels
        WHERE ((lower(prop_loc)>=lower(%s) AND lower(prop_loc)<lower(%s))
               OR (pams_pin>=%s AND pams_pin<%s)
               {parcel_identifier_sql})
          AND geom IS NOT NULL
        ORDER BY prop_loc NULLS LAST,objectid LIMIT 8
        """, (needle, prefix_end, needle, prefix_end, *parcel_identifier_params)
    ))
    rows.extend(query_all(
        """
        SELECT 'REFERENCE' AS result_type,canonical_name AS label,
               concat_ws(' · ',entity_type,entity_subtype,municipality,state) AS detail,
               entity_id::text AS source_id,ST_AsGeoJSON(geom)::json AS geometry
        FROM spatial_reference_entities
        WHERE active=true AND geom IS NOT NULL
          AND (canonical_name ILIKE %s OR coalesce(normalized_address,'') ILIKE %s
               OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE a ILIKE %s))
        ORDER BY importance_tier,canonical_name LIMIT 8
        """, (like, like, like)
    ))
    rows.extend(query_all(
        """
        SELECT 'CUSTOM' AS result_type,coalesce(nullif(f.name,''),l.name) AS label,
               l.name AS detail,f.id::text AS source_id,ST_AsGeoJSON(f.geom)::json AS geometry
        FROM map_features f JOIN map_layers l ON l.id=f.layer_id
        WHERE f.active=true AND l.active=true
          AND (f.name ILIKE %s OR f.properties::text ILIKE %s)
          AND f.geom IS NOT NULL
        ORDER BY l.name,f.name NULLS LAST LIMIT 8
        """, (like, like)
    ))
    return JSONResponse(_feature_collection(rows[:20]))


@app.get("/map/gis/status")
def map_gis_status():
    latest = query_one(
        """
        SELECT run_id,scope,mode,status,phase,source_manifest,progress,
               repository_sha,started_at,updated_at,completed_at,error_message
        FROM gis_refresh_runs ORDER BY started_at DESC LIMIT 1
        """
    )
    datasets = query_all(
        """
        SELECT dataset_id,dataset_name,row_count,status,imported_at
        FROM gis_dataset_versions
        WHERE dataset_id LIKE 'NJOGIS_%%' AND status='ACTIVE'
        ORDER BY dataset_id
        """
    )
    copy = query_one(
        """
        SELECT p.relid::regclass::text AS table_name,p.tuples_processed,p.tuples_excluded,
               p.bytes_processed,now()-a.query_start AS elapsed,a.wait_event_type,a.wait_event
        FROM pg_stat_progress_copy p JOIN pg_stat_activity a ON a.pid=p.pid
        WHERE p.relid::regclass::text LIKE 'stg_nj_%%'
           OR a.query ILIKE '%%stg_nj_%%'
        ORDER BY a.query_start LIMIT 1
        """
    )
    coverage = query_all(
        """
        SELECT a.source,count(*) AS total,
               count(*) FILTER (WHERE a.geom IS NOT NULL) AS precise,
               count(*) FILTER (
                 WHERE a.geom IS NULL AND r.status='RESOLVED' AND r.geom IS NOT NULL
               ) AS approximate,
               count(*) FILTER (
                 WHERE coalesce(a.geom,CASE WHEN r.status='RESOLVED' THEN r.geom END) IS NOT NULL
               ) AS visible,
               count(*) FILTER (WHERE r.status='AMBIGUOUS') AS ambiguous,
               count(*) FILTER (WHERE r.status='UNRESOLVED') AS unresolved,
               count(*) FILTER (WHERE r.id IS NULL) AS pending,
               round(100.0*count(*) FILTER (
                 WHERE coalesce(a.geom,CASE WHEN r.status='RESOLVED' THEN r.geom END) IS NOT NULL
               )/nullif(count(*),0),2) AS visible_percent,
               round(avg(r.confidence),4) AS average_confidence,
               max(r.updated_at) AS last_resolution_at
        FROM alerts a
        LEFT JOIN geo_entity_resolutions r
          ON r.entity_type='ALERT' AND r.entity_id=a.id::text
        GROUP BY a.source
        ORDER BY total DESC,a.source
        """
    )
    return JSONResponse(_json_safe({"run": latest, "datasets": datasets, "copy": copy, "alert_coverage": coverage}))


@app.post("/map/resolve")
async def map_resolve(request: Request):
    """Resolve any fluid location payload strictly against local PostGIS."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    entity_type = str(payload.pop("_entity_type", "") or "").strip() or None
    entity_id = str(payload.pop("_entity_id", "") or "").strip() or None
    with db_conn() as conn:
        result = resolve_payload(
            conn,
            payload,
            entity_type=entity_type,
            entity_id=entity_id,
        )
    return JSONResponse(result)


@app.post("/map/alerts/{alert_id}/location")
async def map_alert_location_correct(alert_id: str, request: Request):
    """Save an operator-verified alert point and queue the existing spatial rematcher."""
    if getattr(request.state, "cmos_role", None) == "READ_ONLY":
        raise HTTPException(status_code=403, detail="Your role cannot change alert locations.")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    if "coordinates" in payload:
        latitude, longitude = _coordinate_pair(payload.get("coordinates"))
    elif "latitude" in payload and "longitude" in payload:
        latitude, longitude = _coordinate_pair(
            f"{payload.get('latitude')}, {payload.get('longitude')}"
        )
    else:
        raise HTTPException(status_code=400, detail="Coordinates are required.")

    requested_label = str(payload.get("label") or "").strip()[:300]
    reason = str(payload.get("reason") or "Corrected from Mapping Center").strip()[:500]
    actor = str(getattr(request.state, "cmos_user", None) or "local")[:120]
    role = str(getattr(request.state, "cmos_role", None) or "LOCAL")[:40]
    corrected_at = datetime.now(timezone.utc).isoformat()

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id::text,a.alert_id,a.title,a.location,a.metadata,a.municipality,a.county,
                       r.state,r.resolved_label,r.match_type AS prior_match_type,
                       r.provenance AS prior_resolution_provenance,
                       ST_Y(coalesce(a.geom,r.geom)) AS prior_latitude,
                       ST_X(coalesce(a.geom,r.geom)) AS prior_longitude,
                       (a.received_at>=now()-interval '6 hours'
                        AND a.status<>'RESOLVED'
                        AND (a.expires_at IS NULL OR a.expires_at>now())) AS rematch_eligible
                FROM alerts a
                LEFT JOIN geo_entity_resolutions r
                  ON r.entity_type='ALERT' AND r.entity_id=a.id::text
                WHERE a.alert_id=%s
                FOR UPDATE OF a
                """,
                (alert_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Alert not found.")

            cur.execute(
                """
                WITH point AS (
                  SELECT ST_SetSRID(ST_MakePoint(%s,%s),4326)::geometry(Point,4326) AS geom
                )
                SELECT ga.fulladdr,ga.post_comm,ga.post_code,ga.state,
                       ST_Distance(ga.geom::geography,point.geom::geography) AS distance_meters
                FROM gis_addresses ga CROSS JOIN point
                WHERE ga.geom IS NOT NULL
                  AND ST_DWithin(ga.geom::geography,point.geom::geography,500)
                ORDER BY ga.geom <-> point.geom,
                         CASE WHEN ga.status='A' THEN 0 ELSE 1 END,
                         ga.objectid
                LIMIT 1
                """,
                (longitude, latitude),
            )
            nearest = cur.fetchone() or {}

            location = _json_object(row.get("location"))
            metadata = _json_object(row.get("metadata"))
            label = requested_label or str(
                nearest.get("fulladdr")
                or location.get("label")
                or location.get("address")
                or row.get("resolved_label")
                or row.get("title")
                or alert_id
            ).strip()[:300]
            previous = {
                "latitude": float(row["prior_latitude"]) if row.get("prior_latitude") is not None else None,
                "longitude": float(row["prior_longitude"]) if row.get("prior_longitude") is not None else None,
                "label": location.get("label") or location.get("address") or row.get("resolved_label"),
                "match_type": row.get("prior_match_type"),
            }
            correction = {
                "corrected_at": corrected_at,
                "corrected_by": actor,
                "role": role,
                "reason": reason,
                "previous": previous,
                "latitude": latitude,
                "longitude": longitude,
                "label": label,
            }

            location.update(
                {
                    "label": label,
                    "latitude": latitude,
                    "longitude": longitude,
                }
            )
            if requested_label or nearest.get("fulladdr"):
                location["address"] = requested_label or nearest["fulladdr"]
            if nearest.get("post_comm"):
                location["municipality"] = nearest["post_comm"]
            if nearest.get("post_code"):
                location["zip"] = nearest["post_code"]
            if not location.get("state"):
                location["state"] = nearest.get("state") or row.get("state") or "NJ"
            cmos = _json_object(metadata.get("_cmos"))
            corrections = cmos.get("location_corrections")
            corrections = list(corrections) if isinstance(corrections, list) else []
            corrections.append(correction)
            cmos.update(
                {
                    "location_source": "MANUAL_COORDINATE_CORRECTION",
                    "location_corrections": corrections,
                    "spatial_rematch_version": "manual-location-pending-v1",
                    "spatial_rematch_requested_at": corrected_at,
                }
            )
            metadata["_cmos"] = cmos
            metadata["geo_resolution"] = {
                "status": "RESOLVED",
                "match_type": "MANUAL_COORDINATE_CORRECTION",
                "confidence": 1.0,
                "spatial_precision": "MANUAL_COORDINATE",
                "resolved_label": label,
                "resolver_version": RESOLVER_VERSION,
            }
            prior_provenance = _json_object(row.get("prior_resolution_provenance"))
            resolution_corrections = prior_provenance.get("location_corrections")
            resolution_corrections = (
                list(resolution_corrections) if isinstance(resolution_corrections, list) else []
            )
            resolution_corrections.append(correction)
            provenance = {
                "runtime_source": "MAPPING_CENTER",
                "resolver_version": RESOLVER_VERSION,
                "corrected_at": corrected_at,
                "corrected_by": actor,
                "role": role,
                "reason": reason,
                "previous": previous,
                "location_corrections": resolution_corrections,
                "nearest_local_address": nearest.get("fulladdr"),
                "nearest_local_address_distance_meters": (
                    float(nearest["distance_meters"])
                    if nearest.get("distance_meters") is not None
                    else None
                ),
            }

            cur.execute(
                """
                UPDATE alerts
                SET geom=ST_SetSRID(ST_MakePoint(%s,%s),4326),
                    location=%s::jsonb,
                    metadata=%s::jsonb,
                    municipality=coalesce(%s::text,municipality),
                    updated_at=now()
                WHERE id=%s::uuid
                """,
                (
                    longitude,
                    latitude,
                    json.dumps(location, default=str),
                    json.dumps(metadata, default=str),
                    nearest.get("post_comm"),
                    row["id"],
                ),
            )
            cur.execute(
                """
                INSERT INTO geo_entity_resolutions(
                  entity_type,entity_id,cache_key,status,match_type,confidence,
                  resolved_label,municipality,county,state,provenance,geom,
                  spatial_precision,resolver_version,resolved_at,last_attempt_at,
                  attempt_count,updated_at
                ) VALUES (
                  'ALERT',%s,NULL,'RESOLVED','MANUAL_COORDINATE_CORRECTION',1.0,
                  %s,%s,%s,%s,%s::jsonb,
                  ST_SetSRID(ST_MakePoint(%s,%s),4326),
                  'MANUAL_COORDINATE',%s,now(),now(),1,now()
                )
                ON CONFLICT (entity_type,entity_id) DO UPDATE
                SET cache_key=NULL,status='RESOLVED',match_type=EXCLUDED.match_type,
                    confidence=1.0,resolved_label=EXCLUDED.resolved_label,
                    municipality=coalesce(EXCLUDED.municipality,geo_entity_resolutions.municipality),
                    county=coalesce(EXCLUDED.county,geo_entity_resolutions.county),
                    state=coalesce(EXCLUDED.state,geo_entity_resolutions.state),
                    provenance=EXCLUDED.provenance,geom=EXCLUDED.geom,
                    spatial_precision=EXCLUDED.spatial_precision,
                    resolver_version=EXCLUDED.resolver_version,resolved_at=now(),
                    last_attempt_at=now(),attempt_count=geo_entity_resolutions.attempt_count+1,
                    updated_at=now()
                """,
                (
                    row["id"],
                    label,
                    location.get("municipality") or row.get("municipality"),
                    location.get("county") or row.get("county"),
                    location.get("state") or row.get("state") or "NJ",
                    json.dumps(provenance, default=str),
                    longitude,
                    latitude,
                    RESOLVER_VERSION,
                ),
            )
            cur.execute(
                """
                DELETE FROM alert_watch_matches
                WHERE alert_id=%s::uuid AND match_type IN ('PROXIMITY','LOCATION_TOPIC')
                """,
                (row["id"],),
            )
            cleared_spatial_matches = cur.rowcount
        conn.commit()

    return JSONResponse(
        {
            "ok": True,
            "alert_id": alert_id,
            "latitude": latitude,
            "longitude": longitude,
            "label": label,
            "match_type": "MANUAL_COORDINATE_CORRECTION",
            "spatial_rematch": "QUEUED" if row.get("rematch_eligible") else "NOT_CURRENT",
            "cleared_spatial_matches": cleared_spatial_matches,
        }
    )


@app.get("/map/system/flood.geojson")
def map_flood_geojson():
    rows = query_all(
        """
        SELECT z.id,z.fld_zone,z.zone_subty,z.sfha_tf,z.static_bfe,
               ST_AsGeoJSON(z.geom)::json AS geometry
        FROM gis_flood_zones z
        WHERE z.geom IS NOT NULL AND NOT ST_IsEmpty(z.geom)
        ORDER BY CASE WHEN z.sfha_tf='T' THEN 0 ELSE 1 END,z.fld_zone,z.id
        """
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/system/parcels.geojson")
def map_parcels_geojson(bbox: str | None = None):
    box = _bbox(bbox)
    if not box:
        return JSONResponse({"type": "FeatureCollection", "features": []})
    params = list(box)
    where = ["geom IS NOT NULL", "ST_Intersects(geom,ST_MakeEnvelope(%s,%s,%s,%s,4326))"]
    rows = query_all(
        f"""
        SELECT objectid,pams_pin,pclblock AS block,pcllot AS lot,prop_loc,
               ST_AsGeoJSON(geom)::json AS geometry
        FROM gis_parcels WHERE {' AND '.join(where)} ORDER BY objectid LIMIT 10000
        """, tuple(params)
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/system/addresses.geojson")
def map_addresses_geojson(bbox: str | None = None):
    box = _bbox(bbox)
    if not box:
        return JSONResponse({"type": "FeatureCollection", "features": []})
    params = list(box)
    where = ["geom IS NOT NULL", "ST_Intersects(geom,ST_MakeEnvelope(%s,%s,%s,%s,4326))"]
    rows = query_all(
        f"""
        SELECT objectid,fulladdr,post_comm,post_code,status,ST_AsGeoJSON(geom)::json AS geometry
        FROM gis_addresses WHERE {' AND '.join(where)}
        ORDER BY CASE WHEN status='A' THEN 0 ELSE 1 END,objectid LIMIT 10000
        """, tuple(params)
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/system/watchlist.geojson")
def map_watchlist_geojson(bbox: str | None = None, q: str = ""):
    box = _bbox(bbox)
    params: list[Any] = []
    where = ["w.active=true", "coalesce(w.spatial_geom,w.geom) IS NOT NULL"]
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "(w.display_name ILIKE %s OR coalesce(w.search_term,'') ILIKE %s "
            "OR array_to_string(w.aliases,' ') ILIKE %s OR coalesce(w.address,'') ILIKE %s "
            "OR coalesce(w.municipality,'') ILIKE %s OR coalesce(w.parent_group,'') ILIKE %s)"
        )
        params.extend([needle] * 6)
    if box:
        where.append(
            "ST_Intersects(coalesce(w.spatial_geom,w.geom),ST_MakeEnvelope(%s,%s,%s,%s,4326))"
        )
        params.extend(box)
    rows = query_all(
        f"""
        SELECT w.watch_id,w.display_name,w.address,w.watch_type,w.min_priority,w.radius_ft,
               w.spatial_scope,w.starts_at,w.expires_at,w.source_filter,w.alert_category_filter,
               CASE
                 WHEN w.starts_at>now() THEN 'SCHEDULED'
                 WHEN w.expires_at<=now() THEN 'EXPIRED'
                 WHEN w.expires_at IS NULL THEN 'PERMANENT'
                 ELSE 'ACTIVE NOW'
               END AS watch_state,
               coalesce((
                 SELECT string_agg(s.name,', ' ORDER BY s.name)
                 FROM watch_item_recipients wir
                 JOIN subscribers s ON s.id=wir.subscriber_id AND s.active=true
                 WHERE wir.watch_item_id=w.id AND wir.active=true
               ),'None') AS intended_recipients,
               ST_AsGeoJSON(coalesce(w.spatial_geom,w.geom))::json AS geometry
        FROM watch_items w
        WHERE {' AND '.join(where)}
        ORDER BY w.display_name LIMIT 5000
        """,
        params,
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/system/alerts.geojson")
def map_alerts_geojson(
    bbox: str | None = None,
    hours: int | None = 12,
    days: int | None = None,
    window: str = "",
    min_priority: int = 1,
    active_only: bool = False,
    q: str = "",
    source: str = "",
    category: str = "",
):
    # Keep the older `hours` and `days` URLs working while the map uses the same
    # named history windows as the full Alert search page.
    if window:
        window_hours = ALERT_WINDOWS.get(window.strip().lower(), 12)
    elif days is not None:
        window_hours = max(24, min(int(days), 365) * 24)
    else:
        window_hours = max(1, min(int(hours or 12), 168))
    min_priority = max(1, min(min_priority, 5))
    box = _bbox(bbox)
    params: list[Any] = [min_priority]
    where = [
        "a.priority >= %s",
        "coalesce(a.geom,r.geom) IS NOT NULL",
    ]
    if window_hours is not None:
        where.insert(0, "a.received_at >= now()-(%s * interval '1 hour')")
        params.insert(0, window_hours)
    if source.strip():
        where.append("upper(a.source)=upper(%s)")
        params.append(source.strip())
    if category.strip():
        where.append("upper(a.category)=upper(%s)")
        params.append(category.strip())
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "(coalesce(a.search_text,'') ILIKE %s OR a.title ILIKE %s OR a.message ILIKE %s "
            "OR coalesce(a.municipality,'') ILIKE %s OR a.alert_id ILIKE %s "
            "OR a.location::text ILIKE %s OR array_to_string(a.tags,' ') ILIKE %s)"
        )
        params.extend([needle] * 7)
    if active_only:
        where.append("a.status <> 'RESOLVED' AND (a.expires_at IS NULL OR a.expires_at > now())")
    if box:
        where.append("ST_Intersects(coalesce(a.geom,r.geom),ST_MakeEnvelope(%s,%s,%s,%s,4326))")
        params.extend(box)
    rows = query_all(
        f"""
        SELECT a.id,a.alert_id,a.source,a.category,a.subtype,a.status,a.event_action,
               a.title,left(a.message,600) AS message,a.priority,a.county,a.municipality,
               coalesce(wm.matched_watches,'No Watch matched') AS matched_watches,
               CASE WHEN r.match_type='MANUAL_COORDINATE_CORRECTION'
                    THEN coalesce(r.resolved_label,nullif(a.location->>'label',''),nullif(a.location->>'address',''))
                    WHEN a.geom IS NULL
                    THEN coalesce(r.resolved_label,nullif(a.location->>'label',''),nullif(a.location->>'address',''))
                    ELSE coalesce(nullif(a.location->>'label',''),nullif(a.location->>'address',''),r.resolved_label)
               END AS mapped_address,
               a.observed_at,a.received_at,a.click_url,
               r.status AS resolution_status,r.match_type,r.confidence,
               r.spatial_precision,(a.geom IS NULL) AS approximate,
               ST_AsGeoJSON(coalesce(a.geom,r.geom))::json AS geometry
        FROM alerts a
        LEFT JOIN geo_entity_resolutions r
          ON r.entity_type='ALERT' AND r.entity_id=a.id::text AND r.status='RESOLVED'
        LEFT JOIN LATERAL (
          SELECT string_agg(m.display_name,', ' ORDER BY m.display_name) AS matched_watches
          FROM (
            SELECT w.display_name
            FROM alert_watch_matches awm
            JOIN watch_items w ON w.id=awm.watch_item_id
            WHERE awm.alert_id=a.id
            UNION
            SELECT w.display_name
            FROM deliveries d
            CROSS JOIN LATERAL jsonb_array_elements_text(
              coalesce(d.matched_watch_ids,'[]'::jsonb)
            ) ids(watch_id)
            JOIN watch_items w ON w.watch_id=ids.watch_id
            WHERE d.alert_id=a.id
              AND (r.match_type IS DISTINCT FROM 'MANUAL_COORDINATE_CORRECTION'
                   OR d.created_at>=r.updated_at)
          ) m
        ) wm ON true
        WHERE {' AND '.join(where)}
        ORDER BY a.priority DESC,a.received_at DESC
        LIMIT 5000
        """,
        tuple(params),
    )
    return JSONResponse(_feature_collection(rows), media_type="application/geo+json")


@app.get("/map/system/issues.geojson")
def map_issues_geojson(bbox: str | None = None, q: str = ""):
    box = _bbox(bbox)
    params: list[Any] = []
    where = [
        "i.status NOT IN ('RESOLVED','CLOSED')",
        "coalesce(i.geom,a.geom) IS NOT NULL",
    ]
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "(i.title ILIKE %s OR coalesce(i.description,'') ILIKE %s "
            "OR coalesce(i.category,'') ILIKE %s OR coalesce(i.next_action,'') ILIKE %s "
            "OR coalesce(i.assigned_to,'') ILIKE %s OR coalesce(i.address,'') ILIKE %s "
            "OR coalesce(i.municipality,'') ILIKE %s)"
        )
        params.extend([needle] * 7)
    if box:
        where.append(
            "ST_Intersects(coalesce(i.geom,a.geom),ST_MakeEnvelope(%s,%s,%s,%s,4326))"
        )
        params.extend(box)
    rows = query_all(
        f"""
        SELECT i.id,i.title,i.item_type,i.status,i.priority,i.assigned_to,i.waiting_on,
               i.next_action,i.due_at,i.follow_up_at,i.source,
               coalesce(i.address,i.employee_location,a.fulladdr) AS mapped_address,
               ST_AsGeoJSON(coalesce(i.geom,a.geom))::json AS geometry
        FROM issues i
        LEFT JOIN LATERAL (
          SELECT ga.geom,ga.fulladdr FROM gis_addresses ga
          WHERE nullif(trim(coalesce(i.address,i.employee_location,'')),'') IS NOT NULL
            AND lower(trim(ga.fulladdr))=lower(trim(coalesce(i.address,i.employee_location,'')))
          ORDER BY CASE WHEN ga.status='A' THEN 0 ELSE 1 END,ga.objectid LIMIT 1
        ) a ON true
        WHERE {' AND '.join(where)}
        ORDER BY i.priority DESC,i.updated_at DESC LIMIT 5000
        """,
        params,
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/system/events.geojson")
def map_system_events(bbox: str | None = None, q: str = ""):
    box = _bbox(bbox)
    point = (
        "COALESCE(e.geom,CASE WHEN e.longitude IS NOT NULL AND e.latitude IS NOT NULL "
        "THEN ST_SetSRID(ST_MakePoint(e.longitude,e.latitude),4326) ELSE NULL END)"
    )
    params: list[Any] = []
    where = ["e.active=true", f"{point} IS NOT NULL"]
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "(e.title ILIKE %s OR coalesce(e.description,'') ILIKE %s "
            "OR coalesce(e.event_type,'') ILIKE %s OR coalesce(e.venue,'') ILIKE %s "
            "OR coalesce(e.address,'') ILIKE %s OR coalesce(e.municipality,'') ILIKE %s "
            "OR coalesce(e.impact_summary,'') ILIKE %s)"
        )
        params.extend([needle] * 7)
    if box:
        where.append(f"ST_Intersects({point},ST_MakeEnvelope(%s,%s,%s,%s,4326))")
        params.extend(box)
    rows = query_all(
        f"""
        WITH ranked AS (
          SELECT e.id,e.title,e.starts_at,e.ends_at,e.venue,e.address,e.municipality,e.state,
                 e.impact_level,e.impact_score,e.impact_summary,e.road_impact,e.transit_impact,
                 e.source_name,e.source_url,e.fingerprint,
                 ST_AsGeoJSON({point})::json AS geometry,
                 row_number() OVER (PARTITION BY e.fingerprint ORDER BY e.impact_score DESC,e.last_changed_at DESC,e.updated_at DESC) AS dedupe_rank
          FROM event_intelligence e
          WHERE {' AND '.join(where)}
        )
        SELECT id,title,starts_at,ends_at,venue,address,municipality,state,impact_level,impact_score,
               impact_summary,road_impact,transit_impact,source_name,source_url,geometry
        FROM ranked WHERE dedupe_rank=1
        ORDER BY CASE impact_level WHEN 'ALERT' THEN 0 WHEN 'WATCH' THEN 1 ELSE 2 END,impact_score DESC,starts_at NULLS LAST
        LIMIT 2000
        """,
        params,
    )
    return JSONResponse(_feature_collection(rows), media_type="application/geo+json")


@app.get("/map/layer/{layer_id}.geojson")
def map_custom_geojson(layer_id: uuid.UUID):
    layer = query_one("SELECT id FROM map_layers WHERE id=%s AND active=true", (layer_id,))
    if not layer:
        raise HTTPException(status_code=404)
    rows = query_all(
        """
        SELECT id,name,properties,ST_AsGeoJSON(geom)::json AS geometry
        FROM map_features WHERE layer_id=%s AND active=true ORDER BY created_at,id LIMIT 20000
        """, (layer_id,)
    )
    return JSONResponse(_feature_collection(rows))


@app.post("/map/layer/create")
def map_layer_create(name: str = Form(...), layer_type: str = Form("CUSTOM_GEOJSON"), source_url: str = Form(""), attribution: str = Form(""), color: str = Form("#4aa3df"), default_visible: str = Form("")):
    name = name.strip()
    layer_type = layer_type.strip().upper()
    source_url = source_url.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Layer name is required")
    if layer_type not in {"CUSTOM_GEOJSON", "XYZ"}:
        raise HTTPException(status_code=400, detail="Unsupported layer type")
    if layer_type == "XYZ":
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or not all(token in source_url for token in ("{z}", "{x}", "{y}")):
            raise HTTPException(status_code=400, detail="XYZ URL must be HTTPS and contain {z}, {x}, and {y}")
    else:
        source_url = ""
    execute(
        """
        INSERT INTO map_layers(layer_key,name,layer_type,source_url,attribution,style,active,default_visible,sort_order)
        VALUES (%s,%s,%s,%s,%s,jsonb_build_object('color',%s::text),true,%s,100)
        """,
        (_layer_key(name),name,layer_type,source_url or None,attribution.strip() or None,color.strip() or "#4aa3df",bool(default_visible)),
    )
    return RedirectResponse("/map?msg=Layer+created", status_code=303)


@app.post("/map/layer/{layer_id}/toggle")
def map_layer_toggle(layer_id: uuid.UUID):
    execute("UPDATE map_layers SET active=NOT active,updated_at=now() WHERE id=%s", (layer_id,))
    return RedirectResponse("/map?msg=Layer+updated", status_code=303)


@app.post("/map/layer/{layer_id}/default")
def map_layer_default(layer_id: uuid.UUID):
    execute("UPDATE map_layers SET default_visible=NOT default_visible,updated_at=now() WHERE id=%s", (layer_id,))
    return RedirectResponse("/map?msg=Default+visibility+updated", status_code=303)


@app.post("/map/layer/{layer_id}/archive")
def map_layer_archive(layer_id: uuid.UUID):
    execute("UPDATE map_layers SET active=false,default_visible=false,updated_at=now() WHERE id=%s", (layer_id,))
    return RedirectResponse("/map?msg=Layer+archived", status_code=303)


@app.post("/map/layer/{layer_id}/upload")
def map_layer_upload(layer_id: uuid.UUID, file: UploadFile = File(...), mode: str = Form("append")):
    layer = query_one("SELECT id,layer_type FROM map_layers WHERE id=%s AND active=true", (layer_id,))
    if not layer or layer["layer_type"] != "CUSTOM_GEOJSON":
        raise HTTPException(status_code=404)
    raw = file.file.read(MAX_UPLOAD_BYTES + 1)
    try:
        features, import_type = read_import(file.filename or "", raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    mode = mode.strip().lower()
    if mode not in {"append", "replace"}:
        raise HTTPException(status_code=400, detail="Import mode must be append or replace")
    inserted = _store_imported_features(layer_id, features, replace=(mode == "replace"), import_type=import_type, filename=file.filename or "")
    execute("UPDATE map_layers SET updated_at=now() WHERE id=%s", (layer_id,))
    return RedirectResponse(f"/map?msg=Imported+{inserted}+features+from+{import_type}", status_code=303)


@app.post("/map/layer/{layer_id}/feature")
async def map_feature_create(layer_id: uuid.UUID, request: Request):
    layer = query_one("SELECT id FROM map_layers WHERE id=%s AND layer_type='CUSTOM_GEOJSON' AND active=true", (layer_id,))
    if not layer:
        raise HTTPException(status_code=404)
    data = await request.json()
    geom = data.get("geometry")
    props = data.get("properties") or {}
    if not isinstance(geom, dict):
        raise HTTPException(status_code=400, detail="Geometry is required")
    name = str(data.get("name") or props.get("name") or props.get("title") or "").strip() or None
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH g AS (SELECT ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) AS geom),
                cleaned AS (SELECT CASE WHEN ST_IsValid(geom) THEN geom ELSE ST_MakeValid(geom) END AS geom FROM g)
                INSERT INTO map_features(layer_id,name,properties,geom)
                SELECT %s,%s,%s::jsonb,geom FROM cleaned
                WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) RETURNING id
                """, (json.dumps(geom),layer_id,name,json.dumps(props, default=str))
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        raise HTTPException(status_code=400, detail="Invalid geometry")
    return JSONResponse({"ok": True, "id": str(row[0] if not isinstance(row, dict) else row["id"])})


@app.post("/map/feature/{feature_id}/archive")
def map_feature_archive(feature_id: uuid.UUID):
    execute("UPDATE map_features SET active=false,updated_at=now() WHERE id=%s", (feature_id,))
    return RedirectResponse("/map?msg=Feature+archived", status_code=303)


@app.get("/map/system/managed-events.geojson")
def map_managed_events_geojson():
    rows = query_all(
        """
        SELECT e.id,e.title,e.event_status,e.event_scope,e.preparation_status,e.confirmation_status,
               e.priority,e.owner,e.starts_at,e.ends_at,e.location_name,e.address,e.municipality,
               a.fulladdr AS mapped_address,ST_AsGeoJSON(a.geom)::json AS geometry
        FROM operational_events e
        LEFT JOIN LATERAL (
          SELECT ga.geom,ga.fulladdr FROM gis_addresses ga
          WHERE nullif(trim(coalesce(e.address,'')),'') IS NOT NULL
            AND lower(trim(ga.fulladdr))=lower(trim(e.address))
          ORDER BY CASE WHEN ga.status='A' THEN 0 ELSE 1 END,ga.objectid LIMIT 1
        ) a ON true
        WHERE e.active=true AND e.event_status NOT IN ('COMPLETED','CANCELLED') AND a.geom IS NOT NULL
        ORDER BY e.starts_at,e.priority DESC LIMIT 2000
        """
    )
    return JSONResponse(_feature_collection(rows))
