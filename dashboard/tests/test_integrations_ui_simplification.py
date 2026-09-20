from pathlib import Path


TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def test_navigation_has_one_integrations_entry():
    nav = (TEMPLATES / "nav.html").read_text()
    assert "/integrations/onboarding" not in nav
    assert nav.count('href="/integrations"') == 2  # desktop and mobile


def test_monitor_keeps_existing_controls_and_points_to_guided_setup():
    template = (TEMPLATES / "integrations.html").read_text()
    assert 'href="/integrations/onboarding"' in template
    assert "Open Guided Source Setup" in template
    assert "Advanced quick setup" in template
    assert 'action="/integrations/create"' in template
    assert 'action="/integrations/{{ i.id }}/test"' in template
    assert 'action="/integrations/{{ i.id }}/run"' in template
    assert 'action="/integrations/{{ i.id }}/toggle"' in template


def test_guided_setup_hides_technical_fields_without_removing_them():
    template = (TEMPLATES / "source_onboarding.html").read_text()
    assert template.count("Advanced connection, parser, and routing controls") == 2
    for field in (
        "adapter_type",
        "method",
        "auth_type",
        "parser_kind",
        "poll_seconds",
        "map_capable",
        "source_owner",
        "geography_scope",
    ):
        assert template.count(f'name="{field}"') == 2
    assert 'action="/integrations/onboarding/create"' in template
    assert 'action="/integrations/onboarding/{{ i.id }}/update"' in template
