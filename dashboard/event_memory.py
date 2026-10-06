"""Private Event Memory references. This module never accepts or proxies file bytes."""
import hmac
import json
import os
from urllib.parse import urlencode
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import app, db_conn, query_all, query_one, templates
from brain_app import _csrf, _owner, _write
from event_memory_protocol import descriptor, key_bytes, obsidian_link, origin, owner_key, paths, sign, verify


class Unconfigured(ValueError):
    pass


def settings(owner):
    if os.getenv('CMOS_EVENT_MEMORY_ENABLED', '').lower() != 'true':
        raise Unconfigured('Event Memory storage is not configured. Files will not be saved on the VPS.')
    if owner.casefold() != os.getenv('EVENT_MEMORY_OWNER', '').casefold():
        raise Unconfigured('A private Event Memory vault is not configured for this account.')
    try:
        return dict(key=key_bytes(os.environ['EVENT_MEMORY_KEY']),
                    bridge=origin(os.environ['EVENT_MEMORY_BRIDGE_ORIGIN']),
                    app_origin=origin(os.environ['CMOS_PUBLIC_ORIGIN']),
                    vault=os.environ['EVENT_MEMORY_VAULT_NAME'])
    except (ValueError, KeyError) as exc:
        raise Unconfigured('Event Memory needs a private NAS connection and vault configuration.') from exc


def source(owner, kind, item_id):
    try:
        item_id = UUID(str(item_id))
    except (ValueError, TypeError):
        raise HTTPException(400, 'Choose a valid event')
    if kind == 'EVENT':
        row = query_one('SELECT id,title FROM operational_events WHERE id=%s', (item_id,))
    elif kind == 'CALENDAR':
        row = query_one('SELECT id,title FROM workspace_calendar_events WHERE id=%s AND owner_username=%s',
                        (item_id, owner))
    else:
        raise HTTPException(400, 'Choose an event or your calendar appointment')
    if not row:
        raise HTTPException(404, 'Event is unavailable to this account')
    return row


def json_response(data, status=200):
    return JSONResponse(data, status_code=status, headers={'Cache-Control': 'no-store'})


def record_data(row):
    return row['descriptor']


def display_material(row):
    data = record_data(row)
    return dict(id=str(row['id']), filename=data['filename'], state=row['state'],
                bytes=data['bytes'], sha256=data['sha256'], title=data['event_title'],
                source_kind=data['source_kind'], source_id=data['source_id'],
                obsidian_url=obsidian_link(row['vault_name'], row['note_path']) if row['state'] == 'READY' else None)


@app.get('/event-memory')
def event_memory_page(request: Request, kind: str = '', id: str = '', q: str = ''):
    owner = _owner(request)
    item = source(owner, kind, id) if kind or id else None
    try:
        cfg = settings(owner)
        enabled, reason = True, 'Configured for direct NAS uploads; this is not a live NAS or sync-health check.'
    except Unconfigured as exc:
        cfg, enabled, reason = None, False, str(exc)
    records = []
    choices = []
    if enabled:
        if item:
            rows = query_all('''SELECT * FROM event_memory_materials
                WHERE owner_username=%s AND source_kind=%s AND source_id=%s
                ORDER BY created_at DESC LIMIT 100''', (owner, kind, item['id']))
            records = [display_material(row) for row in rows]
        else:
            term = q.strip()[:200]
            choices = query_all('''SELECT * FROM (
                SELECT 'EVENT' AS kind,id,title,starts_at FROM operational_events
                UNION ALL
                SELECT 'CALENDAR',id,title,starts_at FROM workspace_calendar_events WHERE owner_username=%s
                ) e WHERE %s='' OR position(lower(%s) in lower(title))>0
                ORDER BY starts_at DESC NULLS LAST,id LIMIT 100''', (owner, term, term))
    response = templates.TemplateResponse(request=request, name='event_memory.html', context={
        'page': 'Event Memory', 'item': item, 'kind': kind, 'records': records, 'choices': choices,
        'enabled': enabled, 'reason': reason, 'q': q[:200], 'csrf': _csrf(request),
        'can_write': request.state.cmos_account.role != 'READ_ONLY',
    })
    return response


async def body(request):
    # Do not call request.form(), UploadFile, or unbounded request.body().
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 12000:
            raise HTTPException(413, 'Event Memory accepts small metadata only, not files')
        raw.extend(chunk)
    try:
        values = json.loads(raw)
        if not isinstance(values, dict):
            raise ValueError('Not an object')
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Invalid metadata')
    return values


def prepare(owner, values):
    cfg = settings(owner)
    item = source(owner, values.get('source_kind'), values.get('source_id'))
    try:
        data = descriptor({**values, 'owner': owner_key(owner), 'event_title': item['title'],
                           'app_origin': cfg['app_origin']})
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, 'Choose a PDF, JPEG or PNG up to 5 MiB with a valid checksum and file name')
    with db_conn() as c:
        # The browser retains this UUID across retry; a collision cannot overwrite another source.
        c.execute('''INSERT INTO event_memory_materials(id,owner_username,source_kind,source_id,descriptor)
            VALUES(%s,%s,%s,%s,%s::jsonb) ON CONFLICT(id) DO NOTHING''',
                  (data['id'], owner, data['source_kind'], data['source_id'], json.dumps(data)))
        row = c.execute('SELECT * FROM event_memory_materials WHERE id=%s FOR UPDATE', (data['id'],)).fetchone()
        if row['owner_username'] != owner or row['descriptor'] != data:
            raise HTTPException(409, 'This upload identifier is already assigned; nothing was overwritten')
        return dict(material=display_material(row), upload_origin=cfg['bridge'],
                    ticket=sign(cfg['key'], 'upload', data))


def grant(owner, values):
    cfg = settings(owner)
    try:
        material_id = UUID(str(values.get('id')))
    except ValueError:
        raise HTTPException(400, 'Invalid material identifier')
    row = query_one('SELECT * FROM event_memory_materials WHERE id=%s AND owner_username=%s', (material_id, owner))
    if not row:
        raise HTTPException(404, 'Material is unavailable')
    purpose = values.get('purpose', 'upload')
    if purpose not in {'upload', 'download'} or (purpose == 'download' and row['state'] != 'READY'):
        raise HTTPException(409, 'This material is not confirmed ready to download')
    return dict(upload_origin=cfg['bridge'], ticket=sign(cfg['key'], purpose, record_data(row)),
                material=display_material(row))


def finalize(owner, values):
    cfg = settings(owner)
    try:
        receipt = verify(cfg['key'], 'stored', values.get('receipt'))
        data = descriptor(receipt['descriptor'])
        if receipt['vault'] != cfg['vault'] or receipt['note_path'] != paths(data)['note']:
            raise ValueError('Wrong vault or note')
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, 'The NAS storage receipt is invalid or expired')
    if not hmac.compare_digest(data['owner'], owner_key(owner)):
        raise HTTPException(403, 'This receipt belongs to another private account')
    with db_conn() as c:
        row = c.execute('SELECT * FROM event_memory_materials WHERE id=%s AND owner_username=%s FOR UPDATE',
                        (data['id'], owner)).fetchone()
        if not row or row['descriptor'] != data:
            raise HTTPException(409, 'Storage receipt does not match the prepared material')
        if row['state'] == 'READY':
            return dict(material=display_material(row), message='Already linked. No duplicate created.')
        query = urlencode({'kind': data['source_kind'], 'id': data['source_id']})
        link = cfg['app_origin'] + '/event-memory?' + query
        text = ('Event Memory: ' + data['event_title'] + '\nMaterial: ' + data['filename']
                + '\nOriginal stored off-VPS in the private NAS vault.\n' + link)
        note = c.execute('''INSERT INTO brain_notes(owner_username,body,kind,tags,source,source_id)
            VALUES(%s,%s,'LINK',ARRAY['event-memory'],'WEB',%s)
            ON CONFLICT(source,source_id) DO UPDATE SET source_id=EXCLUDED.source_id RETURNING id''',
                         (owner, text, 'EVENT_MEMORY:' + data['id'])).fetchone()
        # Link to the event and retain a calendar source when its working snapshot rolls forward.
        c.execute('''INSERT INTO workspace_context_links(owner_username,source_kind,source_id,target_kind,target_id)
            VALUES(%s,'BRAIN',%s,%s,%s) ON CONFLICT DO NOTHING''',
                  (owner, note['id'], data['source_kind'], data['source_id']))
        row = c.execute('''UPDATE event_memory_materials SET state='READY',vault_name=%s,note_path=%s,
            brain_note_id=%s,stored_at=now() WHERE id=%s RETURNING *''',
                        (receipt['vault'], receipt['note_path'], note['id'], data['id'])).fetchone()
        return dict(material=display_material(row), message='Stored on NAS and linked to the event and Brain. Obsidian Sync is separate.')


@app.post('/event-memory/api/{operation}')
async def event_memory_action(request: Request, operation: str):
    values = await body(request)
    owner = _write(request, str(values.get('csrf', '')))
    handlers = {'prepare': prepare, 'grant': grant, 'finalize': finalize}
    if operation not in handlers:
        raise HTTPException(404, 'Unknown Event Memory action')
    try:
        result = await run_in_threadpool(handlers[operation], owner, values)
    except Unconfigured as exc:
        raise HTTPException(503, str(exc))
    return json_response(result)


def install_security(application):
    """Install AFTER private_auth, to permit only the configured bridge on this one page."""
    @application.middleware('http')
    async def event_memory_security(request, call_next):
        response = await call_next(request)
        if request.url.path == '/event-memory' and response.status_code == 200:
            try:
                cfg = settings(_owner(request))
                csp = response.headers.get('Content-Security-Policy', '')
                response.headers['Content-Security-Policy'] = csp.replace(
                    "connect-src 'self';", "connect-src 'self' " + cfg['bridge'] + ';')
            except (Unconfigured, HTTPException):
                pass
        return response
