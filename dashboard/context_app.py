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
            }
        )
    return linked


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


@app.get("/context/{item_kind}/{item_id}", response_class=HTMLResponse)
def context_page(request: Request, item_kind: str, item_id: str):
    owner = _owner(request)
    kind = item_kind.strip().upper()
    record_id = _uid(item_id)
    if kind in workspace_hub.KINDS:
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
        actions = [("Open Work", f"/issues?q={item['title']}&state=all"), ("Map", f"/map?q={item['title']}")]
    elif kind == "EVENT":
        actions = [("Open Event", f"/schedule?q={item['title']}&state=all"), ("Map", f"/map?q={item['title']}")]
    elif kind == "CALENDAR":
        actions = [("Open Intake", "/workspace#inbox")]
        if metadata.get("outlook_url"):
            actions.append(("Open in Outlook", metadata["outlook_url"]))
    elif kind == "MAIL":
        actions = [("Open Intake", "/workspace#inbox")]
        if metadata.get("outlook_url"):
            actions.append(("Open in Outlook", metadata["outlook_url"]))
    elif kind == "RECORD":
        actions = [("Open People & Places", "/workspace#people")]
        address = (metadata.get("attributes") or {}).get("address")
        if address:
            actions.append(("Map Address", f"/map?q={address}"))
    elif kind == "WATCH":
        actions = [("Open Watch", item["route"]), ("Map", f"/map?map_view=1&layers=watchlist&selected_layer=watchlist&selected_id={metadata.get('watch_id','')}")]
    elif kind == "REFERENCE":
        actions = [("Open Reference", item["route"]), ("Map", f"/map?q={item['title']}")]
    elif kind == "BRAIN":
        actions = [("Open Brain", "/brain")]
    elif kind == "TASK":
        actions = [("Open Today", "/workspace#today")]

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

    return templates.TemplateResponse(
        request=request,
        name="context.html",
        context={
            "page": "context",
            "item": item,
            "actions": actions,
            "relationships": relationships,
        },
    )


@app.get("/records/{record_id}")
def record_page(record_id: str):
    return RedirectResponse(f"/context/RECORD/{record_id}", status_code=303)
