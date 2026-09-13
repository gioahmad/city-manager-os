"""Unified spatial matching and browser controls for the existing Watchlist."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from schedule_app import app
from app import (
    MATCH_MODES,
    WATCH_TYPES,
    csv_array,
    db_conn,
    make_watch_id,
    query_all,
    query_one,
    templates,
    validate_watch,
)
from geo_resolver import MIN_PRECISE_CONFIDENCE, resolve_payload


RELEASE_ID = "issue-56-unified-spatial-watch-pack-v1"
LOCAL_ZONE = ZoneInfo("America/New_York")
SPATIAL_DURATIONS = {
    "PERMANENT": None,
    "1_HOUR": 1,
    "4_HOURS": 4,
    "8_HOURS": 8,
    "12_HOURS": 12,
    "24_HOURS": 24,
    "48_HOURS": 48,
    "3_DAYS": 72,
    "7_DAYS": 168,
    "CUSTOM": -1,
}
SPATIAL_WATCH_TYPES = [
    *WATCH_TYPES,
    "POINT",
    "INTERSECTION",
    "PLACE",
    "PARCEL",
    "CORRIDOR",
]


def _remove_route(path: str, method: str) -> None:
    app.router.routes = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", None) == path
            and method in (getattr(route, "methods", set()) or set())
        )
    ]


def _parse_local(value: str):
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, "Invalid date and time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_ZONE)
    return parsed.astimezone(timezone.utc)


def _local_value(value):
    if not value:
        return ""
    return value.astimezone(LOCAL_ZONE).strftime("%Y-%m-%dT%H:%M")


def _schedule(duration: str, starts_at: str, expires_at: str):
    duration = duration.strip().upper()
    if duration not in SPATIAL_DURATIONS:
        raise HTTPException(400, "Invalid duration")
    start = _parse_local(starts_at)
    end = _parse_local(expires_at)
    hours = SPATIAL_DURATIONS[duration]
    if hours is None:
        end = None
    elif hours > 0:
        start = start or datetime.now(timezone.utc)
        end = start + timedelta(hours=hours)
    elif end is None:
        raise HTTPException(400, "Custom duration requires an end date and time")
    if start and end and end <= start:
        raise HTTPException(400, "Watch end must be after its start")
    return start, end


def _float_or_none(value: str):
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise HTTPException(400, "Latitude and longitude must be numbers") from exc


def _resolve_target(conn, location_query: str, latitude: str, longitude: str, municipality: str):
    lat = _float_or_none(latitude)
    lon = _float_or_none(longitude)
    if (lat is None) != (lon is None):
        raise HTTPException(400, "Provide both latitude and longitude")
    if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Latitude or longitude is outside its valid range")
    payload = {
        "address": location_query.strip(),
        "municipality": municipality.strip(),
        "location": {"latitude": lat, "longitude": lon} if lat is not None else {},
    }
    result = resolve_payload(conn, payload)
    if (
        result.get("status") != "RESOLVED"
        or float(result.get("confidence") or 0) < MIN_PRECISE_CONFIDENCE
        or result.get("latitude") is None
        or result.get("longitude") is None
    ):
        raise HTTPException(400, "Location did not resolve to trustworthy point geometry")
    return result


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, timedelta, Decimal, uuid.UUID)):
        return str(value)
    return value


def _watch_state(row: dict) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    if not row.get("active"):
        return "PAUSED", "inactive-status"
    if row.get("starts_at") and row["starts_at"] > now:
        return "SCHEDULED", "waiting"
    if row.get("expires_at") and row["expires_at"] <= now:
        return "EXPIRED", "inactive-status"
    if row.get("nearby_enabled") and not row.get("expires_at"):
        return "PERMANENT", "active"
    return "ACTIVE NOW", "active"


for route_path, route_method in (
    ("/watchlist", "GET"),
    ("/watchlist/create", "POST"),
    ("/watchlist/{item_id}/update", "POST"),
):
    _remove_route(route_path, route_method)


@app.get("/api/spatial-watch/release")
def spatial_watch_release():
    return {
        "release_id": RELEASE_ID,
        "architecture": "SEE IT -> TRACK IT -> TELL ME",
        "watch_source_of_truth": "watch_items",
        "delivery_path": ["Subscribers", "Routing", "Delivery Guard", "ntfy"],
    }


@app.get("/watchlist", response_class=HTMLResponse)
def spatial_watchlist(
    request: Request,
    q: str = "",
    state: str = "all",
    msg: str = "",
    display_name: str = "",
    location_query: str = "",
    latitude: str = "",
    longitude: str = "",
):
    where = []
    params = []
    if state == "active":
        where.append("active=true AND (starts_at IS NULL OR starts_at<=now()) AND (expires_at IS NULL OR expires_at>now())")
    elif state == "scheduled":
        where.append("active=true AND starts_at>now()")
    elif state == "expired":
        where.append("active=true AND expires_at<=now()")
    elif state == "paused":
        where.append("active=false")
    elif state == "spatial":
        where.append("nearby_enabled=true")
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append("(display_name ILIKE %s OR search_term ILIKE %s OR watch_id ILIKE %s OR municipality ILIKE %s)")
        params.extend([needle] * 4)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    items = query_all(
        f"""
        SELECT w.id,w.watch_id,w.active,w.watch_type,w.display_name,w.search_term,w.aliases,
               w.match_mode,w.match_field,w.category,w.subcategory,w.tags,w.min_priority,
               w.address,w.municipality,w.county,w.state,w.block,w.lot,w.notes,
               w.source_filter,w.alert_category_filter,w.starts_at,w.expires_at,w.nearby_enabled,
               w.radius_ft,w.latitude,w.longitude,w.spatial_scope,w.spatial_reference_entity_id,
               ST_GeometryType(w.spatial_target_geom) AS spatial_target_type,
               COALESCE((SELECT array_agg(wir.subscriber_id::text ORDER BY wir.subscriber_id)
                         FROM watch_item_recipients wir
                         WHERE wir.watch_item_id=w.id AND wir.active),ARRAY[]::text[]) AS recipient_ids
        FROM watch_items w
        {clause}
        ORDER BY w.active DESC,w.display_name
        LIMIT 300
        """,
        params,
    )
    for row in items:
        row["state_label"], row["state_class"] = _watch_state(row)
        row["starts_local"] = _local_value(row.get("starts_at"))
        row["expires_local"] = _local_value(row.get("expires_at"))
        row["duration"] = "CUSTOM" if row.get("expires_at") else "PERMANENT"
        row["recipient_ids"] = set(row.get("recipient_ids") or [])
    counts = query_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE active AND (starts_at IS NULL OR starts_at<=now())
                                  AND (expires_at IS NULL OR expires_at>now())) AS active,
               count(*) FILTER (WHERE NOT active) AS paused,
               count(*) FILTER (WHERE active AND starts_at>now()) AS scheduled,
               count(*) FILTER (WHERE active AND expires_at<=now()) AS expired,
               count(*) FILTER (WHERE nearby_enabled) AS spatial
        FROM watch_items
        """
    )
    subscribers = query_all(
        "SELECT id::text AS id,subscriber_id,name,ntfy_topic FROM subscribers WHERE active=true ORDER BY name"
    )
    return templates.TemplateResponse(
        request=request,
        name="watchlist.html",
        context={
            "items": items,
            "counts": counts,
            "subscribers": subscribers,
            "q": q,
            "state": state,
            "msg": msg,
            "watch_types": SPATIAL_WATCH_TYPES,
            "match_modes": sorted(MATCH_MODES),
            "spatial_durations": list(SPATIAL_DURATIONS),
            "prefill": {
                "display_name": display_name,
                "location_query": location_query,
                "latitude": latitude,
                "longitude": longitude,
            },
        },
    )


def _save_recipients(cur, watch_item_id: uuid.UUID, subscriber_ids: list[uuid.UUID]):
    cur.execute("UPDATE watch_item_recipients SET active=false WHERE watch_item_id=%s", (watch_item_id,))
    for subscriber_id in subscriber_ids:
        cur.execute("SELECT id FROM subscribers WHERE id=%s AND active=true", (subscriber_id,))
        if not cur.fetchone():
            raise HTTPException(400, "One or more selected subscribers are inactive or missing")
        cur.execute(
            """INSERT INTO watch_item_recipients(watch_item_id,subscriber_id,active)
               VALUES(%s,%s,true)
               ON CONFLICT(watch_item_id,subscriber_id) DO UPDATE SET active=true""",
            (watch_item_id, subscriber_id),
        )


@app.post("/watchlist/create")
def spatial_watch_create(
    display_name: str = Form(...),
    watch_type: str = Form("PHRASE"),
    search_term: str = Form(""),
    match_mode: str = Form("CONTAINS"),
    match_field: str = Form(""),
    aliases: str = Form(""),
    category: str = Form(""),
    tags: str = Form(""),
    municipality: str = Form(""),
    address: str = Form(""),
    min_priority: int = Form(1),
    notes: str = Form(""),
    source_filter: str = Form(""),
    alert_category_filter: str = Form(""),
    spatial_enabled: str | None = Form(None),
    location_query: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    radius_ft: float = Form(500.0),
    duration: str = Form("PERMANENT"),
    starts_at: str = Form(""),
    expires_at: str = Form(""),
    subscriber_ids: list[uuid.UUID] = Form([]),
):
    display_name = display_name.strip()
    search_term = search_term.strip() or ("" if spatial_enabled is not None else display_name)
    if not display_name:
        raise HTTPException(400, "Display name is required")
    match_mode = match_mode.upper().strip()
    validate_watch(match_mode, match_field, min_priority)
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    start, end = _schedule(duration, starts_at, expires_at)
    watch_uuid = uuid.uuid4()
    with db_conn() as conn:
        resolved = None
        if spatial_enabled is not None:
            resolved = _resolve_target(conn, location_query or address, latitude, longitude, municipality)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watch_items(
                  id,watch_id,active,watch_type,display_name,search_term,aliases,match_mode,match_field,
                  category,tags,min_priority,address,municipality,notes,source_filter,alert_category_filter,
                  starts_at,expires_at,gis_enabled,nearby_enabled,radius_ft,spatial_scope,
                  geom,spatial_target_geom
                ) VALUES(
                  %s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'RADIUS',
                  CASE WHEN %s IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint(%s,%s),4326) END,
                  CASE WHEN %s IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint(%s,%s),4326) END
                )
                """,
                (
                    watch_uuid, make_watch_id(display_name), watch_type.strip().upper(), display_name,
                    search_term, csv_array(aliases), match_mode, match_field.strip() or None,
                    category.strip() or None, csv_array(tags), min_priority, address.strip() or None,
                    municipality.strip() or None, notes.strip() or None, csv_array(source_filter),
                    csv_array(alert_category_filter), start, end, resolved is not None, resolved is not None,
                    radius_ft, resolved.get("longitude") if resolved else None,
                    resolved.get("longitude") if resolved else None, resolved.get("latitude") if resolved else None,
                    resolved.get("longitude") if resolved else None,
                    resolved.get("longitude") if resolved else None, resolved.get("latitude") if resolved else None,
                ),
            )
            _save_recipients(cur, watch_uuid, subscriber_ids)
        conn.commit()
    return RedirectResponse("/watchlist?msg=Watch+and+routing+saved", status_code=303)


@app.post("/watchlist/{item_id}/update")
def spatial_watch_update(
    item_id: uuid.UUID,
    display_name: str = Form(...),
    watch_type: str = Form(...),
    search_term: str = Form(""),
    match_mode: str = Form(...),
    match_field: str = Form(""),
    aliases: str = Form(""),
    category: str = Form(""),
    tags: str = Form(""),
    municipality: str = Form(""),
    address: str = Form(""),
    min_priority: int = Form(1),
    notes: str = Form(""),
    source_filter: str = Form(""),
    alert_category_filter: str = Form(""),
    spatial_enabled: str | None = Form(None),
    location_query: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    radius_ft: float = Form(500.0),
    duration: str = Form("PERMANENT"),
    starts_at: str = Form(""),
    expires_at: str = Form(""),
    active: str | None = Form(None),
    subscriber_ids: list[uuid.UUID] = Form([]),
):
    display_name = display_name.strip()
    search_term = search_term.strip() or ("" if spatial_enabled is not None else display_name)
    if not display_name:
        raise HTTPException(400, "Display name is required")
    match_mode = match_mode.upper().strip()
    validate_watch(match_mode, match_field, min_priority)
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    start, end = _schedule(duration, starts_at, expires_at)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id,spatial_reference_entity_id,latitude,longitude FROM watch_items WHERE id=%s FOR UPDATE",
                (item_id,),
            )
            current = cur.fetchone()
            if not current:
                raise HTTPException(404, "Watch item not found")
        resolved = None
        if spatial_enabled is not None and current.get("spatial_reference_entity_id") is None:
            resolved = _resolve_target(conn, location_query or address, latitude, longitude, municipality)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE watch_items SET
                  active=%s,watch_type=%s,display_name=%s,search_term=%s,aliases=%s,
                  match_mode=%s,match_field=%s,category=%s,tags=%s,min_priority=%s,address=%s,
                  municipality=%s,notes=%s,source_filter=%s,alert_category_filter=%s,
                  starts_at=%s,expires_at=%s,nearby_enabled=%s,radius_ft=%s,
                  geom=CASE WHEN spatial_reference_entity_id IS NOT NULL OR %s IS NULL THEN geom
                            ELSE ST_SetSRID(ST_MakePoint(%s,%s),4326) END,
                  spatial_target_geom=CASE
                    WHEN spatial_reference_entity_id IS NOT NULL OR %s IS NULL THEN spatial_target_geom
                    ELSE ST_SetSRID(ST_MakePoint(%s,%s),4326) END,
                  updated_at=now()
                WHERE id=%s
                """,
                (
                    active is not None, watch_type.strip().upper(), display_name, search_term,
                    csv_array(aliases), match_mode, match_field.strip() or None, category.strip() or None,
                    csv_array(tags), min_priority, address.strip() or None, municipality.strip() or None,
                    notes.strip() or None, csv_array(source_filter), csv_array(alert_category_filter),
                    start, end, spatial_enabled is not None, radius_ft,
                    resolved.get("longitude") if resolved else None,
                    resolved.get("longitude") if resolved else None, resolved.get("latitude") if resolved else None,
                    resolved.get("longitude") if resolved else None,
                    resolved.get("longitude") if resolved else None, resolved.get("latitude") if resolved else None,
                    item_id,
                ),
            )
            _save_recipients(cur, item_id, subscriber_ids)
        conn.commit()
    return RedirectResponse("/watchlist?msg=Watch+and+routing+updated", status_code=303)


@app.get("/api/spatial-watch/{watch_id}/nearby-history")
def spatial_watch_history(
    watch_id: str,
    radius_ft: float = 500.0,
    hours: int = 24,
    source: str = "",
    category: str = "",
    min_priority: int = 1,
):
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    hours = max(1, min(int(hours), 8760))
    row = query_one(
        """SELECT gis_spatial_history(
                 coalesce(spatial_target_geom,geom),%s,make_interval(hours=>%s),%s,%s,%s
               ) AS context
               FROM watch_items WHERE watch_id=%s""",
        (radius_ft, hours, source or None, category or None, max(1, min(min_priority, 5)), watch_id),
    )
    if not row:
        raise HTTPException(404, "Watch item not found")
    return JSONResponse(_json_safe(row["context"]))


@app.get("/api/spatial-watch-point/nearby-history")
def point_spatial_history(
    lat: float,
    lon: float,
    radius_ft: float = 500.0,
    hours: int = 24,
    source: str = "",
    category: str = "",
    min_priority: int = 1,
):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Invalid coordinates")
    row = query_one(
        """SELECT gis_spatial_history(
                 ST_SetSRID(ST_MakePoint(%s,%s),4326),%s,make_interval(hours=>%s),%s,%s,%s
               ) AS context""",
        (lon, lat, max(1.0, min(radius_ft, 26400.0)), max(1, min(hours, 8760)),
         source or None, category or None, max(1, min(min_priority, 5))),
    )
    return JSONResponse(_json_safe(row["context"]))


@app.get("/api/alerts/{alert_id}/spatial-impact")
def alert_spatial_impact(alert_id: str, radius_ft: float = 500.0, hours: int = 24):
    row = query_one(
        """SELECT gis_spatial_history(a.geom,%s,make_interval(hours=>%s),NULL,NULL,1) AS context
           FROM alerts a WHERE a.alert_id=%s AND a.geom IS NOT NULL""",
        (max(1.0, min(radius_ft, 26400.0)), max(1, min(hours, 8760)), alert_id),
    )
    if not row:
        raise HTTPException(404, "Alert has no trustworthy spatial geometry")
    return JSONResponse(_json_safe(row["context"]))


@app.get("/api/alerts/{alert_id}/impact-buffer.geojson")
def alert_impact_buffer(alert_id: str, radius_ft: float = 500.0):
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    row = query_one(
        """SELECT title,ST_AsGeoJSON(ST_Buffer(geom::geography,%s*0.3048)::geometry)::json AS geometry
           FROM alerts WHERE alert_id=%s AND geom IS NOT NULL""",
        (radius_ft, alert_id),
    )
    if not row:
        raise HTTPException(404, "Alert has no trustworthy spatial geometry")
    return JSONResponse(
        {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": row["geometry"],
                "properties": {"alert_id": alert_id, "title": row["title"], "radius_ft": radius_ft},
            }],
        },
        media_type="application/geo+json",
    )
