import uuid

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import schedule_app as schedule_module
import staff_admin_app as staff_admin_module
from app import app, execute, query_all, query_one, templates

DAYS = [
    (1, "Mon"), (2, "Tue"), (3, "Wed"), (4, "Thu"),
    (5, "Fri"), (6, "Sat"), (7, "Sun"),
]


def remove_existing_get(path):
    app.router.routes = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", None) == path
            and "GET" in (getattr(route, "methods", set()) or set())
        )
    ]


def _uuid_or_none(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid identifier.")


def _int_or_none(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid number.")


def _validate_time(value):
    value = (value or "").strip()
    if len(value) != 5 or value[2] != ":":
        raise HTTPException(status_code=400, detail="Time must be HH:MM.")
    try:
        hour, minute = [int(x) for x in value.split(":")]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid time.")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(status_code=400, detail="Invalid time.")
    return value


def _clean_days(days):
    cleaned = sorted({int(x) for x in (days or []) if 1 <= int(x) <= 7})
    if not cleaned:
        raise HTTPException(status_code=400, detail="Select at least one day.")
    return cleaned


def _checklist_lines(value):
    return [line.strip() for line in (value or "").splitlines() if line.strip()][:30]


def _today_operations():
    return query_all(
        """
        SELECT
          rr.id AS run_id, rr.service_date,
          rr.scheduled_for AT TIME ZONE 'America/New_York' AS scheduled_local,
          rr.due_at AT TIME ZONE 'America/New_York' AS due_local,
          rr.status, rr.acknowledged_at, rr.acknowledged_by,
          rr.exception_note, rr.exception_issue_id, rr.issue_id,
          r.id AS routine_id, r.name, r.routine_kind, r.department,
          r.priority, r.confirmation_required, r.escalate_if_missed,
          r.verification_required, r.location_label,
          sl.name AS managed_location,
          wt.name AS work_type_name,
          e.full_name AS employee_name,
          i.status AS issue_status, i.help_reason,
          i.verification_pending
        FROM operations_routine_runs rr
        JOIN operations_routines r ON r.id=rr.routine_id
        LEFT JOIN staff_locations sl ON sl.id=r.location_id
        LEFT JOIN staff_work_types wt ON wt.id=r.work_type_id
        LEFT JOIN staff_employees e ON e.id=r.assigned_employee_id
        LEFT JOIN issues i ON i.id=rr.issue_id
        WHERE rr.service_date=(now() AT TIME ZONE 'America/New_York')::date
          AND r.active=true
        ORDER BY rr.scheduled_for,r.priority DESC,r.name
        """
    )


def _operations_counts():
    return query_one(
        """
        SELECT
          count(*) AS scheduled_today,
          count(*) FILTER (
            WHERE rr.status IN ('MISSED','EXCEPTION','NEEDS_HELP')
          ) AS exceptions,
          count(*) FILTER (
            WHERE rr.status='AWAITING_VERIFICATION'
          ) AS awaiting_verification,
          count(*) FILTER (
            WHERE r.routine_kind='WORK'
              AND rr.status NOT IN ('COMPLETE')
          ) AS work_open,
          count(*) FILTER (
            WHERE r.routine_kind='AWARENESS'
              AND rr.status IN ('EXPECTED','UPCOMING','ACKNOWLEDGED')
          ) AS awareness_now
        FROM operations_routine_runs rr
        JOIN operations_routines r ON r.id=rr.routine_id
        WHERE rr.service_date=(now() AT TIME ZONE 'America/New_York')::date
          AND r.active=true
        """
    )


def _verification_queue():
    return query_all(
        """
        SELECT
          i.id,i.title,i.priority,i.status,i.assigned_to,i.employee_location,
          i.updated_at AT TIME ZONE 'America/New_York' AS updated_local,
          i.verification_note,
          wt.name AS work_type_name,
          (
            SELECT p.id
            FROM issue_photos p
            WHERE p.issue_id=i.id AND p.phase='AFTER'
            ORDER BY p.created_at DESC
            LIMIT 1
          ) AS after_photo_id,
          (
            SELECT u.note
            FROM issue_updates u
            WHERE u.issue_id=i.id
            ORDER BY u.created_at DESC
            LIMIT 1
          ) AS latest_update
        FROM issues i
        LEFT JOIN staff_work_types wt ON wt.id=i.staff_work_type_id
        WHERE i.status='PENDING_VERIFICATION'
           OR i.verification_pending=true
        ORDER BY i.priority DESC,i.updated_at
        LIMIT 100
        """
    )


def _routines():
    rows = query_all(
        """
        SELECT
          r.*,
          sl.name AS managed_location,
          wt.name AS work_type_name,
          e.full_name AS employee_name
        FROM operations_routines r
        LEFT JOIN staff_locations sl ON sl.id=r.location_id
        LEFT JOIN staff_work_types wt ON wt.id=r.work_type_id
        LEFT JOIN staff_employees e ON e.id=r.assigned_employee_id
        ORDER BY r.active DESC,r.scheduled_time,r.name
        """
    )
    for row in rows:
        row["day_set"] = set(row["days_of_week"] or [])
        row["checklist_text"] = "\n".join(row["checklist_items"] or [])
    return rows


def _reference_data():
    return {
        "operations_employees": query_all(
            """
            SELECT id,full_name,department,role,active
            FROM staff_employees
            ORDER BY active DESC,department,full_name
            """
        ),
        "operations_locations": query_all(
            """
            SELECT id,name,department,active
            FROM staff_locations
            ORDER BY active DESC,sort_order,name
            """
        ),
        "operations_work_types": query_all(
            """
            SELECT id,name,department,active,checklist_template
            FROM staff_work_types
            ORDER BY active DESC,sort_order,name
            """
        ),
    }


def _render_injected(response, partial_name, context, marker):
    body = response.body.decode("utf-8")
    if marker not in body:
        raise RuntimeError(f"Template injection marker not found: {marker}")
    partial = templates.get_template(partial_name).render(**context)
    body = body.replace(marker, partial + "\n" + marker, 1)
    headers = dict(response.headers)
    headers.pop("content-length", None)
    return HTMLResponse(
        content=body,
        status_code=response.status_code,
        headers=headers,
    )


def _create_exception_for_run(run_id, note):
    run = query_one(
        """
        SELECT
          rr.id AS run_id, rr.exception_issue_id,
          r.id AS routine_id,r.name,r.priority,r.department,r.location_label,
          sl.name AS managed_location
        FROM operations_routine_runs rr
        JOIN operations_routines r ON r.id=rr.routine_id
        LEFT JOIN staff_locations sl ON sl.id=r.location_id
        WHERE rr.id=%s
        """,
        (run_id,),
    )
    if not run:
        raise HTTPException(status_code=404)

    if run["exception_issue_id"]:
        execute(
            """
            UPDATE issues
            SET description=%s,status='OPEN',
                next_action='Supervisor review / resolve operations exception',
                waiting_on='Operations',updated_at=now()
            WHERE id=%s
            """,
            (note, run["exception_issue_id"]),
        )
        issue_id = run["exception_issue_id"]
    else:
        issue_id = query_one(
            """
            INSERT INTO issues (
              title,description,category,priority,status,source,municipality,
              item_type,next_action,waiting_on,due_at,employee_location,
              operations_routine_id,operations_run_id
            )
            VALUES (
              %s,%s,'OPERATIONS',%s,'OPEN','OPERATIONS_ENGINE','Weehawken',
              'EXCEPTION','Supervisor review / resolve operations exception',
              'Operations',now(),%s,%s,%s
            )
            RETURNING id
            """,
            (
                "Operations exception: " + run["name"],
                note,
                max(3, int(run["priority"] or 3)),
                (run["location_label"] or run["managed_location"]),
                run["routine_id"],
                run["run_id"],
            ),
        )["id"]
        execute(
            """
            UPDATE operations_routine_runs
            SET exception_issue_id=%s,updated_at=now()
            WHERE id=%s
            """,
            (issue_id, run_id),
        )

    execute(
        """
        INSERT INTO issue_updates(issue_id,author,note)
        VALUES (%s,'Supervisor',%s)
        """,
        (issue_id, note),
    )
    return issue_id


remove_existing_get("/staff-admin")
remove_existing_get("/my-day")


@app.get("/staff-admin", response_class=HTMLResponse)
def phase3_staff_admin(
    request: Request,
    msg: str = "",
    view: str = "active",
    department: str = "",
    employee: str = "",
):
    response = staff_admin_module.staff_admin(
        request=request,
        msg=msg,
        view=view,
        department=department,
        employee=employee,
    )
    for item in response.context.get("queue", []):
        if item.get("status") == "PENDING_VERIFICATION" or item.get("verification_pending"):
            item["board_status"] = "AWAITING VERIFICATION"

    context = dict(response.context)
    context.update(
        {
            "today_operations": _today_operations(),
            "operations_counts": _operations_counts(),
            "verification_queue": _verification_queue(),
        }
    )
    return _render_injected(
        response,
        "operations_admin_insert.html",
        context,
        '<section class="board-tabs"',
    )


@app.get("/my-day", response_class=HTMLResponse)
def phase3_my_day(request: Request):
    response = schedule_module.my_day(request)
    context = dict(response.context)
    context.update(
        {
            "today_operations": _today_operations(),
            "operations_counts": _operations_counts(),
        }
    )
    return _render_injected(
        response,
        "operations_my_day_insert.html",
        context,
        '<section class="section-label"><span>DAILY BRIEF',
    )


@app.get("/operations-routines", response_class=HTMLResponse)
def operations_routines_page(request: Request, msg: str = ""):
    context = {
        "request": request,
        "routines": _routines(),
        "today_operations": _today_operations(),
        "operations_counts": _operations_counts(),
        "verification_queue": _verification_queue(),
        "days": DAYS,
        "msg": msg,
        "page": "operations-routines",
    }
    context.update(_reference_data())
    return templates.TemplateResponse(
        request=request,
        name="operations_routines.html",
        context=context,
    )


@app.post("/operations-routines/create")
def routine_create(
    name: str = Form(...),
    routine_kind: str = Form("WORK"),
    department: str = Form(""),
    location_id: str = Form(""),
    location_label: str = Form(""),
    work_type_id: str = Form(""),
    assigned_employee_id: str = Form(""),
    scheduled_time: str = Form(...),
    days: list[int] = Form(...),
    lead_minutes: int = Form(60),
    grace_minutes: int = Form(15),
    display_before_minutes: int = Form(30),
    display_after_minutes: int = Form(60),
    priority: int = Form(2),
    confirmation_required: str | None = Form(None),
    escalate_if_missed: str | None = Form(None),
    verification_required: str | None = Form(None),
    checklist_items: str = Form(""),
    description: str = Form(""),
    notes: str = Form(""),
):
    routine_kind = routine_kind.strip().upper()
    if routine_kind not in {"WORK", "AWARENESS"}:
        raise HTTPException(status_code=400, detail="Invalid routine type.")
    if not 1 <= priority <= 5:
        raise HTTPException(status_code=400, detail="Priority must be 1-5.")

    execute(
        """
        INSERT INTO operations_routines (
          name,routine_kind,department,location_id,location_label,
          work_type_id,assigned_employee_id,scheduled_time,days_of_week,
          lead_minutes,grace_minutes,display_before_minutes,display_after_minutes,
          priority,confirmation_required,escalate_if_missed,
          verification_required,checklist_items,description,notes
        )
        VALUES (
          %s,%s,%s,%s,%s,%s,%s,%s::time,%s,
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
        """,
        (
            name.strip(), routine_kind, department.strip() or None,
            _int_or_none(location_id), location_label.strip() or None,
            _int_or_none(work_type_id), _uuid_or_none(assigned_employee_id),
            _validate_time(scheduled_time), _clean_days(days),
            max(0, min(lead_minutes, 1440)), max(0, min(grace_minutes, 1440)),
            max(0, min(display_before_minutes, 1440)),
            max(0, min(display_after_minutes, 2880)), priority,
            confirmation_required is not None, escalate_if_missed is not None,
            (verification_required is not None) if routine_kind == "WORK" else False,
            _checklist_lines(checklist_items), description.strip() or None,
            notes.strip() or None,
        ),
    )
    return RedirectResponse(url="/operations-routines?msg=Routine+created", status_code=303)


@app.post("/operations-routines/{routine_id}/update")
def routine_update(
    routine_id: uuid.UUID,
    name: str = Form(...), routine_kind: str = Form("WORK"),
    department: str = Form(""), location_id: str = Form(""),
    location_label: str = Form(""), work_type_id: str = Form(""),
    assigned_employee_id: str = Form(""), scheduled_time: str = Form(...),
    days: list[int] = Form(...), lead_minutes: int = Form(60),
    grace_minutes: int = Form(15), display_before_minutes: int = Form(30),
    display_after_minutes: int = Form(60), priority: int = Form(2),
    confirmation_required: str | None = Form(None),
    escalate_if_missed: str | None = Form(None),
    verification_required: str | None = Form(None),
    checklist_items: str = Form(""), description: str = Form(""),
    notes: str = Form(""),
):
    routine_kind = routine_kind.strip().upper()
    if routine_kind not in {"WORK", "AWARENESS"}:
        raise HTTPException(status_code=400, detail="Invalid routine type.")

    execute(
        """
        UPDATE operations_routines
        SET name=%s,routine_kind=%s,department=%s,location_id=%s,
            location_label=%s,work_type_id=%s,assigned_employee_id=%s,
            scheduled_time=%s::time,days_of_week=%s,lead_minutes=%s,
            grace_minutes=%s,display_before_minutes=%s,display_after_minutes=%s,
            priority=%s,confirmation_required=%s,escalate_if_missed=%s,
            verification_required=%s,checklist_items=%s,description=%s,
            notes=%s,updated_at=now()
        WHERE id=%s
        """,
        (
            name.strip(), routine_kind, department.strip() or None,
            _int_or_none(location_id), location_label.strip() or None,
            _int_or_none(work_type_id), _uuid_or_none(assigned_employee_id),
            _validate_time(scheduled_time), _clean_days(days),
            max(0, min(lead_minutes, 1440)), max(0, min(grace_minutes, 1440)),
            max(0, min(display_before_minutes, 1440)),
            max(0, min(display_after_minutes, 2880)), max(1, min(priority, 5)),
            confirmation_required is not None, escalate_if_missed is not None,
            (verification_required is not None) if routine_kind == "WORK" else False,
            _checklist_lines(checklist_items), description.strip() or None,
            notes.strip() or None, routine_id,
        ),
    )
    return RedirectResponse(url="/operations-routines?msg=Routine+updated", status_code=303)


@app.post("/operations-routines/{routine_id}/toggle")
def routine_toggle(routine_id: uuid.UUID):
    execute(
        "UPDATE operations_routines SET active=NOT active,updated_at=now() WHERE id=%s",
        (routine_id,),
    )
    return RedirectResponse(url="/operations-routines?msg=Routine+status+changed", status_code=303)


@app.post("/operations-runs/{run_id}/ack")
def run_ack(run_id: uuid.UUID, return_to: str = Form("/staff-admin")):
    execute(
        """
        UPDATE operations_routine_runs
        SET acknowledged_at=now(),acknowledged_by='Supervisor',
            status='ACKNOWLEDGED',updated_at=now()
        WHERE id=%s
        """,
        (run_id,),
    )
    return RedirectResponse(
        url=return_to if return_to.startswith("/") and not return_to.startswith("//") else "/staff-admin",
        status_code=303,
    )


@app.post("/operations-runs/{run_id}/exception")
def run_exception(run_id: uuid.UUID, note: str = Form(...), return_to: str = Form("/staff-admin")):
    note = note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="Enter what is wrong.")
    issue_id = _create_exception_for_run(run_id, note)
    execute(
        """
        UPDATE operations_routine_runs
        SET exception_note=%s,status='EXCEPTION',exception_issue_id=%s,updated_at=now()
        WHERE id=%s
        """,
        (note, issue_id, run_id),
    )
    return RedirectResponse(
        url=return_to if return_to.startswith("/") and not return_to.startswith("//") else "/staff-admin",
        status_code=303,
    )


@app.post("/staff-admin/issue/{issue_id}/verify")
def verify_issue(issue_id: uuid.UUID, note: str = Form(""), return_to: str = Form("/staff-admin")):
    issue = query_one(
        """
        SELECT id,title FROM issues
        WHERE id=%s AND (status='PENDING_VERIFICATION' OR verification_pending=true)
        """,
        (issue_id,),
    )
    if not issue:
        raise HTTPException(status_code=404)
    execute(
        """
        UPDATE issues
        SET verified_at=now(),verified_by='Supervisor',verification_note=%s,
            verification_pending=false,status='RESOLVED',waiting_on=NULL,
            next_action=NULL,help_reason=NULL,closed_at=COALESCE(closed_at,now()),
            updated_at=now()
        WHERE id=%s
        """,
        (note.strip() or None, issue_id),
    )
    execute(
        "INSERT INTO issue_updates(issue_id,author,note) VALUES (%s,'Supervisor',%s)",
        (issue_id, "Completion verified." + ((" " + note.strip()) if note.strip() else "")),
    )
    return RedirectResponse(url=staff_admin_module.safe_return_to(return_to), status_code=303)


@app.post("/staff-admin/issue/{issue_id}/reject")
def reject_issue(issue_id: uuid.UUID, instruction: str = Form(...), return_to: str = Form("/staff-admin")):
    instruction = instruction.strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="Enter return instruction.")
    issue = query_one(
        """
        SELECT id,title FROM issues
        WHERE id=%s AND (status='PENDING_VERIFICATION' OR verification_pending=true)
        """,
        (issue_id,),
    )
    if not issue:
        raise HTTPException(status_code=404)
    execute(
        """
        UPDATE issues
        SET status='IN_PROGRESS',verification_pending=false,verified_at=NULL,
            verified_by=NULL,verification_note=NULL,closed_at=NULL,waiting_on=NULL,
            next_action=%s,updated_at=now()
        WHERE id=%s
        """,
        (instruction, issue_id),
    )
    execute(
        "INSERT INTO issue_updates(issue_id,author,note) VALUES (%s,'Supervisor',%s)",
        (issue_id, "Completion rejected. " + instruction),
    )
    return RedirectResponse(url=staff_admin_module.safe_return_to(return_to), status_code=303)
