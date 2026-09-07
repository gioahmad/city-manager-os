from pathlib import Path
import sys

sys.path.insert(
    0,
    str(
        Path(__file__)
        .resolve()
        .parents[1]
    ),
)

from datetime import (
    datetime,
    timedelta,
    timezone,
)

from attention_engine import (
    dependency_type,
    evaluate_issue_attention,
    evaluate_waiting,
)


NOW = datetime(
    2026,
    9,
    7,
    16,
    0,
    tzinfo=timezone.utc,
)


def test_external_dependency_classification():
    assert (
        dependency_type(
            "NJDOT"
        )
        == "EXTERNAL"
    )

    assert (
        dependency_type(
            "PSEG engineering"
        )
        == "EXTERNAL"
    )

    assert (
        dependency_type(
            "DPW supervisor"
        )
        == "INTERNAL"
    )


def test_external_wait_becomes_chase_today():
    issue = {
        "waiting_on":
            "NJDOT",
        "waiting_on_since":
            NOW
            - timedelta(
                days=8
            ),
        "waiting_on_chase_count":
            0,
    }

    result = evaluate_waiting(
        issue,
        now=NOW,
    )

    assert (
        result[
            "waiting_status"
        ]
        == "CHASE TODAY"
    )

    assert (
        result[
            "waiting_age_days"
        ]
        == 8
    )


def test_internal_wait_becomes_chase_today():
    issue = {
        "waiting_on":
            "DPW supervisor",
        "waiting_on_since":
            NOW
            - timedelta(
                days=4
            ),
        "waiting_on_chase_count":
            0,
    }

    result = evaluate_waiting(
        issue,
        now=NOW,
    )

    assert (
        result[
            "waiting_status"
        ]
        == "CHASE TODAY"
    )


def test_chase_resets_wait_clock():
    issue = {
        "waiting_on":
            "Verizon",
        "waiting_on_since":
            NOW
            - timedelta(
                days=30
            ),
        "waiting_on_last_chased":
            NOW
            - timedelta(
                days=2
            ),
        "waiting_on_chase_count":
            1,
    }

    result = evaluate_waiting(
        issue,
        now=NOW,
    )

    assert (
        result[
            "waiting_status"
        ]
        == "WAITING AFTER CHASE"
    )

    assert (
        result[
            "waiting_age_days"
        ]
        == 2
    )


def test_three_chases_recommends_escalation():
    issue = {
        "waiting_on":
            "County",
        "waiting_on_since":
            NOW
            - timedelta(
                days=30
            ),
        "waiting_on_last_chased":
            NOW
            - timedelta(
                days=4
            ),
        "waiting_on_chase_count":
            3,
    }

    result = evaluate_waiting(
        issue,
        now=NOW,
    )

    assert (
        result[
            "waiting_status"
        ]
        == "ESCALATE"
    )


def test_overdue_p5_is_escalation():
    issue = {
        "priority":
            5,
        "item_type":
            "ISSUE",
        "assigned_to":
            "DPW",
        "next_action":
            "Repair",
        "due_at":
            NOW
            - timedelta(
                hours=3
            ),
    }

    result = (
        evaluate_issue_attention(
            issue,
            now=NOW,
        )
    )

    assert (
        result[
            "attention_level"
        ]
        == "ESCALATION"
    )

    assert (
        result[
            "needs_manager"
        ]
        is True
    )


def test_routine_future_issue_is_awareness():
    issue = {
        "priority":
            2,
        "item_type":
            "ISSUE",
        "assigned_to":
            "DPW",
        "next_action":
            "Inspect next week",
        "due_at":
            NOW
            + timedelta(
                days=10
            ),
    }

    result = (
        evaluate_issue_attention(
            issue,
            now=NOW,
        )
    )

    assert (
        result[
            "attention_level"
        ]
        == "AWARENESS"
    )

    assert (
        result[
            "needs_manager"
        ]
        is False
    )


def test_stale_external_followup_is_action():
    issue = {
        "priority":
            4,
        "item_type":
            "FOLLOW_UP",
        "assigned_to":
            "Manager",
        "next_action":
            "Get permit answer",
        "waiting_on":
            "Verizon",
        "waiting_on_since":
            NOW
            - timedelta(
                days=10
            ),
        "waiting_on_chase_count":
            0,
    }

    result = (
        evaluate_issue_attention(
            issue,
            now=NOW,
        )
    )

    assert (
        result[
            "attention_level"
        ]
        == "ACTION"
    )

    assert (
        result[
            "waiting_status"
        ]
        == "CHASE TODAY"
    )


def test_decision_always_manager_visible():
    issue = {
        "priority":
            1,
        "item_type":
            "DECISION",
        "assigned_to":
            "Manager",
        "next_action":
            "Review options",
        "decision_by":
            NOW
            + timedelta(
                days=10
            ),
    }

    result = (
        evaluate_issue_attention(
            issue,
            now=NOW,
        )
    )

    assert (
        result[
            "needs_manager"
        ]
        is True
    )
