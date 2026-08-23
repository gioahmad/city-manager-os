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
    elif state == "closed":
        where.append("status IN ('RESOLVED', 'CLOSED')")

    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "(title ILIKE %s OR description ILIKE %s OR category ILIKE %s "
            "OR address ILIKE %s OR municipality ILIKE %s OR assigned_to ILIKE %s)"
        )
        params.extend([needle, needle, needle, needle, needle, needle])

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    items = query_all(
        f"""
        SELECT id, title, description, category, priority, status, source,
               address, municipality, assigned_to, created_at, updated_at, closed_at
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
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status NOT IN ('RESOLVED', 'CLOSED')) AS open,
               count(*) FILTER (WHERE status = 'IN_PROGRESS') AS in_progress,
               count(*) FILTER (WHERE status IN ('RESOLVED', 'CLOSED')) AS closed
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
):
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Issue title is required")
    validate_issue(priority)

    execute(
        """
        INSERT INTO issues (
          title, description, category, priority, status, source,
          address, municipality, assigned_to
        )
        VALUES (%s, %s, %s, %s, 'OPEN', 'MANUAL', %s, %s, %s)
        """,
        (
            title,
            description.strip() or None,
            category.strip().upper() or None,
            priority,
            address.strip() or None,
            municipality.strip() or None,
            assigned_to.strip() or None,
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
            status,
            issue_id,
        ),
    )
    return RedirectResponse(url="/issues?msg=Issue+updated", status_code=303)
