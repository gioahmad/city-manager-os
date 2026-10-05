(()=>{
  const params=new URLSearchParams(location.search);
  if(params.get('embed')==='1'){
    document.body.classList.add('cmos-embedded');
    return;
  }

  const quickActions=[
    ['＋','New Work','Create a new accountable work item','/issues#new-work'],
    ['◎','New Watch','Create a topic, location, or spatial Watch','/watchlist#new-watch'],
    ['⌖','Open Map','Open the full Mapping Center','/map'],
    ['▣','Quick Capture','Capture something now and organize it later','#quick-capture'],
    ['⌕','Search Records','Search across alerts, work, places, people, events and sources','/search']
  ];
  const commands=[
    ['⌂','Overview','Executive operating picture','/'],
    ['◫','Workspace','Today, documents, people, projects and private context','/workspace?view=today'],
    ['◷','My Day','Today, Waiting On and executive attention','/my-day'],
    ['✓','Command Center','Issues, commitments, decisions and follow-ups','/issues'],
    ['▣','Executive Intake','Review Microsoft email, calendar and staged sources','/intake'],
    ['↳','Quick Capture Inbox','Triage quick captures','/inbox'],
    ['◉','Brain','Private notes, ideas, tasks and links','/brain'],
    ['⌖','Mapping Center','Full GIS, parcels, alerts, Watches, layers, import and draw','/map'],
    ['!','Alerts','Search complete alert history and matched Watches','/alerts?window=all&state=all'],
    ['◎','Watches','Monitoring rules, spatial geometry, recipients and evidence','/watchlist'],
    ['♙','Recipients','Notification recipients and Watch subscriptions','/subscribers'],
    ['☷','Contacts','Residents, staff, vendors and officials','/contacts'],
    ['◈','Share','Email, SMS and SMSGate sharing','/share'],
    ['◇','Spatial References','Canonical places, parcels and regional references','/spatial-reference'],
    ['↔','Transit','Regional transit intelligence','/transit'],
    ['≈','Flood','Local flood intelligence','/flood'],
    ['◫','Events Center','Operational schedule and events','/schedule'],
    ['◌','Event Intelligence','Regional event intelligence','/event-intelligence'],
    ['☰','Staff Operations','Assignments, staff work and field operations','/staff-admin'],
    ['↻','What Changed','Recent operational changes','/what-changed'],
    ['⚡','Integrations','Source configuration and integrations','/integrations'],
    ['♥','Source Health','Collector and source health','/source-health'],
    ['⇢','Notification History','Delivery audit and routing results','/deliveries'],
    ['▦','Database Viewer','Operational database inspection','/database'],
    ['⚙','Admin Tools','System controls and maintenance','/admin-tools'],
    ['⌕','Search','Search operational records','/search']
  ];

  const recentKey='cmos.macro.recent.v1';
  const readRecent=()=>{try{return JSON.parse(localStorage.getItem(recentKey)||'[]')}catch{return[]}};
  const remember=(url,title)=>{
    try{
      const rows=readRecent().filter(x=>x.url!==url);
      rows.unshift({url,title,at:Date.now()});
      localStorage.setItem(recentKey,JSON.stringify(rows.slice(0,8)));
    }catch{}
  };
  const embedded=url=>{
    const u=new URL(url,location.origin);
    if(u.origin!==location.origin)return url;
    u.searchParams.set('embed','1');
    return u.pathname+u.search+u.hash;
  };

  // Command palette.
  const dialog=document.createElement('dialog');
  dialog.className='cmos-command-dialog';
  dialog.innerHTML='<div class="cmos-command-box"><input class="cmos-command-input" autocomplete="off" placeholder="Search or open anything in City Manager OS…"></div>'+
    '<div class="cmos-command-meta"><span>Open a module or search all records</span><span>↑↓ select · Enter open · Shift+Enter Quick Look</span></div>'+
    '<div class="cmos-command-list"></div>'+
    '<div class="cmos-command-footer"><span>⌘/Ctrl P · Open</span><span>Quick Capture remains available from every page</span></div>';
  document.body.appendChild(dialog);
  const input=dialog.querySelector('.cmos-command-input');
  const list=dialog.querySelector('.cmos-command-list');
  let selected=0,current=[];

  function filtered(){
    const q=input.value.trim().toLowerCase();
    const actions=quickActions.map(x=>({icon:x[0],title:x[1],detail:x[2],url:x[3],recent:false,action:true}));
    const base=commands.map(x=>({icon:x[0],title:x[1],detail:x[2],url:x[3],recent:false,action:false}));
    const recent=readRecent().map(x=>({icon:'↺',title:x.title,detail:'Recently opened',url:x.url,recent:true}));
    const pool=[...actions,...base,...recent];
    const all=q?pool.filter(x=>(x.title+' '+x.detail).toLowerCase().includes(q)):[...actions,...recent.slice(0,3),...base];
    return all.filter((x,i,a)=>a.findIndex(y=>y.url===x.url)===i).slice(0,28);
  }
  function render(){
    current=filtered();selected=Math.max(0,Math.min(selected,current.length-1));
    if(!current.length){
      list.innerHTML='<div class="cmos-command-empty">Press Enter to search all operational records for “'+escapeHtml(input.value)+'”.</div>';
      return;
    }
    list.innerHTML=current.map((x,i)=>'<button class="cmos-command-item '+(x.action?'action':'')+'" data-i="'+i+'" aria-selected="'+(i===selected)+'">'+
      '<span class="cmos-command-icon">'+escapeHtml(x.icon)+'</span><span class="cmos-command-copy"><strong>'+escapeHtml(x.title)+'</strong><small>'+escapeHtml(x.detail)+'</small></span>'+
      '<span class="cmos-command-key">'+(x.recent?'RECENT':'')+'</span></button>').join('');
  }
  function escapeHtml(v){return String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;")}
  function openCommand(){
    selected=0;input.value='';render();dialog.showModal();setTimeout(()=>input.focus(),0);
  }
  function smartCommandRoute(raw){
    const q=String(raw||'').trim(),lower=q.toLowerCase();
    if(!q)return null;
    const time=lower.match(/last\s+(\d+)\s*(hour|hours|hr|hrs|day|days)/);
    const customHours=time?Math.max(1,Math.min(8760,Number(time[1])*(time[2].startsWith('day')?24:1))):null;
    const stripTime=value=>value.replace(/\blast\s+\d+\s*(?:hour|hours|hr|hrs|day|days)\b/ig,'').replace(/\b(show|map|around|near|in)\b/ig,' ').replace(/\s+/g,' ').trim();
    if(/^(email|emails)\b/.test(lower)){
      const term=q.replace(/^(email|emails)(\s+from)?\s*/i,'').trim();
      const url='/intake?'+new URLSearchParams({intake_source:'MAIL',intake_q:term});
      return {url,title:'Executive Intake · Email'};
    }
    if(/^(calendar|meeting|meetings)\b/.test(lower)){
      const term=q.replace(/^(calendar|meeting|meetings)\s*/i,'').trim();
      const url='/intake?'+new URLSearchParams({intake_source:'CALENDAR',intake_q:term});
      return {url,title:'Executive Intake · Calendar'};
    }
    if(lower.startsWith('waiting on ')){
      const term=q.slice('waiting on '.length).trim();
      return {url:'/issues?'+new URLSearchParams({state:'waiting',q:term}),title:'Waiting On'};
    }
    if(lower.startsWith('watch ')){
      const term=q.slice(6).trim();
      return {url:'/watchlist?'+new URLSearchParams({setup_mode:'TOPIC',search_term:term,display_name:term}),title:'New Watch'};
    }
    if(customHours||/\b(map|show|near|around)\b/.test(lower)){
      const term=stripTime(q);
      const params=new URLSearchParams({map_view:'1',tab:'layers'});
      if(term)params.set('area_q',term);
      if(customHours){params.set('window','custom');params.set('custom_hours',String(customHours));}
      return {url:'/map?'+params,title:'Mapping Center'};
    }
    return {url:'/search?q='+encodeURIComponent(q),title:'Search'};
  }

  function use(item,quick=false){
    if(!item){
      const route=smartCommandRoute(input.value);
      if(route){dialog.close();quick?openSidecar(route.url,route.title):location.assign(route.url)}
      return;
    }
    if(item.url==='#quick-capture'){
      dialog.close();
      document.getElementById('qc-open')?.click();
      return;
    }
    remember(item.url,item.title);
    dialog.close();
    quick?openSidecar(item.url,item.title):location.assign(item.url);
  }
  input.addEventListener('input',()=>{selected=0;render()});
  input.addEventListener('keydown',e=>{
    if(e.key==='ArrowDown'){e.preventDefault();selected=Math.min(selected+1,current.length-1);render()}
    else if(e.key==='ArrowUp'){e.preventDefault();selected=Math.max(selected-1,0);render()}
    else if(e.key==='Enter'){e.preventDefault();use(current[selected],e.shiftKey)}
  });
  list.addEventListener('click',e=>{const b=e.target.closest('[data-i]');if(b)use(current[Number(b.dataset.i)],false)});
  dialog.addEventListener('click',e=>{if(e.target===dialog)dialog.close()});

  // The shared preserve-first rail exposes command launchers on every module.
  document.querySelectorAll('[data-cmos-command-trigger]').forEach(trigger=>trigger.addEventListener('click',openCommand));

  // Mobile rail.
  const mobileToggle=document.querySelector('.cmos-mobile-rail-toggle');
  const railBackdrop=document.querySelector('.cmos-rail-backdrop');
  if(mobileToggle&&railBackdrop){
    mobileToggle.addEventListener('click',()=>{
      document.body.classList.add('cmos-mobile-nav-open');
      railBackdrop.hidden=false;
    });
    railBackdrop.addEventListener('click',()=>{
      document.body.classList.remove('cmos-mobile-nav-open');
      railBackdrop.hidden=true;
    });
  }

  // Universal Quick Look sidecar.
  const backdrop=document.createElement('div');
  backdrop.className='cmos-sidecar-backdrop';
  const sidecar=document.createElement('aside');
  sidecar.className='cmos-sidecar';
  sidecar.setAttribute('aria-label','Quick Look');
  sidecar.innerHTML='<div class="cmos-sidecar-bar"><span class="cmos-sidecar-title">Quick Look</span>'+
    '<button type="button" class="cmos-sidecar-pin" title="Keep open">◇</button>'+
    '<button type="button" class="cmos-sidecar-focus" title="Focus">□</button>'+
    '<a class="cmos-sidecar-open" target="_top" title="Open full page">↗</a>'+
    '<button type="button" class="cmos-sidecar-close" title="Close">×</button></div>'+
    '<div class="cmos-sidecar-resize"></div><iframe class="cmos-sidecar-frame" title="City Manager OS Quick Look"></iframe>';
  document.body.append(backdrop,sidecar);
  const frame=sidecar.querySelector('iframe');
  const titleEl=sidecar.querySelector('.cmos-sidecar-title');
  const openEl=sidecar.querySelector('.cmos-sidecar-open');
  const pin=sidecar.querySelector('.cmos-sidecar-pin');
  const focus=sidecar.querySelector('.cmos-sidecar-focus');

  function openSidecar(url,title='Quick Look'){
    const u=new URL(url,location.origin);
    if(u.origin!==location.origin){location.href=url;return}
    titleEl.textContent=title||'Quick Look';
    openEl.href=u.pathname+u.search+u.hash;
    frame.src=embedded(u.href);
    document.body.classList.add('cmos-sidecar-open');
    remember(u.pathname+u.search+u.hash,title||u.pathname);
  }
  function closeSidecar(){
    document.body.classList.remove('cmos-sidecar-open','cmos-sidecar-pinned');
    sidecar.classList.remove('focused');pin.classList.remove('active');
    setTimeout(()=>{if(!document.body.classList.contains('cmos-sidecar-open'))frame.src='about:blank'},180);
  }
  backdrop.addEventListener('click',()=>{if(!document.body.classList.contains('cmos-sidecar-pinned'))closeSidecar()});
  sidecar.querySelector('.cmos-sidecar-close').addEventListener('click',closeSidecar);
  focus.addEventListener('click',()=>sidecar.classList.toggle('focused'));
  pin.addEventListener('click',()=>{
    document.body.classList.toggle('cmos-sidecar-pinned');
    pin.classList.toggle('active',document.body.classList.contains('cmos-sidecar-pinned'));
  });

  // Resizable split.
  const handle=sidecar.querySelector('.cmos-sidecar-resize');
  let resizing=false;
  handle.addEventListener('pointerdown',e=>{resizing=true;handle.setPointerCapture(e.pointerId);e.preventDefault()});
  handle.addEventListener('pointermove',e=>{
    if(!resizing||sidecar.classList.contains('focused'))return;
    const width=Math.max(380,Math.min(innerWidth-80,innerWidth-e.clientX));
    sidecar.style.width=width+'px';
  });
  handle.addEventListener('pointerup',()=>{resizing=false});

  // Anything marked data-cmos-context opens the existing full module in Quick Look.
  document.addEventListener('click',e=>{
    const target=e.target.closest('[data-cmos-context]');
    if(!target)return;
    const interactive=e.target.closest('a,button,input,select,textarea,summary');
    if(interactive&&interactive!==target)return;
    const url=target.dataset.cmosContext;
    if(!url)return;
    e.preventDefault();
    openSidecar(url,target.dataset.cmosTitle||target.querySelector('strong')?.textContent||'Quick Look');
  });
  document.addEventListener('keydown',e=>{
    if((e.metaKey||e.ctrlKey)&&!e.shiftKey&&e.key.toLowerCase()==='p'){e.preventDefault();openCommand()}
    if(e.key==='Escape'&&document.body.classList.contains('cmos-sidecar-open'))closeSidecar();
    const row=e.target.closest?.('[data-cmos-context]');
    if(row&&(e.key==='Enter'||e.key===' ')){e.preventDefault();openSidecar(row.dataset.cmosContext,row.dataset.cmosTitle||'Quick Look')}
  });

  // Global route links can opt into Quick Look without losing their original href.
  document.addEventListener('click',e=>{
    if(e.defaultPrevented)return;
    const a=e.target.closest('a[data-cmos-quicklook]');
    if(!a)return;
    e.preventDefault();
    openSidecar(a.href,a.dataset.cmosTitle||a.textContent.trim());
  });

  window.CMOS={openSidecar,openCommand};

  // Reorganize inherited module layouts around the user's primary task.
  // Existing forms, routes, controls and data stay untouched; only presentation order changes.
  function organizePageFlow(){
    const main=document.querySelector('body:not(.cmos-embedded)>main, body.cmos-embedded>main');
    if(!main)return;
    const path=location.pathname;
    const panelByTitle=title=>[...main.querySelectorAll('.panel')].find(p=>p.querySelector('.panel-head h2')?.textContent.trim()===title);
    const makeFlow=(title,description,actions=[])=>{
      const bar=document.createElement('section');
      bar.className='cmos-flow-header';
      bar.innerHTML='<div><span class="cmos-flow-kicker">WORKFLOW</span><strong>'+escapeHtml(title)+'</strong><p>'+escapeHtml(description)+'</p></div>'+
        '<div class="cmos-flow-actions">'+actions.map(a=>'<button type="button" data-flow-action="'+escapeHtml(a.key)+'">'+escapeHtml(a.label)+'</button>').join('')+'</div>';
      const first=main.querySelector('.metrics')||main.firstElementChild;
      if(first)first.insertAdjacentElement('afterend',bar); else main.prepend(bar);
      return bar;
    };

    if(path==='/issues'){
      const capture=main.querySelector('.command-capture');
      const queue=[...main.querySelectorAll('.panel')].find(p=>p.querySelector('.panel-kicker')?.textContent.includes('WORK QUEUE'));
      const tabs=main.querySelector('.command-tabs');
      if(capture&&queue){
        const flow=makeFlow('Review work first','Start with what needs attention. Create new work only when you need to capture something new.',[
          {key:'new-work',label:'+ New Work'}
        ]);
        if(tabs)flow.insertAdjacentElement('afterend',tabs);
        queue.insertAdjacentElement('afterend',capture);
        if(!capture.hasAttribute('open'))capture.open=false;
        flow.querySelector('[data-flow-action="new-work"]')?.addEventListener('click',()=>{
          capture.open=true;
          capture.scrollIntoView({behavior:'smooth',block:'start'});
          capture.querySelector('input[name="title"]')?.focus();
        });
        capture.classList.add('cmos-secondary-workflow');
      }
    }

    if(path==='/watchlist'){
      const health=main.querySelector('.watch-health-panel');
      const layout=main.querySelector('.watch-layout');
      const lab=main.querySelector('.watch-lab-panel');
      const bulk=main.querySelector('.watch-location-library');
      const metrics=main.querySelector('.watch-state-metrics');
      if(layout){
        const flow=makeFlow('Create or manage a Watch','Health problems stay visible first. Normal Watch creation and saved Watches come next; diagnostics and bulk GIS tools are advanced.',[
          {key:'new-watch',label:'+ New Watch'},
          {key:'advanced',label:'Advanced Tools'}
        ]);
        if(metrics)flow.insertAdjacentElement('beforebegin',metrics);
        if(health)layout.insertAdjacentElement('beforebegin',health);
        if(health)health.insertAdjacentElement('afterend',layout);

        const advanced=document.createElement('details');
        advanced.className='cmos-advanced-tools';
        advanced.innerHTML='<summary><span><strong>Advanced Tools</strong><small>Watch Lab, bulk GIS Watch creation, diagnostics and specialist workflows</small></span></summary><div class="cmos-advanced-tools-body"></div>';
        layout.insertAdjacentElement('afterend',advanced);
        const body=advanced.querySelector('.cmos-advanced-tools-body');
        if(lab)body.appendChild(lab);
        if(bulk)body.appendChild(bulk);

        flow.querySelector('[data-flow-action="new-watch"]')?.addEventListener('click',()=>{
          layout.querySelector('input[name="display_name"]')?.scrollIntoView({behavior:'smooth',block:'center'});
          setTimeout(()=>layout.querySelector('input[name="display_name"]')?.focus(),250);
        });
        flow.querySelector('[data-flow-action="advanced"]')?.addEventListener('click',()=>{
          advanced.open=true;advanced.scrollIntoView({behavior:'smooth',block:'start'});
        });
      }
    }

    if(path==='/alerts'){
      const search=main.querySelector('.alert-search-panel');
      const results=[...main.querySelectorAll('.panel')].find(p=>p.querySelector('.panel-head h2')?.textContent.trim()==='Alert history');
      const bulk=results?.querySelector('.bulk-action-toolbar');
      if(search&&results){
        const flow=makeFlow('Find and review alerts','Search first, review the incident and its Watch evidence, then take action. Bulk changes stay out of the way until you need them.',[
          {key:'map',label:'Search on Map'},
          {key:'bulk',label:'Bulk Actions'}
        ]);
        search.insertAdjacentElement('afterend',results);
        flow.querySelector('[data-flow-action="map"]')?.addEventListener('click',()=>openSidecar('/map','Mapping Center'));
        if(bulk){
          const hint=bulk.nextElementSibling?.classList.contains('hint')?bulk.nextElementSibling:null;
          const details=document.createElement('details');
          details.className='cmos-inline-advanced';
          details.innerHTML='<summary>Bulk management</summary><div class="cmos-inline-advanced-body"></div>';
          bulk.insertAdjacentElement('beforebegin',details);
          details.querySelector('.cmos-inline-advanced-body').appendChild(bulk);
          if(hint)details.querySelector('.cmos-inline-advanced-body').appendChild(hint);
          flow.querySelector('[data-flow-action="bulk"]')?.addEventListener('click',()=>{
            details.open=true;details.scrollIntoView({behavior:'smooth',block:'center'});
          });
        }
      }
    }

    if(path==='/spatial-reference'){
      const search=panelByTitle('Find a reference');
      const catalog=panelByTitle('Catalog');
      const adopt=panelByTitle('Add or refresh a reference');
      const coverage=panelByTitle('Linked source coverage');
      if(search&&catalog){
        const flow=makeFlow('Find a place, then act on it','The catalog is the primary workspace. Source adoption and refresh status are advanced maintenance tools.',[
          {key:'map',label:'Open Map'},
          {key:'advanced',label:'Manage Sources'}
        ]);
        search.insertAdjacentElement('afterend',catalog);
        const advanced=document.createElement('details');
        advanced.className='cmos-advanced-tools';
        advanced.innerHTML='<summary><span><strong>Source & Catalog Administration</strong><small>Add authoritative references, refresh linked sources, and inspect source coverage</small></span></summary><div class="cmos-advanced-tools-body"></div>';
        catalog.insertAdjacentElement('afterend',advanced);
        const body=advanced.querySelector('.cmos-advanced-tools-body');
        if(adopt)body.appendChild(adopt);
        if(coverage)body.appendChild(coverage);
        flow.querySelector('[data-flow-action="map"]')?.addEventListener('click',()=>openSidecar('/map','Mapping Center'));
        flow.querySelector('[data-flow-action="advanced"]')?.addEventListener('click',()=>{advanced.open=true;advanced.scrollIntoView({behavior:'smooth',block:'start'})});
      }
    }
  }

  organizePageFlow();

  // Deep-link quick actions into the newly organized primary workflows.
  if(location.pathname==='/issues'&&location.hash==='#new-work'){
    const capture=document.querySelector('.command-capture');
    if(capture){
      capture.open=true;
      setTimeout(()=>{
        capture.scrollIntoView({behavior:'smooth',block:'start'});
        capture.querySelector('input[name="title"]')?.focus();
      },80);
    }
  }
  if(location.pathname==='/watchlist'&&location.hash==='#new-watch'){
    setTimeout(()=>{
      const input=document.querySelector('.watch-layout input[name="display_name"]');
      input?.scrollIntoView({behavior:'smooth',block:'center'});
      input?.focus();
    },80);
  }
})();
