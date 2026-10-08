"""Selected original photos: real DB capture, private downloads and rollback."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from microsoft_fixture import setup

application, core, ms, calendar, hub, auth, provider, owner, mail_id, cookie, csrf = setup()
import microsoft_mail_photos as photos


def rejected(fn, status):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == status, (exc.status_code, exc.detail)
    else:
        raise AssertionError('Expected HTTP ' + str(status))


def count():
    return core.query_one("SELECT count(*) AS n FROM workspace_documents WHERE owner_username=%s AND profile->'email_photo'->>'mail_id'=%s",
                          (owner, mail_id))['n']


base = {'kind': 'MAIL', 'id': mail_id, 'destination': 'BRAIN', 'title': 'Saved email with chosen photos',
        'body': 'Reviewed private email content', 'request_id': str(uuid4())}
with TestClient(application) as client:
    client.cookies.set(auth.COOKIE_NAME, cookie)
    client.headers['Origin'] = 'https://fixture.example.com'
    choices = client.get('/workspace/api/microsoft/mail/' + mail_id + '/photos').json()
    assert {p['id'] for p in choices['photos']} == {'site-photo', 'inline-photo', 'unused-photo'}
    assert next(p for p in choices['photos'] if p['id'] == 'inline-photo')['is_inline']
    assert not provider.photo_reads and count() == 0

    plain = ms.capture(owner, base)
    assert not plain.get('photos') and not provider.photo_reads and count() == 0
    data = {**base, 'request_id': str(uuid4()), 'photos': ['site-photo', 'inline-photo']}
    result = ms.capture(owner, data)
    assert len(result['photos']) == 2 and count() == 2
    assert provider.photo_reads == ['site-photo', 'inline-photo'] and not provider.writes
    assert ms.capture(owner, data) == result and count() == 2
    for record in result['photos']:
        downloaded = client.get(record['url'])
        expected = b'original-site-photo' if record['filename'] == 'site.jpg' else b'original-inline-photo'
        assert downloaded.status_code == 200 and downloaded.content == expected
        assert downloaded.headers['x-content-type-options'] == 'nosniff'
        assert downloaded.headers['content-disposition'].startswith('attachment;')
        reopened = hub.find(owner, 'DOCUMENT', record['id'])
        assert reopened['metadata']['profile']['email_photo']['mail_id'] == mail_id
        assert reopened['status'] == 'READY'
    detail = client.get('/workspace/api/hub/detail/MAIL/' + mail_id).json()
    assert len(detail['item']['metadata']['email_photos']) == 2
    links, _ = hub.context(owner, hub.find(owner, 'BRAIN', result['id']))
    assert len([r for r in links if r['kind'] == 'DOCUMENT']) == 2
    client.cookies.set(auth.COOKIE_NAME, auth._issue_session(auth.Account('Reader', 'READ_ONLY', '')))
    assert client.get(result['photos'][0]['url']).status_code == 404
    assert client.get('/workspace/api/microsoft/mail/' + mail_id + '/photos').status_code == 404
    client.cookies.set(auth.COOKIE_NAME, cookie)

    # A new save reuses the originals; simultaneous captures cannot duplicate them.
    attempts = [{**data, 'destination': 'TASK', 'request_id': str(uuid4())} for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        repeated = list(pool.map(lambda entry: ms.capture(owner, entry), attempts))
    assert count() == 2 and provider.photo_reads == ['site-photo', 'inline-photo']
    assert all(r['photos'][0]['id'] == result['photos'][0]['id'] for r in repeated)

    provider.photo_failure = 'unused-photo'
    failing = {**base, 'request_id': str(uuid4()), 'photos': ['unused-photo']}
    rejected(lambda: ms.capture(owner, failing), 502)
    assert not core.query_one('SELECT 1 FROM workspace_capture_receipts WHERE owner_username=%s AND request_id=%s', (owner, failing['request_id']))
    assert count() == 2
    provider.photo_failure = ''
    rejected(lambda: ms.capture(owner, {**failing, 'request_id': str(uuid4()), 'photos': ['pdf-file']}), 400)
    rejected(lambda: ms.capture(owner, {**failing, 'request_id': str(uuid4()), 'photos': ['site-photo'] * 2}), 400)
    with patch.object(photos, 'MAX_FILE', 16):
        # Declared size is small; streamed contents are larger and must be bounded.
        rejected(lambda: ms.capture(owner, {**failing, 'request_id': str(uuid4())}), 413)
    assert count() == 2
    provider.mailbox = 'changed@example.com'
    rejected(lambda: ms.capture(owner, {**data, 'request_id': str(uuid4())}), 409)
    provider.mailbox = 'fixture@example.com'
    with core.db_conn() as connection:
        calendar.replace_mail(connection, owner, [])
    assert hub.find(owner, 'MAIL', mail_id)['id']
    with core.db_conn() as connection:
        connection.execute('DELETE FROM workspace_calendar_connections WHERE owner_username=%s', (owner,))
    assert client.get(result['photos'][0]['url']).status_code == 200
    assert hub.find(owner, 'BRAIN', result['id'])['id']

print('EMAIL PHOTO RETENTION: PASS — explicit selection, inline photos, exact bytes, atomic rollback, deduplication, ownership, mailbox pinning and disconnect retention')
