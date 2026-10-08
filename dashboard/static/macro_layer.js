(()=>{
  const params=new URLSearchParams(location.search);
  // Old embed bookmarks now open ordinary full pages.
  if(params.has('embed')){
    params.delete('embed');
    history.replaceState(history.state,'',location.pathname+(params.size?'?'+params:'')+location.hash);
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
    ['⚙','Modules','Turn workspace tools on or off','/modules'],
    ['⌕','Search','Search operational records','/search']
  ];

  const disabledToolPaths=JSON.parse(document.getElementById('cmos-disabled-tool-paths')?.textContent||'[]');
  const toolAvailable=url=>{
    const path=new URL(url,location.href).pathname;
    return !disabledToolPaths.some(prefix=>path===prefix||path.startsWith(prefix+'/'));
  };
  const recentKey='cmos.macro.recent.v1';
  const readRecent=()=>{try{return JSON.parse(localStorage.getItem(recentKey)||'[]')}catch{return[]}};
  const remember=(url,title)=>{
    try{
      const rows=readRecent().filter(x=>x.url!==url);
      rows.unshift({url,title,at:Date.now()});
      localStorage.setItem(recentKey,JSON.stringify(rows.slice(0,8)));
    }catch{}
  };
  // Command palette.
  const dialog=document.createElement('dialog');
  dialog.className='cmos-command-dialog';
  dialog.innerHTML='<div class="cmos-command-box"><input class="cmos-command-input" autocomplete="off" placeholder="Search or open anything in City Manager OS…"></div>'+
    '<div class="cmos-command-meta"><span>Open a module or search all records</span><span>↑↓ select · Enter open full page</span></div>'+
    '<div class="cmos-command-list"></div>'+
    '<div class="cmos-command-footer"><span>⌘/Ctrl P · Open</span><span>Quick Capture remains available from every page</span></div>';
  document.body.appendChild(dialog);
  const input=dialog.querySelector('.cmos-command-input');
  const list=dialog.querySelector('.cmos-command-list');
  let selected=0,current=[];

  function filtered(){
    const q=input.value.trim().toLowerCase();
    const actions=quickActions.filter(x=>toolAvailable(x[3])).map(x=>({icon:x[0],title:x[1],detail:x[2],url:x[3],recent:false,action:true}));
    const base=commands.filter(x=>toolAvailable(x[3])).map(x=>({icon:x[0],title:x[1],detail:x[2],url:x[3],recent:false,action:false}));
    const recent=readRecent().filter(x=>toolAvailable(x.url)).map(x=>({icon:'↺',title:x.title,detail:'Recently opened',url:x.url,recent:true}));
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
    if(toolAvailable('/map')&&(customHours||/\b(map|show|near|around)\b/.test(lower))){
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
      if(route){dialog.close();navigatePage(route.url,route.title)}
      return;
    }
    if(item.url==='#quick-capture'){
      dialog.close();
      document.getElementById('qc-open')?.click();
      return;
    }
    remember(item.url,item.title);
    dialog.close();
    navigatePage(item.url,item.title);
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

  // One mobile navigation state, with keyboard and focus recovery.
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

  // Private pages intentionally deny framing. Never embed login, editing,
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

