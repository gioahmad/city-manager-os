import uuid
from datetime import datetime

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from operations_app import app, operations_home
from app import execute, query_all, query_one, templates


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
def schedule_home(request: Request):
    response = operations_home(request)
    happening_now = query_all(
        """
        SELECT id, title, category, location_name, address, municipality,
               starts_at AT TIME ZONE 'America/New_York' AS starts_local,
               ends_at AT TIME ZONE 'America/New_York' AS ends_local,
               priority, source, notes,
               CASE
                 WHEN starts_at <= now()
                  AND COALESCE(ends_at, starts_at + interval '2 hours') > now()
                   THEN 'NOW'
                 WHEN starts_at > now() AND starts_at <= now() + interval '3 hours'
                   THEN 'NEXT'
                 ELSE 'UPCOMING'
               END AS timing_status
        FROM operational_events
        WHERE active = true
          AND starts_at < now() + interval '12 hours'
          AND COALESCE(ends_at, starts_at + interval '2 hours') > now() - interval '30 minutes'
        ORDER BY
          CASE
            WHEN starts_at <= now()
             AND COALESCE(ends_at, starts_at + interval '2 hours') > now() THEN 0
            ELSE 1
          END,
          starts_at
        LIMIT 12
        """
    )
    response.context["happening_now"] = happening_now
    return response


@app.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, state: str = "upcoming", q: str = "", msg: str = ""):
    where = []
    params = []

    if state == "upcoming":
        where.append("active = true AND COALESCE(ends_at, starts_at + interval '2 hours') >= now()")
    elif state == "past":
        where.append("COALESCE(ends_at, starts_at + interval '2 hours') < now()")
    elif state == "inactive":
        where.append("active = false")

    if q.strip():
        needle = f"%{q.strip()}%"
        where.append("(title ILIKE %s OR category ILIKE %s OR location_name ILIKE %s OR address ILIKE %s OR municipality ILIKE %s OR notes ILIKE %s)")
        params.extend([needle, needle, needle, needle, needle, needle])

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = query_all(
        f"""
        SELECT id, active, title, category, location_name, address, municipality,
               starts_at AT TIME ZONE 'America/New_York' AS starts_local,
               ends_at AT TIME ZONE 'America/New_York' AS ends_local,
               priority, source, notes, created_at, updated_at,
               CASE
                 WHEN active = false THEN 'INACTIVE'
                 WHEN starts_at <= now()
                  AND COALESCE(ends_at, starts_at + interval '2 hours') > now()
                   THEN 'NOW'
                 WHEN starts_at > now() THEN 'UPCOMING'
                 ELSE 'PAST'
               END AS timing_status
        FROM operational_events
        {clause}
        ORDER BY starts_at ASC
        LIMIT 500
        """,
        params,
    )

    counts = query_one(
        """
        SELECT
          count(*) FILTER (
            WHERE active = true
              AND starts_at <= now()
              AND COALESCE(ends_at, starts_at + interval '2 hours') > now()
          ) AS happening_now,
          count(*) FILTER (
            WHERE active = true
              AND starts_at > now()
              AND starts_at < date_trunc('day', now() AT TIME ZONE 'America/New_York')
                  AT TIME ZONE 'America/New_York' + interval '1 day'
          ) AS later_today,
          count(*) FILTER (
            WHERE active = true
              AND COALESCE(ends_at, starts_at + interval '2 hours') >= now()
          ) AS upcoming
        FROM operational_events
        """
    )

    return templates.TemplateResponse(
        request=request,
        name="schedule.html",
        context={
            "rows": rows,
            "counts": counts,
            "state": state,
            "q": q,
            "msg": msg,
            "page": "schedule",
        },
    )


@app.post("/schedule/create")
def schedule_create(
    title: str = Form(...),
    category: str = Form(""),
    location_name: str = Form(""),
    address: str = Form(""),
    municipality: str = Form("Weehawken"),
    starts_at: str = Form(...),
    ends_at: str = Form(""),
    priority: int = Form(3),
    notes: str = Form(""),
):
    execute(
        """
        INSERT INTO operational_events (
          title, category, location_name, address, municipality,
          starts_at, ends_at, priority, source, notes
        )
        VALUES (
          %s, %s, %s, %s, %s,
          %s::timestamp AT TIME ZONE 'America/New_York',
          CASE WHEN NULLIF(%s, '') IS NULL THEN NULL
               ELSE %s::timestamp AT TIME ZONE 'America/New_York' END,
          %s, 'MANUAL', %s
        )
        """,
        (
            title.strip(), category.strip() or None, location_name.strip() or None,
            address.strip() or None, municipality.strip() or "Weehawken",
            starts_at, ends_at, ends_at, priority, notes.strip() or None,
        ),
    )
    return RedirectResponse(url="/schedule?msg=Schedule+item+created", status_code=303)


@app.post("/schedule/{event_id}/update")
def schedule_update(
    event_id: uuid.UUID,
    title: str = Form(...),
    category: str = Form(""),
    location_name: str = Form(""),
    address: str = Form(""),
    municipality: str = Form("Weehawken"),
    starts_at: str = Form(...),
    ends_at: str = Form(""),
    priority: int = Form(3),
    notes: str = Form(""),
    active: str | None = Form(None),
):
    execute(
        """
        UPDATE operational_events
        SET active = %s,
            title = %s,
            category = %s,
            location_name = %s,
            address = %s,
            municipality = %s,
            starts_at = %s::timestamp AT TIME ZONE 'America/New_York',
            ends_at = CASE WHEN NULLIF(%s, '') IS NULL THEN NULL
                           ELSE %s::timestamp AT TIME ZONE 'America/New_York' END,
            priority = %s,
            notes = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (
            active is not None, title.strip(), category.strip() or None,
            location_name.strip() or None, address.strip() or None,
            municipality.strip() or "Weehawken", starts_at, ends_at, ends_at,
            priority, notes.strip() or None, event_id,
        ),
    )
    return RedirectResponse(url="/schedule?msg=Schedule+item+updated", status_code=303)


@app.post("/schedule/{event_id}/toggle")
def schedule_toggle(event_id: uuid.UUID):
    execute(
        "UPDATE operational_events SET active = NOT active, updated_at = now() WHERE id = %s",
        (event_id,),
    )
    return RedirectResponse(url="/schedule?msg=Schedule+item+status+changed", status_code=303)
