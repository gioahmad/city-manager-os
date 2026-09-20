import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / "workflows/core/CORE_Resolved_Spatial_Rematch_v1.json"
SYNC_PATH = ROOT / "deploy/gis/sync_nj_watch_layers.py"
RELEASE_PATH = ROOT / "deploy/releases/spatial-rematch-nj-watch-layers.sh"


def _sync_module():
    spec = importlib.util.spec_from_file_location("sync_nj_watch_layers", SYNC_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_resolved_alerts_reenter_existing_matcher_once_with_visible_location():
    workflow = json.loads(WORKFLOW_PATH.read_text())
    release = RELEASE_PATH.read_text()
    nodes = {node["name"]: node for node in workflow["nodes"]}
    load = nodes["Load Newly Resolved Alerts"]["parameters"]["query"]
    mark = nodes["Mark Resolved Alert Rematched"]
    send = nodes["Send Resolved Alert to Central Watchlist Matcher"]

    assert workflow["name"] == "CORE - Resolved Alert Spatial Rematch v1"
    assert workflow["id"] == "CmosGeoRematch01"
    assert "workflow.pop('id',None)" not in release
    assert "elif not workflow.get('id')" in release
    assert set(nodes) == {
        "Resolved Alert Schedule",
        "Load Newly Resolved Alerts",
        "Restore Resolved Standard Alert",
        "Send Resolved Alert to Central Watchlist Matcher",
        "Mark Resolved Alert Rematched",
    }
    assert "geo_entity_resolutions" in load
    assert "coalesce(a.geom,r.geom) IS NOT NULL" in load
    assert "r.confidence>=0.85" not in load
    assert "r.spatial_precision='ADDRESS_POINT'" not in load
    assert "SUPPLIED_COORDINATE" not in load
    assert "TIMESTAMPTZ '__CMOS_ACTIVATED_AT__'" in load
    assert "interval '6 hours'" in load
    assert "spatial_rematch_version" in load
    assert "ST_X(coalesce(a.geom,r.geom))" in load
    assert "ST_Y(coalesce(a.geom,r.geom))" in load
    assert "nullif(r.municipality,'')" in load
    assert send["alwaysOutputData"] is True
    assert send["parameters"]["mode"] == "each"
    assert "spatial_rematch_version','geo-v2'" in mark["parameters"]["query"]
    assert "updated_at=now()" not in mark["parameters"]["query"]
    assert "Restore Resolved Standard Alert" in mark["parameters"]["options"]["queryReplacement"]
    assert "ntfy" not in json.dumps(workflow).casefold()
    next_node = workflow["connections"]["Send Resolved Alert to Central Watchlist Matcher"]
    assert next_node["main"][0][0]["node"] == "Mark Resolved Alert Rematched"


def test_official_sources_become_natural_watch_locations():
    sync = _sync_module()
    sources = {source.kind: source for source in sync.SOURCES}
    assert set(sources) == {"municipality", "county", "major_highway"}
    assert all("arcgis.com" in source.url for source in sources.values())
    assert sources["municipality"].owner == "New Jersey Office of GIS"
    assert sources["county"].minimum == sources["county"].maximum == 21
    assert sources["major_highway"].owner == "New Jersey Department of Transportation"

    municipality = sync.canonical_feature(
        sources["municipality"],
        {
            "properties": {
                "NAME": "Weehawken Township",
                "COUNTY": "HUDSON",
                "MUN_CODE": "0909",
                "OBJECTID": 1,
                "POP2020": 17000,
            },
            "geometry": {"type": "Polygon", "coordinates": []},
        },
    )
    highway = sync.canonical_feature(
        sources["major_highway"],
        {
            "properties": {
                "SLD_NAME": "I-495",
                "ROAD_NUM": "495",
                "SRI": "00000495__",
                "OBJECTID": 2,
            },
            "geometry": {"type": "LineString", "coordinates": []},
        },
    )
    assert municipality["properties"]["county"] == "HUDSON"
    assert municipality["feature_key"] == "0909"
    assert highway["properties"]["route_name"] == "I-495"
    assert highway["feature_key"] == "I-495"


def test_change_plan_stays_focused_without_build_or_full_e2e():
    result = subprocess.run(
        [
            str(ROOT / "deploy/cmos-deploy"),
            "plan",
            "--files",
            "deploy/gis/sync_nj_watch_layers.py",
            "workflows/core/CORE_Resolved_Spatial_Rematch_v1.json",
            "dashboard/tests/test_spatial_rematch_layers.py",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    assert "build=no" in result.stdout
    assert "services=none" in result.stdout
    assert "backup_required=yes" in result.stdout
    assert "external=n8n-workflow-publish,postgis-reference-layer-sync" in result.stdout
    assert "full_e2e=no" in result.stdout
    assert "unknown=none" in result.stdout


if __name__ == "__main__":
    tests = sorted(name for name in globals() if name.startswith("test_"))
    for name in tests:
        globals()[name]()
        print(f"PASS {name}")
