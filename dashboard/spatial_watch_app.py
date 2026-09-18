"""Unified spatial matching and browser controls for the existing Watchlist."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from urllib.parse import urlencode
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


RELEASE_ID = "watchlist-reliability-ui-v1"
LOCAL_ZONE = ZoneInfo("America/New_York")
LOGGER = logging.getLogger(__name__)
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
SETUP_MODES = {"NEARBY", "KEYWORD"}


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


def _resolver_input(location_query: str, municipality: str) -> tuple[str, str]:
    """Separate a pasted full address into the local address and town fields."""
    query = location_query.strip()
    town = municipality.strip()
    if not query:
        return query, town
    parts = [part.strip() for part in query.split(",") if part.strip()]
    if len(parts) >= 2:
        return parts[0], town or parts[1]
    return query, town


def _resolve_target(conn, location_query: str, latitude: str, longitude: str, municipality: str):
    lat = _float_or_none(latitude)
    lon = _float_or_none(longitude)
    if (lat is None) != (lon is None):
        raise HTTPException(400, "Provide both latitude and longitude")
    if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Latitude or longitude is outside its valid range")
    resolver_address, resolver_municipality = _resolver_input(location_query, municipality)
    if lat is None and not resolver_address:
        raise HTTPException(400, "Enter a full street address or provide both coordinates")
    payload = {
        "address": resolver_address,
        "municipality": resolver_municipality,
        "location": {"latitude": lat, "longitude": lon} if lat is not None else {},
    }
    result = resolve_payload(conn, payload)
    if (
        result.get("status") != "RESOLVED"
        or float(result.get("confidence") or 0) < MIN_PRECISE_CONFIDENCE
        or result.get("latitude") is None
        or result.get("longitude") is None
    ):
        raise HTTPException(
            400,
            "That location was not found in the local statewide address data. "
            "Try a full street address with municipality and state, or use coordinates under Advanced.",
        )
    return result


def _point_values(resolved: dict | None) -> tuple[float | None, float | None]:
    if not resolved:
        return None, None
    return resolved.get("longitude"), resolved.get("latitude")


def _watch_redirect(*, message: str = "", error: str = "") -> RedirectResponse:
    query = urlencode({key: value for key, value in (("msg", message), ("error", error)) if value})
    return RedirectResponse(f"/watchlist{f'?{query}' if query else ''}", status_code=303)


def _friendly_watch_errors(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except HTTPException as exc:
            return _watch_redirect(error=str(exc.detail))
        except Exception:
            incident_id = uuid.uuid4().hex[:10].upper()
            LOGGER.exception("Watchlist save failed incident=%s", incident_id)
            return _watch_redirect(
                error=(
                    "The watch was not saved. No watch or routing change was committed. "
                    f"Reference {incident_id}."
                )
            )

    return wrapped


def _watch_health() -> dict:
    health = query_one(
        """
        SELECT
          count(*) AS total,
          count(*) FILTER (
            WHERE w.active
              AND (w.starts_at IS NULL OR w.starts_at<=now())
              AND (w.expires_at IS NULL OR w.expires_at>now())
          ) AS active_now,
          count(*) FILTER (
            WHERE w.active
              AND (w.starts_at IS NULL OR w.starts_at<=now())
              AND (w.expires_at IS NULL OR w.expires_at>now())
              AND EXISTS (
                SELECT 1
                FROM watch_item_recipients wir
                JOIN subscribers s ON s.id=wir.subscriber_id
                WHERE wir.watch_item_id=w.id AND wir.active AND s.active
              )
          ) AS routed_now,
          count(*) FILTER (
            WHERE w.active
              AND (w.starts_at IS NULL OR w.starts_at<=now())
              AND (w.expires_at IS NULL OR w.expires_at>now())
              AND NOT EXISTS (
                SELECT 1
                FROM watch_item_recipients wir
                JOIN subscribers s ON s.id=wir.subscriber_id
                WHERE wir.watch_item_id=w.id AND wir.active AND s.active
              )
          ) AS unrouted_now,
          count(*) FILTER (
            WHERE w.active AND w.nearby_enabled
              AND coalesce(w.spatial_target_geom,w.geom) IS NULL
          ) AS spatial_missing_target,
          count(*) FILTER (
            WHERE w.starts_at IS NOT NULL AND w.expires_at IS NOT NULL
              AND w.expires_at<=w.starts_at
          ) AS invalid_schedule,
          (SELECT count(*)
             FROM watch_item_recipients wir
             JOIN subscribers s ON s.id=wir.subscriber_id
            WHERE wir.active AND NOT s.active) AS routes_to_inactive_subscribers,
          (SELECT count(*) FROM alert_watch_matches
            WHERE matched_at>=now()-interval '7 days') AS matches_7d,
          (SELECT max(matched_at) FROM alert_watch_matches) AS last_match_at,
          (SELECT count(*) FROM deliveries
            WHERE status='SENT' AND created_at>=now()-interval '24 hours') AS sent_24h,
          (SELECT count(*) FROM deliveries
            WHERE status='FAILED' AND created_at>=now()-interval '24 hours') AS failed_24h,
          (SELECT max(sent_at) FROM deliveries WHERE status='SENT') AS last_sent_at,
          to_regprocedure('public.gis_active_spatial_watch_matches(text,geometry)') IS NOT NULL
            AS spatial_matcher_ready
        FROM watch_items w
        """
    )
    blocking = (
        int(health.get("spatial_missing_target") or 0)
        + int(health.get("invalid_schedule") or 0)
        + int(health.get("routes_to_inactive_subscribers") or 0)
        + (0 if health.get("spatial_matcher_ready") else 1)
    )
    warnings = int(health.get("unrouted_now") or 0) + int(health.get("failed_24h") or 0)
    health["status"] = "PASS" if blocking == 0 and warnings == 0 else "WARN"
    health["status_class"] = "healthy" if health["status"] == "PASS" else "warning"
    return health


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
        "setup_modes": sorted(SETUP_MODES),
    }


@app.get("/api/watchlist/health")
def watchlist_health():
    return _json_safe(_watch_health())


@app.get("/watchlist", response_class=HTMLResponse)
def spatial_watchlist(
    request: Request,
    q: str = "",
    state: str = "all",
    msg: str = "",
    error: str = "",
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
               (SELECT count(*) FROM alert_watch_matches awm
                 WHERE awm.watch_item_id=w.id
                   AND awm.matched_at>=now()-interval '7 days') AS matches_7d,
               (SELECT max(awm.matched_at) FROM alert_watch_matches awm
                 WHERE awm.watch_item_id=w.id) AS last_match_at,
               (SELECT count(*) FROM deliveries d
                 WHERE d.status='SENT'
                   AND d.created_at>=now()-interval '7 days'
                   AND d.matched_watch_ids ? w.watch_id) AS sent_7d,
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
    health = _watch_health()
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
            "error": error,
            "health": health,
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
@_friendly_watch_errors
def spatial_watch_create(
    display_name: str = Form(...),
    setup_mode: str = Form("NEARBY"),
    watch_type: str = Form(""),
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
    if not display_name:
        raise HTTPException(400, "Display name is required")
    setup_mode = setup_mode.strip().upper()
    if setup_mode not in SETUP_MODES:
        raise HTTPException(400, "Choose Nearby or Keyword setup")
    spatial_requested = setup_mode == "NEARBY" or spatial_enabled is not None
    location_label = (location_query or address).strip()
    search_term = search_term.strip()
    if spatial_requested:
        search_term = search_term or location_label.split(",", 1)[0].strip()
    elif not search_term:
        raise HTTPException(400, "Enter the words this watch should match")
    watch_type = watch_type.strip().upper() or ("ADDRESS" if spatial_requested else "PHRASE")
    match_mode = match_mode.upper().strip()
    validate_watch(match_mode, match_field, min_priority)
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    start, end = _schedule(duration, starts_at, expires_at)
    watch_uuid = uuid.uuid4()
    with db_conn() as conn:
        resolved = None
        if spatial_requested:
            resolved = _resolve_target(conn, location_query or address, latitude, longitude, municipality)
        resolved_lon, resolved_lat = _point_values(resolved)
        saved_address = location_label or (resolved.get("label") if resolved else "") or None
        saved_municipality = (
            municipality.strip() or (resolved.get("municipality") if resolved else "") or None
        )
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
                  CASE WHEN %s::double precision IS NULL OR %s::double precision IS NULL THEN NULL
                       ELSE ST_SetSRID(ST_MakePoint(%s::double precision,%s::double precision),4326) END,
                  CASE WHEN %s::double precision IS NULL OR %s::double precision IS NULL THEN NULL
                       ELSE ST_SetSRID(ST_MakePoint(%s::double precision,%s::double precision),4326) END
                )
                """,
                (
                    watch_uuid, make_watch_id(display_name), watch_type, display_name,
                    search_term, csv_array(aliases), match_mode, match_field.strip() or None,
                    category.strip() or None, csv_array(tags), min_priority, saved_address,
                    saved_municipality, notes.strip() or None, csv_array(source_filter),
                    csv_array(alert_category_filter), start, end, resolved is not None, resolved is not None,
                    radius_ft,
                    resolved_lon, resolved_lat, resolved_lon, resolved_lat,
                    resolved_lon, resolved_lat, resolved_lon, resolved_lat,
                ),
            )
            _save_recipients(cur, watch_uuid, subscriber_ids)
        conn.commit()
    message = "Watch and routing saved"
    if not subscriber_ids:
        message = "Watch saved. Select a notification channel when you are ready to receive alerts."
    return _watch_redirect(message=message)


@app.post("/watchlist/{item_id}/update")
@_friendly_watch_errors
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
        resolved_lon, resolved_lat = _point_values(resolved)
        saved_address = (location_query or address).strip()
        if resolved and not saved_address:
            saved_address = resolved.get("label") or ""
        saved_municipality = municipality.strip()
        if resolved and not saved_municipality:
            saved_municipality = resolved.get("municipality") or ""
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE watch_items SET
                  active=%s,watch_type=%s,display_name=%s,search_term=%s,aliases=%s,
                  match_mode=%s,match_field=%s,category=%s,tags=%s,min_priority=%s,address=%s,
                  municipality=%s,notes=%s,source_filter=%s,alert_category_filter=%s,
                  starts_at=%s,expires_at=%s,gis_enabled=%s,nearby_enabled=%s,radius_ft=%s,
                  geom=CASE
                    WHEN spatial_reference_entity_id IS NOT NULL
                      OR %s::double precision IS NULL OR %s::double precision IS NULL THEN geom
                    ELSE ST_SetSRID(ST_MakePoint(%s::double precision,%s::double precision),4326) END,
                  spatial_target_geom=CASE
                    WHEN spatial_reference_entity_id IS NOT NULL
                      OR %s::double precision IS NULL OR %s::double precision IS NULL THEN spatial_target_geom
                    ELSE ST_SetSRID(ST_MakePoint(%s::double precision,%s::double precision),4326) END,
                  updated_at=now()
                WHERE id=%s
                """,
                (
                    active is not None, watch_type.strip().upper(), display_name, search_term,
                    csv_array(aliases), match_mode, match_field.strip() or None, category.strip() or None,
                    csv_array(tags), min_priority, saved_address or None, saved_municipality or None,
                    notes.strip() or None, csv_array(source_filter), csv_array(alert_category_filter),
                    start, end, spatial_enabled is not None, spatial_enabled is not None, radius_ft,
                    resolved_lon, resolved_lat, resolved_lon, resolved_lat,
                    resolved_lon, resolved_lat, resolved_lon, resolved_lat,
                    item_id,
                ),
            )
            _save_recipients(cur, item_id, subscriber_ids)
        conn.commit()
    message = "Watch and routing updated"
    if not subscriber_ids:
        message = "Watch updated. It has no active notification channel selected."
    return _watch_redirect(message=message)


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
