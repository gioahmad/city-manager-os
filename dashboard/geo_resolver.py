"""Local-first shared geographic resolution for City Manager OS.

Runtime resolution is intentionally limited to datasets held in local PostGIS.
Remote government services are refresh sources only and are never called here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import argparse
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

RESOLVER_VERSION = 8
MAX_CANDIDATE_LENGTH = 300
MAX_CANDIDATES = 40
MIN_PRECISE_CONFIDENCE = 0.75
MAX_INTERSECTION_DISTANCE_FEET = 1320.0

_SPACE_RE = re.compile(r"\s+")
_ADDRESS_RE = re.compile(r"^\s*\d+[A-Z]?(?:-\d+[A-Z]?)?\s+.+", re.I)
_REFERENCE_RE = re.compile(r"^(?:BK|BX|MN|QN|SI)-\d{3,6}$", re.I)
_ROUTE_RE = re.compile(r"\b(?:I|US|NJ|RT|RTE|ROUTE)\s*-?\s*(\d+[A-Z]?)\b", re.I)
_STREET_WORD_RE = re.compile(
    r"\b(?:AVE(?:NUE)?|BLVD|BOULEVARD|CIR(?:CLE)?|CT|COURT|DR(?:IVE)?|HWY|HIGHWAY|"
    r"LN|LANE|PKWY|PARKWAY|PL|PLACE|PLZ|PLAZA|RD|ROAD|ST|STREET|TER|TERRACE|"
    r"TPKE|TURNPIKE|PIKE|RT|RTE|ROUTE|WAY|EXPY|EXPRESSWAY)\b",
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

_EMBEDDED_ADDRESS_RE = re.compile(
    r"\b\d{1,6}[A-Z]?(?:-\d{1,6}[A-Z]?)?\s+"
    r"(?:[NSEW]\s+)?(?:[A-Z0-9'\-]+\s+){0,7}"
    r"(?:AVE(?:NUE)?|BLVD|BOULEVARD|CIR(?:CLE)?|CT|COURT|DR(?:IVE)?|"
    r"HWY|HIGHWAY|LN|LANE|PKWY|PARKWAY|PL|PLACE|PLZ|PLAZA|RD|ROAD|"
    r"ST|STREET|TER|TERRACE|TPKE|TURNPIKE|PIKE|RTE|ROUTE|WAY|"
    r"EXPY|EXPRESSWAY|BROADWAY)\b",
    re.I,
)
_LOCALITY_STATE_RE = re.compile(
    r"(?:^|[,;/|]\s*)([A-Z][A-Z .'-]{1,48}?)\s*,?\s+"
    r"(?:NJ|NEW JERSEY)(?:\s+\d{5}(?:-\d{4})?)?\b",
    re.I,
)

_SUFFIX_VARIANTS = {
    "AVE": "AVENUE",
    "BLVD": "BOULEVARD",
    "CIR": "CIRCLE",
    "CT": "COURT",
    "DR": "DRIVE",
    "HWY": "HIGHWAY",
    "LN": "LANE",
    "PKWY": "PARKWAY",
    "PL": "PLACE",
    "PLZ": "PLAZA",
    "RD": "ROAD",
    "ST": "STREET",
    "TER": "TERRACE",
    "TPKE": "TURNPIKE",
    "RTE": "ROUTE",
    "EXPY": "EXPRESSWAY",
}

_PLACE_CACHE: dict[tuple[str, str, str], dict[str, Any] | None] = {}

_US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

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
    noise_text = re.sub(r"^[A-Z][A-Z0-9_]{1,24}\s*-\s*", "", text)
    if text.startswith(("HTTP://", "HTTPS://")) or _NOISE_RE.fullmatch(noise_text):
        return None
    if _REFERENCE_RE.fullmatch(text):
        return "reference"

    if _intersection_parts(text):
        return "intersection"
    address_words = text.split()[1:]
    if _ADDRESS_RE.match(text) and (
        _STREET_WORD_RE.search(text)
        or any(len(re.sub(r"[^A-Z]", "", word)) >= 3 for word in address_words)
    ):
        return "address"
    if _FACILITY_RE.search(text):
        return "facility"
    if _ROUTE_RE.search(text) or _CORRIDOR_RE.search(text) or _STREET_WORD_RE.search(text):
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
        values: list[tuple[str, str, str]] = []
        fragments = [raw]
        if "|" in raw or "\n" in raw:
            fragments = [part.strip() for part in re.split(r"[|\r\n]+", raw) if part.strip()]
        for fragment in fragments:
            kind = classify_text(fragment, path)
            if kind:
                parts = _intersection_parts(fragment) if kind == "intersection" else None
                values.append((f"{parts[0]} & {parts[1]}" if parts else fragment, kind, fragment))
        for match in _EMBEDDED_ADDRESS_RE.finditer(normalize_text(raw)):
            values.append((match.group(0), "address", raw))
        for value, value_kind, original in values:
            text = _SPACE_RE.sub(" ", value.strip()).strip(" ,.;:")
            normalized = normalize_text(text)
            score = _KIND_WEIGHTS[value_kind] + _path_weight(path)
            if text != original.strip():
                score += 8
            candidate = LocationCandidate(text, normalized, value_kind, path, score)
            key = (normalized, value_kind)
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


def _municipality_hint(payload: Mapping[str, Any]) -> str:
    """Read a New Jersey locality from free text when a source omits its city field."""
    for _path, value in _walk_strings(payload):
        for match in _LOCALITY_STATE_RE.finditer(value):
            locality = _SPACE_RE.sub(" ", match.group(1)).strip(" ,.;:-")
            if locality and not any(char.isdigit() for char in locality) and not _STREET_WORD_RE.search(locality):
                return locality
    return ""


def _municipality_hints(payload: Mapping[str, Any]) -> list[str]:
    """Extract bounded locality guesses, then let local GIS validate them."""
    hints: list[str] = []

    def add(value: str) -> None:
        text = normalize_text(value)
        text = re.sub(r"^(?:CITY|TOWN|TOWNSHIP|TWP|BOROUGH|VILLAGE)\s+OF\s+", "", text)
        text = re.sub(r"\s+(?:TOWN|TOWNSHIP|TWP|BOROUGH|VILLAGE)$", "", text)
        text = re.sub(r"\s+(?:NJ|NEW JERSEY)(?:\s+\d{5}(?:-\d{4})?)?$", "", text)
        text = text.strip(" -/")
        if (
            not text
            or text in {"NJ", "NEW JERSEY"}
            or any(char.isdigit() for char in text)
            or len(text.split()) > 5
            or text.endswith(" COUNTY")
            or _STREET_WORD_RE.search(text)
            or _CORRIDOR_RE.search(text)
        ):
            return
        if text not in hints:
            hints.append(text)

    state_hint = _municipality_hint(payload)
    if state_hint:
        add(state_hint)

    for path, raw in _walk_strings(payload):
        path_name = path.rsplit(".", 1)[-1].lower()
        if path_name in {"municipality", "city", "borough", "post_comm"} or path.lower().endswith(
            "bnn_source_payload.incident"
        ):
            add(raw)

        normalized = normalize_text(raw)
        spans = [match.span() for match in _EMBEDDED_ADDRESS_RE.finditer(normalized)]
        for start, end in spans:
            add(normalized[:start])
            add(normalized[end:])

        if (
            "NJ" in normalized.split()
            or spans
            or _intersection_parts(normalized)
            or any(token in path.lower() for token in ("location", "place", "municipality", "borough", "city"))
        ):
            for part in re.split(r"[,;/|:]", raw):
                add(part)
    return hints[:20]


def _outside_local_state_coverage(value: str) -> bool:
    codes = set(normalize_text(value).replace("/", " ").split()) & _US_STATE_CODES
    return bool(codes) and "NJ" not in codes


def _local_municipality_hint(conn, payload: Mapping[str, Any]) -> str:
    hints = _municipality_hints(payload)
    if not hints:
        return ""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH hints AS (
              SELECT hint,hint_order
              FROM unnest(%s::text[]) WITH ORDINALITY AS item(hint,hint_order)
            )
            SELECT coalesce(a.label,p.label) AS label
            FROM hints h
            LEFT JOIN LATERAL (
              SELECT post_comm AS label
              FROM gis_addresses
              WHERE nullif(post_comm,'') IS NOT NULL
                AND lower(post_comm)=lower(h.hint)
                AND geom IS NOT NULL
              ORDER BY CASE WHEN status='A' THEN 0 ELSE 1 END,objectid
              LIMIT 1
            ) a ON true
            LEFT JOIN LATERAL (
              SELECT mun_name AS label
              FROM gis_parcels
              WHERE a.label IS NULL
                AND nullif(mun_name,'') IS NOT NULL
                AND lower(mun_name)=lower(h.hint)
                AND geom IS NOT NULL
              ORDER BY objectid
              LIMIT 1
            ) p ON true
            WHERE coalesce(a.label,p.label) IS NOT NULL
            ORDER BY h.hint_order
            LIMIT 1
            """,
            (hints,),
        )
        row = cur.fetchone()
    return str(_row_value(row, "label", 0) or "") if row else ""


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


def _cache_key(
    candidates: list[LocationCandidate],
    context: dict[str, str],
    versions: dict[str, Any],
    coordinate: tuple[float, float, str] | None,
) -> str:
    material = {
        "resolver_version": RESOLVER_VERSION,
        "candidates": [candidate.as_dict() for candidate in candidates],
        "context": context,
        "datasets": versions,
        "coordinate": coordinate,
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
    without_place = re.sub(
        r",?\s+[A-Z][A-Z .'-]+,?\s+(?:NJ|NEW JERSEY)(?:\s+\d{5}(?:-\d{4})?)?$",
        "",
        value,
        flags=re.I,
    ).strip(" ,")
    if without_place:
        variants.append(without_place)
    for current in list(variants):
        words = current.split()
        if words:
            suffix = re.sub(r"[^A-Z]", "", words[-1].upper())
            expanded = _SUFFIX_VARIANTS.get(suffix)
            if expanded:
                variants.append(" ".join([*words[:-1], expanded]))
            for short, long_name in _SUFFIX_VARIANTS.items():
                if suffix == long_name:
                    variants.append(" ".join([*words[:-1], short]))
    return list(dict.fromkeys(variants))


def _without_locality(text: str, municipality: str) -> str:
    value = normalize_text(text)
    locality = normalize_text(municipality)
    if locality:
        value = re.sub(
            rf"^{re.escape(locality)}\s+(?:NJ|NEW JERSEY)(?:\s+\d{{5}}(?:-\d{{4}})?)?\s*-?\s*",
            "",
            value,
        ).strip()
    value = re.sub(r"\s+(?:NJ|NEW JERSEY)(?:\s+\d{5}(?:-\d{4})?)?$", "", value).strip()
    if locality:
        value = re.sub(rf"\s+{re.escape(locality)}$", "", value).strip()
    return value


def _street_variants(text: str, municipality: str = "") -> list[str]:
    """Return bounded street-name variants suitable for local address-point lookup."""
    value = _without_locality(text, municipality)
    value = re.sub(r"^\d+[A-Z]?(?:-\d+[A-Z]?)?\s+", "", value).strip()
    if not value:
        return []

    route = _ROUTE_RE.search(value)
    route_variants: list[str] = []
    if route:
        number = route.group(1).upper()
        route_variants = [f"RT {number}", f"RTE {number}", f"ROUTE {number}", f"NJ {number}"]

    words = value.split()
    suffix_positions = [
        index for index, word in enumerate(words)
        if _STREET_WORD_RE.fullmatch(word)
    ]
    if suffix_positions:
        end = suffix_positions[-1]
        street_end = end + 1 if end + 1 < len(words) and words[end + 1] in {"E", "W", "N", "S", "EAST", "WEST", "NORTH", "SOUTH"} else end
        bases = [" ".join(words[start:street_end + 1]) for start in range(max(0, end - 5), end + 1)]
    else:
        bases = [value]

    variants: list[str] = []
    for base in bases:
        if len(base.split()) < 2 and not _CORRIDOR_RE.search(base):
            continue
        variants.extend(normalize_text(item) for item in _address_variants(base))
    return list(dict.fromkeys(item for item in [*route_variants, *variants] if item))


def _intersection_parts(text: str, municipality: str = "") -> tuple[str, str] | None:
    value = _without_locality(text, municipality)
    connectors = list(re.finditer(r"\s+(?:&|@|AT|AND|/|X)\s+", value, re.I))
    for connector in reversed(connectors):
        first = value[:connector.start()].strip()
        second = value[connector.end():].strip()
        first_variants = _street_variants(first)
        second_variants = _street_variants(second)
        if first_variants and second_variants:
            shortest = lambda variants: min(variants, key=lambda item: (len(item.split()), len(item)))
            return shortest(first_variants), shortest(second_variants)
    return None


def _address_number(text: str) -> int | None:
    match = re.match(r"^\s*(\d+)", _without_locality(text, ""))
    return int(match.group(1)) if match else None


def _resolve_street(conn, candidate: LocationCandidate, municipality: str) -> dict[str, Any] | None:
    if not municipality:
        return None
    variants = _street_variants(candidate.text, municipality)
    if not variants:
        return None
    requested_number = _address_number(candidate.text)
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH input AS (
              SELECT %s::integer AS requested_number
            ),
            street_points AS (
              SELECT a.objectid,a.fulladdr,a.post_comm,a.post_code,a.pcl_guid,a.geom,
                     CASE WHEN trim(a.fulladdr) ~ '^[0-9]+'
                          THEN substring(trim(a.fulladdr) from '^([0-9]+)')::integer END
                       AS address_number
              FROM gis_addresses a
              WHERE a.geom IS NOT NULL
                AND coalesce(a.status,'A')='A'
                AND lower(a.post_comm)=lower(%s)
                AND upper(regexp_replace(trim(a.fulladdr),'^[0-9A-Z-]+[[:space:]]+','','i'))
                      =ANY(%s::text[])
            ),
            center AS (
              SELECT ST_Centroid(ST_Collect(geom)) AS geom,count(*) AS address_count
              FROM street_points
            )
            SELECT p.fulladdr,p.post_comm,p.post_code,p.pcl_guid,
                   ST_X(p.geom) AS longitude,ST_Y(p.geom) AS latitude,
                   c.address_count,p.address_number,
                   CASE WHEN i.requested_number IS NOT NULL AND p.address_number IS NOT NULL
                        THEN abs(p.address_number-i.requested_number) END AS number_delta
            FROM street_points p
            CROSS JOIN center c
            CROSS JOIN input i
            WHERE c.geom IS NOT NULL
            ORDER BY
              CASE WHEN i.requested_number IS NOT NULL AND p.address_number IS NOT NULL
                   THEN 0 ELSE 1 END,
              CASE WHEN i.requested_number IS NOT NULL AND p.address_number IS NOT NULL
                   THEN abs(p.address_number-i.requested_number) END NULLS LAST,
              p.geom <-> c.geom,p.objectid
            LIMIT 1
            """,
            (requested_number, municipality, variants),
        )
        row = cur.fetchone()
    if not row:
        return None
    number_delta = _row_value(row, "number_delta", 8)
    confidence = 0.45
    if requested_number is not None and number_delta is not None:
        confidence = 0.72 if number_delta <= 2 else 0.65 if number_delta <= 10 else 0.55
    return {
        "status": "RESOLVED",
        "match_type": "LOCAL_NEAREST_ADDRESS",
        "confidence": confidence,
        "label": _row_value(row, "fulladdr", 0),
        "municipality": _row_value(row, "post_comm", 1) or municipality,
        "postal_code": _row_value(row, "post_code", 2),
        "parcel_id": _row_value(row, "pcl_guid", 3),
        "longitude": _row_value(row, "longitude", 4),
        "latitude": _row_value(row, "latitude", 5),
        "candidate_count": _row_value(row, "address_count", 6),
        "requested_address_number": requested_number,
        "matched_address_number": _row_value(row, "address_number", 7),
        "address_number_delta": number_delta,
        "candidate": candidate.as_dict(),
        "spatial_precision": "APPROXIMATE_ADDRESS_POINT",
    }


def _resolve_intersection(conn, candidate: LocationCandidate, municipality: str) -> dict[str, Any] | None:
    if not municipality:
        return None
    parts = _intersection_parts(candidate.text, municipality)
    if not parts:
        return None
    first, second = parts
    first_variants = _street_variants(first)
    second_variants = _street_variants(second)
    if not first_variants or not second_variants or set(first_variants) == set(second_variants):
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH first_points AS (
              SELECT a.objectid,a.fulladdr,a.post_comm,a.post_code,a.pcl_guid,a.geom
              FROM gis_addresses a
              WHERE a.geom IS NOT NULL
                AND coalesce(a.status,'A')='A'
                AND lower(a.post_comm)=lower(%s)
                AND upper(regexp_replace(trim(a.fulladdr),'^[0-9A-Z-]+[[:space:]]+','','i'))
                      =ANY(%s::text[])
            ),
            second_points AS (
              SELECT a.fulladdr,a.geom
              FROM gis_addresses a
              WHERE a.geom IS NOT NULL
                AND coalesce(a.status,'A')='A'
                AND lower(a.post_comm)=lower(%s)
                AND upper(regexp_replace(trim(a.fulladdr),'^[0-9A-Z-]+[[:space:]]+','','i'))
                      =ANY(%s::text[])
            ),
            closest AS (
              SELECT f.fulladdr AS first_address,s.fulladdr AS second_address,
                     f.post_comm,f.post_code,f.pcl_guid,
                     f.geom AS first_geom,s.geom AS second_geom,
                     ST_Distance(f.geom::geography,s.geom::geography) AS distance_m
              FROM first_points f
              CROSS JOIN LATERAL (
                SELECT fulladdr,geom
                FROM second_points s
                ORDER BY f.geom <-> s.geom
                LIMIT 1
              ) s
              ORDER BY f.geom <-> s.geom
              LIMIT 1
            )
            SELECT first_address,second_address,post_comm,post_code,pcl_guid,
                   ST_X(first_geom) AS longitude,ST_Y(first_geom) AS latitude,
                   distance_m/0.3048 AS distance_ft,
                   (SELECT count(*) FROM first_points) AS first_count,
                   (SELECT count(*) FROM second_points) AS second_count
            FROM closest
            """,
            (municipality, first_variants, municipality, second_variants),
        )
        row = cur.fetchone()
    if not row:
        return None
    distance_feet = float(_row_value(row, "distance_ft", 7) or 0)
    if distance_feet > MAX_INTERSECTION_DISTANCE_FEET:
        return None
    return {
        "status": "RESOLVED",
        "match_type": "LOCAL_NEAREST_INTERSECTION_ADDRESS",
        "confidence": 0.70 if distance_feet <= 250 else 0.60,
        "label": _row_value(row, "first_address", 0),
        "municipality": _row_value(row, "post_comm", 2) or municipality,
        "postal_code": _row_value(row, "post_code", 3),
        "parcel_id": _row_value(row, "pcl_guid", 4),
        "longitude": _row_value(row, "longitude", 5),
        "latitude": _row_value(row, "latitude", 6),
        "candidate_count": int(_row_value(row, "first_count", 8) or 0)
        + int(_row_value(row, "second_count", 9) or 0),
        "distance_feet": distance_feet,
        "intersection": f"{first.title()} & {second.title()}",
        "cross_street_nearest_address": _row_value(row, "second_address", 1),
        "candidate": candidate.as_dict(),
        "spatial_precision": "APPROXIMATE_INTERSECTION",
    }


def _resolve_address(conn, candidate: LocationCandidate, municipality: str) -> dict[str, Any] | None:
    variants = _address_variants(candidate.text)
    scopes = [municipality, ""] if municipality else [""]
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH variants AS (
              SELECT variant,variant_order
              FROM unnest(%s::text[]) WITH ORDINALITY AS item(variant,variant_order)
            ),
            scopes AS (
              SELECT scope,scope_order
              FROM unnest(%s::text[]) WITH ORDINALITY AS item(scope,scope_order)
            ),
            ranked AS (
              SELECT a.objectid,a.fulladdr,a.post_comm,a.post_code,a.pcl_guid,
                     ST_X(a.geom) AS longitude,ST_Y(a.geom) AS latitude,a.status,
                     v.variant_order,s.scope_order,
                     nullif(trim(s.scope),'') IS NOT NULL AS matched_with_municipality,
                     row_number() OVER (
                       PARTITION BY v.variant_order,s.scope_order
                       ORDER BY CASE WHEN a.status='A' THEN 0 ELSE 1 END,
                                CASE WHEN a.primarypt='Y' THEN 0 ELSE 1 END,
                                a.objectid
                     ) AS candidate_order
              FROM variants v
              CROSS JOIN scopes s
              JOIN gis_addresses a
                ON lower(a.fulladdr)=lower(v.variant)
               AND a.geom IS NOT NULL
               AND (
                 nullif(trim(s.scope),'') IS NULL
                 OR lower(trim(coalesce(a.post_comm,'')))=lower(trim(s.scope))
                 OR lower(trim(coalesce(a.inc_muni,'')))=lower(trim(s.scope))
               )
            ),
            winner AS (
              SELECT variant_order,scope_order
              FROM ranked
              ORDER BY variant_order,scope_order
              LIMIT 1
            )
            SELECT objectid,fulladdr,post_comm,post_code,pcl_guid,
                   longitude,latitude,status,matched_with_municipality
            FROM ranked
            JOIN winner USING (variant_order,scope_order)
            WHERE candidate_order <= 25
            ORDER BY candidate_order
            """,
            (variants, scopes),
        )
        rows = cur.fetchall()
    if not rows:
        return None

    matched_with_municipality = bool(_row_value(rows[0], "matched_with_municipality", 8))
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
        "spatial_precision": "ADDRESS_POINT",
    }


def _resolve_place(
    conn,
    municipality: str,
    county: str,
    dataset_key: str,
) -> dict[str, Any] | None:
    place_key = (normalize_text(municipality), normalize_text(county), dataset_key)
    if place_key in _PLACE_CACHE:
        cached = _PLACE_CACHE[place_key]
        return dict(cached) if cached else None
    if municipality:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT coalesce(nullif(trim(post_comm),''),nullif(trim(inc_muni),'')) AS label,
                       ST_X(ST_Centroid(ST_Extent(geom)::geometry)) AS longitude,
                       ST_Y(ST_Centroid(ST_Extent(geom)::geometry)) AS latitude,
                       count(*) AS address_count
                FROM gis_addresses
                WHERE geom IS NOT NULL
                  AND (
                    lower(trim(coalesce(post_comm,'')))=lower(trim(%s))
                    OR lower(trim(coalesce(inc_muni,'')))=lower(trim(%s))
                  )
                GROUP BY coalesce(nullif(trim(post_comm),''),nullif(trim(inc_muni),''))
                ORDER BY count(*) DESC
                LIMIT 1
                """,
                (municipality, municipality),
            )
            row = cur.fetchone()
        if row:
            result = {
                "status": "RESOLVED",
                "match_type": "LOCAL_MUNICIPALITY_CENTROID",
                "confidence": 0.35,
                "label": _row_value(row, "label", 0) or municipality,
                "municipality": municipality,
                "county": county or None,
                "state": "NJ",
                "longitude": _row_value(row, "longitude", 1),
                "latitude": _row_value(row, "latitude", 2),
                "candidate_count": _row_value(row, "address_count", 3),
                "spatial_precision": "APPROXIMATE_MUNICIPALITY",
            }
            _PLACE_CACHE[place_key] = result
            return dict(result)
    if county:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ST_X(ST_Centroid(ST_Extent(geom)::geometry)) AS longitude,
                       ST_Y(ST_Centroid(ST_Extent(geom)::geometry)) AS latitude,
                       count(*) AS parcel_count
                FROM gis_parcels
                WHERE geom IS NOT NULL
                  AND upper(trim(coalesce(county,'')))=upper(trim(%s))
                """,
                (county,),
            )
            row = cur.fetchone()
        if row and _row_value(row, "longitude", 0) is not None:
            result = {
                "status": "RESOLVED",
                "match_type": "LOCAL_COUNTY_CENTROID",
                "confidence": 0.20,
                "label": f"{county} County",
                "municipality": None,
                "county": county,
                "state": "NJ",
                "longitude": _row_value(row, "longitude", 0),
                "latitude": _row_value(row, "latitude", 1),
                "candidate_count": _row_value(row, "parcel_count", 2),
                "spatial_precision": "APPROXIMATE_COUNTY",
            }
            _PLACE_CACHE[place_key] = result
            return dict(result)
    _PLACE_CACHE[place_key] = None
    return None


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
                CASE WHEN %s::double precision IS NOT NULL
                           AND %s::double precision IS NOT NULL
                     THEN ST_SetSRID(ST_MakePoint(
                         %s::double precision,%s::double precision
                     ),4326) ELSE NULL END,
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
                provenance,geom,spatial_precision,resolver_version,
                resolved_at,last_attempt_at,attempt_count,updated_at
            )
            VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                CASE WHEN %s::double precision IS NOT NULL
                           AND %s::double precision IS NOT NULL
                     THEN ST_SetSRID(ST_MakePoint(
                         %s::double precision,%s::double precision
                     ),4326) ELSE NULL END,
                %s,%s,CASE WHEN %s::text='RESOLVED' THEN now() ELSE NULL END,
                now(),1,now()
            )
            ON CONFLICT (entity_type,entity_id) DO UPDATE
            SET cache_key=EXCLUDED.cache_key,status=EXCLUDED.status,
                match_type=EXCLUDED.match_type,confidence=EXCLUDED.confidence,
                resolved_label=EXCLUDED.resolved_label,
                municipality=EXCLUDED.municipality,county=EXCLUDED.county,
                state=EXCLUDED.state,postal_code=EXCLUDED.postal_code,
                parcel_id=EXCLUDED.parcel_id,provenance=EXCLUDED.provenance,
                geom=EXCLUDED.geom,spatial_precision=EXCLUDED.spatial_precision,
                resolver_version=EXCLUDED.resolver_version,
                resolved_at=EXCLUDED.resolved_at,last_attempt_at=now(),
                attempt_count=geo_entity_resolutions.attempt_count+1,updated_at=now()
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
                result.get("spatial_precision"),
                RESOLVER_VERSION,
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
    persist: bool = True,
) -> dict[str, Any]:
    """Resolve a fluid payload using local PostGIS data, optionally without writes."""
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    approximate_coordinate = bool(metadata.get("location_approximate"))
    candidates = extract_location_candidates(payload)
    municipality = _context_value(payload, {"municipality", "city", "borough", "post_comm"})
    if not municipality:
        municipality = _local_municipality_hint(conn, payload)
    context = {
        "municipality": municipality,
        "county": _context_value(payload, {"county"}),
        "state": _context_value(payload, {"state", "state_code"}),
        "source": _context_value(payload, {"source"}),
        "coordinate_policy": "APPROXIMATE_PROVIDER_AREA" if approximate_coordinate else "PRECISE",
    }
    versions = _dataset_versions(conn)
    coordinate = extract_coordinates(payload)
    cache_key = _cache_key(candidates, context, versions, coordinate)
    if use_cache and persist:
        cached = _cached_result(conn, cache_key)
        if cached is not None:
            if entity_type and entity_id:
                _save_entity_resolution(conn, entity_type, str(entity_id), cache_key, cached)
            conn.commit()
            return cached

    if coordinate:
        longitude, latitude, path = coordinate
        result: dict[str, Any] = {
            "status": "RESOLVED",
            "match_type": "PROVIDER_APPROXIMATE_COORDINATE" if approximate_coordinate else "SUPPLIED_COORDINATES",
            "confidence": 0.60 if approximate_coordinate else 1.0,
            "label": next((candidate.text for candidate in candidates), "Supplied coordinates"),
            "municipality": context["municipality"] or None,
            "county": context["county"] or None,
            "state": context["state"] or None,
            "longitude": longitude,
            "latitude": latitude,
            "spatial_precision": "APPROXIMATE_PROVIDER_AREA" if approximate_coordinate else "SUPPLIED_COORDINATE",
            "provenance": {
                "source_path": path,
                "resolver_version": RESOLVER_VERSION,
                "approximate": approximate_coordinate,
                "not_customer_specific": bool(metadata.get("location_not_customer_specific")),
            },
        }
    else:
        result = {}
        if _outside_local_state_coverage(context["state"]):
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
                    "reason": "State is outside the installed NJ address dataset",
                },
            }
        ambiguous_address: tuple[dict[str, Any], LocationCandidate] | None = None
        for candidate in candidates:
            if result:
                break
            if candidate.kind != "address":
                continue
            matched = _resolve_address(conn, candidate, context["municipality"])
            if matched:
                if matched["status"] == "RESOLVED":
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
                if ambiguous_address is None:
                    ambiguous_address = (matched, candidate)
        if not result:
            for candidate in (item for item in candidates if item.kind == "intersection"):
                matched = _resolve_intersection(conn, candidate, context["municipality"])
                if matched:
                    result = matched
                    result["county"] = context["county"] or None
                    result["state"] = context["state"] or "NJ"
                    result["provenance"] = {
                        "source_path": candidate.source_path,
                        "source_text": candidate.text,
                        "resolver_version": RESOLVER_VERSION,
                        "runtime_source": "LOCAL_POSTGIS",
                        "approximate": True,
                    }
                    break
        if not result:
            street_candidates = [
                item for item in candidates if item.kind in {"address", "corridor"}
            ]
            intersection = next((item for item in candidates if item.kind == "intersection"), None)
            parts = _intersection_parts(intersection.text, context["municipality"]) if intersection else None
            if intersection and parts:
                for street in reversed(parts):
                    street_candidates.insert(0, LocationCandidate(
                        street, normalize_text(street), "corridor", intersection.source_path,
                        intersection.score,
                    ))
            for candidate in street_candidates:
                matched = _resolve_street(conn, candidate, context["municipality"])
                if matched:
                    result = matched
                    result["county"] = context["county"] or None
                    result["state"] = context["state"] or "NJ"
                    result["provenance"] = {
                        "source_path": candidate.source_path,
                        "source_text": candidate.text,
                        "resolver_version": RESOLVER_VERSION,
                        "runtime_source": "LOCAL_POSTGIS",
                        "approximate": True,
                    }
                    break
        if not result:
            place = _resolve_place(
                conn,
                context["municipality"],
                context["county"],
                json.dumps(versions, sort_keys=True, default=str),
            )
            if place:
                result = place
                result["provenance"] = {
                    "resolver_version": RESOLVER_VERSION,
                    "runtime_source": "LOCAL_POSTGIS",
                    "approximate": True,
                }
        if not result and ambiguous_address:
            result, candidate = ambiguous_address
            result["county"] = context["county"] or None
            result["state"] = context["state"] or "NJ"
            result["provenance"] = {
                "source_path": candidate.source_path,
                "source_text": candidate.text,
                "resolver_version": RESOLVER_VERSION,
                "runtime_source": "LOCAL_POSTGIS",
                "reason": "Multiple local address points matched",
            }
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
    if persist:
        _save_cache(conn, cache_key, result, candidates, context, versions)
        if entity_type and entity_id:
            _save_entity_resolution(conn, entity_type, str(entity_id), cache_key, result)
        conn.commit()
    return result


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _alert_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    location = _as_dict(row.get("location"))
    metadata = _as_dict(row.get("metadata"))
    metadata.pop("geo_resolution", None)
    if row.get("longitude") is not None and row.get("latitude") is not None:
        location.setdefault("longitude", row["longitude"])
        location.setdefault("latitude", row["latitude"])
    return {
        "source": row.get("source"),
        "county": row.get("county"),
        "municipality": row.get("municipality"),
        "title": row.get("title"),
        "message": row.get("message"),
        "location": location,
        "metadata": metadata,
        "raw_payload": _as_dict(row.get("raw_payload")),
    }


def audit_alerts(
    conn,
    *,
    limit: int | None = None,
    since_days: int | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    """Run stored alerts through the resolver without writing or notifying."""
    started = time.perf_counter()
    limit = max(1, min(int(limit), 1_000_000)) if limit is not None else None
    since_days = max(1, min(int(since_days), 36500)) if since_days is not None else None
    sources = sources or ["BNN"]
    where = ["a.source=ANY(%s::text[])"]
    params: list[Any] = [sources]
    if since_days is not None:
        where.append("a.received_at >= now()-(%s * interval '1 day')")
        params.append(since_days)
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT a.alert_id,a.source,a.county,a.municipality,a.title,a.message,
                   a.location,a.metadata,a.raw_payload,
                   count(*) OVER() AS total_available
            FROM alerts a
            WHERE {' AND '.join(where)}
            ORDER BY a.received_at DESC,a.id
            {limit_sql}
            """,
            tuple(params),
        )
        rows = cur.fetchall()

    status_counts: Counter[str] = Counter()
    match_counts: Counter[str] = Counter()
    precision_counts: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []
    mapped_count = 0
    unresolved_with_candidates = 0
    no_location_evidence = 0
    for index, row in enumerate(rows, 1):
        try:
            result = resolve_payload(conn, _alert_payload(row), use_cache=False, persist=False)
            status = str(result.get("status") or "UNRESOLVED")
            match_type = str(result.get("match_type") or "UNRESOLVED")
            precision = str(result.get("spatial_precision") or "NONE")
            status_counts[status] += 1
            match_counts[match_type] += 1
            precision_counts[precision] += 1
            mapped = (
                status == "RESOLVED"
                and result.get("longitude") is not None
                and result.get("latitude") is not None
            )
            if mapped:
                mapped_count += 1
            elif result.get("candidates"):
                unresolved_with_candidates += 1
            else:
                no_location_evidence += 1
            bucket = examples.setdefault(match_type, [])
            if len(bucket) < 5:
                location = _as_dict(row.get("location"))
                bucket.append(
                    {
                        "alert_id": row.get("alert_id"),
                        "reported_location": location.get("address") or location.get("label"),
                        "resolved_label": result.get("label"),
                        "municipality": result.get("municipality"),
                        "confidence": result.get("confidence"),
                        "longitude": result.get("longitude"),
                        "latitude": result.get("latitude"),
                    }
                )
        except Exception as exc:
            conn.rollback()
            errors.append({"alert_id": str(row.get("alert_id") or ""), "error": str(exc)})
        if index % 100 == 0:
            print(f"GEO AUDIT progress={index}/{len(rows)}", file=sys.stderr, flush=True)

    total_available = int(rows[0].get("total_available") or 0) if rows else 0
    return {
        "mode": "READ_ONLY",
        "sources": sources,
        "total_available": total_available,
        "selected": len(rows),
        "complete": len(rows) == total_available,
        "status_counts": dict(status_counts),
        "match_type_counts": dict(match_counts),
        "spatial_precision_counts": dict(precision_counts),
        "mapped_count": mapped_count,
        "unresolved_with_candidates": unresolved_with_candidates,
        "no_location_evidence": no_location_evidence,
        "examples": examples,
        "errors": errors[:20],
        "error_count": len(errors),
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def process_pending_alerts(
    conn,
    *,
    limit: int = 50,
    since_days: int = 30,
    sources: list[str] | None = None,
    alert_ids: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Resolve a bounded alert batch without creating a second ingestion path."""
    started = time.perf_counter()
    limit = max(1, min(int(limit), 10000))
    since_days = max(1, min(int(since_days), 3650))
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext('CMOS_ALERT_GEO_RESOLVER')) AS locked")
        locked = bool(_row_value(cur.fetchone(), "locked", 0))
    if not locked:
        return {
            "locked": True,
            "selected": 0,
            "processed": 0,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    summary: dict[str, Any] = {
        "locked": False,
        "selected": 0,
        "processed": 0,
        "precise": 0,
        "approximate": 0,
        "ambiguous": 0,
        "unresolved": 0,
        "errors": 0,
    }
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id::text AS entity_id,a.alert_id,a.source,a.county,a.municipality,
                       a.title,a.message,a.location,a.metadata,a.raw_payload,a.updated_at,
                       CASE WHEN a.geom IS NOT NULL THEN ST_X(a.geom) END AS longitude,
                       CASE WHEN a.geom IS NOT NULL THEN ST_Y(a.geom) END AS latitude
                FROM alerts a
                LEFT JOIN geo_entity_resolutions r
                  ON r.entity_type='ALERT' AND r.entity_id=a.id::text
                WHERE a.received_at >= now()-(%s * interval '1 day')
                  AND (%s::text[] IS NULL OR a.source=ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR a.alert_id=ANY(%s::text[]))
                  AND (
                    %s
                    OR r.id IS NULL
                    OR (a.geom IS NULL AND coalesce(r.resolver_version,0) < %s)
                    OR (
                      a.geom IS NULL
                      AND r.status='RESOLVED'
                      AND r.confidence >= %s
                      AND r.spatial_precision IN ('ADDRESS_POINT','SUPPLIED_COORDINATE')
                    )
                    OR a.updated_at > r.updated_at + interval '5 minutes'
                  )
                ORDER BY a.received_at DESC,a.priority DESC,a.id
                LIMIT %s
                """,
                (
                    since_days, sources, sources, alert_ids, alert_ids, force, RESOLVER_VERSION,
                    MIN_PRECISE_CONFIDENCE, limit,
                ),
            )
            rows = cur.fetchall()
        summary["selected"] = len(rows)

        for index, row in enumerate(rows, 1):
            try:
                result = resolve_payload(
                    conn,
                    _alert_payload(row),
                    entity_type="ALERT",
                    entity_id=str(row["entity_id"]),
                    use_cache=not force,
                )
                status = str(result.get("status") or "UNRESOLVED")
                confidence = float(result.get("confidence") or 0)
                longitude = result.get("longitude")
                latitude = result.get("latitude")
                precision = str(result.get("spatial_precision") or "")
                precise = (
                    status == "RESOLVED"
                    and confidence >= MIN_PRECISE_CONFIDENCE
                    and longitude is not None
                    and latitude is not None
                )
                resolution_meta = json.dumps(
                    {
                        "status": status,
                        "match_type": result.get("match_type"),
                        "confidence": confidence,
                        "spatial_precision": precision or None,
                        "resolved_label": result.get("label"),
                        "cache_hit": bool(result.get("cache_hit")),
                        "resolver_version": RESOLVER_VERSION,
                    }
                )
                with conn.cursor() as cur:
                    if precise:
                        cur.execute(
                            """
                            UPDATE alerts
                            SET geom=ST_SetSRID(ST_MakePoint(%s,%s),4326),
                                location=location || jsonb_strip_nulls(jsonb_build_object(
                                  'label',%s::text,
                                  'address',coalesce(nullif(location->>'address',''),%s::text),
                                  'municipality',%s::text,'state',%s::text,'zip',%s::text,
                                  'parcel_id',%s::text,
                                  'longitude',%s::double precision,
                                  'latitude',%s::double precision
                                )),
                                metadata=jsonb_set(metadata,'{geo_resolution}',%s::jsonb,true)
                            WHERE id=%s::uuid
                            """,
                            (
                                longitude, latitude, result.get("label"), result.get("label"),
                                result.get("municipality"), result.get("state"),
                                result.get("postal_code"), result.get("parcel_id"),
                                longitude, latitude, resolution_meta, row["entity_id"],
                            ),
                        )
                        summary["precise"] += 1
                    else:
                        cur.execute(
                            """
                            UPDATE alerts
                            SET metadata=jsonb_set(metadata,'{geo_resolution}',%s::jsonb,true)
                            WHERE id=%s::uuid
                            """,
                            (resolution_meta, row["entity_id"]),
                        )
                        if status == "RESOLVED" and longitude is not None and latitude is not None:
                            summary["approximate"] += 1
                        elif status == "AMBIGUOUS":
                            summary["ambiguous"] += 1
                        else:
                            summary["unresolved"] += 1
                conn.commit()
                summary["processed"] += 1
            except Exception as exc:
                conn.rollback()
                summary["errors"] += 1
                print(f"alert geo resolution failed alert={row.get('alert_id')}: {exc}", flush=True)
            print(
                f"GEO BACKFILL progress={index}/{len(rows)} alert={row.get('alert_id')}",
                file=sys.stderr,
                flush=True,
            )

        summary["duration_ms"] = int((time.perf_counter() - started) * 1000)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO source_health(
                  source_id,status,last_attempt_at,last_success_at,last_error,metadata,updated_at
                )
                VALUES('GEO_RESOLVER',%s,now(),CASE WHEN %s=0 THEN now() ELSE NULL END,%s,%s::jsonb,now())
                ON CONFLICT(source_id) DO UPDATE
                SET status=excluded.status,last_attempt_at=now(),
                    last_success_at=CASE WHEN %s=0 THEN now() ELSE source_health.last_success_at END,
                    last_error=excluded.last_error,metadata=excluded.metadata,updated_at=now()
                """,
                (
                    "OK" if summary["errors"] == 0 else "ERROR",
                    summary["errors"],
                    None if summary["errors"] == 0 else f"{summary['errors']} alert resolution errors",
                    json.dumps(summary),
                    summary["errors"],
                ),
            )
        conn.commit()
        return summary
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext('CMOS_ALERT_GEO_RESOLVER'))")
            conn.commit()
        except Exception:
            conn.rollback()


def _connect():
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        host=os.getenv("DB_HOST", "citymanager-postgis"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "citymanager"),
        user=os.getenv("DB_USER", "citymanager_app"),
        password=os.environ["DB_PASSWORD"],
        row_factory=dict_row,
        connect_timeout=5,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="City Manager OS local alert geography worker")
    parser.add_argument("mode", choices=("audit", "backfill"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--since-days", type=int)
    parser.add_argument("--source", action="append", dest="sources")
    parser.add_argument("--alert-id", action="append", dest="alert_ids")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    with _connect() as conn:
        if args.mode == "audit":
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
            summary = audit_alerts(
                conn,
                limit=args.limit,
                since_days=args.since_days,
                sources=args.sources or ["BNN"],
            )
            conn.rollback()
            print(json.dumps(summary, sort_keys=True))
            return 1 if summary.get("error_count") or not summary.get("complete") else 0
        for attempt in range(1, 31):
            summary = process_pending_alerts(
                conn,
                limit=args.limit or 5000,
                since_days=args.since_days or 3650,
                sources=args.sources,
                alert_ids=args.alert_ids,
                force=args.force,
            )
            if not summary.get("locked"):
                break
            if attempt == 30:
                raise RuntimeError("alert geography resolver remained busy")
            time.sleep(2)
    print(json.dumps(summary, sort_keys=True))
    return 1 if summary.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
