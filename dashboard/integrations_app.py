from __future__ import annotations

import json
import os
import re
import shlex
import urllib.parse
import uuid
from typing import Any

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from schedule_app import app
from app import db_conn, execute, make_watch_id, query_all, query_one, templates, validate_watch
from integration_engine import load_integration, run_integration
from integration_runtime import (
    apply_literal_auth,
    build_request_body,
    parse_header_lines,
    parse_json_object,
    parse_query_lines,
    perform_http_request,
    redact_headers,
    redact_text,
)

AUTH_TYPES = ["NONE", "BEARER_ENV", "BASIC_ENV", "API_KEY_HEADER_ENV", "API_KEY_QUERY_ENV"]
PARSER_KINDS = ["NONE", "JSON_EVENTS", "RSS_EVENTS", "ATOM_EVENTS", "ICS_EVENTS"]
CATEGORIES = ["GENERIC", "EVENTS", "TRANSIT", "TRAFFIC", "WEATHER", "UTILITY", "PUBLIC_SAFETY", "GOVERNMENT"]
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]


def _integration_key(name: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:44] or "INTEGRATION"
    return f"{slug}_{uuid.uuid4().hex[:6].upper()}"


def _json_text(value: Any) -> str:
    if not value:
        return "{}"
    return json.dumps(value, indent=2, sort_keys=True) if not isinstance(value, str) else value


def _env_state(auth_config: Any) -> list[dict[str, Any]]:
    config = auth_config or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            return []
    rows = []
    for key, env_name in config.items():
        if not str(key).endswith("_env") or not env_name:
            continue
        rows.append({"field": key, "env": env_name, "set": bool(os.getenv(str(env_name)))})
    return rows


def _fetch_integrations(q: str = "", state: str = "all") -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    where = []
    params: list[Any] = []
    if state == "active":
        where.append("i.active=true")
    elif state == "inactive":
        where.append("i.active=false")
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append("(i.name ILIKE %s OR i.integration_key ILIKE %s OR i.category ILIKE %s OR i.endpoint_url ILIKE %s)")
        params.extend([needle, needle, needle, needle])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = query_all(
        f"""
        SELECT i.*,
               lr.status AS last_run_status,
               lr.http_status AS last_http_status,
               lr.started_at AS last_run_at,
               lr.elapsed_ms AS last_elapsed_ms,
               lr.error_message AS last_error,
               sh.status AS health_status,
               sh.last_success_at,
               sh.last_event_at
        FROM integrations i
        LEFT JOIN LATERAL (
          SELECT status,http_status,started_at,elapsed_ms,error_message
          FROM integration_runs r
          WHERE r.integration_id=i.id
          ORDER BY r.started_at DESC LIMIT 1
        ) lr ON true
        LEFT JOIN source_health sh ON sh.source_id='INT:' || i.integration_key
        {clause}
        ORDER BY i.active DESC,i.category,i.name
        LIMIT 300
        """,
        params,
    )
    for row in rows:
        row["auth_config_text"] = _json_text(row.get("auth_config"))
        row["headers_text"] = _json_text(row.get("request_headers"))
        row["query_text"] = _json_text(row.get("request_query"))
        row["parser_config_text"] = _json_text(row.get("parser_config"))
        row["env_state"] = _env_state(row.get("auth_config"))
    counts = query_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE active) AS active,
               count(*) FILTER (WHERE NOT active) AS inactive,
               count(*) FILTER (WHERE parser_kind <> 'NONE') AS collectors
        FROM integrations
        """
    )
    recent = query_all(
        """
        SELECT r.*,i.name,i.integration_key
        FROM integration_runs r
        JOIN integrations i ON i.id=r.integration_id
        ORDER BY r.started_at DESC LIMIT 25
        """
    )
    return rows, counts, recent


def _render_integrations(request: Request, *, q: str = "", state: str = "all", msg: str = "", test_result: dict[str, Any] | None = None):
    rows, counts, recent = _fetch_integrations(q, state)
    return templates.TemplateResponse(
        request=request,
        name="integrations.html",
        context={
            "rows": rows,
            "counts": counts,
            "recent": recent,
            "q": q,
            "state": state,
            "msg": msg,
            "test_result": test_result,
            "auth_types": AUTH_TYPES,
            "parser_kinds": PARSER_KINDS,
            "categories": CATEGORIES,
            "methods": METHODS,
            "page": "integrations",
        },
    )


@app.get("/admin-tools", response_class=HTMLResponse)
def admin_tools(request: Request):
    counts = query_one(
        """
        SELECT
          (SELECT count(*) FROM watch_items WHERE active) AS watches,
          (SELECT count(*) FROM subscribers WHERE active) AS subscribers,
          (SELECT count(*) FROM watch_item_recipients WHERE active) AS routes,
          (SELECT count(*) FROM integrations WHERE active) AS integrations,
          (SELECT count(*) FROM event_intelligence WHERE active AND impact_level IN ('WATCH','ALERT')) AS event_watch,
          (SELECT count(*) FROM source_health WHERE upper(status) NOT IN ('OK','HEALTHY')) AS unhealthy
        """
    )
    return templates.TemplateResponse(
        request=request,
        name="admin_tools.html",
        context={"counts": counts, "page": "admin-tools"},
    )


@app.get("/alert-admin", response_class=HTMLResponse)
def alert_admin(request: Request, msg: str = ""):
    watches = query_all(
        """
        SELECT w.id,w.watch_id,w.active,w.watch_type,w.display_name,w.search_term,w.match_mode,
               w.match_field,w.min_priority,w.municipality,w.address,w.notes,
               COALESCE(jsonb_agg(jsonb_build_object(
                 'route_id',wir.id::text,'route_active',wir.active,'subscriber_id',s.id::text,
                 'subscriber_key',s.subscriber_id,'name',s.name,'ntfy_topic',s.ntfy_topic
               ) ORDER BY s.name) FILTER (WHERE s.id IS NOT NULL),'[]'::jsonb) AS recipients
        FROM watch_items w
        LEFT JOIN watch_item_recipients wir ON wir.watch_item_id=w.id
        LEFT JOIN subscribers s ON s.id=wir.subscriber_id
        GROUP BY w.id
        ORDER BY w.active DESC,w.display_name
        LIMIT 300
        """
    )
    subscribers = query_all(
        """SELECT id,subscriber_id,name,active,ntfy_topic,notes
             FROM subscribers ORDER BY active DESC,name"""
    )
    counts = query_one(
        """
        SELECT
          (SELECT count(*) FROM watch_items WHERE active) AS watches,
          (SELECT count(*) FROM subscribers WHERE active) AS subscribers,
          (SELECT count(*) FROM watch_item_recipients WHERE active) AS routes
        """
    )
    return templates.TemplateResponse(
        request=request,name="alert_admin.html",
        context={"watches":watches,"subscribers":subscribers,"counts":counts,"msg":msg,"page":"alert-admin"},
    )


@app.post("/alert-admin/watch")
def alert_admin_create_watch(
    display_name: str = Form(...), search_term: str = Form(...), watch_type: str = Form("PHRASE"),
    match_mode: str = Form("CONTAINS"), match_field: str = Form(""), min_priority: int = Form(1),
    municipality: str = Form(""), address: str = Form(""), notes: str = Form(""),
    subscriber_ids: list[uuid.UUID] = Form([]),
):
    display_name=display_name.strip(); search_term=search_term.strip(); match_mode=match_mode.upper().strip()
    if not display_name or not search_term:
        raise HTTPException(400,"Display name and search term are required")
    validate_watch(match_mode,match_field,min_priority)
    watch_id=make_watch_id(display_name)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watch_items(
              watch_id,active,watch_type,display_name,search_term,match_mode,match_field,min_priority,
              municipality,address,notes
            ) VALUES(%s,true,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (watch_id,watch_type.strip().upper(),display_name,search_term,match_mode,match_field.strip() or None,
             min_priority,municipality.strip() or None,address.strip() or None,notes.strip() or None),
        )
        watch_uuid=cur.fetchone()["id"]
        for subscriber_id in subscriber_ids:
            cur.execute(
                """INSERT INTO watch_item_recipients(watch_item_id,subscriber_id,active)
                   VALUES(%s,%s,true)
                   ON CONFLICT(watch_item_id,subscriber_id) DO UPDATE SET active=true""",
                (watch_uuid,subscriber_id),
            )
        conn.commit()
    return RedirectResponse("/alert-admin?msg=Watch+created+and+routing+saved",status_code=303)


@app.get("/integrations", response_class=HTMLResponse)
def integrations_page(request: Request, q: str = "", state: str = "all", msg: str = ""):
    return _render_integrations(request, q=q, state=state, msg=msg)


def _integration_payload(
    *,
    name: str,
    integration_key: str,
    category: str,
    adapter_type: str,
    endpoint_url: str,
    method: str,
    auth_type: str,
    username_env: str,
    password_env: str,
    token_env: str,
    key_env: str,
    key_name: str,
    headers_json: str,
    query_json: str,
    request_body: str,
    parser_kind: str,
    parser_config_json: str,
    poll_seconds: int,
    timeout_seconds: int,
    max_response_bytes: int,
    allow_redirects: bool,
    verify_tls: bool,
    notes: str,
) -> dict[str, Any]:
    if method not in METHODS:
        raise HTTPException(400, "Invalid method")
    if auth_type not in AUTH_TYPES:
        raise HTTPException(400, "Invalid auth type")
    if parser_kind not in PARSER_KINDS:
        raise HTTPException(400, "Invalid parser kind")
    auth_config = {
        "username_env": username_env.strip() or None,
        "password_env": password_env.strip() or None,
        "token_env": token_env.strip() or None,
        "key_env": key_env.strip() or None,
        "key_name": key_name.strip() or None,
    }
    auth_config = {k: v for k, v in auth_config.items() if v}
    try:
        headers = parse_json_object(headers_json, "Headers")
        query = parse_json_object(query_json, "Query")
        parser_config = parse_json_object(parser_config_json, "Parser config")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "name": name.strip(),
        "integration_key": integration_key.strip().upper() or _integration_key(name),
        "category": category.strip().upper() or "GENERIC",
        "adapter_type": adapter_type.strip().upper() or "HTTP",
        "endpoint_url": endpoint_url.strip(),
        "method": method,
        "auth_type": auth_type,
        "auth_config": json.dumps(auth_config),
        "headers": json.dumps(headers),
        "query": json.dumps(query),
        "request_body": request_body or None,
        "parser_kind": parser_kind,
        "parser_config": json.dumps(parser_config),
        "poll_seconds": max(60, min(86400, int(poll_seconds))),
        "timeout_seconds": max(1, min(60, int(timeout_seconds))),
        "max_response_bytes": max(1024, min(5_000_000, int(max_response_bytes))),
        "allow_redirects": allow_redirects,
        "verify_tls": verify_tls,
        "notes": notes.strip() or None,
    }


@app.post("/integrations/create")
def integration_create(
    name: str = Form(...), integration_key: str = Form(""), category: str = Form("GENERIC"),
    adapter_type: str = Form("HTTP"), endpoint_url: str = Form(...), method: str = Form("GET"),
    auth_type: str = Form("NONE"), username_env: str = Form(""), password_env: str = Form(""),
    token_env: str = Form(""), key_env: str = Form(""), key_name: str = Form(""),
    headers_json: str = Form("{}"), query_json: str = Form("{}"), request_body: str = Form(""),
    parser_kind: str = Form("NONE"), parser_config_json: str = Form("{}"),
    poll_seconds: int = Form(900), timeout_seconds: int = Form(15), max_response_bytes: int = Form(1000000),
    allow_redirects: str | None = Form(None), verify_tls: str | None = Form(None), notes: str = Form(""),
):
    p = _integration_payload(
        name=name, integration_key=integration_key, category=category, adapter_type=adapter_type,
        endpoint_url=endpoint_url, method=method, auth_type=auth_type,
        username_env=username_env, password_env=password_env, token_env=token_env, key_env=key_env, key_name=key_name,
        headers_json=headers_json, query_json=query_json, request_body=request_body,
        parser_kind=parser_kind, parser_config_json=parser_config_json,
        poll_seconds=poll_seconds, timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes,
        allow_redirects=allow_redirects is not None, verify_tls=verify_tls is not None, notes=notes,
    )
    execute(
        """
        INSERT INTO integrations(
          integration_key,name,active,category,adapter_type,endpoint_url,method,auth_type,auth_config,
          request_headers,request_query,request_body,parser_kind,parser_config,poll_seconds,timeout_seconds,
          max_response_bytes,allow_redirects,verify_tls,notes
        ) VALUES(%s,%s,false,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
        """,
        (p["integration_key"],p["name"],p["category"],p["adapter_type"],p["endpoint_url"],p["method"],p["auth_type"],
         p["auth_config"],p["headers"],p["query"],p["request_body"],p["parser_kind"],p["parser_config"],p["poll_seconds"],
         p["timeout_seconds"],p["max_response_bytes"],p["allow_redirects"],p["verify_tls"],p["notes"]),
    )
    return RedirectResponse("/integrations?msg=Integration+created+inactive+-+test+before+activating", status_code=303)


@app.post("/integrations/{integration_id}/update")
def integration_update(
    integration_id: uuid.UUID,
    name: str = Form(...), integration_key: str = Form(...), category: str = Form("GENERIC"),
    adapter_type: str = Form("HTTP"), endpoint_url: str = Form(...), method: str = Form("GET"),
    auth_type: str = Form("NONE"), username_env: str = Form(""), password_env: str = Form(""),
    token_env: str = Form(""), key_env: str = Form(""), key_name: str = Form(""),
    headers_json: str = Form("{}"), query_json: str = Form("{}"), request_body: str = Form(""),
    parser_kind: str = Form("NONE"), parser_config_json: str = Form("{}"),
    poll_seconds: int = Form(900), timeout_seconds: int = Form(15), max_response_bytes: int = Form(1000000),
    allow_redirects: str | None = Form(None), verify_tls: str | None = Form(None), notes: str = Form(""),
    active: str | None = Form(None),
):
    p = _integration_payload(
        name=name, integration_key=integration_key, category=category, adapter_type=adapter_type,
        endpoint_url=endpoint_url, method=method, auth_type=auth_type,
        username_env=username_env, password_env=password_env, token_env=token_env, key_env=key_env, key_name=key_name,
        headers_json=headers_json, query_json=query_json, request_body=request_body,
        parser_kind=parser_kind, parser_config_json=parser_config_json,
        poll_seconds=poll_seconds, timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes,
        allow_redirects=allow_redirects is not None, verify_tls=verify_tls is not None, notes=notes,
    )
    execute(
        """
        UPDATE integrations SET
          integration_key=%s,name=%s,active=%s,category=%s,adapter_type=%s,endpoint_url=%s,method=%s,
          auth_type=%s,auth_config=%s::jsonb,request_headers=%s::jsonb,request_query=%s::jsonb,request_body=%s,
          parser_kind=%s,parser_config=%s::jsonb,poll_seconds=%s,timeout_seconds=%s,max_response_bytes=%s,
          allow_redirects=%s,verify_tls=%s,notes=%s,updated_at=now()
        WHERE id=%s
        """,
        (p["integration_key"],p["name"],active is not None,p["category"],p["adapter_type"],p["endpoint_url"],p["method"],
         p["auth_type"],p["auth_config"],p["headers"],p["query"],p["request_body"],p["parser_kind"],p["parser_config"],
         p["poll_seconds"],p["timeout_seconds"],p["max_response_bytes"],p["allow_redirects"],p["verify_tls"],p["notes"],integration_id),
    )
    return RedirectResponse("/integrations?msg=Integration+updated", status_code=303)


@app.post("/integrations/{integration_id}/toggle")
def integration_toggle(integration_id: uuid.UUID):
    execute("UPDATE integrations SET active=NOT active,updated_at=now() WHERE id=%s", (integration_id,))
    return RedirectResponse("/integrations?msg=Integration+status+changed", status_code=303)


@app.post("/integrations/{integration_id}/test", response_class=HTMLResponse)
def integration_test(request: Request, integration_id: uuid.UUID):
    integration = load_integration(integration_id=str(integration_id))
    outcome = run_integration(integration, run_type="TEST", parse_and_store=False)
    result = outcome.get("result")
    test_result = {
        "name": integration["name"],
        "ok": outcome["ok"],
        "error": outcome.get("error"),
        "status_code": result.status_code if result else None,
        "elapsed_ms": result.elapsed_ms if result else None,
        "content_type": result.content_type if result else None,
        "body_preview": (result.body_text[:12000] if result else ""),
        "headers": result.headers if result else {},
        "truncated": result.truncated if result else False,
    }
    return _render_integrations(request, msg="Connection test complete", test_result=test_result)


@app.post("/integrations/{integration_id}/run")
def integration_run(integration_id: uuid.UUID):
    integration = load_integration(integration_id=str(integration_id))
    outcome = run_integration(integration, run_type="MANUAL", parse_and_store=True)
    if not outcome["ok"]:
        message = "Integration run failed"
    else:
        message = f"Integration run complete: {len(outcome.get('events') or [])} items, {outcome.get('changed') or 0} changed"
    return RedirectResponse("/integrations?msg=" + message.replace(" ", "+").replace(":", "%3A"), status_code=303)


def _pretty_body(text: str, content_type: str) -> str:
    if "json" in (content_type or "").lower() or text.lstrip().startswith(("{", "[")):
        try:
            return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            return text
    return text


def _curl_command(method: str, url: str, headers: dict[str, str], body: str, body_mode: str, query: dict[str, Any], secrets: list[str]) -> str:
    if query:
        parsed = list(urllib.parse.urlsplit(url))
        existing = urllib.parse.parse_qsl(parsed[3], keep_blank_values=True)
        for key, value in query.items():
            values = value if isinstance(value, list) else [value]
            existing.extend((str(key), str(v)) for v in values)
        parsed[3] = urllib.parse.urlencode(existing, doseq=True)
        url = urllib.parse.urlunsplit(parsed)
    parts = ["curl", "-i", "-X", method, shlex.quote(redact_text(url, secrets))]
    safe_headers = redact_headers(headers)
    for key, value in safe_headers.items():
        if body_mode.upper() == "MULTIPART" and key.lower() == "content-type":
            continue
        parts.extend(["-H", shlex.quote(f"{key}: {redact_text(value, secrets)}")])
    if body:
        if body_mode.upper() == "MULTIPART":
            for key, value in parse_query_lines(body).items():
                values = value if isinstance(value, list) else [value]
                for item in values:
                    parts.extend(["-F", shlex.quote(f"{key}={redact_text(str(item), secrets)}")])
        elif body_mode.upper() == "FORM_URLENCODED":
            for key, value in parse_query_lines(body).items():
                values = value if isinstance(value, list) else [value]
                for item in values:
                    parts.extend(["--data-urlencode", shlex.quote(f"{key}={redact_text(str(item), secrets)}")])
        else:
            parts.extend(["--data-raw", shlex.quote(redact_text(body, secrets))])
    return " ".join(parts)


@app.get("/api-lab", response_class=HTMLResponse)
def api_lab(request: Request, preset: str = ""):
    presets = {
        "njt-token": {
            "method": "POST",
            "url": "https://raildata.njt.gov/api/GTFSRT/getToken",
            "headers_text": "Accept: text/plain",
            "query_text": "",
            "body_mode": "MULTIPART",
            "body": "username={{USERNAME}}&password={{PASSWORD}}",
            "auth_type": "NONE",
            "timeout_seconds": 20,
            "max_response_bytes": 1000000,
            "allow_redirects": True,
            "verify_tls": True,
        },
        "njt-rail-data": {
            "method": "POST",
            "url": "https://raildata.njt.gov/api/GTFSRT/getGTFS",
            "headers_text": "Accept: */*",
            "query_text": "",
            "body_mode": "MULTIPART",
            "body": "token={{TOKEN}}",
            "auth_type": "NONE",
            "timeout_seconds": 30,
            "max_response_bytes": 5000000,
            "allow_redirects": True,
            "verify_tls": True,
        },
        "nyc-events": {
            "method": "GET",
            "url": "https://data.cityofnewyork.us/resource/tvpp-9vvx.json",
            "headers_text": "Accept: application/json",
            "query_text": "$limit=20&$where=event_borough='Manhattan'&$order=start_date_time ASC",
            "body_mode": "RAW",
            "body": "",
            "auth_type": "NONE",
            "timeout_seconds": 20,
            "max_response_bytes": 1000000,
            "allow_redirects": True,
            "verify_tls": True,
        },
        "ticketmaster": {
            "method": "GET",
            "url": "https://app.ticketmaster.com/discovery/v2/events.json",
            "headers_text": "Accept: application/json",
            "query_text": "latlong=40.7696,-74.0204&radius=25&unit=miles&size=20&sort=date,asc",
            "body_mode": "RAW",
            "body": "",
            "auth_type": "API_KEY_QUERY",
            "api_key_name": "apikey",
            "timeout_seconds": 20,
            "max_response_bytes": 1000000,
            "allow_redirects": True,
            "verify_tls": True,
        },
    }
    form = presets.get(preset, {})
    return templates.TemplateResponse(
        request=request,
        name="api_lab.html",
        context={"methods": METHODS, "result": None, "form": form, "page": "api-lab", "private_allowed": os.getenv("API_LAB_ALLOW_PRIVATE", "false")},
    )


@app.post("/api-lab", response_class=HTMLResponse)
def api_lab_run(
    request: Request,
    method: str = Form("GET"), url: str = Form(...), headers_text: str = Form(""), query_text: str = Form(""),
    body: str = Form(""), body_mode: str = Form("RAW"), auth_type: str = Form("NONE"), username: str = Form(""), password: str = Form(""),
    token: str = Form(""), api_key_name: str = Form(""), api_key_value: str = Form(""),
    timeout_seconds: int = Form(15), max_response_bytes: int = Form(1000000),
    allow_redirects: str | None = Form(None), verify_tls: str | None = Form(None),
):
    form = {
        "method": method, "url": url, "headers_text": headers_text, "query_text": query_text, "body": body,
        "auth_type": auth_type, "username": username, "body_mode": body_mode, "timeout_seconds": timeout_seconds,
        "max_response_bytes": max_response_bytes, "allow_redirects": allow_redirects is not None, "verify_tls": verify_tls is not None,
        "api_key_name": api_key_name,
    }
    secrets: list[str] = []
    try:
        headers = parse_header_lines(headers_text)
        query = parse_query_lines(query_text)
        secrets = apply_literal_auth(
            auth_type, headers, query, username=username, password=password, token=token,
            api_key_name=api_key_name, api_key_value=api_key_value,
        )
        body_for_request = (body or "")
        substitutions = {
            "{{USERNAME}}": username,
            "{{PASSWORD}}": password,
            "{{TOKEN}}": token,
            "{{API_KEY}}": api_key_value,
        }
        for marker, value in substitutions.items():
            if value:
                body_for_request = body_for_request.replace(marker, value)
        payload, generated_content_type = build_request_body(body_mode, body_for_request)
        if generated_content_type and not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = generated_content_type
        response = perform_http_request(
            method=method, url=url, headers=headers, query=query, body=payload,
            timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes,
            allow_redirects=allow_redirects is not None, verify_tls=verify_tls is not None,
        )
        display_body = redact_text(_pretty_body(response.body_text, response.content_type), secrets)
        result = {
            "ok": response.ok,
            "status_code": response.status_code,
            "final_url": redact_text(response.final_url, secrets),
            "elapsed_ms": response.elapsed_ms,
            "headers": redact_headers(response.headers),
            "body": display_body,
            "body_bytes": response.body_bytes,
            "truncated": response.truncated,
            "content_type": response.content_type,
            "error": redact_text(response.error, secrets) if response.error else None,
            "curl": _curl_command(method, url, headers, body_for_request, body_mode, query, secrets),
        }
    except Exception as exc:
        result = {"ok": False, "error": redact_text(str(exc), secrets), "status_code": None, "headers": {}, "body": "", "curl": ""}
    return templates.TemplateResponse(
        request=request,
        name="api_lab.html",
        context={"methods": METHODS, "result": result, "form": form, "page": "api-lab", "private_allowed": os.getenv("API_LAB_ALLOW_PRIVATE", "false")},
    )


@app.get("/event-intelligence", response_class=HTMLResponse)
def event_intelligence_page(request: Request, level: str = "all", horizon: str = "7d", q: str = "", msg: str = ""):
    where = ["e.active=true"]
    params: list[Any] = []
    if level in {"AWARENESS", "WATCH", "ALERT"}:
        where.append("e.impact_level=%s")
        params.append(level)
    if horizon == "today":
        where.append("e.starts_at >= date_trunc('day',now() AT TIME ZONE 'America/New_York') AT TIME ZONE 'America/New_York'")
        where.append("e.starts_at < (date_trunc('day',now() AT TIME ZONE 'America/New_York') + interval '1 day') AT TIME ZONE 'America/New_York'")
    elif horizon == "24h":
        where.append("e.starts_at BETWEEN now() - interval '2 hours' AND now() + interval '24 hours'")
    elif horizon == "7d":
        where.append("COALESCE(e.starts_at,now()) <= now() + interval '7 days'")
        where.append("COALESCE(e.ends_at,e.starts_at,now()) >= now() - interval '12 hours'")
    elif horizon == "30d":
        where.append("COALESCE(e.starts_at,now()) <= now() + interval '30 days'")
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append("(e.title ILIKE %s OR e.venue ILIKE %s OR e.municipality ILIKE %s OR e.impact_summary ILIKE %s OR e.transit_impact ILIKE %s OR e.road_impact ILIKE %s)")
        params.extend([needle] * 6)
    rows = query_all(
        f"""
        SELECT e.*,i.name AS integration_name,i.integration_key,
               e.starts_at AT TIME ZONE 'America/New_York' AS starts_local,
               e.ends_at AT TIME ZONE 'America/New_York' AS ends_local
        FROM event_intelligence e
        LEFT JOIN integrations i ON i.id=e.source_integration_id
        WHERE {' AND '.join(where)}
        ORDER BY CASE e.impact_level WHEN 'ALERT' THEN 0 WHEN 'WATCH' THEN 1 ELSE 2 END,
                 e.impact_score DESC,e.starts_at NULLS LAST,e.updated_at DESC
        LIMIT 500
        """,
        params,
    )
    counts = query_one(
        """
        SELECT count(*) FILTER (WHERE active AND impact_level='ALERT') AS alerts,
               count(*) FILTER (WHERE active AND impact_level='WATCH') AS watches,
               count(*) FILTER (WHERE active AND impact_level='AWARENESS') AS awareness,
               count(*) FILTER (WHERE active AND starts_at BETWEEN now() AND now()+interval '7 days') AS next7
        FROM event_intelligence
        """
    )
    return templates.TemplateResponse(
        request=request,
        name="event_intelligence.html",
        context={"rows": rows, "counts": counts, "level": level, "horizon": horizon, "q": q, "msg": msg, "page": "event-intelligence"},
    )


@app.post("/event-intelligence/{event_id}/promote")
def event_intelligence_promote(event_id: uuid.UUID):
    execute(
        """
        WITH inserted AS (
          INSERT INTO operational_events(
            active,title,category,location_name,address,municipality,starts_at,ends_at,priority,source,notes,
            owner,event_status,event_scope,source_url,expected_attendance,impact_notes,event_intelligence_id
          )
          SELECT true,e.title,e.event_type,e.venue,e.address,COALESCE(e.municipality,'Weehawken'),
                 COALESCE(e.starts_at,now()),e.ends_at,
                 CASE e.impact_level WHEN 'ALERT' THEN 5 WHEN 'WATCH' THEN 4 ELSE 3 END,
                 'EVENT_INTELLIGENCE',e.description,NULL,'TRACKING','EXTERNAL',e.source_url,e.attendance_estimate,
                 concat_ws(' · ',e.impact_level || ' ' || e.impact_score::text,e.impact_summary,e.road_impact,e.transit_impact),e.id
          FROM event_intelligence e
          WHERE e.id=%s AND e.promoted_event_id IS NULL
          RETURNING id,event_intelligence_id
        )
        UPDATE event_intelligence e
        SET promoted_event_id=i.id,updated_at=now()
        FROM inserted i WHERE e.id=i.event_intelligence_id
        """,
        (event_id,),
    )
    return RedirectResponse("/schedule?msg=External+event+promoted+to+managed+Events", status_code=303)


@app.post("/event-intelligence/{event_id}/create-action")
def event_intelligence_create_action(event_id: uuid.UUID):
    execute(
        """
        INSERT INTO issues(title,description,category,priority,status,source,municipality,item_type,event_intelligence_id)
        SELECT 'Event prep: ' || e.title,
               concat_ws(E'\n',e.impact_summary,e.road_impact,e.transit_impact,e.source_url),
               'EVENT',CASE e.impact_level WHEN 'ALERT' THEN 5 WHEN 'WATCH' THEN 4 ELSE 3 END,
               'OPEN','EVENT_INTELLIGENCE',e.municipality,'TASK',e.id
        FROM event_intelligence e
        WHERE e.id=%s
          AND NOT EXISTS(
            SELECT 1 FROM issues i
            WHERE i.event_intelligence_id=e.id AND i.status NOT IN ('RESOLVED','CLOSED')
          )
        """,
        (event_id,),
    )
    return RedirectResponse("/issues?msg=Event+action+ready", status_code=303)


@app.post("/event-intelligence/{event_id}/level")
def event_intelligence_level(event_id: uuid.UUID, impact_level: str = Form(...)):
    if impact_level not in {"AWARENESS", "WATCH", "ALERT"}:
        raise HTTPException(400, "Invalid impact level")
    execute("UPDATE event_intelligence SET impact_level=%s,alert_pending=(%s='ALERT'),updated_at=now() WHERE id=%s", (impact_level, impact_level, event_id))
    return RedirectResponse("/event-intelligence?msg=Impact+level+updated", status_code=303)


@app.post("/event-intelligence/{event_id}/dismiss")
def event_intelligence_dismiss(event_id: uuid.UUID):
    execute("UPDATE event_intelligence SET active=false,alert_pending=false,updated_at=now() WHERE id=%s", (event_id,))
    return RedirectResponse("/event-intelligence?msg=Event+dismissed", status_code=303)
