#!/usr/bin/env python3
"""Unit tests for bounded, resumable NJ statewide archive synchronization."""

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import statewide_bulk_refresh as refresh


def zip_bytes(name="sample.txt", body=b"statewide"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, body)
    return output.getvalue()


class Response(io.BytesIO):
    def __init__(self, body, *, status=200, headers=None, url="https://example.test/source.zip"):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}
        self._url = url

    def geturl(self):
        return self._url


class StatewideBulkRefreshTests(unittest.TestCase):
    def test_probe_uses_content_range_total(self):
        response = Response(
            b"x", status=206,
            headers={"Content-Length": "1", "Content-Range": "bytes 0-0/1234", "ETag": '"v2"'},
        )
        with patch.object(refresh, "open_with_retry", return_value=response):
            info = refresh.probe("https://example.test/source.zip")
        self.assertEqual(info.content_length, 1234)
        self.assertEqual(info.etag, '"v2"')

    def test_bootstrap_reuses_valid_existing_archive(self):
        data = zip_bytes()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "source.zip"
            target.write_bytes(data)
            remote = refresh.RemoteInfo("https://example.test/source.zip", '"v1"', "today", len(data))
            with patch.object(refresh, "probe", return_value=remote):
                result = refresh.synchronize(
                    "test", target.name, remote.url, root, root / "metadata", bootstrap_existing=True
                )
            self.assertFalse(result["changed"])
            self.assertTrue(result["bootstrapped"])
            self.assertEqual(result["sha256"], refresh.sha256_file(target))

    def test_unchanged_revision_reuses_archive(self):
        data = zip_bytes()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "source.zip"
            target.write_bytes(data)
            metadata_dir = root / "metadata"
            metadata_dir.mkdir()
            (metadata_dir / "source.zip.metadata.json").write_text(json.dumps({
                "content_length": len(data), "etag": '"v1"', "last_modified": "today"
            }))
            remote = refresh.RemoteInfo("https://example.test/source.zip", '"v1"', "today", len(data))
            with patch.object(refresh, "probe", return_value=remote), patch.object(refresh, "download") as download:
                result = refresh.synchronize("test", target.name, remote.url, root, metadata_dir)
            self.assertFalse(result["changed"])
            download.assert_not_called()

    def test_changed_revision_retains_previous_archive(self):
        old = zip_bytes(body=b"old")
        new = zip_bytes(body=b"new revision")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "source.zip"
            target.write_bytes(old)
            metadata_dir = root / "metadata"
            metadata_dir.mkdir()
            (metadata_dir / "source.zip.metadata.json").write_text(json.dumps({
                "content_length": len(old), "etag": '"v1"', "last_modified": "today"
            }))
            remote = refresh.RemoteInfo("https://example.test/source.zip", '"v2"', "tomorrow", len(new))

            def fake_download(_remote, output):
                output.with_suffix(output.suffix + ".part").write_bytes(new)

            with patch.object(refresh, "probe", return_value=remote), patch.object(refresh, "download", fake_download):
                result = refresh.synchronize("test", target.name, remote.url, root, metadata_dir)
            self.assertTrue(result["changed"])
            self.assertEqual(target.read_bytes(), new)
            self.assertEqual(target.with_suffix(".zip.previous").read_bytes(), old)
            previous_metadata = json.loads((metadata_dir / "source.zip.metadata.json.previous").read_text())
            self.assertEqual(previous_metadata["etag"], '"v1"')

    def test_partial_download_uses_range(self):
        data = zip_bytes(body=b"resume")
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "source.zip"
            part = target.with_suffix(".zip.part")
            cut = len(data) // 2
            part.write_bytes(data[:cut])
            remote = refresh.RemoteInfo("https://example.test/source.zip", '"v2"', "today", len(data))
            captured = {}

            def fake_open(request):
                captured["range"] = request.headers.get("Range")
                return Response(data[cut:], status=206)

            with patch.object(refresh, "open_with_retry", fake_open):
                refresh.download(remote, target)
            self.assertEqual(captured["range"], f"bytes={cut}-")
            self.assertEqual(part.read_bytes(), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
