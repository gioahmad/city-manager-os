"""Selected email photos reuse the existing private document store and links."""
import hashlib
import json
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app import query_all, query_one
from brain_app import _filename
from workspace_app import uid

MAX_FILE = 20 * 1024 * 1024
MAX_TOTAL = 50 * 1024 * 1024


def mail(owner, item_id):
    row = query_one('SELECT * FROM workspace_microsoft_mail WHERE id=%s AND owner_username=%s',
                    (uid(item_id), owner))
    if not row:
        raise HTTPException(404, 'This email is unavailable.')
    return row


def metadata(row):
    mime = str(row.get('contentType') or '').lower().split(';', 1)[0].strip()
    if str(row.get('@odata.type') or '').removeprefix('#') != 'microsoft.graph.fileAttachment' or not mime.startswith('image/'):
        return None
    return {'id': str(row['id']), 'filename': _filename(row.get('name')),
            'content_type': mime[:200], 'byte_size': int(row.get('size') or 0),
            'is_inline': row.get('isInline') is True, 'content_id': str(row.get('contentId') or '')[:2000]}


def saved(owner, item_id, account):
    return query_all("""SELECT id,filename,content_type,octet_length(content) AS byte_size,profile
        FROM workspace_documents WHERE owner_username=%s
          AND profile->'email_photo'->>'mail_id'=%s
          AND profile->'email_photo'->>'account_email'=%s""", (owner, str(item_id), account))


def photos(owner, item_id):
    import microsoft_workspace as ms
    source = mail(owner, item_id)
    with ms.graph(owner) as (client, token, account):
        require_read(account)
        path = 'me/messages/' + quote(source['provider_key'], safe='') + '/attachments?'
        # Do not use hasAttachments: Outlook excludes inline photos from that flag.
        path += '$select=id,name,contentType,size,isInline'
        rows = ms.microsoft.graph_pages(client, ms.microsoft.GRAPH + path, token, limit=500, mail=True)
        existing = {r['profile']['email_photo']['attachment_id']: r for r in saved(owner, item_id, account['account_email'])}
        result = []
        for row in rows:
            photo = metadata(row)
            if photo:
                photo['saved'] = photo['id'] in existing
                photo['selectable'] = 0 < photo['byte_size'] <= MAX_FILE
                result.append(photo)
        return {'photos': result, 'message': 'Choose photos to keep. None are selected automatically. Up to 10 photos, 20 MB each and 50 MB total.'}


def require_read(account):
    import workspace_calendar as calendar
    if not calendar.permissions(account['scopes']) & {'Mail.Read', 'Mail.ReadWrite'}:
        raise HTTPException(403, 'Enable Microsoft email access before saving its photos.')


def selections(value):
    if not isinstance(value, list) or len(value) > 10 or any(not isinstance(v, str) or not v or len(v) > 2000 for v in value):
        raise HTTPException(400, 'Choose at most 10 photos from this email.')
    if len(value) != len(set(value)):
        raise HTTPException(400, 'Choose each photo only once.')
    return value


def fetch(owner, item_id, selected):
    import microsoft_workspace as ms
    selected = selections(selected)
    if not selected:
        return []
    source = mail(owner, item_id)
    files, total = [], 0
    with ms.graph(owner) as (client, token, account):
        require_read(account)
        existing = {r['profile']['email_photo']['attachment_id']: r for r in saved(owner, item_id, account['account_email'])}
        for attachment_id in selected:
            if attachment_id in existing:
                row = existing[attachment_id]
                photo = {**row, **row['profile']['email_photo'], 'document_id': row['id'], 'content': None}
            else:
                path = 'me/messages/' + quote(source['provider_key'], safe='') + '/attachments/' + quote(attachment_id, safe='')
                row = ms.request_graph(client, 'GET', path + '?$select=id,name,contentType,size,isInline', token).json()
                photo = metadata(row)
                if not photo or photo['id'] != attachment_id:
                    raise HTTPException(400, 'Select image files attached to this email.')
                if not 0 < photo['byte_size'] <= MAX_FILE:
                    raise HTTPException(413, 'Each selected photo must be between 1 byte and 20 MB.')
                content = bytearray()
                headers = {'Authorization': 'Bearer ' + token, 'Prefer': 'IdType="ImmutableId"'}
                with client.stream('GET', ms.microsoft.GRAPH + path + '/$value', headers=headers) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        content.extend(chunk)
                        if len(content) > MAX_FILE or total + len(content) > MAX_TOTAL:
                            raise HTTPException(413, 'Save at most 20 MB per photo and 50 MB in one save.')
                if not content:
                    raise HTTPException(502, 'Microsoft returned an empty photo. Nothing was saved.')
                photo.update(content=bytes(content), byte_size=len(content), sha256=hashlib.sha256(content).hexdigest())
            total += photo['byte_size']
            if total > MAX_TOTAL:
                raise HTTPException(413, 'Save at most 50 MB of photos at a time.')
            photo.update(attachment_id=attachment_id, account_email=account['account_email'],
                         mail_id=str(source['id']), subject=source['title'], sender=source['sender_email'],
                         received_at=source['received_at'].isoformat())
            files.append(photo)
    return files


def store(connection, owner, source, files, target_kind, target_id):
    result = []
    for photo in files:
        document_id = photo.get('document_id')
        if not document_id:
            profile = {'type': 'image', 'note': 'Original email photo saved. Image text has not been extracted.',
                       'email_photo': {k: v for k, v in photo.items() if k not in {'content', 'document_id', 'id'}}}
            document_id = connection.execute("""INSERT INTO workspace_documents
                (owner_username,filename,content_type,content,status,extracted_text,profile)
                VALUES(%s,%s,%s,%s,'READY',%s,%s::jsonb) RETURNING id""",
                (owner, photo['filename'], photo['content_type'], photo['content'],
                 'Photo from email: ' + source['title'], json.dumps(profile))).fetchone()['id']
        for kind, item_id in [('MAIL', source['id']), (target_kind, target_id)]:
            connection.execute("""INSERT INTO workspace_context_links
                (owner_username,source_kind,source_id,target_kind,target_id)
                VALUES(%s,%s,%s,'DOCUMENT',%s) ON CONFLICT DO NOTHING""",
                (owner, kind, item_id, document_id))
        result.append({'id': str(document_id), 'filename': photo['filename'],
                       'url': '/workspace/documents/' + str(document_id) + '/download'})
    return result
