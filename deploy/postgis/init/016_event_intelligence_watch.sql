-- Create one reusable watch rule for high-impact event intelligence.
-- No recipient is assigned here. Recipient routing remains dynamic and must be
-- configured through Alert Admin / Routing using existing subscribers.
INSERT INTO watch_items (
  watch_id, active, watch_type, display_name, search_term,
  aliases, match_mode, match_field, category, tags, min_priority,
  municipality, source_notes, notes
)
VALUES (
  'CMOS_EVENT_INTELLIGENCE_HIGH',
  true,
  'SOURCE',
  'High Impact Regional Event Intelligence',
  'EVENT_INTELLIGENCE',
  ARRAY[]::text[],
  'FIELD',
  'source',
  'EVENTS',
  ARRAY['event-intelligence','regional-event']::text[],
  4,
  'Weehawken',
  'Regional event intelligence scanner',
  'High-impact regional events. Assign recipients through Alert Admin or Routing; do not hard-code destinations.'
)
ON CONFLICT (watch_id) DO UPDATE SET
  active = true,
  watch_type = EXCLUDED.watch_type,
  display_name = EXCLUDED.display_name,
  search_term = EXCLUDED.search_term,
  match_mode = EXCLUDED.match_mode,
  match_field = EXCLUDED.match_field,
  category = EXCLUDED.category,
  tags = EXCLUDED.tags,
  min_priority = EXCLUDED.min_priority,
  source_notes = EXCLUDED.source_notes,
  notes = EXCLUDED.notes,
  updated_at = now();
