"""Real workspace/context routes, templates and private security middleware.
Only storage data and destination content are fixture-backed. No production host,
credential, database, notification or Microsoft service is contacted.
"""
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs,urlsplit
import json,os,socket,sys,time
ROOT=Path(__file__).resolve().parents[1]
DASHBOARD=ROOT/'dashboard'
sys.path.insert(0,str(DASHBOARD));os.chdir(DASHBOARD)
os.environ.update(DB_PASSWORD='fixture',CMOS_AUTH_ENABLED='true',CMOS_AUTH_SECURE_COOKIES='false',
    CMOS_SESSION_SECRET='navigation-fixture-session-secret-at-least-32-characters',
    CMOS_EXECUTIVE_USERNAME='NavigationTest',CMOS_EXECUTIVE_PASSWORD_HASH='unused')
from fastapi import FastAPI,Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright,expect
import uvicorn
import private_auth as auth
import workspace_app as ws
import context_app as context
from app import templates
ID='00000000-0000-4000-8000-000000000001'
REF='WPD_RADIO:cac06c94'
STAMP='2026-10-05T12:00:00Z'
CONFIG={**ws.DEFAULT_CONFIG,'name':'City Manager OS','organization':'Fixture only'}
ITEM=dict(kind='MAIL',id=ID,title='Exact test email',body='Fixture source; no real email.',status='Unread',
    visibility='PRIVATE',updated_at=STAMP,metadata={},route='/intake?kind=MAIL&id='+ID,links=[])
context._alert_record=lambda owner,record_id:{**ITEM,'kind':'ALERT','title':'Test alert','metadata':{'alert_id':REF}}
context._hub_record=lambda owner,kind,record_id:{**ITEM,'kind':kind}
context._context_insights=lambda *args:([],[])
context.query_one=lambda *args,**kwargs:{'alert_id':REF}
app=FastAPI()
app.mount('/static',StaticFiles(directory=str(DASHBOARD/'static')),name='static')
for path,endpoint in [('/intake',ws.executive_intake_route),('/library',ws.workspace_library_route),('/workspace',ws.workspace_page),
    ('/workspace/display',ws.workspace_page),('/context/{item_kind}/{item_id}',context.context_page)]:
    app.add_api_route(path,endpoint,methods=['GET'])
@app.get('/appearance')
def appearance():return ws.DEFAULT_APPEARANCE
@app.get('/workspace/api/hub')
def hub():
    return dict(config=CONFIG,refreshed_at=STAMP,items=[{**ITEM,'snippet':'Fixture email','attention':False,'handled':False}],has_more=False,local_answers=False)
@app.get('/workspace/api/hub/detail/{kind}/{record_id}')
def detail(kind:str,record_id:str):
    return dict(item={**ITEM,'kind':kind,'id':record_id},links=[],suggestions=[],inbox_state={})
@app.get('/workspace/api/state')
def state():
    return dict(config=CONFIG,refreshed_at=STAMP,today='2026-10-05',calendar={'connected':False,'ready':False},connections=[],
        entities=[],relationships=[],suggestions=[],dates=[],reminders=[],alerts=[],health=[],notices=[],work=[],notes=[],portals=[],
        personal=[],appointments=[],goals={'water_ml':None,'protein_g':None},fast={'id':None},stats={},water_ml=0,protein_g=0)
# The original login handler must precede the destination fallback.
auth.configure_private_auth(app)
@app.get('/{destination:path}',response_class=HTMLResponse)
def destination(request:Request,destination:str):
    nav=templates.get_template('nav.html').render(request=request,page=destination or 'Overview')
    return '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body>'+nav+'''<main><h1 id="destination">Destination fixture</h1>
<a id="legacy-action" href="/alerts?q=WPD_RADIO%3Acac06c94&amp;window=all&amp;state=all" data-cmos-quicklook>Legacy action</a>
<div id="record-card" tabindex="0" role="link" data-cmos-context="/context/ALERT/'''+ID+'''">Open record <button id="nested-button" type="button">Do not navigate</button></div>
</main></body></html>'''
sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
base='http://127.0.0.1:'+str(sock.getsockname()[1])
server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
thread=Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
for _ in range(100):
    if server.started:break
    time.sleep(.05)
assert server.started
artifacts=ROOT/'browser-check-results';artifacts.mkdir(exist_ok=True)
results=[]
def security(response):
    assert response.status==200,(response.url,response.status)
    assert response.headers.get('x-frame-options')=='DENY'
    assert "frame-ancestors 'none'" in response.headers.get('content-security-policy','')
def shell(page):
    expect(page.locator('.cmos-rail')).to_have_count(1)
    expect(page.locator('.cmos-global-bar')).to_have_count(1)
    expect(page.locator('#rail')).to_have_count(0)
    expect(page.locator('iframe,.cmos-sidecar')).to_have_count(0)
def passed(browser,name):
    results.append({'browser':browser,'check':name});print('AUTH NAV PASS:',browser,name,flush=True)
try:
    with sync_playwright() as p:
        for browser_name in ['firefox','chromium']:
            browser=getattr(p,browser_name).launch()
            bc=browser.new_context(viewport={'width':1440,'height':1000})
            session=auth._issue_session(auth.Account('NavigationTest','EXECUTIVE',''))
            bc.add_cookies([{'name':auth.COOKIE_NAME,'value':session,'url':base}])
            page=bc.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            response=page.goto(base+'/intake?kind=MAIL&id='+ID)
            security(response);shell(page)
            expect(page.locator('#inbox-preview .preview-title')).to_have_text('Exact test email')
            expect(page.locator('.cmos-rail-nav a.active')).to_have_attribute('href','/intake')
            assert urlsplit(page.url).path=='/intake'
            expect(page.locator('#notice')).to_have_text('')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            page.screenshot(path=str(artifacts/(browser_name+'-intake.png')))
            page.reload();expect(page.locator('#inbox-preview .preview-title')).to_have_text('Exact test email')
            passed(browser_name,'Actual Intake route: shared rail, exact source and reload')
            page.goto(base+'/workspace?view=inbox&kind=MAIL&id='+ID+'&intake_q=Park%20Avenue')
            assert urlsplit(page.url).path=='/intake'
            assert parse_qs(urlsplit(page.url).query)['intake_q']==['Park Avenue']
            shell(page)
            passed(browser_name,'Old Workspace Intake bookmark preserves exact ID and search')
            page.goto(base+'/library');shell(page)
            expect(page.locator('[data-panel="library"]')).to_be_visible()
            expect(page.locator('.cmos-rail-nav a.active')).to_have_attribute('href','/library')
            page.get_by_role('link',name='Workspace settings',exact=True).click()
            expect(page.locator('[data-panel="settings"]')).to_be_visible()
            expect(page.locator('#notice')).to_have_text('')
            shell(page)
            passed(browser_name,'Library and Settings retain shared navigation and controls')
            expected={'Open Alert':('/alerts','q'),'Map':('/map','q'),'Create Work':('/issues','from_alert'),'Create Watch':('/watchlist','from_alert'),'Share':('/share','alert')}
            for label,(path,key) in expected.items():
                security(page.goto(base+'/context/ALERT/'+ID));shell(page)
                page.locator('.context-actions').get_by_role('link',name=label,exact=True).click()
                page.wait_for_url('**'+path+'?**')
                assert parse_qs(urlsplit(page.url).query)[key]==[REF]
                shell(page)
                if label=='Open Alert':assert parse_qs(urlsplit(page.url).query)['window']==['all']
            passed(browser_name,'All five real Context action links open full pages with exact alert reference')
            page.goto(base+'/transit');page.locator('#legacy-action').click()
            page.wait_for_url('**/alerts?**');shell(page)
            page.goto(base+'/schedule');page.locator('#nested-button').click()
            assert urlsplit(page.url).path=='/schedule'
            page.locator('#record-card').focus();page.keyboard.press('Enter')
            page.wait_for_url('**/context/ALERT/**');shell(page)
            page.goto(base+'/watchlist')
            page.evaluate("window.CMOS.openSidecar('/map?q=WPD_RADIO%3Acac06c94','Map')")
            page.wait_for_url('**/map?**');shell(page)
            passed(browser_name,'Legacy markers, record keyboard navigation and programmatic callers use full pages')
            page.goto(base+'/alerts?embed=1');shell(page)
            assert 'embed' not in parse_qs(urlsplit(page.url).query)
            passed(browser_name,'Old embed bookmarks cannot hide navigation')
            page.set_viewport_size({'width':390,'height':844})
            page.goto(base+'/intake');shell(page)
            toggle=page.locator('.cmos-mobile-rail-toggle');toggle.click()
            expect(toggle).to_have_attribute('aria-expanded','true')
            page.keyboard.press('Escape');expect(toggle).to_have_attribute('aria-expanded','false')
            page.locator('#inbox-items .hub-item').first.click()
            expect(page.locator('#inbox-preview .preview-title')).to_have_text('Exact test email')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            page.screenshot(path=str(artifacts/(browser_name+'-intake-mobile.png')))
            passed(browser_name,'Mobile Intake: menu, Escape, selected source and viewport fit')
            bc.clear_cookies()
            page.goto(base+'/intake?kind=MAIL&id='+ID)
            assert urlsplit(page.url).path=='/login'
            assert parse_qs(urlsplit(page.url).query)['next']==['/intake?kind=MAIL&id='+ID]
            expect(page.locator('iframe')).to_have_count(0)
            passed(browser_name,'Expired session opens full-page login with exact return URL')
            assert not errors,errors
            passed(browser_name,'No uncaught JavaScript errors with actual security headers enabled')
            browser.close()
    (artifacts/'authenticated-navigation.json').write_text(json.dumps({'status':'PASS','checks':results},indent=2))
    print('AUTHENTICATED NAVIGATION: PASS',len(results),'browser scenarios',flush=True)
finally:
    server.should_exit=True;thread.join(timeout=10);sock.close()
