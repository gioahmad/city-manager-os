"""Real PostgreSQL metadata + NAS filesystem; no production host or vault contacted."""
import base64
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from microsoft_fixture import setup
from fastapi.testclient import TestClient

application, core, ms, calendar, hub, auth, provider, owner, mail_id, cookie, csrf = setup()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import event_memory as memory
from event_memory_protocol import verify, key_bytes
from services.event_memory_bridge.app import create_app

SECRET = base64.urlsafe_b64encode(b'E' * 32).decode()
os.environ.update(EVENT_MEMORY_OWNER=owner, EVENT_MEMORY_KEY=SECRET,
                  EVENT_MEMORY_BRIDGE_ORIGIN='https://nas.example',
                  EVENT_MEMORY_VAULT_NAME='Private Vault')
with core.db_conn() as c:
    c.execute((ROOT/'deploy/postgis/init/043_event_memory.sql').read_text())
    c.execute((ROOT/'deploy/postgis/init/043_event_memory.sql').read_text())
    event = c.execute("INSERT INTO operational_events(title,category,starts_at) VALUES('Event Memory fixture','OTHER',now()) RETURNING id").fetchone()
for route in list(core.app.routes):
    if getattr(route, 'path', '').startswith('/event-memory'):
        application.router.routes.append(route)
memory.install_security(application)
client = TestClient(application, base_url='https://fixture.example.com')
client.cookies.set(auth.COOKIE_NAME, cookie)
PDF = b'%PDF-1.7\nTest event program.\n%%EOF'
values = dict(id=str(uuid4()), source_kind='EVENT', source_id=str(event['id']), filename='Program.pdf',
              bytes=len(PDF), sha256=hashlib.sha256(PDF).hexdigest(), csrf=csrf)

def call(operation, data):
    return client.post('/event-memory/api/'+operation, json={**data, 'csrf': csrf})

os.environ.pop('CMOS_EVENT_MEMORY_ENABLED', None)
assert client.get('/event-memory').status_code == 200
assert call('prepare', values).status_code == 503
print('EVENT MEMORY PASS: disabled by default; no VPS file fallback')
os.environ['CMOS_EVENT_MEMORY_ENABLED'] = 'true'
r = client.get('/event-memory?kind=EVENT&id='+str(event['id']))
assert r.status_code == 200, r.text
assert "connect-src 'self' https://nas.example;" in r.headers['Content-Security-Policy']
assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']
assert 'https://nas.example' not in client.get('/email').headers['Content-Security-Policy']
print('EVENT MEMORY PASS: one shared page; exact bridge permitted only on Event Memory; framing remains denied')
assert client.post('/event-memory/api/prepare', json={**values, 'csrf': 'incorrect'}).status_code == 403
assert client.post('/event-memory/api/prepare', content=b'X'*13000).status_code == 413
print('EVENT MEMORY PASS: CSRF enforced and binary-size bodies rejected by metadata API')
prepared = call('prepare', values)
assert prepared.status_code == 200, prepared.text
grant = prepared.json()
assert call('prepare', values).json()['material']['id'] == values['id']
assert call('prepare', {**values, 'filename': 'Changed.pdf'}).status_code == 409
assert call('finalize', {'receipt': grant['ticket']}).status_code == 400
with tempfile.TemporaryDirectory() as storage:
    bridge = TestClient(create_app(storage, SECRET, owner, 'https://fixture.example.com', 'Private Vault', 0))
    headers = {'Authorization': 'Bearer '+grant['ticket'], 'Content-Type': 'application/pdf'}
    upload = bridge.put('/v1/material', content=PDF, headers=headers)
    assert upload.status_code == 200, upload.text
    # Simulate lost final callback: reconnect via a fresh grant and NAS receipt.
    renewed = call('grant', {'id': values['id'], 'purpose': 'upload'}).json()
    receipt = bridge.get('/v1/receipt', headers={'Authorization': 'Bearer '+renewed['ticket']}).json()
    completed = call('finalize', receipt)
    assert completed.status_code == 200, completed.text
    assert completed.json()['material']['state'] == 'READY'
    assert call('finalize', receipt).status_code == 200
    with core.db_conn() as c:
        row = c.execute('SELECT * FROM event_memory_materials WHERE id=%s', (values['id'],)).fetchone()
        note = c.execute('SELECT * FROM brain_notes WHERE id=%s', (row['brain_note_id'],)).fetchone()
        assert note['owner_username'] == owner and 'Event Memory' in note['body']
        assert 'Test event program.' not in note['body']  # Source bytes not inserted in Brain.
        assert c.execute('SELECT count(*) AS n FROM brain_notes WHERE source_id=%s', ('EVENT_MEMORY:'+values['id'],)).fetchone()['n'] == 1
        assert c.execute('SELECT count(*) AS n FROM workspace_context_links WHERE source_id=%s AND target_id=%s', (note['id'], event['id'])).fetchone()['n'] == 1
        assert c.execute("SELECT data_type FROM information_schema.columns WHERE table_name='event_memory_materials'").fetchall()
        assert not c.execute("SELECT 1 FROM information_schema.columns WHERE table_name='event_memory_materials' AND data_type='bytea'").fetchall()
    page = client.get('/event-memory?kind=EVENT&id='+str(event['id']))
    assert 'Program.pdf' in page.text and 'Open in Obsidian' in page.text
    download = call('grant', {'id': values['id'], 'purpose': 'download'}).json()
    assert bridge.get('/v1/file', headers={'Authorization': 'Bearer '+download['ticket']}).content == PDF
    print('EVENT MEMORY PASS: direct NAS storage, lost-callback recovery, real SQL finalize, Brain link, reopen and no duplicate')
    print('EVENT MEMORY PASS: authenticated NAS download; no original bytes stored in application tables')
# Exercise a genuinely registered second login, not a rejected session.
import hmac
os.environ.update(CMOS_SUPERVISOR_USERNAME='SomeoneElse', CMOS_SUPERVISOR_PASSWORD_HASH='unused')
other_cookie = auth._issue_session(auth.Account('SomeoneElse', 'SUPERVISOR', 'unused'))
client.cookies.clear()
client.cookies.set(auth.COOKIE_NAME, other_cookie)
other_csrf = hmac.new(auth._session_secret(), other_cookie.encode(), hashlib.sha256).hexdigest()
other_page = client.get('/event-memory?kind=EVENT&id='+str(event['id']), follow_redirects=False)
assert other_page.status_code == 200 and 'Program.pdf' not in other_page.text
assert client.post('/event-memory/api/grant', json={'id':values['id'],'csrf':csrf}).status_code == 403
# Even when this separate owner has a vault configured, rows/receipts remain owner-bound.
os.environ['EVENT_MEMORY_OWNER'] = 'SomeoneElse'
denied = client.post('/event-memory/api/grant', json={'id':values['id'],'purpose':'download','csrf':other_csrf})
assert denied.status_code == 404, (denied.status_code, denied.text)
denied = client.post('/event-memory/api/finalize', json={**receipt,'csrf':other_csrf})
assert denied.status_code == 403, (denied.status_code, denied.text)
print('EVENT MEMORY PASS: registered second owner cannot read metadata, redeem grants or finalize another owner receipt')
client.cookies.clear()
client.cookies.set(auth.COOKIE_NAME, auth._issue_session(auth.Account('Reader','READ_ONLY','')))
assert client.post('/event-memory/api/prepare', json=values).status_code == 403
client.cookies.clear()
assert client.get('/event-memory', follow_redirects=False).status_code == 303
print('EVENT MEMORY PASS: read-only and signed-out writes blocked')
print('EVENT MEMORY DATABASE LIFECYCLE: PASS')
