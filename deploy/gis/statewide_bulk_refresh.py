#!/usr/bin/env python3
"""Probe and resumably synchronize official NJOGIS statewide bulk archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


USER_AGENT = "CityManagerOS-Statewide-GIS/1.0"
SOURCES = (
    ("addresses", "Addr_NG911.gdb.zip", "https://geoapps.nj.gov/njgin/address/Addr_NG911.gdb.zip"),
    ("parcels", "parcels_MOD4_Statewide.gdb.zip", "https://geoapps.nj.gov/njgin/parcel/parcels_MOD4_Statewide.gdb.zip"),
)


@dataclass(frozen=True)
class RemoteInfo:
    url: str
    etag: str
    last_modified: str
    content_length: int


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def open_with_retry(request: urllib.request.Request, attempts: int = 8):
    for attempt in range(1, attempts + 1):
        try:
            return urllib.request.urlopen(request, timeout=180)
        except urllib.error.HTTPError as exc:
            if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                raise
            if attempt == attempts:
                raise
            delay = min(2**attempt, 120)
            print(f"request attempt {attempt}/{attempts} failed ({exc}); retrying in {delay}s", flush=True)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise
            delay = min(2**attempt, 120)
            print(f"request attempt {attempt}/{attempts} failed ({exc}); retrying in {delay}s", flush=True)
            time.sleep(delay)


def probe(url: str) -> RemoteInfo:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        response = open_with_retry(request)
    except urllib.error.HTTPError as exc:
        if exc.code not in {400, 403, 405, 501}:
            raise
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"}
        )
        response = open_with_retry(request)
    with response:
        headers = response.headers
        length = int(headers.get("Content-Length") or 0)
        content_range = headers.get("Content-Range") or ""
        if "/" in content_range:
            length = int(content_range.rsplit("/", 1)[1])
        if length <= 0:
            raise RuntimeError(f"source did not provide a valid content length: {url}")
        return RemoteInfo(
            url=response.geturl(),
            etag=(headers.get("ETag") or "").strip(),
            last_modified=(headers.get("Last-Modified") or "").strip(),
            content_length=length,
        )


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def remote_matches_local(remote: RemoteInfo, target: Path, metadata: dict) -> bool:
    if not target.is_file() or target.stat().st_size != remote.content_length:
        return False
    if metadata.get("content_length") != remote.content_length:
        return False
    comparable = False
    if remote.etag and metadata.get("etag"):
        comparable = True
        if metadata["etag"] != remote.etag:
            return False
    if remote.last_modified and metadata.get("last_modified"):
        comparable = True
        if metadata["last_modified"] != remote.last_modified:
            return False
    return comparable


def download(remote: RemoteInfo, target: Path) -> None:
    part = target.with_suffix(target.suffix + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    offset = part.stat().st_size if part.exists() else 0
    if offset > remote.content_length:
        part.unlink()
        offset = 0
    if offset == remote.content_length:
        if not zipfile.is_zipfile(part):
            raise RuntimeError(f"completed partial file is not a ZIP archive: {target.name}")
        return
    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(remote.url, headers=headers)
    with open_with_retry(request) as response:
        append = offset > 0 and response.status == 206
        if not append:
            offset = 0
        mode = "ab" if append else "wb"
        written = offset
        next_report = ((written // (64 * 1024 * 1024)) + 1) * 64 * 1024 * 1024
        with part.open(mode) as handle:
            while True:
                block = response.read(8 * 1024 * 1024)
                if not block:
                    break
                handle.write(block)
                written += len(block)
                if written >= next_report:
                    percent = 100.0 * written / remote.content_length
                    print(f"{target.name}: {written:,}/{remote.content_length:,} bytes ({percent:.1f}%)", flush=True)
                    next_report += 64 * 1024 * 1024
            handle.flush()
            os.fsync(handle.fileno())
    if part.stat().st_size != remote.content_length:
        raise RuntimeError(
            f"download size mismatch for {target.name}: {part.stat().st_size} != {remote.content_length}"
        )
    if not zipfile.is_zipfile(part):
        raise RuntimeError(f"download is not a ZIP archive: {target.name}")


def promote_download(part: Path, target: Path) -> None:
    previous = target.with_suffix(target.suffix + ".previous")
    previous.unlink(missing_ok=True)
    if target.exists():
        os.link(target, previous)
    os.replace(part, target)


def synchronize(
    name: str,
    filename: str,
    url: str,
    destination: Path,
    metadata_dir: Path,
    *,
    bootstrap_existing: bool = False,
) -> dict:
    target = destination / filename
    metadata_path = metadata_dir / f"{filename}.metadata.json"
    remote = probe(url)
    metadata = load_json(metadata_path)
    bootstrapped = (
        bootstrap_existing
        and not metadata
        and target.is_file()
        and target.stat().st_size == remote.content_length
        and zipfile.is_zipfile(target)
    )
    changed = not (bootstrapped or remote_matches_local(remote, target, metadata))
    if changed:
        print(f"{name}: remote revision changed or local metadata missing; downloading", flush=True)
        download(remote, target)
        part = target.with_suffix(target.suffix + ".part")
        digest = sha256_file(part)
        promote_download(part, target)
        if metadata:
            atomic_json(metadata_path.with_suffix(metadata_path.suffix + ".previous"), metadata)
    else:
        print(f"{name}: remote revision unchanged; reusing local archive", flush=True)
        digest = sha256_file(target)
    payload = {
        "name": name,
        "file": str(target),
        "url": remote.url,
        "etag": remote.etag,
        "last_modified": remote.last_modified,
        "content_length": remote.content_length,
        "sha256": digest,
        "changed": changed,
        "bootstrapped": bootstrapped,
        "previous_file": str(target.with_suffix(target.suffix + ".previous")) if changed else "",
        "previous_metadata": str(metadata_path.with_suffix(metadata_path.suffix + ".previous")) if changed else "",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(metadata_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("probe", "sync"))
    parser.add_argument("--destination", type=Path, default=Path("/opt/citymanager-data/gis/incoming"))
    parser.add_argument("--metadata-dir", type=Path, default=Path("/opt/citymanager-data/gis/source-metadata"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--bootstrap-existing",
        action="store_true",
        help="Trust an existing validated ZIP when its size matches the first remote probe.",
    )
    args = parser.parse_args()

    results = []
    for name, filename, url in SOURCES:
        if args.mode == "probe":
            results.append({"name": name, **asdict(probe(url))})
        else:
            results.append(
                synchronize(
                    name,
                    filename,
                    url,
                    args.destination,
                    args.metadata_dir,
                    bootstrap_existing=args.bootstrap_existing,
                )
            )
    manifest = {
        "mode": args.mode,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "changed": any(item.get("changed", False) for item in results),
        "sources": results,
    }
    atomic_json(args.manifest, manifest)
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
