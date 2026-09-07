from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app import templates


COOKIE_NAME = "cmos_private_session"
ROLES = {"EXECUTIVE", "SUPERVISOR", "READ_ONLY"}
SESSION_TTL_SECONDS = 12 * 60 * 60
PBKDF2_ITERATIONS = 310_000

_ADMIN_WRITE_PREFIXES = (
    "/integrations",
    "/api-lab",
    "/source-health",
    "/deliveries",
    "/routing",
    "/subscribers",
    "/watchlist",
    "/rules",
    "/alert-admin",
    "/modules",
    "/admin-tools",
)

_PUBLIC_PATHS = {
    "/health",
    "/login",
    "/logout",
}

_LOGIN_WINDOW_SECONDS = int(os.getenv("CMOS_LOGIN_WINDOW_SECONDS", "900"))
_LOGIN_MAX_FAILURES = int(os.getenv("CMOS_LOGIN_MAX_FAILURES", "8"))
_login_failures: dict[str, deque[float]] = defaultdict(deque)
_login_lock = threading.Lock()


@dataclass(frozen=True)
class Account:
    username: str
    role: str
    password_hash: str


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def hash_password(password: str, *, salt_hex: str | None = None) -> str:
    if not password:
        raise ValueError("password is required")
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return (
        f"pbkdf2_sha256:{PBKDF2_ITERATIONS}:"
        f"{salt.hex()}:{digest.hex()}"
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_hex, expected_hex = encoded.split(":", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except (TypeError, ValueError):
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def _accounts() -> dict[str, Account]:
    accounts: dict[str, Account] = {}
    for role, prefix in (
        ("EXECUTIVE", "CMOS_EXECUTIVE"),
        ("SUPERVISOR", "CMOS_SUPERVISOR"),
        ("READ_ONLY", "CMOS_READONLY"),
    ):
        username = os.getenv(f"{prefix}_USERNAME", "").strip()
        password_hash = os.getenv(f"{prefix}_PASSWORD_HASH", "").strip()
        if not username or not password_hash:
            continue
        accounts[username.casefold()] = Account(
            username=username,
            role=role,
            password_hash=password_hash,
        )
    return accounts


def _client_key(request: Request) -> str:
    if env_bool("CMOS_AUTH_TRUST_PROXY", False):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _prune_failures(key: str, now: float) -> deque[float]:
    cutoff = now - _LOGIN_WINDOW_SECONDS
    failures = _login_failures[key]
    while failures and failures[0] < cutoff:
        failures.popleft()
    return failures


def _login_blocked(request: Request) -> bool:
    key = _client_key(request)
    now = time.monotonic()
    with _login_lock:
        return len(_prune_failures(key, now)) >= _LOGIN_MAX_FAILURES


def _record_login(request: Request, success: bool) -> None:
    key = _client_key(request)
    now = time.monotonic()
    with _login_lock:
        failures = _prune_failures(key, now)
        if success:
            failures.clear()
        else:
            failures.append(now)


def _session_secret() -> bytes:
    value = os.getenv("CMOS_SESSION_SECRET", "").strip()
    if len(value) < 32:
        raise RuntimeError("CMOS_SESSION_SECRET must be at least 32 characters")
    return value.encode("utf-8")


def _sign(payload: str) -> str:
    return hmac.new(
        _session_secret(),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _issue_session(account: Account) -> str:
    expires = int(time.time()) + SESSION_TTL_SECONDS
    nonce = secrets.token_hex(8)
    payload = f"{account.username}|{account.role}|{expires}|{nonce}"
    return f"{payload}|{_sign(payload)}"


def _read_session(value: str | None) -> Account | None:
    if not value:
        return None
    try:
        username, role, expires_text, nonce, signature = value.split("|", 4)
        expires = int(expires_text)
    except (TypeError, ValueError):
        return None
    if role not in ROLES or expires < int(time.time()) or not nonce:
        return None
    payload = f"{username}|{role}|{expires}|{nonce}"
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    account = _accounts().get(username.casefold())
    if not account or account.role != role:
        return None
    return account


def _safe_next(value: str) -> str:
    value = (value or "/my-day").strip()
    if not value.startswith("/") or value.startswith("//"):
        return "/my-day"
    if value.startswith("/login") or value.startswith("/logout"):
        return "/my-day"
    return value


def _automation_authorized(request: Request) -> bool:
    expected = os.getenv("CMOS_AUTOMATION_TOKEN", "").strip()
    supplied = request.headers.get("x-cmos-automation-key", "").strip()
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def _same_origin_ok(request: Request) -> bool:
    configured = os.getenv("CMOS_PUBLIC_ORIGIN", "").strip().rstrip("/")
    if not configured:
        return True
    origin = request.headers.get("origin", "").strip().rstrip("/")
    return not origin or hmac.compare_digest(origin, configured)


def _role_allows(account: Account, request: Request) -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    if account.role == "EXECUTIVE":
        return True
    if account.role == "READ_ONLY":
        return False
    path = request.url.path
    return not any(path.startswith(prefix) for prefix in _ADMIN_WRITE_PREFIXES)


def configure_private_auth(app) -> None:
    auth_enabled = env_bool("CMOS_AUTH_ENABLED", False)
    secure_cookie = env_bool("CMOS_AUTH_SECURE_COOKIES", False)

    @app.get("/login", response_class=HTMLResponse)
    def private_login_page(request: Request, next: str = "/my-day", error: str = ""):
        if not auth_enabled:
            return RedirectResponse(url="/my-day", status_code=303)
        existing = _read_session(request.cookies.get(COOKIE_NAME))
        if existing:
            return RedirectResponse(url=_safe_next(next), status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="private_login.html",
            context={
                "error": error,
                "next": _safe_next(next),
            },
        )

    @app.post("/login")
    def private_login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/my-day"),
    ):
        if not auth_enabled:
            return RedirectResponse(url="/my-day", status_code=303)
        if _login_blocked(request):
            return templates.TemplateResponse(
                request=request,
                name="private_login.html",
                context={
                    "error": "Too many failed sign-in attempts. Try again later.",
                    "next": _safe_next(next),
                },
                status_code=429,
            )
        account = _accounts().get(username.strip().casefold())
        success = bool(account and verify_password(password, account.password_hash))
        _record_login(request, success)
        if not success or not account:
            return templates.TemplateResponse(
                request=request,
                name="private_login.html",
                context={
                    "error": "Invalid username or password.",
                    "next": _safe_next(next),
                },
                status_code=401,
            )
        response = RedirectResponse(url=_safe_next(next), status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            _issue_session(account),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/logout")
    def private_logout():
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/whoami")
    def private_whoami(request: Request):
        account = getattr(request.state, "cmos_account", None)
        return {
            "username": account.username if account else None,
            "role": account.role if account else None,
        }

    @app.middleware("http")
    async def private_security_middleware(request: Request, call_next):
        path = request.url.path

        if not auth_enabled:
            response = await call_next(request)
            return response

        if path.startswith("/static/") or path in _PUBLIC_PATHS:
            response = await call_next(request)
        else:
            account: Account | None = None
            if _automation_authorized(request):
                account = Account("automation", "EXECUTIVE", "")
            else:
                account = _read_session(request.cookies.get(COOKIE_NAME))

            if not account:
                if request.method in {"GET", "HEAD"}:
                    target = path
                    if request.url.query:
                        target += "?" + request.url.query
                    return RedirectResponse(
                        url=f"/login?next={quote(target, safe='')}",
                        status_code=303,
                    )
                return PlainTextResponse("Authentication required.", status_code=401)

            if not _role_allows(account, request):
                return PlainTextResponse("Role does not allow this action.", status_code=403)

            if request.method not in {"GET", "HEAD", "OPTIONS"} and not _same_origin_ok(request):
                return PlainTextResponse("Invalid request origin.", status_code=403)

            request.state.cmos_account = account
            request.state.cmos_user = account.username
            request.state.cmos_role = account.role
            response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: blob: https://*.tile.openstreetmap.org; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'"
        )
        if secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response
