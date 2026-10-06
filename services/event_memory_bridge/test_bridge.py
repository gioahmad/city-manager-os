import base64
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'dashboard'))
sys.path.insert(0, str(ROOT))
from event_memory_protocol import descriptor, key_bytes, note_text, owner_key, paths, sign, verify
from services.event_memory_bridge.app import create_app

SECRET = base64.urlsafe_b64encode(b'T' * 32).decode()
KEY = key_bytes(SECRET)
PDF = b'%PDF-1.7\nfixture program\n%%EOF'


@pytest.fixture
def example():
    return descriptor(dict(id=str(uuid4()), owner=owner_key('executive'), source_kind='EVENT',
                           source_id=str(uuid4()), filename='Program.pdf', bytes=len(PDF),
                           sha256=hashlib.sha256(PDF).hexdigest(), event_title='Community event',
                           app_origin='https://dashboard.example'))


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path, SECRET, 'executive', 'https://dashboard.example', 'Private Vault', 0))


def headers(data, purpose='upload'):
    return {'Authorization': 'Bearer ' + sign(KEY, purpose, data), 'Content-Type': data['media_type']}


def test_storage_receipt_and_authenticated_download(client, tmp_path, example):
    response = client.put('/v1/material', headers=headers(example), content=PDF)
    assert response.status_code == 200, response.text
    receipt = verify(KEY, 'stored', response.json()['receipt'])
    assert receipt['descriptor'] == example
    p = paths(example)
    assert (tmp_path/'vault'/p['file']).read_bytes() == PDF
    assert (tmp_path/'vault'/p['note']).is_file()
    assert (tmp_path/'vault'/p['event']).is_file()
    assert not list((tmp_path/'staging').iterdir())
    response = client.get('/v1/file', headers=headers(example, 'download'))
    assert response.content == PDF
    assert 'attachment;' in response.headers['content-disposition']


def test_repeat_does_not_overwrite_notes(client, tmp_path, example):
    assert client.put('/v1/material', headers=headers(example), content=PDF).status_code == 200
    p = paths(example)
    for path in (p['event'], p['note']):
        (tmp_path/'vault'/path).write_text('My handwritten edits')
    assert client.put('/v1/material', headers=headers(example), content=PDF).status_code == 200
    assert client.get('/v1/receipt', headers=headers(example)).status_code == 200
    for path in (p['event'], p['note']):
        assert (tmp_path/'vault'/path).read_text() == 'My handwritten edits'


@pytest.mark.parametrize('filename', ['../program.pdf', 'folder/program.pdf', 'x\\program.pdf', 'script.html', 'note.svg', 'x\n.pdf'])
def test_file_names_never_select_paths(example, filename):
    with pytest.raises(ValueError):
        descriptor({**example, 'filename': filename})


def test_expired_tampered_wrong_purpose_and_wrong_owner(client, example):
    bad = [sign(KEY, 'upload', example, ttl=-1), sign(KEY, 'download', example), sign(KEY, 'upload', example)[:-1] + 'z']
    for token in bad:
        assert client.put('/v1/material', headers={'Authorization': 'Bearer ' + token}, content=PDF).status_code == 403
    assert client.put('/v1/material', content=PDF).status_code == 401
    other = {**example, 'owner': owner_key('another-user')}
    assert client.put('/v1/material', headers=headers(other), content=PDF).status_code == 403


def test_incomplete_and_corrupt_upload_leave_no_receipt(client, example, tmp_path):
    response = client.put('/v1/material', headers=headers(example), content=PDF.replace(b'program', b'garbage'))
    assert response.status_code == 422
    assert client.get('/v1/receipt', headers=headers(example)).status_code == 404
    assert not list((tmp_path/'staging').iterdir())


def test_wrong_content_header_rejected(client, example):
    wrong = b'<html>unsafe file disguised as pdf</html>'
    data = {**example, 'bytes': len(wrong), 'sha256': hashlib.sha256(wrong).hexdigest()}
    assert client.put('/v1/material', headers=headers(data), content=wrong).status_code == 415


def test_disk_reserve_blocks_upload(tmp_path, example):
    c = TestClient(create_app(tmp_path, SECRET, 'executive', 'https://dashboard.example', 'Private Vault', 2**62))
    assert c.put('/v1/material', headers=headers(example), content=PDF).status_code == 507


def test_symlink_cannot_escape_vault(client, example, tmp_path):
    outside = tmp_path/'outside';outside.mkdir()
    (tmp_path/'vault'/'Event Memory').symlink_to(outside, target_is_directory=True)
    assert client.put('/v1/material', headers=headers(example), content=PDF).status_code == 409
    assert not list(outside.iterdir())


def test_receipt_after_file_changed_is_not_issued(client, example, tmp_path):
    client.put('/v1/material', headers=headers(example), content=PDF)
    (tmp_path/'vault'/paths(example)['file']).write_bytes(PDF.replace(b'program', b'changed'))
    assert client.get('/v1/receipt', headers=headers(example)).status_code == 409


def test_exact_cors_allowlist(client):
    good = client.options('/v1/material', headers={'Origin': 'https://dashboard.example', 'Access-Control-Request-Method': 'PUT', 'Access-Control-Request-Headers': 'authorization,content-type'})
    assert good.headers['access-control-allow-origin'] == 'https://dashboard.example'
    bad = client.options('/v1/material', headers={'Origin': 'https://attacker.example', 'Access-Control-Request-Method': 'PUT'})
    assert 'access-control-allow-origin' not in bad.headers


def test_markdown_metadata_cannot_inject_remote_images(example):
    text = note_text({**example, 'event_title': '---\n![](https://attacker.example/x)\n---'})
    assert '\n![](https://attacker.example/x)' not in text
    assert text.count('\n---\n') == 1
