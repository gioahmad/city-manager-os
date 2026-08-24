#!/usr/bin/env python3
"""Download county-selectable NJOGIS parcel snapshots as local GeoJSON.

Examples:
  python3 download_parcels.py --counties HUDSON
  python3 download_parcels.py --counties HUDSON,BERGEN,PASSAIC
  python3 download_parcels.py --counties ALL

The script uses the NJOGIS statewide parcel FeatureServer only as a refresh
source. Runtime City Manager OS GIS queries should use local PostGIS data.
"""

from __future__ import annotations

import argparse
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

LAYER_URL = (
    "https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/ArcGIS/rest/services/"
    "Parcels_Composite_NJ_WM/FeatureServer/0"
)
QUERY_URL = f"{LAYER_URL}/query"
DEFAULT_OUTPUT = Path("/opt/citymanager-data/gis/raw/parcels")
USER_AGENT = "CityManagerOS-GIS/0.1"


def request_json(url: str, params: dict[str, str], *, post: bool = True, attempts: int = 5):
    encoded = urllib.parse.urlencode(params).encode("utf-8")
    for attempt in range(1, attempts + 1):
        try:
            if post:
                req = urllib.request.Request(
                    url,
                    data=encoded,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
            else:
                req = urllib.request.Request(
                    f"{url}?{encoded.decode('utf-8')}",
                    headers={"User-Agent": USER_AGENT},
                )
            with urllib.request.urlopen(req, timeout=180) as response:
                raw = response.read()
            data = json.loads(raw)
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            if attempt == attempts:
                raise
            delay = min(2 ** attempt, 20)
            print(f"  request failed ({exc}); retrying in {delay}s...", file=sys.stderr)
            time.sleep(delay)


def layer_metadata() -> dict:
    return request_json(LAYER_URL, {"f": "json"}, post=False)


def available_counties() -> list[str]:
    data = request_json(
        QUERY_URL,
        {
            "where": "1=1",
            "outFields": "COUNTY",
            "returnDistinctValues": "true",
            "returnGeometry": "false",
            "orderByFields": "COUNTY",
            "f": "json",
        },
    )
    counties = []
    for feature in data.get("features", []):
        county = (feature.get("attributes") or {}).get("COUNTY")
        if county:
            counties.append(str(county).strip().upper())
    return sorted(set(counties))


def parse_counties(raw: str, valid: list[str]) -> list[str]:
    requested = [part.strip().upper() for part in raw.split(",") if part.strip()]
    if not requested:
        raise SystemExit("No counties supplied.")
    if requested == ["ALL"]:
        return valid

    invalid = sorted(set(requested) - set(valid))
    if invalid:
        raise SystemExit(
            "Unknown county value(s): "
            + ", ".join(invalid)
            + "\nValid values: "
            + ", ".join(valid)
        )
    return list(dict.fromkeys(requested))


def object_ids_for_county(county: str) -> list[int]:
    safe = county.replace("'", "''")
    data = request_json(
        QUERY_URL,
        {
            "where": f"COUNTY='{safe}'",
            "returnIdsOnly": "true",
            "f": "json",
        },
    )
    return sorted(data.get("objectIds") or [])


def chunks(values: list[int], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_county(county: str, output_dir: Path, oid_field: str, batch_size: int) -> None:
    ids = object_ids_for_county(county)
    expected = len(ids)
    if not ids:
        raise RuntimeError(f"No parcel object IDs returned for {county}")

    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = output_dir / f"{county.lower()}_parcels_mod4.geojson"
    temp_path = final_path.with_suffix(final_path.suffix + ".part")

    print(f"\n=== {county} ===")
    print(f"Parcels reported by NJOGIS: {expected:,}")
    print(f"Output: {final_path}")

    written = 0
    first_feature = True

    with temp_path.open("w", encoding="utf-8") as out:
        out.write('{"type":"FeatureCollection","features":[')

        total_batches = (expected + batch_size - 1) // batch_size
        for batch_no, batch in enumerate(chunks(ids, batch_size), start=1):
            data = request_json(
                QUERY_URL,
                {
                    "objectIds": ",".join(str(value) for value in batch),
                    "outFields": "*",
                    "returnGeometry": "true",
                    "outSR": "4326",
                    "f": "geojson",
                },
            )
            features = data.get("features") or []
            if len(features) != len(batch):
                raise RuntimeError(
                    f"Batch {batch_no} returned {len(features)} features for "
                    f"{len(batch)} requested object IDs"
                )

            for feature in features:
                if not first_feature:
                    out.write(",")
                json.dump(feature, out, ensure_ascii=False, separators=(",", ":"))
                first_feature = False
                written += 1

            print(
                f"  batch {batch_no}/{total_batches}: "
                f"{written:,}/{expected:,}",
                flush=True,
            )

        out.write("]}")
        out.flush()
        os.fsync(out.fileno())

    if written != expected:
        raise RuntimeError(f"Validation failed: wrote {written}, expected {expected}")

    temp_path.replace(final_path)
    checksum = sha256_file(final_path)
    metadata_path = output_dir / f"{county.lower()}_parcels_mod4.metadata.json"
    metadata = {
        "dataset": "NJOGIS Parcels Composite NJ WM",
        "source_layer": LAYER_URL,
        "county": county,
        "object_id_field": oid_field,
        "feature_count": written,
        "crs": "EPSG:4326",
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "sha256": checksum,
        "file": final_path.name,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    size_mb = final_path.stat().st_size / (1024 * 1024)
    print(f"Completed {county}: {written:,} parcels, {size_mb:.1f} MiB")
    print(f"SHA256: {checksum}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Download NJOGIS parcel snapshots by county")
    parser.add_argument(
        "--counties",
        default=os.getenv("COUNTIES", "HUDSON"),
        help="Comma-separated county names or ALL (default: HUDSON)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output directory (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    print("=== NJOGIS PARCEL DOWNLOADER ===")
    metadata = layer_metadata()
    oid_field = metadata.get("objectIdField") or metadata.get("objectIdFieldName") or "OBJECTID"
    max_record_count = int(metadata.get("maxRecordCount") or 2000)
    batch_size = min(max_record_count, 2000)

    valid = available_counties()
    selected = parse_counties(args.counties, valid)

    print("Available counties:", ", ".join(valid))
    print("Selected counties:", ", ".join(selected))
    print("Object ID field:", oid_field)
    print("Batch size:", batch_size)

    for county in selected:
        download_county(county, args.output_dir, oid_field, batch_size)

    print("\nAll requested counties completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
