/* Organization appearance only. No fonts, stylesheets, or scripts are loaded from third parties. */
(()=>{
  const defaults={theme:'system',accent:'#4363a4',font:'system'},key='cmos.appearance.v1';
  const fonts={system:'ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif',humanist:'"Trebuchet MS",Calibri,Candara,sans-serif',serif:'Georgia,"Times New Roman",serif',mono:'ui-monospace,SFMono-Regular,Consolas,monospace'};
  const root=document.documentElement,media=matchMedia('(prefers-color-scheme:dark)');let saved=defaults,current=defaults,dirty=false;
  const valid=v=>v&&['system','light','dark'].includes(v.theme)&&Object.hasOwn(fonts,v.font)&&/^#[0-9a-f]{6}$/i.test(v.accent);
  function apply(v){
    current=v;root.dataset.colorMode=v.theme==='system'?(media.matches?'dark':'light'):v.theme;
    const rgb=[1,3,5].map(i=>parseInt(v.accent.slice(i,i+2),16));
    const luminance=rgb.map(c=>{c/=255;return c<=.04045?c/12.92:((c+.055)/1.055)**2.4}).reduce((s,c,i)=>s+c*[.2126,.7152,.0722][i],0);
    const fg=(luminance+.05)/.05>1.05/(luminance+.05)?'#000000':'#ffffff';
    const dark=root.dataset.colorMode==='dark';
    root.style.setProperty('--product-accent',v.accent);root.style.setProperty('--product-accent-text',fg);
    root.style.setProperty('--product-link',dark?'rgb('+rgb.map(c=>Math.round(c+(255-c)*.6)).join(',')+')':(luminance>.17?'rgb('+rgb.map(c=>Math.round(c*.55)).join(',')+')':v.accent));
    root.style.setProperty('--product-font',fonts[v.font]);
  }
  try{const cached=JSON.parse(localStorage.getItem(key));if(valid(cached))saved=cached;}catch{}
  apply(saved);media.addEventListener('change',()=>apply(current));
  const form=document.getElementById('appearance-form'),status=document.getElementById('appearance-status');
  const fields=()=>Object.fromEntries(new FormData(form));
  function fill(v){if(form)for(const name of ['theme','accent','font'])form.elements[name].value=v[name];}
  fill(saved);
  if(form){
    form.addEventListener('input',()=>{dirty=true;apply(fields());status.textContent='Preview · not saved yet';});
    form.querySelectorAll('[data-accent]').forEach(b=>b.addEventListener('click',()=>{form.elements.accent.value=b.dataset.accent;form.dispatchEvent(new Event('input'));}));
    document.getElementById('appearance-reset').addEventListener('click',()=>{fill(defaults);form.dispatchEvent(new Event('input'));});
    document.getElementById('appearance-cancel').addEventListener('click',()=>{dirty=false;fill(saved);apply(saved);status.textContent='Saved appearance restored';});
    form.addEventListener('submit',async e=>{
      e.preventDefault();const buttons=form.querySelectorAll('button');buttons.forEach(b=>b.disabled=true);const value=fields();
      try{const response=await fetch('/workspace/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'APPEARANCE',csrf:document.body.dataset.csrf,...value})});
        if(!response.ok){const error=await response.json().catch(()=>({}));throw Error(error.detail||'Could not save appearance.');}
        saved=value;dirty=false;apply(saved);try{localStorage.setItem(key,JSON.stringify(saved));}catch{}status.textContent='Saved across your workspace';
      }catch(e){status.textContent=e.message;}finally{buttons.forEach(b=>b.disabled=false);}
    });
  }
  fetch('/appearance',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error();return r.json();}).then(v=>{if(!valid(v))return;saved=v;try{localStorage.setItem(key,JSON.stringify(v));}catch{}if(!dirty){apply(saved);fill(saved);}}).catch(()=>{if(status)status.textContent='Saved settings unavailable. Preview works; retry saving when connected.';});
})();
