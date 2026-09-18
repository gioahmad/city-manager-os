"""Unified spatial matching and browser controls for the existing Watchlist."""

from __future__ import annotations

import html
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from urllib import error as urllib_error
from urllib import request as urllib_request
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


RELEASE_ID = "alerting-spatial-watch-simplification-v1"
LOCAL_ZONE = ZoneInfo("America/New_York")
LOGGER = logging.getLogger(__name__)
NTFY_PUBLISH_BASE = os.getenv("CMOS_NTFY_PUBLISH_BASE", "http://100.94.203.47:8080").rstrip("/")
SPATIAL_DURATIONS = {
    "PERMANENT": None,
    "1_HOUR": 1,
    "4_HOURS": 4,
    "6_HOURS": 6,
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
SETUP_MODES = {"LOCATION", "TOPIC", "LOCATION_TOPIC"}
SETUP_MODE_ALIASES = {
    "NEARBY": "LOCATION",
    "KEYWORD": "TOPIC",
}
LOCATION_KINDS = {
    "ADDRESS",
    "PARCEL",
    "REFERENCE",
    "CUSTOM_FEATURE",
    "MUNICIPALITY",
    "MAP_POINT",
    "RESOLVED_ADDRESS",
    "TYPED_ADDRESS",
    "EXISTING",
}


def _setup_mode(value: str) -> str:
    mode = SETUP_MODE_ALIASES.get(value.strip().upper(), value.strip().upper())
    if mode not in SETUP_MODES:
        raise HTTPException(
            400,
            "Choose Near a location, About a topic, or Location plus topic",
        )
    return mode


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _distance_label(value) -> str:
    feet = float(value or 0)
    if feet <= 0:
        return ""
    if feet == 5280:
        return "1 mile"
    if feet == 2640:
        return "half mile"
    if feet > 5280 and feet % 5280 == 0:
        return f"{feet / 5280:g} miles"
    return f"{feet:,.0f} feet"


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


def _location_kind(value: str) -> str:
    kind = value.strip().upper() or "TYPED_ADDRESS"
    if kind not in LOCATION_KINDS:
        raise HTTPException(400, "Choose the location again")
    return kind


def _selected_location(
    conn,
    *,
    kind: str,
    source_id: str,
    location_query: str,
    latitude: str,
    longitude: str,
    municipality: str,
    current: dict | None = None,
) -> dict:
    """Resolve a UI location through existing local PostGIS sources only."""
    kind = _location_kind(kind)
    source_id = source_id.strip()
    location_query = location_query.strip()
    municipality = municipality.strip()

    if kind == "EXISTING":
        if not current:
            raise HTTPException(400, "Choose a location")
        if current.get("target_wkt"):
            existing_watch_type = str(current.get("watch_type") or "").upper()
            if existing_watch_type == "LOCATION_TOPIC":
                existing_watch_type = "ADDRESS" if current.get("address") else "AREA"
            return {
                "kind": "EXISTING",
                "label": current.get("address") or current.get("display_name") or "Saved location",
                "address": current.get("address"),
                "municipality": current.get("municipality"),
                "county": current.get("county"),
                "state": current.get("state"),
                "parcel_id": current.get("parcel_id"),
                "block": current.get("block"),
                "lot": current.get("lot"),
                "target_wkt": current.get("target_wkt"),
                "spatial_reference_entity_id": current.get("spatial_reference_entity_id"),
                "watch_type": existing_watch_type or "AREA",
                "spatial": True,
                "replace_target": False,
            }
        if current.get("municipality"):
            return {
                "kind": "MUNICIPALITY",
                "label": current["municipality"],
                "municipality": current["municipality"],
                "watch_type": "TOWN",
                "spatial": False,
                "replace_target": False,
            }
        raise HTTPException(400, "Choose a location")

    if kind in {"MAP_POINT", "RESOLVED_ADDRESS", "TYPED_ADDRESS"}:
        resolved = _resolve_target(
            conn,
            location_query,
            latitude,
            longitude,
            municipality,
        )
        lon, lat = _point_values(resolved)
        label = location_query or resolved.get("label") or "Map selection"
        return {
            "kind": kind,
            "label": label,
            "address": resolved.get("label") or location_query or None,
            "municipality": resolved.get("municipality") or municipality or None,
            "county": resolved.get("county"),
            "state": resolved.get("state") or "NJ",
            "parcel_id": resolved.get("parcel_id"),
            "target_wkt": f"SRID=4326;POINT({float(lon)} {float(lat)})",
            "spatial_reference_entity_id": None,
            "watch_type": "POINT" if kind == "MAP_POINT" else "ADDRESS",
            "spatial": True,
            "replace_target": True,
        }

    with conn.cursor() as cur:
        if kind == "ADDRESS":
            try:
                object_id = int(source_id)
            except ValueError as exc:
                raise HTTPException(400, "Choose the address again") from exc
            cur.execute(
                """
                SELECT fulladdr AS label,fulladdr AS address,
                       coalesce(nullif(inc_muni,''),nullif(post_comm,'')) AS municipality,
                       county,state,post_code AS postal_code,pcl_guid AS parcel_id,
                       ST_AsEWKT(geom) AS target_wkt
                FROM gis_addresses
                WHERE objectid=%s AND geom IS NOT NULL
                LIMIT 1
                """,
                (object_id,),
            )
            row = cur.fetchone()
            watch_type = "ADDRESS"
        elif kind == "PARCEL":
            try:
                object_id = int(source_id)
            except ValueError as exc:
                raise HTTPException(400, "Choose the parcel again") from exc
            cur.execute(
                """
                SELECT coalesce(nullif(prop_loc,''),
                                'Block '||coalesce(pclblock,'?')||' Lot '||coalesce(pcllot,'?')) AS label,
                       nullif(prop_loc,'') AS address,mun_name AS municipality,county,'NJ' AS state,
                       coalesce(pcl_guid,pams_pin) AS parcel_id,pclblock AS block,pcllot AS lot,
                       ST_AsEWKT(geom) AS target_wkt
                FROM gis_parcels
                WHERE objectid=%s AND geom IS NOT NULL
                LIMIT 1
                """,
                (object_id,),
            )
            row = cur.fetchone()
            watch_type = "PARCEL"
        elif kind == "REFERENCE":
            try:
                entity_id = uuid.UUID(source_id)
            except ValueError as exc:
                raise HTTPException(400, "Choose the saved location again") from exc
            cur.execute(
                """
                SELECT canonical_name AS label,normalized_address AS address,municipality,county,state,
                       parcel_id,parcel_objectid::text AS source_object_id,
                       ST_AsEWKT(geom) AS target_wkt,entity_id AS spatial_reference_entity_id,
                       entity_type
                FROM spatial_reference_entities
                WHERE entity_id=%s AND active=true AND geom IS NOT NULL
                """,
                (entity_id,),
            )
            row = cur.fetchone()
            watch_type = (
                "CORRIDOR"
                if row and row.get("entity_type") == "CORRIDOR"
                else "PARCEL"
                if row and row.get("entity_type") == "PARCEL_REFERENCE"
                else "FACILITY"
                if row and row.get("entity_type") in {"FACILITY", "VENUE", "LANDMARK"}
                else "AREA"
            )
        elif kind == "CUSTOM_FEATURE":
            try:
                feature_id = uuid.UUID(source_id)
            except ValueError as exc:
                raise HTTPException(400, "Choose the drawn area again") from exc
            cur.execute(
                """
                SELECT coalesce(nullif(f.name,''),l.name) AS label,
                       nullif(f.properties->>'address','') AS address,
                       nullif(f.properties->>'municipality','') AS municipality,
                       nullif(f.properties->>'county','') AS county,
                       coalesce(nullif(f.properties->>'state',''),'NJ') AS state,
                       ST_AsEWKT(f.geom) AS target_wkt
                FROM map_features f
                JOIN map_layers l ON l.id=f.layer_id
                WHERE f.id=%s AND f.active=true AND l.active=true AND f.geom IS NOT NULL
                """,
                (feature_id,),
            )
            row = cur.fetchone()
            watch_type = "AREA"
        else:
            candidate = source_id or location_query or municipality
            if not candidate:
                raise HTTPException(400, "Choose a municipality")
            cur.execute(
                """
                SELECT coalesce(nullif(inc_muni,''),nullif(post_comm,'')) AS label
                FROM gis_addresses
                WHERE geom IS NOT NULL
                  AND lower(trim(coalesce(nullif(inc_muni,''),nullif(post_comm,''))))=lower(trim(%s))
                GROUP BY coalesce(nullif(inc_muni,''),nullif(post_comm,''))
                ORDER BY count(*) DESC
                LIMIT 1
                """,
                (candidate,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(400, "That municipality was not found in the local address data")
            return {
                "kind": "MUNICIPALITY",
                "label": row["label"],
                "municipality": row["label"],
                "watch_type": "TOWN",
                "spatial": False,
                "replace_target": True,
            }

    if not row:
        raise HTTPException(400, "That location is no longer available. Search and choose it again.")
    result = dict(row)
    result.update(
        {
            "kind": kind,
            "watch_type": watch_type,
            "spatial": True,
            "replace_target": True,
        }
    )
    return result


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
                    "The watch was not saved. No Watch or Recipient connection was changed. "
                    f"Reference {incident_id}."
                )
            )

    return wrapped


def _friendly_watch_page_errors(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except HTTPException as exc:
            return HTMLResponse(
                f"<h1>Watches</h1><p>{html.escape(str(exc.detail))}</p>"
                "<p><a href='/watchlist'>Try again</a></p>",
                status_code=exc.status_code,
            )
        except Exception:
            incident_id = uuid.uuid4().hex[:10].upper()
            LOGGER.exception("Watchlist page failed incident=%s", incident_id)
            return HTMLResponse(
                "<h1>Watches are temporarily unavailable</h1>"
                "<p>No Watch or Notification setting was changed. "
                f"Reference {incident_id}.</p><p><a href='/watchlist'>Try again</a></p>",
                status_code=503,
            )

    return wrapped


def _friendly_watch_api_errors(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except HTTPException as exc:
            return JSONResponse({"ok": False, "message": str(exc.detail)}, status_code=exc.status_code)
        except Exception:
            incident_id = uuid.uuid4().hex[:10].upper()
            LOGGER.exception("Watchlist API action failed incident=%s", incident_id)
            return JSONResponse(
                {
                    "ok": False,
                    "message": f"The request could not be completed. Reference {incident_id}.",
                },
                status_code=500,
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


def _watch_state(row: dict) -> tuple[str, str, str]:
    now = datetime.now(timezone.utc)
    if row.get("expires_at") and row["expires_at"] <= now:
        return "Expired", "inactive-status", "This Watch reached its end time. Reactivate it to keep watching until paused, then edit the duration if needed."
    if not row.get("active"):
        return "Paused", "inactive-status", "This watch is paused and cannot create new matches or notifications."
    if row.get("starts_at") and row["starts_at"] > now:
        return "Watching", "waiting", "Saved and ready. Matching begins at the scheduled start time."
    if row.get("nearby_enabled") and not row.get("spatial_target_type"):
        return "Delivery Problem", "error", "The saved Location is missing. Edit this watch and choose the Location again."
    if _safe_int(row.get("active_recipient_count")) == 0:
        return "Needs Recipient", "warning", "This watch can record Matches, but no Recipient is selected for Notifications."
    if str(row.get("last_delivery_status") or "").upper() == "FAILED":
        return "Delivery Problem", "error", "The latest Notification could not be delivered. Test the Recipient and review delivery history."
    if str(row.get("last_delivery_status") or "").upper() == "SUPPRESSED":
        return "Matching", "waiting", "A Match was recorded. Delivery Guard held the latest repeat or duplicate Notification."
    last_match = row.get("last_match_at")
    last_delivery = row.get("last_delivery_at")
    if str(row.get("last_delivery_status") or "").upper() == "PENDING" or (
        last_match and (not last_delivery or last_match > last_delivery)
    ):
        return "Matching", "waiting", "A Match was recorded and the Notification path is being checked."
    if not last_match:
        return "Watching", "active", "No stored alert has met this watch's Location, topic, priority, and optional filters yet."
    return "Watching", "active", "The watch is on. Its latest Match and Notification evidence are shown below."


for route_path, route_method in (
    ("/watchlist", "GET"),
    ("/watchlist/create", "POST"),
    ("/watchlist/{item_id}/update", "POST"),
    ("/watchlist/{item_id}/toggle", "POST"),
    ("/watchlist/{item_id}/delete", "POST"),
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
        "setup_mode_aliases": SETUP_MODE_ALIASES,
        "location_kinds": sorted(LOCATION_KINDS),
        "watch_states": [
            "Watching",
            "Paused",
            "Expired",
            "Needs Recipient",
            "Matching",
            "Delivery Problem",
        ],
        "normal_terms": {
            "watch": "saved monitoring rule",
            "match": "alert or event caught by a watch",
            "notification": "message delivered",
            "location": "address, parcel, street, municipality, or map area",
            "distance": "area around a location",
            "recipient": "person or notification channel",
        },
        "default_distance_ft": 5280,
        "maximum_distance_ft": 26400,
        "map_alert_window_hours": 12,
        "map_alert_window_max_hours": 168,
        "test_notification_isolated": True,
        "test_notification_endpoint": "/api/watchlist/test-notification",
        "global_alert_search": "/alerts?window=all",
    }


@app.get("/api/watchlist/health")
def watchlist_health():
    return _json_safe(_watch_health())


@app.get("/api/watch-locations/search")
@_friendly_watch_api_errors
def watch_location_search(q: str = ""):
    needle = q.strip()
    if len(needle) < 2:
        return {"items": []}
    like = f"%{needle}%"
    items: list[dict] = []
    items.extend(
        query_all(
            """
            SELECT 'ADDRESS' AS kind,objectid::text AS source_id,fulladdr AS label,
                   concat_ws(' · ',coalesce(nullif(inc_muni,''),post_comm),post_code) AS detail,
                   'Address' AS kind_label
            FROM gis_addresses
            WHERE geom IS NOT NULL AND coalesce(status,'A')='A' AND fulladdr ILIKE %s
            ORDER BY fulladdr
            LIMIT 8
            """,
            (like,),
        )
    )
    items.extend(
        query_all(
            """
            SELECT 'PARCEL' AS kind,objectid::text AS source_id,
                   coalesce(nullif(prop_loc,''),'Block '||coalesce(pclblock,'?')||' Lot '||coalesce(pcllot,'?')) AS label,
                   concat_ws(' · ',mun_name,'Block '||coalesce(pclblock,'?'),'Lot '||coalesce(pcllot,'?'),nullif(pams_pin,'')) AS detail,
                   'Parcel' AS kind_label
            FROM gis_parcels
            WHERE geom IS NOT NULL
              AND (prop_loc ILIKE %s OR pams_pin ILIKE %s OR pclblock ILIKE %s OR pcllot ILIKE %s)
            ORDER BY prop_loc NULLS LAST,objectid
            LIMIT 8
            """,
            (like, like, like, like),
        )
    )
    items.extend(
        query_all(
            """
            SELECT 'REFERENCE' AS kind,entity_id::text AS source_id,canonical_name AS label,
                   concat_ws(' · ',
                     CASE WHEN entity_type='CORRIDOR' THEN 'Street / corridor' ELSE initcap(replace(entity_type,'_',' ')) END,
                     municipality,state) AS detail,
                   CASE WHEN entity_type='CORRIDOR' THEN 'Street / corridor' ELSE 'Saved place or area' END AS kind_label
            FROM spatial_reference_entities
            WHERE active=true AND geom IS NOT NULL
              AND (canonical_name ILIKE %s OR coalesce(normalized_address,'') ILIKE %s
                   OR EXISTS (SELECT 1 FROM unnest(aliases) alias WHERE alias ILIKE %s))
            ORDER BY importance_tier,canonical_name
            LIMIT 8
            """,
            (like, like, like),
        )
    )
    items.extend(
        query_all(
            """
            SELECT 'CUSTOM_FEATURE' AS kind,f.id::text AS source_id,
                   coalesce(nullif(f.name,''),l.name) AS label,l.name AS detail,
                   'Drawn or imported area' AS kind_label
            FROM map_features f
            JOIN map_layers l ON l.id=f.layer_id
            WHERE f.active=true AND l.active=true AND f.geom IS NOT NULL
              AND (f.name ILIKE %s OR f.properties::text ILIKE %s)
            ORDER BY l.name,f.name NULLS LAST
            LIMIT 6
            """,
            (like, like),
        )
    )
    items.extend(
        query_all(
            """
            SELECT 'MUNICIPALITY' AS kind,label AS source_id,label,
                   'All alerts labeled for this municipality' AS detail,
                   'Municipality' AS kind_label
            FROM (
              SELECT coalesce(nullif(inc_muni,''),nullif(post_comm,'')) AS label,count(*) AS total
              FROM gis_addresses
              WHERE geom IS NOT NULL
                AND coalesce(nullif(inc_muni,''),nullif(post_comm,'')) ILIKE %s
              GROUP BY coalesce(nullif(inc_muni,''),nullif(post_comm,''))
            ) towns
            WHERE label IS NOT NULL
            ORDER BY total DESC,label
            LIMIT 6
            """,
            (like,),
        )
    )
    return JSONResponse(_json_safe({"items": items[:30]}))


@app.post("/api/watch-locations/resolve")
@_friendly_watch_api_errors
def watch_location_resolve(
    location_query: str = Form(...),
    municipality: str = Form(""),
):
    with db_conn() as conn:
        resolved = _resolve_target(conn, location_query, "", "", municipality)
    return JSONResponse(
        {
            "ok": True,
            "location": {
                "kind": "RESOLVED_ADDRESS",
                "source_id": "",
                "label": resolved.get("label") or location_query.strip(),
                "detail": "Verified by the local address resolver",
                "kind_label": "Address",
                "municipality": resolved.get("municipality"),
                "latitude": resolved.get("latitude"),
                "longitude": resolved.get("longitude"),
            },
        }
    )


@app.get("/watchlist", response_class=HTMLResponse)
@_friendly_watch_page_errors
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
    location_kind: str = "",
    location_id: str = "",
):
    where = []
    params = []
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "(display_name ILIKE %s OR search_term ILIKE %s OR watch_id ILIKE %s "
            "OR municipality ILIKE %s OR address ILIKE %s)"
        )
        params.extend([needle] * 5)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    all_items = query_all(
        f"""
        SELECT w.id,w.watch_id,w.active,w.watch_type,w.display_name,w.search_term,w.aliases,
               w.match_mode,w.match_field,w.category,w.subcategory,w.tags,w.min_priority,
               w.address,w.municipality,w.county,w.state,w.block,w.lot,w.notes,
               w.source_filter,w.alert_category_filter,w.starts_at,w.expires_at,w.nearby_enabled,
               w.radius_ft,w.latitude,w.longitude,w.spatial_scope,w.spatial_reference_entity_id,
               ST_GeometryType(w.spatial_target_geom) AS spatial_target_type,
               COALESCE((SELECT count(*)
                           FROM watch_item_recipients wir
                           JOIN subscribers s ON s.id=wir.subscriber_id
                          WHERE wir.watch_item_id=w.id AND wir.active AND s.active),0) AS active_recipient_count,
               COALESCE((SELECT array_agg(wir.subscriber_id::text ORDER BY wir.subscriber_id)
                           FROM watch_item_recipients wir
                           JOIN subscribers s ON s.id=wir.subscriber_id
                          WHERE wir.watch_item_id=w.id AND wir.active AND s.active),ARRAY[]::text[]) AS recipient_ids,
               COALESCE((SELECT array_agg(s.name ORDER BY s.name)
                           FROM watch_item_recipients wir
                           JOIN subscribers s ON s.id=wir.subscriber_id
                          WHERE wir.watch_item_id=w.id AND wir.active AND s.active),ARRAY[]::text[]) AS recipient_names,
               (SELECT count(*) FROM alert_watch_matches awm
                 WHERE awm.watch_item_id=w.id
                   AND awm.matched_at>=now()-interval '7 days') AS matches_7d,
               (SELECT count(*) FROM alert_watch_matches awm
                 WHERE awm.watch_item_id=w.id) AS matches_total,
               (SELECT max(awm.matched_at) FROM alert_watch_matches awm
                 WHERE awm.watch_item_id=w.id) AS last_match_at,
               (SELECT a.title FROM alert_watch_matches awm
                  JOIN alerts a ON a.id=awm.alert_id
                 WHERE awm.watch_item_id=w.id
                 ORDER BY awm.matched_at DESC LIMIT 1) AS last_match_title,
               (SELECT count(*) FROM deliveries d
                 WHERE d.status='SENT'
                   AND d.created_at>=now()-interval '7 days'
                   AND d.matched_watch_ids ? w.watch_id) AS sent_7d,
               (SELECT d.status FROM deliveries d
                 WHERE d.matched_watch_ids ? w.watch_id
                 ORDER BY COALESCE(d.sent_at,d.attempted_at,d.created_at) DESC LIMIT 1) AS last_delivery_status,
               (SELECT COALESCE(d.sent_at,d.attempted_at,d.created_at) FROM deliveries d
                 WHERE d.matched_watch_ids ? w.watch_id
                 ORDER BY COALESCE(d.sent_at,d.attempted_at,d.created_at) DESC LIMIT 1) AS last_delivery_at,
               (SELECT left(d.error_message,240) FROM deliveries d
                 WHERE d.matched_watch_ids ? w.watch_id
                 ORDER BY COALESCE(d.sent_at,d.attempted_at,d.created_at) DESC LIMIT 1) AS last_delivery_error
        FROM watch_items w
        {clause}
        ORDER BY w.active DESC,w.display_name
        LIMIT 500
        """,
        params,
    )
    for row in all_items:
        row["state_label"], row["state_class"], row["state_reason"] = _watch_state(row)
        row["starts_local"] = _local_value(row.get("starts_at"))
        row["expires_local"] = _local_value(row.get("expires_at"))
        row["duration"] = "CUSTOM" if row.get("expires_at") else "PERMANENT"
        row["recipient_ids"] = set(row.get("recipient_ids") or [])
        row["setup_mode"] = (
            "LOCATION_TOPIC"
            if row.get("watch_type") == "LOCATION_TOPIC"
            else "LOCATION"
            if row.get("nearby_enabled") or row.get("watch_type") == "TOWN"
            else "TOPIC"
        )
        row["location_label"] = (
            ""
            if row["setup_mode"] == "TOPIC"
            else row.get("address") or row.get("municipality") or "Saved map Location"
        )
        row["distance_label"] = _distance_label(row.get("radius_ft"))
        row["location_kind"] = (
            "TYPED_ADDRESS"
            if row["setup_mode"] == "TOPIC"
            else "REFERENCE"
            if row.get("spatial_reference_entity_id")
            else "MUNICIPALITY"
            if row.get("municipality") and not row.get("spatial_target_type")
            else "EXISTING"
        )
        row["location_id"] = str(
            row.get("spatial_reference_entity_id")
            or (row.get("municipality") if row["location_kind"] == "MUNICIPALITY" else "")
        )

    state_aliases = {
        "inactive": "paused",
        "scheduled": "watching",
        "spatial": "location",
        "unrouted": "needs_recipient",
        "failed": "delivery_problem",
    }
    normalized_state = state_aliases.get(state, state)
    if normalized_state == "location":
        items = [row for row in all_items if row["setup_mode"] in {"LOCATION", "LOCATION_TOPIC"}]
    elif normalized_state == "active":
        now = datetime.now(timezone.utc)
        items = [
            row
            for row in all_items
            if row.get("active")
            and (not row.get("starts_at") or row["starts_at"] <= now)
            and (not row.get("expires_at") or row["expires_at"] > now)
        ]
    elif normalized_state != "all":
        label = normalized_state.replace("_", " ").title()
        items = [row for row in all_items if row["state_label"] == label]
    else:
        items = all_items

    status_counts = {
        "total": len(all_items),
        "watching": sum(row["state_label"] == "Watching" for row in all_items),
        "paused": sum(row["state_label"] == "Paused" for row in all_items),
        "expired": sum(row["state_label"] == "Expired" for row in all_items),
        "needs_recipient": sum(row["state_label"] == "Needs Recipient" for row in all_items),
        "matching": sum(row["state_label"] == "Matching" for row in all_items),
        "delivery_problem": sum(row["state_label"] == "Delivery Problem" for row in all_items),
    }
    subscribers = query_all(
        "SELECT id::text AS id,name FROM subscribers WHERE active=true ORDER BY name"
    )
    alert_sources = query_all(
        "SELECT source,count(*) AS total FROM alerts WHERE nullif(trim(source),'') IS NOT NULL GROUP BY source ORDER BY source"
    )
    alert_categories = query_all(
        "SELECT category,count(*) AS total FROM alerts WHERE nullif(trim(category),'') IS NOT NULL GROUP BY category ORDER BY category"
    )
    health = _watch_health()
    needs_recipient_watches = [row for row in all_items if row["state_label"] == "Needs Recipient"]
    return templates.TemplateResponse(
        request=request,
        name="watchlist.html",
        context={
            "items": items,
            "counts": status_counts,
            "subscribers": subscribers,
            "q": q,
            "state": state,
            "msg": msg,
            "error": error,
            "health": health,
            "watch_types": SPATIAL_WATCH_TYPES,
            "match_modes": sorted(MATCH_MODES),
            "spatial_durations": list(SPATIAL_DURATIONS),
            "alert_sources": alert_sources,
            "alert_categories": alert_categories,
            "needs_recipient_watches": needs_recipient_watches,
            "prefill": {
                "display_name": display_name,
                "location_query": location_query,
                "latitude": latitude,
                "longitude": longitude,
                "location_kind": location_kind or ("MAP_POINT" if latitude and longitude else "TYPED_ADDRESS"),
                "location_id": location_id,
            },
        },
    )


def _save_recipients(cur, watch_item_id: uuid.UUID, subscriber_ids: list[uuid.UUID]):
    cur.execute("UPDATE watch_item_recipients SET active=false WHERE watch_item_id=%s", (watch_item_id,))
    for subscriber_id in subscriber_ids:
        cur.execute("SELECT id FROM subscribers WHERE id=%s AND active=true", (subscriber_id,))
        if not cur.fetchone():
            raise HTTPException(400, "One or more selected Recipients are paused or no longer available")
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
    setup_mode: str = Form("LOCATION"),
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
    location_kind: str = Form("TYPED_ADDRESS"),
    location_id: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    radius_ft: float = Form(5280.0),
    duration: str = Form("PERMANENT"),
    starts_at: str = Form(""),
    expires_at: str = Form(""),
    subscriber_ids: list[uuid.UUID] = Form([]),
):
    display_name = display_name.strip()
    if not display_name:
        raise HTTPException(400, "Name this watch before turning it on")
    setup_mode = _setup_mode(setup_mode)
    location_required = setup_mode in {"LOCATION", "LOCATION_TOPIC"} or spatial_enabled is not None
    topic_required = setup_mode in {"TOPIC", "LOCATION_TOPIC"}
    topic = search_term.strip()
    if topic_required and not topic:
        raise HTTPException(400, "Enter the topic, phrase, organization, or incident wording to watch for")
    match_mode = match_mode.upper().strip() or "CONTAINS"
    validate_watch(match_mode, match_field, min_priority)
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    start, end = _schedule(duration, starts_at, expires_at)
    watch_uuid = uuid.uuid4()
    with db_conn() as conn:
        target = None
        if location_required:
            target = _selected_location(
                conn,
                kind=location_kind,
                source_id=location_id,
                location_query=location_query or address,
                latitude=latitude,
                longitude=longitude,
                municipality=municipality,
            )
        spatial_requested = bool(target and target.get("spatial"))
        if setup_mode == "LOCATION_TOPIC":
            saved_watch_type = "LOCATION_TOPIC"
            saved_search_term = topic
            saved_match_mode = match_mode
            saved_match_field = match_field.strip() or None
        elif setup_mode == "LOCATION":
            saved_watch_type = (target or {}).get("watch_type") or watch_type.strip().upper() or "ADDRESS"
            saved_search_term = (target or {}).get("label") or (location_query or address).strip()
            if not saved_search_term:
                raise HTTPException(400, "Choose the Location this watch should monitor")
            if target and target.get("kind") == "MUNICIPALITY":
                saved_match_mode = "FIELD"
                saved_match_field = "municipality"
            else:
                saved_match_mode = "CONTAINS"
                saved_match_field = None
        else:
            saved_watch_type = watch_type.strip().upper() or "PHRASE"
            saved_search_term = topic
            saved_match_mode = match_mode
            saved_match_field = match_field.strip() or None

        saved_address = (target or {}).get("address") or (location_query or address).strip() or None
        saved_municipality = (target or {}).get("municipality") or municipality.strip() or None
        target_wkt = (target or {}).get("target_wkt")
        reference_id = (target or {}).get("spatial_reference_entity_id")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watch_items(
                  id,watch_id,active,watch_type,display_name,search_term,aliases,match_mode,match_field,
                  category,tags,min_priority,address,municipality,county,state,block,lot,parcel_id,
                  notes,source_notes,source_filter,alert_category_filter,
                  starts_at,expires_at,gis_enabled,nearby_enabled,radius_ft,spatial_scope,
                  spatial_reference_entity_id,spatial_target_geom
                ) VALUES(
                  %s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                  %s,%s,%s,%s,%s,%s,%s,%s,%s,'RADIUS',%s,
                  CASE WHEN %s::text IS NULL THEN NULL ELSE ST_GeomFromEWKT(%s::text) END
                )
                """,
                (
                    watch_uuid,
                    make_watch_id(display_name),
                    saved_watch_type,
                    display_name,
                    saved_search_term,
                    csv_array(aliases),
                    saved_match_mode,
                    saved_match_field,
                    category.strip() or None,
                    csv_array(tags),
                    min_priority,
                    saved_address,
                    saved_municipality,
                    (target or {}).get("county"),
                    (target or {}).get("state") or ("NJ" if target else None),
                    (target or {}).get("block"),
                    (target or {}).get("lot"),
                    (target or {}).get("parcel_id"),
                    notes.strip() or None,
                    f"watch_setup:{(target or {}).get('kind', 'TOPIC')}",
                    csv_array(source_filter),
                    csv_array(alert_category_filter),
                    start,
                    end,
                    spatial_requested,
                    spatial_requested,
                    radius_ft,
                    reference_id,
                    target_wkt,
                    target_wkt,
                ),
            )
            _save_recipients(cur, watch_uuid, subscriber_ids)
        conn.commit()
    message = "Watch is on"
    if not subscriber_ids:
        message = "Watch is on, but it needs a Recipient before it can send Notifications."
    return _watch_redirect(message=message)


@app.post("/watchlist/{item_id}/update")
@_friendly_watch_errors
def spatial_watch_update(
    item_id: uuid.UUID,
    display_name: str = Form(...),
    setup_mode: str = Form(""),
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
    location_kind: str = Form("EXISTING"),
    location_id: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    radius_ft: float = Form(5280.0),
    duration: str = Form("PERMANENT"),
    starts_at: str = Form(""),
    expires_at: str = Form(""),
    active: str | None = Form(None),
    keep_state: str = Form(""),
    subscriber_ids: list[uuid.UUID] = Form([]),
):
    display_name = display_name.strip()
    if not display_name:
        raise HTTPException(400, "Name this watch before saving it")
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    start, end = _schedule(duration, starts_at, expires_at)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,active,watch_type,display_name,search_term,match_mode,match_field,
                       address,municipality,county,state,block,lot,parcel_id,
                       nearby_enabled,radius_ft,spatial_reference_entity_id,
                       ST_AsEWKT(spatial_target_geom) AS target_wkt
                FROM watch_items WHERE id=%s FOR UPDATE
                """,
                (item_id,),
            )
            current = cur.fetchone()
            if not current:
                raise HTTPException(404, "That watch no longer exists")

        if setup_mode.strip():
            saved_setup_mode = _setup_mode(setup_mode)
        elif str(current.get("watch_type") or "").upper() == "LOCATION_TOPIC":
            saved_setup_mode = "LOCATION_TOPIC"
        elif current.get("nearby_enabled") or str(current.get("watch_type") or "").upper() == "TOWN":
            saved_setup_mode = "LOCATION"
        else:
            saved_setup_mode = "TOPIC"

        target = None
        if saved_setup_mode in {"LOCATION", "LOCATION_TOPIC"} or spatial_enabled is not None:
            target = _selected_location(
                conn,
                kind=location_kind,
                source_id=location_id,
                location_query=location_query or address,
                latitude=latitude,
                longitude=longitude,
                municipality=municipality,
                current=current,
            )
        topic = search_term.strip()
        if saved_setup_mode in {"TOPIC", "LOCATION_TOPIC"} and not topic:
            raise HTTPException(400, "Enter the topic, phrase, organization, or incident wording to watch for")

        match_mode = match_mode.upper().strip() or "CONTAINS"
        spatial_requested = bool(target and target.get("spatial"))
        if saved_setup_mode == "LOCATION_TOPIC":
            saved_watch_type = "LOCATION_TOPIC"
            saved_search_term = topic
            saved_match_mode = match_mode
            saved_match_field = match_field.strip() or None
        elif saved_setup_mode == "LOCATION":
            saved_watch_type = (target or {}).get("watch_type") or watch_type.strip().upper() or "ADDRESS"
            saved_search_term = (target or {}).get("label") or (location_query or address).strip()
            if not saved_search_term:
                raise HTTPException(400, "Choose the Location this watch should monitor")
            if target and target.get("kind") == "MUNICIPALITY":
                saved_match_mode = "FIELD"
                saved_match_field = "municipality"
            else:
                saved_match_mode = "CONTAINS"
                saved_match_field = None
        else:
            saved_watch_type = watch_type.strip().upper() or (
                current.get("watch_type") if current.get("watch_type") != "LOCATION_TOPIC" else "PHRASE"
            ) or "PHRASE"
            saved_search_term = topic
            saved_match_mode = match_mode
            saved_match_field = match_field.strip() or None
        validate_watch(saved_match_mode, saved_match_field, min_priority)

        saved_address = (
            (target or {}).get("address")
            or ((location_query or address).strip() if target else None)
            or current.get("address")
        )
        saved_municipality = (
            (target or {}).get("municipality")
            or (municipality.strip() if target else None)
            or current.get("municipality")
        )
        replace_target = bool(target and target.get("replace_target"))
        target_wkt = (target or {}).get("target_wkt")
        reference_id = (
            (target or {}).get("spatial_reference_entity_id")
            if replace_target
            else current.get("spatial_reference_entity_id")
        )
        active_value = current.get("active") if keep_state else active is not None
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE watch_items SET
                  active=%s,watch_type=%s,display_name=%s,search_term=%s,aliases=%s,
                  match_mode=%s,match_field=%s,category=%s,tags=%s,min_priority=%s,address=%s,
                  municipality=%s,county=%s,state=%s,block=%s,lot=%s,parcel_id=%s,
                  notes=%s,source_notes=%s,source_filter=%s,alert_category_filter=%s,
                  starts_at=%s,expires_at=%s,gis_enabled=%s,nearby_enabled=%s,radius_ft=%s,
                  spatial_scope='RADIUS',
                  spatial_reference_entity_id=CASE WHEN %s THEN %s ELSE spatial_reference_entity_id END,
                  spatial_target_geom=CASE
                    WHEN NOT %s THEN spatial_target_geom
                    WHEN %s::text IS NULL THEN NULL
                    ELSE ST_GeomFromEWKT(%s::text) END,
                  updated_at=now()
                WHERE id=%s
                """,
                (
                    active_value, saved_watch_type, display_name, saved_search_term,
                    csv_array(aliases), saved_match_mode, saved_match_field, category.strip() or None,
                    csv_array(tags), min_priority, saved_address or None, saved_municipality or None,
                    (target or {}).get("county") or current.get("county"),
                    (target or {}).get("state") or current.get("state"),
                    (target or {}).get("block") or current.get("block"),
                    (target or {}).get("lot") or current.get("lot"),
                    (target or {}).get("parcel_id") or current.get("parcel_id"),
                    notes.strip() or None,
                    f"watch_setup:{(target or {}).get('kind', saved_setup_mode)}",
                    csv_array(source_filter), csv_array(alert_category_filter), start, end,
                    spatial_requested, spatial_requested, radius_ft,
                    replace_target, reference_id, replace_target, target_wkt, target_wkt,
                    item_id,
                ),
            )
            _save_recipients(cur, item_id, subscriber_ids)
        conn.commit()
    message = "Watch updated"
    if not subscriber_ids:
        message = "Watch updated. It needs a Recipient before it can send Notifications."
    return _watch_redirect(message=message)


@app.post("/watchlist/{item_id}/toggle")
@_friendly_watch_errors
def spatial_watch_toggle(item_id: uuid.UUID, action: str = Form("")):
    requested = action.strip().lower()
    if requested not in {"", "pause", "activate", "reactivate"}:
        raise HTTPException(400, "Choose Pause or Reactivate")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT active,expires_at FROM watch_items WHERE id=%s FOR UPDATE",
                (item_id,),
            )
            current = cur.fetchone()
            if not current:
                raise HTTPException(404, "That watch no longer exists")
            if requested == "pause":
                next_active = False
            elif requested in {"activate", "reactivate"}:
                next_active = True
            else:
                next_active = not current["active"]
            clear_expired = bool(
                next_active
                and current.get("expires_at")
                and current["expires_at"] <= datetime.now(timezone.utc)
            )
            cur.execute(
                """
                UPDATE watch_items
                SET active=%s,
                    starts_at=CASE WHEN %s THEN NULL ELSE starts_at END,
                    expires_at=CASE WHEN %s THEN NULL ELSE expires_at END,
                    updated_at=now()
                WHERE id=%s
                """,
                (next_active, clear_expired, clear_expired, item_id),
            )
        conn.commit()
    return _watch_redirect(message="Watch is on" if next_active else "Watch paused")


@app.post("/watchlist/{item_id}/delete")
@_friendly_watch_errors
def spatial_watch_delete(item_id: uuid.UUID, confirm_delete: str = Form("")):
    if confirm_delete.strip().upper() != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT display_name FROM watch_items WHERE id=%s FOR UPDATE", (item_id,))
            if not cur.fetchone():
                raise HTTPException(404, "That watch no longer exists")
            cur.execute("DELETE FROM watch_items WHERE id=%s", (item_id,))
        conn.commit()
    return _watch_redirect(message="Watch deleted. Stored alerts and delivery history were kept.")


@app.post("/api/watchlist/test-notification")
@_friendly_watch_api_errors
def watchlist_test_notification(recipient_id: uuid.UUID = Form(...)):
    recipient = query_one(
        "SELECT name,ntfy_topic FROM subscribers WHERE id=%s AND active=true",
        (recipient_id,),
    )
    if not recipient:
        raise HTTPException(404, "Choose an active Recipient")
    payload = {
        "topic": recipient["ntfy_topic"],
        "title": "City Manager OS notification test",
        "message": "TEST ONLY: your selected Recipient can receive City Manager OS Notifications. No alert, Match, or Watch was created.",
        "priority": 3,
        "tags": ["test_tube", "white_check_mark"],
    }
    request = urllib_request.Request(
        f"{NTFY_PUBLISH_BASE}/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8") or "{}")
    except (urllib_error.HTTPError, urllib_error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        LOGGER.warning("Test Notification delivery failed type=%s", type(exc).__name__)
        raise HTTPException(
            502,
            "The test Notification could not be delivered. No real alert or Match was created.",
        ) from exc
    return JSONResponse(
        {
            "ok": True,
            "message": f"Test Notification sent to {recipient['name']}.",
            "receipt": str(result.get("id") or "accepted"),
            "isolated": True,
        }
    )


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
