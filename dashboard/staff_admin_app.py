import hashlib
import os
import secrets
import uuid
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app import app, execute, query_all, query_one, templates

UPLOAD_DIR = Path(os.getenv("STAFF_UPLOAD_DIR", "/app/uploads"))

BOARD_VIEWS = {
    "active",
    "unassigned",
    "assigned",
    "in_progress",
    "needs_help",
    "completed",
}


def hash_pin(pin):
    pin = pin.strip()
    if len(pin) < 4:
        raise HTTPException(status_code=400, detail="PIN must be at least 4 digits.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 150000)
    return salt.hex() + "$" + digest.hex()


def safe_return_to(value):
    value = (value or "").strip()
    if not value.startswith("/staff-admin") or value.startswith("//"):
        return "/staff-admin"
    return value


def age_label(hours):
    if hours is None:
        return ""
    hours = max(0, int(float(hours)))
    if hours < 1:
        return "<1h"
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return "1d" if days == 1 else f"{days}d"


def board_url(view="active", department="", employee=""):
    parts = ["view=" + quote_plus(view)]
    if department:
        parts.append("department=" + quote_plus(department))
    if employee:
        parts.append("employee=" + quote_plus(employee))
    return "/staff-admin?" + "&".join(parts)


@app.get("/staff-admin", response_class=HTMLResponse)
def staff_admin(
    request: Request,
    msg: str = "",
    view: str = "active",
    department: str = "",
    employee: str = "",
):
    view = view if view in BOARD_VIEWS else "active"
    department = department.strip()
    employee = employee.strip()

    employee_uuid = None
    if employee:
        try:
            employee_uuid = uuid.UUID(employee)
        except ValueError:
            employee = ""

    employees = query_all(
        """
        SELECT
          e.id,
          e.employee_id,
          e.full_name,
          e.department,
          e.role,
          e.active,
          e.updated_at,
          count(i.id) FILTER (
            WHERE i.status NOT IN ('RESOLVED','CLOSED')
          ) AS open_work,
          count(i.id) FILTER (
            WHERE i.status='IN_PROGRESS'
          ) AS in_progress_work,
          count(i.id) FILTER (
            WHERE i.status='ON_HOLD'
          ) AS needs_help_work,
          min(i.created_at) FILTER (
            WHERE i.status NOT IN ('RESOLVED','CLOSED')
          ) AS oldest_open_at
        FROM staff_employees e
        LEFT JOIN issues i
          ON i.assigned_employee_id=e.id
          AND i.source='EMPLOYEE_PORTAL'
        GROUP BY
          e.id,e.employee_id,e.full_name,e.department,
          e.role,e.active,e.updated_at
        ORDER BY e.active DESC,e.department,e.full_name
        """
    )

    locations = query_all(
        """
        SELECT id,name,department,active,sort_order
        FROM staff_locations
        ORDER BY active DESC,sort_order,name
        """
    )

    work_types = query_all(
        """
        SELECT
          id,name,department,checklist_template,
          priority_normal,priority_attention,
          priority_emergency,active,sort_order
        FROM staff_work_types
        ORDER BY active DESC,sort_order,name
        """
    )

    department_rows = query_all(
        """
        SELECT department
        FROM staff_employees
        WHERE NULLIF(trim(department),'') IS NOT NULL
        UNION
        SELECT submitted_department AS department
        FROM issues
        WHERE source='EMPLOYEE_PORTAL'
          AND NULLIF(trim(submitted_department),'') IS NOT NULL
        ORDER BY department
        """
    )
    departments = [row["department"] for row in department_rows]

    counts = query_one(
        """
        SELECT
          count(*) FILTER (
            WHERE status NOT IN ('RESOLVED','CLOSED')
          ) AS active,
          count(*) FILTER (
            WHERE status NOT IN ('RESOLVED','CLOSED')
              AND assigned_employee_id IS NULL
          ) AS unassigned,
          count(*) FILTER (
            WHERE status='OPEN'
              AND assigned_employee_id IS NOT NULL
          ) AS assigned,
          count(*) FILTER (
            WHERE status='IN_PROGRESS'
          ) AS in_progress,
          count(*) FILTER (
            WHERE status='ON_HOLD'
          ) AS needs_help,
          count(*) FILTER (
            WHERE status IN ('RESOLVED','CLOSED')
              AND COALESCE(closed_at,updated_at) >= now() - interval '30 days'
          ) AS completed
        FROM issues
        WHERE source='EMPLOYEE_PORTAL'
        """
    )

    where = ["i.source='EMPLOYEE_PORTAL'"]
    params = []

    view_where = {
        "active": "i.status NOT IN ('RESOLVED','CLOSED')",
        "unassigned": "i.status NOT IN ('RESOLVED','CLOSED') AND i.assigned_employee_id IS NULL",
        "assigned": "i.status='OPEN' AND i.assigned_employee_id IS NOT NULL",
        "in_progress": "i.status='IN_PROGRESS'",
        "needs_help": "i.status='ON_HOLD'",
        "completed": "i.status IN ('RESOLVED','CLOSED') AND COALESCE(i.closed_at,i.updated_at) >= now() - interval '30 days'",
    }
    where.append(view_where[view])

    if department:
        where.append(
            """
            upper(
              COALESCE(
                NULLIF(i.submitted_department,''),
                submit_emp.department,
                assigned_emp.department,
                ''
              )
            ) = upper(%s)
            """
        )
        params.append(department)

    if employee_uuid:
        where.append("i.assigned_employee_id=%s")
        params.append(employee_uuid)

    order_by = (
        "COALESCE(i.closed_at,i.updated_at) DESC"
        if view == "completed"
        else
        """
        CASE
          WHEN i.status='ON_HOLD' THEN 0
          WHEN i.assigned_employee_id IS NULL THEN 1
          WHEN i.status='IN_PROGRESS' THEN 2
          ELSE 3
        END,
        i.priority DESC,
        i.created_at
        """
    )

    queue = query_all(
        f"""
        SELECT
          i.id,
          i.title,
          i.description,
          i.status,
          i.priority,
          i.employee_location,
          i.submitted_by,
          i.submitted_department,
          i.assigned_to,
          i.assigned_employee_id,
          i.next_action,
          i.waiting_on,
          i.help_reason,
          i.created_at,
          i.updated_at,
          i.closed_at,
          wt.name AS work_type_name,
          assigned_emp.department AS assigned_department,
          extract(epoch from (now() - i.created_at)) / 3600.0 AS age_hours,
          (
            SELECT count(*)
            FROM issue_photos p
            WHERE p.issue_id=i.id
          ) AS photo_count,
          (
            SELECT p.id
            FROM issue_photos p
            WHERE p.issue_id=i.id
            ORDER BY p.created_at
            LIMIT 1
          ) AS first_photo_id,
          (
            SELECT u.note
            FROM issue_updates u
            WHERE u.issue_id=i.id
              AND u.author='Supervisor'
            ORDER BY u.created_at DESC
            LIMIT 1
          ) AS latest_supervisor_note,
          (
            SELECT u.created_at
            FROM issue_updates u
            WHERE u.issue_id=i.id
              AND u.author='Supervisor'
            ORDER BY u.created_at DESC
            LIMIT 1
          ) AS latest_supervisor_at
        FROM issues i
        LEFT JOIN staff_work_types wt
          ON wt.id=i.staff_work_type_id
        LEFT JOIN staff_employees assigned_emp
          ON assigned_emp.id=i.assigned_employee_id
        LEFT JOIN staff_employees submit_emp
          ON submit_emp.id=i.submitted_employee_id
        WHERE {" AND ".join(where)}
        ORDER BY {order_by}
        LIMIT 200
        """,
        tuple(params),
    )

    for item in queue:
        item["age_label"] = age_label(item["age_hours"])
        hours = float(item["age_hours"] or 0)
        item["age_level"] = "late" if hours >= 72 else ("aging" if hours >= 24 else "fresh")

        if item["status"] == "ON_HOLD":
            item["board_status"] = "NEEDS HELP"
        elif item["assigned_employee_id"] is None and item["status"] not in {"RESOLVED", "CLOSED"}:
            item["board_status"] = "UNASSIGNED"
        elif item["status"] == "OPEN":
            item["board_status"] = "ASSIGNED"
        elif item["status"] == "IN_PROGRESS":
            item["board_status"] = "IN PROGRESS"
        else:
            item["board_status"] = "COMPLETED"

    current_url = board_url(view, department, employee)
    view_urls = {
        name: board_url(name, department, employee)
        for name in BOARD_VIEWS
    }

    return templates.TemplateResponse(
        request=request,
        name="staff_admin.html",
        context={
            "employees": employees,
            "locations": locations,
            "work_types": work_types,
            "queue": queue,
            "counts": counts,
            "departments": departments,
            "selected_view": view,
            "selected_department": department,
            "selected_employee": employee,
            "current_url": current_url,
            "view_urls": view_urls,
            "msg": msg,
            "page": "staff-admin",
        },
    )


@app.post("/staff-admin/employee/create")
def employee_create(
    employee_id: str = Form(...),
    full_name: str = Form(...),
    department: str = Form(...),
    role: str = Form("EMPLOYEE"),
    pin: str = Form(...),
):
    employee_id = employee_id.strip().upper()
    full_name = full_name.strip()
    department = department.strip()
    role = role.strip().upper()

    if role not in {"EMPLOYEE", "SUPERVISOR"}:
        raise HTTPException(status_code=400)

    execute(
        """
        INSERT INTO staff_employees (
          employee_id,full_name,department,role,pin_hash,active
        )
        VALUES (%s,%s,%s,%s,%s,true)
        """,
        (employee_id,full_name,department,role,hash_pin(pin)),
    )

    return RedirectResponse(
        url="/staff-admin?msg=Employee+created",
        status_code=303,
    )


@app.post("/staff-admin/employee/{employee_uuid}/toggle")
def employee_toggle(employee_uuid: uuid.UUID):
    execute(
        """
        UPDATE staff_employees
        SET active=NOT active,updated_at=now()
        WHERE id=%s
        """,
        (employee_uuid,),
    )
    return RedirectResponse(
        url="/staff-admin?msg=Employee+status+updated",
        status_code=303,
    )


@app.post("/staff-admin/employee/{employee_uuid}/pin")
def employee_pin(
    employee_uuid: uuid.UUID,
    pin: str = Form(...),
):
    execute(
        """
        UPDATE staff_employees
        SET pin_hash=%s,updated_at=now()
        WHERE id=%s
        """,
        (hash_pin(pin),employee_uuid),
    )
    return RedirectResponse(
        url="/staff-admin?msg=PIN+reset",
        status_code=303,
    )


@app.post("/staff-admin/location/create")
def location_create(
    name: str = Form(...),
    department: str = Form(""),
    sort_order: int = Form(100),
):
    execute(
        """
        INSERT INTO staff_locations (
          name,department,active,sort_order
        )
        VALUES (%s,%s,true,%s)
        """,
        (name.strip(),department.strip() or None,sort_order),
    )
    return RedirectResponse(
        url="/staff-admin?msg=Location+created",
        status_code=303,
    )


@app.post("/staff-admin/location/{location_id}/toggle")
def location_toggle(location_id: int):
    execute(
        """
        UPDATE staff_locations
        SET active=NOT active,updated_at=now()
        WHERE id=%s
        """,
        (location_id,),
    )
    return RedirectResponse(
        url="/staff-admin?msg=Location+updated",
        status_code=303,
    )


@app.post("/staff-admin/work-type/create")
def work_type_create(
    name: str = Form(...),
    checklist_template: str = Form("GENERAL"),
    sort_order: int = Form(100),
):
    if checklist_template not in {
        "GENERAL","SITE_CHECK","EVENT_SETUP",
        "VEHICLE","OPEN_CLOSE","NONE",
    }:
        checklist_template = "GENERAL"

    execute(
        """
        INSERT INTO staff_work_types (
          name,checklist_template,priority_normal,
          priority_attention,priority_emergency,
          active,sort_order
        )
        VALUES (%s,%s,2,4,5,true,%s)
        """,
        (name.strip(),checklist_template,sort_order),
    )
    return RedirectResponse(
        url="/staff-admin?msg=Work+type+created",
        status_code=303,
    )


@app.post("/staff-admin/work-type/{work_type_id}/toggle")
def work_type_toggle(work_type_id: int):
    execute(
        """
        UPDATE staff_work_types
        SET active=NOT active,updated_at=now()
        WHERE id=%s
        """,
        (work_type_id,),
    )
    return RedirectResponse(
        url="/staff-admin?msg=Work+type+updated",
        status_code=303,
    )


@app.post("/staff-admin/issue/{issue_id}/assign")
def admin_assign(
    issue_id: uuid.UUID,
    employee_uuid: str = Form(""),
    return_to: str = Form("/staff-admin"),
):
    return_to = safe_return_to(return_to)
    employee_uuid = employee_uuid.strip()

    if not employee_uuid:
        execute(
            """
            UPDATE issues
            SET assigned_employee_id=NULL,
                assigned_to=NULL,
                updated_at=now()
            WHERE id=%s
              AND source='EMPLOYEE_PORTAL'
            """,
            (issue_id,),
        )
        execute(
            """
            INSERT INTO issue_updates (issue_id,author,note)
            VALUES (%s,'Supervisor','Assignment cleared.')
            """,
            (issue_id,),
        )
        return RedirectResponse(url=return_to,status_code=303)

    try:
        parsed_employee_uuid = uuid.UUID(employee_uuid)
    except ValueError:
        raise HTTPException(status_code=400,detail="Invalid employee.")

    employee = query_one(
        """
        SELECT id,full_name
        FROM staff_employees
        WHERE id=%s AND active=true
        """,
        (parsed_employee_uuid,),
    )
    if not employee:
        raise HTTPException(status_code=400)

    execute(
        """
        UPDATE issues
        SET assigned_employee_id=%s,
            assigned_to=%s,
            next_action=COALESCE(
              NULLIF(trim(next_action),''),
              'Complete assigned work'
            ),
            updated_at=now()
        WHERE id=%s
          AND source='EMPLOYEE_PORTAL'
        """,
        (employee["id"],employee["full_name"],issue_id),
    )
    execute(
        """
        INSERT INTO issue_updates (issue_id,author,note)
        VALUES (%s,'Supervisor',%s)
        """,
        (issue_id,"Assigned to " + employee["full_name"] + "."),
    )
    return RedirectResponse(url=return_to,status_code=303)


@app.post("/staff-admin/issue/{issue_id}/instruction")
def admin_instruction(
    issue_id: uuid.UUID,
    instruction: str = Form(...),
    return_to: str = Form("/staff-admin"),
):
    return_to = safe_return_to(return_to)
    instruction = instruction.strip()

    if not instruction:
        raise HTTPException(status_code=400,detail="Instruction cannot be empty.")
    if len(instruction) > 500:
        raise HTTPException(status_code=400,detail="Instruction is too long.")

    issue = query_one(
        """
        SELECT id
        FROM issues
        WHERE id=%s AND source='EMPLOYEE_PORTAL'
        """,
        (issue_id,),
    )
    if not issue:
        raise HTTPException(status_code=404)

    execute(
        """
        UPDATE issues
        SET next_action=%s,updated_at=now()
        WHERE id=%s
        """,
        (instruction,issue_id),
    )
    execute(
        """
        INSERT INTO issue_updates (issue_id,author,note)
        VALUES (%s,'Supervisor',%s)
        """,
        (issue_id,"Instruction: " + instruction),
    )
    return RedirectResponse(url=return_to,status_code=303)


@app.get("/staff-admin/photo/{photo_id}")
def admin_photo(photo_id: uuid.UUID):
    photo = query_one(
        """
        SELECT stored_name,original_name,content_type
        FROM issue_photos
        WHERE id=%s
        """,
        (photo_id,),
    )
    if not photo:
        raise HTTPException(status_code=404)

    path = UPLOAD_DIR / photo["stored_name"]
    if not path.is_file():
        raise HTTPException(status_code=404)

    return FileResponse(
        path,
        media_type=photo["content_type"],
        filename=photo["original_name"] or path.name,
    )
