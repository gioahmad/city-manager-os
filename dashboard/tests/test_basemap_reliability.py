from pathlib import Path


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]


def test_osm_tiles_override_the_private_document_referrer_policy_only_on_tiles():
    private_auth = (DASHBOARD_ROOT / "private_auth.py").read_text()
    mapping = (DASHBOARD_ROOT / "templates/map.html").read_text()
    flood = (DASHBOARD_ROOT / "templates/flood.html").read_text()

    # Private dashboard pages keep the privacy-first document policy. Leaflet
    # applies this narrower override only to OSM image requests, whose service
    # requires a valid web Referer.
    assert 'response.headers["Referrer-Policy"] = "no-referrer"' in private_auth
    assert "const OSM_TILE_HOST='tile.openstreetmap.org'" in mapping
    assert "options.referrerPolicy=OSM_REFERRER_POLICY" in mapping
    assert "options.attribution=OSM_ATTRIBUTION" in mapping
    assert "referrerPolicy:'strict-origin-when-cross-origin'" in flood
    assert 'href="https://www.openstreetmap.org/copyright"' in mapping
    assert 'href="https://www.openstreetmap.org/copyright"' in flood


def test_basemap_failure_does_not_hide_local_operational_layers():
    mapping = (DASHBOARD_ROOT / "templates/map.html").read_text()
    flood = (DASHBOARD_ROOT / "templates/flood.html").read_text()

    assert "layer.on('tileerror'" in mapping
    assert "Local alerts, Watches, parcels, and searches still work." in mapping
    assert "Alerts, Watches, parcels, searches, and other operational layers remain available" in mapping

    assert "basemap.on('tileerror'" in flood
    assert "Local FEMA flood zones remain available." in flood
    assert 'id="flood-basemap-status"' in flood


def test_geolibre_is_not_loaded_from_a_public_host_or_given_database_credentials():
    templates = "\n".join(
        path.read_text()
        for path in (DASHBOARD_ROOT / "templates").glob("*.html")
    )
    assert "web.geolibre.app" not in templates
    assert "postgresql://" not in templates
