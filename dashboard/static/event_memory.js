(() => {
  'use strict';
  const notice = document.getElementById('memory-notice');
  const fileInput = document.getElementById('memory-file');
  const form = document.getElementById('memory-upload');
  const progress = document.getElementById('memory-progress');
  let current = null;
  function message(text, error = false) {
    notice.textContent = text;
    notice.classList.toggle('memory-error', error);
  }
  async function api(operation, values) {
    const response = await fetch('/event-memory/api/' + operation, {
      method: 'POST', cache: 'no-store', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...values, csrf: document.body.dataset.csrf})
    });
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Sign in again before continuing.');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Event Memory request failed.');
    return data;
  }
  async function digest(file) {
    if (!window.crypto?.subtle) throw new Error('Use the HTTPS dashboard domain for secure uploads.');
    const hash = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
    return [...new Uint8Array(hash)].map(v => v.toString(16).padStart(2, '0')).join('');
  }
  function transfer(grant, file) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', grant.upload_origin + '/v1/material');
      xhr.timeout = 120000;
      xhr.setRequestHeader('Authorization', 'Bearer ' + grant.ticket);
      const suffix = file.name.split('.').pop().toLowerCase();
      xhr.setRequestHeader('Content-Type', {pdf: 'application/pdf', jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png'}[suffix]);
      xhr.upload.onprogress = e => {if (e.lengthComputable) progress.value = Math.round(e.loaded / e.total * 100);};
      xhr.onload = () => {
        let data;
        try {data = JSON.parse(xhr.responseText);} catch {return reject(new Error('NAS returned an invalid response. Keep your original and check NAS.'));}
        if (xhr.status < 200 || xhr.status >= 300) return reject(new Error(data.detail || 'NAS rejected this upload.'));
        resolve(data);
      };
      const uncertain = () => reject(new Error('NAS upload not confirmed. Keep the original. Use Check NAS / recover link before uploading again.'));
      xhr.onerror = uncertain;
      xhr.ontimeout = uncertain;
      xhr.onabort = uncertain;
      xhr.send(file); // Direct to NAS. Never POST the file to a City Manager endpoint.
    });
  }
  form?.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const file = fileInput.files[0];
      if (!file || !file.size || file.size > 5 * 1024 * 1024) throw new Error('Choose a file from 1 byte to 5 MiB.');
      const sha256 = await digest(file);
      const fingerprint = file.name + ':' + sha256;
      if (!current || current.fingerprint !== fingerprint) current = {id: crypto.randomUUID(), fingerprint};
      message('Preparing a private NAS upload…');
      const grant = await api('prepare', {id: current.id, filename: file.name, bytes: file.size, sha256,
        source_kind: document.body.dataset.memoryKind, source_id: document.body.dataset.memoryId});
      if (grant.material.state === 'READY') {message('Already saved. Reloading the event.');location.reload();return;}
      progress.hidden = false;
      message('Uploading directly to NAS…');
      const receipt = await transfer(grant, file);
      message('NAS received the file. Linking the event and Brain…');
      await api('finalize', receipt);
      location.reload();
    } catch (error) {
      message(error.message + '\nNothing is silently saved on the VPS. Reload this page to find any pending upload and check NAS.', true);
    } finally {
      button.disabled = false;
      progress.hidden = true;
    }
  });
  document.querySelectorAll('[data-memory-action]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const download = button.dataset.memoryAction === 'download';
      const grant = await api('grant', {id: button.closest('[data-material-id]').dataset.materialId, purpose: download ? 'download' : 'upload'});
      const response = await fetch(grant.upload_origin + (download ? '/v1/file' : '/v1/receipt'), {
        headers: {Authorization: 'Bearer ' + grant.ticket}, cache: 'no-store', credentials: 'omit', redirect: 'error',
        signal: AbortSignal.timeout(120000)
      });
      if (!response.ok) throw new Error((await response.json()).detail || 'NAS is unavailable. Keep the original.');
      if (!download) {await api('finalize', await response.json());location.reload();return;}
      const blob = await response.blob();
      if (blob.size !== grant.material.bytes || await digest(blob) !== grant.material.sha256) throw new Error('Checksum mismatch. The downloaded copy is not the recorded original.');
      const url = URL.createObjectURL(blob), a = document.createElement('a');
      a.href = url;a.download = grant.material.filename;a.click();setTimeout(() => URL.revokeObjectURL(url), 30000);
      message('Downloaded directly from NAS.');
    } catch (error) {message(error.message, true);} finally {button.disabled = false;}
  }));
})();
