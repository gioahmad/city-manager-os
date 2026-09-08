from datetime import datetime

from today_board_engine import EASTERN, next_change, resolve_rule


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=EASTERN)


def test_weekday_rule():
    config = {"days": [1, 4], "active_value": "GARBAGE TODAY", "inactive_value": "NO"}
    assert resolve_rule("WEEKDAY", config, dt("2026-09-07T12:00"))["value"] == "GARBAGE TODAY"
    assert resolve_rule("WEEKDAY", config, dt("2026-09-08T12:00"))["value"] == "NO"


def test_cycle_anchor_and_repeat():
    config = {
        "anchor_date": "2026-09-07",
        "sequence": [
            {"value": "Group 2"},
            {"value": "Group 3"},
            {"value": "Group 4"},
            {"value": "Group 1"},
        ],
    }
    assert resolve_rule("CYCLE", config, dt("2026-09-07T12:00"))["value"] == "Group 2"
    assert resolve_rule("CYCLE", config, dt("2026-09-11T12:00"))["value"] == "Group 2"


def test_cycle_day_boundary_uses_previous_service_date_before_shift_change():
    config = {
        "anchor_date": "2026-09-07",
        "day_boundary": "07:00",
        "sequence": [{"value": "A"}, {"value": "B"}],
    }
    assert resolve_rule("CYCLE", config, dt("2026-09-08T06:30"))["value"] == "A"
    assert resolve_rule("CYCLE", config, dt("2026-09-08T07:01"))["value"] == "B"


def test_cycle_without_anchor_is_explicit_setup_not_guess():
    result = resolve_rule(
        "CYCLE",
        {"sequence": [], "setup_value": "SET ROTATION", "needs_setup": True},
        dt("2026-09-07T12:00"),
    )
    assert result["value"] == "SET ROTATION"
    assert result["state"] == "SETUP"
    assert result["needs_setup"] is True


def test_last_weekday_pattern():
    config = {"weekday": 1, "ordinal": -1, "active_value": "YES", "inactive_value": "NO"}
    assert resolve_rule("DATE_PATTERN", config, dt("2026-09-28T12:00"))["value"] == "YES"
    assert resolve_rule("DATE_PATTERN", config, dt("2026-09-21T12:00"))["value"] == "NO"


def test_cross_year_season():
    config = {"start_mmdd": "11-01", "end_mmdd": "03-31", "active_value": "WINTER", "inactive_value": "OFF"}
    assert resolve_rule("SEASON", config, dt("2026-12-15T12:00"))["value"] == "WINTER"
    assert resolve_rule("SEASON", config, dt("2026-07-15T12:00"))["value"] == "OFF"


def test_override_wins_without_modifying_base_rule():
    result = resolve_rule(
        "WEEKDAY",
        {"days": [1], "active_value": "YES", "inactive_value": "NO"},
        dt("2026-09-07T12:00"),
        override={"value": {"value": "NO"}, "reason": "Holiday"},
    )
    assert result["value"] == "NO"
    assert result["is_override"] is True
    assert result["override_reason"] == "Holiday"


def test_derived_payload_and_stale_fallback():
    config = {"derived_key": "EMS_API", "unavailable_value": "SOURCE ERROR"}
    stale = resolve_rule("DERIVED", config, dt("2026-09-07T12:00"), derived=None)
    assert stale["state"] == "STALE"
    live = resolve_rule(
        "DERIVED",
        config,
        dt("2026-09-07T12:00"),
        derived={"value": "2 UNITS", "detail": "4 personnel", "state": "ACTIVE"},
    )
    assert live["value"] == "2 UNITS"
    assert live["detail"] == "4 personnel"


def test_next_change_scans_forward():
    config = {"days": [1], "active_value": "YES", "inactive_value": "NO"}
    change = next_change("WEEKDAY", config, dt("2026-09-07T12:00"))
    assert str(change["date"]) == "2026-09-08"
    assert change["value"] == "NO"
