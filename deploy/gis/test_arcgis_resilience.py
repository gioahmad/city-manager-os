#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

import arcgis_resilience as resilience


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_request_recovers_from_dns_failure():
    responses = [
        urllib.error.URLError("temporary DNS failure"),
        Response({"ok": True}),
    ]

    def fake_open(*_args, **_kwargs):
        result = responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    with mock.patch.object(resilience.urllib.request, "urlopen", fake_open), mock.patch.object(
        resilience.time, "sleep"
    ) as sleep:
        assert resilience.request_json("https://example.invalid", {"f": "json"}, attempts=2) == {
            "ok": True
        }
    sleep.assert_called_once_with(2)


def test_download_resumes_at_failed_batch():
    with tempfile.TemporaryDirectory() as directory:
        part = Path(directory) / "sample.geojson.part"
        calls: list[tuple[int, ...]] = []
        fail_once = {3: True}

        def request(_url, params):
            ids = tuple(int(value) for value in params["objectIds"].split(","))
            calls.append(ids)
            if ids[0] == 3 and fail_once.pop(3, False):
                raise urllib.error.URLError("DNS")
            return {
                "features": [
                    {"type": "Feature", "properties": {"id": value}, "geometry": None}
                    for value in ids
                ]
            }

        try:
            resilience.download_feature_collection(
                ids=[1, 2, 3, 4, 5],
                batch_size=2,
                query_url="https://example.invalid/query",
                query_parameters={"f": "geojson"},
                temporary_path=part,
                label="TEST",
                request=request,
            )
        except urllib.error.URLError:
            pass
        else:
            raise AssertionError("first run should fail")

        assert calls == [(1, 2), (3, 4)]
        calls.clear()
        written = resilience.download_feature_collection(
            ids=[1, 2, 3, 4, 5],
            batch_size=2,
            query_url="https://example.invalid/query",
            query_parameters={"f": "geojson"},
            temporary_path=part,
            label="TEST",
            request=request,
        )
        assert written == 5
        assert calls == [(3, 4), (5,)]
        payload = json.loads(part.read_text())
        assert [item["properties"]["id"] for item in payload["features"]] == [1, 2, 3, 4, 5]
        assert not Path(f"{part}.checkpoint.json").exists()


def test_snapshot_revision_check():
    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory) / "snapshot.geojson"
        metadata = Path(directory) / "snapshot.metadata.json"
        ids = [5, 10, 15]
        data.write_text('{"type":"FeatureCollection","features":[]}')
        metadata.write_text(
            json.dumps(
                {
                    "feature_count": 3,
                    "object_ids_sha256": resilience.object_ids_sha256(ids),
                    "source_last_edit_ms": 123,
                }
            )
        )
        assert resilience.snapshot_is_current(data, metadata, ids, 123)
        assert not resilience.snapshot_is_current(data, metadata, ids, 124)
        assert not resilience.snapshot_is_current(data, metadata, ids + [20], 123)


if __name__ == "__main__":
    test_request_recovers_from_dns_failure()
    test_download_resumes_at_failed_batch()
    test_snapshot_revision_check()
    print("CMOS ARCGIS RESILIENCE TESTS: PASS")
