"""Real PostgreSQL lifecycle for Google Drive event references. No Google network access."""
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from microsoft_fixture import setup

application, core, ms, calendar, hub, auth, provider, owner, mail_id, cookie, csrf = setup()
ROOT = Path(__file__).resolve().parents[1]

with core.db_conn() as c:
    c.execute((ROOT / 'deploy/postgis/init/043_event_material_links.sql').read_text())
    c.execute((ROOT / 'deploy/postgis/init/043_event_material_links.sql').read_text())
    event = c.execute(
        """INSERT INTO operational_events(title,category,starts_at)
           VALUES('Fixture civic dinner','OTHER',now()) RETURNING id"""
    ).fetchone()

import event_materials
import context_app
for route in list(core.app.routes):
    path = getattr(route, 'path', '')
    if path.startswith('/event-materials') or path.startswith('/context/'):
        application.router.routes.append(route)

with TestClient(application, base_url='https://fixture.example.com') as client:
    client.cookies.set(auth.COOKIE_NAME, cookie)
    url = 'https://drive.google.com/file/d/1AbC_defGHIjkLMnopQR/view?usp=sharing'
    response = client.post('/event-materials/add', data={
        'source_kind':'EVENT','source_id':str(event['id']),'title':'Dinner program',
        'url':url,'notes':'Table assignments and honorees','csrf':csrf,
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    with core.db_conn() as c:
        row = c.execute(
            'SELECT * FROM workspace_event_materials WHERE owner_username=%s AND source_id=%s',
            (owner, event['id']),
        ).fetchone()
        assert row and row['url'] == url and row['external_id'] == '1AbC_defGHIjkLMnopQR'
        columns = c.execute(
            """SELECT column_name,data_type FROM information_schema.columns
               WHERE table_name='workspace_event_materials'"""
        ).fetchall()
        assert not any(r['data_type']=='bytea' for r in columns)
    page = client.get('/context/EVENT/'+str(event['id']))
    assert page.status_code == 200 and 'Dinner program' in page.text
    assert 'no PDF or photo is copied to the VPS' in page.text
    assert event_materials.materials('different-owner','EVENT',event['id']) == []
    bad = client.post('/event-materials/add', data={
        'source_kind':'EVENT','source_id':str(event['id']),'title':'Bad',
        'url':'https://example.com/file.pdf','csrf':csrf,
    })
    assert bad.status_code == 400
    delete = client.post('/event-materials/'+str(row['id'])+'/delete', data={'csrf':csrf}, follow_redirects=False)
    assert delete.status_code == 303
    with core.db_conn() as c:
        assert not c.execute('SELECT 1 FROM workspace_event_materials WHERE id=%s',(row['id'],)).fetchone()

print('EVENT MATERIALS: PASS — private Google Drive references save/reopen/detach with no file bytes or Google network access')
