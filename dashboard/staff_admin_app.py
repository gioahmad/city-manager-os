import hashlib
import os
import secrets
import uuid
from pathlib import Path

from fastapi import Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
)

from app import (
    app,
    execute,
    query_all,
    query_one,
    templates,
)


UPLOAD_DIR = Path(
    os.getenv(
        "STAFF_UPLOAD_DIR",
        "/app/uploads",
    )
)


def hash_pin(pin):
    pin = pin.strip()

    if len(pin) < 4:
        raise HTTPException(
            status_code=400,
            detail=(
                "PIN must be at "
                "least 4 digits."
            ),
        )

    salt = secrets.token_bytes(
        16
    )

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        pin.encode(),
        salt,
        150000,
    )

    return (
        salt.hex()
        + "$"
        + digest.hex()
    )


@app.get(
    "/staff-admin",
    response_class=HTMLResponse,
)
def staff_admin(
    request: Request,
    msg: str = "",
):
    employees = query_all(
        """
        SELECT
          id,
          employee_id,
          full_name,
          department,
          role,
          active,
          updated_at
        FROM staff_employees
        ORDER BY
          active DESC,
          department,
          full_name
        """
    )

    locations = query_all(
        """
        SELECT
          id,
          name,
          department,
          active,
          sort_order
        FROM staff_locations
        ORDER BY
          active DESC,
          sort_order,
          name
        """
    )

    work_types = query_all(
        """
        SELECT
          id,
          name,
          department,
          checklist_template,
          priority_normal,
          priority_attention,
          priority_emergency,
          active,
          sort_order
        FROM staff_work_types
        ORDER BY
          active DESC,
          sort_order,
          name
        """
    )

    queue = query_all(
        """
        SELECT
          i.id,
          i.title,
          i.status,
          i.priority,
          i.employee_location,
          i.submitted_by,
          i.assigned_to,
          i.created_at,
          wt.name AS work_type_name,
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
          ) AS first_photo_id
        FROM issues i
        LEFT JOIN staff_work_types wt
          ON wt.id=i.staff_work_type_id
        WHERE i.source='EMPLOYEE_PORTAL'
          AND i.status NOT IN (
            'RESOLVED',
            'CLOSED'
          )
        ORDER BY
          CASE
            WHEN i.assigned_employee_id
              IS NULL THEN 0
            ELSE 1
          END,
          i.priority DESC,
          i.created_at
        LIMIT 150
        """
    )

    return templates.TemplateResponse(
        request=request,
        name="staff_admin.html",
        context={
            "employees": employees,
            "locations": locations,
            "work_types": work_types,
            "queue": queue,
            "msg": msg,
            "page": "staff-admin",
        },
    )


@app.post(
    "/staff-admin/employee/create"
)
def employee_create(
    employee_id: str = Form(...),
    full_name: str = Form(...),
    department: str = Form(...),
    role: str = Form("EMPLOYEE"),
    pin: str = Form(...),
):
    employee_id = (
        employee_id
        .strip()
        .upper()
    )

    full_name = (
        full_name.strip()
    )

    department = (
        department.strip()
    )

    role = (
        role
        .strip()
        .upper()
    )

    if role not in {
        "EMPLOYEE",
        "SUPERVISOR",
    }:
        raise HTTPException(
            status_code=400,
        )

    execute(
        """
        INSERT INTO staff_employees (
          employee_id,
          full_name,
          department,
          role,
          pin_hash,
          active
        )
        VALUES (
          %s,%s,%s,%s,%s,true
        )
        """,
        (
            employee_id,
            full_name,
            department,
            role,
            hash_pin(pin),
        ),
    )

    return RedirectResponse(
        url=(
            "/staff-admin"
            "?msg=Employee+created"
        ),
        status_code=303,
    )


@app.post(
    "/staff-admin/employee/{employee_uuid}/toggle"
)
def employee_toggle(
    employee_uuid: uuid.UUID,
):
    execute(
        """
        UPDATE staff_employees
        SET
          active=NOT active,
          updated_at=now()
        WHERE id=%s
        """,
        (employee_uuid,),
    )

    return RedirectResponse(
        url=(
            "/staff-admin"
            "?msg=Employee+status+updated"
        ),
        status_code=303,
    )


@app.post(
    "/staff-admin/employee/{employee_uuid}/pin"
)
def employee_pin(
    employee_uuid: uuid.UUID,
    pin: str = Form(...),
):
    execute(
        """
        UPDATE staff_employees
        SET
          pin_hash=%s,
          updated_at=now()
        WHERE id=%s
        """,
        (
            hash_pin(pin),
            employee_uuid,
        ),
    )

    return RedirectResponse(
        url=(
            "/staff-admin"
            "?msg=PIN+reset"
        ),
        status_code=303,
    )


@app.post(
    "/staff-admin/location/create"
)
def location_create(
    name: str = Form(...),
    department: str = Form(""),
    sort_order: int = Form(100),
):
    execute(
        """
        INSERT INTO staff_locations (
          name,
          department,
          active,
          sort_order
        )
        VALUES (
          %s,%s,true,%s
        )
        """,
        (
            name.strip(),
            department.strip()
            or None,
            sort_order,
        ),
    )

    return RedirectResponse(
        url=(
            "/staff-admin"
            "?msg=Location+created"
        ),
        status_code=303,
    )


@app.post(
    "/staff-admin/location/{location_id}/toggle"
)
def location_toggle(
    location_id: int,
):
    execute(
        """
        UPDATE staff_locations
        SET
          active=NOT active,
          updated_at=now()
        WHERE id=%s
        """,
        (location_id,),
    )

    return RedirectResponse(
        url=(
            "/staff-admin"
            "?msg=Location+updated"
        ),
        status_code=303,
    )


@app.post(
    "/staff-admin/work-type/create"
)
def work_type_create(
    name: str = Form(...),
    checklist_template: str = Form("GENERAL"),
    sort_order: int = Form(100),
):
    if checklist_template not in {
        "GENERAL",
        "SITE_CHECK",
        "EVENT_SETUP",
        "VEHICLE",
        "OPEN_CLOSE",
        "NONE",
    }:
        checklist_template = "GENERAL"

    execute(
        """
        INSERT INTO staff_work_types (
          name,
          checklist_template,
          priority_normal,
          priority_attention,
          priority_emergency,
          active,
          sort_order
        )
        VALUES (
          %s,%s,2,4,5,true,%s
        )
        """,
        (
            name.strip(),
            checklist_template,
            sort_order,
        ),
    )

    return RedirectResponse(
        url=(
            "/staff-admin"
            "?msg=Work+type+created"
        ),
        status_code=303,
    )


@app.post(
    "/staff-admin/work-type/{work_type_id}/toggle"
)
def work_type_toggle(
    work_type_id: int,
):
    execute(
        """
        UPDATE staff_work_types
        SET
          active=NOT active,
          updated_at=now()
        WHERE id=%s
        """,
        (work_type_id,),
    )

    return RedirectResponse(
        url=(
            "/staff-admin"
            "?msg=Work+type+updated"
        ),
        status_code=303,
    )


@app.post(
    "/staff-admin/issue/{issue_id}/assign"
)
def admin_assign(
    issue_id: uuid.UUID,
    employee_uuid: uuid.UUID = Form(...),
):
    employee = query_one(
        """
        SELECT
          id,
          full_name
        FROM staff_employees
        WHERE id=%s
          AND active=true
        """,
        (employee_uuid,),
    )

    if not employee:
        raise HTTPException(
            status_code=400,
        )

    execute(
        """
        UPDATE issues
        SET
          assigned_employee_id=%s,
          assigned_to=%s,
          next_action='Complete assigned work',
          updated_at=now()
        WHERE id=%s
          AND source='EMPLOYEE_PORTAL'
        """,
        (
            employee["id"],
            employee["full_name"],
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
        VALUES (
          %s,
          'Supervisor',
          %s
        )
        """,
        (
            issue_id,
            (
                "Assigned to "
                + employee["full_name"]
                + "."
            ),
        ),
    )

    return RedirectResponse(
        url=(
            "/staff-admin"
            "?msg=Work+assigned"
        ),
        status_code=303,
    )


@app.get(
    "/staff-admin/photo/{photo_id}"
)
def admin_photo(
    photo_id: uuid.UUID,
):
    photo = query_one(
        """
        SELECT
          stored_name,
          original_name,
          content_type
        FROM issue_photos
        WHERE id=%s
        """,
        (photo_id,),
    )

    if not photo:
        raise HTTPException(
            status_code=404,
        )

    path = (
        UPLOAD_DIR
        / photo["stored_name"]
    )

    if not path.is_file():
        raise HTTPException(
            status_code=404,
        )

    return FileResponse(
        path,
        media_type=photo[
            "content_type"
        ],
        filename=photo[
            "original_name"
        ]
        or path.name,
    )
