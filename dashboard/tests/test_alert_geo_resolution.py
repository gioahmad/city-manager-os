from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geo_resolver import (
    _address_variants,
    _cache_key,
    _save_cache,
    _save_entity_resolution,
    LocationCandidate,
    extract_location_candidates,
    normalize_text,
)


class _Cursor:
    queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params):
        assert query.count("%s") == len(params)
        self.queries.append(query)


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
    assert "a.geom IS NULL" in source
    assert "r.spatial_precision IN ('ADDRESS_POINT','SUPPLIED_COORDINATE')" in source


def test_entity_resolution_sql_parameter_contract():
    _Cursor.queries.clear()
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
    query = _Cursor.queries[-1]
    assert query.count("%s::double precision") == 4
    assert "%s::text='RESOLVED'" in query


def test_cache_geometry_parameters_are_typed_for_postgres_nulls():
    _Cursor.queries.clear()
    _save_cache(
        _Connection(),
        "cache-key",
        {
            "status": "UNRESOLVED",
            "match_type": None,
            "confidence": 0,
            "longitude": None,
            "latitude": None,
            "provenance": {"runtime_source": "LOCAL_POSTGIS"},
        },
        [LocationCandidate("address", "Unknown", "UNKNOWN", "message", 0.5)],
        {"municipality": "", "county": "", "state": "NJ", "source": "BNN"},
        {"NJOGIS_NJ_STATEWIDE_ADDRESSES": {"row_count": 3755307}},
    )
    assert _Cursor.queries[-1].count("%s::double precision") == 4


def test_alert_metadata_parameters_have_explicit_sql_types():
    source = Path(__file__).resolve().parents[1].joinpath("geo_resolver.py").read_text()
    assert "'label',%s::text" in source
    assert "'longitude',%s::double precision" in source


def test_map_alert_geojson_serializes_numeric_confidence_and_escapes_like():
    source = Path(__file__).resolve().parents[1].joinpath("map_app.py").read_text()
    assert "elif isinstance(value, Decimal):" in source
    assert "props[key] = float(value)" in source
    assert "LIKE 'NJOGIS_%%'" in source
