from __future__ import annotations

import uuid

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from app import (
    app,
    execute,
    query_one,
)


def _waiting_issue(
    issue_id: uuid.UUID,
):
    row = query_one(
        """
        SELECT
          id,
          waiting_on
        FROM issues
        WHERE id=%s
          AND status NOT IN (
            'RESOLVED',
            'CLOSED'
          )
        """,
        (
            issue_id,
        ),
    )

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Issue not found",
        )

    if not str(
        row.get(
            "waiting_on"
        )
        or ""
    ).strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Issue is not waiting "
                "on anyone"
            ),
        )

    return row


@app.post(
    "/issues/{issue_id}/waiting/chased"
)
def issue_waiting_chased(
    issue_id: uuid.UUID,
):
    _waiting_issue(
        issue_id
    )

    execute(
        """
        UPDATE issues
        SET
          waiting_on_last_chased=now(),
          waiting_on_chase_count=
            COALESCE(
              waiting_on_chase_count,
              0
            ) + 1,
          updated_at=now()
        WHERE id=%s
        """,
        (
            issue_id,
        ),
    )

    return RedirectResponse(
        url="/my-day",
        status_code=303,
    )


@app.post(
    "/issues/{issue_id}/waiting/response"
)
def issue_waiting_response(
    issue_id: uuid.UUID,
):
    _waiting_issue(
        issue_id
    )

    execute(
        """
        UPDATE issues
        SET
          waiting_on=NULL,
          updated_at=now()
        WHERE id=%s
        """,
        (
            issue_id,
        ),
    )

    return RedirectResponse(
        url="/my-day",
        status_code=303,
    )
