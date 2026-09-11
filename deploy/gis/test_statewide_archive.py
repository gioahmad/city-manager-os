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


def test_curved_polygon_import_contract() -> None:
    script = Path(__file__).with_name("import_statewide_gis.sh").read_text()
    assert script.count("-nlt CONVERT_TO_LINEAR -nlt PROMOTE_TO_MULTI") == 2
    assert "-skipfailures" not in script


def test_statewide_county_and_reuse_contract() -> None:
    script = Path(__file__).with_name("import_statewide_gis.sh").read_text()
    assert 'HUDSON_ADDRESS_COUNTY_CODE="882278"' in script
    assert "'882278','882279','882910'" in script
    assert "trim(county) ~ '^[0-9]{6}$'" in script
    assert "'','MERCER COUNTY'" in script
    assert "MAX_NONSPATIAL_ADDRESSES=10" in script
    assert script.count("validate_staging") == 3
    assert 'PGOPTIONS="-c client_min_messages=warning"' in script


if __name__ == "__main__":
    test_valid_archive()
    test_wrong_checksum()
    test_multiple_gdb_roots_rejected()
    test_curved_polygon_import_contract()
    test_statewide_county_and_reuse_contract()
    print("CMOS STATEWIDE ARCHIVE TESTS: PASS")
