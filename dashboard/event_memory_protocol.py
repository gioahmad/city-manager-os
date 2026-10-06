"""Small shared wire contract. No database, network, Obsidian or filesystem access."""
import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

MAX_BYTES = 5 * 1024 * 1024  # Conservative upload cap; verify Sync plan limits before increasing.
TYPES = {'.pdf': 'application/pdf', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png'}


def key_bytes(value):
    try:
        key = base64.b64decode(value, altchars=b'-_', validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('Configure a separate base64 Event Memory signing key') from exc
    if len(key) < 32:
        raise ValueError('Event Memory signing key must contain at least 32 random bytes')
    return key


def origin(value):
    p = urlsplit(value)
    if (p.scheme != 'https' or not p.hostname or p.username or p.password or
            p.path not in ('', '/') or p.query or p.fragment or
            not re.fullmatch(r'[A-Za-z0-9.:-]+', p.netloc)):
        raise ValueError('Use an exact HTTPS origin without a path')
    _ = p.port  # Reject malformed ports.
    return 'https://' + p.netloc.lower()


def owner_key(owner):
    return hashlib.sha256(owner.casefold().encode()).hexdigest()


def descriptor(values):
    """Allowlist fields. File names never become filesystem paths."""
    filename = str(values['filename'])
    suffix = '.' + filename.rsplit('.', 1)[-1].lower()
    if (not 1 <= len(filename) <= 180 or any(ord(c) < 32 for c in filename)
            or any(c in filename for c in '/\\') or suffix not in TYPES):
        raise ValueError('Choose a PDF, JPEG or PNG with a plain file name')
    size = values['bytes']
    if type(size) is not int or not 1 <= size <= MAX_BYTES:
        raise ValueError('Choose a nonempty file no larger than 5 MiB')
    digest = str(values['sha256'])
    owner = str(values['owner'])
    if not re.fullmatch('[a-f0-9]{64}', digest) or not re.fullmatch('[a-f0-9]{64}', owner):
        raise ValueError('Invalid digest or owner')
    kind = values['source_kind']
    if kind not in {'EVENT', 'CALENDAR'}:
        raise ValueError('Choose an internal event or an owned calendar appointment')
    title = str(values.get('event_title') or 'Event')
    if len(title) > 500:
        raise ValueError('Event title is too long')
    source_id, material_id = str(UUID(str(values['source_id']))), str(UUID(str(values['id'])))
    source_origin = origin(values['app_origin'])
    return dict(v=1, id=material_id, owner=owner, source_kind=kind, source_id=source_id,
                filename=filename, bytes=size, sha256=digest, media_type=TYPES[suffix],
                event_title=title, app_origin=source_origin)


def paths(data):
    d = descriptor(data)
    base = f"Event Memory/{d['owner']}/{d['source_kind']}-{d['source_id']}"
    material = base + '/' + d['id']
    suffix = {'application/pdf': '.pdf', 'image/jpeg': '.jpg', 'image/png': '.png'}[d['media_type']]
    return dict(event=base + '/Event.md', note=material + '/Material.md',
                file=material + '/original' + suffix, directory=material)


def sign(key, purpose, data, ttl=900):
    body = dict(purpose=purpose, exp=int(time.time()) + ttl, data=data)
    encoded = base64.urlsafe_b64encode(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).decode().rstrip('=')
    signature = hmac.new(key, ('event-memory-v1.' + encoded).encode(), hashlib.sha256).hexdigest()
    return encoded + '.' + signature


def verify(key, purpose, token):
    try:
        if not isinstance(token, str) or len(token) > 12000:
            raise ValueError('Invalid ticket')
        encoded, supplied = token.split('.')
        expected = hmac.new(key, ('event-memory-v1.' + encoded).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError('Invalid ticket signature')
        body = json.loads(base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True))
        if body['purpose'] != purpose or type(body['exp']) is not int or body['exp'] <= time.time():
            raise ValueError('Ticket expired or has the wrong purpose')
        if not isinstance(body['data'], dict):
            raise ValueError('Invalid ticket payload')
        return body['data']
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError('Invalid or expired Event Memory ticket') from exc


def obsidian_link(vault, note):
    return 'obsidian://open?' + urlencode({'vault': vault, 'file': note}, quote_via=quote)


def note_text(data):
    d, p = descriptor(data), paths(data)
    fields = dict(type='event-material', event_memory_id=d['id'], source_kind=d['source_kind'],
                  city_manager_id=d['source_id'], event_title=d['event_title'],
                  original_filename=d['filename'], sha256=d['sha256'], visibility='PRIVATE')
    front = '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in fields.items())
    event_url = d['app_origin'] + '/context/' + d['source_kind'] + '/' + d['source_id']
    return ('---\n' + front + '\n---\n\n# Event material\n\n'
            + '[[' + p['event'][:-3] + '|Event notebook]]\n\n'
            + '![[' + p['file'] + ']]\n\n'
            + '[Open source event in City Manager OS](' + event_url + ')\n\n'
            + '## My notes\n\n## Follow-ups to review\n\n'
            + 'This file is private. No people or tasks are automatically created from it.\n')
