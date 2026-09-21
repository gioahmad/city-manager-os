"""Unified spatial matching and browser controls for the existing Watchlist."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from schedule_app import app
from operations_app import (
    ALERT_FILTERED_BULK_LIMIT,
    SEARCH_SCOPES,
    alert_keyword_choices,
    require_watch_recipients,
)
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
COMPLETION_RELEASE_ID = "alert-watch-completion-performance-v1"
GEOMETRY_RELEASE_ID = "spatial-watch-effective-geometry-v1"
WATCH_LAB_RELEASE_ID = "watch-lab-read-only-v1"
LOCAL_ZONE = ZoneInfo(os.getenv("APP_TIMEZONE") or os.getenv("TZ") or "America/New_York")
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
BULK_WATCH_LIMIT = 250
BULK_WATCH_ACTION_LIMIT = 500
BULK_NO_PROPERTY = "__none__"
BULK_EACH_FEATURE = "__feature__"
BULK_STORED_NAME = "__stored_name__"
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


def _watch_type_for_geometry(geometry_type: str | None) -> str:
    value = str(geometry_type or "").upper()
    if "LINESTRING" in value:
        return "CORRIDOR"
    if "POLYGON" in value:
        return "AREA"
    return "POINT" if "POINT" in value else "AREA"


def _property_value(properties: dict | None, *names: str) -> str:
    lowered = {str(key).casefold(): value for key, value in (properties or {}).items()}
    for name in names:
        value = lowered.get(name.casefold())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


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
                       f.name AS feature_name,l.name AS layer_name,
                       f.properties,ST_GeometryType(f.geom) AS geometry_type,
                       ST_AsEWKT(f.geom) AS target_wkt
                FROM map_features f
                JOIN map_layers l ON l.id=f.layer_id
                WHERE f.id=%s AND f.active=true AND l.active=true AND f.geom IS NOT NULL
                """,
                (feature_id,),
            )
            row = cur.fetchone()
            watch_type = _watch_type_for_geometry(row.get("geometry_type") if row else "")
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
    if kind == "CUSTOM_FEATURE":
        properties = result.pop("properties", {}) or {}
        result["label"] = str(result.pop("feature_name", "") or "").strip() or _property_value(
            properties,
            "name", "title", "label", "municipality", "mun_name", "munname",
            "city", "town", "county", "route_name", "route", "road_name", "road",
        ) or result.get("label") or result.pop("layer_name", "Map Location")
        result.pop("layer_name", None)
        result["address"] = _property_value(properties, "address", "fulladdr") or None
        result["municipality"] = _property_value(
            properties, "municipality", "mun_name", "munname", "city", "town"
        ) or None
        result["county"] = _property_value(
            properties, "county", "county_name", "countyname", "cnty_name", "cntyname"
        ) or None
        result["state"] = _property_value(properties, "state", "state_name", "st") or "NJ"
        result["gis_lookup"] = f"map_feature:{source_id}"
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


def _watch_prefill_from_alert(alert_reference: str) -> dict:
    """Build an editable Watch draft from one existing alert without writing data."""
    alert_reference = alert_reference.strip()[:160]
    if not alert_reference:
        return {}
    alert = query_one(
        """
        SELECT a.alert_id,a.title,a.message,a.source,a.category,a.subtype,a.tags,a.municipality,
               coalesce(
                 nullif(a.location->>'label',''),
                 nullif(a.location->>'address',''),
                 nullif(r.resolved_label,''),
                 nullif(a.municipality,'')
               ) AS location_label,
               CASE WHEN coalesce(a.geom,r.geom) IS NOT NULL
                    THEN ST_Y(ST_PointOnSurface(coalesce(a.geom,r.geom))) END AS latitude,
               CASE WHEN coalesce(a.geom,r.geom) IS NOT NULL
                    THEN ST_X(ST_PointOnSurface(coalesce(a.geom,r.geom))) END AS longitude
        FROM alerts a
        LEFT JOIN geo_entity_resolutions r
          ON r.entity_type='ALERT' AND r.entity_id=a.id::text AND r.status='RESOLVED'
        WHERE a.alert_id=%s
        ORDER BY a.received_at DESC,a.id DESC
        LIMIT 1
        """,
        (alert_reference,),
    )
    if not alert:
        return {}

    topic = str(
        alert.get("title")
        or alert.get("subtype")
        or alert.get("category")
        or "Alert"
    ).strip()
    location_label = str(alert.get("location_label") or "").strip()
    municipality = str(alert.get("municipality") or "").strip()
    latitude = alert.get("latitude")
    longitude = alert.get("longitude")
    has_coordinates = latitude is not None and longitude is not None
    has_location = bool(location_label or has_coordinates)
    setup_mode = (
        "LOCATION_TOPIC"
        if has_location and topic
        else "LOCATION"
        if has_location
        else "TOPIC"
    )

    keyword_choices = alert_keyword_choices(alert)
    suggested_keyword = keyword_choices[0] if keyword_choices else topic

    if has_coordinates:
        location_kind = "MAP_POINT"
        location_id = ""
        location_label = location_label or "Alert location"
    elif municipality and location_label.casefold() == municipality.casefold():
        location_kind = "MUNICIPALITY"
        location_id = municipality
    else:
        location_kind = "TYPED_ADDRESS"
        location_id = ""

    watch_name = f"{topic[:110].rstrip()} Watch"
    return {
        "from_alert": str(alert.get("alert_id") or alert_reference),
        "source_alert_title": topic,
        "suggested_source": str(alert.get("source") or "").strip(),
        "suggested_category": str(alert.get("category") or "").strip(),
        "suggested_subtype": str(alert.get("subtype") or "").strip(),
        "display_name": watch_name,
        "setup_mode": setup_mode,
        "search_term": suggested_keyword,
        "aliases": "",
        "keyword_choices": keyword_choices,
        "selected_keywords": [suggested_keyword] if suggested_keyword else [],
        "location_query": location_label,
        "latitude": latitude if has_coordinates else "",
        "longitude": longitude if has_coordinates else "",
        "location_kind": location_kind,
        "location_id": location_id,
        "source_filter": "",
        "alert_category_filter": "",
        "notes": f"Started from alert {alert.get('alert_id') or alert_reference}",
    }


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
    if row.get("nearby_enabled") and not row.get("spatial_target_type"):
        return "Delivery Problem", "error", "The saved Location is missing. Edit this watch and choose the Location again."
    if _safe_int(row.get("active_recipient_count")) == 0:
        return "Needs Recipient", "warning", "This watch can record Matches, but no Recipient is selected for Notifications."
    if row.get("starts_at") and row["starts_at"] > now:
        return "Watching", "waiting", "Saved and ready. Matching begins at the scheduled start time."
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
        "completion_release_id": COMPLETION_RELEASE_ID,
        "geometry_release_id": GEOMETRY_RELEASE_ID,
        "watch_lab_release_id": WATCH_LAB_RELEASE_ID,
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
        "watch_lab_endpoint": "/api/watch-lab/evaluate",
        "watch_lab_read_only": True,
        "global_alert_search": "/alerts?window=all",
        "recipient_watch_assignment": "/subscribers/{recipient_id}/watches",
        "unrouted_watch_repair": "/watchlist/repair-unrouted",
        "activation_requires_recipient": True,
        "alert_keyword_choices": "derived from the selected stored alert",
        "alert_watch_prefill_query": ["from_alert", "setup_mode", "selected_keywords"],
        "bulk_watch_endpoint": "/watchlist/bulk-create",
        "bulk_watch_limit": BULK_WATCH_LIMIT,
        "bulk_watch_actions": ["pause", "activate", "delete"],
        "individual_location_selection": True,
        "saved_keyword_switches": True,
        "alert_match_explanations": True,
        "bulk_alert_actions": ["resolve", "delete"],
        "filtered_bulk_alert_limit": ALERT_FILTERED_BULK_LIMIT,
        "county_to_town_selection": True,
        "visible_area_search": {
            "alerts": "/map/system/alerts.geojson",
            "events": "/map/system/events.geojson",
            "work_items": "/map/system/issues.geojson",
            "watches": "/map/system/watchlist.geojson",
        },
        "global_search_scopes": list(SEARCH_SCOPES),
        "reusable_location_source": "Mapping Center map_layers and map_features",
    }


@app.get("/api/watchlist/health")
def watchlist_health():
    return _json_safe(_watch_health())


@app.post("/api/watch-lab/evaluate")
@_friendly_watch_api_errors
def watch_lab_evaluate(
    watch_item_id: uuid.UUID = Form(...),
    alert_id: str = Form(...),
    point_mode: str = Form("ALERT"),
    latitude: str = Form(""),
    longitude: str = Form(""),
):
    """Load one real Alert and Watch for a read-only run of the shared matcher."""
    alert_reference = alert_id.strip()[:160]
    if not alert_reference:
        raise HTTPException(400, "Enter an alert ID")
    point_mode = point_mode.strip().upper() or "ALERT"
    if point_mode not in {"ALERT", "WATCH_CENTER", "CUSTOM"}:
        raise HTTPException(400, "Choose the saved alert point, Watch center, or custom point")
    lat = _float_or_none(latitude)
    lon = _float_or_none(longitude)
    if point_mode == "CUSTOM":
        if lat is None or lon is None:
            raise HTTPException(400, "Custom point requires both latitude and longitude")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(400, "Latitude or longitude is outside its valid range")
    else:
        lat = lon = None

    row = query_one(
        """
        WITH selected_watch AS (
          SELECT w.*
          FROM watch_items w
          WHERE w.id=%s
        ), selected_alert AS (
          SELECT a.*,
                 r.status AS resolver_status,
                 r.match_type AS resolver_match_type,
                 r.confidence AS resolver_confidence,
                 r.spatial_precision AS resolver_spatial_precision,
                 r.resolved_label,
                 r.geom AS resolver_geom
          FROM alerts a
          LEFT JOIN geo_entity_resolutions r
            ON r.entity_type='ALERT' AND r.entity_id=a.id::text
          WHERE a.alert_id=%s
          LIMIT 1
        ), prepared AS (
          SELECT w.*,a.id AS alert_uuid,a.alert_id,a.source,a.category AS alert_category,a.subtype,a.status,
                 a.event_action,a.title,a.message,a.priority,a.county AS alert_county,
                 a.municipality AS alert_municipality,a.location,a.tags AS alert_tags,a.click_url,
                 a.source_url,a.received_at,a.geom AS stored_alert_geom,
                 a.resolver_status,a.resolver_match_type,a.resolver_confidence,
                 a.resolver_spatial_precision,a.resolved_label,a.resolver_geom,
                 CASE
                   WHEN %s='WATCH_CENTER' AND coalesce(w.spatial_target_geom,w.geom) IS NOT NULL
                     THEN ST_PointOnSurface(coalesce(w.spatial_target_geom,w.geom))
                   WHEN %s='CUSTOM'
                     THEN ST_SetSRID(ST_MakePoint(%s::double precision,%s::double precision),4326)
                   ELSE NULL::geometry
                 END::geometry(Point,4326) AS supplied_geom
          FROM selected_watch w CROSS JOIN selected_alert a
        ), effective AS (
          SELECT p.*,
                 CASE
                   WHEN p.stored_alert_geom IS NOT NULL THEN p.stored_alert_geom
                   WHEN p.supplied_geom IS NOT NULL THEN p.supplied_geom
                   WHEN p.resolver_status='RESOLVED' THEN p.resolver_geom
                   ELSE NULL::geometry
                 END::geometry(Point,4326) AS effective_alert_geom,
                 CASE
                   WHEN p.stored_alert_geom IS NOT NULL THEN 'stored alert point'
                   WHEN p.supplied_geom IS NOT NULL THEN 'supplied alert point'
                   WHEN p.resolver_status='RESOLVED' AND p.resolver_geom IS NOT NULL
                     THEN concat(
                       'resolver point',
                       CASE WHEN nullif(p.resolver_spatial_precision,'') IS NULL THEN ''
                            ELSE ' ('||p.resolver_spatial_precision||')' END
                     )
                   ELSE NULL
                 END AS effective_geometry_source
          FROM prepared p
        )
        SELECT
          e.alert_uuid::text,e.alert_id,e.source,e.alert_category AS category,e.subtype,e.status,e.event_action,
          e.title,e.message,e.priority,e.alert_county,e.alert_municipality,e.location,e.alert_tags AS tags,
          e.click_url,e.source_url,e.received_at,e.resolver_status,e.resolver_match_type,
          e.resolver_confidence,e.resolver_spatial_precision,e.resolved_label,
          ST_X(e.effective_alert_geom) AS effective_longitude,
          ST_Y(e.effective_alert_geom) AS effective_latitude,
          e.effective_geometry_source,
          e.stored_alert_geom IS NOT NULL AND e.supplied_geom IS NOT NULL AS supplied_point_ignored,
          e.id::text AS watch_item_uuid,e.watch_id,e.active,e.watch_type,e.display_name,
          e.search_term,e.aliases,e.match_mode,e.match_field,e.min_priority,e.address,
          e.municipality,e.source_filter,e.alert_category_filter,e.starts_at,e.expires_at,
          e.nearby_enabled,e.radius_ft,e.spatial_scope,
          e.effective_alert_geom IS NOT NULL AS alert_geometry_ready,
          coalesce(e.spatial_target_geom,e.geom) IS NOT NULL AND e.spatial_geom IS NOT NULL
            AS watch_target_ready,
          sm.match_type AS spatial_match_type,sm.match_reason AS spatial_match_reason,
          sm.distance_ft AS spatial_distance_ft,
          CASE WHEN e.effective_alert_geom IS NOT NULL
                     AND coalesce(e.spatial_target_geom,e.geom) IS NOT NULL
               THEN ST_Distance(
                 e.effective_alert_geom::geography,
                 coalesce(e.spatial_target_geom,e.geom)::geography
               )/0.3048 END AS distance_ft,
          CASE
            WHEN e.effective_alert_geom IS NULL OR e.spatial_geom IS NULL THEN false
            WHEN upper(coalesce(e.spatial_scope,'RADIUS'))='RADIUS'
              THEN ST_DWithin(
                e.effective_alert_geom::geography,
                coalesce(e.spatial_target_geom,e.geom)::geography,
                e.radius_ft*0.3048
              )
            ELSE ST_Intersects(e.effective_alert_geom,e.spatial_geom)
          END AS point_inside_watch,
          coalesce((
            SELECT jsonb_agg(jsonb_build_object(
              'subscriber_uuid',s.id::text,
              'subscriber_id',s.subscriber_id,
              'name',s.name,
              'ntfy_topic',s.ntfy_topic
            ) ORDER BY s.name)
            FROM watch_item_recipients wir
            JOIN subscribers s ON s.id=wir.subscriber_id AND s.active
            WHERE wir.watch_item_id=e.id AND wir.active
          ),'[]'::jsonb) AS recipients,
          EXISTS (
            SELECT 1 FROM alert_watch_matches awm
            WHERE awm.alert_id=e.alert_uuid AND awm.watch_item_id=e.id
          ) AS persisted_match,
          (
            SELECT awm.match_reason FROM alert_watch_matches awm
            WHERE awm.alert_id=e.alert_uuid AND awm.watch_item_id=e.id
            ORDER BY awm.matched_at DESC LIMIT 1
          ) AS persisted_match_reason
        FROM effective e
        LEFT JOIN LATERAL gis_active_spatial_watch_matches(e.alert_id,e.supplied_geom) sm
          ON sm.watch_item_id=e.id
        """,
        (watch_item_id, alert_reference, point_mode, point_mode, lon, lat),
    )
    if not row:
        raise HTTPException(404, "That Watch or alert ID was not found")

    alert = {
        key: row.get(key)
        for key in (
            "alert_uuid", "alert_id", "source", "category", "subtype", "status",
            "event_action", "title", "message", "priority", "location", "tags",
            "click_url", "source_url", "received_at",
        )
    }
    alert["county"] = row.get("alert_county")
    alert["municipality"] = row.get("alert_municipality")
    watch = {
        key: row.get(key)
        for key in (
            "watch_item_uuid", "watch_id", "active", "watch_type", "display_name",
            "search_term", "aliases", "match_mode", "match_field", "min_priority",
            "address", "municipality", "source_filter", "alert_category_filter",
            "starts_at", "expires_at", "nearby_enabled", "radius_ft", "spatial_scope",
            "alert_geometry_ready", "watch_target_ready", "spatial_match_type",
            "spatial_match_reason", "spatial_distance_ft", "distance_ft", "recipients",
            "effective_geometry_source", "point_inside_watch",
        )
    }
    return JSONResponse(
        _json_safe(
            {
                "ok": True,
                "read_only": True,
                "matcher_version": "watch-matcher-v2",
                "point_mode": point_mode,
                "alert": alert,
                "watch": watch,
                "evidence": {
                    "point_inside_watch": row.get("point_inside_watch"),
                    "persisted_match": row.get("persisted_match"),
                    "persisted_match_reason": row.get("persisted_match_reason"),
                    "supplied_point_ignored": row.get("supplied_point_ignored"),
                    "resolver_status": row.get("resolver_status"),
                    "resolver_match_type": row.get("resolver_match_type"),
                    "resolver_confidence": row.get("resolver_confidence"),
                    "resolver_spatial_precision": row.get("resolver_spatial_precision"),
                    "resolved_label": row.get("resolved_label"),
                    "effective_latitude": row.get("effective_latitude"),
                    "effective_longitude": row.get("effective_longitude"),
                },
            }
        )
    )


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
                   coalesce(nullif(f.name,''),nullif(inferred.label,''),l.name) AS label,l.name AS detail,
                   'Drawn or imported Location' AS kind_label
            FROM map_features f
            JOIN map_layers l ON l.id=f.layer_id
            LEFT JOIN LATERAL (
              SELECT value AS label
              FROM jsonb_each_text(f.properties)
              WHERE lower(key) IN (
                'name','title','label','municipality','mun_name','munname','city','town',
                'county','county_name','route_name','route','highway','road_name','road'
              ) AND nullif(trim(value),'') IS NOT NULL
              ORDER BY CASE lower(key)
                WHEN 'name' THEN 1 WHEN 'title' THEN 2 WHEN 'label' THEN 3
                WHEN 'municipality' THEN 4 WHEN 'mun_name' THEN 5 WHEN 'munname' THEN 6
                WHEN 'city' THEN 7 WHEN 'town' THEN 8 WHEN 'county' THEN 9
                WHEN 'county_name' THEN 10 WHEN 'route_name' THEN 11 WHEN 'route' THEN 12
                WHEN 'highway' THEN 13 WHEN 'road_name' THEN 14 ELSE 15 END
              LIMIT 1
            ) inferred ON true
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
    from_alert: str = "",
    setup_mode: str = "",
    search_term: str = "",
    selected_keywords: list[str] = Query(default=[]),
    bulk_layer: str = "",
    bulk_parent_by: str = "",
    bulk_group_by: str = "",
    bulk_name_by: str = "",
    bulk_group_filter: str = "",
):
    where = []
    params = []
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "(w.display_name ILIKE %s OR w.search_term ILIKE %s OR w.watch_id ILIKE %s "
            "OR w.municipality ILIKE %s OR w.address ILIKE %s OR w.parent_group ILIKE %s)"
        )
        params.extend([needle] * 6)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    all_items = query_all(
        f"""
        WITH recipient_rollup AS (
          SELECT wir.watch_item_id,
                 count(*) FILTER (WHERE wir.active AND s.active) AS active_recipient_count,
                 array_agg(wir.subscriber_id::text ORDER BY wir.subscriber_id)
                   FILTER (WHERE wir.active AND s.active) AS recipient_ids,
                 array_agg(s.name ORDER BY s.name)
                   FILTER (WHERE wir.active AND s.active) AS recipient_names
          FROM watch_item_recipients wir
          JOIN subscribers s ON s.id=wir.subscriber_id
          GROUP BY wir.watch_item_id
        ), match_rollup AS (
          SELECT awm.watch_item_id,
                 count(*) FILTER (WHERE awm.matched_at>=now()-interval '7 days') AS matches_7d,
                 count(*) AS matches_total,
                 max(awm.matched_at) AS last_match_at,
                 (array_agg(a.title ORDER BY awm.matched_at DESC))[1] AS last_match_title
          FROM alert_watch_matches awm
          JOIN alerts a ON a.id=awm.alert_id
          GROUP BY awm.watch_item_id
        ), delivery_rows AS (
          SELECT ids.watch_id,d.status,d.error_message,d.created_at,
                 coalesce(d.sent_at,d.attempted_at,d.created_at) AS happened_at
          FROM deliveries d
          CROSS JOIN LATERAL jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(d.matched_watch_ids)='array'
                 THEN d.matched_watch_ids ELSE '[]'::jsonb END
          ) ids(watch_id)
        ), delivery_rollup AS (
          SELECT watch_id,
                 count(*) FILTER (
                   WHERE status='SENT' AND created_at>=now()-interval '7 days'
                 ) AS sent_7d,
                 (array_agg(status ORDER BY happened_at DESC))[1] AS last_delivery_status,
                 max(happened_at) AS last_delivery_at,
                 (array_agg(left(error_message,240) ORDER BY happened_at DESC))[1]
                   AS last_delivery_error
          FROM delivery_rows
          GROUP BY watch_id
        )
        SELECT w.id,w.watch_id,w.active,w.watch_type,w.display_name,w.search_term,w.aliases,
               w.match_mode,w.match_field,w.category,w.subcategory,w.parent_group,w.tags,w.min_priority,
               w.address,w.municipality,w.county,w.state,w.block,w.lot,w.notes,
               w.source_filter,w.alert_category_filter,w.starts_at,w.expires_at,w.nearby_enabled,
               w.radius_ft,w.latitude,w.longitude,w.spatial_scope,w.spatial_reference_entity_id,
               ST_GeometryType(w.spatial_target_geom) AS spatial_target_type,
               coalesce(rr.active_recipient_count,0) AS active_recipient_count,
               coalesce(rr.recipient_ids,ARRAY[]::text[]) AS recipient_ids,
               coalesce(rr.recipient_names,ARRAY[]::text[]) AS recipient_names,
               coalesce(mr.matches_7d,0) AS matches_7d,
               coalesce(mr.matches_total,0) AS matches_total,
               mr.last_match_at,mr.last_match_title,
               coalesce(dr.sent_7d,0) AS sent_7d,
               dr.last_delivery_status,dr.last_delivery_at,dr.last_delivery_error
        FROM watch_items w
        LEFT JOIN recipient_rollup rr ON rr.watch_item_id=w.id
        LEFT JOIN match_rollup mr ON mr.watch_item_id=w.id
        LEFT JOIN delivery_rollup dr ON dr.watch_id=w.watch_id
        {clause}
        ORDER BY w.active DESC,w.parent_group NULLS FIRST,w.display_name
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
        row["keyword_choices"] = list(
            dict.fromkeys(
                value
                for value in [row.get("search_term"), *(row.get("aliases") or [])]
                if value
            )
        ) if row["setup_mode"] != "LOCATION" else []

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
        "SELECT id::text AS id,subscriber_id,name FROM subscribers WHERE active=true ORDER BY name"
    )
    watch_location_layers = query_all(
        """
        SELECT l.id::text AS id,l.layer_key,l.name,
               (SELECT count(*) FROM map_features f WHERE f.layer_id=l.id AND f.active=true) AS feature_count
        FROM map_layers l
        WHERE l.active=true AND l.layer_type='CUSTOM_GEOJSON'
          AND EXISTS (SELECT 1 FROM map_features f WHERE f.layer_id=l.id AND f.active=true)
        ORDER BY l.name
        """
    )
    layer_descriptions = {
        "NJ_OFFICIAL_MUNICIPALITIES": "Choose individual towns, grouped by county",
        "NJ_OFFICIAL_COUNTIES": "Watch an entire county boundary",
        "NJDOT_MAJOR_HIGHWAYS": "Watch along a major road using a Distance",
    }
    for layer in watch_location_layers:
        layer["description"] = layer_descriptions.get(
            layer.get("layer_key"),
            "Choose individual saved Locations",
        )
    municipality_layer = next(
        (
            layer
            for layer in watch_location_layers
            if layer.get("layer_key") == "NJ_OFFICIAL_MUNICIPALITIES"
        ),
        None,
    )
    bulk_context = None
    if bulk_layer.strip():
        try:
            bulk_layer_id = uuid.UUID(bulk_layer.strip())
        except ValueError as exc:
            raise HTTPException(400, "Choose an imported or drawn map layer again") from exc
        bulk_context = _bulk_location_context(
            bulk_layer_id,
            parent_by=bulk_parent_by,
            group_by=bulk_group_by,
            name_by=bulk_name_by,
            group_filter=bulk_group_filter,
        )
        if (
            municipality_layer
            and bulk_context["layer"].get("layer_key") == "NJ_OFFICIAL_COUNTIES"
        ):
            for group in bulk_context["groups"]:
                group["town_picker_url"] = "/watchlist?" + urlencode(
                    {
                        "bulk_layer": municipality_layer["id"],
                        "bulk_group_filter": group["label"],
                    }
                ) + "#bulk-watch-builder"
    alert_sources = query_all(
        "SELECT source,count(*) AS total FROM alerts WHERE nullif(trim(source),'') IS NOT NULL GROUP BY source ORDER BY source"
    )
    alert_categories = query_all(
        "SELECT category,count(*) AS total FROM alerts WHERE nullif(trim(category),'') IS NOT NULL GROUP BY category ORDER BY category"
    )
    health = _watch_health()
    needs_recipient_watches = [row for row in all_items if row["state_label"] == "Needs Recipient"]
    prefill = {
        "from_alert": "",
        "source_alert_title": "",
        "suggested_source": "",
        "suggested_category": "",
        "suggested_subtype": "",
        "keyword_choices": [],
        "selected_keywords": [],
        "display_name": display_name,
        "setup_mode": _setup_mode(setup_mode) if setup_mode.strip() else "LOCATION",
        "search_term": search_term,
        "aliases": "",
        "location_query": location_query,
        "latitude": latitude,
        "longitude": longitude,
        "location_kind": location_kind or ("MAP_POINT" if latitude and longitude else "TYPED_ADDRESS"),
        "location_id": location_id,
        "source_filter": "",
        "alert_category_filter": "",
        "notes": "",
    }
    if from_alert.strip():
        alert_prefill = _watch_prefill_from_alert(from_alert)
        if alert_prefill:
            prefill.update(alert_prefill)
            if setup_mode.strip():
                prefill["setup_mode"] = _setup_mode(setup_mode)
            chosen = list(
                dict.fromkeys(
                    keyword.strip()[:80]
                    for keyword in selected_keywords[:12]
                    if keyword.strip()
                )
            )
            if chosen:
                prefill["selected_keywords"] = chosen
                prefill["search_term"] = chosen[0]
                prefill["aliases"] = ", ".join(chosen[1:])
        elif not error:
            error = "That alert could not be found. No Watch was created."
    return templates.TemplateResponse(
        request=request,
        name="watchlist.html",
        context={
            "items": items,
            "watch_lab_items": all_items,
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
            "watch_location_layers": watch_location_layers,
            "bulk_context": bulk_context,
            "bulk_watch_limit": BULK_WATCH_LIMIT,
            "prefill": prefill,
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


def _validate_alert_filters(cur, source_values: list[str], category_values: list[str]) -> None:
    """Keep private Watch labels out of exact source/category filters."""
    for column, label, values in (
        ("source", "source", source_values),
        ("category", "category", category_values),
    ):
        if not values:
            continue
        cur.execute(
            f"""SELECT filter_value
                FROM unnest(%s::text[]) filter_value
                WHERE NOT EXISTS (
                  SELECT 1 FROM alerts a
                  WHERE upper(btrim(a.{column}))=upper(btrim(filter_value))
                )""",
            (values,),
        )
        unknown = [str(row["filter_value"]) for row in cur.fetchall()]
        if unknown:
            raise HTTPException(
                400,
                f"Unknown alert {label}: {', '.join(unknown)}. Choose a current value or leave it blank.",
            )


@app.post("/watchlist/repair-unrouted")
@_friendly_watch_errors
def spatial_watch_repair_unrouted(subscriber_id: uuid.UUID = Form(...)):
    """Connect every effective unrouted Watch to one explicitly chosen Recipient."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM subscribers WHERE id=%s AND active=true FOR SHARE",
                (subscriber_id,),
            )
            recipient = cur.fetchone()
            if not recipient:
                raise HTTPException(400, "Choose an active Recipient")
            cur.execute(
                """
                INSERT INTO watch_item_recipients(watch_item_id,subscriber_id,active)
                SELECT w.id,%s,true
                FROM watch_items w
                WHERE w.active=true
                  AND (w.expires_at IS NULL OR w.expires_at>now())
                  AND NOT EXISTS (
                    SELECT 1
                    FROM watch_item_recipients wir
                    JOIN subscribers s ON s.id=wir.subscriber_id
                    WHERE wir.watch_item_id=w.id AND wir.active=true AND s.active=true
                  )
                ON CONFLICT(watch_item_id,subscriber_id) DO UPDATE SET active=true
                RETURNING watch_item_id
                """,
                (subscriber_id,),
            )
            repaired = len(cur.fetchall())
        conn.commit()
    if not repaired:
        return _watch_redirect(message="Every active Watch already has a Recipient.")
    noun = "Watch" if repaired == 1 else "Watches"
    return _watch_redirect(
        message=(
            f"Connected {repaired} {noun} to {recipient['name']}. "
            "Future Matches can now send Notifications."
        )
    )


def _insert_watch_item(cur, item: dict) -> None:
    """Write one Watch through the existing watch_items and PostGIS trigger contract."""
    target_wkt = item.get("target_wkt")
    cur.execute(
        """
        INSERT INTO watch_items(
          id,watch_id,active,watch_type,display_name,search_term,aliases,match_mode,match_field,
          category,parent_group,tags,min_priority,address,municipality,county,state,block,lot,parcel_id,
          notes,source_notes,source_filter,alert_category_filter,
          starts_at,expires_at,gis_enabled,gis_lookup,nearby_enabled,radius_ft,spatial_scope,
          spatial_reference_entity_id,spatial_target_geom
        ) VALUES(
          %s,%s,%s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
          CASE WHEN %s::text IS NULL THEN NULL ELSE ST_GeomFromEWKT(%s::text) END
        )
        """,
        (
            item["id"],
            item["watch_id"],
            item.get("active", True),
            item["watch_type"],
            item["display_name"],
            item["search_term"],
            item.get("aliases", []),
            item.get("match_mode", "CONTAINS"),
            item.get("match_field"),
            item.get("category"),
            item.get("parent_group"),
            item.get("tags", []),
            item.get("min_priority", 1),
            item.get("address"),
            item.get("municipality"),
            item.get("county"),
            item.get("state"),
            item.get("block"),
            item.get("lot"),
            item.get("parcel_id"),
            item.get("notes"),
            item.get("source_notes"),
            item.get("source_filter", []),
            item.get("alert_category_filter", []),
            item.get("starts_at"),
            item.get("expires_at"),
            item.get("gis_enabled", False),
            item.get("gis_lookup"),
            item.get("nearby_enabled", False),
            item.get("radius_ft"),
            item.get("spatial_scope", "RADIUS"),
            item.get("spatial_reference_entity_id"),
            target_wkt,
            target_wkt,
        ),
    )


def _normalized_property_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _infer_property_key(keys: list[str], preferences: tuple[str, ...], excluded: set[str] | None = None) -> str:
    excluded = excluded or set()
    by_normalized = {
        _normalized_property_key(key): key
        for key in keys
        if key not in excluded
    }
    for preference in preferences:
        match = by_normalized.get(_normalized_property_key(preference))
        if match:
            return match
    return ""


def _bulk_location_context(
    layer_id: uuid.UUID,
    *,
    parent_by: str = "",
    group_by: str = "",
    name_by: str = "",
    group_filter: str = "",
) -> dict:
    """Organize one existing Mapping Center layer without copying its GIS data."""
    layer = query_one(
        """
        SELECT id::text AS id,layer_key,name
        FROM map_layers
        WHERE id=%s AND active=true AND layer_type='CUSTOM_GEOJSON'
        """,
        (layer_id,),
    )
    if not layer:
        raise HTTPException(404, "That reusable Watch Location layer is no longer available")

    key_rows = query_all(
        """
        SELECT property_key,count(*) AS feature_count
        FROM map_features f
        CROSS JOIN LATERAL jsonb_object_keys(f.properties) AS keys(property_key)
        WHERE f.layer_id=%s AND f.active=true AND keys.property_key !~ '^_'
        GROUP BY property_key
        ORDER BY property_key
        """,
        (layer_id,),
    )
    keys = [str(row["property_key"]) for row in key_rows]
    valid_parent = {"", BULK_NO_PROPERTY, *keys}
    valid_group = {"", BULK_NO_PROPERTY, BULK_EACH_FEATURE, *keys}
    valid_name = {"", BULK_STORED_NAME, *keys}
    if parent_by not in valid_parent or group_by not in valid_group or name_by not in valid_name:
        raise HTTPException(400, "That layer field is no longer available. Choose how to organize the layer again.")

    name_by = name_by or _infer_property_key(
        keys,
        (
            "name", "title", "label", "municipality", "mun_name", "munname",
            "city", "town", "county", "county_name", "route_name", "route",
            "highway", "road_name", "road", "fulladdr", "address",
        ),
    ) or BULK_STORED_NAME
    parent_by = parent_by or _infer_property_key(
        keys,
        ("state", "state_name", "region"),
        {name_by},
    ) or BULK_NO_PROPERTY
    group_by = group_by or _infer_property_key(
        keys,
        ("county", "county_name", "countyname", "cnty_name", "cntyname", "route", "route_name", "highway"),
        {name_by, parent_by},
    ) or BULK_EACH_FEATURE

    rows = query_all(
        """
        SELECT f.id::text AS id,f.name,f.properties,
               ST_GeometryType(f.geom) AS geometry_type
        FROM map_features f
        WHERE f.layer_id=%s AND f.active=true AND f.geom IS NOT NULL
        ORDER BY f.name NULLS LAST,f.id
        LIMIT 5001
        """,
        (layer_id,),
    )
    if len(rows) > 5000:
        raise HTTPException(400, "This layer has more than 5,000 active locations. Split it into smaller Mapping Center layers before bulk creation.")

    groups: dict[str, dict] = {}
    features_by_id: dict[str, dict] = {}
    for row in rows:
        properties = row.get("properties") or {}
        label = (
            str(row.get("name") or "").strip()
            if name_by == BULK_STORED_NAME
            else str(properties.get(name_by) or "").strip()
        )
        label = label or str(row.get("name") or "").strip() or _property_value(
            properties,
            "name", "title", "label", "municipality", "mun_name", "munname",
            "city", "town", "county", "route_name", "route", "road_name", "road",
        )
        label = label or f"{layer['name']} location {str(row['id'])[:8]}"
        parent = "" if parent_by == BULK_NO_PROPERTY else str(properties.get(parent_by) or "Other").strip()
        if group_by == BULK_NO_PROPERTY:
            group = "All locations"
            feature_token = ""
        elif group_by == BULK_EACH_FEATURE:
            group = label
            feature_token = str(row["id"])
        else:
            group = str(properties.get(group_by) or "Other").strip()
            feature_token = ""
        token = json.dumps([parent, group, feature_token], separators=(",", ":"))
        path = " › ".join(value for value in (parent, group) if value)
        item = {
            **dict(row),
            "label": label,
            "parent_group": path or layer["name"],
            "municipality": _property_value(properties, "municipality", "mun_name", "munname", "city", "town"),
            "county": _property_value(properties, "county", "county_name", "countyname", "cnty_name", "cntyname"),
            "state": _property_value(properties, "state", "state_name", "st"),
        }
        features_by_id[str(row["id"])] = item
        bucket = groups.setdefault(
            token,
            {
                "token": token,
                "parent": parent,
                "label": group,
                "path": path,
                "feature_ids": [],
                "features": [],
                "samples": [],
            },
        )
        bucket["feature_ids"].append(str(row["id"]))
        bucket["features"].append({"id": str(row["id"]), "label": label})
        if len(bucket["samples"]) < 5:
            bucket["samples"].append(label)

    group_list = sorted(groups.values(), key=lambda item: (item["parent"].casefold(), item["label"].casefold()))
    normalized_filter = group_filter.strip().casefold()
    if normalized_filter:
        group_list = [
            group
            for group in group_list
            if group["label"].casefold() == normalized_filter
        ]
        if not group_list:
            raise HTTPException(404, "That county is no longer available in this Location layer")
    for group in group_list:
        group["count"] = len(group["feature_ids"])
    return {
        "layer": layer,
        "property_keys": key_rows,
        "parent_by": parent_by,
        "group_by": group_by,
        "name_by": name_by,
        "group_filter": group_filter.strip(),
        "groups": group_list,
        "features_by_id": features_by_id,
        "feature_count": len(rows),
        "visible_feature_count": sum(group["count"] for group in group_list),
    }


def _selected_bulk_feature_ids(
    context: dict,
    *,
    group_tokens: list[str],
    feature_ids: list[uuid.UUID],
) -> list[str]:
    """Accept town-level choices while preserving the older group submission contract."""
    known_features = context["features_by_id"]
    if feature_ids:
        requested = list(dict.fromkeys(str(feature_id) for feature_id in feature_ids))
        if not set(requested).issubset(known_features):
            raise HTTPException(400, "One or more selected Locations changed. Review the layer and try again.")
        return requested

    selected_tokens = set(group_tokens)
    if not selected_tokens:
        raise HTTPException(400, "Choose at least one Location")
    known_groups = {group["token"]: group for group in context["groups"]}
    if not selected_tokens.issubset(known_groups):
        raise HTTPException(400, "One or more Location groups changed. Review the layer and try again.")
    return list(
        dict.fromkeys(
            feature_id
            for token in selected_tokens
            for feature_id in known_groups[token]["feature_ids"]
        )
    )


@app.post("/watchlist/bulk-create")
@_friendly_watch_errors
def spatial_watch_bulk_create(
    layer_id: uuid.UUID = Form(...),
    parent_by: str = Form(BULK_NO_PROPERTY),
    group_by: str = Form(BULK_EACH_FEATURE),
    name_by: str = Form(BULK_STORED_NAME),
    group_tokens: list[str] = Form([]),
    feature_ids: list[uuid.UUID] = Form([]),
    topic: str = Form(""),
    aliases: str = Form(""),
    radius_ft: float = Form(50.0),
    min_priority: int = Form(1),
    duration: str = Form("PERMANENT"),
    starts_at: str = Form(""),
    expires_at: str = Form(""),
    activate: str | None = Form(None),
    subscriber_ids: list[uuid.UUID] = Form([]),
):
    context = _bulk_location_context(
        layer_id,
        parent_by=parent_by,
        group_by=group_by,
        name_by=name_by,
    )
    selected_feature_ids = _selected_bulk_feature_ids(
        context,
        group_tokens=group_tokens,
        feature_ids=feature_ids,
    )
    if len(selected_feature_ids) > BULK_WATCH_LIMIT:
        raise HTTPException(
            400,
            f"This selection contains {len(selected_feature_ids):,} Locations. Choose {BULK_WATCH_LIMIT} or fewer at a time.",
        )

    topic = topic.strip()
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    validate_watch("CONTAINS", "", min_priority)
    start, end = _schedule(duration, starts_at, expires_at)
    turn_on = activate is not None
    if turn_on and not subscriber_ids:
        raise HTTPException(400, "Choose at least one Recipient before turning bulk Watches on")

    created_ids: list[uuid.UUID] = []
    skipped = 0
    with db_conn() as conn:
        with conn.cursor() as cur:
            unique_subscribers = list(dict.fromkeys(subscriber_ids))
            for subscriber_id in unique_subscribers:
                cur.execute("SELECT id FROM subscribers WHERE id=%s AND active=true", (subscriber_id,))
                if not cur.fetchone():
                    raise HTTPException(400, "One or more selected Recipients are paused or no longer available")

            cur.execute(
                """
                SELECT id::text AS id,ST_AsEWKT(geom) AS target_wkt
                FROM map_features
                WHERE layer_id=%s AND active=true AND geom IS NOT NULL
                  AND id=ANY(%s::uuid[])
                """,
                (layer_id, [uuid.UUID(feature_id) for feature_id in selected_feature_ids]),
            )
            targets = {row["id"]: row["target_wkt"] for row in cur.fetchall()}
            if len(targets) != len(selected_feature_ids):
                raise HTTPException(400, "One or more selected Locations changed. Review the layer and try again.")

            for feature_id in selected_feature_ids:
                feature = context["features_by_id"][feature_id]
                search_term = topic or feature["label"]
                gis_lookup = f"map_feature:{feature_id}"
                cur.execute(
                    """
                    SELECT id FROM watch_items
                    WHERE gis_lookup=%s AND lower(search_term)=lower(%s)
                    LIMIT 1
                    """,
                    (gis_lookup, search_term),
                )
                if cur.fetchone():
                    skipped += 1
                    continue
                display_name = feature["label"] if not topic else f"{feature['label']} · {topic}"
                watch_uuid = uuid.uuid4()
                _insert_watch_item(
                    cur,
                    {
                        "id": watch_uuid,
                        "watch_id": make_watch_id(display_name),
                        "active": turn_on,
                        "watch_type": "LOCATION_TOPIC" if topic else _watch_type_for_geometry(feature.get("geometry_type")),
                        "display_name": display_name,
                        "search_term": search_term,
                        "aliases": csv_array(aliases),
                        "match_mode": "CONTAINS",
                        "parent_group": feature["parent_group"],
                        "min_priority": min_priority,
                        "address": feature["label"],
                        "municipality": feature.get("municipality") or None,
                        "county": feature.get("county") or None,
                        "state": feature.get("state") or None,
                        "notes": f"Created from Mapping Center layer {context['layer']['name']}",
                        "source_notes": "watch_setup:BULK_CUSTOM_FEATURE",
                        "starts_at": start,
                        "expires_at": end,
                        "gis_enabled": True,
                        "gis_lookup": gis_lookup,
                        "nearby_enabled": True,
                        "radius_ft": radius_ft,
                        "target_wkt": targets[feature_id],
                    },
                )
                created_ids.append(watch_uuid)

            if created_ids and unique_subscribers:
                cur.executemany(
                    """
                    INSERT INTO watch_item_recipients(watch_item_id,subscriber_id,active)
                    VALUES(%s,%s,true)
                    ON CONFLICT(watch_item_id,subscriber_id) DO UPDATE SET active=true
                    """,
                    [(watch_id, subscriber_id) for watch_id in created_ids for subscriber_id in unique_subscribers],
                )
        conn.commit()

    state_message = "on" if turn_on else "paused for review"
    message = f"Created {len(created_ids)} Watches, {state_message}"
    if skipped:
        message += f". Skipped {skipped} existing Watch{'es' if skipped != 1 else ''}"
    params = urlencode({"bulk_layer": str(layer_id), "msg": message})
    return RedirectResponse(f"/watchlist?{params}#bulk-watch-builder", status_code=303)


@app.post("/watchlist/bulk-action")
@_friendly_watch_errors
def spatial_watch_bulk_action(
    watch_item_ids: list[uuid.UUID] = Form([]),
    action: str = Form(...),
    confirm_delete: str = Form(""),
):
    selected = list(dict.fromkeys(watch_item_ids))
    if not selected:
        raise HTTPException(400, "Choose at least one Watch")
    if len(selected) > BULK_WATCH_ACTION_LIMIT:
        raise HTTPException(400, f"Choose {BULK_WATCH_ACTION_LIMIT} or fewer Watches at a time")
    action = action.strip().lower()
    if action not in {"pause", "activate", "delete"}:
        raise HTTPException(400, "Choose Pause, Reactivate, or Delete")
    if action == "delete" and confirm_delete.strip().upper() != "DELETE":
        raise HTTPException(400, "Type DELETE to permanently delete the selected Watches")

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM watch_items WHERE id=ANY(%s::uuid[]) FOR UPDATE",
                (selected,),
            )
            total = len(cur.fetchall())
            if not total:
                raise HTTPException(404, "The selected Watches no longer exist")
            if action == "delete":
                cur.execute("DELETE FROM watch_items WHERE id=ANY(%s::uuid[])", (selected,))
            elif action == "pause":
                cur.execute(
                    "UPDATE watch_items SET active=false,updated_at=now() WHERE id=ANY(%s::uuid[])",
                    (selected,),
                )
            else:
                cur.execute(
                    """
                    UPDATE watch_items
                    SET active=true,
                        starts_at=CASE WHEN expires_at<=now() THEN NULL ELSE starts_at END,
                        expires_at=CASE WHEN expires_at<=now() THEN NULL ELSE expires_at END,
                        updated_at=now()
                    WHERE id=ANY(%s::uuid[])
                    """,
                    (selected,),
                )
                require_watch_recipients(cur, selected)
        conn.commit()

    label = "deleted" if action == "delete" else "paused" if action == "pause" else "reactivated"
    suffix = ". Stored alerts and Notification history were kept; Match links were removed." if action == "delete" else "."
    return _watch_redirect(message=f"{total} Watch{'es' if total != 1 else ''} {label}{suffix}")


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
    alert_keywords: list[str] = Form([]),
    subscriber_ids: list[uuid.UUID] = Form([]),
    activation: str = Form("on"),
):
    display_name = display_name.strip()
    if not display_name:
        raise HTTPException(400, "Name this watch before turning it on")
    setup_mode = _setup_mode(setup_mode)
    location_required = setup_mode in {"LOCATION", "LOCATION_TOPIC"} or spatial_enabled is not None
    topic_required = setup_mode in {"TOPIC", "LOCATION_TOPIC"}
    topic = search_term.strip()
    alias_values = csv_array(aliases)
    selected_keywords: list[str] = []
    for value in alert_keywords:
        value = re.sub(r"\s+", " ", value).strip()[:80]
        if value and value.casefold() not in {item.casefold() for item in selected_keywords}:
            selected_keywords.append(value)
    if topic_required and selected_keywords:
        combined_aliases = [*selected_keywords, *alias_values]
        alias_values = []
        seen_aliases = {topic.casefold()}
        for value in combined_aliases:
            if value.casefold() not in seen_aliases:
                seen_aliases.add(value.casefold())
                alias_values.append(value)
    if topic_required and not topic:
        raise HTTPException(400, "Enter the topic, phrase, organization, or incident wording to watch for")
    match_mode = match_mode.upper().strip() or "CONTAINS"
    validate_watch(match_mode, match_field, min_priority)
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    start, end = _schedule(duration, starts_at, expires_at)
    activation = activation.strip().lower()
    if activation not in {"on", "paused"}:
        raise HTTPException(400, "Choose Turn On Watch or Save Paused")
    turn_on = activation == "on"
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

        saved_source_filter = csv_array(source_filter)
        saved_category_filter = csv_array(alert_category_filter)

        saved_address = (target or {}).get("address") or (location_query or address).strip() or None
        saved_municipality = (target or {}).get("municipality") or municipality.strip() or None
        target_wkt = (target or {}).get("target_wkt")
        reference_id = (target or {}).get("spatial_reference_entity_id")
        with conn.cursor() as cur:
            _validate_alert_filters(cur, saved_source_filter, saved_category_filter)
            _insert_watch_item(
                cur,
                {
                    "id": watch_uuid,
                    "watch_id": make_watch_id(display_name),
                    "active": turn_on,
                    "watch_type": saved_watch_type,
                    "display_name": display_name,
                    "search_term": saved_search_term,
                    "aliases": alias_values,
                    "match_mode": saved_match_mode,
                    "match_field": saved_match_field,
                    "category": category.strip() or None,
                    "tags": csv_array(tags),
                    "min_priority": min_priority,
                    "address": saved_address,
                    "municipality": saved_municipality,
                    "county": (target or {}).get("county"),
                    "state": (target or {}).get("state") or ("NJ" if target else None),
                    "block": (target or {}).get("block"),
                    "lot": (target or {}).get("lot"),
                    "parcel_id": (target or {}).get("parcel_id"),
                    "notes": notes.strip() or None,
                    "source_notes": f"watch_setup:{(target or {}).get('kind', 'TOPIC')}",
                    "source_filter": saved_source_filter,
                    "alert_category_filter": saved_category_filter,
                    "starts_at": start,
                    "expires_at": end,
                    "gis_enabled": spatial_requested,
                    "gis_lookup": (target or {}).get("gis_lookup"),
                    "nearby_enabled": spatial_requested,
                    "radius_ft": radius_ft,
                    "spatial_reference_entity_id": reference_id,
                    "target_wkt": target_wkt,
                },
            )
            _save_recipients(cur, watch_uuid, subscriber_ids)
            require_watch_recipients(cur, [watch_uuid])
        conn.commit()
    message = "Watch is on and ready to notify" if turn_on else "Watch saved Paused for review"
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
                       nearby_enabled,radius_ft,gis_lookup,spatial_reference_entity_id,
                       ST_AsEWKT(spatial_target_geom) AS target_wkt,
                       EXISTS (
                         SELECT 1
                         FROM watch_item_recipients wir
                         JOIN subscribers s ON s.id=wir.subscriber_id
                         WHERE wir.watch_item_id=watch_items.id AND wir.active AND s.active
                       ) AS had_active_recipient
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
        saved_source_filter = csv_array(source_filter)
        saved_category_filter = csv_array(alert_category_filter)

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
        gis_lookup = (
            (target or {}).get("gis_lookup")
            if replace_target
            else current.get("gis_lookup")
        )
        active_value = current.get("active") if keep_state else active is not None
        with conn.cursor() as cur:
            _validate_alert_filters(cur, saved_source_filter, saved_category_filter)
            cur.execute(
                """
                UPDATE watch_items SET
                  active=%s,watch_type=%s,display_name=%s,search_term=%s,aliases=%s,
                  match_mode=%s,match_field=%s,category=%s,tags=%s,min_priority=%s,address=%s,
                  municipality=%s,county=%s,state=%s,block=%s,lot=%s,parcel_id=%s,
                  notes=%s,source_notes=%s,source_filter=%s,alert_category_filter=%s,
                  starts_at=%s,expires_at=%s,gis_enabled=%s,nearby_enabled=%s,radius_ft=%s,
                  spatial_scope='RADIUS',
                  gis_lookup=CASE WHEN %s THEN %s ELSE gis_lookup END,
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
                    saved_source_filter, saved_category_filter, start, end,
                    spatial_requested, spatial_requested, radius_ft,
                    replace_target, gis_lookup,
                    replace_target, reference_id, replace_target, target_wkt, target_wkt,
                    item_id,
                ),
            )
            _save_recipients(cur, item_id, subscriber_ids)
            preserving_match_only = bool(
                current.get("active")
                and keep_state
                and not current.get("had_active_recipient")
                and active_value
                and not subscriber_ids
            )
            if not preserving_match_only:
                require_watch_recipients(cur, [item_id])
        conn.commit()
    message = "Watch updated"
    if preserving_match_only:
        message = "Watch updated. It is still Match-only until a Recipient is selected."
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
            if next_active:
                require_watch_recipients(cur, [item_id])
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
        """SELECT gis_spatial_history(
                    coalesce(a.geom,r.geom),%s,make_interval(hours=>%s),NULL,NULL,1
                  ) AS context
           FROM alerts a
           LEFT JOIN geo_entity_resolutions r
             ON r.entity_type='ALERT' AND r.entity_id=a.id::text AND r.status='RESOLVED'
           WHERE a.alert_id=%s AND coalesce(a.geom,r.geom) IS NOT NULL""",
        (max(1.0, min(radius_ft, 26400.0)), max(1, min(hours, 8760)), alert_id),
    )
    if not row:
        raise HTTPException(404, "Alert has no resolved spatial point")
    return JSONResponse(_json_safe(row["context"]))


@app.get("/api/alerts/{alert_id}/impact-buffer.geojson")
def alert_impact_buffer(alert_id: str, radius_ft: float = 500.0):
    radius_ft = max(1.0, min(float(radius_ft), 26400.0))
    row = query_one(
        """SELECT a.title,ST_AsGeoJSON(
                    ST_Buffer(coalesce(a.geom,r.geom)::geography,%s*0.3048)::geometry
                  )::json AS geometry
           FROM alerts a
           LEFT JOIN geo_entity_resolutions r
             ON r.entity_type='ALERT' AND r.entity_id=a.id::text AND r.status='RESOLVED'
           WHERE a.alert_id=%s AND coalesce(a.geom,r.geom) IS NOT NULL""",
        (radius_ft, alert_id),
    )
    if not row:
        raise HTTPException(404, "Alert has no resolved spatial point")
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
