from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geo_resolver import (
    _address_variants,
    _cache_key,
    _save_entity_resolution,
    extract_location_candidates,
    normalize_text,
)


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params):
        assert query.count("%s") == len(params)


class _Connection:
    def cursor(self):
        return _Cursor()


def test_embedded_bnn_address_is_extracted():
    candidates = extract_location_candidates(
        {
            "source": "BNN",
            "address": "Working Fire",
            "message": "Second alarm at 4100 Park Ave, Weehawken NJ",
        }
    )
    assert any(item.normalized == "4100 PARK AVE" for item in candidates)
    assert all(item.normalized != "WORKING FIRE" for item in candidates)


def test_common_street_suffixes_expand_without_remote_lookup():
    variants = {normalize_text(item) for item in _address_variants("4100 Park Ave")}
    assert "4100 PARK AVENUE" in variants


def test_coordinate_is_part_of_cache_identity():
    versions = {"NJOGIS_NJ_STATEWIDE_ADDRESSES": {"row_count": 3755307}}
    one = _cache_key([], {}, versions, (-74.02, 40.77, "raw.location"))
    two = _cache_key([], {}, versions, (-74.03, 40.78, "raw.location"))
    assert one != two


def test_worker_contract_stays_inside_existing_alerts_and_resolver():
    source = Path(__file__).resolve().parents[1].joinpath("geo_resolver.py").read_text()
    worker = Path(__file__).resolve().parents[1].joinpath("integration_worker.py").read_text()
    assert "FROM alerts a" in source
    assert "geo_entity_resolutions" in source
    assert "CMOS_ALERT_GEO_RESOLVER" in source
    assert "process_pending_alerts" in worker


def test_entity_resolution_sql_parameter_contract():
    _save_entity_resolution(
        _Connection(),
        "ALERT",
        "00000000-0000-0000-0000-000000000001",
        "cache-key",
        {
            "status": "RESOLVED",
            "match_type": "LOCAL_EXACT_ADDRESS",
            "confidence": 0.98,
            "label": "4100 Park Avenue",
            "municipality": "Weehawken",
            "county": "Hudson",
            "state": "NJ",
            "postal_code": "07086",
            "parcel_id": "parcel",
            "longitude": -74.02,
            "latitude": 40.77,
            "spatial_precision": "ADDRESS_POINT",
            "provenance": {"runtime_source": "LOCAL_POSTGIS"},
        },
    )
