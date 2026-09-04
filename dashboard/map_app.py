import json
import re
import uuid
from urllib.parse import urlparse

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from schedule_app import app
from app import db_conn, execute, query_all, query_one, templates


SYSTEM_LAYERS = [
    {
        "key": "flood",
        "name": "FEMA Flood Zones",
        "endpoint": "/map/system/flood.geojson",
        "default_visible": True,
        "style": {"color": "#e26d6d", "weight": 1, "fillOpacity": 0.28},
    },
    {
        "key": "parcels",
        "name": "Parcels",
        "endpoint": "/map/system/parcels.geojson",
        "default_visible": False,
        "style": {"color": "#7fb3d5", "weight": 1, "fillOpacity": 0.04},
        "viewport": True,
    },
    {
        "key": "addresses",
        "name": "NG911 Addresses",
        "endpoint": "/map/system/addresses.geojson",
        "default_visible": False,
        "point": True,
        "viewport": True,
    },
    {
        "key": "watchlist",
        "name": "Watch Locations",
        "endpoint": "/map/system/watchlist.geojson",
        "default_visible": True,
        "point": True,
    },
    {
        "key": "operations",
        "name": "Operations / Issues",
        "endpoint": "/map/system/issues.geojson",
        "default_visible": True,
        "point": True,
    },
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
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": features}


@app.get("/map", response_class=HTMLResponse)
def mapping_center(request: Request, msg: str = ""):
    bounds = query_one(
        """
        WITH e AS (
          SELECT ST_Extent(geom) AS b
          FROM gis_parcels
          WHERE lower(coalesce(mun_name,'')) LIKE '%%weehawken%%'
        )
        SELECT ST_XMin(b) AS minx,ST_YMin(b) AS miny,
               ST_XMax(b) AS maxx,ST_YMax(b) AS maxy
        FROM e WHERE b IS NOT NULL
        """
    )
    custom_layers = query_all(
        """
        SELECT l.id,l.layer_key,l.name,l.layer_type,l.source_url,l.attribution,
               l.style,l.active,l.default_visible,l.sort_order,
               count(f.id) FILTER (WHERE f.active=true) AS feature_count
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
        },
    )


@app.get("/map/system/flood.geojson")
def map_flood_geojson():
    rows = query_all(
        """
        WITH b AS (
          SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom
          FROM gis_parcels
          WHERE lower(coalesce(mun_name,'')) LIKE '%%weehawken%%'
        )
        SELECT z.id,z.fld_zone,z.zone_subty,z.sfha_tf,z.static_bfe,
               ST_AsGeoJSON(ST_Intersection(z.geom,b.geom))::json AS geometry
        FROM gis_flood_zones z
        CROSS JOIN b
        WHERE b.geom IS NOT NULL
          AND ST_Intersects(z.geom,b.geom)
          AND NOT ST_IsEmpty(ST_Intersection(z.geom,b.geom))
        ORDER BY CASE WHEN z.sfha_tf='T' THEN 0 ELSE 1 END,z.fld_zone,z.id
        """
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/system/parcels.geojson")
def map_parcels_geojson(bbox: str | None = None):
    box = _bbox(bbox)
    params = []
    where = ["lower(coalesce(mun_name,'')) LIKE '%%weehawken%%'"]
    if box:
        where.append("ST_Intersects(geom,ST_MakeEnvelope(%s,%s,%s,%s,4326))")
        params.extend(box)
    rows = query_all(
        f"""
        SELECT objectid,pams_pin,pclblock AS block,pcllot AS lot,prop_loc,
               ST_AsGeoJSON(geom)::json AS geometry
        FROM gis_parcels
        WHERE {' AND '.join(where)}
        ORDER BY objectid
        LIMIT 10000
        """,
        tuple(params),
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/system/addresses.geojson")
def map_addresses_geojson(bbox: str | None = None):
    box = _bbox(bbox)
    params = []
    where = ["lower(trim(coalesce(post_comm,'')))='weehawken'"]
    if box:
        where.append("ST_Intersects(geom,ST_MakeEnvelope(%s,%s,%s,%s,4326))")
        params.extend(box)
    rows = query_all(
        f"""
        SELECT objectid,fulladdr,post_comm,post_code,status,
               ST_AsGeoJSON(geom)::json AS geometry
        FROM gis_addresses
        WHERE {' AND '.join(where)}
        ORDER BY CASE WHEN status='A' THEN 0 ELSE 1 END,objectid
        LIMIT 10000
        """,
        tuple(params),
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/system/watchlist.geojson")
def map_watchlist_geojson():
    rows = query_all(
        """
        SELECT watch_id,display_name,address,watch_type,min_priority,
               ST_AsGeoJSON(geom)::json AS geometry
        FROM watch_items
        WHERE active=true AND geom IS NOT NULL
        ORDER BY display_name
        LIMIT 5000
        """
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/system/issues.geojson")
def map_issues_geojson():
    rows = query_all(
        """
        SELECT i.id,i.title,i.status,i.priority,i.source,
               coalesce(i.address,i.employee_location,a.fulladdr) AS mapped_address,
               ST_AsGeoJSON(coalesce(i.geom,a.geom))::json AS geometry
        FROM issues i
        LEFT JOIN LATERAL (
          SELECT ga.geom,ga.fulladdr
          FROM gis_addresses ga
          WHERE nullif(trim(coalesce(i.address,i.employee_location,'')),'') IS NOT NULL
            AND lower(trim(ga.fulladdr))=lower(trim(coalesce(i.address,i.employee_location,'')))
          ORDER BY CASE WHEN ga.status='A' THEN 0 ELSE 1 END,ga.objectid
          LIMIT 1
        ) a ON true
        WHERE i.status NOT IN ('RESOLVED','CLOSED')
          AND coalesce(i.geom,a.geom) IS NOT NULL
        ORDER BY i.priority DESC,i.updated_at DESC
        LIMIT 5000
        """
    )
    return JSONResponse(_feature_collection(rows))


@app.get("/map/layer/{layer_id}.geojson")
def map_custom_geojson(layer_id: uuid.UUID):
    layer = query_one("SELECT id FROM map_layers WHERE id=%s AND active=true", (layer_id,))
    if not layer:
        raise HTTPException(status_code=404)
    rows = query_all(
        """
        SELECT id,name,properties,ST_AsGeoJSON(geom)::json AS geometry
        FROM map_features
        WHERE layer_id=%s AND active=true
        ORDER BY created_at,id
        LIMIT 20000
        """,
        (layer_id,),
    )
    return JSONResponse(_feature_collection(rows))


@app.post("/map/layer/create")
def map_layer_create(
    name: str = Form(...),
    layer_type: str = Form("CUSTOM_GEOJSON"),
    source_url: str = Form(""),
    attribution: str = Form(""),
    color: str = Form("#4aa3df"),
    default_visible: str = Form(""),
):
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
        INSERT INTO map_layers(
          layer_key,name,layer_type,source_url,attribution,style,
          active,default_visible,sort_order
        )
        VALUES (%s,%s,%s,%s,%s,jsonb_build_object('color',%s),true,%s,100)
        """,
        (
            _layer_key(name),name,layer_type,source_url or None,
            attribution.strip() or None,color.strip() or "#4aa3df",
            bool(default_visible),
        ),
    )
    return RedirectResponse("/map?msg=Layer+created", status_code=303)


@app.post("/map/layer/{layer_id}/toggle")
def map_layer_toggle(layer_id: uuid.UUID):
    execute("UPDATE map_layers SET active=NOT active,updated_at=now() WHERE id=%s", (layer_id,))
    return RedirectResponse("/map?msg=Layer+updated", status_code=303)


@app.post("/map/layer/{layer_id}/upload")
async def map_layer_upload(layer_id: uuid.UUID, file: UploadFile = File(...)):
    layer = query_one(
        "SELECT id,layer_type FROM map_layers WHERE id=%s AND active=true",
        (layer_id,),
    )
    if not layer or layer["layer_type"] != "CUSTOM_GEOJSON":
        raise HTTPException(status_code=404)
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="GeoJSON upload is limited to 10 MB")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid GeoJSON") from exc

    if payload.get("type") == "FeatureCollection":
        features = payload.get("features") or []
    elif payload.get("type") == "Feature":
        features = [payload]
    else:
        features = [{"type": "Feature", "geometry": payload, "properties": {}}]
    if len(features) > 5000:
        raise HTTPException(status_code=400, detail="Web uploads are limited to 5,000 features per file")

    inserted = 0
    with db_conn() as conn:
        with conn.cursor() as cur:
            for feature in features:
                geom = feature.get("geometry")
                if not geom:
                    continue
                props = feature.get("properties") or {}
                name = str(props.get("name") or props.get("title") or "").strip() or None
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
                    (json.dumps(geom),layer_id,name,json.dumps(props)),
                )
                inserted += cur.rowcount
        conn.commit()
    return RedirectResponse(f"/map?msg=Imported+{inserted}+features", status_code=303)


@app.post("/map/layer/{layer_id}/feature")
async def map_feature_create(layer_id: uuid.UUID, request: Request):
    layer = query_one(
        "SELECT id FROM map_layers WHERE id=%s AND layer_type='CUSTOM_GEOJSON' AND active=true",
        (layer_id,),
    )
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
                WITH g AS (
                  SELECT ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) AS geom
                ), cleaned AS (
                  SELECT CASE WHEN ST_IsValid(geom) THEN geom ELSE ST_MakeValid(geom) END AS geom FROM g
                )
                INSERT INTO map_features(layer_id,name,properties,geom)
                SELECT %s,%s,%s::jsonb,geom
                FROM cleaned
                WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
                RETURNING id
                """,
                (json.dumps(geom),layer_id,name,json.dumps(props)),
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
