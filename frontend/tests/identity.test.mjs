// Synthetic strings only; a valid checksum does not prove issuance or identity.
import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { buildSync } from 'esbuild';

const compiled = buildSync({
  entryPoints: [fileURLToPath(new URL('../src/identity.ts', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs', target: 'node20',
}).outputFiles[0].text;
const loaded = { exports: {} };
new Function('module', 'exports', compiled)(loaded, loaded.exports);
const { validateTaiwanId, normalizeTaiwanId, parseBirthDate, parseFrontOCR,
  parseBackOCR, isHsinchuCityAddress, checkIdentity } = loaded.exports;
const today = new Date(2026, 8, 19, 12);

test('官方算法已知校驗向量', () => {
  assert.equal(validateTaiwanId('A123456789').valid, true);
  assert.equal(validateTaiwanId('O100000004').valid, true);
  assert.equal(validateTaiwanId('A123456788').code, 'checksum_mismatch');
});
test('全形、小寫與空白標準化，不修改易混字', () => {
  assert.equal(normalizeTaiwanId(' ａ１２３ ４５６７８９ '), 'A123456789');
  assert.equal(validateTaiwanId(' ａ１２３ ４５６７８９ ').valid, true);
  assert.equal(normalizeTaiwanId('A12345678O'), 'A12345678O');
  assert.equal(validateTaiwanId('A12345678O').valid, false);
  assert.equal(validateTaiwanId('A12345678I').valid, false);
});
test('外來人口證號與不完整字號不誤稱假證', () => {
  assert.equal(validateTaiwanId('A800000014').code, 'unsupported');
  assert.equal(validateTaiwanId('AB12345678').code, 'unsupported');
  assert.match(validateTaiwanId('AB12345678').message, /不代表證件無效/);
  assert.equal(validateTaiwanId('A12345678').valid, false);
  assert.equal(validateTaiwanId('A323456789').valid, false);
});
test('特殊字母映射與末碼 4 都可校驗', () => {
  // Independently calculated vectors for I34, O35, W32, X30, Y31, Z33.
  const expected = ['I100000003', 'O100000004', 'W100000001', 'X100000009', 'Y100000000', 'Z100000002'];
  for (const value of expected) assert.equal(validateTaiwanId(value).valid, true, value[0]);
});
test('民國整數年轉西元與前導零', () => {
  assert.equal(parseBirthDate('民國90年5月16日', today).iso, '2001-05-16');
  assert.equal(parseBirthDate('090/05/16', today).iso, '2001-05-16');
  assert.equal(parseBirthDate('90/5/16', today).assumedROC, true);
  assert.equal(parseBirthDate('民國１年１月１日', today).iso, '1912-01-01');
});
test('尊重明示紀元且不接受民國 0 年', () => {
  assert.equal(parseBirthDate('西元2001年5月16日', today).iso, '2001-05-16');
  assert.equal(parseBirthDate('2001-05-16', today).iso, '2001-05-16');
  assert.equal(parseBirthDate('民國0年5月16日', today).valid, false);
  assert.equal(parseBirthDate('民國前1年5月16日', today).valid, false);
  assert.equal(parseBirthDate('西元90年5月16日', today).valid, false);
});
test('閏年、月日溢位與未來生日', () => {
  assert.equal(parseBirthDate('089/02/29', today).iso, '2000-02-29');
  assert.equal(parseBirthDate('090/02/29', today).valid, false);
  assert.equal(parseBirthDate('1900-02-29', today).valid, false);
  for (const input of ['2001-02-30', '2001-04-31', '2001-13-01', '2001-00-01', '2001-01-00', '2026-09-20', '2001-05']) {
    assert.equal(parseBirthDate(input, today).valid, false, input);
  }
  assert.equal(parseBirthDate('2026-09-19', today).valid, true);
});
test('以標籤擷取正面，發證日期不取代生日', () => {
  const result = parseFrontOCR('練習資料・非正式證件\n姓 名：林 小 竹\n身分證字號：Ａ１２３４５６７８９\n出生日期：民國90年05月16日\n發證日期：民國110年01月02日', today);
  assert.deepEqual(result.fields, { name: '林小竹', idNumber: 'A123456789', birth: '2001-05-16' });
  assert.equal(parseFrontOCR('發證日期：民國110年01月02日', today).fields.birth, '');
});
test('標籤下一行與拉丁姓名空格保留', () => {
  const result = parseFrontOCR('姓名：\nJohn Smith\n統一編號：\nA123456789\n出生年月日：\n090/05/16', today);
  assert.deepEqual(result.fields, { name: 'John Smith', idNumber: 'A123456789', birth: '2001-05-16' });
});
test('父母配偶姓名不誤當本人', () => {
  assert.equal(parseFrontOCR('父親姓名：陳大竹\n母親姓名：王小竹\n配偶姓名：張小竹', today).fields.name, '');
  assert.equal(parseFrontOCR('姓名：林小竹\n父親：林大竹\n配偶：張小竹', today).fields.name, '林小竹');
});
test('多個不同候選需人工，不任選第一個', () => {
  const result = parseFrontOCR('姓名：林小竹\n姓名：陳小竹\n字號 A123456789\n字號 O100000004\n出生日期：090/05/16\n出生日期：090/05/17', today);
  assert.deepEqual(result.fields, { name: '', idNumber: '', birth: '' });
  assert.match(result.notes.join(' '), /多個不同候選/);
  assert.equal(parseFrontOCR('出生日期：090/02/29\n出生日期：090/05/16', today).fields.birth, '');
  assert.equal(parseFrontOCR('出生日期：090/05/16 出生日期：090/05/17', today).fields.birth, '');
  assert.equal(parseFrontOCR('姓名：林小竹姓名：陳小竹', today).fields.name, '');
  assert.equal(parseFrontOCR('身分證字號：A123456789 身分證字號：O100000004', today).fields.idNumber, '');
});
test('錯字字號保留供人更正，不猜成有效資料', () => {
  assert.equal(parseFrontOCR('身分證字號：A12345678O', today).fields.idNumber, 'A12345678O');
  assert.equal(validateTaiwanId(parseFrontOCR('身分證字號：A12345678O', today).fields.idNumber).valid, false);
});
test('出生欄不截取過長日期，也不任取雙日期第一個', () => {
  for (const source of ['出生日期：2001-05-160', '出生日期：民國90年05月16日／民國91年05月16日', '出生日期：2001-05-16/2002-05-16']) {
    const result = parseFrontOCR(source, today);
    assert.equal(result.fields.birth, '', source);
    assert.match(result.notes.join(' '), /原圖|填寫/);
  }
  assert.equal(parseFrontOCR('出生日期：2001-05-16 發證日期：2020-05-16', today).fields.birth, '2001-05-16');
});
test('第二個遮罩或不完整字號不會被忽略', () => {
  for (const candidate of ['A12****789', 'A12••••789', 'A123', '']) {
    const result = parseFrontOCR(`身分證字號：A123456789\n身分證字號：${candidate}`, today);
    assert.equal(result.fields.idNumber, '', candidate);
    assert.match(result.notes.join(' '), /手動填寫/);
  }
  assert.equal(parseFrontOCR('身分證字號：A12****789\n其他參考 A123456789', today).fields.idNumber, '');
});
test('反面多行地址、不包含配偶父母或說明文字', () => {
  const result = parseBackOCR('戶籍地址：新竹市東區\n練習路100號\n配偶：陳小竹');
  assert.equal(result.fields.address, '新竹市東區練習路100號');
  assert.equal(parseBackOCR('住址：新竹市北區\n國父路100號\n父：林大竹').fields.address, '新竹市北區國父路100號');
  assert.equal(parseBackOCR('住址：新竹市東區\n光復路一段100號\n配偶：陳小竹').fields.address, '新竹市東區光復路一段100號');
  assert.equal(parseBackOCR('戶籍地址：\n新竹市北區光華街100號\n父：陳大竹').fields.address, '新竹市北區光華街100號');
});
test('反面地址多候選不自行選擇', () => {
  assert.equal(parseBackOCR('住址：新竹市東區光復路1號\n住址：新竹縣竹北市中華路1號').fields.address, '');
});
test('同一行兩個地址標籤不合併成新竹市候選', () => {
  for (const source of [
    '戶籍地址：新竹市東區甲路1號 戶籍地址：新竹縣竹北市乙路2號',
    '住址：新竹市東區甲路1號 地址：新竹縣竹北市乙路2號',
    '戶籍地址：新竹市東區甲路1號 戶籍地址：新竹縣竹北市乙路2號\n戶籍地址：新竹市東區丙路3號',
  ]) {
    const result = parseBackOCR(source);
    assert.equal(result.fields.address, '');
    assert.match(result.notes.join(' '), /多個地址標籤.*手動填寫/);
    assert.equal(isHsinchuCityAddress(result.fields.address), false);
  }
});
test('新竹市比對以地址開頭，與字號首字母無關', () => {
  for (const address of ['新竹市東區光復路1號', '300新竹市北區', '３０００１２臺灣省新竹市東區', '中華民國台灣省新竹市香山區']) assert.equal(isHsinchuCityAddress(address), true, address);
  for (const address of ['新竹縣竹北市', '臺北市中山區備註新竹市', '原住新竹市現住新竹縣', '']) assert.equal(isHsinchuCityAddress(address), false, address);
});
test('兩面存在與完成辨識列為確認門檻', () => {
  const fields = { name: '林小竹', idNumber: 'A123456789', birth: '2001-05-16', address: '新竹市東區光復路1號' };
  const checks = checkIdentity(fields, false, false);
  assert.deepEqual(checks.filter(c => c.level === 'error').map(c => c.field), ['front', 'back']);
  assert.equal(checkIdentity(fields, true, true).some(c => c.level === 'error'), false);
  assert.match(checkIdentity(fields, true, true).find(c => c.field === 'idNumber').message, /尚未查證/);
});
test('資料不同與非新竹市提示，不捏造資格通過', () => {
  const fields = { name: '林小竹', idNumber: 'O100000004', birth: '2001-05-16', address: '新竹縣竹北市中華路1號' };
  const checks = checkIdentity(fields, true, true, { name: '陳小竹', birth: '民國90年5月16日', address: '新竹市東區', idNumber: 'A•••••••••' });
  assert.equal(checks.some(c => c.level === 'error'), false);
  assert.equal(checks.filter(c => c.level === 'warning').length, 3);
  assert.equal(checks.some(c => c.field === 'birth' && c.level === 'warning'), false);
  assert.equal(checks.some(c => c.field === 'idNumber' && c.level === 'warning'), false);
});
