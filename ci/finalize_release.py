"""One-time exact-match repair of the October 5 candidate. No database or deployment access."""
from pathlib import Path
import hashlib
import re

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / 'dashboard'
changes = {}

def read(name):
    return (DASHBOARD / name).read_text(encoding='utf-8')

def replace(source, old, new, label):
    if source.count(old) != 1:
        raise RuntimeError(f'{label}: expected exactly one unchanged target; refusing partial repair')
    return source.replace(old, new, 1)

if 'ASSET_CONTENT_CONTRACT' in read('tests/test_executive_intake_context_map.py'):
    print('Source repair already applied.')
    raise SystemExit(0)

s = read('static/workspace_hub.js')
s = replace(s, 'async function load(nextView,force=false,append=false) {', 'async function load(nextView,force=false,append=false,retainExact=false) {', 'Intake load signature')
s = replace(s,
    'if(selected&&!pendingOpen){const row=items.find(r=>r.id===selected.id&&r.kind===selected.kind);if(row)await open(row,false);else closePreview();}',
    'if(selected&&!pendingOpen){const row=items.find(r=>r.id===selected.id&&r.kind===selected.kind);if(row)await open(row,false);else if(retainExact)await openExact(selected.kind,selected.id);else closePreview();}',
    'Intake off-page selection')
s = replace(s, 'await safe(()=>load(current,true));', 'await safe(()=>load(current,true,false,true));', 'Intake automatic refresh')
s = replace(s, "row.snippet.replace(/\\s+/g,' ')", "String(row.snippet||'').replace(/\\s+/g,' ')", 'Intake nullable snippet')
s = replace(s, "row.status.replaceAll('_',' ').toLowerCase()", "String(row.status||'').replaceAll('_',' ').toLowerCase()", 'Intake nullable status')
s = replace(s, "item.status.replaceAll('_',' ').toLowerCase()", "String(item.status||'').replaceAll('_',' ').toLowerCase()", 'Intake nullable detail status')
s = replace(s, 'const meta=item.metadata;', 'const meta=item.metadata||{};', 'Intake optional metadata')
s = replace(s, 'if(meta.recipients.length)', 'if(Array.isArray(meta.recipients)&&meta.recipients.length)', 'Intake optional recipients')
changes['static/workspace_hub.js'] = s

s = read('static/macro_layer.js')
start = s.index('  // Mobile rail.')
end = s.index('  // Universal Quick Look sidecar.', start)
s = s[:start] + '''  // One mobile navigation state, with keyboard and focus recovery.
  const mobileToggle=document.querySelector('.cmos-mobile-rail-toggle');
  const railBackdrop=document.querySelector('.cmos-rail-backdrop');
  const rail=document.querySelector('.cmos-rail');
  function setMobileNavigation(open){
    document.body.classList.toggle('cmos-mobile-nav-open',open);
    if(railBackdrop)railBackdrop.hidden=!open;
    if(mobileToggle)mobileToggle.setAttribute('aria-expanded',String(open));
    if(open)rail?.querySelector('a.active,a')?.focus();
    else mobileToggle?.focus();
  }
  if(mobileToggle&&railBackdrop){
    mobileToggle.setAttribute('aria-expanded','false');
    mobileToggle.addEventListener('click',()=>setMobileNavigation(!document.body.classList.contains('cmos-mobile-nav-open')));
    railBackdrop.addEventListener('click',()=>setMobileNavigation(false));
    document.addEventListener('keydown',event=>{
      if(event.key==='Escape'&&document.body.classList.contains('cmos-mobile-nav-open')){
        event.preventDefault();setMobileNavigation(false);
      }
    });
    matchMedia('(max-width:840px)').addEventListener('change',event=>{
      if(!event.matches&&document.body.classList.contains('cmos-mobile-nav-open'))setMobileNavigation(false);
    });
  }
  document.querySelector('[data-cmos-capture]')?.addEventListener('click',event=>{
    const capture=document.getElementById('qc-open');
    if(capture){event.preventDefault();capture.click();}
  });

''' + s[end:]
s = replace(s, "    const a=e.target.closest('a[data-cmos-quicklook]');\n    if(!a)return;", "    const a=e.target.closest('a[data-cmos-quicklook]');\n    if(!a||e.button!==0||e.ctrlKey||e.metaKey||e.shiftKey||e.altKey)return;", 'Preview modified clicks')
changes['static/macro_layer.js'] = s

s = read('templates/nav.html')
s = replace(s, '<aside class="cmos-rail" aria-label="Primary navigation">', '<aside class="cmos-rail" id="cmos-primary-navigation" aria-label="Primary navigation">', 'Navigation identifier')
s = replace(s, 'class="cmos-mobile-rail-toggle" aria-label="Open navigation"', 'class="cmos-mobile-rail-toggle" aria-controls="cmos-primary-navigation" aria-expanded="false" aria-label="Toggle navigation"', 'Navigation accessibility')
s = replace(s, '''<button type="button" class="cmos-global-capture" onclick="document.getElementById('qc-open')?.click()">+ Capture</button>''', '<a href="/inbox" class="cmos-global-capture" data-cmos-capture>+ Capture</a>', 'Capture fallback')
s = replace(s, '<span>Systems connected</span>', '<span>Private workspace</span>', 'Unverified connection indicator')
s = s.replace('<span class="cmos-rail-live"></span>', '<span aria-hidden="true">◈</span>')
changes['templates/nav.html'] = s

s = read('static/macro_layer.css')
s = replace(s, 'body.cmos-embedded>.mobile-nav,', 'body.cmos-embedded>.mobile-nav,\nbody.cmos-embedded>.cmos-rail,\nbody.cmos-embedded>.cmos-global-bar,\nbody.cmos-embedded>.cmos-rail-backdrop,', 'Preview outer chrome')
start = s.index('/* Executive workspace cohesion pass v3 */')
v3 = s[start:]
for old, new in [('font-size:7px','font-size:10px'), ('font-size:7.5px','font-size:10px'), ('font-size:10.5px','font-size:13px'), ('font-size:9.5px','font-size:12px'), ('font-size:10px','font-size:12px'), ('font-size:9px','font-size:12px'), ('min-height:32px','min-height:38px'), ('min-height:28px','min-height:36px'), ('font-size:19px!important','font-size:22px!important'), ('font-size:17px!important','font-size:20px!important')]:
    v3 = v3.replace(old, new)
v3 = v3.replace('.cmos-global-actions>a{display:none}', '.cmos-global-actions>a:not(.cmos-global-capture){display:none}')
s = s[:start] + v3
changes['static/macro_layer.css'] = s

s = read('templates/alerts.html')
s = replace(s, 'action="/alerts/bulk-action" data-alert-bulk-form>', 'action="/alerts/bulk-action" id="alert-bulk-form" data-alert-bulk-form>', 'Bulk form ID')
s = replace(s, '''      {% endif %}
      <div class="data-list alert-search-results">''', '''      </form>
      {% endif %}
      <div class="data-list alert-search-results">''', 'Bulk form boundary')
s = replace(s, '''      {% if alerts %}</form>{% endif %}''', '', 'Old bulk closing tag')
s = replace(s, 'name="alert_ids" value="{{ a.alert_uuid }}" data-alert-select', 'name="alert_ids" value="{{ a.alert_uuid }}" form="alert-bulk-form" data-alert-select', 'Checkbox form association')
s = replace(s, "const choices = [...form.querySelectorAll('[data-alert-select]')];", "const choices = [...document.querySelectorAll('[data-alert-select]')];", 'Bulk checkbox lookup')
changes['templates/alerts.html'] = s

assets = {
    'templates/workspace.html': ['workspace.js','workspace_hub.js','workspace.css'],
    'templates/nav.html': ['macro_layer.css','macro_layer.js'],
    'templates/context.html': ['context.css'],
}
for name, names in assets.items():
    s = changes.get(name, read(name))
    for asset in names:
        source = changes.get('static/'+asset)
        raw = source.encode('utf-8') if source is not None else (DASHBOARD/'static'/asset).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()[:12]
        s, count = re.subn(re.escape(asset)+r'\?v=[A-Za-z0-9._-]+', asset+'?v='+digest, s)
        if count < 1:
            raise RuntimeError(f'Missing versioned asset {asset} in {name}')
    changes[name] = s

s = read('tests/test_executive_intake_context_map.py')
s = replace(s, 'import json\n', 'import json\nimport hashlib\n', 'Hash import')
s = replace(s, 'deploy_root=Path("/deploy")', 'deploy_root=ROOT.parent/"deploy"', 'Portable source test path')
start = s.index('    assert "workspace.js?v=')
end = s.index('\n\n\ndef test_alert_timeline_editor', start)
s = s[:start] + '''    # ASSET_CONTENT_CONTRACT: real files plus content-derived cache revisions.
    for document, assets in (
        (page, ('workspace.js', 'workspace_hub.js', 'workspace.css')),
        (nav, ('macro_layer.css', 'macro_layer.js')),
        (context, ('context.css',)),
    ):
        for asset in assets:
            path = ROOT / 'static' / asset
            assert path.is_file(), asset
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            assert f'{asset}?v={digest}' in document, f'Stale or missing cache key: {asset}'
''' + s[end:]
changes['tests/test_executive_intake_context_map.py'] = s

# All targets must validate before the first write.
for name, content in changes.items():
    if name.endswith('.py'):
        compile(content, name, 'exec')
for name, content in changes.items():
    (DASHBOARD/name).write_text(content, encoding='utf-8')
    print('REPAIRED', name)
