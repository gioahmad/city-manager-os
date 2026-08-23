import os
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="City Manager OS Dashboard", version="0.1")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


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
