from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
RULE_TYPES = {"WEEKDAY", "CYCLE", "DATE_PATTERN", "SEASON", "MANUAL", "DERIVED"}


def _as_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _as_time(value: Any, default: time = time(0, 0)) -> time:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return time.fromisoformat(text)
    except ValueError:
        return default


def _payload(value: Any, default_detail: str | None = None) -> dict[str, Any]:
    if isinstance(value, dict):
        label = str(value.get("value") or value.get("label") or "").strip()
        detail = value.get("detail")
        return {
            "value": label,
            "detail": str(detail).strip() if detail not in (None, "") else default_detail,
            "meta": dict(value),
        }
    return {
        "value": str(value or "").strip(),
        "detail": default_detail,
        "meta": {},
    }


def _effective_service_date(target: datetime, config: dict[str, Any]) -> date:
    boundary = _as_time(config.get("day_boundary"), time(0, 0))
    service_date = target.date()
    if target.timetz().replace(tzinfo=None) < boundary:
        service_date -= timedelta(days=1)
    return service_date


def _date_pattern_matches(service_date: date, config: dict[str, Any]) -> bool:
    weekday = int(config.get("weekday") or 0)
    ordinal = int(config.get("ordinal") or 0)
    parity = str(config.get("week_parity") or "ANY").upper()

    if parity in {"ODD", "EVEN"}:
        week = service_date.isocalendar().week
        if parity == "ODD" and week % 2 == 0:
            return False
        if parity == "EVEN" and week % 2 == 1:
            return False

    if weekday and service_date.isoweekday() != weekday:
        return False
    if not ordinal:
        return True

    if ordinal > 0:
        occurrence = (service_date.day - 1) // 7 + 1
        return occurrence == ordinal

    # -1 means last matching weekday of the month.
    next_week = service_date + timedelta(days=7)
    return next_week.month != service_date.month


def _season_matches(service_date: date, config: dict[str, Any]) -> bool:
    start_text = str(config.get("start_mmdd") or "").strip()
    end_text = str(config.get("end_mmdd") or "").strip()
    if len(start_text) != 5 or len(end_text) != 5:
        return False
    current = service_date.strftime("%m-%d")
    if start_text <= end_text:
        return start_text <= current <= end_text
    return current >= start_text or current <= end_text


def resolve_rule(
    rule_type: str,
    config: dict[str, Any] | None,
    target: datetime,
    *,
    derived: dict[str, Any] | None = None,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one Daily Constant rule without touching the database."""
    rule_type = str(rule_type or "").upper()
    config = dict(config or {})
    if rule_type not in RULE_TYPES:
        raise ValueError(f"Unsupported Daily Constant rule type: {rule_type}")
    if target.tzinfo is None:
        target = target.replace(tzinfo=EASTERN)
    else:
        target = target.astimezone(EASTERN)

    service_date = _effective_service_date(target, config)

    if override:
        result = _payload(override.get("value"), override.get("detail"))
        result.update(
            {
                "state": "OVERRIDE",
                "service_date": service_date,
                "is_override": True,
                "override_reason": override.get("reason"),
                "needs_setup": False,
            }
        )
        return result

    needs_setup = bool(config.get("needs_setup"))
    detail = str(config.get("detail") or "").strip() or None

    if rule_type == "MANUAL":
        result = _payload(config.get("value"), detail)
        state = "SETUP" if needs_setup else "ACTIVE"

    elif rule_type == "WEEKDAY":
        days = {int(x) for x in (config.get("days") or []) if str(x).isdigit()}
        active = service_date.isoweekday() in days
        result = _payload(
            config.get("active_value", "YES") if active else config.get("inactive_value", "NO"),
            detail,
        )
        state = "ACTIVE" if active else "INACTIVE"

    elif rule_type == "CYCLE":
        anchor = _as_date(config.get("anchor_date"))
        sequence = list(config.get("sequence") or [])
        if not anchor or not sequence:
            result = _payload(config.get("setup_value", "SET ROTATION"), detail)
            state = "SETUP"
            needs_setup = True
        else:
            index = (service_date - anchor).days % len(sequence)
            result = _payload(sequence[index], detail)
            result["cycle_index"] = index
            state = "ACTIVE"

    elif rule_type == "DATE_PATTERN":
        active = _date_pattern_matches(service_date, config)
        result = _payload(
            config.get("active_value", "YES") if active else config.get("inactive_value", "NO"),
            detail,
        )
        state = "ACTIVE" if active else "INACTIVE"

    elif rule_type == "SEASON":
        active = _season_matches(service_date, config)
        result = _payload(
            config.get("active_value", "YES") if active else config.get("inactive_value", "NO"),
            detail,
        )
        state = "ACTIVE" if active else "INACTIVE"

    else:  # DERIVED
        if derived is None:
            result = _payload(config.get("unavailable_value", "UNAVAILABLE"), detail)
            state = "STALE"
        else:
            result = _payload(derived.get("value"), derived.get("detail") or detail)
            result["meta"].update(derived.get("meta") or {})
            state = str(derived.get("state") or "ACTIVE").upper()
            needs_setup = bool(derived.get("needs_setup", False))

    result.update(
        {
            "state": state,
            "service_date": service_date,
            "is_override": False,
            "override_reason": None,
            "needs_setup": needs_setup,
        }
    )
    return result


def next_change(
    rule_type: str,
    config: dict[str, Any] | None,
    target: datetime,
    *,
    derived_by_date: dict[date, dict[str, Any]] | None = None,
    days: int = 35,
) -> dict[str, Any] | None:
    """Find the next date whose resolved display value differs."""
    current = resolve_rule(
        rule_type,
        config,
        target,
        derived=(derived_by_date or {}).get(target.date()),
    )
    current_value = current.get("value")
    for offset in range(1, max(2, days + 1)):
        probe = target + timedelta(days=offset)
        result = resolve_rule(
            rule_type,
            config,
            probe,
            derived=(derived_by_date or {}).get(probe.date()),
        )
        if result.get("value") != current_value:
            return {"date": result["service_date"], "value": result.get("value")}
    return None
