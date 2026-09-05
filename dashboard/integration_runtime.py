from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.azure.internal",
    "instance-data",
}
SENSITIVE_HEADER_NAMES = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key"}


@dataclass
class FetchResult:
    ok: bool
    status_code: int | None
    final_url: str
    elapsed_ms: int
    headers: dict[str, str]
    body_text: str
    body_bytes: int
    truncated: bool
    content_type: str
    error: str | None = None


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def redact_text(value: str | None, secrets: list[str] | None = None) -> str:
    text = value or ""
    for secret in secrets or []:
        if secret:
            text = text.replace(secret, "***REDACTED***")
    return text


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        out[key] = "***REDACTED***" if key.lower() in SENSITIVE_HEADER_NAMES else value
    return out


def _resolved_ips(hostname: str) -> list[ipaddress._BaseAddress]:
    results: list[ipaddress._BaseAddress] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM):
        raw = sockaddr[0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if addr not in results:
            results.append(addr)
    return results


def validate_url(url: str, allow_private: bool | None = None) -> urllib.parse.SplitResult:
    allow_private = _bool_env("API_LAB_ALLOW_PRIVATE", False) if allow_private is None else allow_private
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only http:// and https:// URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in the URL are not allowed; use the authentication fields")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in BLOCKED_HOSTNAMES and not allow_private:
        raise ValueError("Local/metadata hostnames are blocked by API Lab safety rules")
    try:
        ips = _resolved_ips(hostname)
    except socket.gaierror as exc:
        raise ValueError(f"Hostname could not be resolved: {exc}") from exc
    if not ips:
        raise ValueError("Hostname did not resolve to an IP address")
    if not allow_private:
        for addr in ips:
            if (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_multicast
                or addr.is_reserved
                or addr.is_unspecified
            ):
                raise ValueError(f"Target resolves to blocked address {addr}")
    return parsed


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private: bool):
        super().__init__()
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        validate_url(newurl, self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _body_bytes(body: str | bytes | None, content_type: str | None) -> bytes | None:
    if body is None or body == "":
        return None
    if isinstance(body, bytes):
        return body
    if content_type and "application/json" in content_type.lower():
        try:
            parsed = json.loads(body)
            return json.dumps(parsed, separators=(",", ":")).encode("utf-8")
        except json.JSONDecodeError:
            pass
    return body.encode("utf-8")


def perform_http_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    body: str | bytes | None = None,
    timeout_seconds: int = 15,
    max_response_bytes: int = 1_000_000,
    allow_redirects: bool = True,
    verify_tls: bool = True,
    allow_private: bool | None = None,
) -> FetchResult:
    method = method.upper().strip()
    if method not in ALLOWED_METHODS:
        raise ValueError(f"Unsupported HTTP method: {method}")
    if timeout_seconds < 1 or timeout_seconds > 60:
        raise ValueError("Timeout must be between 1 and 60 seconds")
    if max_response_bytes < 1024 or max_response_bytes > 5_000_000:
        raise ValueError("Response limit must be between 1 KB and 5 MB")

    allow_private_value = _bool_env("API_LAB_ALLOW_PRIVATE", False) if allow_private is None else allow_private
    validate_url(url, allow_private_value)
    parsed = urllib.parse.urlsplit(url)
    current_query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in (query or {}).items():
        if value is None:
            continue
        if isinstance(value, list):
            current_query.extend((str(key), str(item)) for item in value)
        else:
            current_query.append((str(key), str(value)))
    final_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", urllib.parse.urlencode(current_query, doseq=True), parsed.fragment)
    )

    request_headers = {str(k): str(v) for k, v in (headers or {}).items() if str(k).strip()}
    content_type = next((v for k, v in request_headers.items() if k.lower() == "content-type"), None)
    payload = _body_bytes(body, content_type)
    request = urllib.request.Request(final_url, data=payload, headers=request_headers, method=method)

    context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()  # noqa: SLF001
    redirect_handler = SafeRedirectHandler(allow_private_value) if allow_redirects else NoRedirectHandler()
    opener = urllib.request.build_opener(redirect_handler, urllib.request.HTTPSHandler(context=context))
    started = time.perf_counter()

    try:
        response = opener.open(request, timeout=timeout_seconds)
        status = getattr(response, "status", None)
        response_headers = dict(response.headers.items())
        raw = response.read(max_response_bytes + 1)
        actual_url = response.geturl()
        response.close()
        ok = bool(status is not None and 200 <= status < 400)
        error = None
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        raw = exc.read(max_response_bytes + 1)
        actual_url = exc.geturl()
        ok = False
        error = f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:  # network errors should be shown in API Lab, not crash it
        elapsed = int((time.perf_counter() - started) * 1000)
        return FetchResult(
            ok=False,
            status_code=None,
            final_url=final_url,
            elapsed_ms=elapsed,
            headers={},
            body_text="",
            body_bytes=0,
            truncated=False,
            content_type="",
            error=str(exc),
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    truncated = len(raw) > max_response_bytes
    if truncated:
        raw = raw[:max_response_bytes]
    content_type_header = response_headers.get("Content-Type", "")
    charset = "utf-8"
    match = re.search(r"charset=([^;\s]+)", content_type_header, re.I)
    if match:
        charset = match.group(1).strip('"\'')
    try:
        body_text = raw.decode(charset, errors="replace")
    except LookupError:
        body_text = raw.decode("utf-8", errors="replace")

    return FetchResult(
        ok=ok,
        status_code=status,
        final_url=actual_url,
        elapsed_ms=elapsed,
        headers=redact_headers(response_headers),
        body_text=body_text,
        body_bytes=len(raw),
        truncated=truncated,
        content_type=content_type_header,
        error=error,
    )


def parse_json_object(text: str, field_name: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return value


def parse_header_lines(text: str) -> dict[str, str]:
    if not text.strip():
        return {}
    if text.lstrip().startswith("{"):
        return {str(k): str(v) for k, v in parse_json_object(text, "Headers").items()}
    headers: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"Invalid header line: {line}")
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def parse_query_lines(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    if text.lstrip().startswith("{"):
        return parse_json_object(text, "Query parameters")
    pairs = urllib.parse.parse_qsl(text.replace("\n", "&"), keep_blank_values=True)
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            existing = output[key]
            output[key] = existing + [value] if isinstance(existing, list) else [existing, value]
        else:
            output[key] = value
    return output


def build_request_body(body_mode: str, body_text: str) -> tuple[bytes | None, str | None]:
    mode = (body_mode or "RAW").upper().strip()
    if not body_text and mode != "MULTIPART":
        return None, None
    if mode == "RAW":
        return body_text.encode("utf-8") if body_text else None, None
    if mode == "JSON":
        value = json.loads(body_text or "{}")
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), "application/json"
    if mode == "FORM_URLENCODED":
        fields = parse_query_lines(body_text)
        return urllib.parse.urlencode(fields, doseq=True).encode("utf-8"), "application/x-www-form-urlencoded"
    if mode == "MULTIPART":
        fields = parse_query_lines(body_text)
        boundary = "----CityManagerOS" + hashlib.sha256(os.urandom(24)).hexdigest()[:24]
        chunks: list[bytes] = []
        for key, raw_value in fields.items():
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            for value in values:
                chunks.append(f"--{boundary}\r\n".encode())
                safe_key = str(key).replace('"', '')
                chunks.append(f'Content-Disposition: form-data; name="{safe_key}"\r\n\r\n'.encode())
                chunks.append(str(value).encode("utf-8"))
                chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
    raise ValueError(f"Unsupported body mode: {body_mode}")


def apply_literal_auth(
    auth_type: str,
    headers: dict[str, str],
    query: dict[str, Any],
    *,
    username: str = "",
    password: str = "",
    token: str = "",
    api_key_name: str = "",
    api_key_value: str = "",
) -> list[str]:
    auth_type = (auth_type or "NONE").upper()
    secrets = [value for value in (password, token, api_key_value) if value]
    if auth_type == "NONE":
        return secrets
    if auth_type == "BEARER":
        if not token:
            raise ValueError("Bearer token is required")
        headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "BASIC":
        if not username:
            raise ValueError("Username is required for Basic auth")
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
        secrets.append(encoded)
    elif auth_type == "API_KEY_HEADER":
        if not api_key_name or not api_key_value:
            raise ValueError("API key header name and value are required")
        headers[api_key_name] = api_key_value
    elif auth_type == "API_KEY_QUERY":
        if not api_key_name or not api_key_value:
            raise ValueError("API key query name and value are required")
        query[api_key_name] = api_key_value
    else:
        raise ValueError(f"Unsupported auth type: {auth_type}")
    return secrets


def integration_auth_from_env(integration: dict[str, Any], headers: dict[str, str], query: dict[str, Any]) -> list[str]:
    auth_type = str(integration.get("auth_type") or "NONE").upper()
    auth_config = integration.get("auth_config") or {}
    if isinstance(auth_config, str):
        auth_config = json.loads(auth_config)
    env = lambda key: os.getenv(str(auth_config.get(key) or ""), "")  # noqa: E731
    if auth_type == "NONE":
        return []
    if auth_type == "BEARER_ENV":
        token = env("token_env")
        if not token:
            raise ValueError(f"Missing environment variable {auth_config.get('token_env')}")
        headers["Authorization"] = f"Bearer {token}"
        return [token]
    if auth_type == "BASIC_ENV":
        username = env("username_env")
        password = env("password_env")
        if not username or not password:
            raise ValueError("Configured Basic auth environment variables are missing")
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
        return [password, encoded]
    if auth_type in {"API_KEY_HEADER_ENV", "API_KEY_QUERY_ENV"}:
        key_value = env("key_env")
        key_name = str(auth_config.get("key_name") or "")
        if not key_value or not key_name:
            raise ValueError("Configured API key environment variable/name is missing")
        if auth_type == "API_KEY_HEADER_ENV":
            headers[key_name] = key_value
        else:
            query[key_name] = key_value
        return [key_value]
    raise ValueError(f"Unsupported permanent integration auth type: {auth_type}")


def get_path(value: Any, path: str | None) -> Any:
    if not path:
        return value
    current = value
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
    return current


def parse_datetime(value: Any, default_timezone: str = "UTC") -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo:
            return parsed
        try:
            return parsed.replace(tzinfo=ZoneInfo(default_timezone))
        except Exception:
            return parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            try:
                return parsed.replace(tzinfo=ZoneInfo(default_timezone))
            except Exception:
                return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    match = re.search(r"\d[\d,]*", str(value))
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


def normalized_fingerprint(title: str, start: datetime | None, location: str | None) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", " ", f"{title} {location or ''}".upper()).strip()
    day = start.astimezone(timezone.utc).strftime("%Y-%m-%d") if start else "UNKNOWN"
    return hashlib.sha256(f"{normalized}|{day}".encode()).hexdigest()


def score_event(event: dict[str, Any]) -> tuple[int, str, str]:
    score = 0
    reasons: list[str] = []
    municipality = str(event.get("municipality") or "").upper()
    county = str(event.get("county") or "").upper()
    state = str(event.get("state") or "").upper()
    blob = " ".join(
        str(event.get(key) or "")
        for key in ("title", "description", "event_type", "venue", "address", "municipality", "road_impact", "transit_impact")
    ).upper()

    if "WEEHAWKEN" in municipality or "WEEHAWKEN" in blob:
        score += 80
        reasons.append("Weehawken")
    elif municipality in {"UNION CITY", "HOBOKEN", "WEST NEW YORK", "NORTH BERGEN", "GUTTENBERG", "JERSEY CITY", "SECAUCUS"}:
        score += 48
        reasons.append("immediate Hudson neighbor")
    elif county == "HUDSON" or "HUDSON COUNTY" in blob:
        score += 38
        reasons.append("Hudson County")
    elif municipality in {"NEW YORK", "NEW YORK CITY", "MANHATTAN"} or "MANHATTAN" in blob or "NEW YORK CITY" in blob:
        score += 28
        reasons.append("NYC")
    elif state in {"NJ", "NEW JERSEY"}:
        score += 10
        reasons.append("New Jersey")

    keyword_weights = {
        "LINCOLN TUNNEL": 28,
        "ROUTE 495": 25,
        "NJ-495": 25,
        "PORT AUTHORITY BUS TERMINAL": 24,
        "PABT": 24,
        "FERRY": 12,
        "PATH": 12,
        "NJ TRANSIT": 12,
        "ROAD CLOSURE": 16,
        "STREET CLOSURE": 16,
        "PARADE": 10,
        "MARATHON": 12,
        "DEMONSTRATION": 12,
        "PROTEST": 12,
        "FIREWORKS": 8,
        "METLIFE": 40,
        "METLIFE STADIUM": 40,
        "JAVITS": 18,
        "MADISON SQUARE GARDEN": 20,
        "YANKEE STADIUM": 18,
        "CITI FIELD": 16,
        "BARCLAYS CENTER": 10,
    }
    for keyword, weight in keyword_weights.items():
        if keyword in blob:
            score += weight
            reasons.append(keyword.title())

    attendance = _int(event.get("attendance_estimate"))
    if attendance:
        if attendance >= 50_000:
            score += 24
            reasons.append("50k+ attendance")
        elif attendance >= 10_000:
            score += 15
            reasons.append("10k+ attendance")
        elif attendance >= 2_000:
            score += 8
            reasons.append("2k+ attendance")

    score = min(score, 100)
    level = "ALERT" if score >= 75 else "WATCH" if score >= 45 else "AWARENESS"
    summary = ", ".join(dict.fromkeys(reasons)) if reasons else "Regional awareness"
    return score, level, summary


def _event_from_mapping(item: dict[str, Any], mapping: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    def mapped(name: str) -> Any:
        path = mapping.get(name)
        return get_path(item, path) if path else defaults.get(name)

    event = {
        "external_key": _text(mapped("id")),
        "title": _text(mapped("title")) or "Untitled event",
        "description": _text(mapped("description")),
        "event_type": _text(mapped("event_type")) or _text(defaults.get("event_type")) or "EVENT",
        "venue": _text(mapped("venue")),
        "address": _text(mapped("address")),
        "municipality": _text(mapped("municipality")) or _text(defaults.get("municipality")),
        "county": _text(mapped("county")) or _text(defaults.get("county")),
        "state": _text(mapped("state")) or _text(defaults.get("state")),
        "starts_at": parse_datetime(mapped("start"), str(defaults.get("default_timezone") or "UTC")),
        "ends_at": parse_datetime(mapped("end"), str(defaults.get("default_timezone") or "UTC")),
        "source_url": _text(mapped("url")),
        "attendance_estimate": _int(mapped("attendance")),
        "latitude": mapped("latitude"),
        "longitude": mapped("longitude"),
        "status": (_text(mapped("status")) or "SCHEDULED").upper(),
        "road_impact": _text(mapped("road_impact")),
        "transit_impact": _text(mapped("transit_impact")),
        "metadata": {"source_item": item} if defaults.get("retain_source_item") else {},
    }
    score, level, summary = score_event(event)
    event["impact_score"] = score
    event["impact_level"] = level
    event["impact_summary"] = summary
    event["fingerprint"] = normalized_fingerprint(event["title"], event["starts_at"], event.get("venue") or event.get("municipality"))
    return event


def parse_json_events(body_text: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    root = json.loads(body_text)
    items = get_path(root, config.get("list_path")) if config.get("list_path") else root
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise ValueError("JSON event parser list_path did not resolve to a list")
    mapping = config.get("mapping") or {}
    defaults = config.get("defaults") or {}
    return [_event_from_mapping(item, mapping, defaults) for item in items if isinstance(item, dict)]


def _xml_local(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


def _xml_child_text(element: ET.Element, names: set[str]) -> str | None:
    for child in list(element):
        if _xml_local(child.tag) in names:
            if child.text and child.text.strip():
                return child.text.strip()
            href = child.attrib.get("href")
            if href:
                return href
    return None


def parse_rss_events(body_text: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    root = ET.fromstring(body_text)
    entries = [el for el in root.iter() if _xml_local(el.tag) in {"item", "entry"}]
    defaults = config.get("defaults") or {}
    out = []
    for entry in entries:
        item = {
            "id": _xml_child_text(entry, {"guid", "id", "link"}),
            "title": _xml_child_text(entry, {"title"}),
            "description": _xml_child_text(entry, {"description", "summary", "content"}),
            "start": _xml_child_text(entry, {"pubdate", "published", "updated", "start", "dtstart"}),
            "url": _xml_child_text(entry, {"link"}),
        }
        mapping = {
            "id": "id",
            "title": "title",
            "description": "description",
            "start": "start",
            "url": "url",
        }
        out.append(_event_from_mapping(item, mapping, defaults))
    return out


def _unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _ics_value(raw: str) -> str:
    return raw.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_ics_events(body_text: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    defaults = config.get("defaults") or {}
    events: list[dict[str, Any]] = []
    current: dict[str, str] | None = None
    for line in _unfold_ics(body_text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                item = {
                    "id": current.get("UID"),
                    "title": current.get("SUMMARY"),
                    "description": current.get("DESCRIPTION"),
                    "start": current.get("DTSTART"),
                    "end": current.get("DTEND"),
                    "venue": current.get("LOCATION"),
                    "url": current.get("URL"),
                    "status": current.get("STATUS"),
                }
                mapping = {k: k for k in item}
                events.append(_event_from_mapping(item, mapping, defaults))
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key_part, value = line.split(":", 1)
        key = key_part.split(";", 1)[0].upper()
        current[key] = _ics_value(value)
    return events


def parse_events(body_text: str, parser_kind: str, parser_config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    parser_kind = (parser_kind or "NONE").upper()
    config = parser_config or {}
    if parser_kind == "JSON_EVENTS":
        return parse_json_events(body_text, config)
    if parser_kind in {"RSS_EVENTS", "ATOM_EVENTS"}:
        return parse_rss_events(body_text, config)
    if parser_kind == "ICS_EVENTS":
        return parse_ics_events(body_text, config)
    if parser_kind == "NONE":
        return []
    raise ValueError(f"Unsupported parser kind: {parser_kind}")
