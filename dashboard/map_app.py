import json
import re
import uuid
from datetime import date, datetime
from urllib.parse import urlparse

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from schedule_app import app
from app import db_conn, execute, query_all, query_one, templates
from gis_import import MAX_UPLOAD_BYTES, read_import
from geo_resolver import resolve_payload


SYSTEM_LAYERS = [
    {"key": "flood", "name": "FEMA Flood Zones", "endpoint": "/map/system/flood.geojson", "default_visible": True, "style": {"color": "#e26d6d"}},
    {"key": "parcels", "name": "Parcels", "endpoint": "/map/system/parcels.geojson", "default_visible": False, "style": {"color": "#7fb3d5"}, "viewport": True},
    {"key": "addresses", "name": "NG911 Addresses", "endpoint": "/map/system/addresses.geojson", "default_visible": False, "point": True, "viewport": True},
    {"key": "watchlist", "name": "Watch Locations", "endpoint": "/map/system/watchlist.geojson", "default_visible": True, "point": True},
    {"key": "operations", "name": "Operations / Work Items", "endpoint": "/map/system/issues.geojson", "default_visible": True, "point": True},
    {"key": "event-intelligence", "name": "Event Intelligence", "endpoint": "/map/system/events.geojson", "default_visible": False, "point": True},
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
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": features}


def _feature_name(properties: dict) -> str | None:
    if not isinstance(properties, dict):
        return None
    lowered = {str(key).lower(): value for key, value in properties.items()}
    for key in ("name", "title", "label", "address", "fulladdr"):
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
          SELECT ST_Extent(geom) AS b
          FROM gis_parcels
          WHERE geom IS NOT NULL
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
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        },
    )


@app.get("/map/search")
def map_search(q: str = ""):
    needle = q.strip()
    if len(needle) < 2:
        return JSONResponse({"type": "FeatureCollection", "features": []})
    like = f"%{needle}%"
    rows = []
    rows.extend(query_all(
        """
        SELECT 'ADDRESS' AS result_type,fulladdr AS label,
               concat_ws(' · ',post_comm,post_code) AS detail,
               objectid::text AS source_id,ST_AsGeoJSON(geom)::json AS geometry
        FROM gis_addresses
        WHERE fulladdr ILIKE %s AND geom IS NOT NULL
        ORDER BY CASE WHEN status='A' THEN 0 ELSE 1 END,fulladdr LIMIT 8
        """, (like,)
    ))
    rows.extend(query_all(
        """
        SELECT 'PARCEL' AS result_type,
               coalesce(nullif(prop_loc,''),'Block ' || coalesce(pclblock,'?') || ' Lot ' || coalesce(pcllot,'?')) AS label,
               concat_ws(' · ',mun_name,'Block ' || coalesce(pclblock,'?'),'Lot ' || coalesce(pcllot,'?'),nullif(pams_pin,'')) AS detail,
               objectid::text AS source_id,ST_AsGeoJSON(geom)::json AS geometry
        FROM gis_parcels
        WHERE (prop_loc ILIKE %s OR pams_pin ILIKE %s OR pclblock ILIKE %s OR pcllot ILIKE %s)
          AND geom IS NOT NULL
        ORDER BY prop_loc NULLS LAST,objectid LIMIT 8
        """, (like, like, like, like)
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
def map_watchlist_geojson():
    rows = query_all(
        """
        SELECT watch_id,display_name,address,watch_type,min_priority,ST_AsGeoJSON(geom)::json AS geometry
        FROM watch_items WHERE active=true AND geom IS NOT NULL ORDER BY display_name LIMIT 5000
        """
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/system/issues.geojson")
def map_issues_geojson():
    rows = query_all(
        """
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
        WHERE i.status NOT IN ('RESOLVED','CLOSED') AND coalesce(i.geom,a.geom) IS NOT NULL
        ORDER BY i.priority DESC,i.updated_at DESC LIMIT 5000
        """
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/system/events.geojson")
def map_system_events():
    rows = query_all(
        """
        WITH ranked AS (
          SELECT e.id,e.title,e.starts_at,e.ends_at,e.venue,e.address,e.municipality,e.state,
                 e.impact_level,e.impact_score,e.impact_summary,e.road_impact,e.transit_impact,
                 e.source_name,e.source_url,e.fingerprint,
                 ST_AsGeoJSON(COALESCE(e.geom,CASE WHEN e.longitude IS NOT NULL AND e.latitude IS NOT NULL
                   THEN ST_SetSRID(ST_MakePoint(e.longitude,e.latitude),4326) ELSE NULL END))::json AS geometry,
                 row_number() OVER (PARTITION BY e.fingerprint ORDER BY e.impact_score DESC,e.last_changed_at DESC,e.updated_at DESC) AS dedupe_rank
          FROM event_intelligence e
          WHERE e.active=true AND (e.geom IS NOT NULL OR (e.latitude IS NOT NULL AND e.longitude IS NOT NULL))
        )
        SELECT id,title,starts_at,ends_at,venue,address,municipality,state,impact_level,impact_score,
               impact_summary,road_impact,transit_impact,source_name,source_url,geometry
        FROM ranked WHERE dedupe_rank=1
        ORDER BY CASE impact_level WHEN 'ALERT' THEN 0 WHEN 'WATCH' THEN 1 ELSE 2 END,impact_score DESC,starts_at NULLS LAST
        LIMIT 2000
        """
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
async def map_layer_upload(layer_id: uuid.UUID, file: UploadFile = File(...), mode: str = Form("append")):
    layer = query_one("SELECT id,layer_type FROM map_layers WHERE id=%s AND active=true", (layer_id,))
    if not layer or layer["layer_type"] != "CUSTOM_GEOJSON":
        raise HTTPException(status_code=404)
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
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
