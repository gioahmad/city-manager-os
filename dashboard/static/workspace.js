(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const all = q => [...document.querySelectorAll(q)];
  const display = document.body.dataset.display === 'true';
  const readonly = document.body.dataset.readonly === 'true';
  const csrf = document.body.dataset.csrf;
  let state, selected, period = 'day', scope = 'both', initial = true;
  document.body.classList.toggle('display', display);
  document.body.classList.toggle('readonly', readonly);
  function node(tag, value, cls) {
    const n = document.createElement(tag);
    if (value !== undefined) n.textContent = String(value);
    if (cls) n.className = cls;
    return n;
  }
  function button(label, callback, cls = '') {
    const b = node('button', label, cls); b.type = 'button';
    b.addEventListener('click', callback); return b;
  }
  function link(label, href) {
    const a = node('a', label); a.href = href; return a;
  }
  function empty(container, label) {
    container.replaceChildren(node('p', label, 'muted'));
  }
  const pretty = s => String(s).replaceAll('_', ' ').toLowerCase();
  const entity = id => state.entities?.find(e => e.id === id);
  const formValues = form => Object.fromEntries(new FormData(form).entries());
  function notice(text, error = false) {
    $('notice').textContent = text; $('notice').classList.toggle('error', error);
  }
  async function act(action, data = {}, reload = true) {
    const response = await fetch('/workspace/api/action', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, csrf, ...data})
    });
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Sign in again to continue.');
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'The action could not be completed.');
    notice(result.message || 'Saved.');
    if (reload) await load();
    return result;
  }
  async function safely(callback) {
    try { return await callback(); } catch (e) { notice(e.message, true); }
  }
  function bindForm(id, action, transform = v => v) {
    $(id).addEventListener('submit', e => {
      e.preventDefault(); const form = e.currentTarget;
      safely(async () => {
        const submit = form.querySelector('button:not([type="button"])');
        if (submit) submit.disabled = true;
        try { await act(action, transform(formValues(form))); }
        finally { if (submit) submit.disabled = false; }
      });
    });
  }
  function navigate(view) {
    all('[data-panel]').forEach(s => s.hidden = s.dataset.panel !== view);
    all('[data-view]').forEach(b => {
      if (b.dataset.view === view) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    });
    $('breadcrumb').textContent = ({today:'Today',intelligence:'Area intelligence',work:'Work & requests',brain:'Brain',people:'People & places',dates:'Important dates',settings:'Settings'})[view];
  }
  function setOptions(select, rows, selectedValue) {
    const value = selectedValue ?? select.value;
    select.replaceChildren(...rows.map(r => { const o = node('option', r.name); o.value = r.id; return o; }));
    if (rows.some(r => r.id === value)) select.value = value;
  }
  function showScope() {
    all('[data-category]').forEach(p => p.hidden = (p.dataset.category === 'personal' && !state.config.personal) || (scope !== 'both' && p.dataset.category !== scope));
    all('[data-scope]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.scope === scope)));
  }
  function renderWork(target, rows, compact = false) {
    target.replaceChildren();
    if (!rows.length) return empty(target, 'No open work items.');
    for (const work of rows) {
      const row = node('div', undefined, compact ? 'entry' : 'panel');
      const content = node('div'); content.append(node('h3', work.title), node('p', work.next_action || work.address || 'No next action recorded', 'muted small'), node('span', pretty(work.status), 'badge'));
      const actions = node('div', undefined, 'row'); actions.append(link('Open work', '/issues?q=' + encodeURIComponent(work.title)));
      if (!readonly && !compact) actions.append(button('Create request link', () => safely(async () => {
        if (!confirm('Share this work title, status, and public conversation through a 30-day request link?')) return;
        const result = await act('PORTAL', {issue_id: work.id});
        $('portal-share').hidden = false;
        $('portal-url').value = result.portal_url.startsWith('/') ? location.origin + result.portal_url : result.portal_url;
      })));
      row.append(content, actions); target.append(row);
    }
  }
  function renderIntelligence() {
    $('alert-feed').replaceChildren();
    if (!state.alerts.length) empty($('alert-feed'), 'No alerts in this time window.');
    for (const alert of state.alerts) {
      const item = node('article', undefined, 'alert'); item.dataset.priority = alert.priority;
      const meta = node('div', undefined, 'row'); meta.append(node('span', alert.source, 'badge'), node('span', alert.municipality || 'Location not recorded', 'muted small'));
      item.append(meta, node('h3', alert.title), node('div', pretty(alert.status) + ' · ' + formatTime(alert.received_at), 'muted small'));
      if (!display) item.append(link('Open alert controls', '/alerts?q=' + encodeURIComponent(alert.alert_id)));
      $('alert-feed').append(item);
    }
    $('source-health').replaceChildren();
    if (!state.health.length) empty($('source-health'), 'No source health records.');
    for (const source of state.health) {
      const row = node('div', undefined, 'entry'), text = node('div');
      text.append(node('h3', source.source_id), node('div', 'Last success: ' + formatTime(source.last_success_at), 'muted small'));
      row.append(text, node('span', source.status, 'badge')); $('source-health').append(row);
    }
  }
  function formatTime(value) {
    return value ? new Intl.DateTimeFormat(undefined, {timeZone: state.config.timezone, dateStyle:'medium', timeStyle:'short'}).format(new Date(value)) : 'Not recorded';
  }
  function renderToday() {
    $('today-label').textContent = state.today + ' · ' + state.config.timezone;
    renderWork($('today-work'), state.work.slice(0, 6), true);
    $('personal-tasks').replaceChildren();
    if (!state.personal.length) empty($('personal-tasks'), 'Add a private task using quick capture.');
    for (const task of state.personal) {
      const row = node('div', undefined, 'entry'), label = node('label', undefined, 'row');
      const check = document.createElement('input'); check.type = 'checkbox'; check.style.width = '20px'; check.checked = task.done; check.disabled = readonly;
      check.addEventListener('change', () => safely(() => act('TASK', {id:task.id, done:check.checked})));
      label.append(check, node('span', task.title)); row.append(label);
      if (task.due_date) row.append(node('span', task.due_date, 'muted small'));
      $('personal-tasks').append(row);
    }
    const total = kind => state.health_logs.filter(r => r.kind === kind && r.day === state.today).reduce((n, r) => n + Number(r.amount), 0);
    $('water-total').textContent = total('WATER'); $('protein-total').textContent = total('PROTEIN');
    $('water-goal').textContent = state.goals.water_ml ? 'Your goal: ' + state.goals.water_ml + ' mL' : 'No goal set';
    $('protein-goal').textContent = state.goals.protein_g ? 'Your goal: ' + state.goals.protein_g + ' g' : 'No goal set';
    $('fast-button').textContent = state.fast.id ? 'End' : 'Start'; updateTimer();
    $('health-stats').replaceChildren();
    const table = node('table'), header = node('tr'); ['Day','Metric','Logged amount'].forEach(t => header.append(node('th', t))); table.append(header);
    state.health_logs.forEach(r => {const row = node('tr'); [r.day,pretty(r.kind),r.amount + (r.kind === 'WATER' ? ' mL' : ' g')].forEach(t => row.append(node('td', t))); table.append(row);});
    $('health-stats').append(table);
    state.fast_history.forEach(f => $('health-stats').append(node('p', 'Fast ended ' + formatTime(f.ended_at) + ' · ' + Math.round((new Date(f.ended_at) - new Date(f.started_at))/60000) + ' minutes', 'muted small')));
    renderReminders($('today-reminders'), state.reminders.filter(r => r.remind_on <= state.today));
    showScope();
  }
  function updateTimer() {
    if (!state || display) return;
    if (!state.fast.id) { $('fast-timer').textContent = 'Not running'; return; }
    const minutes = Math.max(0, Math.floor((Date.now() - new Date(state.fast.started_at))/60000));
    $('fast-timer').textContent = Math.floor(minutes/60) + 'h ' + minutes%60 + 'm';
  }
  function renderBrain() {
    $('brain-list').replaceChildren();
    if (!state.notes.length) return empty($('brain-list'), 'Capture a thought into your private Brain.');
    for (const note of state.notes) {
      const item = node('article', undefined, 'panel');
      item.append(node('span', pretty(note.kind), 'badge'), node('p', note.body, 'preserve'), node('div', (note.tags || []).map(t => '#' + t).join(' '), 'muted small'));
      $('brain-list').append(item);
    }
  }
  function selectRecord(id) { selected = id; renderDirectory(); renderInspector(); }
  function renderDirectory() {
    const q = $('person-search').value.trim().toLocaleLowerCase(); $('directory').replaceChildren();
    $('directory-limit').hidden = !state.directory_limited;
    const rows = state.entities.filter(e => (e.name + ' ' + JSON.stringify(e.attributes)).toLocaleLowerCase().includes(q));
    if (!rows.length) empty($('directory'), 'Add a record, or link your existing shared contacts.');
    for (const record of rows) {
      const b = button('', () => selectRecord(record.id), 'record-button'); b.setAttribute('aria-pressed', String(record.id === selected));
      const content = node('div'); content.append(node('h3', record.name), node('span', pretty(record.kind) + ' · ' + (record.visibility === 'PRIVATE' ? 'private' : 'work'), 'muted small'));
      b.append(node('span', record.name.split(/\s+/).map(x=>x[0]).slice(0,2).join(''), 'avatar'), content); $('directory').append(b);
    }
  }
  function renderInspector() {
    const pane = $('record-inspector'), record = entity(selected); pane.replaceChildren();
    if (!record) return empty(pane, 'Select a record to explore its connections.');
    pane.append(node('span', pretty(record.kind) + ' / ' + pretty(record.visibility), 'eyebrow'), node('h1', record.name));
    const attrs = record.attributes || {};
    for (const key of ['organization','title','address','unit','phones','emails','aliases','tags','notes']) {
      if (attrs[key]?.length) pane.append(node('p', key + ': ' + (Array.isArray(attrs[key]) ? attrs[key].join(', ') : attrs[key]), 'muted small'));
    }
    if (!readonly) {
      const controls = node('div', undefined, 'row');
      if (!record.contact_id) controls.append(button('Edit record', () => {const form = $('record-form'); form.hidden = false; for (const key of ['id','name','kind','visibility']) form.elements[key].value = record[key]; for (const key of ['organization','title','address','unit','phones','emails','aliases','tags','notes']) form.elements[key].value = Array.isArray(attrs[key]) ? attrs[key].join(', ') : attrs[key] || ''; form.elements.name.focus();}));
      else controls.append(link('Edit canonical contact', '/contacts?q=' + encodeURIComponent(record.name)));
      if (record.kind === 'PERSON') {
        controls.append(button('+ Important date', () => {navigate('dates'); $('date-form').hidden = false; $('date-form').elements.entity_id.value = record.id;}));
        controls.append(button('Draft text', () => compose(record)));
      }
      pane.append(controls);
    }
    pane.append(node('h2', 'Connections', 'section-title'));
    const connections = [...state.facts,...state.derived].filter(f => f.source_id === record.id || f.target_id === record.id);
    if (!connections.length) pane.append(node('p', 'No confirmed relationships yet.', 'muted'));
    else pane.append(relationshipGraph(record, connections));
    for (const f of connections) {
      const row = node('div', undefined, f.derived ? 'connection derived' : 'connection');
      const from = entity(f.source_id), to = entity(f.target_id); if (!from || !to) continue;
      row.append(node('h3', from.name + ' → ' + pretty(f.relation) + ' → ' + to.name), node('span', f.derived ? 'Derived' : 'Confirmed', 'badge'));
      if (f.derived) for (const proofId of f.proof) {const proof = state.facts.find(p=>p.id===proofId); if (proof) row.append(node('div', entity(proof.source_id)?.name + ' → ' + pretty(proof.relation) + ' → ' + entity(proof.target_id)?.name, 'evidence'));}
      else if (f.evidence) row.append(node('div', f.evidence, 'evidence'));
      row.append(button('Explore connected record', () => selectRecord(f.source_id === record.id ? f.target_id : f.source_id)));
      if (!readonly && !f.derived) row.append(button('Remove source relationship', () => safely(async()=> {if(confirm('Remove this fact? Derived connections will be recalculated.')) await act('REMOVE_RELATIONSHIP', {id:f.id});})));
      pane.append(row);
    }
  }
  function relationshipGraph(record, connections) {
    const ns = 'http://www.w3.org/2000/svg';
    const svgNode = (tag, attrs, text) => {
      const element = document.createElementNS(ns, tag);
      for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
      if (text !== undefined) element.textContent = text;
      return element;
    };
    const pairs = new Map();
    for (const f of connections) {
      const other = f.source_id === record.id ? f.target_id : f.source_id;
      if (!pairs.has(other) || (!f.derived && pairs.get(other).derived)) pairs.set(other, f);
    }
    const neighbors = [...pairs.entries()].slice(0, 8), height = Math.max(180, neighbors.length * 90);
    const wrap = node('div', undefined, 'relationship-graph');
    const svg = svgNode('svg', {viewBox: `0 0 640 ${height}`, role: 'group', 'aria-label': 'Connections for ' + record.name});
    const centerY = height / 2;
    const makeRecord = (item, x, y) => {
      const group = svgNode('g', {role: 'button', tabindex: 0, 'aria-label': 'Explore ' + item.name});
      group.append(svgNode('rect', {x:x-100, y:y-24, width:200, height:48, rx:8}),
        svgNode('text', {x, y:y+4, 'text-anchor':'middle'}, item.name.length > 25 ? item.name.slice(0,22)+'…' : item.name),
        svgNode('title', {}, item.name));
      group.addEventListener('click', () => selectRecord(item.id));
      group.addEventListener('keydown', e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); selectRecord(item.id);}});
      return group;
    };
    for (let i=0; i<neighbors.length; i++) {
      const [id, f] = neighbors[i], y=(i+.5)*height/neighbors.length;
      svg.append(svgNode('path', {d:`M 240 ${centerY} C 310 ${centerY}, 330 ${y}, 400 ${y}`, class:f.derived?'graph-edge derived':'graph-edge'}));
      const label=svgNode('text', {x:490,y:y+39,'text-anchor':'middle',class:'graph-label'},
        (f.source_id===record.id?'→ ':'← ')+pretty(f.relation)+(f.derived?' · derived':''));
      svg.append(label, makeRecord(entity(id), 500, y));
    }
    svg.append(makeRecord(record, 140, centerY)); wrap.append(svg);
    if (pairs.size > 8) wrap.append(node('p', 'Eight neighbors shown. All connections and evidence are listed below.', 'muted small'));
    return wrap;
  }
  function renderSuggestions() {
    $('suggestions').replaceChildren();
    if (!state.suggestions.length) return empty($('suggestions'), 'No suggested connections in the visible directory.');
    for (const s of state.suggestions) {
      const a = entity(s.source_id), b = entity(s.target_id), item = node('article', undefined, 'panel');
      item.append(node('h3', a.name + ' ↔ ' + b.name), node('span', s.strength, 'badge'), node('p', s.reasons.join(' · '), 'evidence'));
      if (!readonly) {const controls=node('div', undefined, 'row'); controls.append(button('Review relationship',()=> {selectRecord(a.id); $('relationship-form').elements.source_id.value=a.id; $('relationship-form').elements.target_id.value=b.id; $('relationship-form').elements.relation.value='LINKED_TO'; $('relationship-form').elements.evidence.value=s.reasons.join('; '); $('relationship-form').scrollIntoView({block:'center'});}),button('Dismiss',()=>safely(()=>act('DISMISS',{fingerprint:s.fingerprint})))); item.append(controls);}
      $('suggestions').append(item);
    }
  }
  function compose(person, reminder) {
    const form=$('text-form');
    if (!(person.attributes.phones || []).length) return notice('Save and verify a phone number on this person first.',true);
    form.elements.entity_id.value=person.id; form.elements.date_id.value=reminder?.id || '';
    form.elements.message_id.value=crypto.randomUUID();
    setOptions(form.elements.phone,person.attributes.phones.map(p=>({id:p,name:p})));
    form.elements.body.value=reminder?.draft || 'Hi ' + person.name.split(/\s+/)[0] + ', just checking in.';
    $('text-title').textContent=person.name + (reminder ? ' · ' + reminder.label : '');
    $('text-status').textContent=reminder?.context ? 'Private context: ' + reminder.context : '';
    $('text-send').disabled=false; $('text-dialog').showModal();
  }
  function renderReminders(target, rows) {
    target.replaceChildren(); if (!rows.length) return empty(target,'No reminders due in this period.');
    for (const reminder of rows) {
      const item=node('article',undefined,'entry'),body=node('div');
      body.append(node('h3',reminder.name),node('p',reminder.label + ' · ' + reminder.occurrence + (reminder.overdue ? ' · overdue' : ''),'muted small'));
      if(reminder.context) body.append(node('p',reminder.context,'muted small'));
      const controls=node('div',undefined,'row');
      if(!readonly) controls.append(button('Draft text',()=>compose(entity(reminder.entity_id),reminder)),button('Handled',()=>safely(()=>act('DATE_DONE',{id:reminder.id}))));
      item.append(body,controls);target.append(item);
    }
  }
  function renderDates() {
    renderReminders($('reminder-list'),state.reminders);
    $('all-dates').replaceChildren();
    for(const d of state.dates){const row=node('div',undefined,'entry');row.append(node('span',d.name+' · '+d.label+' · '+d.event_date+(d.annual?' · annually':'')));
      if(!readonly)row.append(button('Remove date',()=>safely(async()=>{if(confirm('Remove this reminder?'))await act('DATE_REMOVE',{id:d.id});})));$('all-dates').append(row);}
  }
  function renderPortals() {
    $('portal-list').replaceChildren(); if(!state.portals.length) empty($('portal-list'),'Create a scoped request link from a work item above.');
    for(const portal of state.portals){const card=node('article',undefined,'panel');card.append(node('h3',portal.title),node('p',(portal.revoked?'Revoked':'Expires '+formatTime(portal.expires_at))+' · '+pretty(portal.status),'muted small'));
      for(const message of portal.messages){const entry=node('div',undefined,'connection');entry.append(node('span',message.author==='REQUESTER'?'Requester':'Staff','badge'),node('p',message.body,'preserve'));card.append(entry);}
      if(!readonly&&!portal.revoked){const form=node('form',undefined,'row');const label=node('label',undefined,'grow');label.append(node('span','Public reply · visible to requester'));const input=document.createElement('textarea');input.rows=2;input.maxLength=4000;input.required=true;label.append(input);const send=node('button','Add public update','primary');form.append(label,send);form.addEventListener('submit',e=>{e.preventDefault();safely(()=>act('PORTAL_REPLY',{id:portal.id,body:input.value}));});card.append(form,button('Revoke link',()=>safely(async()=>{if(confirm('Revoke this request link?'))await act('PORTAL_REVOKE',{id:portal.id});})));}
      $('portal-list').append(card);}
  }
  async function load() {
    try {
      const response=await fetch('/workspace/api/state?'+new URLSearchParams({display:String(display),period,window:$('window').value}));
      if(!response.ok||!response.headers.get('content-type')?.includes('application/json'))throw new Error('Sign in again or check the database connection.');
      state=await response.json();
      $('workspace-name').textContent=state.config.name;$('organization').textContent=state.config.organization;
      document.title=state.config.name+(display?' · Area display':' · Workspace');
      $('refresh-time').textContent='Updated '+formatTime(state.refreshed_at);renderIntelligence();
      if(display){$('mode-badge').textContent='View only · work information';return;}
      renderToday();renderWork($('work-list'),state.work);renderBrain();renderDirectory();renderInspector();renderSuggestions();renderDates();renderPortals();
      const selectable=state.entities.map(e=>({id:e.id,name:e.name+' · '+pretty(e.kind)}));
      setOptions($('relationship-form').elements.source_id,selectable);setOptions($('relationship-form').elements.target_id,selectable);
      setOptions($('date-form').elements.entity_id,state.entities.filter(e=>e.kind==='PERSON'));
      if(initial){for(const key of ['name','organization','timezone','template','personal'])$('settings-form').elements[key].value=String(state.config[key]);
        $('goals').elements.water_ml.value=state.goals.water_ml || '';$('goals').elements.protein_g.value=state.goals.protein_g || '';initial=false;}
    }catch(e){notice('Data may be stale. '+e.message,true);throw e;}
  }
  all('[data-view]').forEach(b=>b.addEventListener('click',()=>navigate(b.dataset.view)));
  all('[data-open]').forEach(b=>b.addEventListener('click',()=>navigate(b.dataset.open)));
  all('[data-scope]').forEach(b=>b.addEventListener('click',()=>{scope=b.dataset.scope;showScope();}));
  all('[data-period]').forEach(b=>b.addEventListener('click',()=>{period=b.dataset.period;all('[data-period]').forEach(q=>q.setAttribute('aria-pressed',String(q===b)));safely(load);}));
  all('[data-log]').forEach(b=>b.addEventListener('click',()=>safely(()=>act('HEALTH',{metric:b.dataset.log,amount:b.dataset.amount}))));
  $('refresh').addEventListener('click',()=>safely(load));$('window').addEventListener('change',()=>safely(load));
  $('fast-button').addEventListener('click',()=>safely(()=>act(state.fast.id?'FAST_END':'FAST_START')));
  $('person-search').addEventListener('input',()=>state&&renderDirectory());
  $('add-record').addEventListener('click',()=>{$('record-form').reset();$('record-form').elements.id.value='';$('record-form').hidden=false;});
  $('add-date').addEventListener('click',()=>{$('date-form').hidden=!$('date-form').hidden;});
  $('import-contacts').addEventListener('click',()=>safely(()=>act('IMPORT_CONTACTS')));
  const relations=['MOTHER_OF','FATHER_OF','PARENT_OF','DAUGHTER_OF','SON_OF','CHILD_OF','SISTER_OF','BROTHER_OF','SIBLING_OF','SPOUSE_OF','AUNT_OF','UNCLE_OF','GUARDIAN_OF','WORKS_FOR','CONTACT_FOR','LIVES_AT','OWNS','MANAGES','ON_STREET','AFFECTS','MENTIONS','LINKED_TO'];
  setOptions($('relation-type'),relations.map(r=>({id:r,name:pretty(r)})));
  bindForm('capture','CAPTURE');bindForm('health-log','HEALTH');bindForm('goals','GOALS');bindForm('record-form','ENTITY');bindForm('relationship-form','RELATIONSHIP');
  bindForm('date-form','DATE',v=>({...v,annual:v.annual==='true'}));bindForm('settings-form','CONFIG',v=>({...v,personal:v.personal==='true'}));
  $('text-close').addEventListener('click',()=>$('text-dialog').close());
  $('text-form').addEventListener('submit',e=>{e.preventDefault();if(!confirm('Send this text now to '+e.currentTarget.elements.phone.value+' through SMSGate?'))return;const data=formValues(e.currentTarget);$('text-send').disabled=true;
    safely(async()=>{try{const result=await act('SEND_TEXT',data);$('text-status').textContent=result.message;}catch(err){$('text-status').textContent=err.message;$('text-status').classList.add('error');throw err;}});});
  if(display){navigate('intelligence');all('.writable,.tools-link').forEach(n=>n.hidden=true);setInterval(()=>safely(load),60000);}
  setInterval(updateTimer,30000);safely(load);
})();
