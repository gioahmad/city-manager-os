// Exercise the browser API helper with the responses returned by private auth.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('dashboard/static/outlook_contacts.js', 'utf8');
const helper = source.slice(source.indexOf('  async function api('), source.indexOf('  function button('));
const macro = fs.readFileSync('dashboard/static/macro_layer.js', 'utf8');
const sharedHelper = macro.slice(macro.indexOf('  async function readJsonResponse('), macro.indexOf('  // Legacy data-cmos-quicklook'));
const readJsonResponse = vm.runInNewContext(sharedHelper + '\nreadJsonResponse', {URL, location: {href: 'https://dashboard.example/watchlist'}});

const sharedResponse = (status, type, body, options = {}) => readJsonResponse({
  status, ok: status >= 200 && status < 300, redirected: false,
  headers: {get: () => type}, text: async () => body, ...options,
});

async function request(status, type, body, options = {}) {
  return vm.runInNewContext(helper + "\napi('status')", {
    API: '/workspace/api/microsoft/', csrf: 'test', URL, Error,
    fetch: async () => ({
      status, ok: status >= 200 && status < 300, redirected: false,
      url: 'https://dashboard.example/workspace/api/microsoft/status',
      headers: {get: () => type}, text: async () => body,
      json: async () => JSON.parse(body), ...options,
    }),
  });
}

(async () => {
  await assert.rejects(request(401, 'text/plain', 'Authentication required.'), /City Manager OS session expired/);
  await assert.rejects(request(200, 'text/html', '<html>Login</html>', {
    redirected: true, url: 'https://dashboard.example/login?next=/contacts',
  }), /City Manager OS session expired/);
  await assert.rejects(request(403, 'text/plain', 'Invalid request origin.'), /^Error: Invalid request origin\.$/);
  await assert.rejects(request(403, 'text/plain', 'Role does not allow this action.'), /Role does not allow/);
  await assert.rejects(request(500, 'text/plain', 'Internal Server Error'), /HTTP 500/);
  await assert.rejects(request(403, 'application/json', '{"detail":"Enable Microsoft contact access."}'), /Enable Microsoft contact access/);
  assert.equal((await request(200, 'application/json', '{"connected":true}')).connected, true);
  console.log('OUTLOOK REQUEST ERRORS: PASS — login, origin, role, server and Microsoft permission errors distinguished');
  await assert.rejects(sharedResponse(403, 'text/plain', 'Invalid request origin.'), /^Error: Invalid request origin\.$/);
  await assert.rejects(sharedResponse(400, 'application/json', '{"message":"Choose a recipient."}'), /Choose a recipient/);
  await assert.rejects(sharedResponse(422, 'application/json', '{"detail":[{"msg":"A location is required."}]}'), /A location is required/);
  await assert.rejects(sharedResponse(200, 'text/html', '<html>Login</html>', {redirected: true, url: 'https://dashboard.example/login?next=/watchlist'}), /sign in again/);
  assert.equal((await sharedResponse(200, 'application/json', '{"ok":true,"message":"Sent."}')).message, 'Sent.');
  assert.equal((await sharedResponse(200, 'application/json', '{"ok":false,"message":"No recipient."}')).ok, false);
  console.log('SHARED RESPONSE ERRORS: PASS — JSON, plain-text errors and sign-in pages remain readable');
})().catch(error => { console.error(error); process.exitCode = 1; });
