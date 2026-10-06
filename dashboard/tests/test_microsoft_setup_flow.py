from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_microsoft_connection_has_one_guided_setup_surface():
    template = (ROOT / 'templates/microsoft_workspace.html').read_text()
    js = (ROOT / 'static/microsoft_workspace.js').read_text()
    workspace_js = (ROOT / 'static/workspace.js').read_text()
    assert '<h2>Connect Microsoft 365</h2>' in template
    assert 'Continue to Microsoft sign-in' in template
    assert 'Reconnect read access' not in js
    assert 'Enable Microsoft actions' not in js
    assert "Finish Microsoft setup" in js
    assert "Connect once for Email, Contacts, Calendar, Send, Drafts and calendar creation." in js
    assert "'/email?setup=1'" in workspace_js


def test_microsoft_auth_returns_clear_outcomes_and_readiness():
    calendar = (ROOT / 'workspace_calendar.py').read_text()
    workspace = (ROOT / 'microsoft_workspace.py').read_text()
    legacy = (ROOT / 'workspace_app.py').read_text()
    assert "/email?auth=connected" in calendar
    assert "/email?auth=cancelled" in calendar
    assert "/email?auth=failed" in calendar
    assert "'setup':" in calendar
    assert "status['access_complete']" in workspace
    assert "'PERMISSIONS_INCOMPLETE'" in workspace
    assert "return {'redirect_url':'/email?setup=1'}" in legacy
