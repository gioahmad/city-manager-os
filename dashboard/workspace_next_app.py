"""Unified City Manager OS shell.

This is a parallel presentation layer over existing authoritative tables and
engines. Existing routes remain available as advanced controls.
"""
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from app import app, templates
from brain_app import _owner
from unified_objects import OBJECT_KINDS, home_snapshot, object_detail, search_objects
from workspace_hub import list_items


def _json(value):
    return JSONResponse(jsonable_encoder(value), headers={"Cache-Control":"no-store"})


@app.get("/workspace-next", response_class=HTMLResponse)
def workspace_next(request: Request):
    owner=_owner(request)
    return templates.TemplateResponse(
        request=request,
        name="workspace_next.html",
        context={"username":owner},
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
