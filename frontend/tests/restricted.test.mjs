// The browser check only moves the refusal earlier; the server refuses anyway.
// These cases mirror the ones in tests/test_cases.py so the two cannot drift.
import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { buildSync } from 'esbuild';

const compiled = buildSync({
  entryPoints: [fileURLToPath(new URL('../src/restricted.ts', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs', target: 'node20',
}).outputFiles[0].text;
const loaded = { exports: {} };
new Function('module', 'exports', compiled)(loaded, loaded.exports);
const { matchDeclared, scanText } = loaded.exports;

const catalog = {
  version: 'test', notice: '', source: { title: 't', url: 'u', checked_date: '2026-09-20' },
  entries: [
    { id: 'capcut', name: 'CapCut', kind: 'tool', clause: 'c', distinctive: true, aliases: ['capcut', '剪映'] },
    { id: 'kling', name: 'Kling', kind: 'tool', clause: 'c', distinctive: true, aliases: ['kling', '可靈'] },
    { id: 'meitu', name: 'Meitu（美圖秀秀）', kind: 'tool', clause: 'c', distinctive: true, aliases: ['meitu', '美圖秀秀'] },
    { id: 'wink', name: 'Wink', kind: 'tool', clause: 'c', distinctive: false, aliases: ['wink'] },
    { id: 'manus', name: 'Manus', kind: 'tool', clause: 'c', distinctive: false, aliases: ['manus'] },
    { id: 'goingbus', name: 'GoingBus', kind: 'aggregator', clause: 'a', distinctive: true, aliases: ['goingbus'] },
  ],
};

test('a declared name is matched even when the word is common', () => {
  for (const named of ['CapCut', 'CapCut Pro', 'Kling', '美圖秀秀', 'Wink', 'Manus', 'GoingBus'])
    assert.ok(matchDeclared(named, catalog), named);
});

test('a longer word that merely contains a listed name is not a match', () => {
  for (const safe of ['manuscript', 'Winkler Studio', 'ChatGPT Plus', 'Perplexity Pro', 'Notion AI'])
    assert.equal(matchDeclared(safe, catalog), null, safe);
});

test('receipt text reports only names distinctive enough to rely on', () => {
  assert.deepEqual(scanText('wink beauty salon receipt NT$500', catalog).map((e) => e.name), []);
  assert.deepEqual(scanText('INVOICE CapCut Pro NT$390', catalog).map((e) => e.name), ['CapCut']);
  assert.deepEqual(scanText('OpenAI ChatGPT Plus USD 20.00', catalog).map((e) => e.name), []);
});

test('a missing catalogue never blocks, because the server still refuses', () => {
  assert.equal(matchDeclared('CapCut', null), null);
  assert.deepEqual(scanText('CapCut', null), []);
});
