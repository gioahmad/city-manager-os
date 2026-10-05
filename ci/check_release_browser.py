"""Chromium checks of repository HTML/CSS/JS with synthetic records only.
No production account, database, or notification endpoint is contacted.
Capture/share dialogs are isolated from the shared-shell checks.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
import json
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT/'dashboard'
ARTIFACTS = ROOT/'browser-check-results'
ARTIFACTS.mkdir(exist_ok=True)
env = Environment(loader=ChoiceLoader([
    DictLoader({'quick_capture_global.html': '<button id="qc-open" hidden>Capture fixture</button>', 'share_dialog.html': ''}),
    FileSystemLoader(DASHBOARD/'templates'),
]), autoescape=True)
IDS = ['00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000002']

def alerts_context(query):
    filters = dict(q='',source='',category='',municipality='',county='',state='all',window='all',custom_hours=12,min_priority=1)
    filters.update({k:v[-1] for k,v in query.items() if k in filters})
    rows=[]
    for i,uid in enumerate(IDS):
        rows.append(dict(alert_uuid=uid,alert_id=f'FIXTURE:{i}',title=f'Synthetic alert {i+1}',message='Fixture only',
            source='BNN',category='OTHER',subtype='TEST',priority=2,status='ACTIVE',municipality='Weehawken',
            county='Hudson',activity_at=None,received_at=None,activity_local_value='2026-10-05T12:00',
            received_local_value='2026-10-05T12:01',watch_evidence=[],keyword_choices=[],
            map_latitude=None,map_longitude=None,map_url='/map',map_status='Location not mapped',
            location_label='Fixture',watch_from_alert_url='',track_alert_url='',share_url='',click_url=''))
    return dict(**filters,alerts=rows,counties=[dict(county='Hudson',total=2),dict(county='Bergen',total=1)],
        municipalities=[dict(municipality='Weehawken',county='Hudson',total=1),dict(municipality='Union City',county='Hudson',total=1),dict(municipality='Hackensack',county='Bergen',total=1)],
        sources=[dict(source='BNN',total=2)],categories=[dict(category='OTHER',total=2)],
        counts=dict(total=2,active=2,resolved=0),result_total=2,current_page=1,total_pages=2,
        previous_url='',next_url='/alerts?'+urlencode({**filters,'page':2}),current_url='/alerts?'+urlencode(filters),
        msg='',error='',can_delete_alerts=True,can_correct_alert_locations=True,can_correct_alert_times=True,
        request=SimpleNamespace(url=SimpleNamespace(path='/alerts'),query_params=query),page='alerts')

def intake_fixture():
    blocks=[]
    for name in ('inbox','library'):
        blocks.append(f'''<form id="{name}-filters"><input name="q"><select name="source"><option value="">All</option></select><select name="scope"><option value="both">Both</option></select><select name="bucket"><option value="open">Open</option><option value="all">All</option></select></form>
<div id="{name}-layout"><div id="{name}-items"></div><div id="{name}-preview"></div></div><span id="{name}-count"></span><button id="{name}-more">More</button>''')
    return '''<!doctype html><html><body data-readonly="false" data-csrf="fixture"><header class="topbar">Intake fixture</header><div id="loading-indicator" hidden></div>'''+''.join(blocks)+'''<span id="local-answer-status"></span><form id="library-upload"><input name="files" type="file"><button>Upload</button></form><form id="library-ask"><input name="question"><button>Ask</button></form><div id="library-answer"></div><script src="/static/workspace_hub.js"></script><script>
window.notices=[];
CmosHub.init({notice:(message,error)=>{if(error)window.notices.push(message);},clearCache:()=>{},navigate:()=>{}});
window.initialLoad=CmosHub.load('inbox');
</script></body></html>'''

class Handler(SimpleHTTPRequestHandler):
    def log_message(self,*args):
        pass
    def do_GET(self):
        parts=urlsplit(self.path)
        query=parse_qs(parts.query)
        if parts.path=='/alerts':
            html=env.get_template('alerts.html').render(**alerts_context(query))
        elif parts.path=='/shell':
            nav=env.get_template('nav.html').render(request=SimpleNamespace(url=SimpleNamespace(path='/alerts'),query_params={}),page='alerts')
            html='<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body><header class="topbar"><h1>Alerts</h1></header>'+nav+'<main><h2>Fixture workspace</h2></main></body></html>'
        elif parts.path=='/intake':
            html=intake_fixture()
        else:
            return super().do_GET()
        raw=html.encode()
        self.send_response(200)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(DASHBOARD)))
Thread(target=server.serve_forever,daemon=True).start()
base=f'http://127.0.0.1:{server.server_port}'
checks=[]

def passed(name):
    checks.append(name)
    print('BROWSER PASS:',name,flush=True)

try:
    with sync_playwright() as p:
        browser=p.chromium.launch()
        page=browser.new_page(viewport=dict(width=1440,height=1000))
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(base+'/shell')
        expect(page.locator('.cmos-rail-nav a.active')).to_have_count(1)
        assert page.locator('.cmos-rail-nav a.active').inner_text().strip().endswith('Alerts')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert page.locator('.cmos-rail-nav a.active').evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)')>=12
        page.screenshot(path=str(ARTIFACTS/'desktop.png'))
        passed('Desktop navigation: active destination, readable text, no horizontal overflow')
        page.set_viewport_size(dict(width=390,height=844))
        page.goto(base+'/shell')
        toggle=page.locator('.cmos-mobile-rail-toggle')
        toggle.click()
        expect(toggle).to_have_attribute('aria-expanded','true')
        page.wait_for_function("document.querySelector('.cmos-rail').getBoundingClientRect().left>=-1")
        page.screenshot(path=str(ARTIFACTS/'mobile-menu.png'))
        page.keyboard.press('Escape')
        expect(toggle).to_have_attribute('aria-expanded','false')
        expect(page.locator('.cmos-rail-backdrop')).to_be_hidden()
        expect(page.locator('[data-cmos-capture]')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        passed('Mobile navigation: open, Escape, backdrop, capture, width')
        page.goto(base+'/shell?embed=1')
        expect(page.locator('body')).to_have_class('cmos-embedded')
        expect(page.locator('.cmos-rail')).to_be_hidden()
        expect(page.locator('.cmos-global-bar')).to_be_hidden()
        passed('Embedded preview: no duplicate sidebar or global header')
        page.set_viewport_size(dict(width=1440,height=1000))
        page.goto(base+'/alerts')
        page.select_option('#alerts-county','Hudson')
        page.select_option('#alerts-municipalities',['Weehawken','Union City'])
        expect(page.locator('#alerts-municipality-value')).to_have_value('Weehawken|Union City')
        assert page.locator('#alerts-municipalities option[value="Hackensack"]').evaluate('(el)=>el.hidden')
        page.get_by_role('button',name='Search Alerts',exact=True).click()
        page.wait_for_url('**/alerts?**')
        query=parse_qs(urlsplit(page.url).query)
        assert query['county']==['Hudson']
        assert query['municipality']==['Weehawken|Union City']
        assert page.locator('#alerts-municipalities').evaluate('(el)=>[...el.selectedOptions].map(o=>o.value)')==['Weehawken','Union City']
        next_href=page.get_by_role('link',name='Next',exact=True).get_attribute('href')
        assert parse_qs(urlsplit(next_href).query)['municipality']==['Weehawken|Union City']
        passed('County cascade and multi-town selection survive Search and pagination URLs')
        page.get_by_role('button',name='Bulk Actions',exact=True).click()
        page.locator('[data-alert-select-all]').check()
        assert page.locator('[data-alert-select]').count()==2
        assert page.locator('[data-alert-select]').evaluate_all('(els)=>els.every(e=>e.checked&&e.form.id==="alert-bulk-form")')
        assert page.locator('#alert-bulk-form').evaluate('(el)=>new FormData(el).getAll("alert_ids")')==IDS
        assert page.locator('form[action$="/time-correction"]').count()==2
        page.screenshot(path=str(ARTIFACTS/'alerts.png'))
        passed('All alert checkboxes belong to the bulk form; time-correction forms remain independent')
        intake=browser.new_page(viewport=dict(width=1200,height=900))
        intake.on('pageerror',lambda e:errors.append(str(e)))
        intake.add_init_script('''const originalSetTimeout=window.setTimeout;window.setTimeout=(fn,ms,...args)=>{if(ms===60000){window.runPoll=fn;return 987654;}return originalSetTimeout(fn,ms,...args);};''')
        def mock_api(route):
            url=route.request.url
            if '/detail/' in url:
                uid=url.rsplit('/',1)[-1]
                data=dict(item=dict(kind='MAIL',id=uid,title='Exact off-page email',body='Synthetic source body',status='Unread',visibility='PRIVATE',updated_at='2026-10-05T12:00:00Z',metadata={},route='/intake?kind=MAIL&id='+uid),links=[],suggestions=[],inbox_state={})
            elif route.request.method=='POST':
                data={'message':'Fixture action saved'}
            else:
                data=dict(items=[dict(kind='MAIL',id=IDS[1],title='Another email',snippet=None,status=None,visibility='PRIVATE',updated_at='2026-10-05T12:00:00Z',attention=False,handled=False)],has_more=False,local_answers=False)
            route.fulfill(status=200,content_type='application/json',body=json.dumps(data))
        intake.route('**/workspace/api/hub**',mock_api)
        intake.goto(base+'/intake?kind=MAIL&id='+IDS[0])
        expect(intake.locator('#inbox-preview .preview-title')).to_have_text('Exact off-page email')
        intake.evaluate('window.runPoll()')
        expect(intake.locator('#inbox-preview .preview-title')).to_have_text('Exact off-page email')
        assert parse_qs(urlsplit(intake.url).query)['id']==[IDS[0]]
        intake.reload()
        expect(intake.locator('#inbox-preview .preview-title')).to_have_text('Exact off-page email')
        assert intake.evaluate('window.notices')==[]
        passed('Intake exact off-page email survives polling and reload, including optional empty fields')
        intake.get_by_role('button',name='Mark handled',exact=True).click()
        expect(intake.locator('#inbox-preview .preview-title')).to_have_count(0)
        assert 'id' not in parse_qs(urlsplit(intake.url).query)
        passed('Intake deliberate handle clears selection rather than reopening it')
        assert not errors,errors
        passed('No uncaught browser JavaScript errors')
        browser.close()
    (ARTIFACTS/'summary.json').write_text(json.dumps({'status':'PASS','checks':checks},indent=2))
    print(f'BROWSER RELEASE CHECK: PASS ({len(checks)} scenarios)',flush=True)
finally:
    server.shutdown()
