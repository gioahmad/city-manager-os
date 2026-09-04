ALTER TABLE operations_routines
    ADD COLUMN IF NOT EXISTS starts_on date,
    ADD COLUMN IF NOT EXISTS ends_on date;

ALTER TABLE operations_routine_runs
    ADD COLUMN IF NOT EXISTS run_note text,
    ADD COLUMN IF NOT EXISTS run_note_by text,
    ADD COLUMN IF NOT EXISTS run_note_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_operations_routines_date_window
    ON operations_routines(starts_on, ends_on)
    WHERE active = true;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'operations_routines_valid_window'
    ) THEN
        ALTER TABLE operations_routines
            ADD CONSTRAINT operations_routines_valid_window
            CHECK (
                starts_on IS NULL
                OR ends_on IS NULL
                OR ends_on >= starts_on
            );
    END IF;
END;
$$;
