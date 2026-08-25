import re
import uuid
from datetime import datetime

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from issues_app import app
from app import execute, query_all, query_one, templates

MODULES = [
    {"key": "PSEG", "name": "Utilities", "description": "Electric utility outages and restorations"},
    {"key": "FIRE", "name": "Fire Intelligence", "description": "Fire and public-safety incident intelligence"},
    {"key": "WEATHER", "name": "Weather / Flood", "description": "Weather, flood, tide and warning intelligence"},
    {"key": "TRAFFIC", "name": "Traffic", "description": "Road closures, incidents and construction impacts"},
    {"key": "TRANSIT", "name": "Transit", "description": "NJ Transit, PATH and regional transit disruptions"},
    {"key": "EVENTS", "name": "Events", "description": "Regional events and operational impacts"},
]


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

    open_issues = query_all(
        """
        SELECT id, title, category, priority, status, assigned_to, updated_at
        FROM issues
        WHERE status NOT IN ('RESOLVED','CLOSED')
        ORDER BY priority DESC, updated_at DESC
        LIMIT 10
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
            "open_issues": open_issues,
            "generated_at": datetime.now(),
            "page": "overview",
        },
    )


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, q: str = "", source: str = "", state: str = "active"):
    where = []
    params = []
    if state == "active":
        where.append("status <> 'RESOLVED' AND (expires_at IS NULL OR expires_at > now())")
    elif state == "resolved":
        where.append("status = 'RESOLVED'")
    if source.strip():
        where.append("upper(source) = upper(%s)")
        params.append(source.strip())
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append("(title ILIKE %s OR message ILIKE %s OR municipality ILIKE %s OR alert_id ILIKE %s)")
        params.extend([needle, needle, needle, needle])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    alerts = query_all(
        f"""
        SELECT alert_id, source, category, subtype, status, event_action,
               title, message, priority, municipality, received_at, updated_at,
               click_url
        FROM alerts
        {clause}
        ORDER BY received_at DESC
        LIMIT 300
        """,
        params,
    )
    sources = query_all("SELECT DISTINCT source FROM alerts ORDER BY source")
    counts = query_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status <> 'RESOLVED' AND (expires_at IS NULL OR expires_at > now())) AS active,
               count(*) FILTER (WHERE status = 'RESOLVED') AS resolved
        FROM alerts
        """
    )
    return templates.TemplateResponse(
        request=request,
        name="alerts.html",
        context={"alerts": alerts, "sources": sources, "counts": counts, "q": q, "source": source, "state": state, "page": "alerts"},
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


@app.get("/deliveries", response_class=HTMLResponse)
def deliveries_page(request: Request, status: str = "", q: str = ""):
    where = []
    params = []
    if status.strip():
        where.append("upper(d.status) = upper(%s)")
        params.append(status.strip())
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append("(a.title ILIKE %s OR s.name ILIKE %s OR d.ntfy_topic ILIKE %s OR a.source ILIKE %s)")
        params.extend([needle, needle, needle, needle])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = query_all(
        f"""
        SELECT d.id, d.status, d.ntfy_topic, d.attempted_at, d.sent_at,
               d.error_message, d.matched_watch_ids,
               s.name AS subscriber_name, s.subscriber_id,
               a.title AS alert_title, a.source, a.alert_id
        FROM deliveries d
        JOIN subscribers s ON s.id = d.subscriber_id
        JOIN alerts a ON a.id = d.alert_id
        {clause}
        ORDER BY d.created_at DESC
        LIMIT 300
        """,
        params,
    )
    return templates.TemplateResponse(request=request, name="deliveries.html", context={"rows": rows, "status": status, "q": q, "page": "deliveries"})


@app.get("/subscribers", response_class=HTMLResponse)
def subscribers_page(request: Request, q: str = "", state: str = "all", msg: str = ""):
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
               count(wir.id) FILTER (WHERE wir.active) AS active_routes
        FROM subscribers s
        LEFT JOIN watch_item_recipients wir ON wir.subscriber_id = s.id
        {clause}
        GROUP BY s.id
        ORDER BY s.active DESC, s.name
        LIMIT 250
        """,
        params,
    )
    counts = query_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE active) AS active,
               count(*) FILTER (WHERE NOT active) AS inactive
        FROM subscribers
        """
    )
    return templates.TemplateResponse(request=request, name="subscribers.html", context={"rows": rows, "counts": counts, "q": q, "state": state, "msg": msg, "page": "subscribers"})


@app.post("/subscribers/create")
def subscriber_create(name: str = Form(...), ntfy_topic: str = Form(...), notes: str = Form(""), subscriber_id: str = Form("")):
    name = name.strip()
    ntfy_topic = ntfy_topic.strip()
    if not name or not ntfy_topic:
        raise HTTPException(status_code=400, detail="Name and ntfy topic are required")
    sid = subscriber_id.strip().upper() or make_subscriber_id(name)
    execute(
        """
        INSERT INTO subscribers (subscriber_id, name, active, ntfy_topic, notes)
        VALUES (%s, %s, true, %s, %s)
        """,
        (sid, name, ntfy_topic, notes.strip() or None),
    )
    return RedirectResponse(url="/subscribers?msg=Subscriber+created", status_code=303)


@app.post("/subscribers/{subscriber_uuid}/update")
def subscriber_update(subscriber_uuid: uuid.UUID, name: str = Form(...), ntfy_topic: str = Form(...), notes: str = Form(""), active: str | None = Form(None)):
    execute(
        """
        UPDATE subscribers
        SET name=%s, ntfy_topic=%s, notes=%s, active=%s, updated_at=now()
        WHERE id=%s
        """,
        (name.strip(), ntfy_topic.strip(), notes.strip() or None, active is not None, subscriber_uuid),
    )
    return RedirectResponse(url="/subscribers?msg=Subscriber+updated", status_code=303)


@app.post("/subscribers/{subscriber_uuid}/toggle")
def subscriber_toggle(subscriber_uuid: uuid.UUID):
    execute("UPDATE subscribers SET active = NOT active, updated_at=now() WHERE id=%s", (subscriber_uuid,))
    return RedirectResponse(url="/subscribers?msg=Subscriber+status+changed", status_code=303)


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
    execute(
        """
        INSERT INTO watch_item_recipients (watch_item_id, subscriber_id, active)
        VALUES (%s, %s, true)
        ON CONFLICT (watch_item_id, subscriber_id)
        DO UPDATE SET active = true
        """,
        (watch_item_id, subscriber_id),
    )
    return RedirectResponse(url="/routing?msg=Route+activated", status_code=303)


@app.post("/routing/{route_id}/toggle")
def routing_toggle(route_id: uuid.UUID):
    execute("UPDATE watch_item_recipients SET active = NOT active WHERE id=%s", (route_id,))
    return RedirectResponse(url="/routing?msg=Route+status+changed", status_code=303)
