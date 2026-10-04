(() => {
  const $ = id => document.getElementById(id);
  const state = {view:'home',kind:'',query:'',selected:null,home:null};

  function esc(v){
    return String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;')
      .replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;");
  }
  function pretty(v){return String(v||'').replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());}
  function when(v){
    if(!v)return '';
    const d=new Date(v); if(Number.isNaN(d.getTime()))return String(v);
    return d.toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  }
  async function api(url){
    const r=await fetch(url,{cache:'no-store'});
    if(!r.ok)throw new Error('Request failed ('+r.status+')');
    return r.json();
  }
  function setNav(view){
    state.view=view;
    document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
    const workbench=document.querySelector('.workbench');
    if(workbench)workbench.classList.toggle('map-mode',view==='map');
    document.body.classList.remove('nav-open');
    $('rail-backdrop').hidden=true;
  }
  function head(eyebrow,title,description,right=''){
    return '<div class="page-head"><div><div class="eyebrow">'+esc(eyebrow)+'</div><h1>'+esc(title)+'</h1>'+
      (description?'<p class="muted">'+esc(description)+'</p>':'')+'</div>'+right+'</div>';
  }
  function emptyContext(title='Operational context',body='Select a record to see everything connected to it.'){
    $('context').classList.remove('open');
    $('context').innerHTML='<div class="context-empty"><div class="context-mark">CM</div><strong>'+esc(title)+
      '</strong><span>'+esc(body)+'</span></div>';
  }
  function badge(text,type=''){return '<span class="pill '+type+'">'+esc(text)+'</span>';}
  function rowItem(title,detail,meta='',tag=''){
    return '<div class="row-item"><div class="row-main">'+(tag?tag:'')+'<strong>'+esc(title)+'</strong>'+
      (detail?'<p>'+esc(detail)+'</p>':'')+'</div>'+(meta?'<div class="row-meta">'+esc(meta)+'</div>':'')+'</div>';
  }
  function objectRows(items){
    if(!items.length)return '<div class="card-empty">Nothing matches this view.</div>';
    return items.map(item=>{
      const key=item.kind+':'+item.id;
      return '<button class="object-row '+(state.selected===key?'active':'')+'" data-object-kind="'+esc(item.kind)+
        '" data-object-id="'+esc(item.id)+'"><span class="object-kind">'+esc(item.kind)+'</span><span class="object-copy"><strong>'+
        esc(item.title)+'</strong><span>'+esc(item.subtitle||'')+'</span></span><span class="object-meta">'+esc(item.meta||'')+'</span></button>';
    }).join('');
  }
  function bindObjects(){
    document.querySelectorAll('[data-object-kind]').forEach(b=>b.addEventListener('click',()=>openObject(b.dataset.objectKind,b.dataset.objectId)));
  }

  async function renderHome(){
    setNav('home');
    $('canvas').innerHTML=head('EXECUTIVE WORKSPACE','Home','What needs attention, what is happening now, and what changed.');
    $('canvas').insertAdjacentHTML('beforeend','<div class="placeholder">Loading your operational picture…</div>');
    emptyContext('Today at a glance','Live operational context will stay visible here.');

    try{
      const data=await api('/api/workspace-next/home');
      state.home=data;
      const c=data.counts||{};
      let html=head('EXECUTIVE WORKSPACE','Home','What needs attention, what is happening now, and what changed.');
      html+='<div class="metric-grid">'+
        '<div class="metric"><small>Open work</small><strong>'+esc(c.open_work||0)+'</strong><span>Active municipal work</span></div>'+
        '<div class="metric"><small>Alerts · 24h</small><strong>'+esc(c.alerts_24h||0)+'</strong><span>Incoming intelligence</span></div>'+
        '<div class="metric"><small>Active watches</small><strong>'+esc(c.active_watches||0)+'</strong><span>Monitoring rules on</span></div>'+
        '<div class="metric"><small>Exceptions</small><strong>'+esc((Number(c.failed_24h||0)+Number(c.unhealthy_sources||0)))+
        '</strong><span>Delivery or source issues</span></div></div>';

      const attention=(data.attention||[]).map(x=>rowItem(
        x.title,x.next_action||x.waiting_on||x.address||x.status,when(x.updated_at),
        badge(x.attention_reason,x.attention_reason==='OVERDUE'?'danger':x.attention_reason==='WAITING'?'warning':'')
      )).join('')||'<div class="card-empty">No urgent work exceptions.</div>';

      const alerts=(data.alerts||[]).slice(0,8).map(x=>rowItem(
        x.title,[x.source,x.municipality].filter(Boolean).join(' · '),when(x.received_at),
        badge('P'+x.priority,x.priority>=4?'danger':'')
      )).join('')||'<div class="card-empty">No recent alerts.</div>';

      const events=(data.events||[]).slice(0,7).map(x=>rowItem(
        x.title,x.venue||x.address||x.municipality||x.event_type,when(x.starts_at),
        badge(x.impact_level||'EVENT')
      )).join('')||'<div class="card-empty">No upcoming events in view.</div>';

      html+='<div class="home-grid"><div class="stack">'+
        '<section class="card"><div class="card-head"><h2>Needs attention</h2><button class="link-button" data-jump="work">Open work</button></div><div class="card-body">'+attention+'</div></section>'+
        '<section class="card"><div class="card-head"><h2>Live intelligence</h2><button class="link-button" data-ops-kind="ALERT">View alerts</button></div><div class="card-body">'+alerts+'</div></section>'+
        '</div><div class="stack">'+
        '<section class="card"><div class="card-head"><h2>Upcoming</h2><span class="small muted">Next 14 days</span></div><div class="card-body">'+events+'</div></section>'+
        '<section class="card"><div class="card-head"><h2>Recent activity</h2></div><div class="card-body">'+
        (data.recent||[]).slice(0,10).map(x=>rowItem(x.title,x.detail,when(x.occurred_at),badge(x.kind))).join('')+
        '</div></section></div></div>';
      $('canvas').innerHTML=html;

      const unhealthy=(data.source_health||[]);
      const failed=(data.failed_deliveries||[]);
      $('context').innerHTML='<div class="context-head"><div class="eyebrow">SYSTEM PULSE</div><h2>Operational snapshot</h2><p class="muted small">Exceptions stay visible without becoming separate apps.</p></div>'+
        '<div class="context-body">'+
        '<section class="context-section"><h3>Source health</h3>'+
        (unhealthy.length?unhealthy.map(x=>'<div class="related"><strong>'+esc(x.source_id)+'</strong><p>'+esc(x.status)+(x.last_error?' · '+esc(x.last_error):'')+'</p></div>').join(''):
          '<div class="related"><strong>All monitored sources OK</strong></div>')+'</section>'+
        '<section class="context-section"><h3>Delivery failures · 24h</h3>'+
        (failed.length?failed.map(x=>'<div class="related"><strong>'+esc(x.alert_title)+'</strong><p>'+esc(x.subscriber_name)+' · '+esc(x.error_message||x.status)+'</p></div>').join(''):
          '<div class="related"><strong>No failed deliveries</strong></div>')+'</section></div>';

      document.querySelectorAll('[data-jump]').forEach(b=>b.addEventListener('click',()=>openView(b.dataset.jump)));
      document.querySelectorAll('[data-ops-kind]').forEach(b=>b.addEventListener('click',()=>loadKind(b.dataset.opsKind,'operations')));
    }catch(e){$('canvas').innerHTML=head('EXECUTIVE WORKSPACE','Home','')+'<div class="placeholder">'+esc(e.message)+'</div>';}
  }

  async function loadKind(kind,view){
    setNav(view);
    const copy={
      WORK:['EXECUTION','Work','Issues, tasks, follow-ups, decisions and operational work.'],
      PERSON:['RELATIONSHIPS','People','Residents, staff, officials, vendors and connected contacts.'],
      PLACE:['SPATIAL CONTEXT','Places','Addresses, facilities, parcels, corridors and regional references.'],
      ALERT:['OPERATIONS','Alerts','Live intelligence from your existing alerting system.'],
      WATCH:['OPERATIONS','Watches','Topic and spatial monitoring with existing match history.'],
      EVENT:['OPERATIONS','Events','Regional events and operational impact.']
    }[kind];
    state.kind=kind; state.query='';
    $('canvas').innerHTML=head(copy[0],copy[1],copy[2])+
      '<div class="list-toolbar"><input id="view-query" type="search" placeholder="Filter '+esc(copy[1].toLowerCase())+'…"></div>'+
      '<div class="object-list"><div class="card-empty">Loading…</div></div>';
    emptyContext(copy[1],'Select a record to open its context.');
    try{
      const data=await api('/api/objects/search?kind='+encodeURIComponent(kind)+'&limit=80');
      $('canvas').querySelector('.object-list').innerHTML=objectRows(data.items||[]);
      bindObjects();
      const input=$('view-query');
      let t;
      input.addEventListener('input',()=>{clearTimeout(t);t=setTimeout(async()=>{
        const d=await api('/api/objects/search?kind='+encodeURIComponent(kind)+'&q='+encodeURIComponent(input.value)+'&limit=80');
        $('canvas').querySelector('.object-list').innerHTML=objectRows(d.items||[]);bindObjects();
      },220);});
    }catch(e){$('canvas').querySelector('.object-list').innerHTML='<div class="card-empty">'+esc(e.message)+'</div>';}
  }

  async function renderInbox(){
    setNav('inbox');
    $('canvas').innerHTML=head('UNIFIED INBOX','Inbox','Messages, requests and records that need attention.')+
      '<div class="segment" id="inbox-segment"><button class="active" data-bucket="open">Inbox</button><button data-bucket="action">Needs action</button><button data-bucket="all">All</button></div>'+
      '<div class="object-list" id="hub-list"><div class="card-empty">Loading…</div></div>';
    emptyContext('Inbox','Select an item to keep its context beside you.');
    async function load(bucket){
      const data=await api('/api/workspace-next/inbox?bucket='+encodeURIComponent(bucket));
      $('inbox-count').textContent=data.items?.length?String(data.items.length):'';
      $('hub-list').innerHTML=(data.items||[]).map(x=>
        '<button class="object-row" data-hub-kind="'+esc(x.kind)+'" data-hub-id="'+esc(x.id)+'"><span class="object-kind">'+esc(x.kind)+'</span>'+
        '<span class="object-copy"><strong>'+esc(x.title)+'</strong><span>'+esc(x.snippet||'')+'</span></span><span class="object-meta">'+esc(x.status||'')+'</span></button>'
      ).join('')||'<div class="card-empty">Inbox is clear.</div>';
      document.querySelectorAll('[data-hub-kind]').forEach(b=>b.addEventListener('click',()=>{
        $('context').classList.add('open');
        $('context').innerHTML='<div class="context-head"><div class="eyebrow">'+esc(b.dataset.hubKind)+'</div><h2>Inbox item</h2></div>'+
          '<div class="context-body"><p class="muted">Full inbox triage remains connected to the existing canonical record. Native actions are being brought into this pane rather than creating another system.</p></div>';
      }));
    }
    document.querySelectorAll('[data-bucket]').forEach(b=>b.addEventListener('click',()=>{
      document.querySelectorAll('[data-bucket]').forEach(x=>x.classList.toggle('active',x===b));load(b.dataset.bucket);
    }));
    await load('open');
  }

  async function renderDocuments(){
    setNav('documents');
    $('canvas').innerHTML=head('KNOWLEDGE','Documents','Files, datasets, Brain captures and extracted knowledge.')+
      '<div class="list-toolbar"><input id="doc-query" type="search" placeholder="Search your knowledge…"></div>'+
      '<div class="object-list" id="doc-list"><div class="card-empty">Loading…</div></div>';
    emptyContext('Documents','Select knowledge without leaving the workspace.');
    async function load(){
      const q=$('doc-query')?.value||'';
      const data=await api('/api/workspace-next/documents?q='+encodeURIComponent(q));
      $('doc-list').innerHTML=(data.items||[]).map(x=>
        '<div class="object-row"><span class="object-kind">'+esc(x.kind)+'</span><span class="object-copy"><strong>'+esc(x.title)+'</strong><span>'+
        esc(x.snippet||'')+'</span></span><span class="object-meta">'+esc(x.status||'')+'</span></div>'
      ).join('')||'<div class="card-empty">No documents match.</div>';
    }
    await load();
    let t;$('doc-query').addEventListener('input',()=>{clearTimeout(t);t=setTimeout(load,220);});
  }

  async function renderSearch(q=''){
    setNav('search'); state.query=q;
    $('canvas').innerHTML=head('GLOBAL','Search','One search across operational objects and connected context.')+
      '<div class="list-toolbar"><input id="search-page-query" type="search" placeholder="Search everything…" value="'+esc(q)+'"></div>'+
      '<div class="object-list" id="search-results"><div class="card-empty">Type to search, or browse recent records.</div></div>';
    emptyContext('Search context','Open any result without leaving Search.');
    async function run(){
      const value=$('search-page-query').value.trim();
      const data=await api('/api/objects/search?q='+encodeURIComponent(value)+'&limit=80');
      $('search-results').innerHTML=objectRows(data.items||[]);bindObjects();
    }
    await run();
    let t;$('search-page-query').addEventListener('input',()=>{clearTimeout(t);t=setTimeout(run,220);});
    $('search-page-query').focus();
  }

  function renderMap(){
    setNav('map');
    $('canvas').innerHTML='<iframe class="map-frame" src="/map?embed=1" title="City Manager OS Mapping Center"></iframe>';
    $('context').classList.remove('open');
    $('context').innerHTML='';
  }

  async function renderOperations(){
    setNav('operations');
    $('canvas').innerHTML=head('MUNICIPAL OPERATIONS','Operations','Spatial intelligence, alerts, Watches, events and service health stay connected here.')+
      '<div class="ops-grid" id="ops-grid"><div class="placeholder">Loading operational systems…</div></div>';
    emptyContext('Operations','Select an alert, Watch or event to inspect it here.');
    try{
      const [alerts,watches,events,home]=await Promise.all([
        api('/api/objects/search?kind=ALERT&limit=12'),
        api('/api/objects/search?kind=WATCH&limit=12'),
        api('/api/objects/search?kind=EVENT&limit=12'),
        api('/api/workspace-next/home')
      ]);
      const card=(title,kind,items)=>'<section class="ops-card"><div class="card-head"><h2>'+esc(title)+'</h2><button data-kind-open="'+kind+'">View all</button></div>'+
        (items.length?items.slice(0,6).map(x=>'<button class="object-row" data-object-kind="'+x.kind+'" data-object-id="'+x.id+'"><span class="object-kind">'+x.kind+
        '</span><span class="object-copy"><strong>'+esc(x.title)+'</strong><span>'+esc(x.subtitle||'')+'</span></span></button>').join(''):
        '<div class="card-empty">No records.</div>')+'</section>';
      $('ops-grid').innerHTML=card('Live alerts','ALERT',alerts.items||[])+card('Active Watches','WATCH',watches.items||[])+
        card('Events','EVENT',events.items||[])+
        '<section class="ops-card"><div class="card-head"><h2>Spatial operations</h2><button data-open-map>Open live map</button></div><p class="muted small">Parcels, addresses, alerts, Watches, work, events, transit, flood and your custom GIS layers remain available together.</p></section>'+
        '<section class="ops-card"><h2>System health</h2><p class="muted small">'+esc(home.counts?.unhealthy_sources||0)+' unhealthy sources · '+
        esc(home.counts?.failed_24h||0)+' failed deliveries in 24h</p><p class="muted small">Map, parcels, flood, transit, PSEG and routing remain backed by the existing operational engines.</p></section>';
      bindObjects();
      document.querySelectorAll('[data-kind-open]').forEach(b=>b.addEventListener('click',()=>loadKind(b.dataset.kindOpen,'operations')));
      const mapButton=document.querySelector('[data-open-map]');
      if(mapButton)mapButton.addEventListener('click',renderMap);
    }catch(e){$('ops-grid').innerHTML='<div class="placeholder">'+esc(e.message)+'</div>';}
  }

  function renderAdmin(){
    setNav('admin');
    $('canvas').innerHTML=head('ADVANCED','Admin','Deep controls remain available without becoming the primary navigation.')+
      '<div class="admin-grid">'+
      '<section class="admin-card"><h2>Spatial & intelligence</h2><a href="/map">Mapping Center ↗</a><a href="/alerts">Alert Explorer ↗</a><a href="/watchlist">Watch configuration ↗</a><a href="/spatial-reference">Reference Catalog ↗</a><a href="/flood">Flood intelligence ↗</a></section>'+
      '<section class="admin-card"><h2>Operations</h2><a href="/issues">Full Work controls ↗</a><a href="/staff-admin">Staff Operations ↗</a><a href="/schedule">Events Center ↗</a><a href="/event-intelligence">Event Intelligence ↗</a><a href="/transit">Transit Intelligence ↗</a></section>'+
      '<section class="admin-card"><h2>Alert delivery</h2><a href="/subscribers">Recipients ↗</a><a href="/deliveries">Delivery history ↗</a><a href="/share">Share / SMS ↗</a></section>'+
      '<section class="admin-card"><h2>System</h2><a href="/integrations">Integrations ↗</a><a href="/source-health">Source health ↗</a><a href="/database">Database viewer ↗</a><a href="/admin-tools">Admin tools ↗</a></section>'+
      '</div>';
    emptyContext('Advanced controls','These remain the safety net while each control is absorbed natively.');
  }

  async function openObject(kind,id){
    state.selected=kind+':'+id;
    document.querySelectorAll('.object-row').forEach(b=>b.classList.toggle('active',b.dataset.objectKind===kind&&b.dataset.objectId===id));
    $('context').classList.add('open');
    $('context').innerHTML='<div class="context-empty">Loading context…</div>';
    try{
      const data=await api('/api/objects/'+encodeURIComponent(kind)+'/'+encodeURIComponent(id));
      const o=data.object||{};
      const title=o.name||o.canonical_name||o.title||o.display_name||o.alert_id||o.watch_id||kind;
      const subtitle=o.normalized_address||o.address||o.municipality||o.organization||o.source||'';
      const hidden=new Set(['raw_payload','metadata','provenance','geom','centroid','attributes','search_text','description','message','preparation_checklist']);
      const fields=Object.entries(o).filter(([k,v])=>!hidden.has(k)&&v!==null&&v!==''&&typeof v!=='object').slice(0,18)
        .map(([k,v])=>'<div class="field"><small>'+esc(pretty(k))+'</small><div>'+esc(v)+'</div></div>').join('');
      const related=Object.entries(data.related||{}).map(([name,value])=>{
        if(!value)return '';
        if(!Array.isArray(value)){
          if(typeof value!=='object'||!Object.keys(value).length)return '';
          const body=Object.entries(value).filter(([,v])=>v!==null&&v!==''&&typeof v!=='object').slice(0,12)
            .map(([k,v])=>'<div class="field"><small>'+esc(pretty(k))+'</small><div>'+esc(v)+'</div></div>').join('');
          return '<section class="context-section"><h3>'+esc(pretty(name))+'</h3><div class="field-grid">'+body+'</div></section>';
        }
        if(!value.length)return '';
        return '<section class="context-section"><h3>'+esc(pretty(name))+' · '+value.length+'</h3>'+
          value.slice(0,30).map(r=>{
            const t=r.other_name||r.display_name||r.title||r.subscriber_name||r.name||r.alert_id||r.summary||r.relation||r.status||'Record';
            const d=r.match_reason||r.note||r.summary||r.evidence||r.source||r.status||r.created_at||r.matched_at||'';
            return '<div class="related"><strong>'+esc(t)+'</strong>'+(d?'<p>'+esc(d)+'</p>':'')+'</div>';
          }).join('')+'</section>';
      }).join('');
      $('context').innerHTML='<div class="context-head"><div class="eyebrow">'+esc(kind)+'</div><h2>'+esc(title)+'</h2>'+
        (subtitle?'<p class="muted small">'+esc(subtitle)+'</p>':'')+
        '<div class="context-actions">'+(data.map_url?'<a href="'+esc(data.map_url)+'">Full map ↗</a>':'')+
        (data.legacy_url?'<a href="'+esc(data.legacy_url)+'">Advanced controls ↗</a>':'')+'</div></div>'+
        '<div class="context-body"><section class="context-section"><h3>Overview</h3><div class="field-grid">'+fields+'</div></section>'+related+'</div>';
    }catch(e){$('context').innerHTML='<div class="context-empty">'+esc(e.message)+'</div>';}
  }

  function openView(view){
    if(view==='home')return renderHome();
    if(view==='inbox')return renderInbox();
    if(view==='search')return renderSearch('');
    if(view==='work')return loadKind('WORK','work');
    if(view==='people')return loadKind('PERSON','people');
    if(view==='places')return loadKind('PLACE','places');
    if(view==='map')return renderMap();
    if(view==='documents')return renderDocuments();
    if(view==='operations')return renderOperations();
    if(view==='admin')return renderAdmin();
  }

  document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>openView(b.dataset.view)));
  $('global-search').addEventListener('submit',e=>{e.preventDefault();renderSearch($('global-query').value.trim());});
  $('quick-search').addEventListener('click',()=>{$('global-query').focus();$('global-query').select();});
  document.addEventListener('keydown',e=>{
    if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('global-query').focus();$('global-query').select();}
    if(e.key==='Escape')$('context').classList.remove('open');
  });
  $('mobile-menu').addEventListener('click',()=>{document.body.classList.add('nav-open');$('rail-backdrop').hidden=false;});
  $('rail-backdrop').addEventListener('click',()=>{document.body.classList.remove('nav-open');$('rail-backdrop').hidden=true;});

  renderHome();
})();
