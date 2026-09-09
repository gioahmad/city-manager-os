"""Reliable, resumable ArcGIS snapshot download helpers.

These helpers are used only by scheduled dataset refresh jobs. Runtime City
Manager OS resolution remains local to PostGIS.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


DEFAULT_ATTEMPTS = 20
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_MAX_DELAY_SECONDS = 300
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
CHECKPOINT_VERSION = 1


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def request_json(
    url: str,
    params: dict[str, str],
    *,
    post: bool = True,
    attempts: int | None = None,
) -> Any:
    """Request ArcGIS JSON with bounded recovery for transient source failures."""
    attempts = attempts or _positive_env("GIS_ARCGIS_ATTEMPTS", DEFAULT_ATTEMPTS)
    timeout = _positive_env("GIS_ARCGIS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    max_delay = _positive_env("GIS_ARCGIS_MAX_DELAY_SECONDS", DEFAULT_MAX_DELAY_SECONDS)
    encoded = urllib.parse.urlencode(params).encode("utf-8")

    for attempt in range(1, attempts + 1):
        try:
            if post:
                request = urllib.request.Request(
                    url,
                    data=encoded,
                    headers={
                        "User-Agent": "CityManagerOS-GIS/0.2",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
            else:
                request = urllib.request.Request(
                    f"{url}?{encoded.decode('utf-8')}",
                    headers={"User-Agent": "CityManagerOS-GIS/0.2"},
                )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            data = json.loads(raw)
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_CODES:
                raise
            error: BaseException = exc
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:
            error = exc

        if attempt == attempts:
            raise RuntimeError(
                f"ArcGIS request failed after {attempts} attempts: {error}"
            ) from error

        delay = min(2 ** attempt, max_delay)
        print(
            f"  request attempt {attempt}/{attempts} failed ({error}); "
            f"retrying in {delay}s...",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)

    raise AssertionError("unreachable")


def object_ids_sha256(ids: Iterable[int]) -> str:
    digest = hashlib.sha256()
    for object_id in ids:
        digest.update(str(int(object_id)).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def snapshot_is_current(
    data_path: Path,
    metadata_path: Path,
    ids: list[int],
    source_last_edit_ms: int | None,
) -> bool:
    """Return true only for a complete snapshot of the same source revision."""
    if not data_path.is_file() or data_path.stat().st_size <= 0:
        return False
    if not metadata_path.is_file():
        return False
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if int(metadata.get("feature_count") or 0) != len(ids):
        return False
    if metadata.get("object_ids_sha256") != object_ids_sha256(ids):
        return False
    recorded_edit = metadata.get("source_last_edit_ms")
    if source_last_edit_ms is not None and recorded_edit != source_last_edit_ms:
        return False
    return True


def _write_checkpoint(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _retire_incompatible(paths: Iterable[Path]) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for path in paths:
        if path.exists():
            path.replace(path.with_name(f"{path.name}.stale.{stamp}"))


def download_feature_collection(
    *,
    ids: list[int],
    batch_size: int,
    query_url: str,
    query_parameters: dict[str, str],
    temporary_path: Path,
    label: str,
    request: Callable[..., Any] = request_json,
) -> int:
    """Download a FeatureCollection with an fsynced checkpoint per batch."""
    if not ids:
        raise RuntimeError(f"{label}: no object IDs supplied")
    if batch_size <= 0:
        raise RuntimeError("batch_size must be positive")

    temporary_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = Path(f"{temporary_path}.checkpoint.json")
    fingerprint = object_ids_sha256(ids)
    expected = len(ids)
    batches = [ids[start : start + batch_size] for start in range(0, expected, batch_size)]
    header = b'{"type":"FeatureCollection","features":['
    state: dict[str, Any] | None = None

    if temporary_path.exists() and checkpoint_path.exists():
        try:
            candidate = json.loads(checkpoint_path.read_text())
            valid = (
                candidate.get("version") == CHECKPOINT_VERSION
                and candidate.get("object_ids_sha256") == fingerprint
                and candidate.get("expected") == expected
                and candidate.get("batch_size") == batch_size
                and 0 <= int(candidate.get("next_batch_index", -1)) <= len(batches)
                and 0 <= int(candidate.get("written", -1)) <= expected
                and len(header) <= int(candidate.get("part_size", -1)) <= temporary_path.stat().st_size
            )
            if valid:
                state = candidate
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            state = None
        if state is None:
            _retire_incompatible((temporary_path, checkpoint_path))
    elif temporary_path.exists() or checkpoint_path.exists():
        _retire_incompatible((temporary_path, checkpoint_path))

    if state is None:
        with temporary_path.open("wb") as output:
            output.write(header)
            output.flush()
            os.fsync(output.fileno())
        state = {
            "version": CHECKPOINT_VERSION,
            "object_ids_sha256": fingerprint,
            "expected": expected,
            "batch_size": batch_size,
            "next_batch_index": 0,
            "written": 0,
            "part_size": len(header),
        }
        _write_checkpoint(checkpoint_path, state)
    else:
        with temporary_path.open("r+b") as output:
            output.truncate(int(state["part_size"]))
            output.flush()
            os.fsync(output.fileno())
        print(
            f"Resuming {label} at batch {int(state['next_batch_index']) + 1}/"
            f"{len(batches)} after {int(state['written']):,} features",
            flush=True,
        )

    written = int(state["written"])
    start_index = int(state["next_batch_index"])
    with temporary_path.open("ab") as output:
        for batch_index in range(start_index, len(batches)):
            batch = batches[batch_index]
            parameters = dict(query_parameters)
            parameters["objectIds"] = ",".join(str(value) for value in batch)
            data = request(query_url, parameters)
            features = data.get("features") or []
            if len(features) != len(batch):
                raise RuntimeError(
                    f"{label} batch {batch_index + 1} returned {len(features)} "
                    f"features for {len(batch)} requested object IDs"
                )

            for feature in features:
                if written:
                    output.write(b",")
                output.write(
                    json.dumps(
                        feature,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                written += 1
            output.flush()
            os.fsync(output.fileno())
            state.update(
                next_batch_index=batch_index + 1,
                written=written,
                part_size=output.tell(),
            )
            _write_checkpoint(checkpoint_path, state)
            print(
                f"  batch {batch_index + 1}/{len(batches)}: "
                f"{written:,}/{expected:,}",
                flush=True,
            )

        output.write(b"]}")
        output.flush()
        os.fsync(output.fileno())

    if written != expected:
        raise RuntimeError(f"{label}: wrote {written}, expected {expected}")
    checkpoint_path.unlink()
    return written
