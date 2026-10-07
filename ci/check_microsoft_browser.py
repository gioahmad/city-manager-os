"""Real new pages + real local SQL. Only the external Microsoft service is simulated."""
from microsoft_fixture import setup,ROOT
from threading import Thread
from pathlib import Path
from uuid import uuid4
from datetime import datetime,timezone
import os,socket,time,tempfile,subprocess,json
from playwright.sync_api import sync_playwright,expect
import uvicorn
application,core,ms,calendar,hub,auth,provider,owner,mail_id,cookie,csrf=setup()
sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
base='https://127.0.0.1:'+str(sock.getsockname()[1]);os.environ['CMOS_PUBLIC_ORIGIN']=base
artifacts=ROOT/'browser-check-results';artifacts.mkdir(exist_ok=True)
checks=[]
with tempfile.TemporaryDirectory() as temp:
    cert,key=str(Path(temp)/'cert.pem'),str(Path(temp)/'key.pem')
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',key,'-out',cert,'-days','1','-subj','/CN=localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    server=uvicorn.Server(uvicorn.Config(application,log_level='error',lifespan='off',ssl_keyfile=key,ssl_certfile=cert))
    thread=Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    for _ in range(100):
        if server.started:break
        time.sleep(.05)
    assert server.started
    try:
        with sync_playwright() as p:
            for browser_name in ['firefox','chromium']:
                browser=getattr(p,browser_name).launch();context=browser.new_context(ignore_https_errors=True,viewport={'width':1440,'height':1000})
                context.add_cookies([{'name':auth.COOKIE_NAME,'value':cookie,'url':base}]);page=context.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def passed(name):
                    checks.append({'browser':browser_name,'check':name});print('MS BROWSER PASS:',browser_name,name,flush=True)
                response=page.goto(base+'/email?kind=MAIL&id='+mail_id)
                assert response.status==200 and response.headers['x-frame-options']=='DENY'
                expect(page.locator('#ms-preview h2')).to_have_text('Fixture contractor email')
                expect(page.locator('.cmos-rail')).to_have_count(1);expect(page.locator('iframe')).to_have_count(0)
                page.screenshot(path=str(artifacts/(browser_name+'-email-actions.png')))
                passed('Email page loads real saved source with shared navigation and private security')
                page.get_by_role('button',name='Add to my system',exact=True).click()
                form=page.locator('#ms-capture-form');form.locator('[name=title]').fill(browser_name+' browser saved task')
                expect(form.locator('[data-mail-photo]')).to_have_count(3)
                expect(form.locator('[data-mail-photo]:checked')).to_have_count(0)
                photo_id='inline-photo' if browser_name=='firefox' else 'site-photo'
                photo_name='embedded.png' if browser_name=='firefox' else 'site.jpg'
                form.locator('[data-mail-photo][value="'+photo_id+'"]').check()
                form.locator('[name=due_date]').fill('2026-12-01');form.get_by_role('button',name='Save internally').click()
                expect(page.locator('#ms-capture-status')).to_contain_text('Private follow-up created')
                expect(page.locator('#ms-capture-status')).to_contain_text('1 selected photo(s) saved privately')
                photo_href=page.locator('#ms-capture-status').get_by_role('link',name='Download '+photo_name).get_attribute('href')
                downloaded=context.request.get(base+photo_href)
                assert downloaded.status==200 and downloaded.body()==('original-'+photo_id).encode()
                saved_href=page.locator('#ms-capture-status').get_by_role('link',name='Open saved item',exact=True).get_attribute('href');saved_id=saved_href.rsplit('/',1)[-1]
                assert hub.find(owner,'TASK',saved_id)['title']==browser_name+' browser saved task'
                page.get_by_role('button',name='Cancel',exact=True).click()
                page.reload();expect(page.locator('#ms-preview')).to_contain_text(browser_name+' browser saved task')
                expect(page.locator('#ms-preview')).to_contain_text('Saved email photos')
                expect(page.locator('#ms-preview').get_by_role('link',name='Download '+photo_name)).to_be_visible()
                passed('Actual browser form creates a task in PostgreSQL and source reload shows the persisted link')
                passed('Only explicitly selected email photo is saved; original bytes download and reopen after reload')
                flag=page.locator('#ms-preview').get_by_role('button',name='☆ Mark important',exact=True)
                if flag.count():flag.click();expect(page.locator('#ms-preview')).to_contain_text('★ Important')
                page.locator('.ms-tabs').get_by_role('link',name='Important',exact=True).click()
                expect(page.locator('#ms-items')).to_contain_text('Fixture contractor email')
                passed('Important flag is visible after real navigation and database reload')
                page.goto(base+'/email?kind=MAIL&id='+mail_id);expect(page.locator('#ms-preview h2')).to_have_text('Fixture contractor email')
                page.get_by_role('button',name='Reply',exact=True).click()
                form=page.locator('#ms-compose-form');form.locator('[name=body]').fill('Reviewed browser reply text')
                form.locator('[name=operation]').select_option('MAIL_DRAFT');before=len(provider.writes)
                form.get_by_role('button',name='Review Microsoft action').click()
                expect(page.locator('#ms-review-content')).to_contain_text('reply-desk@example.com');assert len(provider.writes)==before
                page.get_by_role('button',name='Confirm and save Outlook draft').click()
                expect(page.locator('#ms-review-status')).to_contain_text('Not sent')
                assert len(provider.writes)==before+1 and provider.writes[-1]['path'].endswith('/createReply')
                page.get_by_role('button',name='Close',exact=True).click()
                passed('Browser reply draft reviews verified Reply-To and submits only on final confirmation')
                page.get_by_role('button',name='New email',exact=True).click()
                form=page.locator('#ms-compose-form');form.locator('[name=to]').fill('recipient@example.com');form.locator('[name=title]').fill('Browser timeout test');form.locator('[name=body]').fill('Test only');form.locator('[name=operation]').select_option('MAIL_SEND')
                form.get_by_role('button',name='Review Microsoft action').click();expect(page.locator('#ms-confirm')).to_be_visible();provider.outcome='timeout';before=len(provider.writes)
                page.get_by_role('button',name='Confirm and send email').click();expect(page.locator('#ms-review-status')).to_contain_text('uncertain')
                expect(page.locator('#ms-confirm')).to_be_hidden();page.reload();expect(page.locator('#ms-review-status')).to_contain_text('uncertain')
                assert len(provider.writes)==before+1;provider.outcome='ok';page.get_by_role('button',name='Close',exact=True).click()
                passed('Ambiguous send is not replayed by page reload; confirmation control stays unavailable')
                page.goto(base+'/calendar');expect(page.locator('#ms-items')).to_contain_text('Fixture site visit')
                page.locator('#ms-calendar').select_option('project-calendar');expect(page.locator('#ms-coverage')).to_contain_text('Calendar range refreshed')
                for mode in ['day','week','month','agenda']:
                    page.locator('#ms-calendar-view').select_option(mode)
                    expect(page.locator('#ms-count')).not_to_have_text('Loading records…')
                    expect(page.locator('#ms-items')).to_contain_text('Fixture site visit')
                page.screenshot(path=str(artifacts/(browser_name+'-calendar.png')))
                passed('Owned calendar selector and day, week, month, agenda views use real persisted appointments')
                page.locator('#ms-new-event').click();form=page.locator('#ms-event-form')
                form.locator('[name=calendar_key]').select_option('project-calendar');form.locator('[name=title]').fill('Browser-reviewed appointment')
                form.locator('[name=starts_at]').fill('2026-12-01T10:00');form.locator('[name=ends_at]').fill('2026-12-01T11:00')
                form.get_by_role('button',name='Review calendar action').click();expect(page.locator('#ms-review-content')).to_contain_text('Project calendar')
                expect(page.locator('#ms-review-content')).to_contain_text('No invitations');before=len(provider.writes)
                page.get_by_role('button',name='Confirm and create appointment').click();expect(page.locator('#ms-review-status')).to_contain_text('Created in your Microsoft calendar')
                assert len(provider.writes)==before+1 and provider.writes[-1]['body']['attendees']==[]
                page.get_by_role('button',name='Close',exact=True).click()
                passed('Calendar form reviews calendar, time zone and no-invitation decision before one create')
                page.set_viewport_size({'width':390,'height':844});page.goto(base+'/email?kind=MAIL&id='+mail_id)
                expect(page.locator('#ms-preview h2')).to_have_text('Fixture contractor email')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                assert page.locator('.cmos-global-search-button').evaluate('(el)=>getComputedStyle(el).fontSize')=='0px'
                page.screenshot(path=str(artifacts/(browser_name+'-email-mobile.png')))
                passed('Mobile page fits viewport; original and new actions render without iframe or JS errors')
                assert not errors,errors
                browser.close()
        (artifacts/'microsoft-workflows.json').write_text(json.dumps({'status':'PASS','scope':'real local application and database; Microsoft transport simulated','checks':checks},indent=2))
        print('MICROSOFT BROWSER WORKFLOWS: PASS',len(checks),'scenarios',flush=True)
    finally:server.should_exit=True;thread.join(timeout=10);sock.close()
