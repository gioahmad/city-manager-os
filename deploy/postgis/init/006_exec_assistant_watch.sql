INSERT INTO watch_items (
  watch_id,
  active,
  watch_type,
  display_name,
  search_term,
  match_mode,
  match_field,
  category,
  min_priority,
  notes
)
VALUES (
  'W_SOURCE_EXEC_ASSISTANT',
  true,
  'SOURCE',
  'Executive Assistant',
  'EXEC_ASSISTANT',
  'FIELD',
  'source',
  'EXECUTIVE',
  1,
  'Internal proactive open-loop resurfacing'
)
ON CONFLICT (watch_id) DO UPDATE SET
  active=EXCLUDED.active,
  display_name=EXCLUDED.display_name,
  search_term=EXCLUDED.search_term,
  match_mode=EXCLUDED.match_mode,
  match_field=EXCLUDED.match_field,
  category=EXCLUDED.category,
  min_priority=EXCLUDED.min_priority,
  notes=EXCLUDED.notes,
  updated_at=now();

INSERT INTO watch_item_recipients (
  watch_item_id,
  subscriber_id,
  active
)
SELECT
  wi.id,
  s.id,
  true
FROM watch_items wi
JOIN subscribers s
  ON s.subscriber_id='GIO_CATCHALL'
WHERE wi.watch_id='W_SOURCE_EXEC_ASSISTANT'
ON CONFLICT (watch_item_id, subscriber_id)
DO UPDATE SET active=true;
