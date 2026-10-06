"""Browser -> two actual HTTPS applications -> NAS disk and PostgreSQL. CI-local only."""
import base64
import hashlib
import ipaddress
import os
import socket
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import sync_playwright, expect
import uvicorn
from microsoft_fixture import setup

application, core, ms, calendar, hub, auth, provider, owner, mail_id, cookie, csrf = setup()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import event_memory as memory
from services.event_memory_bridge.app import create_app

with core.db_conn() as c:
    c.execute((ROOT/'deploy/postgis/init/043_event_memory.sql').read_text())
    event = c.execute("INSERT INTO operational_events(title,category,starts_at) VALUES('Event Memory browser fixture','OTHER',now()) RETURNING id").fetchone()
for route in list(core.app.routes):
    if getattr(route, 'path', '').startswith('/event-memory'):
        application.router.routes.append(route)
memory.install_security(application)
secret = base64.urlsafe_b64encode(b'B'*32).decode()
sockets = []
for _ in range(2):
    sock = socket.socket();sock.bind(('127.0.0.1', 0));sock.listen(128);sockets.append(sock)
base, nas = ['https://127.0.0.1:'+str(s.getsockname()[1]) for s in sockets]
os.environ.update(CMOS_EVENT_MEMORY_ENABLED='true', EVENT_MEMORY_OWNER=owner, EVENT_MEMORY_KEY=secret,
                  EVENT_MEMORY_BRIDGE_ORIGIN=nas, EVENT_MEMORY_VAULT_NAME='Private Event Vault', CMOS_PUBLIC_ORIGIN=base)
artifacts = ROOT/'browser-check-results';artifacts.mkdir(exist_ok=True)
servers, threads = [], []
with tempfile.TemporaryDirectory() as temp:
    directory = Path(temp)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, '127.0.0.1')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
                   .not_valid_after(now+timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = directory/'cert.pem', directory/'key.pem'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    bridge = create_app(directory/'nas', secret, owner, base, 'Private Event Vault', 0)
    try:
        for app, sock in zip([application, bridge], sockets):
            server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off', ssl_certfile=str(cert_path), ssl_keyfile=str(key_path)))
            thread = Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True);thread.start()
            servers.append(server);threads.append(thread)
        for _ in range(100):
            if all(s.started for s in servers):break
            time.sleep(.05)
        assert all(s.started for s in servers)
        with sync_playwright() as pw:
            for name in ['firefox', 'chromium']:
                browser = getattr(pw, name).launch()
                context = browser.new_context(ignore_https_errors=True, viewport={'width':1440,'height':1000})
                context.add_cookies([{'name':auth.COOKIE_NAME, 'value':cookie, 'url':base}])
                page = context.new_page();errors=[];app_sizes=[];nas_uploads=[]
                page.on('pageerror', lambda e: errors.append(str(e)))
                def capture(request):
                    if request.method=='POST' and request.url.startswith(base+'/event-memory/api/'):
                        app_sizes.append(len(request.post_data_buffer or b''))
                    if request.method=='PUT' and request.url==nas+'/v1/material':nas_uploads.append(True)
                page.on('request', capture)
                response = page.goto(base+'/event-memory?kind=EVENT&id='+str(event['id']))
                assert response.status==200
                assert 'connect-src' in response.headers['content-security-policy'] and nas in response.headers['content-security-policy']
                expect(page.locator('.cmos-rail')).to_have_count(1)
                payload=b'%PDF-1.7\nEvent program '+name.encode()+b'\n%%EOF'
                page.locator('#memory-file').set_input_files({'name':name+'-program.pdf','mimeType':'application/pdf','buffer':payload})
                page.get_by_role('button', name='Attach to event', exact=True).click()
                expect(page.locator('.memory-material').filter(has_text=name+'-program.pdf').locator('span')).to_have_text('READY', timeout=15000)
                assert nas_uploads and app_sizes and max(app_sizes)<12000
                assert any(f.read_bytes()==payload for f in (directory/'nas'/'vault').rglob('original.pdf'))
                with core.db_conn() as c:
                    row=c.execute('SELECT * FROM event_memory_materials WHERE descriptor->>\'sha256\'=%s', (hashlib.sha256(payload).hexdigest(),)).fetchone()
                    assert row['state']=='READY' and row['brain_note_id']
                print('EVENT MEMORY BROWSER PASS:',name,'direct cross-origin HTTPS upload, NAS checksum, actual SQL receipt and Brain link',flush=True)
                material=page.locator('.memory-material').filter(has_text=name+'-program.pdf')
                assert material.get_by_role('link',name='Open in Obsidian').get_attribute('href').startswith('obsidian://open?')
                page.reload();expect(material.locator('span')).to_have_text('READY')
                page.screenshot(path=str(artifacts/(name+'-event-memory.png')))
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
                page.screenshot(path=str(artifacts/(name+'-event-memory-mobile.png')))
                assert not errors,errors
                print('EVENT MEMORY BROWSER PASS:',name,'reopen, Obsidian link, one sidebar, mobile layout, no uncaught JavaScript errors',flush=True)
                browser.close()
        print('EVENT MEMORY BROWSER: PASS — four grouped scenarios across Firefox and Chromium; no live NAS or Sync account')
    finally:
        for server in servers:server.should_exit=True
        for thread in threads:thread.join(timeout=10)
        for sock in sockets:sock.close()
