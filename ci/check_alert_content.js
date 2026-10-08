// Pure presentation checks: no network, matching, delivery, or storage writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const rendererPath = path.join(__dirname, '../dashboard/static/alert_content.js');
const {render, DEFAULTS} = require(rendererPath);
const source = fs.readFileSync(rendererPath, 'utf8');
const browser = {};
vm.runInNewContext(source, browser);
assert.equal(typeof browser.CmosAlertContent.render, 'function');
assert.equal(DEFAULTS.mapping_link, false);
assert.equal(DEFAULTS.source_link, true);

const message = [
  '25 of 4,000 customers out in Union City.', 'ETR 10/08 3:30 PM.', 'Started 10/08 1:00 PM.',
  'Jobs 2 | Working 1 | Circuits 1', 'Damage: Pending 1 | Tree 1',
  'Change +25 since the previous check.',
  'Approximate outage area: Central Union City. Provider map areas are not customer addresses.',
  'Mapping Center: http://100.94.203.47:8090/map'
].join('\n');
const keys = ['core', 'pseg_etr', 'pseg_started', 'pseg_operations', 'pseg_damage', 'pseg_change', 'pseg_area', 'mapping_link'];
const sections = message.split('\n').map((text, index) => ({key: keys[index], text: (index ? '\n' : '') + text}));
const alert = {source: 'PSEG', message, click_url: 'https://outagecenter.pseg.com/',
  metadata: {content_sections: sections, mapping_center_url: 'http://100.94.203.47:8090/map'},
  match_reasons: ['CONTAINS search_text matched search_term "outage"; county matched "Hudson"'],
  watch_evidence: [{watch_name: 'Utility Watch', raw_match_reason: 'WORD search_text matched alias "power restored"'}]};
const original = JSON.stringify(alert);
const defaults = render(alert);
assert.equal(defaults.message, message.split('\n').slice(0, -1).join('\n'));
assert.equal(defaults.click, alert.click_url);
assert.deepEqual(defaults.explanations, []);
assert.equal(render(alert, {mapping_link: true}).message, message);
assert.equal(render(alert, {source_link: false}).click, '');
assert.equal(render({...alert, click_url: alert.metadata.mapping_center_url}).click, '');
assert.equal(render({...alert, click_url: alert.metadata.mapping_center_url, source_url: 'https://provider.example/outage'}).click, 'https://provider.example/outage');
for (const key of keys.slice(1, -1)) {
  const rendered = render(alert, {[key]: false});
  assert.ok(!rendered.message.includes(sections.find(section => section.key === key).text.trim()), key);
  assert.ok(rendered.message.includes('25 of 4,000 customers out'), key);
  assert.equal(render({...alert, metadata: {}}, {[key]: false}).message, rendered.message, key + ' legacy parity');
}
const allOff = Object.fromEntries(Object.keys(DEFAULTS).map(key => [key, false]));
assert.equal(render(alert, allOff).message, message.split('\n')[0]);
assert.equal(render({...alert, metadata: {}}, allOff).message, message.split('\n')[0]);
assert.equal(JSON.stringify(alert), original, 'render must preserve raw alert/evidence');

const stale = {...alert, message: 'Corrected operator message', metadata: alert.metadata};
assert.equal(render(stale).message, stale.message, 'stale structured content must not replace corrections');
const unknown = {source: 'CUSTOM', message: 'ETR is a team name.\nDamage: Pending 1\nMapping Center: our meeting room'};
assert.equal(render(unknown, allOff).message, unknown.message, 'unrecognized source content is retained');
const extended = {...alert, message: message + '\nProvider note', metadata: {content_sections: [
  ...sections, {key: 'future_section', text: '\nProvider note'}]}};
assert.ok(render(extended, allOff).message.endsWith('Provider note'), 'unknown metadata sections are retained');
const mapAlert = {source: 'PSEG', message: '12 customers out. Approximate municipality outage area; not customer-specific.'};
assert.equal(render(mapAlert, {pseg_area: false}).message, '12 customers out.');
assert.equal(render({source: 'PSEG', message: 'RESTORED\nUnion City\n\nReason: Restoration.\nLocations are approximate municipality areas, never customer-specific.'}, allOff).message, 'RESTORED\nUnion City');

const explained = render(alert, {explanation: true, watch_names: true});
assert.ok(explained.message.includes('The alert matched a saved topic rule.'));
assert.ok(explained.message.includes('Alert county matched “Hudson”'));
assert.ok(!explained.message.includes('“outage”') && !explained.message.includes('“power restored”'));
assert.deepEqual(explained.watch_names, ['Utility Watch']);
const words = render(alert, {explanation: true, keywords: true});
assert.deepEqual(words.keywords, ['outage', 'power restored']);
assert.equal(words.message.split('“outage”').length - 1, 1);
assert.equal(words.message.split('“power restored”').length - 1, 1);
const quoted = {message: 'Raw body', match_reasons: ['WORD search_text matched alias "chief\'s office; fire"; county matched "Hudson"']};
assert.deepEqual(render(quoted, {keywords: true}).keywords, ["chief's office; fire"]);
assert.ok(!render(quoted, {explanation: true}).message.includes("chief's"));
const humanized = {message: 'Raw body', watch_evidence: [{reason: 'Keyword “chief\'s office; fire” matched this alert'}]};
assert.deepEqual(render(humanized, {keywords: true}).keywords, ["chief's office; fire"]);
assert.ok(!render(humanized, {explanation: true}).message.includes("chief's"));
const sourceRule = {message: 'Raw body', match_reasons: ['FIELD source matched search_term "BNN"',
  'FIELD county matched search_term "Hudson"', 'FIELD municipality matched alias "Union City"']};
const fieldResult = render(sourceRule, {keywords: true, explanation: true});
assert.deepEqual(fieldResult.keywords, []);
assert.ok(fieldResult.message.includes('Alert source matched “BNN”'));
const preferred = render({message: 'Raw body', watch_evidence: [{watch_name: 'BNN',
  raw_match_reason: 'FIELD source matched search_term "BNN"', reason: 'Keyword “BNN” matched this alert'}]},
  {keywords: true, explanation: true});
assert.deepEqual(preferred.keywords, [], 'raw field reason takes precedence over old humanization');
assert.deepEqual(render({message: 'Raw body', aliases: ['never matched'], search_term: 'whole rule'}, {keywords: true}).keywords, []);
assert.equal(browser.CmosAlertContent.render(alert, {keywords: true}).message, render(alert, {keywords: true}).message);
console.log('ALERT CONTENT: PASS — source/channel options, PSEG structured/legacy parity, stale metadata, matching evidence, source links, and immutable raw data');
