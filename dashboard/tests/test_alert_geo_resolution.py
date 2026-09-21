from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geo_resolver
from geo_resolver import (
    _address_variants,
    _cache_key,
    _local_municipality_hint,
    _municipality_hint,
    _municipality_hints,
    _resolve_address,
    _resolve_intersection,
    _resolve_street,
    _save_cache,
    _save_entity_resolution,
    _street_variants,
    audit_alerts,
    LocationCandidate,
    RESOLVER_VERSION,
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


class _AddressCursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params):
        assert query.count("%s") == len(params)
        self.calls.append((query, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _AddressConnection:
    def __init__(self, rows):
        self.cursor_value = _AddressCursor(rows)

    def cursor(self):
        return self.cursor_value


class _AuditConnection(_AddressConnection):
    def __init__(self, rows):
        super().__init__(rows)
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


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


def test_bnn_free_text_locality_is_used_when_city_field_is_missing():
    assert _municipality_hint(
        {"source": "BNN", "message": "Fire at Park Ave & 49th St, Weehawken NJ"}
    ) == "Weehawken"


def test_bnn_location_formats_extract_address_intersection_and_locality():
    cases = {
        "North Bergen, 7611 Broadway": ("NORTH BERGEN", "7611 BROADWAY", "address"),
        "4100 Park Ave Weehawken": ("WEEHAWKEN", "4100 PARK AVE", "address"),
        "Working Fire at Park Ave / 49th St, Weehawken": (
            "WEEHAWKEN", "PARK AVE & 49TH ST", "intersection"
        ),
    }
    for location, (municipality, candidate_text, candidate_kind) in cases.items():
        payload = {"source": "BNN", "location": {"address": location}, "message": location}
        assert municipality in _municipality_hints(payload)
        candidates = extract_location_candidates(payload)
        assert any(
            item.normalized == candidate_text and item.kind == candidate_kind
            for item in candidates
        )

    assert "BOULEVARD EAST" in _street_variants(
        "400 block Boulevard East", "Weehawken"
    )
    assert "ROUTE 3" in _street_variants("Route 3 EB", "Secaucus")
    route_intersection = extract_location_candidates(
        {"source": "BNN", "location": {"address": "Route 3 X Paterson Plank Rd, Secaucus"}}
    )
    assert any(item.kind == "intersection" for item in route_intersection)


def test_bnn_locality_guesses_are_validated_against_existing_local_gis():
    connection = _AddressConnection([("North Bergen",)])
    result = _local_municipality_hint(
        connection,
        {"source": "BNN", "location": {"address": "7611 Broadway, North Bergen"}},
    )

    assert result == "North Bergen"
    query, params = connection.cursor_value.calls[0]
    assert "FROM unnest(%s::text[]) WITH ORDINALITY" in query
    assert "FROM gis_addresses" in query
    assert "FROM gis_parcels" in query
    assert params == (["NORTH BERGEN"],)


def test_failed_address_uses_closest_real_ng911_address_before_city():
    connection = _AddressConnection(
        [("4098 Park Avenue", "Weehawken", "07086", "parcel", -74.025, 40.775, 14, 4098, 2)]
    )
    candidate = LocationCandidate("4100 Park Ave", "4100 PARK AVE", "address", "location.address", 85)

    result = _resolve_street(connection, candidate, "Weehawken")

    assert result["match_type"] == "LOCAL_NEAREST_ADDRESS"
    assert result["spatial_precision"] == "APPROXIMATE_ADDRESS_POINT"
    assert result["label"] == "4098 Park Avenue"
    assert result["requested_address_number"] == 4100
    assert result["matched_address_number"] == 4098
    assert result["address_number_delta"] == 2
    assert result["confidence"] < geo_resolver.MIN_PRECISE_CONFIDENCE
    query, params = connection.cursor_value.calls[0]
    assert "abs(p.address_number-i.requested_number)" in query
    assert "p.geom <-> c.geom" in query
    assert "lower(a.post_comm)=lower(%s)" in query
    assert params[0] == 4100
    assert params[1] == "Weehawken"
    assert "PARK AVE" in params[2]


def test_intersection_fallback_uses_closest_local_address_pair():
    connection = _AddressConnection(
        [
            (
                "4000 Park Avenue", "10 49th Street", "Weehawken", "07086", "parcel",
                -74.023, 40.774, 120.0, 12, 8,
            )
        ]
    )
    candidate = LocationCandidate(
        "Park Ave & 49th St, Weehawken NJ",
        "PARK AVE & 49TH ST WEEHAWKEN NJ",
        "intersection",
        "location.address",
        80,
    )

    result = _resolve_intersection(connection, candidate, "Weehawken")

    assert result["match_type"] == "LOCAL_NEAREST_INTERSECTION_ADDRESS"
    assert result["spatial_precision"] == "APPROXIMATE_INTERSECTION"
    assert result["label"] == "4000 Park Avenue"
    assert result["cross_street_nearest_address"] == "10 49th Street"
    assert result["longitude"] == -74.023
    assert result["confidence"] == 0.70
    assert result["distance_feet"] == 120.0
    assert len(connection.cursor_value.calls) == 1


def test_resolution_prefers_intersection_before_street_and_city(monkeypatch):
    events = []

    class Connection:
        def commit(self):
            pass

    monkeypatch.setattr(geo_resolver, "_dataset_versions", lambda _conn: {})
    monkeypatch.setattr(geo_resolver, "_save_cache", lambda *_args: None)
    monkeypatch.setattr(
        geo_resolver,
        "_resolve_intersection",
        lambda _conn, _candidate, _municipality: events.append("intersection") or {
            "status": "RESOLVED",
            "match_type": "LOCAL_NEAREST_INTERSECTION_ADDRESS",
            "confidence": 0.70,
            "label": "Near Park Ave & 49th St, Weehawken",
            "municipality": "Weehawken",
            "longitude": -74.023,
            "latitude": 40.774,
            "spatial_precision": "APPROXIMATE_INTERSECTION",
        },
    )
    monkeypatch.setattr(
        geo_resolver,
        "_resolve_street",
        lambda *_args: events.append("street") or None,
    )
    monkeypatch.setattr(
        geo_resolver,
        "_resolve_place",
        lambda *_args: events.append("city") or None,
    )

    result = geo_resolver.resolve_payload(
        Connection(),
        {
            "source": "BNN",
            "municipality": "Weehawken",
            "location": {"address": "Park Ave & 49th St"},
        },
        use_cache=False,
    )

    assert result["match_type"] == "LOCAL_NEAREST_INTERSECTION_ADDRESS"
    assert events == ["intersection"]


def test_resolution_prefers_nearest_address_before_city(monkeypatch):
    events = []

    class Connection:
        def commit(self):
            pass

    monkeypatch.setattr(geo_resolver, "_dataset_versions", lambda _conn: {})
    monkeypatch.setattr(geo_resolver, "_save_cache", lambda *_args: None)
    monkeypatch.setattr(
        geo_resolver,
        "_resolve_address",
        lambda *_args: events.append("exact") or None,
    )
    monkeypatch.setattr(
        geo_resolver,
        "_resolve_street",
        lambda *_args: events.append("nearest") or {
            "status": "RESOLVED",
            "match_type": "LOCAL_NEAREST_ADDRESS",
            "confidence": 0.72,
            "label": "4098 Park Avenue",
            "municipality": "Weehawken",
            "longitude": -74.025,
            "latitude": 40.775,
            "spatial_precision": "APPROXIMATE_ADDRESS_POINT",
        },
    )
    monkeypatch.setattr(
        geo_resolver,
        "_resolve_place",
        lambda *_args: events.append("city") or None,
    )

    result = geo_resolver.resolve_payload(
        Connection(),
        {
            "source": "BNN",
            "municipality": "Weehawken",
            "location": {"address": "4100 Park Ave"},
        },
        use_cache=False,
    )

    assert result["label"] == "4098 Park Avenue"
    assert events == ["exact", "nearest"]


def test_read_only_bnn_audit_rechecks_every_existing_alert_without_persisting(monkeypatch):
    rows = [
        {
            "alert_id": "BNN:1",
            "source": "BNN",
            "title": "Incident one",
            "message": "4100 Park Ave, Weehawken NJ",
            "location": {"address": "4100 Park Ave"},
            "metadata": {},
            "raw_payload": {},
            "total_available": 2,
        },
        {
            "alert_id": "BNN:2",
            "source": "BNN",
            "title": "Incident two",
            "message": "Park Ave and 49th St, Weehawken NJ",
            "location": {"label": "Park Ave and 49th St"},
            "metadata": {},
            "raw_payload": {},
            "total_available": 2,
        },
    ]
    connection = _AuditConnection(rows)
    calls = []

    def fake_resolve(_conn, payload, **options):
        calls.append((payload, options))
        return {
            "status": "RESOLVED",
            "match_type": "LOCAL_NEAREST_ADDRESS",
            "spatial_precision": "APPROXIMATE_ADDRESS_POINT",
            "label": payload["location"].get("address") or payload["location"].get("label"),
            "municipality": "Weehawken",
            "confidence": 0.72,
            "longitude": -74.02,
            "latitude": 40.77,
        }

    monkeypatch.setattr(geo_resolver, "resolve_payload", fake_resolve)

    summary = audit_alerts(connection, sources=["BNN"])

    assert summary["mode"] == "READ_ONLY"
    assert summary["selected"] == summary["total_available"] == 2
    assert summary["complete"] is True
    assert summary["error_count"] == 0
    assert summary["match_type_counts"] == {"LOCAL_NEAREST_ADDRESS": 2}
    assert summary["mapped_count"] == 2
    assert summary["unresolved_with_candidates"] == 0
    assert summary["no_location_evidence"] == 0
    assert all(options == {"use_cache": False, "persist": False} for _, options in calls)
    assert connection.commits == 0
    query, params = connection.cursor_value.calls[0]
    assert "a.source=ANY(%s::text[])" in query
    assert "a.received_at >=" not in query
    assert "LIMIT" not in query
    assert params == (["BNN"],)


def test_non_persistent_resolution_skips_cache_entity_and_commit(monkeypatch):
    class Connection:
        commits = 0

        def commit(self):
            self.commits += 1

    connection = Connection()
    monkeypatch.setattr(geo_resolver, "_dataset_versions", lambda _conn: {})
    monkeypatch.setattr(geo_resolver, "_cached_result", lambda *_args: (_ for _ in ()).throw(AssertionError("cache read")))
    monkeypatch.setattr(geo_resolver, "_save_cache", lambda *_args: (_ for _ in ()).throw(AssertionError("cache write")))
    monkeypatch.setattr(geo_resolver, "_save_entity_resolution", lambda *_args: (_ for _ in ()).throw(AssertionError("entity write")))

    result = geo_resolver.resolve_payload(
        connection,
        {"source": "BNN", "location": {"latitude": 40.77, "longitude": -74.02}},
        entity_type="ALERT",
        entity_id="alert-id",
        persist=False,
    )

    assert result["match_type"] == "SUPPLIED_COORDINATES"
    assert connection.commits == 0


def test_audit_cli_is_read_only_and_defaults_to_all_bnn_history():
    source = Path(__file__).resolve().parents[1].joinpath("geo_resolver.py").read_text()
    assert 'parser.add_argument("mode", choices=("audit", "backfill"))' in source
    assert 'cur.execute("SET TRANSACTION READ ONLY")' in source
    assert 'sources=args.sources or ["BNN"]' in source
    assert "limit=args.limit," in source
    assert "since_days=args.since_days," in source


def test_address_variants_and_municipality_fallback_use_one_ordered_query():
    connection = _AddressConnection(
        [(1, "4100 Park Avenue", "Weehawken", "07086", "parcel", -74.02, 40.77, "A", True)]
    )
    candidate = LocationCandidate("4100 Park Ave", "4100 PARK AVE", "address", "location.address", 85)

    result = _resolve_address(connection, candidate, "Weehawken")

    assert len(connection.cursor_value.calls) == 1
    query, params = connection.cursor_value.calls[0]
    assert query.count("WITH ORDINALITY") == 2
    assert "ORDER BY variant_order,scope_order" in query
    assert params == (_address_variants(candidate.text), ["Weehawken", ""])
    assert result["label"] == "4100 Park Avenue"
    assert result["confidence"] == 0.98


def test_worker_and_resolver_publish_runtime_measurements():
    root = Path(__file__).resolve().parents[1]
    resolver = root.joinpath("geo_resolver.py").read_text()
    worker = root.joinpath("integration_worker.py").read_text()
    assert 'summary["duration_ms"]' in resolver
    assert "cycle_started = time.perf_counter()" in worker
    assert "engine cycle complete duration_ms=" in worker


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
    assert "a.geom IS NULL AND coalesce(r.resolver_version,0) < %s" in source
    assert RESOLVER_VERSION == 5


def test_bnn_recovery_release_requires_target_mapping_and_real_coverage_gain():
    release = Path(__file__).resolve().parents[2].joinpath(
        "deploy/releases/bnn-map-recovery.sh"
    ).read_text()

    assert "BNN:d1468ab2" in release
    assert 'cur.execute("SET TRANSACTION READ ONLY")' in release
    assert "use_cache=False,persist=False" in release
    assert 'result.get("status")!="RESOLVED"' in release
    assert "backfill --limit 10000 --since-days 3650 --source BNN" in release
    assert "--force" not in release
    assert 'after["target_mapped"] is True' in release
    assert 'int(after["mapped"])>int(before["mapped"])' in release
    assert "full_e2e=NOT_RUN notifications=NONE" in release


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


def test_map_status_counts_only_geometry_the_map_can_really_display():
    source = Path(__file__).resolve().parents[1].joinpath("map_app.py").read_text()
    status_query = source.split("def map_gis_status():", 1)[1].split(
        '@app.post("/map/resolve")', 1
    )[0]

    assert "FROM alert_geo_coverage" not in status_query
    assert "r.status='RESOLVED' AND r.geom IS NOT NULL" in status_query
    assert "CASE WHEN r.status='RESOLVED' THEN r.geom END" in status_query
    assert "r.status='AMBIGUOUS'" in status_query
