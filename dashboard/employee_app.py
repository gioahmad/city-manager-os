import os
import secrets
import uuid

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


staff_app = FastAPI(
    title="Township Operations Staff Portal",
    docs_url=None,
    redoc_url=None,
)

staff_app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static",
)

templates = Jinja2Templates(directory="templates")

STAFF_TOKEN = os.environ["STAFF_TOKEN"]

DEPARTMENTS = [
    "DPW",
    "Buildings & Grounds",
    "Parks",
    "Public Safety",
    "Administration",
    "Other",
]

CHECKLISTS = {
    "NONE": [],
    "GENERAL": [
        "Inspect location / condition",
        "Complete required work",
        "Clean and secure work area",
        "Confirm work is complete",
    ],
    "SITE_CHECK": [
        "Inspect site",
        "Check safety hazards",
        "Check access / doors / gates",
        "Check lighting / utilities",
        "Report deficiencies",
    ],
    "EVENT_SETUP": [
        "Confirm event location",
        "Set tables / chairs / equipment",
        "Check power / lighting",
        "Check trash / sanitation",
        "Final walkthrough",
    ],
    "VEHICLE": [
        "Visual safety inspection",
        "Check fuel / charge",
        "Check tires / lights",
        "Check equipment",
        "Report defects",
    ],
    "OPEN_CLOSE": [
        "Inspect facility / park",
        "Unlock or secure required areas",
        "Check lights / utilities",
        "Check restrooms / common areas",
        "Final safety walkthrough",
    ],
}


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


def insert_one(sql, params=None):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
        conn.commit()
        return row


def verify(token: str):
    if not secrets.compare_digest(
        str(token),
        str(STAFF_TOKEN),
    ):
        raise HTTPException(status_code=404)


def ticket_code(issue_id):
    return str(issue_id).split("-")[0].upper()


@staff_app.get("/health")
def health():
    return {"status": "ok"}


@staff_app.get("/staff/{token}", response_class=HTMLResponse)
def staff_home(
    request: Request,
    token: str,
    msg: str = "",
):
    verify(token)

    counts = query_one(
        """
        SELECT
          count(*) FILTER (
            WHERE source='EMPLOYEE_PORTAL'
              AND status NOT IN ('RESOLVED','CLOSED')
          ) AS open_staff,
          count(*) FILTER (
            WHERE source='EMPLOYEE_PORTAL'
              AND status='IN_PROGRESS'
          ) AS in_progress,
          count(*) FILTER (
            WHERE source='EMPLOYEE_PORTAL'
              AND status IN ('RESOLVED','CLOSED')
              AND closed_at >= now() - interval '7 days'
          ) AS completed_week
        FROM issues
        """
    )

    return templates.TemplateResponse(
        request=request,
        name="staff_home.html",
        context={
            "token": token,
            "counts": counts,
            "msg": msg,
        },
    )


@staff_app.get(
    "/staff/{token}/report",
    response_class=HTMLResponse,
)
def staff_report_page(
    request: Request,
    token: str,
):
    verify(token)

    return templates.TemplateResponse(
        request=request,
        name="staff_report.html",
        context={
            "token": token,
            "departments": DEPARTMENTS,
            "checklists": CHECKLISTS,
        },
    )


@staff_app.post("/staff/{token}/report")
def staff_report_create(
    token: str,
    employee_name: str = Form(...),
    department: str = Form(...),
    location: str = Form(""),
    title: str = Form(...),
    description: str = Form(""),
    priority: int = Form(3),
    checklist_template: str = Form("NONE"),
):
    verify(token)

    employee_name = employee_name.strip()
    title = title.strip()

    if not employee_name or not title:
        raise HTTPException(
            status_code=400,
            detail="Employee name and request are required",
        )

    if priority < 1 or priority > 5:
        priority = 3

    row = insert_one(
        """
        INSERT INTO issues (
          title,
          description,
          category,
          priority,
          status,
          source,
          municipality,
          item_type,
          next_action,
          submitted_by,
          submitted_department,
          employee_location
        )
        VALUES (
          %s,
          %s,
          %s,
          %s,
          'OPEN',
          'EMPLOYEE_PORTAL',
          'Weehawken',
          'TASK',
          'Supervisor review / assign',
          %s,
          %s,
          %s
        )
        RETURNING id
        """,
        (
            title,
            description.strip() or None,
            department.strip().upper(),
            priority,
            employee_name,
            department.strip(),
            location.strip() or None,
        ),
    )

    issue_id = row["id"]

    items = CHECKLISTS.get(
        checklist_template,
        [],
    )

    for order, label in enumerate(items, start=1):
        execute(
            """
            INSERT INTO issue_checklist_items (
              issue_id,
              label,
              sort_order
            )
            VALUES (%s,%s,%s)
            """,
            (
                issue_id,
                label,
                order * 10,
            ),
        )

    execute(
        """
        INSERT INTO issue_updates (
          issue_id,
          author,
          note
        )
        VALUES (%s,%s,%s)
        """,
        (
            issue_id,
            employee_name,
            "Work request submitted.",
        ),
    )

    return RedirectResponse(
        url=(
            f"/staff/{token}/ticket/{issue_id}"
            f"?name={employee_name}&created=1"
        ),
        status_code=303,
    )


@staff_app.get(
    "/staff/{token}/work",
    response_class=HTMLResponse,
)
def staff_work(
    request: Request,
    token: str,
    name: str = "",
):
    verify(token)

    name = name.strip()
    rows = []

    if name:
        rows = query_all(
            """
            SELECT
              id,
              title,
              status,
              priority,
              assigned_to,
              submitted_by,
              submitted_department,
              employee_location,
              next_action,
              created_at,
              updated_at,
              (
                SELECT count(*)
                FROM issue_checklist_items c
                WHERE c.issue_id=issues.id
              ) AS checklist_total,
              (
                SELECT count(*)
                FROM issue_checklist_items c
                WHERE c.issue_id=issues.id
                  AND c.completed=true
              ) AS checklist_done
            FROM issues
            WHERE source='EMPLOYEE_PORTAL'
              AND (
                lower(trim(COALESCE(assigned_to,''))) =
                  lower(trim(%s))
                OR
                lower(trim(COALESCE(submitted_by,''))) =
                  lower(trim(%s))
              )
            ORDER BY
              CASE
                WHEN status='IN_PROGRESS' THEN 0
                WHEN status='OPEN' THEN 1
                WHEN status='ON_HOLD' THEN 2
                ELSE 3
              END,
              priority DESC,
              updated_at DESC
            LIMIT 100
            """,
            (
                name,
                name,
            ),
        )

    for r in rows:
        r["ticket_code"] = ticket_code(r["id"])

    return templates.TemplateResponse(
        request=request,
        name="staff_work.html",
        context={
            "token": token,
            "name": name,
            "items": rows,
        },
    )


@staff_app.get(
    "/staff/{token}/ticket/{issue_id}",
    response_class=HTMLResponse,
)
def staff_ticket(
    request: Request,
    token: str,
    issue_id: uuid.UUID,
    name: str = "",
    created: int = 0,
):
    verify(token)

    issue = query_one(
        """
        SELECT
          id,
          title,
          description,
          category,
          priority,
          status,
          assigned_to,
          submitted_by,
          submitted_department,
          employee_location,
          next_action,
          waiting_on,
          created_at,
          updated_at,
          closed_at
        FROM issues
        WHERE id=%s
          AND source='EMPLOYEE_PORTAL'
        """,
        (issue_id,),
    )

    if not issue:
        raise HTTPException(status_code=404)

    issue["ticket_code"] = ticket_code(issue["id"])

    checklist = query_all(
        """
        SELECT
          id,
          label,
          completed,
          completed_by,
          completed_at
        FROM issue_checklist_items
        WHERE issue_id=%s
        ORDER BY sort_order, created_at
        """,
        (issue_id,),
    )

    updates = query_all(
        """
        SELECT
          author,
          note,
          created_at
        FROM issue_updates
        WHERE issue_id=%s
        ORDER BY created_at DESC
        LIMIT 30
        """,
        (issue_id,),
    )

    return templates.TemplateResponse(
        request=request,
        name="staff_ticket.html",
        context={
            "token": token,
            "name": name.strip(),
            "issue": issue,
            "checklist": checklist,
            "updates": updates,
            "created": created,
        },
    )


@staff_app.post(
    "/staff/{token}/ticket/{issue_id}/note"
)
def staff_ticket_note(
    token: str,
    issue_id: uuid.UUID,
    employee_name: str = Form(...),
    note: str = Form(...),
):
    verify(token)

    employee_name = employee_name.strip()
    note = note.strip()

    if employee_name and note:
        execute(
            """
            INSERT INTO issue_updates (
              issue_id,
              author,
              note
            )
            VALUES (%s,%s,%s)
            """,
            (
                issue_id,
                employee_name,
                note,
            ),
        )

        execute(
            """
            UPDATE issues
            SET updated_at=now()
            WHERE id=%s
            """,
            (issue_id,),
        )

    return RedirectResponse(
        url=(
            f"/staff/{token}/ticket/{issue_id}"
            f"?name={employee_name}"
        ),
        status_code=303,
    )


@staff_app.post(
    "/staff/{token}/ticket/{issue_id}/status"
)
def staff_ticket_status(
    token: str,
    issue_id: uuid.UUID,
    employee_name: str = Form(...),
    status: str = Form(...),
):
    verify(token)

    status = status.upper().strip()

    if status not in {
        "OPEN",
        "IN_PROGRESS",
        "ON_HOLD",
        "RESOLVED",
    }:
        raise HTTPException(status_code=400)

    execute(
        """
        UPDATE issues
        SET
          status=%s,
          closed_at=CASE
            WHEN %s='RESOLVED'
              THEN COALESCE(closed_at,now())
            ELSE NULL
          END,
          updated_at=now()
        WHERE id=%s
          AND source='EMPLOYEE_PORTAL'
        """,
        (
            status,
            status,
            issue_id,
        ),
    )

    execute(
        """
        INSERT INTO issue_updates (
          issue_id,
          author,
          note
        )
        VALUES (%s,%s,%s)
        """,
        (
            issue_id,
            employee_name.strip() or "Employee",
            f"Status changed to {status.replace('_',' ')}.",
        ),
    )

    return RedirectResponse(
        url=(
            f"/staff/{token}/ticket/{issue_id}"
            f"?name={employee_name.strip()}"
        ),
        status_code=303,
    )


@staff_app.post(
    "/staff/{token}/ticket/{issue_id}/checklist/{check_id}"
)
def staff_checklist_toggle(
    token: str,
    issue_id: uuid.UUID,
    check_id: uuid.UUID,
    employee_name: str = Form(...),
):
    verify(token)

    employee_name = employee_name.strip()

    execute(
        """
        UPDATE issue_checklist_items
        SET
          completed=NOT completed,
          completed_by=CASE
            WHEN completed=false THEN %s
            ELSE NULL
          END,
          completed_at=CASE
            WHEN completed=false THEN now()
            ELSE NULL
          END
        WHERE id=%s
          AND issue_id=%s
        """,
        (
            employee_name or "Employee",
            check_id,
            issue_id,
        ),
    )

    execute(
        """
        UPDATE issues
        SET updated_at=now()
        WHERE id=%s
        """,
        (issue_id,),
    )

    return RedirectResponse(
        url=(
            f"/staff/{token}/ticket/{issue_id}"
            f"?name={employee_name}"
        ),
        status_code=303,
    )
