import os
import threading
import time
import uuid
from collections import defaultdict, deque

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

import employee_app as legacy


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


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


SECURE_COOKIES = env_bool(
    "STAFF_SECURE_COOKIES",
    False,
)
TRUST_PROXY = env_bool(
    "STAFF_TRUST_PROXY",
    False,
)
PUBLIC_ORIGIN = os.getenv(
    "STAFF_PUBLIC_ORIGIN",
    "",
).strip().rstrip("/")

ALLOWED_HOSTS = [
    item.strip()
    for item in os.getenv(
        "STAFF_ALLOWED_HOSTS",
        "*",
    ).split(",")
    if item.strip()
]

staff_app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=ALLOWED_HOSTS or ["*"],
)

LOGIN_WINDOW_SECONDS = int(
    os.getenv(
        "STAFF_LOGIN_WINDOW_SECONDS",
        "900",
    )
)
LOGIN_MAX_FAILURES = int(
    os.getenv(
        "STAFF_LOGIN_MAX_FAILURES",
        "8",
    )
)

_login_failures = defaultdict(deque)
_login_lock = threading.Lock()


def _client_key(request):
    if TRUST_PROXY:
        forwarded = request.headers.get(
            "x-forwarded-for",
            "",
        )
        if forwarded:
            return forwarded.split(",", 1)[0].strip()

    if request.client:
        return request.client.host

    return "unknown"


def _prune_failures(key, now):
    cutoff = now - LOGIN_WINDOW_SECONDS
    failures = _login_failures[key]

    while failures and failures[0] < cutoff:
        failures.popleft()

    return failures


def _login_blocked(request):
    key = _client_key(request)
    now = time.monotonic()

    with _login_lock:
        failures = _prune_failures(
            key,
            now,
        )
        return len(failures) >= LOGIN_MAX_FAILURES


def _record_login_result(request, success):
    key = _client_key(request)
    now = time.monotonic()

    with _login_lock:
        failures = _prune_failures(
            key,
            now,
        )

        if success:
            failures.clear()
        else:
            failures.append(now)


def _strip_legacy_token(location):
    if not location:
        return location

    legacy_prefix = (
        "/staff/"
        + str(legacy.STAFF_TOKEN)
    )

    if location == legacy_prefix:
        return "/staff"

    if location.startswith(
        legacy_prefix + "/"
    ):
        return (
            "/staff"
            + location[len(legacy_prefix):]
        )

    return location


@staff_app.middleware("http")
async def staff_security_middleware(
    request: Request,
    call_next,
):
    legacy_prefix = (
        "/staff/"
        + str(legacy.STAFF_TOKEN)
    )

    if (
        request.url.path == legacy_prefix
        or request.url.path.startswith(
            legacy_prefix + "/"
        )
    ):
        target = _strip_legacy_token(
            request.url.path
        )

        if request.url.query:
            target += "?" + request.url.query

        return RedirectResponse(
            url=target,
            status_code=307,
        )

    if (
        request.method
        in {"POST", "PUT", "PATCH", "DELETE"}
        and PUBLIC_ORIGIN
    ):
        origin = request.headers.get(
            "origin",
            "",
        ).strip().rstrip("/")

        if origin and origin != PUBLIC_ORIGIN:
            return PlainTextResponse(
                "Invalid request origin.",
                status_code=403,
            )

    response = await call_next(request)

    location = response.headers.get(
        "location"
    )
    cleaned_location = _strip_legacy_token(
        location
    )

    if cleaned_location != location:
        response.headers[
            "location"
        ] = cleaned_location

    cookie = response.headers.get(
        "set-cookie"
    )

    if (
        SECURE_COOKIES
        and cookie
        and legacy.SESSION_COOKIE
        in cookie
        and "secure" not in cookie.lower()
    ):
        response.headers[
            "set-cookie"
        ] = cookie + "; Secure"

    response.headers[
        "X-Content-Type-Options"
    ] = "nosniff"
    response.headers[
        "X-Frame-Options"
    ] = "DENY"
    response.headers[
        "Referrer-Policy"
    ] = "no-referrer"
    response.headers[
        "X-Robots-Tag"
    ] = "noindex, nofollow"
    response.headers[
        "Permissions-Policy"
    ] = "camera=(self), geolocation=()"
    response.headers[
        "Content-Security-Policy"
    ] = (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )

    if request.url.path.startswith(
        "/staff"
    ):
        response.headers[
            "Cache-Control"
        ] = "no-store"

    if SECURE_COOKIES:
        response.headers[
            "Strict-Transport-Security"
        ] = "max-age=31536000"

    return response


@staff_app.get("/health")
def health():
    return {
        "status": "ok",
        "version": 3,
        "public_routes": True,
        "secure_cookies": SECURE_COOKIES,
    }


@staff_app.get("/")
def root():
    return RedirectResponse(
        url="/staff",
        status_code=303,
    )


@staff_app.get("/staff")
def portal_entry(
    request: Request,
    error: str = "",
):
    return legacy.portal_entry(
        request,
        legacy.STAFF_TOKEN,
        error,
    )


@staff_app.post("/staff/login")
def staff_login(
    request: Request,
    employee_id: str = Form(...),
    pin: str = Form(...),
):
    if _login_blocked(request):
        return legacy.templates.TemplateResponse(
            request=request,
            name="staff_login.html",
            context={
                "token": "",
                "error": (
                    "Too many failed sign-in attempts. "
                    "Try again later or contact a supervisor."
                ),
            },
            status_code=429,
        )

    response = legacy.staff_login(
        request,
        legacy.STAFF_TOKEN,
        employee_id,
        pin,
    )

    _record_login_result(
        request,
        success=(
            response.status_code
            != 401
        ),
    )

    return response


@staff_app.get("/staff/logout")
def staff_logout():
    return legacy.staff_logout(
        legacy.STAFF_TOKEN
    )


@staff_app.get("/staff/home")
def staff_home(
    request: Request,
):
    return legacy.staff_home(
        request,
        legacy.STAFF_TOKEN,
    )


@staff_app.get("/staff/report")
def staff_report_page(
    request: Request,
):
    return legacy.staff_report_page(
        request,
        legacy.STAFF_TOKEN,
    )


@staff_app.post("/staff/report")
async def staff_report_create(
    request: Request,
    location_id: int = Form(...),
    other_location: str = Form(""),
    work_type_id: int = Form(...),
    urgency: str = Form("NORMAL"),
    description: str = Form(""),
    photos: list[UploadFile] = File(default=[]),
):
    return await legacy.staff_report_create(
        request,
        legacy.STAFF_TOKEN,
        location_id,
        other_location,
        work_type_id,
        urgency,
        description,
        photos,
    )


@staff_app.get("/staff/work")
def staff_work(
    request: Request,
):
    return legacy.staff_work(
        request,
        legacy.STAFF_TOKEN,
    )


@staff_app.get(
    "/staff/ticket/{issue_id}"
)
def staff_ticket(
    request: Request,
    issue_id: uuid.UUID,
    created: int = 0,
):
    return legacy.staff_ticket(
        request,
        legacy.STAFF_TOKEN,
        issue_id,
        created,
    )


@staff_app.get(
    "/staff/photo/{photo_id}"
)
def staff_photo(
    request: Request,
    photo_id: uuid.UUID,
):
    return legacy.staff_photo(
        request,
        legacy.STAFF_TOKEN,
        photo_id,
    )


@staff_app.post(
    "/staff/ticket/{issue_id}/start"
)
def start_work(
    request: Request,
    issue_id: uuid.UUID,
):
    return legacy.start_work(
        request,
        legacy.STAFF_TOKEN,
        issue_id,
    )


@staff_app.post(
    "/staff/ticket/{issue_id}/help"
)
def need_help(
    request: Request,
    issue_id: uuid.UUID,
    reason: str = Form(...),
    detail: str = Form(""),
):
    return legacy.need_help(
        request,
        legacy.STAFF_TOKEN,
        issue_id,
        reason,
        detail,
    )


@staff_app.post(
    "/staff/ticket/{issue_id}/complete"
)
async def complete_work(
    request: Request,
    issue_id: uuid.UUID,
    completion_note: str = Form(""),
    completion_photo: UploadFile | None = File(None),
):
    return await legacy.complete_work(
        request,
        legacy.STAFF_TOKEN,
        issue_id,
        completion_note,
        completion_photo,
    )


@staff_app.post(
    "/staff/ticket/{issue_id}/note"
)
def staff_ticket_note(
    request: Request,
    issue_id: uuid.UUID,
    note: str = Form(...),
):
    return legacy.staff_ticket_note(
        request,
        legacy.STAFF_TOKEN,
        issue_id,
        note,
    )


@staff_app.post(
    "/staff/ticket/{issue_id}/photo"
)
async def add_ticket_photo(
    request: Request,
    issue_id: uuid.UUID,
    phase: str = Form("BEFORE"),
    photo: UploadFile = File(...),
):
    return await legacy.add_ticket_photo(
        request,
        legacy.STAFF_TOKEN,
        issue_id,
        phase,
        photo,
    )


@staff_app.post(
    "/staff/ticket/{issue_id}"
    "/checklist/{check_id}"
)
def checklist_toggle(
    request: Request,
    issue_id: uuid.UUID,
    check_id: uuid.UUID,
):
    return legacy.checklist_toggle(
        request,
        legacy.STAFF_TOKEN,
        issue_id,
        check_id,
    )


@staff_app.get("/staff/supervisor")
def supervisor_queue(
    request: Request,
):
    return legacy.supervisor_queue(
        request,
        legacy.STAFF_TOKEN,
    )


@staff_app.post(
    "/staff/supervisor/{issue_id}/assign"
)
def supervisor_assign(
    request: Request,
    issue_id: uuid.UUID,
    employee_uuid: uuid.UUID = Form(...),
):
    return legacy.supervisor_assign(
        request,
        legacy.STAFF_TOKEN,
        issue_id,
        employee_uuid,
    )


def _verify_legacy_token(token):
    legacy.verify_portal(token)


@staff_app.api_route(
    "/staff/{token}",
    methods=["GET", "POST"],
)
def legacy_staff_root(
    request: Request,
    token: str,
):
    _verify_legacy_token(token)

    target = "/staff"
    if request.url.query:
        target += "?" + request.url.query

    return RedirectResponse(
        url=target,
        status_code=307,
    )


@staff_app.api_route(
    "/staff/{token}/{rest:path}",
    methods=["GET", "POST"],
)
def legacy_staff_route(
    request: Request,
    token: str,
    rest: str,
):
    _verify_legacy_token(token)

    target = "/staff/" + rest
    if request.url.query:
        target += "?" + request.url.query

    return RedirectResponse(
        url=target,
        status_code=307,
    )
