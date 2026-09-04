import uuid
from datetime import date

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import operations_routines_app as routines
from app import app, execute, query_all, query_one, templates


def _date_or_none(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date.")


def _safe_return_to(value, fallback="/operations-routines"):
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return fallback
    return value


def _today_operations_with_notes():
    return query_all(
        """
        SELECT
          rr.id AS run_id, rr.service_date,
          rr.scheduled_for AT TIME ZONE 'America/New_York' AS scheduled_local,
          rr.due_at AT TIME ZONE 'America/New_York' AS due_local,
          rr.status, rr.acknowledged_at, rr.acknowledged_by,
          rr.exception_note, rr.exception_issue_id, rr.issue_id,
          rr.run_note, rr.run_note_by,
          rr.run_note_at AT TIME ZONE 'America/New_York' AS run_note_local,
          r.id AS routine_id, r.name, r.routine_kind, r.department,
          r.priority, r.confirmation_required, r.escalate_if_missed,
          r.verification_required, r.location_label,
          r.starts_on, r.ends_on,
          sl.name AS managed_location,
          wt.name AS work_type_name,
          e.full_name AS employee_name,
          i.status AS issue_status, i.help_reason,
          i.verification_pending
        FROM operations_routine_runs rr
        JOIN operations_routines r ON r.id=rr.routine_id
        LEFT JOIN staff_locations sl ON sl.id=r.location_id
        LEFT JOIN staff_work_types wt ON wt.id=r.work_type_id
        LEFT JOIN staff_employees e ON e.id=r.assigned_employee_id
        LEFT JOIN issues i ON i.id=rr.issue_id
        WHERE rr.service_date=(now() AT TIME ZONE 'America/New_York')::date
          AND r.active=true
          AND (r.starts_on IS NULL OR rr.service_date >= r.starts_on)
          AND (r.ends_on IS NULL OR rr.service_date <= r.ends_on)
        ORDER BY rr.scheduled_for,r.priority DESC,r.name
        """
    )


# Make every Phase 3 view use the occurrence-aware timeline.
routines._today_operations = _today_operations_with_notes


# Wrap the setup page so date-window and today-note controls are added without
# replacing the existing Phase 3 routine editor.
routines.remove_existing_get("/operations-routines")


@app.get("/operations-routines", response_class=HTMLResponse)
def operations_routines_occurrence_page(request: Request, msg: str = ""):
    response = routines.operations_routines_page(request=request, msg=msg)
    context = dict(response.context)
    context["today_operations"] = _today_operations_with_notes()
    return routines._render_injected(
        response,
        "operations_occurrence_controls.html",
        context,
        "<footer>",
    )


@app.post("/operations-routines/{routine_id}/window")
def routine_window(
    routine_id: uuid.UUID,
    starts_on: str = Form(""),
    ends_on: str = Form(""),
):
    start = _date_or_none(starts_on)
    end = _date_or_none(ends_on)
    if start and end and end < start:
        raise HTTPException(status_code=400, detail="End date cannot be before start date.")

    exists = query_one("SELECT id FROM operations_routines WHERE id=%s", (routine_id,))
    if not exists:
        raise HTTPException(status_code=404)

    execute(
        """
        UPDATE operations_routines
        SET starts_on=%s,ends_on=%s,updated_at=now()
        WHERE id=%s
        """,
        (start, end, routine_id),
    )

    # Remove only untouched generated occurrences that are now outside the
    # window. Historical, acknowledged, exception and work-linked runs stay.
    execute(
        """
        DELETE FROM operations_routine_runs rr
        USING operations_routines r
        WHERE rr.routine_id=r.id
          AND r.id=%s
          AND rr.service_date >= (now() AT TIME ZONE 'America/New_York')::date
          AND rr.issue_id IS NULL
          AND rr.acknowledged_at IS NULL
          AND rr.exception_note IS NULL
          AND (
            (r.starts_on IS NOT NULL AND rr.service_date < r.starts_on)
            OR
            (r.ends_on IS NOT NULL AND rr.service_date > r.ends_on)
          )
        """,
        (routine_id,),
    )

    return RedirectResponse(
        url="/operations-routines?msg=Routine+date+window+updated",
        status_code=303,
    )


@app.post("/operations-runs/{run_id}/note")
def run_note(
    run_id: uuid.UUID,
    note: str = Form(""),
    return_to: str = Form("/operations-routines"),
):
    run = query_one(
        """
        SELECT rr.id,rr.issue_id,r.name
        FROM operations_routine_runs rr
        JOIN operations_routines r ON r.id=rr.routine_id
        WHERE rr.id=%s
        """,
        (run_id,),
    )
    if not run:
        raise HTTPException(status_code=404)

    clean_note = note.strip() or None
    execute(
        """
        UPDATE operations_routine_runs
        SET run_note=%s,
            run_note_by=CASE WHEN %s IS NULL THEN NULL ELSE 'Supervisor' END,
            run_note_at=CASE WHEN %s IS NULL THEN NULL ELSE now() END,
            updated_at=now()
        WHERE id=%s
        """,
        (clean_note, clean_note, clean_note, run_id),
    )

    if run["issue_id"]:
        activity = (
            "Occurrence note: " + clean_note
            if clean_note
            else "Occurrence note cleared."
        )
        execute(
            "INSERT INTO issue_updates(issue_id,author,note) VALUES (%s,'Supervisor',%s)",
            (run["issue_id"], activity),
        )

    return RedirectResponse(
        url=_safe_return_to(return_to),
        status_code=303,
    )
