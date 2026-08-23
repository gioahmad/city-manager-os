import os
import re
import uuid
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="City Manager OS Dashboard", version="0.1")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

MATCH_MODES = {"FIELD", "CONTAINS", "WORD", "EXACT"}
WATCH_TYPES = ["ADDRESS", "FACILITY", "AREA", "PHRASE", "SOURCE", "INCIDENT_TYPE", "OTHER"]


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


def query_all(sql, params=None):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()


def query_one(sql, params=None):
    rows = query_all(sql, params)
    return rows[0] if rows else {}


def execute(sql, params=None):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()


def csv_array(value: str | None):
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def make_watch_id(display_name: str):
    slug = re.sub(r"[^A-Z0-9]+", "_", display_name.upper()).strip("_")[:32] or "ITEM"
    return f"W_{slug}_{uuid.uuid4().hex[:6].upper()}"


def validate_watch(match_mode: str, match_field: str | None, min_priority: int):
    if match_mode not in MATCH_MODES:
        raise HTTPException(status_code=400, detail="Invalid match mode")
    if match_mode == "FIELD" and not (match_field or "").strip():
        raise HTTPException(status_code=400, detail="FIELD match mode requires match_field")
    if min_priority < 1 or min_priority > 5:
        raise HTTPException(status_code=400, detail="Priority must be between 1 and 5")


@app.get("/health")
def health():
    row = query_one("SELECT now() AS db_time")
    return {"status": "ok", "db_time": row.get("db_time")}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    metrics = query_one(
        """
        SELECT
          (SELECT count(*) FROM alerts
             WHERE status <> 'RESOLVED'
               AND (expires_at IS NULL OR expires_at > now())) AS active_alerts,
          (SELECT count(*) FROM watch_items WHERE active = true) AS active_watch_items,
          (SELECT count(*) FROM deliveries WHERE status = 'SENT'
             AND created_at >= now() - interval '24 hours') AS sent_24h,
          (SELECT count(*) FROM source_health
             WHERE status IS DISTINCT FROM 'OK') AS unhealthy_sources
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
        LIMIT 20
        """
    )

    intelligence_feed = query_all(
        """
        SELECT alert_id, source, category, subtype, status, event_action,
               title, priority, municipality, received_at
        FROM alerts
        ORDER BY received_at DESC
        LIMIT 40
        """
    )

    utility_status = query_all(
        """
        SELECT alert_id, title, message, status, event_action, priority,
               municipality, received_at
        FROM alerts
        WHERE upper(source) = 'PSEG'
        ORDER BY received_at DESC
        LIMIT 12
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
               d.matched_watch_ids, s.subscriber_id, s.name AS subscriber_name,
               a.title AS alert_title, a.source
        FROM deliveries d
        JOIN subscribers s ON s.id = d.subscriber_id
        JOIN alerts a ON a.id = d.alert_id
        ORDER BY d.created_at DESC
        LIMIT 20
        """
    )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "metrics": metrics,
            "active_alerts": active_alerts,
            "intelligence_feed": intelligence_feed,
            "utility_status": utility_status,
            "source_health": source_health,
            "recent_deliveries": recent_deliveries,
            "generated_at": datetime.now(),
        },
    )


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist(request: Request, q: str = "", state: str = "all", msg: str = ""):
    where = []
    params = []

    if state == "active":
        where.append("active = true")
    elif state == "inactive":
        where.append("active = false")

    if q.strip():
        needle = f"%{q.strip()}%"
        where.append("(display_name ILIKE %s OR search_term ILIKE %s OR watch_id ILIKE %s OR municipality ILIKE %s)")
        params.extend([needle, needle, needle, needle])

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    items = query_all(
        f"""
        SELECT id, watch_id, active, watch_type, display_name, search_term,
               aliases, match_mode, match_field, category, subcategory,
               tags, min_priority, address, municipality, county, state,
               block, lot, notes, created_at, updated_at
        FROM watch_items
        {clause}
        ORDER BY active DESC, display_name ASC
        LIMIT 250
        """,
        params,
    )

    counts = query_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE active) AS active,
               count(*) FILTER (WHERE NOT active) AS inactive
        FROM watch_items
        """
    )

    return templates.TemplateResponse(
        request=request,
        name="watchlist.html",
        context={
            "items": items,
            "counts": counts,
            "q": q,
            "state": state,
            "msg": msg,
            "watch_types": WATCH_TYPES,
            "match_modes": sorted(MATCH_MODES),
        },
    )


@app.post("/watchlist/create")
def watchlist_create(
    display_name: str = Form(...),
    watch_type: str = Form(...),
    search_term: str = Form(...),
    match_mode: str = Form("CONTAINS"),
    match_field: str = Form(""),
    aliases: str = Form(""),
    category: str = Form(""),
    tags: str = Form(""),
    municipality: str = Form(""),
    address: str = Form(""),
    min_priority: int = Form(1),
    notes: str = Form(""),
):
    display_name = display_name.strip()
    search_term = search_term.strip()
    if not display_name or not search_term:
        raise HTTPException(status_code=400, detail="Display name and search term are required")

    match_mode = match_mode.upper().strip()
    validate_watch(match_mode, match_field, min_priority)
    watch_id = make_watch_id(display_name)

    execute(
        """
        INSERT INTO watch_items (
          watch_id, active, watch_type, display_name, search_term, aliases,
          match_mode, match_field, category, tags, min_priority,
          address, municipality, notes
        )
        VALUES (%s, true, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            watch_id,
            watch_type.strip().upper(),
            display_name,
            search_term,
            csv_array(aliases),
            match_mode,
            match_field.strip() or None,
            category.strip() or None,
            csv_array(tags),
            min_priority,
            address.strip() or None,
            municipality.strip() or None,
            notes.strip() or None,
        ),
    )
    return RedirectResponse(url="/watchlist?msg=Watch+item+created", status_code=303)


@app.post("/watchlist/{item_id}/update")
def watchlist_update(
    item_id: uuid.UUID,
    display_name: str = Form(...),
    watch_type: str = Form(...),
    search_term: str = Form(...),
    match_mode: str = Form(...),
    match_field: str = Form(""),
    aliases: str = Form(""),
    category: str = Form(""),
    tags: str = Form(""),
    municipality: str = Form(""),
    address: str = Form(""),
    min_priority: int = Form(1),
    notes: str = Form(""),
    active: str | None = Form(None),
):
    display_name = display_name.strip()
    search_term = search_term.strip()
    if not display_name or not search_term:
        raise HTTPException(status_code=400, detail="Display name and search term are required")

    match_mode = match_mode.upper().strip()
    validate_watch(match_mode, match_field, min_priority)

    execute(
        """
        UPDATE watch_items
        SET active = %s,
            watch_type = %s,
            display_name = %s,
            search_term = %s,
            aliases = %s,
            match_mode = %s,
            match_field = %s,
            category = %s,
            tags = %s,
            min_priority = %s,
            address = %s,
            municipality = %s,
            notes = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (
            active is not None,
            watch_type.strip().upper(),
            display_name,
            search_term,
            csv_array(aliases),
            match_mode,
            match_field.strip() or None,
            category.strip() or None,
            csv_array(tags),
            min_priority,
            address.strip() or None,
            municipality.strip() or None,
            notes.strip() or None,
            item_id,
        ),
    )
    return RedirectResponse(url="/watchlist?msg=Watch+item+updated", status_code=303)


@app.post("/watchlist/{item_id}/toggle")
def watchlist_toggle(item_id: uuid.UUID):
    execute(
        "UPDATE watch_items SET active = NOT active, updated_at = now() WHERE id = %s",
        (item_id,),
    )
    return RedirectResponse(url="/watchlist?msg=Watch+item+status+changed", status_code=303)
