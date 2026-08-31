import uuid

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import app, execute, query_all, query_one, templates

ISSUE_STATUSES = ["OPEN", "IN_PROGRESS", "ON_HOLD", "RESOLVED", "CLOSED"]


def validate_issue(priority: int, status: str | None = None):
    if priority < 1 or priority > 5:
        raise HTTPException(status_code=400, detail="Priority must be between 1 and 5")
    if status is not None and status not in ISSUE_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid issue status")


@app.get("/issues", response_class=HTMLResponse)
def issues(request: Request, q: str = "", state: str = "open", msg: str = ""):
    where = []
    params = []

    if state == "open":
        where.append("status NOT IN ('RESOLVED', 'CLOSED')")
    elif state == "today":
        where.append("status NOT IN ('RESOLVED', 'CLOSED')")
        where.append(
            "("
            "(due_at IS NOT NULL AND due_at < ((date_trunc('day', now() AT TIME ZONE 'America/New_York') + interval '1 day') AT TIME ZONE 'America/New_York')) "
            "OR "
            "(follow_up_at IS NOT NULL AND follow_up_at < ((date_trunc('day', now() AT TIME ZONE 'America/New_York') + interval '1 day') AT TIME ZONE 'America/New_York'))"
            ")"
        )
    elif state == "waiting":
        where.append("status NOT IN ('RESOLVED', 'CLOSED')")
        where.append("NULLIF(trim(waiting_on), '') IS NOT NULL")
    elif state == "no_next":
        where.append("status NOT IN ('RESOLVED', 'CLOSED')")
        where.append("NULLIF(trim(next_action), '') IS NULL")
    elif state == "closed":
        where.append("status IN ('RESOLVED', 'CLOSED')")

    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "(title ILIKE %s OR description ILIKE %s OR category ILIKE %s "
            "OR address ILIKE %s OR municipality ILIKE %s OR assigned_to ILIKE %s "
            "OR item_type ILIKE %s OR next_action ILIKE %s OR waiting_on ILIKE %s)"
        )
        params.extend([needle] * 9)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    items = query_all(
        f"""
        SELECT id, title, description, category, priority, status, source,
               address, municipality, assigned_to,
               item_type, next_action, waiting_on, operational_event_id,
               due_at AT TIME ZONE 'America/New_York' AS due_local,
               follow_up_at AT TIME ZONE 'America/New_York' AS follow_up_local,
               created_at, updated_at, closed_at
        FROM issues
        {clause}
        ORDER BY
          CASE WHEN status IN ('RESOLVED', 'CLOSED') THEN 1 ELSE 0 END,
          priority DESC,
          updated_at DESC
        LIMIT 250
        """,
        params,
    )

    counts = query_one(
        """
        SELECT
               count(*) AS total,
               count(*) FILTER (
                   WHERE status NOT IN ('RESOLVED', 'CLOSED')
               ) AS open,
               count(*) FILTER (
                   WHERE status NOT IN ('RESOLVED', 'CLOSED')
                     AND (
                       (due_at IS NOT NULL AND due_at < ((date_trunc('day', now() AT TIME ZONE 'America/New_York') + interval '1 day') AT TIME ZONE 'America/New_York'))
                       OR
                       (follow_up_at IS NOT NULL AND follow_up_at < ((date_trunc('day', now() AT TIME ZONE 'America/New_York') + interval '1 day') AT TIME ZONE 'America/New_York'))
                     )
               ) AS today,
               count(*) FILTER (
                   WHERE status NOT IN ('RESOLVED', 'CLOSED')
                     AND NULLIF(trim(waiting_on), '') IS NOT NULL
               ) AS waiting,
               count(*) FILTER (
                   WHERE status NOT IN ('RESOLVED', 'CLOSED')
                     AND NULLIF(trim(next_action), '') IS NULL
               ) AS no_next_action,
               count(*) FILTER (
                   WHERE status IN ('RESOLVED', 'CLOSED')
               ) AS closed
        FROM issues
        """
    )

    return templates.TemplateResponse(
        request=request,
        name="issues.html",
        context={
            "items": items,
            "counts": counts,
            "q": q,
            "state": state,
            "msg": msg,
            "issue_statuses": ISSUE_STATUSES,
        },
    )


@app.post("/issues/create")
def issue_create(
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form(""),
    priority: int = Form(3),
    address: str = Form(""),
    municipality: str = Form("Weehawken"),
    assigned_to: str = Form(""),
    item_type: str = Form("ISSUE"),
    next_action: str = Form(""),
    waiting_on: str = Form(""),
    due_at: str = Form(""),
    follow_up_at: str = Form(""),
    operational_event_id: str = Form(""),
):
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Issue title is required")
    validate_issue(priority)

    execute(
        """
        INSERT INTO issues (
          title, description, category, priority, status, source,
          address, municipality, assigned_to,
          item_type, next_action, waiting_on, due_at, follow_up_at,
          operational_event_id
        )
        VALUES (
          %s, %s, %s, %s, 'OPEN', 'MANUAL', %s, %s, %s,
          %s, %s, %s,
          NULLIF(%s, '')::timestamp AT TIME ZONE 'America/New_York',
          NULLIF(%s, '')::timestamp AT TIME ZONE 'America/New_York',
          NULLIF(%s, '')::uuid
        )
        """,
        (
            title,
            description.strip() or None,
            category.strip().upper() or None,
            priority,
            address.strip() or None,
            municipality.strip() or None,
            assigned_to.strip() or None,
            item_type.strip().upper() or "ISSUE",
            next_action.strip() or None,
            waiting_on.strip() or None,
            due_at.strip(),
            follow_up_at.strip(),
            operational_event_id.strip(),
        ),
    )
    return RedirectResponse(url="/issues?msg=Issue+created", status_code=303)


@app.post("/issues/{issue_id}/update")
def issue_update(
    issue_id: uuid.UUID,
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form(""),
    priority: int = Form(3),
    status: str = Form("OPEN"),
    address: str = Form(""),
    municipality: str = Form(""),
    assigned_to: str = Form(""),
    item_type: str = Form("ISSUE"),
    next_action: str = Form(""),
    waiting_on: str = Form(""),
    due_at: str = Form(""),
    follow_up_at: str = Form(""),
):
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Issue title is required")

    status = status.upper().strip()
    validate_issue(priority, status)

    execute(
        """
        UPDATE issues
        SET title = %s,
            description = %s,
            category = %s,
            priority = %s,
            status = %s,
            address = %s,
            municipality = %s,
            assigned_to = %s,
            item_type = %s,
            next_action = %s,
            waiting_on = %s,
            due_at = NULLIF(%s, '')::timestamp AT TIME ZONE 'America/New_York',
            follow_up_at = NULLIF(%s, '')::timestamp AT TIME ZONE 'America/New_York',
            updated_at = now(),
            closed_at = CASE
              WHEN %s IN ('RESOLVED', 'CLOSED') THEN COALESCE(closed_at, now())
              ELSE NULL
            END
        WHERE id = %s
        """,
        (
            title,
            description.strip() or None,
            category.strip().upper() or None,
            priority,
            status,
            address.strip() or None,
            municipality.strip() or None,
            assigned_to.strip() or None,
            item_type.strip().upper() or "ISSUE",
            next_action.strip() or None,
            waiting_on.strip() or None,
            due_at.strip(),
            follow_up_at.strip(),
            status,
            issue_id,
        ),
    )
    return RedirectResponse(url="/issues?msg=Issue+updated", status_code=303)
