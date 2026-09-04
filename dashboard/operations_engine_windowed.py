import operations_engine as base


def ensure_runs(cur, service_date):
    # Remove only untouched generated occurrences that no longer match the
    # active recurrence definition. Historical/linked work is preserved.
    cur.execute(
        """
        DELETE FROM operations_routine_runs rr
        USING operations_routines r
        WHERE rr.routine_id=r.id
          AND rr.service_date=%s
          AND rr.issue_id IS NULL
          AND rr.acknowledged_at IS NULL
          AND rr.exception_note IS NULL
          AND (
            r.active=false
            OR NOT (extract(isodow from rr.service_date)::smallint = ANY(r.days_of_week))
            OR (r.starts_on IS NOT NULL AND rr.service_date < r.starts_on)
            OR (r.ends_on IS NOT NULL AND rr.service_date > r.ends_on)
          )
        """,
        (service_date,),
    )

    cur.execute(
        """
        SELECT id, scheduled_time, grace_minutes
        FROM operations_routines
        WHERE active=true
          AND extract(isodow from %s::date)::smallint = ANY(days_of_week)
          AND (starts_on IS NULL OR starts_on <= %s::date)
          AND (ends_on IS NULL OR ends_on >= %s::date)
        """,
        (service_date, service_date, service_date),
    )

    created = 0
    for routine in cur.fetchall():
        scheduled_for = base._scheduled_at(service_date, routine["scheduled_time"])
        due_at = scheduled_for + base.timedelta(minutes=routine["grace_minutes"])
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


base.ensure_runs = ensure_runs


if __name__ == "__main__":
    base.main()
