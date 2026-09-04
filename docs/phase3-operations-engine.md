# Phase 3 — Operations Engine

## Goal

Make City Manager OS understand both recurring municipal work and expected daily activity.

The system distinguishes:

- **WORK**: creates a normal Employee Operations ticket in the existing `issues` spine, with assignment, checklist, photos, Start Work, Need Help, completion and supervisor verification.
- **AWARENESS**: appears in Today's Operations / My Day as something expected to be happening. It can require confirmation and can automatically create a Command Center exception if missed.

Routine activity stays visible without becoming noise. Exceptions become actionable.

## Core behavior

- Routines are configured from `/operations-routines`.
- Runs are generated idempotently for today and tomorrow.
- Recurring work uses `source='EMPLOYEE_PORTAL'` so the existing employee workflow is reused.
- Awareness items do not create tickets unless they become exceptions.
- Employee Operations completion is held at `PENDING_VERIFICATION` by a database trigger unless verification is explicitly disabled.
- Supervisors verify or return work from the private Operations Board.
- Routine due dates naturally surface in My Day and Command Center through the existing `issues.due_at` logic.
- Awareness exceptions create normal Command Center `EXCEPTION` issues with `source='OPERATIONS_ENGINE'`.
- `citymanager-ops-engine` is a dedicated worker container and reports health through `source_health`.

## No sample routines

The migration intentionally creates no synthetic or guessed Township routines. Configure real activities only after deployment.

Examples discussed during design, such as garbage zones or school bus pickup, were illustrative only.

## Safety

Deployment:
- requires a clean repository
- backs up PostgreSQL before migration
- applies an idempotent migration
- compiles Python and parses templates before restart
- runs a read-only engine dry run
- recreates only the dashboard, employee app and new Operations Engine
- verifies HTTPS/secure cookies and localhost-only 8091
- runs the full City Manager OS health check

The worker does not change n8n source workflows, raw feeds, watchlist routing or notification destinations.
