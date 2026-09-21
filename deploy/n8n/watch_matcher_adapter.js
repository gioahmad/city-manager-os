const alert = CmosWatchMatcher.prepareAlert($('Normalize Alert').first().json);
const rows = $input.all()
  .map((item) => item.json)
  .filter((row) => row && row.watch_id);
const matches = [];
const recipientMap = new Map();

for (const row of rows) {
  const result = CmosWatchMatcher.evaluateWatch(alert, row);
  if (!result.matched) continue;
  matches.push({
    watch_item_uuid: row.watch_item_uuid || row.id || null,
    watch_id: row.watch_id,
    display_name: row.display_name,
    watch_type: row.watch_type,
    match_mode: result.match_type || row.match_mode,
    match_field: result.match_field,
    matched_candidate: result.matched_candidate,
    matched_candidate_type: result.matched_candidate_type,
    match_reason: result.match_reason
  });

  for (const recipient of CmosWatchMatcher.toArray(row.recipients)) {
    if (!recipient || typeof recipient !== 'object') continue;
    if (!recipient.subscriber_id && !recipient.ntfy_topic) continue;
    const key = recipient.subscriber_id || recipient.ntfy_topic;
    if (!recipientMap.has(key)) {
      recipientMap.set(key, {
        subscriber_uuid: recipient.subscriber_uuid || null,
        subscriber_id: recipient.subscriber_id || null,
        name: recipient.name || null,
        ntfy_topic: recipient.ntfy_topic || null,
        matched_watch_ids: [],
        match_reasons: []
      });
    }
    const resolved = recipientMap.get(key);
    if (!resolved.matched_watch_ids.includes(row.watch_id)) {
      resolved.matched_watch_ids.push(row.watch_id);
    }
    if (!resolved.match_reasons.includes(result.match_reason)) {
      resolved.match_reasons.push(result.match_reason);
    }
  }
}

const recipients = Array.from(recipientMap.values());
const delivery_payloads = recipients.map((recipient) => ({
  alert_id: alert.alert_id,
  subscriber_id: recipient.subscriber_id,
  subscriber_name: recipient.name,
  ntfy_topic: recipient.ntfy_topic,
  title: alert.title,
  message: alert.message,
  priority: alert.priority,
  tags: alert.tags || [],
  click: alert.click_url || '',
  matched_watch_ids: recipient.matched_watch_ids,
  match_reasons: recipient.match_reasons
}));

return [{
  json: {
    alert,
    matched: matches.length > 0,
    match_count: matches.length,
    recipient_count: recipients.length,
    matched_watch_ids: matches.map((match) => match.watch_id),
    matches,
    recipients,
    delivery_payloads
  }
}];
