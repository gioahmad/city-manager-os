"""Unified City Manager OS shell.

This is a parallel presentation layer over existing authoritative tables and
engines. Existing routes remain available as advanced controls.
"""
import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from app import app, db_conn, execute, query_one, templates
from brain_app import _csrf, _owner, _write
from unified_objects import OBJECT_KINDS, home_snapshot, object_detail, search_objects
from workspace_hub import context as hub_context, find as hub_find, list_items
from operations_app import require_watch_recipients


def _json(value):
    return JSONResponse(jsonable_encoder(value), headers={"Cache-Control":"no-store"})


@app.get("/workspace-next", response_class=HTMLResponse)
def workspace_next(request: Request):
    owner=_owner(request)
    return templates.TemplateResponse(
        request=request,
        name="workspace_next.html",
        context={"username":owner,"csrf":_csrf(request)},
    )


@app.get("/api/workspace-next/home")
def workspace_next_home(request: Request):
    return _json(home_snapshot(_owner(request)))


@app.get("/api/workspace-next/inbox")
def workspace_next_inbox(request: Request, q: str="", bucket: str="open", offset: int=0):
    owner=_owner(request)
    return _json(list_items(owner, view="inbox", q=q, scope="both", source="", bucket=bucket, offset=offset))


@app.get("/api/workspace-next/documents")
def workspace_next_documents(request: Request, q: str="", offset: int=0):
    owner=_owner(request)
    return _json(list_items(owner, view="library", q=q, scope="both", source="", bucket="all", offset=offset))


@app.get("/api/workspace-next/hub/{item_kind}/{item_id}")
def workspace_next_hub_detail(request: Request, item_kind: str, item_id: str):
    owner=_owner(request)
    item=hub_find(owner,item_kind,item_id)
    links,suggestions=hub_context(owner,item)
    if item["kind"]=="BRAIN":
        item["metadata"]["attachments"]=query_one(
            """SELECT coalesce(jsonb_agg(jsonb_build_object('id',a.id,'filename',a.filename)),'[]'::jsonb) AS items
               FROM brain_attachments a JOIN brain_notes n ON n.id=a.note_id
               WHERE n.id=%s AND n.owner_username=%s AND n.deleted_at IS NULL""",
            (item["id"],owner),
        ).get("items") or []
    return _json({"item":item,"links":links,"suggestions":suggestions})


@app.get("/api/objects/search")
def unified_search(request: Request, q: str="", kind: str="", limit: int=60):
    owner=_owner(request)
    return _json({"items":search_objects(owner,q=q,kind=kind,limit=limit)})


@app.get("/api/objects/{kind}/{object_id}")
def unified_detail(request: Request, kind: str, object_id: str):
    owner=_owner(request)
    kind=kind.upper()
    if kind not in OBJECT_KINDS:
        raise HTTPException(404,"Unknown object type")
    row=object_detail(owner,kind,object_id)
    if not row:
        raise HTTPException(404,"Object not found")
    return _json(row)



def _uid(value):
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(400, "Invalid record identifier")


def _clean(value, limit=4000):
    value=str(value or "").strip()
    return value[:limit]


@app.post("/api/workspace-next/action")
async def workspace_next_action(request: Request):
    raw=await request.body()
    if len(raw)>40000:
        raise HTTPException(413,"Request too large")
    try:
        data=json.loads(raw)
    except (ValueError,TypeError,UnicodeDecodeError):
        raise HTTPException(400,"Invalid request")
    if not isinstance(data,dict):
        raise HTTPException(400,"Invalid request")
    owner=_write(request,str(data.get("csrf") or ""))
    action=str(data.get("action") or "").upper()

    if action=="WORK_UPDATE":
        issue_id=_uid(data.get("id"))
        current=query_one("SELECT * FROM issues WHERE id=%s",(issue_id,))
        if not current:
            raise HTTPException(404,"Work item not found")
        status=_clean(data.get("status") or current["status"],30).upper()
        if status not in {"OPEN","IN_PROGRESS","ON_HOLD","RESOLVED","CLOSED"}:
            raise HTTPException(400,"Invalid work status")
        try:
            priority=max(1,min(5,int(data.get("priority") or current.get("priority") or 3)))
        except (TypeError,ValueError):
            raise HTTPException(400,"Invalid priority")
        execute(
            """
            UPDATE issues SET
              title=%s,description=%s,category=%s,priority=%s,status=%s,
              address=%s,municipality=%s,assigned_to=%s,item_type=%s,
              next_action=%s,waiting_on=%s,
              due_at=NULLIF(%s,'')::timestamp AT TIME ZONE current_setting('TimeZone'),
              follow_up_at=NULLIF(%s,'')::timestamp AT TIME ZONE current_setting('TimeZone'),
              updated_at=now(),
              closed_at=CASE WHEN %s IN ('RESOLVED','CLOSED')
                             THEN COALESCE(closed_at,now()) ELSE NULL END
            WHERE id=%s
            """,
            (
              _clean(data.get("title") or current["title"],500),
              _clean(data.get("description") if "description" in data else current.get("description"),10000) or None,
              _clean(data.get("category") if "category" in data else current.get("category"),100) or None,
              priority,status,
              _clean(data.get("address") if "address" in data else current.get("address"),1000) or None,
              _clean(data.get("municipality") if "municipality" in data else current.get("municipality"),200) or None,
              _clean(data.get("assigned_to") if "assigned_to" in data else current.get("assigned_to"),200) or None,
              _clean(data.get("item_type") or current.get("item_type") or "ISSUE",50).upper(),
              _clean(data.get("next_action") if "next_action" in data else current.get("next_action"),2000) or None,
              _clean(data.get("waiting_on") if "waiting_on" in data else current.get("waiting_on"),500) or None,
              _clean(data.get("due_at"),40),
              _clean(data.get("follow_up_at"),40),
              status,issue_id,
            ),
        )
        return _json({"ok":True,"message":"Work item updated.","id":issue_id})

    if action=="WORK_NOTE":
        issue_id=_uid(data.get("id"))
        if not query_one("SELECT id FROM issues WHERE id=%s",(issue_id,)):
            raise HTTPException(404,"Work item not found")
        note=_clean(data.get("note"),10000)
        if not note:
            raise HTTPException(400,"Enter an update")
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO issue_updates(issue_id,author,note) VALUES(%s,%s,%s)",
                (issue_id,owner,note),
            )
        return _json({"ok":True,"message":"Update added."})

    if action in {"WORK_CHASED","WORK_RESPONSE"}:
        issue_id=_uid(data.get("id"))
        row=query_one(
            "SELECT id,waiting_on FROM issues WHERE id=%s AND status NOT IN ('RESOLVED','CLOSED')",
            (issue_id,),
        )
        if not row:
            raise HTTPException(404,"Work item not found")
        if not str(row.get("waiting_on") or "").strip():
            raise HTTPException(400,"This item is not waiting on anyone")
        if action=="WORK_CHASED":
            execute(
                """UPDATE issues SET waiting_on_last_chased=now(),
                   waiting_on_chase_count=coalesce(waiting_on_chase_count,0)+1,
                   updated_at=now() WHERE id=%s""",(issue_id,))
            message="Chase recorded."
        else:
            execute("UPDATE issues SET waiting_on=NULL,updated_at=now() WHERE id=%s",(issue_id,))
            message="Response received; Waiting On cleared."
        return _json({"ok":True,"message":message})

    if action=="ALERT_TO_WORK":
        alert_id=_uid(data.get("id"))
        alert=query_one(
            """SELECT alert_id,title,message,source,category,priority,municipality,
                      coalesce(nullif(location->>'address',''),nullif(location->>'label','')) AS address
               FROM alerts WHERE id=%s""",(alert_id,))
        if not alert:
            raise HTTPException(404,"Alert not found")
        with db_conn() as conn:
            row=conn.execute(
                """INSERT INTO issues(title,description,category,priority,status,source,address,
                                      municipality,item_type,next_action)
                   VALUES(%s,%s,%s,%s,'OPEN','ALERT',%s,%s,'ISSUE',%s)
                   RETURNING id""",
                (
                  alert["title"],
                  "Alert reference: "+str(alert["alert_id"])+"\nSource: "+str(alert["source"])+"\n\n"+str(alert.get("message") or ""),
                  alert.get("category"),
                  max(1,min(5,int(alert.get("priority") or 3))),
                  alert.get("address"),
                  alert.get("municipality"),
                  "Review, assign, and determine the municipal response.",
                ),
            ).fetchone()
        return _json({"ok":True,"message":"Alert added to Work.","issue_id":row["id"]})

    if action=="WATCH_TOGGLE":
        watch_id=_uid(data.get("id"))
        requested=str(data.get("state") or "").lower().strip()
        if requested not in {"pause","activate","reactivate"}:
            raise HTTPException(400,"Choose Pause or Reactivate")
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT active,expires_at FROM watch_items WHERE id=%s FOR UPDATE",
                    (watch_id,),
                )
                row=cur.fetchone()
                if not row:
                    raise HTTPException(404,"Watch not found")
                next_active=requested in {"activate","reactivate"}
                clear_expired=bool(
                    next_active and row.get("expires_at") and row["expires_at"]<=datetime.now(timezone.utc)
                )
                cur.execute(
                    """UPDATE watch_items SET active=%s,
                       starts_at=CASE WHEN %s THEN NULL ELSE starts_at END,
                       expires_at=CASE WHEN %s THEN NULL ELSE expires_at END,
                       updated_at=now() WHERE id=%s""",
                    (next_active,clear_expired,clear_expired,watch_id),
                )
                if next_active:
                    require_watch_recipients(cur,[watch_id])
        return _json({"ok":True,"message":"Watch is on" if next_active else "Watch paused."})

    if action=="INBOX_HANDLE":
        item_kind=_clean(data.get("kind"),30)
        item_id=_uid(data.get("id"))
        handled=data.get("handled") is not False
        with db_conn() as conn:
            if handled:
                conn.execute(
                    """INSERT INTO workspace_inbox_handled(owner_username,kind,item_id)
                       VALUES(%s,%s,%s)
                       ON CONFLICT(owner_username,kind,item_id) DO UPDATE SET handled_at=now()""",
                    (owner,item_kind,item_id),
                )
            else:
                conn.execute(
                    "DELETE FROM workspace_inbox_handled WHERE owner_username=%s AND kind=%s AND item_id=%s",
                    (owner,item_kind,item_id),
                )
        return _json({"ok":True,"message":"Inbox updated."})

    raise HTTPException(400,"Unknown action")
