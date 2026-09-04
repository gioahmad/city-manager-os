#!/usr/bin/env python3
"""Download effective FEMA NFHL flood hazard zones for a supplied bbox as GeoJSON."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

LAYER_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
QUERY_URL = f"{LAYER_URL}/query"
USER_AGENT = "CityManagerOS-Flood/1.0"


def fetch(params: dict[str, str]) -> dict:
    url = QUERY_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.load(r)
    if "error" in data:
        raise RuntimeError(data["error"])
    return data


def normalized_argv() -> list[str]:
    """Allow `--bbox -74,...` as well as `--bbox=-74,...`.

    argparse can interpret a comma-delimited western-hemisphere bbox beginning with
    a minus sign as another option. Normalize that one known value before parsing.
    """
    argv = sys.argv[1:]
    try:
        i = argv.index("--bbox")
    except ValueError:
        return argv
    if i + 1 < len(argv):
        value = argv[i + 1]
        if value.startswith("-") and value.count(",") == 3:
            argv[i] = f"--bbox={value}"
            del argv[i + 1]
    return argv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", required=True, help="xmin,ymin,xmax,ymax in EPSG:4326")
    ap.add_argument("--output", required=True, type=pathlib.Path)
    args = ap.parse_args(normalized_argv())

    bbox = args.bbox.strip()
    try:
        parts = [float(x) for x in bbox.split(",")]
    except ValueError as exc:
        raise SystemExit("Invalid bbox") from exc
    if len(parts) != 4 or parts[0] >= parts[2] or parts[1] >= parts[3]:
        raise SystemExit("Invalid bbox")

    base = {
        "where": "1=1",
        "geometry": bbox,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": "1000",
    }

    features: list[dict] = []
    offset = 0
    while True:
        params = dict(base)
        params["resultOffset"] = str(offset)
        data = fetch(params)
        batch = data.get("features") or []
        features.extend(batch)
        if not data.get("exceededTransferLimit") and len(batch) < 1000:
            break
        if not batch:
            break
        offset += len(batch)

    if not features:
        raise RuntimeError("FEMA NFHL returned zero flood-zone features for bbox")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "FeatureCollection", "features": features}
    args.output.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    meta = {
        "dataset": "FEMA National Flood Hazard Layer - Flood Hazard Zones",
        "source_layer": LAYER_URL,
        "bbox": bbox,
        "feature_count": len(features),
        "crs": "EPSG:4326",
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "file": args.output.name,
    }
    args.output.with_suffix(".metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"FEMA_NFHL_FEATURES={len(features)}")
    print(f"FEMA_NFHL_OUTPUT={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
