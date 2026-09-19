// Synthetic local data only; importing candidates never submits an application.
import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { buildSync } from 'esbuild';

function load(relativePath) {
  const compiled = buildSync({
    entryPoints: [fileURLToPath(new URL(relativePath, import.meta.url))],
    bundle: true, write: false, platform: 'node', format: 'cjs', target: 'node20',
  }).outputFiles[0].text;
  const module = { exports: {} };
  new Function('module', 'exports', compiled)(module, module.exports);
  return module.exports;
}
const { mapPrecheckToForm } = load('../src/precheck-import.ts');
const { goodForm, checks, estimate, money } = load('../src/model.ts');
const purchased = {
  purchase_stage: 'purchased', birth_date: '2000-02-29', residency: 'hsinchu_county',
  tool_name: 'Synthetic AI', billing_type: 'annual', billing_component: 'subscription',
  purchase_channel: 'official', purchase_date: '2026-09-01', subscription_end: '2027-09-01',
  payer: 'self', payment_method: 'credit_card', application_type: 'standard',
  eligible_cost_twd: '680.1000', foreign_currency_only: false, multiple_tools: false, transactions: [],
};
const map = changes => mapPrecheckToForm({ ...purchased, ...changes }, goodForm);
const finding = (form, id) => checks({ ...goodForm, ...form }, []).find(item => item.id === id);

test('candidate mapping includes conflicts and never mutates current fields or source', () => {
  const source = Object.freeze({ ...purchased });
  const current = Object.freeze({ ...goodForm, amount: '999', city: '新竹市', special: true });
  const { updates } = mapPrecheckToForm(source, current);
  assert.deepEqual(updates, {
    birth: '2000-02-29', city: '新竹縣', tool: 'Synthetic AI', special: false,
    purchaseDate: '2026-09-01', periodEnd: '2027-09-01', plan: 'annual',
    channel: 'official', paymentMethod: 'card', payer: 'self', amount: '680.1',
  });
  assert.equal(current.amount, '999');
  assert.equal(current.city, '新竹市');
  assert.equal(current.special, true);
  assert.equal(source.eligible_cost_twd, '680.1000');
});

test('planning never imports stale purchase, payment or amount answers', () => {
  const { updates, unresolved } = map({ purchase_stage: 'planning' });
  assert.deepEqual(updates, { birth: '2000-02-29', city: '新竹縣', tool: 'Synthetic AI', special: false });
  assert.match(unresolved.join(' '), /尚未購買/);
});

test('dates require a real complete calendar date and do not normalize overflow', () => {
  for (const invalid of ['', '2001-02-29', '1900-02-29', '2000-13-01', '2000-04-31', '0000-01-01',
    '20000229', '2000-2-29', '2000-02-29T00:00:00Z', 20000229]) {
    const { updates, unresolved } = map({ birth_date: invalid, purchase_date: invalid, subscription_end: invalid });
    assert.equal(updates.birth, undefined, String(invalid));
    assert.equal(updates.purchaseDate, undefined, String(invalid));
    assert.equal(updates.periodEnd, undefined, String(invalid));
    assert.ok(unresolved.length >= 3);
  }
  assert.equal(map({ birth_date: '2000-02-29' }).updates.birth, '2000-02-29');
});

test('unknown residency and tool IDs never become invented city or tool names', () => {
  const result = map({ residency: 'unsure', tool_id: 'chatgpt', tool_name: '' });
  assert.equal(result.updates.city, undefined);
  assert.equal(result.updates.tool, undefined);
  assert.match(result.unresolved.join(' '), /戶籍.*工具/);
  assert.equal(map({ residency: 'other' }).updates.city, '其他');
  for (const tool_name of [123, ['Synthetic AI'], ' ', 'Name\u0000']) assert.equal(map({ tool_name }).updates.tool, undefined);
});

test('monthly/annual/credits are literal plan values; mixed and unknown do not coerce', () => {
  for (const billing_type of ['monthly', 'annual', 'credits']) assert.equal(map({ billing_type }).updates.plan, billing_type);
  for (const billing_type of ['mixed', 'other', 'unsure', ['monthly'], 1]) assert.equal(map({ billing_type }).updates.plan, undefined);
  assert.equal(map({ billing_component: 'mixed' }).updates.plan, undefined);
});

test('channels preserve uncertainty and payment methods require known options', () => {
  for (const purchase_channel of ['agent', 'marketplace']) assert.equal(map({ purchase_channel }).updates.channel, 'reseller');
  for (const purchase_channel of ['app_store', 'other', 'unsure']) {
    const result = map({ purchase_channel });
    assert.equal(result.updates.channel, 'unknown');
    assert.match(result.unresolved.join(' '), /購買管道需人工確認/);
  }
  assert.equal(map({ payment_method: 'other' }).updates.paymentMethod, 'other');
  assert.equal(map({ payment_method: 'unsure' }).updates.paymentMethod, undefined);
});

test('only the named family/guardian relations map to relative, with a warning', () => {
  for (const payer_relationship of ['parent', 'spouse', 'legal_guardian']) {
    const result = map({ payer: 'other', payer_relationship });
    assert.equal(result.updates.payer, 'relative');
    assert.match(result.unresolved.join(' '), /共同切結書.*承辦確認/);
  }
  for (const payer_relationship of ['friend', 'other', 'unsure', '', ['parent']]) {
    assert.equal(map({ payer: 'other', payer_relationship }).updates.payer, undefined);
  }
});

test('money candidates retain exact value and remove only insignificant trailing zeros', () => {
  for (const [eligible_cost_twd, expected] of [['0', '0'], ['680.00', '680'], ['1.0100', '1.01'],
    ['0.100000', '0.1'], ['999999999.9900', '999999999.99']]) {
    assert.equal(map({ eligible_cost_twd }).updates.amount, expected);
  }
  for (const eligible_cost_twd of [null, undefined, '', 680, 'NaN', 'Infinity', '1e2', '-1', '01.00',
    '1000000000', '0.001', '680.999900', '0.000000000000001']) {
    const result = map({ eligible_cost_twd });
    assert.equal(result.updates.amount, undefined, String(eligible_cost_twd));
    assert.match(result.unresolved.join(' '), /未自行補零或四捨五入/);
  }
});

test('ambiguous expenses and transaction rows are never flattened into a formal amount', () => {
  for (const change of [{ foreign_currency_only: true }, { multiple_tools: true }, { billing_component: 'mixed' },
    { billing_component: 'unsure' }, { foreign_currency_only: 'true' }, { multiple_tools: null },
    { transactions: [{ purchase_date: '2026-08-01', eligible_cost_twd: '99' }] }]) {
    assert.equal(map(change).updates.amount, undefined, JSON.stringify(change));
  }
  const result = map({ transactions: [{ purchase_date: '2026-08-01', eligible_cost_twd: '99' }] });
  assert.equal(result.updates.purchaseDate, undefined);
  assert.equal(result.updates.periodEnd, undefined);
  assert.match(result.unresolved.join(' '), /逐筆核對/);
});

test('application category is a candidate, never proof or a financial decision', () => {
  for (const application_type of ['specific', 'language']) {
    const result = map({ application_type });
    assert.equal(result.updates.special, true);
    assert.match(result.unresolved.join(' '), /須另附證明/);
  }
  assert.equal(map({ application_type: 'other' }).updates.special, undefined);
  const { updates } = map({ name: 'Injected', email: 'injected@example.test', bankName: 'Injected',
    requested: '612', receiptAmount: '680', prepared_documents: ['bank'], address: 'Injected',
    identityHint: 'Injected', files: ['file'], eligibility: true });
  for (const key of ['name', 'email', 'bankName', 'requested', 'receiptAmount', 'prepared_documents',
    'address', 'identityHint', 'files', 'eligibility']) assert.equal(Object.hasOwn(updates, key), false, key);
});

test('blank or partial name/email pairs cannot make identity matching pass', () => {
  for (const form of [
    { name: '', receiptName: '', email: '', receiptEmail: '' },
    { name: ' ', receiptName: ' ', email: 'a@example.test', receiptEmail: '' },
    { name: 'Alice', receiptName: 'Bob', email: '', receiptEmail: '' },
    { name: '', receiptName: '', email: 'a@example.test', receiptEmail: 'b@example.test' },
  ]) assert.equal(finding(form, 'identity').severity, 'warning');
  assert.equal(finding({ name: ' Alice ', receiptName: 'Alice', email: '', receiptEmail: '' }, 'identity').severity, 'pass');
  assert.equal(finding({ name: '', receiptName: '', email: 'A@example.test', receiptEmail: 'a@example.test' }, 'identity').severity, 'pass');
});

test('bank name requires both names: missing warns, unequal names do not match', () => {
  for (const form of [{ name: '', bankName: '' }, { name: ' ', bankName: ' ' },
    { name: '', bankName: 'Alice' }, { name: 'Alice', bankName: '' }]) assert.equal(finding(form, 'bank').severity, 'warning');
  assert.equal(finding({ name: 'Alice', bankName: 'Alice' }, 'bank').severity, 'pass');
  assert.equal(finding({ name: 'Alice', bankName: 'Bob' }, 'bank').severity, 'error');
});

test('conditional estimates preserve fractional cents and unknown never becomes zero', () => {
  assert.equal(estimate({ ...goodForm, amount: '0.01', special: false }), '0.005');
  assert.equal(estimate({ ...goodForm, amount: '0.01', special: true }), '0.009');
  assert.equal(estimate({ ...goodForm, amount: '12.35', special: false }), '6.175');
  assert.equal(estimate({ ...goodForm, amount: '999999999.99', special: false }), '3000');
  assert.equal(estimate({ ...goodForm, amount: '999999999.99', special: true }), '6000');
  assert.equal(estimate({ ...goodForm, amount: '0' }), '0');
  for (const amount of ['', ' ', 'NaN', '1e2', '0.001', '-1']) assert.equal(estimate({ ...goodForm, amount }), null);
  assert.equal(money(null), '待確認');
  assert.equal(money(''), '待確認');
  assert.equal(money('1234.005'), '1,234.005');
  assert.equal(finding({ amount: '12.35', requested: '6.17', receiptAmount: '12.35', special: false }, 'amount').severity, 'warning');
  assert.equal(finding({ amount: '12.35', requested: '6.18', receiptAmount: '12.35', special: false }, 'amount').severity, 'warning');
});
