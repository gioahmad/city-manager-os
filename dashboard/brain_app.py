"""Private capture and retrieval, independent of the alert and spatial engines."""
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import PurePath
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import app, db_conn, query_all, query_one, templates

KINDS = ('NOTE', 'IDEA', 'TASK', 'LINK')
MAX_ATTACHMENT = 20 * 1024 * 1024
EASTERN = ZoneInfo('America/New_York')


def _owner(request, write=False):
    account = getattr(request.state, 'cmos_account', None)
    if not account or account.username == 'automation':
        raise HTTPException(401, 'Sign in to use your private Brain.')
    if write and account.role == 'READ_ONLY':
        raise HTTPException(403, 'This account is read-only.')
    return account.username.casefold()


def _csrf(request):
    # Bind forms to the signed, HttpOnly login cookie. Other accounts cannot reuse them.
    from private_auth import COOKIE_NAME, _session_secret
    return hmac.new(_session_secret(), request.cookies.get(COOKIE_NAME, '').encode(), hashlib.sha256).hexdigest()


def _write(request, token):
    owner = _owner(request, True)
    if not hmac.compare_digest(_csrf(request), token):
        raise HTTPException(403, 'Refresh the page and try again.')
    return owner


def _values(body, kind='AUTO', tags=''):
    body = body.strip()
    if not 1 <= len(body) <= 20000:
        raise HTTPException(400, 'Enter a note of 1 to 20,000 characters.')
    if kind == 'AUTO':
        first = body.casefold()
        kind = ('IDEA' if first.startswith(('idea:', 'idea ', '#idea')) else
                'TASK' if first.startswith(('todo:', 'task:', 'remind me', 'follow up', '#task')) else
                'LINK' if re.search(r'https?://\S+', body) else 'NOTE')
    if kind not in KINDS:
        raise HTTPException(400, 'Choose a valid note type.')
    values = re.findall(r'(?<!\w)#([\w-]{1,50})', body)
    values += [x.strip().lstrip('#') for x in tags.split(',') if x.strip()]
    values = sorted({x.casefold() for x in values if x})
    if len(values) > 30 or any(len(x) > 50 for x in values):
        raise HTTPException(400, 'Use up to 30 tags, each under 51 characters.')
    return body, kind, values


def _filename(name):
    return re.sub(r'[\x00-\x1f\x7f]', '', PurePath((name or 'attachment').replace('\\', '/')).name)[:200] or 'attachment'


def _insert(owner, body, kind, tags, source='WEB', source_id=None, attachment=None):
    with db_conn() as conn:
        row = conn.execute('''INSERT INTO brain_notes(owner_username,body,kind,tags,source,source_id)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(source,source_id) DO NOTHING RETURNING id''',
            (owner, body, kind, tags, source, source_id)).fetchone()
        if row and attachment:
            conn.execute('INSERT INTO brain_attachments(note_id,filename,content) VALUES(%s,%s,%s)',
                         (row['id'], *attachment))
        return bool(row)


@app.get('/brain', response_class=HTMLResponse)
def brain_page(request: Request, q: str = '', kind: str = '', view: str = '', page: int = 1, msg: str = ''):
    owner = _owner(request)
    if kind and kind not in KINDS:
        raise HTTPException(400, 'Invalid note type.')
    q = q.strip()[:500]
    page = max(1, min(page, 10000))
    rows = query_all('''SELECT n.*,
        coalesce((SELECT jsonb_agg(jsonb_build_object('id',a.id,'filename',a.filename))
          FROM brain_attachments a WHERE a.note_id=n.id),'[]'::jsonb) AS attachments
        FROM brain_notes n WHERE owner_username=%s
        AND (deleted_at IS NOT NULL)=%s AND (%s='' OR kind=%s)
        AND (%s<> 'pinned' OR pinned)
        AND (%s='' OR to_tsvector('english',body) @@ websearch_to_tsquery('english',%s)
             OR body ILIKE %s ESCAPE '\\' OR array_to_string(tags,',') ILIKE %s ESCAPE '\\')
        ORDER BY pinned DESC,created_at DESC,id DESC LIMIT 51 OFFSET %s''',
        (owner, view == 'trash', kind, kind, view, q, q,
         '%' + re.sub(r'([\\%_])', r'\\\1', q) + '%',
         '%' + re.sub(r'([\\%_])', r'\\\1', q.lstrip('#')) + '%', (page - 1) * 50))
    more = len(rows) > 50
    for row in rows[:50]:
        row['local_time'] = row['created_at'].astimezone(EASTERN).strftime('%b %d, %Y · %I:%M %p')
        row['links'] = list(dict.fromkeys(re.findall(r'https?://[^\s<>"\']+', row['body'])))[:20]
    settings = query_one('SELECT owner_username,senders FROM brain_sms_settings WHERE singleton')
    return templates.TemplateResponse(request=request, name='brain.html', context={
        'rows': rows[:50], 'more': more, 'page': page, 'q': q, 'kind': kind,
        'view': view, 'kinds': KINDS, 'csrf': _csrf(request),
        'msg': msg[:500],
        'can_write': request.state.cmos_account.role != 'READ_ONLY',
        'executive': request.state.cmos_account.role == 'EXECUTIVE',
        'sms_ready': bool(settings),
        'sms_senders': ', '.join(settings.get('senders', [])) if settings.get('owner_username') == owner else '',
    })


@app.post('/brain/save')
async def brain_save(request: Request, body: str = Form(...), kind: str = Form('AUTO'),
                     tags: str = Form(''), csrf: str = Form(...), attachment: UploadFile | None = File(None)):
    owner = _write(request, csrf)
    values = _values(body, kind, tags)
    file = None
    if attachment and attachment.filename:
        content = await attachment.read(MAX_ATTACHMENT + 1)
        await attachment.close()
        if len(content) > MAX_ATTACHMENT:
            raise HTTPException(413, 'Attachments must be 20 MB or smaller.')
        if content:
            file = (_filename(attachment.filename), content)
    _insert(owner, *values, attachment=file)
    return RedirectResponse('/brain', 303)


@app.post('/brain/{note_id}/update')
def brain_update(request: Request, note_id: UUID, body: str = Form(...), kind: str = Form('AUTO'),
                 tags: str = Form(''), csrf: str = Form(...)):
    owner = _write(request, csrf)
    body, kind, tags = _values(body, kind, tags)
    with db_conn() as conn:
        row = conn.execute('''UPDATE brain_notes SET body=%s,kind=%s,tags=%s,updated_at=now()
            WHERE id=%s AND owner_username=%s AND deleted_at IS NULL RETURNING id''',
            (body, kind, tags, note_id, owner)).fetchone()
        if not row:
            raise HTTPException(404, 'Note not found.')
    return RedirectResponse('/brain', 303)


@app.post('/brain/{note_id}/action')
def brain_action(request: Request, note_id: UUID, action: str = Form(...), csrf: str = Form(...)):
    owner = _write(request, csrf)
    updates = {'pin': 'pinned=NOT pinned', 'trash': 'deleted_at=now()', 'restore': 'deleted_at=NULL'}
    if action not in updates:
        raise HTTPException(400, 'Invalid action.')
    with db_conn() as conn:
        row = conn.execute(f'UPDATE brain_notes SET {updates[action]},updated_at=now() WHERE id=%s AND owner_username=%s RETURNING id',
                           (note_id, owner)).fetchone()
        if not row:
            raise HTTPException(404, 'Note not found.')
    return RedirectResponse('/brain?view=trash' if action == 'restore' else '/brain', 303)


@app.get('/brain/files/{attachment_id}')
def brain_file(request: Request, attachment_id: UUID):
    owner = _owner(request)
    row = query_one('''SELECT a.filename,a.content FROM brain_attachments a
        JOIN brain_notes n ON n.id=a.note_id WHERE a.id=%s AND n.owner_username=%s''', (attachment_id, owner))
    if not row:
        raise HTTPException(404, 'Attachment not found.')
    from urllib.parse import quote
    return Response(bytes(row['content']), media_type='application/octet-stream', headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(_filename(row['filename']), safe=''),
        'X-Content-Type-Options': 'nosniff'})


@app.get('/brain/export')
def brain_export(request: Request):
    owner = _owner(request)
    rows = query_all('''SELECT id,body,kind,tags,source,pinned,deleted_at,created_at,updated_at
        FROM brain_notes WHERE owner_username=%s ORDER BY created_at''', (owner,))
    return Response(json.dumps(rows, default=str, ensure_ascii=False, indent=2), media_type='application/json',
                    headers={'Content-Disposition': 'attachment; filename="brain-notes.json"'})


def _phone(value):
    digits = re.sub(r'[^0-9]', '', value)
    if len(digits) == 10:
        digits = '1' + digits
    if not 8 <= len(digits) <= 15:
        raise HTTPException(400, 'Enter a valid phone number including country code.')
    return '+' + digits


@app.post('/brain/sms/settings')
def brain_sms_settings(request: Request, senders: str = Form(...), signing_key: str = Form(''), csrf: str = Form(...)):
    owner = _write(request, csrf)
    if request.state.cmos_account.role != 'EXECUTIVE':
        raise HTTPException(403, 'Executive access required.')
    numbers = sorted({_phone(x.strip()) for x in senders.split(',') if x.strip()})
    if not numbers or len(numbers) > 20:
        raise HTTPException(400, 'Enter between 1 and 20 allowed sender numbers.')
    with db_conn() as conn:
        old = conn.execute('SELECT signing_key FROM brain_sms_settings WHERE singleton').fetchone()
        key = signing_key.strip() or (old or {}).get('signing_key', '')
        if len(key) < 16 or len(key) > 500:
            raise HTTPException(400, 'Enter the signing key from the SMSGate device (16–500 characters).')
        conn.execute('''INSERT INTO brain_sms_settings(singleton,owner_username,senders,signing_key)
            VALUES(true,%s,%s,%s) ON CONFLICT(singleton) DO UPDATE
            SET owner_username=EXCLUDED.owner_username,senders=EXCLUDED.senders,signing_key=EXCLUDED.signing_key''',
            (owner, numbers, key))
    return RedirectResponse('/brain', 303)


def _verify_sms(raw, timestamp, signature, key):
    if not re.fullmatch(r'[0-9]{1,11}', timestamp):
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False
    digest = hmac.new(key.encode(), raw + timestamp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


@app.post('/brain/sms/connect')
def brain_sms_connect(request: Request, csrf: str = Form(...)):
    """Reuse saved gateway credentials and retain all existing destinations."""
    from urllib.parse import urlencode, urlsplit, urlunsplit
    from operations_app import _smsgate_settings
    from integration_runtime import apply_literal_auth, perform_http_request
    owner = _write(request, csrf)
    if request.state.cmos_account.role != 'EXECUTIVE':
        raise HTTPException(403, 'Executive access required.')
    capture = query_one('SELECT owner_username FROM brain_sms_settings WHERE singleton')
    if capture.get('owner_username') != owner:
        raise HTTPException(400, 'Save your SMS capture settings first.')
    origin = os.getenv('CMOS_PUBLIC_ORIGIN', '').strip().rstrip('/')
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise HTTPException(400, 'Set CMOS_PUBLIC_ORIGIN to your City Manager OS HTTPS origin first.')
    callback = origin + '/brain/webhooks/sms'
    settings = _smsgate_settings()
    gateway = urlsplit(settings['url'])
    if not settings['username'] or not settings['password'] or not re.search(r'/messages?/?$', gateway.path):
        raise HTTPException(400, 'Complete the existing SMSGate settings on the Share page first.')
    endpoint = urlunsplit((gateway.scheme, gateway.netloc, re.sub(r'/messages?/?$', '/webhooks', gateway.path), '', ''))
    headers = {'Accept':'application/json', 'Content-Type':'application/json', 'User-Agent':'CityManagerOS/1.0'}
    apply_literal_auth('BASIC', headers, {}, username=settings['username'], password=settings['password'])
    def call(method, body=None):
        result = perform_http_request(method=method,url=endpoint,headers=headers,body=body,
            timeout_seconds=20,max_response_bytes=100000,allow_redirects=False,allow_private=True)
        if not result.ok:
            raise HTTPException(502, 'SMSGate could not register the webhook. Check the gateway and its credentials on the Share page.')
        return result
    result = call('GET')
    try:
        existing = json.loads(result.body_text)
    except ValueError:
        raise HTTPException(502, 'SMSGate returned an unreadable webhook list.')
    if not isinstance(existing, list):
        raise HTTPException(502, 'SMSGate returned an unexpected webhook list.')
    if not any(isinstance(w,dict) and w.get('url')==callback and w.get('event')=='sms:received' for w in existing):
        call('POST', json.dumps({'url':callback,'event':'sms:received'}))
    return RedirectResponse('/brain?' + urlencode({'msg':'Brain webhook registered. Send a test text to your gateway SIM number and check the feed.'}),303)


@app.post('/brain/webhooks/sms')
async def brain_sms(request: Request):
    settings = query_one('SELECT * FROM brain_sms_settings WHERE singleton')
    if not settings:
        raise HTTPException(503, 'Brain SMS capture is not configured.')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 100000:
            raise HTTPException(413, 'Webhook body is too large.')
    if not _verify_sms(bytes(raw), request.headers.get('x-timestamp', ''), request.headers.get('x-signature', ''), settings['signing_key']):
        raise HTTPException(401, 'Invalid webhook signature.')
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Invalid JSON.')
    if not isinstance(event, dict):
        raise HTTPException(400, 'Invalid event.')
    if event.get('event') != 'sms:received':
        return {'ok': True, 'saved': False}
    payload = event.get('payload')
    if not isinstance(payload, dict):
        raise HTTPException(400, 'Missing SMS payload.')
    sender = payload.get('sender') or payload.get('phoneNumber')  # Older SMSGate versions.
    if not isinstance(sender, str) or _phone(sender) not in settings['senders']:
        raise HTTPException(403, 'Sender is not allowed.')
    event_id = event.get('id')
    message = payload.get('message')
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200 or not isinstance(message, str):
        raise HTTPException(400, 'Missing message or event ID.')
    # Retried events cannot create duplicate notes. No replies or automatic downstream actions.
    saved = _insert(settings['owner_username'], *_values(message), source='SMS', source_id=event_id)
    return {'ok': True, 'saved': saved}
