(function (root, factory) {
  const content = factory();
  if (typeof module === 'object' && module.exports) module.exports = content;
  root.CmosAlertContent = content;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // Callers resolve the saved source/channel settings. Rendering never changes the alert.
  const DEFAULTS = Object.freeze({
    mapping_link: false, explanation: false, watch_names: false, keywords: false,
    source_link: true, pseg_etr: true, pseg_started: true, pseg_operations: true,
    pseg_damage: true, pseg_change: true, pseg_area: true
  });
  const PSEG_KEYS = ['pseg_etr', 'pseg_started', 'pseg_operations', 'pseg_damage', 'pseg_change', 'pseg_area'];
  const list = value => Array.isArray(value) ? value : [];
  const unique = values => [...new Set(values.filter(value => typeof value === 'string').map(value => value.trim()).filter(Boolean))];
  const mappingLine = /^Mapping Center:[ \t]+(https?:\/\/[^/\s]+\/map(?:[?#][^\s]*)?)[ \t]*$/;

  function legacySection(line) {
    if (/^ETR .+\.$/.test(line)) return 'pseg_etr';
    if (/^Started .+\.$/.test(line)) return 'pseg_started';
    if (/^(?:Jobs|Working|Circuits) [\d,]+(?: \| (?:Jobs|Working|Circuits) [\d,]+)*$/.test(line)) return 'pseg_operations';
    if (/^Damage: (?:Pending|Pole|Tree|Road) [\d,]+(?: \| (?:Pending|Pole|Tree|Road) [\d,]+)*$/.test(line)) return 'pseg_damage';
    if (/^Change [+-][\d,]+ since the previous check\.$/.test(line) || /^Reason: .+\.$/.test(line)) return 'pseg_change';
    if (/^Approximate outage area: .+\. Provider map areas are not customer addresses\.$/.test(line)
        || /^Approximate location: .+; not customer-specific\.$/.test(line)
        || line === 'Locations are approximate municipality areas, never customer-specific.') return 'pseg_area';
    return '';
  }

  function body(alert, options) {
    const raw = String(alert.message || '');
    const metadata = alert.metadata && typeof alert.metadata === 'object' ? alert.metadata : {};
    const pseg = String(alert.source || '').trim().toUpperCase() === 'PSEG';
    const sections = metadata.content_sections;
    let message;
    if (Array.isArray(sections) && sections.every(section => section && typeof section.text === 'string')
        && sections.map(section => section.text).join('') === raw) {
      message = sections.filter(section => {
        if (section.key === 'mapping_link') return options.mapping_link;
        return !pseg || !PSEG_KEYS.includes(section.key) || options[section.key];
      }).map(section => section.text).join('');
    } else {
      const legacy = pseg && !options.pseg_change
        ? raw.replace(/\r?\n\r?\nReason: [^\r\n]+\.(?=\r?\n|$)/g, '') : raw;
      message = legacy.split(/\r?\n/).filter(line => {
        if (mappingLine.test(line)) return options.mapping_link;
        const key = pseg ? legacySection(line) : '';
        return !key || options[key];
      }).map(line => pseg && !options.pseg_area
        ? line.replace(/^([\d,]+ customers out\.) Approximate municipality outage area; not customer-specific\.$/, '$1')
        : line).join('\n');
    }
    if (options.mapping_link && metadata.mapping_center_url
        && mappingLine.test('Mapping Center: ' + metadata.mapping_center_url)
        && !message.split(/\r?\n/).some(line => mappingLine.test(line))) {
      message += (message ? '\n' : '') + 'Mapping Center: ' + metadata.mapping_center_url;
    }
    return message.trim();
  }

  function reasonParts(reason) {
    const raw = String(reason || '');
    const clauses = [];
    let start = 0, quote = '';
    for (let index = 0; index < raw.length; index += 1) {
      const character = raw[index];
      if (quote && character === quote && raw[index - 1] !== '\\') quote = '';
      else if (!quote && ['"', "'", '“'].includes(character)) quote = character === '“' ? '”' : character;
      else if (!quote && character === ';') { clauses.push(raw.slice(start, index).trim()); start = index + 1; }
    }
    clauses.push(raw.slice(start).trim());
    return clauses.filter(Boolean).map(part => {
      const match = part.match(/(?:FIELD|CONTAINS|WORD|EXACT)\s+(\S+)\s+matched\s+(?:search_term|alias)\s+(["'])(.*)\2$/i);
      if (match) {
        const field = match[1].toLowerCase();
        if (['source', 'county', 'municipality'].includes(field)) {
          return {text: `Alert ${field} matched “${match[3]}”`};
        }
        return {text: 'The alert matched a saved topic rule.', keyword: match[3]};
      }
      const humanized = part.match(/(?:Alternate keyword|Keyword) “(.+)” matched this alert/i)?.[1]
        || part.match(/(?:Alternate keyword|Keyword) (["'])(.*?)\1 matched this alert/i)?.[2];
      if (humanized) return {text: 'The alert matched a saved topic rule.', keyword: humanized};
      const distance = part.match(/PROXIMITY alert geometry is ([0-9.]+) ft from target, inside ([0-9.]+) ft buffer/i);
      if (distance) return {text: `Alert Location was ${Math.round(Number(distance[1])).toLocaleString('en-US')} feet from the Watch center, within the ${Math.round(Number(distance[2])).toLocaleString('en-US')}-foot Distance`};
      if (/selected parcel or adjoining-parcel/i.test(part)) return {text: 'Alert Location matched the selected parcel or a neighboring parcel'};
      if (/intersected the selected reference/i.test(part)) return {text: 'Alert Location matched the selected map area'};
      const geography = part.match(/(municipality|county) matched ["']([^"']+)["']/i);
      if (geography) return {text: `Alert ${geography[1].toLowerCase()} matched “${geography[2]}”`};
      return {text: part.replace(/\bPROXIMITY\b/gi, 'Location')
        .replace(/\balert geometry\b/gi, 'Alert Location').replace(/\bsearch_text\b/gi, 'alert text')
        .replace(/\bsearch_term\b/gi, 'keyword').replace(/\btarget\b/gi, 'Watch center')
        .replace(/\bbuffer\b/gi, 'Distance')};
    });
  }

  function render(input, selected) {
    const alert = input && typeof input === 'object' ? input : {};
    const options = {...DEFAULTS};
    for (const key of Object.keys(DEFAULTS)) {
      if (selected && typeof selected[key] === 'boolean') options[key] = selected[key];
    }
    const evidence = list(alert.watch_evidence).filter(item => item && typeof item === 'object');
    const reasons = unique([...list(alert.match_reasons), ...evidence.map(item =>
      item.raw_match_reason || item.match_reason || item.reason || '')]);
    const parts = reasons.flatMap(reasonParts);
    const explanations = options.explanation ? unique(parts.map(part => part.text.slice(0, 240))).slice(0, 3) : [];
    const watchNames = options.watch_names ? unique([
      ...list(alert.matched_watch_names), ...evidence.map(item => item.watch_name || item.display_name || '')
    ]).slice(0, 20) : [];
    const keywords = options.keywords ? unique(parts.map(part => part.keyword)).slice(0, 20) : [];
    const blocks = [body(alert, options)];
    if (explanations.length) blocks.push('Why you received this:\n' + explanations.map(reason => '• ' + reason).join('\n'));
    if (watchNames.length) blocks.push('Matched Watches: ' + watchNames.join(', '));
    if (keywords.length) blocks.push('Matched keywords: ' + keywords.map(word => '“' + word + '”').join(', '));
    const mapUrl = (alert.metadata || {}).mapping_center_url;
    const legacyMapUrls = String(alert.message || '').split(/\r?\n/).map(line => line.match(mappingLine)?.[1]);
    const sourceClick = [alert.source_url, alert.click_url, alert.click].find(value =>
      value && value !== mapUrl && !legacyMapUrls.includes(value));
    return {
      message: blocks.filter(Boolean).join('\n\n'),
      click: options.source_link ? String(sourceClick || '') : '',
      explanations, watch_names: watchNames, keywords
    };
  }

  return {DEFAULTS, render};
});
