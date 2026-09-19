#!/usr/bin/env python3
"""Load official NJ boundary and major-route geometry into existing Mapping Center tables."""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


PAGE_SIZE = 2_000


@dataclass(frozen=True)
class LayerSource:
    key: str
    name: str
    url: str
    owner: str
    color: str
    sort_order: int
    kind: str
    minimum: int
    maximum: int
    final_minimum: int
    final_maximum: int


SOURCES = (
    LayerSource(
        "NJ_OFFICIAL_MUNICIPALITIES",
        "New Jersey Municipalities",
        "https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/NJ_Municipalities_3857/FeatureServer/0",
        "New Jersey Office of GIS",
        "#2f7d5c",
        30,
        "municipality",
        560,
        570,
        560,
        570,
    ),
    LayerSource(
        "NJ_OFFICIAL_COUNTIES",
        "New Jersey Counties",
        "https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/NJ_Counties_3857/FeatureServer/0",
        "New Jersey Office of GIS",
        "#7a58a6",
        31,
        "county",
        21,
        21,
        21,
        21,
    ),
    LayerSource(
        "NJDOT_MAJOR_HIGHWAYS",
        "New Jersey Major Highways",
        "https://services.arcgis.com/HggmsDF7UJsNN1FK/arcgis/rest/services/New_Jersey_DOT_Roadway_Network/FeatureServer/1",
        "New Jersey Department of Transportation",
        "#c06c24",
        32,
        "major_highway",
        500,
        600,
        250,
        350,
    ),
)


def _request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "City-Manager-OS/1.0"})
    error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status != 200:
                    raise RuntimeError(f"official GIS service returned HTTP {response.status}")
                payload = json.load(response)
                if not isinstance(payload, dict) or payload.get("error"):
                    raise RuntimeError("official GIS service returned an error payload")
                return payload
        except Exception as exc:  # pragma: no cover - exercised against live services
            error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    raise RuntimeError(f"official GIS service request failed: {type(error).__name__}") from error


def _query_url(source: LayerSource, **params: Any) -> str:
    return f"{source.url}/query?{urllib.parse.urlencode(params)}"


def _value(properties: dict[str, Any], *names: str) -> str:
    for name in names:
        value = properties.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def canonical_feature(source: LayerSource, feature: dict[str, Any]) -> dict[str, Any]:
    properties = feature.get("properties") or {}
    geometry = feature.get("geometry")
    if not isinstance(properties, dict) or not isinstance(geometry, dict):
        raise ValueError(f"{source.name} returned a feature without properties or geometry")

    geometry_type = str(geometry.get("type") or "")
    if source.kind in {"municipality", "county"} and "Polygon" not in geometry_type:
        raise ValueError(f"{source.name} returned non-polygon geometry")
    if source.kind == "major_highway" and "LineString" not in geometry_type:
        raise ValueError(f"{source.name} returned non-line geometry")

    if source.kind == "municipality":
        name = _value(properties, "NAME", "MUN_LABEL", "GNIS_NAME")
        feature_key = _value(properties, "MUN_CODE", "GNIS", "OBJECTID")
        canonical = {
            "name": name,
            "municipality": name,
            "county": _value(properties, "COUNTY"),
            "state": "NJ",
            "kind": "municipality",
            "municipal_code": _value(properties, "MUN_CODE"),
            "population_2020": properties.get("POP2020"),
        }
    elif source.kind == "county":
        name = _value(properties, "COUNTY", "COUNTY_LABEL", "GNIS_NAME")
        feature_key = _value(properties, "FIPSSTCO", "FIPSCO", "OBJECTID")
        canonical = {
            "name": name,
            "county": name,
            "state": "NJ",
            "kind": "county",
            "fips": _value(properties, "FIPSSTCO", "FIPSCO"),
            "population_2020": properties.get("POP2020"),
        }
    else:
        name = _value(properties, "SLD_NAME", "ROAD_NUM", "SRI")
        feature_key = name.upper()
        canonical = {
            "name": name,
            "route_name": name,
            "highway": name,
            "state": "NJ",
            "kind": "major_highway",
            "road_number": _value(properties, "ROAD_NUM"),
            "sri": _value(properties, "SRI"),
        }

    if not name or not feature_key:
        raise ValueError(f"{source.name} returned an unnamed feature")
    canonical.update(
        {
            "_source_owner": source.owner,
            "_source_service": source.url,
            "_source_objectid": properties.get("OBJECTID"),
        }
    )
    return {
        "source_key": source.key,
        "feature_key": feature_key,
        "name": name,
        "properties": canonical,
        "geometry": geometry,
    }


def fetch_source(source: LayerSource) -> list[dict[str, Any]]:
    count_payload = _request_json(
        _query_url(source, where="1=1", returnCountOnly="true", f="json")
    )
    expected = int(count_payload.get("count") or 0)
    if not source.minimum <= expected <= source.maximum:
        raise RuntimeError(
            f"{source.name} count {expected} is outside {source.minimum}..{source.maximum}"
        )

    features: list[dict[str, Any]] = []
    while len(features) < expected:
        payload = _request_json(
            _query_url(
                source,
                where="1=1",
                outFields="*",
                returnGeometry="true",
                outSR=4326,
                orderByFields="OBJECTID",
                resultOffset=len(features),
                resultRecordCount=min(PAGE_SIZE, expected - len(features)),
                f="geojson",
            )
        )
        page = payload.get("features")
        if payload.get("type") != "FeatureCollection" or not isinstance(page, list) or not page:
            raise RuntimeError(f"{source.name} returned an incomplete GeoJSON page")
        features.extend(page)

    if len(features) != expected:
        raise RuntimeError(f"{source.name} returned {len(features)} of {expected} features")
    return [canonical_feature(source, feature) for feature in features]


def _predicted_locations(source: LayerSource, features: list[dict[str, Any]]) -> int:
    count = len({feature["feature_key"] for feature in features})
    if not source.final_minimum <= count <= source.final_maximum:
        raise RuntimeError(
            f"{source.name} produced {count} locations outside "
            f"{source.final_minimum}..{source.final_maximum}"
        )
    return count


def apply_layers(downloads: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    from app import db_conn

    results: list[dict[str, Any]] = []
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TEMP TABLE cmos_watch_layer_stage(
                  source_key text NOT NULL,
                  feature_key text NOT NULL,
                  name text NOT NULL,
                  properties jsonb NOT NULL,
                  geom geometry(Geometry,4326) NOT NULL
                ) ON COMMIT DROP;
                CREATE TEMP TABLE cmos_watch_layer_final(
                  id uuid PRIMARY KEY,
                  source_key text NOT NULL,
                  name text NOT NULL,
                  properties jsonb NOT NULL,
                  geom geometry(Geometry,4326) NOT NULL
                ) ON COMMIT DROP;
                """
            )
            for source in SOURCES:
                rows = downloads[source.key]
                cur.executemany(
                    """
                    INSERT INTO cmos_watch_layer_stage(source_key,feature_key,name,properties,geom)
                    SELECT %s,%s,%s,%s::jsonb,
                           ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))
                    """,
                    [
                        (
                            row["source_key"],
                            row["feature_key"],
                            row["name"],
                            json.dumps(row["properties"], default=str),
                            json.dumps(row["geometry"]),
                        )
                        for row in rows
                    ],
                )
                cur.execute(
                    """
                    INSERT INTO map_layers(
                      layer_key,name,layer_type,source_url,attribution,style,
                      active,default_visible,sort_order,updated_at
                    ) VALUES(%s,%s,'CUSTOM_GEOJSON',%s,%s,%s::jsonb,true,false,%s,now())
                    ON CONFLICT(layer_key) DO UPDATE SET
                      name=excluded.name,source_url=excluded.source_url,
                      attribution=excluded.attribution,style=excluded.style,
                      active=true,sort_order=excluded.sort_order,updated_at=now()
                    """,
                    (
                        source.key,
                        source.name,
                        source.url,
                        source.owner,
                        json.dumps({"color": source.color, "weight": 2, "fillOpacity": 0.08}),
                        source.sort_order,
                    ),
                )

            polygon_keys = [source.key for source in SOURCES if source.kind != "major_highway"]
            cur.execute(
                """
                INSERT INTO cmos_watch_layer_final(id,source_key,name,properties,geom)
                SELECT (md5(source_key || ':' || feature_key))::uuid,source_key,name,properties,
                       ST_Multi(ST_CollectionExtract(ST_MakeValid(geom),3))
                FROM cmos_watch_layer_stage
                WHERE source_key=ANY(%s::text[])
                """,
                (polygon_keys,),
            )
            cur.execute(
                """
                INSERT INTO cmos_watch_layer_final(id,source_key,name,properties,geom)
                SELECT (md5(source_key || ':' || feature_key))::uuid,source_key,max(name),
                       (jsonb_agg(properties ORDER BY name)->0)
                         || jsonb_build_object('segment_count',count(*)),
                       ST_LineMerge(ST_UnaryUnion(ST_Collect(geom)))
                FROM cmos_watch_layer_stage
                WHERE source_key='NJDOT_MAJOR_HIGHWAYS'
                GROUP BY source_key,feature_key
                """
            )
            cur.execute(
                """
                INSERT INTO map_features(id,layer_id,name,properties,geom,active,updated_at)
                SELECT f.id,l.id,f.name,f.properties,f.geom,true,now()
                FROM cmos_watch_layer_final f
                JOIN map_layers l ON l.layer_key=f.source_key
                WHERE f.geom IS NOT NULL AND NOT ST_IsEmpty(f.geom)
                ON CONFLICT(id) DO UPDATE SET
                  layer_id=excluded.layer_id,name=excluded.name,properties=excluded.properties,
                  geom=excluded.geom,active=true,updated_at=now()
                """
            )
            cur.execute(
                """
                UPDATE map_features existing
                SET active=false,updated_at=now()
                FROM map_layers layer
                WHERE existing.layer_id=layer.id
                  AND layer.layer_key=ANY(%s::text[])
                  AND existing.active=true
                  AND NOT EXISTS (
                    SELECT 1 FROM cmos_watch_layer_final current
                    WHERE current.source_key=layer.layer_key AND current.id=existing.id
                  )
                """,
                ([source.key for source in SOURCES],),
            )
            cur.execute(
                """
                SELECT l.layer_key,count(f.id) AS locations
                FROM map_layers l
                LEFT JOIN map_features f ON f.layer_id=l.id AND f.active=true
                WHERE l.layer_key=ANY(%s::text[]) AND l.active=true
                GROUP BY l.layer_key
                """,
                ([source.key for source in SOURCES],),
            )
            actual = {row["layer_key"]: int(row["locations"]) for row in cur.fetchall()}
            for source in SOURCES:
                count = actual.get(source.key, 0)
                if not source.final_minimum <= count <= source.final_maximum:
                    raise RuntimeError(f"{source.name} committed location count is invalid: {count}")
                results.append(
                    {
                        "layer_key": source.key,
                        "downloaded": len(downloads[source.key]),
                        "locations": count,
                    }
                )
        conn.commit()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="upsert the validated layers")
    args = parser.parse_args()

    downloads = {source.key: fetch_source(source) for source in SOURCES}
    predicted = {
        source.key: _predicted_locations(source, downloads[source.key]) for source in SOURCES
    }
    results = (
        apply_layers(downloads)
        if args.apply
        else [
            {
                "layer_key": source.key,
                "downloaded": len(downloads[source.key]),
                "locations": predicted[source.key],
            }
            for source in SOURCES
        ]
    )
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "check",
                "layers": results,
                "total_locations": sum(row["locations"] for row in results),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
