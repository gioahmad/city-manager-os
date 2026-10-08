"""Real settings, module guards and browser controls on the CI-local database."""
import json
import os
import socket
import sys
import time
from pathlib import Path
from threading import Thread

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from psycopg.types.json import Jsonb
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'ci'))
_, core, _, _, _, auth, provider, owner, mail_id, cookie, csrf = __import__('microsoft_fixture').setup()
os.environ.update(CMOS_SUPERVISOR_USERNAME='ModuleSupervisor', CMOS_SUPERVISOR_PASSWORD_HASH='unused',
                  CMOS_AUTOMATION_TOKEN='module-fixture-token')
import phase3_app
import workspace_modules as modules

client = TestClient(core.app, base_url='https://fixture.example.com')
client.cookies.set(auth.COOKIE_NAME, cookie)
headers = {'Origin': 'https://fixture.example.com'}

def settings():
    return core.query_one('SELECT settings FROM workspace_config WHERE singleton=true')['settings']

def set_module(key, enabled):
    response = client.post('/modules/'+key, data={'csrf': csrf, 'enabled': str(enabled).lower()},
                           headers=headers, follow_redirects=False)
    assert response.status_code == 303, (response.status_code, response.text)

@core.app.get('/map/module-fixture-probe')
def automation_probe():
    return {'fixture': True}

original = settings()
sock = server = thread = None
try:
    with core.db_conn() as c:
        c.execute("UPDATE workspace_config SET settings=(settings-'modules') || %s::jsonb WHERE singleton=true",
                  (Jsonb({'notification_explanations': False, 'module_test_sentinel': 'keep'}),))
    paths = [r.path for r in core.app.routes if 'GET' in (getattr(r, 'methods', set()) or set())]
    assert paths.count('/modules') == 1
    assert all(modules.module_for_path(path) is None for path in
               ('/', '/watchlist', '/alerts', '/subscribers', '/modules', '/integrations', '/maple', '/brainstorm'))
    response = client.get('/modules')
    assert response.status_code == 200 and response.text.count('data-module-state>On<') == len(modules.MODULES)
    for key in modules.MODULES:
        set_module(key, False)
    assert settings()['modules'] == {key: False for key in modules.MODULES}
    assert settings()['notification_explanations'] is False and settings()['module_test_sentinel'] == 'keep'
    before_notes = core.query_one('SELECT count(*) AS n FROM brain_notes')['n']
    before_events = core.query_one('SELECT count(*) AS n FROM operational_events')['n']
    for path in ('/brain', '/brain/export', '/workspace/brain.md', '/workspace?view=brain',
                 '/workspace/api/state?view=brain', '/map', '/map/layer/create', '/flood', '/schedule',
                 '/event-intelligence', '/transit', '/spatial-reference', '/api/spatial-reference/search',
                 '/staff-admin', '/today-board', '/operations-routines', '/operations-runs/fixture/ack'):
        response = client.get(path)
        assert response.status_code == 423, (path, response.status_code, response.text[:300])
    for path in ('/brain/save', '/schedule/create', '/transit/watch/create', '/map/layer/create'):
        assert client.post(path, headers=headers).status_code == 423
    for path, payload in (
        ('/workspace/api/action', {'action': 'CAPTURE', 'destination': 'BRAIN', 'body': 'Blocked capture'}),
        ('/workspace/api/hub/action', {'action': 'BRAIN', 'kind': 'MAIL', 'id': mail_id}),
        ('/workspace/api/hub/action', {'action': 'EVENT', 'kind': 'MAIL', 'id': mail_id}),
        ('/workspace/api/microsoft/capture', {'destination': 'BRAIN', 'kind': 'MAIL', 'id': mail_id}),
        ('/workspace/api/microsoft/capture', {'destination': 'EVENT', 'kind': 'MAIL', 'id': mail_id}),
    ):
        response = client.post(path, json={'csrf': csrf, **payload}, headers=headers)
        assert response.status_code == 423, (path, response.status_code, response.text)
    assert core.query_one('SELECT count(*) AS n FROM brain_notes')['n'] == before_notes
    assert core.query_one('SELECT count(*) AS n FROM operational_events')['n'] == before_events
    response = client.get('/brain', headers={'Accept': 'text/html'})
    assert response.status_code == 423 and 'Brain is turned off' in response.text
    assert response.headers['x-frame-options'] == 'DENY'
    response = client.get('/modules')
    assert response.text.count('data-module-state>Off<') == len(modules.MODULES)
    assert 'href="/brain"' not in response.text and 'href="/map"' not in response.text
    assert all('href="'+path+'"' in response.text for path in ('/alerts', '/watchlist', '/subscribers', '/modules'))
    assert client.get('/health').status_code == 200
    assert client.get('/appearance').status_code == 200
    assert client.get('/map/module-fixture-probe', headers={'x-cmos-automation-key': 'module-fixture-token'}).status_code == 200
    assert client.post('/modules/brain', data={'csrf': 'bad', 'enabled': 'true'}, headers=headers).status_code == 403
    assert client.post('/modules/brain', data={'csrf': csrf, 'enabled': 'true'}, headers={'Origin': 'https://wrong.example'}).status_code == 403
    assert client.post('/modules/unknown', data={'csrf': csrf, 'enabled': 'true'}, headers=headers).status_code == 400
    assert client.post('/modules/brain', data={'csrf': csrf, 'enabled': 'maybe'}, headers=headers).status_code == 400
    for username, role in (('ModuleSupervisor', 'SUPERVISOR'), ('Reader', 'READ_ONLY')):
        client.cookies.set(auth.COOKIE_NAME, auth._issue_session(auth.Account(username, role, '')))
        page = client.get('/modules')
        assert page.status_code == 200 and 'method="post" action="/modules/' not in page.text
        assert client.post('/modules/brain', data={'csrf': csrf, 'enabled': 'true'}, headers=headers).status_code == 403
    client.cookies.clear()
    assert client.get('/modules', follow_redirects=False).status_code == 303
    client.cookies.set(auth.COOKIE_NAME, cookie)
    assert settings()['modules']['brain'] is False
    set_module('brain', True)
    assert settings()['modules']['mapping'] is False and client.get('/brain').status_code == 200
    print('MODULE API PASS: default on; atomic independent switches; direct and shared writes blocked; roles, CSRF, origin, automation and recovery', flush=True)

    sock = socket.socket();sock.bind(('127.0.0.1', 0));sock.listen(128)
    base = 'http://127.0.0.1:'+str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(core.app, log_level='error', lifespan='off'))
    thread = Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True);thread.start()
    for _ in range(100):
        if server.started:break
        time.sleep(.05)
    assert server.started
    # Native form submissions must match the origin configured on the deployed app.
    os.environ['CMOS_PUBLIC_ORIGIN'] = base
    with sync_playwright() as p:
        for name in ('firefox', 'chromium'):
            browser = getattr(p, name).launch()
            context = browser.new_context(viewport={'width': 390, 'height': 844})
            context.add_cookies([{'name': auth.COOKIE_NAME, 'value': cookie, 'url': base}])
            page = context.new_page();errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base+'/modules')
            row = page.locator('[data-module="brain"]')
            expect(row.locator('[data-module-state]')).to_have_text('On')
            row.get_by_role('button', name='Turn off Brain', exact=True).click()
            expect(row.locator('[data-module-state]')).to_have_text('Off')
            page.reload();expect(row.locator('[data-module-state]')).to_have_text('Off')
            assert page.locator('.cmos-rail-nav a[href="/brain"]').count() == 0
            page.evaluate("localStorage.setItem('cmos.macro.recent.v1',JSON.stringify([{url:'/brain',title:'Brain fixture'},{url:'/map',title:'Map fixture'}]))")
            page.keyboard.press('Control+p')
            page.locator('.cmos-command-input').fill('Brain')
            expect(page.locator('.cmos-command-item')).to_have_count(0)
            page.locator('.cmos-command-input').fill('Mapping')
            expect(page.locator('.cmos-command-item')).to_have_count(0)
            page.locator('.cmos-command-input').fill('Modules')
            expect(page.locator('.cmos-command-item')).to_have_count(1)
            page.keyboard.press('Escape')
            response = page.goto(base+'/brain')
            assert response.status == 423
            expect(page.get_by_role('heading', name='Brain is turned off', exact=True)).to_be_visible()
            page.get_by_role('link', name='Manage modules', exact=True).click()
            page.locator('[data-module="brain"]').get_by_role('button', name='Turn on Brain', exact=True).click()
            expect(page.locator('[data-module="brain"] [data-module-state]')).to_have_text('On')
            assert page.locator('.cmos-rail-nav a[href="/brain"]').count() == 1
            assert page.goto(base+'/brain').status == 200
            page.goto(base+'/modules')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            assert not errors, errors
            assert provider.writes == [], provider.writes
            browser.close()
            print('MODULE BROWSER PASS:', name, 'toggle, reload, menu, command history, saved link, recovery and mobile width', flush=True)
finally:
    if server:server.should_exit = True
    if thread:thread.join(timeout=10)
    if sock:sock.close()
    with core.db_conn() as c:
        c.execute('UPDATE workspace_config SET settings=%s WHERE singleton=true', (Jsonb(original),))
