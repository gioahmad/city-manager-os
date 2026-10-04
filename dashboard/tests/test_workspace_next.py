from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_workspace_next_is_parallel_read_layer():
    source=(ROOT/"workspace_next_app.py").read_text()
    assert '@app.get("/workspace-next"' in source
    assert '@app.get("/api/workspace-next/home")' in source
    assert '@app.get("/api/workspace-next/inbox")' in source
    assert '@app.get("/api/workspace-next/documents")' in source
    assert '@app.get("/api/objects/search")' in source
    assert '@app.get("/api/objects/{kind}/{object_id}")' in source
    assert "@app.post(" not in source


def test_unified_objects_preserve_authoritative_systems():
    source=(ROOT/"unified_objects.py").read_text()
    for table in (
        "workspace_entities",
        "workspace_relationships",
        "workspace_messages",
        "spatial_reference_entities",
        "gis_parcels",
        "issues",
        "issue_updates",
        "alerts",
        "alert_watch_matches",
        "watch_items",
        "watch_item_recipients",
        "deliveries",
        "event_intelligence",
        "source_health",
    ):
        assert table in source


def test_shell_has_one_primary_navigation_model():
    source=(ROOT/"templates"/"workspace_next.html").read_text()
    for label in ("Home","Inbox","Search","Work","People","Places","Map","Documents","Operations","Admin"):
        assert f">{label}<" in source
    assert "Intelligence</span>" not in source
    assert "Watches</span>" not in source
    assert "Events</span>" not in source


def test_admin_keeps_legacy_controls_available():
    source=(ROOT/"static"/"workspace_next.js").read_text()
    for route in (
        "/map","/issues","/alerts","/watchlist","/spatial-reference",
        "/staff-admin","/schedule","/event-intelligence","/transit",
        "/subscribers","/deliveries","/share","/integrations",
        "/source-health","/database","/admin-tools",
    ):
        assert route in source


def test_home_is_default_and_context_is_not_blank():
    source=(ROOT/"static"/"workspace_next.js").read_text()
    assert "renderHome();" in source
    assert "Operational snapshot" in source
    assert "Needs attention" in source
    assert "Live intelligence" in source


def test_mapping_center_embeds_without_legacy_chrome():
    shell=(ROOT/"static"/"workspace_next.js").read_text()
    template=(ROOT/"templates"/"map.html").read_text()
    css=(ROOT/"static"/"map.css").read_text()
    assert 'src="/map?embed=1"' in shell
    assert "request.query_params.get('embed')" in template
    assert "embedded-map" in template
    assert "Unified shell embedded map" in css


def test_workspace_uses_optional_split_windows():
    template=(ROOT/"templates"/"workspace_next.html").read_text()
    js=(ROOT/"static"/"workspace_next.js").read_text()
    css=(ROOT/"static"/"workspace_next.css").read_text()
    assert 'id="window-manager"' in template
    assert 'id="secondary-window" hidden' in template
    assert "openSplit(" in js
    assert "closeSplit()" in js
    assert ".window-manager.split-open" in css


def test_map_uses_full_primary_window():
    js=(ROOT/"static"/"workspace_next.js").read_text()
    assert 'setPrimary(\'Map\'' in js
    assert 'src="/map?embed=1"' in js
