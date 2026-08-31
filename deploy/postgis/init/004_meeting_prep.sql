ALTER TABLE operational_events
  ADD COLUMN IF NOT EXISTS attendees text,
  ADD COLUMN IF NOT EXISTS objective text,
  ADD COLUMN IF NOT EXISTS prep_notes text,
  ADD COLUMN IF NOT EXISTS decisions_needed text,
  ADD COLUMN IF NOT EXISTS debrief_notes text;
