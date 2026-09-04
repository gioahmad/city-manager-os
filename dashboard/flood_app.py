import json

from fastapi import Request
from fastapi.responses import HTMLResponse

from schedule_app import app
from app import query_all, query_one, templates


@app.get("/flood", response_class=HTMLResponse)
def flood_page(request: Request):
    zones = query_all(
        """
        SELECT id,fld_zone,zone_subty,sfha_tf,static_bfe,
               ST_AsGeoJSON(geom)::json AS geometry
        FROM gis_flood_zones
        ORDER BY CASE WHEN sfha_tf='T' THEN 0 ELSE 1 END,fld_zone,id
        """
    )
    zone_counts = query_all(
        """
        SELECT coalesce(fld_zone,'UNKNOWN') AS fld_zone,
               coalesce(zone_subty,'') AS zone_subty,
               coalesce(sfha_tf,'') AS sfha_tf,
               count(*) AS polygons
        FROM gis_flood_zones
        GROUP BY 1,2,3
        ORDER BY CASE WHEN coalesce(sfha_tf,'')='T' THEN 0 ELSE 1 END,1,2
        """
    )
    watched = query_all("SELECT * FROM gis_watch_items_in_flood_zones() LIMIT 200")
    latest = query_one(
        """
        SELECT source,station_id,observed_at AT TIME ZONE 'America/New_York' AS observed_local,
               water_level_mhhw_ft,predicted_level_mhhw_ft,flood_category,title
        FROM flood_observations
        ORDER BY observed_at DESC
        LIMIT 1
        """
    )
    flood_alerts = query_all(
        """
        SELECT source,title,message,priority,status,received_at AT TIME ZONE 'America/New_York' AS received_local
        FROM alerts
        WHERE source IN ('NWS_FLOOD','NOAA_TIDE')
          AND status <> 'RESOLVED'
          AND (expires_at IS NULL OR expires_at > now())
        ORDER BY priority DESC,received_at DESC
        LIMIT 20
        """
    )
    features = []
    for z in zones:
        features.append({
            "type": "Feature",
            "geometry": z["geometry"],
            "properties": {
                "id": z["id"],
                "fld_zone": z["fld_zone"],
                "zone_subty": z["zone_subty"],
                "sfha_tf": z["sfha_tf"],
                "static_bfe": z["static_bfe"],
            },
        })
    return templates.TemplateResponse(
        request=request,
        name="flood.html",
        context={
            "page": "flood",
            "zone_counts": zone_counts,
            "watched": watched,
            "latest": latest,
            "flood_alerts": flood_alerts,
            "geojson": json.dumps({"type": "FeatureCollection", "features": features}),
        },
    )
