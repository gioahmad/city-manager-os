"""Reparse stored BNN geography without replaying alerts or delivery workflows.

The original message/raw payload and alert identity/timestamps are never rewritten.
Only structured geography/search fields are corrected. Prior values are preserved
inside metadata so the operation is auditable and reversible.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone

from app import db_conn


VERSION = "bnn-geography-v3"
STATE_ALIASES = {
    "NJ": "NJ", "NEW JERSEY": "NJ",
    "NY": "NY", "NEW YORK": "NY", "NEW YORK STATE": "NY",
}
COUNTIES = {
    "NJ": {
        "ATLANTIC","BERGEN","BURLINGTON","CAMDEN","CAPE MAY","CUMBERLAND","ESSEX",
        "GLOUCESTER","HUDSON","HUNTERDON","MERCER","MIDDLESEX","MONMOUTH","MORRIS",
        "OCEAN","PASSAIC","SALEM","SOMERSET","SUSSEX","UNION","WARREN",
    },
    "NY": {
        "ALBANY","ALLEGANY","BRONX","BROOME","CATTARAUGUS","CAYUGA","CHAUTAUQUA",
        "CHEMUNG","CHENANGO","CLINTON","COLUMBIA","CORTLAND","DELAWARE","DUTCHESS",
        "ERIE","ESSEX","FRANKLIN","FULTON","GENESEE","GREENE","HAMILTON","HERKIMER",
        "JEFFERSON","KINGS","LEWIS","LIVINGSTON","MADISON","MONROE","MONTGOMERY",
        "NASSAU","NEW YORK","NIAGARA","ONEIDA","ONONDAGA","ONTARIO","ORANGE",
        "ORLEANS","OSWEGO","OTSEGO","PUTNAM","QUEENS","RENSSELAER","RICHMOND",
        "ROCKLAND","SARATOGA","SCHENECTADY","SCHOHARIE","SCHUYLER","SENECA",
        "ST LAWRENCE","STEUBEN","SUFFOLK","SULLIVAN","TIOGA","TOMPKINS","ULSTER",
        "WARREN","WASHINGTON","WAYNE","WESTCHESTER","WYOMING","YATES",
    },
}
NYC_BOROUGHS = {"BRONX", "BROOKLYN", "MANHATTAN", "QUEENS", "STATEN ISLAND"}


def norm(value) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def county_key(value) -> str:
    return re.sub(r"\s+COUNTY$", "", norm(value)).strip()


def title_name(value: str) -> str:
    return " ".join(part.capitalize() for part in norm(value).split())


def first(payload: dict, *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def payload_body(row: dict) -> dict:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    value = metadata.get("bnn_source_payload")
    if isinstance(value, dict):
        return value
    raw = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
    body = raw.get("body")
    return body if isinstance(body, dict) else {}


def known_municipalities(conn) -> dict[str, str]:
    values: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT coalesce(nullif(trim(inc_muni),''),nullif(trim(post_comm),'')) AS name
            FROM gis_addresses
            WHERE geom IS NOT NULL
              AND coalesce(nullif(trim(inc_muni),''),nullif(trim(post_comm),'')) IS NOT NULL
        """)
        for row in cur.fetchall():
            name = str(row["name"]).strip()
            values[norm(name)] = name
    for borough in NYC_BOROUGHS:
        values.setdefault(borough, title_name(borough))
    return values


def classify(row: dict, municipalities: dict[str, str]) -> dict:
    message = str(row.get("message") or "")
    segments = [part.strip() for part in message.split("|") if part.strip()]
    body = payload_body(row)
    resolution = row.get("resolution") if isinstance(row.get("resolution"), dict) else {}
    location = row.get("location") if isinstance(row.get("location"), dict) else {}

    explicit_state = first(body, "state", "state_code", "stateCode")
    state = STATE_ALIASES.get(norm(explicit_state), "")
    if not state:
        state = STATE_ALIASES.get(norm(resolution.get("state")), "")
    if not state:
        state = next((STATE_ALIASES[norm(part)] for part in segments if norm(part) in STATE_ALIASES), "")
    if not state:
        state = STATE_ALIASES.get(norm(location.get("state")), "")

    explicit_county = first(body, "county", "county_name", "countyName")
    county = ""
    county_source = ""
    for value, source in (
        (explicit_county, "source_payload"),
        (resolution.get("county"), "geo_resolver"),
        (row.get("county"), "stored"),
    ):
        key = county_key(value)
        candidate_sets = [COUNTIES[state]] if state in COUNTIES else list(COUNTIES.values())
        if key and any(key in items for items in candidate_sets):
            county, county_source = title_name(key), source
            break
    if not county:
        candidate_sets = [COUNTIES[state]] if state in COUNTIES else list(COUNTIES.values())
        for part in segments:
            key = county_key(part)
            if any(key in items for items in candidate_sets):
                county, county_source = title_name(key), "pipe_segment"
                break

    explicit_municipality = first(body, "municipality", "city", "town", "borough", "townFull")
    municipality = ""
    municipality_source = ""
    for value, source in (
        (explicit_municipality, "source_payload"),
        (resolution.get("municipality"), "geo_resolver"),
        (row.get("municipality"), "stored"),
    ):
        key = norm(value)
        if key and key in municipalities:
            municipality, municipality_source = municipalities[key], source
            break
    if not municipality:
        for part in segments:
            key = norm(part)
            if key in municipalities:
                municipality, municipality_source = municipalities[key], "pipe_segment"
                break

    return {
        "segments": segments,
        "state": state,
        "county": county,
        "county_source": county_source,
        "municipality": municipality,
        "municipality_source": municipality_source,
    }


def search_text(row: dict, parsed: dict) -> str:
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    tags = row.get("tags") if isinstance(row.get("tags"), list) else []
    values = [
        row.get("source"), row.get("category"), row.get("subtype"), row.get("status"),
        parsed.get("state"), parsed.get("county"),
        f"{parsed['county']} County" if parsed.get("county") else "",
        parsed.get("municipality"),
        location.get("label"), location.get("address"),
        row.get("title"), row.get("message"), *tags,
    ]
    return " ".join(str(value).strip() for value in values if value is not None and str(value).strip())


def run(*, apply: bool, limit: int | None = None) -> dict:
    summary = Counter()
    examples: list[dict] = []
    with db_conn() as conn:
        municipalities = known_municipalities(conn)
        params: list[object] = []
        limit_sql = ""
        if limit:
            limit_sql = "LIMIT %s"
            params.append(max(1, min(limit, 1000000)))
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT a.id::text,a.alert_id,a.source,a.category,a.subtype,a.status,a.title,a.message,
                       a.county,a.municipality,a.location,a.tags,a.metadata,a.raw_payload,a.search_text,
                       CASE WHEN r.entity_id IS NOT NULL THEN jsonb_build_object(
                           'status',r.status,'municipality',r.municipality,'county',r.county,'state',r.state,
                           'confidence',r.confidence,'match_type',r.match_type
                       ) ELSE '{{}}'::jsonb END AS resolution
                FROM alerts a
                LEFT JOIN geo_entity_resolutions r
                  ON r.entity_type='ALERT' AND r.entity_id=a.id::text
                WHERE upper(a.source)='BNN'
                  AND coalesce(a.metadata#>>'{{bnn_reparse,version}}','')<>%s
                ORDER BY a.received_at,a.id
                {limit_sql}
            """, (VERSION, *params))
            rows = cur.fetchall()

        summary["selected"] = len(rows)
        corrected_at = datetime.now(timezone.utc).isoformat()

        for row in rows:
            parsed = classify(row, municipalities)
            new_county = parsed["county"] or str(row.get("county") or "")
            new_municipality = parsed["municipality"] or str(row.get("municipality") or "")
            new_search = search_text(row, {**parsed, "county": new_county, "municipality": new_municipality})
            location = dict(row.get("location") or {})
            if parsed["state"]:
                location["state"] = parsed["state"]
            if new_county:
                location["county"] = new_county
            if new_municipality:
                location["municipality"] = new_municipality

            changed = (
                str(row.get("county") or "") != new_county
                or str(row.get("municipality") or "") != new_municipality
                or row.get("location") != location
                or str(row.get("search_text") or "") != new_search
            )
            summary["changed" if changed else "unchanged"] += 1
            if parsed["county_source"]:
                summary[f"county_{parsed['county_source']}"] += 1
            if parsed["municipality_source"]:
                summary[f"municipality_{parsed['municipality_source']}"] += 1
            if len(examples) < 20 and changed:
                examples.append({
                    "alert_id": row["alert_id"],
                    "prior_county": row.get("county"),
                    "county": new_county,
                    "prior_municipality": row.get("municipality"),
                    "municipality": new_municipality,
                    "county_source": parsed["county_source"],
                    "municipality_source": parsed["municipality_source"],
                    "segments": parsed["segments"],
                })

            if not apply:
                continue

            metadata = dict(row.get("metadata") or {})
            metadata["bnn_reparse"] = {
                "version": VERSION,
                "corrected_at": corrected_at,
                "prior": {
                    "county": row.get("county"),
                    "municipality": row.get("municipality"),
                    "location_state": (row.get("location") or {}).get("state"),
                    "location_county": (row.get("location") or {}).get("county"),
                    "location_municipality": (row.get("location") or {}).get("municipality"),
                    "search_text": row.get("search_text"),
                },
                "classification": {
                    "state": parsed["state"],
                    "county": new_county,
                    "county_source": parsed["county_source"],
                    "municipality": new_municipality,
                    "municipality_source": parsed["municipality_source"],
                    "segments": parsed["segments"],
                    "order_trusted": False,
                },
            }
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE alerts
                    SET county=%s,municipality=%s,location=%s::jsonb,
                        search_text=%s,metadata=%s::jsonb
                    WHERE id=%s::uuid
                """, (
                    new_county or None,
                    new_municipality or None,
                    json.dumps(location),
                    new_search,
                    json.dumps(metadata),
                    row["id"],
                ))
        if apply:
            conn.commit()
        else:
            conn.rollback()

    return {
        "mode": "APPLY" if apply else "READ_ONLY",
        "version": VERSION,
        "counts": dict(summary),
        "examples": examples,
        "notifications_created": 0,
        "matches_created": 0,
        "original_messages_rewritten": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    result = run(apply=args.apply, limit=args.limit)
    print(json.dumps(result, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
