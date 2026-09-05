from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from integration_runtime import (
    integration_auth_from_env,
    parse_events,
    perform_http_request,
    redact_text,
)


def db_conn():
    return psycopg.connect(
        host=os.getenv("DB_HOST", "citymanager-postgis"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "citymanager"),
        user=os.getenv("DB_USER", "citymanager_app"),
        password=os.environ["DB_PASSWORD"],
        row_factory=dict_row,
        connect_timeout=5,
    )


def load_integration(integration_id: str | None = None, integration_key: str | None = None) -> dict[str, Any]:
    if not integration_id and not integration_key:
        raise ValueError("integration_id or integration_key is required")
    with db_conn() as conn, conn.cursor() as cur:
        if integration_id:
            cur.execute("SELECT * FROM integrations WHERE id=%s", (integration_id,))
        else:
            cur.execute("SELECT * FROM integrations WHERE integration_key=%s", (integration_key,))
        row = cur.fetchone()
    if not row:
        raise ValueError("Integration not found")
    return row


def _json(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return dict(value)


def _record_run_start(integration_id: str, run_type: str) -> str:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO integration_runs(integration_id,run_type,status)
            VALUES (%s,%s,'RUNNING') RETURNING id::text
            """,
            (integration_id, run_type),
        )
        run_id = cur.fetchone()["id"]
        conn.commit()
    return run_id


def _finish_run(
    run_id: str,
    *,
    status: str,
    http_status: int | None = None,
    elapsed_ms: int | None = None,
    response_bytes: int | None = None,
    content_type: str | None = None,
    items_found: int | None = None,
    items_changed: int | None = None,
    error_message: str | None = None,
) -> None:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE integration_runs
            SET status=%s,http_status=%s,elapsed_ms=%s,response_bytes=%s,
                content_type=%s,items_found=%s,items_changed=%s,error_message=%s,
                finished_at=now()
            WHERE id=%s
            """,
            (
                status,
                http_status,
                elapsed_ms,
                response_bytes,
                content_type,
                items_found,
                items_changed,
                error_message,
                run_id,
            ),
        )
        conn.commit()


def _update_health(integration: dict[str, Any], *, ok: bool, error: str | None, event_count: int = 0) -> None:
    source_id = f"INT:{integration['integration_key']}"
    metadata = {
        "integration_id": str(integration["id"]),
        "name": integration["name"],
        "category": integration["category"],
        "event_count": event_count,
    }
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO source_health(
              source_id,status,last_attempt_at,last_success_at,last_event_at,last_error,metadata,updated_at
            )
            VALUES(
              %s,%s,now(),CASE WHEN %s THEN now() ELSE NULL END,
              CASE WHEN %s > 0 THEN now() ELSE NULL END,%s,%s::jsonb,now()
            )
            ON CONFLICT(source_id) DO UPDATE SET
              status=EXCLUDED.status,
              last_attempt_at=now(),
              last_success_at=CASE WHEN %s THEN now() ELSE source_health.last_success_at END,
              last_event_at=CASE WHEN %s > 0 THEN now() ELSE source_health.last_event_at END,
              last_error=%s,
              metadata=EXCLUDED.metadata,
              updated_at=now()
            """,
            (
                source_id,
                "OK" if ok else "ERROR",
                ok,
                event_count,
                error,
                json.dumps(metadata),
                ok,
                event_count,
                error,
            ),
        )
        conn.commit()


def _event_change_hash(event: dict[str, Any]) -> str:
    selected = {
        "title": event.get("title"),
        "starts_at": event.get("starts_at").isoformat() if event.get("starts_at") else None,
        "ends_at": event.get("ends_at").isoformat() if event.get("ends_at") else None,
        "status": event.get("status"),
        "impact_level": event.get("impact_level"),
        "impact_score": event.get("impact_score"),
        "impact_summary": event.get("impact_summary"),
        "road_impact": event.get("road_impact"),
        "transit_impact": event.get("transit_impact"),
        "venue": event.get("venue"),
        "address": event.get("address"),
    }
    return hashlib.sha256(json.dumps(selected, sort_keys=True, default=str).encode()).hexdigest()


def _upsert_event(conn, integration: dict[str, Any], event: dict[str, Any]) -> bool:
    external_key = event.get("external_key") or event["fingerprint"]
    source_event_key = f"{integration['integration_key']}:{external_key}"
    change_hash = _event_change_hash(event)
    metadata = event.get("metadata") or {}
    lat = event.get("latitude")
    lon = event.get("longitude")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT change_hash FROM event_intelligence WHERE source_event_key=%s",
            (source_event_key,),
        )
        before = cur.fetchone()
        changed = not before or before["change_hash"] != change_hash
        starts_at = event.get("starts_at")
        near_term_alert = bool(
            event.get("impact_level") == "ALERT"
            and (starts_at is None or (starts_at - datetime.now(timezone.utc)).total_seconds() <= 72 * 3600)
        )

        cur.execute(
            """
            INSERT INTO event_intelligence(
              source_integration_id,source_event_key,external_key,fingerprint,active,
              title,description,event_type,venue,address,municipality,county,state,
              starts_at,ends_at,status,source_name,source_url,attendance_estimate,
              road_impact,transit_impact,impact_score,impact_level,impact_summary,
              latitude,longitude,geom,metadata,change_hash,
              first_seen_at,last_seen_at,last_changed_at,alert_pending,created_at,updated_at
            )
            VALUES(
              %s,%s,%s,%s,true,
              %s,%s,%s,%s,%s,%s,%s,%s,
              %s,%s,%s,%s,%s,%s,
              %s,%s,%s,%s,%s,
              %s,%s,
              CASE WHEN %s IS NOT NULL AND %s IS NOT NULL
                   THEN ST_SetSRID(ST_MakePoint(%s,%s),4326) ELSE NULL END,
              %s::jsonb,%s,
              now(),now(),now(),%s,now(),now()
            )
            ON CONFLICT(source_event_key) DO UPDATE SET
              source_integration_id=EXCLUDED.source_integration_id,
              external_key=EXCLUDED.external_key,
              fingerprint=EXCLUDED.fingerprint,
              active=true,
              title=EXCLUDED.title,
              description=EXCLUDED.description,
              event_type=EXCLUDED.event_type,
              venue=EXCLUDED.venue,
              address=EXCLUDED.address,
              municipality=EXCLUDED.municipality,
              county=EXCLUDED.county,
              state=EXCLUDED.state,
              starts_at=EXCLUDED.starts_at,
              ends_at=EXCLUDED.ends_at,
              status=EXCLUDED.status,
              source_name=EXCLUDED.source_name,
              source_url=EXCLUDED.source_url,
              attendance_estimate=EXCLUDED.attendance_estimate,
              road_impact=EXCLUDED.road_impact,
              transit_impact=EXCLUDED.transit_impact,
              impact_score=EXCLUDED.impact_score,
              impact_level=EXCLUDED.impact_level,
              impact_summary=EXCLUDED.impact_summary,
              latitude=EXCLUDED.latitude,
              longitude=EXCLUDED.longitude,
              geom=EXCLUDED.geom,
              metadata=EXCLUDED.metadata,
              last_seen_at=now(),
              last_changed_at=CASE
                WHEN event_intelligence.change_hash IS DISTINCT FROM EXCLUDED.change_hash THEN now()
                ELSE event_intelligence.last_changed_at
              END,
              alert_pending=CASE
                WHEN event_intelligence.change_hash IS DISTINCT FROM EXCLUDED.change_hash
                  THEN EXCLUDED.alert_pending
                ELSE event_intelligence.alert_pending
              END,
              change_hash=EXCLUDED.change_hash,
              updated_at=now()
            """,
            (
                integration["id"],
                source_event_key,
                event.get("external_key"),
                event["fingerprint"],
                event["title"],
                event.get("description"),
                event.get("event_type") or "EVENT",
                event.get("venue"),
                event.get("address"),
                event.get("municipality"),
                event.get("county"),
                event.get("state"),
                event.get("starts_at"),
                event.get("ends_at"),
                event.get("status") or "SCHEDULED",
                integration["name"],
                event.get("source_url"),
                event.get("attendance_estimate"),
                event.get("road_impact"),
                event.get("transit_impact"),
                event.get("impact_score") or 0,
                event.get("impact_level") or "AWARENESS",
                event.get("impact_summary"),
                lat,
                lon,
                lat,
                lon,
                lon,
                lat,
                json.dumps(metadata, default=str),
                change_hash,
                near_term_alert,
            ),
        )
    return changed


def run_integration(integration: dict[str, Any], *, run_type: str = "POLL", parse_and_store: bool = True) -> dict[str, Any]:
    run_id = _record_run_start(str(integration["id"]), run_type)
    headers = _json(integration.get("request_headers"))
    query = _json(integration.get("request_query"))
    secrets: list[str] = []
    try:
        secrets = integration_auth_from_env(integration, headers, query)
        result = perform_http_request(
            method=integration.get("method") or "GET",
            url=integration["endpoint_url"],
            headers=headers,
            query=query,
            body=integration.get("request_body"),
            timeout_seconds=int(integration.get("timeout_seconds") or 15),
            max_response_bytes=int(integration.get("max_response_bytes") or 1_000_000),
            allow_redirects=bool(integration.get("allow_redirects", True)),
            verify_tls=bool(integration.get("verify_tls", True)),
            allow_private=False,
        )
        if not result.ok:
            error = redact_text(result.error or f"HTTP {result.status_code}", secrets)
            _finish_run(
                run_id,
                status="ERROR",
                http_status=result.status_code,
                elapsed_ms=result.elapsed_ms,
                response_bytes=result.body_bytes,
                content_type=result.content_type,
                error_message=error,
            )
            _update_health(integration, ok=False, error=error)
            return {"ok": False, "run_id": run_id, "result": result, "error": error, "events": [], "changed": 0}

        events: list[dict[str, Any]] = []
        changed_count = 0
        if parse_and_store and str(integration.get("parser_kind") or "NONE").upper() != "NONE":
            events = parse_events(result.body_text, integration["parser_kind"], _json(integration.get("parser_config")))
            with db_conn() as event_conn:
                for event in events:
                    if _upsert_event(event_conn, integration, event):
                        changed_count += 1
                event_conn.commit()

        _finish_run(
            run_id,
            status="OK",
            http_status=result.status_code,
            elapsed_ms=result.elapsed_ms,
            response_bytes=result.body_bytes,
            content_type=result.content_type,
            items_found=len(events),
            items_changed=changed_count,
        )
        _update_health(integration, ok=True, error=None, event_count=len(events))
        return {"ok": True, "run_id": run_id, "result": result, "events": events, "changed": changed_count}
    except Exception as exc:
        error = redact_text(str(exc), secrets)
        _finish_run(run_id, status="ERROR", error_message=error)
        _update_health(integration, ok=False, error=error)
        return {"ok": False, "run_id": run_id, "error": error, "events": [], "changed": 0}


def due_integrations(limit: int = 25) -> list[dict[str, Any]]:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.*
            FROM integrations i
            LEFT JOIN LATERAL (
              SELECT max(started_at) AS last_run
              FROM integration_runs r
              WHERE r.integration_id=i.id AND r.run_type IN ('POLL','MANUAL')
            ) lr ON true
            WHERE i.active=true
              AND i.parser_kind <> 'NONE'
              AND (lr.last_run IS NULL OR lr.last_run <= now() - make_interval(secs => i.poll_seconds))
            ORDER BY lr.last_run NULLS FIRST, i.integration_key
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def run_due_integrations(limit: int = 25) -> list[dict[str, Any]]:
    summaries = []
    for integration in due_integrations(limit=limit):
        outcome = run_integration(integration, run_type="POLL", parse_and_store=True)
        summaries.append(
            {
                "integration_key": integration["integration_key"],
                "name": integration["name"],
                "ok": outcome["ok"],
                "events": len(outcome.get("events") or []),
                "changed": outcome.get("changed") or 0,
                "error": outcome.get("error"),
            }
        )
    return summaries


def mark_stale_events() -> int:
    # Do not deactivate far-future events merely because a source omitted one poll.
    # Only retire events that ended more than 48 hours ago.
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE event_intelligence
            SET active=false,updated_at=now()
            WHERE active=true
              AND COALESCE(ends_at,starts_at) IS NOT NULL
              AND COALESCE(ends_at,starts_at) < now() - interval '48 hours'
            """
        )
        count = cur.rowcount
        conn.commit()
    return count
