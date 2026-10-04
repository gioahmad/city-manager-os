(() => {
  const $ = id => document.getElementById(id);
  const state = {view:'home',kind:'',selected:null,history:[]};

  function esc(v){
    return String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;')
      .replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;");
  }
  function pretty(v){return String(v||'').replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());}
  function when(v){
    if(!v)return '';
    const d=new Date(v);
    if(Number.isNaN(d.getTime()))return String(v);
    return d.toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  }
  async function api(url){
    const r=await fetch(url,{cache:'no-store'});
    if(!r.ok)throw new Error('Request failed ('+r.status+')');
    return r.json();
  }
  async function action(payload){
    const r=await fetch('/api/workspace-next/action',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({...payload,csrf:document.body.dataset.csrf||''})
    });
    const data=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(data.detail||'Action failed');
    return data;
  }
  function localInput(value){
    if(!value)return '';
    const d=new Date(value);
    if(Number.isNaN(d.getTime()))return '';
    const pad=n=>String(n).padStart(2,'0');
    return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes());
  }
  function setNav(view){
    state.view=view;
    document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
    document.body.classList.remove('nav-open');
    $('rail-backdrop').hidden=true;
  }
  function setPrimary(title,html,options={}){
    $('primary-breadcrumb').textContent=title;
    $('primary').className='window-body'+(options.map?' map-pane':'');
    $('primary').innerHTML=html;
    if(!options.keepSplit)closeSplit();
  }
  function openSplit(title,html){
    $('secondary-breadcrumb').textContent=title;
    $('secondary').innerHTML=html;
    $('secondary-window').hidden=false;
    $('window-manager').classList.add('split-open');
  }
  function closeSplit(){
    $('secondary-window').hidden=true;
    $('secondary').innerHTML='';
    $('window-manager').classList.remove('split-open');
    document.querySelectorAll('.object-row.active').forEach(x=>x.classList.remove('active'));
    state.selected=null;
  }
  function wrap(content){return '<div class="view-wrap">'+content+'</div>';}
  function head(eyebrow,title,description,right=''){
    return '<div class="page-head"><div><div class="eyebrow">'+esc(eyebrow)+'</div><h1>'+esc(title)+'</h1>'+
      (description?'<p class="muted">'+esc(description)+'</p>':'')+'</div>'+right+'</div>';
  }
  function badge(text,type=''){return '<span class="pill '+type+'">'+esc(text)+'</span>';}
  function rowItem(title,detail,meta='',tag=''){
    return '<div class="row-item"><div class="row-main">'+tag+'<strong>'+esc(title)+'</strong>'+
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
    document.querySelectorAll('[data-object-kind]').forEach(b=>b.addEventListener('click',()=>openObject(b.dataset.objectKind,b.dataset.objectId,b)));
  }

  async function renderHome(){
    setNav('home');
    setPrimary('Home',wrap(head('EXECUTIVE WORKSPACE','Home','What needs attention, what is happening now, and what changed.')+
      '<div class="placeholder">Loading operational picture…</div>'));

    try{
      const data=await api('/api/workspace-next/home');
      const c=data.counts||{};
      let html=head('EXECUTIVE WORKSPACE','Home','What needs attention, what is happening now, and what changed.');
      html+='<div class="metric-grid">'+
        '<div class="metric"><small>Open work</small><strong>'+esc(c.open_work||0)+'</strong><span>Active municipal work</span></div>'+
        '<div class="metric"><small>Alerts · 24h</small><strong>'+esc(c.alerts_24h||0)+'</strong><span>Incoming intelligence</span></div>'+
        '<div class="metric"><small>Active watches</small><strong>'+esc(c.active_watches||0)+'</strong><span>Monitoring rules on</span></div>'+
        '<div class="metric"><small>Exceptions</small><strong>'+esc(Number(c.failed_24h||0)+Number(c.unhealthy_sources||0))+
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
      )).join('')||'<div class="card-empty">No upcoming events.</div>';

      const health=(data.source_health||[]);
      const failures=(data.failed_deliveries||[]);
      const pulse=(health.length||failures.length)
        ? health.slice(0,5).map(x=>rowItem(x.source_id,x.last_error||x.status,when(x.last_success_at),badge(x.status,'warning'))).join('')+
          failures.slice(0,5).map(x=>rowItem(x.alert_title,x.subscriber_name+' · '+(x.error_message||x.status),when(x.attempted_at),badge('DELIVERY','danger'))).join('')
        : '<div class="card-empty">No source or delivery exceptions.</div>';

      html+='<div class="home-grid"><div class="stack">'+
        '<section class="card"><div class="card-head"><h2>Needs attention</h2><button data-jump="work">Open Work</button></div><div class="card-body">'+attention+'</div></section>'+
        '<section class="card"><div class="card-head"><h2>Live intelligence</h2><button data-kind-open="ALERT">View Alerts</button></div><div class="card-body">'+alerts+'</div></section>'+
        '</div><div class="stack">'+
        '<section class="card"><div class="card-head"><h2>Upcoming</h2><span class="small muted">Next 14 days</span></div><div class="card-body">'+events+'</div></section>'+
        '<section class="card"><div class="card-head"><h2>System pulse</h2><button data-jump="operations">Operations</button></div><div class="card-body">'+pulse+'</div></section>'+
        '<section class="card"><div class="card-head"><h2>Recent activity</h2></div><div class="card-body">'+
        (data.recent||[]).slice(0,9).map(x=>rowItem(x.title,x.detail,when(x.occurred_at),badge(x.kind))).join('')+
        '</div></section></div></div>';
      setPrimary('Home',wrap(html));
      document.querySelectorAll('[data-jump]').forEach(b=>b.addEventListener('click',()=>openView(b.dataset.jump)));
      document.querySelectorAll('[data-kind-open]').forEach(b=>b.addEventListener('click',()=>loadKind(b.dataset.kindOpen,'operations')));
    }catch(e){
      setPrimary('Home',wrap(head('EXECUTIVE WORKSPACE','Home','')+'<div class="placeholder">'+esc(e.message)+'</div>'));
    }
  }

  async function loadKind(kind,view){
    setNav(view);
    const copy={
      WORK:['EXECUTION','Work','Issues, tasks, follow-ups, decisions and operational work.'],
      PERSON:['RELATIONSHIPS','People','Residents, staff, officials, vendors and connected contacts.'],
      PLACE:['SPATIAL CONTEXT','Places','Addresses, facilities, parcels, corridors and regional references.'],
      ALERT:['OPERATIONS','Alerts','Live intelligence from the existing alerting system.'],
      WATCH:['OPERATIONS','Watches','Topic and spatial monitoring with existing match history.'],
      EVENT:['OPERATIONS','Events','Regional events and operational impact.']
    }[kind];
    state.kind=kind;
    setPrimary(copy[1],wrap(head(copy[0],copy[1],copy[2])+
      '<div class="list-toolbar"><input id="view-query" type="search" placeholder="Filter '+esc(copy[1].toLowerCase())+'…"></div>'+
      '<div class="object-list" id="kind-list"><div class="card-empty">Loading…</div></div>'));
    try{
      const data=await api('/api/objects/search?kind='+encodeURIComponent(kind)+'&limit=100');
      $('kind-list').innerHTML=objectRows(data.items||[]);bindObjects();
      let t;$('view-query').addEventListener('input',()=>{clearTimeout(t);t=setTimeout(async()=>{
        const d=await api('/api/objects/search?kind='+encodeURIComponent(kind)+'&q='+encodeURIComponent($('view-query').value)+'&limit=100');
        $('kind-list').innerHTML=objectRows(d.items||[]);bindObjects();
      },220);});
    }catch(e){$('kind-list').innerHTML='<div class="card-empty">'+esc(e.message)+'</div>';}
  }

  async function renderInbox(){
    setNav('inbox');
    setPrimary('Inbox',wrap(head('UNIFIED INBOX','Inbox','Messages, requests and records that need attention.')+
      '<div class="segment" id="inbox-segment"><button class="active" data-bucket="open">Inbox</button><button data-bucket="action">Needs action</button><button data-bucket="all">All</button></div>'+
      '<div class="object-list" id="hub-list"><div class="card-empty">Loading…</div></div>'));
    async function load(bucket){
      const data=await api('/api/workspace-next/inbox?bucket='+encodeURIComponent(bucket));
      $('inbox-count').textContent=data.items?.length?String(data.items.length):'';
      $('hub-list').innerHTML=(data.items||[]).map(x=>
        '<button class="object-row" data-hub-kind="'+esc(x.kind)+'" data-hub-id="'+esc(x.id)+'"><span class="object-kind">'+esc(x.kind)+'</span>'+
        '<span class="object-copy"><strong>'+esc(x.title)+'</strong><span>'+esc(x.snippet||'')+'</span></span><span class="object-meta">'+esc(x.status||'')+'</span></button>'
      ).join('')||'<div class="card-empty">Inbox is clear.</div>';
      document.querySelectorAll('[data-hub-kind]').forEach(b=>b.addEventListener('click',()=>{
        openSplit('Inbox · '+b.dataset.hubKind,
          '<div class="detail-wrap"><div class="detail-head"><div class="eyebrow">'+esc(b.dataset.hubKind)+'</div><h2>Inbox item</h2></div>'+
          '<section class="native-actions"><h3>Inbox</h3><button class="native-primary" id="handle-inbox">Mark handled</button><div class="native-status" id="native-status"></div></section>'+
          '<p class="muted">The source record stays intact; handling only clears it from your active Inbox.</p></div>');
        const handle=$('handle-inbox');
        if(handle)handle.addEventListener('click',async()=>{
          const status=$('native-status');if(status)status.textContent='Updating…';
          try{
            const r=await action({action:'INBOX_HANDLE',kind:b.dataset.hubKind,id:b.dataset.hubId,handled:true});
            if(status)status.textContent=r.message||'Updated';
            closeSplit();await load('open');
          }catch(err){if(status)status.textContent=err.message;}
        });
      }));
    }
    document.querySelectorAll('[data-bucket]').forEach(b=>b.addEventListener('click',()=>{
      document.querySelectorAll('[data-bucket]').forEach(x=>x.classList.toggle('active',x===b));load(b.dataset.bucket);
    }));
    await load('open');
  }

  async function renderDocuments(){
    setNav('documents');
    setPrimary('Documents',wrap(head('KNOWLEDGE','Documents','Files, datasets, Brain captures and extracted knowledge.')+
      '<div class="list-toolbar"><input id="doc-query" type="search" placeholder="Search your knowledge…"></div>'+
      '<div class="object-list" id="doc-list"><div class="card-empty">Loading…</div></div>'));
    async function load(){
      const q=$('doc-query')?.value||'';
      const data=await api('/api/workspace-next/documents?q='+encodeURIComponent(q));
      $('doc-list').innerHTML=(data.items||[]).map(x=>
        '<button class="object-row" data-doc-title="'+esc(x.title)+'"><span class="object-kind">'+esc(x.kind)+'</span><span class="object-copy"><strong>'+
        esc(x.title)+'</strong><span>'+esc(x.snippet||'')+'</span></span><span class="object-meta">'+esc(x.status||'')+'</span></button>'
      ).join('')||'<div class="card-empty">No documents match.</div>';
      document.querySelectorAll('[data-doc-title]').forEach(b=>b.addEventListener('click',()=>openSplit('Document',
        '<div class="detail-wrap"><div class="detail-head"><div class="eyebrow">KNOWLEDGE</div><h2>'+esc(b.dataset.docTitle)+'</h2></div><p class="muted">Document detail and editing stay in this split as the native document surface is completed.</p></div>')));
    }
    await load();
    let t;$('doc-query').addEventListener('input',()=>{clearTimeout(t);t=setTimeout(load,220);});
  }

  async function renderSearch(q=''){
    setNav('search');
    setPrimary('Search',wrap(head('GLOBAL','Search','One search across operational objects and connected context.')+
      '<div class="list-toolbar"><input id="search-page-query" type="search" placeholder="Search everything…" value="'+esc(q)+'"></div>'+
      '<div class="object-list" id="search-results"><div class="card-empty">Type to search, or browse recent records.</div></div>'));
    async function run(){
      const value=$('search-page-query').value.trim();
      const data=await api('/api/objects/search?q='+encodeURIComponent(value)+'&limit=100');
      $('search-results').innerHTML=objectRows(data.items||[]);bindObjects();
    }
    await run();
    let t;$('search-page-query').addEventListener('input',()=>{clearTimeout(t);t=setTimeout(run,220);});
    $('search-page-query').focus();
  }

  function renderMap(){
    setNav('map');
    setPrimary('Map','<iframe class="map-frame" src="/map?embed=1" title="City Manager OS Mapping Center"></iframe>',{map:true});
  }

  async function renderOperations(){
    setNav('operations');
    setPrimary('Operations',wrap(head('MUNICIPAL OPERATIONS','Operations','Alerts, Watches, events, spatial operations and system health in one place.')+
      '<div class="ops-grid" id="ops-grid"><div class="placeholder">Loading operational systems…</div></div>'));
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
      $('ops-grid').innerHTML=
        card('Live alerts','ALERT',alerts.items||[])+
        card('Active Watches','WATCH',watches.items||[])+
        card('Events','EVENT',events.items||[])+
        '<section class="ops-card"><div class="card-head"><h2>Spatial operations</h2><button data-open-map>Open live map</button></div><p class="muted small">Parcels, addresses, alerts, Watches, work, events, transit, flood and custom GIS layers.</p></section>'+
        '<section class="ops-card"><h2>System health</h2><p class="muted small">'+esc(home.counts?.unhealthy_sources||0)+' unhealthy sources · '+
        esc(home.counts?.failed_24h||0)+' failed deliveries in 24h</p></section>';
      bindObjects();
      document.querySelectorAll('[data-kind-open]').forEach(b=>b.addEventListener('click',()=>loadKind(b.dataset.kindOpen,'operations')));
      const mb=document.querySelector('[data-open-map]');if(mb)mb.addEventListener('click',renderMap);
    }catch(e){$('ops-grid').innerHTML='<div class="placeholder">'+esc(e.message)+'</div>';}
  }

  function renderAdmin(){
    setNav('admin');
    setPrimary('Admin',wrap(head('ADVANCED','Admin','Deep controls remain available without becoming the primary product.')+
      '<div class="admin-grid">'+
      '<section class="admin-card"><h2>Spatial & intelligence</h2><a href="/map">Mapping Center ↗</a><a href="/alerts">Alert Explorer ↗</a><a href="/watchlist">Watch configuration ↗</a><a href="/spatial-reference">Reference Catalog ↗</a><a href="/flood">Flood intelligence ↗</a></section>'+
      '<section class="admin-card"><h2>Operations</h2><a href="/issues">Full Work controls ↗</a><a href="/staff-admin">Staff Operations ↗</a><a href="/schedule">Events Center ↗</a><a href="/event-intelligence">Event Intelligence ↗</a><a href="/transit">Transit Intelligence ↗</a></section>'+
      '<section class="admin-card"><h2>Alert delivery</h2><a href="/subscribers">Recipients ↗</a><a href="/deliveries">Delivery history ↗</a><a href="/share">Share / SMS ↗</a></section>'+
      '<section class="admin-card"><h2>System</h2><a href="/integrations">Integrations ↗</a><a href="/source-health">Source health ↗</a><a href="/database">Database viewer ↗</a><a href="/admin-tools">Admin tools ↗</a></section>'+
      '</div>'));
  }

  function nativeControls(kind,id,o){
    if(kind==='WORK'){
      const statuses=['OPEN','IN_PROGRESS','ON_HOLD','RESOLVED','CLOSED'];
      return '<section class="native-actions"><h3>Work controls</h3>'+
        '<form id="work-edit-form" class="native-form">'+
        '<label>Title<input name="title" value="'+esc(o.title||'')+'" required></label>'+
        '<div class="native-grid"><label>Status<select name="status">'+statuses.map(x=>'<option '+(o.status===x?'selected':'')+'>'+x+'</option>').join('')+'</select></label>'+
        '<label>Priority<input name="priority" type="number" min="1" max="5" value="'+esc(o.priority||3)+'"></label></div>'+
        '<label>Assigned to<input name="assigned_to" value="'+esc(o.assigned_to||'')+'"></label>'+
        '<label>Next action<textarea name="next_action" rows="2">'+esc(o.next_action||'')+'</textarea></label>'+
        '<label>Waiting on<input name="waiting_on" value="'+esc(o.waiting_on||'')+'"></label>'+
        '<div class="native-grid"><label>Due<input name="due_at" type="datetime-local" value="'+esc(localInput(o.due_at))+'"></label>'+
        '<label>Follow up<input name="follow_up_at" type="datetime-local" value="'+esc(localInput(o.follow_up_at))+'"></label></div>'+
        '<button class="native-primary" type="submit">Save work</button></form>'+
        (o.waiting_on?'<div class="native-inline"><button data-work-chased>Chased today</button><button data-work-response>Response received</button></div>':'')+
        '<form id="work-note-form" class="native-form compact"><label>Add update<textarea name="note" rows="3" placeholder="Add a note or update…"></textarea></label><button type="submit">Add update</button></form>'+
        '<div class="native-status" id="native-status"></div></section>';
    }
    if(kind==='ALERT'){
      return '<section class="native-actions"><h3>Actions</h3><button class="native-primary" data-alert-work>Track as Work</button>'+
        '<button data-open-map-internal>Open in Map</button><div class="native-status" id="native-status"></div></section>';
    }
    if(['PLACE','WATCH','EVENT'].includes(kind)){
      return '<section class="native-actions"><h3>Actions</h3><button data-open-map-internal>Open in Map</button></section>';
    }
    return '';
  }

  function bindNativeActions(kind,id,o){
    const status=$('native-status');
    const show=message=>{if(status)status.textContent=message||'';};

    const workForm=$('work-edit-form');
    if(workForm)workForm.addEventListener('submit',async e=>{
      e.preventDefault();show('Saving…');
      const v=Object.fromEntries(new FormData(workForm).entries());
      try{
        const result=await action({action:'WORK_UPDATE',id,...v});
        show(result.message||'Saved');
        await openObject(kind,id,null);
      }catch(err){show(err.message);}
    });

    const noteForm=$('work-note-form');
    if(noteForm)noteForm.addEventListener('submit',async e=>{
      e.preventDefault();show('Adding update…');
      const note=String(new FormData(noteForm).get('note')||'');
      try{
        const result=await action({action:'WORK_NOTE',id,note});
        show(result.message||'Added');
        await openObject(kind,id,null);
      }catch(err){show(err.message);}
    });

    const chased=document.querySelector('[data-work-chased]');
    if(chased)chased.addEventListener('click',async()=>{
      try{const r=await action({action:'WORK_CHASED',id});show(r.message);await openObject(kind,id,null);}catch(err){show(err.message);}
    });
    const response=document.querySelector('[data-work-response]');
    if(response)response.addEventListener('click',async()=>{
      try{const r=await action({action:'WORK_RESPONSE',id});show(r.message);await openObject(kind,id,null);}catch(err){show(err.message);}
    });
    const alertWork=document.querySelector('[data-alert-work]');
    if(alertWork)alertWork.addEventListener('click',async()=>{
      show('Creating Work item…');
      try{
        const r=await action({action:'ALERT_TO_WORK',id});
        show(r.message||'Added to Work');
        if(r.issue_id)await openObject('WORK',r.issue_id,null);
      }catch(err){show(err.message);}
    });
    const mapButton=document.querySelector('[data-open-map-internal]');
    if(mapButton)mapButton.addEventListener('click',()=>renderMap());
  }

  async function openObject(kind,id,button){
    state.selected=kind+':'+id;
    document.querySelectorAll('.object-row').forEach(b=>b.classList.remove('active'));
    if(button)button.classList.add('active');
    openSplit(kind,'<div class="detail-empty">Loading context…</div>');
    try{
      const data=await api('/api/objects/'+encodeURIComponent(kind)+'/'+encodeURIComponent(id));
      const o=data.object||{};
      const title=o.name||o.canonical_name||o.title||o.display_name||o.alert_id||o.watch_id||kind;
      const subtitle=o.normalized_address||o.address||o.municipality||o.organization||o.source||'';
      const hidden=new Set(['raw_payload','metadata','provenance','geom','centroid','attributes','search_text','description','message','preparation_checklist']);
      const fields=Object.entries(o).filter(([k,v])=>!hidden.has(k)&&v!==null&&v!==''&&typeof v!=='object').slice(0,20)
        .map(([k,v])=>'<div class="field"><small>'+esc(pretty(k))+'</small><div>'+esc(v)+'</div></div>').join('');
      const related=Object.entries(data.related||{}).map(([name,value])=>{
        if(!value)return '';
        if(!Array.isArray(value)){
          if(typeof value!=='object'||!Object.keys(value).length)return '';
          const body=Object.entries(value).filter(([,v])=>v!==null&&v!==''&&typeof v!=='object').slice(0,14)
            .map(([k,v])=>'<div class="field"><small>'+esc(pretty(k))+'</small><div>'+esc(v)+'</div></div>').join('');
          return '<section class="context-section"><h3>'+esc(pretty(name))+'</h3><div class="field-grid">'+body+'</div></section>';
        }
        if(!value.length)return '';
        return '<section class="context-section"><h3>'+esc(pretty(name))+' · '+value.length+'</h3>'+
          value.slice(0,40).map(r=>{
            const t=r.other_name||r.display_name||r.title||r.subscriber_name||r.name||r.alert_id||r.summary||r.relation||r.status||'Record';
            const d=r.match_reason||r.note||r.summary||r.evidence||r.source||r.status||r.created_at||r.matched_at||'';
            return '<div class="related"><strong>'+esc(t)+'</strong>'+(d?'<p>'+esc(d)+'</p>':'')+'</div>';
          }).join('')+'</section>';
      }).join('');
      openSplit(title,
        '<div class="detail-wrap"><div class="detail-head"><div class="eyebrow">'+esc(kind)+'</div><h2>'+esc(title)+'</h2>'+
        (subtitle?'<p class="muted small">'+esc(subtitle)+'</p>':'')+
        '<div class="detail-actions">'+
        (data.legacy_url?'<a href="'+esc(data.legacy_url)+'">Advanced controls ↗</a>':'')+
        '</div></div>'+nativeControls(kind,id,o)+
        '<section class="context-section"><h3>Overview</h3><div class="field-grid">'+fields+'</div></section>'+related+'</div>');
      bindNativeActions(kind,id,o);
    }catch(e){openSplit(kind,'<div class="detail-empty">'+esc(e.message)+'</div>');}
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
  $('refresh-view').addEventListener('click',()=>openView(state.view));
  $('secondary-close').addEventListener('click',closeSplit);
  $('back-button').addEventListener('click',()=>history.back());
  $('forward-button').addEventListener('click',()=>history.forward());

  function toggleFocus(id){
    const win=$(id);
    const other=id==='secondary-window'?document.querySelector('.primary-window'):$('secondary-window');
    const focused=win.classList.toggle('focused');
    if(focused&&other)other.classList.remove('focused');
  }
  $('primary-maximize').addEventListener('click',()=>toggleFocus('primary-window'));
  $('secondary-maximize').addEventListener('click',()=>toggleFocus('secondary-window'));

  document.addEventListener('keydown',e=>{
    if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('global-query').focus();$('global-query').select();}
    if((e.metaKey||e.ctrlKey)&&e.key==='Escape'){e.preventDefault();closeSplit();}
    if(e.key==='Escape'&&document.body.classList.contains('nav-open')){
      document.body.classList.remove('nav-open');$('rail-backdrop').hidden=true;
    }
  });
  $('mobile-menu').addEventListener('click',()=>{document.body.classList.add('nav-open');$('rail-backdrop').hidden=false;});
  $('rail-backdrop').addEventListener('click',()=>{document.body.classList.remove('nav-open');$('rail-backdrop').hidden=true;});

  renderHome();
})();
