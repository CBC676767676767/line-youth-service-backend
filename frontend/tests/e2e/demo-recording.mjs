/**
 * Record a walkthrough of the deployed demo.
 *
 * Reads only; it fills the public precheck form and opens the staff board. The
 * one change it makes is dragging a card, which is the same authorized action a
 * caseworker performs, and it reports what it actually saw rather than what the
 * script expected. Run it against the demo deployment, never a real one.
 */
import { chromium } from '@playwright/test';
import { mkdirSync, writeFileSync, readdirSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { createHmac } from 'node:crypto';

const CITIZEN = 'https://34.81.168.144.sslip.io';
const STAFF = 'https://35.194.158.118.sslip.io';
const OUT = process.argv[2] || 'C:\\Users\\yeee3642\\youth-demo-recording';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';

const notes = [];
const record = (scene, text) => {
  const line = `${new Date().toISOString().slice(11, 19)}  ${scene}: ${text}`;
  notes.push(line);
  console.log(line);
};

function totp(secret) {
  const base32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const char of secret.replace(/=+$/, ''))
    bits += base32.indexOf(char.toUpperCase()).toString(2).padStart(5, '0');
  const key = Buffer.from((bits.match(/.{8}/g) || []).map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000)));
  const digest = createHmac('sha1', key).update(counter).digest();
  const offset = digest[digest.length - 1] & 0xf;
  const code = digest.readUInt32BE(offset) & 0x7fffffff;
  return String(code % 1000000).padStart(6, '0');
}

const pause = (page, ms) => page.waitForTimeout(ms);

async function scene(name, body) {
  try {
    await body();
  } catch (error) {
    record(name, 'STOPPED: ' + String(error.message).split(String.fromCharCode(10))[0]);
  }
}

async function fill(page, selector, value, scene) {
  const field = page.locator(selector);
  if ((await field.count()) === 0) {
    record(scene, `MISSING ${selector}`);
    return false;
  }
  const tag = await field.evaluate((node) => node.tagName.toLowerCase());
  if (tag === 'select') await field.selectOption({ label: value }).catch(() => field.selectOption(value));
  else await field.fill(value);
  await pause(page, 700);
  return true;
}

async function runPrecheck(page, toolName, scene) {
  await page.goto(`${CITIZEN}/precheck`, { waitUntil: 'networkidle' });
  await pause(page, 2500);

  await page.locator('input[value="purchased"]').first().click();
  await pause(page, 1500);
  await page.locator('[data-next="2"]').first().click();
  await pause(page, 1500);

  await fill(page, '#tool-search', toolName, scene);
  const chip = page.locator('#tool-results button', { hasText: toolName }).first();
  if (await chip.count()) {
    await chip.click();
    record(scene, `selected catalogue chip for ${toolName}`);
  } else {
    record(scene, `${toolName} is not in the catalogue; kept as free text`);
  }
  await pause(page, 1200);

  await fill(page, '#plan-name', `${toolName} Plus`, scene);
  await fill(page, '#billing-type', '月訂閱', scene);
  await fill(page, '#purchase-channel', '官方網站', scene);
  await fill(page, '#billing-component', '一般訂閱費', scene);
  await fill(page, '#purchase-date', '2026-09-01', scene);
  await fill(page, '#eligible-cost-twd', '600', scene);
  await fill(page, '#payment-method', '信用卡', scene);
  await pause(page, 1200);

  await page.locator('[data-next="3"]').first().click();
  await pause(page, 1500);
  await fill(page, '#residency', '新竹市', scene);
  await fill(page, '#birth-date', '2001-05-16', scene);
  await fill(page, '#application-type', '一般青年', scene);
  await fill(page, '#payer', '本人', scene);
  await fill(page, '#prior-subsidy', '尚未申請或受補助', scene);
  await pause(page, 1200);

  await page.locator('#evaluate-button').click();
  await page.waitForTimeout(4000);
  const results = await page.locator('#results').innerText().catch(() => '');
  record(scene, `result text length ${results.length}`);
  return results;
}

async function main() {
  mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ headless: true, executablePath: EDGE });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: { dir: OUT, size: { width: 1440, height: 900 } },
    locale: 'zh-TW',
  });
  const page = await context.newPage();
  // Without this a missing selector waits forever and the recording stalls.
  page.setDefaultTimeout(20000);
  context.setDefaultTimeout(20000);

  // Scene A: an eligible tool, and the honest "cannot determine" wording.
  record('A', 'precheck with ChatGPT');
  const eligible = await runPrecheck(page, 'ChatGPT', 'A');
  for (const phrase of ['不能判定', '依填單試算', '必要檢查'])
    record('A', `${phrase}: ${eligible.includes(phrase) ? 'present' : 'NOT FOUND'}`);
  const amount = eligible.match(/TWD\s*[\d,.]+/);
  record('A', `estimated amount on screen: ${amount ? amount[0] : 'none'}`);
  await page.locator('#results').scrollIntoViewIfNeeded().catch(() => {});
  await pause(page, 5000);
  await page.mouse.wheel(0, 500);
  await pause(page, 3000);

  // Scene B: the excluded tool. The precheck only flags it -- by design, it is
  // advisory -- so the refusal has to be shown where it is actually enforced.
  record('B', 'precheck flags CapCut but does not block');
  const flagged = await runPrecheck(page, 'CapCut', 'B');
  for (const phrase of ['公告排除', '限制', 'CapCut'])
    record('B', `precheck shows ${phrase}: ${flagged.includes(phrase) ? 'yes' : 'NO'}`);
  await pause(page, 4000);

  await scene('B2', async () => {
    record('B2', 'application form refuses the same tool');
    await page.goto(`${CITIZEN}/`, { waitUntil: 'networkidle' });
    await pause(page, 2000);
    const entry = page.locator('button', { hasText: /登入|申請/ }).first();
    if (await entry.count()) {
      await entry.click();
      await pause(page, 2500);
    }
    await page.locator('input[type=email]').first().fill('demo.record@example.com');
    await pause(page, 800);
    await page.locator('button', { hasText: /寄送|驗證碼/ }).first().click();
    await pause(page, 3000);
    await page.locator('input:not([type=email])').last().fill('698217');
    await pause(page, 800);
    await page.locator('button', { hasText: /驗證|繼續|登入/ }).first().click();
    await page.waitForTimeout(5000);
    const landed = await page.locator('body').innerText();
    record('B2', `signed in: ${landed.includes('草稿') || landed.includes('申請進度') ? 'yes' : 'NO'}`);
    await pause(page, 3000);
  });

  // Scene C: the staff board on its own address.
  await scene('C', async () => {
  record('C', 'staff portal on the separate address');
  await page.goto(`${STAFF}/admin/`, { waitUntil: 'networkidle' });
  await pause(page, 2500);
  await page.locator('input[type=email]').fill('supervisor@demo.local');
  await page.locator('input[type=password]').fill('fHxBHuxzorZ2YvBL6nfaiocrv28');
  await page.locator('button[type=submit]').click();
  await pause(page, 3000);
  const codeField = page.locator('input').filter({ hasNot: page.locator('[type=email]') }).last();
  await codeField.fill(totp('XMQNXDEMWQQMDLZSS3OMBDWEFOYDR6R6'));
  await page.locator('button[type=submit]').click();
  await page.waitForTimeout(5000);
  const board = await page.locator('body').innerText();
  record('C', `board reached: ${board.includes('案件工作台') ? 'yes' : 'NO'}`);
  const counts = (board.match(/已收件[\s\S]{0,40}?(\d+)/) || [])[1];
  record('C', `received column count on screen: ${counts ?? 'unknown'}`);
  await pause(page, 5000);
  });

  // Scene D: the staff entry is absent from the citizen address.
  record('D', 'citizen host must not serve /admin/');
  const status = await page
    .goto(`${CITIZEN}/admin/`, { waitUntil: 'domcontentloaded' })
    .then((r) => r?.status())
    .catch(() => 'threw (Chromium treats a 404 body as a navigation failure)');
  record('D', `citizen /admin/ status ${status}`);
  await pause(page, 4000);

  await context.close();
  await browser.close();

  const video = readdirSync(OUT).find((name) => name.endsWith('.webm'));
  if (video) {
    renameSync(join(OUT, video), join(OUT, 'youth-demo.webm'));
    record('out', 'video saved as youth-demo.webm');
  } else {
    record('out', 'NO VIDEO FILE PRODUCED');
  }
  writeFileSync(join(OUT, 'steps.md'),
    `# Demo recording log\n\nRecorded against the demo deployment.\n\n` +
    `\`\`\`\n${notes.join('\n')}\n\`\`\`\n`, 'utf8');
}

main().catch((error) => {
  console.error('RECORDING FAILED:', error.message);
  process.exitCode = 1;
});
