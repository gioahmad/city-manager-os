import hashlib
import hmac
import os
import secrets
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


staff_app = FastAPI(
    title="Weehawken Employee Operations",
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
SESSION_SECRET = os.environ["STAFF_SESSION_SECRET"].encode()
UPLOAD_DIR = Path(
    os.getenv("STAFF_UPLOAD_DIR", "/app/uploads")
)

UPLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

SESSION_COOKIE = "cmos_staff_session"
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTOS_PER_UPLOAD = 5

ALLOWED_IMAGES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

HELP_REASONS = [
    "Need material / parts",
    "Need equipment",
    "Need another employee",
    "Cannot access area",
    "Safety issue",
    "Supervisor needed",
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
        host=os.getenv(
            "DB_HOST",
            "citymanager-postgis",
        ),
        port=int(
            os.getenv(
                "DB_PORT",
                "5432",
            )
        ),
        dbname=os.getenv(
            "DB_NAME",
            "citymanager",
        ),
        user=os.getenv(
            "DB_USER",
            "citymanager_app",
        ),
        password=os.environ["DB_PASSWORD"],
        row_factory=dict_row,
        connect_timeout=5,
    )


def query_all(sql, params=None):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                params or (),
            )
            return cur.fetchall()


def query_one(sql, params=None):
    rows = query_all(
        sql,
        params,
    )
    return rows[0] if rows else {}


def execute(sql, params=None):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                params or (),
            )
        conn.commit()


def insert_one(sql, params=None):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                params or (),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def verify_portal(token):
    if not secrets.compare_digest(
        str(token),
        str(STAFF_TOKEN),
    ):
        raise HTTPException(
            status_code=404,
        )


def verify_pin(pin, encoded):
    try:
        salt_hex, hash_hex = encoded.split(
            "$",
            1,
        )

        salt = bytes.fromhex(
            salt_hex
        )

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            pin.encode(),
            salt,
            150000,
        ).hex()

        return hmac.compare_digest(
            actual,
            hash_hex,
        )
    except Exception:
        return False


def session_value(employee_uuid):
    value = str(
        employee_uuid
    )

    signature = hmac.new(
        SESSION_SECRET,
        value.encode(),
        hashlib.sha256,
    ).hexdigest()

    return (
        value
        + "."
        + signature
    )


def current_employee(request):
    raw = request.cookies.get(
        SESSION_COOKIE
    )

    if not raw or "." not in raw:
        return None

    employee_uuid, signature = raw.rsplit(
        ".",
        1,
    )

    expected = hmac.new(
        SESSION_SECRET,
        employee_uuid.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        signature,
        expected,
    ):
        return None

    try:
        employee_uuid = uuid.UUID(
            employee_uuid
        )
    except Exception:
        return None

    employee = query_one(
        """
        SELECT
          id,
          employee_id,
          full_name,
          department,
          role,
          active
        FROM staff_employees
        WHERE id=%s
          AND active=true
        """,
        (employee_uuid,),
    )

    return employee or None


def employee_redirect(token):
    return RedirectResponse(
        url=f"/staff/{token}",
        status_code=303,
    )


def ticket_code(issue_id):
    return str(
        issue_id
    ).split("-")[0].upper()


def can_access_ticket(
    employee,
    issue,
):
    if not employee:
        return False

    if employee["role"] == "SUPERVISOR":
        return True

    return (
        issue.get(
            "submitted_employee_id"
        )
        == employee["id"]
        or
        issue.get(
            "assigned_employee_id"
        )
        == employee["id"]
    )


async def save_photo(
    issue_id,
    employee_id,
    upload,
    phase,
):
    if not upload:
        return None

    if not upload.filename:
        return None

    content_type = (
        upload.content_type
        or ""
    ).lower()

    if content_type not in ALLOWED_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Photo must be JPG, PNG, "
                "WebP or HEIC."
            ),
        )

    data = await upload.read(
        MAX_PHOTO_BYTES + 1
    )

    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "Photo is too large. "
                "Maximum size is 10 MB."
            ),
        )

    ext = ALLOWED_IMAGES[
        content_type
    ]

    stored_name = (
        uuid.uuid4().hex
        + ext
    )

    path = (
        UPLOAD_DIR
        / stored_name
    )

    path.write_bytes(
        data
    )

    try:
        row = insert_one(
            """
            INSERT INTO issue_photos (
              issue_id,
              uploaded_by_employee_id,
              phase,
              original_name,
              stored_name,
              content_type,
              size_bytes
            )
            VALUES (
              %s,%s,%s,%s,%s,%s,%s
            )
            RETURNING id
            """,
            (
                issue_id,
                employee_id,
                phase,
                upload.filename,
                stored_name,
                content_type,
                len(data),
            ),
        )

        return row["id"]

    except Exception:
        path.unlink(
            missing_ok=True
        )
        raise


def ticket_record(issue_id):
    issue = query_one(
        """
        SELECT
          i.id,
          i.title,
          i.description,
          i.category,
          i.priority,
          i.status,
          i.assigned_to,
          i.submitted_by,
          i.submitted_department,
          i.employee_location,
          i.next_action,
          i.waiting_on,
          i.help_reason,
          i.submitted_employee_id,
          i.assigned_employee_id,
          i.staff_location_id,
          i.staff_work_type_id,
          i.created_at,
          i.updated_at,
          i.closed_at,
          sl.name AS location_name,
          wt.name AS work_type_name
        FROM issues i
        LEFT JOIN staff_locations sl
          ON sl.id=i.staff_location_id
        LEFT JOIN staff_work_types wt
          ON wt.id=i.staff_work_type_id
        WHERE i.id=%s
          AND i.source='EMPLOYEE_PORTAL'
        """,
        (issue_id,),
    )

    if issue:
        issue["ticket_code"] = ticket_code(
            issue["id"]
        )

    return issue


@staff_app.get("/health")
def health():
    return {
        "status": "ok",
        "version": 2,
    }


@staff_app.get(
    "/staff/{token}",
    response_class=HTMLResponse,
)
def portal_entry(
    request: Request,
    token: str,
    error: str = "",
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if employee:
        return RedirectResponse(
            url=f"/staff/{token}/home",
            status_code=303,
        )

    return templates.TemplateResponse(
        request=request,
        name="staff_login.html",
        context={
            "token": token,
            "error": error,
        },
    )


@staff_app.post(
    "/staff/{token}/login"
)
def staff_login(
    request: Request,
    token: str,
    employee_id: str = Form(...),
    pin: str = Form(...),
):
    verify_portal(
        token
    )

    employee_id = (
        employee_id
        .strip()
        .upper()
    )

    employee = query_one(
        """
        SELECT
          id,
          employee_id,
          full_name,
          department,
          role,
          pin_hash
        FROM staff_employees
        WHERE upper(employee_id)=upper(%s)
          AND active=true
        """,
        (employee_id,),
    )

    if (
        not employee
        or not verify_pin(
            pin,
            employee["pin_hash"],
        )
    ):
        return templates.TemplateResponse(
            request=request,
            name="staff_login.html",
            context={
                "token": token,
                "error": (
                    "Employee ID or PIN "
                    "is incorrect."
                ),
            },
            status_code=401,
        )

    response = RedirectResponse(
        url=f"/staff/{token}/home",
        status_code=303,
    )

    response.set_cookie(
        SESSION_COOKIE,
        session_value(
            employee["id"]
        ),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=False,
    )

    return response


@staff_app.get(
    "/staff/{token}/logout"
)
def staff_logout(
    token: str,
):
    verify_portal(
        token
    )

    response = RedirectResponse(
        url=f"/staff/{token}",
        status_code=303,
    )

    response.delete_cookie(
        SESSION_COOKIE
    )

    return response


@staff_app.get(
    "/staff/{token}/home",
    response_class=HTMLResponse,
)
def staff_home(
    request: Request,
    token: str,
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    counts = query_one(
        """
        SELECT
          count(*) FILTER (
            WHERE status NOT IN (
              'RESOLVED',
              'CLOSED'
            )
          ) AS open_count,

          count(*) FILTER (
            WHERE status='IN_PROGRESS'
          ) AS in_progress,

          count(*) FILTER (
            WHERE status IN (
              'RESOLVED',
              'CLOSED'
            )
            AND closed_at >=
              now() - interval '7 days'
          ) AS completed_week

        FROM issues
        WHERE source='EMPLOYEE_PORTAL'
          AND (
            submitted_employee_id=%s
            OR assigned_employee_id=%s
          )
        """,
        (
            employee["id"],
            employee["id"],
        ),
    )

    return templates.TemplateResponse(
        request=request,
        name="staff_home.html",
        context={
            "token": token,
            "employee": employee,
            "counts": counts,
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
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    locations = query_all(
        """
        SELECT id, name
        FROM staff_locations
        WHERE active=true
        ORDER BY sort_order, name
        """
    )

    work_types = query_all(
        """
        SELECT id, name
        FROM staff_work_types
        WHERE active=true
          AND (
            department IS NULL
            OR department=''
            OR upper(department)=upper(%s)
          )
        ORDER BY sort_order, name
        """,
        (
            employee["department"],
        ),
    )

    return templates.TemplateResponse(
        request=request,
        name="staff_report.html",
        context={
            "token": token,
            "employee": employee,
            "locations": locations,
            "work_types": work_types,
        },
    )


@staff_app.post(
    "/staff/{token}/report"
)
async def staff_report_create(
    request: Request,
    token: str,
    location_id: int = Form(...),
    other_location: str = Form(""),
    work_type_id: int = Form(...),
    urgency: str = Form("NORMAL"),
    description: str = Form(""),
    photos: list[UploadFile] = File(default=[]),
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    location = query_one(
        """
        SELECT id, name
        FROM staff_locations
        WHERE id=%s
          AND active=true
        """,
        (location_id,),
    )

    work_type = query_one(
        """
        SELECT
          id,
          name,
          checklist_template,
          priority_normal,
          priority_attention,
          priority_emergency
        FROM staff_work_types
        WHERE id=%s
          AND active=true
        """,
        (work_type_id,),
    )

    if not location or not work_type:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid location "
                "or work type."
            ),
        )

    location_name = location[
        "name"
    ]

    if location_name in {
        "Street / Public Right of Way",
        "Other / Enter Location",
    }:
        if not other_location.strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    "Enter the street, "
                    "address or location."
                ),
            )

        location_name = (
            other_location.strip()
        )

    urgency = (
        urgency
        .upper()
        .strip()
    )

    if urgency == "EMERGENCY":
        priority = work_type[
            "priority_emergency"
        ]
    elif urgency == "ATTENTION":
        priority = work_type[
            "priority_attention"
        ]
    else:
        priority = work_type[
            "priority_normal"
        ]
        urgency = "NORMAL"

    title = (
        work_type["name"]
        + " — "
        + location_name
    )

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
          employee_location,
          submitted_employee_id,
          staff_location_id,
          staff_work_type_id
        )
        VALUES (
          %s,%s,%s,%s,
          'OPEN',
          'EMPLOYEE_PORTAL',
          'Weehawken',
          'TASK',
          'Supervisor review / assign',
          %s,%s,%s,%s,%s,%s
        )
        RETURNING id
        """,
        (
            title,
            description.strip() or None,
            employee[
                "department"
            ].upper(),
            priority,
            employee["full_name"],
            employee["department"],
            location_name,
            employee["id"],
            location["id"],
            work_type["id"],
        ),
    )

    issue_id = row[
        "id"
    ]

    checklist = CHECKLISTS.get(
        work_type[
            "checklist_template"
        ],
        CHECKLISTS["GENERAL"],
    )

    for order, label in enumerate(
        checklist,
        start=1,
    ):
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
            employee["full_name"],
            (
                "Work request submitted. "
                f"Urgency: {urgency}."
            ),
        ),
    )

    valid_photos = [
        p
        for p in photos
        if p and p.filename
    ]

    if len(valid_photos) > MAX_PHOTOS_PER_UPLOAD:
        raise HTTPException(
            status_code=400,
            detail=(
                "Maximum 5 photos "
                "per upload."
            ),
        )

    for photo in valid_photos:
        await save_photo(
            issue_id,
            employee["id"],
            photo,
            "BEFORE",
        )

    return RedirectResponse(
        url=(
            f"/staff/{token}"
            f"/ticket/{issue_id}"
            "?created=1"
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
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    rows = query_all(
        """
        SELECT
          i.id,
          i.title,
          i.status,
          i.priority,
          i.assigned_to,
          i.employee_location,
          i.next_action,
          i.help_reason,
          i.created_at,
          i.updated_at,
          wt.name AS work_type_name,

          (
            SELECT count(*)
            FROM issue_checklist_items c
            WHERE c.issue_id=i.id
          ) AS checklist_total,

          (
            SELECT count(*)
            FROM issue_checklist_items c
            WHERE c.issue_id=i.id
              AND c.completed=true
          ) AS checklist_done,

          (
            SELECT count(*)
            FROM issue_photos p
            WHERE p.issue_id=i.id
          ) AS photo_count

        FROM issues i
        LEFT JOIN staff_work_types wt
          ON wt.id=i.staff_work_type_id

        WHERE i.source='EMPLOYEE_PORTAL'
          AND (
            i.submitted_employee_id=%s
            OR i.assigned_employee_id=%s
          )

        ORDER BY
          CASE
            WHEN i.status='IN_PROGRESS'
              THEN 0
            WHEN i.status='OPEN'
              THEN 1
            WHEN i.status='ON_HOLD'
              THEN 2
            ELSE 3
          END,
          i.priority DESC,
          i.updated_at DESC

        LIMIT 100
        """,
        (
            employee["id"],
            employee["id"],
        ),
    )

    for row in rows:
        row["ticket_code"] = (
            ticket_code(
                row["id"]
            )
        )

    return templates.TemplateResponse(
        request=request,
        name="staff_work.html",
        context={
            "token": token,
            "employee": employee,
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
    created: int = 0,
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    issue = ticket_record(
        issue_id
    )

    if (
        not issue
        or not can_access_ticket(
            employee,
            issue,
        )
    ):
        raise HTTPException(
            status_code=404,
        )

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
        LIMIT 40
        """,
        (issue_id,),
    )

    photos = query_all(
        """
        SELECT
          id,
          phase,
          original_name,
          created_at
        FROM issue_photos
        WHERE issue_id=%s
        ORDER BY created_at
        """,
        (issue_id,),
    )

    return templates.TemplateResponse(
        request=request,
        name="staff_ticket.html",
        context={
            "token": token,
            "employee": employee,
            "issue": issue,
            "checklist": checklist,
            "updates": updates,
            "photos": photos,
            "help_reasons": HELP_REASONS,
            "created": created,
        },
    )


@staff_app.get(
    "/staff/{token}/photo/{photo_id}"
)
def staff_photo(
    request: Request,
    token: str,
    photo_id: uuid.UUID,
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    photo = query_one(
        """
        SELECT
          p.id,
          p.issue_id,
          p.stored_name,
          p.original_name,
          p.content_type,
          i.submitted_employee_id,
          i.assigned_employee_id
        FROM issue_photos p
        JOIN issues i
          ON i.id=p.issue_id
        WHERE p.id=%s
          AND i.source='EMPLOYEE_PORTAL'
        """,
        (photo_id,),
    )

    if not photo:
        raise HTTPException(
            status_code=404,
        )

    issue = {
        "submitted_employee_id":
            photo[
                "submitted_employee_id"
            ],
        "assigned_employee_id":
            photo[
                "assigned_employee_id"
            ],
    }

    if not can_access_ticket(
        employee,
        issue,
    ):
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


@staff_app.post(
    "/staff/{token}/ticket/{issue_id}/start"
)
def start_work(
    request: Request,
    token: str,
    issue_id: uuid.UUID,
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    issue = ticket_record(
        issue_id
    )

    if (
        not issue
        or not can_access_ticket(
            employee,
            issue,
        )
    ):
        raise HTTPException(
            status_code=404,
        )

    execute(
        """
        UPDATE issues
        SET
          status='IN_PROGRESS',
          help_reason=NULL,
          waiting_on=NULL,
          updated_at=now()
        WHERE id=%s
        """,
        (issue_id,),
    )

    execute(
        """
        INSERT INTO issue_updates (
          issue_id,
          author,
          note
        )
        VALUES (%s,%s,'Work started.')
        """,
        (
            issue_id,
            employee["full_name"],
        ),
    )

    return RedirectResponse(
        url=(
            f"/staff/{token}"
            f"/ticket/{issue_id}"
        ),
        status_code=303,
    )


@staff_app.post(
    "/staff/{token}/ticket/{issue_id}/help"
)
def need_help(
    request: Request,
    token: str,
    issue_id: uuid.UUID,
    reason: str = Form(...),
    detail: str = Form(""),
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    issue = ticket_record(
        issue_id
    )

    if (
        not issue
        or not can_access_ticket(
            employee,
            issue,
        )
    ):
        raise HTTPException(
            status_code=404,
        )

    if reason not in HELP_REASONS:
        raise HTTPException(
            status_code=400,
        )

    note = (
        f"Needs help: {reason}"
    )

    if detail.strip():
        note += (
            " — "
            + detail.strip()
        )

    execute(
        """
        UPDATE issues
        SET
          status='ON_HOLD',
          help_reason=%s,
          waiting_on=%s,
          updated_at=now()
        WHERE id=%s
        """,
        (
            reason,
            reason,
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
            employee["full_name"],
            note,
        ),
    )

    return RedirectResponse(
        url=(
            f"/staff/{token}"
            f"/ticket/{issue_id}"
        ),
        status_code=303,
    )


@staff_app.post(
    "/staff/{token}/ticket/{issue_id}/complete"
)
async def complete_work(
    request: Request,
    token: str,
    issue_id: uuid.UUID,
    completion_note: str = Form(""),
    completion_photo: UploadFile | None = File(None),
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    issue = ticket_record(
        issue_id
    )

    if (
        not issue
        or not can_access_ticket(
            employee,
            issue,
        )
    ):
        raise HTTPException(
            status_code=404,
        )

    if (
        completion_photo
        and completion_photo.filename
    ):
        await save_photo(
            issue_id,
            employee["id"],
            completion_photo,
            "AFTER",
        )

    execute(
        """
        UPDATE issues
        SET
          status='RESOLVED',
          help_reason=NULL,
          waiting_on=NULL,
          closed_at=COALESCE(
            closed_at,
            now()
          ),
          updated_at=now()
        WHERE id=%s
        """,
        (issue_id,),
    )

    note = "Work marked complete."

    if completion_note.strip():
        note += (
            " "
            + completion_note.strip()
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
            employee["full_name"],
            note,
        ),
    )

    return RedirectResponse(
        url=(
            f"/staff/{token}"
            f"/ticket/{issue_id}"
        ),
        status_code=303,
    )


@staff_app.post(
    "/staff/{token}/ticket/{issue_id}/note"
)
def staff_ticket_note(
    request: Request,
    token: str,
    issue_id: uuid.UUID,
    note: str = Form(...),
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    issue = ticket_record(
        issue_id
    )

    if (
        not issue
        or not can_access_ticket(
            employee,
            issue,
        )
    ):
        raise HTTPException(
            status_code=404,
        )

    note = note.strip()

    if note:
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
                employee["full_name"],
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
            f"/staff/{token}"
            f"/ticket/{issue_id}"
        ),
        status_code=303,
    )


@staff_app.post(
    "/staff/{token}/ticket/{issue_id}/photo"
)
async def add_ticket_photo(
    request: Request,
    token: str,
    issue_id: uuid.UUID,
    phase: str = Form("BEFORE"),
    photo: UploadFile = File(...),
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    issue = ticket_record(
        issue_id
    )

    if (
        not issue
        or not can_access_ticket(
            employee,
            issue,
        )
    ):
        raise HTTPException(
            status_code=404,
        )

    phase = (
        phase
        .upper()
        .strip()
    )

    if phase not in {
        "BEFORE",
        "AFTER",
    }:
        phase = "BEFORE"

    await save_photo(
        issue_id,
        employee["id"],
        photo,
        phase,
    )

    return RedirectResponse(
        url=(
            f"/staff/{token}"
            f"/ticket/{issue_id}"
        ),
        status_code=303,
    )


@staff_app.post(
    "/staff/{token}/ticket/{issue_id}"
    "/checklist/{check_id}"
)
def checklist_toggle(
    request: Request,
    token: str,
    issue_id: uuid.UUID,
    check_id: uuid.UUID,
):
    verify_portal(
        token
    )

    employee = current_employee(
        request
    )

    if not employee:
        return employee_redirect(
            token
        )

    issue = ticket_record(
        issue_id
    )

    if (
        not issue
        or not can_access_ticket(
            employee,
            issue,
        )
    ):
        raise HTTPException(
            status_code=404,
        )

    execute(
        """
        UPDATE issue_checklist_items
        SET
          completed=NOT completed,

          completed_by=CASE
            WHEN completed=false
              THEN %s
            ELSE NULL
          END,

          completed_at=CASE
            WHEN completed=false
              THEN now()
            ELSE NULL
          END

        WHERE id=%s
          AND issue_id=%s
        """,
        (
            employee["full_name"],
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
            f"/staff/{token}"
            f"/ticket/{issue_id}"
        ),
        status_code=303,
    )


@staff_app.get(
    "/staff/{token}/supervisor",
    response_class=HTMLResponse,
)
def supervisor_queue(
    request: Request,
    token: str,
):
    verify_portal(
        token
    )

    supervisor = current_employee(
        request
    )

    if (
        not supervisor
        or supervisor["role"]
        != "SUPERVISOR"
    ):
        raise HTTPException(
            status_code=404,
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
          ) AS photo_count
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

    employees = query_all(
        """
        SELECT
          id,
          employee_id,
          full_name,
          department
        FROM staff_employees
        WHERE active=true
          AND role IN (
            'EMPLOYEE',
            'SUPERVISOR'
          )
        ORDER BY
          department,
          full_name
        """
    )

    for row in queue:
        row["ticket_code"] = (
            ticket_code(
                row["id"]
            )
        )

    return templates.TemplateResponse(
        request=request,
        name="staff_supervisor.html",
        context={
            "token": token,
            "employee": supervisor,
            "queue": queue,
            "employees": employees,
        },
    )


@staff_app.post(
    "/staff/{token}/supervisor/{issue_id}/assign"
)
def supervisor_assign(
    request: Request,
    token: str,
    issue_id: uuid.UUID,
    employee_uuid: uuid.UUID = Form(...),
):
    verify_portal(
        token
    )

    supervisor = current_employee(
        request
    )

    if (
        not supervisor
        or supervisor["role"]
        != "SUPERVISOR"
    ):
        raise HTTPException(
            status_code=404,
        )

    assignee = query_one(
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

    if not assignee:
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
            assignee["id"],
            assignee["full_name"],
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
            supervisor["full_name"],
            (
                "Assigned to "
                + assignee["full_name"]
                + "."
            ),
        ),
    )

    return RedirectResponse(
        url=(
            f"/staff/{token}"
            "/supervisor"
        ),
        status_code=303,
    )
