(()=>{
  const form=document.querySelector('[data-alert-appearance]');
  if(!form)return;
  const data=JSON.parse(document.getElementById('alert-appearance-data').textContent);
  const drafts=JSON.parse(JSON.stringify(data.settings));
  const source=form.querySelector('[data-appearance-source]');
  const status=form.querySelector('[data-appearance-status]');
  const options=channel=>Object.fromEntries([...form.querySelectorAll(`[data-appearance-channel="${channel}"]`)].map(input=>[input.dataset.appearanceKey,input.checked]));
  function sourceLink(link,click){
    if(!link)return;
    link.hidden=true;
    if(!click)return;
    try{
      const url=new URL(click,location.href);
      if(['http:','https:'].includes(url.protocol)){link.href=url.href;link.hidden=false;}
    }catch{}
  }
  function updateStatus(){
    const changed=Object.keys(drafts).filter(key=>JSON.stringify(drafts[key])!==JSON.stringify(data.settings[key]));
    status.textContent=changed.length?`Unsaved choices for ${changed.join(', ')}. Save each source separately.`:'Preview uses a recent stored alert and sends nothing.';
  }
  function renderCards(){
    document.querySelectorAll('[data-alert-row]').forEach(row=>{
      const payload=row.querySelector('[data-alert-content]');
      if(!payload)return;
      const alert=JSON.parse(payload.textContent);
      const result=window.CmosAlertContent.render(alert,data.settings[alert.source]?.dashboard||data.defaults);
      row.querySelector('[data-alert-content-message]').textContent=result.message;
      sourceLink(row.querySelector('[data-alert-source-link]'),result.click);
    });
  }
  function preview(channel){
    const sample=data.samples[source.value];
    const caption=form.querySelector(`[data-appearance-sample="${channel}"]`);
    const title=form.querySelector(`[data-appearance-title="${channel}"]`);
    const message=form.querySelector(`[data-appearance-message="${channel}"]`);
    const link=form.querySelector(`[data-appearance-link="${channel}"]`);
    link.hidden=true;
    title.textContent=sample?.title||'';
    if(!sample){caption.textContent=`No stored ${source.value} alert is available to preview.`;message.textContent='';return;}
    caption.textContent=`Recent ${source.value} alert · ${sample.alert_id}`;
    const result=window.CmosAlertContent.render(sample,options(channel));
    message.textContent=result.message;
    sourceLink(link,result.click);
  }
  function selectSource(){
    form.querySelectorAll('[data-appearance-key]').forEach(input=>{
      input.checked=drafts[source.value]?.[input.dataset.appearanceChannel]?.[input.dataset.appearanceKey]??data.defaults[input.dataset.appearanceKey];
    });
    form.querySelectorAll('[data-appearance-pseg]').forEach(row=>{row.hidden=source.value.toUpperCase()!=='PSEG';});
    updateStatus();
    preview('dashboard');preview('notification');
  }
  source.addEventListener('change',selectSource);
  form.querySelectorAll('[data-appearance-preview]').forEach(button=>button.addEventListener('click',()=>preview(button.dataset.appearancePreview)));
  form.querySelectorAll('[data-appearance-key]').forEach(input=>input.addEventListener('change',()=>{
    drafts[source.value][input.dataset.appearanceChannel]=options(input.dataset.appearanceChannel);
    updateStatus();
    preview(input.dataset.appearanceChannel);
  }));
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const button=form.querySelector('[type="submit"]');
    button.disabled=true;status.textContent='Saving appearance…';
    try{
      const response=await fetch(form.action,{method:'POST',headers:{Accept:'application/json'},body:new FormData(form)});
      const saved=await window.CMOS.readJsonResponse(response);
      data.settings[saved.source]=saved.settings;
      renderCards();updateStatus();
      if(!status.textContent.startsWith('Unsaved'))status.textContent=`Saved appearance for ${saved.source}.`;
    }catch(error){status.textContent=error.message;}
    finally{button.disabled=false;}
  });
  if(location.hash==='#alert-appearance')document.getElementById('alert-appearance').open=true;
  selectSource();
  renderCards();
})();
