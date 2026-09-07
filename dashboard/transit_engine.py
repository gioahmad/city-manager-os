
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any
import urllib.parse

import psycopg
from psycopg.rows import dict_row

from transit_runtime import (
    TransitResult,
    get_bytes,
    get_cached_bus_token,
    get_cached_rail_token,
    invalidate_token,
    json_value,
    post_multipart,
    post_urlencoded,
)

TRANSIT_ADAPTERS = {"NJT_BUSDATA", "NJT_RAILDATA", "NJT_BUS_GTFS", "NJT_RAIL_GTFS", "NJT_RSS", "PATH_REALTIME", "TRANSIT_GTFS_URL", "NYW_ADVISORIES"}


def db_conn():
    return psycopg.connect(
        host=os.getenv("DB_HOST", "citymanager-postgis"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "citymanager"),
        user=os.getenv("DB_USER", "citymanager_app"),
        password=os.environ["DB_PASSWORD"],
        row_factory=dict_row,
        connect_timeout=5,
    )


def is_transit_adapter(integration: dict[str, Any]) -> bool:
    return str(integration.get("adapter_type") or "").upper() in TRANSIT_ADAPTERS


def _config(integration: dict[str, Any]) -> dict[str, Any]:
    value = integration.get("auth_config") or {}
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value)


def _parser_config(integration: dict[str, Any]) -> dict[str, Any]:
    value = integration.get("parser_config") or {}
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value)


def _required_values(integration: dict[str, Any]) -> dict[str, str]:
    cfg = _config(integration)
    values: dict[str, str] = {}
    missing: list[str] = []
    for key, env_name in cfg.items():
        if not str(key).endswith("_env") or not env_name:
            continue
        value = os.getenv(str(env_name), "")
        if not value:
            missing.append(str(env_name))
        else:
            values[key] = value
    if missing:
        raise ValueError("Missing environment variable(s): " + ", ".join(sorted(set(missing))))
    return values


def _provider_id(conn, provider_key: str = "NJ_TRANSIT") -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM transit_providers WHERE provider_key=%s", (provider_key,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"Transit provider {provider_key} is not configured")
    return row["id"]


def _record_run_start(conn, integration_id: uuid.UUID, run_type: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO integration_runs(integration_id,run_type,status)
            VALUES (%s,%s,'RUNNING') RETURNING id
            """,
            (integration_id, run_type),
        )
        return cur.fetchone()["id"]


def _finish_run(
    conn,
    run_id: uuid.UUID,
    *,
    status: str,
    result: TransitResult | None = None,
    items_found: int = 0,
    items_changed: int = 0,
    error_message: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE integration_runs
            SET status=%s,
                http_status=%s,
                elapsed_ms=%s,
                response_bytes=%s,
                content_type=%s,
                items_found=%s,
                items_changed=%s,
                error_message=%s,
                finished_at=now()
            WHERE id=%s
            """,
            (
                status,
                result.status_code if result else None,
                result.elapsed_ms if result else None,
                result.body_bytes if result else None,
                result.content_type if result else None,
                items_found,
                items_changed,
                error_message,
                run_id,
            ),
        )


def _update_health(
    conn,
    integration: dict[str, Any],
    *,
    ok: bool,
    error: str | None,
    item_count: int = 0,
) -> None:
    source_id = f"INT:{integration['integration_key']}"
    metadata = {
        "integration_id": str(integration["id"]),
        "name": integration["name"],
        "category": integration["category"],
        "adapter_type": integration["adapter_type"],
        "item_count": item_count,
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO source_health(
              source_id,status,last_attempt_at,last_success_at,last_event_at,last_error,metadata,updated_at
            )
            VALUES(
              %s,%s,now(),CASE WHEN %s THEN now() ELSE NULL END,
              CASE WHEN %s > 0 THEN now() ELSE NULL END,%s,%s::jsonb,now()
            )
            ON CONFLICT(source_id) DO UPDATE SET
              status=EXCLUDED.status,
              last_attempt_at=now(),
              last_success_at=CASE WHEN %s THEN now() ELSE source_health.last_success_at END,
              last_event_at=CASE WHEN %s > 0 THEN now() ELSE source_health.last_event_at END,
              last_error=%s,
              metadata=EXCLUDED.metadata,
              updated_at=now()
            """,
            (
                source_id,
                "OK" if ok else "ERROR",
                ok,
                item_count,
                error,
                json.dumps(metadata),
                ok,
                item_count,
                error,
            ),
        )


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", _text(value).upper()).strip()


def _float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _bbox() -> tuple[float, float, float, float]:
    raw = os.getenv("TRANSIT_CAPTURE_BBOX", "-74.10,40.68,-73.90,40.85")
    try:
        west, south, east, north = [float(x.strip()) for x in raw.split(",")]
        return west, south, east, north
    except Exception:
        return -74.10, 40.68, -73.90, 40.85


def _in_bbox(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    west, south, east, north = _bbox()
    return west <= lon <= east and south <= lat <= north


def _pick(row: dict[str, Any], *names: str) -> Any:
    upper = {str(k).upper(): v for k, v in row.items()}
    for name in names:
        if name.upper() in upper and _text(upper[name.upper()]):
            return upper[name.upper()]
    return None


def _json_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in ("items", "ITEMS", "data", "DATA", "results", "RESULTS"):
            if isinstance(value.get(key), list):
                return [x for x in value[key] if isinstance(x, dict)]
        # NJ TRANSIT APIs have used several wrapper names over time.
        for candidate in value.values():
            if isinstance(candidate, list) and all(isinstance(x, dict) for x in candidate):
                return list(candidate)
        return [value]
    return []


def _parse_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    candidates = [text, text.replace("Z", "+00:00")]
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _hash_dict(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _watch_rows(conn, provider_id: uuid.UUID) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id,target_type,target_key,display_name,municipality,corridor,min_impact_level
            FROM transit_watch_config
            WHERE active=true AND (provider_id=%s OR provider_id IS NULL)
            ORDER BY display_name
            """,
            (provider_id,),
        )
        return cur.fetchall()


def _watch_hits(text: str, watches: list[dict[str, Any]]) -> list[str]:
    hay = _norm(text)
    hits: list[str] = []
    for watch in watches:
        candidates = [_norm(watch.get("target_key")), _norm(watch.get("display_name")), _norm(watch.get("corridor"))]
        if any(candidate and candidate in hay for candidate in candidates):
            hits.append(_text(watch.get("display_name")) or _text(watch.get("target_key")))
    return sorted(set(hits))


def _score_text(text: str, watch_hits: list[str] | None = None) -> tuple[int, str]:
    hay = _norm(text)
    score = 0
    if watch_hits:
        score += 30
    if re.search(r"\b(SUSPEND|SUSPENDED|NO SERVICE|SERVICE SUSPENSION|SHUTDOWN|CLOSED)\b", hay):
        score += 55
    if re.search(r"\b(MAJOR DELAY|SIGNIFICANT DELAY|WIDESPREAD|MULTIPLE LINES|SYSTEMWIDE)\b", hay):
        score += 45
    if re.search(r"\b(DELAY|DELAYED|LATE|SIGNAL|SWITCH|POLICE|MEDICAL|MECHANICAL)\b", hay):
        score += 18
    if re.search(r"\b(SERVICE CHANGE|SCHEDULE CHANGE|DETOUR|BYPASS|SKIP|SKIPPING|REVISED SERVICE)\b", hay):
        score += 18
    if re.search(r"\b(CANCEL|CANCELLED|CANCELED)\b", hay):
        score += 20
    if re.search(r"\b(LINCOLN TUNNEL|PABT|PORT AUTHORITY BUS TERMINAL|PORT IMPERIAL|HOBOKEN|SECAUCUS)\b", hay):
        score += 15
    score = min(score, 100)
    level = "ALERT" if score >= 75 else "WATCH" if score >= 45 else "AWARENESS"
    return score, level

def _upsert_asset(
    conn,
    *,
    provider_id: uuid.UUID,
    integration_id: uuid.UUID,
    asset_key: str,
    asset_type: str,
    mode: str | None,
    name: str,
    short_name: str | None = None,
    parent_asset_key: str | None = None,
    municipality: str | None = None,
    county: str | None = None,
    state: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT metadata,latitude,longitude,name,mode FROM transit_assets WHERE provider_id=%s AND asset_key=%s",
            (provider_id, asset_key),
        )
        before = cur.fetchone()
        cur.execute(
            """
            INSERT INTO transit_assets(
              provider_id,source_integration_id,asset_key,asset_type,mode,name,short_name,parent_asset_key,
              municipality,county,state,latitude,longitude,geom,metadata,active,first_seen_at,last_seen_at,created_at,updated_at
            )
            VALUES(
              %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
              CASE WHEN %s::double precision IS NOT NULL AND %s::double precision IS NOT NULL
                   THEN ST_SetSRID(ST_MakePoint(%s::double precision,%s::double precision),4326)
                   ELSE NULL END,
              %s::jsonb,true,now(),now(),now(),now()
            )
            ON CONFLICT(provider_id,asset_key) DO UPDATE SET
              source_integration_id=EXCLUDED.source_integration_id,
              asset_type=EXCLUDED.asset_type,
              mode=EXCLUDED.mode,
              name=EXCLUDED.name,
              short_name=EXCLUDED.short_name,
              parent_asset_key=EXCLUDED.parent_asset_key,
              municipality=COALESCE(EXCLUDED.municipality,transit_assets.municipality),
              county=COALESCE(EXCLUDED.county,transit_assets.county),
              state=COALESCE(EXCLUDED.state,transit_assets.state),
              latitude=COALESCE(EXCLUDED.latitude,transit_assets.latitude),
              longitude=COALESCE(EXCLUDED.longitude,transit_assets.longitude),
              geom=COALESCE(EXCLUDED.geom,transit_assets.geom),
              metadata=EXCLUDED.metadata,
              active=true,
              last_seen_at=now(),
              updated_at=now()
            """,
            (
                provider_id, integration_id, asset_key, asset_type, mode, name, short_name, parent_asset_key,
                municipality, county, state, latitude, longitude,
                latitude, longitude, longitude, latitude,
                json.dumps(metadata or {}, default=str),
            ),
        )
    current = {"metadata": metadata or {}, "latitude": latitude, "longitude": longitude, "name": name, "mode": mode}
    return before is None or _hash_dict(dict(before)) != _hash_dict(current)


def _upsert_observation(
    conn,
    *,
    provider_id: uuid.UUID,
    integration_id: uuid.UUID,
    external_key: str,
    mode: str | None,
    route_key: str | None,
    route_name: str | None,
    asset_key: str | None,
    asset_name: str | None,
    title: str,
    description: str | None,
    status: str,
    municipality: str | None,
    county: str | None,
    state: str | None,
    source_url: str | None,
    impact_score: int,
    impact_level: str,
    starts_at: datetime | None,
    ends_at: datetime | None,
    latitude: float | None,
    longitude: float | None,
    metadata: dict[str, Any],
) -> bool:
    selected = {
        "mode": mode,
        "route_key": route_key,
        "route_name": route_name,
        "asset_key": asset_key,
        "asset_name": asset_name,
        "title": title,
        "description": description,
        "status": status,
        "impact_score": impact_score,
        "impact_level": impact_level,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "latitude": latitude,
        "longitude": longitude,
        "metadata": metadata,
    }
    change_hash = _hash_dict(selected)
    fingerprint = hashlib.sha256(
        f"{integration_id}|{external_key}|{_norm(title)}".encode()
    ).hexdigest()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT change_hash FROM transit_observations WHERE source_integration_id=%s AND external_key=%s",
            (integration_id, external_key),
        )
        before = cur.fetchone()
        changed = before is None or before["change_hash"] != change_hash
        pending = bool(changed and impact_level == "ALERT")
        cur.execute(
            """
            INSERT INTO transit_observations(
              source_integration_id,provider_id,external_key,fingerprint,active,mode,route_key,route_name,
              asset_key,asset_name,title,description,status,municipality,county,state,source_url,
              impact_score,impact_level,starts_at,ends_at,latitude,longitude,geom,metadata,change_hash,
              first_seen_at,last_seen_at,last_changed_at,alert_pending,created_at,updated_at
            )
            VALUES(
              %s,%s,%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
              %s,%s,%s,%s,%s,%s,
              CASE WHEN %s::double precision IS NOT NULL AND %s::double precision IS NOT NULL
                   THEN ST_SetSRID(ST_MakePoint(%s::double precision,%s::double precision),4326)
                   ELSE NULL END,
              %s::jsonb,%s,now(),now(),now(),%s,now(),now()
            )
            ON CONFLICT(source_integration_id,external_key) DO UPDATE SET
              provider_id=EXCLUDED.provider_id,
              fingerprint=EXCLUDED.fingerprint,
              active=true,
              mode=EXCLUDED.mode,
              route_key=EXCLUDED.route_key,
              route_name=EXCLUDED.route_name,
              asset_key=EXCLUDED.asset_key,
              asset_name=EXCLUDED.asset_name,
              title=EXCLUDED.title,
              description=EXCLUDED.description,
              status=EXCLUDED.status,
              municipality=EXCLUDED.municipality,
              county=EXCLUDED.county,
              state=EXCLUDED.state,
              source_url=EXCLUDED.source_url,
              impact_score=EXCLUDED.impact_score,
              impact_level=EXCLUDED.impact_level,
              starts_at=EXCLUDED.starts_at,
              ends_at=EXCLUDED.ends_at,
              latitude=EXCLUDED.latitude,
              longitude=EXCLUDED.longitude,
              geom=EXCLUDED.geom,
              metadata=EXCLUDED.metadata,
              change_hash=EXCLUDED.change_hash,
              last_seen_at=now(),
              last_changed_at=CASE WHEN transit_observations.change_hash IS DISTINCT FROM EXCLUDED.change_hash
                                   THEN now() ELSE transit_observations.last_changed_at END,
              alert_pending=CASE
                  WHEN transit_observations.change_hash IS DISTINCT FROM EXCLUDED.change_hash
                    THEN EXCLUDED.alert_pending
                  ELSE transit_observations.alert_pending
                END,
              updated_at=now()
            """,
            (
                integration_id, provider_id, external_key, fingerprint, mode, route_key, route_name,
                asset_key, asset_name, title, description, status, municipality, county, state, source_url,
                impact_score, impact_level, starts_at, ends_at, latitude, longitude,
                latitude, longitude, longitude, latitude,
                json.dumps(metadata, default=str), change_hash, pending,
            ),
        )
    return changed


def _xml_records(text: str) -> list[dict[str, str]]:
    def parse(raw: str) -> ET.Element:
        return ET.fromstring(raw)

    root = parse(text)
    if len(root) == 0 and root.text and "<" in root.text:
        root = parse(root.text)

    records: list[dict[str, str]] = []
    for element in root.iter():
        children = list(element)
        if len(children) < 2:
            continue
        row: dict[str, str] = {}
        for child in children:
            if list(child):
                continue
            tag = child.tag.split("}")[-1]
            value = _text(child.text)
            if value:
                row[tag] = value
        if len(row) >= 2:
            records.append(row)

    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in records:
        key = _hash_dict(row)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _mode_from_route_type(route_type: Any, fallback: str) -> str:
    value = _text(route_type)
    return {
        "0": "LIGHT_RAIL",
        "1": "SUBWAY",
        "2": "RAIL",
        "3": "BUS",
        "4": "FERRY",
    }.get(value, fallback)



def _gtfs_payload_bytes(result: TransitResult) -> bytes:
    if result.raw.startswith(b"PK"):
        return result.raw
    text = result.body_text.strip()
    if not text:
        raise ValueError("GTFS response was empty")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = text
    candidates: list[str] = []
    if isinstance(payload, str):
        candidates.append(payload)
    elif isinstance(payload, dict):
        for key in ("data", "Data", "gtfs", "GTFS", "file", "File", "content", "Content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
    for candidate in candidates:
        candidate = candidate.strip().strip('"')
        try:
            decoded = base64.b64decode(candidate, validate=True)
        except Exception:
            continue
        if decoded.startswith(b"PK"):
            return decoded
    raise ValueError("GTFS response was not a ZIP archive or recognized base64 ZIP payload")

def _import_gtfs_zip(
    conn,
    *,
    raw: bytes,
    provider_id: uuid.UUID,
    integration_id: uuid.UUID,
    fallback_mode: str,
    force_mode: str | None = None,
    allowed_modes: set[str] | None = None,
    regional_stops_only: bool = False,
) -> tuple[int, int]:
    # Stops do not carry route_type. Use route -> trip -> stop_time relationships
    # so NJT HBLR stops inherit LIGHT_RAIL safely from GTFS.
    changed = 0
    found = 0
    allowed = {str(x).upper() for x in (allowed_modes or set())}

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = set(zf.namelist())
        route_modes: dict[str, str] = {}

        if "routes.txt" in names:
            reader = csv.DictReader(io.TextIOWrapper(zf.open("routes.txt"), encoding="utf-8-sig"))
            for row in reader:
                route_id = _text(row.get("route_id"))
                if not route_id:
                    continue
                mode = (force_mode or _mode_from_route_type(row.get("route_type"), fallback_mode)).upper()
                if allowed and mode not in allowed:
                    continue
                route_modes[route_id] = mode
                found += 1
                name = _text(row.get("route_long_name")) or _text(row.get("route_short_name")) or route_id
                changed += int(_upsert_asset(
                    conn,
                    provider_id=provider_id,
                    integration_id=integration_id,
                    asset_key=f"ROUTE:{route_id}",
                    asset_type="ROUTE",
                    mode=mode,
                    name=name,
                    short_name=_text(row.get("route_short_name")) or None,
                    metadata={**row, "_CMOS_MODE": mode},
                ))

        stop_modes: dict[str, set[str]] = {}
        if route_modes and "trips.txt" in names and "stop_times.txt" in names:
            trip_modes: dict[str, str] = {}
            reader = csv.DictReader(io.TextIOWrapper(zf.open("trips.txt"), encoding="utf-8-sig"))
            for row in reader:
                trip_id = _text(row.get("trip_id"))
                route_id = _text(row.get("route_id"))
                mode = route_modes.get(route_id)
                if trip_id and mode:
                    trip_modes[trip_id] = mode

            reader = csv.DictReader(io.TextIOWrapper(zf.open("stop_times.txt"), encoding="utf-8-sig"))
            for row in reader:
                trip_id = _text(row.get("trip_id"))
                stop_id = _text(row.get("stop_id"))
                mode = trip_modes.get(trip_id)
                if stop_id and mode:
                    stop_modes.setdefault(stop_id, set()).add(mode)

        if "stops.txt" in names:
            reader = csv.DictReader(io.TextIOWrapper(zf.open("stops.txt"), encoding="utf-8-sig"))
            for row in reader:
                stop_id = _text(row.get("stop_id"))
                if not stop_id:
                    continue
                modes = stop_modes.get(stop_id, set())
                if allowed and not modes:
                    continue

                if force_mode:
                    mode = force_mode.upper()
                elif "LIGHT_RAIL" in modes:
                    mode = "LIGHT_RAIL"
                elif "FERRY" in modes:
                    mode = "FERRY"
                elif "RAIL" in modes:
                    mode = "RAIL"
                elif "SUBWAY" in modes:
                    mode = "SUBWAY"
                elif "BUS" in modes:
                    mode = "BUS"
                elif modes:
                    mode = sorted(modes)[0]
                else:
                    mode = fallback_mode.upper()

                if allowed and mode not in allowed:
                    continue

                found += 1
                lat = _float(row.get("stop_lat"))
                lon = _float(row.get("stop_lon"))

                # NJ TRANSIT's static bus GTFS is statewide. Keep all route
                # definitions, but only materialize geocoded stops inside the
                # City Manager OS regional transit capture area so unrelated
                # statewide stops cannot crowd the map layer.
                if regional_stops_only and not _in_bbox(lat, lon):
                    continue

                name = _text(row.get("stop_name")) or stop_id
                if mode in {"RAIL", "LIGHT_RAIL", "SUBWAY"}:
                    asset_type = "STATION"
                elif mode == "FERRY":
                    asset_type = "TERMINAL"
                else:
                    asset_type = "STOP"

                changed += int(_upsert_asset(
                    conn,
                    provider_id=provider_id,
                    integration_id=integration_id,
                    asset_key=f"STOP:{stop_id}",
                    asset_type=asset_type,
                    mode=mode,
                    name=name,
                    latitude=lat,
                    longitude=lon,
                    state=None,
                    metadata={**row, "_CMOS_MODES": sorted(modes) if modes else [mode]},
                ))
    return found, changed

def _run_busdata(conn, integration: dict[str, Any], *, store: bool) -> tuple[TransitResult, int, int]:
    env = _required_values(integration)
    username, password = env["username_env"], env["password_env"]
    host = os.getenv("NJT_BUS_HOST", "https://pcsdata.njtransit.com").rstrip("/")
    west, south, east, north = _bbox()
    center_lat = (south + north) / 2.0
    center_lon = (west + east) / 2.0
    radius = max(1000, int(os.getenv("NJT_BUS_RADIUS_FEET", "30000")))

    collected: list[dict[str, Any]] = []
    bus_result: TransitResult | None = None
    bus_ok = False
    token: str | None = None
    token_error: Exception | None = None

    # Prefer current token-based BUSDV2, but token acquisition itself must not
    # prevent the known legacy BUSDATA fallback from being attempted.
    try:
        token = get_cached_bus_token(
            host=host,
            family="BUSDV2",
            username=username,
            password=password,
        )
    except Exception as exc:
        token_error = exc

    if token:
        for requested_mode in ("BUS",):
            result = post_multipart(
                f"{host}/api/BUSDV2/getVehicleLocations",
                {
                    "token": token,
                    "lat": center_lat,
                    "lon": center_lon,
                    "radius": radius,
                    "mode": requested_mode,
                },
                timeout_seconds=int(integration.get("timeout_seconds") or 30),
                max_response_bytes=25_000_000,
            )

            if not result.ok and result.status_code in {400, 401, 403}:
                invalidate_token(host, "BUSDV2", username)
                try:
                    token = get_cached_bus_token(
                        host=host,
                        family="BUSDV2",
                        username=username,
                        password=password,
                        force_refresh=True,
                    )
                    result = post_multipart(
                        f"{host}/api/BUSDV2/getVehicleLocations",
                        {
                            "token": token,
                            "lat": center_lat,
                            "lon": center_lon,
                            "radius": radius,
                            "mode": requested_mode,
                        },
                        timeout_seconds=int(integration.get("timeout_seconds") or 30),
                        max_response_bytes=25_000_000,
                    )
                except Exception as exc:
                    token_error = exc

            if not result.ok:
                if requested_mode == "BUS":
                    break
                continue

            rows = _json_list(json_value(result))
            if requested_mode == "BUS":
                bus_ok = True
                # Run/source health follows the required BUS request, not a
                # later optional HBLR failure.
                bus_result = result

            for row in rows:
                item = dict(row)
                item["_CMOS_REQUESTED_MODE"] = requested_mode
                collected.append(item)

    # Preserve legacy BUSDATA as operational fallback.
    if not bus_ok:
        legacy = post_urlencoded(
            "https://busdata.njtransit.com/NJTBusData.asmx/getBusVehicleDataXML2",
            {"username": username, "password": password},
            timeout_seconds=int(integration.get("timeout_seconds") or 30),
            max_response_bytes=25_000_000,
        )
        if not legacy.ok:
            detail = (
                f"; BUSDV2 auth error: {token_error}"
                if token_error is not None
                else ""
            )
            raise RuntimeError(
                (
                    legacy.error
                    or (
                        "BUSDV2 and legacy BUSDATA both failed; "
                        f"legacy HTTP {legacy.status_code}"
                    )
                )
                + detail
            )

        bus_result = legacy
        for row in _xml_records(legacy.body_text):
            item = dict(row)
            item["_CMOS_REQUESTED_MODE"] = "BUS"
            collected.append(item)

    if bus_result is None:
        raise RuntimeError("NJ TRANSIT BUSDATA produced no successful BUS response")

    if not store:
        return bus_result, len(collected), 0

    provider_id = _provider_id(conn)
    changed = 0
    kept = 0

    for row in collected:
        lat = _float(
            _pick(row, "VehicleLat", "LATITUDE", "LAT", "GPSLATITUDE")
        )
        lon = _float(
            _pick(row, "VehicleLong", "LONGITUDE", "LON", "LONG", "GPSLONGITUDE")
        )
        if not _in_bbox(lat, lon):
            continue

        vehicle = _text(
            _pick(
                row,
                "VehicleID",
                "VEHICLE_NO",
                "VEHICLE",
                "VEHICLEID",
                "BUSID",
                "BUS_ID",
            )
        )
        if not vehicle:
            continue

        route = _text(
            _pick(
                row,
                "VehicleRoute",
                "ROUTE",
                "ROUTE_ID",
                "ROUTEID",
                "ROUTE_NO",
                "ROUTENO",
            )
        )
        requested_mode = _text(row.get("_CMOS_REQUESTED_MODE")).upper()
        mode = "BUS"

        kept += 1
        changed += int(
            _upsert_asset(
                conn,
                provider_id=provider_id,
                integration_id=integration["id"],
                asset_key=f"VEHICLE:{mode}:{vehicle}",
                asset_type="VEHICLE",
                mode=mode,
                name=(
                    f"NJ TRANSIT HBLR Vehicle {vehicle}"
                    if mode == "LIGHT_RAIL"
                    else f"NJ TRANSIT Bus {vehicle}"
                ),
                short_name=vehicle,
                parent_asset_key=f"ROUTE:{route}" if route else None,
                latitude=lat,
                longitude=lon,
                metadata=row,
            )
        )

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE transit_assets "
            "SET active=false,updated_at=now() "
            "WHERE provider_id=%s "
            "AND asset_type='VEHICLE' "
            "AND mode='BUS' "
            "AND last_seen_at < now() - interval '15 minutes'",
            (provider_id,),
        )

    return bus_result, kept, changed

def _station_matches_watch(station: dict[str, Any], watches: list[dict[str, Any]]) -> bool:
    code = _norm(_pick(station, "STATION_2CHAR", "STATION_14CHAR"))
    name = _norm(_pick(station, "STATIONNAME", "STATION_NAME", "NAME"))
    hay = f"{code} {name}"
    for watch in watches:
        if watch.get("target_type") not in {"STATION", "TERMINAL", "CORRIDOR"}:
            continue
        candidates = [_norm(watch.get("target_key")), _norm(watch.get("display_name"))]
        if any(candidate and candidate in hay for candidate in candidates):
            return True
    return False


def _run_raildata(conn, integration: dict[str, Any], *, store: bool) -> tuple[TransitResult, int, int]:
    env = _required_values(integration)
    username, password = env["username_env"], env["password_env"]
    host = os.getenv("NJT_RAIL_HOST", "https://raildata.njtransit.com").rstrip("/")
    token = get_cached_rail_token(host=host, family="TrainData", username=username, password=password)

    result = post_multipart(
        f"{host}/api/TrainData/getStationList",
        {"token": token},
        timeout_seconds=int(integration.get("timeout_seconds") or 30),
        max_response_bytes=5_000_000,
    )
    if not result.ok and result.status_code in {400, 401, 403}:
        invalidate_token(host, "TrainData", username)
        token = get_cached_rail_token(
            host=host, family="TrainData", username=username, password=password, force_refresh=True
        )
        result = post_multipart(
            f"{host}/api/TrainData/getStationList",
            {"token": token},
            timeout_seconds=int(integration.get("timeout_seconds") or 30),
            max_response_bytes=5_000_000,
        )
    if not result.ok:
        raise RuntimeError(result.error or f"RailData HTTP {result.status_code}")

    stations = _json_list(json_value(result))
    if not store:
        return result, len(stations), 0

    provider_id = _provider_id(conn)
    watches = _watch_rows(conn, provider_id)
    changed = 0
    item_count = len(stations)

    watched_stations: list[dict[str, Any]] = []
    for station in stations:
        code = _text(_pick(station, "STATION_2CHAR", "STATION_14CHAR"))
        name = _text(_pick(station, "STATIONNAME", "STATION_NAME", "NAME")) or code
        if not code:
            continue
        changed += int(_upsert_asset(
            conn,
            provider_id=provider_id,
            integration_id=integration["id"],
            asset_key=f"STATION:{code}",
            asset_type="STATION",
            mode="RAIL",
            name=name,
            short_name=code,
            state="NJ",
            metadata=station,
        ))
        if _station_matches_watch(station, watches):
            watched_stations.append(station)

    for station in watched_stations[:12]:
        code = _text(_pick(station, "STATION_2CHAR", "STATION_14CHAR"))
        name = _text(_pick(station, "STATIONNAME", "STATION_NAME", "NAME")) or code
        msg_result = post_multipart(
            f"{host}/api/TrainData/getStationMSG",
            {"token": token, "station": code, "line": ""},
            timeout_seconds=int(integration.get("timeout_seconds") or 30),
            max_response_bytes=2_000_000,
        )
        if not msg_result.ok:
            raise RuntimeError(
                msg_result.error
                or f"RailData station message HTTP {msg_result.status_code} for {code}"
            )
        messages = _json_list(json_value(msg_result))
        item_count += len(messages)
        for msg in messages:
            text = _text(_pick(msg, "MSG_TEXT", "MSG_RICHTEXT", "MESSAGE", "TEXT"))
            if not text:
                continue
            msg_id = _text(_pick(msg, "MSG_ID", "ID")) or hashlib.sha256(
                f"{code}|{text}".encode()
            ).hexdigest()[:24]
            line_scope = _text(_pick(msg, "MSG_LINE_SCOPE", "LINE", "LINE_SCOPE"))
            hits = _watch_hits(f"{name} {line_scope} {text}", watches)
            score, level = _score_text(f"{name} {line_scope} {text}", hits)
            changed += int(_upsert_observation(
                conn,
                provider_id=provider_id,
                integration_id=integration["id"],
                external_key=f"STATIONMSG:{code}:{msg_id}",
                mode="RAIL",
                route_key=line_scope or None,
                route_name=line_scope or None,
                asset_key=f"STATION:{code}",
                asset_name=name,
                title=f"{name}: {text[:160]}",
                description=text,
                status="ACTIVE",
                municipality="Hoboken" if "HOBOKEN" in _norm(name) else "Secaucus" if "SECAUCUS" in _norm(name) else "New York" if "NEW YORK" in _norm(name) or "PENN" in _norm(name) else None,
                county="Hudson" if any(x in _norm(name) for x in ("HOBOKEN", "SECAUCUS")) else None,
                state="NY" if "NEW YORK" in _norm(name) or "PENN" in _norm(name) else "NJ",
                source_url=None,
                impact_score=score,
                impact_level=level,
                starts_at=_parse_datetime(_pick(msg, "MSG_PUBDATE_UTC", "MSG_PUBDATE")),
                ends_at=None,
                latitude=None,
                longitude=None,
                metadata={"message": msg, "watch_hits": hits},
            ))

    return result, item_count, changed


def _run_rail_gtfs(conn, integration: dict[str, Any], *, store: bool) -> tuple[TransitResult, int, int]:
    env = _required_values(integration)
    username, password = env["username_env"], env["password_env"]
    host = os.getenv("NJT_RAIL_HOST", "https://raildata.njtransit.com").rstrip("/")
    token = get_cached_rail_token(host=host, family="GTFSRT", username=username, password=password)
    result = post_multipart(
        f"{host}/api/GTFSRT/getGTFS",
        {"token": token},
        timeout_seconds=int(integration.get("timeout_seconds") or 60),
        max_response_bytes=50_000_000,
    )
    if not result.ok and result.status_code in {400, 401, 403}:
        invalidate_token(host, "GTFSRT", username)
        token = get_cached_rail_token(
            host=host, family="GTFSRT", username=username, password=password, force_refresh=True
        )
        result = post_multipart(
            f"{host}/api/GTFSRT/getGTFS",
            {"token": token},
            timeout_seconds=int(integration.get("timeout_seconds") or 60),
            max_response_bytes=50_000_000,
        )
    if not result.ok:
        raise RuntimeError(result.error or f"Rail GTFS HTTP {result.status_code}")
    if not store:
        return result, 1, 0
    provider_id = _provider_id(conn)
    found, changed = _import_gtfs_zip(
        conn,
        raw=_gtfs_payload_bytes(result),
        provider_id=provider_id,
        integration_id=integration["id"],
        fallback_mode="RAIL",
    )
    return result, found, changed


def _run_bus_gtfs(conn, integration: dict[str, Any], *, store: bool) -> tuple[TransitResult, int, int]:
    env = _required_values(integration)
    username, password = env["username_env"], env["password_env"]
    host = os.getenv("NJT_BUS_HOST", "https://pcsdata.njtransit.com").rstrip("/")

    configured_prefix = os.getenv("NJT_BUS_GTFS_PREFIX", "/api/GTFS").strip()
    if not configured_prefix.startswith("/"):
        configured_prefix = "/" + configured_prefix

    prefixes: list[str] = []
    for prefix in (configured_prefix, "/api/GTFS", "/api/GTFSG2"):
        if prefix not in prefixes:
            prefixes.append(prefix)

    errors: list[str] = []
    selected_result: TransitResult | None = None
    selected_raw: bytes | None = None
    max_bytes = max(int(integration.get("max_response_bytes") or 0), 90_000_000)

    for prefix in prefixes:
        family = "GTFSG2" if "GTFSG2" in prefix.upper() else "GTFS"
        try:
            token = get_cached_bus_token(host=host,family=family,username=username,password=password)
        except Exception as exc:
            errors.append(f"{prefix} auth: {exc}")
            continue

        result = post_multipart(
            f"{host}{prefix.rstrip('/')}/getGTFS",
            {"token": token},
            timeout_seconds=max(int(integration.get("timeout_seconds") or 0), 90),
            max_response_bytes=max_bytes,
        )
        if not result.ok and result.status_code in {400,401,403}:
            invalidate_token(host,family,username)
            try:
                token = get_cached_bus_token(host=host,family=family,username=username,password=password,force_refresh=True)
                result = post_multipart(
                    f"{host}{prefix.rstrip('/')}/getGTFS",
                    {"token": token},
                    timeout_seconds=max(int(integration.get("timeout_seconds") or 0), 90),
                    max_response_bytes=max_bytes,
                )
            except Exception as exc:
                errors.append(f"{prefix} reauth: {exc}")
                continue

        if not result.ok:
            errors.append(f"{prefix}: {result.error or 'HTTP ' + str(result.status_code)}")
            continue
        if result.truncated:
            errors.append(f"{prefix}: response exceeded {max_bytes} byte safety cap")
            continue
        try:
            raw = _gtfs_payload_bytes(result)
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                if "routes.txt" not in zf.namelist() or "stops.txt" not in zf.namelist():
                    raise ValueError("GTFS ZIP missing routes.txt or stops.txt")
        except Exception as exc:
            errors.append(f"{prefix}: {exc}")
            continue

        selected_result=result
        selected_raw=raw
        break

    if selected_result is None or selected_raw is None:
        raise RuntimeError("NJ TRANSIT GTFS-BUS failed across GTFS/GTFSG2 endpoints: " + " | ".join(errors))

    if not store:
        with zipfile.ZipFile(io.BytesIO(selected_raw)) as zf:
            return selected_result,len(zf.namelist()),0

    provider_id=_provider_id(conn)
    found,changed=_import_gtfs_zip(
        conn,
        raw=selected_raw,
        provider_id=provider_id,
        integration_id=integration["id"],
        fallback_mode="BUS",
        regional_stops_only=True,
    )
    return selected_result,found,changed


def _html_text(value: str) -> str:
    text=re.sub(r"<script\b[^>]*>.*?</script>"," ",value or "",flags=re.I|re.S)
    text=re.sub(r"<style\b[^>]*>.*?</style>"," ",text,flags=re.I|re.S)
    text=re.sub(r"<[^>]+>"," ",text)
    return re.sub(r"\s+"," ",unescape(text)).strip()


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str,str]]=[]
        self._href: str | None=None
        self._parts: list[str]=[]

    def handle_starttag(self,tag: str,attrs: list[tuple[str,str|None]]) -> None:
        if tag.lower()!="a":
            return
        self._href=dict(attrs).get("href")
        self._parts=[]

    def handle_data(self,data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self,tag: str) -> None:
        if tag.lower()!="a" or self._href is None:
            return
        text=re.sub(r"\s+"," "," ".join(self._parts)).strip()
        self.links.append((self._href,text))
        self._href=None
        self._parts=[]


def _advisory_links(html: str,base_url: str) -> list[tuple[str,str]]:
    parser=_AnchorCollector()
    parser.feed(html or "")
    rows=[]
    seen=set()
    for href,title in parser.links:
        if "advisorydetails.aspx" not in href.lower():
            continue
        url=urllib.parse.urljoin(base_url,href)
        if url in seen:
            continue
        seen.add(url)
        rows.append((url,title))
    return rows


def _rss_items(result: TransitResult) -> list[dict[str,Any]]:
    root=ET.fromstring(result.raw or result.body_text.encode())
    rows=[]
    for item in root.findall(".//item"):
        def value(tag: str) -> str:
            node=item.find(tag)
            return _text(node.text if node is not None else "")
        title=value("title")
        description=_html_text(value("description"))
        if not title and not description:
            continue
        rows.append({
            "title":title or description[:160],
            "description":description,
            "link":value("link"),
            "guid":value("guid"),
            "pub_date":value("pubDate"),
        })
    return rows


def _run_njt_rss(conn,integration: dict[str,Any],*,store: bool) -> tuple[TransitResult,int,int]:
    result=get_bytes(
        integration["endpoint_url"],
        timeout_seconds=int(integration.get("timeout_seconds") or 30),
        max_response_bytes=int(integration.get("max_response_bytes") or 4_000_000),
    )
    if not result.ok:
        raise RuntimeError(result.error or f"NJT RSS HTTP {result.status_code}")
    rows=_rss_items(result)
    if not store:
        return result,len(rows),0

    cfg=_parser_config(integration)
    provider_id=_provider_id(conn,str(cfg.get("provider_key") or "NJ_TRANSIT"))
    watches=_watch_rows(conn,provider_id)
    mode=str(cfg.get("mode") or "BUS").upper()
    include_terms=[_norm(x) for x in (cfg.get("include_terms") or []) if _norm(x)]
    changed=0
    kept=0
    for row in rows:
        text=f"{row['title']} {row['description']}"
        hay=_norm(text)
        hits=_watch_hits(text,watches)
        if include_terms and not any(term in hay for term in include_terms):
            continue
        if not include_terms and not hits:
            continue
        score,level=_score_text(text,hits)
        link=row["link"] or integration["endpoint_url"]
        external=row["guid"] or link or hashlib.sha256(text.encode()).hexdigest()
        published=None
        if row["pub_date"]:
            try:
                published=parsedate_to_datetime(row["pub_date"])
                if published.tzinfo is None:
                    published=published.replace(tzinfo=timezone.utc)
            except Exception:
                published=None
        municipality=None
        if re.search(r"\b(WEEHAWKEN|PORT IMPERIAL|LINCOLN TUNNEL)\b",hay):
            municipality="Weehawken"
        elif "HOBOKEN" in hay:
            municipality="Hoboken"
        kept+=1
        changed+=int(_upsert_observation(
            conn,
            provider_id=provider_id,
            integration_id=integration["id"],
            external_key=f"RSS:{external}",
            mode=mode,
            route_key=None,
            route_name=None,
            asset_key=None,
            asset_name=hits[0] if hits else None,
            title=row["title"],
            description=row["description"][:3000] or None,
            status="ACTIVE",
            municipality=municipality,
            county="Hudson" if municipality else None,
            state="NJ",
            source_url=link,
            impact_score=score,
            impact_level=level,
            starts_at=published,
            ends_at=None,
            latitude=None,
            longitude=None,
            metadata={"watch_hits":hits,"published":row["pub_date"],"feed":integration["integration_key"]},
        ))
    return result,kept,changed


PATH_STATION_NAMES={
    "NWK":"Newark","HAR":"Harrison","JSQ":"Journal Square","GRV":"Grove Street",
    "NEW":"Newport","EXP":"Exchange Place","HOB":"Hoboken","WTC":"World Trade Center",
    "CHR":"Christopher Street","09S":"9th Street","14S":"14th Street","23S":"23rd Street","33S":"33rd Street",
}


def _path_rows(payload: Any) -> list[dict[str,Any]]:
    if not isinstance(payload,dict) or not isinstance(payload.get("results"),list):
        raise ValueError("PATH realtime payload missing results")
    rows=[]
    for station in payload["results"]:
        if not isinstance(station,dict):
            continue
        code=_text(station.get("consideredStation")).upper()
        if not code:
            continue
        messages=[]
        for dest in station.get("destinations") or []:
            if not isinstance(dest,dict):
                continue
            label=_text(dest.get("label"))
            for msg in dest.get("messages") or []:
                if not isinstance(msg,dict):
                    continue
                item=dict(msg)
                item["_direction"]=label
                messages.append(item)
        rows.append({"code":code,"messages":messages})
    return rows


def _run_path_realtime(conn,integration: dict[str,Any],*,store: bool) -> tuple[TransitResult,int,int]:
    result=get_bytes(
        integration["endpoint_url"],
        timeout_seconds=int(integration.get("timeout_seconds") or 30),
        max_response_bytes=int(integration.get("max_response_bytes") or 4_000_000),
    )
    if not result.ok:
        raise RuntimeError(result.error or f"PATH realtime HTTP {result.status_code}")
    rows=_path_rows(json_value(result))
    if not store:
        return result,len(rows),0

    cfg=_parser_config(integration)
    provider_id=_provider_id(conn,str(cfg.get("provider_key") or "PATH"))
    watches=_watch_rows(conn,provider_id)
    configured={str(x).upper() for x in (cfg.get("station_codes") or ["HOB","NEW","EXP","JSQ","WTC","33S"])}
    changed=0
    kept=0
    for station in rows:
        code=station["code"]
        messages=station["messages"]
        if code not in configured and not any("DELAY" in _norm(m.get("arrivalTimeMessage")) for m in messages):
            continue
        if not messages:
            continue
        name=PATH_STATION_NAMES.get(code,code)
        parts=[]
        delayed=False
        latest=None
        for msg in messages[:8]:
            arrival=_text(msg.get("arrivalTimeMessage"))
            headsign=_text(msg.get("headSign"))
            direction=_text(msg.get("_direction"))
            if "DELAY" in _norm(arrival):
                delayed=True
            updated=_parse_datetime(msg.get("lastUpdated"))
            if updated and (latest is None or updated>latest):
                latest=updated
            detail=f"{headsign}: {arrival}" if headsign else arrival
            if direction:
                detail=f"{direction} {detail}"
            if detail:
                parts.append(detail)
        description="; ".join(parts)
        text=f"PATH {name} {code} {description}"
        hits=_watch_hits(text,watches)
        score,level=_score_text(text,hits)
        if delayed and score<45:
            score,level=45,"WATCH"
        kept+=1
        changed+=int(_upsert_observation(
            conn,
            provider_id=provider_id,
            integration_id=integration["id"],
            external_key=f"ARRIVALS:{code}",
            mode="RAIL",
            route_key=None,
            route_name=None,
            asset_key=None,
            asset_name=f"PATH {name}",
            title=f"PATH {name} live arrivals",
            description=description[:3000],
            status="DELAYED" if delayed else "ACTIVE",
            municipality="Hoboken" if code=="HOB" else "Jersey City" if code in {"NEW","EXP","JSQ","GRV"} else "New York" if code in {"WTC","33S","23S","14S","09S","CHR"} else None,
            county="Hudson" if code in {"HOB","NEW","EXP","JSQ","GRV"} else None,
            state="NJ" if code in {"NWK","HAR","JSQ","GRV","NEW","EXP","HOB"} else "NY",
            source_url="https://www.panynj.gov/path/en/index.html",
            impact_score=score,
            impact_level=level,
            starts_at=latest,
            ends_at=None,
            latitude=None,
            longitude=None,
            metadata={"station_code":code,"messages":messages[:8],"watch_hits":hits},
        ))
    return result,kept,changed


def _run_public_gtfs(conn,integration: dict[str,Any],*,store: bool) -> tuple[TransitResult,int,int]:
    result=get_bytes(
        integration["endpoint_url"],
        timeout_seconds=int(integration.get("timeout_seconds") or 60),
        max_response_bytes=int(integration.get("max_response_bytes") or 25_000_000),
    )
    if not result.ok:
        raise RuntimeError(result.error or f"GTFS HTTP {result.status_code}")
    if result.truncated:
        raise RuntimeError("GTFS response exceeded configured response cap")
    raw=_gtfs_payload_bytes(result)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names=zf.namelist()
        if "routes.txt" not in names or "stops.txt" not in names:
            raise ValueError("GTFS ZIP missing routes.txt or stops.txt")
    if not store:
        return result,len(names),0
    cfg=_parser_config(integration)
    provider_key=str(cfg.get("provider_key") or "").upper()
    if not provider_key:
        raise ValueError("TRANSIT_GTFS_URL requires parser_config.provider_key")
    fallback_mode=str(cfg.get("fallback_mode") or "RAIL").upper()
    force_mode=str(cfg.get("force_mode") or "").upper() or None
    allowed_modes={str(x).upper() for x in (cfg.get("allowed_modes") or [])}
    provider_id=_provider_id(conn,provider_key)
    found,changed=_import_gtfs_zip(
        conn,
        raw=raw,
        provider_id=provider_id,
        integration_id=integration["id"],
        fallback_mode=fallback_mode,
        force_mode=force_mode,
        allowed_modes=allowed_modes or None,
    )
    return result,found,changed


def _run_nyw_advisories(conn,integration: dict[str,Any],*,store: bool) -> tuple[TransitResult,int,int]:
    result=get_bytes(
        integration["endpoint_url"],
        timeout_seconds=int(integration.get("timeout_seconds") or 30),
        max_response_bytes=int(integration.get("max_response_bytes") or 4_000_000),
    )
    if not result.ok:
        raise RuntimeError(result.error or f"NY Waterway advisories HTTP {result.status_code}")
    links=_advisory_links(result.body_text,result.final_url)
    if not store:
        return result,len(links),0
    cfg=_parser_config(integration)
    provider_id=_provider_id(conn,str(cfg.get("provider_key") or "NY_WATERWAY"))
    watches=_watch_rows(conn,provider_id)
    changed=0
    kept=0
    seen=set()
    for url,root_title in links[:25]:
        if url in seen:
            continue
        seen.add(url)
        detail=get_bytes(url,timeout_seconds=int(integration.get("timeout_seconds") or 30),max_response_bytes=2_000_000)
        if not detail.ok:
            continue
        visible=_html_text(detail.body_text)
        upper=visible.upper()
        if "PORT IMPERIAL" not in upper:
            continue
        title=root_title.strip() or "NY Waterway Port Imperial advisory"
        idx=upper.find("PORT IMPERIAL")
        excerpt=visible[max(0,idx-300):idx+1600] if idx>=0 else visible[:1900]
        text=f"{title} {excerpt} Port Imperial Weehawken"
        hits=_watch_hits(text,watches)
        score,level=_score_text(text,hits)
        m=re.search(r"[?&]aid=([^&#]+)",url,re.I)
        external=m.group(1) if m else hashlib.sha256(url.encode()).hexdigest()
        dm=re.search(r"\b\d{1,2}/\d{1,2}/20\d{2}\b",visible)
        kept+=1
        changed+=int(_upsert_observation(
            conn,
            provider_id=provider_id,
            integration_id=integration["id"],
            external_key=f"ADVISORY:{external}",
            mode="FERRY",
            route_key="PORT_IMPERIAL",
            route_name="Port Imperial / Weehawken",
            asset_key=None,
            asset_name="Port Imperial / Weehawken",
            title=title,
            description=excerpt[:3000] or None,
            status="ACTIVE",
            municipality="Weehawken",
            county="Hudson",
            state="NJ",
            source_url=url,
            impact_score=score,
            impact_level=level,
            starts_at=None,
            ends_at=None,
            latitude=None,
            longitude=None,
            metadata={"published_date":dm.group(0) if dm else None,"watch_hits":hits},
        ))
    return result,kept,changed

def run_transit_integration(
    integration: dict[str, Any],
    *,
    run_type: str = "POLL",
    parse_and_store: bool = True,
) -> dict[str, Any]:
    if not is_transit_adapter(integration):
        raise ValueError("Not a custom transit integration")
    with db_conn() as conn:
        run_id = _record_run_start(conn, integration["id"], run_type)
        conn.commit()

    result: TransitResult | None = None
    try:
        with db_conn() as conn:
            adapter = str(integration["adapter_type"]).upper()
            if adapter == "NJT_BUSDATA":
                result, items, changed = _run_busdata(conn, integration, store=parse_and_store)
            elif adapter == "NJT_RAILDATA":
                result, items, changed = _run_raildata(conn, integration, store=parse_and_store)
            elif adapter == "NJT_RAIL_GTFS":
                result, items, changed = _run_rail_gtfs(conn, integration, store=parse_and_store)
            elif adapter == "NJT_BUS_GTFS":
                result, items, changed = _run_bus_gtfs(conn, integration, store=parse_and_store)
            elif adapter == "NJT_RSS":
                result, items, changed = _run_njt_rss(conn, integration, store=parse_and_store)
            elif adapter == "PATH_REALTIME":
                result, items, changed = _run_path_realtime(conn, integration, store=parse_and_store)
            elif adapter == "TRANSIT_GTFS_URL":
                result, items, changed = _run_public_gtfs(conn, integration, store=parse_and_store)
            elif adapter == "NYW_ADVISORIES":
                result, items, changed = _run_nyw_advisories(conn, integration, store=parse_and_store)
            else:
                raise ValueError(f"Unsupported transit adapter {adapter}")

            _finish_run(
                conn,
                run_id,
                status="OK",
                result=result,
                items_found=items,
                items_changed=changed,
            )
            _update_health(conn, integration, ok=True, error=None, item_count=items)
            conn.commit()
        return {
            "ok": True,
            "run_id": str(run_id),
            "result": result,
            "items": items,
            "changed": changed,
            "events": [],
        }
    except Exception as exc:
        error = str(exc)
        with db_conn() as conn:
            _finish_run(conn, run_id, status="ERROR", result=result, error_message=error)
            _update_health(conn, integration, ok=False, error=error, item_count=0)
            conn.commit()
        return {
            "ok": False,
            "run_id": str(run_id),
            "result": result,
            "items": 0,
            "changed": 0,
            "events": [],
            "error": error,
        }



def mark_stale_transit_observations() -> int:
    """Clear live transit observations that a polling feed no longer returns."""
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH stale AS (
              UPDATE transit_observations o
              SET active=false,
                  status='CLEARED',
                  alert_pending=false,
                  updated_at=now()
              FROM integrations i
              WHERE o.source_integration_id=i.id
                AND o.active=true
                AND i.category='TRANSIT'
                AND i.adapter_type = ANY(%s)
                AND EXISTS (
                  SELECT 1
                  FROM integration_runs r
                  WHERE r.integration_id=i.id
                    AND r.status='OK'
                    AND r.run_type IN ('POLL','MANUAL')
                    AND r.finished_at IS NOT NULL
                    AND r.started_at > o.last_seen_at
                )
                AND o.last_seen_at <
                    now() - make_interval(
                      secs => GREATEST(COALESCE(i.poll_seconds,180) * 4, 900)
                    )
              RETURNING o.id
            ),
            resolved AS (
              UPDATE alerts a
              SET status='RESOLVED',
                  event_action='RESOLVE',
                  expires_at=COALESCE(a.expires_at,now()),
                  updated_at=now()
              WHERE a.source='TRANSIT_INTELLIGENCE'
                AND EXISTS (
                  SELECT 1
                  FROM stale s
                  WHERE a.metadata->>'transit_observation_id'=s.id::text
                )
              RETURNING a.id
            )
            SELECT count(*) AS cleared
            FROM stale
            """,
            (list(TRANSIT_ADAPTERS),),
        )
        row = cur.fetchone()
        conn.commit()
    return int(row["cleared"] if row else 0)


def due_transit_integrations(limit: int = 12) -> list[dict[str, Any]]:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.*
            FROM integrations i
            LEFT JOIN LATERAL (
              SELECT max(started_at) AS last_run
              FROM integration_runs r
              WHERE r.integration_id=i.id AND r.run_type IN ('POLL','MANUAL')
            ) lr ON true
            WHERE i.active=true
              AND i.category='TRANSIT'
              AND i.adapter_type = ANY(%s)
              AND (lr.last_run IS NULL OR lr.last_run <= now() - make_interval(secs => i.poll_seconds))
            ORDER BY lr.last_run NULLS FIRST,i.integration_key
            LIMIT %s
            """,
            (list(TRANSIT_ADAPTERS), limit),
        )
        return cur.fetchall()


def run_due_transit_integrations(limit: int = 12) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for integration in due_transit_integrations(limit):
        outcome = run_transit_integration(integration, run_type="POLL", parse_and_store=True)
        summaries.append(
            {
                "integration_key": integration["integration_key"],
                "name": integration["name"],
                "ok": outcome["ok"],
                "events": 0,
                "items": outcome.get("items") or 0,
                "changed": outcome.get("changed") or 0,
                "error": outcome.get("error"),
            }
        )
    return summaries
