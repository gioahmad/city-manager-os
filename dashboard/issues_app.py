import uuid

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import app, execute, query_all, query_one, templates
from attention_engine import annotate_issue_rows

ISSUE_STATUSES = ["OPEN", "IN_PROGRESS", "ON_HOLD", "RESOLVED", "CLOSED"]
MANAGER_ALIASES = {
    "manager",
    "township manager",
    "gio",
    "giovanni",
    "giovanni ahmad",
    "giovanni d. ahmad",
}
ACTIVE_WORK_SQL = """
status NOT IN ('RESOLVED','CLOSED')
AND NOT (
  item_type='IDEA'
  AND NULLIF(trim(coalesce(next_action,'')),'') IS NULL
  AND NULLIF(trim(coalesce(assigned_to,'')),'') IS NULL
  AND NULLIF(trim(coalesce(waiting_on,'')),'') IS NULL
  AND due_at IS NULL
  AND follow_up_at IS NULL
  AND decision_by IS NULL
  AND status='OPEN'
)
""".strip()


def validate_issue(priority: int, status: str | None = None):
    if priority < 1 or priority > 5:
        raise HTTPException(status_code=400, detail="Priority must be between 1 and 5")
    if status is not None and status not in ISSUE_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid issue status")


def _manager_owner(value: str | None) -> bool:
    owner = (value or "").strip().lower()
    return owner in MANAGER_ALIASES or owner.startswith("giovanni ")


def _select_fields() -> str:
    return """
      id,title,description,category,priority,status,source,address,municipality,
      assigned_to,item_type,next_action,waiting_on,operational_event_id,
      decision_options,recommendation,decision_outcome,
      visibility_status,visibility_audience,visibility_note,
      due_at,follow_up_at,decision_by,
      waiting_on_since,waiting_on_last_chased,waiting_on_chase_count,
      due_at AT TIME ZONE 'America/New_York' AS due_local,
      follow_up_at AT TIME ZONE 'America/New_York' AS follow_up_local,
      decision_by AT TIME ZONE 'America/New_York' AS decision_by_local,
      created_at,updated_at,closed_at
    """


@app.get("/issues", response_class=HTMLResponse)
def issues(request: Request, q: str = "", state: str = "open", msg: str = ""):
    where = []
    params = []
    post_filter = None

    if state == "open":
        where.append(ACTIVE_WORK_SQL)
    elif state == "mine":
        where.append(ACTIVE_WORK_SQL)
        post_filter = "mine"
    elif state == "assigned":
        where.append(ACTIVE_WORK_SQL)
        where.append("NULLIF(trim(assigned_to),'') IS NOT NULL")
    elif state == "waiting":
        where.append(ACTIVE_WORK_SQL)
        where.append("NULLIF(trim(waiting_on),'') IS NOT NULL")
    elif state == "today":
        where.append(ACTIVE_WORK_SQL)
        where.append(
            "((due_at IS NOT NULL AND due_at < ((date_trunc('day',now() AT TIME ZONE 'America/New_York') + interval '1 day') AT TIME ZONE 'America/New_York')) "
            "OR (follow_up_at IS NOT NULL AND follow_up_at < ((date_trunc('day',now() AT TIME ZONE 'America/New_York') + interval '1 day') AT TIME ZONE 'America/New_York')) "
            "OR (decision_by IS NOT NULL AND decision_by < ((date_trunc('day',now() AT TIME ZONE 'America/New_York') + interval '1 day') AT TIME ZONE 'America/New_York')))"
        )
    elif state == "overdue":
        where.append(ACTIVE_WORK_SQL)
        where.append("(due_at < now() OR follow_up_at < now() OR decision_by < now())")
    elif state == "no_next":
        where.append(ACTIVE_WORK_SQL)
        where.append("NULLIF(trim(next_action),'') IS NULL")
    elif state == "unowned":
        where.append(ACTIVE_WORK_SQL)
        where.append("NULLIF(trim(assigned_to),'') IS NULL")
    elif state == "commitments":
        where.append(ACTIVE_WORK_SQL)
        where.append("item_type='COMMITMENT'")
    elif state == "communications":
        where.append(ACTIVE_WORK_SQL)
        where.append("item_type='COMMUNICATION'")
    elif state == "decisions":
        where.append(ACTIVE_WORK_SQL)
        where.append("item_type='DECISION'")
    elif state == "visibility":
        where.append(ACTIVE_WORK_SQL)
        where.append("visibility_status IN ('WATCH','PREP','READY')")
    elif state == "backlog":
        where.append("status NOT IN ('RESOLVED','CLOSED')")
        where.append("item_type='IDEA'")
        where.append("NULLIF(trim(coalesce(next_action,'')),'') IS NULL")
        where.append("NULLIF(trim(coalesce(assigned_to,'')),'') IS NULL")
        where.append("NULLIF(trim(coalesce(waiting_on,'')),'') IS NULL")
        where.append("due_at IS NULL AND follow_up_at IS NULL AND decision_by IS NULL")
    elif state == "closed":
        where.append("status IN ('RESOLVED','CLOSED')")
    elif state != "all":
        state = "open"
        where.append(ACTIVE_WORK_SQL)

    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "(title ILIKE %s OR description ILIKE %s OR category ILIKE %s "
            "OR address ILIKE %s OR municipality ILIKE %s OR assigned_to ILIKE %s "
            "OR item_type ILIKE %s OR next_action ILIKE %s OR waiting_on ILIKE %s "
            "OR decision_options ILIKE %s OR recommendation ILIKE %s OR decision_outcome ILIKE %s "
            "OR visibility_audience ILIKE %s OR visibility_note ILIKE %s)"
        )
        params.extend([needle] * 14)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    items = query_all(
        f"""
        SELECT {_select_fields()}
        FROM issues
        {clause}
        ORDER BY
          CASE WHEN status IN ('RESOLVED','CLOSED') THEN 1 ELSE 0 END,
          priority DESC,
          updated_at DESC
        LIMIT 500
        """,
        params,
    )
    items = annotate_issue_rows(items)

    if post_filter == "mine":
        items = [
            item for item in items
            if item.get("needs_manager") or _manager_owner(item.get("assigned_to"))
        ]
    elif state == "assigned":
        items = [item for item in items if not _manager_owner(item.get("assigned_to"))]

    open_pool = query_all(
        f"""
        SELECT {_select_fields()}
        FROM issues
        WHERE {ACTIVE_WORK_SQL}
        ORDER BY priority DESC,updated_at DESC
        LIMIT 2000
        """
    )
    open_pool = annotate_issue_rows(open_pool)

    counts = query_one(
        f"""
        SELECT
          count(*) FILTER (WHERE {ACTIVE_WORK_SQL}) AS open,
          count(*) FILTER (WHERE {ACTIVE_WORK_SQL} AND NULLIF(trim(waiting_on),'') IS NOT NULL) AS waiting,
          count(*) FILTER (WHERE {ACTIVE_WORK_SQL} AND (due_at < now() OR follow_up_at < now() OR decision_by < now())) AS overdue,
          count(*) FILTER (WHERE {ACTIVE_WORK_SQL} AND NULLIF(trim(next_action),'') IS NULL) AS no_next_action,
          count(*) FILTER (WHERE {ACTIVE_WORK_SQL} AND item_type='COMMITMENT') AS commitments,
          count(*) FILTER (WHERE {ACTIVE_WORK_SQL} AND item_type='DECISION') AS decisions,
          count(*) FILTER (
            WHERE status NOT IN ('RESOLVED','CLOSED') AND item_type='IDEA'
              AND NULLIF(trim(coalesce(next_action,'')),'') IS NULL
              AND NULLIF(trim(coalesce(assigned_to,'')),'') IS NULL
              AND NULLIF(trim(coalesce(waiting_on,'')),'') IS NULL
              AND due_at IS NULL AND follow_up_at IS NULL AND decision_by IS NULL
          ) AS backlog,
          count(*) FILTER (WHERE status IN ('RESOLVED','CLOSED')) AS closed
        FROM issues
        """
    )
    counts["needs_me"] = sum(
        1 for item in open_pool
        if item.get("needs_manager") or _manager_owner(item.get("assigned_to"))
    )
    counts["assigned_out"] = sum(
        1 for item in open_pool
        if (item.get("assigned_to") or "").strip() and not _manager_owner(item.get("assigned_to"))
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
    decision_options: str = Form(""),
    recommendation: str = Form(""),
    decision_by: str = Form(""),
    decision_outcome: str = Form(""),
    visibility_status: str = Form("NONE"),
    visibility_audience: str = Form(""),
    visibility_note: str = Form(""),
):
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Work item title is required")
    validate_issue(priority)
    execute(
        """
        INSERT INTO issues(
          title,description,category,priority,status,source,address,municipality,assigned_to,
          item_type,next_action,waiting_on,due_at,follow_up_at,operational_event_id,
          decision_options,recommendation,decision_by,decision_outcome,
          visibility_status,visibility_audience,visibility_note
        )
        VALUES(
          %s,%s,%s,%s,'OPEN','MANUAL',%s,%s,%s,%s,%s,%s,
          NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',
          NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',
          NULLIF(%s,'')::uuid,%s,%s,
          NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',
          %s,%s,%s,%s
        )
        """,
        (
            title,description.strip() or None,category.strip().upper() or None,priority,
            address.strip() or None,municipality.strip() or None,assigned_to.strip() or None,
            item_type.strip().upper() or "ISSUE",next_action.strip() or None,waiting_on.strip() or None,
            due_at.strip(),follow_up_at.strip(),operational_event_id.strip(),
            decision_options.strip() or None,recommendation.strip() or None,decision_by.strip(),
            decision_outcome.strip() or None,visibility_status.strip().upper() or "NONE",
            visibility_audience.strip() or None,visibility_note.strip() or None,
        ),
    )
    return RedirectResponse(url="/issues?msg=Work+item+created", status_code=303)


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
    decision_options: str = Form(""),
    recommendation: str = Form(""),
    decision_by: str = Form(""),
    decision_outcome: str = Form(""),
    visibility_status: str = Form("NONE"),
    visibility_audience: str = Form(""),
    visibility_note: str = Form(""),
):
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Work item title is required")
    status = status.upper().strip()
    validate_issue(priority, status)
    execute(
        """
        UPDATE issues
        SET title=%s,description=%s,category=%s,priority=%s,status=%s,address=%s,municipality=%s,
            assigned_to=%s,item_type=%s,next_action=%s,waiting_on=%s,
            due_at=NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',
            follow_up_at=NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',
            decision_options=%s,recommendation=%s,
            decision_by=NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',
            decision_outcome=%s,visibility_status=%s,visibility_audience=%s,visibility_note=%s,
            updated_at=now(),
            closed_at=CASE WHEN %s IN ('RESOLVED','CLOSED') THEN COALESCE(closed_at,now()) ELSE NULL END
        WHERE id=%s
        """,
        (
            title,description.strip() or None,category.strip().upper() or None,priority,status,
            address.strip() or None,municipality.strip() or None,assigned_to.strip() or None,
            item_type.strip().upper() or "ISSUE",next_action.strip() or None,waiting_on.strip() or None,
            due_at.strip(),follow_up_at.strip(),decision_options.strip() or None,recommendation.strip() or None,
            decision_by.strip(),decision_outcome.strip() or None,visibility_status.strip().upper() or "NONE",
            visibility_audience.strip() or None,visibility_note.strip() or None,status,issue_id,
        ),
    )
    return RedirectResponse(url="/issues?msg=Work+item+updated", status_code=303)
