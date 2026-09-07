from __future__ import annotations

import csv
import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import shapefile
from pyproj import CRS, Transformer

MAX_FEATURES = 5000
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = 60 * 1024 * 1024


def _coords_look_lonlat(bounds: list[float] | tuple[float, ...] | None) -> bool:
    if not bounds or len(bounds) != 4:
        return False
    xmin, ymin, xmax, ymax = [float(v) for v in bounds]
    return -180 <= xmin <= 180 and -180 <= xmax <= 180 and -90 <= ymin <= 90 and -90 <= ymax <= 90


def _transform_coordinates(value: Any, transformer: Transformer) -> Any:
    if not isinstance(value, list):
        return value
    if value and isinstance(value[0], (int, float)):
        if len(value) < 2:
            return value
        x, y = transformer.transform(float(value[0]), float(value[1]))
        return [x, y, *value[2:]]
    return [_transform_coordinates(item, transformer) for item in value]


def _transform_geometry(geometry: dict[str, Any], transformer: Transformer) -> dict[str, Any]:
    output = dict(geometry)
    if output.get("type") == "GeometryCollection":
        output["geometries"] = [
            _transform_geometry(item, transformer)
            for item in output.get("geometries") or []
        ]
    elif "coordinates" in output:
        output["coordinates"] = _transform_coordinates(output["coordinates"], transformer)
    return output


def _normalize_features(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ptype = payload.get("type")
    if ptype == "FeatureCollection":
        features = payload.get("features") or []
    elif ptype == "Feature":
        features = [payload]
    else:
        features = [{"type": "Feature", "geometry": payload, "properties": {}}]
    if len(features) > MAX_FEATURES:
        raise ValueError(f"Import is limited to {MAX_FEATURES:,} features")
    return features


def read_geojson(raw: bytes) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError("Invalid GeoJSON / JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("GeoJSON root must be an object")
    return _normalize_features(payload)


def read_csv_points(raw: bytes) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV is missing a header row")

    field_lookup = {name.strip().lower(): name for name in reader.fieldnames if name}
    lat_key = next((field_lookup[k] for k in ("latitude", "lat", "y") if k in field_lookup), None)
    lon_key = next((field_lookup[k] for k in ("longitude", "lon", "lng", "long", "x") if k in field_lookup), None)
    if not lat_key or not lon_key:
        raise ValueError("CSV requires latitude/longitude columns (latitude+longitude, lat+lon/lng, or y+x)")

    features: list[dict[str, Any]] = []
    for row_number, row in enumerate(reader, start=2):
        if len(features) >= MAX_FEATURES:
            raise ValueError(f"Import is limited to {MAX_FEATURES:,} features")
        try:
            lat = float((row.get(lat_key) or "").strip())
            lon = float((row.get(lon_key) or "").strip())
        except ValueError as exc:
            raise ValueError(f"Invalid latitude/longitude on CSV row {row_number}") from exc
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"Latitude/longitude out of range on CSV row {row_number}")
        props = {str(k): v for k, v in row.items() if k is not None}
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            }
        )
    return features


def _parse_kml_coordinates(text: str | None) -> list[list[float]]:
    result: list[list[float]] = []
    for token in (text or "").replace("\n", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        result.append([float(parts[0]), float(parts[1])])
    return result


def _kml_geometry(element: ET.Element) -> dict[str, Any] | None:
    tag = element.tag.rsplit("}", 1)[-1]
    if tag == "Point":
        coords = _parse_kml_coordinates(element.findtext(".//{*}coordinates"))
        return {"type": "Point", "coordinates": coords[0]} if coords else None
    if tag == "LineString":
        coords = _parse_kml_coordinates(element.findtext(".//{*}coordinates"))
        return {"type": "LineString", "coordinates": coords} if len(coords) >= 2 else None
    if tag == "Polygon":
        rings: list[list[list[float]]] = []
        outer = element.find("./{*}outerBoundaryIs/{*}LinearRing/{*}coordinates")
        if outer is not None:
            coords = _parse_kml_coordinates(outer.text)
            if len(coords) >= 4:
                rings.append(coords)
        for inner in element.findall("./{*}innerBoundaryIs/{*}LinearRing/{*}coordinates"):
            coords = _parse_kml_coordinates(inner.text)
            if len(coords) >= 4:
                rings.append(coords)
        return {"type": "Polygon", "coordinates": rings} if rings else None
    return None


def read_kml(raw: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(raw)
    except Exception as exc:
        raise ValueError("Invalid KML") from exc

    features: list[dict[str, Any]] = []
    for placemark in root.findall(".//{*}Placemark"):
        if len(features) >= MAX_FEATURES:
            raise ValueError(f"Import is limited to {MAX_FEATURES:,} features")
        properties: dict[str, Any] = {}
        name = placemark.findtext("./{*}name")
        description = placemark.findtext("./{*}description")
        if name:
            properties["name"] = name.strip()
        if description:
            properties["description"] = description.strip()
        for data in placemark.findall(".//{*}ExtendedData/{*}Data"):
            key = (data.attrib.get("name") or "").strip()
            value = data.findtext("./{*}value")
            if key and value is not None:
                properties[key] = value

        geometries: list[dict[str, Any]] = []
        for geom_tag in ("Point", "LineString", "Polygon"):
            for node in placemark.findall(f".//{{*}}{geom_tag}"):
                geom = _kml_geometry(node)
                if geom:
                    geometries.append(geom)
        if not geometries:
            continue
        geometry = geometries[0] if len(geometries) == 1 else {"type": "GeometryCollection", "geometries": geometries}
        features.append({"type": "Feature", "geometry": geometry, "properties": properties})
    return features


def read_kmz(raw: bytes) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [name for name in zf.namelist() if name.lower().endswith(".kml")]
            if not names:
                raise ValueError("KMZ does not contain a KML file")
            info = zf.getinfo(names[0])
            if info.file_size > MAX_UPLOAD_BYTES:
                raise ValueError("KML inside KMZ is too large")
            return read_kml(zf.read(names[0]))
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid KMZ") from exc


def read_shapefile_zip(raw: bytes) -> list[dict[str, Any]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid Shapefile ZIP") from exc

    with zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if len(infos) > 100:
            raise ValueError("Shapefile ZIP contains too many files")
        if sum(info.file_size for info in infos) > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ValueError("Shapefile ZIP expands beyond the allowed size")

        by_base: dict[str, dict[str, zipfile.ZipInfo]] = {}
        for info in infos:
            filename = Path(info.filename).name
            suffix = Path(filename).suffix.lower()
            if suffix not in {".shp", ".dbf", ".shx", ".prj", ".cpg"}:
                continue
            stem = Path(filename).stem.lower()
            by_base.setdefault(stem, {})[suffix] = info

        candidates = [parts for parts in by_base.values() if {".shp", ".dbf", ".shx"}.issubset(parts)]
        if not candidates:
            raise ValueError("Shapefile ZIP must contain matching .shp, .dbf and .shx files")
        parts = candidates[0]

        with tempfile.TemporaryDirectory(prefix="cmos-shp-") as temp_dir:
            temp = Path(temp_dir)
            written: dict[str, Path] = {}
            for suffix, info in parts.items():
                target = temp / f"layer{suffix}"
                target.write_bytes(zf.read(info))
                written[suffix] = target

            reader = shapefile.Reader(str(written[".shp"]))
            transformer: Transformer | None = None
            if ".prj" in written:
                try:
                    source_crs = CRS.from_wkt(written[".prj"].read_text(errors="ignore"))
                    target_crs = CRS.from_epsg(4326)
                    if source_crs != target_crs:
                        transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
                except Exception as exc:
                    raise ValueError("Could not read Shapefile .prj coordinate system") from exc
            elif not _coords_look_lonlat(reader.bbox):
                raise ValueError("Projected Shapefiles require a .prj file so coordinates can be transformed to EPSG:4326")

            if len(reader) > MAX_FEATURES:
                raise ValueError(f"Import is limited to {MAX_FEATURES:,} features")

            field_names = [field[0] for field in reader.fields[1:]]
            features: list[dict[str, Any]] = []
            for shape_record in reader.iterShapeRecords():
                geometry = shape_record.shape.__geo_interface__
                if transformer:
                    geometry = _transform_geometry(geometry, transformer)
                record_values = list(shape_record.record)
                properties = {
                    field_names[idx]: record_values[idx]
                    for idx in range(min(len(field_names), len(record_values)))
                }
                features.append({"type": "Feature", "geometry": geometry, "properties": properties})
            return features


def read_import(filename: str, raw: bytes) -> tuple[list[dict[str, Any]], str]:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Upload is limited to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".geojson", ".json"}:
        return read_geojson(raw), "GEOJSON"
    if suffix == ".csv":
        return read_csv_points(raw), "CSV_POINTS"
    if suffix == ".kml":
        return read_kml(raw), "KML"
    if suffix == ".kmz":
        return read_kmz(raw), "KMZ"
    if suffix == ".zip":
        return read_shapefile_zip(raw), "SHAPEFILE_ZIP"
    raise ValueError("Supported imports: GeoJSON/JSON, zipped Shapefile, KML/KMZ, or CSV latitude/longitude")
