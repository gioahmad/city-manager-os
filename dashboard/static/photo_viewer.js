(()=>{
  const box=document.createElement('div');
  box.className='photo-lightbox';
  box.hidden=true;
  box.innerHTML=`<div class="photo-lightbox-toolbar"><div class="photo-lightbox-title" id="photo-lightbox-title">Photo</div><div class="photo-lightbox-actions"><button class="photo-lightbox-button" type="button" id="photo-lightbox-zoom">Zoom</button><a class="photo-lightbox-button" id="photo-lightbox-download" href="#">Download</a><button class="photo-lightbox-button" type="button" id="photo-lightbox-close">Close</button></div></div><div class="photo-lightbox-stage"><img class="photo-lightbox-image" id="photo-lightbox-image" alt="Operations photo"></div>`;
  document.body.appendChild(box);
  const image=box.querySelector('#photo-lightbox-image');
  const title=box.querySelector('#photo-lightbox-title');
  const download=box.querySelector('#photo-lightbox-download');
  const zoom=box.querySelector('#photo-lightbox-zoom');
  const close=box.querySelector('#photo-lightbox-close');
  function closeBox(){box.hidden=true;image.src='';image.classList.remove('zoomed');document.body.style.overflow='';}
  function toggleZoom(){image.classList.toggle('zoomed');zoom.textContent=image.classList.contains('zoomed')?'Fit':'Zoom';}
  document.addEventListener('click',e=>{
    const trigger=e.target.closest('[data-photo-view]');
    if(!trigger)return;
    e.preventDefault();
    const src=trigger.dataset.src||trigger.getAttribute('href');
    if(!src)return;
    title.textContent=trigger.dataset.photoTitle||trigger.getAttribute('aria-label')||'Operations photo';
    image.src=src;
    download.href=trigger.dataset.download||src;
    download.setAttribute('download','');
    box.hidden=false;
    document.body.style.overflow='hidden';
  });
  image.addEventListener('click',toggleZoom);
  zoom.addEventListener('click',toggleZoom);
  close.addEventListener('click',closeBox);
  box.addEventListener('click',e=>{if(e.target===box||e.target.classList.contains('photo-lightbox-stage'))closeBox();});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!box.hidden)closeBox();});
})();
