-- #26 Events Center
-- Extend operational_events. Do not create a parallel event/task system.

ALTER TABLE operational_events
  ADD COLUMN IF NOT EXISTS confirmation_status text NOT NULL DEFAULT 'CONFIRMED',
  ADD COLUMN IF NOT EXISTS waiting_on text,
  ADD COLUMN IF NOT EXISTS preparation_status text NOT NULL DEFAULT 'NOT_STARTED',
  ADD COLUMN IF NOT EXISTS agencies_involved text,
  ADD COLUMN IF NOT EXISTS reference_links text,
  ADD COLUMN IF NOT EXISTS preparation_checklist jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS reminder_at timestamptz;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname='operational_events_confirmation_status_check'
  ) THEN
    ALTER TABLE operational_events
      ADD CONSTRAINT operational_events_confirmation_status_check
      CHECK (
        confirmation_status IN (
          'CONFIRMED',
          'AWAITING_CONFIRMATION',
          'TENTATIVE',
          'NOT_REQUIRED'
        )
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname='operational_events_preparation_status_check'
  ) THEN
    ALTER TABLE operational_events
      ADD CONSTRAINT operational_events_preparation_status_check
      CHECK (
        preparation_status IN (
          'NOT_STARTED',
          'IN_PROGRESS',
          'READY',
          'NOT_REQUIRED'
        )
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname='operational_events_event_status_check'
  ) THEN
    ALTER TABLE operational_events
      ADD CONSTRAINT operational_events_event_status_check
      CHECK (
        event_status IN (
          'PLANNING',
          'TRACKING',
          'CONFIRMED',
          'COMPLETED',
          'CANCELLED'
        )
      );
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname='operational_events_preparation_checklist_check'
  ) THEN
    ALTER TABLE operational_events
      ADD CONSTRAINT operational_events_preparation_checklist_check
      CHECK (
        jsonb_typeof(preparation_checklist)='array'
      );
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_operational_events_preparation
  ON operational_events(
    active,
    preparation_status,
    starts_at
  );

CREATE INDEX IF NOT EXISTS idx_operational_events_confirmation
  ON operational_events(
    active,
    confirmation_status,
    starts_at
  );

CREATE INDEX IF NOT EXISTS idx_operational_events_reminder
  ON operational_events(reminder_at)
  WHERE active=true
    AND reminder_at IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE
ON operational_events
TO citymanager_app;
