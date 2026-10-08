"""Selective Outlook contact import and reviewed updates to the linked source."""
import json
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

import httpx
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app import app, db_conn, query_all, query_one
from brain_app import _owner
import microsoft_workspace as ms
import workspace_calendar as calendar
from operations_app import _contact_emails, _contact_form_values, _contact_phones

FIELDS = ('name', 'organization', 'title', 'phones', 'emails', 'address')
ADDRESS_FIELDS = ('street', 'city', 'state', 'postalCode', 'countryOrRegion')


def local_values(row):
    return {key: row.get(key) or ([] if key in {'phones', 'emails'} else '') for key in FIELDS}


def source_values(remote):
    _, _, name, raw = calendar.contact_row('', remote)
    attrs = json.loads(raw)
    warnings = []
    for key, normalize in [('phones', _contact_phones), ('emails', _contact_emails)]:
        cleaned = []
        for value in attrs[key]:
            try:
                cleaned.extend(normalize(value))
            except ValueError:
                warnings.append(f'Review original {key}: {value}')
        attrs[key] = list(dict.fromkeys(cleaned))
    return local_values({'name': name, **attrs}), warnings


def contact_path(provider):
    return 'me/contacts/' + quote(ms.clean(provider, 2000, required=True), safe='')


def read_contact(client, token, provider):
    try:
        return ms.request_graph(client, 'GET', contact_path(provider), token).json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, 'That Outlook contact moved or was removed. Find it again in Outlook.') from None
        raise


def matches(owner, fields):
    return query_all('''SELECT id,name,organization,phones,emails FROM contacts
        WHERE active AND (visibility='ALL' OR owner_username=%s)
          AND (lower(name)=lower(%s) OR emails && %s::text[] OR phones && %s::text[])
        ORDER BY name,id LIMIT 20''', (owner, fields['name'], fields['emails'], fields['phones']))


def search_contacts(owner, q, cursor, live):
    q = ms.clean(q, 160)
    if not live:
        pattern = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        rows = query_all('''SELECT provider_key,name,attributes FROM workspace_microsoft_contacts
            WHERE owner_username=%s AND (name ILIKE %s OR attributes::text ILIKE %s)
            ORDER BY name,id LIMIT 100''', (owner, pattern, pattern))
        return {'items': rows, 'next_cursor': '', 'message': 'Retained Outlook snapshot, up to 500 contacts. Browse Outlook for contacts outside this snapshot.'}
    with ms.graph(owner) as (client, token, account):
        if not calendar.permissions(account['scopes']) & {'Contacts.Read', 'Contacts.ReadWrite'}:
            raise HTTPException(403, 'Enable contact access using the Microsoft connection controls.')
        # A cursor is a Microsoft nextLink, never a caller-selected Graph endpoint.
        url = calendar.GRAPH + 'me/contacts?' + urlencode({'$top': 100})
        if cursor:
            parsed = urlsplit(ms.clean(cursor, 6000))
            if parsed.scheme != 'https' or parsed.netloc != 'graph.microsoft.com' or parsed.path != '/v1.0/me/contacts' or parsed.fragment:
                raise HTTPException(400, 'Invalid contact page. Start a new Outlook search.')
            url = cursor
        response = client.get(url, headers={'Authorization': 'Bearer ' + token})
        response.raise_for_status()
        body = response.json()
        if not isinstance(body.get('value'), list):
            raise HTTPException(502, 'Microsoft returned an invalid contact page.')
        items = []
        for remote in body['value']:
            _, provider, name, raw = calendar.contact_row(owner, remote)
            attrs = json.loads(raw)
            if q.casefold() in (name + ' ' + json.dumps(attrs, ensure_ascii=False)).casefold():
                items.append({'provider_key': provider, 'name': name, 'attributes': attrs})
        return {'items': items, 'next_cursor': body.get('@odata.nextLink') or '',
                'message': 'Live default Outlook Contacts folder. Search applies to this page; continue through more pages if needed.'}


@app.get('/workspace/api/microsoft/contacts')
def contacts_search(request: Request, q: str = '', cursor: str = '', live: bool = False):
    return ms.hub.reply(search_contacts(_owner(request), q, cursor, live))


def preview(owner, provider):
    with ms.graph(owner) as (client, token, account):
        remote = read_contact(client, token, provider)
    fields, warnings = source_values(remote)
    link = query_one('''SELECT contact_id FROM outlook_contact_links
        WHERE owner_username=%s AND account_email=%s AND provider_key=%s''', (owner, account['account_email'], provider))
    return {'fields': fields, 'warnings': warnings, 'matches': matches(owner, fields),
            'provider_key': provider, 'version': ms.digest(remote), 'linked_contact_id': (link or {}).get('contact_id')}


@app.post('/workspace/api/microsoft/contacts/preview')
async def preview_page(request: Request):
    owner, data = await ms.values(request)
    return ms.hub.reply(await run_in_threadpool(preview, owner, ms.clean(data.get('provider_key'), 2000, required=True)))


def import_contact(owner, data):
    provider = ms.clean(data.get('provider_key'), 2000, required=True)
    with ms.graph(owner) as (client, token, account):
        remote = read_contact(client, token, provider)
    if ms.digest(remote) != data.get('version'):
        raise HTTPException(409, 'The Outlook contact changed. Open a fresh import preview.')
    source, warnings = source_values(remote)
    fields = data.get('fields')
    if not isinstance(fields, dict):
        raise HTTPException(400, 'Review the contact fields before importing.')
    try:
        values = _contact_form_values(ms.clean(fields.get('name'), 200, required=True), 'OTHER',
            ms.clean(fields.get('organization'), 200), ms.clean(fields.get('title'), 200),
            ms.clean(fields.get('phones'), 2000), ms.clean(fields.get('emails'), 2000),
            ms.clean(fields.get('address'), 1000), 'Microsoft 365', '\n'.join(warnings), 'PRIVATE')
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    with db_conn() as c:
        key = (owner, account['account_email'], provider)
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('outlook-contact:' + json.dumps(key),))
        linked = c.execute('''SELECT contact_id FROM outlook_contact_links
            WHERE owner_username=%s AND account_email=%s AND provider_key=%s''', key).fetchone()
        if linked:
            return {'message': 'Already imported. Open the linked contact to edit it.', 'contact_id': linked['contact_id']}
        if data.get('contact_id'):
            chosen = c.execute("SELECT id FROM contacts WHERE id=%s AND (visibility='ALL' OR owner_username=%s)",
                (ms.uid(data['contact_id']), owner)).fetchone()
            if not chosen:
                raise HTTPException(404, 'That directory contact is not available to you.')
            contact_id = chosen['id']
            if c.execute('SELECT contact_id FROM outlook_contact_links WHERE owner_username=%s AND account_email=%s AND contact_id=%s',
                         (owner, account['account_email'], contact_id)).fetchone():
                raise HTTPException(409, 'This directory contact already has an Outlook link. Open that contact instead.')
        else:
            # Check inside the transaction so two imports of the same person cannot race.
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('contact-import:' + owner,))
            duplicates = c.execute('''SELECT id FROM contacts WHERE active AND (visibility='ALL' OR owner_username=%s)
                AND (lower(name)=lower(%s) OR emails && %s::text[] OR phones && %s::text[]) LIMIT 1''',
                (owner, values[0], values[5], values[4])).fetchone()
            if duplicates and data.get('create_separate') is not True:
                raise HTTPException(409, 'A matching contact exists. Link it, or explicitly choose Create separate contact.')
            contact_id = c.execute('''INSERT INTO contacts(contact_id,name,contact_type,organization,title,phones,emails,address,tags,notes,visibility,owner_username)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''', ('OUTLOOK_' + uuid4().hex[:20], *values, owner)).fetchone()['id']
        c.execute('''INSERT INTO outlook_contact_links(owner_username,account_email,provider_key,contact_id,local_baseline,remote_baseline)
            VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb)''', (*key, contact_id, json.dumps(source), json.dumps(remote)))
    return {'message': 'Linked to Outlook. Local edits are saved here until you review and push them.', 'contact_id': contact_id}


@app.post('/workspace/api/microsoft/contacts/import')
async def import_page(request: Request):
    owner, data = await ms.values(request)
    return ms.hub.reply(await run_in_threadpool(import_contact, owner, data))


def linked_contact(owner, contact_id):
    row = query_one('''SELECT c.*,l.provider_key,l.account_email,l.local_baseline,l.remote_baseline
        FROM outlook_contact_links l JOIN contacts c ON c.id=l.contact_id
        JOIN workspace_calendar_connections a ON a.owner_username=l.owner_username AND a.account_email=l.account_email
        WHERE l.owner_username=%s AND c.id=%s AND (c.visibility='ALL' OR c.owner_username=%s)''',
        (owner, ms.uid(contact_id), owner))
    if not row:
        raise HTTPException(404, 'No Outlook link for this contact and your connected account. Find and import or link it first.')
    return row


def phone_key(value):
    try:
        normalized = _contact_phones(value)
        return normalized[0] if len(normalized) == 1 else value
    except ValueError:
        return value


def contact_patch(local, baseline, remote, address=None):
    """Only locally edited fields; preserve Outlook phone lanes and email labels."""
    patch = {}
    for key, target in [('name', 'displayName'), ('organization', 'companyName'), ('title', 'jobTitle')]:
        if local[key] != baseline[key]:
            patch[target] = local[key]
    if local['emails'] != baseline['emails']:
        original = {r.get('address', '').casefold(): r for r in remote.get('emailAddresses', [])}
        patch['emailAddresses'] = [original.get(v.casefold()) or {'address': v, 'name': local['name']} for v in local['emails']]
    if local['phones'] != baseline['phones']:
        desired = set(local['phones'])
        removed = set(baseline['phones']) - desired
        kept = set()
        for key in ('mobilePhone', 'businessPhones', 'homePhones'):
            old = ([remote.get(key)] if remote.get(key) else []) if key == 'mobilePhone' else remote.get(key) or []
            new = [v for v in old if phone_key(v) not in removed]
            kept.update(phone_key(v) for v in new)
            if old != new:
                patch[key] = new[0] if key == 'mobilePhone' and new else None if key == 'mobilePhone' else new
        added = [v for v in local['phones'] if v not in kept]
        if added:
            patch['businessPhones'] = (patch.get('businessPhones', remote.get('businessPhones')) or []) + added
    if address is not None:
        if not isinstance(address, dict):
            raise HTTPException(400, 'Enter each address part in the review form.')
        kind = address.get('kind')
        if kind not in {'businessAddress', 'homeAddress', 'otherAddress'}:
            raise HTTPException(400, 'Choose the Outlook address to update.')
        patch[kind] = {k: ms.clean(address.get(k), 1000 if k == 'street' else 200) for k in ADDRESS_FIELDS}
    elif local['address'] != baseline['address']:
        raise HTTPException(400, 'Review the changed address as Street, City, State, ZIP and Country before pushing.')
    return patch


def sync_preview(owner, contact_id):
    row = linked_contact(owner, contact_id)
    with ms.graph(owner) as (client, token, account):
        if account['account_email'] != row['account_email']:
            raise HTTPException(409, 'Reconnect the original Outlook account.')
        remote = read_contact(client, token, row['provider_key'])
    return {'local': local_values(row), 'remote': remote, 'baseline': row['local_baseline']}


@app.get('/workspace/api/microsoft/contacts/{contact_id}/sync')
def sync_preview_page(request: Request, contact_id: str):
    return ms.hub.reply(sync_preview(_owner(request), contact_id))


def prepare_contact(owner, data):
    request_id = ms.uid(data.get('request_id'))
    fingerprint = ms.digest({k: v for k, v in data.items() if k != 'request_id'})
    old = query_one('SELECT * FROM workspace_microsoft_operations WHERE owner_username=%s AND request_id=%s', (owner, request_id))
    if old:
        if old['fingerprint'] != fingerprint:
            raise HTTPException(409, 'Request ID conflict. Start a new review.')
        return ms.public_operation(old)
    row = linked_contact(owner, data.get('contact_id'))
    local = local_values(row)
    with ms.graph(owner, 'CONTACT_UPDATE') as (client, token, account):
        if account['account_email'] != row['account_email']:
            raise HTTPException(409, 'Reconnect the original Outlook account.')
        remote = read_contact(client, token, row['provider_key'])
        patch = contact_patch(local, row['local_baseline'], remote, data.get('address'))
    patch = {k: v for k, v in patch.items() if v != remote.get(k)}
    if not patch:
        raise HTTPException(400, 'No contact changes to push.')
    # Graph can recalculate displayName during other updates unless it is supplied.
    patch.setdefault('displayName', remote.get('displayName') or local['name'])
    payload = {'path': contact_path(row['provider_key']), 'json': patch, 'etag': remote.get('@odata.etag'),
               'remote_digest': ms.digest(remote), 'contact_id': str(row['id']), 'local': local,
               'account_email': account['account_email'], 'provider_key': row['provider_key']}
    review = {'operation': 'CONTACT_UPDATE', 'title': row['name'], 'account_email': account['account_email'],
              'changes': [{'field': k, 'before': remote.get(k), 'after': v} for k, v in patch.items() if v != remote.get(k)],
              'warning': 'Only the listed Outlook fields will change. Local notes, tags and access settings stay in City Manager OS.'}
    with db_conn() as c:
        stored = c.execute('''INSERT INTO workspace_microsoft_operations(owner_username,request_id,fingerprint,operation,account_email,payload,review)
            VALUES(%s,%s,%s,'CONTACT_UPDATE',%s,%s,%s::jsonb) ON CONFLICT(owner_username,request_id) DO NOTHING RETURNING *''',
            (owner, request_id, fingerprint, account['account_email'], calendar.cipher().encrypt(json.dumps(payload).encode()).decode(), json.dumps(review))).fetchone()
        if not stored:
            stored = c.execute('SELECT * FROM workspace_microsoft_operations WHERE owner_username=%s AND request_id=%s', (owner, request_id)).fetchone()
            if stored['fingerprint'] != fingerprint:
                raise HTTPException(409, 'Request ID conflict. Start a new review.')
    return ms.public_operation(stored)


def verify_contact(owner, payload, client, token):
    row = linked_contact(owner, payload['contact_id'])
    if row['provider_key'] != payload['provider_key'] or row['account_email'] != payload['account_email'] or local_values(row) != payload['local']:
        raise HTTPException(409, 'The local contact changed after review. Prepare a fresh review.')
    remote = read_contact(client, token, payload['provider_key'])
    if ms.digest(remote) != payload['remote_digest']:
        raise HTTPException(409, 'The Outlook contact changed after review. Prepare a fresh review.')


def finish_contact(owner, payload, remote):
    with db_conn() as c:
        c.execute('''UPDATE outlook_contact_links SET local_baseline=%s::jsonb,remote_baseline=%s::jsonb,updated_at=now()
            WHERE owner_username=%s AND account_email=%s AND provider_key=%s AND contact_id=%s''',
            (json.dumps(payload['local']), json.dumps(remote), owner, payload['account_email'], payload['provider_key'], ms.uid(payload['contact_id'])))


@app.get('/workspace/api/microsoft/contact-links')
def links_page(request: Request):
    owner = _owner(request)
    rows = query_all('''SELECT l.contact_id,l.updated_at FROM outlook_contact_links l
        JOIN workspace_calendar_connections a ON a.owner_username=l.owner_username AND a.account_email=l.account_email
        JOIN contacts c ON c.id=l.contact_id
        WHERE l.owner_username=%s AND (c.visibility='ALL' OR c.owner_username=%s)''', (owner, owner))
    return ms.hub.reply({'links': rows})
