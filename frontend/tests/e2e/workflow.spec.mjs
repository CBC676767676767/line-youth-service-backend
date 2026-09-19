import { test, expect } from '@playwright/test';
import { freshCodes, isolatedFixture, localContext, syntheticDocument } from './fixture.mjs';

function responseFor(page, method, ending) {
  return page.waitForResponse((response) => response.request().method() === method && new URL(response.url()).pathname.endsWith(ending));
}

async function fillFormalForm(page, fixture) {
  const fields = {
    姓名: 'name', 電子信箱: 'email', 出生日期: 'birth', 聯絡電話: 'phone', 戶籍地址: 'address',
    '訂閱 AI 工具': 'tool', 工具提供公司: 'company', 購買日期: 'purchaseDate', 訂閱期間結束日: 'periodEnd',
    臺幣實付金額: 'amount', 申請補助金額: 'requested', 收據姓名: 'receiptName', 收據電子信箱: 'receiptEmail',
    收據對應臺幣金額: 'receiptAmount', 存摺戶名: 'bankName', 原幣金額: 'originalAmount',
  };
  for (const [label, field] of Object.entries(fields)) await page.getByLabel(label, { exact: true }).fill(String(fixture.form[field]));
  for (const [label, field] of Object.entries({ 戶籍縣市: 'city', 購買管道: 'channel', 訂閱方式: 'plan', 付款方式: 'paymentMethod', 付款人: 'payer', 原始幣別: 'currency' })) {
    await page.getByRole('combobox', { name: new RegExp('^' + label) }).selectOption(fixture.form[field]);
  }
}

async function loginPrecheck(page, fixture) {
  try {
    await page.getByLabel('電子信箱', { exact: true }).fill(fixture.applicant.email);
    await page.getByRole('button', { name: '取得一次性驗證碼', exact: true }).click();
    await expect(page.getByLabel('信箱中的 6 位驗證碼')).toBeVisible();
    const code = freshCodes(fixture).applicant_otp;
    if (!code) throw new Error('Synthetic applicant OTP was not available in private spool.');
    await page.getByLabel('信箱中的 6 位驗證碼').fill(code);
    await page.getByRole('button', { name: '驗證並登入', exact: true }).click();
    await expect(page.getByLabel('選擇草稿', { exact: true })).toBeVisible();
  } catch {
    await clearSecretInputs(page);
    throw new Error('Synthetic email OTP login or draft loading failed; secret-bearing locator details suppressed.');
  }
}

async function loginSupervisor(page, fixture) {
  try {
    await page.goto('/admin/');
    await page.getByLabel('工作帳號信箱', { exact: true }).fill(fixture.staff.supervisor.email);
    await page.getByLabel('密碼', { exact: true }).fill(fixture.staff.supervisor.password);
    await page.getByRole('button', { name: '繼續第二步驗證', exact: true }).click();
    await expect(page.getByLabel('六碼驗證碼')).toBeVisible();
    await page.getByLabel('六碼驗證碼').fill(freshCodes(fixture).supervisor_totp);
    await page.getByRole('button', { name: '驗證並登入', exact: true }).click();
    await expect(page.getByRole('heading', { name: '案件工作台', exact: true })).toBeVisible();
  } catch {
    await clearSecretInputs(page);
    throw new Error('Synthetic staff password/MFA login failed; secret-bearing locator details suppressed.');
  }
}

async function clearSecretInputs(page) {
  await page.locator('input[type=password], input[autocomplete=one-time-code], #login-code, #mfa-code').evaluateAll((inputs) => {
    for (const input of inputs) input.value = '';
  }).catch(() => {});
}

test('real browser: precheck snapshot, grant draft, idempotent submission, staff MFA and supplement', async ({ browser }) => {
  const fixture = await isolatedFixture();
  const youth = await localContext(browser, fixture);
  const staff = await localContext(browser, fixture);
  const page = await youth.newPage();
  const staffPage = await staff.newPage();
  const pageErrors = [];
  page.on('pageerror', () => pageErrors.push('citizen-runtime-error'));
  staffPage.on('pageerror', () => pageErrors.push('staff-runtime-error'));
  let caseId; let caseNo;
  try {
    await test.step('anonymous precheck and real email OTP save a server-computed snapshot', async () => {
      await page.goto('/precheck');
      await page.getByRole('button', { name: '填入一組合成示範資料', exact: true }).click();
      await page.getByRole('button', { name: /下一步：條件自查/ }).click();
      await page.getByRole('button', { name: /執行自填預檢/ }).click();
      await expect(page.getByRole('heading', { name: '你的預檢摘要', exact: true })).toBeVisible();
      await expect(page.locator('#results')).toContainText('尚未核對文件');
      await page.getByRole('button', { name: '登入後保存至本人草稿', exact: true }).click();
      await loginPrecheck(page, fixture);
      await page.getByLabel('選擇草稿', { exact: true }).selectOption('new');
      await page.getByLabel('新草稿所屬方案', { exact: true }).selectOption(fixture.scheme_id);
      const saved = responseFor(page, 'POST', '/precheck');
      await page.getByRole('button', { name: '重新計算並保存快照', exact: true }).click();
      const response = await saved;
      expect(response.status()).toBe(201);
      const snapshot = (await response.json()).data;
      caseId = snapshot.case_id;
      caseNo = (await (await youth.request.get(`/api/v1/cases/${caseId}`)).json()).data.case_no;
      expect(snapshot.snapshot.result.formal_submission).toBe(false);
      await expect(page.getByRole('heading', { name: '已保存的預檢摘要', exact: true })).toBeVisible();
    });

    await test.step('React citizen case page reads the same snapshot and opens its own draft', async () => {
      await page.goto('/cases/' + encodeURIComponent(caseId));
      await expect(page.getByRole('heading', { name: '預檢摘要', exact: true })).toBeVisible();
      await expect(page.getByText('public_2026_09_19', { exact: false }).first()).toBeVisible();
      await page.getByRole('button', { name: /繼續填寫/ }).click();
      await expect(page.getByRole('heading', { name: '申請人與訂閱資料', exact: true })).toBeVisible();
      await expect(page.getByLabel('姓名', { exact: true })).toHaveValue('');
      const backgroundWrites = [];
      const observeWrites = (request) => { if (request.method() === 'PATCH' && new URL(request.url()).pathname.endsWith('/cases/' + caseId)) backgroundWrites.push(true); };
      page.on('request', observeWrites);
      await page.getByLabel('臺幣實付金額', { exact: true }).fill('999');
      await page.getByRole('button', { name: '檢視已保存預檢可帶入欄位', exact: true }).click();
      await expect(page.getByRole('checkbox', { name: '帶入臺幣實付金額', exact: true })).not.toBeChecked();
      await expect(page.getByLabel('臺幣實付金額', { exact: true })).toHaveValue('999');
      for (const item of await page.locator('.precheck-import-preview input[type=checkbox]').all()) await item.uncheck();
      await page.getByRole('checkbox', { name: '帶入出生日期', exact: true }).check();
      await page.getByRole('button', { name: '帶入已勾選欄位', exact: true }).click();
      await expect(page.getByLabel('出生日期', { exact: true })).toHaveValue('2000-01-01');
      await expect(page.getByLabel('臺幣實付金額', { exact: true })).toHaveValue('999');
      await expect(page.getByLabel('申請補助金額', { exact: true })).toHaveValue('');
      await expect(page.getByLabel('姓名', { exact: true })).toHaveValue('');
      expect(backgroundWrites).toHaveLength(0);
      page.off('request', observeWrites);
      await fillFormalForm(page, fixture);
      await page.getByRole('navigation', { name: '民眾服務', exact: true }).getByRole('button', { name: '服務首頁', exact: true }).click();
      await expect(page.getByRole('alertdialog')).toBeVisible();
      await page.getByRole('button', { name: '留在本頁', exact: true }).click();
      await expect(page.getByLabel('姓名', { exact: true })).toHaveValue(fixture.form.name);
      const beforeUnload = page.waitForEvent('dialog', { timeout: 5000 });
      const cancelledReload = page.reload({ timeout: 5000 }).catch(() => null);
      const dialog = await beforeUnload;
      expect(dialog.type()).toBe('beforeunload');
      await dialog.dismiss(); await cancelledReload;
      await expect(page.getByLabel('臺幣實付金額', { exact: true })).toHaveValue(fixture.form.amount);
      const saved = responseFor(page, 'PATCH', '/cases/' + caseId);
      await page.getByRole('button', { name: '儲存草稿', exact: true }).click();
      expect((await saved).status()).toBe(200);
      await expect(page.getByText('草稿已儲存，可以稍後登入繼續。', { exact: true })).toBeVisible();
      await page.reload();
      await page.getByRole('button', { name: /繼續填寫/ }).click();
      await expect(page.getByLabel('姓名', { exact: true })).toHaveValue(fixture.form.name);
      await expect(page.getByLabel('臺幣實付金額', { exact: true })).toHaveValue(fixture.form.amount);
    });

    await test.step('six real synthetic PNG uploads persist and lost-response submission retries once', async () => {
      await page.getByRole('button', { name: /證明文件/ }).click();
      const documents = { ID_FRONT: '身分證正面', ID_BACK: '身分證反面', RECEIPT: '官方訂閱收據／憑證', PAYMENT_PROOF: '臺幣帳單與繳款證明', BANK_ACCOUNT: '本人存摺封面', AFFIDAVIT: '親筆簽名切結書' };
      for (const [kind, title] of Object.entries(documents)) {
        const completed = responseFor(page, 'POST', '/complete');
        await page.getByLabel('上傳' + title, { exact: true }).setInputFiles(syntheticDocument(fixture, kind));
        expect((await completed).status()).toBe(200);
        await expect(page.getByLabel('上傳' + title, { exact: true })).toBeEnabled();
      }
      await page.getByRole('button', { name: /確認送件/, exact: false }).click();
      await page.getByRole('checkbox', { name: /我已核對原始文件與申請內容/ }).check();
      let firstKey; let attempts = 0;
      const submitPath = `**/api/v1/cases/${caseId}/submit`;
      await page.route(submitPath, async (route) => {
        attempts += 1;
        const key = route.request().headers()['idempotency-key'];
        if (attempts === 1) {
          firstKey = key;
          const response = await route.fetch();
          if (response.status() !== 201) throw new Error('Initial synthetic submission did not succeed.');
          await route.abort('failed');
        } else {
          if (key !== firstKey) throw new Error('UI retry changed its idempotency key.');
          await route.continue();
        }
      });
      await page.getByRole('button', { name: '確認送出申請', exact: false }).click();
      await expect(page.getByRole('button', { name: '確認並重試原送件', exact: false })).toBeVisible();
      await page.getByRole('button', { name: '確認並重試原送件', exact: false }).click();
      await expect(page.getByRole('heading', { name: '收件回執', exact: true })).toBeVisible();
      expect(attempts).toBe(2);
      await page.unroute(submitPath);
      const receiptResponse = await youth.request.get(`/api/v1/cases/${caseId}/receipts`);
      expect((await receiptResponse.json()).data.items).toHaveLength(1);
    });

    await test.step('supervisor MFA starts review and creates a supplement task via UI', async () => {
      await loginSupervisor(staffPage, fixture);
      await staffPage.locator('.ad-case-row').filter({ hasText: caseNo }).click();
      await staffPage.getByRole('button', { name: '開始審查', exact: true }).click();
      await expect(staffPage.getByText('已開始審查。', { exact: true })).toBeVisible();
      await staffPage.getByRole('button', { name: '補件處理', exact: true }).click();
      await staffPage.locator('summary').filter({ hasText: '建立補件要求' }).click();
      await staffPage.getByLabel('補件標題', { exact: true }).fill('瀏覽器合成付款補件');
      await staffPage.getByLabel('需要補正的內容', { exact: true }).fill('請補新的合成付款圖片，只供本機流程測試。');
      await staffPage.getByLabel('驗收標準', { exact: true }).fill('核對測試圖片版本與合成說明，不代表真實資格核定。');
      const created = responseFor(staffPage, 'POST', '/tasks');
      await staffPage.getByRole('button', { name: '建立補件要求', exact: true }).click();
      expect((await created).status()).toBe(201);
      await expect(staffPage.getByRole('heading', { name: '瀏覽器合成付款補件', exact: true })).toBeVisible();
    });

    await test.step('applicant uploads a revised payment document and supervisor accepts it after development scan', async () => {
      await page.getByRole('button', { name: '更新進度', exact: true }).click();
      const task = page.locator('article.task-card').filter({ hasText: '瀏覽器合成付款補件' });
      await task.getByLabel('補充說明', { exact: true }).fill('已更新合成付款圖片，僅驗證補件流程。');
      await task.getByRole('combobox', { name: /^補件文件類別/ }).selectOption('PAYMENT_PROOF');
      const complete = responseFor(page, 'POST', '/complete');
      await task.getByLabel('上傳補件文件：瀏覽器合成付款補件', { exact: true }).setInputFiles(syntheticDocument(fixture, 'PAYMENT_PROOF_REVISED'));
      expect((await complete).status()).toBe(200);
      const submitted = responseFor(page, 'POST', '/submissions');
      await task.getByRole('button', { name: '確認送出補件', exact: true }).click();
      expect((await submitted).status()).toBe(201);
      await expect.poll(async () => {
        const response = await staff.request.get(`/api/v1/staff/cases/${caseId}`);
        const files = (await response.json()).data.files.filter((file) => file.task_id);
        return files.length > 0 && files.every((file) => file.scan_status === 'CLEAN');
      }).toBe(true);
      await staffPage.getByRole('button', { name: '更新案件', exact: true }).click();
      const staffTask = staffPage.locator('section.ad-card').filter({ has: staffPage.getByRole('heading', { name: '瀏覽器合成付款補件', exact: true }) });
      await expect(staffTask.getByRole('button', { name: '接受補件', exact: true })).toBeEnabled();
      await staffTask.getByRole('button', { name: '接受補件', exact: true }).click();
      await staffTask.getByLabel('驗收紀錄', { exact: true }).fill('合成測試：已對照新的付款圖片與說明。');
      const accepted = responseFor(staffPage, 'POST', '/accept');
      await staffTask.getByRole('button', { name: '確認接受補件', exact: true }).click();
      expect((await accepted).status()).toBe(200);
      await expect(staffPage.getByText('已接受這次補件。', { exact: true })).toBeVisible();
      await page.getByRole('button', { name: '更新進度', exact: true }).click();
      const detail = (await (await youth.request.get(`/api/v1/cases/${caseId}`)).json()).data;
      expect(detail.tasks[0].status).toBe('ACCEPTED');
      expect(detail.decision).toBeNull();
    });

    await test.step('saving one staff review keeps a sibling edit, and explicit synthetic decision remains separate from payment', async () => {
      await staffPage.getByRole('button', { name: '人工檢核', exact: true }).click();
      const cards = staffPage.locator('form.ad-card').filter({ has: staffPage.getByRole('combobox', { name: /^人工檢核結果/ }) });
      await expect(cards).toHaveCount(5);
      async function prepareCard(index) {
        const card = cards.nth(index);
        await card.getByRole('combobox', { name: /^人工檢核結果/ }).selectOption('PASS');
        await card.getByRole('textbox', { name: /^內部審查紀錄/ }).fill('隔離合成測試檢核 ' + (index + 1) + '，不代表真實資格核定。');
        await card.getByRole('button', { name: '新增依據', exact: true }).click();
        await card.getByRole('combobox', { name: /^依據來源/ }).selectOption({ index: 1 });
      }
      async function saveCard(index) {
        const response = staffPage.waitForResponse((item) => item.request().method() === 'PATCH' && /\/review-items\//.test(new URL(item.url()).pathname));
        await cards.nth(index).getByRole('button', { name: '儲存檢核', exact: true }).click();
        expect((await response).status()).toBe(200);
        await expect(cards.nth(index).getByText('已存：符合', { exact: true })).toBeVisible();
      }
      await prepareCard(0); await prepareCard(1);
      const siblingEvidence = await cards.nth(1).getByRole('combobox', { name: /^依據來源/ }).inputValue();
      await saveCard(0);
      await expect(cards.nth(1).getByRole('combobox', { name: /^人工檢核結果/ })).toHaveValue('PASS');
      await expect(cards.nth(1).getByRole('textbox', { name: /^內部審查紀錄/ })).toHaveValue('隔離合成測試檢核 2，不代表真實資格核定。');
      await expect(cards.nth(1).getByRole('combobox', { name: /^依據來源/ })).toHaveValue(siblingEvidence);
      await saveCard(1);
      for (let index = 2; index < 5; index += 1) { await prepareCard(index); await saveCard(index); }
      await staffPage.getByRole('button', { name: '核定與結案', exact: true }).click();
      await staffPage.getByLabel('核定理由', { exact: true }).fill('隔離合成瀏覽器流程測試，非真實補助核定。');
      await staffPage.getByRole('button', { name: '帶入已儲存的檢核依據', exact: true }).click();
      await staffPage.getByRole('checkbox', { name: '我已核對檢核結果、補件與證據，確認作成此決定。', exact: true }).check();
      const decided = responseFor(staffPage, 'POST', '/decisions');
      await staffPage.getByRole('button', { name: '確認送出主管決定', exact: true }).click();
      expect((await decided).status()).toBe(201);
      await page.getByRole('button', { name: '更新進度', exact: true }).click();
      await expect(page.getByRole('heading', { name: '核定通過', exact: true })).toBeVisible();
      await expect(page.getByText('核定及結案紀錄不代表已完成撥款；實際匯款請依機關出納通知。', { exact: true })).toBeVisible();
    });
    expect(pageErrors).toEqual([]);
  } finally {
    await youth.close(); await staff.close();
  }
});

test('anonymous mobile precheck keeps purchase data optional and has no horizontal overflow', async ({ browser }) => {
  const fixture = await isolatedFixture();
  const context = await localContext(browser, fixture, { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const page = await context.newPage();
  try {
    await page.goto('/precheck');
    await page.getByRole('button', { name: /下一步：工具與交易/ }).click();
    await expect(page.locator('#purchase-dates')).toBeHidden();
    await page.getByLabel('工具名稱或別名').fill('尚未收錄的合成工具');
    await page.getByRole('button', { name: /下一步：條件自查/ }).click();
    await expect(page.locator('#payer-fields')).toBeHidden();
    await page.getByRole('button', { name: /執行自填預檢/ }).click();
    await expect(page.getByRole('heading', { name: '你的預檢摘要', exact: true })).toBeVisible();
    await expect(page.locator('#results')).toContainText('尚未核對文件');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    expect(await context.cookies()).toHaveLength(0);
    expect((await context.request.get('/api/v1/me')).status()).toBe(401);
  } finally { await context.close(); }
});
