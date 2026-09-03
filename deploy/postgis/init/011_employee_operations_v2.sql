CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS staff_employees (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id text NOT NULL UNIQUE,
    full_name text NOT NULL,
    department text NOT NULL,
    role text NOT NULL DEFAULT 'EMPLOYEE',
    pin_hash text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS staff_locations (
    id bigserial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    department text,
    active boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 100,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS staff_work_types (
    id bigserial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    department text,
    checklist_template text NOT NULL DEFAULT 'GENERAL',
    priority_normal integer NOT NULL DEFAULT 2,
    priority_attention integer NOT NULL DEFAULT 4,
    priority_emergency integer NOT NULL DEFAULT 5,
    active boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 100,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE issues
    ADD COLUMN IF NOT EXISTS submitted_employee_id uuid
        REFERENCES staff_employees(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS assigned_employee_id uuid
        REFERENCES staff_employees(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS staff_location_id bigint
        REFERENCES staff_locations(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS staff_work_type_id bigint
        REFERENCES staff_work_types(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS help_reason text;

CREATE INDEX IF NOT EXISTS idx_issues_submitted_employee
    ON issues(submitted_employee_id);

CREATE INDEX IF NOT EXISTS idx_issues_assigned_employee
    ON issues(assigned_employee_id);

CREATE TABLE IF NOT EXISTS issue_photos (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    issue_id uuid NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    uploaded_by_employee_id uuid
        REFERENCES staff_employees(id) ON DELETE SET NULL,
    phase text NOT NULL DEFAULT 'BEFORE',
    original_name text,
    stored_name text NOT NULL UNIQUE,
    content_type text NOT NULL,
    size_bytes bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_issue_photos_issue
    ON issue_photos(issue_id, created_at);

INSERT INTO staff_locations(name, sort_order)
VALUES
    ('Town Hall',10),
    ('DPW Garage',20),
    ('Waterfront Park',30),
    ('Hamilton Park',40),
    ('Weehawken Waterfront',50),
    ('Municipal Building / Office',60),
    ('Street / Public Right of Way',70),
    ('Other / Enter Location',999)
ON CONFLICT (name) DO NOTHING;

INSERT INTO staff_work_types(
    name,
    checklist_template,
    priority_normal,
    priority_attention,
    priority_emergency,
    sort_order
)
VALUES
    ('Cleaning','GENERAL',2,3,5,10),
    ('Electrical','GENERAL',2,4,5,20),
    ('Plumbing','GENERAL',2,4,5,30),
    ('HVAC','GENERAL',2,4,5,40),
    ('Grounds / Landscaping','GENERAL',2,3,5,50),
    ('Garbage / Sanitation','GENERAL',2,3,5,60),
    ('Building Repair','GENERAL',2,4,5,70),
    ('Vehicle / Equipment','VEHICLE',2,4,5,80),
    ('Street / Sidewalk','SITE_CHECK',2,4,5,90),
    ('Park','SITE_CHECK',2,3,5,100),
    ('Event Setup','EVENT_SETUP',2,4,5,110),
    ('Opening / Closing','OPEN_CLOSE',2,4,5,120),
    ('Safety Hazard','SITE_CHECK',4,5,5,130),
    ('Other','GENERAL',2,4,5,999)
ON CONFLICT (name) DO NOTHING;
