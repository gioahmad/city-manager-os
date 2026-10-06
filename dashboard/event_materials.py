"""Private event-material references. Originals remain in the user's Google Drive."""
import re
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from fastapi import Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import app, db_conn, query_all, query_one
from brain_app import _owner, _write

KINDS = {'EVENT', 'CALENDAR'}
GOOGLE_HOSTS = {'drive.google.com', 'docs.google.com'}


def _uid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        raise HTTPException(400, 'Invalid event identifier') from None


def _source(owner, kind, source_id):
    kind = str(kind or '').strip().upper()
    source_id = _uid(source_id)
    if kind == 'EVENT':
        row = query_one('SELECT id,title FROM operational_events WHERE id=%s', (source_id,))
    elif kind == 'CALENDAR':
        row = query_one(
            'SELECT id,title FROM workspace_calendar_events WHERE id=%s AND owner_username=%s',
            (source_id, owner),
        )
    else:
        raise HTTPException(400, 'Event materials can be linked only to Events or your Microsoft calendar items')
    if not row:
        raise HTTPException(404, 'Event was not found or is not available to this account')
    return kind, source_id, row


def _drive_url(value):
    value = str(value or '').strip()
    if not 1 <= len(value) <= 3000:
        raise HTTPException(400, 'Paste a Google Drive sharing link')
    parsed = urlparse(value)
    host = (parsed.hostname or '').casefold()
    if parsed.scheme != 'https' or host not in GOOGLE_HOSTS or parsed.username or parsed.password:
        raise HTTPException(400, 'Use an HTTPS Google Drive or Google Docs link')
    if not parsed.path or parsed.path == '/':
        raise HTTPException(400, 'Paste a link to a specific Google Drive file or folder')
    return value


def _drive_id(value):
    parsed = urlparse(value)
    match = re.search(r'/d/([A-Za-z0-9_-]{10,})', parsed.path)
    if match:
        return match.group(1)[:500]
    values = parse_qs(parsed.query).get('id') or []
    return values[0][:500] if values and re.fullmatch(r'[A-Za-z0-9_-]{10,}', values[0]) else None


def materials(owner, kind, source_id):
    kind, source_id, _ = _source(owner, kind, source_id)
    return query_all(
        '''SELECT id,provider,external_id,title,url,notes,created_at,updated_at
           FROM workspace_event_materials
           WHERE owner_username=%s AND source_kind=%s AND source_id=%s
           ORDER BY created_at DESC,id DESC''',
        (owner, kind, source_id),
    )


@app.post('/event-materials/add')
def add_material(
    request: Request,
    source_kind: str = Form(...),
    source_id: str = Form(...),
    title: str = Form(...),
    url: str = Form(...),
    notes: str = Form(''),
    csrf: str = Form(...),
):
    owner = _write(request, csrf)
    kind, record_id, _ = _source(owner, source_kind, source_id)
    title = re.sub(r'\s+', ' ', title).strip()[:300]
    notes = notes.strip()[:3000]
    if not title:
        raise HTTPException(400, 'Name this material')
    url = _drive_url(url)
    with db_conn() as conn:
        conn.execute(
            '''INSERT INTO workspace_event_materials(
                 owner_username,source_kind,source_id,provider,external_id,title,url,notes)
               VALUES(%s,%s,%s,'GOOGLE_DRIVE',%s,%s,%s,%s)
               ON CONFLICT(owner_username,source_kind,source_id,url)
               DO UPDATE SET title=EXCLUDED.title,notes=EXCLUDED.notes,external_id=EXCLUDED.external_id,updated_at=now()''',
            (owner, kind, record_id, _drive_id(url), title, url, notes or None),
        )
    return RedirectResponse(
        f'/context/{kind}/{record_id}?msg=Google+Drive+material+linked',
        status_code=303,
    )


@app.post('/event-materials/{material_id}/delete')
def delete_material(request: Request, material_id: UUID, csrf: str = Form(...)):
    owner = _write(request, csrf)
    with db_conn() as conn:
        row = conn.execute(
            '''DELETE FROM workspace_event_materials
               WHERE id=%s AND owner_username=%s
               RETURNING source_kind,source_id''',
            (material_id, owner),
        ).fetchone()
    if not row:
        raise HTTPException(404, 'Event material not found')
    return RedirectResponse(
        f"/context/{row['source_kind']}/{row['source_id']}?msg=Material+link+removed",
        status_code=303,
    )
