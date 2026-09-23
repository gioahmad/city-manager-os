import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from issues_app import app
from app import db_conn, execute, query_all, query_one, templates
from integration_runtime import apply_literal_auth, perform_http_request, redact_text

LOGGER = logging.getLogger(__name__)

SMSGATE_DEFAULT_URL = "https://api.sms-gate.app/3rdparty/v1/message"


def _smsgate_config_path() -> Path:
    configured = os.getenv("CMOS_SMSGATE_CONFIG_FILE", "").strip()
    if configured:
        return Path(configured)
    default_dir = os.getenv("TRANSIT_TOKEN_CACHE_DIR", "/tmp/cmos-private-config")
    return Path(default_dir) / "smsgate.json"


def _smsgate_settings() -> dict[str, str]:
    settings = {
        "url": os.getenv("CMOS_SMSGATE_URL", SMSGATE_DEFAULT_URL).strip(),
        "username": os.getenv("CMOS_SMSGATE_USERNAME", "").strip(),
        "password": os.getenv("CMOS_SMSGATE_PASSWORD", ""),
    }
    try:
        saved = json.loads(_smsgate_config_path().read_text())
        if not isinstance(saved, dict):
            raise ValueError("SMSGate settings must be an object")
        settings.update({key: str(saved[key]) for key in settings if saved.get(key) is not None})
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        LOGGER.warning("Could not read saved SMSGate settings", exc_info=True)
    return settings


def _save_smsgate_settings(url: str, username: str, password: str) -> None:
    current = _smsgate_settings()
    values = {
        "url": url.strip()[:1000],
        "username": username.strip()[:200],
        "password": password[:500] or current["password"],
    }
    parsed = urlsplit(values["url"])
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a valid HTTP or HTTPS SMSGate URL")
    if parsed.username or parsed.password:
        raise ValueError("Do not put credentials in the SMSGate URL")
    if not values["username"] or not values["password"]:
        raise ValueError("SMSGate username and password are required")
    path = _smsgate_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(values, handle)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _phone_numbers(value: str) -> list[str]:
    numbers = []
    for raw in re.split(r"[,;\n]+", value):
        number = re.sub(r"[\s().-]", "", raw.strip())
        if not number:
            continue
        if not re.fullmatch(r"\+?[1-9]\d{6,14}", number):
            raise ValueError(f"Invalid phone number: {raw.strip()}")
        if number not in numbers:
            numbers.append(number)
    if not numbers:
        raise ValueError("Enter at least one phone number")
    if len(numbers) > 25:
        raise ValueError("Send to no more than 25 phone numbers at once")
    return numbers


def _send_smsgate(phone_numbers: str, message: str):
    settings = _smsgate_settings()
    url = settings["url"]
    username = settings["username"]
    password = settings["password"]
    if not url or not username or not password:
        raise ValueError("SMSGate credentials are not configured")
    message = message.strip()
    if not message:
        raise ValueError("Enter a message")
    if len(message) > 4000:
        raise ValueError("Message must be 4,000 characters or fewer")
    headers = {"Content-Type": "application/json"}
    secrets = apply_literal_auth(
        "BASIC", headers, {}, username=username, password=password
    )
    result = perform_http_request(
        method="POST",
        url=url,
        headers=headers,
        body=json.dumps({
            "textMessage": {"text": message},
            "phoneNumbers": _phone_numbers(phone_numbers),
        }),
        timeout_seconds=20,
        max_response_bytes=100_000,
        allow_redirects=False,
        allow_private=True,
    )
    if not result.ok:
        detail = result.error or result.body_text[:300] or "SMSGate rejected the request"
        raise ValueError(redact_text(detail, secrets))
    return result


def _alert_share_content(alert_reference: str) -> tuple[str, str]:
    alert = query_one(
        """
        SELECT title,message,source,category,alert_id,received_at,click_url,
               coalesce(nullif(location->>'label',''),nullif(location->>'address',''),
                        nullif(municipality,''),'Not mapped') AS location_label
        FROM alerts
        WHERE alert_id=%s
        ORDER BY received_at DESC
        LIMIT 1
        """,
        (alert_reference,),
    )
    if not alert:
        raise ValueError("That alert could not be found")
    subject = f"Alert: {alert.get('title') or alert_reference}"
    detail = str(alert.get("message") or "").strip()
    if len(detail) > 3000:
        detail = detail[:2997] + "..."
    lines = [str(alert.get("title") or "Alert"), detail]
    lines.extend([
        f"Location: {alert.get('location_label')}",
        f"Source: {alert.get('source')} · {alert.get('category')}",
        f"Received: {alert['received_at'].strftime('%m/%d/%Y %I:%M %p') if alert.get('received_at') else 'Unknown'}",
        f"Reference: {alert.get('alert_id')}",
    ])
    if alert.get("click_url"):
        lines.append(str(alert["click_url"]))
    return subject[:200], "\n".join(line for line in lines if line)[:4000]


def require_watch_recipients(cur, watch_item_ids) -> None:
    """Keep an active Watch from looking ready when it cannot notify anyone."""
    watch_item_ids = list(dict.fromkeys(watch_item_ids))
    if not watch_item_ids:
        return
    cur.execute(
        """
        SELECT count(*) AS total
        FROM watch_items w
        WHERE w.id=ANY(%s::uuid[])
          AND w.active=true
          AND (w.expires_at IS NULL OR w.expires_at>now())
          AND NOT EXISTS (
            SELECT 1
            FROM watch_item_recipients wir
            JOIN subscribers s ON s.id=wir.subscriber_id
            WHERE wir.watch_item_id=w.id AND wir.active=true AND s.active=true
          )
        """,
        (watch_item_ids,),
    )
    total = int(cur.fetchone()["total"])
    if total:
        noun = "Watch would" if total == 1 else "Watches would"
        raise HTTPException(
            status_code=400,
            detail=(
                f"{total} active {noun} have no active Recipient. "
                f"Choose a Recipient or pause {'it' if total == 1 else 'them'} first."
            ),
        )


ALERT_KEYWORD_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "for", "from",
    "has", "have", "in", "incident", "is", "it", "new", "of", "on", "or",
    "received", "reported", "that", "the", "this", "to", "transmitted", "update",
    "was", "were", "with",
}


def alert_keyword_choices(alert: dict, limit: int = 12) -> list[str]:
    """Suggest reusable phrases found in one Alert, never a fixed incident dictionary."""
    choices: list[str] = []
    seen: set[str] = set()

    def add(value) -> None:
        text = re.sub(r"\s+", " ", str(value or "").replace("_", " ")).strip(" ,.;:|-/")
        normalized = re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()
        words = normalized.split()
        if (
            not normalized
            or normalized in seen
            or len(normalized) > 80
            or all(word.casefold() in ALERT_KEYWORD_STOPWORDS for word in words)
        ):
            return
        seen.add(normalized)
        choices.append(text)

    for value in (alert.get("message"), alert.get("title")):
        tokens = re.findall(r"[A-Za-z0-9]+(?:['/-][A-Za-z0-9]+)*", str(value or ""))
        useful = [
            (index, token)
            for index, token in enumerate(tokens)
            if len(token) >= 3
            and not token.isdigit()
            and token.casefold() not in ALERT_KEYWORD_STOPWORDS
        ]
        for (left_index, left), (right_index, right) in zip(useful, useful[1:]):
            if right_index == left_index + 1:
                add(f"{left} {right}")
        for _, token in useful:
            add(token)

    for value in (alert.get("subtype"), alert.get("category"), *(alert.get("tags") or [])):
        add(value)
    return choices[: max(1, min(limit, 20))]


def alert_map_url(alert: dict) -> str:
    """Open one Alert in Mapping Center, or its placement tool when unmapped."""
    alert_reference = str(alert.get("alert_id") or "").strip()
    if not alert_reference:
        return ""
    params = {
        "map_view": "1",
        "focus": "alert",
        "tab": "layers",
        "window": "all",
        "area_q": alert_reference,
    }
    latitude = alert.get("map_latitude")
    longitude = alert.get("map_longitude")
    if latitude is not None and longitude is not None:
        params.update(
            {
                "lat": f"{float(latitude):.6f}",
                "lng": f"{float(longitude):.6f}",
                "zoom": "17",
                "selected_layer": "alerts",
                "selected_id": alert_reference,
            }
        )
    else:
        params["edit_alert"] = alert_reference
    return f"/map?{urlencode(params)}"

MODULES = [
    {"key": "PSEG", "name": "Utilities", "description": "Electric utility outages and restorations"},
    {"key": "FIRE", "name": "Fire Intelligence", "description": "Fire and public-safety incident intelligence"},
    {"key": "WEATHER", "name": "Weather / Flood", "description": "Weather, flood, tide and warning intelligence"},
    {"key": "TRAFFIC", "name": "Traffic", "description": "Road closures, incidents and construction impacts"},
    {"key": "TRANSIT", "name": "Transit", "description": "NJ Transit, PATH and regional transit disruptions"},
    {"key": "EVENTS", "name": "Events", "description": "Regional events and operational impacts"},
]

SEARCH_SCOPES = {
    "all": "Everything",
    "alerts": "Alerts",
    "work": "Work Items",
    "watches": "Watches",
    "notifications": "Notifications",
    "events": "Events",
    "transit": "Transit",
    "locations": "Locations",
    "sources": "Sources",
    "people": "Recipients & Staff",
    "operations": "Routines & Managed Locations",
    "configuration": "Rules & Map Layers",
    "conditions": "Flood & Utility State",
}

SEARCH_SECTION_ORDER = (
    "Alerts",
    "Work Items",
    "Watches",
    "Notifications",
    "Events",
    "Transit",
    "Locations",
    "Sources",
    "Recipients",
    "Staff",
    "Routines",
    "Managed Locations",
    "Rules",
    "Map Layers",
    "Flood",
    "Utility State",
)
ALERT_BULK_LIMIT = 100
ALERT_FILTERED_BULK_LIMIT = 5000
ALERT_WINDOWS = {
    "6h": 6,
    "12h": 12,
    "24h": 24,
    "7d": 168,
    "30d": 720,
    "all": None,
}


def _alert_filter(
    *,
    q: str = "",
    source: str = "",
    category: str = "",
    municipality: str = "",
    state: str = "all",
    window: str = "7d",
    min_priority: int = 1,
) -> tuple[str, list, dict]:
    """Build the one Alert search contract used by the page and bulk actions."""
    where = []
    params = []
    window = window if window in ALERT_WINDOWS else "7d"
    window_hours = ALERT_WINDOWS[window]
    if window_hours is not None:
        where.append("a.received_at>=now()-(%s * interval '1 hour')")
        params.append(window_hours)
    state = state if state in {"active", "resolved", "all"} else "all"
    if state == "active":
        where.append("a.status <> 'RESOLVED' AND (a.expires_at IS NULL OR a.expires_at > now())")
    elif state == "resolved":
        where.append("a.status = 'RESOLVED'")
    for value, column in (
        (source, "a.source"),
        (category, "a.category"),
        (municipality, "a.municipality"),
    ):
        if value.strip():
            where.append(f"upper({column}) = upper(%s)")
            params.append(value.strip())
    min_priority = max(1, min(int(min_priority), 5))
    if min_priority > 1:
        where.append("a.priority >= %s")
        params.append(min_priority)
    q = q.strip()[:160]
    if q:
        needle = f"%{q}%"
        where.append(
            "(coalesce(a.search_text,'') ILIKE %s OR a.title ILIKE %s OR a.message ILIKE %s "
            "OR coalesce(a.municipality,'') ILIKE %s OR coalesce(a.county,'') ILIKE %s "
            "OR a.alert_id ILIKE %s OR a.source ILIKE %s OR a.category ILIKE %s "
            "OR a.subtype ILIKE %s OR a.location::text ILIKE %s "
            "OR array_to_string(a.tags,' ') ILIKE %s)"
        )
        params.extend([needle] * 11)
    filters = {
        "q": q,
        "source": source.strip(),
        "category": category.strip(),
        "municipality": municipality.strip(),
        "state": state,
        "window": window,
        "min_priority": min_priority,
    }
    return (f"WHERE {' AND '.join(where)}" if where else ""), params, filters


def make_subscriber_id(name: str):
    slug = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:32] or "SUBSCRIBER"
    return f"S_{slug}_{uuid.uuid4().hex[:6].upper()}"


def remove_existing_get(path: str):
    app.router.routes = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", None) == path
            and "GET" in (getattr(route, "methods", set()) or set())
        )
    ]


remove_existing_get("/")


@app.get("/", response_class=HTMLResponse)
def operations_home(request: Request):
    metrics = query_one(
        """
        SELECT
          (SELECT count(*) FROM alerts
             WHERE status <> 'RESOLVED'
               AND (expires_at IS NULL OR expires_at > now())) AS active_alerts,
          (SELECT count(*) FROM watch_items WHERE active = true) AS active_watch_items,
          (SELECT count(*) FROM subscribers WHERE active = true) AS active_subscribers,
          (SELECT count(*) FROM issues
             WHERE status NOT IN ('RESOLVED','CLOSED')) AS open_issues,
          (SELECT count(*) FROM deliveries WHERE status = 'SENT'
             AND created_at >= now() - interval '24 hours') AS sent_24h,
          (SELECT count(*) FROM source_health
             WHERE upper(status) NOT IN ('OK','HEALTHY')) AS unhealthy_sources
        """
    )

    active_alerts = query_all(
        """
        SELECT alert_id, source, category, subtype, status, event_action,
               title, message, priority, municipality, received_at, click_url
        FROM alerts
        WHERE status <> 'RESOLVED'
          AND (expires_at IS NULL OR expires_at > now())
        ORDER BY priority DESC, received_at DESC
        LIMIT 12
        """
    )

    intelligence_feed = query_all(
        """
        SELECT alert_id, source, category, subtype, status, event_action,
               title, priority, municipality, received_at
        FROM alerts
        ORDER BY received_at DESC
        LIMIT 20
        """
    )

    source_health = query_all(
        """
        SELECT source_id, status, last_attempt_at, last_success_at,
               last_event_at, last_error, updated_at
        FROM source_health
        ORDER BY source_id
        """
    )

    recent_deliveries = query_all(
        """
        SELECT d.status, d.ntfy_topic, d.sent_at, d.attempted_at,
               s.subscriber_id, s.name AS subscriber_name,
               a.title AS alert_title, a.source
        FROM deliveries d
        JOIN subscribers s ON s.id = d.subscriber_id
        JOIN alerts a ON a.id = d.alert_id
        ORDER BY d.created_at DESC
        LIMIT 12
        """
    )

    command_center = query_all(
        """
        SELECT
          id, title, item_type, category, priority, status,
          assigned_to, next_action, waiting_on,
          due_at AT TIME ZONE current_setting('TimeZone') AS due_local,
          follow_up_at AT TIME ZONE current_setting('TimeZone') AS follow_up_local,
          updated_at,
          CASE
            WHEN due_at IS NOT NULL AND due_at <= now() THEN 'DUE'
            WHEN follow_up_at IS NOT NULL AND follow_up_at <= now() THEN 'FOLLOW UP'
            WHEN waiting_on IS NOT NULL AND trim(waiting_on) <> '' THEN 'WAITING'
            WHEN next_action IS NULL OR trim(next_action) = '' THEN 'NO NEXT ACTION'
            ELSE 'OPEN'
          END AS loop_status
        FROM issues
        WHERE status NOT IN ('RESOLVED','CLOSED')
        ORDER BY
          CASE
            WHEN due_at IS NOT NULL AND due_at <= now() THEN 0
            WHEN follow_up_at IS NOT NULL AND follow_up_at <= now() THEN 1
            WHEN waiting_on IS NOT NULL AND trim(waiting_on) <> '' THEN 2
            WHEN next_action IS NULL OR trim(next_action) = '' THEN 3
            ELSE 4
          END,
          priority DESC,
          updated_at DESC
        LIMIT 10
        """
    )

    command_counts = query_one(
        """
        SELECT
          count(*) FILTER (
            WHERE status NOT IN ('RESOLVED','CLOSED')
              AND (
                (due_at IS NOT NULL AND due_at < ((date_trunc('day', now() AT TIME ZONE current_setting('TimeZone')) + interval '1 day') AT TIME ZONE current_setting('TimeZone')))
                OR
                (follow_up_at IS NOT NULL AND follow_up_at < ((date_trunc('day', now() AT TIME ZONE current_setting('TimeZone')) + interval '1 day') AT TIME ZONE current_setting('TimeZone')))
              )
          ) AS today,
          count(*) FILTER (
            WHERE status NOT IN ('RESOLVED','CLOSED')
              AND NULLIF(trim(waiting_on), '') IS NOT NULL
          ) AS waiting,
          count(*) FILTER (
            WHERE status NOT IN ('RESOLVED','CLOSED')
              AND NULLIF(trim(next_action), '') IS NULL
          ) AS no_next_action
        FROM issues
        """
    )

    happening_now = query_all(
        """
        SELECT id, title, category, location_name, address, municipality,
               starts_at AT TIME ZONE current_setting('TimeZone') AS starts_local,
               ends_at AT TIME ZONE current_setting('TimeZone') AS ends_local,
               priority, source, notes,
               CASE
                 WHEN starts_at <= now()
                  AND COALESCE(ends_at, starts_at + interval '2 hours') > now()
                   THEN 'NOW'
                 WHEN starts_at > now()
                  AND starts_at <= now() + interval '3 hours'
                   THEN 'NEXT'
                 ELSE 'UPCOMING'
               END AS timing_status
        FROM operational_events
        WHERE active = true
          AND event_status NOT IN ('COMPLETED','CANCELLED')
          AND starts_at < now() + interval '12 hours'
          AND COALESCE(ends_at, starts_at + interval '2 hours')
                > now() - interval '30 minutes'
        ORDER BY
          CASE
            WHEN starts_at <= now()
             AND COALESCE(ends_at, starts_at + interval '2 hours') > now()
              THEN 0
            ELSE 1
          END,
          starts_at
        LIMIT 12
        """
    )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "metrics": metrics,
            "active_alerts": active_alerts,
            "intelligence_feed": intelligence_feed,
            "source_health": source_health,
            "recent_deliveries": recent_deliveries,
            "command_center": command_center,
            "command_counts": command_counts,
            "happening_now": happening_now,
            "generated_at": datetime.now(),
            "page": "overview",
        },
    )


@app.get("/share", response_class=HTMLResponse)
def share_page(
    request: Request,
    alert: str = "",
    subject: str = "",
    message: str = "",
    phones: str = "",
    emails: str = "",
    msg: str = "",
    error: str = "",
):
    if alert and not message:
        try:
            subject, message = _alert_share_content(alert.strip()[:300])
        except ValueError as exc:
            error = str(exc)
    settings = _smsgate_settings()
    can_manage = getattr(request.state, "cmos_role", None) in {None, "EXECUTIVE"}
    return templates.TemplateResponse(
        request=request,
        name="share.html",
        context={
            "subject": subject,
            "message": message,
            "phones": phones,
            "emails": emails,
            "msg": msg,
            "error": error,
            "smsgate_configured": bool(settings["url"] and settings["username"] and settings["password"]),
            "smsgate_url": settings["url"],
            "smsgate_username": settings["username"],
            "can_manage_smsgate": can_manage,
            "page": "share",
        },
    )


@app.post("/share/smsgate")
def share_smsgate(
    request: Request,
    phones: str = Form(...),
    message: str = Form(...),
    subject: str = Form(""),
    emails: str = Form(""),
):
    try:
        result = _send_smsgate(phones, message)
    except ValueError as exc:
        return share_page(
            request,
            subject=subject,
            message=message,
            phones=phones,
            emails=emails,
            error=str(exc),
        )
    query = urlencode({"msg": f"SMSGate accepted the message (HTTP {result.status_code})"})
    return RedirectResponse(f"/share?{query}", status_code=303)


@app.post("/share/settings")
def share_smsgate_settings(
    request: Request,
    url: str = Form(...),
    username: str = Form(...),
    password: str = Form(""),
):
    role = getattr(request.state, "cmos_role", None)
    if role and role != "EXECUTIVE":
        raise HTTPException(status_code=403, detail="Executive access required")
    try:
        _save_smsgate_settings(url, username, password)
    except ValueError as exc:
        return share_page(request, error=str(exc))
    return RedirectResponse("/share?msg=SMSGate+settings+saved", status_code=303)


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(
    request: Request,
    q: str = "",
    source: str = "",
    category: str = "",
    municipality: str = "",
    state: str = "all",
    window: str = "7d",
    min_priority: int = 1,
    page: int = 1,
    msg: str = "",
    error: str = "",
):
    clause, params, filters = _alert_filter(
        q=q,
        source=source,
        category=category,
        municipality=municipality,
        state=state,
        window=window,
        min_priority=min_priority,
    )
    q = filters["q"]
    source = filters["source"]
    category = filters["category"]
    municipality = filters["municipality"]
    state = filters["state"]
    window = filters["window"]
    min_priority = filters["min_priority"]
    result_total = int(
        query_one(f"SELECT count(*) AS total FROM alerts a {clause}", params).get("total") or 0
    )
    per_page = 100
    page = max(1, int(page))
    offset = (page - 1) * per_page
    alerts = query_all(
        f"""
        SELECT a.id AS alert_uuid,a.alert_id,a.source,a.category,a.subtype,a.status,a.event_action,
               a.title,a.message,a.priority,a.county,a.municipality,a.received_at,a.updated_at,
               a.observed_at,a.click_url,a.tags,
               coalesce(
                 CASE WHEN r.match_type='MANUAL_COORDINATE_CORRECTION' THEN r.resolved_label END,
                 nullif(a.location->>'label',''),nullif(a.location->>'address',''),r.resolved_label
               ) AS location_label,
               ST_Y(coalesce(a.geom,r.geom)) AS map_latitude,
               ST_X(coalesce(a.geom,r.geom)) AS map_longitude,
               r.match_type AS location_match_type,r.spatial_precision,
               r.confidence AS location_confidence,
               (a.geom IS NULL AND r.geom IS NOT NULL) AS map_approximate,
               coalesce(wm.matched_watches,'No Watch matched') AS matched_watches,
               coalesce(wm.watch_evidence,'[]'::jsonb) AS watch_evidence
        FROM alerts a
        LEFT JOIN geo_entity_resolutions r
          ON r.entity_type='ALERT' AND r.entity_id=a.id::text AND r.status='RESOLVED'
        LEFT JOIN LATERAL (
          SELECT string_agg(m.display_name,', ' ORDER BY m.display_name) AS matched_watches,
                 jsonb_agg(
                   jsonb_build_object(
                     'watch_name',m.display_name,
                     'match_reason',m.match_reason
                   ) ORDER BY m.display_name
                 ) AS watch_evidence
          FROM (
            SELECT DISTINCT ON (candidate.watch_key)
                   candidate.watch_key,candidate.display_name,
                   candidate.match_reason,candidate.happened_at
            FROM (
              SELECT w.id::text AS watch_key,w.display_name,awm.match_reason,
                     awm.matched_at AS happened_at
              FROM alert_watch_matches awm
              JOIN watch_items w ON w.id=awm.watch_item_id
              WHERE awm.alert_id=a.id
              UNION ALL
              SELECT coalesce(w.id::text,ids.watch_id) AS watch_key,
                     coalesce(w.display_name,'Deleted Watch') AS display_name,
                     d.match_reasons->>((ids.position-1)::int) AS match_reason,
                     d.created_at AS happened_at
              FROM deliveries d
              CROSS JOIN LATERAL jsonb_array_elements_text(
                coalesce(d.matched_watch_ids,'[]'::jsonb)
              ) WITH ORDINALITY AS ids(watch_id,position)
              LEFT JOIN watch_items w ON w.watch_id=ids.watch_id
              WHERE d.alert_id=a.id
            ) candidate
            ORDER BY candidate.watch_key,candidate.happened_at DESC
          ) m
        ) wm ON true
        {clause}
        ORDER BY a.received_at DESC,a.id
        LIMIT %s OFFSET %s
        """,
        [*params, per_page, offset],
    )
    for alert in alerts:
        alert["watch_evidence"] = [
            {
                "watch_name": item.get("watch_name") or "Saved Watch",
                "reason": _humanize_match_reason(item.get("match_reason")),
            }
            for item in (alert.get("watch_evidence") or [])
            if isinstance(item, dict)
        ]
        alert_reference = str(alert.get("alert_id") or "").strip()
        alert["keyword_choices"] = alert_keyword_choices(alert)
        alert["suggested_setup_mode"] = (
            "LOCATION_TOPIC"
            if alert.get("location_label") or alert.get("municipality")
            else "TOPIC"
        )
        alert["watch_from_alert_url"] = (
            f"/watchlist?{urlencode({'from_alert': alert_reference})}"
            if alert_reference
            else ""
        )
        alert["track_alert_url"] = (
            f"/issues?{urlencode({'from_alert': alert_reference})}"
            if alert_reference
            else ""
        )
        alert["share_url"] = f"/share?{urlencode({'alert': alert_reference})}" if alert_reference else ""
        alert["map_url"] = alert_map_url(alert)
        alert["map_status"] = (
            "Approximate location"
            if alert.get("map_approximate")
            else "Mapped location"
            if alert.get("map_latitude") is not None
            else "Location not mapped"
        )
    sources = query_all("SELECT source,count(*) AS total FROM alerts GROUP BY source ORDER BY source")
    categories = query_all("SELECT category,count(*) AS total FROM alerts GROUP BY category ORDER BY category")
    municipalities = query_all(
        "SELECT municipality,count(*) AS total FROM alerts WHERE nullif(trim(municipality),'') IS NOT NULL GROUP BY municipality ORDER BY municipality"
    )
    counts = query_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status <> 'RESOLVED' AND (expires_at IS NULL OR expires_at > now())) AS active,
               count(*) FILTER (WHERE status = 'RESOLVED') AS resolved
        FROM alerts
        """
    )
    total_pages = max(1, (result_total + per_page - 1) // per_page)
    previous_url = f"/alerts?{urlencode({**filters, 'page': page - 1})}" if page > 1 else ""
    next_url = f"/alerts?{urlencode({**filters, 'page': page + 1})}" if page < total_pages else ""
    return templates.TemplateResponse(
        request=request,
        name="alerts.html",
        context={
            "alerts": alerts,
            "sources": sources,
            "categories": categories,
            "municipalities": municipalities,
            "counts": counts,
            "result_total": result_total,
            "q": q,
            "source": source,
            "category": category,
            "municipality": municipality,
            "state": state,
            "window": window,
            "min_priority": min_priority,
            "current_page": page,
            "total_pages": total_pages,
            "previous_url": previous_url,
            "next_url": next_url,
            "current_url": f"/alerts?{urlencode({**filters, 'page': page})}",
            "msg": msg,
            "error": error,
            "can_delete_alerts": not getattr(request.state, "cmos_role", None)
            or getattr(request.state, "cmos_role", None) == "EXECUTIVE",
            "can_correct_alert_locations": getattr(request.state, "cmos_role", None)
            != "READ_ONLY",
            "page": "alerts",
        },
    )


def _alerts_redirect(return_to: str, *, message: str = "", error: str = "") -> RedirectResponse:
    target = (
        return_to
        if "\r" not in return_to
        and "\n" not in return_to
        and (return_to == "/alerts" or return_to.startswith("/alerts?"))
        else "/alerts"
    )
    query = urlencode({key: value for key, value in (("msg", message), ("error", error)) if value})
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}{query}" if query else target, status_code=303)


@app.post("/alerts/bulk-action")
def alerts_bulk_action(
    request: Request,
    alert_ids: list[uuid.UUID] = Form([]),
    action: str = Form(...),
    selection_scope: str = Form("selected"),
    confirm_delete: str = Form(""),
    return_to: str = Form("/alerts"),
    q: str = Form(""),
    source: str = Form(""),
    category: str = Form(""),
    municipality: str = Form(""),
    state: str = Form("all"),
    window: str = Form("7d"),
    min_priority: int = Form(1),
):
    try:
        action = action.strip().lower()
        if action not in {"resolve", "delete"}:
            raise HTTPException(400, "Choose Mark Resolved or Delete Permanently")
        selection_scope = selection_scope.strip().lower()
        if selection_scope not in {"selected", "matching"}:
            raise HTTPException(400, "Choose selected alerts or all matching search results")
        role = str(getattr(request.state, "cmos_role", "") or "").upper()
        if action == "delete" and role and role != "EXECUTIVE":
            raise HTTPException(403, "Only an Executive user can permanently delete alerts")

        selected = list(dict.fromkeys(alert_ids))
        clause = ""
        filter_params: list = []
        if selection_scope == "selected":
            if not selected:
                raise HTTPException(400, "Choose at least one alert")
            if len(selected) > ALERT_BULK_LIMIT:
                raise HTTPException(400, f"Choose {ALERT_BULK_LIMIT} or fewer alerts at a time")
            expected_confirmation = "DELETE" if action == "delete" else ""
        else:
            clause, filter_params, filters = _alert_filter(
                q=q,
                source=source,
                category=category,
                municipality=municipality,
                state=state,
                window=window,
                min_priority=min_priority,
            )
            narrowed = any(
                (
                    filters["q"],
                    filters["source"],
                    filters["category"],
                    filters["municipality"],
                    filters["state"] != "all",
                    filters["window"] != "all",
                    filters["min_priority"] > 1,
                )
            )
            if not narrowed:
                raise HTTPException(400, "Narrow the search before changing all matching alerts")
            expected_confirmation = "DELETE ALL" if action == "delete" else "APPLY ALL"
        if expected_confirmation and confirm_delete.strip().upper() != expected_confirmation:
            detail = (
                "Type DELETE to permanently delete the selected alerts"
                if expected_confirmation == "DELETE"
                else f"Type {expected_confirmation} to confirm this change"
            )
            raise HTTPException(400, detail)

        with db_conn() as conn:
            with conn.cursor() as cur:
                if selection_scope == "matching":
                    cur.execute(
                        f"""
                        SELECT a.id FROM alerts a {clause}
                        ORDER BY a.received_at DESC,a.id
                        LIMIT %s FOR UPDATE
                        """,
                        [*filter_params, ALERT_FILTERED_BULK_LIMIT + 1],
                    )
                else:
                    cur.execute(
                        "SELECT id FROM alerts WHERE id=ANY(%s::uuid[]) FOR UPDATE",
                        (selected,),
                    )
                found = [row["id"] for row in cur.fetchall()]
                if not found:
                    raise HTTPException(404, "No alerts in this selection still exist")
                if len(found) > ALERT_FILTERED_BULK_LIMIT:
                    raise HTTPException(
                        400,
                        f"This search has more than {ALERT_FILTERED_BULK_LIMIT:,} alerts. Narrow it before making a bulk change.",
                    )
                if action == "delete":
                    cur.execute(
                        "SELECT count(*) AS total FROM deliveries WHERE alert_id=ANY(%s::uuid[])",
                        (found,),
                    )
                    delivery_total = int(cur.fetchone()["total"] or 0)
                    cur.execute(
                        "DELETE FROM geo_entity_resolutions WHERE entity_type='ALERT' AND entity_id=ANY(%s::text[])",
                        ([str(alert_id) for alert_id in found],),
                    )
                    cur.execute("DELETE FROM alerts WHERE id=ANY(%s::uuid[])", (found,))
                else:
                    delivery_total = 0
                    cur.execute(
                        """
                        UPDATE alerts
                        SET status='RESOLVED',event_action='RESOLVED',updated_at=now()
                        WHERE id=ANY(%s::uuid[])
                        """,
                        (found,),
                    )
            conn.commit()

        if action == "delete":
            message = (
                f"Deleted {len(found)} alert{'s' if len(found) != 1 else ''} and "
                f"{delivery_total} related Notification record{'s' if delivery_total != 1 else ''}. "
                "A live source may send the alert again."
            )
        else:
            message = f"Marked {len(found)} alert{'s' if len(found) != 1 else ''} resolved. History was kept."
        return _alerts_redirect(return_to, message=message)
    except HTTPException as exc:
        return _alerts_redirect(return_to, error=str(exc.detail))
    except Exception:
        incident_id = uuid.uuid4().hex[:10].upper()
        LOGGER.exception("Alert bulk action failed incident=%s", incident_id)
        return _alerts_redirect(
            return_to,
            error=f"The alerts were not changed. Reference {incident_id}.",
        )


def _global_search_rows(q: str, scope: str):
    needle = f"%{q}%"
    prefix_end = f"{q}\U0010ffff"
    statements = []
    params = []

    def include(section):
        return scope == "all" or scope == section

    def add(section, sql, values):
        if include(section):
            statements.append(f"({sql.strip()})")
            params.extend(values)

    add(
        "alerts",
        """
        SELECT 'Alerts'::text AS section, 'ALERT'::text AS result_type,
               a.title, left(a.message,240) AS summary,
               concat_ws(' · ',a.source,a.category,nullif(a.municipality,'')) AS context,
               a.alert_id AS result_id, a.received_at AS happened_at
        FROM alerts a
        WHERE coalesce(a.search_text,'') ILIKE %s OR a.title ILIKE %s
           OR a.message ILIKE %s OR a.source ILIKE %s OR a.category ILIKE %s
           OR coalesce(a.municipality,'') ILIKE %s OR a.alert_id ILIKE %s
        ORDER BY a.received_at DESC
        LIMIT 12
        """,
        [needle] * 7,
    )
    add(
        "work",
        """
        SELECT 'Work Items'::text AS section, 'WORK_ITEM'::text AS result_type,
               i.title, left(coalesce(nullif(i.description,''),nullif(i.next_action,''),'No description'),240) AS summary,
               concat_ws(' · ',i.item_type,i.status,nullif(i.category,''),nullif(i.assigned_to,'')) AS context,
               i.id::text AS result_id, i.updated_at AS happened_at
        FROM issues i
        WHERE i.title ILIKE %s OR coalesce(i.description,'') ILIKE %s
           OR coalesce(i.category,'') ILIKE %s OR coalesce(i.next_action,'') ILIKE %s
           OR coalesce(i.waiting_on,'') ILIKE %s OR coalesce(i.assigned_to,'') ILIKE %s
           OR coalesce(i.address,'') ILIKE %s OR coalesce(i.municipality,'') ILIKE %s
        ORDER BY i.updated_at DESC
        LIMIT 12
        """,
        [needle] * 8,
    )
    add(
        "watches",
        """
        SELECT 'Watches'::text AS section, 'WATCH'::text AS result_type,
               w.display_name AS title,
               CASE WHEN nullif(w.search_term,'') IS NOT NULL
                    THEN 'Watches for ' || w.search_term
                    ELSE 'Location Watch' END AS summary,
               concat_ws(' · ',CASE WHEN w.active THEN 'Watching' ELSE 'Paused' END,
                         nullif(w.municipality,''),nullif(w.address,'')) AS context,
               w.watch_id AS result_id, w.updated_at AS happened_at
        FROM watch_items w
        WHERE w.display_name ILIKE %s OR w.search_term ILIKE %s
           OR array_to_string(w.aliases,' ') ILIKE %s OR array_to_string(w.tags,' ') ILIKE %s
           OR coalesce(w.municipality,'') ILIKE %s OR coalesce(w.address,'') ILIKE %s
           OR w.watch_id ILIKE %s
        ORDER BY w.updated_at DESC
        LIMIT 12
        """,
        [needle] * 7,
    )
    add(
        "notifications",
        """
        SELECT 'Notifications'::text AS section, 'NOTIFICATION'::text AS result_type,
               a.title,
               CASE WHEN d.status='SENT' THEN 'Delivered to ' || s.name
                    WHEN d.status='FAILED' THEN 'Delivery problem for ' || s.name
                    ELSE initcap(lower(d.status)) || ' for ' || s.name END AS summary,
               concat_ws(' · ',a.source,d.status) AS context,
               d.id::text AS result_id, coalesce(d.sent_at,d.attempted_at,d.created_at) AS happened_at
        FROM deliveries d
        JOIN alerts a ON a.id=d.alert_id
        JOIN subscribers s ON s.id=d.subscriber_id
        WHERE a.title ILIKE %s OR a.message ILIKE %s OR a.source ILIKE %s
           OR s.name ILIKE %s OR d.ntfy_topic ILIKE %s
           OR d.match_reasons::text ILIKE %s OR d.matched_watch_ids::text ILIKE %s
        ORDER BY coalesce(d.sent_at,d.attempted_at,d.created_at) DESC
        LIMIT 12
        """,
        [needle] * 7,
    )
    add(
        "events",
        """
        SELECT 'Events'::text AS section, 'MANAGED_EVENT'::text AS result_type,
               e.title, left(coalesce(nullif(e.notes,''),nullif(e.impact_notes,''),'Managed event'),240) AS summary,
               concat_ws(' · ','Managed',nullif(e.category,''),nullif(e.municipality,''),e.event_status) AS context,
               e.id::text AS result_id, coalesce(e.starts_at,e.updated_at) AS happened_at
        FROM operational_events e
        WHERE e.title ILIKE %s OR coalesce(e.notes,'') ILIKE %s
           OR coalesce(e.impact_notes,'') ILIKE %s OR coalesce(e.category,'') ILIKE %s
           OR coalesce(e.location_name,'') ILIKE %s OR coalesce(e.address,'') ILIKE %s
           OR coalesce(e.municipality,'') ILIKE %s OR coalesce(e.owner,'') ILIKE %s
        ORDER BY coalesce(e.starts_at,e.updated_at) DESC
        LIMIT 8
        """,
        [needle] * 8,
    )
    add(
        "events",
        """
        SELECT 'Events'::text AS section, 'EVENT_INTELLIGENCE'::text AS result_type,
               e.title, left(coalesce(nullif(e.description,''),nullif(e.impact_summary,''),'Event intelligence'),240) AS summary,
               concat_ws(' · ','Intelligence',nullif(e.event_type,''),nullif(e.venue,''),nullif(e.municipality,''),e.impact_level) AS context,
               e.id::text AS result_id, coalesce(e.starts_at,e.last_changed_at) AS happened_at
        FROM event_intelligence e
        WHERE e.title ILIKE %s OR coalesce(e.description,'') ILIKE %s
           OR coalesce(e.event_type,'') ILIKE %s OR coalesce(e.venue,'') ILIKE %s
           OR coalesce(e.address,'') ILIKE %s OR coalesce(e.municipality,'') ILIKE %s
           OR coalesce(e.source_name,'') ILIKE %s OR coalesce(e.impact_summary,'') ILIKE %s
        ORDER BY coalesce(e.starts_at,e.last_changed_at) DESC
        LIMIT 8
        """,
        [needle] * 8,
    )
    add(
        "transit",
        """
        SELECT 'Transit'::text AS section, 'TRANSIT_OBSERVATION'::text AS result_type,
               o.title, left(coalesce(nullif(o.description,''),'Transit observation'),240) AS summary,
               concat_ws(' · ',p.name,nullif(o.route_name,''),nullif(o.asset_name,''),o.impact_level) AS context,
               o.id::text AS result_id, o.last_changed_at AS happened_at
        FROM transit_observations o
        JOIN transit_providers p ON p.id=o.provider_id
        WHERE o.title ILIKE %s OR coalesce(o.description,'') ILIKE %s
           OR coalesce(o.route_name,'') ILIKE %s OR coalesce(o.asset_name,'') ILIKE %s
           OR coalesce(o.municipality,'') ILIKE %s OR p.name ILIKE %s
           OR coalesce(o.external_key,'') ILIKE %s
        ORDER BY o.last_changed_at DESC
        LIMIT 10
        """,
        [needle] * 7,
    )
    add(
        "transit",
        """
        SELECT 'Transit'::text AS section, 'TRANSIT_ASSET'::text AS result_type,
               a.name AS title, concat_ws(' · ',a.asset_type,nullif(a.mode,''),nullif(a.short_name,'')) AS summary,
               concat_ws(' · ',p.name,nullif(a.municipality,'')) AS context,
               a.id::text AS result_id, a.updated_at AS happened_at
        FROM transit_assets a
        JOIN transit_providers p ON p.id=a.provider_id
        WHERE a.name ILIKE %s OR coalesce(a.short_name,'') ILIKE %s
           OR a.asset_key ILIKE %s OR a.asset_type ILIKE %s OR coalesce(a.mode,'') ILIKE %s
           OR coalesce(a.municipality,'') ILIKE %s OR p.name ILIKE %s
        ORDER BY a.updated_at DESC
        LIMIT 10
        """,
        [needle] * 7,
    )
    add(
        "locations",
        """
        SELECT 'Locations'::text AS section, 'ADDRESS'::text AS result_type,
               a.fulladdr AS title, concat_ws(' · ',a.post_comm,a.post_code) AS summary,
               'Address'::text AS context, a.objectid::text AS result_id, NULL::timestamptz AS happened_at
        FROM gis_addresses a
        WHERE a.geom IS NOT NULL
          AND ((lower(a.fulladdr)>=lower(%s) AND lower(a.fulladdr)<lower(%s))
               OR lower(a.post_comm)=lower(%s))
        ORDER BY CASE WHEN a.status='A' THEN 0 ELSE 1 END,a.fulladdr
        LIMIT 10
        """,
        [q, prefix_end, q],
    )
    add(
        "locations",
        """
        SELECT 'Locations'::text AS section, 'PARCEL'::text AS result_type,
               coalesce(nullif(p.prop_loc,''),'Block ' || coalesce(p.pclblock,'?') || ' Lot ' || coalesce(p.pcllot,'?')) AS title,
               concat_ws(' · ',p.mun_name,'Block ' || coalesce(p.pclblock,'?'),'Lot ' || coalesce(p.pcllot,'?')) AS summary,
               'Parcel'::text AS context, p.objectid::text AS result_id, NULL::timestamptz AS happened_at
        FROM gis_parcels p
        WHERE p.geom IS NOT NULL AND (
          (lower(p.prop_loc)>=lower(%s) AND lower(p.prop_loc)<lower(%s))
          OR (p.pams_pin>=%s AND p.pams_pin<%s)
          OR lower(p.mun_name)=lower(%s)
        )
        ORDER BY p.prop_loc NULLS LAST,p.objectid
        LIMIT 10
        """,
        [q, prefix_end, q, prefix_end, q],
    )
    add(
        "locations",
        """
        SELECT 'Locations'::text AS section, 'REFERENCE'::text AS result_type,
               r.canonical_name AS title,
               concat_ws(' · ',r.entity_type,nullif(r.entity_subtype,''),nullif(r.municipality,'')) AS summary,
               'Saved map reference'::text AS context, r.entity_id::text AS result_id, r.updated_at AS happened_at
        FROM spatial_reference_entities r
        WHERE r.active=true AND (
          r.canonical_name ILIKE %s OR coalesce(r.normalized_address,'') ILIKE %s
          OR EXISTS (SELECT 1 FROM unnest(r.aliases) alias WHERE alias ILIKE %s)
        )
        ORDER BY r.importance_tier,r.canonical_name
        LIMIT 10
        """,
        [needle] * 3,
    )
    add(
        "locations",
        """
        SELECT 'Locations'::text AS section, 'MAP_FEATURE'::text AS result_type,
               coalesce(nullif(f.name,''),l.name) AS title, l.name AS summary,
               'Mapping Center layer'::text AS context, f.id::text AS result_id, f.updated_at AS happened_at
        FROM map_features f JOIN map_layers l ON l.id=f.layer_id
        WHERE f.active=true AND l.active=true AND coalesce(f.name,'') ILIKE %s
        ORDER BY f.updated_at DESC
        LIMIT 10
        """,
        [needle],
    )
    add(
        "sources",
        """
        SELECT 'Sources'::text AS section, 'INTEGRATION'::text AS result_type,
               i.name AS title, left(coalesce(nullif(i.notes,''),i.category),240) AS summary,
               concat_ws(' · ',i.category,CASE WHEN i.active THEN 'Active' ELSE 'Inactive' END) AS context,
               i.integration_key AS result_id, i.updated_at AS happened_at
        FROM integrations i
        WHERE i.name ILIKE %s OR i.integration_key ILIKE %s OR i.category ILIKE %s
           OR coalesce(i.notes,'') ILIKE %s
        ORDER BY i.updated_at DESC
        LIMIT 10
        """,
        [needle] * 4,
    )
    add(
        "sources",
        """
        SELECT 'Sources'::text AS section, 'SOURCE_HEALTH'::text AS result_type,
               h.source_id AS title, coalesce(nullif(h.last_error,''),'No current error') AS summary,
               'Source health · ' || h.status AS context, h.source_id AS result_id, h.updated_at AS happened_at
        FROM source_health h
        WHERE h.source_id ILIKE %s OR h.status ILIKE %s OR coalesce(h.last_error,'') ILIKE %s
        ORDER BY h.updated_at DESC
        LIMIT 10
        """,
        [needle] * 3,
    )
    add(
        "people",
        """
        SELECT 'Recipients'::text AS section, 'RECIPIENT'::text AS result_type,
               s.name AS title, left(coalesce(nullif(s.notes,''),'Notification Recipient'),240) AS summary,
               CASE WHEN s.active THEN 'Active Recipient' ELSE 'Paused Recipient' END AS context,
               s.id::text AS result_id, s.updated_at AS happened_at
        FROM subscribers s
        WHERE s.name ILIKE %s OR s.subscriber_id ILIKE %s
           OR coalesce(s.notes,'') ILIKE %s OR s.ntfy_topic ILIKE %s
        ORDER BY s.updated_at DESC
        LIMIT 12
        """,
        [needle] * 4,
    )
    add(
        "people",
        """
        SELECT 'Staff'::text AS section, 'STAFF_MEMBER'::text AS result_type,
               e.full_name AS title, concat_ws(' · ',e.department,e.role) AS summary,
               CASE WHEN e.active THEN 'Active staff member' ELSE 'Inactive staff member' END AS context,
               e.id::text AS result_id, e.updated_at AS happened_at
        FROM staff_employees e
        WHERE e.full_name ILIKE %s OR e.employee_id ILIKE %s
           OR e.department ILIKE %s OR e.role ILIKE %s
        ORDER BY e.updated_at DESC
        LIMIT 12
        """,
        [needle] * 4,
    )
    add(
        "operations",
        """
        SELECT 'Routines'::text AS section, 'ROUTINE'::text AS result_type,
               r.name AS title, left(coalesce(nullif(r.description,''),nullif(r.notes,''),'Operations routine'),240) AS summary,
               concat_ws(' · ',r.routine_kind,nullif(r.department,''),nullif(r.location_label,'')) AS context,
               r.id::text AS result_id, r.updated_at AS happened_at
        FROM operations_routines r
        WHERE r.name ILIKE %s OR coalesce(r.description,'') ILIKE %s
           OR coalesce(r.notes,'') ILIKE %s OR coalesce(r.department,'') ILIKE %s
           OR coalesce(r.location_label,'') ILIKE %s
        ORDER BY r.updated_at DESC
        LIMIT 12
        """,
        [needle] * 5,
    )
    add(
        "operations",
        """
        SELECT 'Managed Locations'::text AS section, 'MANAGED_LOCATION'::text AS result_type,
               l.name AS title, coalesce(nullif(l.department,''),'Shared municipal Location') AS summary,
               CASE WHEN l.active THEN 'Active Location' ELSE 'Inactive Location' END AS context,
               l.id::text AS result_id, l.updated_at AS happened_at
        FROM staff_locations l
        WHERE l.name ILIKE %s OR coalesce(l.department,'') ILIKE %s
        ORDER BY l.updated_at DESC
        LIMIT 12
        """,
        [needle] * 2,
    )
    add(
        "configuration",
        """
        SELECT 'Rules'::text AS section, 'RULE_GROUP'::text AS result_type,
               concat_ws(' / ',s.name,ss.name) AS title, 'Watch organization'::text AS summary,
               CASE WHEN s.active AND ss.active THEN 'Active rule group' ELSE 'Inactive rule group' END AS context,
               ss.id::text AS result_id, greatest(s.updated_at,ss.updated_at) AS happened_at
        FROM rule_subsections ss
        JOIN rule_sections s ON s.id=ss.section_id
        WHERE s.name ILIKE %s OR s.slug ILIKE %s OR ss.name ILIKE %s OR ss.slug ILIKE %s
        ORDER BY greatest(s.updated_at,ss.updated_at) DESC
        LIMIT 12
        """,
        [needle] * 4,
    )
    add(
        "configuration",
        """
        SELECT 'Map Layers'::text AS section, 'MAP_LAYER'::text AS result_type,
               l.name AS title, concat_ws(' · ',replace(l.layer_type,'_',' '),nullif(l.attribution,'')) AS summary,
               CASE WHEN l.active THEN 'Active map layer' ELSE 'Inactive map layer' END AS context,
               l.id::text AS result_id, l.updated_at AS happened_at
        FROM map_layers l
        WHERE l.name ILIKE %s OR l.layer_key ILIKE %s OR l.layer_type ILIKE %s
           OR coalesce(l.attribution,'') ILIKE %s OR coalesce(l.source_url,'') ILIKE %s
        ORDER BY l.updated_at DESC
        LIMIT 12
        """,
        [needle] * 5,
    )
    add(
        "conditions",
        """
        SELECT 'Flood'::text AS section, 'FLOOD_OBSERVATION'::text AS result_type,
               coalesce(nullif(f.title,''),'Flood observation') AS title,
               concat_ws(' · ',f.source,nullif(f.station_id,''),nullif(f.flood_category,'')) AS summary,
               'Observed flood condition'::text AS context,
               f.id::text AS result_id, f.observed_at AS happened_at
        FROM flood_observations f
        WHERE coalesce(f.title,'') ILIKE %s OR f.source ILIKE %s
           OR coalesce(f.station_id,'') ILIKE %s OR coalesce(f.flood_category,'') ILIKE %s
        ORDER BY f.observed_at DESC
        LIMIT 12
        """,
        [needle] * 4,
    )
    add(
        "conditions",
        """
        SELECT 'Utility State'::text AS section, 'UTILITY_STATE'::text AS result_type,
               CASE WHEN nullif(p.municipality,'') IS NOT NULL THEN p.municipality ELSE 'New Jersey Statewide' END AS title,
               concat_ws(' · ',p.customers_out::text || ' customers out',nullif(p.etr,'')) AS summary,
               concat_ws(' · ','PSEG',nullif(p.county,''),p.scope) AS context,
               concat_ws(':',p.scope,p.county,p.municipality) AS result_id,
               p.updated_at AS happened_at
        FROM pseg_outage_state p
        WHERE p.municipality ILIKE %s OR p.county ILIKE %s OR p.scope ILIKE %s
           OR coalesce(p.etr,'') ILIKE %s OR coalesce(p.last_alert_reason,'') ILIKE %s
        ORDER BY p.updated_at DESC
        LIMIT 12
        """,
        [needle] * 5,
    )

    if not statements:
        return []
    sql = "SELECT * FROM (" + "\nUNION ALL\n".join(statements) + ") results " \
          "ORDER BY happened_at DESC NULLS LAST,title LIMIT 220"
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '12s'")
            cur.execute(sql, params)
            return cur.fetchall()


def _global_result_url(row, q):
    query = urlencode({"q": q})
    result_type = row.get("result_type")
    if result_type == "ALERT":
        return f"/alerts?{urlencode({'q': q, 'window': 'all'})}"
    if result_type == "WORK_ITEM":
        return f"/issues?{urlencode({'q': q, 'state': 'all'})}"
    if result_type == "WATCH":
        return f"/watchlist?{query}"
    if result_type == "NOTIFICATION":
        return f"/deliveries?{query}"
    if result_type == "MANAGED_EVENT":
        return f"/schedule?{urlencode({'q': q, 'state': 'all'})}"
    if result_type == "EVENT_INTELLIGENCE":
        return f"/event-intelligence?{urlencode({'q': q, 'horizon': 'all'})}"
    if result_type in {"TRANSIT_OBSERVATION", "TRANSIT_ASSET"}:
        return f"/transit?{query}"
    if result_type in {"ADDRESS", "PARCEL", "REFERENCE", "MAP_FEATURE"}:
        return f"/map?{query}"
    if result_type == "INTEGRATION":
        return "/integrations"
    if result_type == "RECIPIENT":
        return f"/subscribers?{query}"
    if result_type in {"STAFF_MEMBER", "MANAGED_LOCATION"}:
        return "/staff-admin"
    if result_type == "ROUTINE":
        return "/operations-routines"
    if result_type == "RULE_GROUP":
        return f"/rules?{query}"
    if result_type == "MAP_LAYER":
        return f"/map?{query}"
    if result_type == "FLOOD_OBSERVATION":
        return "/flood"
    if result_type == "UTILITY_STATE":
        return "/integrations/pseg"
    return "/source-health"


@app.get("/search", response_class=HTMLResponse)
def global_search_page(request: Request, q: str = "", scope: str = "all"):
    q = q.strip()[:160]
    if scope not in SEARCH_SCOPES:
        scope = "all"
    rows = []
    error = ""
    if q and len(q) < 2:
        error = "Enter at least two characters to search."
    elif q:
        try:
            rows = _global_search_rows(q, scope)
        except Exception:
            LOGGER.exception("Global search failed")
            error = "Search is temporarily unavailable. Please try again."
    grouped = {section: [] for section in SEARCH_SECTION_ORDER}
    for row in rows:
        row["url"] = _global_result_url(row, q)
        grouped.setdefault(row["section"], []).append(row)
    grouped = {section: grouped[section] for section in SEARCH_SECTION_ORDER if grouped.get(section)}
    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context={
            "q": q,
            "scope": scope,
            "scopes": SEARCH_SCOPES,
            "groups": grouped,
            "result_total": len(rows),
            "error": error,
            "page": "search",
        },
    )


@app.get("/modules", response_class=HTMLResponse)
def modules_page(request: Request):
    health_rows = query_all("SELECT * FROM source_health ORDER BY source_id")
    health = {str(row["source_id"]).upper(): row for row in health_rows}
    alert_rows = query_all(
        """
        SELECT upper(source) AS source_id,
               count(*) AS total_alerts,
               count(*) FILTER (WHERE status <> 'RESOLVED' AND (expires_at IS NULL OR expires_at > now())) AS active_alerts,
               max(received_at) AS last_alert_at
        FROM alerts
        GROUP BY upper(source)
        """
    )
    alert_stats = {str(row["source_id"]).upper(): row for row in alert_rows}
    modules = []
    for item in MODULES:
        key = item["key"]
        h = health.get(key)
        a = alert_stats.get(key)
        modules.append({
            **item,
            "status": h.get("status") if h else ("DATA" if a else "NOT CONNECTED"),
            "last_success_at": h.get("last_success_at") if h else None,
            "last_event_at": h.get("last_event_at") if h else (a.get("last_alert_at") if a else None),
            "last_error": h.get("last_error") if h else None,
            "active_alerts": a.get("active_alerts") if a else 0,
            "total_alerts": a.get("total_alerts") if a else 0,
        })
    return templates.TemplateResponse(request=request, name="modules.html", context={"modules": modules, "page": "modules"})


@app.get("/source-health", response_class=HTMLResponse)
def source_health_page(request: Request):
    rows = query_all(
        """
        SELECT source_id, status, last_attempt_at, last_success_at,
               last_event_at, last_error, metadata, updated_at
        FROM source_health
        ORDER BY source_id
        """
    )
    return templates.TemplateResponse(request=request, name="source_health.html", context={"rows": rows, "page": "source-health"})


def _humanize_match_reason(reason):
    text = str(reason or "").strip()
    if not text:
        return "This Watch matched, but an explanation was not recorded."

    parts = []
    for raw_part in re.split(r";\s*", text):
        part = raw_part.strip()
        keyword = re.search(
            r"(?:FIELD|CONTAINS|WORD|EXACT)\s+\S+\s+matched\s+"
            r"(search_term|alias)\s+[\"']([^\"']+)[\"']",
            part,
            flags=re.IGNORECASE,
        )
        if keyword:
            label = "Alternate keyword" if keyword.group(1).lower() == "alias" else "Keyword"
            parts.append(f"{label} “{keyword.group(2)}” matched this alert")
            continue
        distance = re.search(
            r"PROXIMITY alert geometry is ([0-9.]+) ft from target, inside ([0-9.]+) ft buffer",
            part,
            flags=re.IGNORECASE,
        )
        if distance:
            measured = f"{float(distance.group(1)):,.0f}"
            allowed = f"{float(distance.group(2)):,.0f}"
            parts.append(
                f"Alert Location was {measured} feet from the Watch center, within the {allowed}-foot Distance"
            )
            continue
        if "selected parcel or adjoining-parcel" in part.lower():
            parts.append("Alert Location matched the selected parcel or a neighboring parcel")
            continue
        if "intersected the selected reference" in part.lower():
            parts.append("Alert Location matched the selected map area")
            continue
        municipality = re.search(r"municipality matched [\"']([^\"']+)[\"']", part, re.IGNORECASE)
        if municipality:
            parts.append(f"Alert municipality matched “{municipality.group(1)}”")
            continue
        if part.lower() == "manual ntfy sender test":
            parts.append("Manual Notification test")
            continue
        friendly = re.sub(r"\bPROXIMITY\b", "Location", part, flags=re.IGNORECASE)
        friendly = re.sub(r"\balert geometry\b", "Alert Location", friendly, flags=re.IGNORECASE)
        friendly = re.sub(r"\bsearch_text\b", "alert text", friendly, flags=re.IGNORECASE)
        friendly = re.sub(r"\bsearch_term\b", "keyword", friendly, flags=re.IGNORECASE)
        friendly = re.sub(r"\btarget\b", "Watch center", friendly, flags=re.IGNORECASE)
        friendly = re.sub(r"\bbuffer\b", "Distance", friendly, flags=re.IGNORECASE)
        parts.append(friendly[:240])
    return "; ".join(part for part in parts if part)


def _delivery_evidence(row):
    watches = row.get("matched_watches") or []
    reasons = row.get("match_reasons") or []
    if not isinstance(watches, list):
        watches = []
    if not isinstance(reasons, list):
        reasons = []
    evidence = []
    count = max(len(watches), len(reasons))
    for index in range(count):
        watch = watches[index] if index < len(watches) and isinstance(watches[index], dict) else {}
        reason = watch.get("match_reason") or (reasons[index] if index < len(reasons) else "")
        evidence.append(
            {
                "watch_name": watch.get("display_name") or "Saved Watch",
                "reason": _humanize_match_reason(reason),
            }
        )
    return evidence


@app.get("/deliveries", response_class=HTMLResponse)
def deliveries_page(request: Request, status: str = "", q: str = ""):
    where = []
    params = []
    if status.strip():
        where.append("upper(d.status) = upper(%s)")
        params.append(status.strip())
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "(a.title ILIKE %s OR a.message ILIKE %s OR s.name ILIKE %s "
            "OR d.ntfy_topic ILIKE %s OR a.source ILIKE %s "
            "OR d.match_reasons::text ILIKE %s OR coalesce(mw.watch_search,'') ILIKE %s)"
        )
        params.extend([needle] * 7)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = query_all(
        f"""
        SELECT d.id, d.status, d.ntfy_topic, d.attempted_at, d.sent_at,
               d.error_message, d.matched_watch_ids, d.match_reasons,
               coalesce(mw.matched_watches,'[]'::jsonb) AS matched_watches,
               s.name AS subscriber_name, s.subscriber_id,
               a.title AS alert_title, a.source, a.alert_id
        FROM deliveries d
        JOIN subscribers s ON s.id = d.subscriber_id
        JOIN alerts a ON a.id = d.alert_id
        LEFT JOIN LATERAL (
          SELECT jsonb_agg(
                   jsonb_build_object(
                     'watch_id', ids.watch_id,
                     'display_name', coalesce(w.display_name,'Saved Watch'),
                     'match_reason', coalesce(awm.match_reason,d.match_reasons->>((ids.position-1)::int))
                   ) ORDER BY ids.position
                 ) AS matched_watches,
                 string_agg(coalesce(w.display_name,ids.watch_id),' ') AS watch_search
          FROM jsonb_array_elements_text(coalesce(d.matched_watch_ids,'[]'::jsonb))
               WITH ORDINALITY AS ids(watch_id,position)
          LEFT JOIN watch_items w ON w.watch_id=ids.watch_id
          LEFT JOIN LATERAL (
            SELECT m.match_reason
            FROM alert_watch_matches m
            WHERE m.alert_id=d.alert_id AND m.watch_item_id=w.id
            ORDER BY m.matched_at DESC
            LIMIT 1
          ) awm ON true
        ) mw ON true
        {clause}
        ORDER BY d.created_at DESC
        LIMIT 300
        """,
        params,
    )
    for row in rows:
        row["evidence"] = _delivery_evidence(row)
        row["track_alert_url"] = f"/issues?{urlencode({'from_alert': row['alert_id']})}"
    return templates.TemplateResponse(request=request, name="deliveries.html", context={"rows": rows, "status": status, "q": q, "page": "deliveries"})


@app.get("/subscribers", response_class=HTMLResponse)
def subscribers_page(
    request: Request,
    q: str = "",
    state: str = "all",
    msg: str = "",
    error: str = "",
    manage: str = "",
):
    where = []
    params = []
    if state == "active":
        where.append("s.active = true")
    elif state == "inactive":
        where.append("s.active = false")
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append("(s.name ILIKE %s OR s.subscriber_id ILIKE %s OR s.ntfy_topic ILIKE %s)")
        params.extend([needle, needle, needle])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = query_all(
        f"""
        SELECT s.id, s.subscriber_id, s.name, s.active, s.ntfy_topic, s.notes,
               s.created_at, s.updated_at,
               count(wir.id) FILTER (WHERE wir.active AND w.active) AS active_routes,
               coalesce(
                 array_agg(wir.watch_item_id::text ORDER BY wir.watch_item_id)
                   FILTER (WHERE wir.active),
                 ARRAY[]::text[]
               ) AS active_watch_ids
        FROM subscribers s
        LEFT JOIN watch_item_recipients wir ON wir.subscriber_id = s.id
        LEFT JOIN watch_items w ON w.id = wir.watch_item_id
        {clause}
        GROUP BY s.id
        ORDER BY s.active DESC, s.name
        LIMIT 250
        """,
        params,
    )
    managed_subscriber = manage.strip()
    for row in rows:
        row["active_watch_ids"] = set(row.get("active_watch_ids") or [])
        row["manage_watches"] = str(row["id"]) == managed_subscriber
    watch_options = []
    if any(row["manage_watches"] for row in rows):
        watch_options = query_all(
            """
            SELECT id::text AS id,display_name,
                   CASE
                     WHEN expires_at IS NOT NULL AND expires_at<=now() THEN 'Expired'
                     WHEN active THEN 'On'
                     ELSE 'Paused'
                   END AS state_label
            FROM watch_items
            ORDER BY active DESC,display_name
            LIMIT 500
            """
        )
    counts = query_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE active) AS active,
               count(*) FILTER (WHERE NOT active) AS inactive
        FROM subscribers
        """
    )
    return templates.TemplateResponse(
        request=request,
        name="subscribers.html",
        context={
            "rows": rows,
            "counts": counts,
            "watch_options": watch_options,
            "q": q,
            "state": state,
            "msg": msg,
            "error": error,
            "page": "subscribers",
        },
    )


@app.post("/subscribers/create")
def subscriber_create(name: str = Form(...), ntfy_topic: str = Form(...), notes: str = Form(""), subscriber_id: str = Form("")):
    name = name.strip()
    ntfy_topic = ntfy_topic.strip()
    if not name or not ntfy_topic:
        raise HTTPException(status_code=400, detail="Recipient name and Notification channel are required")
    sid = subscriber_id.strip().upper() or make_subscriber_id(name)
    execute(
        """
        INSERT INTO subscribers (subscriber_id, name, active, ntfy_topic, notes)
        VALUES (%s, %s, true, %s, %s)
        """,
        (sid, name, ntfy_topic, notes.strip() or None),
    )
    return RedirectResponse(url="/subscribers?msg=Recipient+created", status_code=303)


@app.post("/subscribers/{subscriber_uuid}/update")
def subscriber_update(subscriber_uuid: uuid.UUID, name: str = Form(...), ntfy_topic: str = Form(...), notes: str = Form(""), active: str | None = Form(None)):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT active FROM subscribers WHERE id=%s FOR UPDATE", (subscriber_uuid,))
        current = cur.fetchone()
        if not current:
            raise HTTPException(404, "Recipient not found")
        cur.execute(
            """SELECT watch_item_id FROM watch_item_recipients
               WHERE subscriber_id=%s AND active=true""",
            (subscriber_uuid,),
        )
        affected = [row["watch_item_id"] for row in cur.fetchall()]
        cur.execute(
            """
            UPDATE subscribers
            SET name=%s,ntfy_topic=%s,notes=%s,active=%s,updated_at=now()
            WHERE id=%s
            RETURNING id
            """,
            (name.strip(), ntfy_topic.strip(), notes.strip() or None, active is not None, subscriber_uuid),
        )
        cur.fetchone()
        if current["active"] and active is None:
            require_watch_recipients(cur, affected)
        conn.commit()
    return RedirectResponse(url="/subscribers?msg=Recipient+updated", status_code=303)


@app.post("/subscribers/{subscriber_uuid}/toggle")
def subscriber_toggle(subscriber_uuid: uuid.UUID):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT watch_item_id FROM watch_item_recipients
               WHERE subscriber_id=%s AND active=true""",
            (subscriber_uuid,),
        )
        affected = [row["watch_item_id"] for row in cur.fetchall()]
        cur.execute(
            """UPDATE subscribers SET active=NOT active,updated_at=now()
               WHERE id=%s RETURNING active""",
            (subscriber_uuid,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Recipient not found")
        if not row["active"]:
            require_watch_recipients(cur, affected)
        conn.commit()
    return RedirectResponse(url="/subscribers?msg=Recipient+status+changed", status_code=303)


@app.post("/subscribers/{subscriber_uuid}/watches")
def subscriber_watches_update(
    subscriber_uuid: uuid.UUID,
    watch_item_ids: list[uuid.UUID] = Form([]),
    visible_watch_item_ids: list[uuid.UUID] = Form([]),
):
    active_total = 0
    try:
        selected = list(dict.fromkeys(watch_item_ids))
        visible = list(dict.fromkeys(visible_watch_item_ids))
        if not set(selected).issubset(visible):
            raise HTTPException(400, "Review the visible Watch choices and try again")
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,active FROM subscribers WHERE id=%s FOR UPDATE", (subscriber_uuid,))
                subscriber = cur.fetchone()
                if not subscriber:
                    raise HTTPException(404, "That Recipient no longer exists")
                if selected and not subscriber["active"]:
                    raise HTTPException(400, "Reactivate this Recipient before assigning Watches")
                for watch_item_id in selected:
                    cur.execute("SELECT id FROM watch_items WHERE id=%s", (watch_item_id,))
                    if not cur.fetchone():
                        raise HTTPException(400, "One or more selected Watches no longer exist")
                if visible:
                    cur.execute(
                        """
                        SELECT watch_item_id
                        FROM watch_item_recipients
                        WHERE subscriber_id=%s AND active=true
                          AND watch_item_id=ANY(%s::uuid[])
                          AND NOT (watch_item_id=ANY(%s::uuid[]))
                        """,
                        (subscriber_uuid, visible, selected),
                    )
                    removed = [row["watch_item_id"] for row in cur.fetchall()]
                    cur.execute(
                        """
                        UPDATE watch_item_recipients SET active=false
                        WHERE subscriber_id=%s AND watch_item_id=ANY(%s::uuid[])
                        """,
                        (subscriber_uuid, visible),
                    )
                if selected:
                    cur.executemany(
                        """
                        INSERT INTO watch_item_recipients(watch_item_id,subscriber_id,active)
                        VALUES(%s,%s,true)
                        ON CONFLICT(watch_item_id,subscriber_id) DO UPDATE SET active=true
                        """,
                        [(watch_item_id, subscriber_uuid) for watch_item_id in selected],
                    )
                require_watch_recipients(cur, removed if visible else [])
                cur.execute(
                    "SELECT count(*) AS total FROM watch_item_recipients WHERE subscriber_id=%s AND active=true",
                    (subscriber_uuid,),
                )
                active_total = int(cur.fetchone()["total"])
            conn.commit()
    except HTTPException as exc:
        return RedirectResponse(
            url=(
                f"/subscribers?{urlencode({'manage': str(subscriber_uuid), 'error': str(exc.detail)})}"
                f"#recipient-{subscriber_uuid}"
            ),
            status_code=303,
        )
    except Exception:
        incident_id = uuid.uuid4().hex[:10].upper()
        LOGGER.exception("Recipient Watch assignment failed incident=%s", incident_id)
        return RedirectResponse(
            url=(
                f"/subscribers?{urlencode({'manage': str(subscriber_uuid), 'error': f'Watch assignments were not changed. Reference {incident_id}.'})}"
                f"#recipient-{subscriber_uuid}"
            ),
            status_code=303,
        )
    return RedirectResponse(
        url=(
            f"/subscribers?{urlencode({'manage': str(subscriber_uuid), 'msg': f'Recipient now follows {active_total} Watches'})}"
            f"#recipient-{subscriber_uuid}"
        ),
        status_code=303,
    )


@app.get("/routing", response_class=HTMLResponse)
def routing_page(request: Request, msg: str = ""):
    routes = query_all(
        """
        SELECT wir.id, wir.active,
               w.watch_id, w.display_name AS watch_name, w.watch_type,
               s.subscriber_id, s.name AS subscriber_name, s.ntfy_topic
        FROM watch_item_recipients wir
        JOIN watch_items w ON w.id = wir.watch_item_id
        JOIN subscribers s ON s.id = wir.subscriber_id
        ORDER BY wir.active DESC, w.display_name, s.name
        LIMIT 500
        """
    )
    watch_items = query_all("SELECT id, watch_id, display_name FROM watch_items WHERE active ORDER BY display_name")
    subscribers = query_all("SELECT id, subscriber_id, name, ntfy_topic FROM subscribers WHERE active ORDER BY name")
    counts = query_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE active) AS active,
               count(*) FILTER (WHERE NOT active) AS inactive
        FROM watch_item_recipients
        """
    )
    return templates.TemplateResponse(request=request, name="routing.html", context={"routes": routes, "watch_items": watch_items, "subscribers": subscribers, "counts": counts, "msg": msg, "page": "routing"})


@app.post("/routing/create")
def routing_create(watch_item_id: uuid.UUID = Form(...), subscriber_id: uuid.UUID = Form(...)):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watch_item_recipients (watch_item_id,subscriber_id,active)
            VALUES (%s,%s,true)
            ON CONFLICT (watch_item_id,subscriber_id) DO UPDATE SET active=true
            """,
            (watch_item_id, subscriber_id),
        )
        require_watch_recipients(cur, [watch_item_id])
        conn.commit()
    return RedirectResponse(url="/routing?msg=Delivery+connection+activated", status_code=303)


@app.post("/routing/{route_id}/toggle")
def routing_toggle(route_id: uuid.UUID):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE watch_item_recipients SET active=NOT active
               WHERE id=%s RETURNING active,watch_item_id""",
            (route_id,),
        )
        route = cur.fetchone()
        if not route:
            raise HTTPException(404, "Delivery connection not found")
        require_watch_recipients(cur, [route["watch_item_id"]])
        conn.commit()
    return RedirectResponse(url="/routing?msg=Delivery+connection+status+changed", status_code=303)
