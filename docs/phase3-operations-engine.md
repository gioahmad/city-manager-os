# Phase 3 — Operations Engine

## Goal

Make City Manager OS understand both recurring municipal work and expected daily activity.

The system distinguishes:

- **WORK**: creates a normal Employee Operations ticket in the existing `issues` spine, with assignment, checklist, photos, Start Work, Need Help, completion and supervisor verification.
- **AWARENESS**: appears in Today's Operations / My Day as something expected to be happening. It can require confirmation and can automatically create a Command Center exception if missed.

Routine activity stays visible without becoming noise. Exceptions become actionable.

## Core behavior

- Routines are configured from `/operations-routines`.
- Runs are generated idempotently for today and tomorrow, rather than creating hundreds of future records.
- Recurring work uses `source='EMPLOYEE_PORTAL'` so the existing employee workflow is reused.
- Awareness items do not create tickets unless they become exceptions.
- Employee Operations completion is held at `PENDING_VERIFICATION` by a database trigger unless verification is explicitly disabled.
- Supervisors verify or return work from the private Operations Board.
- Routine due dates naturally surface in My Day and Command Center through the existing `issues.due_at` logic.
- Awareness exceptions create normal Command Center `EXCEPTION` issues with `source='OPERATIONS_ENGINE'`.
- `citymanager-ops-engine` is a dedicated worker container and reports health through `source_health`.

## Recurrence windows

A routine can have an optional start date and optional end date in addition to its selected weekdays and scheduled time.

- no start date means it is already eligible
- no end date means it continues indefinitely
- an end date stops new occurrences after that date
- changing the date window removes only untouched future generated occurrences that no longer belong
- historical, acknowledged, exception, and work-linked occurrences are preserved

This supports patterns such as `Garbage collection, Area A — every Tuesday indefinitely`, a seasonal park routine, or a temporary construction-period activity with a defined end date.

## Occurrence-specific notes

Each generated daily occurrence can carry a **Today Note** that belongs only to that service date. It does not alter the recurring routine or future occurrences.

Example: a recurring Tuesday sanitation route can carry `Truck 3 broke down, approximately 45 minutes late` today while next Tuesday remains unchanged.

Today Notes appear in:
- My Day
- Operations Board
- Daily & Recurring Operations setup

A Today Note is informational and does not create an exception by itself. Use **Report issue / Escalate** when the occurrence requires follow-up or intervention.

## No sample routines

The migration intentionally creates no synthetic or guessed Township routines. Configure real activities only after deployment.

Examples discussed during design, such as garbage zones or school bus pickup, were illustrative only.

## Safety

Deployment:
- requires a clean repository
- backs up PostgreSQL before migration
- applies idempotent additive migrations
- compiles Python and parses templates before restart
- runs a read-only engine dry run
- keeps the public employee portal untouched for the occurrence-controls update
- verifies HTTPS/secure cookies and localhost-only 8091
- runs the full City Manager OS health check

The worker does not change n8n source workflows, raw feeds, watchlist routing or notification destinations.
