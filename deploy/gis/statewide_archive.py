#!/usr/bin/env python3
"""Validate an official NJOGIS statewide FileGDB archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArchiveInfo:
    path: str
    sha256: str
    gdb_root: str
    entries: int
    compressed_bytes: int
    expanded_bytes: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_archive(path: Path, expected_sha256: str, *, crc: bool) -> ArchiveInfo:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"missing or empty archive: {path}")
    if not zipfile.is_zipfile(path):
        raise ValueError(f"not a ZIP archive: {path}")

    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256.lower():
        raise ValueError(
            f"SHA256 mismatch for {path.name}: expected {expected_sha256}, got {actual_sha256}"
        )

    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        roots = sorted(
            {
                item.filename.split("/", 1)[0]
                for item in members
                if ".gdb/" in item.filename.lower()
            }
        )
        if len(roots) != 1 or not roots[0].lower().endswith(".gdb"):
            raise ValueError(f"expected exactly one FileGDB root in {path.name}; found {roots}")
        if crc:
            bad_member = archive.testzip()
            if bad_member:
                raise ValueError(f"CRC failure in {path.name}: {bad_member}")
        return ArchiveInfo(
            path=str(path),
            sha256=actual_sha256,
            gdb_root=roots[0],
            entries=len(members),
            compressed_bytes=sum(item.compress_size for item in members),
            expanded_bytes=sum(item.file_size for item in members),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--crc", action="store_true")
    parser.add_argument("--field", choices=("gdb_root", "sha256"))
    args = parser.parse_args()

    try:
        info = inspect_archive(args.archive, args.sha256, crc=args.crc)
    except ValueError as exc:
        parser.error(str(exc))
    if args.field:
        print(getattr(info, args.field))
    else:
        print(json.dumps(asdict(info), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
