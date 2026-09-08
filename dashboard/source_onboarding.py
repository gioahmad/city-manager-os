from __future__ import annotations

import json
import os
import re
import urllib.parse
import uuid
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db_conn, query_all, query_one, templates
from integration_engine import load_integration, run_integration
from integration_runtime import parse_events, parse_json_object, redact_headers, redact_text
from schedule_app import app
from transit_engine import TRANSIT_ADAPTERS, is_transit_adapter, run_transit_integration


AUTH_TYPES = ["NONE", "BEARER_ENV", "BASIC_ENV", "API_KEY_HEADER_ENV", "API_KEY_QUERY_ENV"]
PARSER_KINDS = ["NONE", "JSON_EVENTS", "RSS_EVENTS", "ATOM_EVENTS", "ICS_EVENTS"]
CATEGORIES = ["GENERIC", "EVENTS", "TRANSIT", "TRAFFIC", "WEATHER", "UTILITY", "PUBLIC_SAFETY", "GOVERNMENT"]
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
ADAPTER_TYPES = ["HTTP"] + sorted(TRANSIT_ADAPTERS)

SOURCE_TEMPLATES: dict[str, dict[str, Any]] = {
    "CUSTOM": dict(label="Custom / Placeholder", description="Create the shell now and complete endpoint, credentials and parser later.", category="GENERIC", adapter_type="HTTP", method="GET", parser_kind="NONE", headers={}, query={}, parser_config={}, poll_seconds=900, map_capable=False),
    "GENERIC_JSON": dict(label="Generic JSON API", description="HTTP JSON source using configurable list path and field mapping.", category="GENERIC", adapter_type="HTTP", method="GET", parser_kind="JSON_EVENTS", headers={"Accept": "application/json"}, query={}, parser_config={"list_path": "", "mapping": {"id": "id", "title": "title", "description": "description", "start": "start", "end": "end", "url": "url", "municipality": "municipality", "latitude": "latitude", "longitude": "longitude"}, "defaults": {"default_timezone": "America/New_York"}}, poll_seconds=900, map_capable=False),
    "GEOJSON": dict(label="GeoJSON", description="GeoJSON FeatureCollection using feature properties and point geometry.", category="GENERIC", adapter_type="HTTP", method="GET", parser_kind="JSON_EVENTS", headers={"Accept": "application/geo+json, application/json"}, query={}, parser_config={"list_path": "features", "mapping": {"id": "id", "title": "properties.title", "description": "properties.description", "start": "properties.start", "end": "properties.end", "url": "properties.url", "municipality": "properties.municipality", "latitude": "geometry.coordinates.1", "longitude": "geometry.coordinates.0"}, "defaults": {"default_timezone": "America/New_York"}}, poll_seconds=900, map_capable=True),
    "RSS": dict(label="RSS Feed", description="RSS items normalized through the existing event parser.", category="EVENTS", adapter_type="HTTP", method="GET", parser_kind="RSS_EVENTS", headers={"Accept": "application/rss+xml, application/xml, text/xml"}, query={}, parser_config={"defaults": {"default_timezone": "America/New_York"}}, poll_seconds=900, map_capable=False),
    "ATOM": dict(label="Atom Feed", description="Atom entries normalized through the existing event parser.", category="EVENTS", adapter_type="HTTP", method="GET", parser_kind="ATOM_EVENTS", headers={"Accept": "application/atom+xml, application/xml, text/xml"}, query={}, parser_config={"defaults": {"default_timezone": "America/New_York"}}, poll_seconds=900, map_capable=False),
    "ICS": dict(label="ICS Calendar", description="Public iCalendar feed normalized into regional event intelligence.", category="EVENTS", adapter_type="HTTP", method="GET", parser_kind="ICS_EVENTS", headers={"Accept": "text/calendar, text/plain"}, query={}, parser_config={"defaults": {"event_type": "EVENT", "default_timezone": "America/New_York"}}, poll_seconds=900, map_capable=False),
    "SOCRATA": dict(label="Socrata Open Data", description="Socrata JSON endpoint with editable SoQL query parameters and field mapping.", category="GOVERNMENT", adapter_type="HTTP", method="GET", parser_kind="JSON_EVENTS", headers={"Accept": "application/json"}, query={"$limit": 1000}, parser_config={"list_path": "", "mapping": {"id": "id", "title": "title", "description": "description", "start": "start", "end": "end", "url": "url", "municipality": "municipality"}, "defaults": {"default_timezone": "America/New_York"}}, poll_seconds=1800, map_capable=False),
    "ARCGIS": dict(label="ArcGIS REST / FeatureServer", description="ArcGIS FeatureServer query shell. Edit attribute mappings for the selected layer.", category="GOVERNMENT", adapter_type="HTTP", method="GET", parser_kind="JSON_EVENTS", headers={"Accept": "application/json"}, query={"f": "json", "where": "1=1", "outFields": "*", "returnGeometry": "true"}, parser_config={"list_path": "features", "mapping": {"id": "attributes.OBJECTID", "title": "attributes.NAME", "description": "attributes.DESCRIPTION", "latitude": "geometry.y", "longitude": "geometry.x"}, "defaults": {"default_timezone": "America/New_York"}}, poll_seconds=1800, map_capable=True),
    "GTFS_STATIC": dict(label="GTFS Static", description="Public GTFS ZIP using the provider-neutral TRANSIT_GTFS_URL adapter.", category="TRANSIT", adapter_type="TRANSIT_GTFS_URL", method="GET", parser_kind="NONE", headers={}, query={}, parser_config={"provider_key": ""}, poll_seconds=43200, map_capable=True),
    "GTFS_RT": dict(label="GTFS-RT Setup Shell", description="Placeholder for GTFS-Realtime. Keep paused until a supported adapter is selected.", category="TRANSIT", adapter_type="HTTP", method="GET", parser_kind="NONE", headers={"Accept": "application/x-protobuf"}, query={}, parser_config={}, poll_seconds=300, map_capable=True),
    "CSV": dict(label="CSV Setup Shell", description="Connection/configuration shell. Generic CSV normalization is not enabled yet.", category="GENERIC", adapter_type="HTTP", method="GET", parser_kind="NONE", headers={"Accept": "text/csv, text/plain"}, query={}, parser_config={}, poll_seconds=3600, map_capable=False),
    "XML": dict(label="XML Setup Shell", description="Connection/configuration shell. Select RSS/Atom or a specialized adapter before activation.", category="GENERIC", adapter_type="HTTP", method="GET", parser_kind="NONE", headers={"Accept": "application/xml, text/xml"}, query={}, parser_config={}, poll_seconds=3600, map_capable=False),
}


def _json_obj(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    return dict(value)


def _json_text(value: Any) -> str:
    return json.dumps(_json_obj(value), indent=2, sort_keys=True)


def _split_keywords(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else re.split(r"[\n,]+", str(value or ""))
    output: list[str] = []
    seen: set[str] = set()
    for item in raw:
        cleaned = str(item).strip()
        folded = cleaned.casefold()
        if cleaned and folded not in seen:
            output.append(cleaned)
            seen.add(folded)
    return output[:100]


def _integration_key(name: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:44] or "SOURCE"
    return f"{slug}_{uuid.uuid4().hex[:6].upper()}"


def integration_env_state(integration: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for field, env_name in _json_obj(integration.get("auth_config")).items():
        if str(field).endswith("_env") and env_name:
            rows.append({"field": str(field), "env": str(env_name), "set": bool(os.getenv(str(env_name)))})
    return rows


def _secret_values(integration: dict[str, Any]) -> list[str]:
    return [os.getenv(row["env"], "") for row in integration_env_state(integration) if row["set"]]


def connection_setup_issues(integration: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not str(integration.get("endpoint_url") or "").strip():
        issues.append("Endpoint URL is not configured")
    method = str(integration.get("method") or "GET").upper()
    if method not in METHODS:
        issues.append(f"Unsupported HTTP method: {method}")
    adapter = str(integration.get("adapter_type") or "HTTP").upper()
    if adapter != "HTTP" and adapter not in TRANSIT_ADAPTERS:
        issues.append(f"Unsupported adapter: {adapter}")

    auth_type = str(integration.get("auth_type") or "NONE").upper()
    auth = _json_obj(integration.get("auth_config"))
    required: list[tuple[str, str]] = []
    if auth_type == "BEARER_ENV":
        required = [("token_env", "Bearer token environment reference")]
    elif auth_type == "BASIC_ENV":
        required = [("username_env", "Basic-auth username environment reference"), ("password_env", "Basic-auth password environment reference")]
    elif auth_type in {"API_KEY_HEADER_ENV", "API_KEY_QUERY_ENV"}:
        required = [("key_env", "API-key environment reference")]
        if not str(auth.get("key_name") or "").strip():
            issues.append("API-key parameter/header name is not configured")
    elif auth_type != "NONE":
        issues.append(f"Unsupported auth type: {auth_type}")
    for field, label in required:
        if not str(auth.get(field) or "").strip():
            issues.append(label + " is not configured")
    for row in integration_env_state(integration):
        if not row["set"]:
            issues.append(f"Environment secret {row['env']} is missing")
    return list(dict.fromkeys(issues))


def collection_setup_issues(integration: dict[str, Any]) -> list[str]:
    issues = connection_setup_issues(integration)
    adapter = str(integration.get("adapter_type") or "HTTP").upper()
    parser = str(integration.get("parser_kind") or "NONE").upper()
    if adapter == "HTTP" and parser == "NONE":
        issues.append("Select a supported parser or specialized adapter before activation")
    elif adapter == "HTTP" and parser not in PARSER_KINDS:
        issues.append(f"Unsupported parser: {parser}")
    if adapter == "TRANSIT_GTFS_URL" and not str(_json_obj(integration.get("parser_config")).get("provider_key") or "").strip():
        issues.append("GTFS static adapter requires parser_config.provider_key")
    return list(dict.fromkeys(issues))


def activation_state(integration: dict[str, Any]) -> dict[str, Any]:
    issues = collection_setup_issues(integration)
    env_state = integration_env_state(integration)
    try:
        config_version = int(integration.get("config_version") or 1)
    except (TypeError, ValueError):
        config_version = 1
    try:
        tested_version = int(integration.get("last_test_config_version"))
    except (TypeError, ValueError):
        tested_version = -1
    tested_current = bool(integration.get("last_test_ok")) and tested_version == config_version
    if issues:
        detail = issues[0]
    elif not tested_current:
        detail = "A successful TEST of the current configuration is required"
    else:
        detail = "Configuration and current TEST are ready for activation"
    return {
        "setup_issues": issues,
        "env_state": env_state,
        "missing_secrets": [x["env"] for x in env_state if not x["set"]],
        "tested_current": tested_current,
        "can_activate": not issues and tested_current,
        "activation_detail": detail,
        "config_version": config_version,
        "tested_version": tested_version,
    }


def _preview_events(events: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    fields = ("external_key", "title", "event_type", "venue", "address", "municipality", "county", "state", "starts_at", "ends_at", "status", "source_url", "attendance_estimate", "road_impact", "transit_impact", "impact_score", "impact_level", "impact_summary", "latitude", "longitude")
    output = []
    for event in events[:limit]:
        row = {}
        for field in fields:
            value = event.get(field)
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            if value is not None and value != "":
                row[field] = value
        output.append(row)
    return output


def record_test_result(integration: dict[str, Any], outcome: dict[str, Any]) -> bool:
    result = outcome.get("result")
    items = outcome.get("items")
    if items is None:
        items = len(outcome.get("events") or [])
    summary = {
        "http_status": getattr(result, "status_code", None),
        "content_type": getattr(result, "content_type", None),
        "response_bytes": getattr(result, "body_bytes", None),
        "items_found": int(items or 0),
        "error": outcome.get("error"),
    }
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE integrations
               SET last_test_at=now(),last_test_ok=%s,last_test_config_version=config_version,
                   last_test_summary=%s::jsonb,updated_at=now()
               WHERE id=%s AND config_version=%s""",
            (bool(outcome.get("ok")), json.dumps(summary, default=str), integration["id"], int(integration.get("config_version") or 1)),
        )
        updated = cur.rowcount == 1
        conn.commit()
    return updated


def build_test_result(integration: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    result = outcome.get("result")
    secrets = _secret_values(integration)
    safe_headers: dict[str, str] = {}
    body_preview = ""
    if result:
        content_type = str(getattr(result, "content_type", "") or "")
        raw_body = str(getattr(result, "body_text", "") or "")
        body_preview = "[binary response omitted from browser preview]" if any(x in content_type.lower() for x in ("zip", "protobuf", "octet-stream")) else redact_text(raw_body[:12000], secrets)
        safe_headers = {str(k): redact_text(str(v), secrets) for k, v in redact_headers(dict(getattr(result, "headers", {}) or {})).items()}
    events = outcome.get("events") or []
    items = outcome.get("items")
    if items is None:
        items = len(events)
    health = query_one("SELECT status,last_attempt_at,last_success_at,last_error FROM source_health WHERE source_id=%s", (f"INT:{integration['integration_key']}",)) or {}
    return {
        "name": integration["name"], "ok": bool(outcome.get("ok")),
        "error": redact_text(str(outcome.get("error") or ""), secrets) or None,
        "status_code": getattr(result, "status_code", None) if result else None,
        "elapsed_ms": getattr(result, "elapsed_ms", None) if result else None,
        "content_type": getattr(result, "content_type", None) if result else None,
        "response_bytes": getattr(result, "body_bytes", None) if result else None,
        "body_preview": body_preview, "headers": safe_headers,
        "truncated": bool(getattr(result, "truncated", False)) if result else False,
        "parser_kind": integration.get("parser_kind") or "NONE",
        "items_found": int(items or 0), "normalized_preview": _preview_events(events),
        "source_health": health,
    }


def _record_activation(integration_id: Any, action: str, actor: str, reason: str | None = None) -> None:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO integration_activation_audit(integration_id,action,actor,reason,config_version)
               SELECT id,%s,%s,%s,config_version FROM integrations WHERE id=%s""",
            (action, actor or "unknown", reason, integration_id),
        )
        conn.commit()


def _template_form(template_key: str) -> dict[str, Any]:
    key = template_key if template_key in SOURCE_TEMPLATES else "CUSTOM"
    t = SOURCE_TEMPLATES[key]
    return {
        "provider_template": key, "template_label": t["label"], "template_description": t["description"],
        "category": t["category"], "adapter_type": t["adapter_type"], "endpoint_url": "", "method": t["method"],
        "headers_text": json.dumps(t["headers"], indent=2, sort_keys=True),
        "query_text": json.dumps(t["query"], indent=2, sort_keys=True),
        "request_body": "", "parser_kind": t["parser_kind"],
        "parser_config_text": json.dumps(t["parser_config"], indent=2, sort_keys=True),
        "poll_seconds": t["poll_seconds"], "timeout_seconds": 15, "max_response_bytes": 1_000_000,
        "allow_redirects": True, "verify_tls": True, "map_capable": bool(t.get("map_capable")),
    }


def _int_form(form: Any, name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(form.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _payload_from_form(form: Any, *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Source name is required")
    method = str(form.get("method") or "GET").upper()
    auth_type = str(form.get("auth_type") or "NONE").upper()
    parser = str(form.get("parser_kind") or "NONE").upper()
    adapter = str(form.get("adapter_type") or "HTTP").upper()
    if method not in METHODS or auth_type not in AUTH_TYPES or parser not in PARSER_KINDS:
        raise HTTPException(400, "Invalid method, auth type or parser")
    if adapter != "HTTP" and adapter not in TRANSIT_ADAPTERS:
        raise HTTPException(400, "Invalid adapter")
    try:
        headers = parse_json_object(str(form.get("headers_json") or "{}"), "Headers")
        query = parse_json_object(str(form.get("query_json") or "{}"), "Query")
        parser_config = parse_json_object(str(form.get("parser_config_json") or "{}"), "Parser config")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    watch = _int_form(form, "watch_threshold", 45, 0, 100)
    alert = _int_form(form, "alert_threshold", 75, 0, 100)
    if watch > alert:
        raise HTTPException(400, "WATCH threshold cannot exceed ALERT threshold")
    keywords = _split_keywords(form.get("relevance_keywords"))
    auth = {
        k: str(form.get(k) or "").strip()
        for k in ("username_env", "password_env", "token_env", "key_env", "key_name")
        if str(form.get(k) or "").strip()
    }
    map_config = {
        "enabled": "map_capable" in form,
        "layer_key": str(form.get("map_layer_key") or "").strip() or None,
        "layer_label": str(form.get("map_layer_label") or "").strip() or None,
    }
    return {
        "integration_key": existing["integration_key"] if existing else (str(form.get("integration_key") or "").strip().upper() or _integration_key(name)),
        "provider_template": str(form.get("provider_template") or "CUSTOM") if str(form.get("provider_template") or "CUSTOM") in SOURCE_TEMPLATES else "CUSTOM",
        "name": name, "category": str(form.get("category") or "GENERIC").upper(), "adapter_type": adapter,
        "endpoint_url": str(form.get("endpoint_url") or "").strip(), "method": method, "auth_type": auth_type,
        "auth_config": auth, "headers": headers, "query": query, "request_body": str(form.get("request_body") or "") or None,
        "parser_kind": parser, "parser_config": parser_config,
        "poll_seconds": _int_form(form, "poll_seconds", 900, 60, 86400),
        "timeout_seconds": _int_form(form, "timeout_seconds", 15, 1, 60),
        "max_response_bytes": _int_form(form, "max_response_bytes", 1_000_000, 1024, 5_000_000),
        "allow_redirects": "allow_redirects" in form, "verify_tls": "verify_tls" in form,
        "source_owner": str(form.get("source_owner") or "").strip() or None,
        "source_contact": str(form.get("source_contact") or "").strip() or None,
        "access_instructions": str(form.get("access_instructions") or "").strip() or None,
        "geography_scope": str(form.get("geography_scope") or "").strip() or None,
        "relevance_keywords": ", ".join(keywords) or None,
        "attention_config": {"watch_threshold": watch, "alert_threshold": alert, "relevance_keywords": keywords},
        "map_config": {k: v for k, v in map_config.items() if v is not None},
        "notes": str(form.get("notes") or "").strip() or None,
    }


def _source_rows(q: str = "") -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if q.strip():
        needle = f"%{q.strip()}%"
        where = "WHERE i.name ILIKE %s OR i.integration_key ILIKE %s OR i.category ILIKE %s OR COALESCE(i.endpoint_url,'') ILIKE %s"
        params = [needle] * 4
    rows = query_all(
        f"""SELECT i.*,lr.status AS last_run_status,lr.http_status AS last_http_status,
                   lr.started_at AS last_run_at,lr.error_message AS last_error,
                   sh.status AS health_status,sh.last_success_at,sh.last_event_at
            FROM integrations i
            LEFT JOIN LATERAL (
              SELECT status,http_status,started_at,error_message FROM integration_runs r
              WHERE r.integration_id=i.id ORDER BY r.started_at DESC LIMIT 1
            ) lr ON true
            LEFT JOIN source_health sh ON sh.source_id='INT:' || i.integration_key
            {where}
            ORDER BY i.active DESC,i.category,i.name LIMIT 400""",
        params,
    )
    for row in rows:
        for field in ("auth_config", "request_headers", "request_query", "parser_config", "attention_config", "map_config"):
            row[field] = _json_obj(row.get(field))
        row["headers_text"], row["query_text"], row["parser_config_text"] = _json_text(row["request_headers"]), _json_text(row["request_query"]), _json_text(row["parser_config"])
        row.update(activation_state(row))
        if row["setup_issues"]:
            row["onboarding_state"], row["onboarding_detail"] = "NEEDS SETUP", row["setup_issues"][0]
        elif row.get("active"):
            bad = str(row.get("health_status") or "").upper() in {"ERROR", "FAILED", "UNHEALTHY"}
            row["onboarding_state"] = "ERROR" if bad else "LIVE"
            row["onboarding_detail"] = str(row.get("last_error") or "Source health reports an error") if bad else "Active through the shared integration runtime"
        elif row["tested_current"]:
            row["onboarding_state"], row["onboarding_detail"] = "PAUSED", "Current configuration tested successfully and is ready to activate"
        else:
            row["onboarding_state"], row["onboarding_detail"] = "PAUSED", "Inactive; run TEST before activation"
    return rows


def _render(request: Request, *, template_key: str = "CUSTOM", q: str = "", msg: str = "", test_result: dict[str, Any] | None = None):
    rows = _source_rows(q)
    counts = {
        "total": len(rows), "active": sum(bool(x.get("active")) for x in rows),
        "needs_setup": sum(x.get("onboarding_state") == "NEEDS SETUP" for x in rows),
        "ready": sum(bool(x.get("can_activate")) for x in rows),
    }
    return templates.TemplateResponse(
        request=request, name="source_onboarding.html",
        context={
            "page": "source-onboarding", "rows": rows, "counts": counts, "q": q, "msg": msg,
            "test_result": test_result, "source_templates": SOURCE_TEMPLATES, "new_source": _template_form(template_key),
            "auth_types": AUTH_TYPES, "parser_kinds": PARSER_KINDS, "categories": CATEGORIES,
            "methods": METHODS, "adapter_types": ADAPTER_TYPES, "role": getattr(request.state, "cmos_role", None),
        },
    )


def _redirect(message: str) -> RedirectResponse:
    return RedirectResponse("/integrations/onboarding?msg=" + urllib.parse.quote_plus(message), status_code=303)


@app.get("/integrations/onboarding", response_class=HTMLResponse)
def source_onboarding_page(request: Request, template: str = "CUSTOM", q: str = "", msg: str = ""):
    return _render(request, template_key=template, q=q, msg=msg)


@app.post("/integrations/onboarding/create")
async def source_onboarding_create(request: Request):
    p = _payload_from_form(await request.form())
    if query_one("SELECT 1 AS found FROM integrations WHERE integration_key=%s", (p["integration_key"],)):
        raise HTTPException(409, "Integration key already exists")
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO integrations(
                 integration_key,name,active,category,adapter_type,endpoint_url,method,auth_type,auth_config,
                 request_headers,request_query,request_body,parser_kind,parser_config,poll_seconds,timeout_seconds,
                 max_response_bytes,allow_redirects,verify_tls,notes,provider_template,source_owner,source_contact,
                 access_instructions,geography_scope,relevance_keywords,attention_config,map_config
               ) VALUES(
                 %s,%s,false,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,
                 %s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
            (
                p["integration_key"], p["name"], p["category"], p["adapter_type"], p["endpoint_url"], p["method"], p["auth_type"],
                json.dumps(p["auth_config"]), json.dumps(p["headers"]), json.dumps(p["query"]), p["request_body"], p["parser_kind"],
                json.dumps(p["parser_config"]), p["poll_seconds"], p["timeout_seconds"], p["max_response_bytes"], p["allow_redirects"],
                p["verify_tls"], p["notes"], p["provider_template"], p["source_owner"], p["source_contact"], p["access_instructions"],
                p["geography_scope"], p["relevance_keywords"], json.dumps(p["attention_config"]), json.dumps(p["map_config"]),
            ),
        )
        conn.commit()
    return _redirect("Source created as an inactive placeholder")


@app.post("/integrations/onboarding/{integration_id}/update")
async def source_onboarding_update(request: Request, integration_id: uuid.UUID):
    existing = load_integration(integration_id=str(integration_id))
    p = _payload_from_form(await request.form(), existing=existing)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE integrations SET
                 name=%s,category=%s,adapter_type=%s,endpoint_url=%s,method=%s,auth_type=%s,auth_config=%s::jsonb,
                 request_headers=%s::jsonb,request_query=%s::jsonb,request_body=%s,parser_kind=%s,parser_config=%s::jsonb,
                 poll_seconds=%s,timeout_seconds=%s,max_response_bytes=%s,allow_redirects=%s,verify_tls=%s,notes=%s,
                 provider_template=%s,source_owner=%s,source_contact=%s,access_instructions=%s,geography_scope=%s,
                 relevance_keywords=%s,attention_config=%s::jsonb,map_config=%s::jsonb,updated_at=now()
               WHERE id=%s""",
            (
                p["name"], p["category"], p["adapter_type"], p["endpoint_url"], p["method"], p["auth_type"], json.dumps(p["auth_config"]),
                json.dumps(p["headers"]), json.dumps(p["query"]), p["request_body"], p["parser_kind"], json.dumps(p["parser_config"]),
                p["poll_seconds"], p["timeout_seconds"], p["max_response_bytes"], p["allow_redirects"], p["verify_tls"], p["notes"],
                p["provider_template"], p["source_owner"], p["source_contact"], p["access_instructions"], p["geography_scope"],
                p["relevance_keywords"], json.dumps(p["attention_config"]), json.dumps(p["map_config"]), integration_id,
            ),
        )
        conn.commit()
    return _redirect("Source configuration saved; connection/parser changes invalidate the prior TEST and pause a live source")


def _mark_generic_parser_test(integration: dict[str, Any], outcome: dict[str, Any]) -> None:
    if not outcome.get("ok") or str(integration.get("parser_kind") or "NONE").upper() == "NONE":
        return
    try:
        events = parse_events(outcome["result"].body_text, str(integration["parser_kind"]), _json_obj(integration.get("parser_config")))
        outcome["events"] = events
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE integration_runs SET items_found=%s WHERE id=%s", (len(events), outcome["run_id"]))
            conn.commit()
    except Exception as exc:
        error = redact_text(str(exc), _secret_values(integration))
        outcome.update(ok=False, error=error, events=[])
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE integration_runs SET status='ERROR',items_found=0,error_message=%s WHERE id=%s", (error, outcome["run_id"]))
            cur.execute(
                """INSERT INTO source_health(source_id,status,last_attempt_at,last_error,metadata,updated_at)
                   VALUES(%s,'ERROR',now(),%s,%s::jsonb,now())
                   ON CONFLICT(source_id) DO UPDATE SET status='ERROR',last_attempt_at=now(),
                     last_error=EXCLUDED.last_error,updated_at=now()""",
                (f"INT:{integration['integration_key']}", error, json.dumps({"integration_id": str(integration["id"]), "name": integration["name"], "test_parser_failure": True})),
            )
            conn.commit()


@app.post("/integrations/onboarding/{integration_id}/test", response_class=HTMLResponse)
def source_onboarding_test(request: Request, integration_id: uuid.UUID):
    integration = load_integration(integration_id=str(integration_id))
    issues = connection_setup_issues(integration)
    if issues:
        return _render(request, msg="TEST blocked: " + "; ".join(issues))
    outcome = run_transit_integration(integration, run_type="TEST", parse_and_store=False) if is_transit_adapter(integration) else run_integration(integration, run_type="TEST", parse_and_store=False)
    if not is_transit_adapter(integration):
        _mark_generic_parser_test(integration, outcome)
    recorded = record_test_result(integration, outcome)
    message = "Read-only TEST complete" + ("" if recorded else "; configuration changed during TEST, so activation remains blocked")
    return _render(request, msg=message, test_result=build_test_result(integration, outcome))


@app.post("/integrations/onboarding/{integration_id}/activate")
def source_onboarding_activate(request: Request, integration_id: uuid.UUID):
    integration = load_integration(integration_id=str(integration_id))
    gate = activation_state(integration)
    if not gate["can_activate"]:
        return _redirect("Activation blocked: " + gate["activation_detail"])
    actor = getattr(request.state, "cmos_user", "web")
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE integrations SET active=true,activation_override_at=NULL,
                       activation_override_by=NULL,activation_override_reason=NULL,updated_at=now() WHERE id=%s""", (integration_id,))
        conn.commit()
    _record_activation(integration_id, "ACTIVATE", actor)
    return _redirect("Source activated")


@app.post("/integrations/onboarding/{integration_id}/pause")
def source_onboarding_pause(request: Request, integration_id: uuid.UUID):
    actor = getattr(request.state, "cmos_user", "web")
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE integrations SET active=false,updated_at=now() WHERE id=%s", (integration_id,))
        conn.commit()
    _record_activation(integration_id, "PAUSE", actor)
    return _redirect("Source paused")


@app.post("/integrations/onboarding/{integration_id}/activate-override")
async def source_onboarding_activate_override(request: Request, integration_id: uuid.UUID):
    if getattr(request.state, "cmos_role", None) != "EXECUTIVE":
        raise HTTPException(403, "Executive authorization is required")
    form = await request.form()
    reason = str(form.get("reason") or "").strip()
    if len(reason) < 12:
        raise HTTPException(400, "Override reason must be at least 12 characters")
    integration = load_integration(integration_id=str(integration_id))
    issues = collection_setup_issues(integration)
    if issues:
        return _redirect("Override cannot bypass incomplete source setup: " + issues[0])
    actor = getattr(request.state, "cmos_user", "web")
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE integrations SET active=true,activation_override_at=now(),
                       activation_override_by=%s,activation_override_reason=%s,updated_at=now() WHERE id=%s""",
                    (actor, reason, integration_id))
        conn.commit()
    _record_activation(integration_id, "OVERRIDE_ACTIVATE", actor, reason)
    return _redirect("Source activated with recorded executive override")
