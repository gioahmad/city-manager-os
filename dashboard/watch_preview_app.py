"""Read-only historical Watch preview built from stored Alerts.

This module creates no Watch, Match, delivery or Notification. Saved Watch
previews retain their area, topic and all source/category/priority filters.
"""
from __future__ import annotations

import re
import os
import unicodedata
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from operations_app import app, db_conn, query_one, templates

WINDOW_HOURS = {
    "1h": 1, "2h": 2, "4h": 4, "6h": 6, "12h": 12, "24h": 24,
    "3d": 72, "7d": 168, "30d": 720, "all": None,
}
LOCAL_ZONE = ZoneInfo(os.getenv('APP_TIMEZONE') or os.getenv('TZ') or 'America/New_York')
FIELDS = {"search_text", "county", "municipality", "title", "message", "category", "subtype", "source"}
MODES = {"CONTAINS", "WORD", "EXACT", "FIELD"}


def _normalize(value) -> str:
    text = "" if value is None else str(value)
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    ).upper().replace("&", "")
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


def watch_terms(term: str, aliases: str = "") -> list[str]:
    values = []
    seen = set()
    for value in re.split(r"[|,\r\n]+", term + "\n" + aliases):
        value = value.strip().strip('"').strip()
        if not value or value.casefold() in seen:
            continue
        if len(value) > 160 or len(values) >= 200:
            raise HTTPException(400, "Use up to 200 terms, each at most 160 characters.")
        seen.add(value.casefold())
        values.append(value)
    return values


def _phrase_pattern(value: str) -> str:
    return "[0-9]".join(re.escape(_normalize("A" + part + "Z")[1:-1]) for part in value.strip().split(r"\d"))


STATE_ALIASES = {"NJ": "NJ", "NEW JERSEY": "NJ", "NY": "NY", "NEW YORK STATE": "NY"}
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


def _county_name(value: str) -> str:
    return re.sub(r"\s+COUNTY$", "", _normalize(value), flags=re.I).strip()


def _geography_key(field: str, value) -> str:
    text = _normalize(value)
    if field == "county":
        text = re.sub(r"\s+COUNTY$", "", text, flags=re.I).strip()
    return text


def _pipe_fields(alert: dict) -> dict:
    """Read BNN pipe segments by semantic value; field order is intentionally ignored."""
    if str(alert.get("source") or "").upper() != "BNN":
        return {}
    parts = [part.strip() for part in str(alert.get("message") or "").split("|") if part.strip()]
    if len(parts) < 2:
        return {}
    state = next((STATE_ALIASES[_normalize(part)] for part in parts if _normalize(part) in STATE_ALIASES), "")
    sets = [COUNTIES[state]] if state else [COUNTIES["NJ"], COUNTIES["NY"]]
    county = next(
        (_county_name(part).title() for part in parts if any(_county_name(part) in values for values in sets)),
        "",
    )
    location = next(
        (
            part for part in parts
            if re.search(r"^\d{1,6}[A-Z]?(?:-\d{1,6}[A-Z]?)?\s+|\s(?:&|@|/|AT|AND|X)\s", part, re.I)
        ),
        "",
    )
    incident = next(
        (
            part for part in parts
            if re.search(r"\b(ALERT|FIRE|MVA|MVC|ACCIDENT|POLICE|EMS|MEDICAL|HAZMAT|RESCUE|SHOOTING|STABBING|ALARM|ENTRAPMENT|MCI|MAYDAY|TRAFFIC)\b", part, re.I)
            and part != location
            and _normalize(part) not in STATE_ALIASES
            and not any(_county_name(part) in values for values in sets)
        ),
        "",
    )
    source_code = next(
        (part for part in reversed(parts) if re.fullmatch(r"[A-Za-z]{1,4}\d{1,8}", part)),
        "",
    )
    return {
        "segments": parts,
        "state": state,
        "county": county,
        "incident_type": incident,
        "location": location,
        "source_code": source_code,
        "order_trusted": False,
    }


def _prepared(alert: dict) -> dict:
    pipe = _pipe_fields(alert)
    location = alert.get("location") if isinstance(alert.get("location"), dict) else {}
    county = str(alert.get("county") or pipe.get("county") or "")
    municipality = str(alert.get("municipality") or pipe.get("municipality") or "")
    tags = alert.get("tags") if isinstance(alert.get("tags"), list) else []
    parts = [
        alert.get("source"), alert.get("category"), alert.get("subtype"), alert.get("status"),
        county, f"{county} County" if county and not county.lower().endswith(" county") else "",
        municipality, pipe.get("incident_type"), pipe.get("location"),
        location.get("label"), location.get("address"), alert.get("title"), alert.get("message"), *tags,
    ]
    return {
        **alert,
        "county": county,
        "municipality": municipality,
        "search_text": " ".join(str(value) for value in parts if value is not None and str(value).strip()),
        "_pipe": pipe,
    }


def _matches(alert: dict, rule: dict) -> tuple[bool, str]:
    if int(alert.get("priority") or 0) < rule["min_priority"]:
        return False, f"P{alert.get('priority') or 0} below P{rule['min_priority']}"
    sources = rule.get("sources", [rule["source"]] if rule["source"] else [])
    categories = rule.get("categories", [rule["category"]] if rule["category"] else [])
    if sources and _normalize(alert.get("source")) not in {_normalize(value) for value in sources}:
        return False, "source filter"
    if categories and _normalize(alert.get("category")) not in {_normalize(value) for value in categories}:
        return False, "category filter"

    prepared = _prepared(alert)
    metadata = alert.get("metadata") if isinstance(alert.get("metadata"), dict) else {}
    if rule.get("canonical") and metadata.get("mapped_activity_only") and not rule["location_required"]:
        return False, "mapped activity requires an area Watch"
    location_field = rule.get("location_field")
    if location_field:
        wanted = _geography_key(location_field, rule["location_term"])
        location = prepared.get("location") if isinstance(prepared.get("location"), dict) else {}
        values = [prepared.get(location_field), location.get(location_field)]
        if _normalize(prepared.get("source")) == "BNN":
            values += (prepared.get("_pipe") or {}).get("segments", [])
        if not wanted or wanted not in {_geography_key(location_field, value) for value in values}:
            return False, "outside saved location"
    if rule.get("topic_required") is False:
        return True, "Inside saved Watch area"
    value = prepared
    if rule.get("canonical") and rule["field"] == "search_text":
        location = alert.get("location") if isinstance(alert.get("location"), dict) else {}
        tags = alert.get("tags") if isinstance(alert.get("tags"), list) else []
        parts = [alert.get(key) for key in ("source", "category", "subtype", "status", "county", "municipality")]
        parts += [location.get("label"), location.get("address"), alert.get("title"), alert.get("message"), *tags]
        value = {**prepared, "search_text": " ".join(str(part) for part in parts if part is not None and str(part).strip())}
    for part in rule["field"].split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    haystack = _normalize(value)
    candidates = [rule["term"], *rule["aliases"]]
    for candidate in candidates:
        needle = _normalize(candidate)
        if not needle:
            continue
        mode = rule["mode"]
        if mode in {"FIELD", "EXACT"}:
            if rule["field"] in {"county", "municipality"}:
                direct = _geography_key(rule["field"], prepared.get(rule["field"]))
                wanted = _geography_key(rule["field"], candidate)
                pipe_values = [
                    _geography_key(rule["field"], value)
                    for value in (prepared.get("_pipe") or {}).get("segments", [])
                ] if _normalize(prepared.get("source")) == "BNN" else []
                location = prepared.get("location") if isinstance(prepared.get("location"), dict) else {}
                location_value = _geography_key(rule["field"], location.get(rule["field"]))
                matched = bool(wanted and (direct == wanted or location_value == wanted or wanted in pipe_values))
            else:
                matched = haystack == needle
        elif mode == "WORD":
            matched = bool(re.search(r"(^|\s)" + _phrase_pattern(candidate) + r"(?=\s|$)", haystack))
        else:
            matched = bool(re.search(_phrase_pattern(candidate), haystack)) if r"\d" in candidate else needle in haystack
        if matched:
            return True, f"{mode} {rule['field']} matched \"{candidate}\""
    return False, f"{rule['mode']} {rule['field']} did not match"


def _rule_from_search(source: str, category: str, county: str, municipality: str, q: str,
                      min_priority: int, field: str, mode: str, term: str,
                      aliases_text: str = "") -> tuple[dict, list[str]]:
    warnings: list[str] = []
    municipality_values = [part.strip() for part in municipality.split("|") if part.strip()]
    selected = [bool(term.strip()), bool(county.strip()), bool(municipality_values), bool(q.strip())]
    if sum(selected) > 1 and term.strip():
        warnings.append("The explicit preview rule below replaces the Alerts text/place criterion.")

    if term.strip():
        chosen_field = field if field in FIELDS else "search_text"
        chosen_mode = mode.upper() if mode.upper() in MODES else "CONTAINS"
        values = watch_terms(term, aliases_text) if chosen_mode in {"CONTAINS", "WORD"} else [term.strip()[:160], *watch_terms(aliases_text)]
        chosen_term, aliases = values[0], values[1:]
    elif county.strip():
        chosen_field, chosen_mode = "county", "FIELD"
        chosen_term = re.sub(r"\s+County$", "", county.strip(), flags=re.I)
        aliases = []
        if q.strip():
            warnings.append("The current Watch model cannot require County AND a separate free-text phrase in one non-spatial rule. County is being previewed; the text search is not.")
    elif municipality_values:
        chosen_field, chosen_mode = "municipality", "FIELD"
        chosen_term, aliases = municipality_values[0], municipality_values[1:12]
        if q.strip():
            warnings.append("The current Watch model cannot require Municipality AND a separate free-text phrase in one non-spatial rule. Municipality is being previewed; the text search is not.")
    elif q.strip():
        chosen_field, chosen_mode, chosen_term, aliases = "search_text", "CONTAINS", q.strip()[:160], []
    elif category.strip():
        chosen_field, chosen_mode, chosen_term, aliases = "category", "FIELD", category.strip(), []
    elif source.strip():
        chosen_field, chosen_mode, chosen_term, aliases = "source", "FIELD", source.strip(), []
    else:
        raise HTTPException(400, "Choose a source, county, municipality, category or phrase before previewing a Watch")

    return {
        "source": source.strip()[:120],
        "category": category.strip()[:120],
        "field": chosen_field,
        "mode": chosen_mode,
        "term": chosen_term,
        "aliases": aliases,
        "min_priority": max(1, min(int(min_priority or 1), 5)),
    }, warnings


def _saved_rule(watch_item_id: UUID) -> dict:
    watch = query_one("""SELECT id,display_name,watch_type,search_term,aliases,match_field,match_mode,
        source_filter,alert_category_filter,min_priority,nearby_enabled,radius_ft,spatial_scope,
        address,municipality,county,ST_AsEWKT(coalesce(spatial_target_geom,geom)) AS target,
        ST_AsEWKT(spatial_geom) AS boundary FROM watch_items WHERE id=%s""", (watch_item_id,))
    if not watch:
        raise HTTPException(404, "Watch not found")
    spatial = bool(watch["nearby_enabled"])
    watch_type = str(watch["watch_type"] or '').upper()
    location_topic = watch_type == 'LOCATION_TOPIC'
    location_field = ("municipality" if watch_type == 'TOWN' or (location_topic and watch["municipality"])
                      else "county" if watch_type == 'COUNTY' or (location_topic and watch["county"]) else '') if not spatial else ''
    location_term = (watch.get(location_field) or watch["search_term"]) if location_field else ''
    if spatial and (not watch["target"] or not watch["boundary"]):
        raise HTTPException(400, "This Watch is missing its saved area. Edit the Watch and choose its location again.")
    if location_topic and not spatial and not location_field:
        raise HTTPException(400, "This Watch needs a saved location before previewing history.")
    sources = list(watch["source_filter"] or [])
    categories = list(watch["alert_category_filter"] or [])
    return {
        **watch, "canonical": True, "source": ', '.join(sources), "category": ', '.join(categories),
        "sources": sources, "categories": categories, "field": watch["match_field"] or 'search_text',
        "mode": watch["match_mode"], "term": watch["search_term"] or '', "aliases": watch["aliases"] or [],
        "min_priority": int(watch["min_priority"] or 1), "spatial": spatial,
        "location_required": spatial or location_topic or bool(location_field),
        "location_field": location_field, "location_term": location_term,
        "location_label": watch["address"] or watch["municipality"] or watch["county"] or 'Saved map location',
        "topic_required": location_topic or (not spatial and not location_field),
    }


@app.get("/watch-preview", response_class=HTMLResponse)
def watch_preview(
    request: Request,
    q: str = "",
    source: str = "",
    category: str = "",
    county: str = "",
    municipality: str = "",
    state: str = "all",
    window: str = "30d",
    custom_hours: int = 720,
    min_priority: int = 1,
    field: str = "",
    mode: str = "",
    term: str = "",
    aliases: str = "",
    watch_item_id: UUID | None = None,
    latitude: float | None = Query(default=None, ge=-90, le=90),
    longitude: float | None = Query(default=None, ge=-180, le=180),
    radius_ft: float = Query(default=1000, ge=1, le=26400),
    history_page: int = Query(default=1, ge=1),
):
    del state  # Watch preview evaluates future matching, not current/resolved lifecycle state.
    map_point = latitude is not None or longitude is not None
    if map_point and (latitude is None or longitude is None or watch_item_id):
        raise HTTPException(400, "Choose a saved Watch or supply both coordinates for a map point.")
    if not watch_item_id and not map_point and not any(
        value.strip() for value in (source, category, county, municipality, q, term)
    ):
        return templates.TemplateResponse(
            request=request, name="watch_preview.html",
            context={"needs_criteria": True, "page": "watchlist"},
        )
    if map_point and 'window' not in request.query_params:
        window = 'all'
    window = window if window in WINDOW_HOURS or window == "custom" else "30d"
    custom_hours = max(1, min(int(custom_hours or 720), 24 * 365))
    hours = custom_hours if window == "custom" else WINDOW_HOURS[window]
    if watch_item_id:
        rule, warnings = _saved_rule(watch_item_id), []
    elif map_point:
        if any(value.strip() for value in (term, q, source, category)):
            rule, warnings = _rule_from_search(source, category, '', '', q, min_priority, field, mode, term, aliases)
        else:
            rule, warnings = {'source': '', 'category': '', 'field': 'search_text', 'mode': 'CONTAINS',
                              'term': '', 'aliases': [], 'min_priority': max(1, min(min_priority, 5))}, []
        if not term.strip() and not q.strip():
            rule.update(field='search_text', mode='CONTAINS', term='', aliases=[])
        point = f'SRID=4326;POINT({longitude} {latitude})'
        rule.update(canonical=True, spatial=True, location_required=True, topic_required=bool(term.strip() or q.strip()),
                    target=point, boundary=point, radius_ft=radius_ft, spatial_scope='RADIUS',
                    location_label=f'{latitude:.6f}, {longitude:.6f}')
    else:
        rule, warnings = _rule_from_search(
            source, category, county, municipality, q, min_priority, field, mode, term, aliases
        )

    where = []
    params: list = []
    area_cte = area_join = ''
    distance_column = ''
    if rule.get("spatial"):
        area_cte = 'WITH watch_area AS (SELECT ST_GeomFromEWKT(%s) AS target,ST_GeomFromEWKT(%s) AS boundary)'
        params.extend([rule["target"], rule["boundary"]])
        area_join = """LEFT JOIN geo_entity_resolutions r ON r.entity_type='ALERT'
            AND r.entity_id=a.id::text AND r.status='RESOLVED' CROSS JOIN watch_area w"""
        point = 'coalesce(a.geom,r.geom)'
        distance_column = f',ST_Distance({point}::geography,w.target::geography)/0.3048 AS preview_distance_ft'
        if str(rule["spatial_scope"] or 'RADIUS').upper() == 'RADIUS':
            where.append(f'ST_DWithin({point}::geography,w.target::geography,%s*0.3048)')
            params.append(rule["radius_ft"])
        else:
            where.append(f'ST_Intersects({point},w.boundary)')
    if hours is not None:
        where.append("coalesce(a.observed_at,a.received_at)>=now()-(%s * interval '1 hour')")
        params.append(hours)
    for column, values in (
        ('source', rule.get('sources', [rule['source']] if rule['source'] else [])),
        ('category', rule.get('categories', [rule['category']] if rule['category'] else [])),
    ):
        if values:
            where.append(f'EXISTS(SELECT 1 FROM unnest(%s::text[]) value WHERE upper(btrim(a.{column}))=upper(btrim(value)))')
            params.append(values)
    if rule["min_priority"] > 1:
        where.append("a.priority>=%s")
        params.append(rule["min_priority"])
    clause = "WHERE " + " AND ".join(where) if where else ""
    history_query = f"""
        {area_cte}
        SELECT a.id::text AS alert_uuid,a.alert_id,a.source,a.category,a.subtype,a.status,
               a.title,a.message,a.priority,a.county,a.municipality,a.location,a.tags,
               a.metadata,coalesce(a.observed_at,a.received_at) AS activity_at,a.received_at{distance_column}
        FROM alerts a
        {area_join}
        {clause}
        ORDER BY coalesce(a.observed_at,a.received_at) DESC,a.received_at DESC,a.id
        """
    matches = []
    match_total = candidate_total = 0
    # A server cursor checks the entire period without loading its history into memory.
    # Keep only this page's 100 display rows; counts include every eligible stored alert.
    first_match = (history_page - 1) * 100
    with db_conn() as conn:
        with conn.cursor(name='watch_history_preview') as cursor:
            cursor.execute(history_query, params)
            for row in cursor:
                candidate_total += 1
                matched, reason = _matches(row, rule)
                if matched:
                    match_total += 1
                    if match_total <= first_match or len(matches) >= 100:
                        continue
                    prepared = _prepared(row)
                    prepared["preview_reason"] = reason
                    prepared["activity_display"] = row['activity_at'].astimezone(LOCAL_ZONE).strftime('%m/%d/%Y %I:%M %p %Z')
                    if rule.get("spatial"):
                        prepared["preview_reason"] = f"Inside selected area · {row['preview_distance_ft']:,.1f} feet from target" + (f" · {reason}" if rule['topic_required'] else '')
                    prepared["pipe_fields"] = prepared.pop("_pipe", {})
                    matches.append(prepared)

    def history_page_url(number: int) -> str:
        params = dict(request.query_params)
        params.update(window=window, history_page=number)
        return '/watch-preview?' + urlencode(params) + '#history-results'

    history_pages = max(1, (match_total + 99) // 100)
    geography = rule["field"] in {"county", "municipality"}
    builder = {
        "display_name": (
            f"{rule['source'] + ' · ' if rule['source'] else ''}"
            f"{rule['field'].replace('_', ' ').title()} · {rule['term']}"
        )[:140],
        "setup_mode": "LOCATION" if geography else "TOPIC",
        "search_term": "" if geography else rule["term"],
        "aliases": "" if geography else ",".join(rule["aliases"]),
        "location_query": rule["term"] if geography else "",
        "location_kind": (
            "COUNTY" if rule["field"] == "county"
            else "MUNICIPALITY" if rule["field"] == "municipality"
            else ""
        ),
        "location_id": rule["term"] if geography else "",
        "source_filter": rule["source"],
        "alert_category_filter": rule["category"] if rule["field"] != "category" else "",
        "match_mode": rule["mode"],
        "match_field": rule["field"],
        "min_priority": str(rule["min_priority"]),
        "previewed": "1",
    }
    builder_url = "/watchlist?" + urlencode(builder) + "#new-watch"
    if watch_item_id:
        builder_url = '/watchlist?' + urlencode({'focus': str(watch_item_id)}) + '#edit-watch'
    elif map_point:
        builder.update(display_name='Map area watch', setup_mode='LOCATION_TOPIC' if rule['topic_required'] else 'LOCATION', location_kind='MAP_POINT',
                       latitude=latitude, longitude=longitude, radius_ft=radius_ft, location_query='', location_id='',
                       match_selection='ANY' if not source and not category and min_priority == 1 and not rule['topic_required'] else 'FILTERED')
        builder_url = '/watchlist?' + urlencode(builder) + '#new-watch'
    return templates.TemplateResponse(
        request=request,
        name="watch_preview.html",
        context={
            "rule": rule,
            "watch_item_id": str(watch_item_id) if watch_item_id else '',
            "map_point": map_point,
            "latitude": latitude,
            "longitude": longitude,
            "matches": matches,
            "match_total": match_total,
            "candidate_total": candidate_total,
            "history_page": history_page,
            "history_pages": history_pages,
            "first_match": first_match + 1,
            "last_match": first_match + len(matches),
            "previous_url": history_page_url(min(history_page - 1, history_pages)) if history_page > 1 else '',
            "next_url": history_page_url(history_page + 1) if history_page < history_pages else '',
            "window": window,
            "custom_hours": custom_hours,
            "warnings": warnings,
            "builder_url": builder_url,
            "page": "watch-preview",
        },
    )


