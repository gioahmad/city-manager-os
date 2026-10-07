(() => {
  'use strict';
  const $ = id => document.getElementById(id), API = '/workspace/api/microsoft/';
  const csrf = document.body.dataset.csrf, readonly = document.body.dataset.readonly === 'true';
  let status = {}, cursor = '', searchNumber = 0, syncNumber = 0, preview = null, contactId = '', syncData = null, review = null;
  const el = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = String(text); return node; };
  const notice = (text, id = 'outlook-notice') => { $(id).textContent = text; };
  const safe = async (fn, id) => { try { await fn(); } catch (e) { notice(e.message || 'Request failed.', id); } };
  async function api(path, data) {
    const response = await fetch(API + path, {cache:'no-store', ...(data ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({csrf, ...data})} : {})});
    if (!response.headers.get('content-type')?.includes('application/json')) throw Error('Sign in again, then return to Contacts.');
    const result = await response.json();
    if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'Could not complete this contact action.');
    return result;
  }
  function button(text, action) { const node = el('button', text); node.type = 'button'; node.addEventListener('click', () => safe(action)); return node; }
  async function connection() {
    status = await api('status');
    $('outlook-status').textContent = status.connected
      ? `${status.account_email || 'Microsoft connected'} · ${status.contact_count || 0} retained contacts · ${status.contacts_write ? 'Contact updates enabled' : 'Read only until you enable contact updates'}`
      : status.ready ? 'Microsoft is ready. Connect your Outlook account to find contacts.' : 'Microsoft needs server setup. Open Microsoft connection above for setup details.';
    if (!readonly) {
      $('outlook-connect').hidden = !status.ready || status.connected;
      $('outlook-enable').hidden = !status.ready || !status.connected || status.contacts_write;
      const data = await api('contact-links'), links = new Set(data.links.map(v => String(v.contact_id)));
      for (const box of document.querySelectorAll('.outlook-contact-actions')) {
        box.replaceChildren();
        if (links.has(box.dataset.contactId)) {
          box.append(el('p', 'Linked to your Outlook contact. Save your local edits first.'));
          box.append(button('Review Outlook update', () => openSync(box.dataset.contactId)));
        }
      }
    }
  }
  async function search(more = false) {
    const number = ++searchNumber, form = $('outlook-search');
    if (!more) { cursor = ''; $('outlook-results').replaceChildren(); }
    const params = new URLSearchParams({q:form.elements.q.value, live:String(form.elements.source.value === 'live'), cursor:more ? cursor : ''});
    $('outlook-more').hidden = true; notice('Finding Outlook contacts…');
    const result = await api('contacts?' + params);
    if (number !== searchNumber) return;
    cursor = result.next_cursor;
    notice(result.message + (result.items.length ? '' : ' No matches on this page.'));
    for (const item of result.items) {
      const card = el('article'); card.className = 'watch-card'; card.style.padding = '14px';
      card.append(el('strong', item.name), el('p', [item.attributes.organization, ...(item.attributes.emails || []), ...(item.attributes.phones || [])].filter(Boolean).join(' · ')));
      if (!readonly) card.append(button('Review and import', () => openImport(item.provider_key)));
      $('outlook-results').append(card);
    }
    $('outlook-more').hidden = !cursor;
  }
  async function openImport(provider) {
    preview = await api('contacts/preview', {provider_key:provider});
    if (preview.linked_contact_id) { location.href = '/contacts?' + new URLSearchParams({q:preview.fields.name, focus:preview.linked_contact_id}); return; }
    const form = $('outlook-import-form'); form.reset();
    for (const [key, value] of Object.entries(preview.fields)) form.elements[key].value = Array.isArray(value) ? value.join('\n') : value;
    const select = form.elements.contact_id; select.replaceChildren(); const fresh = el('option', 'Create a new private contact'); fresh.value = ''; select.append(fresh);
    for (const match of preview.matches) { const option = el('option', `${match.name} · ${match.organization || (match.emails || []).join(', ') || 'Directory contact'}`); option.value = match.id; select.append(option); }
    $('outlook-separate-label').hidden = !preview.matches.length;
    notice(preview.warnings.join('\n') || 'Phone numbers and email addresses are cleaned for review.', 'outlook-import-warnings');
    notice(preview.matches.length ? 'Possible duplicates found. Choose the matching record, or explicitly create a separate contact.' : 'This creates a private contact in Contacts.', 'outlook-import-notice');
    $('outlook-import-dialog').showModal();
  }
  async function openSync(id) {
    if (!status.contacts_write) { $('outlook-consent-dialog').showModal(); return; }
    const number = ++syncNumber, result = await api('contacts/' + encodeURIComponent(id) + '/sync');
    if (number !== syncNumber) return;
    contactId = id; syncData = result;
    const form = $('outlook-address-form'); form.reset(); $('outlook-address-fields').disabled = true;
    form.elements.kind.value = ['businessAddress','homeAddress','otherAddress'].find(key => Object.values(syncData.remote[key] || {}).some(Boolean)) || 'businessAddress';
    notice('Saved local contact: ' + syncData.local.name + '. The next screen shows current Outlook values and the proposed changes.', 'outlook-local-summary');
    const changed = syncData.local.address !== syncData.baseline.address;
    form.elements.include_address.checked = changed; $('outlook-address-fields').disabled = !changed;
    loadAddress();
    if (changed) {
      for (const key of ['street','city','state','postalCode','countryOrRegion']) form.elements[key].value = '';
      notice('Changed local address: ' + syncData.local.address + '. Enter its parts below, including empty fields you intend to clear.', 'outlook-address-notice');
    } else notice('Address is unchanged. Include it only if you want to update the structured Outlook address.', 'outlook-address-notice');
    $('outlook-address-dialog').showModal();
  }
  function loadAddress() {
    const form = $('outlook-address-form'), value = syncData.remote[form.elements.kind.value] || {};
    for (const key of ['street','city','state','postalCode','countryOrRegion']) form.elements[key].value = value[key] || '';
  }
  function showReview(operation) {
    review = operation; const box = $('outlook-review-content'); box.replaceChildren();
    box.append(el('p', 'Outlook account: ' + operation.review.account_email), el('strong', operation.review.title));
    for (const change of operation.review.changes) {
      box.append(el('h3', change.field.replace(/([A-Z])/g, ' $1')));
      box.append(el('p', 'Current Outlook value'), el('pre', JSON.stringify(change.before, null, 2)), el('p', 'Proposed value'), el('pre', JSON.stringify(change.after, null, 2)));
    }
    box.append(el('p', operation.review.warning)); notice('', 'outlook-review-notice');
    $('outlook-confirm').disabled = operation.status !== 'REVIEW'; $('outlook-review-dialog').showModal();
  }
  $('outlook-search').addEventListener('submit', event => { event.preventDefault(); safe(() => search()); });
  $('outlook-more').addEventListener('click', () => safe(() => search(true)));
  for (const control of [$('outlook-search').elements.q, $('outlook-search').elements.source]) control.addEventListener('input', () => { searchNumber++; cursor = ''; $('outlook-more').hidden = true; });
  for (const close of document.querySelectorAll('[data-outlook-close]')) close.addEventListener('click', () => $(close.dataset.outlookClose).close());
  for (const control of document.querySelectorAll('[data-clean-contact]')) control.addEventListener('click', () => {
    const form = control.closest('form');
    for (const name of ['name','organization','title','address','notes']) form.elements[name].value = form.elements[name].value.trim();
    for (const name of ['phones','emails']) {
      const seen = new Set();
      form.elements[name].value = form.elements[name].value.split(/[,;\n]+/).map(v => v.trim()).filter(Boolean).map(v => name === 'emails' ? v.toLowerCase() : v).filter(v => {
        const key = name === 'phones' ? v.replace(/[^0-9+]/g, '') : v; if (seen.has(key)) return false; seen.add(key); return true;
      }).join('\n');
    }
  });
  if (!readonly) {
    $('outlook-connect').addEventListener('click', () => safe(async () => { const data = await api('connect', {}); location.href = data.redirect_url; }));
    $('outlook-enable').addEventListener('click', () => $('outlook-consent-dialog').showModal());
    $('outlook-consent-continue').addEventListener('click', () => safe(async () => { const data = await api('connect', {enable_write:true, consent_reviewed:true}); location.href = data.redirect_url; }));
    $('outlook-import-form').addEventListener('submit', event => {
      event.preventDefault(); const form = event.currentTarget, submit = form.querySelector('[type=submit]'); submit.disabled = true;
      safe(async () => {
        const fields = Object.fromEntries(new FormData(form));
        const data = await api('contacts/import', {provider_key:preview.provider_key, version:preview.version, fields, contact_id:fields.contact_id, create_separate:form.elements.create_separate.checked});
        location.href = '/contacts?' + new URLSearchParams({q:fields.name, focus:data.contact_id});
      }, 'outlook-import-notice').finally(() => { submit.disabled = false; });
    });
    $('outlook-address-form').elements.include_address.addEventListener('change', event => { $('outlook-address-fields').disabled = !event.target.checked; });
    $('outlook-address-form').elements.kind.addEventListener('change', loadAddress);
    $('outlook-address-form').addEventListener('submit', event => {
      event.preventDefault(); const form = event.currentTarget, submit = form.querySelector('[type=submit]'); submit.disabled = true;
      safe(async () => {
        const data = {operation:'CONTACT_UPDATE', contact_id:contactId, request_id:crypto.randomUUID()};
        if (form.elements.include_address.checked) data.address = Object.fromEntries(new FormData(form));
        const operation = await api('prepare', data); $('outlook-address-dialog').close(); showReview(operation);
      }, 'outlook-address-notice').finally(() => { submit.disabled = false; });
    });
    $('outlook-confirm').addEventListener('click', () => {
      $('outlook-confirm').disabled = true;
      safe(async () => {
        const result = await api('execute', {operation_id:review.id, confirmed:true});
        review = result;
        notice(result.result.message || 'Operation status: ' + result.status, 'outlook-review-notice');
      }, 'outlook-review-notice');
    });
    const cancel = () => safe(async () => {
      if (review?.status === 'REVIEW') {
        const current = await api('operations/' + encodeURIComponent(review.id));
        if (current.status === 'REVIEW') await api('cancel', {operation_id:review.id});
      }
      $('outlook-review-dialog').close(); review = null;
    });
    $('outlook-cancel-review').addEventListener('click', cancel);
    $('outlook-review-dialog').addEventListener('cancel', event => { event.preventDefault(); cancel(); });
  }
  safe(connection);
})();
