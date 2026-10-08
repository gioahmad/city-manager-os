"""Private Email / Calendar / Important workspace and explicitly reviewed Graph writes.

There is no background sender. A reviewed executable payload is encrypted;
only its owner can explicitly execute it. A durable RUNNING claim precedes the
network request. Ambiguous outcomes are never automatically replayed.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from cryptography.fernet import InvalidToken
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from app import app, db_conn, query_all, query_one, templates
from brain_app import _owner, _write, _csrf
import workspace_calendar as microsoft
import workspace_hub as hub
from workspace_app import config, uid

UTC = timezone.utc
OPS = {'MAIL_SEND', 'MAIL_DRAFT', 'CALENDAR_CREATE', 'CONTACT_UPDATE'}


def clean(value: Any, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ''
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise HTTPException(400, f'Text must be at most {limit:,} characters.')
    result = value.strip()
    if required and not result:
        raise HTTPException(400, 'Complete the required fields.')
    return result


def addresses(value: Any, *, required: bool = False) -> list[dict]:
    """Bare addresses only: reject display-name/header syntax, never guess recipients."""
    import re
    if isinstance(value, str):
        if len(value) > 6000 or '\r' in value or '\n' in value:
            raise HTTPException(400, 'Enter email addresses separated by commas.')
        values = [v.strip() for v in value.replace(';', ',').split(',') if v.strip()]
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        values = value
    else:
        raise HTTPException(400, 'Enter email addresses separated by commas.')
    unique = {}
    for address in values:
        address = address.strip()
        if len(address) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", address):
            raise HTTPException(400, 'Use a complete email address, without a display name.')
        local, domain = address.rsplit('@', 1)
        if '..' in address or local.startswith('.') or local.endswith('.') or any(p.startswith('-') or p.endswith('-') for p in domain.split('.')):
            raise HTTPException(400, 'Check the email address spelling.')
        unique.setdefault(address.casefold(), {'emailAddress': {'address': address}})
    if len(unique) > 50 or (required and not unique):
        raise HTTPException(400, 'Choose between 1 and 50 recipients.')
    return list(unique.values())


def timestamp(value: Any, zone: str) -> datetime:
    """Interpret wall time in the selected zone; reject DST gaps and ambiguous times."""
    try:
        dt = datetime.fromisoformat(clean(value, 60, required=True).replace('Z', '+00:00'))
        tz = ZoneInfo(zone)
        if dt.tzinfo is None:
            a, b = dt.replace(tzinfo=tz, fold=0), dt.replace(tzinfo=tz, fold=1)
            if a.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != dt:
                raise ValueError('gap')
            if a.utcoffset() != b.utcoffset():
                raise ValueError('ambiguous')
            dt = a
        return dt.astimezone(UTC)
    except (ValueError, ZoneInfoNotFoundError, OverflowError):
        raise HTTPException(400, 'Use valid dates and an IANA time zone. For a daylight-saving overlap, choose UTC and its exact time.') from None


def interval(values: dict) -> tuple[datetime, datetime, str]:
    zone = clean(values.get('timezone') or config()['timezone'], 100, required=True)
    start, end = timestamp(values.get('starts_at'), zone), timestamp(values.get('ends_at'), zone)
    if end <= start or end - start > timedelta(days=366):
        raise HTTPException(400, 'End must follow start, by no more than one year.')
    return start, end, zone


def digest(values: dict) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def capabilities(scopes: str) -> dict[str, bool]:
    granted = microsoft.permissions(scopes)
    return {'mail_read': bool(granted & {'Mail.Read', 'Mail.ReadWrite'}),
            'mail_send': 'Mail.Send' in granted, 'mail_draft': 'Mail.ReadWrite' in granted,
            'calendar_write': 'Calendars.ReadWrite' in granted, 'contacts_write': 'Contacts.ReadWrite' in granted}


def require(scopes: str, operation: str) -> None:
    field = {'MAIL_SEND': 'mail_send', 'MAIL_DRAFT': 'mail_draft', 'CALENDAR_CREATE': 'calendar_write', 'CONTACT_UPDATE': 'contacts_write'}[operation]
    if not capabilities(scopes)[field]:
        raise HTTPException(403, 'Approve the additional Microsoft permissions using Enable Microsoft actions. Existing read access remains available.')


def request_graph(client: httpx.Client, method: str, path: str, token: str, *, payload=None, if_match=None) -> httpx.Response:
    # Paths are built in this module, never accepted as a complete user-supplied URL.
    if not path.startswith('me/') or '..' in path.split('?')[0].split('/') or '://' in path:
        raise ValueError('Invalid Microsoft endpoint')
    response = client.request(method, microsoft.GRAPH + path,
        headers={'Authorization': 'Bearer ' + token, 'Prefer': ('IdType="ImmutableId"' if path.startswith(('me/messages','me/sendMail')) else 'outlook.timezone="UTC"'), **({'If-Match': if_match} if if_match else {})},
        **({'json': payload} if payload is not None else {}))
    response.raise_for_status()
    return response


def access(owner: str, client: httpx.Client, operation: str | None = None) -> tuple[str, dict]:
    """Serialize token rotation, preserve it before other I/O, and pin mailbox identity."""
    with db_conn() as c:
        row = c.execute('SELECT * FROM workspace_calendar_connections WHERE owner_username=%s FOR UPDATE', (owner,)).fetchone()
        if not row:
            raise HTTPException(409, 'Connect Microsoft 365 first. No Microsoft action was performed.')
        if operation:
            require(row['scopes'], operation)
        try:
            saved = json.loads(microsoft.cipher().decrypt(row['tokens'].encode()))
            if saved['owner'] != owner:
                raise ValueError('Owner mismatch')
            tokens = microsoft.token_request(client, {'grant_type': 'refresh_token', 'refresh_token': saved['refresh'], 'scope': row['scopes']})
            if operation and tokens.get('scope'):
                require(tokens['scope'], operation)
            saved['refresh'] = tokens.get('refresh_token') or saved['refresh']
            c.execute('UPDATE workspace_calendar_connections SET tokens=%s WHERE owner_username=%s',
                (microsoft.cipher().encrypt(json.dumps(saved).encode()).decode(), owner))
            c.commit()  # A later Graph failure must not discard a rotated refresh token.
            primary = request_graph(client, 'GET', 'me/calendar?$select=id,name,owner,canEdit', tokens['access_token']).json()
            email = clean((primary.get('owner') or {}).get('address'), 320, required=True).casefold()
            if row.get('account_email') and row['account_email'].casefold() != email:
                raise HTTPException(409, 'Microsoft account changed. Reconnect the original account before using its imported records.')
            c.execute('UPDATE workspace_calendar_connections SET account_email=%s WHERE owner_username=%s', (email, owner))
            return tokens['access_token'], {**row, 'account_email': email, 'primary_id': primary['id']}
        except (httpx.HTTPError, InvalidToken, ValueError, KeyError, TypeError):
            raise HTTPException(502, 'Microsoft authentication or account verification failed. Reconnect in Settings; no write was sent.') from None


@contextmanager
def graph(owner: str, operation: str | None = None):
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        token, account = access(owner, client, operation)
        yield client, token, account


def connection_identity(owner: str, client: httpx.Client, token: str) -> str:
    """Used by OAuth callback: changing accounts must not merge two mailboxes' records."""
    incoming = request_graph(client, 'GET', 'me/calendar?$select=owner', token).json()
    email = clean((incoming.get('owner') or {}).get('address'), 320, required=True).casefold()
    previous = query_one('SELECT account_email FROM workspace_calendar_connections WHERE owner_username=%s', (owner,))
    if previous:
        expected = previous.get('account_email')
        if not expected:
            _, verified = access(owner, client)
            expected = verified['account_email']
        if expected.casefold() != email:
            raise HTTPException(409, 'Choose the previously connected Microsoft account, or explicitly disconnect before changing accounts.')
    return email


def _page(request: Request, section: str):
    owner = _owner(request)
    return templates.TemplateResponse(request=request, name='microsoft_workspace.html', context={
        'page': {'email': 'Email', 'calendar': 'Calendar', 'important': 'Important'}[section],
        'section': section, 'csrf': _csrf(request), 'username': owner,
        'readonly': request.state.cmos_role == 'READ_ONLY', 'timezone': config()['timezone']})


@app.get('/email', response_class=HTMLResponse)
def email_page(request: Request):
    return _page(request, 'email')


@app.get('/calendar', response_class=HTMLResponse)
def calendar_page(request: Request):
    return _page(request, 'calendar')


@app.get('/important', response_class=HTMLResponse)
def important_page(request: Request):
    return _page(request, 'important')


@app.get('/workspace/api/microsoft/status')
def status_page(request: Request):
    owner = _owner(request)
    status = microsoft.status(owner)
    row = query_one('SELECT scopes,account_email FROM workspace_calendar_connections WHERE owner_username=%s', (owner,))
    status.update(capabilities((row or {}).get('scopes', '')))
    status['account_email'] = (row or {}).get('account_email')
    status['access_complete'] = bool(
        status.get('connected') and status.get('mail_read') and status.get('contacts_enabled')
        and status.get('mail_send') and status.get('mail_draft') and status.get('calendar_write') and status.get('contacts_write')
    )
    cfg = microsoft.settings()
    origin = str(cfg.get('redirect') or '').removesuffix('/workspace/calendar/microsoft/callback')
    status['setup'] = {
        'public_https': bool(origin.startswith('https://')),
        'client_id': bool(cfg.get('client')),
        'client_secret': bool(cfg.get('secret')),
        'encryption_key': bool(cfg.get('key')),
        'tenant': bool(cfg.get('tenant')),
        'redirect_uri': cfg.get('redirect') if origin.startswith('https://') else '',
    }
    status['connection_state'] = (
        'SERVER_SETUP_REQUIRED' if not status.get('ready') else
        'NOT_CONNECTED' if not status.get('connected') else
        'SYNC_NEEDS_ATTENTION' if status.get('sync_error') else
        'FULL' if status['access_complete'] else
        'PERMISSIONS_INCOMPLETE'
    )
    status['requested_permissions'] = [
        'Mail.Read', 'Contacts.Read', 'Calendars.ReadBasic',
        'Mail.Send', 'Mail.ReadWrite', 'Calendars.ReadWrite', 'Contacts.ReadWrite',
    ]
    status['calendars'] = query_all('SELECT calendar_key,name,can_edit,last_sync_at FROM workspace_microsoft_calendars WHERE owner_username=%s ORDER BY name', (owner,))
    status['operations'] = query_all('''SELECT id,operation,status,account_email,created_at,updated_at,result
        FROM workspace_microsoft_operations WHERE owner_username=%s AND status<>'REVIEW'
        ORDER BY created_at DESC LIMIT 20''', (owner,))
    return hub.reply(status)


async def values(request: Request) -> tuple[str, dict]:
    owner = _owner(request, write=True)
    raw = await request.body()
    if len(raw) > 65000:
        raise HTTPException(413, 'Request is too large.')
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, 'Invalid request.') from None
    _write(request, str(data.get('csrf', '')))
    return owner, {k: v for k, v in data.items() if k != 'csrf'}


@app.post('/workspace/api/microsoft/connect')
async def connect(request: Request):
    owner, data = await values(request)
    enable_write = data.get('enable_write') is True
    if enable_write and data.get('consent_reviewed') is not True:
        raise HTTPException(400, 'Review the Microsoft permission request first.')
    return hub.reply(await run_in_threadpool(microsoft.begin, owner, request, enable_write=enable_write))


@app.post('/workspace/api/microsoft/refresh')
async def refresh(request: Request):
    owner, _ = await values(request)
    return hub.reply(await run_in_threadpool(microsoft.sync, owner))


@app.get('/workspace/api/microsoft/items')
def items_page(request: Request, section: str = 'email', q: str = '', offset: int = 0):
    owner = _owner(request)
    if section == 'email':
        q = clean(q, 500)
        pattern = '%' + q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
        rows = query_all("""SELECT 'MAIL'::text AS kind,id,title,left(body,250) AS snippet,
            'PRIVATE'::text AS visibility,received_at AS updated_at,NOT is_read AS attention,
            CASE WHEN is_read THEN 'Read' ELSE 'Unread' END AS status
            FROM workspace_microsoft_mail WHERE owner_username=%s
              AND (title ILIKE %s OR body ILIKE %s OR sender_name ILIKE %s OR sender_email ILIKE %s)
            ORDER BY received_at DESC,id LIMIT 61 OFFSET %s""",
            (owner,pattern,pattern,pattern,pattern,max(0,min(offset,10000))))
        return hub.reply({'items':rows[:60], 'has_more':len(rows)>60})
    if section != 'important':
        raise HTTPException(400, 'Choose Email or Important.')
    search = clean(q, 500).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    rows = query_all(hub.SOURCES + '''SELECT i.kind,i.id,i.title,left(i.body,250) AS snippet,
        i.visibility,i.updated_at,i.attention,i.status,i.route
        FROM items i JOIN workspace_important f ON f.owner_username=%s AND f.kind=i.kind AND f.item_id=i.id
        WHERE (i.title ILIKE %s OR i.body ILIKE %s)
        ORDER BY f.created_at DESC,i.kind,i.id LIMIT 61 OFFSET %s''',
        (owner, owner, '%' + search + '%', '%' + search + '%', max(0, min(offset, 10000))))
    return hub.reply({'items': rows[:60], 'has_more': len(rows) > 60})


@app.get('/workspace/api/microsoft/flags/{kind}/{item_id}')
def flag_status(request: Request, kind: str, item_id: str):
    owner = _owner(request)
    hub.find(owner, kind, item_id)
    row = query_one('SELECT item_id FROM workspace_important WHERE owner_username=%s AND kind=%s AND item_id=%s', (owner, kind, uid(item_id)))
    return {'important': bool(row)}


@app.post('/workspace/api/microsoft/important')
async def mark_important(request: Request):
    owner, data = await values(request)
    def mark():
        item = hub.find(owner, data.get('kind'), data.get('id'))
        if not isinstance(data.get('important'), bool):
            raise HTTPException(400, 'Choose whether to mark this important.')
        with db_conn() as c:
            if data['important']:
                c.execute('INSERT INTO workspace_important(owner_username,kind,item_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (owner, item['kind'], item['id']))
            else:
                c.execute('DELETE FROM workspace_important WHERE owner_username=%s AND kind=%s AND item_id=%s', (owner, item['kind'], item['id']))
        return {'message': 'Marked important. Only you can see this flag.' if data['important'] else 'Important flag removed.', 'important': data['important']}
    return hub.reply(await run_in_threadpool(mark))


def saved_url(kind: str, item_id: str) -> str:
    if kind == 'WORK':
        return '/issues?' + urlencode({'focus': item_id, 'state': 'all'})
    if kind == 'EVENT':
        return '/schedule?' + urlencode({'focus': item_id, 'state': 'all'})
    return '/context/' + quote(kind, safe='') + '/' + quote(str(item_id), safe='')


def capture(owner: str, data: dict) -> dict:
    import microsoft_mail_photos as mail_photos
    selected_photos = mail_photos.selections(data.get('photos', []))
    if selected_photos and data.get('kind') != 'MAIL':
        raise HTTPException(400, 'Photos must come from a saved email.')
    request_id = uid(data.get('request_id'))
    target = data.get('destination')
    if target not in {'TASK', 'WORK', 'EVENT', 'BRAIN', 'LINK'}:
        raise HTTPException(400, 'Choose a destination.')
    if target in {'WORK', 'EVENT'} and data.get('share_confirmed') is not True:
        raise HTTPException(400, 'Confirm that the reviewed content may be copied into shared work records.')
    canonical = {k: v for k, v in data.items() if k != 'request_id'}
    fingerprint = digest(canonical)
    with db_conn() as c:
        # Protect retries and two simultaneous clicks, including across application workers.
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('capture:' + owner + ':' + str(request_id),))
        old = c.execute('SELECT fingerprint,result FROM workspace_capture_receipts WHERE owner_username=%s AND request_id=%s', (owner, request_id)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise HTTPException(409, 'This save ID already belongs to different content. Start a new save.')
            return old['result']
        item = hub.find(owner, data.get('kind'), data.get('id'), connection=c)
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('source:' + owner + ':' + item['kind'] + ':' + str(item['id']),))
        title = clean(data.get('title') or item['title'], 500, required=True)
        body = clean(data.get('body') if 'body' in data else item['body'], 20000)
        props = {'action': target, 'kind': item['kind'], 'id': str(item['id']), 'title': title, 'description': body, 'body': body}
        if target == 'TASK':
            due = clean(data.get('due_date'), 10)
            if due:
                try:
                    date.fromisoformat(due)
                except ValueError:
                    raise HTTPException(400, 'Use a valid due date.') from None
            props['due_date'] = due
        if target == 'WORK':
            try:
                priority = int(data.get('priority', 3))
                if priority not in range(1, 6):
                    raise ValueError()
            except (ValueError, TypeError):
                raise HTTPException(400, 'Priority must be between 1 and 5.') from None
            props.update(priority=priority, item_type='TASK', next_action=clean(data.get('next_action') or 'Review and follow up.', 1000),
                         assigned_to=clean(data.get('assigned_to'), 200))
            if data.get('due_date'):
                props['due_at'] = timestamp(clean(data['due_date'], 10) + 'T17:00:00', config()['timezone']).isoformat()
        if target == 'EVENT':
            start, end, _ = interval(data)
            props.update(starts_at=start.isoformat(), ends_at=end.isoformat(), location=clean(data.get('location'), 500))
        if target == 'LINK':
            props.update(target_kind=data.get('target_kind'), target_id=data.get('target_id'))
        try:
            files = mail_photos.fetch(owner, item['id'], selected_photos)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(502, 'Could not retrieve the selected email photos. Nothing was saved; try again or deselect the photos.') from None
        result = hub.action(owner, props, connection=c)
        result_kind = data.get('target_kind') if target == 'LINK' else target
        result_id = data.get('target_id') if target == 'LINK' else result.get('id')
        result = {**result, 'kind': result_kind, 'id': str(result_id), 'url': saved_url(result_kind, str(result_id))}
        if files:
            result['photos'] = mail_photos.store(c, owner, item, files, result_kind, result_id)
            result['message'] += ' ' + str(len(files)) + ' selected photo(s) saved privately.'
        c.execute('INSERT INTO workspace_capture_receipts(owner_username,request_id,fingerprint,result) VALUES(%s,%s,%s,%s::jsonb)', (owner, request_id, fingerprint, json.dumps(result, default=str)))
        return result


@app.post('/workspace/api/microsoft/capture')
async def capture_page(request: Request):
    owner, data = await values(request)
    if data.get('destination') in {'BRAIN', 'EVENT'}:
        from workspace_modules import require_enabled
        require_enabled(request, 'brain' if data['destination'] == 'BRAIN' else 'events')
    return hub.reply(await run_in_threadpool(capture, owner, data))


@app.get('/workspace/api/microsoft/mail/{item_id}/photos')
def email_photos(request: Request, item_id: str):
    import microsoft_mail_photos as mail_photos
    try:
        return hub.reply(mail_photos.photos(_owner(request), item_id))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(502, 'Could not load email photos from Microsoft. You can save the email text and try the photos again later.') from None


def calendars(client, token: str, account: dict, owner: str) -> list[dict]:
    rows = microsoft.graph_pages(client, microsoft.GRAPH + 'me/calendars?$select=id,name,canEdit,owner&$top=100', token, limit=200)
    entries = []
    for row in rows:
        # This release handles the signed-in mailbox's calendars, not delegated/shared mailbox writes.
        address = str((row.get('owner') or {}).get('address') or '').casefold()
        if address != account['account_email']:
            continue
        entries.append({'calendar_key': 'primary' if row['id'] == account['primary_id'] else row['id'],
                        'name': clean(row.get('name') or 'Calendar', 500), 'can_edit': row.get('canEdit') is True})
    with db_conn() as c:
        c.execute('DELETE FROM workspace_microsoft_calendars WHERE owner_username=%s', (owner,))
        for entry in entries:
            c.execute('''INSERT INTO workspace_microsoft_calendars(owner_username,calendar_key,name,can_edit) VALUES(%s,%s,%s,%s)
                ON CONFLICT(owner_username,calendar_key) DO UPDATE SET name=EXCLUDED.name,can_edit=EXCLUDED.can_edit''',
                      (owner, entry['calendar_key'], entry['name'], entry['can_edit']))
    return entries


def calendar_window(owner: str, key: str, start_text: str, end_text: str) -> dict:
    key = clean(key, 2000, required=True)
    start, end = timestamp(start_text, config()['timezone']), timestamp(end_text, config()['timezone'])
    if end <= start or end - start > timedelta(days=43):
        raise HTTPException(400, 'Choose a calendar window of at most 42 days.')
    try:
        with graph(owner) as (client, token, account):
            choices = calendars(client, token, account, owner)
            chosen = next((v for v in choices if v['calendar_key'] == key), None)
            if not chosen:
                raise HTTPException(404, 'That calendar is not available in your connected mailbox.')
            # Use the legacy event ID format so current source links retain their local UUIDs.
            path = 'me/calendarView' if key == 'primary' else 'me/calendars/' + quote(key, safe='') + '/calendarView'
            url = microsoft.GRAPH + path + '?' + urlencode({'startDateTime': start.isoformat(), 'endDateTime': end.isoformat(),
                '$top': 100, '$select': 'id,subject,start,end,location,isAllDay,isCancelled,webLink'})
            events = [row for event in microsoft.graph_pages(client, url, token, limit=1000) if (row := microsoft.event_row(owner, event))]
        result = []
        with db_conn() as c:
            for row in events:
                stored = c.execute('''INSERT INTO workspace_calendar_events(owner_username,event_key,title,starts_at,ends_at,location,all_day,outlook_url,calendar_key,calendar_name)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(owner_username,event_key) DO UPDATE SET title=EXCLUDED.title,starts_at=EXCLUDED.starts_at,
                    ends_at=EXCLUDED.ends_at,location=EXCLUDED.location,all_day=EXCLUDED.all_day,outlook_url=EXCLUDED.outlook_url,
                    calendar_key=EXCLUDED.calendar_key,calendar_name=EXCLUDED.calendar_name RETURNING *''', (*row, key, chosen['name'])).fetchone()
                result.append(event_item(stored))
        return {'items': result, 'calendars': choices, 'fresh': True, 'refreshed_at': datetime.now(UTC), 'message': 'Calendar range refreshed from Microsoft.'}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, HTTPException) as exc:
        if isinstance(exc, HTTPException) and exc.status_code in {400, 404}:
            raise
        rows = query_all('''SELECT * FROM workspace_calendar_events WHERE owner_username=%s AND calendar_key=%s
            AND starts_at<%s AND ends_at>%s ORDER BY starts_at,id LIMIT 1001''', (owner, key, end, start))
        return {'items': [event_item(r) for r in rows[:1000]], 'fresh': False,
                'message': 'Microsoft is unavailable or not connected. Showing retained local appointments, which may be incomplete or out of date. Refresh or check the connection.'}


def event_item(row: dict) -> dict:
    return {'kind': 'CALENDAR', 'id': row['id'], 'title': row['title'], 'snippet': row.get('location') or '',
            'updated_at': row['starts_at'], 'starts_at': row['starts_at'], 'ends_at': row['ends_at'],
            'all_day': row['all_day'], 'visibility': 'PRIVATE', 'status': 'Appointment',
            'calendar_key': row.get('calendar_key', 'primary'), 'calendar_name': row.get('calendar_name', 'Primary calendar')}


@app.get('/workspace/api/microsoft/calendar')
def calendar_data(request: Request, calendar_key: str = 'primary', start: str = '', end: str = ''):
    return hub.reply(calendar_window(_owner(request), calendar_key, start, end))


def prepare(owner: str, data: dict) -> dict:
    if data.get('operation') == 'CONTACT_UPDATE':
        from microsoft_contacts import prepare_contact
        return prepare_contact(owner, data)
    operation = data.get('operation')
    if operation not in OPS:
        raise HTTPException(400, 'Choose a Microsoft action.')
    request_id = uid(data.get('request_id'))
    fingerprint = digest({k: v for k, v in data.items() if k != 'request_id'})
    old = query_one('SELECT * FROM workspace_microsoft_operations WHERE owner_username=%s AND request_id=%s', (owner, request_id))
    if old:
        if old['fingerprint'] != fingerprint:
            raise HTTPException(409, 'This request ID already has different content. Start a new review.')
        return public_operation(old)
    item = None
    if data.get('source_kind') or data.get('source_id'):
        item = hub.find(owner, data.get('source_kind'), data.get('source_id'))
    title, body = clean(data.get('title'), 500, required=True), clean(data.get('body'), 20000)
    payload: dict = {}
    review = {'title': title, 'body': body, 'operation': operation, 'source': item['title'] if item else None}
    with graph(owner, operation) as (client, token, account):
        review['account_email'] = account['account_email']
        if operation.startswith('MAIL_'):
            mode = data.get('mode', 'new')
            if mode not in {'new', 'reply', 'forward'}:
                raise HTTPException(400, 'Choose New, Reply or Forward text.')
            recipients = addresses(data.get('to', ''), required=mode != 'reply')
            cc, bcc = addresses(data.get('cc', '')), addresses(data.get('bcc', ''))
            message = {'subject': title, 'body': {'contentType': 'Text', 'content': body},
                       'toRecipients': recipients, 'ccRecipients': cc, 'bccRecipients': bcc}
            if mode == 'reply':
                if not item or item['kind'] != 'MAIL':
                    raise HTTPException(400, 'Select the email to reply to.')
                source = query_one('SELECT provider_key FROM workspace_microsoft_mail WHERE owner_username=%s AND id=%s', (owner, item['id']))
                if not source:
                    raise HTTPException(404, 'The source email is unavailable.')
                path = 'me/messages/' + quote(source['provider_key'], safe='')
                original = request_graph(client, 'GET', path + '?$select=from,replyTo', token).json()
                reply_to = original.get('replyTo') or [original.get('from') or {}]
                message['toRecipients'] = addresses([(r.get('emailAddress') or {}).get('address', '') for r in reply_to], required=True)
                path += '/reply' if operation == 'MAIL_SEND' else '/createReply'
                payload = {'path': path, 'json': {'message': message}}
            else:
                path = 'me/sendMail' if operation == 'MAIL_SEND' else 'me/messages'
                payload = {'path': path, 'json': {'message': message, 'saveToSentItems': True} if operation == 'MAIL_SEND' else message}
            if sum(len(message.get(key, [])) for key in ('toRecipients','ccRecipients','bccRecipients')) > 50:
                raise HTTPException(400, 'Use at most 50 recipients across To, CC and BCC.')
            review.update(mode=mode, to=[r['emailAddress']['address'] for r in message['toRecipients']],
                          cc=[r['emailAddress']['address'] for r in cc], bcc=[r['emailAddress']['address'] for r in bcc],
                          warning='Text only. Attachments are not included.' if mode != 'reply' else 'Replies to the Microsoft reply-to address shown above. No new attachments.')
        else:
            start, end, zone = interval(data)
            key = clean(data.get('calendar_key') or 'primary', 2000, required=True)
            chosen = next((v for v in calendars(client, token, account, owner) if v['calendar_key'] == key and v['can_edit']), None)
            if not chosen:
                raise HTTPException(403, 'Select an editable calendar owned by your connected Microsoft account.')
            recipients = addresses(data.get('attendees', ''))
            if recipients and data.get('send_invitations') is not True:
                raise HTTPException(400, 'Attendees will receive invitations. Explicitly choose Send invitations, or clear attendees.')
            if item and item['kind'] == 'CALENDAR':
                source = query_one('SELECT calendar_key,outlook_url FROM workspace_calendar_events WHERE owner_username=%s AND id=%s', (owner, item['id']))
                if source and source['calendar_key'] == key:
                    raise HTTPException(409, 'This appointment is already in that Microsoft calendar. Open the original instead of creating a duplicate.')
            event = {'subject': title, 'body': {'contentType': 'Text', 'content': body},
                     'start': {'dateTime': start.replace(tzinfo=None).isoformat(), 'timeZone': 'UTC'},
                     'end': {'dateTime': end.replace(tzinfo=None).isoformat(), 'timeZone': 'UTC'},
                     'location': {'displayName': clean(data.get('location'), 500)},
                     'transactionId': str(request_id), 'attendees': [{**r, 'type': 'required'} for r in recipients]}
            path = 'me/calendar/events' if key == 'primary' else 'me/calendars/' + quote(key, safe='') + '/events'
            payload = {'path': path, 'json': event, 'calendar_key': key}
            review.update(calendar=chosen['name'], calendar_key=key, starts_at=start.isoformat(), ends_at=end.isoformat(),
                          timezone=zone, location=event['location']['displayName'], attendees=[r['emailAddress']['address'] for r in recipients],
                          warning='Microsoft will send invitations to these attendees.' if recipients else 'Appointment only. No invitations will be sent.')
    encrypted = microsoft.cipher().encrypt(json.dumps(payload).encode()).decode()
    with db_conn() as c:
        row = c.execute('''INSERT INTO workspace_microsoft_operations(owner_username,request_id,fingerprint,operation,account_email,
            source_kind,source_id,payload,review) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT(owner_username,request_id) DO NOTHING RETURNING *''',
            (owner, request_id, fingerprint, operation, account['account_email'], item['kind'] if item else None,
             item['id'] if item else None, encrypted, json.dumps(review))).fetchone()
        if not row:
            row = c.execute('SELECT * FROM workspace_microsoft_operations WHERE owner_username=%s AND request_id=%s', (owner, request_id)).fetchone()
            if row['fingerprint'] != fingerprint:
                raise HTTPException(409, 'Request ID conflict. Start a new review.')
    return public_operation(row)


def public_operation(row: dict) -> dict:
    return {'id': str(row['id']), 'status': row['status'], 'review': row['review'], 'result': row['result'], 'expires_at': row['expires_at']}


@app.post('/workspace/api/microsoft/prepare')
async def prepare_page(request: Request):
    owner, data = await values(request)
    return hub.reply(await run_in_threadpool(prepare, owner, data))


@app.get('/workspace/api/microsoft/operations/{operation_id}')
def operation_page(request: Request, operation_id: str):
    row = query_one('SELECT * FROM workspace_microsoft_operations WHERE owner_username=%s AND id=%s', (_owner(request), uid(operation_id)))
    if not row:
        raise HTTPException(404, 'Operation not found.')
    return hub.reply(public_operation(row))


def execute_operation(owner: str, data: dict) -> dict:
    if data.get('confirmed') is not True:
        raise HTTPException(400, 'Review and explicitly confirm this exact action.')
    operation_id = uid(data.get('operation_id'))
    with db_conn() as c:
        row = c.execute('SELECT * FROM workspace_microsoft_operations WHERE owner_username=%s AND id=%s FOR UPDATE', (owner, operation_id)).fetchone()
        if not row:
            raise HTTPException(404, 'Operation not found.')
        if row['status'] != 'REVIEW':
            return public_operation(row)
        if row['expires_at'] <= datetime.now(UTC):
            raise HTTPException(409, 'This review expired. Prepare and review a new action.')
        if row['source_kind']:
            hub.find(owner, row['source_kind'], row['source_id'], connection=c)
        payload = json.loads(microsoft.cipher().decrypt(row['payload'].encode()))
        c.execute("UPDATE workspace_microsoft_operations SET status='RUNNING',updated_at=now() WHERE id=%s AND owner_username=%s", (operation_id, owner))
    # The claim is committed BEFORE the request. A crash stays RUNNING, never becomes a resend.
    status = 'FAILED'
    result = {'message': 'No Microsoft write was sent. Check the connection and prepare a new review.'}
    submitted = False
    try:
        with graph(owner, row['operation']) as (client, token, account):
            if account['account_email'] != row['account_email']:
                raise HTTPException(409, 'The connected mailbox changed after review. Prepare a new action.')
            if row['operation'] == 'CALENDAR_CREATE':
                allowed = calendars(client, token, account, owner)
                if not any(r['calendar_key'] == payload['calendar_key'] and r['can_edit'] for r in allowed):
                    raise HTTPException(403, 'The selected calendar is no longer editable.')
            if row['operation'] == 'CONTACT_UPDATE':
                from microsoft_contacts import verify_contact
                verify_contact(owner, payload, client, token)
            submitted = True
            response = request_graph(client, 'PATCH' if row['operation'] == 'CONTACT_UPDATE' else 'POST', payload['path'], token, payload=payload['json'], **({'if_match': payload['etag']} if payload.get('etag') else {}))
            expected = 200 if row['operation'] == 'CONTACT_UPDATE' else 202 if row['operation'] == 'MAIL_SEND' else 201
            if response.status_code != expected:
                raise ValueError('Unexpected response')
            status = 'SUCCEEDED'
            if row['operation'] == 'MAIL_SEND':
                result = {'message': 'Accepted by Microsoft for sending. Delivery is not yet confirmed; check Outlook Sent Items.', 'accepted': True}
            else:
                remote = response.json()
                if not remote.get('id'):
                    raise ValueError('Missing result ID')
                result = {'message': 'Saved to Outlook Drafts. Not sent.' if row['operation'] == 'MAIL_DRAFT' else 'Created in your Microsoft calendar.',
                          'provider_id': remote['id'], 'outlook_url': microsoft.safe_outlook_url(remote.get('webLink'))}
                if row['operation'] == 'CONTACT_UPDATE':
                    from microsoft_contacts import finish_contact
                    finish_contact(owner, payload, remote)
                    result = {'message': 'Linked Outlook contact updated.', 'provider_id': remote['id']}
    except httpx.HTTPStatusError as exc:
        # A definite 4xx is a rejection; timeout/5xx could have happened after acceptance.
        definite = 400 <= exc.response.status_code < 500 and exc.response.status_code != 408
        status = 'FAILED' if definite else 'UNKNOWN'
        result = {'message': 'Microsoft rejected the action. Nothing was automatically retried. Check permissions, recipients and availability.' if definite else 'Microsoft outcome is uncertain. Check Outlook before any new attempt. This operation will not be resent.'}
    except HTTPException as exc:
        status = 'FAILED' if not submitted else 'UNKNOWN'
        result = {'message': exc.detail if not submitted else 'Outcome uncertain. Check Outlook; do not resend until verified.'}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidToken):
        status = 'UNKNOWN' if submitted else 'FAILED'
        result = {'message': 'Outcome uncertain. Check Outlook Drafts, Sent Items or the selected calendar. No automatic retry.' if submitted else 'Connection verification failed before sending. Reconnect and prepare a new review.'}
    with db_conn() as c:
        row = c.execute('''UPDATE workspace_microsoft_operations SET status=%s,result=%s::jsonb,updated_at=now()
            WHERE owner_username=%s AND id=%s RETURNING *''', (status, json.dumps(result), owner, operation_id)).fetchone()
    return public_operation(row)


@app.post('/workspace/api/microsoft/execute')
async def execute_page(request: Request):
    owner, data = await values(request)
    return hub.reply(await run_in_threadpool(execute_operation, owner, data))


@app.post('/workspace/api/microsoft/cancel')
async def cancel_page(request: Request):
    owner, data = await values(request)
    def cancel():
        with db_conn() as c:
            row = c.execute("UPDATE workspace_microsoft_operations SET status='CANCELLED',updated_at=now() WHERE owner_username=%s AND id=%s AND status='REVIEW' RETURNING id", (owner, uid(data.get('operation_id')))).fetchone()
        if not row:
            raise HTTPException(409, 'Only an unsubmitted review can be cancelled.')
        return {'message': 'Review cancelled. Nothing sent to Microsoft.'}
    return hub.reply(await run_in_threadpool(cancel))


@app.get('/workspace/api/microsoft/calendars')
def calendar_choices(request: Request):
    owner = _owner(request)
    with graph(owner) as (client, token, account):
        return hub.reply({'calendars': calendars(client, token, account, owner)})
