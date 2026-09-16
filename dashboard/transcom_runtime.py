from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from integration_runtime import FetchResult, perform_http_request


DEFAULT_BASE_URL = "https://de.infosensedigital.com"
TOKEN_PATH = "/ISGDE/api/token/v4/get"


def _pick(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return None


def _cache_dir() -> Path:
    path = Path(
        os.getenv("TRANSCOM_TOKEN_CACHE_DIR")
        or os.getenv("TRANSIT_TOKEN_CACHE_DIR")
        or "/tmp/cmos-transcom-tokens"
    )
    path.mkdir(parents=True, exist_ok=True)

    try:
        path.chmod(0o700)
    except OSError:
        pass

    return path


def _cache_file(base_url: str, username: str) -> Path:
    key = hashlib.sha256(
        f"{base_url.rstrip('/')}|{username}".encode()
    ).hexdigest()

    return _cache_dir() / f"transcom-{key}.json"


def invalidate_transcom_token(
    base_url: str,
    username: str,
) -> None:
    try:
        _cache_file(base_url, username).unlink(missing_ok=True)
    except OSError:
        pass


def _parse_token_response(
    result: FetchResult,
) -> tuple[str, float]:

    text = result.body_text.strip()

    if not text:
        raise ValueError(
            "TRANSCOM token response was empty"
        )

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "TRANSCOM token response was not valid JSON"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            "TRANSCOM token response was not a JSON object"
        )

    # Support both documented camelCase responses and
    # the capitalized Response/Entity schema supplied by TRANSCOM.
    response = payload.get("Response")

    root = (
        response
        if isinstance(response, dict)
        else payload
    )

    status_raw = _pick(
        root,
        "statusCode",
        "StatusCode",
    )

    try:
        status_code = int(status_raw)
    except (TypeError, ValueError):
        status_code = int(
            result.status_code or 0
        )

    if status_code != 200:
        message = _pick(
            root,
            "message",
            "Message",
            "error",
            "Error",
        ) or f"status {status_code}"

        raise ValueError(
            f"TRANSCOM token request failed: {message}"
        )

    entity = _pick(
        root,
        "entity",
        "Entity",
    )

    if not isinstance(entity, dict):
        raise ValueError(
            "TRANSCOM token response did not include entity"
        )

    token = str(
        _pick(
            entity,
            "token",
            "Token",
        )
        or ""
    ).strip()

    if not token:
        raise ValueError(
            "TRANSCOM token response did not include token"
        )

    expire_raw = _pick(
        entity,
        "expireAt",
        "ExpireAt",
    )

    try:
        expire_value = float(expire_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "TRANSCOM token response did not include a valid expiry"
        ) from exc

    # TRANSCOM examples use epoch milliseconds.
    expires_at = (
        expire_value / 1000.0
        if expire_value > 10_000_000_000
        else expire_value
    )

    if expires_at <= time.time():
        raise ValueError(
            "TRANSCOM returned an already-expired token"
        )

    return token, expires_at


def get_cached_transcom_token(
    *,
    username: str,
    password: str,
    base_url: str | None = None,
    force_refresh: bool = False,
) -> str:

    username = str(
        username or ""
    ).strip()

    password = str(
        password or ""
    )

    base = str(
        base_url
        or os.getenv("TRANSCOM_BASE_URL")
        or DEFAULT_BASE_URL
    ).strip().rstrip("/")

    if not username or not password:
        raise ValueError(
            "TRANSCOM username/password are required"
        )

    if not base:
        raise ValueError(
            "TRANSCOM base URL is required"
        )

    cache_file = _cache_file(
        base,
        username,
    )

    now = time.time()

    if (
        not force_refresh
        and cache_file.exists()
    ):
        try:
            cached = json.loads(
                cache_file.read_text()
            )

            token = str(
                cached.get("token") or ""
            )

            expires_at = float(
                cached.get("expires_at") or 0
            )

            # Refresh before expiration.
            if (
                token
                and expires_at > now + 60
            ):
                return token

        except Exception:
            pass

    body = json.dumps(
        {
            "username": username,
            "password": password,
        },
        separators=(",", ":"),
    )

    result = perform_http_request(
        method="POST",
        url=f"{base}{TOKEN_PATH}",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        body=body,
        timeout_seconds=30,
        max_response_bytes=1_000_000,
        allow_redirects=True,
        verify_tls=True,
        allow_private=False,
    )

    if not result.ok:
        raise RuntimeError(
            result.error
            or (
                "TRANSCOM token request failed "
                f"with HTTP {result.status_code}"
            )
        )

    token, expires_at = _parse_token_response(
        result
    )

    cache_file.write_text(
        json.dumps(
            {
                "token": token,
                "expires_at": expires_at,
            }
        )
    )

    try:
        cache_file.chmod(0o600)
    except OSError:
        pass

    return token
