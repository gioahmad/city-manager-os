import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from geo_resolver import resolve_payload
from pseg_engine import (
    HUDSON_TOWNS,
    _record_health,
    _save_state,
    _upsert_alert,
    build_hudson_alert,
    build_statewide_alert,
    decode_polyline,
    hudson_quadkeys,
    parse_summary,
    statewide_eligible,
    statewide_transition,
)


ROOT = Path(__file__).resolve().parents[2]


def _settings(**overrides):
    value = {
        "statewide_enabled": True,
        "minimum_customers": 500,
        "minimum_percent": None,
        "counties": [],
        "material_increase_customers": 250,
        "restoration_notifications": True,
    }
    value.update(overrides)
    return value


def test_provider_polyline_and_hudson_tile_contract():
    assert decode_polyline("kcuuFz`jfM") == [(40.41798, -74.60382)]
    keys = hudson_quadkeys()
    assert keys and len(keys) <= 12
    assert all(len(value) == 10 and set(value) <= set("0123") for value in keys)


def test_summary_requires_complete_feed_and_all_hudson_towns():
    rows = []
    for town in HUDSON_TOWNS:
        rows.append(
            {
                "displayTown": town.title(),
                "displayCounty": "Hudson",
                "customersOut": 0,
                "customersServed": 1000,
                "updatedAt": "2026-09-14T12:00:00Z",
            }
        )
    counties = ["Bergen", "Essex", "Mercer", "Middlesex", "Monmouth", "Morris", "Ocean", "Passaic", "Somerset", "Union"]
    for index in range(200 - len(rows)):
        rows.append(
            {
                "displayTown": f"Municipality {index}",
                "displayCounty": counties[index % len(counties)],
                "customersOut": index,
                "customersServed": 10000,
                "updatedAt": "2026-09-14T12:00:00Z",
            }
        )
    generation, parsed = parse_summary({"content": {"generatedAt": "generation", "towns": rows}})
    assert generation == "generation"
    assert len(parsed) == 200
    assert {row["municipality"] for row in parsed if row["county"] == "HUDSON"} == set(HUDSON_TOWNS)


def test_statewide_policy_always_excludes_hudson_and_supports_percent():
    hudson = {"county": "HUDSON", "customers_out": 5000, "percent_out": 50}
    bergen = {"county": "BERGEN", "customers_out": 499, "percent_out": 6}
    assert not statewide_eligible(hudson, _settings())
    assert not statewide_eligible(bergen, _settings())
    assert statewide_eligible(bergen, _settings(minimum_percent=5))
    assert not statewide_eligible(bergen, _settings(counties=["ESSEX"], minimum_percent=5))


def test_statewide_transitions_suppress_unchanged_and_gate_restoration():
    current = {"eligible": True, "customers_out": 800}
    assert statewide_transition(None, current, _settings()) is None
    assert statewide_transition({"eligible": False, "customers_out": 100}, current, _settings()) == "THRESHOLD_CROSSING"
    assert statewide_transition({"eligible": True, "customers_out": 600}, current, _settings()) is None
    assert statewide_transition({"eligible": True, "customers_out": 500}, current, _settings()) == "MATERIAL_INCREASE"
    assert statewide_transition(
        {"eligible": True, "customers_out": 700, "material_baseline_customers_out": 500},
        current,
        _settings(),
    ) == "MATERIAL_INCREASE"
    restored = {"eligible": False, "customers_out": 0}
    assert statewide_transition({"eligible": True, "customers_out": 500}, restored, _settings()) is None
    assert statewide_transition(
        {"eligible": True, "customers_out": 500, "last_alert_created_at": datetime.now(timezone.utc)},
        restored,
        _settings(),
    ) == "RESTORATION"


def test_alert_copy_is_explicitly_approximate_and_not_customer_specific():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    record = {
        "county": "HUDSON",
        "municipality": "WEEHAWKEN",
        "customers_out": 12,
        "customers_served": 7000,
        "percent_out": 0.17,
        "etr": "2026-09-14T15:00:00Z",
        "started_at": datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc),
        "outage_count": 3,
        "jobs_working": 2,
        "circuits": 1,
        "pending_damage": 1,
        "confirmed_roads": 1,
        "updated_at": now,
    }
    context = {
        "label": "Park Avenue corridor",
        "latitude": 40.77,
        "longitude": -74.02,
        "areas": [{"label": "Park Avenue corridor"}],
        "approximate": True,
    }
    with patch.dict("os.environ", {"CMOS_PUBLIC_ORIGIN": "https://private.example"}):
        alert = build_hudson_alert(record, {"customers_out": 0}, context, "CYCLE", now)
    assert alert["metadata"]["location_approximate"] is True
    assert alert["metadata"]["location_not_customer_specific"] is True
    assert "not customer" in alert["message"].lower()
    assert "Started " in alert["message"]
    assert "Jobs 3 | Working 2 | Circuits 1" in alert["message"]
    assert "Damage: Pending 1 | Road 1" in alert["message"]
    assert "Mapping Center: https://private.example/map" in alert["message"]
    assert alert["metadata"]["mapping_center_url"] == "https://private.example/map"
    assert alert["metadata"]["earliest_current_outage"] == record["started_at"]
    assert alert["metadata"]["_cmos"]["route_pending"] is True


def test_statewide_alert_is_compact_grouped_and_hudson_free():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"county": "BERGEN", "municipality": "FORT LEE", "customers_out": 700, "percent_out": 2.1},
        {"county": "ESSEX", "municipality": "NEWARK CITY", "customers_out": 900, "percent_out": 1.2},
    ]
    alert = build_statewide_alert(rows, [(rows[0], "THRESHOLD_CROSSING")], now)
    assert "HUDSON COUNTY EXCLUDED" in alert["message"]
    assert "BERGEN" in alert["message"] and "ESSEX" in alert["message"]
    assert alert["event_action"] == "NEW"
    assert alert["metadata"]["hudson_excluded"] is True
    assert all(row["county"] != "HUDSON" for row in alert["metadata"]["municipalities"])


def test_repository_contract_reuses_existing_alert_route_and_no_direct_ntfy():
    workflow_path = ROOT / "workflows/sources/PSEG_Unified_Spatial_Statewide_v2.json"
    workflow = json.loads(workflow_path.read_text())
    nodes = {node["name"]: node for node in workflow["nodes"]}
    assert "Load Pending PSEG Alerts" in nodes
    assert "Send to Central Watchlist Matcher" in nodes
    assert "Mark PSEG Alert Routed" in nodes
    assert "route_pending" in nodes["Load Pending PSEG Alerts"]["parameters"]["query"]
    assert nodes["Send to Central Watchlist Matcher"]["alwaysOutputData"] is True
    assert nodes["Send to Central Watchlist Matcher"]["parameters"]["workflowId"]["value"] == "ESH9c2pZ8QfkMosO"
    assert "Restore Standard PSEG Alert" in nodes["Mark PSEG Alert Routed"]["parameters"]["options"]["queryReplacement"]
    assert all("ntfy" not in node["type"].lower() for node in workflow["nodes"])
    assert "ntfy" not in workflow_path.read_text().lower()


def test_migration_ui_worker_and_resolver_are_one_coordinated_pack():
    migration = (ROOT / "deploy/postgis/init/033_pseg_spatial_alerts.sql").read_text()
    worker = (ROOT / "dashboard/integration_worker.py").read_text()
    app = (ROOT / "dashboard/integrations_app.py").read_text()
    resolver = (ROOT / "dashboard/geo_resolver.py").read_text()
    assert "pseg_alert_settings" in migration and "pseg_outage_state" in migration
    assert "material_baseline_customers_out" in migration
    assert "DEFAULT 500" in migration and "statewide_enabled boolean NOT NULL DEFAULT false" in migration
    assert "run_due_pseg" in worker
    assert '@app.get("/integrations/pseg"' in app
    assert '@app.post("/integrations/pseg/settings"' in app
    assert "PROVIDER_APPROXIMATE_COORDINATE" in resolver
    assert "APPROXIMATE_PROVIDER_AREA" in resolver


class _CaptureCursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        params = params or ()
        assert sql.count("%s") == len(params)
        self.calls.append((sql, params))


class _CaptureConnection:
    def __init__(self):
        self.cursor_value = _CaptureCursor()

    def cursor(self):
        return self.cursor_value

    def commit(self):
        pass


def test_pseg_sql_parameter_contracts():
    cursor = _CaptureCursor()
    _upsert_alert(cursor, {"alert_id": "PSEG:TEST"})
    _save_state(
        cursor,
        {
            "scope": "HUDSON", "county": "HUDSON", "municipality": "WEEHAWKEN",
            "customers_out": 1, "previous_customers_out": 0, "eligible": True,
            "current_hash": "hash",
        },
    )
    connection = _CaptureConnection()
    _record_health(connection, ok=True, summary={"alerts_created": 1})
    assert len(cursor.calls) == 2
    assert len(connection.cursor_value.calls) == 1


def test_geo_resolver_keeps_provider_area_geometry_approximate():
    connection = _CaptureConnection()
    payload = {
        "source": "PSEG",
        "municipality": "WEEHAWKEN",
        "county": "HUDSON",
        "location": {"label": "Approximate Park Avenue corridor", "latitude": 40.77, "longitude": -74.02},
        "metadata": {"location_approximate": True, "location_not_customer_specific": True},
    }
    with (
        patch("geo_resolver._dataset_versions", return_value={"NJOGIS": {"version": "test"}}),
        patch("geo_resolver._save_cache"),
    ):
        result = resolve_payload(connection, payload, use_cache=False)
    assert result["match_type"] == "PROVIDER_APPROXIMATE_COORDINATE"
    assert result["spatial_precision"] == "APPROXIMATE_PROVIDER_AREA"
    assert result["confidence"] == 0.60
    assert result["provenance"]["not_customer_specific"] is True
    assert result["dataset_versions"] == {"NJOGIS": {"version": "test"}}
