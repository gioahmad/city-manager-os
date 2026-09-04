import argparse
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

EASTERN = ZoneInfo("America/New_York")
INTERVAL_SECONDS = max(15, int(os.getenv("OPS_ENGINE_INTERVAL_SECONDS", "60")))

CHECKLISTS = {
    "NONE": [],
    "GENERAL": [
        "Inspect location / condition",
        "Complete required work",
        "Clean and secure work area",
        "Confirm work is complete",
    ],
    "SITE_CHECK": [
        "Inspect site",
        "Check safety hazards",
        "Check access / doors / gates",
        "Check lighting / utilities",
        "Report deficiencies",
    ],
    "EVENT_SETUP": [
        "Confirm event location",
        "Set tables / chairs / equipment",
        "Check power / lighting",
        "Check trash / sanitation",
        "Final walkthrough",
    ],
    "VEHICLE": [
        "Visual safety inspection",
        "Check fuel / charge",
        "Check tires / lights",
        "Check equipment",
        "Report defects",
    ],
    "OPEN_CLOSE": [
        "Inspect facility / park",
        "Unlock or secure required areas",
        "Check lights / utilities",
        "Check restrooms / common areas",
        "Final safety walkthrough",
    ],
}


def db_conn():
    return psycopg.connect(
        host=os.getenv("DB_HOST", "citymanager-postgis"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "citymanager"),
        user=os.getenv("DB_USER", "citymanager_app"),
        password=os.environ["DB_PASSWORD"],
        row_factory=dict_row,
        connect_timeout=5,
    )


def mark_health(status, error=None, metadata=None):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO source_health (
                  source_id,status,last_attempt_at,last_success_at,last_error,
                  metadata,updated_at
                )
                VALUES (
                  'OPERATIONS_ENGINE',%s,now(),
                  CASE WHEN %s='OK' THEN now() ELSE NULL END,
                  %s,%s,now()
                )
                ON CONFLICT (source_id) DO UPDATE
                SET status=EXCLUDED.status,
                    last_attempt_at=EXCLUDED.last_attempt_at,
                    last_success_at=CASE
                      WHEN EXCLUDED.status='OK' THEN EXCLUDED.last_success_at
                      ELSE source_health.last_success_at
                    END,
                    last_error=EXCLUDED.last_error,
                    metadata=EXCLUDED.metadata,
                    updated_at=now()
                """,
                (status, status, error, Jsonb(metadata or {})),
            )
        conn.commit()


def _scheduled_at(service_date, scheduled_time):
    return datetime.combine(service_date, scheduled_time, tzinfo=EASTERN)


def ensure_runs(cur, service_date):
    cur.execute(
        """
        SELECT id, scheduled_time, grace_minutes
        FROM operations_routines
        WHERE active=true
          AND extract(isodow from %s::date)::smallint = ANY(days_of_week)
        """,
        (service_date,),
    )
    created = 0
    for routine in cur.fetchall():
        scheduled_for = _scheduled_at(service_date, routine["scheduled_time"])
        due_at = scheduled_for + timedelta(minutes=routine["grace_minutes"])
        cur.execute(
            """
            INSERT INTO operations_routine_runs (
              routine_id,service_date,scheduled_for,due_at,status
            )
            VALUES (%s,%s,%s,%s,'SCHEDULED')
            ON CONFLICT (routine_id,service_date) DO UPDATE
            SET scheduled_for=EXCLUDED.scheduled_for,
                due_at=EXCLUDED.due_at,
                updated_at=now()
            WHERE operations_routine_runs.issue_id IS NULL
              AND operations_routine_runs.acknowledged_at IS NULL
              AND operations_routine_runs.exception_note IS NULL
            """,
            (routine["id"], service_date, scheduled_for, due_at),
        )
        created += cur.rowcount
    return created


def create_work_issues(cur, now):
    cur.execute(
        """
        SELECT
          rr.id AS run_id, rr.routine_id, rr.service_date, rr.scheduled_for, rr.due_at,
          r.name, r.department, r.location_id, r.location_label, r.work_type_id,
          r.assigned_employee_id, r.priority, r.verification_required,
          r.checklist_items, r.description,
          sl.name AS managed_location,
          wt.name AS work_type_name, wt.checklist_template,
          e.full_name AS employee_name
        FROM operations_routine_runs rr
        JOIN operations_routines r ON r.id=rr.routine_id
        LEFT JOIN staff_locations sl ON sl.id=r.location_id
        LEFT JOIN staff_work_types wt ON wt.id=r.work_type_id
        LEFT JOIN staff_employees e
          ON e.id=r.assigned_employee_id AND e.active=true
        WHERE r.active=true
          AND r.routine_kind='WORK'
          AND rr.issue_id IS NULL
          AND rr.scheduled_for - make_interval(mins => r.lead_minutes) <= %s
        ORDER BY rr.scheduled_for
        FOR UPDATE OF rr SKIP LOCKED
        """,
        (now,),
    )
    rows = cur.fetchall()
    made = 0

    for row in rows:
        location = (
            (row["location_label"] or "").strip()
            or (row["managed_location"] or "").strip()
            or "Location not specified"
        )
        title = f'{row["name"]} — {location}'
        next_action = (
            "Complete scheduled work"
            if row["employee_name"]
            else "Supervisor assign scheduled work"
        )

        cur.execute(
            """
            INSERT INTO issues (
              title,description,category,priority,status,source,municipality,
              item_type,next_action,submitted_by,submitted_department,
              employee_location,assigned_to,assigned_employee_id,
              staff_location_id,staff_work_type_id,
              due_at,operations_routine_id,operations_run_id,
              verification_required
            )
            VALUES (
              %s,%s,%s,%s,'OPEN','EMPLOYEE_PORTAL','Weehawken',
              'TASK',%s,'Operations Engine',%s,
              %s,%s,%s,%s,%s,%s,%s,%s,%s
            )
            RETURNING id
            """,
            (
                title,
                row["description"],
                (row["department"] or "OPERATIONS").upper(),
                row["priority"],
                next_action,
                row["department"],
                location,
                row["employee_name"],
                row["assigned_employee_id"] if row["employee_name"] else None,
                row["location_id"],
                row["work_type_id"],
                row["due_at"],
                row["routine_id"],
                row["run_id"],
                row["verification_required"],
            ),
        )
        issue_id = cur.fetchone()["id"]

        custom = [x.strip() for x in (row["checklist_items"] or []) if x and x.strip()]
        checklist = custom or CHECKLISTS.get(
            row["checklist_template"] or "GENERAL",
            CHECKLISTS["GENERAL"],
        )
        for order, label in enumerate(checklist, 1):
            cur.execute(
                """
                INSERT INTO issue_checklist_items(issue_id,label,sort_order)
                VALUES (%s,%s,%s)
                """,
                (issue_id, label, order * 10),
            )

        cur.execute(
            """
            INSERT INTO issue_updates(issue_id,author,note)
            VALUES (%s,'Operations Engine',%s)
            """,
            (
                issue_id,
                f'Scheduled routine for {row["scheduled_for"].astimezone(EASTERN).strftime("%m/%d/%Y %I:%M %p")}.',
            ),
        )
        run_status = "ASSIGNED" if row["employee_name"] else "UNASSIGNED"
        cur.execute(
            """
            UPDATE operations_routine_runs
            SET issue_id=%s,status=%s,updated_at=now()
            WHERE id=%s
            """,
            (issue_id, run_status, row["run_id"]),
        )
        made += 1
    return made


def sync_work_runs(cur):
    cur.execute(
        """
        UPDATE operations_routine_runs rr
        SET status = CASE
              WHEN i.status='PENDING_VERIFICATION' OR i.verification_pending
                THEN 'AWAITING_VERIFICATION'
              WHEN i.status='IN_PROGRESS' THEN 'IN_PROGRESS'
              WHEN i.status='ON_HOLD' THEN 'NEEDS_HELP'
              WHEN i.status IN ('RESOLVED','CLOSED') THEN 'COMPLETE'
              WHEN i.assigned_employee_id IS NULL THEN 'UNASSIGNED'
              ELSE 'ASSIGNED'
            END,
            updated_at=now()
        FROM issues i, operations_routines r
        WHERE rr.issue_id=i.id
          AND rr.routine_id=r.id
          AND r.routine_kind='WORK'
          AND rr.status IS DISTINCT FROM CASE
              WHEN i.status='PENDING_VERIFICATION' OR i.verification_pending
                THEN 'AWAITING_VERIFICATION'
              WHEN i.status='IN_PROGRESS' THEN 'IN_PROGRESS'
              WHEN i.status='ON_HOLD' THEN 'NEEDS_HELP'
              WHEN i.status IN ('RESOLVED','CLOSED') THEN 'COMPLETE'
              WHEN i.assigned_employee_id IS NULL THEN 'UNASSIGNED'
              ELSE 'ASSIGNED'
            END
        """
    )
    return cur.rowcount


def create_exception_issue(cur, run, reason):
    title = f'Operations exception: {run["name"]}'
    location = (
        (run["location_label"] or "").strip()
        or (run["managed_location"] or "").strip()
        or None
    )
    cur.execute(
        """
        INSERT INTO issues (
          title,description,category,priority,status,source,municipality,
          item_type,next_action,waiting_on,due_at,employee_location,
          operations_routine_id,operations_run_id
        )
        VALUES (
          %s,%s,'OPERATIONS',%s,'OPEN','OPERATIONS_ENGINE','Weehawken',
          'EXCEPTION','Supervisor review / resolve operations exception',
          'Operations',now(),%s,%s,%s
        )
        RETURNING id
        """,
        (
            title,
            reason,
            max(3, int(run["priority"] or 3)),
            location,
            run["routine_id"],
            run["run_id"],
        ),
    )
    issue_id = cur.fetchone()["id"]
    cur.execute(
        """
        INSERT INTO issue_updates(issue_id,author,note)
        VALUES (%s,'Operations Engine',%s)
        """,
        (issue_id, reason),
    )
    return issue_id


def sync_awareness_runs(cur, now, service_date):
    cur.execute(
        """
        SELECT
          rr.id AS run_id, rr.routine_id, rr.status, rr.scheduled_for, rr.due_at,
          rr.acknowledged_at, rr.exception_note, rr.exception_issue_id,
          r.name,r.priority,r.confirmation_required,r.escalate_if_missed,
          r.display_after_minutes,r.location_label,
          sl.name AS managed_location
        FROM operations_routine_runs rr
        JOIN operations_routines r ON r.id=rr.routine_id
        LEFT JOIN staff_locations sl ON sl.id=r.location_id
        WHERE r.active=true
          AND r.routine_kind='AWARENESS'
          AND rr.service_date=%s
        ORDER BY rr.scheduled_for
        FOR UPDATE OF rr SKIP LOCKED
        """,
        (service_date,),
    )

    changed = 0
    exceptions = 0
    for run in cur.fetchall():
        if run["exception_note"]:
            target = "EXCEPTION"
        elif run["acknowledged_at"]:
            target = "ACKNOWLEDGED"
        elif run["confirmation_required"] and now > run["due_at"]:
            target = "MISSED"
        elif now >= run["scheduled_for"]:
            end_display = run["scheduled_for"] + timedelta(
                minutes=run["display_after_minutes"]
            )
            target = "EXPECTED" if now <= end_display else "PASSED"
        else:
            target = "UPCOMING"

        if target != run["status"]:
            cur.execute(
                """
                UPDATE operations_routine_runs
                SET status=%s,updated_at=now()
                WHERE id=%s
                """,
                (target, run["run_id"]),
            )
            changed += 1

        if (
            target == "MISSED"
            and run["escalate_if_missed"]
            and not run["exception_issue_id"]
        ):
            reason = (
                "Expected activity was not confirmed by "
                + run["due_at"].astimezone(EASTERN).strftime("%I:%M %p")
                + "."
            )
            issue_id = create_exception_issue(cur, run, reason)
            cur.execute(
                """
                UPDATE operations_routine_runs
                SET exception_issue_id=%s,status='EXCEPTION',updated_at=now()
                WHERE id=%s
                """,
                (issue_id, run["run_id"]),
            )
            exceptions += 1
    return changed, exceptions


def tick(dry_run=False):
    now = datetime.now(EASTERN)
    today = now.date()
    tomorrow = today + timedelta(days=1)

    with db_conn() as conn:
        if dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      count(*) FILTER (WHERE active) AS active_routines,
                      count(*) FILTER (WHERE active AND routine_kind='WORK') AS work_routines,
                      count(*) FILTER (WHERE active AND routine_kind='AWARENESS') AS awareness_routines
                    FROM operations_routines
                    """
                )
                counts = cur.fetchone()
                cur.execute(
                    """
                    SELECT count(*) AS today_runs
                    FROM operations_routine_runs
                    WHERE service_date=%s
                    """,
                    (today,),
                )
                counts["today_runs"] = cur.fetchone()["today_runs"]
                return counts

        with conn.cursor() as cur:
            run_count = ensure_runs(cur, today) + ensure_runs(cur, tomorrow)
            work_created = create_work_issues(cur, now)
            work_synced = sync_work_runs(cur)
            awareness_changed, exceptions = sync_awareness_runs(cur, now, today)
        conn.commit()

    metadata = {
        "local_time": now.isoformat(),
        "runs_created": run_count,
        "work_created": work_created,
        "work_synced": work_synced,
        "awareness_changed": awareness_changed,
        "exceptions_created": exceptions,
    }
    mark_health("OK", metadata=metadata)
    return metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.once or args.dry_run:
        print(tick(dry_run=args.dry_run))
        return

    while True:
        try:
            result = tick()
            print(
                f'[{datetime.now(EASTERN).isoformat()}] operations tick {result}',
                flush=True,
            )
        except Exception as exc:
            print(
                f'[{datetime.now(EASTERN).isoformat()}] operations error: {exc}',
                flush=True,
            )
            try:
                mark_health("ERROR", error=str(exc))
            except Exception:
                pass
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
