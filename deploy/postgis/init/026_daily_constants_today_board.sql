-- #52 Daily Constants / Today Board
-- Informational state only. This does not create a new task system.

CREATE TABLE IF NOT EXISTS daily_constant_rules (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_key text UNIQUE NOT NULL,
  name text NOT NULL,
  category text NOT NULL DEFAULT 'OTHER',
  rule_type text NOT NULL,
  active boolean NOT NULL DEFAULT true,
  pinned boolean NOT NULL DEFAULT false,
  sort_order integer NOT NULL DEFAULT 100,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  display_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_label text,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (rule_type IN ('WEEKDAY','CYCLE','DATE_PATTERN','SEASON','MANUAL','DERIVED'))
);

CREATE INDEX IF NOT EXISTS idx_daily_constant_rules_display
  ON daily_constant_rules(active, pinned DESC, sort_order, category, name);

CREATE TABLE IF NOT EXISTS daily_constant_overrides (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_id uuid NOT NULL REFERENCES daily_constant_rules(id) ON DELETE CASCADE,
  starts_at timestamptz NOT NULL,
  ends_at timestamptz NOT NULL,
  value jsonb NOT NULL,
  detail text,
  reason text NOT NULL,
  created_by text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS idx_daily_constant_overrides_active
  ON daily_constant_overrides(rule_id, starts_at, ends_at);

-- Starter cards. Values requiring the user's exact operational schedule are
-- intentionally visible as SETUP rather than guessed or hard-coded.
INSERT INTO daily_constant_rules (
  rule_key,name,category,rule_type,active,pinned,sort_order,config,source_label,notes
)
VALUES
(
  'FIRE_DUTY_GROUP','Fire Duty Group','PUBLIC_SAFETY','CYCLE',true,true,10,
  '{"needs_setup":true,"setup_value":"SET ROTATION","sequence":[],"day_boundary":"07:00","detail":"Configure anchor date and exact group sequence in Today Board Admin."}'::jsonb,
  'NHRFR duty rotation',
  'Do not guess the group sequence. Configure the known anchor date and ordered group cycle in the browser.'
),
(
  'POLICE_DUTY_SQUADS','Police Duty Squads','PUBLIC_SAFETY','CYCLE',true,true,20,
  '{"needs_setup":true,"setup_value":"SET ROTATION","sequence":[],"day_boundary":"07:00","detail":"Configure the 8-day ACE/BDF rotation anchor. Shift detail can be stored with each cycle value."}'::jsonb,
  'Weehawken Police duty rotation',
  'Supports values such as A / C / E with attached 07-17, 16-02 and 23-09 shift detail.'
),
(
  'EMS_STAFFING','EMS Staffing','PUBLIC_SAFETY','MANUAL',true,true,30,
  '{"value":"API NOT CONNECTED","needs_setup":true,"detail":"EMS is API-ready. Later switch this card to DERIVED without redesigning the Today Board."}'::jsonb,
  'EMS staffing API (future)',
  'Future API should preserve last-known staffing and surface STALE/SOURCE ERROR rather than silently clearing crews.'
),
(
  'GARBAGE_TODAY','Garbage Collection','SANITATION_DPW','MANUAL',true,true,40,
  '{"value":"SET SCHEDULE","needs_setup":true,"detail":"Convert to WEEKDAY or DATE_PATTERN after entering the Township collection schedule."}'::jsonb,
  'Township sanitation schedule',NULL
),
(
  'RECYCLING_TODAY','Recycling','SANITATION_DPW','MANUAL',true,true,50,
  '{"value":"SET SCHEDULE","needs_setup":true,"detail":"Convert to WEEKDAY or DATE_PATTERN after entering the Township recycling schedule."}'::jsonb,
  'Township recycling schedule',NULL
),
(
  'REGIONAL_EVENTS_TODAY','Regional Events','EVENTS','DERIVED',true,false,100,
  '{"derived_key":"EVENTS_TODAY","unavailable_value":"0"}'::jsonb,
  'event_intelligence',
  'Counts active regional events overlapping the selected day.'
),
(
  'EVENT_WATCHES_TODAY','Event Watch / Alert','EVENTS','DERIVED',true,false,110,
  '{"derived_key":"EVENT_WATCHES_TODAY","unavailable_value":"0"}'::jsonb,
  'event_intelligence',
  'Counts WATCH and ALERT regional events overlapping the selected day.'
),
(
  'OPERATIONS_EXCEPTIONS_TODAY','Operations Exceptions','OPERATIONS','DERIVED',true,false,120,
  '{"derived_key":"OPERATIONS_EXCEPTIONS","unavailable_value":"0"}'::jsonb,
  'operations_routine_runs',
  'Counts missed/exception/help-needed recurring operations for the selected day.'
)
ON CONFLICT (rule_key) DO NOTHING;

GRANT SELECT, INSERT, UPDATE, DELETE ON daily_constant_rules TO citymanager_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON daily_constant_overrides TO citymanager_app;
