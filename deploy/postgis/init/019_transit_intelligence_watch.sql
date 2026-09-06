-- Create one reusable Master Watchlist rule for high-impact transit intelligence.
-- Recipient routing remains dynamic and is configured through Alert Admin / Routing.
INSERT INTO watch_items (
  watch_id, active, watch_type, display_name, search_term,
  aliases, match_mode, match_field, category, tags, min_priority,
  municipality, source_notes, notes
)
VALUES (
  'CMOS_TRANSIT_INTELLIGENCE_HIGH',
  true,
  'SOURCE',
  'High Impact Regional Transit Intelligence',
  'TRANSIT_INTELLIGENCE',
  ARRAY[]::text[],
  'FIELD',
  'source',
  'TRANSIT',
  ARRAY['transit-intelligence','regional-transit']::text[],
  4,
  'Weehawken',
  'Regional transit intelligence center',
  'High-impact regional transit conditions. Assign recipients through Alert Admin or Routing; do not hard-code destinations.'
)
ON CONFLICT (watch_id) DO UPDATE SET
  active = true,
  watch_type = EXCLUDED.watch_type,
  display_name = EXCLUDED.display_name,
  search_term = EXCLUDED.search_term,
  aliases = EXCLUDED.aliases,
  match_mode = EXCLUDED.match_mode,
  match_field = EXCLUDED.match_field,
  category = EXCLUDED.category,
  tags = EXCLUDED.tags,
  min_priority = EXCLUDED.min_priority,
  municipality = EXCLUDED.municipality,
  source_notes = EXCLUDED.source_notes,
  notes = EXCLUDED.notes,
  updated_at = now();
