"""One-time, exact-source navigation repair; no production or database access."""
from pathlib import Path
import hashlib
import re
D=Path(__file__).resolve().parents[1]/'dashboard'
def edit(name,fn):
    p=D/name;s=p.read_text();n=fn(s);assert n!=s,name;p.write_text(n)
def once(s,a,b):
    assert s.count(a)==1,(a[:80],s.count(a));return s.replace(a,b)
def macro(s):
    s=once(s,"  if(params.get('embed')==='1'){\n    document.body.classList.add('cmos-embedded');\n    return;\n  }","  // Old embed bookmarks now open ordinary full pages.\n  if(params.has('embed')){\n    params.delete('embed');\n    history.replaceState(history.state,'',location.pathname+(params.size?'?'+params:'')+location.hash);\n  }")
    a=s.index('  const embedded=url=>{');b=s.index('  // Command palette.',a);s=s[:a]+s[b:]
    s=s.replace('↑↓ select · Enter open · Shift+Enter Quick Look','↑↓ select · Enter open full page')
    s=s.replace('quick?openSidecar(route.url,route.title):location.assign(route.url)','navigatePage(route.url,route.title)').replace('quick?openSidecar(item.url,item.title):location.assign(item.url)','navigatePage(item.url,item.title)')
    a=s.index('  // Universal Quick Look sidecar.');b=s.index('  // Reorganize inherited module layouts',a)
    s=s[:a]+'''  // Private pages intentionally deny framing. Never embed login, editing,
  // mapping or sharing routes in a blocked iframe.
  function navigatePage(url,title=''){
    const target=new URL(url,location.href);
    if(!['http:','https:'].includes(target.protocol))return;
    if(target.origin===location.origin){
      target.searchParams.delete('embed');
      remember(target.pathname+target.search+target.hash,title||target.pathname);
    }
    location.assign(target.href);
  }
  // Compatibility for existing callers; no cmos-sidecar is constructed.
  function openSidecar(url,title=''){navigatePage(url,title);}
  // Anchors retain native behavior, including Ctrl-click and new tabs.
  document.addEventListener('click',event=>{
    if(event.defaultPrevented||event.button!==0)return;
    const target=event.target.closest('[data-cmos-context]');
    if(!target||event.target.closest('a,button,input,select,textarea,summary'))return;
    const url=target.dataset.cmosContext;
    if(!url)return;
    event.preventDefault();
    if(event.ctrlKey||event.metaKey||event.shiftKey){
      const destination=new URL(url,location.href);
      if(destination.origin===location.origin)window.open(destination.href,'_blank','noopener');
      return;
    }
    if(target.dataset.cmosOpen==='full'){navigatePage(url,target.dataset.cmosTitle);return;}
    navigatePage(url,target.dataset.cmosTitle);
  });
  document.addEventListener('keydown',event=>{
    if(event.defaultPrevented)return;
    if((event.metaKey||event.ctrlKey)&&!event.shiftKey&&event.key.toLowerCase()==='p'){
      event.preventDefault();openCommand();return;
    }
    if(event.key!=='Enter'&&event.key!==' ')return;
    const row=event.target.closest?.('[data-cmos-context]');
    if(!row||event.target.closest('a,button,input,select,textarea,summary,[contenteditable]'))return;
    event.preventDefault();navigatePage(row.dataset.cmosContext,row.dataset.cmosTitle);
  });
  // Legacy data-cmos-quicklook anchors are metadata only, never intercepted.
  window.CMOS={openSidecar,openCommand,navigatePage};

'''+s[b:];return s
edit('static/macro_layer.js',macro)
def routes(s):
    a=s.index("@app.get('/intake')");b=s.index("@app.get('/workspace/api/state')",a)
    return s[:a]+'''@app.get('/intake')
def executive_intake_route(request: Request):
    return _workspace_response(request, 'inbox')


@app.get('/library')
def workspace_library_route(request: Request):
    return _workspace_response(request, 'library')


def _workspace_response(request: Request, view: str):
    _owner(request)
    display = request.url.path.endswith('/display')
    return templates.TemplateResponse(request=request, name='workspace.html', context={
        'csrf': _csrf(request), 'display': display, 'initial_view': view,
        'page': 'Executive Intake' if view == 'inbox' else 'Library' if view == 'library' else 'Workspace',
        'readonly': request.state.cmos_role == 'READ_ONLY',
        'username': request.state.cmos_account.username,
    })


@app.get('/workspace',response_class=HTMLResponse)
@app.get('/workspace/display',response_class=HTMLResponse)
def workspace_page(request: Request):
    _owner(request)
    display = request.url.path.endswith('/display')
    view = request.query_params.get('view', 'today')
    # Saved links retain their exact source and filters on the canonical route.
    if not display and view in {'inbox', 'library'}:
        params = [(k, v) for k, v in request.query_params.multi_items() if k not in {'view', 'embed'}]
        suffix = '&'.join(quote_plus(k)+'='+quote_plus(v) for k, v in params)
        target = '/intake' if view == 'inbox' else '/library'
        return RedirectResponse(target+('?' + suffix if suffix else ''), status_code=303)
    return _workspace_response(request, 'intelligence' if display else view)


'''+s[b:]
edit('workspace_app.py',routes)
def html(s):
    s=s.replace('<title>Workspace · City Manager OS</title>','<title>{{ page|default("Workspace") }} · City Manager OS</title>')
    s=s.replace('<body data-username=', '<body class="workspace-page" data-initial-view="{{ initial_view|default(\'today\') }}" data-username=',1)
    a=s.index('<div class="app-shell">');b=s.index('{% include "workspace_hub.html" %}',a)
    s=s[:a]+'''{% if not display %}{% include "nav.html" %}{% endif %}
<div class="main workspace-main">
  <header class="workspace-toolbar">
    <div class="row"><strong id="breadcrumb">{{ page|default('Workspace') }}</strong><button type="button" id="global-search" class="global-search">Search intake <kbd>Ctrl K</kbd></button></div>
    <div class="row"><span class="badge" id="mode-badge">Your workspace</span><span id="loading-indicator" class="loading-indicator" role="status" hidden>Loading…</span><span id="refresh-time" class="muted"></span><button type="button" id="refresh">Refresh</button></div>
  </header>
  {% if not display %}
  <nav class="workspace-section-nav" aria-label="Workspace sections">
    <a href="/intake" data-workspace-view="inbox">Intake</a>
    <a href="/library" data-workspace-view="library">Library</a>
    <a href="/workspace?view=today" data-workspace-view="today">Workspace Today</a>
    <a href="/workspace?view=people" data-workspace-view="people">People, places &amp; projects</a>
    <a href="/workspace?view=dates" data-workspace-view="dates">Important dates</a>
    <a href="/workspace?view=settings" data-workspace-view="settings">Workspace settings</a>
  </nav>
  {% endif %}
  <div id="notice" role="status" aria-live="polite"></div><main class="workspace-content">
'''+s[b:]
    s=once(s,'</main></div></div>','</main></div>')
    scripts=re.findall(r'<script src="/static/workspace(?:_hub)?\.js[^>]*></script>',s);assert len(scripts)==2
    for script in scripts:s=s.replace(script,'',1)
    s=s.replace('</body>','\n'+''.join(scripts)+'\n</body>')
    s=s.replace('<script src="/static/appearance.js?v=20261003-1" defer></script>','{% if display %}<script src="/static/appearance.js?v=20261003-1" defer></script>{% endif %}')
    s=s.replace('<link rel="stylesheet" href="/static/workspace.css','<link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/workspace.css',1)
    return s
edit('templates/workspace.html',html)
def workspace(s):
    s=once(s,"    if (updateHash && !display) {\n      const url=new URL(location.href);\n      url.searchParams.set('view',view);", "    if (updateHash && !display) {\n      const url=new URL(location.href);\n      url.pathname=view==='inbox'?'/intake':view==='library'?'/library':'/workspace';\n      if(view==='inbox'||view==='library')url.searchParams.delete('view');\n      else url.searchParams.set('view',view);")
    s=s.replace("    all('[data-view]').forEach(b => {","    all('[data-workspace-view]').forEach(b => {").replace('      if (b.dataset.view === view)','      if (b.dataset.workspaceView === view)')
    s=once(s,'    closeNavigation();\n    return load();','''    document.querySelectorAll('.cmos-rail-nav a').forEach(a=>{
      const match=new URL(a.href,location.href).pathname===location.pathname;
      a.classList.toggle('active',match);
      if(match)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');
    });
    const title=document.querySelector('.cmos-global-page strong');
    if(title)title.textContent=$('breadcrumb').textContent;
    document.querySelector('.cmos-rail-nav a.active')?.closest('details')?.setAttribute('open','');
    return load();''')
    a=s.index('  function closeNavigation()');b=s.index('  function setOptions(',a);s=s[:a]+s[b:]
    s=s.replace("$('workspace-name').textContent=state.config.name; $('organization').textContent=state.config.organization;","document.title=($('breadcrumb').textContent||'Workspace')+' · '+state.config.name;")
    s=s.replace("    document.title=state.config.name+(display?' · Area display':' · Workspace');","    if(display)document.title=state.config.name+' · Area display';")
    s=s.replace("$('workspace-name').textContent=data.config.name; $('organization').textContent=data.config.organization;","document.title=($('breadcrumb').textContent||'Workspace')+' · '+data.config.name;")
    s=s.replace("  all('[data-view]').forEach(b=>b.addEventListener('click',()=>safely(()=>navigate(b.dataset.view))));\n",'')
    a=s.index("  $('nav-toggle').addEventListener");b=s.index('  if(display){',a)
    s=s[:a]+'''  function viewFromLocation(){
    if(location.pathname==='/intake')return 'inbox';
    if(location.pathname==='/library')return 'library';
    const view=new URLSearchParams(location.search).get('view');
    return views.includes(view)?view:'today';
  }
  window.addEventListener('popstate',()=>location.reload());
'''+s[b:]
    s=once(s,"  const incomingView=new URLSearchParams(location.search).get('view');\n  safely(()=>navigate(display?'intelligence':(views.includes(incomingView)?incomingView:'today'),false));","  const incomingView=document.body.dataset.initialView||viewFromLocation();\n  safely(()=>navigate(display?'intelligence':(views.includes(incomingView)?incomingView:'today'),false));")
    return s
edit('static/workspace.js',workspace)
edit('static/workspace_hub.js',lambda s:once(s,"document.querySelector('.topbar').offsetHeight","(document.querySelector('.workspace-toolbar,.topbar')?.offsetHeight||50)").replace("    if(view!=='inbox'||location.pathname!=='/intake')return;","    if(!((view==='inbox'&&location.pathname==='/intake')||(view==='library'&&location.pathname==='/library')))return;"))
edit('templates/context.html',lambda s:re.sub(r'\sdata-cmos-quicklook data-cmos-title="[^"]*"','',s))
def nav(s):
    s=once(s,'      <a href="/brain"','      <a href="/library" class="{% if p.startswith(\'/library\') %}active{% endif %}"><span>▤</span>Library</a>\n      <a href="/brain"')
    s=s.replace('      <a href="/today-board"','      <a href="/today-board/setup">Today Board Setup</a>\n      <a href="/today-board"',1)
    return s.replace('      <a href="/workspace?view=today"','      <a href="/workspace/display" target="_blank" rel="noopener">Area Display</a>\n      <a href="https://ops.nhnj.us/staff" target="_blank" rel="noopener">Employee Portal</a>\n      <a href="/workspace?view=today"',1)
edit('templates/nav.html',nav)
edit('static/workspace.css',lambda s:s+'''
/* Workspace shares the app rail with Intake, Alerts and the other modules. */
.workspace-page .workspace-main{min-width:0;width:100%}
.workspace-toolbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;padding:14px 22px;border-bottom:1px solid var(--line);background:var(--paper)}
.workspace-section-nav{display:flex;flex-wrap:wrap;gap:4px 12px;padding:9px 22px;border-bottom:1px solid var(--line)}
.workspace-section-nav a{display:block;padding:6px 8px;color:var(--muted);font-size:13px;border-radius:6px}
.workspace-section-nav a[aria-current=page]{background:var(--soft);color:var(--ink);font-weight:650}
.workspace-page .workspace-content{width:100%;max-width:none;margin:0;padding:22px}
.workspace-page .workspace-content .panel{padding:20px}
.workspace-page [hidden]{display:none!important}
.workspace-page input[type=checkbox],.workspace-page input[type=radio]{width:auto;min-height:0}
.workspace-page .cmos-rail .cmos-rail-home{text-decoration:none;color:#fff}
.workspace-page .cmos-rail .cmos-rail-nav a{text-decoration:none}
.workspace-page.display{padding:0}
.workspace-page.display .workspace-toolbar{display:none}
@media(max-width:840px){.workspace-toolbar{padding:10px 13px}.workspace-page .workspace-content{padding:14px 13px}.workspace-section-nav{padding:8px 13px;gap:3px}.workspace-page .workspace-content .panel{padding:14px}}
''')
edit('tests/test_workspace.py',lambda s:once(s,"    assert 'All tools' in response.text\n    assert 'Everything, still here.' in response.text","    assert response.text.count('class=\"cmos-rail\"') == 1\n    assert 'id=\"rail\"' not in response.text\n    assert 'aria-label=\"Workspace sections\"' in response.text"))
edit('tests/test_macro_layer.py',lambda s:s.replace('def test_macro_layer_has_command_palette_and_quicklook():','def test_macro_layer_has_command_palette_and_native_navigation():').replace('    assert "cmos-sidecar" in js','    assert "function navigatePage" in js\n    assert "<iframe" not in js\n    assert "frame.src" not in js'))
p=D.parent/'ci/check_release_browser.py';s=p.read_text();s=once(s,"        expect(page.locator('body')).to_have_class('cmos-embedded')\n        expect(page.locator('.cmos-rail')).to_be_hidden()\n        expect(page.locator('.cmos-global-bar')).to_be_hidden()\n        passed('Embedded preview: no duplicate sidebar or global header')","        expect(page.locator('.cmos-rail')).to_be_visible()\n        expect(page.locator('.cmos-global-bar')).to_be_visible()\n        expect(page.locator('iframe')).to_have_count(0)\n        assert 'embed' not in parse_qs(urlsplit(page.url).query)\n        passed('Legacy embed link: normal page with one sidebar and no iframe')");p.write_text(s)
for name in ['templates/nav.html','templates/workspace.html','templates/context.html']:
    p=D/name;s=p.read_text();s=re.sub(r'/static/((?:macro_layer|workspace(?:_hub)?|context)\.(?:css|js))\?v=[A-Za-z0-9-]+',lambda m:'/static/'+m[1]+'?v='+hashlib.sha256((D/'static'/m[1]).read_bytes()).hexdigest()[:12],s);p.write_text(s)
for p in D.glob('*.py'):compile(p.read_text(),str(p),'exec')
print('Native navigation source repair applied; private frame/security headers unchanged.')
