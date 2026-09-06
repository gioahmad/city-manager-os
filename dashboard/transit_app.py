
from __future__ import annotations

import json
import uuid
from datetime import date, datetime

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from schedule_app import app
from app import execute, query_all, query_one, templates


LEVELS = {"AWARENESS", "WATCH", "ALERT"}
TARGET_TYPES = ["PROVIDER", "MODE", "ROUTE", "LINE", "STOP", "STATION", "TERMINAL", "CORRIDOR", "AREA"]


def _feature_collection(rows):
    features = []
    for row in rows:
        geom = row.get("geometry")
        if not geom:
            continue
        props = {k: v for k, v in row.items() if k != "geometry"}
        for key, value in list(props.items()):
            if isinstance(value, uuid.UUID):
                props[key] = str(value)
            elif isinstance(value, (date, datetime)):
                props[key] = value.isoformat()
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": features}


@app.get("/transit", response_class=HTMLResponse)
def transit_center(
    request: Request,
    level: str = "all",
    provider: str = "all",
    q: str = "",
    msg: str = "",
):
    where = ["o.active=true"]
    params = []

    level = level.lower().strip()
    if level in {"awareness", "watch", "alert"}:
        where.append("o.impact_level=%s")
        params.append(level.upper())

    provider = provider.strip().upper()
    if provider and provider != "ALL":
        where.append("p.provider_key=%s")
        params.append(provider)

    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "("
            "o.title ILIKE %s OR o.description ILIKE %s OR o.route_name ILIKE %s "
            "OR o.asset_name ILIKE %s OR o.municipality ILIKE %s OR p.name ILIKE %s"
            ")"
        )
        params.extend([needle] * 6)

    observations = query_all(
        f"""
        SELECT o.*,p.provider_key,p.name AS provider_name,i.integration_key,i.name AS integration_name,
               o.starts_at AT TIME ZONE 'America/New_York' AS starts_local,
               o.ends_at AT TIME ZONE 'America/New_York' AS ends_local
        FROM transit_observations o
        JOIN transit_providers p ON p.id=o.provider_id
        LEFT JOIN integrations i ON i.id=o.source_integration_id
        WHERE {' AND '.join(where)}
        ORDER BY
          CASE o.impact_level WHEN 'ALERT' THEN 0 WHEN 'WATCH' THEN 1 ELSE 2 END,
          o.impact_score DESC,o.last_changed_at DESC
        LIMIT 400
        """,
        params,
    )

    metrics = query_one(
        """
        SELECT
          count(*) FILTER (WHERE active AND impact_level='ALERT') AS alerts,
          count(*) FILTER (WHERE active AND impact_level='WATCH') AS watches,
          count(*) FILTER (WHERE active AND impact_level='AWARENESS') AS awareness,
          count(*) FILTER (WHERE active AND last_seen_at >= now()-interval '60 minutes') AS fresh
        FROM transit_observations
        """
    )
    metrics["assets"] = query_one("SELECT count(*) AS n FROM transit_assets WHERE active=true").get("n", 0)
    metrics["watched"] = query_one("SELECT count(*) AS n FROM transit_watch_config WHERE active=true").get("n", 0)

    providers = query_all(
        """
        SELECT p.*,
               count(DISTINCT a.id) FILTER (WHERE a.active) AS asset_count,
               count(DISTINCT o.id) FILTER (WHERE o.active AND o.impact_level IN ('WATCH','ALERT')) AS active_impact_count
        FROM transit_providers p
        LEFT JOIN transit_assets a ON a.provider_id=p.id
        LEFT JOIN transit_observations o ON o.provider_id=p.id
        GROUP BY p.id
        ORDER BY p.phase,p.name
        """
    )

    integrations = query_all(
        """
        SELECT i.id,i.integration_key,i.name,i.active,i.adapter_type,i.endpoint_url,i.poll_seconds,
               r.status AS last_run_status,r.started_at AS last_run_at,r.error_message AS last_error,
               h.status AS health_status,h.last_success_at,h.updated_at AS health_updated_at
        FROM integrations i
        LEFT JOIN LATERAL (
          SELECT status,started_at,error_message
          FROM integration_runs r
          WHERE r.integration_id=i.id
          ORDER BY started_at DESC LIMIT 1
        ) r ON true
        LEFT JOIN source_health h ON h.source_id='INT:' || i.integration_key
        WHERE i.category='TRANSIT'
        ORDER BY i.integration_key
        """
    )

    watches = query_all(
        """
        SELECT w.*,p.provider_key,p.name AS provider_name
        FROM transit_watch_config w
        LEFT JOIN transit_providers p ON p.id=w.provider_id
        ORDER BY w.active DESC,w.display_name
        """
    )

    event_context = query_all(
        """
        SELECT id,title,starts_at,venue,municipality,impact_level,impact_score,transit_impact,source_url
        FROM event_intelligence
        WHERE active=true
          AND COALESCE(ends_at,starts_at,now()) >= now()-interval '2 hours'
          AND COALESCE(starts_at,now()) <= now()+interval '48 hours'
          AND (
            NULLIF(trim(COALESCE(transit_impact,'')),'') IS NOT NULL
            OR impact_score >= 45
          )
        ORDER BY impact_score DESC,starts_at NULLS FIRST
        LIMIT 12
        """
    )

    return templates.TemplateResponse(
        request=request,
        name="transit.html",
        context={
            "page": "transit",
            "msg": msg,
            "level": level,
            "provider_filter": provider,
            "q": q,
            "observations": observations,
            "metrics": metrics,
            "providers": providers,
            "integrations": integrations,
            "watches": watches,
            "event_context": event_context,
            "target_types": TARGET_TYPES,
            "levels": sorted(LEVELS),
        },
    )


@app.post("/transit/watch/create")
def transit_watch_create(
    provider_key: str = Form("NJ_TRANSIT"),
    target_type: str = Form(...),
    target_key: str = Form(...),
    display_name: str = Form(...),
    municipality: str = Form(""),
    corridor: str = Form(""),
    min_impact_level: str = Form("WATCH"),
    notes: str = Form(""),
):
    target_type = target_type.strip().upper()
    min_impact_level = min_impact_level.strip().upper()
    target_key = target_key.strip().upper()
    display_name = display_name.strip()
    if target_type not in TARGET_TYPES:
        raise HTTPException(400, "Invalid transit watch type")
    if min_impact_level not in LEVELS:
        raise HTTPException(400, "Invalid impact level")
    if not target_key or not display_name:
        raise HTTPException(400, "Target key and display name are required")

    provider = query_one("SELECT id FROM transit_providers WHERE provider_key=%s", (provider_key.strip().upper(),))
    if not provider:
        raise HTTPException(400, "Transit provider not found")

    execute(
        """
        INSERT INTO transit_watch_config(
          provider_id,active,target_type,target_key,display_name,municipality,corridor,min_impact_level,notes
        )
        VALUES(%s,true,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(provider_id,target_type,target_key) DO UPDATE SET
          active=true,
          display_name=EXCLUDED.display_name,
          municipality=EXCLUDED.municipality,
          corridor=EXCLUDED.corridor,
          min_impact_level=EXCLUDED.min_impact_level,
          notes=EXCLUDED.notes,
          updated_at=now()
        """,
        (
            provider["id"],
            target_type,
            target_key,
            display_name,
            municipality.strip() or None,
            corridor.strip() or None,
            min_impact_level,
            notes.strip() or None,
        ),
    )
    return RedirectResponse("/transit?msg=Transit+watch+saved", status_code=303)


@app.post("/transit/watch/{watch_id}/toggle")
def transit_watch_toggle(watch_id: uuid.UUID):
    execute(
        "UPDATE transit_watch_config SET active=NOT active,updated_at=now() WHERE id=%s",
        (watch_id,),
    )
    return RedirectResponse("/transit?msg=Transit+watch+updated", status_code=303)


@app.post("/transit/{observation_id}/level")
def transit_level(observation_id: uuid.UUID, impact_level: str = Form(...)):
    impact_level = impact_level.strip().upper()
    if impact_level not in LEVELS:
        raise HTTPException(400, "Invalid impact level")
    execute(
        """
        UPDATE transit_observations
        SET impact_level=%s,
            impact_score=CASE
              WHEN %s='ALERT' AND impact_score<75 THEN 75
              WHEN %s='WATCH' AND impact_score<45 THEN 45
              ELSE impact_score
            END,
            alert_pending=CASE WHEN %s='ALERT' THEN true ELSE false END,
            updated_at=now()
        WHERE id=%s
        """,
        (impact_level, impact_level, impact_level, impact_level, observation_id),
    )
    return RedirectResponse("/transit?msg=Transit+impact+level+updated", status_code=303)


@app.post("/transit/{observation_id}/create-action")
def transit_create_action(observation_id: uuid.UUID):
    execute(
        """
        INSERT INTO issues(
          title,description,category,priority,status,source,municipality,item_type,transit_observation_id
        )
        SELECT
          'Transit response: ' || o.title,
          concat_ws(E'\n',o.description,o.route_name,o.asset_name,o.source_url),
          'TRANSIT',
          CASE o.impact_level WHEN 'ALERT' THEN 5 WHEN 'WATCH' THEN 4 ELSE 3 END,
          'OPEN',
          'TRANSIT_INTELLIGENCE',
          o.municipality,
          'TASK',
          o.id
        FROM transit_observations o
        WHERE o.id=%s
          AND NOT EXISTS(
            SELECT 1 FROM issues i
            WHERE i.transit_observation_id=o.id AND i.status NOT IN ('RESOLVED','CLOSED')
          )
        """,
        (observation_id,),
    )
    return RedirectResponse("/issues?msg=Transit+action+ready", status_code=303)


@app.get("/map/system/transit.geojson")
def transit_map_geojson():
    rows = query_all(
        """
        SELECT
          p.provider_key,
          a.asset_key,
          a.asset_type,
          a.mode,
          a.name,
          a.short_name,
          a.updated_at,
          CASE WHEN a.asset_type='VEHICLE' THEN 'LIVE ASSET' ELSE 'REFERENCE' END AS map_state,
          ST_AsGeoJSON(a.geom)::json AS geometry
        FROM transit_assets a
        JOIN transit_providers p ON p.id=a.provider_id
        WHERE a.active=true
          AND a.geom IS NOT NULL
          AND (
            a.asset_type <> 'VEHICLE'
            OR a.last_seen_at >= now()-interval '20 minutes'
          )
        ORDER BY CASE WHEN a.asset_type='VEHICLE' THEN 0 ELSE 1 END,a.name
        LIMIT 5000
        """
    )

    obs = query_all(
        """
        SELECT
          p.provider_key,
          'OBS:' || o.id::text AS asset_key,
          'OBSERVATION' AS asset_type,
          o.mode,
          o.title AS name,
          o.impact_level AS short_name,
          o.updated_at,
          o.impact_level AS map_state,
          ST_AsGeoJSON(o.geom)::json AS geometry
        FROM transit_observations o
        JOIN transit_providers p ON p.id=o.provider_id
        WHERE o.active=true AND o.geom IS NOT NULL
          AND o.impact_level IN ('WATCH','ALERT')
        ORDER BY o.impact_score DESC,o.updated_at DESC
        LIMIT 1000
        """
    )
    return JSONResponse(_feature_collection(rows + obs), media_type="application/geo+json")
