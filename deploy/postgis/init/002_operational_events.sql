CREATE TABLE IF NOT EXISTS operational_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  active boolean NOT NULL DEFAULT true,
  title text NOT NULL,
  category text,
  location_name text,
  address text,
  municipality text NOT NULL DEFAULT 'Weehawken',
  starts_at timestamptz NOT NULL,
  ends_at timestamptz,
  priority integer NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
  source text NOT NULL DEFAULT 'MANUAL',
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_operational_events_active_time
  ON operational_events(active, starts_at);

CREATE INDEX IF NOT EXISTS idx_operational_events_municipality
  ON operational_events(municipality);
