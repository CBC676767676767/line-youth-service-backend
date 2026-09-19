// All network calls are mocked. No account, browser storage, or real document is used.
import assert from 'node:assert/strict';
import { File } from 'node:buffer';
import { webcrypto } from 'node:crypto';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import { buildSync } from 'esbuild';

const compiled = buildSync({
  entryPoints: [fileURLToPath(new URL('../src/shared/api.ts', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs', target: 'node20',
}).outputFiles[0].text;

const jsonResponse = (value, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { 'Content-Type': 'application/json' },
});
const success = data => jsonResponse({ data });
const failure = (status, code, message = 'server message', fields = []) => jsonResponse({
  error: { code, message, field_errors: fields },
}, status);

function makeClient(responses = []) {
  const calls = [], events = [];
  const disallowStorage = () => { throw new Error('Authentication data must stay out of browser storage'); };
  const window = { dispatchEvent: event => { events.push(event.type); return true; } };
  Object.defineProperties(window, {
    localStorage: { get: disallowStorage }, sessionStorage: { get: disallowStorage },
  });
  const globals = { Headers, Event, crypto: webcrypto, window, fetch: async (url, options) => {
    calls.push({ url, ...options });
    assert.ok(responses.length, `Unexpected fetch: ${url}`);
    const next = responses.shift();
    if (next instanceof Error) throw next;
    return typeof next === 'function' ? next(url, options) : next;
  } };
  Object.defineProperties(globals, {
    localStorage: { get: disallowStorage }, sessionStorage: { get: disallowStorage },
  });
  const module = runInNewContext(`(() => {
    const module = { exports: {} }; const exports = module.exports;
    ${compiled}
    return module.exports;
  })()`, globals);
  return { ...module, calls, events };
}

test('CSRF stays in module memory, is renewed by the session response, and is sent only on mutations', async () => {
  const client = makeClient([success({ csrf_token: 'session-one' }), success({ saved: true }), success({})]);
  await client.api('/me');
  await client.api('/cases/case-1', {
    method: 'patch', credentials: 'include', cache: 'force-cache',
    json: { form_data: { name: 'Synthetic applicant' } },
    headers: { 'X-CSRF-Token': 'outdated', 'X-Request-Id': 'request-one' },
    etag: '"case-1-v3"', idempotencyKey: 'stable-operation-key',
  });
  const [read, write] = client.calls;
  assert.equal(read.headers.has('X-CSRF-Token'), false);
  assert.equal(write.url, '/api/v1/cases/case-1');
  assert.equal(write.method, 'PATCH');
  assert.equal(write.credentials, 'same-origin');
  assert.equal(write.cache, 'no-store');
  assert.equal(write.headers.get('X-CSRF-Token'), 'session-one');
  assert.equal(write.headers.get('X-Request-Id'), 'request-one');
  assert.equal(write.headers.get('If-Match'), '"case-1-v3"');
  assert.equal(write.headers.get('Idempotency-Key'), 'stable-operation-key');
  assert.deepEqual(JSON.parse(write.body), { form_data: { name: 'Synthetic applicant' } });
  client.setCsrf();
  await client.api('/auth/logout', { method: 'POST' });
  assert.equal(client.calls[2].headers.has('X-CSRF-Token'), false);

  const independent = makeClient([success({})]);
  await independent.api('/cases', { method: 'POST' });
  assert.equal(independent.calls[0].headers.has('X-CSRF-Token'), false);
});

test('a 401 clears CSRF and announces session expiry before a later request', async () => {
  const client = makeClient([failure(401, 'SESSION_EXPIRED'), success({})]);
  client.setCsrf('expired-secret');
  await assert.rejects(client.api('/cases'), error => error instanceof client.ApiError && error.status === 401);
  assert.deepEqual(client.events, ['youth:session-expired']);
  await client.api('/cases', { method: 'POST' });
  assert.equal(client.calls[1].headers.has('X-CSRF-Token'), false);
});

test('204 responses succeed without attempting to parse JSON', async () => {
  const client = makeClient([{
    status: 204, ok: true,
    json: () => { throw new Error('A 204 response has no body'); },
  }]);
  assert.equal(await client.api('/auth/logout', { method: 'POST' }), undefined);
});

test('validation fields and conflict codes survive errors without expiring a valid session', async () => {
  const fields = [{ field: 'amount', message: 'Use a decimal string' }];
  const client = makeClient([
    failure(422, 'FORM_INVALID', '請確認欄位', fields),
    failure(412, 'VERSION_CONFLICT'), failure(403, 'REAUTH_REQUIRED'),
    failure(409, 'IDEMPOTENCY_MISMATCH'), success({}),
  ]);
  client.setCsrf('still-valid');
  await assert.rejects(client.api('/cases/case-1', { method: 'PATCH', json: {} }), error => {
    assert.ok(error instanceof client.ApiError);
    assert.equal(error.code, 'FORM_INVALID');
    assert.equal(client.errorMessage(error), '請確認欄位');
    assert.deepEqual(error.fields, fields);
    return true;
  });
  await assert.rejects(client.api('/cases/case-1'), error => {
    assert.match(client.errorMessage(error), /重新讀取案件/);
    return error.status === 412;
  });
  await assert.rejects(client.api('/cases/case-1'), error => {
    assert.match(client.errorMessage(error), /重新登入/);
    return error.code === 'REAUTH_REQUIRED';
  });
  await assert.rejects(client.api('/cases/case-1'), error => error.code === 'IDEMPOTENCY_MISMATCH');
  assert.deepEqual(client.events, []);
  await client.api('/cases', { method: 'POST' });
  assert.equal(client.calls.at(-1).headers.get('X-CSRF-Token'), 'still-valid');
});

test('non-JSON failures and malformed successful envelopes become typed service errors', async () => {
  const client = makeClient([
    new Response('<html>upstream failure</html>', { status: 503 }),
    ...[null, {}, [], 123, true, 'unexpected'].map(value => jsonResponse(value)),
    new Response('not-json', { status: 200 }),
  ]);
  await assert.rejects(client.api('/me'), error => error instanceof client.ApiError &&
    error.status === 503 && error.code === 'REQUEST_FAILED');
  for (let i = 0; i < 7; i++) {
    await assert.rejects(client.api('/me'), error => error instanceof client.ApiError &&
      error.status === 502 && error.code === 'INVALID_RESPONSE');
  }
});

test('transport failure is not retried automatically; caller can retry the identical version and operation key', async () => {
  const client = makeClient([new Error('connection lost after send'), success({ receipt_id: 'receipt-one' })]);
  const options = { method: 'POST', etag: '"case-1-v4"', idempotencyKey: client.operationKey(),
    json: { file_version_ids: ['version-one'] } };
  await assert.rejects(client.api('/cases/case-1/submit', options), /connection lost/);
  assert.equal(client.calls.length, 1);
  assert.match(client.errorMessage(new Error('offline')), /確認是否已成功/);
  assert.deepEqual(await client.api('/cases/case-1/submit', options), { receipt_id: 'receipt-one' });
  assert.equal(client.calls[0].body, client.calls[1].body);
  assert.equal(client.calls[0].headers.get('If-Match'), client.calls[1].headers.get('If-Match'));
  assert.equal(client.calls[0].headers.get('Idempotency-Key'), client.calls[1].headers.get('Idempotency-Key'));
  assert.notEqual(client.operationKey(), options.idempotencyKey);
});

test('absolute, protocol-relative and parent-directory API paths never reach fetch', async () => {
  const client = makeClient();
  for (const path of ['https://elsewhere.invalid/cases', '//elsewhere.invalid/cases', 'cases', '/../cases', '/cases/../me']) {
    await assert.rejects(client.api(path), /Invalid API path/);
  }
  assert.equal(client.calls.length, 0);
});

const intent = (upload_url = '/api/v1/files/file-one/content') => ({
  file_id: 'file-one', file_version_id: 'version-one', upload_url,
  upload_headers: { 'X-Upload-Token': 'one-use-token', 'Content-Type': 'application/pdf' },
});
const uploaded = {
  file_id: 'file-one', file_version_id: 'version-one', case_id: 'case-one', task_id: 'task-one',
  document_type: 'PAYMENT_PROOF', file_name: 'synthetic.pdf', scan_status: 'PENDING_SCAN', allowed_actions: [],
};
const smallFile = () => new File(['%PDF-1.7\nsynthetic test content'], 'synthetic.pdf', { type: 'application/pdf' });

test('upload obtains an intent, sends original bytes, then completes that exact version with its task binding', async () => {
  const client = makeClient([success(intent()), success({ persisted: true }), success(uploaded)]);
  client.setCsrf('upload-csrf');
  const file = smallFile();
  const result = await client.uploadFile('case-one', 'PAYMENT_PROOF', file, 'task-one');
  assert.deepEqual(result, uploaded);
  assert.deepEqual(client.calls.map(call => [call.method, call.url]), [
    ['POST', '/api/v1/files/upload-intents'],
    ['PUT', '/api/v1/files/file-one/content'],
    ['POST', '/api/v1/files/file-one/complete'],
  ]);
  assert.deepEqual(JSON.parse(client.calls[0].body), {
    case_id: 'case-one', task_id: 'task-one', document_type: 'PAYMENT_PROOF',
    file_name: file.name, size_bytes: file.size, content_type: file.type,
  });
  assert.equal(client.calls[1].body, file);
  assert.equal(client.calls[1].headers.get('X-Upload-Token'), 'one-use-token');
  assert.equal(client.calls[1].headers.get('Content-Type'), 'application/pdf');
  assert.deepEqual(JSON.parse(client.calls[2].body), { file_version_id: 'version-one' });
  for (const call of client.calls) {
    assert.equal(call.credentials, 'same-origin');
    assert.equal(call.headers.get('X-CSRF-Token'), 'upload-csrf');
    assert.equal(call.url.includes('one-use-token'), false);
  }
  assert.equal(result.scan_status, 'PENDING_SCAN'); // Completion is not a clean scan or approval.
});

test('a failed upload stage never claims completion or starts a later stage', async () => {
  for (const responses of [
    [failure(403, 'UPLOAD_NOT_ALLOWED')],
    [success(intent()), failure(415, 'FILE_TYPE_MISMATCH')],
    [success(intent()), success({ persisted: true }), failure(409, 'FILE_INTEGRITY_ERROR')],
  ]) {
    const expectedCalls = responses.length;
    const client = makeClient(responses);
    await assert.rejects(client.uploadFile('case-one', 'RECEIPT', smallFile()),
      error => error instanceof client.ApiError);
    assert.equal(client.calls.length, expectedCalls);
    assert.equal(JSON.parse(client.calls[0].body).task_id, null);
  }
});

test('server upload locations cannot redirect file bytes or credentials off-origin or to a different route', async () => {
  for (const url of [
    'https://elsewhere.invalid/api/v1/files/file-one/content',
    '//elsewhere.invalid/api/v1/files/file-one/content',
    '/api/v1/files/file-one/content?token=unsafe', '/api/v1/files/file-one/content#fragment',
    '/api/v1/files/file-one/complete', '/uploads/file-one',
  ]) {
    const client = makeClient([success(intent(url))]);
    await assert.rejects(client.uploadFile('case-one', 'RECEIPT', smallFile()),
      error => error instanceof client.ApiError && error.code === 'UPLOAD_URL_INVALID');
    assert.equal(client.calls.length, 1, url);
  }
});

test('unsupported formats, empty files and files above 20 MiB fail before requesting an upload', async () => {
  const client = makeClient();
  for (const type of ['image/webp', 'image/svg+xml', 'text/html', 'application/octet-stream', '']) {
    await assert.rejects(client.uploadFile('case-one', 'RECEIPT', new File(['synthetic'], 'file', { type })),
      error => error instanceof client.ApiError && error.status === 415);
  }
  for (const size of [0, 20_971_521]) {
    const file = new File([new Uint8Array(size)], 'synthetic.pdf', { type: 'application/pdf' });
    await assert.rejects(client.uploadFile('case-one', 'RECEIPT', file),
      error => error instanceof client.ApiError && error.status === 413);
  }
  assert.equal(client.calls.length, 0);
});

test('a file of exactly 20 MiB is allowed through all upload phases', async () => {
  const client = makeClient([success(intent()), success({ persisted: true }), success(uploaded)]);
  const file = new File([new Uint8Array(20_971_520)], 'synthetic.pdf', { type: 'application/pdf' });
  await client.uploadFile('case-one', 'PAYMENT_PROOF', file);
  assert.equal(JSON.parse(client.calls[0].body).size_bytes, 20_971_520);
  assert.equal(client.calls[1].body, file);
  assert.equal(client.calls.length, 3);
});
