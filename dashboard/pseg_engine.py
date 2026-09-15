"""Consolidated PSEG ingestion for the existing City Manager OS alert path.

Counts come from the established PSEG summary feed. Provider map data is used
only for approximate outage-area coordinates; local PostGIS supplies nearby
parcel, address-corridor, and reference context. No runtime geocoder is used.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row


RELEASE_ID = "issue-60-pseg-spatial-alerts-v1"
PSEG_MAP_URL = "https://outagecenter.pseg.com/"
PSEG_SUMMARY_URL = (
    "https://func-psegnj-outages-prod-001.azurewebsites.net/"
    "api/outagecenter_getOutageSummary"
)
KUBRA_STATE_URL = (
    "https://kubra.io/stormcenter/api/v1/stormcenters/"
    "79d82478-c10a-4956-a045-89de3cda618c/views/"
    "bdb7f5be-1c1a-46fa-862e-ebdf21851d2e/currentState?preview=false"
)
KUBRA_ROOT = "https://kubra.io/"
LOCAL_ZONE = ZoneInfo("America/New_York")
HUDSON_BOUNDS = (40.62, -74.18, 40.91, -73.86)
HUDSON_TOWNS = {
    "BAYONNE": "BAYONNE",
    "EAST NEWARK": "EAST NEWARK",
    "GUTTENBERG": "GUTTENBERG",
    "HARRISON": "HARRISON",
    "HOBOKEN": "HOBOKEN",
    "JERSEY CITY": "JERSEY CITY",
    "KEARNY": "KEARNY",
    "NORTH BERGEN": "NORTH BERGEN",
    "SECAUCUS": "SECAUCUS",
    "UNION CITY": "UNION CITY",
    "WEEHAWKEN": "WEEHAWKEN",
    "WEST NEW YORK": "WEST NEW YORK",
}

_SPACE_RE = re.compile(r"\s+")
_SAFE_PROVIDER_PATH_RE = re.compile(r"^[A-Za-z0-9_{}./-]+$")
_HOUSE_NUMBER_RE = re.compile(r"^\s*\d+[A-Z]?(?:-\d+[A-Z]?)?\s+", re.I)


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


def _number(value: Any, default: int | float | None = None):
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _integer(value: Any, default: int | None = 0) -> int | None:
    number = _number(value)
    return max(0, int(round(number))) if number is not None else default


def _text(value: Any) -> str | None:
    value = str(value or "").strip()
    return value or None


def _town_key(value: Any) -> str:
    value = str(value or "").upper().replace(".", " ").replace(",", " ")
    value = re.sub(r"\bTOWNSHIP\b", "TWP", value)
    value = re.sub(r"\bBOROUGH\b", "BORO", value)
    return _SPACE_RE.sub(" ", value).strip()


def _municipality_key(value: Any, county: Any = "") -> str:
    key = _town_key(value)
    if _town_key(county) == "HUDSON":
        base = re.sub(r"\s+(?:TWP|TOWN|CITY|BORO|VILLAGE)$", "", key).strip()
        if base in HUDSON_TOWNS:
            return base
    return key


def _display_town(value: str) -> str:
    small = {"Twp", "Boro", "City", "Town", "Village"}
    return " ".join(word if word in small else word.capitalize() for word in value.title().split())


def _slug(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", str(value or "").upper()).strip("-") or "UNKNOWN"


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text or text.upper() in {"ETR-NULL", "UNKNOWN", "PENDING", "TBD"}:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _display_time(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return "--"
    parsed = _parse_time(value)
    if parsed:
        return parsed.astimezone(LOCAL_ZONE).strftime("%-I:%M %p")
    return raw.upper()


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _mapping_center_url() -> str | None:
    origin = os.getenv("CMOS_PUBLIC_ORIGIN", "").strip().rstrip("/")
    return f"{origin}/map" if origin else None


def _read_json(url: str, *, timeout: int = 25, allow_404: bool = False) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "CityManagerOS/1.0 PSEG municipal operations monitor",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if int(response.status) != 200:
                raise RuntimeError(f"provider returned HTTP {response.status}")
            body = response.read(16_000_001)
            if len(body) > 16_000_000:
                raise RuntimeError("provider response exceeded 16 MB")
            if response.headers.get("Content-Encoding", "").lower() == "gzip" or body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
                if len(body) > 16_000_000:
                    raise RuntimeError("decompressed provider response exceeded 16 MB")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return None
        raise RuntimeError(f"provider returned HTTP {exc.code}") from exc
    except (json.JSONDecodeError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"provider response was unavailable or invalid: {exc}") from exc


def decode_polyline(value: str) -> list[tuple[float, float]]:
    """Decode a Google encoded polyline into latitude/longitude pairs."""
    points: list[tuple[float, float]] = []
    latitude = longitude = index = 0
    while index < len(value):
        deltas: list[int] = []
        for _ in range(2):
            result = shift = 0
            while True:
                if index >= len(value):
                    raise ValueError("truncated encoded polyline")
                byte = ord(value[index]) - 63
                index += 1
                if byte < 0:
                    raise ValueError("invalid encoded polyline")
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
                if shift > 60:
                    raise ValueError("invalid encoded polyline")
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        points.append((latitude / 100000.0, longitude / 100000.0))
    return points


def parse_summary(payload: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    content = payload.get("content") if isinstance(payload, Mapping) else None
    towns = content.get("towns") if isinstance(content, Mapping) else None
    if not isinstance(towns, list) or len(towns) < 200:
        raise ValueError("PSEG summary did not contain a complete New Jersey town set")
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in towns:
        if not isinstance(raw, Mapping):
            continue
        county = _town_key(raw.get("displayCounty") or raw.get("county"))
        municipality = _municipality_key(raw.get("displayTown") or raw.get("town"), county)
        if not county or not municipality:
            continue
        key = (county, municipality)
        if key in seen:
            raise ValueError(f"duplicate PSEG municipality record: {county}/{municipality}")
        seen.add(key)
        served = _number(raw.get("customersServed"))
        customers_out = _integer(raw.get("customersOut")) or 0
        percent = _number(raw.get("percentOut"))
        if percent is None and served and served > 0:
            percent = customers_out * 100.0 / served
        records.append(
            {
                "county": county,
                "municipality": municipality,
                "customers_out": customers_out,
                "customers_served": _integer(served) if served is not None else None,
                "percent_out": percent,
                "outage_count": _integer(raw.get("numberOfJobs"), None),
                "etr": _text(raw.get("etrEarliest")),
                "etr_latest": _text(raw.get("etrLatest")),
                "started_at": _parse_time(raw.get("earliestCurrentOutage")),
                "updated_at": _parse_time(raw.get("updatedAt")),
                "jobs_working": _integer(raw.get("numberOfJobsBeingWorked"), None),
                "circuits": _integer(raw.get("numberOfCircuits"), None),
                "pending_damage": _integer(raw.get("pendingDamage"), None),
                "confirmed_poles": _integer(raw.get("confirmedPoles"), None),
                "confirmed_trees": _integer(raw.get("confirmedTrees"), None),
                "confirmed_roads": _integer(
                    raw.get("confirmedRoadsBlocked")
                    if raw.get("confirmedRoadsBlocked") is not None
                    else raw.get("roadsBlocked", raw.get("confirmedRoads")),
                    None,
                ),
                "raw": dict(raw),
            }
        )
    if len(records) < 200 or len({row["county"] for row in records}) < 10:
        raise ValueError("PSEG summary failed completeness validation")
    missing_hudson = set(HUDSON_TOWNS) - {
        row["municipality"] for row in records if row["county"] == "HUDSON"
    }
    if missing_hudson:
        raise ValueError("PSEG summary omitted Hudson municipalities: " + ", ".join(sorted(missing_hudson)))
    updated_values = [
        str(row.get("updatedAt"))
        for row in towns
        if isinstance(row, Mapping) and row.get("updatedAt")
    ]
    generation = _text(content.get("generatedAt")) or _text(max(updated_values, default="")) or "unknown"
    return generation, records


def parse_thematic_points(payload: Mapping[str, Any]) -> dict[tuple[str, str], tuple[float, float]]:
    rows = payload.get("file_data") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("PSEG municipality map data was invalid")
    points: dict[tuple[str, str], tuple[float, float]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        desc = row.get("desc") if isinstance(row.get("desc"), Mapping) else {}
        hierarchy = desc.get("hierarchy") if isinstance(desc.get("hierarchy"), Mapping) else {}
        county = _town_key(hierarchy.get("county"))
        municipality = _municipality_key(desc.get("name") or row.get("title"), county)
        encoded = ((row.get("geom") or {}).get("p") or [None])[0]
        if not county or not municipality or not isinstance(encoded, str):
            continue
        try:
            decoded = decode_polyline(encoded)
        except ValueError:
            continue
        if decoded:
            points[(county, municipality)] = decoded[0]
    return points


def _tile_xy(latitude: float, longitude: float, zoom: int) -> tuple[int, int]:
    latitude = max(-85.05112878, min(85.05112878, latitude))
    scale = 2**zoom
    x = int((longitude + 180.0) / 360.0 * scale)
    sine = math.sin(math.radians(latitude))
    y = int((0.5 - math.log((1 + sine) / (1 - sine)) / (4 * math.pi)) * scale)
    return x, y


def _quadkey(x: int, y: int, zoom: int) -> str:
    digits = []
    for level in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (level - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        digits.append(str(digit))
    return "".join(digits)


def hudson_quadkeys(zoom: int = 10) -> list[str]:
    south, west, north, east = HUDSON_BOUNDS
    min_x, max_y = _tile_xy(south, west, zoom)
    max_x, min_y = _tile_xy(north, east, zoom)
    return [_quadkey(x, y, zoom) for x in range(min_x, max_x + 1) for y in range(min_y, max_y + 1)]


def _safe_provider_path(value: Any, name: str) -> str:
    value = str(value or "").strip().strip("/")
    if not value or ".." in value or not _SAFE_PROVIDER_PATH_RE.fullmatch(value):
        raise ValueError(f"PSEG provider returned an unsafe {name} path")
    return value


def _fetch_map_data() -> tuple[dict[tuple[str, str], tuple[float, float]], list[dict[str, Any]]]:
    state = _read_json(KUBRA_STATE_URL)
    data = state.get("data") if isinstance(state, Mapping) else None
    if not isinstance(data, Mapping):
        raise ValueError("PSEG map state did not contain data paths")
    interval_path = _safe_provider_path(data.get("interval_generation_data"), "interval data")
    cluster_path = _safe_provider_path(data.get("cluster_interval_generation_data"), "cluster data")
    thematic = _read_json(f"{KUBRA_ROOT}{interval_path}/public/thematic-5/thematic_areas.json")
    municipality_points = parse_thematic_points(thematic)

    def fetch_tile(q: str):
        qkh = q[-3:][::-1]
        path = cluster_path.replace("{qkh}", qkh)
        url = f"{KUBRA_ROOT}{path}/public/cluster-1/{q}.json"
        return _read_json(url, timeout=15, allow_404=True)

    with ThreadPoolExecutor(max_workers=6) as pool:
        tiles = list(pool.map(fetch_tile, hudson_quadkeys()))
    incidents: dict[str, dict[str, Any]] = {}
    for tile in tiles:
        rows = tile.get("file_data") if isinstance(tile, Mapping) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            desc = row.get("desc") if isinstance(row.get("desc"), Mapping) else {}
            encoded = ((row.get("geom") or {}).get("p") or [None])[0]
            if not isinstance(encoded, str):
                continue
            try:
                decoded = decode_polyline(encoded)
            except ValueError:
                continue
            if not decoded:
                continue
            latitude, longitude = decoded[0]
            south, west, north, east = HUDSON_BOUNDS
            if not (south <= latitude <= north and west <= longitude <= east):
                continue
            incident_id = _text(desc.get("inc_id")) or f"cluster:{row.get('id')}"
            incidents[incident_id] = {
                "incident_id": incident_id,
                "latitude": latitude,
                "longitude": longitude,
                "customers_out": _integer((desc.get("cust_a") or {}).get("val")) or 0,
                "outage_count": _integer(desc.get("n_out"), 1) or 1,
                "etr": _text(desc.get("etr")),
                "started_at": _text(desc.get("start_time")),
                "cluster": bool(desc.get("cluster")),
            }
    return municipality_points, list(incidents.values())


def _street_only(value: Any) -> str | None:
    value = _text(value)
    if not value:
        return None
    value = _HOUSE_NUMBER_RE.sub("", value).split(",", 1)[0].strip()
    return value.title() if value else None


def enrich_hudson_incidents(conn, incidents: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    if not incidents:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH incoming AS (
              SELECT *,ST_SetSRID(ST_MakePoint(longitude,latitude),4326) AS geom
              FROM jsonb_to_recordset(%s::jsonb) AS x(
                incident_id text,latitude double precision,longitude double precision,
                customers_out integer,outage_count integer,etr text,started_at text,cluster boolean
              )
            )
            SELECT i.incident_id,i.latitude,i.longitude,i.customers_out,i.outage_count,
                   i.etr,i.started_at,i.cluster,
                   coalesce(a.municipality,p.municipality,r.municipality) AS municipality,
                   p.parcel_objectid,p.parcel_id,p.property_location,
                   a.fulladdr AS nearest_address,a.distance_ft AS address_distance_ft,
                   r.entity_type,r.canonical_name AS reference_name,r.distance_ft AS reference_distance_ft
            FROM incoming i
            LEFT JOIN LATERAL gis_parcel_for_point(i.latitude,i.longitude,25.0) p ON true
            LEFT JOIN LATERAL (
              SELECT fulladdr,post_comm AS municipality,
                     ST_Distance(geom::geography,i.geom::geography)/0.3048 AS distance_ft
              FROM gis_addresses
              WHERE geom IS NOT NULL AND coalesce(status,'A')='A'
                AND geom && ST_Expand(i.geom,0.012)
                AND ST_DWithin(geom::geography,i.geom::geography,3000*0.3048)
              ORDER BY geom <-> i.geom,objectid LIMIT 1
            ) a ON true
            LEFT JOIN LATERAL (
              SELECT entity_type,canonical_name,municipality,
                     ST_Distance(centroid::geography,i.geom::geography)/0.3048 AS distance_ft
              FROM spatial_reference_entities
              WHERE active=true AND centroid && ST_Expand(i.geom,0.025)
                AND ST_DWithin(centroid::geography,i.geom::geography,5280*0.3048)
              ORDER BY importance_tier,centroid <-> i.geom,canonical_name LIMIT 1
            ) r ON true
            ORDER BY i.incident_id
            """,
            (json.dumps(incidents),),
        )
        rows = cur.fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        municipality = _municipality_key(row.get("municipality"), "HUDSON")
        if municipality not in HUDSON_TOWNS:
            continue
        label = None
        kind = "MUNICIPALITY"
        if row.get("reference_name") and float(row.get("reference_distance_ft") or 999999) <= 1500:
            label = str(row["reference_name"]).strip()
            kind = str(row.get("entity_type") or "REFERENCE")
        if not label:
            street = _street_only(row.get("nearest_address") or row.get("property_location"))
            if street:
                label = f"{street} corridor"
                kind = "STREET_CORRIDOR"
        if not label and row.get("parcel_objectid"):
            label = "mapped parcel area"
            kind = "PARCEL_AREA"
        if not label:
            label = _display_town(municipality)
        row["municipality"] = municipality
        row["label"] = label
        row["location_kind"] = kind
        row["approximate"] = True
        row["provenance"] = ["PSEG_PROVIDER_MAP_CLUSTER", "LOCAL_POSTGIS"]
        grouped.setdefault(municipality, []).append(row)
    for values in grouped.values():
        values.sort(key=lambda row: (-int(row.get("customers_out") or 0), str(row.get("incident_id"))))
    return grouped


def statewide_eligible(record: Mapping[str, Any], settings: Mapping[str, Any]) -> bool:
    if not settings.get("statewide_enabled") or _town_key(record.get("county")) == "HUDSON":
        return False
    counties = {_town_key(value) for value in (settings.get("counties") or [])}
    if counties and _town_key(record.get("county")) not in counties:
        return False
    count_match = int(record.get("customers_out") or 0) >= int(settings.get("minimum_customers") or 500)
    percent_limit = _number(settings.get("minimum_percent"))
    percent_match = percent_limit is not None and float(record.get("percent_out") or 0) >= percent_limit
    return count_match or percent_match


def statewide_transition(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> str | None:
    if previous is None:
        return None
    was_eligible = bool(previous.get("eligible"))
    is_eligible = bool(current.get("eligible"))
    old_count = int(previous.get("customers_out") or 0)
    new_count = int(current.get("customers_out") or 0)
    if is_eligible and not was_eligible:
        return "THRESHOLD_CROSSING"
    baseline_value = previous.get("material_baseline_customers_out")
    material_baseline = old_count if baseline_value is None else int(baseline_value)
    if is_eligible and was_eligible and new_count - material_baseline >= int(
        settings.get("material_increase_customers") or 250
    ):
        return "MATERIAL_INCREASE"
    if (
        settings.get("statewide_enabled")
        and settings.get("restoration_notifications")
        and was_eligible
        and new_count == 0
        and previous.get("last_alert_created_at")
    ):
        return "RESTORATION"
    return None


def _record_hash(record: Mapping[str, Any], scope: str) -> str:
    fields = {
        "customers_out": record.get("customers_out"),
        "customers_served": record.get("customers_served"),
        "percent_out": record.get("percent_out"),
        "outage_count": record.get("outage_count"),
    }
    if scope == "HUDSON":
        fields.update(
            {
                "etr": record.get("etr"),
                "etr_latest": record.get("etr_latest"),
                "started_at": record.get("started_at"),
                "jobs_working": record.get("jobs_working"),
                "circuits": record.get("circuits"),
                "pending_damage": record.get("pending_damage"),
                "confirmed_poles": record.get("confirmed_poles"),
                "confirmed_trees": record.get("confirmed_trees"),
                "confirmed_roads": record.get("confirmed_roads"),
            }
        )
    return _hash(fields)


def _cycle_token(record: Mapping[str, Any], now: datetime) -> str:
    started = record.get("started_at")
    if isinstance(started, datetime):
        value = started.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    else:
        value = now.strftime("%Y%m%dT%H%M%SZ")
    return _slug(value)


def _context_for_record(
    record: Mapping[str, Any],
    municipality_points: Mapping[tuple[str, str], tuple[float, float]],
    hudson_incidents: Mapping[str, list[dict[str, Any]]],
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    municipality = str(record["municipality"])
    county = str(record["county"])
    if county == "HUDSON" and hudson_incidents.get(municipality):
        top = hudson_incidents[municipality][0]
        return {
            "label": top["label"],
            "kind": top["location_kind"],
            "latitude": top["latitude"],
            "longitude": top["longitude"],
            "parcel_id": top.get("parcel_id"),
            "incident_id": top.get("incident_id"),
            "areas": [
                {
                    "label": item["label"],
                    "kind": item["location_kind"],
                    "customers_out": item.get("customers_out"),
                    "incident_id": item.get("incident_id"),
                }
                for item in hudson_incidents[municipality][:3]
            ],
            "confidence": 0.60,
            "approximate": True,
            "not_customer_specific": True,
            "provenance": ["PSEG_PROVIDER_MAP_CLUSTER", "LOCAL_POSTGIS"],
        }
    point = municipality_points.get((county, municipality))
    if point:
        return {
            "label": f"{_display_town(municipality)} municipality area",
            "kind": "MUNICIPALITY_AREA",
            "latitude": point[0],
            "longitude": point[1],
            "confidence": 0.35,
            "approximate": True,
            "not_customer_specific": True,
            "provenance": ["PSEG_PROVIDER_MAP_MUNICIPALITY"],
        }
    old_context = (previous or {}).get("location_context") or {}
    if isinstance(old_context, str):
        try:
            old_context = json.loads(old_context)
        except json.JSONDecodeError:
            old_context = {}
    if old_context:
        return dict(old_context)
    return {
        "label": f"{_display_town(municipality)} municipality",
        "kind": "MUNICIPALITY",
        "confidence": 0.20,
        "approximate": True,
        "not_customer_specific": True,
        "provenance": ["LOCAL_GEO_RESOLVER_PENDING"],
    }


def _standard_alert(
    *, alert_id: str, title: str, message: str, event_action: str, status: str,
    priority: int, county: str | None, municipality: str | None,
    location: Mapping[str, Any], metadata: Mapping[str, Any], observed_at: datetime | None,
    route_pending: bool, tags: Iterable[str] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    mapping_center_url = _mapping_center_url()
    if mapping_center_url:
        message = f"{message}\nMapping Center: {mapping_center_url}"
    payload = {
        "schema_version": "1.0",
        "alert_id": alert_id,
        "source": "PSEG",
        "source_event_id": alert_id.rsplit(":", 1)[-1],
        "category": "UTILITY",
        "subtype": "POWER_OUTAGE",
        "status": status,
        "event_action": event_action,
        "title": title,
        "message": message,
        "priority": max(1, min(priority, 5)),
        "county": county or "",
        "municipality": municipality or "",
        "location": dict(location),
        "tags": list(tags or (["white_check_mark", "zap"] if status == "RESOLVED" else ["zap"])),
        "click_url": PSEG_MAP_URL,
        "source_url": PSEG_MAP_URL,
        "observed_at": (observed_at or now).isoformat(),
        "source_updated_at": now.isoformat(),
        "received_at": now.isoformat(),
        "expires_at": None,
        "metadata": {
            **dict(metadata),
            "location_approximate": True,
            "location_not_customer_specific": True,
            "mapping_center_path": "/map",
            "mapping_center_url": mapping_center_url,
            "_cmos": {
                "route_pending": bool(route_pending),
                "route_marked_at": now.isoformat() if route_pending else None,
                "release_id": RELEASE_ID,
            },
        },
    }
    payload["search_text"] = " ".join(
        str(value) for value in (
            payload["source"], payload["category"], payload["subtype"], payload["title"],
            payload["message"], county, municipality, *payload["tags"]
        ) if value
    )
    return payload


def build_hudson_alert(
    record: Mapping[str, Any], previous: Mapping[str, Any], context: Mapping[str, Any],
    cycle_token: str, now: datetime,
) -> dict[str, Any]:
    current_out = int(record.get("customers_out") or 0)
    previous_out = int(previous.get("customers_out") or 0)
    municipality = str(record["municipality"])
    display = _display_town(municipality)
    if current_out <= 0:
        action, status, priority = "RESOLVED", "RESOLVED", 4
        title = f"PSEG - {display} - Power Restored"
        customer_word = "customer was" if previous_out == 1 else "customers were"
        message = (
            f"Power restored in {display}. {previous_out:,} {customer_word} "
            "out during the previous check."
        )
        tags = ["white_check_mark", "zap"]
    else:
        action = "NEW" if previous_out <= 0 else "UPDATE"
        status = "ACTIVE"
        priority = 5 if current_out > previous_out else 3 if current_out < previous_out else 4
        title = f"PSEG - {display} - {current_out:,} Out"
        served = record.get("customers_served")
        message = f"{current_out:,}"
        if served:
            message += f" of {int(served):,}"
        message += f" customers out in {display}.\nETR {_display_time(record.get('etr'))}."
        message += f"\nStarted {_display_time(record.get('started_at'))}."
        operations = []
        for label, key in (("Jobs", "outage_count"), ("Working", "jobs_working"), ("Circuits", "circuits")):
            if record.get(key) is not None:
                operations.append(f"{label} {int(record[key]):,}")
        if operations:
            message += "\n" + " | ".join(operations)
        damage = []
        for label, key in (
            ("Pending", "pending_damage"), ("Pole", "confirmed_poles"),
            ("Tree", "confirmed_trees"), ("Road", "confirmed_roads"),
        ):
            if int(record.get(key) or 0) > 0:
                damage.append(f"{label} {int(record[key]):,}")
        if damage:
            message += "\nDamage: " + " | ".join(damage)
        if previous_out != current_out:
            delta = current_out - previous_out
            message += f"\nChange {delta:+,} since the previous check."
        areas = context.get("areas") or []
        labels = list(dict.fromkeys(str(item.get("label")) for item in areas if item.get("label")))
        if labels:
            message += "\nApproximate outage area: " + "; ".join(labels[:3]) + "."
            message += " Provider map areas are not customer addresses."
        else:
            message += f"\nApproximate location: {context.get('label', display)}; not customer-specific."
        if action == "NEW" or current_out > previous_out:
            tags = ["warning", "zap"]
        elif current_out < previous_out:
            tags = ["chart_with_downwards_trend", "zap"]
        else:
            tags = ["clock1", "zap"]
    location = {
        "label": f"Approximate outage area near {context.get('label', display)}",
        "address": "",
        "state": "NJ",
        "zip": "",
        "latitude": context.get("latitude"),
        "longitude": context.get("longitude"),
        "block": "",
        "lot": "",
        "qualifier": "APPROXIMATE PROVIDER OUTAGE AREA - NOT CUSTOMER SPECIFIC",
        "parcel_id": context.get("parcel_id") or "",
    }
    return _standard_alert(
        alert_id=f"PSEG:OUTAGE:{_slug(municipality)}:{cycle_token}",
        title=title,
        message=message,
        event_action=action,
        status=status,
        priority=priority,
        county="HUDSON",
        municipality=municipality,
        location=location,
        metadata={
            "scope": "HUDSON",
            "customers_out": current_out,
            "previous_customers_out": previous_out,
            "customers_served": record.get("customers_served"),
            "percent_out": record.get("percent_out"),
            "outage_count": record.get("outage_count"),
            "etr": record.get("etr"),
            "etr_latest": record.get("etr_latest"),
            "etr_earliest": record.get("etr"),
            "earliest_current_outage": record.get("started_at"),
            "jobs": record.get("outage_count"),
            "jobs_being_worked": record.get("jobs_working"),
            "circuits": record.get("circuits"),
            "pending_damage": record.get("pending_damage"),
            "confirmed_poles": record.get("confirmed_poles"),
            "confirmed_trees": record.get("confirmed_trees"),
            "confirmed_roads_blocked": record.get("confirmed_roads"),
            "location_context": dict(context),
        },
        observed_at=(previous.get("started_at") if status == "RESOLVED" else record.get("started_at")) or now,
        route_pending=True,
        tags=tags,
    )


def build_statewide_alert(
    active: Iterable[Mapping[str, Any]], transitions: Iterable[tuple[Mapping[str, Any], str]], now: datetime,
) -> dict[str, Any]:
    rows = sorted(active, key=lambda row: (str(row["county"]), -int(row.get("customers_out") or 0), str(row["municipality"])))
    changes = list(transitions)
    total = sum(int(row.get("customers_out") or 0) for row in rows)
    lines = [
        "NJ STATEWIDE - HUDSON COUNTY EXCLUDED",
        f"{total:,} customers out across {len(rows)} alert-level municipalities.",
    ]
    county = None
    for row in rows[:40]:
        if row["county"] != county:
            county = row["county"]
            lines.append(f"\n{county}")
        percent = row.get("percent_out")
        suffix = f" ({float(percent):.2f}%)" if percent is not None else ""
        lines.append(f"• {_display_town(str(row['municipality']))}: {int(row.get('customers_out') or 0):,}{suffix}")
    restored = [row for row, reason in changes if reason == "RESTORATION"]
    if restored:
        lines.append("\nRESTORED")
        lines.extend(f"• {row['county']} - {_display_town(str(row['municipality']))}" for row in restored[:20])
    reasons = sorted({reason.replace("_", " ").title() for _, reason in changes})
    lines.append("\nReason: " + ", ".join(reasons or ["Scheduled reminder"]) + ".")
    lines.append("Locations are approximate municipality areas, never customer-specific.")
    only_restored = not rows and bool(restored)
    threshold_crossing = any(reason == "THRESHOLD_CROSSING" for _, reason in changes)
    action = "RESOLVED" if only_restored else "NEW" if threshold_crossing else "UPDATE"
    title = "PSEG - NJ Statewide - Restoration" if only_restored else f"PSEG - NJ Statewide - {total:,} Out"
    fingerprint = _hash({"rows": [(r["county"], r["municipality"], r.get("customers_out")) for r in rows], "changes": [(r["county"], r["municipality"], why) for r, why in changes]})[:12]
    return _standard_alert(
        alert_id=f"PSEG:STATEWIDE:{now.strftime('%Y%m%dT%H%M')}:{fingerprint}",
        title=title,
        message="\n".join(lines),
        event_action=action,
        status="RESOLVED" if only_restored else "ACTIVE",
        priority=4 if only_restored else 5,
        county=None,
        municipality=None,
        location={
            "label": "New Jersey statewide (Hudson County excluded)", "address": "", "state": "NJ",
            "zip": "", "latitude": None, "longitude": None, "block": "", "lot": "",
            "qualifier": "MUNICIPALITY-LEVEL SUMMARY", "parcel_id": "",
        },
        metadata={
            "scope": "STATEWIDE_COMPACT",
            "hudson_excluded": True,
            "customers_out": total,
            "municipality_count": len(rows),
            "reasons": [reason for _, reason in changes],
            "municipalities": [
                {"county": row["county"], "municipality": row["municipality"], "customers_out": row.get("customers_out"), "percent_out": row.get("percent_out")}
                for row in rows
            ],
        },
        observed_at=now,
        route_pending=True,
        tags=["white_check_mark", "zap"] if only_restored else ["warning", "zap"],
    )


def build_map_alert(
    record: Mapping[str, Any], previous: Mapping[str, Any] | None, context: Mapping[str, Any],
    cycle_token: str, active: bool, now: datetime,
) -> dict[str, Any]:
    municipality = str(record["municipality"])
    county = str(record["county"])
    count = int(record.get("customers_out") or 0)
    status = "ACTIVE" if active else "RESOLVED"
    action = "NEW" if active and not (previous or {}).get("eligible") else "UPDATE" if active else "RESOLVED"
    title = f"PSEG - {_display_town(municipality)} - {count:,} Out" if active else f"PSEG - {_display_town(municipality)} - Below Alert Threshold"
    message = (
        f"{count:,} customers out. Approximate municipality outage area; not customer-specific."
        if active else
        f"This municipality no longer meets the configured statewide threshold. Provider currently reports {count:,} out."
    )
    return _standard_alert(
        alert_id=f"PSEG:MAP:{_slug(county)}:{_slug(municipality)}:{cycle_token}",
        title=title,
        message=message,
        event_action=action,
        status=status,
        priority=3,
        county=county,
        municipality=municipality,
        location={
            "label": f"Approximate {context.get('label', _display_town(municipality))}",
            "address": "", "state": "NJ", "zip": "",
            "latitude": context.get("latitude"), "longitude": context.get("longitude"),
            "block": "", "lot": "", "qualifier": "APPROXIMATE MUNICIPALITY AREA", "parcel_id": "",
        },
        metadata={
            "scope": "STATEWIDE_MAP_CONTEXT", "display_only": True,
            "customers_out": count, "customers_served": record.get("customers_served"),
            "percent_out": record.get("percent_out"), "location_context": dict(context),
        },
        observed_at=record.get("updated_at") or now,
        route_pending=False,
    )


def _upsert_alert(cur, alert: Mapping[str, Any]) -> None:
    cur.execute(
        """
        WITH d AS (SELECT %s::jsonb AS j)
        INSERT INTO alerts(
          schema_version,alert_id,source,source_event_id,category,subtype,status,event_action,
          title,message,priority,county,municipality,location,tags,click_url,source_url,
          observed_at,source_updated_at,received_at,expires_at,metadata,raw_payload,search_text,updated_at
        )
        SELECT j->>'schema_version',j->>'alert_id',j->>'source',j->>'source_event_id',
          j->>'category',j->>'subtype',j->>'status',j->>'event_action',j->>'title',j->>'message',
          (j->>'priority')::integer,nullif(j->>'county',''),nullif(j->>'municipality',''),
          coalesce(j->'location','{}'::jsonb),
          ARRAY(SELECT jsonb_array_elements_text(coalesce(j->'tags','[]'::jsonb))),
          nullif(j->>'click_url',''),nullif(j->>'source_url',''),
          nullif(j->>'observed_at','')::timestamptz,nullif(j->>'source_updated_at','')::timestamptz,
          coalesce(nullif(j->>'received_at','')::timestamptz,now()),
          nullif(j->>'expires_at','')::timestamptz,coalesce(j->'metadata','{}'::jsonb),j,
          j->>'search_text',now()
        FROM d
        ON CONFLICT(alert_id) DO UPDATE SET
          source_event_id=excluded.source_event_id,status=excluded.status,event_action=excluded.event_action,
          title=excluded.title,message=excluded.message,priority=excluded.priority,county=excluded.county,
          municipality=excluded.municipality,location=excluded.location,tags=excluded.tags,
          click_url=excluded.click_url,source_url=excluded.source_url,observed_at=excluded.observed_at,
          source_updated_at=excluded.source_updated_at,received_at=excluded.received_at,
          expires_at=excluded.expires_at,metadata=excluded.metadata,raw_payload=excluded.raw_payload,
          search_text=excluded.search_text,updated_at=now()
        """,
        (json.dumps(alert, default=str),),
    )


def _save_state(cur, row: Mapping[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO pseg_outage_state(
          scope,county,municipality,customers_out,previous_customers_out,customers_served,
          percent_out,outage_count,etr,started_at,eligible,cycle_token,current_hash,
          representative_latitude,representative_longitude,location_context,provider_record,
          baseline_at,first_seen_at,last_seen_at,last_changed_at,last_alert_created_at,
          last_alert_reason,material_baseline_customers_out,updated_at
        ) VALUES(
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,
          coalesce(%s,now()),coalesce(%s,now()),now(),coalesce(%s,now()),%s,%s,%s,now()
        )
        ON CONFLICT(scope,county,municipality) DO UPDATE SET
          previous_customers_out=excluded.previous_customers_out,
          customers_out=excluded.customers_out,customers_served=excluded.customers_served,
          percent_out=excluded.percent_out,outage_count=excluded.outage_count,etr=excluded.etr,
          started_at=excluded.started_at,eligible=excluded.eligible,cycle_token=excluded.cycle_token,
          current_hash=excluded.current_hash,representative_latitude=excluded.representative_latitude,
          representative_longitude=excluded.representative_longitude,
          location_context=excluded.location_context,provider_record=excluded.provider_record,
          last_seen_at=now(),last_changed_at=excluded.last_changed_at,
          last_alert_created_at=excluded.last_alert_created_at,
          last_alert_reason=excluded.last_alert_reason,
          material_baseline_customers_out=excluded.material_baseline_customers_out,updated_at=now()
        """,
        (
            row["scope"], row["county"], row["municipality"], row["customers_out"],
            row["previous_customers_out"], row.get("customers_served"), row.get("percent_out"),
            row.get("outage_count"), row.get("etr"), row.get("started_at"), row["eligible"],
            row.get("cycle_token"), row["current_hash"], row.get("representative_latitude"),
            row.get("representative_longitude"), json.dumps(row.get("location_context") or {}, default=str),
            json.dumps(row.get("provider_record") or {}, default=str), row.get("baseline_at"),
            row.get("first_seen_at"), row.get("last_changed_at"), row.get("last_alert_created_at"),
            row.get("last_alert_reason"), row.get("material_baseline_customers_out"),
        ),
    )


def process_snapshot(
    conn, records: list[dict[str, Any]], municipality_points: Mapping[tuple[str, str], tuple[float, float]],
    hudson_incidents: Mapping[str, list[dict[str, Any]]], settings: Mapping[str, Any],
    *, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM pseg_outage_state")
        existing = {(row["scope"], row["county"], row["municipality"]): row for row in cur.fetchall()}
        cur.execute(
            """SELECT max(received_at) AS latest
                 FROM alerts WHERE source='PSEG'
                  AND metadata->>'scope'='STATEWIDE_COMPACT'"""
        )
        latest_statewide = cur.fetchone()["latest"]
        cur.execute(
            """SELECT county,municipality FROM alerts
                 WHERE source='PSEG' AND metadata->>'scope'='STATEWIDE_MAP_CONTEXT'
                   AND status<>'RESOLVED'"""
        )
        active_map_keys = {(row["county"], row["municipality"]) for row in cur.fetchall()}

    alerts: list[dict[str, Any]] = []
    map_alerts: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []
    transitions: list[tuple[dict[str, Any], str]] = []
    current_statewide: list[dict[str, Any]] = []

    for record in records:
        scope = "HUDSON" if record["county"] == "HUDSON" else "STATEWIDE"
        key = (scope, record["county"], record["municipality"])
        previous = existing.get(key)
        context = _context_for_record(record, municipality_points, hudson_incidents, previous)
        record_hash = _record_hash(record, scope)
        old_count = int((previous or {}).get("customers_out") or 0)
        changed = previous is not None and previous.get("current_hash") != record_hash
        if scope == "HUDSON":
            eligible = int(record["customers_out"]) > 0
        else:
            eligible = statewide_eligible(record, settings)

        cycle = (previous or {}).get("cycle_token")
        if int(record["customers_out"]) > 0 and (previous is None or old_count <= 0):
            cycle = _cycle_token(record, now)
        if scope == "STATEWIDE" and eligible and not bool((previous or {}).get("eligible")):
            cycle = _cycle_token(record, now)

        reason = None
        if scope == "HUDSON" and previous is not None and changed:
            if old_count <= 0 < int(record["customers_out"]):
                reason = "NEW_OUTAGE"
            elif old_count > 0 and int(record["customers_out"]) <= 0:
                reason = "RESTORATION"
            elif int(record["customers_out"]) > old_count:
                reason = "INCREASE"
            elif int(record["customers_out"]) < old_count:
                reason = "DECREASE"
            else:
                reason = "DETAIL_UPDATE"
            alerts.append(build_hudson_alert(record, previous, context, cycle or _cycle_token(record, now), now))
        elif scope == "HUDSON" and previous is None and int(record["customers_out"]) > 0:
            baseline = build_hudson_alert(
                record,
                {"customers_out": 0},
                context,
                cycle or _cycle_token(record, now),
                now,
            )
            baseline["metadata"]["_cmos"]["route_pending"] = False
            baseline["metadata"]["baseline_only"] = True
            map_alerts.append(baseline)
        elif scope == "STATEWIDE":
            current = {**record, "eligible": eligible}
            reason = statewide_transition(previous, current, settings)
            if reason:
                transitions.append((current, reason))
            if eligible:
                current_statewide.append(current)
            if settings.get("mapping_enabled"):
                was_eligible = bool((previous or {}).get("eligible"))
                map_key = (record["county"], record["municipality"])
                if eligible and (map_key not in active_map_keys or previous is None or not was_eligible or changed):
                    map_alerts.append(build_map_alert(record, previous, context, cycle or _cycle_token(record, now), True, now))
                elif not eligible and map_key in active_map_keys:
                    map_alerts.append(build_map_alert(record, previous, context, cycle or _cycle_token(record, now), False, now))

        state_rows.append(
            {
                "scope": scope,
                "county": record["county"],
                "municipality": record["municipality"],
                "customers_out": int(record["customers_out"]),
                "previous_customers_out": old_count,
                "customers_served": record.get("customers_served"),
                "percent_out": record.get("percent_out"),
                "outage_count": record.get("outage_count"),
                "etr": record.get("etr"),
                "started_at": record.get("started_at"),
                "eligible": eligible,
                "cycle_token": cycle,
                "current_hash": record_hash,
                "representative_latitude": context.get("latitude"),
                "representative_longitude": context.get("longitude"),
                "location_context": context,
                "provider_record": record.get("raw") or {},
                "baseline_at": (previous or {}).get("baseline_at"),
                "first_seen_at": (previous or {}).get("first_seen_at"),
                "last_changed_at": now if changed or previous is None else previous.get("last_changed_at"),
                "last_alert_created_at": now if reason else (previous or {}).get("last_alert_created_at"),
                "last_alert_reason": reason or (previous or {}).get("last_alert_reason"),
                "material_baseline_customers_out": (
                    (previous or {}).get("material_baseline_customers_out")
                    if previous is not None
                    else (int(record["customers_out"]) if scope == "STATEWIDE" and eligible else None)
                ),
            }
        )

    reminder_minutes = int(settings.get("reminder_minutes") or 0)
    reminder_due = bool(
        settings.get("statewide_enabled")
        and reminder_minutes > 0
        and current_statewide
        and existing
        and latest_statewide
        and latest_statewide <= now - timedelta(minutes=reminder_minutes)
    )
    if transitions or reminder_due:
        compact_changes = transitions or [(row, "SCHEDULED_REMINDER") for row in current_statewide]
        alerts.append(build_statewide_alert(current_statewide, compact_changes, now))
        transition_keys = {(row["county"], row["municipality"]): reason for row, reason in compact_changes}
        for state in state_rows:
            marker = transition_keys.get((state["county"], state["municipality"]))
            if state["scope"] == "STATEWIDE" and marker:
                state["last_alert_created_at"] = now
                state["last_alert_reason"] = marker
                state["material_baseline_customers_out"] = state["customers_out"]

    with conn.cursor() as cur:
        if not settings.get("mapping_enabled"):
            cur.execute(
                """
                UPDATE alerts
                SET status='RESOLVED',event_action='RESOLVED',
                    message='Statewide PSEG mapping is disabled in Integrations Center.',
                    metadata=jsonb_set(metadata,'{_cmos,route_pending}','false'::jsonb,true),
                    updated_at=now()
                WHERE source='PSEG' AND metadata->>'scope'='STATEWIDE_MAP_CONTEXT'
                  AND status<>'RESOLVED'
                """
            )
        for state in state_rows:
            _save_state(cur, state)
        for alert in [*alerts, *map_alerts]:
            _upsert_alert(cur, alert)
    return {
        "ok": True,
        "municipalities": len(records),
        "hudson_active": sum(1 for row in records if row["county"] == "HUDSON" and row["customers_out"] > 0),
        "statewide_eligible": len(current_statewide),
        "alerts_created": len(alerts),
        "map_alerts_updated": len(map_alerts),
        "transition_reasons": sorted({reason for _, reason in transitions}),
        "silent_baseline": not existing,
    }


def _record_health(conn, *, ok: bool, summary: Mapping[str, Any] | None = None, error: str | None = None) -> None:
    metadata = {"release_id": RELEASE_ID, **dict(summary or {})}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO source_health(
              source_id,status,last_attempt_at,last_success_at,last_event_at,last_error,metadata,updated_at
            ) VALUES(
              'PSEG',%s,now(),CASE WHEN %s THEN now() END,
              CASE WHEN %s>0 THEN now() END,%s,%s::jsonb,now()
            )
            ON CONFLICT(source_id) DO UPDATE SET
              status=excluded.status,last_attempt_at=now(),
              last_success_at=CASE WHEN %s THEN now() ELSE source_health.last_success_at END,
              last_event_at=CASE WHEN %s>0 THEN now() ELSE source_health.last_event_at END,
              last_error=excluded.last_error,metadata=excluded.metadata,updated_at=now()
            """,
            (
                "OK" if ok else "ERROR", ok, int(metadata.get("alerts_created") or 0),
                error, json.dumps(metadata, default=str), ok, int(metadata.get("alerts_created") or 0),
            ),
        )


def run_due_pseg(*, force: bool = False) -> dict[str, Any]:
    """Run one due, locked 15-minute poll; preserve prior state on source errors."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext('CMOS_PSEG_INGESTION')) AS locked")
            if not cur.fetchone()["locked"]:
                return {"ok": True, "skipped": "locked"}
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM pseg_alert_settings WHERE settings_key='DEFAULT'")
                settings = cur.fetchone()
                if not settings:
                    return {"ok": False, "error": "PSEG settings are not installed"}
                last_started = settings.get("last_poll_started_at")
                due = not last_started or last_started <= datetime.now(timezone.utc) - timedelta(minutes=int(settings["poll_minutes"]))
                if not force and not due:
                    return {"ok": True, "skipped": "not-due"}
                cur.execute(
                    """UPDATE pseg_alert_settings
                         SET last_poll_started_at=now(),last_poll_error=NULL
                       WHERE settings_key='DEFAULT'"""
                )
            conn.commit()

            payload = _read_json(PSEG_SUMMARY_URL)
            generation, records = parse_summary(payload)
            municipality_points: dict[tuple[str, str], tuple[float, float]] = {}
            incidents: list[dict[str, Any]] = []
            hudson_incidents: dict[str, list[dict[str, Any]]] = {}
            map_warnings: list[str] = []
            try:
                municipality_points, incidents = _fetch_map_data()
            except Exception as exc:
                map_warnings.append(f"provider map unavailable: {str(exc)[:400]}")
            if incidents:
                try:
                    hudson_incidents = enrich_hudson_incidents(conn, incidents)
                except Exception as exc:
                    conn.rollback()
                    map_warnings.append(f"local spatial enrichment unavailable: {str(exc)[:400]}")
            summary = process_snapshot(conn, records, municipality_points, hudson_incidents, settings)
            poll_warning = " | ".join(map_warnings)[:1000] or None
            summary.update(
                {
                    "provider_generation": generation,
                    "hudson_incidents": len(incidents),
                    "map_status": "DEGRADED" if map_warnings else "OK",
                    "map_warnings": map_warnings,
                }
            )
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE pseg_alert_settings
                         SET last_poll_success_at=now(),last_poll_error=%s,last_provider_generation=%s
                       WHERE settings_key='DEFAULT'""",
                    (poll_warning, generation),
                )
            _record_health(conn, ok=True, summary=summary, error=poll_warning)
            conn.commit()
            return summary
        except Exception as exc:
            conn.rollback()
            error = str(exc)[:1000]
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE pseg_alert_settings SET last_poll_error=%s
                       WHERE settings_key='DEFAULT'""",
                    (error,),
                )
            _record_health(conn, ok=False, error=error)
            conn.commit()
            return {"ok": False, "error": error}
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext('CMOS_PSEG_INGESTION'))")
                conn.commit()
            except Exception:
                conn.rollback()
