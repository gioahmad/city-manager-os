"""Navigation checks against real route handlers and security middleware."""
from pathlib import Path
import os
from urllib.parse import parse_qs,urlsplit
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
os.environ.setdefault('DB_PASSWORD','test')
import private_auth as auth
import workspace_app as ws
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('CMOS_AUTH_ENABLED','true')
    monkeypatch.setenv('CMOS_SESSION_SECRET','native-navigation-test-secret-at-least-32-characters')
    monkeypatch.setenv('CMOS_EXECUTIVE_USERNAME','NavTest')
    monkeypatch.setenv('CMOS_EXECUTIVE_PASSWORD_HASH','unused')
    app=FastAPI()
    for path,endpoint in [('/intake',ws.executive_intake_route),('/library',ws.workspace_library_route),('/workspace',ws.workspace_page),('/workspace/display',ws.workspace_page)]:
        app.add_api_route(path,endpoint,methods=['GET'])
    auth.configure_private_auth(app)
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE_NAME,auth._issue_session(auth.Account('NavTest','EXECUTIVE','')))
        yield c

@pytest.mark.parametrize('path,view',[('/intake','inbox'),('/library','library'),('/workspace?view=settings','settings')])
def test_real_page_uses_shared_navigation_and_security(client,path,view):
    response=client.get(path,follow_redirects=False)
    assert response.status_code==200
    assert response.headers['x-frame-options']=='DENY'
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    assert response.text.count('class="cmos-rail"')==1
    assert 'id="rail"' not in response.text
    assert 'data-initial-view="'+view+'"' in response.text
    assert '<iframe' not in response.text
    assert response.text.count('id="qc-open"')==1

@pytest.mark.parametrize('view,path',[('inbox','/intake'),('library','/library')])
def test_old_bookmarks_preserve_record_and_filters(client,view,path):
    r=client.get('/workspace?view='+view+'&kind=MAIL&id=exact-id&intake_q=Park+%26+River&scope=work',follow_redirects=False)
    assert r.status_code==303
    url=urlsplit(r.headers['location'])
    assert url.path==path
    assert parse_qs(url.query)=={'kind':['MAIL'],'id':['exact-id'],'intake_q':['Park & River'],'scope':['work']}

def test_display_does_not_include_private_navigation(client):
    r=client.get('/workspace/display')
    assert r.status_code==200
    assert 'class="cmos-rail"' not in r.text
    assert 'data-initial-view="intelligence"' in r.text

def test_no_iframe_navigation_or_context_action_interception():
    source=(ROOT/'static/macro_layer.js').read_text()
    assert '<iframe' not in source
    assert "createElement('iframe')" not in source
    assert 'frame.src=' not in source
    assert 'location.assign(target.href)' in source
    actions=(ROOT/'templates/context.html').read_text().split('<section class="context-actions">',1)[1].split('</section>',1)[0]
    assert 'href="{{ url }}"' in actions
    assert 'data-cmos-quicklook' not in actions

def test_unauthenticated_intake_goes_to_full_page_login(client):
    client.cookies.clear()
    r=client.get('/intake?kind=MAIL&id=exact-id',follow_redirects=False)
    assert r.status_code==303
    assert parse_qs(urlsplit(r.headers['location']).query)['next']==['/intake?kind=MAIL&id=exact-id']
