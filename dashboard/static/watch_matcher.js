(function (root, factory) {
  const matcher = factory();
  if (typeof module === 'object' && module.exports) module.exports = matcher;
  root.CmosWatchMatcher = matcher;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const VERSION = 'watch-matcher-v2';

  function normalize(value) {
    if (value === undefined || value === null) return '';
    let text;
    if (Array.isArray(value)) text = value.join(' ');
    else if (typeof value === 'object') text = JSON.stringify(value);
    else text = String(value);
    return text
      .normalize('NFKD')
      .replace(/[\u0300-\u036f]/g, '')
      .toUpperCase()
      .replace(/&/g, '')
      .replace(/[^A-Z0-9]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function toArray(value) {
    if (value === undefined || value === null || value === '') return [];
    if (Array.isArray(value)) return value.filter(Boolean);
    if (typeof value === 'string') {
      return value.split('|').map((part) => part.trim()).filter(Boolean);
    }
    return [value];
  }

  function getPath(object, path) {
    if (!path) return undefined;
    return String(path).split('.').reduce((current, key) => {
      if (current === undefined || current === null) return undefined;
      return current[key];
    }, object);
  }

  function escapeRegExp(value) {
    return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function enabled(value, fallback) {
    if (value === undefined || value === null || value === '') return fallback;
    return value === true || value === 1 || normalize(value) === 'TRUE';
  }

  function prepareAlert(input) {
    const alert = input && typeof input === 'object' ? input : {};
    const location = alert.location && typeof alert.location === 'object'
      ? alert.location
      : {};
    const tags = Array.isArray(alert.tags)
      ? alert.tags
      : (alert.tags ? [String(alert.tags)] : []);
    const searchParts = [
      alert.source,
      alert.category,
      alert.subtype,
      alert.status,
      alert.county,
      alert.municipality,
      location.label,
      location.address,
      alert.title,
      alert.message,
      ...tags
    ].filter((value) => value !== undefined && value !== null && String(value).trim());
    return {
      ...alert,
      location,
      tags,
      priority: Number(alert.priority || 0),
      search_text: searchParts.join(' ')
    };
  }

  function gate(key, label, passed, detail, status) {
    return {
      key,
      label,
      passed: passed === undefined ? null : Boolean(passed),
      status: status || (passed ? 'PASS' : 'FAIL'),
      detail: detail || ''
    };
  }

  function textResult(alert, row) {
    const matchField = row.match_field || 'search_text';
    const haystack = normalize(getPath(alert, matchField));
    const candidates = [
      { value: row.search_term, kind: 'search_term' },
      ...toArray(row.aliases).map((value) => ({ value, kind: 'alias' }))
    ].map((candidate) => ({
      ...candidate,
      normalized: normalize(candidate.value)
    })).filter((candidate) => candidate.normalized);
    const mode = normalize(row.match_mode);

    for (const candidate of candidates) {
      let matched = false;
      if (mode === 'FIELD' || mode === 'EXACT') matched = haystack === candidate.normalized;
      else if (mode === 'CONTAINS') matched = haystack.includes(candidate.normalized);
      else if (mode === 'WORD') {
        matched = new RegExp(`(^|\\s)${escapeRegExp(candidate.normalized)}(?=\\s|$)`).test(haystack);
      }
      if (matched) {
        return {
          matched: true,
          match_field: matchField,
          match_mode: mode,
          matched_candidate: candidate.value,
          matched_candidate_type: candidate.kind,
          reason: `${mode} ${matchField} matched ${candidate.kind} \"${candidate.value}\"`
        };
      }
    }
    return {
      matched: false,
      match_field: matchField,
      match_mode: mode,
      reason: candidates.length
        ? `${mode || 'UNKNOWN'} ${matchField} did not match the saved topic or aliases`
        : 'No saved topic or alias was available to match'
    };
  }

  function deliveryGate(result, row, gates) {
    const recipients = toArray(row.recipients).filter((recipient) => (
      recipient && typeof recipient === 'object'
      && (recipient.subscriber_id || recipient.ntfy_topic)
    ));
    const routed = recipients.length > 0;
    gates.push(gate(
      'recipient_route',
      'Active recipient route',
      routed,
      routed
        ? `${recipients.length} active recipient${recipients.length === 1 ? '' : 's'} connected`
        : 'The Watch can match, but no active recipient is connected'
    ));

    if (!result.matched || !routed) {
      gates.push(gate(
        'delivery_guard',
        'Delivery Guard',
        undefined,
        !result.matched
          ? 'Not reached because the Watch did not match'
          : 'Not reached because no active recipient route exists',
        'NOT_REACHED'
      ));
      return {
        ...result,
        recipient_count: recipients.length,
        notification_ready: false,
        delivery_guard_eligible: false,
        gates
      };
    }

    gates.push(gate(
      'delivery_guard',
      'Delivery Guard',
      true,
      'Eligible for the live Delivery Guard check; this read-only test reserves nothing'
    ));
    return {
      ...result,
      recipient_count: recipients.length,
      notification_ready: true,
      delivery_guard_eligible: true,
      gates
    };
  }

  function evaluateWatch(inputAlert, row, options) {
    const alert = prepareAlert(inputAlert);
    const watch = row && typeof row === 'object' ? row : {};
    const now = new Date((options && options.now) || Date.now());
    const startsAt = watch.starts_at ? new Date(watch.starts_at) : null;
    const expiresAt = watch.expires_at ? new Date(watch.expires_at) : null;
    const active = enabled(watch.active, true);
    const scheduleOk = active
      && (!startsAt || Number.isNaN(startsAt.getTime()) || startsAt <= now)
      && (!expiresAt || Number.isNaN(expiresAt.getTime()) || expiresAt > now);
    const gates = [gate(
      'watch_state',
      'Watch active and in schedule',
      scheduleOk,
      !active
        ? 'Watch is paused'
        : startsAt && startsAt > now
          ? `Watch starts ${startsAt.toISOString()}`
          : expiresAt && expiresAt <= now
            ? `Watch expired ${expiresAt.toISOString()}`
            : 'Watch is active now'
    )];

    const alertPriority = Number(alert.priority || 0);
    const minimumPriority = Number(watch.min_priority || 1);
    const priorityOk = alertPriority >= minimumPriority;
    gates.push(gate(
      'priority',
      'Minimum priority',
      priorityOk,
      priorityOk
        ? `P${alertPriority} meets minimum P${minimumPriority}`
        : `P${alertPriority} is below minimum P${minimumPriority}`
    ));

    const sourceFilters = toArray(watch.source_filter).map(normalize);
    const sourceOk = !sourceFilters.length || sourceFilters.includes(normalize(alert.source));
    gates.push(gate(
      'source',
      'Alert source filter',
      sourceOk,
      sourceFilters.length
        ? `${alert.source || 'blank source'} ${sourceOk ? 'is allowed by' : 'is not in'} ${toArray(watch.source_filter).join(', ')}`
        : 'No source restriction'
    ));

    const categoryFilters = toArray(watch.alert_category_filter).map(normalize);
    const categoryOk = !categoryFilters.length || categoryFilters.includes(normalize(alert.category));
    gates.push(gate(
      'category',
      'Alert category filter',
      categoryOk,
      categoryFilters.length
        ? `${alert.category || 'blank category'} ${categoryOk ? 'is allowed by' : 'is not in'} ${toArray(watch.alert_category_filter).join(', ')}`
        : 'No category restriction'
    ));

    const nearby = enabled(watch.nearby_enabled, false);
    const watchType = normalize(watch.watch_type);
    const locationPlusTopic = watchType === 'LOCATION TOPIC';
    const municipalityOnly = !nearby && watchType === 'TOWN';
    const locationRequired = nearby || locationPlusTopic || municipalityOnly;
    const topicRequired = locationPlusTopic || (!nearby && !municipalityOnly);
    const explicitAlertGeometry = watch.alert_geometry_ready;
    const explicitWatchTarget = watch.watch_target_ready;

    if (nearby || locationPlusTopic) {
      gates.push(gate(
        'alert_geometry',
        'Effective alert point',
        explicitAlertGeometry === undefined ? undefined : enabled(explicitAlertGeometry, false),
        watch.effective_geometry_source
          ? `Using ${watch.effective_geometry_source}`
          : 'The central PostGIS matcher evaluates the stored, supplied, then resolver point',
        explicitAlertGeometry === undefined
          ? 'INFO'
          : enabled(explicitAlertGeometry, false) ? 'PASS' : 'FAIL'
      ));
      gates.push(gate(
        'watch_target',
        'Saved Watch area',
        explicitWatchTarget === undefined ? undefined : enabled(explicitWatchTarget, false),
        explicitWatchTarget === undefined
          ? 'The central PostGIS matcher evaluates the saved Watch geometry'
          : enabled(explicitWatchTarget, false) ? 'Saved Watch geometry is ready' : 'Saved Watch geometry is missing',
        explicitWatchTarget === undefined
          ? 'INFO'
          : enabled(explicitWatchTarget, false) ? 'PASS' : 'FAIL'
      ));
    } else {
      gates.push(gate('alert_geometry', 'Effective alert point', undefined, 'Not required for this Watch', 'NOT_APPLICABLE'));
      gates.push(gate('watch_target', 'Saved Watch area', undefined, 'Not required for this Watch', 'NOT_APPLICABLE'));
    }

    const municipalityMatch = municipalityOnly || locationPlusTopic
      ? Boolean(normalize(watch.municipality) && normalize(watch.municipality) === normalize(alert.municipality))
      : false;
    const spatialMatch = Boolean(watch.spatial_match_type);
    const measuredSpatialMatch = watch.point_inside_watch === undefined
      ? spatialMatch
      : enabled(watch.point_inside_watch, false);
    const locationMatched = spatialMatch || (!nearby && municipalityMatch);
    const locationGateMatched = nearby ? measuredSpatialMatch : locationMatched;
    const locationReason = watch.spatial_match_reason
      || (municipalityMatch ? `municipality matched \"${watch.municipality}\"` : '')
      || (watch.distance_ft !== undefined && watch.distance_ft !== null
        ? `alert point is ${Number(watch.distance_ft).toFixed(1)} ft from the Watch target`
        : 'alert point is outside the saved Watch area');
    gates.push(gate(
      'location',
      'Location or boundary',
      locationRequired ? locationGateMatched : undefined,
      locationRequired ? locationReason : 'Not required for this Watch',
      locationRequired ? (locationGateMatched ? 'PASS' : 'FAIL') : 'NOT_APPLICABLE'
    ));

    const topic = textResult(alert, watch);
    gates.push(gate(
      'topic',
      'Topic or field match',
      topicRequired ? topic.matched : undefined,
      topicRequired ? topic.reason : 'Not required for this location-only Watch',
      topicRequired ? (topic.matched ? 'PASS' : 'FAIL') : 'NOT_APPLICABLE'
    ));

    const matched = scheduleOk && priorityOk && sourceOk && categoryOk
      && (!locationRequired || locationMatched)
      && (!topicRequired || topic.matched);
    let result = { matched };
    if (matched && spatialMatch && !locationPlusTopic) {
      result = {
        matched: true,
        match_type: watch.spatial_match_type,
        matched_candidate: 'trusted alert geometry',
        matched_candidate_type: 'geometry',
        match_field: 'geom',
        match_reason: watch.spatial_match_reason,
        distance_ft: watch.spatial_distance_ft
      };
    } else if (matched && municipalityOnly) {
      result = {
        matched: true,
        match_type: 'FIELD',
        matched_candidate: watch.municipality,
        matched_candidate_type: 'location',
        match_field: 'municipality',
        match_reason: `FIELD municipality matched search_term \"${watch.municipality}\"`
      };
    } else if (matched) {
      result = {
        matched: true,
        match_type: locationPlusTopic ? 'LOCATION_TOPIC' : undefined,
        matched_candidate: topic.matched_candidate,
        matched_candidate_type: topic.matched_candidate_type,
        match_field: topic.match_field,
        match_reason: locationPlusTopic ? `${locationReason}; ${topic.reason}` : topic.reason,
        distance_ft: watch.spatial_distance_ft
      };
    } else {
      const failed = gates.find((item) => item.status === 'FAIL');
      result.skip_reason = failed ? failed.detail : 'Watch did not match';
    }

    gates.push(gate(
      'matcher_result',
      'Watch match decision',
      matched,
      matched ? (result.match_reason || 'All required Watch conditions passed') : result.skip_reason
    ));
    return deliveryGate(result, watch, gates);
  }

  return {
    version: VERSION,
    normalize,
    toArray,
    prepareAlert,
    evaluateWatch
  };
});
