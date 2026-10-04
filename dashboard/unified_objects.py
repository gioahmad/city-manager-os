"""Unified read model for the City Manager OS workspace.

Existing tables remain authoritative. This module only reads and normalizes
records for the unified shell.
"""
from uuid import UUID

from app import query_all, query_one


OBJECT_KINDS = {"PERSON", "ORGANIZATION", "PLACE", "WORK", "ALERT", "WATCH", "EVENT"}


def _uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _like(value):
    return f"%{(value or '').strip()[:200]}%"


def home_snapshot(owner: str):
    attention = query_all(
        """
        SELECT id,title,status,item_type,priority,address,municipality,assigned_to,
               next_action,waiting_on,due_at,follow_up_at,updated_at,
               CASE
                 WHEN due_at IS NOT NULL AND due_at < now() THEN 'OVERDUE'
                 WHEN follow_up_at IS NOT NULL AND follow_up_at <= now() THEN 'FOLLOW_UP'
                 WHEN nullif(trim(coalesce(waiting_on,'')),'') IS NOT NULL THEN 'WAITING'
                 WHEN nullif(trim(coalesce(next_action,'')),'') IS NULL THEN 'NO_NEXT_ACTION'
                 ELSE 'ACTIVE'
               END AS attention_reason
        FROM issues
        WHERE status NOT IN ('RESOLVED','CLOSED')
          AND (
            (due_at IS NOT NULL AND due_at < now())
            OR (follow_up_at IS NOT NULL AND follow_up_at <= now())
            OR nullif(trim(coalesce(waiting_on,'')),'') IS NOT NULL
            OR nullif(trim(coalesce(next_action,'')),'') IS NULL
          )
        ORDER BY
          CASE WHEN due_at IS NOT NULL AND due_at < now() THEN 0 ELSE 1 END,
          priority DESC NULLS LAST,
          updated_at DESC
        LIMIT 18
        """
    )

    alerts = query_all(
        """
        SELECT id,alert_id,title,source,category,priority,status,municipality,received_at
        FROM alerts
        WHERE received_at >= now()-interval '24 hours'
        ORDER BY priority DESC,received_at DESC
        LIMIT 18
        """
    )

    events = query_all(
        """
        SELECT id,title,event_type,venue,address,municipality,starts_at,ends_at,
               impact_level,impact_score
        FROM event_intelligence
        WHERE active
          AND status NOT IN ('CANCELLED','COMPLETED')
          AND starts_at >= now()-interval '6 hours'
          AND starts_at < now()+interval '14 days'
        ORDER BY starts_at
        LIMIT 12
        """
    )

    source_health = query_all(
        """
        SELECT source_id,status,last_success_at,last_event_at,last_error
        FROM source_health
        WHERE status IS DISTINCT FROM 'OK'
        ORDER BY updated_at DESC
        LIMIT 12
        """
    )

    failed_deliveries = query_all(
        """
        SELECT d.id,d.status,d.attempted_at,d.error_message,
               a.id AS alert_db_id,a.alert_id,a.title AS alert_title,
               s.name AS subscriber_name
        FROM deliveries d
        JOIN alerts a ON a.id=d.alert_id
        JOIN subscribers s ON s.id=d.subscriber_id
        WHERE d.status='FAILED'
          AND d.created_at>=now()-interval '24 hours'
        ORDER BY d.created_at DESC
        LIMIT 12
        """
    )

    counts = query_one(
        """
        SELECT
          (SELECT count(*) FROM issues WHERE status NOT IN ('RESOLVED','CLOSED')) AS open_work,
          (SELECT count(*) FROM alerts WHERE received_at>=now()-interval '24 hours') AS alerts_24h,
          (SELECT count(*) FROM watch_items WHERE active) AS active_watches,
          (SELECT count(*) FROM deliveries WHERE status='FAILED' AND created_at>=now()-interval '24 hours') AS failed_24h,
          (SELECT count(*) FROM source_health WHERE status IS DISTINCT FROM 'OK') AS unhealthy_sources,
          (SELECT count(*) FROM event_intelligence
             WHERE active AND status NOT IN ('CANCELLED','COMPLETED')
             AND starts_at>=now() AND starts_at<now()+interval '7 days') AS events_7d
        """
    )

    recent = query_all(
        """
        SELECT * FROM (
          SELECT 'WORK'::text AS kind,i.id,i.title,
                 coalesce(i.next_action,i.status)::text AS detail,
                 i.updated_at AS occurred_at
          FROM issues i
          UNION ALL
          SELECT 'ALERT',a.id,a.title,
                 concat_ws(' · ',a.source,a.municipality,'P'||a.priority::text),
                 a.received_at
          FROM alerts a
          WHERE a.received_at>=now()-interval '7 days'
          UNION ALL
          SELECT 'EVENT',e.id,e.title,
                 concat_ws(' · ',e.event_type,e.municipality,e.impact_level),
                 e.updated_at
          FROM event_intelligence e
          WHERE e.updated_at>=now()-interval '14 days'
        ) x
        ORDER BY occurred_at DESC
        LIMIT 24
        """
    )

    return {
        "counts": counts,
        "attention": attention,
        "alerts": alerts,
        "events": events,
        "source_health": source_health,
        "failed_deliveries": failed_deliveries,
        "recent": recent,
    }


def search_objects(owner: str, q: str = "", kind: str = "", limit: int = 60):
    q=(q or "").strip()
    kind=(kind or "").strip().upper()
    limit=max(1,min(int(limit or 60),100))
    if kind and kind not in OBJECT_KINDS:
        return []
    needle=_like(q)
    rows=[]

    if not kind or kind in {"PERSON","ORGANIZATION"}:
        params=[owner]
        where=["(visibility='WORK' OR owner_username=%s)"]
        if kind in {"PERSON","ORGANIZATION"}:
            where.append("kind=%s");params.append(kind)
        else:
            where.append("kind IN ('PERSON','ORGANIZATION')")
        if q:
            where.append("(name ILIKE %s OR attributes::text ILIKE %s)")
            params.extend([needle,needle])
        params.append(limit)
        found=query_all(
            f"""SELECT id,kind,name,visibility,attributes,updated_at
                FROM workspace_entities
                WHERE {' AND '.join(where)}
                ORDER BY updated_at DESC,name LIMIT %s""",
            tuple(params))
        for r in found:
            attrs=r.get("attributes") or {}
            rows.append({
                "kind":r["kind"],"id":str(r["id"]),"title":r["name"],
                "subtitle":attrs.get("organization") or attrs.get("address") or "",
                "meta":r["visibility"],"updated_at":r.get("updated_at")})

    if not kind or kind=="PLACE":
        params=[];where=["active=true"]
        if q:
            where.append("(canonical_name ILIKE %s OR normalized_address ILIKE %s OR municipality ILIKE %s OR aliases::text ILIKE %s)")
            params.extend([needle]*4)
        params.append(limit)
        found=query_all(
            f"""SELECT entity_id,entity_type,canonical_name,normalized_address,
                       municipality,county,state,authoritative,importance_tier,updated_at
                FROM spatial_reference_entities
                WHERE {' AND '.join(where)}
                ORDER BY importance_tier,canonical_name LIMIT %s""",
            tuple(params))
        for r in found:
            subtitle=" · ".join(x for x in [r.get("normalized_address"),r.get("municipality")] if x)
            rows.append({"kind":"PLACE","id":str(r["entity_id"]),"title":r["canonical_name"],
                         "subtitle":subtitle,"meta":r["entity_type"],"updated_at":r.get("updated_at")})

    if not kind or kind=="WORK":
        params=[];where=[]
        if q:
            where.append("(title ILIKE %s OR description ILIKE %s OR address ILIKE %s OR municipality ILIKE %s OR assigned_to ILIKE %s OR next_action ILIKE %s)")
            params.extend([needle]*6)
        sql_where="WHERE "+" AND ".join(where) if where else ""
        params.append(limit)
        found=query_all(
            f"""SELECT id,title,status,item_type,priority,address,municipality,assigned_to,
                       next_action,waiting_on,due_at,follow_up_at,updated_at
                FROM issues {sql_where}
                ORDER BY CASE WHEN status IN ('RESOLVED','CLOSED') THEN 1 ELSE 0 END,
                         updated_at DESC LIMIT %s""",
            tuple(params))
        for r in found:
            rows.append({"kind":"WORK","id":str(r["id"]),"title":r["title"],
                         "subtitle":r.get("next_action") or r.get("address") or r.get("municipality") or "",
                         "meta":f"{r['item_type']} · {r['status']}","updated_at":r.get("updated_at")})

    if not kind or kind=="ALERT":
        params=[];where=[]
        if q:
            where.append("(title ILIKE %s OR message ILIKE %s OR source ILIKE %s OR municipality ILIKE %s OR alert_id ILIKE %s)")
            params.extend([needle]*5)
        sql_where="WHERE "+" AND ".join(where) if where else ""
        params.append(limit)
        found=query_all(
            f"""SELECT id,alert_id,title,source,category,status,priority,municipality,
                       received_at,updated_at
                FROM alerts {sql_where}
                ORDER BY received_at DESC LIMIT %s""",tuple(params))
        for r in found:
            rows.append({"kind":"ALERT","id":str(r["id"]),"title":r["title"],
                         "subtitle":r.get("municipality") or r["category"],
                         "meta":f"{r['source']} · P{r['priority']} · {r['status']}",
                         "updated_at":r.get("received_at")})

    if not kind or kind=="WATCH":
        params=[];where=[]
        if q:
            where.append("(display_name ILIKE %s OR search_term ILIKE %s OR address ILIKE %s OR municipality ILIKE %s OR watch_id ILIKE %s)")
            params.extend([needle]*5)
        sql_where="WHERE "+" AND ".join(where) if where else ""
        params.append(limit)
        found=query_all(
            f"""SELECT id,watch_id,display_name,watch_type,active,search_term,address,
                       municipality,spatial_scope,updated_at
                FROM watch_items {sql_where}
                ORDER BY active DESC,updated_at DESC LIMIT %s""",tuple(params))
        for r in found:
            rows.append({"kind":"WATCH","id":str(r["id"]),"title":r["display_name"],
                         "subtitle":r.get("address") or r.get("municipality") or r.get("search_term") or "",
                         "meta":f"{r['watch_type']} · {'ON' if r['active'] else 'OFF'}",
                         "updated_at":r.get("updated_at")})

    if not kind or kind=="EVENT":
        params=[];where=["active=true"]
        if q:
            where.append("(title ILIKE %s OR description ILIKE %s OR venue ILIKE %s OR address ILIKE %s OR municipality ILIKE %s)")
            params.extend([needle]*5)
        params.append(limit)
        found=query_all(
            f"""SELECT id,title,event_type,venue,address,municipality,starts_at,ends_at,
                       impact_level,updated_at
                FROM event_intelligence WHERE {' AND '.join(where)}
                ORDER BY starts_at DESC NULLS LAST,updated_at DESC LIMIT %s""",
            tuple(params))
        for r in found:
            rows.append({"kind":"EVENT","id":str(r["id"]),"title":r["title"],
                         "subtitle":r.get("venue") or r.get("address") or r.get("municipality") or "",
                         "meta":f"{r['event_type']} · {r['impact_level']}",
                         "updated_at":r.get("starts_at") or r.get("updated_at")})

    rows.sort(key=lambda r:str(r.get("updated_at") or ""),reverse=True)
    return rows[:limit]


def object_detail(owner: str, kind: str, object_id: str):
    kind=(kind or "").upper()
    uid=_uuid(object_id)
    if kind not in OBJECT_KINDS or not uid:
        return None

    if kind in {"PERSON","ORGANIZATION"}:
        row=query_one("""SELECT * FROM workspace_entities
                         WHERE id=%s AND kind=%s AND (visibility='WORK' OR owner_username=%s)""",
                      (uid,kind,owner))
        if not row:return None
        relationships=query_all(
            """SELECT r.id,r.relation,r.evidence,r.visibility,
                      CASE WHEN r.source_id=%s THEN t.id ELSE s.id END AS other_id,
                      CASE WHEN r.source_id=%s THEN t.kind ELSE s.kind END AS other_kind,
                      CASE WHEN r.source_id=%s THEN t.name ELSE s.name END AS other_name
               FROM workspace_relationships r
               JOIN workspace_entities s ON s.id=r.source_id
               JOIN workspace_entities t ON t.id=r.target_id
               WHERE r.active AND (r.source_id=%s OR r.target_id=%s)
                 AND (r.visibility='WORK' OR r.owner_username=%s)
               ORDER BY r.created_at DESC""",
            (uid,uid,uid,uid,uid,owner))
        messages=query_all("""SELECT id,phone,body,status,created_at
                              FROM workspace_messages
                              WHERE entity_id=%s AND owner_username=%s
                              ORDER BY created_at DESC LIMIT 25""",(uid,owner))
        activity=[]
        if row.get("contact_id"):
            activity=query_all("""SELECT id,activity_type,summary,actor,created_at
                                  FROM contact_activity WHERE contact_id=%s
                                  ORDER BY created_at DESC LIMIT 30""",(row["contact_id"],))
        return {"object":row,"related":{"relationships":relationships,"messages":messages,"activity":activity},
                "legacy_url":f"/contacts?q={row['name']}"}

    if kind=="PLACE":
        row=query_one("""SELECT r.*,ST_X(r.centroid) AS longitude,ST_Y(r.centroid) AS latitude
                         FROM spatial_reference_entities r WHERE r.entity_id=%s AND r.active""",(uid,))
        if not row:return None
        radius_ft=float(row.get("default_buffer_ft") or 1000);radius_m=radius_ft*0.3048
        watches=query_all("""SELECT id,watch_id,display_name,active,watch_type,spatial_scope,radius_ft
                             FROM watch_items WHERE spatial_reference_entity_id=%s
                             ORDER BY active DESC,display_name""",(uid,))
        alerts=query_all("""SELECT id,alert_id,title,source,category,priority,status,municipality,received_at
                            FROM alerts a WHERE a.geom IS NOT NULL
                              AND ST_DWithin(a.geom::geography,%s::geography,%s)
                            ORDER BY received_at DESC LIMIT 40""",(row["centroid"],radius_m))
        work=query_all("""SELECT id,title,status,priority,address,next_action,updated_at
                          FROM issues i WHERE i.geom IS NOT NULL
                            AND ST_DWithin(i.geom::geography,%s::geography,%s)
                          ORDER BY CASE WHEN status IN ('RESOLVED','CLOSED') THEN 1 ELSE 0 END,
                                   updated_at DESC LIMIT 40""",(row["centroid"],radius_m))
        parcel={}
        if row.get("parcel_objectid") is not None:
            parcel=query_one("""SELECT objectid,pams_pin,pclblock,pcllot,prop_loc,owner_name,
                                      prop_class,land_val,imprvt_val,net_value,bldg_desc,
                                      land_desc,yr_constr,sale_price
                               FROM gis_parcels WHERE objectid=%s LIMIT 1""",(row["parcel_objectid"],))
        return {"object":row,"related":{"watches":watches,"alerts":alerts,"work":work,"parcel":parcel},
                "legacy_url":f"/spatial-reference/{uid}","map_url":f"/map?reference={uid}"}

    if kind=="WORK":
        row=query_one("SELECT * FROM issues WHERE id=%s",(uid,))
        if not row:return None
        updates=query_all("""SELECT id,author,note,created_at FROM issue_updates
                             WHERE issue_id=%s ORDER BY created_at DESC LIMIT 50""",(uid,))
        watch={}
        if row.get("watch_item_id"):
            watch=query_one("""SELECT id,watch_id,display_name,active,watch_type
                               FROM watch_items WHERE id=%s""",(row["watch_item_id"],))
        return {"object":row,"related":{"updates":updates,"watch":watch},
                "legacy_url":f"/issues?q={row['title']}"}

    if kind=="ALERT":
        row=query_one("""SELECT a.*,
                                CASE WHEN geom IS NULL THEN NULL ELSE ST_X(geom) END AS longitude,
                                CASE WHEN geom IS NULL THEN NULL ELSE ST_Y(geom) END AS latitude
                         FROM alerts a WHERE id=%s""",(uid,))
        if not row:return None
        matches=query_all("""SELECT m.id,m.match_type,m.match_reason,m.matched_at,
                                    w.id AS watch_item_id,w.watch_id,w.display_name,w.active
                             FROM alert_watch_matches m
                             JOIN watch_items w ON w.id=m.watch_item_id
                             WHERE m.alert_id=%s ORDER BY m.matched_at DESC""",(uid,))
        deliveries=query_all("""SELECT d.id,d.status,d.attempted_at,d.sent_at,d.error_message,
                                       d.match_reasons,s.name AS subscriber_name
                                FROM deliveries d JOIN subscribers s ON s.id=d.subscriber_id
                                WHERE d.alert_id=%s ORDER BY d.created_at DESC""",(uid,))
        return {"object":row,"related":{"matches":matches,"deliveries":deliveries},
                "legacy_url":f"/alerts?q={row['alert_id']}","map_url":"/map"}

    if kind=="WATCH":
        row=query_one("SELECT * FROM watch_items WHERE id=%s",(uid,))
        if not row:return None
        matches=query_all("""SELECT m.id,m.match_type,m.match_reason,m.matched_at,
                                    a.id AS alert_db_id,a.alert_id,a.title,a.source,a.priority,a.status,a.received_at
                             FROM alert_watch_matches m JOIN alerts a ON a.id=m.alert_id
                             WHERE m.watch_item_id=%s ORDER BY m.matched_at DESC LIMIT 50""",(uid,))
        recipients=query_all("""SELECT r.id,r.active,s.id AS subscriber_id,s.name
                                FROM watch_item_recipients r JOIN subscribers s ON s.id=r.subscriber_id
                                WHERE r.watch_item_id=%s ORDER BY r.active DESC,s.name""",(uid,))
        return {"object":row,"related":{"matches":matches,"recipients":recipients},
                "legacy_url":f"/watchlist?q={row['display_name']}","map_url":"/map"}

    if kind=="EVENT":
        row=query_one("SELECT * FROM event_intelligence WHERE id=%s",(uid,))
        if not row:return None
        work=query_all("""SELECT id,title,status,priority,next_action,updated_at FROM issues
                          WHERE event_intelligence_id=%s ORDER BY updated_at DESC""",(uid,))
        return {"object":row,"related":{"work":work},"legacy_url":"/event-intelligence","map_url":"/map"}
    return None
