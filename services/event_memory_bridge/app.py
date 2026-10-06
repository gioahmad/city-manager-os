"""NAS-only storage bridge. Raw uploads never pass through the City Manager VPS."""
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from event_memory_protocol import descriptor, key_bytes, note_text, origin, owner_key, paths, sign, verify


def safe_path(root, relative):
    target = root / relative
    current = root
    for component in Path(relative).parts:
        current = current / component
        if current.is_symlink():
            raise HTTPException(409, 'Symlinks are not permitted in managed material paths')
    if not target.resolve().is_relative_to(root):
        raise HTTPException(409, 'Invalid managed material path')
    return target


def write_file(path, data):
    with path.open('xb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_app(storage, secret, owner, allowed_origin, vault_name, min_free_bytes=1024**3):
    root = Path(storage).resolve()
    vault = root / 'vault'
    staging = root / 'staging'  # Outside the synced vault, on the same NAS filesystem.
    for directory in (vault, staging):
        if directory.is_symlink():
            raise ValueError('Storage directories cannot be symlinks')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if vault.stat().st_dev != staging.stat().st_dev:
        raise ValueError('Vault and staging must share a filesystem for atomic publication')
    if not vault_name or len(vault_name) > 100:
        raise ValueError('Set an Obsidian vault name')
    key, identity = key_bytes(secret), owner_key(owner)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=[origin(allowed_origin)],
                       allow_methods=['GET', 'PUT'], allow_headers=['Authorization', 'Content-Type'],
                       expose_headers=['Content-Disposition'], allow_credentials=False)
    in_flight = set()  # One worker, with at most two active uploads.

    def ticket(request, purpose):
        raw = request.headers.get('authorization', '')
        if not raw.startswith('Bearer '):
            raise HTTPException(401, 'An Event Memory ticket is required')
        try:
            data = descriptor(verify(key, purpose, raw[7:]))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(403, 'Invalid or expired Event Memory ticket')
        if data['owner'] != identity or data['app_origin'] != origin(allowed_origin):
            raise HTTPException(403, 'This private vault belongs to another account or application')
        return data

    def stored(data):
        p = paths(data)
        manifest = safe_path(vault, p['directory'] + '/receipt.json')
        original = safe_path(vault, p['file'])
        if not manifest.exists():
            raise HTTPException(404, 'Not stored yet; keep the original file')
        try:
            saved = json.loads(manifest.read_text())
            if saved != data or original.stat().st_size != data['bytes']:
                raise ValueError('Mismatch')
            with original.open('rb') as f:
                digest = hashlib.file_digest(f, 'sha256').hexdigest()
            if digest != data['sha256']:
                raise ValueError('Mismatch')
        except (OSError, ValueError):
            raise HTTPException(409, 'The NAS copy is missing or differs from its recorded checksum')
        return p

    def receipt(data):
        p = stored(data)
        return {'receipt': sign(key, 'stored', dict(descriptor=data, vault=vault_name,
                                                  note_path=p['note']), ttl=86400)}

    @app.middleware('http')
    async def private_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY'})
        return response

    @app.get('/health')
    def health():
        return {'service': 'event-memory-bridge', 'version': 1}  # Not a vault-sync or backup claim.

    @app.put('/v1/material')
    async def upload(request: Request):
        data = ticket(request, 'upload')
        p = paths(data)
        destination = safe_path(vault, p['directory'])
        if destination.exists():
            return receipt(data)  # Idempotent recovery, never rewrite user-edited notes.
        if len(in_flight) >= 2 or data['id'] in in_flight:
            raise HTTPException(409, 'Upload in progress. Check NAS before retrying.')
        try:
            supplied_size = int(request.headers.get('content-length', '-1'))
        except ValueError:
            raise HTTPException(400, 'Invalid file length')
        if supplied_size not in (-1, data['bytes']):
            raise HTTPException(400, 'File length differs from the authorized upload')
        if request.headers.get('content-type', '').split(';')[0] != data['media_type']:
            raise HTTPException(400, 'Unexpected file type')
        if shutil.disk_usage(root).free < data['bytes'] + min_free_bytes:
            raise HTTPException(507, 'NAS free-space reserve reached; keep the file on your device')
        in_flight.add(data['id'])
        temp = None
        try:
            temp = Path(tempfile.mkdtemp(prefix='upload-', dir=staging))
            count, digest, header = 0, hashlib.sha256(), b''
            with (temp / Path(p['file']).name).open('xb') as output:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > data['bytes']:
                        raise HTTPException(413, 'Upload exceeds its authorized size')
                    header = (header + chunk[:8])[:8]
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            signatures = {'application/pdf': b'%PDF-', 'image/jpeg': b'\xff\xd8\xff', 'image/png': b'\x89PNG\r\n\x1a\n'}
            if count != data['bytes'] or digest.hexdigest() != data['sha256']:
                raise HTTPException(422, 'Incomplete upload or checksum mismatch; no receipt issued')
            if not header.startswith(signatures[data['media_type']]):
                raise HTTPException(415, 'File header does not match PDF, JPEG or PNG')
            write_file(temp / 'Material.md', note_text(data).encode())
            write_file(temp / 'receipt.json', json.dumps(data, sort_keys=True).encode())
            fsync_directory(temp)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            safe_path(vault, p['directory'])
            try:
                os.rename(temp, destination)
            except FileExistsError:
                return receipt(data)
            fsync_directory(destination.parent)
            # A notebook is created once; future material uploads never rewrite it.
            notebook = safe_path(vault, p['event'])
            draft = staging / ('notebook-' + data['id'] + '.md')
            try:
                write_file(draft, ('---\ntype: event-memory\ncity_manager_id: ' + json.dumps(data['source_id'])
                                  + '\ntitle: ' + json.dumps(data['event_title'])
                                  + '\nvisibility: PRIVATE\n---\n\n# Event notebook\n\n'
                                  + 'Each material note links here. Use Obsidian backlinks to see materials.\n\n'
                                  + '## People I met\n\n## Notes\n\n## Follow-ups to review\n').encode())
                try:
                    os.link(draft, notebook)
                    fsync_directory(notebook.parent)
                except FileExistsError:
                    pass
            finally:
                draft.unlink(missing_ok=True)
            return receipt(data)
        finally:
            if temp is not None:
                shutil.rmtree(temp, ignore_errors=True)
            in_flight.discard(data['id'])

    @app.get('/v1/receipt')
    def get_receipt(request: Request):
        return receipt(ticket(request, 'upload'))

    @app.get('/v1/file')
    def download(request: Request):
        data = ticket(request, 'download')
        p = stored(data)
        return FileResponse(safe_path(vault, p['file']), filename=data['filename'],
                            media_type=data['media_type'], content_disposition_type='attachment')

    return app


def from_environment():
    return create_app(os.environ['EVENT_MEMORY_STORAGE'], os.environ['EVENT_MEMORY_KEY'],
                      os.environ['EVENT_MEMORY_OWNER'], os.environ['EVENT_MEMORY_APP_ORIGIN'],
                      os.environ['EVENT_MEMORY_VAULT_NAME'])
