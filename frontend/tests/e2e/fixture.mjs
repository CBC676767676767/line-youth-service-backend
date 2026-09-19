import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';

const FORMAT = 'youth-isolated-web-test-v1';

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Set ${name} to the isolated web_smoke fixture configuration.`);
  return value;
}

export function hostPath(path) {
  if (process.platform !== 'win32' || !path.startsWith('/')) return path;
  return `\\\\wsl.localhost\\${process.env.YOUTH_E2E_WSL_DISTRO || 'kali-linux'}${path.replaceAll('/', '\\')}`;
}

export async function isolatedFixture() {
  const statePath = required('YOUTH_E2E_STATE_FILE');
  let state;
  try { state = JSON.parse(readFileSync(hostPath(statePath), 'utf8')); }
  catch { throw new Error('Cannot read the private fixture state. Start scripts/web_smoke.py serve first.'); }
  if (state.format !== FORMAT || !/^http:\/\/127\.0\.0\.1:\d{4,5}$/.test(state.base_url)) {
    throw new Error('Refusing a non-isolated or non-loopback test target.');
  }
  const settings = state.settings;
  if (settings.app_env !== 'test' || settings.mail_backend !== 'spool' || settings.scan_backend !== 'development' ||
      settings.smtp_host || settings.line_channel_id || settings.line_channel_secret || settings.line_channel_access_token) {
    throw new Error('Fixture must disable external mail and LINE and use development-only scans.');
  }
  const marker = JSON.parse(readFileSync(hostPath(state.data_dir + '/fixture-marker.json'), 'utf8'));
  if (marker.format !== FORMAT || marker.fixture_id !== state.fixture_id) throw new Error('Fixture marker mismatch.');
  const response = await fetch(state.base_url + '/__smoke__/identity');
  const identity = await response.json();
  if (!response.ok || identity.test_only !== true || identity.fixture_id !== state.fixture_id) {
    throw new Error('Refusing browser operations: live fixture identity mismatch.');
  }
  return state;
}

export function freshCodes(state) {
  const python = required('YOUTH_E2E_PYTHON');
  const statePath = required('YOUTH_E2E_STATE_FILE');
  let script = process.env.YOUTH_E2E_SMOKE_SCRIPT || resolve('../scripts/web_smoke.py');
  try {
    if (process.platform === 'win32' && python.startsWith('/')) {
      const distro = process.env.YOUTH_E2E_WSL_DISTRO || 'kali-linux';
      if (!script.startsWith('/')) script = execFileSync('wsl.exe', ['-d', distro, '--exec', 'wslpath', '-a', script], { stdio: ['ignore', 'pipe', 'ignore'], timeout: 10_000 }).toString().trim();
      execFileSync('wsl.exe', ['-d', distro, '--exec', python, script, 'codes', '--state-file', statePath], { stdio: 'ignore', timeout: 20_000 });
    } else execFileSync(python, [script, 'codes', '--state-file', statePath], { stdio: 'ignore', timeout: 20_000 });
    return JSON.parse(readFileSync(hostPath(state.codes_file), 'utf8'));
  } catch { throw new Error('Could not obtain fresh codes from the isolated private spool. No credentials were printed.'); }
}

export function syntheticDocument(state, kind) {
  const document = state.documents[kind];
  if (!document || !document.path.startsWith(state.data_dir + '/documents/')) throw new Error('Unknown synthetic fixture document.');
  return { name: document.file_name, mimeType: document.content_type, buffer: readFileSync(hostPath(document.path)) };
}

export async function localContext(browser, state, options = {}) {
  const context = await browser.newContext({ baseURL: state.base_url, viewport: { width: 1440, height: 1000 }, ...options });
  context.setDefaultTimeout(20_000);
  context.setDefaultNavigationTimeout(20_000);
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === state.base_url || ['data:', 'blob:'].includes(url.protocol)) await route.continue();
    else await route.abort('blockedbyclient');
  });
  return context;
}
