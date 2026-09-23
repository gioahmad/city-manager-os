import json
import shutil
import subprocess
from pathlib import Path
from unittest import SkipTest


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = DASHBOARD_ROOT.parent


def _repository_file(relative_path: str) -> Path:
    path = REPOSITORY_ROOT / relative_path
    if not path.is_file():
        raise SkipTest("repository source is outside the dashboard-only test mount")
    return path


def _workflow_node(relative_path: str, name: str) -> dict:
    payload = json.loads(_repository_file(relative_path).read_text())
    workflow = payload[0] if isinstance(payload, list) else payload
    return next(node for node in workflow["nodes"] if node.get("name") == name)


def test_watch_lab_endpoint_is_read_only_and_uses_canonical_spatial_matcher():
    source = (DASHBOARD_ROOT / "spatial_watch_app.py").read_text()
    endpoint = source.split('def watch_lab_evaluate(', 1)[1].split(
        '@app.get("/api/watch-locations/search")', 1
    )[0]
    assert '@app.post("/api/watch-lab/evaluate")' in source
    assert "gis_active_spatial_watch_matches(e.alert_id,e.supplied_geom)" in endpoint
    assert "a.category AS alert_category" in endpoint
    assert "a.tags AS alert_tags" in endpoint
    assert "e.alert_category AS category" in endpoint
    assert "e.alert_tags AS tags" in endpoint
    assert "stored_alert_geom IS NOT NULL THEN" in endpoint
    assert "WHEN p.supplied_geom IS NOT NULL THEN" in endpoint
    assert "WHEN p.resolver_status='RESOLVED'" in endpoint
    assert 'point_mode not in {"ALERT", "WATCH_CENTER", "CUSTOM"}' in endpoint
    assert "persisted_match" in endpoint
    assert "watch_item_recipients" in endpoint
    for write in ("INSERT INTO", "UPDATE ", "DELETE FROM", "urlopen"):
        assert write not in endpoint
    assert "urllib_request" not in endpoint


def test_watch_lab_ui_states_safety_and_loads_shared_matcher():
    template = (DASHBOARD_ROOT / "templates/watchlist.html").read_text()
    assert "Watch Lab · Why did this match?" in template
    assert "Run Read-Only Test" in template
    assert "never creates an alert, Match, delivery, or Notification" in template
    assert 'value="WATCH_CENTER"' in template
    assert 'value="CUSTOM"' in template
    assert '<script src="/static/watch_matcher.js?v=2"></script>' in template
    assert "window.CmosWatchMatcher.evaluateWatch" in template
    assert "Copy Diagnostic Receipt" in template


def test_tracked_workflows_embed_the_one_shared_evaluator():
    matcher = (DASHBOARD_ROOT / "static/watch_matcher.js").read_text().rstrip()
    adapter = _repository_file("deploy/n8n/watch_matcher_adapter.js").read_text().lstrip()
    expected = matcher + "\n\n" + adapter
    codes = []
    for path in (
        "workflows/core/CORE_Watchlist_Matcher_v1.json",
        "workflows/live/CORE_Watchlist_Matcher_live.json",
    ):
        code = _workflow_node(path, "Match + Resolve Recipients")["parameters"]["jsCode"]
        assert code == expected
        codes.append(code)
    assert codes[0] == codes[1]
    assert "CmosWatchMatcher.evaluateWatch(alert, row)" in adapter
    assert "function evaluateWatch" not in adapter


def test_shared_evaluator_enforces_spatial_only_and_separates_routing():
    if not shutil.which("node"):
        raise SkipTest("Node is not installed in this focused test environment")
    matcher = DASHBOARD_ROOT / "static/watch_matcher.js"
    script = r"""
const matcher = require(process.argv[1]);
const alert = {
  alert_id: 'ORU:eb5707b3', source: 'ORU', category: 'UTILITY', subtype: 'OUTAGE',
  status: 'ACTIVE', title: 'Valley Hospital service alert', message: 'Utility event',
  municipality: 'RIDGEWOOD', priority: 5, tags: []
};
const recipient = {subscriber_id: 'OPS', ntfy_topic: 'ops'};
const spatial = {
  active: true, watch_id: 'W_VALLEY', watch_type: 'ADDRESS', display_name: 'Valley Hospital',
  search_term: 'Valley Hospital', aliases: [], match_mode: 'CONTAINS', min_priority: 1,
  source_filter: [], alert_category_filter: [], nearby_enabled: true,
  spatial_match_type: 'PROXIMITY', spatial_match_reason: 'inside saved radius',
  recipients: [recipient]
};
const inside = matcher.evaluateWatch(alert, spatial, {now: '2026-09-21T12:00:00Z'});
if (!inside.matched || !inside.notification_ready) throw new Error('inside spatial Watch did not match');

const outside = matcher.evaluateWatch(alert, {...spatial, spatial_match_type: null, spatial_match_reason: null});
if (outside.matched) throw new Error('spatial-only Watch fell through to its display-name text');

const wrongSource = matcher.evaluateWatch(alert, {...spatial, source_filter: ['PSEG']});
if (wrongSource.matched || wrongSource.gates.find(g => g.key === 'source').status !== 'FAIL') {
  throw new Error('exact source failure was not explained');
}
const wrongCategory = matcher.evaluateWatch(alert, {...spatial, alert_category_filter: ['Valley']});
if (wrongCategory.matched || wrongCategory.gates.find(g => g.key === 'category').status !== 'FAIL') {
  throw new Error('exact category failure was not explained');
}
const unrouted = matcher.evaluateWatch(alert, {...spatial, recipients: []});
if (!unrouted.matched || unrouted.notification_ready) {
  throw new Error('match eligibility was not separated from recipient routing');
}
const both = matcher.evaluateWatch(alert, {
  ...spatial, watch_type: 'LOCATION_TOPIC', search_term: 'UTILITY EVENT', match_mode: 'CONTAINS'
});
if (!both.matched || both.match_type !== 'LOCATION_TOPIC') throw new Error('Location plus topic did not require both');
const missingLocation = matcher.evaluateWatch(alert, {...spatial, watch_type: 'LOCATION_TOPIC', spatial_match_type: null});
if (missingLocation.matched) throw new Error('Location plus topic matched outside its Location');
console.log('WATCH_MATCHER_CONTRACT=PASS');
"""
    completed = subprocess.run(
        ["node", "-e", script, str(matcher)],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "WATCH_MATCHER_CONTRACT=PASS" in completed.stdout


def test_workflow_adapter_rebuilds_search_text_from_standard_alert_fields():
    if not shutil.which("node"):
        raise SkipTest("Node is not installed in this focused test environment")
    workflow = _repository_file("workflows/live/CORE_Watchlist_Matcher_live.json")
    script = r"""
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const workflow = Array.isArray(payload) ? payload[0] : payload;
const code = workflow.nodes.find(node => node.name === 'Match + Resolve Recipients').parameters.jsCode;
const run = new Function('$', '$input', code);
const alert = {alert_id:'CONTRACT',priority:4,source:'SYSTEM_TEST',category:'TEST',municipality:'WEEHAWKEN',title:'PARK AVENUE ROAD CLOSURE',message:''};
const recipient = {subscriber_id:'OPS',ntfy_topic:'ops'};
const row = {watch_id:'CONTRACT',watch_type:'CORRIDOR',search_term:'PARK AVENUE',aliases:[],match_mode:'CONTAINS',match_field:'search_text',min_priority:1,source_filter:[],alert_category_filter:[],spatial_match_type:'PROXIMITY',spatial_match_reason:'inside',recipients:[recipient,recipient]};
const evaluate = (caseAlert, caseRow) => run(() => ({first: () => ({json:caseAlert})}), {all: () => [{json:caseRow}]})[0].json;
const matched = evaluate(alert, row);
if (matched.match_count !== 1 || matched.recipient_count !== 1) throw new Error(JSON.stringify(matched));
const wrongTopic = evaluate({...alert,title:'PARK AVENUE WATER MAIN'}, {...row,watch_type:'LOCATION_TOPIC',search_term:'ROAD CLOSURE'});
if (wrongTopic.match_count !== 0) throw new Error(JSON.stringify(wrongTopic));
console.log('WATCH_ADAPTER_CONTRACT=PASS');
"""
    completed = subprocess.run(
        ["node", "-e", script, str(workflow)],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "WATCH_ADAPTER_CONTRACT=PASS" in completed.stdout


def test_sync_script_has_no_second_matcher_implementation():
    sync = _repository_file("deploy/n8n/sync_watch_matcher.py").read_text()
    assert 'MATCHER_SOURCE = ROOT / "dashboard/static/watch_matcher.js"' in sync
    assert 'ADAPTER_SOURCE = ROOT / "deploy/n8n/watch_matcher_adapter.js"' in sync
    assert "function evaluateWatch" not in sync
