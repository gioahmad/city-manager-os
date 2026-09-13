from pathlib import Path
from unittest import SkipTest


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = DASHBOARD_ROOT.parent


def _deployment_file(relative_path):
    path = REPOSITORY_ROOT / relative_path
    if not path.is_file():
        raise SkipTest("deployment source is outside the dashboard-only test mount")
    return path


def test_catalog_migration_is_additive_and_reuses_watchlist():
    sql = _deployment_file("deploy/postgis/init/031_spatial_reference_catalog.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS spatial_reference_entities" in sql
    assert "ALTER TABLE watch_items" in sql
    assert "spatial_reference_entity_id" in sql
    assert "spatial_geom geometry(Geometry,4326)" in sql
    assert "transit_asset_id uuid REFERENCES transit_assets" in sql
    assert "spatial_reference_refresh_local_sources" in sql
    assert "gis_parcel_for_point" in sql
    assert "gis_addresses_for_parcel" in sql
    assert "gis_adjoining_parcels" in sql
    assert "gis_parcels_within_radius" in sql
    assert "gis_parcel_context" in sql
    assert "gis_spatial_impact_context" in sql
    assert "gis_parcels_within_radius(\n  p_lat double precision" in sql
    assert "SET LOCAL check_function_bodies = off" in sql
    assert "CASE WHEN c.link_method='FUZZY_BOUNDARY'" in sql
    assert "/0.3048" in sql
    assert "*0.3048" in sql
    assert "26400.0))*0.3048)),'[]'::jsonb" in sql
    assert "CREATE TABLE IF NOT EXISTS watch_items" not in sql
    assert "CREATE TABLE IF NOT EXISTS subscribers" not in sql
    assert "CREATE TABLE IF NOT EXISTS deliveries" not in sql


def test_installer_validates_function_sql_before_refreshing_sources():
    installer = _deployment_file("deploy/gis/install_spatial_reference_catalog.sh").read_text()
    validation = "SELECT gis_parcel_context(NULL::integer,500.0) IS NULL;"
    refresh = "SELECT spatial_reference_refresh_local_sources() AS refresh_result;"
    assert validation in installer
    assert refresh in installer
    assert installer.index(validation) < installer.index(refresh)


def test_catalog_routes_and_normal_watch_promotion():
    source = (DASHBOARD_ROOT / "spatial_reference_app.py").read_text()
    assert '@app.get("/spatial-reference"' in source
    assert '@app.get("/api/spatial-reference/release")' in source
    assert '@app.post("/spatial-reference/adopt")' in source
    assert '@app.post("/spatial-reference/refresh")' in source
    assert '@app.post("/spatial-reference/{entity_id}/watch")' in source
    assert '@app.get("/api/spatial-reference/{entity_id}/nearby-history")' in source
    assert '@app.get("/api/spatial-reference/{entity_id}/impact-buffer.geojson")' in source
    assert '@app.get("/api/parcel/for-point")' in source
    assert '@app.get("/api/parcels/within-radius")' in source
    assert '@app.get("/api/parcel/{parcel_objectid}/context")' in source
    assert '@app.get("/map/system/spatial-references.geojson")' in source
    assert "INSERT INTO watch_items" in source
    assert "INSERT INTO watch_item_recipients" in source
    assert "spatial_geom=p.watch_geometry" in source
    assert "SELECT p.geom FROM gis_parcels p WHERE p.objectid=r.parcel_objectid" in source
    assert "source_filter=%s" in source
    assert "alert_category_filter=%s" in source
    assert "INSERT INTO deliveries" not in source
    assert "INSERT INTO alerts" not in source
    assert "requests.post" not in source


def test_composition_and_mapping_layer():
    phase3 = (DASHBOARD_ROOT / "phase3_app.py").read_text()
    map_app = (DASHBOARD_ROOT / "map_app.py").read_text()
    nav = (DASHBOARD_ROOT / "templates/nav.html").read_text()
    assert "import spatial_reference_app" in phase3
    assert '"key": "spatial-references"' in map_app
    assert "SELECT 'REFERENCE' AS result_type" in map_app
    assert "coalesce(spatial_geom,geom)" in map_app.lower()
    assert 'href="/spatial-reference"' in nav
