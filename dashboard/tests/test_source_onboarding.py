from __future__ import annotations

import source_onboarding as so


def base_source(**overrides):
    source = {
        "id": "00000000-0000-0000-0000-000000000001",
        "integration_key": "TEST_SOURCE",
        "name": "Test Source",
        "active": False,
        "category": "EVENTS",
        "adapter_type": "HTTP",
        "endpoint_url": "https://example.com/events.json",
        "method": "GET",
        "auth_type": "NONE",
        "auth_config": {},
        "parser_kind": "JSON_EVENTS",
        "parser_config": {"list_path": "", "mapping": {"id": "id", "title": "title"}},
        "config_version": 3,
        "last_test_ok": None,
        "last_test_config_version": None,
    }
    source.update(overrides)
    return source


def test_template_catalog_covers_requested_shells():
    expected = {"GENERIC_JSON", "GEOJSON", "RSS", "ATOM", "ICS", "SOCRATA", "ARCGIS", "GTFS_STATIC", "GTFS_RT", "CSV", "XML"}
    assert expected.issubset(set(so.SOURCE_TEMPLATES))


def test_placeholder_can_exist_but_is_not_activatable():
    state = so.activation_state(base_source(endpoint_url="", parser_kind="NONE"))
    assert not state["can_activate"]
    assert any("Endpoint URL" in issue for issue in state["setup_issues"])


def test_current_successful_test_unlocks_activation():
    state = so.activation_state(base_source(last_test_ok=True, last_test_config_version=3))
    assert state["tested_current"] is True
    assert state["can_activate"] is True


def test_stale_test_does_not_unlock_activation():
    state = so.activation_state(base_source(last_test_ok=True, last_test_config_version=2))
    assert state["tested_current"] is False
    assert state["can_activate"] is False


def test_missing_environment_secret_is_reported(monkeypatch):
    monkeypatch.delenv("CMOS_TEST_API_KEY", raising=False)
    state = so.activation_state(base_source(auth_type="API_KEY_QUERY_ENV", auth_config={"key_env": "CMOS_TEST_API_KEY", "key_name": "apikey"}))
    assert "CMOS_TEST_API_KEY" in state["missing_secrets"]
    assert not state["can_activate"]


def test_present_environment_secret_is_never_returned(monkeypatch):
    monkeypatch.setenv("CMOS_TEST_TOKEN", "super-secret-value")
    rows = so.integration_env_state(base_source(auth_type="BEARER_ENV", auth_config={"token_env": "CMOS_TEST_TOKEN"}))
    assert rows == [{"field": "token_env", "env": "CMOS_TEST_TOKEN", "set": True}]
    assert "super-secret-value" not in repr(rows)


def test_http_parser_none_remains_setup_only():
    issues = so.collection_setup_issues(base_source(parser_kind="NONE"))
    assert any("supported parser" in issue for issue in issues)


def test_gtfs_static_requires_provider_key():
    source = base_source(category="TRANSIT", adapter_type="TRANSIT_GTFS_URL", parser_kind="NONE", parser_config={})
    issues = so.collection_setup_issues(source)
    assert any("provider_key" in issue for issue in issues)


def test_keyword_split_is_deduplicated_case_insensitively():
    assert so._split_keywords("Weehawken, Lincoln Tunnel\nweehawken") == ["Weehawken", "Lincoln Tunnel"]
