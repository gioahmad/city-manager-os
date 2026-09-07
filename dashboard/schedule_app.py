import json
import uuid
from datetime import datetime, timedelta

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from operations_app import app, operations_home
import rules_app  # registers Rules Center routes
import staff_admin_app  # registers Staff Admin routes
from app import execute, query_all, query_one, templates
from attention_engine import annotate_issue_rows


def _parse_event_checklist(raw: str) -> str:
    """Convert simple [ ] / [x] lines into compact event checklist JSON."""
    items = []

    for raw_line in (raw or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        done = False
        label = line

        lower = line.lower()

        if lower.startswith("[x]"):
            done = True
            label = line[3:].strip()
        elif lower.startswith("[ ]"):
            label = line[3:].strip()
        elif lower.startswith("- [x]"):
            done = True
            label = line[5:].strip()
        elif lower.startswith("- [ ]"):
            label = line[5:].strip()
        elif line.startswith("-"):
            label = line[1:].strip()

        if label:
            items.append(
                {
                    "label": label[:500],
                    "done": done,
                }
            )

    return json.dumps(items)




@app.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, state: str = "upcoming", q: str = "", msg: str = ""):
    where = []
    params = []

    if state == "upcoming":
        where.append("active = true AND event_status NOT IN ('COMPLETED','CANCELLED') AND COALESCE(ends_at, starts_at + interval '2 hours') >= now()")
    elif state == "week":
        where.append("""
            active = true
            AND event_status NOT IN ('COMPLETED','CANCELLED')
            AND starts_at >= now()
            AND starts_at < now() + interval '7 days'
        """)
    elif state == "needs-prep":
        where.append("""
            active = true
            AND event_status NOT IN ('COMPLETED','CANCELLED')
            AND COALESCE(ends_at, starts_at + interval '2 hours') >= now()
            AND preparation_status IN ('NOT_STARTED','IN_PROGRESS')
        """)
    elif state == "awaiting":
        where.append("""
            active = true
            AND event_status NOT IN ('COMPLETED','CANCELLED')
            AND COALESCE(ends_at, starts_at + interval '2 hours') >= now()
            AND confirmation_status IN ('AWAITING_CONFIRMATION','TENTATIVE')
        """)
    elif state == "completed":
        where.append("event_status = 'COMPLETED'")
    elif state == "past":
        where.append("COALESCE(ends_at, starts_at + interval '2 hours') < now()")
    elif state == "inactive":
        where.append("active = false")

    if q.strip():
        needle = f"%{q.strip()}%"
        where.append("""
            (
              title ILIKE %s
              OR category ILIKE %s
              OR location_name ILIKE %s
              OR address ILIKE %s
              OR municipality ILIKE %s
              OR notes ILIKE %s
              OR owner ILIKE %s
              OR waiting_on ILIKE %s
              OR agencies_involved ILIKE %s
              OR attendees ILIKE %s
            )
        """)
        params.extend([needle] * 10)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = query_all(
        f"""
        SELECT id, active, title, category, location_name, address, municipality,
               starts_at AT TIME ZONE 'America/New_York' AS starts_local,
               ends_at AT TIME ZONE 'America/New_York' AS ends_local,
               priority, source, notes,
               attendees, objective, prep_notes, decisions_needed, debrief_notes,
               owner, event_status, event_scope, source_url,
               expected_attendance, impact_notes,
               confirmation_status, waiting_on, preparation_status,
               agencies_involved, reference_links, preparation_checklist,
               reminder_at AT TIME ZONE 'America/New_York' AS reminder_local,
               COALESCE(
                 (
                   SELECT jsonb_agg(
                     jsonb_build_object(
                       'id', i.id,
                       'title', i.title,
                       'status', i.status,
                       'assigned_to', i.assigned_to,
                       'next_action', i.next_action,
                       'waiting_on', i.waiting_on,
                       'due_at', i.due_at AT TIME ZONE 'America/New_York'
                     )
                     ORDER BY
                       CASE
                         WHEN i.status IN ('RESOLVED','CLOSED') THEN 1
                         ELSE 0
                       END,
                       i.updated_at DESC
                   )
                   FROM issues i
                   WHERE i.operational_event_id=operational_events.id
                 ),
                 '[]'::jsonb
               ) AS linked_issues,
               event_intelligence_id,
               created_at, updated_at,
               CASE
                 WHEN active = false THEN 'INACTIVE'
                 WHEN starts_at <= now()
                  AND COALESCE(ends_at, starts_at + interval '2 hours') > now()
                   THEN 'NOW'
                 WHEN starts_at > now() THEN 'UPCOMING'
                 ELSE 'PAST'
               END AS timing_status
        FROM operational_events
        {clause}
        ORDER BY starts_at ASC
        LIMIT 500
        """,
        params,
    )

    counts = query_one(
        """
        SELECT
          count(*) FILTER (
            WHERE active = true
              AND event_status NOT IN ('COMPLETED','CANCELLED')
              AND starts_at <= now()
              AND COALESCE(ends_at, starts_at + interval '2 hours') > now()
          ) AS happening_now,
          count(*) FILTER (
            WHERE active = true
              AND event_status NOT IN ('COMPLETED','CANCELLED')
              AND starts_at > now()
              AND starts_at < date_trunc('day', now() AT TIME ZONE 'America/New_York')
                  AT TIME ZONE 'America/New_York' + interval '1 day'
          ) AS later_today,
          count(*) FILTER (
            WHERE active = true
              AND event_status NOT IN ('COMPLETED','CANCELLED')
              AND COALESCE(ends_at, starts_at + interval '2 hours') >= now()
          ) AS upcoming,
          count(*) FILTER (
            WHERE active = true
              AND event_status NOT IN ('COMPLETED','CANCELLED')
              AND starts_at >= now()
              AND starts_at < now() + interval '7 days'
          ) AS this_week,
          count(*) FILTER (
            WHERE active = true
              AND event_status NOT IN ('COMPLETED','CANCELLED')
              AND COALESCE(ends_at, starts_at + interval '2 hours') >= now()
              AND preparation_status IN ('NOT_STARTED','IN_PROGRESS')
          ) AS needs_preparation,
          count(*) FILTER (
            WHERE active = true
              AND event_status NOT IN ('COMPLETED','CANCELLED')
              AND COALESCE(ends_at, starts_at + interval '2 hours') >= now()
              AND confirmation_status IN ('AWAITING_CONFIRMATION','TENTATIVE')
          ) AS awaiting_confirmation
        FROM operational_events
        """
    )

    return templates.TemplateResponse(
        request=request,
        name="schedule.html",
        context={
            "rows": rows,
            "counts": counts,
            "state": state,
            "q": q,
            "msg": msg,
            "page": "schedule",
        },
    )


@app.post("/schedule/create")
def schedule_create(
    title: str = Form(...),
    category: str = Form(""),
    location_name: str = Form(""),
    address: str = Form(""),
    municipality: str = Form("Weehawken"),
    starts_at: str = Form(...),
    ends_at: str = Form(""),
    priority: int = Form(3),
    notes: str = Form(""),
    attendees: str = Form(""),
    objective: str = Form(""),
    prep_notes: str = Form(""),
    decisions_needed: str = Form(""),
    debrief_notes: str = Form(""),
    owner: str = Form(""),
    event_status: str = Form("PLANNING"),
    event_scope: str = Form("MANAGED"),
    source_url: str = Form(""),
    expected_attendance: str = Form(""),
    impact_notes: str = Form(""),
    confirmation_status: str = Form("CONFIRMED"),
    waiting_on: str = Form(""),
    preparation_status: str = Form("NOT_STARTED"),
    agencies_involved: str = Form(""),
    reference_links: str = Form(""),
    preparation_checklist_text: str = Form(""),
    reminder_at: str = Form(""),
):
    execute(
        """
        INSERT INTO operational_events (
          title, category, location_name, address, municipality,
          starts_at, ends_at, priority, source, notes,
          attendees, objective, prep_notes, decisions_needed, debrief_notes,
          owner, event_status, event_scope, source_url,
          expected_attendance, impact_notes,
          confirmation_status, waiting_on, preparation_status,
          agencies_involved, reference_links, preparation_checklist, reminder_at
        )
        VALUES (
          %s, %s, %s, %s, %s,
          %s::timestamp AT TIME ZONE 'America/New_York',
          NULLIF(%s, '')::timestamp AT TIME ZONE 'America/New_York',
          %s, 'MANUAL', %s,
          %s, %s, %s, %s, %s,
          %s, %s, %s, %s,
          NULLIF(%s, '')::integer,
          %s, %s, %s, %s, %s, %s, %s::jsonb,
          NULLIF(%s, '')::timestamp AT TIME ZONE 'America/New_York'
        )
        """,
        (
            title.strip(), category.strip() or None, location_name.strip() or None,
            address.strip() or None, municipality.strip() or "Weehawken",
            starts_at, ends_at, priority, notes.strip() or None,
            attendees.strip() or None,
            objective.strip() or None,
            prep_notes.strip() or None,
            decisions_needed.strip() or None,
            debrief_notes.strip() or None,
            owner.strip() or None,
            event_status,
            event_scope,
            source_url.strip() or None,
            expected_attendance,
            impact_notes.strip() or None,
            confirmation_status,
            waiting_on.strip() or None,
            preparation_status,
            agencies_involved.strip() or None,
            reference_links.strip() or None,
            _parse_event_checklist(preparation_checklist_text),
            reminder_at,
        ),
    )
    return RedirectResponse(url="/schedule?msg=Schedule+item+created", status_code=303)


@app.post("/schedule/{event_id}/update")
def schedule_update(
    event_id: uuid.UUID,
    title: str = Form(...),
    category: str = Form(""),
    location_name: str = Form(""),
    address: str = Form(""),
    municipality: str = Form("Weehawken"),
    starts_at: str = Form(...),
    ends_at: str = Form(""),
    priority: int = Form(3),
    notes: str = Form(""),
    attendees: str = Form(""),
    objective: str = Form(""),
    prep_notes: str = Form(""),
    decisions_needed: str = Form(""),
    debrief_notes: str = Form(""),
    owner: str = Form(""),
    event_status: str = Form("PLANNING"),
    event_scope: str = Form("MANAGED"),
    source_url: str = Form(""),
    expected_attendance: str = Form(""),
    impact_notes: str = Form(""),
    confirmation_status: str = Form("CONFIRMED"),
    waiting_on: str = Form(""),
    preparation_status: str = Form("NOT_STARTED"),
    agencies_involved: str = Form(""),
    reference_links: str = Form(""),
    preparation_checklist_text: str = Form(""),
    reminder_at: str = Form(""),
    active: str | None = Form(None),
):
    execute(
        """
        UPDATE operational_events
        SET active = %s,
            title = %s,
            category = %s,
            location_name = %s,
            address = %s,
            municipality = %s,
            starts_at = %s::timestamp AT TIME ZONE 'America/New_York',
            ends_at = NULLIF(%s, '')::timestamp AT TIME ZONE 'America/New_York',
            priority = %s,
            notes = %s,
            attendees = %s,
            objective = %s,
            prep_notes = %s,
            decisions_needed = %s,
            debrief_notes = %s,
            owner = %s,
            event_status = %s,
            event_scope = %s,
            source_url = %s,
            expected_attendance =
              NULLIF(%s, '')::integer,
            impact_notes = %s,
            confirmation_status = %s,
            waiting_on = %s,
            preparation_status = %s,
            agencies_involved = %s,
            reference_links = %s,
            preparation_checklist = %s::jsonb,
            reminder_at =
              NULLIF(%s, '')::timestamp AT TIME ZONE 'America/New_York',
            updated_at = now()
        WHERE id = %s
        """,
        (
            active is not None, title.strip(), category.strip() or None,
            location_name.strip() or None, address.strip() or None,
            municipality.strip() or "Weehawken", starts_at, ends_at,
            priority,
            notes.strip() or None,
            attendees.strip() or None,
            objective.strip() or None,
            prep_notes.strip() or None,
            decisions_needed.strip() or None,
            debrief_notes.strip() or None,
            owner.strip() or None,
            event_status,
            event_scope,
            source_url.strip() or None,
            expected_attendance,
            impact_notes.strip() or None,
            confirmation_status,
            waiting_on.strip() or None,
            preparation_status,
            agencies_involved.strip() or None,
            reference_links.strip() or None,
            _parse_event_checklist(preparation_checklist_text),
            reminder_at,
            event_id,
        ),
    )
    return RedirectResponse(url="/schedule?msg=Schedule+item+updated", status_code=303)



@app.post("/schedule/{event_id}/checklist/{item_index}/toggle")
def schedule_toggle_checklist(
    event_id: uuid.UUID,
    item_index: int,
):
    row = query_one(
        """
        SELECT preparation_checklist
        FROM operational_events
        WHERE id=%s
        """,
        (event_id,),
    )

    if not row:
        raise HTTPException(404, "Event not found")

    items = row["preparation_checklist"] or []

    if (
        not isinstance(items, list)
        or item_index < 0
        or item_index >= len(items)
    ):
        raise HTTPException(400, "Invalid checklist item")

    item = dict(items[item_index])
    item["done"] = not bool(item.get("done"))
    items[item_index] = item

    execute(
        """
        UPDATE operational_events
        SET preparation_checklist=%s::jsonb,
            updated_at=now()
        WHERE id=%s
        """,
        (
            json.dumps(items),
            event_id,
        ),
    )

    return RedirectResponse(
        url="/schedule?msg=Preparation+checklist+updated",
        status_code=303,
    )


@app.post("/schedule/{event_id}/create-action")
def schedule_create_action(
    event_id: uuid.UUID,
    action_title: str = Form(""),
    assigned_to: str = Form(""),
    next_action: str = Form(""),
    action_due_at: str = Form(""),
):
    event = query_one(
        """
        SELECT
          id,
          title,
          objective,
          prep_notes,
          waiting_on,
          municipality,
          priority
        FROM operational_events
        WHERE id=%s
        """,
        (event_id,),
    )

    if not event:
        raise HTTPException(404, "Event not found")

    title = (
        action_title.strip()
        or f"Event prep: {event['title']}"
    )

    description_parts = []

    if event.get("objective"):
        description_parts.append(
            "Event objective: " + event["objective"]
        )

    if event.get("prep_notes"):
        description_parts.append(
            "Preparation: " + event["prep_notes"]
        )

    description = "\n".join(description_parts) or None

    execute(
        """
        INSERT INTO issues (
          title,
          description,
          category,
          priority,
          status,
          source,
          municipality,
          item_type,
          operational_event_id,
          assigned_to,
          next_action,
          waiting_on,
          due_at
        )
        VALUES (
          %s,
          %s,
          'EVENT',
          %s,
          'OPEN',
          'EVENTS_CENTER',
          %s,
          'FOLLOW_UP',
          %s,
          %s,
          %s,
          %s,
          NULLIF(%s, '')::timestamp AT TIME ZONE 'America/New_York'
        )
        """,
        (
            title,
            description,
            event["priority"],
            event["municipality"],
            event_id,
            assigned_to.strip() or None,
            next_action.strip() or None,
            event.get("waiting_on"),
            action_due_at,
        ),
    )

    return RedirectResponse(
        url="/schedule?msg=Command+Center+action+created",
        status_code=303,
    )


@app.post("/schedule/{event_id}/toggle")
def schedule_toggle(event_id: uuid.UUID):
    execute(
        "UPDATE operational_events SET active = NOT active, updated_at = now() WHERE id = %s",
        (event_id,),
    )
    return RedirectResponse(url="/schedule?msg=Schedule+item+status+changed", status_code=303)


@app.post("/my-day/reviewed")
def my_day_mark_reviewed():
    response = RedirectResponse(
        url="/my-day",
        status_code=303,
    )
    response.set_cookie(
        "cmos_my_day_reviewed",
        datetime.now().astimezone().isoformat(),
        max_age=31536000,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/my-day", response_class=HTMLResponse)
def my_day(request: Request):
    raw_reviewed = request.cookies.get(
        "cmos_my_day_reviewed",
        "",
    )

    try:
        review_since = datetime.fromisoformat(
            raw_reviewed
        )
        if review_since.tzinfo is None:
            raise ValueError(
                "review timestamp requires timezone"
            )
    except (TypeError, ValueError):
        review_since = (
            datetime.now().astimezone()
            - timedelta(hours=24)
        )

    schedule = query_all(
        """
        SELECT
          id, title, category, location_name,
          address, municipality,
          starts_at AT TIME ZONE
            'America/New_York' AS starts_local,
          ends_at AT TIME ZONE
            'America/New_York' AS ends_local,
          priority, notes,
          attendees, objective, prep_notes,
          decisions_needed, debrief_notes
        FROM operational_events
        WHERE active = true
          AND event_status NOT IN (
            'COMPLETED','CANCELLED'
          )
          AND starts_at >= date_trunc(
                'day',
                now() AT TIME ZONE
                  'America/New_York'
              ) AT TIME ZONE
                'America/New_York'
          AND starts_at < (
                date_trunc(
                  'day',
                  now() AT TIME ZONE
                    'America/New_York'
                ) + interval '1 day'
              ) AT TIME ZONE
                'America/New_York'
        ORDER BY starts_at
        """
    )

    priority_alerts = query_all(
        """
        WITH current_alerts AS (
          SELECT DISTINCT ON (
            CASE
              WHEN source IN (
                'NJ_DIVERT','PSEG','ORU'
              )
                THEN source || '|'
                  || COALESCE(subtype,'')
                  || '|'
                  || COALESCE(
                       municipality,
                       ''
                     )
              ELSE alert_id
            END
          )
            alert_id,
            source,
            category,
            subtype,
            title,
            message,
            priority,
            municipality,
            event_action,
            click_url,
            received_at
          FROM alerts
          WHERE status <> 'RESOLVED'
            AND source <> 'EXEC_ASSISTANT'
            AND source <> 'SYSTEM_TEST'
            AND (
              expires_at IS NULL
              OR expires_at > now()
            )
          ORDER BY
            CASE
              WHEN source IN (
                'NJ_DIVERT','PSEG','ORU'
              )
                THEN source || '|'
                  || COALESCE(subtype,'')
                  || '|'
                  || COALESCE(
                       municipality,
                       ''
                     )
              ELSE alert_id
            END,
            received_at DESC
        )
        SELECT
          alert_id,
          source,
          category,
          subtype,
          title,
          message,
          priority,
          municipality,
          event_action,
          click_url,
          received_at AT TIME ZONE
            'America/New_York'
            AS received_local
        FROM current_alerts
        ORDER BY
          priority DESC,
          received_at DESC
        LIMIT 8
        """
    )

    source_warnings = query_all(
        """
        SELECT
          source_id,
          status,
          last_error,
          last_success_at AT TIME ZONE
            'America/New_York'
            AS last_success_local,
          last_event_at AT TIME ZONE
            'America/New_York'
            AS last_event_local
        FROM source_health
        WHERE upper(status)
          NOT IN ('OK','HEALTHY')
        ORDER BY updated_at DESC
        """
    )

    attention = query_all(
        """
        SELECT
          id,
          title,
          item_type,
          priority,
          status,
          next_action,
          waiting_on,
          assigned_to,
          due_at,
          follow_up_at,
          decision_by,
          waiting_on_since,
          waiting_on_last_chased,
          waiting_on_chase_count,
          created_at,
          updated_at,
          due_at AT TIME ZONE
            'America/New_York'
            AS due_local,
          follow_up_at AT TIME ZONE
            'America/New_York'
            AS follow_up_local
        FROM issues
        WHERE status NOT IN (
          'RESOLVED','CLOSED'
        )
        AND (
          due_at <= (
            date_trunc(
              'day',
              now() AT TIME ZONE
                'America/New_York'
            ) + interval '1 day'
          ) AT TIME ZONE
            'America/New_York'

          OR follow_up_at <= (
            date_trunc(
              'day',
              now() AT TIME ZONE
                'America/New_York'
            ) + interval '1 day'
          ) AT TIME ZONE
            'America/New_York'

          OR decision_by <=
            now() + interval '72 hours'

          OR next_action IS NULL
          OR trim(next_action) = ''

          OR NULLIF(
            trim(waiting_on),
            ''
          ) IS NOT NULL

          OR item_type='DECISION'
          OR priority >= 5
        )
        ORDER BY
          priority DESC,
          updated_at DESC
        LIMIT 100
        """
    )

    attention = [
        item
        for item
        in annotate_issue_rows(
            attention
        )
        if item["needs_manager"]
    ]

    attention.sort(
        key=lambda item: (
            -item[
                "attention_score"
            ],
            -int(
                item.get(
                    "priority"
                )
                or 0
            ),
        )
    )


    waiting = query_all(
        """
        SELECT
          id,
          title,
          item_type,
          priority,
          status,
          waiting_on,
          next_action,
          assigned_to,
          due_at,
          follow_up_at,
          decision_by,
          waiting_on_since,
          waiting_on_last_chased,
          waiting_on_chase_count,
          created_at,
          updated_at,
          follow_up_at AT TIME ZONE
            'America/New_York'
            AS follow_up_local
        FROM issues
        WHERE status NOT IN (
          'RESOLVED','CLOSED'
        )
        AND NULLIF(
          trim(waiting_on),
          ''
        ) IS NOT NULL
        ORDER BY
          follow_up_at NULLS LAST,
          priority DESC,
          updated_at DESC
        LIMIT 50
        """
    )

    waiting = annotate_issue_rows(
        waiting
    )

    waiting_status_rank = {
        "ESCALATE": 0,
        "CHASE TODAY": 1,
        "CHASE AGAIN": 1,
        "WAITING AFTER CHASE": 2,
        "WAITING": 3,
    }

    waiting.sort(
        key=lambda item: (
            waiting_status_rank.get(
                item[
                    "waiting_status"
                ],
                9,
            ),
            -item[
                "attention_score"
            ],
            -item[
                "waiting_age_days"
            ],
        )
    )


    commitments = query_all(
        """
        SELECT
          id,
          title,
          priority,
          next_action,
          waiting_on,
          assigned_to,
          due_at AT TIME ZONE
            'America/New_York'
            AS due_local,

          CASE
            WHEN due_at < now()
              THEN 'OVERDUE'

            WHEN due_at <=
              now() + interval '24 hours'
              THEN 'DUE <24H'

            WHEN due_at <=
              now() + interval '72 hours'
              THEN 'DUE <72H'

            WHEN due_at IS NULL
              THEN 'NO DUE DATE'

            ELSE 'ON TRACK'
          END AS commitment_status

        FROM issues
        WHERE status NOT IN (
          'RESOLVED','CLOSED'
        )
        AND item_type='COMMITMENT'

        ORDER BY
          CASE
            WHEN due_at < now()
              THEN 0

            WHEN due_at <=
              now() + interval '72 hours'
              THEN 1

            WHEN due_at IS NULL
              THEN 3

            ELSE 2
          END,
          due_at NULLS LAST,
          priority DESC,
          updated_at DESC

        LIMIT 20
        """
    )

    communications = query_all(
        """
        SELECT
          id,
          title,
          priority,
          next_action,
          waiting_on,
          assigned_to,
          due_at AT TIME ZONE
            'America/New_York'
            AS due_local,
          follow_up_at AT TIME ZONE
            'America/New_York'
            AS follow_up_local
        FROM issues
        WHERE status NOT IN (
          'RESOLVED','CLOSED'
        )
        AND item_type='COMMUNICATION'
        ORDER BY
          COALESCE(
            due_at,
            follow_up_at
          ) NULLS LAST,
          priority DESC,
          updated_at DESC
        LIMIT 20
        """
    )

    decisions = query_all(
        """
        SELECT
          id,
          title,
          description,
          priority,
          assigned_to,
          next_action,
          waiting_on,
          decision_options,
          recommendation,
          decision_outcome,
          decision_by AT TIME ZONE
            'America/New_York'
            AS decision_by_local
        FROM issues
        WHERE status NOT IN (
          'RESOLVED','CLOSED'
        )
        AND item_type='DECISION'
        ORDER BY
          decision_by NULLS LAST,
          priority DESC,
          updated_at DESC
        LIMIT 20
        """
    )

    visibility = query_all(
        """
        SELECT
          id,
          title,
          item_type,
          priority,
          visibility_status,
          visibility_audience,
          visibility_note,
          next_action,
          assigned_to
        FROM issues
        WHERE status NOT IN (
          'RESOLVED','CLOSED'
        )
        AND visibility_status IN (
          'WATCH','PREP','READY'
        )
        ORDER BY
          CASE visibility_status
            WHEN 'READY' THEN 0
            WHEN 'PREP' THEN 1
            ELSE 2
          END,
          priority DESC,
          updated_at DESC
        LIMIT 20
        """
    )

    overdue = query_all(
        """
        SELECT
          id,
          title,
          item_type,
          priority,
          next_action,
          waiting_on,
          assigned_to,
          due_at AT TIME ZONE
            'America/New_York'
            AS due_local,
          follow_up_at AT TIME ZONE
            'America/New_York'
            AS follow_up_local
        FROM issues
        WHERE status NOT IN (
          'RESOLVED','CLOSED'
        )
        AND (
          due_at < now()
          OR follow_up_at < now()
        )
        ORDER BY
          LEAST(
            COALESCE(
              due_at,
              'infinity'::timestamptz
            ),
            COALESCE(
              follow_up_at,
              'infinity'::timestamptz
            )
          ),
          priority DESC
        LIMIT 25
        """
    )

    changed = query_all(
        """
        SELECT *
        FROM (
          SELECT
            'ISSUE'::text
              AS change_type,
            id::text
              AS ref_id,
            title,
            item_type::text
              AS detail,
            priority,
            updated_at
              AS changed_at
          FROM issues
          WHERE updated_at > %s

          UNION ALL

          SELECT
            'EVENT'::text,
            id::text,
            title,
            COALESCE(
              preparation_status,
              event_status
            )::text,
            priority,
            updated_at
          FROM operational_events
          WHERE updated_at > %s

          UNION ALL

          SELECT
            'ALERT'::text,
            alert_id::text,
            title,
            source::text,
            priority,
            received_at
          FROM alerts
          WHERE received_at > %s
            AND source NOT IN (
              'EXEC_ASSISTANT',
              'SYSTEM_TEST'
            )
        ) changes

        ORDER BY
          changed_at DESC,
          priority DESC

        LIMIT 25
        """,
        (
            review_since,
            review_since,
            review_since,
        ),
    )

    approaching = query_all(
        """
        SELECT
          id,
          title,
          item_type,
          priority,
          next_action,
          waiting_on,
          due_at AT TIME ZONE
            'America/New_York'
            AS due_local,
          follow_up_at AT TIME ZONE
            'America/New_York'
            AS follow_up_local,
          decision_by AT TIME ZONE
            'America/New_York'
            AS decision_by_local,

          CASE
            WHEN due_at < now()
              THEN 'OVERDUE'

            WHEN follow_up_at < now()
              THEN 'FOLLOW-UP OVERDUE'

            WHEN decision_by < now()
              THEN 'DECISION OVERDUE'

            WHEN due_at <=
              now() + interval '24 hours'
              THEN 'DUE <24H'

            WHEN follow_up_at <=
              now() + interval '24 hours'
              THEN 'FOLLOW <24H'

            WHEN decision_by <=
              now() + interval '24 hours'
              THEN 'DECIDE <24H'

            ELSE 'NEXT 72H'
          END AS deadline_status

        FROM issues
        WHERE status NOT IN (
          'RESOLVED','CLOSED'
        )
        AND (
          due_at <=
            now() + interval '72 hours'

          OR follow_up_at <=
            now() + interval '72 hours'

          OR decision_by <=
            now() + interval '72 hours'
        )

        ORDER BY
          CASE
            WHEN due_at < now()
              OR follow_up_at < now()
              OR decision_by < now()
              THEN 0
            ELSE 1
          END,

          LEAST(
            COALESCE(
              due_at,
              'infinity'::timestamptz
            ),
            COALESCE(
              follow_up_at,
              'infinity'::timestamptz
            ),
            COALESCE(
              decision_by,
              'infinity'::timestamptz
            )
          ),

          priority DESC

        LIMIT 20
        """
    )

    meeting_prep = query_all(
        """
        SELECT
          e.id,
          e.title,
          e.location_name,
          e.priority,
          e.starts_at AT TIME ZONE
            'America/New_York'
            AS starts_local,
          e.attendees,
          e.objective,
          e.decisions_needed,
          e.waiting_on,
          e.preparation_status,

          (
            SELECT count(*)
            FROM issues i
            WHERE
              i.operational_event_id=e.id
              AND i.status NOT IN (
                'RESOLVED','CLOSED'
              )
          ) AS open_issue_count

        FROM operational_events e
        WHERE e.active=true
          AND e.event_status NOT IN (
            'COMPLETED','CANCELLED'
          )
          AND e.starts_at >= now()
          AND e.starts_at <=
            now() + interval '72 hours'

        ORDER BY
          e.starts_at,
          e.priority DESC

        LIMIT 15
        """
    )

    counts = query_one(
        """
        SELECT
          (
            SELECT count(*)
            FROM operational_events
            WHERE active=true
              AND starts_at >= date_trunc(
                'day',
                now() AT TIME ZONE
                  'America/New_York'
              ) AT TIME ZONE
                'America/New_York'
              AND starts_at < (
                date_trunc(
                  'day',
                  now() AT TIME ZONE
                    'America/New_York'
                ) + interval '1 day'
              ) AT TIME ZONE
                'America/New_York'
          ) AS schedule_today,

          count(*) FILTER (
            WHERE status NOT IN (
              'RESOLVED','CLOSED'
            )
            AND (
              due_at <= now()
              OR follow_up_at <= now()
            )
          ) AS due_now,

          count(*) FILTER (
            WHERE status NOT IN (
              'RESOLVED','CLOSED'
            )
            AND NULLIF(
              trim(waiting_on),
              ''
            ) IS NOT NULL
          ) AS waiting,

          count(*) FILTER (
            WHERE status NOT IN (
              'RESOLVED','CLOSED'
            )
            AND NULLIF(
              trim(next_action),
              ''
            ) IS NULL
          ) AS no_next_action

        FROM issues
        """
    )

    brief_counts = query_one(
        """
        SELECT
          (
            (
              SELECT count(*)
              FROM issues
              WHERE updated_at > %s
            )
            +
            (
              SELECT count(*)
              FROM operational_events
              WHERE updated_at > %s
            )
            +
            (
              SELECT count(*)
              FROM alerts
              WHERE received_at > %s
                AND source NOT IN (
                  'EXEC_ASSISTANT',
                  'SYSTEM_TEST'
                )
            )
          ) AS changed,

          (
            SELECT count(*)
            FROM issues
            WHERE status NOT IN (
              'RESOLVED','CLOSED'
            )
            AND (
              item_type='DECISION'
              OR due_at <= now()
              OR follow_up_at <= now()
              OR NULLIF(
                trim(next_action),
                ''
              ) IS NULL
            )
          ) AS needs_me,

          (
            SELECT count(*)
            FROM issues
            WHERE status NOT IN (
              'RESOLVED','CLOSED'
            )
            AND NULLIF(
              trim(waiting_on),
              ''
            ) IS NOT NULL
          ) AS waiting,

          (
            SELECT count(*)
            FROM issues
            WHERE status NOT IN (
              'RESOLVED','CLOSED'
            )
            AND (
              due_at < now()
              OR follow_up_at < now()
              OR decision_by < now()
            )
          ) AS late,

          (
            (
              SELECT count(*)
              FROM operational_events
              WHERE active=true
                AND event_status NOT IN (
                  'COMPLETED','CANCELLED'
                )
                AND starts_at >= now()
                AND starts_at <=
                  now() + interval '72 hours'
            )
            +
            (
              SELECT count(*)
              FROM issues
              WHERE status NOT IN (
                'RESOLVED','CLOSED'
              )
              AND (
                due_at BETWEEN
                  now()
                  AND now()
                    + interval '72 hours'

                OR follow_up_at BETWEEN
                  now()
                  AND now()
                    + interval '72 hours'

                OR decision_by BETWEEN
                  now()
                  AND now()
                    + interval '72 hours'
              )
            )
          ) AS coming_next
        """,
        (
            review_since,
            review_since,
            review_since,
        ),
    )

    if brief_counts is not None:
        brief_counts[
            "needs_me"
        ] = len(
            attention
        )

        brief_counts[
            "waiting"
        ] = len(
            waiting
        )


    review_state = {
        "since": review_since.astimezone(),
        "is_default": not bool(raw_reviewed),
    }

    return templates.TemplateResponse(
        request=request,
        name="my_day.html",
        context={
            "schedule": schedule,
            "priority_alerts": priority_alerts,
            "source_warnings": source_warnings,
            "attention": attention,
            "waiting": waiting,
            "commitments": commitments,
            "communications": communications,
            "decisions": decisions,
            "visibility": visibility,
            "overdue": overdue,
            "changed": changed,
            "approaching": approaching,
            "meeting_prep": meeting_prep,
            "brief_counts": brief_counts,
            "review_state": review_state,
            "counts": counts,
        },
    )



@app.post("/schedule/{event_id}/create-follow-up")
def schedule_create_follow_up(event_id: uuid.UUID):
    execute(
        """
        INSERT INTO issues (
          title,
          description,
          category,
          priority,
          status,
          source,
          municipality,
          item_type,
          operational_event_id
        )
        SELECT
          'Follow up: ' || e.title,
          CASE
            WHEN NULLIF(trim(e.objective), '') IS NOT NULL
              THEN 'Meeting objective: ' || e.objective
            ELSE NULL
          END,
          'MEETING',
          e.priority,
          'OPEN',
          'SCHEDULE',
          e.municipality,
          'FOLLOW_UP',
          e.id
        FROM operational_events e
        WHERE e.id = %s
          AND NOT EXISTS (
            SELECT 1
            FROM issues i
            WHERE i.operational_event_id = e.id
              AND i.item_type = 'FOLLOW_UP'
              AND i.status NOT IN ('RESOLVED','CLOSED')
          )
        """,
        (event_id,),
    )

    return RedirectResponse(
        url="/issues?msg=Meeting+follow-up+ready",
        status_code=303,
    )
