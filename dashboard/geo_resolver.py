"""Local-first shared geographic resolution for City Manager OS.

Runtime resolution is intentionally limited to datasets held in local PostGIS.
Remote government services are refresh sources only and are never called here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping


RESOLVER_VERSION = 1
MAX_CANDIDATE_LENGTH = 300
MAX_CANDIDATES = 40

_SPACE_RE = re.compile(r"\s+")
_ADDRESS_RE = re.compile(r"^\s*\d+[A-Z]?(?:-\d+[A-Z]?)?\s+.+", re.I)
_INTERSECTION_RE = re.compile(r"^\s*(.+?)\s+(?:&|@|AT|AND)\s+(.+?)\s*$", re.I)
_REFERENCE_RE = re.compile(r"^(?:BK|BX|MN|QN|SI)-\d{3,6}$", re.I)
_STREET_WORD_RE = re.compile(
    r"\b(?:AVE(?:NUE)?|BLVD|BOULEVARD|CIR(?:CLE)?|CT|COURT|DR(?:IVE)?|HWY|HIGHWAY|"
    r"LN|LANE|PKWY|PARKWAY|PL|PLACE|PLZ|PLAZA|RD|ROAD|ST|STREET|TER|TERRACE|"
    r"TPKE|TURNPIKE|WAY|EXPY|EXPRESSWAY)\b",
    re.I,
)
_CORRIDOR_RE = re.compile(r"\b(?:BRIDGE|TUNNEL|ROUTE|HIGHWAY|PARKWAY|TURNPIKE|EXPRESSWAY)\b", re.I)
_FACILITY_RE = re.compile(
    r"\b(?:HOSPITAL|SCHOOL|ACADEMY|STATION|TERMINAL|AIRPORT|CITY HALL|TOWN HALL|"
    r"FIREHOUSE|FIRE STATION|POLICE|LIBRARY|PARK|ARENA|STADIUM|CENTER|CENTRE)\b",
    re.I,
)
_NOISE_RE = re.compile(
    r"^(?:MVA(?:\s+LOCAL)?(?:\s*/.*)?|WORKING FIRE|\d+(?:ST|ND|RD|TH) ALARM|"
    r"MULTIPLE ALARM FIRE|FIRE DEPARTMENT ACTIVITY|TRAFFIC ALERT|POLICE ACTIVITY|"
    r"SMOKE CONDITION|HEAVY SMOKE CONDITION|FIRE DAMAGE|VEHICLE FIRE|CAR FIRE|"
    r"STRUCTURE FIRE|STABBING|SHOOTING|HAZMAT(?:\s*/.*)?|MEDEVAC|PURSUIT|"
    r"FOOT PURSUIT|BARRICADED SUBJECT|ODOR OF GAS|LIVE WIRES DOWN|"
    r"PEDESTRIAN STRUCK|MOTORCYCLE ACCIDENT|OVERTURNED .+|CAR VS .+|TRUCK VS .+|"
    r"POSSIBLE .+|SERIOUS TRAUMA|APPARATUS ACCIDENT|BOX ALARM|CO INCIDENT)$",
    re.I,
)

_PATH_WEIGHTS = {
    "address": 35,
    "fulladdr": 35,
    "street_address": 35,
    "venue": 28,
    "location_name": 28,
    "location": 22,
    "message": 14,
    "description": 12,
    "title": 10,
}

_KIND_WEIGHTS = {
    "coordinate": 100,
    "address": 50,
    "intersection": 45,
    "facility": 35,
    "corridor": 30,
    "place": 15,
    "reference": 5,
}


@dataclass(frozen=True)
class LocationCandidate:
    text: str
    normalized: str
    kind: str
    source_path: str
    score: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_text(value: str) -> str:
    text = str(value or "").strip().upper()
    text = text.replace("’", "'").replace("–", "-").replace("—", "-")
    text = re.sub(r"[^A-Z0-9&@'\-/ ]+", " ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _path_weight(path: str) -> int:
    parts = [part.lower() for part in path.split(".")]
    return max((_PATH_WEIGHTS.get(part, 0) for part in parts), default=0)


def classify_text(value: str, source_path: str = "") -> str | None:
    text = normalize_text(value)
    if not text or len(text) < 3 or len(text) > MAX_CANDIDATE_LENGTH:
        return None
    if text.startswith(("HTTP://", "HTTPS://")) or _NOISE_RE.fullmatch(text):
        return None
    if _REFERENCE_RE.fullmatch(text):
        return "reference"

    match = _INTERSECTION_RE.match(text)
    if match and all(len(part.strip()) >= 2 for part in match.groups()):
        return "intersection"
    if _ADDRESS_RE.match(text) and (_STREET_WORD_RE.search(text) or len(text.split()) >= 3):
        return "address"
    if _FACILITY_RE.search(text):
        return "facility"
    if _CORRIDOR_RE.search(text) or _STREET_WORD_RE.search(text):
        return "corridor"

    path = source_path.lower()
    if any(token in path for token in ("location", "place", "neighborhood", "municipality", "borough", "city")):
        return "place"
    return None


def _walk_strings(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            yield from _walk_strings(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value[:100]):
            child_path = f"{path}.{index}" if path else str(index)
            yield from _walk_strings(child, child_path)
    elif isinstance(value, str):
        yield path, value


def extract_location_candidates(payload: Mapping[str, Any]) -> list[LocationCandidate]:
    """Extract ranked candidates from every field, including nested source data."""
    selected: dict[tuple[str, str], LocationCandidate] = {}
    for path, raw in _walk_strings(payload):
        kind = classify_text(raw, path)
        if not kind:
            continue
        text = _SPACE_RE.sub(" ", raw.strip())
        normalized = normalize_text(text)
        score = _KIND_WEIGHTS[kind] + _path_weight(path)
        candidate = LocationCandidate(text, normalized, kind, path, score)
        key = (normalized, kind)
        previous = selected.get(key)
        if previous is None or candidate.score > previous.score:
            selected[key] = candidate
    return sorted(selected.values(), key=lambda item: (-item.score, item.source_path, item.normalized))[:MAX_CANDIDATES]


def extract_coordinates(payload: Mapping[str, Any]) -> tuple[float, float, str] | None:
    """Find a valid longitude/latitude pair anywhere in a nested payload."""
    pairs = (("longitude", "latitude"), ("lon", "lat"), ("lng", "lat"))

    def walk(value: Any, path: str = ""):
        if isinstance(value, Mapping):
            lowered = {str(key).lower(): child for key, child in value.items()}
            for lon_key, lat_key in pairs:
                if lon_key in lowered and lat_key in lowered:
                    try:
                        lon = float(lowered[lon_key])
                        lat = float(lowered[lat_key])
                    except (TypeError, ValueError):
                        continue
                    if -180 <= lon <= 180 and -90 <= lat <= 90:
                        yield lon, lat, path or "root"
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                yield from walk(child, child_path)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value[:100]):
                yield from walk(child, f"{path}.{index}" if path else str(index))

    return next(walk(payload), None)


def _context_value(payload: Mapping[str, Any], names: set[str]) -> str:
    for path, value in _walk_strings(payload):
        if path.rsplit(".", 1)[-1].lower() in names and value.strip():
            return value.strip()
    return ""


def _row_value(row: Any, key: str, position: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return row[position]


def _dataset_versions(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT dataset_id,imported_at,row_count,status
            FROM gis_dataset_versions
            WHERE status='ACTIVE'
            ORDER BY dataset_id
            """
        )
        rows = cur.fetchall()
    return {
        str(_row_value(row, "dataset_id", 0)): {
            "imported_at": str(_row_value(row, "imported_at", 1)),
            "row_count": _row_value(row, "row_count", 2),
            "status": _row_value(row, "status", 3),
        }
        for row in rows
    }


def _cache_key(candidates: list[LocationCandidate], context: dict[str, str], versions: dict[str, Any]) -> str:
    material = {
        "resolver_version": RESOLVER_VERSION,
        "candidates": [candidate.as_dict() for candidate in candidates],
        "context": context,
        "datasets": versions,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _cached_result(conn, cache_key: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT result
            FROM geo_resolution_cache
            WHERE cache_key=%s
              AND resolver_version=%s
              AND (expires_at IS NULL OR expires_at > now())
            """,
            (cache_key, RESOLVER_VERSION),
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            """
            UPDATE geo_resolution_cache
            SET hit_count=hit_count+1,last_used_at=now(),updated_at=now()
            WHERE cache_key=%s
            """,
            (cache_key,),
        )
    result = _row_value(row, "result", 0)
    if isinstance(result, str):
        result = json.loads(result)
    result = dict(result)
    result["cache_hit"] = True
    return result


def _address_variants(text: str) -> list[str]:
    value = _SPACE_RE.sub(" ", text.strip()).strip(" ,")
    variants = [value]
    without_unit = re.sub(r"\s+(?:APT|UNIT|SUITE|STE|#)\s*[A-Z0-9-]+.*$", "", value, flags=re.I)
    if without_unit and without_unit.lower() != value.lower():
        variants.append(without_unit)
    return list(dict.fromkeys(variants))


def _resolve_address(conn, candidate: LocationCandidate, municipality: str) -> dict[str, Any] | None:
    rows: list[Any] = []
    matched_with_municipality = False
    for variant in _address_variants(candidate.text):
        scopes = [municipality, ""] if municipality else [""]
        for scope in scopes:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT objectid,fulladdr,post_comm,post_code,pcl_guid,
                           ST_X(geom) AS longitude,ST_Y(geom) AS latitude,status
                    FROM gis_addresses
                    WHERE lower(fulladdr)=lower(%s)
                      AND geom IS NOT NULL
                      AND (
                          nullif(trim(%s),'') IS NULL
                          OR lower(trim(coalesce(post_comm,'')))=lower(trim(%s))
                          OR lower(trim(coalesce(inc_muni,'')))=lower(trim(%s))
                      )
                    ORDER BY CASE WHEN status='A' THEN 0 ELSE 1 END,
                             CASE WHEN primarypt='Y' THEN 0 ELSE 1 END,
                             objectid
                    LIMIT 25
                    """,
                    (variant, scope, scope, scope),
                )
                rows = cur.fetchall()
            if rows:
                matched_with_municipality = bool(scope)
                break
        if rows:
            break
    if not rows:
        return None

    parcels = {str(_row_value(row, "pcl_guid", 4) or "") for row in rows}
    parcels.discard("")
    communities = {str(_row_value(row, "post_comm", 2) or "") for row in rows}
    first = rows[0]
    unambiguous = len(parcels) <= 1 and len(communities) <= 1
    confidence = 0.98 if matched_with_municipality and unambiguous else 0.93 if unambiguous else 0.65
    return {
        "status": "RESOLVED" if unambiguous else "AMBIGUOUS",
        "match_type": "LOCAL_EXACT_ADDRESS",
        "confidence": confidence,
        "label": _row_value(first, "fulladdr", 1),
        "municipality": _row_value(first, "post_comm", 2),
        "postal_code": _row_value(first, "post_code", 3),
        "parcel_id": _row_value(first, "pcl_guid", 4),
        "longitude": _row_value(first, "longitude", 5),
        "latitude": _row_value(first, "latitude", 6),
        "candidate_count": len(rows),
        "candidate": candidate.as_dict(),
    }


def _save_cache(
    conn,
    cache_key: str,
    result: dict[str, Any],
    candidates: list[LocationCandidate],
    context: dict[str, str],
    versions: dict[str, Any],
) -> None:
    longitude = result.get("longitude")
    latitude = result.get("latitude")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO geo_resolution_cache(
                cache_key,resolver_version,status,match_type,normalized_input,
                context,result,candidates,confidence,provenance,dataset_versions,
                geom,last_used_at,updated_at
            )
            VALUES (
                %s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s::jsonb,
                CASE WHEN %s IS NOT NULL AND %s IS NOT NULL
                     THEN ST_SetSRID(ST_MakePoint(%s,%s),4326) ELSE NULL END,
                now(),now()
            )
            ON CONFLICT (cache_key) DO UPDATE
            SET status=EXCLUDED.status,
                match_type=EXCLUDED.match_type,
                result=EXCLUDED.result,
                candidates=EXCLUDED.candidates,
                confidence=EXCLUDED.confidence,
                provenance=EXCLUDED.provenance,
                dataset_versions=EXCLUDED.dataset_versions,
                geom=EXCLUDED.geom,
                last_used_at=now(),
                updated_at=now()
            """,
            (
                cache_key,
                RESOLVER_VERSION,
                result["status"],
                result.get("match_type"),
                candidates[0].normalized if candidates else "",
                json.dumps(context),
                json.dumps(result, default=str),
                json.dumps([candidate.as_dict() for candidate in candidates]),
                result.get("confidence", 0),
                json.dumps(result.get("provenance") or {}),
                json.dumps(versions, default=str),
                longitude,
                latitude,
                longitude,
                latitude,
            ),
        )


def _save_entity_resolution(conn, entity_type: str, entity_id: str, cache_key: str, result: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO geo_entity_resolutions(
                entity_type,entity_id,cache_key,status,match_type,confidence,
                resolved_label,municipality,county,state,postal_code,parcel_id,
                provenance,geom,resolved_at,updated_at
            )
            VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                CASE WHEN %s IS NOT NULL AND %s IS NOT NULL
                     THEN ST_SetSRID(ST_MakePoint(%s,%s),4326) ELSE NULL END,
                CASE WHEN %s='RESOLVED' THEN now() ELSE NULL END,now()
            )
            ON CONFLICT (entity_type,entity_id) DO UPDATE
            SET cache_key=EXCLUDED.cache_key,status=EXCLUDED.status,
                match_type=EXCLUDED.match_type,confidence=EXCLUDED.confidence,
                resolved_label=EXCLUDED.resolved_label,
                municipality=EXCLUDED.municipality,county=EXCLUDED.county,
                state=EXCLUDED.state,postal_code=EXCLUDED.postal_code,
                parcel_id=EXCLUDED.parcel_id,provenance=EXCLUDED.provenance,
                geom=EXCLUDED.geom,resolved_at=EXCLUDED.resolved_at,updated_at=now()
            """,
            (
                entity_type,
                entity_id,
                cache_key,
                result["status"],
                result.get("match_type"),
                result.get("confidence", 0),
                result.get("label"),
                result.get("municipality"),
                result.get("county"),
                result.get("state"),
                result.get("postal_code"),
                result.get("parcel_id"),
                json.dumps(result.get("provenance") or {}),
                result.get("longitude"),
                result.get("latitude"),
                result.get("longitude"),
                result.get("latitude"),
                result["status"],
            ),
        )


def resolve_payload(
    conn,
    payload: Mapping[str, Any],
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Resolve a fluid payload using local PostGIS data and persist provenance."""
    candidates = extract_location_candidates(payload)
    context = {
        "municipality": _context_value(payload, {"municipality", "city", "borough", "post_comm"}),
        "county": _context_value(payload, {"county"}),
        "state": _context_value(payload, {"state", "state_code"}),
        "source": _context_value(payload, {"source"}),
    }
    versions = _dataset_versions(conn)
    cache_key = _cache_key(candidates, context, versions)
    if use_cache:
        cached = _cached_result(conn, cache_key)
        if cached is not None:
            if entity_type and entity_id:
                _save_entity_resolution(conn, entity_type, str(entity_id), cache_key, cached)
            conn.commit()
            return cached

    coordinate = extract_coordinates(payload)
    if coordinate:
        longitude, latitude, path = coordinate
        result: dict[str, Any] = {
            "status": "RESOLVED",
            "match_type": "SUPPLIED_COORDINATES",
            "confidence": 1.0,
            "label": next((candidate.text for candidate in candidates), "Supplied coordinates"),
            "municipality": context["municipality"] or None,
            "county": context["county"] or None,
            "state": context["state"] or None,
            "longitude": longitude,
            "latitude": latitude,
            "provenance": {"source_path": path, "resolver_version": RESOLVER_VERSION},
        }
    else:
        result = {}
        for candidate in candidates:
            if candidate.kind != "address":
                continue
            matched = _resolve_address(conn, candidate, context["municipality"])
            if matched:
                result = matched
                result["county"] = context["county"] or None
                result["state"] = context["state"] or "NJ"
                result["provenance"] = {
                    "source_path": candidate.source_path,
                    "source_text": candidate.text,
                    "resolver_version": RESOLVER_VERSION,
                    "runtime_source": "LOCAL_POSTGIS",
                }
                break
        if not result:
            result = {
                "status": "UNRESOLVED",
                "match_type": None,
                "confidence": 0.0,
                "label": None,
                "municipality": context["municipality"] or None,
                "county": context["county"] or None,
                "state": context["state"] or None,
                "provenance": {
                    "resolver_version": RESOLVER_VERSION,
                    "runtime_source": "LOCAL_POSTGIS",
                    "reason": "No unambiguous local match",
                },
            }

    result["cache_key"] = cache_key
    result["cache_hit"] = False
    result["candidates"] = [candidate.as_dict() for candidate in candidates]
    result["dataset_versions"] = versions
    _save_cache(conn, cache_key, result, candidates, context, versions)
    if entity_type and entity_id:
        _save_entity_resolution(conn, entity_type, str(entity_id), cache_key, result)
    conn.commit()
    return result
