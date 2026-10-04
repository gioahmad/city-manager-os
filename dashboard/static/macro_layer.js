(()=>{
  const params=new URLSearchParams(location.search);
  if(params.get('embed')==='1'){
    document.body.classList.add('cmos-embedded');
    return;
  }

  const commands=[
    ['⌂','Overview','Executive operating picture','/'],
    ['◫','Workspace','Inbox, documents, people and private context','/workspace'],
    ['◷','My Day','Today, Waiting On and executive attention','/my-day'],
    ['✓','Command Center','Issues, commitments, decisions and follow-ups','/issues'],
    ['▣','Inbox','Triage quick captures and incoming work','/inbox'],
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
    const base=commands.map(x=>({icon:x[0],title:x[1],detail:x[2],url:x[3],recent:false}));
    const recent=readRecent().map(x=>({icon:'↺',title:x.title,detail:'Recently opened',url:x.url,recent:true}));
    const all=q?[...base,...recent].filter(x=>(x.title+' '+x.detail).toLowerCase().includes(q)):[...recent.slice(0,4),...base];
    return all.filter((x,i,a)=>a.findIndex(y=>y.url===x.url)===i).slice(0,28);
  }
  function render(){
    current=filtered();selected=Math.max(0,Math.min(selected,current.length-1));
    if(!current.length){
      list.innerHTML='<div class="cmos-command-empty">Press Enter to search all operational records for “'+escapeHtml(input.value)+'”.</div>';
      return;
    }
    list.innerHTML=current.map((x,i)=>'<button class="cmos-command-item" data-i="'+i+'" aria-selected="'+(i===selected)+'">'+
      '<span class="cmos-command-icon">'+escapeHtml(x.icon)+'</span><span class="cmos-command-copy"><strong>'+escapeHtml(x.title)+'</strong><small>'+escapeHtml(x.detail)+'</small></span>'+
      '<span class="cmos-command-key">'+(x.recent?'RECENT':'')+'</span></button>').join('');
  }
  function escapeHtml(v){return String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;")}
  function openCommand(){
    selected=0;input.value='';render();dialog.showModal();setTimeout(()=>input.focus(),0);
  }
  function use(item,quick=false){
    if(!item){
      const q=input.value.trim();
      if(q){dialog.close();location.href='/search?q='+encodeURIComponent(q)}
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
})();
