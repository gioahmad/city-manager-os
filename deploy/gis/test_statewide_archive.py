#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from statewide_archive import inspect_archive, sha256_file


def make_archive(path: Path, root: str = "Sample.gdb") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{root}/a00000001.gdbtable", b"table")
        archive.writestr(f"{root}/a00000001.gdbtablx", b"index")


def test_valid_archive() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sample.zip"
        make_archive(path)
        digest = sha256_file(path)
        info = inspect_archive(path, digest, crc=True)
        assert info.gdb_root == "Sample.gdb"
        assert info.entries == 2
        assert info.sha256 == digest


def test_wrong_checksum() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sample.zip"
        make_archive(path)
        try:
            inspect_archive(path, "0" * 64, crc=False)
        except ValueError as exc:
            assert "SHA256 mismatch" in str(exc)
        else:
            raise AssertionError("wrong checksum was accepted")


def test_multiple_gdb_roots_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sample.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("One.gdb/a.gdbtable", b"one")
            archive.writestr("Two.gdb/a.gdbtable", b"two")
        try:
            inspect_archive(path, sha256_file(path), crc=False)
        except ValueError as exc:
            assert "exactly one FileGDB root" in str(exc)
        else:
            raise AssertionError("archive with multiple FileGDB roots was accepted")


if __name__ == "__main__":
    test_valid_archive()
    test_wrong_checksum()
    test_multiple_gdb_roots_rejected()
    print("CMOS STATEWIDE ARCHIVE TESTS: PASS")
