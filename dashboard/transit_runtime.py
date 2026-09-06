
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from integration_runtime import SafeRedirectHandler, validate_url


@dataclass
class TransitResult:
    ok: bool
    status_code: int | None
    final_url: str
    elapsed_ms: int
    headers: dict[str, str]
    body_text: str
    body_bytes: int
    truncated: bool
    content_type: str
    raw: bytes
    error: str | None = None


def _safe_headers(headers: Any) -> dict[str, str]:
    return {
        str(k): ("***REDACTED***" if str(k).lower() in {"authorization", "cookie", "set-cookie", "x-api-key", "api-key"} else str(v))
        for k, v in dict(headers or {}).items()
    }


def _request(
    *,
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 30,
    max_response_bytes: int = 25_000_000,
) -> TransitResult:
    validate_url(url, allow_private=False)
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method.upper())
    started = time.perf_counter()
    ctx = ssl.create_default_context()
    try:
        opener = urllib.request.build_opener(
            SafeRedirectHandler(False),
            urllib.request.HTTPSHandler(context=ctx),
        )
        with opener.open(req, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None)
            response_headers = dict(response.headers.items())
            raw = response.read(max_response_bytes + 1)
            actual_url = response.geturl()
            ok = bool(status is not None and 200 <= status < 400)
            error = None
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        raw = exc.read(max_response_bytes + 1)
        actual_url = exc.geturl()
        ok = False
        error = f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return TransitResult(
            ok=False,
            status_code=None,
            final_url=url,
            elapsed_ms=elapsed,
            headers={},
            body_text="",
            body_bytes=0,
            truncated=False,
            content_type="",
            raw=b"",
            error=str(exc),
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    truncated = len(raw) > max_response_bytes
    if truncated:
        raw = raw[:max_response_bytes]
    content_type = response_headers.get("Content-Type", "")
    charset = "utf-8"
    match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    if match:
        charset = match.group(1).strip("\"'")
    try:
        body_text = raw.decode(charset, errors="replace")
    except LookupError:
        body_text = raw.decode("utf-8", errors="replace")
    return TransitResult(
        ok=ok,
        status_code=status,
        final_url=actual_url,
        elapsed_ms=elapsed,
        headers=_safe_headers(response_headers),
        body_text=body_text,
        body_bytes=len(raw),
        truncated=truncated,
        content_type=content_type,
        raw=raw,
        error=error,
    )


def post_urlencoded(
    url: str,
    fields: dict[str, Any],
    *,
    timeout_seconds: int = 30,
    max_response_bytes: int = 25_000_000,
) -> TransitResult:
    payload = urllib.parse.urlencode({k: "" if v is None else str(v) for k, v in fields.items()}).encode()
    return _request(
        method="POST",
        url=url,
        body=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "*/*",
            "User-Agent": "CityManagerOS/1.0",
        },
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
    )


def post_multipart(
    url: str,
    fields: dict[str, Any],
    *,
    timeout_seconds: int = 30,
    max_response_bytes: int = 25_000_000,
) -> TransitResult:
    boundary = "----CityManagerOSTransit" + hashlib.sha256(os.urandom(24)).hexdigest()[:24]
    chunks: list[bytes] = []
    for key, value in fields.items():
        safe_key = str(key).replace('"', "")
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{safe_key}"\r\n\r\n'.encode())
        chunks.append(("" if value is None else str(value)).encode())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return _request(
        method="POST",
        url=url,
        body=b"".join(chunks),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "*/*",
            "User-Agent": "CityManagerOS/1.0",
        },
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
    )


def get_bytes(
    url: str,
    *,
    timeout_seconds: int = 60,
    max_response_bytes: int = 50_000_000,
) -> TransitResult:
    return _request(
        method="GET",
        url=url,
        headers={"Accept": "*/*", "User-Agent": "CityManagerOS/1.0"},
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
    )


def json_value(result: TransitResult) -> Any:
    if not result.body_text.strip():
        return None
    return json.loads(result.body_text)


def extract_token(result: TransitResult) -> str:
    text = result.body_text.strip()
    if not text:
        raise ValueError("NJ TRANSIT token response was empty")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = text

    if isinstance(payload, str):
        token = payload.strip().strip('"')
        if token:
            return token

    if isinstance(payload, dict):
        for key in ("UserToken", "userToken", "token", "Token", "access_token"):
            value = payload.get(key)
            if value:
                return str(value).strip()
        for value in payload.values():
            if isinstance(value, str) and len(value.strip()) >= 16:
                return value.strip()

    match = re.search(r'(?i)(?:UserToken|token)\s*["\']?\s*[:=]\s*["\']([^"\']+)', text)
    if match:
        return match.group(1).strip()

    raise ValueError("Could not identify token in NJ TRANSIT token response")


def _cache_dir() -> Path:
    path = Path(os.getenv("TRANSIT_TOKEN_CACHE_DIR", "/tmp/cmos-transit-tokens"))
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _cache_file(host: str, family: str, username: str) -> Path:
    key = hashlib.sha256(f"{host}|{family}|{username}".encode()).hexdigest()
    return _cache_dir() / f"{key}.json"


def invalidate_token(host: str, family: str, username: str) -> None:
    try:
        _cache_file(host, family, username).unlink(missing_ok=True)
    except OSError:
        pass


def _get_cached_token(
    *,
    host: str,
    cache_family: str,
    username: str,
    password: str,
    auth_urls: list[str],
    force_refresh: bool = False,
) -> str:
    cache_file = _cache_file(host, cache_family, username)
    now = time.time()

    if not force_refresh and cache_file.exists():
        try:
            payload = json.loads(cache_file.read_text())
            token = str(payload.get("token") or "")
            expires_at = float(payload.get("expires_at") or 0)
            if token and expires_at > now + 60:
                return token
        except Exception:
            pass

    ordered_urls: list[str] = []
    for url in auth_urls:
        clean = str(url or "").strip()
        if clean and clean not in ordered_urls:
            ordered_urls.append(clean)

    if not ordered_urls:
        raise ValueError("No NJ TRANSIT authentication endpoint configured")

    errors: list[str] = []
    for token_url in ordered_urls:
        result = post_multipart(
            token_url,
            {"username": username, "password": password},
            timeout_seconds=30,
            max_response_bytes=1_000_000,
        )
        if not result.ok:
            errors.append(
                f"{urllib.parse.urlsplit(token_url).path}: "
                f"{result.error or 'HTTP ' + str(result.status_code)}"
            )
            continue
        try:
            token = extract_token(result)
        except Exception as exc:
            errors.append(
                f"{urllib.parse.urlsplit(token_url).path}: token parse failed: {exc}"
            )
            continue

        payload = {
            "token": token,
            "expires_at": now + int(os.getenv("NJT_TOKEN_CACHE_SECONDS", "5400")),
        }
        cache_file.write_text(json.dumps(payload))
        try:
            cache_file.chmod(0o600)
        except OSError:
            pass
        return token

    raise RuntimeError(
        "NJ TRANSIT authentication failed across configured endpoints: "
        + " | ".join(errors)
    )


def get_cached_rail_token(
    *,
    host: str,
    family: str,
    username: str,
    password: str,
    force_refresh: bool = False,
) -> str:
    family = "GTFSRT" if family.upper() == "GTFSRT" else "TrainData"
    base = host.rstrip("/")
    auth_urls = [
        f"{base}/api/{family}/getToken",
        f"{base}/api/TrainData/getToken",
        f"{base}/api/getToken",
    ]
    return _get_cached_token(
        host=base,
        cache_family=family,
        username=username,
        password=password,
        auth_urls=auth_urls,
        force_refresh=force_refresh,
    )


def get_cached_bus_token(
    *,
    host: str,
    family: str,
    username: str,
    password: str,
    force_refresh: bool = False,
) -> str:
    family = family.upper()
    base = host.rstrip("/")

    if family == "GTFSG2":
        paths = [
            "/api/GTFSG2/authenticateUser",
            "/api/GTFS/authenticateUser",
            "/api/BUSDV2/authenticateUser",
        ]
    elif family == "GTFS":
        paths = [
            "/api/GTFS/authenticateUser",
            "/api/GTFSG2/authenticateUser",
            "/api/BUSDV2/authenticateUser",
        ]
    else:
        family = "BUSDV2"
        paths = [
            "/api/BUSDV2/authenticateUser",
            "/api/GTFSG2/authenticateUser",
            "/api/GTFS/authenticateUser",
        ]

    return _get_cached_token(
        host=base,
        cache_family=family,
        username=username,
        password=password,
        auth_urls=[base + path for path in paths],
        force_refresh=force_refresh,
    )
