from __future__ import annotations

import re
import uuid

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from schedule_app import app
from app import db_conn, execute, query_all, query_one, templates


def _make_subscriber_id(name: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:32] or "SUBSCRIBER"
    return f"S_{slug}_{uuid.uuid4().hex[:6].upper()}"


def _remove_existing_get(path: str) -> None:
    app.router.routes = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", None) == path
            and "GET" in (getattr(route, "methods", set()) or set())
        )
    ]


def _watch_health(row: dict) -> tuple[str, str]:
    if not row.get("active"):
        return "PAUSED", "inactive-status"
    if int(row.get("active_routes") or 0) == 0:
        return "NO ROUTE", "inactive-status"
    if str(row.get("last_delivery_status") or "").upper() == "FAILED":
        return "DELIVERY ISSUE", "inactive-status"
    if str(row.get("last_delivery_status") or "").upper() == "SENT":
        return "HEALTHY", "active"
    if row.get("last_match_at"):
        return "MATCHED", "active"
    return "WAITING", "active"


_remove_existing_get("/alert-admin")


@app.get("/alert-admin", response_class=HTMLResponse)
def alert_admin_v2(
    request: Request,
    q: str = "",
    state: str = "all",
    health: str = "all",
    msg: str = "",
):
    watches = query_all(
        """
        SELECT
          w.id,w.watch_id,w.active,w.watch_type,w.display_name,w.search_term,w.match_mode,
          w.match_field,w.min_priority,w.municipality,w.address,w.notes,
          COALESCE(rr.total_routes,0) AS total_routes,
          COALESCE(rr.active_routes,0) AS active_routes,
          COALESCE(rr.recipients,'[]'::jsonb) AS recipients,
          lm.matched_at AS last_match_at,
          lm.match_type AS last_match_type,
          lm.match_reason AS last_match_reason,
          lm.alert_id AS last_match_alert_id,
          lm.alert_title AS last_match_title,
          lm.alert_priority AS last_match_priority,
          lm.alert_source AS last_match_source,
          ld.delivery_at AS last_delivery_at,
          ld.status AS last_delivery_status,
          ld.error_message AS last_delivery_error,
          ld.subscriber_name AS last_delivery_subscriber,
          ld.subscriber_key AS last_delivery_subscriber_key
        FROM watch_items w
        LEFT JOIN LATERAL (
          SELECT
            count(*) AS total_routes,
            count(*) FILTER (WHERE wir.active AND s.active) AS active_routes,
            jsonb_agg(
              jsonb_build_object(
                'route_id',wir.id::text,
                'route_active',wir.active,
                'subscriber_id',s.id::text,
                'subscriber_key',s.subscriber_id,
                'subscriber_active',s.active,
                'name',s.name,
                'ntfy_topic',s.ntfy_topic
              )
              ORDER BY s.active DESC,s.name
            ) AS recipients
          FROM watch_item_recipients wir
          JOIN subscribers s ON s.id=wir.subscriber_id
          WHERE wir.watch_item_id=w.id
        ) rr ON true
        LEFT JOIN LATERAL (
          SELECT
            awm.matched_at,
            awm.match_type,
            awm.match_reason,
            a.alert_id,
            a.title AS alert_title,
            a.priority AS alert_priority,
            a.source AS alert_source
          FROM alert_watch_matches awm
          JOIN alerts a ON a.id=awm.alert_id
          WHERE awm.watch_item_id=w.id
          ORDER BY awm.matched_at DESC
          LIMIT 1
        ) lm ON true
        LEFT JOIN LATERAL (
          SELECT
            COALESCE(d.sent_at,d.attempted_at,d.created_at) AS delivery_at,
            d.status,
            d.error_message,
            s.name AS subscriber_name,
            s.subscriber_id AS subscriber_key
          FROM alert_watch_matches awm
          JOIN deliveries d ON d.alert_id=awm.alert_id
          JOIN subscribers s ON s.id=d.subscriber_id
          WHERE awm.watch_item_id=w.id
          ORDER BY COALESCE(d.sent_at,d.attempted_at,d.created_at) DESC
          LIMIT 1
        ) ld ON true
        ORDER BY w.active DESC,w.display_name
        LIMIT 300
        """
    )

    q_norm = q.strip().lower()
    filtered = []
    for row in watches:
        recipients = row.get("recipients") or []
        row["active_recipient_ids"] = {
            str(r.get("subscriber_id"))
            for r in recipients
            if r.get("route_active") and r.get("subscriber_active")
        }
        label, css_class = _watch_health(row)
        row["health_label"] = label
        row["health_class"] = css_class

        haystack = " ".join(
            str(row.get(k) or "")
            for k in ("display_name", "watch_id", "search_term", "municipality", "address", "watch_type")
        ).lower()
        if q_norm and q_norm not in haystack:
            continue
        if state == "active" and not row.get("active"):
            continue
        if state == "inactive" and row.get("active"):
            continue
        if health == "unrouted" and not (row.get("active") and int(row.get("active_routes") or 0) == 0):
            continue
        if health == "failed" and str(row.get("last_delivery_status") or "").upper() != "FAILED":
            continue
        if health == "matched" and not row.get("last_match_at"):
            continue
        filtered.append(row)

    subscribers = query_all(
        """
        SELECT
          s.id,s.subscriber_id,s.name,s.active,s.ntfy_topic,s.notes,
          COALESCE(r.total_routes,0) AS total_routes,
          COALESCE(r.active_routes,0) AS active_routes,
          ld.delivery_at AS last_delivery_at,
          ld.status AS last_delivery_status,
          ld.error_message AS last_delivery_error
        FROM subscribers s
        LEFT JOIN LATERAL (
          SELECT
            count(*) AS total_routes,
            count(*) FILTER (WHERE wir.active) AS active_routes
          FROM watch_item_recipients wir
          WHERE wir.subscriber_id=s.id
        ) r ON true
        LEFT JOIN LATERAL (
          SELECT
            COALESCE(d.sent_at,d.attempted_at,d.created_at) AS delivery_at,
            d.status,
            d.error_message
          FROM deliveries d
          WHERE d.subscriber_id=s.id
          ORDER BY COALESCE(d.sent_at,d.attempted_at,d.created_at) DESC
          LIMIT 1
        ) ld ON true
        ORDER BY s.active DESC,s.name
        """
    )
    for subscriber in subscribers:
        subscriber["id_text"] = str(subscriber["id"])

    counts = query_one(
        """
        SELECT
          (SELECT count(*) FROM watch_items WHERE active) AS watches,
          (SELECT count(*) FROM subscribers WHERE active) AS subscribers,
          (SELECT count(*) FROM watch_item_recipients wir
             JOIN watch_items w ON w.id=wir.watch_item_id
             JOIN subscribers s ON s.id=wir.subscriber_id
             WHERE wir.active AND w.active AND s.active) AS routes,
          (SELECT count(*) FROM watch_items w
             WHERE w.active
               AND NOT EXISTS (
                 SELECT 1
                 FROM watch_item_recipients wir
                 JOIN subscribers s ON s.id=wir.subscriber_id
                 WHERE wir.watch_item_id=w.id
                   AND wir.active
                   AND s.active
               )) AS unrouted_watches,
          (SELECT count(*) FROM subscribers s
             WHERE s.active
               AND NOT EXISTS (
                 SELECT 1
                 FROM watch_item_recipients wir
                 JOIN watch_items w ON w.id=wir.watch_item_id
                 WHERE wir.subscriber_id=s.id
                   AND wir.active
                   AND w.active
               )) AS subscribers_without_routes,
          (SELECT count(*) FROM deliveries
             WHERE status='SENT'
               AND created_at >= now() - interval '24 hours') AS sent_24h,
          (SELECT count(*) FROM deliveries
             WHERE status='FAILED'
               AND created_at >= now() - interval '24 hours') AS failed_24h
        """
    )

    recent = query_all(
        """
        SELECT
          awm.matched_at,
          awm.match_type,
          awm.match_reason,
          w.watch_id,
          w.display_name AS watch_name,
          a.alert_id,
          a.title AS alert_title,
          a.source,
          a.priority,
          d.status AS delivery_status,
          COALESCE(d.sent_at,d.attempted_at,d.created_at) AS delivery_at,
          d.error_message AS delivery_error,
          s.name AS subscriber_name,
          s.subscriber_id AS subscriber_key
        FROM alert_watch_matches awm
        JOIN watch_items w ON w.id=awm.watch_item_id
        JOIN alerts a ON a.id=awm.alert_id
        LEFT JOIN deliveries d ON d.alert_id=a.id
        LEFT JOIN subscribers s ON s.id=d.subscriber_id
        ORDER BY awm.matched_at DESC,
                 COALESCE(d.sent_at,d.attempted_at,d.created_at) DESC NULLS LAST
        LIMIT 40
        """
    )

    return templates.TemplateResponse(
        request=request,
        name="alert_admin.html",
        context={
            "watches": filtered,
            "subscribers": subscribers,
            "counts": counts,
            "recent": recent,
            "q": q,
            "state": state,
            "health": health,
            "msg": msg,
            "page": "alert-admin",
        },
    )


@app.post("/alert-admin/watch/{watch_item_id}/recipients")
def alert_admin_save_recipients(
    watch_item_id: uuid.UUID,
    subscriber_ids: list[uuid.UUID] = Form([]),
):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM watch_items WHERE id=%s", (watch_item_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Watch item not found")

        for subscriber_id in subscriber_ids:
            cur.execute("SELECT id FROM subscribers WHERE id=%s AND active=true", (subscriber_id,))
            if not cur.fetchone():
                raise HTTPException(400, "One or more selected subscribers are inactive or missing")

        cur.execute(
            "UPDATE watch_item_recipients SET active=false WHERE watch_item_id=%s",
            (watch_item_id,),
        )
        for subscriber_id in subscriber_ids:
            cur.execute(
                """
                INSERT INTO watch_item_recipients(watch_item_id,subscriber_id,active)
                VALUES(%s,%s,true)
                ON CONFLICT(watch_item_id,subscriber_id)
                DO UPDATE SET active=true
                """,
                (watch_item_id, subscriber_id),
            )
        conn.commit()

    return RedirectResponse(
        "/alert-admin?msg=Recipients+updated",
        status_code=303,
    )


@app.post("/alert-admin/watch/{watch_item_id}/toggle")
def alert_admin_toggle_watch(watch_item_id: uuid.UUID):
    execute(
        "UPDATE watch_items SET active=NOT active,updated_at=now() WHERE id=%s",
        (watch_item_id,),
    )
    return RedirectResponse("/alert-admin?msg=Watch+status+updated", status_code=303)


@app.post("/alert-admin/subscriber")
def alert_admin_create_subscriber(
    name: str = Form(...),
    ntfy_topic: str = Form(...),
    subscriber_id: str = Form(""),
    notes: str = Form(""),
):
    name = name.strip()
    ntfy_topic = ntfy_topic.strip()
    subscriber_id = subscriber_id.strip() or _make_subscriber_id(name)

    if not name or not ntfy_topic:
        raise HTTPException(400, "Subscriber name and ntfy topic are required")
    if query_one("SELECT id FROM subscribers WHERE subscriber_id=%s", (subscriber_id,)):
        raise HTTPException(400, "Subscriber ID already exists")

    execute(
        """
        INSERT INTO subscribers(subscriber_id,name,active,ntfy_topic,notes)
        VALUES(%s,%s,true,%s,%s)
        """,
        (subscriber_id, name, ntfy_topic, notes.strip() or None),
    )
    return RedirectResponse("/alert-admin?msg=Subscriber+created", status_code=303)


@app.post("/alert-admin/subscriber/{subscriber_uuid}/toggle")
def alert_admin_toggle_subscriber(subscriber_uuid: uuid.UUID):
    execute(
        "UPDATE subscribers SET active=NOT active,updated_at=now() WHERE id=%s",
        (subscriber_uuid,),
    )
    return RedirectResponse("/alert-admin?msg=Subscriber+status+updated", status_code=303)
