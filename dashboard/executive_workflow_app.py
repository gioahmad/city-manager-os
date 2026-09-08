from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import today_board_app as today_module
from app import app, execute, query_all, query_one, templates

EASTERN = ZoneInfo("America/New_York")
CAPTURE_TYPES = {
    "INBOX",
    "ISSUE",
    "TASK",
    "FOLLOW_UP",
    "DECISION",
    "COMMITMENT",
    "COMMUNICATION",
    "IDEA",
}
TRIAGE_TYPES = sorted(CAPTURE_TYPES - {"INBOX"})
WIZARD_KEYS = {
    "FIRE_DUTY_GROUP",
    "POLICE_DUTY_SQUADS",
    "GARBAGE_TODAY",
    "RECYCLING_TODAY",
}


def _remove_get(path: str) -> None:
    app.router.routes = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", None) == path
            and "GET" in (getattr(route, "methods", set()) or set())
        )
    ]


def _safe_return(value: str | None, default: str = "/my-day") -> str:
    value = (value or default).strip()
    if not value.startswith("/") or value.startswith("//"):
        return default
    if value.startswith("/login") or value.startswith("/logout"):
        return default
    return value


def _capture_title(raw: str) -> str:
    for line in (raw or "").splitlines():
        text = re.sub(r"\s+", " ", line).strip()
        if text:
            return text if len(text) <= 180 else text[:177].rstrip() + "..."
    return "Quick Capture"


def _int(value: Any, default: int = 3) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _username(request: Request) -> str:
    return str(getattr(request.state, "cmos_user", None) or "local").strip() or "local"


def _json_obj(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    return dict(value)


def _parse_cycle_lines(text: str) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        value, _, detail = line.partition("|")
        item = {"value": value.strip()}
        if detail.strip():
            item["detail"] = detail.strip()
        if item["value"]:
            output.append(item)
    return output[:366]


def _cycle_text(config: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in config.get("sequence") or []:
        if isinstance(item, dict):
            value = str(item.get("value") or "").strip()
            detail = str(item.get("detail") or "").strip()
            if value:
                lines.append(value + (" | " + detail if detail else ""))
        elif str(item).strip():
            lines.append(str(item).strip())
    return "\n".join(lines)


def _inject(response, marker: str, template_name: str, context: dict[str, Any]) -> HTMLResponse:
    body = response.body.decode("utf-8")
    if marker not in body:
        raise RuntimeError(f"Executive Workflow injection marker not found: {marker}")
    partial = templates.get_template(template_name).render(**context)
    body = body.replace(marker, partial + "\n" + marker, 1)
    headers = dict(response.headers)
    headers.pop("content-length", None)
    return HTMLResponse(content=body, status_code=response.status_code, headers=headers)


def _review_state(request: Request) -> dict[str, Any]:
    user = _username(request)
    now = datetime.now(EASTERN)
    row = query_one(
        "SELECT last_reviewed_at FROM executive_review_state WHERE username=%s",
        (user,),
    )
    first = not bool(row and row.get("last_reviewed_at"))
    cutoff = row.get("last_reviewed_at") if row else None
    if cutoff is None:
        cutoff = now - timedelta(hours=24)
    return {"username": user, "cutoff": cutoff, "now": now, "first_review": first}


def _change_window(request: Request) -> dict[str, Any]:
    state = _review_state(request)
    cutoff = state["cutoff"]

    changed_issues = query_all(
        """
        SELECT id,title,item_type,priority,status,assigned_to,waiting_on,
               created_at,updated_at,closed_at,
               CASE
                 WHEN created_at >= %s THEN 'NEW'
                 WHEN closed_at IS NOT NULL AND closed_at >= %s THEN 'CLOSED'
                 ELSE 'UPDATED'
               END AS change_type
        FROM issues
        WHERE updated_at >= %s OR created_at >= %s OR closed_at >= %s
        ORDER BY GREATEST(updated_at,created_at,COALESCE(closed_at,'epoch'::timestamptz)) DESC
        LIMIT 80
        """,
        (cutoff, cutoff, cutoff, cutoff, cutoff),
    )

    alerts = query_all(
        """
        SELECT alert_id,source,category,title,priority,status,municipality,received_at
        FROM alerts
        WHERE received_at >= %s
        ORDER BY received_at DESC
        LIMIT 60
        """,
        (cutoff,),
    )

    events = query_all(
        """
        SELECT DISTINCT ON (fingerprint)
               fingerprint,title,impact_level,impact_score,municipality,venue,starts_at,updated_at
        FROM event_intelligence
        WHERE updated_at >= %s
          AND active=true
          AND (starts_at IS NULL OR starts_at <= now() + interval '14 days')
        ORDER BY fingerprint,updated_at DESC,impact_score DESC
        LIMIT 60
        """,
        (cutoff,),
    )

    source_warnings = query_all(
        """
        SELECT source_id,status,last_error,updated_at,last_success_at
        FROM source_health
        WHERE updated_at >= %s
          AND upper(status) IN ('ERROR','DOWN','FAILED','UNHEALTHY','STALE')
        ORDER BY updated_at DESC
        LIMIT 30
        """,
        (cutoff,),
    )

    counts = {
        "issues": len(changed_issues),
        "alerts": len(alerts),
        "events": len(events),
        "source_warnings": len(source_warnings),
    }
    counts["total"] = sum(counts.values())
    return {
        **state,
        "changed_issues": changed_issues,
        "changed_alerts": alerts,
        "changed_events": events,
        "source_warnings": source_warnings,
        "change_counts": counts,
    }


def _wizard_rows() -> list[dict[str, Any]]:
    rows = query_all(
        """
        SELECT id,rule_key,name,category,rule_type,active,pinned,sort_order,config,source_label,notes
        FROM daily_constant_rules
        WHERE rule_key = ANY(%s)
        ORDER BY sort_order,name
        """,
        (list(WIZARD_KEYS),),
    )
    for row in rows:
        row["config"] = _json_obj(row.get("config"))
        row["cycle_text"] = _cycle_text(row["config"])
        row["days"] = {int(x) for x in row["config"].get("days") or [] if str(x).isdigit()}
    return rows


# Wrap My Day after Today Board has registered its renderer.
_remove_get("/my-day")


@app.get("/my-day", response_class=HTMLResponse)
def executive_workflow_my_day(request: Request):
    response = today_module.today_board_my_day(request)
    changes = _change_window(request)
    context = {"request": request, **changes}
    return _inject(
        response,
        '<section class="section-label"><span>DAILY BRIEF',
        "executive_changes_strip.html",
        context,
    )


@app.post("/quick-capture")
async def quick_capture(request: Request):
    form = await request.form()
    raw = str(form.get("raw_text") or "").strip()
    if not raw:
        raise HTTPException(400, "Capture text is required")
    priority = max(1, min(_int(form.get("priority"), 3), 5))
    category = str(form.get("category") or "").strip().upper() or None
    assigned_to = str(form.get("assigned_to") or "").strip() or None
    waiting_on = str(form.get("waiting_on") or "").strip() or None
    next_action = str(form.get("next_action") or "").strip() or None
    follow_up_at = str(form.get("follow_up_at") or "").strip()
    requested_type = str(form.get("item_type") or "INBOX").strip().upper()
    item_type = requested_type if requested_type in CAPTURE_TYPES else "INBOX"
    title = str(form.get("title") or "").strip() or _capture_title(raw)
    if len(title) > 180:
        title = title[:177].rstrip() + "..."

    execute(
        """
        INSERT INTO issues(
          title,description,category,priority,status,source,municipality,assigned_to,
          item_type,next_action,waiting_on,waiting_on_since,follow_up_at
        ) VALUES(
          %s,%s,%s,%s,'OPEN','QUICK_CAPTURE','Weehawken',%s,%s,%s,%s,
          CASE WHEN %s IS NULL THEN NULL ELSE now() END,
          NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York'
        )
        """,
        (
            title,
            raw,
            category,
            priority,
            assigned_to,
            item_type,
            next_action,
            waiting_on,
            waiting_on,
            follow_up_at,
        ),
    )
    target = _safe_return(str(form.get("return_to") or ""), "/inbox")
    joiner = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{joiner}msg=Captured", status_code=303)


@app.get("/inbox", response_class=HTMLResponse)
def executive_inbox(request: Request, msg: str = ""):
    rows = query_all(
        """
        SELECT id,title,description,priority,category,assigned_to,next_action,waiting_on,
               follow_up_at AT TIME ZONE 'America/New_York' AS follow_up_local,
               created_at,updated_at
        FROM issues
        WHERE source='QUICK_CAPTURE'
          AND item_type='INBOX'
          AND status NOT IN ('RESOLVED','CLOSED')
        ORDER BY created_at ASC
        LIMIT 300
        """
    )
    return templates.TemplateResponse(
        request=request,
        name="executive_inbox.html",
        context={
            "request": request,
            "rows": rows,
            "msg": msg,
            "triage_types": TRIAGE_TYPES,
            "page": "inbox",
        },
    )


@app.post("/inbox/{issue_id}/triage")
async def executive_inbox_triage(issue_id: uuid.UUID, request: Request):
    form = await request.form()
    item_type = str(form.get("item_type") or "ISSUE").upper().strip()
    if item_type not in TRIAGE_TYPES:
        raise HTTPException(400, "Invalid triage type")
    title = str(form.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Title is required")
    category = str(form.get("category") or "").strip().upper() or None
    assigned_to = str(form.get("assigned_to") or "").strip() or None
    next_action = str(form.get("next_action") or "").strip() or None
    waiting_on = str(form.get("waiting_on") or "").strip() or None
    follow_up_at = str(form.get("follow_up_at") or "").strip()
    priority = max(1, min(_int(form.get("priority"), 3), 5))

    execute(
        """
        UPDATE issues
        SET title=%s,item_type=%s,category=%s,priority=%s,assigned_to=%s,next_action=%s,
            waiting_on=%s,
            waiting_on_since=CASE
              WHEN %s IS NULL THEN NULL
              WHEN waiting_on_since IS NULL OR waiting_on IS DISTINCT FROM %s THEN now()
              ELSE waiting_on_since
            END,
            follow_up_at=NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',
            updated_at=now()
        WHERE id=%s AND source='QUICK_CAPTURE' AND item_type='INBOX'
        """,
        (
            title,
            item_type,
            category,
            priority,
            assigned_to,
            next_action,
            waiting_on,
            waiting_on,
            waiting_on,
            follow_up_at,
            issue_id,
        ),
    )
    return RedirectResponse(url="/inbox?msg=Triaged", status_code=303)


@app.post("/inbox/{issue_id}/dismiss")
def executive_inbox_dismiss(issue_id: uuid.UUID):
    execute(
        """
        UPDATE issues
        SET status='CLOSED',closed_at=now(),updated_at=now()
        WHERE id=%s AND source='QUICK_CAPTURE' AND item_type='INBOX'
        """,
        (issue_id,),
    )
    return RedirectResponse(url="/inbox?msg=Dismissed", status_code=303)


@app.post("/issues/{issue_id}/quick-waiting")
async def quick_waiting(issue_id: uuid.UUID, request: Request):
    form = await request.form()
    waiting_on = str(form.get("waiting_on") or "").strip()
    if not waiting_on:
        raise HTTPException(400, "Waiting On is required")
    follow_up_at = str(form.get("follow_up_at") or "").strip()
    execute(
        """
        UPDATE issues
        SET waiting_on=%s,
            waiting_on_since=CASE WHEN waiting_on IS DISTINCT FROM %s OR waiting_on_since IS NULL THEN now() ELSE waiting_on_since END,
            follow_up_at=NULLIF(%s,'')::timestamp AT TIME ZONE 'America/New_York',
            updated_at=now()
        WHERE id=%s AND status NOT IN ('RESOLVED','CLOSED')
        """,
        (waiting_on, waiting_on, follow_up_at, issue_id),
    )
    target = _safe_return(str(form.get("return_to") or ""), "/issues?state=waiting")
    joiner = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{joiner}msg=Waiting+On+set", status_code=303)


@app.get("/what-changed", response_class=HTMLResponse)
def what_changed(request: Request, msg: str = ""):
    context = _change_window(request)
    return templates.TemplateResponse(
        request=request,
        name="executive_changes.html",
        context={"request": request, **context, "msg": msg, "page": "what-changed"},
    )


@app.post("/what-changed/mark-reviewed")
def what_changed_mark_reviewed(request: Request):
    user = _username(request)
    execute(
        """
        INSERT INTO executive_review_state(username,last_reviewed_at,updated_at)
        VALUES (%s,now(),now())
        ON CONFLICT (username) DO UPDATE
        SET last_reviewed_at=EXCLUDED.last_reviewed_at,updated_at=now()
        """,
        (user,),
    )
    return RedirectResponse(url="/what-changed?msg=Review+checkpoint+saved", status_code=303)


@app.get("/today-board/setup", response_class=HTMLResponse)
def today_board_setup(request: Request, msg: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="today_board_wizard.html",
        context={
            "request": request,
            "rules": _wizard_rows(),
            "msg": msg,
            "page": "today-board",
        },
    )


@app.post("/today-board/setup/save")
async def today_board_setup_save(request: Request):
    form = await request.form()
    rule_key = str(form.get("rule_key") or "").strip().upper()
    if rule_key not in WIZARD_KEYS:
        raise HTTPException(400, "Unsupported starter rule")
    source_label = str(form.get("source_label") or "").strip() or None
    notes = str(form.get("notes") or "").strip() or None

    if rule_key in {"FIRE_DUTY_GROUP", "POLICE_DUTY_SQUADS"}:
        anchor_date = str(form.get("anchor_date") or "").strip()
        sequence = _parse_cycle_lines(str(form.get("cycle_sequence") or ""))
        if not anchor_date or not sequence:
            raise HTTPException(400, "Rotation requires an anchor date and sequence")
        day_boundary = str(form.get("day_boundary") or "00:00").strip() or "00:00"
        config = {
            "anchor_date": anchor_date,
            "sequence": sequence,
            "day_boundary": day_boundary,
            "setup_value": "SET ROTATION",
            "needs_setup": False,
        }
        rule_type = "CYCLE"
    else:
        days = [day for day in range(1, 8) if str(form.get(f"day_{day}") or "")]
        if not days:
            raise HTTPException(400, "Choose at least one weekday")
        config = {
            "days": days,
            "active_value": str(form.get("active_value") or "YES").strip() or "YES",
            "inactive_value": str(form.get("inactive_value") or "NO").strip() or "NO",
            "detail": str(form.get("detail") or "").strip() or None,
            "day_boundary": str(form.get("day_boundary") or "00:00").strip() or "00:00",
        }
        config = {k: v for k, v in config.items() if v is not None}
        rule_type = "WEEKDAY"

    execute(
        """
        UPDATE daily_constant_rules
        SET rule_type=%s,config=%s::jsonb,source_label=%s,notes=%s,
            active=true,pinned=true,updated_at=now()
        WHERE rule_key=%s
        """,
        (rule_type, json.dumps(config), source_label, notes, rule_key),
    )
    return RedirectResponse(
        url=f"/today-board/setup?msg={quote_plus('Saved ' + rule_key.replace('_', ' ').title())}",
        status_code=303,
    )
