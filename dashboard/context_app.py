"""Universal context inspector for connected City Manager OS records."""
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import app, query_all, query_one, templates
from brain_app import _owner
import workspace_hub


def _uid(value: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid record identifier")


def _link_row(owner: str, kind: str, record_id: UUID) -> list[dict]:
    rows = query_all(
        """SELECT id,source_kind,source_id,target_kind,target_id,created_at
           FROM workspace_context_links
           WHERE owner_username=%s
             AND ((source_kind=%s AND source_id=%s) OR (target_kind=%s AND target_id=%s))
           ORDER BY created_at DESC LIMIT 100""",
        (owner, kind, record_id, kind, record_id),
    )
    linked = []
    for row in rows:
        target_kind, target_id = (
            (row["target_kind"], row["target_id"])
            if row["source_kind"] == kind and row["source_id"] == record_id
            else (row["source_kind"], row["source_id"])
        )
        try:
            target = workspace_hub.find(owner, target_kind, target_id)
        except HTTPException:
            continue
        linked.append(
            {
                "link_id": row["id"],
                "kind": target["kind"],
                "id": target["id"],
                "title": target["title"],
                "status": target["status"],
                "visibility": target["visibility"],
                "route": target["route"],
                "updated_at": target.get("updated_at"),
                "snippet": str(target.get("body") or "")[:220],
            }
        )
    return linked


def _alert_record(owner: str, record_id: UUID) -> dict:
    row = query_one(
        """SELECT a.id,a.alert_id,a.title,a.message,a.status,a.priority,a.source,a.category,a.subtype,
                  a.municipality,a.county,a.observed_at,a.received_at,
                  coalesce(a.observed_at,a.received_at) AS activity_at,a.updated_at,
                  coalesce(nullif(a.location->>'label',''),nullif(a.location->>'address','')) AS address,
                  coalesce((SELECT count(*) FROM alert_watch_matches m WHERE m.alert_id=a.id),0) AS match_count,
                  coalesce((SELECT count(*) FROM deliveries d WHERE d.alert_id=a.id),0) AS delivery_count,
                  coalesce((SELECT string_agg(DISTINCT w.display_name,', ' ORDER BY w.display_name)
                            FROM alert_watch_matches m JOIN watch_items w ON w.id=m.watch_item_id
                            WHERE m.alert_id=a.id),'') AS matched_watches
           FROM alerts a WHERE a.id=%s""",
        (record_id,),
    )
    if not row:
        raise HTTPException(404, "Alert not found")
    return {
        "kind": "ALERT",
        "id": row["id"],
        "title": row["title"],
        "body": row.get("message") or "",
        "status": row.get("status") or "",
        "visibility": "WORK",
        "updated_at": row.get("updated_at") or row.get("received_at"),
        "metadata": row,
        "route": f"/alerts?q={row['alert_id']}&window=all&state=all",
        "links": _link_row(owner, "ALERT", record_id),
    }


def _hub_record(owner: str, kind: str, record_id: UUID) -> dict:
    item = workspace_hub.find(owner, kind, record_id)
    return {
        "kind": item["kind"],
        "id": item["id"],
        "title": item["title"],
        "body": item.get("body") or "",
        "status": item.get("status") or "",
        "visibility": item.get("visibility") or "",
        "updated_at": item.get("updated_at"),
        "metadata": item.get("metadata") or {},
        "route": item.get("route") or "",
        "links": _link_row(owner, kind, record_id),
    }


def _watch_record(record_id: UUID) -> dict:
    row = query_one(
        """SELECT w.id,w.watch_id,w.display_name,w.active,w.watch_type,w.match_mode,
                  w.search_term,w.aliases,w.municipality,w.address,w.radius_ft,w.min_priority,
                  w.starts_at,w.expires_at,w.updated_at,
                  coalesce((SELECT count(*) FROM alert_watch_matches m WHERE m.watch_item_id=w.id),0) AS match_count,
                  coalesce((SELECT count(*) FROM watch_item_recipients r WHERE r.watch_item_id=w.id AND r.active),0) AS recipient_count,
                  coalesce((SELECT count(*) FROM deliveries d WHERE d.matched_watch_ids ? w.watch_id AND d.created_at>=now()-interval '24 hours'),0) AS deliveries_24h
           FROM watch_items w WHERE w.id=%s""",
        (record_id,),
    )
    if not row:
        raise HTTPException(404, "Watch not found")
    body = " · ".join(
        part
        for part in (
            row.get("search_term") or "",
            row.get("address") or row.get("municipality") or "",
            f"{row.get('radius_ft') or 0:,.0f} ft" if row.get("radius_ft") else "",
        )
        if part
    )
    return {
        "kind": "WATCH",
        "id": row["id"],
        "title": row["display_name"],
        "body": body,
        "status": "WATCHING" if row["active"] else "PAUSED",
        "visibility": "WORK",
        "updated_at": row.get("updated_at"),
        "metadata": row,
        "route": f"/watchlist?q={row['watch_id']}",
        "links": [],
    }


def _reference_record(record_id: UUID) -> dict:
    row = query_one(
        """SELECT entity_id,canonical_name,entity_type,entity_subtype,normalized_address,
                  municipality,county,state,source_provider,authoritative,importance_tier,
                  default_buffer_ft,parcel_objectid,transit_asset_id,updated_at
           FROM spatial_reference_entities WHERE entity_id=%s""",
        (record_id,),
    )
    if not row:
        raise HTTPException(404, "Reference not found")
    return {
        "kind": "REFERENCE",
        "id": row["entity_id"],
        "title": row["canonical_name"],
        "body": row.get("normalized_address") or row.get("municipality") or "Mapped reference",
        "status": "AUTHORITATIVE" if row.get("authoritative") else "MANAGED",
        "visibility": "WORK",
        "updated_at": row.get("updated_at"),
        "metadata": row,
        "route": f"/spatial-reference/{row['entity_id']}",
        "links": [],
    }


def _context_insights(owner: str, kind: str, record_id: UUID, item: dict, relationships: list[dict]) -> tuple[list[dict], list[dict]]:
    metadata = item.get("metadata") or {}
    metrics: list[dict] = []
    evidence: list[dict] = []

    if kind == "ALERT":
        matches = query_all(
            """SELECT w.id AS watch_uuid,w.display_name,m.match_type,m.match_reason,m.matched_at
               FROM alert_watch_matches m
               JOIN watch_items w ON w.id=m.watch_item_id
               WHERE m.alert_id=%s
               ORDER BY m.matched_at DESC LIMIT 25""",
            (record_id,),
        )
        deliveries = query_all(
            """SELECT d.status,d.attempted_at,d.sent_at,d.error_message,d.ntfy_topic,s.name AS subscriber_name
               FROM deliveries d LEFT JOIN subscribers s ON s.id=d.subscriber_id
               WHERE d.alert_id=%s ORDER BY d.attempted_at DESC LIMIT 25""",
            (record_id,),
        )
        metrics = [
            {"label": "Priority", "value": f"P{metadata.get('priority') or '—'}"},
            {"label": "Watch matches", "value": str(len(matches))},
            {"label": "Notifications", "value": str(len(deliveries))},
            {"label": "Activity", "value": metadata.get("activity_at") or metadata.get("received_at")},
        ]
        for row in matches:
            evidence.append({
                "kind": "WATCH",
                "title": row["display_name"],
                "detail": " · ".join(x for x in (row.get("match_type"), row.get("match_reason")) if x),
                "happened_at": row.get("matched_at"),
                "url": f"/context/WATCH/{row['watch_uuid']}",
            })
        for row in deliveries:
            evidence.append({
                "kind": "NOTIFICATION",
                "title": row.get("subscriber_name") or row.get("ntfy_topic") or "Notification",
                "detail": " · ".join(x for x in (row.get("status"), row.get("error_message")) if x),
                "happened_at": row.get("sent_at") or row.get("attempted_at"),
                "url": "/deliveries",
            })
    elif kind == "WATCH":
        recent = query_all(
            """SELECT a.id AS alert_uuid,a.title,a.source,a.priority,m.match_reason,m.matched_at
               FROM alert_watch_matches m JOIN alerts a ON a.id=m.alert_id
               WHERE m.watch_item_id=%s
               ORDER BY m.matched_at DESC LIMIT 30""",
            (record_id,),
        )
        metrics = [
            {"label": "State", "value": item.get("status") or "—"},
            {"label": "Matches", "value": str(metadata.get("match_count") or 0)},
            {"label": "Recipients", "value": str(metadata.get("recipient_count") or 0)},
            {"label": "Notifications · 24h", "value": str(metadata.get("deliveries_24h") or 0)},
        ]
        for row in recent:
            evidence.append({
                "kind": "ALERT",
                "title": row["title"],
                "detail": f"{row.get('source') or 'Alert'} · P{row.get('priority') or '—'}" + (f" · {row['match_reason']}" if row.get("match_reason") else ""),
                "happened_at": row.get("matched_at"),
                "url": f"/context/ALERT/{row['alert_uuid']}",
            })
    elif kind == "WORK":
        row = query_one(
            """SELECT priority,item_type,status,assigned_to,waiting_on,next_action,address,municipality,due_at,follow_up_at
               FROM issues WHERE id=%s""",
            (record_id,),
        ) or {}
        metadata.update(row)
        item["metadata"] = metadata
        metrics = [
            {"label": "Priority", "value": f"P{row.get('priority') or '—'}"},
            {"label": "Owner", "value": row.get("assigned_to") or "Unassigned"},
            {"label": "Waiting on", "value": row.get("waiting_on") or "—"},
            {"label": "Next action", "value": row.get("next_action") or "Not set"},
        ]
    elif kind == "RECORD":
        attrs = metadata.get("attributes") or {}
        record_kind = str(item.get("status") or "RECORD").replace("_", " ")
        metrics = [
            {"label": "Record type", "value": record_kind},
            {"label": "Linked context", "value": str(len(item.get("links") or []))},
            {"label": "Relationships", "value": str(len(relationships))},
            {"label": "Location", "value": attrs.get("address") or "—"},
        ]
    elif kind == "EVENT":
        metrics = [
            {"label": "State", "value": item.get("status") or "—"},
            {"label": "Starts", "value": metadata.get("starts_at") or "—"},
            {"label": "Location", "value": metadata.get("location") or "—"},
            {"label": "Linked context", "value": str(len(item.get("links") or []))},
        ]
    else:
        metrics = [
            {"label": "State", "value": item.get("status") or "—"},
            {"label": "Visibility", "value": item.get("visibility") or "—"},
            {"label": "Linked context", "value": str(len(item.get("links") or []))},
            {"label": "Updated", "value": item.get("updated_at") or "—"},
        ]

    evidence.sort(key=lambda row: str(row.get("happened_at") or ""), reverse=True)
    return metrics, evidence


@app.get("/context/{item_kind}/{item_id}", response_class=HTMLResponse)
def context_page(request: Request, item_kind: str, item_id: str):
    owner = _owner(request)
    kind = item_kind.strip().upper()
    record_id = _uid(item_id)
    if kind == "ALERT":
        item = _alert_record(owner, record_id)
    elif kind in workspace_hub.KINDS:
        item = _hub_record(owner, kind, record_id)
    elif kind == "WATCH":
        item = _watch_record(record_id)
    elif kind == "REFERENCE":
        item = _reference_record(record_id)
    else:
        raise HTTPException(404, "Context type is not available")

    metadata = item.get("metadata") or {}
    actions = []
    if kind == "ALERT":
        alert = query_one("SELECT alert_id FROM alerts WHERE id=%s", (record_id,))
        if alert:
            ref = alert["alert_id"]
            actions = [
                ("Open Alert", f"/alerts?q={ref}&window=all&state=all"),
                ("Map", f"/map?q={ref}"),
                ("Create Work", f"/issues?from_alert={ref}"),
                ("Create Watch", f"/watchlist?from_alert={ref}"),
                ("Share", f"/share?alert={ref}"),
            ]
    elif kind == "WORK":
        actions = [("Open Work", f"/issues?focus={record_id}&state=all"), ("Map", f"/map?q={item['title']}")]
    elif kind == "EVENT":
        actions = [("Open Event", f"/schedule?focus={record_id}&state=all"), ("Map", f"/map?q={item['title']}")]
    elif kind == "CALENDAR":
        actions = [("Open Intake", f"/intake?kind=CALENDAR&id={record_id}")]
        if metadata.get("outlook_url"):
            actions.append(("Open in Outlook", metadata["outlook_url"]))
    elif kind == "MAIL":
        actions = [("Open Intake", f"/intake?kind=MAIL&id={record_id}")]
        if metadata.get("outlook_url"):
            actions.append(("Open in Outlook", metadata["outlook_url"]))
    elif kind == "RECORD":
        attrs = metadata.get("attributes") or {}
        record_type = str(item.get("status") or "").upper()
        actions = [("Open People, Places & Projects", "/workspace?view=people"), ("Search Everything", f"/search?q={item['title']}")]
        address = attrs.get("address")
        if address:
            actions.append(("Map Address", f"/map?q={address}"))
        if record_type == "PROJECT":
            actions.append(("Find Project Work", f"/issues?q={item['title']}"))
        elif record_type in {"BUILDING","STREET"}:
            actions.append(("Nearby Activity", f"/map?q={address or item['title']}"))
    elif kind == "WATCH":
        actions = [("Open Watch", item["route"]), ("Map", f"/map?map_view=1&layers=watchlist&selected_layer=watchlist&selected_id={metadata.get('watch_id','')}")]
    elif kind == "REFERENCE":
        actions = [("Open Reference", item["route"]), ("Map", f"/map?q={item['title']}")]
    elif kind == "BRAIN":
        actions = [("Open Brain", "/brain")]
    elif kind == "TASK":
        actions = [("Open Today", "/workspace?view=today")]

    relationships = []
    if kind == "RECORD":
        relationships = query_all(
            """SELECT r.id,r.relation,
                      CASE WHEN r.source_id=%s THEN t.id ELSE s.id END AS other_id,
                      CASE WHEN r.source_id=%s THEN t.name ELSE s.name END AS other_name,
                      CASE WHEN r.source_id=%s THEN t.kind ELSE s.kind END AS other_kind,
                      r.visibility,r.evidence
               FROM workspace_relationships r
               JOIN workspace_entities s ON s.id=r.source_id
               JOIN workspace_entities t ON t.id=r.target_id
               WHERE r.active AND r.owner_username=%s AND (r.source_id=%s OR r.target_id=%s)
               ORDER BY r.created_at DESC LIMIT 100""",
            (record_id, record_id, record_id, owner, record_id, record_id),
        )

    metrics, evidence = _context_insights(owner, kind, record_id, item, relationships)

    return templates.TemplateResponse(
        request=request,
        name="context.html",
        context={
            "page": "context",
            "item": item,
            "actions": actions,
            "relationships": relationships,
            "metrics": metrics,
            "evidence": evidence,
        },
    )


@app.get("/records/{record_id}")
def record_page(record_id: str):
    return RedirectResponse(f"/context/RECORD/{record_id}", status_code=303)
