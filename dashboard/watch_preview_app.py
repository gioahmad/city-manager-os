"""Read-only historical Watch preview built from stored Alerts.

This module creates no Watch, Match, delivery or Notification. It intentionally
supports the non-spatial rule shape already used by the central matcher:
source/category/priority filters plus one topic/field condition.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from operations_app import app, query_all, templates

WINDOW_HOURS = {
    "1h": 1, "2h": 2, "4h": 4, "6h": 6, "12h": 12, "24h": 24,
    "3d": 72, "7d": 168, "30d": 720, "all": None,
}
MAX_CANDIDATES = 25000
FIELDS = {"search_text", "county", "municipality", "title", "message", "category", "subtype", "source"}
MODES = {"CONTAINS", "WORD", "EXACT", "FIELD"}


def _normalize(value) -> str:
    text = "" if value is None else str(value)
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    ).upper().replace("&", "")
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


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
    if rule["source"] and _normalize(alert.get("source")) != _normalize(rule["source"]):
        return False, "source filter"
    if rule["category"] and _normalize(alert.get("category")) != _normalize(rule["category"]):
        return False, "category filter"

    prepared = _prepared(alert)
    haystack = _normalize(prepared.get(rule["field"]))
    candidates = [rule["term"], *rule["aliases"]]
    for candidate in candidates:
        needle = _normalize(candidate)
        if not needle:
            continue
        mode = rule["mode"]
        if mode in {"FIELD", "EXACT"}:
            matched = haystack == needle
        elif mode == "WORD":
            matched = bool(re.search(r"(^|\s)" + re.escape(needle) + r"(?=\s|$)", haystack))
        else:
            matched = needle in haystack
        if matched:
            return True, f"{mode} {rule['field']} matched \"{candidate}\""
    return False, f"{rule['mode']} {rule['field']} did not match"


def _rule_from_search(source: str, category: str, county: str, municipality: str, q: str,
                      min_priority: int, field: str, mode: str, term: str) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    municipality_values = [part.strip() for part in municipality.split("|") if part.strip()]
    selected = [bool(term.strip()), bool(county.strip()), bool(municipality_values), bool(q.strip())]
    if sum(selected) > 1 and term.strip():
        warnings.append("The explicit preview rule below replaces the Alerts text/place criterion.")

    if term.strip():
        chosen_field = field if field in FIELDS else "search_text"
        chosen_mode = mode.upper() if mode.upper() in MODES else "CONTAINS"
        chosen_term = term.strip()[:160]
        aliases: list[str] = []
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
):
    del state  # Watch preview evaluates future matching, not current/resolved lifecycle state.
    window = window if window in WINDOW_HOURS or window == "custom" else "30d"
    hours = max(1, min(int(custom_hours or 720), 24 * 365)) if window == "custom" else WINDOW_HOURS[window]
    rule, warnings = _rule_from_search(
        source, category, county, municipality, q, min_priority, field, mode, term
    )

    where = []
    params: list = []
    if hours is not None:
        where.append("coalesce(a.observed_at,a.received_at)>=now()-(%s * interval '1 hour')")
        params.append(hours)
    if rule["source"]:
        where.append("lower(btrim(a.source))=lower(btrim(%s))")
        params.append(rule["source"])
    if rule["category"] and rule["field"] != "category":
        where.append("lower(btrim(a.category))=lower(btrim(%s))")
        params.append(rule["category"])
    if rule["min_priority"] > 1:
        where.append("a.priority>=%s")
        params.append(rule["min_priority"])
    clause = "WHERE " + " AND ".join(where) if where else ""
    rows = query_all(
        f"""
        SELECT a.id::text AS alert_uuid,a.alert_id,a.source,a.category,a.subtype,a.status,
               a.title,a.message,a.priority,a.county,a.municipality,a.location,a.tags,
               coalesce(a.observed_at,a.received_at) AS activity_at,a.received_at
        FROM alerts a
        {clause}
        ORDER BY coalesce(a.observed_at,a.received_at) DESC,a.received_at DESC,a.id
        LIMIT {MAX_CANDIDATES + 1}
        """,
        params,
    )
    truncated = len(rows) > MAX_CANDIDATES
    rows = rows[:MAX_CANDIDATES]
    matches = []
    for row in rows:
        matched, reason = _matches(row, rule)
        if matched:
            prepared = _prepared(row)
            prepared["preview_reason"] = reason
            prepared["pipe_fields"] = prepared.pop("_pipe", {})
            matches.append(prepared)

    builder = {
        "display_name": (
            f"{rule['source'] + ' · ' if rule['source'] else ''}"
            f"{rule['field'].replace('_', ' ').title()} · {rule['term']}"
        )[:140],
        "setup_mode": "TOPIC",
        "search_term": rule["term"],
        "aliases": ",".join(rule["aliases"]),
        "source_filter": rule["source"],
        "alert_category_filter": rule["category"] if rule["field"] != "category" else "",
        "match_mode": rule["mode"],
        "match_field": rule["field"],
        "min_priority": str(rule["min_priority"]),
    }
    builder_url = "/watchlist?" + urlencode(builder) + "#new-watch"
    return templates.TemplateResponse(
        request=request,
        name="watch_preview.html",
        context={
            "rule": rule,
            "matches": matches[:100],
            "match_total": len(matches),
            "candidate_total": len(rows),
            "truncated": truncated,
            "window": window,
            "custom_hours": custom_hours,
            "warnings": warnings,
            "builder_url": builder_url,
            "page": "watch-preview",
        },
    )
