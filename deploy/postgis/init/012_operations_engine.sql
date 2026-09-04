CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS operations_routines (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    active boolean NOT NULL DEFAULT true,
    name text NOT NULL,
    routine_kind text NOT NULL DEFAULT 'WORK'
        CHECK (routine_kind IN ('WORK','AWARENESS')),
    department text,
    location_id bigint REFERENCES staff_locations(id) ON DELETE SET NULL,
    location_label text,
    work_type_id bigint REFERENCES staff_work_types(id) ON DELETE SET NULL,
    assigned_employee_id uuid REFERENCES staff_employees(id) ON DELETE SET NULL,
    scheduled_time time NOT NULL,
    days_of_week smallint[] NOT NULL DEFAULT ARRAY[1,2,3,4,5,6,7]::smallint[],
    lead_minutes integer NOT NULL DEFAULT 60 CHECK (lead_minutes BETWEEN 0 AND 1440),
    grace_minutes integer NOT NULL DEFAULT 15 CHECK (grace_minutes BETWEEN 0 AND 1440),
    display_before_minutes integer NOT NULL DEFAULT 30 CHECK (display_before_minutes BETWEEN 0 AND 1440),
    display_after_minutes integer NOT NULL DEFAULT 60 CHECK (display_after_minutes BETWEEN 0 AND 2880),
    priority integer NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 5),
    confirmation_required boolean NOT NULL DEFAULT false,
    escalate_if_missed boolean NOT NULL DEFAULT false,
    verification_required boolean NOT NULL DEFAULT true,
    checklist_items text[] NOT NULL DEFAULT '{}',
    description text,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_operations_routines_active
    ON operations_routines(active, routine_kind);
CREATE INDEX IF NOT EXISTS idx_operations_routines_employee
    ON operations_routines(assigned_employee_id);

CREATE TABLE IF NOT EXISTS operations_routine_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    routine_id uuid NOT NULL REFERENCES operations_routines(id) ON DELETE CASCADE,
    service_date date NOT NULL,
    scheduled_for timestamptz NOT NULL,
    due_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'SCHEDULED',
    issue_id uuid REFERENCES issues(id) ON DELETE SET NULL,
    acknowledged_at timestamptz,
    acknowledged_by text,
    exception_note text,
    exception_issue_id uuid REFERENCES issues(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (routine_id, service_date)
);

CREATE INDEX IF NOT EXISTS idx_operations_runs_today
    ON operations_routine_runs(service_date, status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_operations_runs_issue
    ON operations_routine_runs(issue_id);

ALTER TABLE issues
    ADD COLUMN IF NOT EXISTS operations_routine_id uuid
        REFERENCES operations_routines(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS operations_run_id uuid
        REFERENCES operations_routine_runs(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS verification_required boolean,
    ADD COLUMN IF NOT EXISTS verification_pending boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS verified_at timestamptz,
    ADD COLUMN IF NOT EXISTS verified_by text,
    ADD COLUMN IF NOT EXISTS verification_note text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_issues_operations_run_unique
    ON issues(operations_run_id)
    WHERE operations_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_issues_verification_pending
    ON issues(verification_pending, status)
    WHERE verification_pending = true;

CREATE OR REPLACE FUNCTION cmos_hold_completion_for_verification()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IN ('RESOLVED','CLOSED')
       AND OLD.status NOT IN ('RESOLVED','CLOSED')
       AND COALESCE(OLD.verification_required, OLD.source = 'EMPLOYEE_PORTAL')
       AND NEW.verified_at IS NULL THEN
        NEW.status := 'PENDING_VERIFICATION';
        NEW.verification_pending := true;
        NEW.closed_at := NULL;
        NEW.next_action := 'Awaiting supervisor verification';
        NEW.waiting_on := 'Supervisor verification';
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_cmos_hold_completion_for_verification ON issues;
CREATE TRIGGER trg_cmos_hold_completion_for_verification
BEFORE UPDATE OF status ON issues
FOR EACH ROW
EXECUTE FUNCTION cmos_hold_completion_for_verification();
