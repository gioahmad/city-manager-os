const settings = $input.first().json;
const input = settings.sender_input || settings;
const appearance = settings.alert_appearance || {};
const payloads = Array.isArray(input.delivery_payloads)
  ? input.delivery_payloads : input.ntfy_topic ? [input] : [];
if (!payloads.length) throw new Error('No delivery payloads found.');

return payloads.map((payload) => {
  if (!payload.ntfy_topic) throw new Error('Delivery payload is missing its notification topic.');
  const source = String(payload.source || '');
  const options = (appearance[source] || {}).notification || {};
  const rendered = CmosAlertContent.render(payload, options);
  const ntfyBody = {
    topic: payload.ntfy_topic,
    title: payload.title || 'City Manager OS',
    message: rendered.message,
    priority: Math.max(1, Math.min(5, Number(payload.priority) || 3)),
    tags: Array.isArray(payload.tags) ? payload.tags : []
  };
  if (rendered.click) ntfyBody.click = rendered.click;
  return {json: {...payload, ntfy_base_url: 'http://100.94.203.47:8080', ntfy_body: ntfyBody}};
});
