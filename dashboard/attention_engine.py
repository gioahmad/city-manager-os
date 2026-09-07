from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


ATTENTION_LEVELS = (
    "AWARENESS",
    "WATCH",
    "ACTION",
    "ESCALATION",
)

_EXTERNAL_RE = re.compile(
    r"(county|njdot|verizon|pseg|utility|engineer|"
    r"attorney|contractor|vendor|state|federal|"
    r"consultant|agency|mayor|counsel|nj transit|"
    r"port authority)",
    re.I,
)


def _now(
    value: datetime | None = None,
) -> datetime:
    current = (
        value
        or datetime.now(
            timezone.utc
        )
    )

    if current.tzinfo is None:
        return current.replace(
            tzinfo=timezone.utc
        )

    return current


def _dt(
    value: Any,
) -> datetime | None:
    if not isinstance(
        value,
        datetime,
    ):
        return None

    if value.tzinfo is None:
        return value.replace(
            tzinfo=timezone.utc
        )

    return value


def dependency_type(
    waiting_on: str | None,
) -> str:
    text = (
        waiting_on
        or ""
    ).strip()

    if not text:
        return "NONE"

    return (
        "EXTERNAL"
        if _EXTERNAL_RE.search(
            text
        )
        else "INTERNAL"
    )


def evaluate_waiting(
    issue: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:

    current = _now(now)

    waiting_on = str(
        issue.get(
            "waiting_on"
        )
        or ""
    ).strip()

    if not waiting_on:
        return {
            "dependency_type":
                "NONE",
            "waiting_age_days":
                0,
            "waiting_status":
                "NOT WAITING",
            "waiting_reason":
                "",
            "chase_recommended":
                False,
        }

    dep_type = dependency_type(
        waiting_on
    )

    threshold_days = (
        7
        if dep_type == "EXTERNAL"
        else 3
    )

    waiting_since = (
        _dt(
            issue.get(
                "waiting_on_last_chased"
            )
        )
        or _dt(
            issue.get(
                "waiting_on_since"
            )
        )
        or _dt(
            issue.get(
                "updated_at"
            )
        )
        or _dt(
            issue.get(
                "created_at"
            )
        )
        or current
    )

    age_seconds = max(
        0.0,
        (
            current
            - waiting_since
        ).total_seconds(),
    )

    age_days = int(
        age_seconds
        // 86400
    )

    chase_count = int(
        issue.get(
            "waiting_on_chase_count"
        )
        or 0
    )

    if (
        chase_count >= 3
        and age_days >= max(
            1,
            threshold_days // 2,
        )
    ):
        status = "ESCALATE"

        reason = (
            f"{waiting_on}: "
            f"{chase_count} chases, "
            f"still waiting {age_days}d"
        )

        chase = True

    elif age_days >= threshold_days:

        status = (
            "CHASE TODAY"
            if chase_count == 0
            else "CHASE AGAIN"
        )

        reason = (
            f"{waiting_on}: "
            f"waiting {age_days}d"
        )

        chase = True

    elif chase_count > 0:

        status = (
            "WAITING AFTER CHASE"
        )

        reason = (
            f"{waiting_on}: "
            f"chased {chase_count}x, "
            f"{age_days}d since last chase"
        )

        chase = False

    else:
        status = "WAITING"

        reason = (
            f"{waiting_on}: "
            f"waiting {age_days}d"
        )

        chase = False

    return {
        "dependency_type":
            dep_type,
        "waiting_age_days":
            age_days,
        "waiting_status":
            status,
        "waiting_reason":
            reason,
        "chase_recommended":
            chase,
    }


def evaluate_issue_attention(
    issue: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:

    current = _now(now)

    score = 0

    reasons: list[
        tuple[int, str]
    ] = []

    priority = int(
        issue.get(
            "priority"
        )
        or 0
    )

    if priority >= 5:
        score += 33
        reasons.append(
            (
                33,
                "P5 priority",
            )
        )

    elif priority == 4:
        score += 20
        reasons.append(
            (
                20,
                "P4 priority",
            )
        )

    elif priority == 3:
        score += 10

    due_at = _dt(
        issue.get(
            "due_at"
        )
    )

    follow_up_at = _dt(
        issue.get(
            "follow_up_at"
        )
    )

    decision_by = _dt(
        issue.get(
            "decision_by"
        )
    )

    def seconds_until(
        value: datetime | None,
    ) -> float | None:

        if value is None:
            return None

        return (
            value
            - current
        ).total_seconds()

    due_seconds = seconds_until(
        due_at
    )

    follow_seconds = seconds_until(
        follow_up_at
    )

    decision_seconds = seconds_until(
        decision_by
    )

    if due_seconds is not None:

        if due_seconds < 0:
            score += 42
            reasons.append(
                (
                    42,
                    "overdue",
                )
            )

        elif due_seconds <= 86400:
            score += 24
            reasons.append(
                (
                    24,
                    "due within 24h",
                )
            )

        elif due_seconds <= 259200:
            score += 12
            reasons.append(
                (
                    12,
                    "due within 72h",
                )
            )

    if follow_seconds is not None:

        if follow_seconds < 0:
            score += 34
            reasons.append(
                (
                    34,
                    "follow-up overdue",
                )
            )

        elif follow_seconds <= 86400:
            score += 20
            reasons.append(
                (
                    20,
                    "follow-up within 24h",
                )
            )

    if decision_seconds is not None:

        if decision_seconds < 0:
            score += 40
            reasons.append(
                (
                    40,
                    "decision overdue",
                )
            )

        elif decision_seconds <= 86400:
            score += 24
            reasons.append(
                (
                    24,
                    "decision due within 24h",
                )
            )

        elif decision_seconds <= 259200:
            score += 12
            reasons.append(
                (
                    12,
                    "decision due within 72h",
                )
            )

    item_type = str(
        issue.get(
            "item_type"
        )
        or ""
    ).upper()

    if item_type == "DECISION":

        score += 15

        reasons.append(
            (
                15,
                "manager decision",
            )
        )

    elif (
        item_type == "COMMITMENT"
        and due_at is None
    ):

        score += 8

        reasons.append(
            (
                8,
                "commitment has no due date",
            )
        )

    if not str(
        issue.get(
            "next_action"
        )
        or ""
    ).strip():

        score += 14

        reasons.append(
            (
                14,
                "no next action",
            )
        )

    waiting = evaluate_waiting(
        issue,
        now=current,
    )

    waiting_status = waiting[
        "waiting_status"
    ]

    if waiting_status == "ESCALATE":

        score += 35

        reasons.append(
            (
                35,
                waiting[
                    "waiting_reason"
                ],
            )
        )

    elif waiting_status in {
        "CHASE TODAY",
        "CHASE AGAIN",
    }:

        score += 30

        reasons.append(
            (
                30,
                waiting[
                    "waiting_reason"
                ],
            )
        )

    elif (
        waiting_status
        == "WAITING AFTER CHASE"
    ):
        score += 8

    if (
        not str(
            issue.get(
                "assigned_to"
            )
            or ""
        ).strip()
        and (
            due_at is not None
            or follow_up_at is not None
            or item_type
            in {
                "DECISION",
                "COMMITMENT",
                "FOLLOW_UP",
            }
        )
    ):
        score += 10

        reasons.append(
            (
                10,
                "no owner",
            )
        )

    score = min(
        score,
        100,
    )

    if score >= 75:
        level = "ESCALATION"

    elif score >= 50:
        level = "ACTION"

    elif score >= 25:
        level = "WATCH"

    else:
        level = "AWARENESS"

    reasons.sort(
        key=lambda item:
            item[0],
        reverse=True,
    )

    reason_text = (
        " + ".join(
            text
            for _, text
            in reasons[:3]
        )
        or "routine open item"
    )

    return {
        "attention_score":
            score,
        "attention_level":
            level,
        "attention_reason":
            reason_text,
        "needs_manager":
            (
                level
                in {
                    "ACTION",
                    "ESCALATION",
                }
                or item_type
                == "DECISION"
            ),
        **waiting,
    }


def annotate_issue_rows(
    rows: list[
        dict[str, Any]
    ],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:

    current = _now(now)

    output: list[
        dict[str, Any]
    ] = []

    for row in rows:

        item = dict(
            row
        )

        item.update(
            evaluate_issue_attention(
                item,
                now=current,
            )
        )

        output.append(
            item
        )

    return output
