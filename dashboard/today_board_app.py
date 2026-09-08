from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import operations_app as operations_module
import operations_routines_app as routines_module
from app import app, db_conn, execute, query_all, query_one, templates
from today_board_engine import EASTERN, RULE_TYPES, next_change, resolve_rule

CATEGORIES = [
    "PUBLIC_SAFETY",
    "SANITATION_DPW",
    "SCHOOLS",
    "PARKING_TRAFFIC",
    "BUILDINGS_PARKS",
    "EVENTS",
    "OPERATIONS",
    "OTHER",
]
DERIVED_KEYS = ["EVENTS_TODAY", "EVENT_WATCHES_TODAY", "OPERATIONS_EXCEPTIONS"]


def _remove_get(path: str) -> None:
    app.router.routes = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", None) == path
            and "GET" in (getattr(route, "methods", set()) or set())
        )
    ]


def _json_obj(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    return dict(value)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")[:48] or "CONSTANT"


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
    lines = []
    for item in config.get("sequence") or []:
        if isinstance(item, dict):
            value = str(item.get("value") or item.get("label") or "").strip()
            detail = str(item.get("detail") or "").strip()
            lines.append(value + (" | " + detail if detail else ""))
        elif str(item).strip():
            lines.append(str(item).strip())
    return "\n".join(lines)


def _rule_rows() -> list[dict[str, Any]]:
    rows = query_all(
        """
        SELECT *
        FROM daily_constant_rules
        ORDER BY pinned DESC,sort_order,category,name
        """
    )
    for row in rows:
        row["config"] = _json_obj(row.get("config"))
        row["cycle_text"] = _cycle_text(row["config"])
        row["days_text"] = ",".join(str(x) for x in row["config"].get("days") or [])
    return rows


def _override_for(rule_id: Any, target: datetime) -> dict[str, Any] | None:
    row = query_one(
        """
        SELECT id,value,detail,reason,starts_at,ends_at,created_by,created_at
        FROM daily_constant_overrides
        WHERE rule_id=%s
          AND starts_at <= %s
          AND ends_at > %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (rule_id, target, target),
    )
    return dict(row) if row else None


def _derived_value(key: str, service_date: date) -> dict[str, Any] | None:
    key = str(key or "").upper()
    start_local = datetime.combine(service_date, time.min, tzinfo=EASTERN)
    end_local = start_local + timedelta(days=1)

    if key in {"EVENTS_TODAY", "EVENT_WATCHES_TODAY"}:
        extra = ""
        if key == "EVENT_WATCHES_TODAY":
            extra = "AND impact_level IN ('WATCH','ALERT')"
        row = query_one(
            f"""
            SELECT count(*) AS n,
                   count(*) FILTER (WHERE impact_level='ALERT') AS alerts,
                   count(*) FILTER (WHERE impact_level='WATCH') AS watches
            FROM event_intelligence
            WHERE active=true
              AND COALESCE(ends_at,starts_at + interval '2 hours') >= %s
              AND starts_at < %s
              {extra}
            """,
            (start_local, end_local),
        )
        count = int(row.get("n") or 0)
        detail = None
        if key == "EVENT_WATCHES_TODAY":
            detail = f"{int(row.get('watches') or 0)} watch · {int(row.get('alerts') or 0)} alert"
        return {"value": str(count), "detail": detail, "state": "ACTIVE", "meta": dict(row)}

    if key == "OPERATIONS_EXCEPTIONS":
        row = query_one(
            """
            SELECT count(*) AS n
            FROM operations_routine_runs
            WHERE service_date=%s
              AND status IN ('MISSED','EXCEPTION','NEEDS_HELP')
            """,
            (service_date,),
        )
        count = int(row.get("n") or 0)
        return {
            "value": str(count),
            "detail": "missed / exception / needs help" if count else "No recurring-operations exceptions",
            "state": "ACTIVE",
            "meta": dict(row),
        }

    return None


def _resolve_board(target: datetime) -> list[dict[str, Any]]:
    rows = [row for row in _rule_rows() if row.get("active")]
    output: list[dict[str, Any]] = []
    for row in rows:
        config = row["config"]
        derived = None
        if row["rule_type"] == "DERIVED":
            derived = _derived_value(str(config.get("derived_key") or ""), target.date())
        override = _override_for(row["id"], target)
        resolved = resolve_rule(
            row["rule_type"],
            config,
            target,
            derived=derived,
            override=override,
        )
        next_item = None
        if row["rule_type"] != "DERIVED":
            next_item = next_change(row["rule_type"], config, target, days=35)
        output.append(
            {
                **row,
                "resolved": resolved,
                "next": next_item,
            }
        )
    return output


def _board_context() -> dict[str, Any]:
    now = datetime.now(EASTERN)
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time(12, 0), tzinfo=EASTERN)
    return {
        "today_board": _resolve_board(now),
        "tomorrow_board": _resolve_board(tomorrow),
        "today_label": now.strftime("%A, %B %-d"),
        "tomorrow_label": tomorrow.strftime("%A, %B %-d"),
        "today_date": now.date(),
        "tomorrow_date": tomorrow.date(),
    }


def _inject(response, marker: str, context: dict[str, Any]) -> HTMLResponse:
    body = response.body.decode("utf-8")
    if marker not in body:
        raise RuntimeError(f"Today Board injection marker not found: {marker}")
    partial = templates.get_template("today_board_strip.html").render(**context)
    body = body.replace(marker, partial + "\n" + marker, 1)
    headers = dict(response.headers)
    headers.pop("content-length", None)
    return HTMLResponse(content=body, status_code=response.status_code, headers=headers)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bool(form: Any, key: str) -> bool:
    return key in form


def _build_config(form: Any, rule_type: str) -> dict[str, Any]:
    detail = str(form.get("detail") or "").strip() or None
    day_boundary = str(form.get("day_boundary") or "00:00").strip() or "00:00"
    active_value = str(form.get("active_value") or "YES").strip() or "YES"
    inactive_value = str(form.get("inactive_value") or "NO").strip() or "NO"
    config: dict[str, Any] = {"detail": detail, "day_boundary": day_boundary}

    if rule_type == "MANUAL":
        config["value"] = str(form.get("manual_value") or "").strip()
        config["needs_setup"] = _bool(form, "needs_setup")
    elif rule_type == "WEEKDAY":
        days = sorted({int(x) for x in re.findall(r"[1-7]", str(form.get("days_text") or ""))})
        config.update({"days": days, "active_value": active_value, "inactive_value": inactive_value})
    elif rule_type == "CYCLE":
        sequence = _parse_cycle_lines(str(form.get("cycle_sequence") or ""))
        anchor_date = str(form.get("anchor_date") or "").strip()
        config.update(
            {
                "anchor_date": anchor_date,
                "sequence": sequence,
                "setup_value": "SET ROTATION",
                "needs_setup": not bool(anchor_date and sequence),
            }
        )
    elif rule_type == "DATE_PATTERN":
        config.update(
            {
                "weekday": _int(form.get("pattern_weekday"), 0),
                "ordinal": _int(form.get("pattern_ordinal"), 0),
                "week_parity": str(form.get("week_parity") or "ANY").upper(),
                "active_value": active_value,
                "inactive_value": inactive_value,
            }
        )
    elif rule_type == "SEASON":
        config.update(
            {
                "start_mmdd": str(form.get("start_mmdd") or "").strip(),
                "end_mmdd": str(form.get("end_mmdd") or "").strip(),
                "active_value": active_value,
                "inactive_value": inactive_value,
            }
        )
    elif rule_type == "DERIVED":
        config.update(
            {
                "derived_key": str(form.get("derived_key") or "").strip().upper(),
                "unavailable_value": "UNAVAILABLE",
            }
        )
    return {k: v for k, v in config.items() if v is not None}


def _selected_rule(rule_id: str) -> dict[str, Any] | None:
    if not rule_id:
        return None
    try:
        uid = uuid.UUID(rule_id)
    except ValueError:
        return None
    row = query_one("SELECT * FROM daily_constant_rules WHERE id=%s", (uid,))
    if not row:
        return None
    row = dict(row)
    row["config"] = _json_obj(row.get("config"))
    row["cycle_text"] = _cycle_text(row["config"])
    row["days_text"] = ",".join(str(x) for x in row["config"].get("days") or [])
    return row


# Replace the existing GET renderers only. Their underlying functions remain reusable.
_remove_get("/my-day")
_remove_get("/")


@app.get("/my-day", response_class=HTMLResponse)
def today_board_my_day(request: Request):
    response = routines_module.phase3_my_day(request)
    context = {"request": request, **_board_context(), "board_compact": False}
    return _inject(response, '<section class="section-label"><span>DAILY BRIEF', context)


@app.get("/", response_class=HTMLResponse)
def today_board_overview(request: Request):
    response = operations_module.operations_home(request)
    context = {"request": request, **_board_context(), "board_compact": True}
    return _inject(response, "<main>", context)


@app.get("/today-board", response_class=HTMLResponse)
def today_board_admin(request: Request, edit: str = "", test_date: str = "", msg: str = ""):
    now = datetime.now(EASTERN)
    try:
        probe_date = date.fromisoformat(test_date) if test_date else now.date()
    except ValueError:
        probe_date = now.date()
    probe = datetime.combine(probe_date, time(12, 0), tzinfo=EASTERN)
    overrides = query_all(
        """
        SELECT o.*,r.name AS rule_name
        FROM daily_constant_overrides o
        JOIN daily_constant_rules r ON r.id=o.rule_id
        WHERE o.ends_at > now() - interval '7 days'
        ORDER BY o.starts_at DESC
        LIMIT 100
        """
    )
    return templates.TemplateResponse(
        request=request,
        name="today_board.html",
        context={
            "request": request,
            **_board_context(),
            "rules": _rule_rows(),
            "selected": _selected_rule(edit),
            "test_date": probe_date,
            "test_board": _resolve_board(probe),
            "overrides": overrides,
            "categories": CATEGORIES,
            "rule_types": sorted(RULE_TYPES),
            "derived_keys": DERIVED_KEYS,
            "msg": msg,
            "page": "today-board",
        },
    )


@app.post("/today-board/save")
async def today_board_save(request: Request):
    form = await request.form()
    raw_id = str(form.get("id") or "").strip()
    name = str(form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    rule_type = str(form.get("rule_type") or "MANUAL").upper()
    if rule_type not in RULE_TYPES:
        raise HTTPException(400, "Invalid rule type")
    category = str(form.get("category") or "OTHER").upper()
    if category not in CATEGORIES:
        category = "OTHER"
    config = _build_config(form, rule_type)
    pinned = _bool(form, "pinned")
    active = _bool(form, "active")
    sort_order = max(0, min(_int(form.get("sort_order"), 100), 10000))
    source_label = str(form.get("source_label") or "").strip() or None
    notes = str(form.get("notes") or "").strip() or None

    if raw_id:
        try:
            rule_id = uuid.UUID(raw_id)
        except ValueError as exc:
            raise HTTPException(400, "Invalid rule id") from exc
        execute(
            """
            UPDATE daily_constant_rules
            SET name=%s,category=%s,rule_type=%s,active=%s,pinned=%s,
                sort_order=%s,config=%s::jsonb,source_label=%s,notes=%s,updated_at=now()
            WHERE id=%s
            """,
            (name, category, rule_type, active, pinned, sort_order, json.dumps(config), source_label, notes, rule_id),
        )
    else:
        rule_key = str(form.get("rule_key") or "").strip().upper() or _slug(name)
        execute(
            """
            INSERT INTO daily_constant_rules(
              rule_key,name,category,rule_type,active,pinned,sort_order,config,source_label,notes
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            """,
            (rule_key, name, category, rule_type, active, pinned, sort_order, json.dumps(config), source_label, notes),
        )
    return RedirectResponse(url="/today-board?msg=Rule+saved", status_code=303)


@app.post("/today-board/{rule_id}/toggle")
def today_board_toggle(rule_id: uuid.UUID):
    execute("UPDATE daily_constant_rules SET active=NOT active,updated_at=now() WHERE id=%s", (rule_id,))
    return RedirectResponse(url="/today-board?msg=Rule+status+updated", status_code=303)


@app.post("/today-board/{rule_id}/override")
async def today_board_override(request: Request, rule_id: uuid.UUID):
    form = await request.form()
    starts = str(form.get("starts_at") or "").strip()
    ends = str(form.get("ends_at") or "").strip()
    value = str(form.get("value") or "").strip()
    detail = str(form.get("detail") or "").strip() or None
    reason = str(form.get("reason") or "").strip()
    if not starts or not ends or not value or len(reason) < 3:
        raise HTTPException(400, "Override requires start, end, value and reason")
    actor = getattr(request.state, "cmos_user", None) or "unknown"
    execute(
        """
        INSERT INTO daily_constant_overrides(rule_id,starts_at,ends_at,value,detail,reason,created_by)
        VALUES (
          %s,
          %s::timestamp AT TIME ZONE 'America/New_York',
          %s::timestamp AT TIME ZONE 'America/New_York',
          %s::jsonb,%s,%s,%s
        )
        """,
        (rule_id, starts, ends, json.dumps({"value": value}), detail, reason, actor),
    )
    return RedirectResponse(url="/today-board?msg=Override+saved", status_code=303)


@app.post("/today-board/override/{override_id}/delete")
def today_board_override_delete(override_id: uuid.UUID):
    execute("DELETE FROM daily_constant_overrides WHERE id=%s", (override_id,))
    return RedirectResponse(url="/today-board?msg=Override+deleted", status_code=303)


@app.get("/today-board/api/summary")
def today_board_api():
    context = _board_context()
    def compact(rows):
        return [
            {
                "key": row["rule_key"],
                "name": row["name"],
                "category": row["category"],
                "value": row["resolved"].get("value"),
                "detail": row["resolved"].get("detail"),
                "state": row["resolved"].get("state"),
                "override": row["resolved"].get("is_override"),
            }
            for row in rows
        ]
    return {
        "today": str(context["today_date"]),
        "tomorrow": str(context["tomorrow_date"]),
        "today_items": compact(context["today_board"]),
        "tomorrow_items": compact(context["tomorrow_board"]),
    }


@app.get("/today-board/summary.txt", response_class=PlainTextResponse)
def today_board_summary_text():
    context = _board_context()
    lines = ["TODAY BOARD " + str(context["today_date"])]
    for row in context["today_board"]:
        if row["pinned"] or row["category"] in {"PUBLIC_SAFETY", "SANITATION_DPW"}:
            lines.append(f"{row['name']}: {row['resolved'].get('value') or '-'}")
    return "\n".join(lines) + "\n"
