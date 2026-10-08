"""Shared workspace tool switches; ingestion and delivery keep their own controls."""
from urllib.parse import urlencode

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from app import db_conn, query_one, templates
from brain_app import _csrf, _owner, _write

# Keep shared services (geocoding, search, recipients and delivery) available.
MODULES = {
    'brain': ('Brain', 'Private notes, ideas, tasks and attachments.', ('/brain', '/workspace/brain.md')),
    'mapping': ('Mapping', 'Map, GIS layers, drawings and flood views.', ('/map', '/flood')),
    'events': ('Events', 'Manage operational events and their checklists.', ('/schedule',)),
    'event_intelligence': ('Event Intelligence', 'Review regional notices and event intelligence.', ('/event-intelligence',)),
    'transit': ('Transit', 'Review transit observations and manage transit monitoring.', ('/transit',)),
    'places': ('Places / References', 'Browse and manage canonical places and regional references.', ('/spatial-reference', '/api/spatial-reference')),
    'staff': ('Staff', 'Manage staff, assignments and field work. The employee portal stays available.', ('/staff-admin',)),
    'today_board': ('Today Board', 'Configure and review daily operating constants.', ('/today-board',)),
    'routines': ('Routines', 'Manage recurring operations and their run records.', ('/operations-routines', '/operations-runs')),
}


def module_for_path(path):
    return next((key for key, (_, _, paths) in MODULES.items()
                 if any(path == p or path.startswith(p + '/') for p in paths)), None)


def disabled_modules():
    settings = query_one('SELECT settings FROM workspace_config WHERE singleton=true').get('settings') or {}
    values = settings.get('modules')
    return [key for key in MODULES if isinstance(values, dict) and values.get(key) is False]


def require_enabled(request, key):
    if key in getattr(request.state, 'disabled_modules', ()):
        raise HTTPException(423, f'{MODULES[key][0]} is turned off. An Executive can turn it on in Modules.')


def configure_workspace_modules(app):
    @app.middleware('http')
    async def workspace_module_access(request: Request, call_next):
        # Private authentication runs outside this middleware. Public endpoints,
        # assets and automation must not acquire a new dependency on UI settings.
        account = getattr(request.state, 'cmos_account', None)
        if not account or account.username == 'automation':
            return await call_next(request)
        disabled = await run_in_threadpool(disabled_modules)
        request.state.disabled_modules = disabled
        request.state.disabled_tool_paths = [p for key in disabled for p in MODULES[key][2]]
        key = module_for_path(request.url.path)
        if request.url.path in {'/workspace', '/workspace/api/state'} and request.query_params.get('view') == 'brain':
            key = 'brain'
        if key in disabled:
            detail = f'{MODULES[key][0]} is turned off. An Executive can turn it on in Modules.'
            if request.method == 'GET' and 'text/html' in request.headers.get('accept', ''):
                return templates.TemplateResponse(request=request, name='workspace_modules.html', status_code=423,
                    context={'page': 'Module turned off', 'blocked_module': MODULES[key][0], 'modules': [],
                             'can_edit': account.role == 'EXECUTIVE', 'csrf': _csrf(request)})
            return JSONResponse({'detail': detail, 'modules_url': '/modules'}, status_code=423)
        return await call_next(request)

    @app.get('/modules', response_class=HTMLResponse)
    def module_settings(request: Request):
        _owner(request)
        disabled = getattr(request.state, 'disabled_modules', ())
        return templates.TemplateResponse(request=request, name='workspace_modules.html', context={
            'page': 'Modules', 'blocked_module': None, 'csrf': _csrf(request),
            'can_edit': request.state.cmos_role == 'EXECUTIVE',
            'modules': [{'key': key, 'label': label, 'description': description, 'enabled': key not in disabled}
                        for key, (label, description, _) in MODULES.items()],
        })

    @app.post('/modules/{key}')
    def set_module(key: str, request: Request, csrf: str = Form(...), enabled: str = Form(...)):
        _write(request, csrf)
        if request.state.cmos_role != 'EXECUTIVE':
            raise HTTPException(403, 'Executive settings only.')
        if key not in MODULES or enabled not in {'true', 'false'}:
            raise HTTPException(400, 'Choose a listed module and an on/off state.')
        value = {key: enabled == 'true'}
        with db_conn() as conn:
            conn.execute("""INSERT INTO workspace_config(singleton,settings)
                VALUES(true,jsonb_build_object('modules',%s::jsonb))
                ON CONFLICT(singleton) DO UPDATE SET settings=jsonb_set(workspace_config.settings,'{modules}',
                  (CASE WHEN jsonb_typeof(workspace_config.settings->'modules')='object'
                        THEN workspace_config.settings->'modules' ELSE '{}'::jsonb END) || %s::jsonb)""",
                (Jsonb(value), Jsonb(value)))
        msg = MODULES[key][0] + (' turned on.' if enabled == 'true' else ' turned off.')
        return RedirectResponse('/modules?' + urlencode({'msg': msg}), status_code=303)
