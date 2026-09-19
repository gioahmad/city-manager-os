import json
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = DASHBOARD_ROOT.parent


def test_global_search_is_bounded_grouped_and_uses_existing_records():
    source = (DASHBOARD_ROOT / "operations_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/search.html").read_text()
    nav = (DASHBOARD_ROOT / "templates/nav.html").read_text()

    assert '@app.get("/search", response_class=HTMLResponse)' in source
    assert "q = q.strip()[:160]" in source
    assert "LIMIT 140" in source
    assert "SET LOCAL statement_timeout = '12s'" in source
    assert "with db_conn() as conn" in source
    assert "lower(p.prop_loc)>=lower(%s)" in source
    assert "p.pams_pin>=%s AND p.pams_pin<%s" in source
    assert "lower(p.mun_name)=lower(%s)" in source
    assert "coalesce(p.pclblock,'')=%s" not in source
    for table in (
        "alerts",
        "issues",
        "watch_items",
        "deliveries",
        "operational_events",
        "event_intelligence",
        "transit_observations",
        "transit_assets",
        "gis_addresses",
        "gis_parcels",
        "spatial_reference_entities",
        "map_features",
        "integrations",
        "source_health",
    ):
        assert table in source
    assert "Search Everything" in template
    assert "does not create another database" in template
    assert 'href="/search"' in nav
    assert "CREATE TABLE" not in source


def test_release_acceptance_uses_gets_without_racing_live_feed_counts():
    runner = (REPOSITORY_ROOT / "deploy/releases/system-search-ntfy-clarity.sh").read_text()
    assert "method='GET'" in runner
    assert "'acceptance_requests':'GET-only'" in runner
    assert "'write_requests_sent':0" in runner
    assert "before=counts()" not in runner
    assert "after=counts()" not in runner
    assert "read-only acceptance changed stored records" not in runner


def test_search_template_compiles_and_has_friendly_failure_state():
    environment = Environment(loader=FileSystemLoader(DASHBOARD_ROOT / "templates"))
    environment.get_template("search.html")
    source = (DASHBOARD_ROOT / "operations_app.py").read_text()
    assert "Search is temporarily unavailable. Please try again." in source
    assert "Enter at least two characters to search." in source


def test_map_startup_avoids_full_extent_and_eager_flood_load():
    source = (DASHBOARD_ROOT / "map_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/map.html").read_text()
    assert "ST_EstimatedExtent('public','gis_parcels','geom')" in source
    assert '"key": "flood"' in source
    flood_line = next(line for line in source.splitlines() if '"key": "flood"' in line)
    assert '"default_visible": False' in flood_line
    assert "SELECT DISTINCT source FROM alerts" in source
    assert "SELECT DISTINCT category FROM alerts" in source
    assert "lower(fulladdr)>=lower(%s) AND lower(fulladdr)<lower(%s)" in source
    assert "parcel_identifier_sql" in source
    assert "if block_lot:" in source
    assert "elif simple_parcel_number:" in source
    assert 'new URLSearchParams(window.location.search).get(\'q\')' in template


def test_notification_history_explains_the_watch_and_reason_without_jargon():
    source = (DASHBOARD_ROOT / "operations_app.py").read_text()
    template = (DASHBOARD_ROOT / "templates/deliveries.html").read_text()
    assert "d.match_reasons" in source
    assert "matched_watches" in source
    assert "_humanize_match_reason" in source
    assert "Keyword" in source
    assert "Watch center" in source
    assert "Why you received this" in template
    assert "item.watch_name" in template
    assert "item.reason" in template


def test_ntfy_sender_appends_plain_language_reason_and_preserves_unmatched_message(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node is required for the workflow contract")
    workflow_path = REPOSITORY_ROOT / "workflows/core/CORE_ntfy_Sender_v1.json"
    if not workflow_path.is_file():
        pytest.skip("workflow source is outside the dashboard-only test mount")
    workflow = json.loads(workflow_path.read_text())
    nodes = {node["name"]: node for node in workflow["nodes"]}
    code = nodes["Prepare ntfy Requests"]["parameters"]["jsCode"]
    script = f"""
const run = new Function('$input', {json.dumps(code)});
function prepare(payload) {{
  return run({{first:()=>({{json:{{delivery_payloads:[payload]}}}})}})[0].json.ntfy_body.message;
}}
const base={{ntfy_topic:'contract',title:'Contract',message:'Original alert',priority:3,tags:[]}};
const keyword=prepare({{...base,match_reasons:['CONTAINS search_text matched search_term "PSEG"']}});
if(!keyword.includes('Why you received this:')||!keyword.includes('Keyword “PSEG” matched this alert')) throw new Error('keyword reason missing');
if(keyword.includes('search_text')||keyword.includes('search_term')) throw new Error('technical terms leaked');
const location=prepare({{...base,match_reasons:['PROXIMITY alert geometry is 125.0 ft from target, inside 5280.0 ft buffer']}});
if(!location.includes('Watch center')||!location.includes('5,280-foot Distance')) throw new Error('location reason missing');
if(location.includes('geometry')||location.includes('buffer')) throw new Error('spatial jargon leaked');
const unchanged=prepare(base);
if(unchanged!=='Original alert') throw new Error('message without match reasons changed');
console.log('NTFY_REASON_CONTRACT=PASS');
"""
    path = tmp_path / "contract.js"
    path.write_text(script)
    result = subprocess.run(["node", str(path)], check=True, capture_output=True, text=True)
    assert "NTFY_REASON_CONTRACT=PASS" in result.stdout
