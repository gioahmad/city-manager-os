(() => {
  'use strict';
  const $=id=>document.getElementById(id),readonly=document.body.dataset.readonly==='true';
  const csrf=document.body.dataset.csrf;
  let api,view,controller,detailController,pickerController,sequence=0,detailSequence=0,selected=null,items=[],more=false,busy=false,timer;
  const labels={MAIL:'Email',CALENDAR:'Calendar',CONTACT:'Contact import',DOCUMENT:'File',BRAIN:'Brain',TASK:'Personal task',WORK:'Work',EVENT:'Event',REQUEST:'Request',ALERT:'Area alert',RECORD:'Record'};
  const paths={MAIL:'M4 5h16v14H4z M4 6l8 6 8-6',CALENDAR:'M5 4h14v16H5z M8 2v4 M16 2v4 M5 9h14 M8 13h3 M13 13h3 M8 16h3',CONTACT:'M12 3a4 4 0 1 0 0 8 4 4 0 1 0 0-8 M4 21v-3a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v3',DOCUMENT:'M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h6',BRAIN:'M12 3v18 M3 12h18 M5 5l14 14 M5 19L19 5',TASK:'M9 3H4v17h16v-8 M8 10l4 4 9-10',WORK:'M4 7h16v14H4z M8 7V3h8v4 M4 13h16 M10 11v4h4v-4',EVENT:'M5 4h14v16H5z M8 2v4 M16 2v4 M5 9h14',REQUEST:'M3 4h18v13H9l-6 4z M7 8h10 M7 12h7',ALERT:'M12 3L2 21h20z M12 9v5 M12 17v1',RECORD:'M12 2l9 5v10l-9 5-9-5V7z M3 7l9 5 9-5 M12 12v10'};
  function node(tag,value,cls) {const n=document.createElement(tag);if(value!==undefined)n.textContent=String(value);if(cls)n.className=cls;return n;}
  function button(label,fn,cls='') {const b=node('button',label,cls);b.type='button';b.addEventListener('click',()=>safe(fn));return b;}
  function link(label,url) {const a=node('a',label,'button');a.href=url;return a;}
  function icon(kind){const box=node('span',undefined,'hub-icon kind-'+kind.toLowerCase()),svg=document.createElementNS('http://www.w3.org/2000/svg','svg'),path=document.createElementNS('http://www.w3.org/2000/svg','path');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('width','17');svg.setAttribute('height','17');svg.setAttribute('aria-hidden','true');path.setAttribute('d',paths[kind]);path.setAttribute('fill','none');path.setAttribute('stroke','currentColor');path.setAttribute('stroke-width','1.6');path.setAttribute('stroke-linecap','round');path.setAttribute('stroke-linejoin','round');svg.append(path);box.append(svg);return box;}
  async function safe(fn) {try{return await fn();}catch(e){if(e.name!=='AbortError')api.notice(e.message,true);}}
  async function json(url,options={}) {
    const response=await fetch(url,{cache:'no-store',...options});
    if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Sign in again to continue.');
    const data=await response.json();if(!response.ok)throw new Error(data.detail || 'Could not complete this request.');return data;
  }
  function filters() {return Object.fromEntries(new FormData($(view+'-filters')).entries());}
  function leave() {controller?.abort();detailController?.abort();pickerController?.abort();clearTimeout(timer);view=undefined;sequence++;detailSequence++;}
  async function load(nextView,force=false,append=false) {
    if(nextView!==view){leave();view=nextView;selected=null;items=[];closePreview();}
    controller?.abort();controller=new AbortController();const version=++sequence,current=view;
    $('loading-indicator').hidden=false;
    const params={view,...filters(),offset:String(append?items.length:0)};
    try {
      const data=await json('/workspace/api/hub?'+new URLSearchParams(params),{signal:controller.signal});
      if(version!==sequence || view!==current)return;
      items=append?[...items,...data.items]:data.items;more=data.has_more;renderList();
      $('local-answer-status').textContent=data.local_answers?'Local answers available':'Source evidence available · local model optional';
      $('library-ask').querySelector('button').textContent=data.local_answers?'Ask with sources':'Find evidence';
      if(selected){const row=items.find(r=>r.id===selected.id&&r.kind===selected.kind);if(row)await open(row,false);else closePreview();}
      schedulePoll();
      return data;
    } catch(e) {if(e.name!=='AbortError')throw e;} finally {if(version===sequence)$('loading-indicator').hidden=true;}
  }
  function time(value) {return new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date(value));}
  function schedulePoll(){
    clearTimeout(timer);const current=view;if(!current)return;
    const processing=items.some(r=>r.kind==='DOCUMENT'&&['QUEUED','PROCESSING'].includes(r.status));
    timer=setTimeout(async()=>{
      if(view!==current)return;
      const editing=['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)||$(view+'-preview').querySelector('.hub-task-form:not([hidden]),.hub-event-form:not([hidden]),.context-picker');
      if(!document.hidden&&!busy&&!editing&&items.length<=60)await safe(()=>load(current,true));
      else schedulePoll();
    },processing?6000:60000);
  }
  function renderList() {
    const container=$(view+'-items');container.replaceChildren();
    $(view+'-count').textContent=items.length+' record'+(items.length===1?'':'s')+(more?' · more available':'');
    $(view+'-more').hidden=!more;
    if(!items.length){const empty=node('div',undefined,'hub-empty');empty.append(node('h2',filters().q?'No matching sources.':view==='library'?'Your library starts here.':'A clear inbox.'),node('p',filters().q?'Try a shorter phrase or change the source and scope filters.':view==='library'?'Add a file above or save a thought to Brain.':'Connect Microsoft 365 in Settings, capture a thought, or switch to All records.','muted'));container.append(empty);return;}
    for(const row of items){
      const b=button('',()=>open(row),'hub-item'+(selected?.id===row.id&&selected?.kind===row.kind?' selected':''));
      b.dataset.kind=row.kind;b.dataset.id=row.id;b.setAttribute('aria-label',labels[row.kind]+': '+row.title);b.setAttribute('aria-pressed',String(selected?.id===row.id&&selected?.kind===row.kind));
      const glyph=icon(row.kind),body=node('span',undefined,'hub-item-body');
      const top=node('span',undefined,'hub-item-top');top.append(node('span',labels[row.kind],'hub-source'),node('span',row.visibility==='PRIVATE'?'Private':'Work','hub-privacy'),node('time',time(row.updated_at),'hub-time'));
      body.append(top,node('strong',row.title,'hub-item-title'),node('span',row.snippet.replace(/\s+/g,' '),'hub-snippet'));
      const state=node('span',row.status.replaceAll('_',' ').toLowerCase(),'hub-item-state'+(row.attention?' attention':''));
      b.append(glyph,body,state);container.append(b);
    }
  }
  function closePreview() {
    selected=null;detailController?.abort();detailSequence++;
    if(!view)return;
    $(view+'-layout').classList.remove('preview-open');
    const p=node('p','Choose a record.','preview-empty');p.append(node('br'),node('span','Its source, follow-ups, and connections appear here.'));$(view+'-preview').replaceChildren(p);
    renderList();
  }
  async function open(row,focus=true) {
    selected=row;detailController?.abort();detailController=new AbortController();const version=++detailSequence,current=view;
    $(view+'-layout').classList.add('preview-open');renderList();
    if(focus)$(view+'-preview').replaceChildren(node('p','Loading source…','muted'));
    const data=await json('/workspace/api/hub/detail/'+row.kind+'/'+row.id,{signal:detailController.signal});
    if(version!==detailSequence||view!==current)return;
    renderPreview(data);
    if(focus&&window.matchMedia('(max-width:900px)').matches){const preview=$(view+'-preview');preview.style.scrollMarginTop=(document.querySelector('.topbar').offsetHeight+12)+'px';preview.scrollIntoView({block:'start'});preview.querySelector('button')?.focus({preventScroll:true});}
  }
  async function act(action,extra={}) {
    if(busy)throw new Error('Please wait for the current action.');
    if(!selected)throw new Error('Select a source first.');
    busy=true;const source={...selected},current=view;document.body.classList.add('hub-saving');
    try {
      const result=await json('/workspace/api/hub/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,csrf,kind:source.kind,id:source.id,...extra})});
      api.clearCache();api.notice(result.message || 'Saved.');
      if(view===current){
        // A successful write remains successful even when the follow-up refresh fails.
        try {await load(current,true);} catch {api.notice((result.message || 'Saved.')+' Refresh is unavailable; use Refresh to update the view.');}
      }
      return result;
    } finally {busy=false;document.body.classList.remove('hub-saving');}
  }
  function renderPreview(data) {
    const {item,links,suggestions}=data,target=$(view+'-preview');target.replaceChildren();
    const row=items.find(r=>r.kind===item.kind&&r.id===item.id) || {};
    const head=node('div',undefined,'preview-top');head.append(button('← Back',()=>closePreview(),'preview-back'),node('span',item.visibility==='PRIVATE'?'Private · only you':'Internal work','badge'));target.append(head);
    target.append(node('span',labels[item.kind],'eyebrow'),node('h2',item.title,'preview-title'),node('p',time(item.updated_at)+' · '+item.status.replaceAll('_',' ').toLowerCase(),'muted small'));
    const meta=item.metadata;
    if(item.kind==='MAIL'){
      target.append(node('p','From: '+[meta.sender_name,meta.sender_email].filter(Boolean).join(' · '),'small'));
      if(meta.recipients.length)target.append(node('p','To: '+meta.recipients.map(r=>r.name||r.email).join(', '),'muted small'));
    }
    if(meta.work_title)target.append(node('p','Work: '+meta.work_title,'muted small'));
    const actions=node('div',undefined,'preview-actions');
    if(!readonly){
      actions.append(button('Make a follow-up',()=>{const form=target.querySelector('.hub-task-form');form.hidden=!form.hidden;if(!form.hidden)form.elements.title.focus();},'primary'));
      if(['MAIL','CALENDAR'].includes(item.kind)){
        actions.append(button('Bring into Work',()=>act('WORK',{title:item.title,item_type:'TASK',priority:3,next_action:'Review and determine the next municipal action.'})));
        actions.append(button('Add to Brain',()=>act('BRAIN',{body:item.body||item.title})));
        if(item.kind==='CALENDAR')actions.append(button('Bring into Events',()=>act('EVENT')));
        else actions.append(button('Create Event',()=>{const form=target.querySelector('.hub-event-form');form.hidden=!form.hidden;if(!form.hidden)form.elements.starts_at.focus();}));
      }
      actions.append(button(row.handled?'Return to inbox':'Mark handled',()=>act('HANDLE',{handled:!row.handled})));
      if(!row.handled)actions.append(button('Snooze',()=>{const raw=prompt('Snooze for how many hours?','24');if(raw===null)return;const hours=Number(raw);if(!Number.isFinite(hours)||hours<1)throw new Error('Enter at least 1 hour.');return act('SNOOZE',{hours:Math.round(hours)});}));
      if(item.kind==='CONTACT'&&!meta.entity_id)actions.append(button('Import as private person',()=>act('IMPORT_CONTACT')));
      if(item.kind==='DOCUMENT'&&['FAILED','NEEDS_OCR'].includes(item.status))actions.append(button('Retry processing',()=>act('RETRY')));
    }
    if(item.kind==='DOCUMENT')actions.append(link('Download original ↗','/workspace/documents/'+item.id+'/download'));
    else if(item.kind!=='MAIL'&&item.kind!=='CONTACT')actions.append(link('Open full controls ↗',item.kind==='WORK'?'/issues?q='+encodeURIComponent(item.title):item.route));
    actions.append(link('Open connected context ↗','/context/'+item.kind+'/'+item.id));
    if(meta.outlook_url){try{const u=new URL(meta.outlook_url);if(u.protocol==='https:'){const a=link('Open in Outlook ↗',u.href);a.target='_blank';a.rel='noopener noreferrer';actions.append(a);}}catch{}}
    target.append(actions);
    if(!readonly){
      const form=node('form',undefined,'hub-task-form');form.hidden=true;
      const title=node('label','Private follow-up'),input=node('input');input.name='title';input.required=true;input.maxLength=500;input.value=item.title.slice(0,500);title.append(input);
      const due=node('label','Due date (optional)'),date=node('input');date.name='due_date';date.type='date';due.append(date);
      form.append(title,due,node('button','Create linked task','primary'));form.addEventListener('submit',e=>{e.preventDefault();safe(()=>act('TASK',Object.fromEntries(new FormData(form).entries())));});target.append(form);
      if(item.kind==='MAIL'){
        const eventForm=node('form',undefined,'hub-event-form');eventForm.hidden=true;
        const startLabel=node('label','Event start'),start=document.createElement('input');start.name='starts_at';start.type='datetime-local';start.required=true;startLabel.append(start);
        const endLabel=node('label','Event end (optional)'),end=document.createElement('input');end.name='ends_at';end.type='datetime-local';endLabel.append(end);
        const locationLabel=node('label','Location (optional)'),location=document.createElement('input');location.name='location';location.maxLength=500;locationLabel.append(location);
        eventForm.append(startLabel,endLabel,locationLabel,node('button','Create linked event','primary'));
        eventForm.addEventListener('submit',e=>{e.preventDefault();safe(()=>act('EVENT',Object.fromEntries(new FormData(eventForm).entries())));});
        target.append(eventForm);
      }
    }
    if(item.kind==='DOCUMENT')renderProfile(target,meta.profile,item.status,meta.error);
    else if(item.kind==='CONTACT'||item.kind==='RECORD')renderAttributes(target,meta.attributes);
    if(item.body&&item.kind!=='CONTACT'&&item.kind!=='RECORD'){const content=node('div',item.body,'preview-content');target.append(content);}
    if(item.kind==='DOCUMENT'&&['QUEUED','PROCESSING'].includes(item.status))target.append(node('p',item.status==='QUEUED'?'Queued. The intake worker will process this file.':'Extracting text and profiling data…','processing-status'));
    if(meta.attachments)for(const file of meta.attachments)target.append(link(file.filename+' ↗','/brain/files/'+file.id));
    const context=node('section',undefined,'preview-context');context.append(node('h3','Linked context'));
    if(!links.length)context.append(node('p','No confirmed context links yet.','muted small'));
    for(const record of links){const entry=node('div',undefined,'context-row');entry.append(button(labels[record.kind]+' · '+record.title,()=>open(record),'context-open'));if(!readonly)entry.append(button('Remove',()=>act('UNLINK',{link_id:record.link_id}),'small'));context.append(entry);}
    if(!readonly){context.append(button('+ Link a record',()=>showPicker(context)));}
    if(suggestions.length){context.append(node('h3','Suggested context'),node('p','Matches are suggestions. Review the evidence before linking.','muted small'));
      for(const record of suggestions){if(links.some(r=>r.kind===record.kind&&r.id===record.id))continue;const entry=node('div',undefined,'context-suggestion');entry.append(node('strong',record.title),node('p',record.evidence,'muted small'));if(!readonly)entry.append(button('Confirm private link',()=>act('LINK',{target_kind:record.kind,target_id:record.id})));context.append(entry);}
    }
    target.append(context);
    if(!readonly&&item.kind==='DOCUMENT')target.append(button('Delete this file',()=>{if(confirm('Delete the original file and its extracted content?'))return act('DELETE_DOCUMENT');},'delete-file'));
  }
  function renderAttributes(target,attrs) {
    const list=node('dl',undefined,'source-attributes');
    for(const [key,value] of Object.entries(attrs||{})){if(value===null||value===''||(Array.isArray(value)&&!value.length))continue;list.append(node('dt',key.replaceAll('_',' ')),node('dd',Array.isArray(value)?value.join(', '):String(value)));}target.append(list);
  }
  function renderProfile(target,profile,status,error) {
    if(error)target.append(node('p',error,'error'));
    if(profile.note)target.append(node('p',profile.note,'processing-status'));
    if(!Object.keys(profile).length)return;
    const box=node('details',undefined,'data-profile');box.open=true;box.append(node('summary','What this file contains'));
    if(profile.pages)box.append(node('p',profile.pages+' PDF pages · '+profile.characters+' extracted characters','muted small'));
    if(profile.type==='dataset')dataset(box,profile);
    if(profile.type==='workbook')for(const sheet of profile.sheets){box.append(node('h4',sheet.name));dataset(box,sheet);}
    if(profile.emails?.length)box.append(node('p','Email addresses found: '+profile.emails.join(', '),'small'));
    if(profile.dates?.length)box.append(node('p','Date strings found: '+profile.dates.join(', '),'small'));
    if(!profile.pages&&!profile.rows&&!profile.sheets)box.append(node('p',profile.type+' · '+profile.characters+' extracted characters','muted small'));
    target.append(box);
  }
  function dataset(target,profile) {
    target.append(node('p',profile.rows.toLocaleString()+' data rows · '+profile.columns.length+' columns','profile-count'));
    if(profile.columns.length){const wrap=node('div',undefined,'profile-table-wrap'),table=node('table'),head=node('thead'),row=node('tr');for(const name of profile.columns)row.append(node('th',name));head.append(row);table.append(head);const body=node('tbody');for(const values of profile.sample.slice(0,10)){const tr=node('tr');for(const value of values)tr.append(node('td',value));body.append(tr);}table.append(body);wrap.append(table);target.append(wrap);}
    const stats=profile.column_stats.filter(s=>s.numeric_sample_count>0);if(stats.length){const details=node('details');details.append(node('summary','Numeric statistics · first 10,000 numeric values per column'));for(const stat of stats)details.append(node('p',stat.name+': min '+stat.min+' · max '+stat.max+' · mean '+Number(stat.mean.toFixed(3))+' · missing '+stat.missing,'small'));target.append(details);}
    if(profile.search_text_limited)target.append(node('p','Search text is limited to 200,000 characters. Download the original for every row.','muted small'));
  }
  function showPicker(context) {
    if(context.querySelector('.context-picker')){context.querySelector('input')?.focus();return;}
    const form=node('form',undefined,'context-picker'),label=node('label','Search a record to link privately'),input=node('input');input.type='search';input.placeholder='Name, title, street…';input.maxLength=500;label.append(input);const results=node('div');form.append(label,results);context.append(form);input.focus();let debounce;
    form.addEventListener('submit',e=>e.preventDefault());
    input.addEventListener('input',()=>{clearTimeout(debounce);pickerController?.abort();debounce=setTimeout(()=>safe(async()=>{
      if(!input.value.trim())return results.replaceChildren();pickerController=new AbortController();const source=selected;
      const data=await json('/workspace/api/hub?'+new URLSearchParams({q:input.value,bucket:'all'}),{signal:pickerController.signal});
      if(selected!==source)return;results.replaceChildren();
      for(const row of data.items.slice(0,10)){if(row.id===source.id&&row.kind===source.kind)continue;results.append(button(labels[row.kind]+' · '+row.title,()=>act('LINK',{target_kind:row.kind,target_id:row.id}),'context-result'));}
      if(!results.childElementCount)results.append(node('p','No matching accessible records.','muted small'));
    }),250);});
  }
  function init(callbacks) {
    api=callbacks;
    const incoming=new URLSearchParams(location.search);
    const inboxFilters=$('inbox-filters');
    if(inboxFilters){
      const source=incoming.get('intake_source'),query=incoming.get('intake_q');
      if(source&&inboxFilters.elements.source)inboxFilters.elements.source.value=source;
      if(query&&inboxFilters.elements.q)inboxFilters.elements.q.value=query;
    }
    for(const name of ['inbox','library']){
      const form=$(name+'-filters');let debounce;
      form.addEventListener('submit',e=>{e.preventDefault();safe(()=>load(name,true));});
      form.addEventListener('input',e=>{clearTimeout(debounce);if(e.target.type==='search')debounce=setTimeout(()=>safe(()=>load(name,true)),250);});
      form.addEventListener('change',e=>{if(e.target.tagName==='SELECT')safe(()=>load(name,true));});
      $(name+'-more').addEventListener('click',()=>safe(()=>load(name,true,true)));
    }
    $('library-upload').addEventListener('submit',e=>{e.preventDefault();safe(()=>upload(e.currentTarget));});
    $('library-ask').addEventListener('submit',e=>{e.preventDefault();safe(async()=>{
      const form=e.currentTarget,submit=form.querySelector('button'),target=$('library-answer');submit.disabled=true;target.textContent='Finding relevant sources…';
      try{
        const data=await json('/workspace/api/hub/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,question:form.elements.question.value})});
        target.replaceChildren(node('p',data.message,'muted small'));if(data.answer)target.append(node('div',data.answer,'preview-content'));
        data.sources.forEach((source,i)=>{const entry=node('div',undefined,'answer-source');entry.append(button('['+(i+1)+'] '+source.title,async()=>{await api.navigate('library');await open(source);}),node('p',source.excerpt.slice(0,450),'muted small'));target.append(entry);});
      }finally{submit.disabled=false;}
    });});
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&view&&selected){closePreview();e.preventDefault();}});
  }
  async function upload(form) {
    if(busy)throw new Error('Please wait for the current action.');
    const files=form.elements.files.files;if(!files.length)return;
    if(files.length>10||[...files].some(f=>f.size>20*1024*1024)||[...files].reduce((s,f)=>s+f.size,0)>50*1024*1024)throw new Error('Use up to 10 files, 20 MB each, and 50 MB total.');
    const data=new FormData(form);data.append('csrf',csrf);const submit=form.querySelector('button');busy=true;submit.disabled=true;
    $('upload-progress').hidden=false;$('upload-progress').value=0;$('upload-status').textContent='Uploading…';
    try {
      const result=await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/workspace/api/hub/upload');xhr.timeout=180000;
        xhr.upload.onprogress=e=>{if(e.lengthComputable){const pct=Math.round(e.loaded/e.total*100);$('upload-progress').value=pct;$('upload-status').textContent=pct===100?'Uploaded · saving private originals…':'Uploading · '+pct+'%';}};
        xhr.onload=()=>{try{const result=JSON.parse(xhr.responseText);if(xhr.status>=200&&xhr.status<300)resolve(result);else reject(new Error(result.detail||'Upload failed.'));}catch{reject(new Error('Sign in again before uploading.'));}};
        xhr.onerror=()=>reject(new Error('Upload connection failed.'));xhr.ontimeout=()=>reject(new Error('Upload timed out. Refresh the library to check whether it saved.'));xhr.send(data);});
      form.reset();$('upload-status').textContent=result.message;api.notice(result.message);api.clearCache();
      if(view==='library')await safe(()=>load('library',true));
    } catch(e){$('upload-status').textContent=e.message;throw e;}finally{busy=false;submit.disabled=false;}
  }
  window.CmosHub={init,load,leave};
})();
