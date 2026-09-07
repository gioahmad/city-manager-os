from pathlib import Path
import io
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shapefile

from gis_import import read_csv_points, read_import, read_kml, read_shapefile_zip


def test_csv_lat_lon_import():
    raw = b"name,latitude,longitude\nTown Hall,40.769,-74.021\n"
    features = read_csv_points(raw)
    assert len(features) == 1
    assert features[0]["geometry"]["type"] == "Point"
    assert features[0]["geometry"]["coordinates"] == [-74.021, 40.769]


def test_kml_point_import():
    raw = b'''<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>Port Imperial</name><Point><coordinates>-74.011,40.776,0</coordinates></Point></Placemark></Document></kml>'''
    features = read_kml(raw)
    assert len(features) == 1
    assert features[0]["properties"]["name"] == "Port Imperial"
    assert features[0]["geometry"]["coordinates"] == [-74.011, 40.776]


def test_zipped_shapefile_lonlat_import():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "sample"
        writer = shapefile.Writer(str(base), shapeType=shapefile.POINT)
        writer.field("name", "C")
        writer.point(-74.02, 40.77)
        writer.record("Sample")
        writer.close()

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as zf:
            for suffix in (".shp", ".shx", ".dbf"):
                zf.write(base.with_suffix(suffix), arcname="sample" + suffix)

        features = read_shapefile_zip(payload.getvalue())
        assert len(features) == 1
        assert features[0]["geometry"]["type"] == "Point"
        assert features[0]["properties"]["name"] == "Sample"


def test_dispatch_rejects_unknown_format():
    try:
        read_import("data.exe", b"x")
    except ValueError as exc:
        assert "Supported imports" in str(exc)
    else:
        raise AssertionError("Unknown import format should fail")
