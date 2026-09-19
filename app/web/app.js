"use strict";

// Personal answers and session tokens stay in memory. No browser storage or analytics.
(() => {
  const $ = (id) => document.getElementById(id);
  const handoffEntry = captureLineHandoff();
  const initialAnswers = () => ({
    purchase_stage: "planning", tool_id: null, tool_name: "", plan_id: null,
    plan_name: "", billing_type: "unsure", purchase_channel: "unsure", purchase_url: "",
    seller: "", purchase_date: "", subscription_start: "", subscription_end: "",
    residency: "unsure", birth_date: "", application_type: "", payer: "unsure",
    payer_relationship: "", prepared_documents: [], eligible_cost_twd: null,
    foreign_currency_only: false, qualification_categories: [], payment_method: "unsure",
    billing_component: "unsure", prior_subsidy: "unsure", multiple_tools: false, transactions: [],
  });
  const state = {
    answers: initialAnswers(), catalog: null, result: null, lastInputs: null,
    step: 1, view: "precheck", dirty: false, me: null, csrf: "", challenge: null,
    mfaChallenge: null, cases: [], schemes: [], selectedCase: null,
    saveAttempt: null, createAttempt: null, returnToSave: false, evaluating: false,
    preferredDraftId: null, useMonthlyTransactions: false, catalogLoading: false,
    lineHandoffToken: handoffEntry.token, lineHandoffLoaded: false, lineHandoffReady: false,
  };
  const fieldLabels = {
    purchase_stage: "購買情境", tool_id: "工具", tool_name: "工具名稱", plan_id: "方案",
    plan_name: "方案完整名稱", billing_type: "計費方式", purchase_channel: "購買通路",
    purchase_url: "購買網址", seller: "賣方", purchase_date: "購買日期",
    subscription_start: "訂閱開始日", subscription_end: "訂閱結束日", residency: "設籍情形",
    birth_date: "出生日期", application_type: "申請類型", payer: "付款人",
    payer_relationship: "代付關係", prepared_documents: "使用者自述已備妥文件",
    eligible_cost_twd: "待核對的臺幣費用", foreign_currency_only: "僅有外幣金額",
    qualification_categories: "適用身分／認證", payment_method: "付款方式",
    billing_component: "費用內容", prior_subsidy: "本年度申請情形", multiple_tools: "多工具情境",
    transactions: "逐月交易明細",
  };
  const valueLabels = {
    planning: "還沒買，先了解", purchased: "已購買，準備申請", monthly: "月訂閱",
    annual: "年訂閱", credits: "另外購買額度／儲值", other: "其他", unsure: "不確定",
    official: "官方網站", marketplace: "集合平台", agent: "代購", app_store: "App 商店",
    hsinchu: "新竹市", self: "本人", standard: "一般申請", student: "學生申請",
    specific: "特定對象", language: "文化語言保存者", credit_card: "信用卡",
    subscription: "一般訂閱費", included_credits: "訂閱內含額度", standalone_credits: "單獨購買額度", mixed: "訂閱與額度混合",
    parent: "父母", spouse: "配偶", legal_guardian: "法定代理人", received: "已受補助",
    applied: "已申請待結果", withdrawn: "已撤回", rejected: "未核准", none: "尚未申請或受補助", draft: "只有草稿，尚未送出",
  };
  const caseStatus = { DRAFT: "草稿", SUBMITTED: "已送出案件", UNDER_REVIEW: "審查中", NEEDS_INFO: "待補件", APPROVED: "已核定", REJECTED: "未核定", CLOSED: "已結案" };
  const stepForField = (field) => field === "purchase_stage" ? 1 : ["residency", "birth_date", "application_type", "payer", "payer_relationship", "qualification_categories", "prior_subsidy"].includes(field) ? 3 : 2;
  const isPublicPolicy = () => Boolean(state.catalog?.snapshot || state.catalog?.mode === "public_advisory");

  function captureLineHandoff() {
    const url = new URL(window.location.href);
    if (!url.searchParams.has("line_handoff")) return { requested: false, token: null };
    const values = url.searchParams.getAll("line_handoff");
    // The opaque bridge key lives only in memory; no query values become form answers.
    window.history.replaceState(null, "", url.pathname + url.hash);
    const token = values.length === 1 && /^[A-Za-z0-9_-]{16,256}$/.test(values[0]) ? values[0] : null;
    return { requested: true, token };
  }

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  }

  function button(text, handler, className = "secondary") {
    const element = node("button", text, className);
    element.type = "button";
    element.addEventListener("click", () => handler());
    return element;
  }

  function status(element, message, isError = false) {
    element.textContent = message || "";
    element.hidden = !message;
    element.classList.toggle("error", isError);
  }

  function safeLink(url, label) {
    try {
      const parsed = new URL(url);
      if (!["https:", "http:"].includes(parsed.protocol) || parsed.username || parsed.password) return null;
      const link = node("a", label);
      link.href = parsed.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      return link;
    } catch { return null; }
  }

  function prettyDate(value) {
    if (!value) return "未提供";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "時間待確認";
    return new Intl.DateTimeFormat("zh-TW", { timeZone: "Asia/Taipei", dateStyle: "medium", timeStyle: "short" }).format(parsed) + "（臺北）";
  }

  function friendlyError(error) {
    if (error.status === 401) return "登入已失效或驗證資料不正確，請重新登入。尚未保存的填答仍保留在本頁。";
    if (error.status === 403) return "目前帳號沒有操作權限，或安全驗證已失效。請重新登入後再試。";
    if (error.status === 404) return "找不到資料，或目前帳號無權查看。";
    if (error.status === 412) return "案件已由其他操作更新。請重新載入案件後再保存；目前填答仍保留。";
    if (error.status === 429) return "操作次數較多，請稍候再試。尚未保存的填答仍保留。";
    if (error.status === 409) return "案件或重複提交狀態已改變，請重新載入案件再操作。";
    if (error.status === 422) return "部分欄位格式或長度不正確，請檢查日期、網址及文字內容後再試。";
    if (error.status === 410) return "驗證碼或驗證流程已過期，請重新取得驗證碼。";
    if (error.status >= 500) return "服務暫時無法完成操作，請稍後重試。這次沒有收到成功確認。";
    return error.publicMessage || "目前無法連線，請確認網路及服務狀態後重試。填答仍保留在目前頁面。";
  }

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (state.csrf && options.method && options.method !== "GET") headers["X-CSRF-Token"] = state.csrf;
    let response;
    try {
      response = await fetch("/api/v1" + path, {
        ...options, headers, credentials: "same-origin", cache: "no-store",
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
    } catch { throw { status: 0 }; }
    if (response.status === 204) return { data: null, etag: response.headers.get("ETag") };
    let envelope;
    try { envelope = await response.json(); } catch { throw { status: response.status || 500 }; }
    if (!response.ok) throw { status: response.status, code: envelope.error?.code };
    return { data: envelope.data, etag: response.headers.get("ETag") };
  }

  function busy(form, value) {
    form.setAttribute("aria-busy", String(value));
    form.querySelectorAll("button").forEach((item) => { item.disabled = value; });
  }

  function currentInput() {
    const input = { ...state.answers, prepared_documents: [...state.answers.prepared_documents], qualification_categories: [...state.answers.qualification_categories], transactions: state.answers.transactions.map((row) => ({ ...row })) };
    if (input.purchase_stage === "planning") {
      input.purchase_date = "";
      input.subscription_start = "";
      input.subscription_end = "";
      input.payer = "unsure";
      input.payer_relationship = "";
      input.eligible_cost_twd = null;
      input.foreign_currency_only = false;
      input.payment_method = "unsure";
      input.transactions = [];
    } else if (input.payer !== "other") input.payer_relationship = "";
    if (!state.useMonthlyTransactions || input.billing_type !== "monthly") input.transactions = [];
    if (!isPublicPolicy()) {
      ["eligible_cost_twd", "foreign_currency_only", "qualification_categories", "payment_method", "billing_component", "prior_subsidy", "multiple_tools", "transactions"].forEach((field) => { delete input[field]; });
    }
    return input;
  }

  function markChanged() {
    state.dirty = true;
    state.saveAttempt = null;
    status($("global-status"), state.result ? "填答已修改，需重新預檢。保存時伺服器會以目前填答重新計算。" : "");
  }

  function setView(view) {
    state.view = view;
    $("precheck-view").hidden = view !== "precheck" || !state.catalog;
    $("account-view").hidden = view !== "account";
    $("cases-view").hidden = view !== "cases";
    $("nav-precheck").classList.toggle("active", view === "precheck");
    $("nav-cases").classList.toggle("active", view === "cases");
    ["nav-precheck", "nav-cases"].forEach((id) => $(id).removeAttribute("aria-current"));
    if (view === "precheck") $("nav-precheck").setAttribute("aria-current", "page");
    if (view === "cases") $("nav-cases").setAttribute("aria-current", "page");
  }

  function showStep(step, focus = true) {
    if (step === 4 && (!state.result || state.dirty)) { evaluate(); return; }
    state.step = step;
    setView("precheck");
    for (let i = 1; i <= 4; i += 1) $("step-" + i).hidden = i !== step;
    document.querySelectorAll("[data-step]").forEach((item) => {
      if (Number(item.dataset.step) === step) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
    conditionalFields();
    if (focus) (step === 4 ? $("result-title") : $("step-title-" + step))?.focus({ preventScroll: true });
  }

  function conditionalFields() {
    const purchased = state.answers.purchase_stage === "purchased";
    $("purchase-dates").hidden = !purchased;
    $("payer-fields").hidden = !purchased;
    $("relationship-field").hidden = state.answers.payer !== "other";
    $("seller-fields").hidden = state.answers.purchase_channel === "unsure";
    const publicPolicy = isPublicPolicy();
    $("public-billing").hidden = !publicPolicy;
    $("public-payment").hidden = !publicPolicy || !purchased;
    $("prior-subsidy-field").hidden = !publicPolicy;
    $("monthly-section").hidden = !publicPolicy || !purchased || state.answers.billing_type !== "monthly";
    $("monthly-editor").hidden = !state.useMonthlyTransactions;
    $("qualification-section").hidden = !publicPolicy || !["specific", "language"].includes(state.answers.application_type);
  }

  function modeText(value) {
    if (value.demo || value.mode === "demo") return "DEMO · 合成規則示範";
    if (value.mode === "public_advisory" || value.snapshot?.usage === "advisory_precheck") return "公開來源預檢 · 未經機關確認系統規格";
    if (value.mode === "draft" || value.rules?.status === "draft") return "規則草案 · 待機關確認";
    if (value.mode === "confirmed" || value.rules?.status === "confirmed") return "申請前自填預檢 · 尚未核對文件";
    return "規則待確認 · 不能作為正式適用判斷";
  }

  async function loadCatalog() {
    if (state.catalogLoading) return false;
    state.catalogLoading = true;
    $("retry-catalog").disabled = true;
    $("catalog-loading").hidden = false;
    $("catalog-error").hidden = true;
    try {
      const { data } = await api("/precheck/catalog");
      state.catalog = data;
      const banner = $("catalog-banner");
      banner.replaceChildren(node("strong", modeText(data)), node("span", data.demo ? "以下工具、方案與條件皆為合成示範，不能用於真實申請判斷。" : "已核對公開來源與機關確認系統規格是不同狀態。僅依網頁文字提示；尚未閱讀全部附件或查證個人資格。"));
      if (data.snapshot) banner.append(node("p", "來源快照 " + data.snapshot.snapshot_version + " · 查核日期 " + data.snapshot.source_checked_date + " · " + (data.snapshot.timezone || "Asia/Taipei"), "snapshot-metadata"));
      banner.hidden = false;
      $("fill-demo").hidden = !(data.tools || []).length;
      renderTools();
      renderPlans();
      const types = $("application-type");
      types.replaceChildren(new Option("請選擇／不確定", ""));
      (data.application_types || []).forEach((type) => types.add(new Option(type.name, type.id)));
      types.value = state.answers.application_type;
      renderQualifications();
      if (data.available === false && !isPublicPolicy()) status($("global-status"), "目前沒有可用的已確認規則。仍可整理填答，結果會保留待確認，不作正式適用判斷。");
      if (state.view === "precheck") showStep(state.step, false);
      return true;
    } catch (error) { $("catalog-error").hidden = false; return false; }
    finally { $("catalog-loading").hidden = true; $("retry-catalog").disabled = false; state.catalogLoading = false; }
  }

  async function bootstrap() {
    if (await loadCatalog()) await loadLineHandoff();
  }

  function handoffNotice(message, isError = false) {
    const notice = $("line-handoff-notice");
    const link = node("a", "返回申請服務"); link.href = "/";
    notice.replaceChildren(node("p", message), link);
    notice.hidden = false;
    notice.classList.toggle("error", isError);
  }

  async function loadLineHandoff() {
    if (!handoffEntry.requested || state.lineHandoffLoaded) return;
    state.lineHandoffLoaded = true;
    if (!state.lineHandoffToken) {
      handoffNotice("聊天銜接連結無效。請在 LINE 輸入「預檢」重新開始，或直接在本頁手動預檢。", true);
      return;
    }
    handoffNotice("正在帶入聊天中選擇的購買選項。此連結不會登入、建立案件或保存填答。");
    try {
      const { data } = await api("/line/handoffs/" + encodeURIComponent(state.lineHandoffToken));
      const choices = data?.choices;
      if (!choices || typeof choices !== "object" || Array.isArray(choices)) throw { status: 422 };
      const allowed = {
        purchase_stage: ["planning", "purchased"],
        billing_type: ["monthly", "annual", "credits", "other", "unsure"],
        billing_component: ["subscription", "included_credits", "standalone_credits", "mixed", "unsure"],
        purchase_channel: ["official", "marketplace", "agent", "app_store", "other", "unsure"],
      };
      const imported = {};
      Object.entries(allowed).forEach(([field, values]) => {
        if (values.includes(choices[field])) imported[field] = choices[field];
      });
      const tool = (state.catalog.tools || []).find((item) => item.id === choices.tool_id);
      if (tool) { imported.tool_id = tool.id; imported.tool_name = tool.name; }
      state.lineHandoffReady = true;
      if (state.dirty) {
        handoffNotice("聊天選項已取得；你已開始修改，本頁保留目前填答。銜接不代表登入或身分驗證，也不會保存或正式送件。");
        return;
      }
      // Do not merge arbitrary response fields, identities, documents or eligibility flags.
      Object.assign(state.answers, imported);
      syncForm(); markChanged();
      const labels = Object.keys(allowed).filter((field) => imported[field]).map((field) => fieldLabels[field] + "：" + (valueLabels[imported[field]] || imported[field]));
      if (tool) labels.splice(1, 0, "工具：" + tool.name);
      handoffNotice("已帶入聊天選項，請在下方核對。" + (labels.length ? labels.join("；") + "。" : "請重新選擇工具與購買方式。") + "銜接不代表登入或身分驗證，也不會保存或正式送件。");
      state.step = 2;
      if (state.view === "precheck") showStep(2);
    } catch (error) {
      state.lineHandoffToken = null; state.lineHandoffReady = false;
      const message = [404, 410, 422].includes(error.status) ? "聊天銜接連結無效或已過期。" : "目前無法帶入聊天選項。";
      handoffNotice(message + "請在 LINE 輸入「預檢」重新開始，或直接在本頁手動預檢；本頁填答仍保留。", true);
    }
  }

  function selectedTool() { return (state.catalog?.tools || []).find((tool) => tool.id === state.answers.tool_id); }

  function renderTools() {
    const target = $("tool-results");
    target.replaceChildren();
    const tools = state.catalog?.tools || [];
    const query = state.answers.tool_name.trim().toLocaleLowerCase();
    const matches = tools.filter((tool) => [tool.name, ...(tool.aliases || [])].some((name) => name.toLocaleLowerCase().includes(query)));
    matches.slice(0, 12).forEach((tool) => {
      const item = button(tool.name, () => {
        const changed = state.answers.tool_id !== tool.id;
        state.answers.tool_id = tool.id;
        state.answers.tool_name = tool.name;
        $("tool-search").value = tool.name;
        if (changed) { state.answers.plan_id = null; state.answers.plan_name = ""; $("plan-name").value = ""; }
        markChanged(); renderTools(); renderPlans();
      }, "tool-option");
      item.setAttribute("aria-pressed", String(state.answers.tool_id === tool.id));
      target.append(item);
    });
    target.append(button("其他／找不到這個工具", () => {
      state.answers.tool_id = null; state.answers.plan_id = null;
      markChanged(); renderTools(); renderPlans(); $("tool-search").focus();
    }, "tool-option"));
    const selected = selectedTool();
    $("tool-selection").textContent = selected ? "已選取目錄項目：" + selected.name + "。公告列舉不代表保證核准，方案、通路及交易仍須分別檢視。" : !tools.length ? "目錄目前沒有工具，可繼續填寫名稱並保留人工確認。" : "未選取目錄項目：將以自填工具名稱預檢，未知工具保留待確認。";
  }

  function renderPlans() {
    const selector = $("plan-select");
    selector.replaceChildren(new Option("其他／找不到／不確定", ""));
    (selectedTool()?.plans || []).forEach((plan) => selector.add(new Option(plan.name, plan.id)));
    selector.value = state.answers.plan_id || "";
  }

  function syncForm() {
    $("precheck-form").querySelectorAll("[name]").forEach((field) => {
      if (field.type === "radio") field.checked = field.value === state.answers[field.name];
      else if (field.type === "checkbox") field.checked = Boolean(state.answers[field.name]);
      else field.value = state.answers[field.name] === null || state.answers[field.name] === undefined ? "" : String(state.answers[field.name]);
    });
    $("use-monthly-transactions").checked = state.useMonthlyTransactions;
    renderTools(); renderPlans(); renderQualifications(); renderTransactions(); conditionalFields();
  }

  function renderQualifications() {
    const target = $("qualification-options"); target.replaceChildren();
    const kind = state.answers.application_type;
    const categories = (state.catalog?.qualification_categories || []).filter((category) => category.kind === kind);
    categories.forEach((category) => {
      const label = node("label", null, "inline-checkbox"); const input = node("input"); input.type = "checkbox";
      input.checked = state.answers.qualification_categories.includes(category.id);
      input.addEventListener("change", () => {
        state.answers.qualification_categories = state.answers.qualification_categories.filter((id) => id !== category.id);
        if (input.checked) state.answers.qualification_categories.push(category.id);
        markChanged();
      }); label.append(input, node("span", category.name)); target.append(label);
    });
    if (!categories.length) target.append(node("p", "目前沒有可選類別，請保留待機關確認。", "quiet"));
  }

  function renderTransactions() {
    const target = $("monthly-transactions"); target.replaceChildren();
    state.answers.transactions.forEach((row, index) => {
      const block = node("div", null, "transaction-row"); const heading = node("div", null, "panel-heading");
      heading.append(node("h4", "第 " + (index + 1) + " 筆月費"), button("移除此筆", () => {
        state.answers.transactions.splice(index, 1);
        state.answers.prepared_documents = [];
        if (!state.answers.transactions.length) { state.useMonthlyTransactions = false; $("use-monthly-transactions").checked = false; }
        markChanged(); renderTransactions(); conditionalFields();
      }, "text-button")); block.append(heading);
      const grid = node("div", null, "field-grid");
      [["purchase_date", "購買日期", "date"], ["subscription_start", "訂閱開始日", "date"], ["subscription_end", "訂閱結束日", "date"], ["eligible_cost_twd", "臺幣帳單金額（待核對）", "text"]].forEach(([key, text, type]) => {
        const field = node("div", null, "field"); const label = node("label", text); const input = node("input");
        input.id = "transaction-" + index + "-" + key; label.htmlFor = input.id; input.type = type; input.value = row[key] ?? "";
        if (type === "text") { input.inputMode = "decimal"; input.maxLength = 18; }
        input.addEventListener("input", () => {
          state.answers.transactions[index][key] = key === "eligible_cost_twd" ? input.value.trim() || null : input.value;
          state.answers.prepared_documents = [];
          markChanged();
        });
        field.append(label, input); grid.append(field);
      }); block.append(grid); target.append(block);
    });
    $("add-transaction").disabled = state.answers.transactions.length >= 12;
  }

  async function evaluate() {
    if (state.evaluating || !state.catalog) return false;
    state.evaluating = true;
    const trigger = $("evaluate-button");
    trigger.disabled = true;
    trigger.textContent = "正在執行預檢…";
    status($("evaluate-error"), "");
    status($("global-status"), "正在以目前填答執行完整預檢…");
    const input = currentInput();
    try {
      const headers = state.lineHandoffReady ? { "X-Line-Handoff": state.lineHandoffToken } : {};
      const { data } = await api("/precheck/evaluate", { method: "POST", body: input, headers });
      state.result = data; state.lastInputs = input;
      // Edits made during a request remain dirty and must be checked again before saving.
      state.dirty = JSON.stringify(input) !== JSON.stringify(currentInput());
      renderResult($("results"), data, { editable: true, inputs: input });
      if (state.dirty) status($("global-status"), "預檢期間填答已改變，請再執行一次預檢後保存。");
      else { status($("global-status"), ""); showStep(4); }
      return !state.dirty;
    } catch (error) {
      status($("global-status"), friendlyError(error), true);
      status($("evaluate-error"), friendlyError(error) + " 可按「執行自填預檢」重試。", true);
      return false;
    } finally {
      trigger.disabled = false; trigger.textContent = "執行自填預檢 →"; state.evaluating = false;
    }
  }

  function outcomeBadge(check) {
    if (check.execution_status === "not_applicable") return node("span", "此情境不適用", "badge pending");
    if (check.execution_status === "failed") return node("span", "執行失敗・待確認", "badge issue");
    if (check.execution_status !== "completed") return node("span", "尚未完成", "badge pending");
    if (check.outcome === "action_needed") return node("span", "需處理", "badge issue");
    if (check.outcome === "manual_review") return node("span", "需人工確認", "badge warning");
    return node("span", check.evidence_type === "server_record" ? "本機紀錄未發現異常" : "自填未發現異常", "badge");
  }

  function displayValue(field, value) {
    if (value === null || value === undefined || value === "") return "未填寫";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (field === "transactions" && Array.isArray(value)) return value.length ? value.map((row, index) => "第 " + (index + 1) + " 筆：" + (row.purchase_date || "日期未填") + "，TWD " + (row.eligible_cost_twd ?? "未填")).join("；") : "未使用逐月明細";
    if (Array.isArray(value)) return value.length ? value.map((id) => (state.catalog?.qualification_categories || []).find((item) => item.id === id)?.name || id).join("、") : "未勾選";
    if (field === "payer" && value === "other") return "他人代付";
    if (field === "residency" && value === "other") return "其他地區";
    if (field === "tool_id") return (state.catalog?.tools || []).find((tool) => tool.id === value)?.name || String(value);
    if (field === "plan_id") return (state.catalog?.tools || []).flatMap((tool) => tool.plans || []).find((plan) => plan.id === value)?.name || String(value);
    return valueLabels[value] || String(value);
  }

  function renderCheck(check, inputs, editable, documentSection) {
    const article = node("article", null, "check-item");
    const heading = node("div", null, "check-heading");
    heading.append(node("h4", check.title || check.check_id), outcomeBadge(check));
    article.append(heading, node("p", check.reason || check.message || "尚無可確認的結果。"));
    const fields = check.related_fields || [];
    const evidence = fields.map((field) => (fieldLabels[field] || field) + "：" + displayValue(field, check.triggered_by?.[field] ?? inputs?.[field])).join("；");
    article.append(node("p", check.evidence_type === "server_record"
      ? "依據目前授權可讀的本系統案件紀錄；未查證跨機關紀錄或實際撥款。"
      : "觸發填答（使用者自述）：" + (evidence || "此檢查依規則或目錄狀態執行"), "check-evidence"));
    const source = node("p", "規則 " + (check.rule_id ? check.rule_id + " · " : "") + (check.rule_version || "待確認") + " · 依據：", "check-evidence");
    source.append(safeLink(check.source?.url, check.source?.title || "查看來源") || node("span", check.source?.title || "未提供可查核來源"));
    article.append(source, node("p", "下一步：" + (check.next_step || "請依公告與機關說明確認。"), "next-step"));
    if (editable) {
      const actions = node("div", null, "check-actions");
      if (fields.length) actions.append(button("返回修改", () => showStep(stepForField(fields[0])), "text-button"));
      actions.append(button("查看所需資料", () => { documentSection.scrollIntoView({ block: "start", behavior: "smooth" }); documentSection.focus({ preventScroll: true }); }, "text-button"));
      article.append(actions);
    }
    return article;
  }

  function renderResult(target, result, options = {}) {
    const editable = Boolean(options.editable);
    const inputs = options.inputs || {};
    target.replaceChildren();
    const top = node("div", null, "result-topline");
    top.append(node("p", "步驟 04 / 04 · 行政預檢", "section-label"), node("span", modeText(result), "badge " + (result.demo ? "demo" : "warning")));
    const title = node("h2", editable ? "你的預檢摘要" : "已保存的預檢摘要");
    title.id = editable ? "result-title" : "snapshot-result-title";
    title.tabIndex = -1;
    target.append(top, title, node("p", result.summary?.message || "部分檢查待確認，請查看各項原因。", "result-message"));
    target.append(node("p", "使用者自述，尚未核對文件。完成預檢與登入保存皆不等於正式送件或核定。", "small-notice"));
    const metrics = node("div", null, "metrics");
    [["required_total", "必要檢查"], ["completed", "已完成檢查"], ["incomplete", "尚未完成"], ["issues", "需處理／確認"]].forEach(([key, label]) => {
      const item = node("div"); item.append(node("span", result.summary?.[key] ?? "—", "metric-value"), node("span", label, "metric-label")); metrics.append(item);
    });
    target.append(metrics, node("p", "規則：" + result.rules_version + " · 目錄：" + result.catalog_version + " · 輸入版本：" + result.input_version + "\n執行時間：" + prettyDate(result.executed_at), "result-meta"));
    if (result.snapshot) {
      target.append(node("p", "公開來源快照：" + result.snapshot.snapshot_version + " · 來源查核日期：" + result.snapshot.source_checked_date + " · 使用範圍：申請前提示 · 機關已確認系統規格：否 · 自動核定：關閉", "result-meta"));
    }
    if (result.estimate) {
      const estimate = node("section", null, "estimate-section"); estimate.append(node("h3", "依填答試算"));
      if (result.estimate.available) estimate.append(node("p", "TWD " + result.estimate.estimated_subsidy_twd, "estimate-amount"));
      else estimate.append(node("p", "目前無法試算", "result-message"));
      if (result.estimate.eligible_cost_twd !== null && result.estimate.eligible_cost_twd !== undefined) estimate.append(node("p", "待核對費用：TWD " + result.estimate.eligible_cost_twd + " · 適用比例：" + result.estimate.rate + " · 上限：TWD " + result.estimate.cap_twd, "quiet"));
      if (result.estimate.reason) estimate.append(node("p", result.estimate.reason, "quiet"));
      estimate.append(node("p", "條件式試算，不是核定金額。費用尚未核對，小數捨入與費用認列方式待確認。" + (result.estimate.proof_pending ? " 身分證明待確認；不同身分的比例及上限不疊加。" : ""), "quiet"));
      target.append(estimate);
    }
    if (result.funding_status) target.append(node("p", "經費／名額狀態：未知。日期在受理期間內不保證仍有預算；完成預檢不保留額度或期限。", "small-notice"));
    if (result.deadline_reminder) target.append(node("p", "期限提醒：" + (result.deadline_reminder.reason || "正式起算、月末及休假日方式須由機關確認。") + (result.deadline_reminder.annual_cutoff ? " 年度公告受理截止：" + result.deadline_reminder.annual_cutoff + "。" : "") + " 本頁不自動產生正式補件倒數。", "result-meta"));
    const documentSection = node("section", null, "result-section");
    documentSection.tabIndex = -1;
    documentSection.append(node("h3", "依你的情境準備資料"), node("p", "勾選僅表示使用者自述已備妥，不代表系統收到或內容核對。", "quiet"));
    const checks = node("section"); checks.append(node("h3", "每項檢查與下一步"));
    (result.checks || []).forEach((check) => checks.append(renderCheck(check, inputs, editable, documentSection)));
    target.append(checks);
    const docs = node("ul", null, "documents");
    (result.documents || []).forEach((doc) => {
      const item = node("li", null, "document-item");
      const label = node("label", null, "document-toggle");
      const checkbox = document.createElement("input"); checkbox.type = "checkbox";
      checkbox.checked = doc.status === "self_reported_ready";
      checkbox.disabled = !editable;
      label.append(checkbox, node("span", doc.title));
      const statuses = node("div", null, "document-statuses");
      const selfStatus = node("span", checkbox.checked ? "使用者自述已備妥" : "需要準備");
      statuses.append(selfStatus, node("span", "系統收到：" + (doc.received ? "已收到" : "尚未收到")), node("span", "內容核對：" + (doc.verified ? "已核對" : "尚未核對")), node("span", "承辦覆核：" + (doc.reviewed ? "已覆核" : "尚未覆核")));
      checkbox.addEventListener("change", () => {
        state.answers.prepared_documents = state.answers.prepared_documents.filter((id) => id !== doc.doc_id);
        if (checkbox.checked) state.answers.prepared_documents.push(doc.doc_id);
        selfStatus.textContent = checkbox.checked ? "使用者自述已備妥" : "需要準備";
        markChanged();
      });
      item.append(label, node("p", doc.reason), statuses); docs.append(item);
    });
    if (!(result.documents || []).length) documentSection.append(node("p", "此情境目前沒有要求準備的文件。尚未購買時不要求現有收據。", "quiet"));
    else documentSection.append(docs);
    target.append(documentSection);
    const uncovered = node("section", null, "result-section"); uncovered.append(node("h3", "這次還沒有查核的範圍"));
    const list = node("ul", null, "uncovered-list"); (result.uncovered_checks || ["收據內容、付款一致性與戶籍真實性尚未查證。"]).forEach((item) => list.append(node("li", typeof item === "string" ? item : item.title || item.message || "待人工查核")));
    uncovered.append(list); target.append(uncovered);
    if (result.pending_confirmations?.length) {
      const pending = node("details", null, "pending-confirmations result-section"); pending.append(node("summary", "待機關確認事項（" + result.pending_confirmations.length + " 項）"));
      result.pending_confirmations.forEach((item) => { const entry = node("div", null, "check-item"); entry.append(node("h4", item.title), node("p", item.detail), node("p", "相關規則：" + (item.rule_ids || []).join("、"), "quiet")); pending.append(entry); }); target.append(pending);
    }
    const caveats = node("div", null, "print-caveats");
    (result.disclaimers || []).forEach((text) => caveats.append(node("p", text)));
    caveats.append(node("p", "未與政府平台同步；不保留申請期限或補助額度。LINE 或信箱登入都不能當成身分或戶籍查證。"));
    target.append(caveats);
    if (editable) {
      const actions = node("div", null, "result-actions");
      actions.append(button("返回修改", () => showStep(3)), button("重新預檢", () => evaluate()), button(state.me ? "保存至本人草稿" : "登入後保存至本人草稿", openSave, "primary"), button("列印預檢摘要", () => window.print()));
      target.append(actions);
      const savePanel = node("div", null, "save-panel"); savePanel.id = "save-panel"; savePanel.hidden = true; target.append(savePanel);
    }
    const official = node("div", null, "official-entry");
    const link = safeLink(result.official_application_url || state.catalog?.official_application_url, "前往正式申辦入口（另開視窗）");
    if (link) official.append(link, node("p", "請在正式申辦平台完成申請；本頁不會代為送件。", "quiet"));
    else official.append(node("p", "正式申辦入口：尚未配置。請由機關公告確認申辦方式，不要把本頁保存當作送件。"));
    target.append(official);
  }

  async function restoreSession() {
    try { const { data } = await api("/me"); onLogin(data, false); }
    catch { state.me = null; state.csrf = ""; }
  }

  function showAccount(forSave = false) {
    state.returnToSave = forSave;
    setView("account");
    $("login-content").hidden = Boolean(state.me);
    $("account-info").hidden = !state.me;
    if (state.me) {
      $("account-info").replaceChildren(node("p", "已登入：" + (state.me.account?.email || "目前帳號")));
      if (forSave) $("account-info").append(button("回到預檢並選擇草稿", openSave, "primary"));
      $("account-info").append(button("查看可存取的案件", showCases, "secondary"), button("登出並清除本頁填答", logout, "text-button"));
    }
    $("account-title").focus({ preventScroll: true });
  }

  function onLogin(data, navigate = true) {
    state.me = data; state.csrf = data.csrf_token || "";
    $("nav-account").textContent = "帳號";
    $("login-content").hidden = true;
    loadSecurityTips();
    if (navigate) {
      status($("auth-status"), "已登入。尚未正式送件，也尚未核對身分、戶籍或文件。");
      if (state.returnToSave) openSave(); else showCases();
    }
  }

  async function logout() {
    try {
      await api("/auth/logout", { method: "POST" });
      state.me = null; state.csrf = ""; state.cases = []; state.schemes = [];
      state.answers = initialAnswers(); state.result = null; state.lastInputs = null; state.selectedCase = null;
      state.dirty = false; state.saveAttempt = null; state.createAttempt = null;
      state.lineHandoffToken = null; state.lineHandoffReady = false;
      $("line-handoff-notice").hidden = true;
      $("results").replaceChildren(); $("cases-content").replaceChildren(); $("case-detail").replaceChildren();
      $("nav-account").textContent = "登入保存";
      $("security-content").replaceChildren(node("p", "網域符合目錄不代表交易或軟體安全。資安提醒不參與補助預檢判定。"));
      ["login-email", "login-code", "staff-email", "staff-password", "mfa-code"].forEach((id) => { $(id).value = ""; });
      state.challenge = null; state.mfaChallenge = null;
      $("email-form").hidden = false; $("code-form").hidden = true;
      syncForm(); showStep(1); status($("global-status"), "已登出並清除本頁記憶體中的填答。先前已保存的案件資料仍依既有保存政策保留。");
    } catch (error) { status($("auth-status"), friendlyError(error), true); }
  }

  async function sendEmail(event) {
    event?.preventDefault();
    if (!$("email-form").reportValidity()) return;
    busy($("email-form"), true); status($("auth-status"), "正在建立驗證流程…");
    try {
      const { data } = await api("/auth/email/challenges", { method: "POST", body: { email: $("login-email").value.trim(), purpose: "login" } });
      state.challenge = data.challenge_id;
      $("code-form").hidden = false; $("login-code").value = ""; $("login-code").focus();
      status($("auth-status"), "驗證流程已建立，請查看信箱。開發環境請依 README 讀取私有郵件。驗證不會保存預檢或正式送件。");
    } catch (error) { status($("auth-status"), friendlyError(error), true); }
    finally { busy($("email-form"), false); }
  }

  async function verifyEmail(event) {
    event.preventDefault();
    if (!state.challenge || !$("code-form").reportValidity()) return;
    busy($("code-form"), true);
    try {
      const { data } = await api("/auth/email/challenges/verify", { method: "POST", body: { challenge_id: state.challenge, code: $("login-code").value } });
      $("login-code").value = ""; state.challenge = null; onLogin(data);
    } catch (error) { status($("auth-status"), friendlyError(error), true); }
    finally { busy($("code-form"), false); }
  }

  async function staffLogin(event) {
    event.preventDefault(); if (!$("staff-form").reportValidity()) return;
    busy($("staff-form"), true);
    try {
      const { data } = await api("/auth/staff/session", { method: "POST", body: { identifier: $("staff-email").value.trim(), password: $("staff-password").value } });
      $("staff-password").value = ""; state.mfaChallenge = data.mfa_challenge_id;
      $("mfa-form").hidden = false; $("mfa-code").focus(); status($("auth-status"), "請完成既有多因素驗證。尚未建立承辦登入工作階段。");
    } catch (error) { status($("auth-status"), friendlyError(error), true); }
    finally { busy($("staff-form"), false); }
  }

  async function verifyStaff(event) {
    event.preventDefault(); if (!state.mfaChallenge || !$("mfa-form").reportValidity()) return;
    busy($("mfa-form"), true);
    try {
      const { data } = await api("/auth/staff/mfa", { method: "POST", body: { mfa_challenge_id: state.mfaChallenge, code: $("mfa-code").value } });
      $("mfa-code").value = ""; state.mfaChallenge = null; onLogin(data);
    } catch (error) { status($("auth-status"), friendlyError(error), true); }
    finally { busy($("mfa-form"), false); }
  }

  async function loadCasesAndSchemes() {
    const [caseResponse, schemeResponse] = await Promise.all([api("/cases"), api("/schemes")]);
    state.cases = caseResponse.data.items || [];
    state.schemes = schemeResponse.data.items || [];
    return caseResponse.data;
  }

  async function openSave() {
    if (!state.result) { showStep(3); return; }
    if (!state.me) { showAccount(true); return; }
    if (state.dirty && !(await evaluate())) return;
    showStep(4);
    const panel = $("save-panel");
    panel.hidden = false; panel.replaceChildren(node("p", "正在載入可保存的本人草稿…"));
    try {
      await loadCasesAndSchemes();
      if (state.preferredDraftId && !state.cases.some((item) => item.id === state.preferredDraftId)) {
        const { data } = await api("/cases/" + encodeURIComponent(state.preferredDraftId));
        state.cases.push(data);
      }
      renderSavePanel(panel);
    } catch (error) {
      panel.replaceChildren(node("p", friendlyError(error)), button("重新載入草稿", openSave));
    }
    panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function renderSavePanel(panel) {
    panel.replaceChildren(node("h3", "保存至本人草稿"), node("p", "伺服器會重新計算並保存版本快照。不會正式送件，也不會更動核定或財務狀態。"));
    const drafts = state.cases.filter((item) => item.status === "DRAFT" && item.allowed_actions?.includes("save_draft"));
    const chooseField = node("div", null, "field"); const chooseLabel = node("label", "選擇草稿"); chooseLabel.htmlFor = "save-draft";
    const selector = node("select"); selector.id = "save-draft";
    drafts.forEach((draft) => selector.add(new Option((draft.case_no || draft.id) + " · " + schemeName(draft.scheme_id), draft.id)));
    selector.add(new Option("建立新的本人草稿", "new"));
    if (state.preferredDraftId && drafts.some((draft) => draft.id === state.preferredDraftId)) selector.value = state.preferredDraftId;
    chooseField.append(chooseLabel, selector); panel.append(chooseField);
    const schemeField = node("div", null, "field"); const schemeLabel = node("label", "新草稿所屬方案"); schemeLabel.htmlFor = "save-scheme";
    const schemeSelect = node("select"); schemeSelect.id = "save-scheme";
    state.schemes.filter((item) => item.active).forEach((scheme) => schemeSelect.add(new Option(scheme.name, scheme.id)));
    if (isPublicPolicy() && state.schemes.some((scheme) => scheme.active && scheme.id === "hsinchu-ai-grant-2026")) schemeSelect.value = "hsinchu-ai-grant-2026";
    schemeField.append(schemeLabel, schemeSelect); panel.append(schemeField);
    const warning = node("p", "目前沒有可建立草稿的方案，請由管理者先設定方案。已有草稿仍可選取。", "quiet"); panel.append(warning);
    const saveStatus = node("div", null, "status-message"); saveStatus.hidden = true; saveStatus.setAttribute("role", "status"); panel.append(saveStatus);
    const save = button("重新計算並保存快照", async () => {
      busy(panel, true); status(saveStatus, "正在重新計算並保存…");
      try {
        let caseId = selector.value;
        if (caseId === "new") {
          if (!schemeSelect.value) throw { publicMessage: "目前沒有可建立草稿的方案。" };
          if (!state.createAttempt || state.createAttempt.scheme !== schemeSelect.value) state.createAttempt = { scheme: schemeSelect.value, key: crypto.randomUUID(), caseId: null };
          if (!state.createAttempt.caseId) {
            const response = await api("/cases", { method: "POST", headers: { "Idempotency-Key": state.createAttempt.key }, body: { scheme_id: schemeSelect.value } });
            state.createAttempt.caseId = response.data.id;
          }
          caseId = state.createAttempt.caseId;
        }
        const caseResponse = await api("/cases/" + encodeURIComponent(caseId));
        const body = currentInput(); const fingerprint = caseId + JSON.stringify(body);
        if (!state.saveAttempt || state.saveAttempt.fingerprint !== fingerprint) state.saveAttempt = { fingerprint, key: crypto.randomUUID(), etag: caseResponse.etag || caseResponse.data.etag };
        const response = await api("/cases/" + encodeURIComponent(caseId) + "/precheck", { method: "POST", headers: { "If-Match": state.saveAttempt.etag, "Idempotency-Key": state.saveAttempt.key,
          ...(state.lineHandoffReady ? { "X-Line-Handoff": state.lineHandoffToken } : {}) }, body });
        state.result = response.data.snapshot.result; state.lastInputs = body;
        state.dirty = JSON.stringify(body) !== JSON.stringify(currentInput());
        state.saveAttempt = null; state.createAttempt = null; state.preferredDraftId = caseId;
        renderResult($("results"), state.result, { editable: true, inputs: body });
        status($("global-status"), "預檢快照已保存至草稿。尚未正式送件，尚未核對文件。" + (state.dirty ? "保存期間另有修改，後續修改尚未保存。" : ""));
        await showCases(caseId);
      } catch (error) {
        if (error.status === 412 || error.status === 409) state.saveAttempt = null;
        status(saveStatus, friendlyError(error), true);
        if (error.status === 401 || error.status === 403) panel.append(button("重新登入，保留填答", () => { state.me = null; state.csrf = ""; showAccount(true); }, "text-button"));
      } finally { busy(panel, false); }
    }, "primary"); panel.append(save);
    const update = () => { schemeField.hidden = selector.value !== "new"; warning.hidden = Boolean(schemeSelect.options.length); save.disabled = selector.value === "new" && !schemeSelect.value; };
    selector.addEventListener("change", update); update();
  }

  function schemeName(id) { return state.schemes.find((scheme) => scheme.id === id)?.name || "既有方案"; }

  async function showCases(caseId) {
    if (!state.me) { showAccount(); return; }
    setView("cases"); $("cases-title").focus({ preventScroll: true });
    const target = $("cases-content"); target.replaceChildren(node("p", "正在載入可存取的案件…", "quiet"));
    $("case-detail").replaceChildren();
    try {
      const response = await loadCasesAndSchemes();
      target.replaceChildren();
      if (!state.cases.length) target.append(node("p", "目前沒有可存取的案件。完成預檢後，可登入保存至本人草稿。", "small-notice"), button("回到預檢", () => showStep(state.step)));
      else {
        const list = node("div", null, "case-list");
        state.cases.forEach((item) => {
          const row = node("article", null, "case-row"); const description = node("div");
          description.append(node("h3", item.case_no || "尚未編號的草稿"), node("p", schemeName(item.scheme_id) + " · " + (caseStatus[item.status] || item.status) + " · 版本 " + item.version));
          row.append(description, button("查看預檢摘要", () => showCaseDetail(item.id))); list.append(row);
        }); target.append(list);
      }
      if (response.next_cursor) target.append(button("載入更多案件", () => loadMoreCases(response.next_cursor), "text-button"));
      if (caseId) await showCaseDetail(caseId);
    } catch (error) { target.replaceChildren(node("p", friendlyError(error), "status-message error"), button("重試", () => showCases())); }
  }

  async function loadMoreCases(cursor) {
    try {
      const { data } = await api("/cases?cursor=" + encodeURIComponent(cursor));
      const target = $("cases-content"); const list = target.querySelector(".case-list");
      if (!list) return;
      target.querySelectorAll(":scope > .text-button").forEach((item) => item.remove());
      (data.items || []).forEach((item) => {
        state.cases.push(item); const row = node("article", null, "case-row"); const description = node("div");
        description.append(node("h3", item.case_no || "尚未編號的草稿"), node("p", schemeName(item.scheme_id) + " · " + (caseStatus[item.status] || item.status)));
        row.append(description, button("查看預檢摘要", () => showCaseDetail(item.id))); list.append(row);
      });
      if (data.next_cursor) target.append(button("載入更多案件", () => loadMoreCases(data.next_cursor), "text-button"));
    } catch (error) { status($("global-status"), friendlyError(error), true); }
  }

  async function showCaseDetail(caseId) {
    const target = $("case-detail"); target.replaceChildren(node("p", "正在載入案件預檢快照…", "quiet"));
    try {
      const [caseResponse, precheckResponse] = await Promise.all([api("/cases/" + encodeURIComponent(caseId)), api("/cases/" + encodeURIComponent(caseId) + "/precheck")]);
      state.selectedCase = { ...caseResponse.data, precheck: precheckResponse.data, etag: precheckResponse.etag || caseResponse.etag };
      renderCaseDetail(target);
    } catch (error) { target.replaceChildren(node("p", friendlyError(error), "status-message error"), button("重新載入摘要", () => showCaseDetail(caseId))); }
    target.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  function renderDifferences(target, differences) {
    if (!differences) return;
    target.append(node("h3", "與前一版快照比較"));
    if (differences.rule_changed || differences.catalog_changed) target.append(node("p", "這次比較涉及" + [differences.rule_changed ? "規則版本更新" : "", differences.catalog_changed ? "目錄版本更新" : ""].filter(Boolean).join("及") + "，差異不全是使用者修改造成。", "small-notice"));
    if (differences.transaction_set_changed || differences.transaction_changes_require_review) target.append(node("p", differences.transaction_change_message || "逐月交易內容或順序已變更，請逐筆核對前後快照。不同索引的交易不自動視為同筆問題已解除。", "small-notice"));
    const grid = node("div", null, "diff-grid");
    [["new_issues", "新增問題"], ["resolved_issues", "已解除問題"], ["still_pending", "仍待確認"], ["lost_confirmation", "原已完成，現在無法確認"]].forEach(([key, label]) => {
      const group = node("div", null, "diff-group"); group.append(node("h4", label));
      const items = differences[key] || [];
      if (!items.length) group.append(node("p", "無", "quiet"));
      else { const list = node("ul"); items.forEach((item) => list.append(node("li", typeof item === "string" ? item : item.title || item.check_id))); group.append(list); }
      grid.append(group);
    }); target.append(grid);
  }

  function renderCaseDetail(target, selectedSnapshot) {
    const item = state.selectedCase; const data = item.precheck; const snapshot = selectedSnapshot || data.latest;
    target.replaceChildren();
    const heading = node("div", null, "snapshot-heading"); heading.append(node("h3", (item.case_no || "本人草稿") + " · 預檢摘要"));
    target.append(heading);
    if (!snapshot) {
      target.append(node("p", "此案件尚未保存預檢快照。這不代表案件缺件或不符合補助條件。", "small-notice"));
      if (item.status === "DRAFT") target.append(button("開始預檢", () => showStep(1), "primary"));
      return;
    }
    const history = data.history || [];
    if (history.length > 1) {
      const field = node("div", null, "field"); const label = node("label", "查看快照版本"); label.htmlFor = "snapshot-version";
      const selector = node("select"); selector.id = "snapshot-version";
      history.forEach((entry) => selector.add(new Option("第 " + entry.sequence + " 版 · " + prettyDate(entry.executed_at) + " · " + entry.rules_version, entry.id)));
      selector.value = snapshot.id;
      selector.addEventListener("change", () => renderCaseDetail(target, history.find((entry) => entry.id === selector.value)));
      field.append(label, selector); heading.append(field);
    }
    target.append(node("p", "快照第 " + snapshot.sequence + " 版 · 案件狀態：" + (caseStatus[item.status] || item.status) + "。以下顯示該次執行結果，不會把舊版本當作目前規則重新判定。", "result-meta"));
    renderDifferences(target, snapshot.differences || data.differences);
    const summary = node("div", null, "snapshot-summary"); renderResult(summary, snapshot.result, { inputs: snapshot.inputs }); target.append(summary);
    const actions = node("div", null, "result-actions");
    if (item.status === "DRAFT" && item.allowed_actions?.includes("save_draft")) actions.append(button("以這份填答修改並重新預檢", () => {
      state.answers = { ...initialAnswers(), ...snapshot.inputs, prepared_documents: [...(snapshot.inputs?.prepared_documents || [])], qualification_categories: [...(snapshot.inputs?.qualification_categories || [])], transactions: (snapshot.inputs?.transactions || []).map((row) => ({ ...row })) };
      state.useMonthlyTransactions = Boolean(state.answers.transactions.length); state.preferredDraftId = item.id;
      state.dirty = true; state.result = snapshot.result; syncForm(); showStep(2);
      status($("global-status"), "已載入第 " + snapshot.sequence + " 版填答供修改，原快照保持不變。重新預檢並保存後才會新增版本。");
    }, "primary"));
    actions.append(button("列印此份預檢摘要", () => window.print()), button("回到補助預檢", () => showStep(state.step)));
    target.append(actions);
  }

  async function loadSecurityTips() {
    if (!state.me) return;
    const target = $("security-content");
    try {
      const { data } = await api("/security-tips");
      target.replaceChildren(node("p", "資安提醒獨立於行政預檢，不會變更補助適用結果。網域相符不代表交易或軟體安全。"));
      if (!(data.items || []).length) target.append(node("p", "目前沒有已發布的資安提醒。", "quiet"));
      (data.items || []).forEach((tip) => {
        const article = node("article", null, "security-tip");
        article.append(node("h3", tip.content?.title || tip.code || "資安提醒"));
        const content = tip.content;
        if (typeof content === "string") article.append(node("p", content));
        else if (content) {
          [content.body, content.message, content.description, ...(Array.isArray(content.items) ? content.items : [])].filter((part) => typeof part === "string").forEach((part) => article.append(node("p", part)));
        }
        article.append(node("p", "資安內容版本：" + tip.version + " · 不參與行政判定", "quiet")); target.append(article);
      });
    } catch { target.replaceChildren(node("p", "目前無法載入既有資安提醒，行政預檢仍可使用。資安提醒不參與補助適用判定。"), button("重試載入資安提醒", loadSecurityTips, "text-button")); }
  }

  function switchLogin(mode) {
    $("login-youth").setAttribute("aria-pressed", String(mode === "youth"));
    $("login-staff").setAttribute("aria-pressed", String(mode === "staff"));
    $("email-form").hidden = mode !== "youth"; $("code-form").hidden = mode !== "youth" || !state.challenge;
    $("staff-form").hidden = mode !== "staff"; $("mfa-form").hidden = mode !== "staff" || !state.mfaChallenge;
    status($("auth-status"), "");
  }

  $("precheck-form").addEventListener("input", (event) => {
    const field = event.target; if (!field.name) return;
    if (field.name === "tool_name") {
      if (selectedTool()?.name !== field.value) { state.answers.tool_id = null; state.answers.plan_id = null; }
      state.answers.tool_name = field.value; renderTools(); renderPlans();
    } else if (field.name === "plan_id") {
      state.answers.plan_id = field.value || null;
      const plan = selectedTool()?.plans?.find((item) => item.id === field.value);
      if (plan) {
        state.answers.plan_name = plan.name; $("plan-name").value = plan.name;
        if (plan.billing_types?.length === 1) { state.answers.billing_type = plan.billing_types[0]; $("billing-type").value = plan.billing_types[0]; }
      }
    } else if (field.name === "plan_name") {
      state.answers.plan_name = field.value;
      const known = selectedTool()?.plans?.find((plan) => plan.id === state.answers.plan_id);
      if (known && known.name !== field.value) { state.answers.plan_id = null; renderPlans(); }
    } else if (field.name === "multiple_tools") state.answers.multiple_tools = field.value === "true";
    else if (field.type === "checkbox") state.answers[field.name] = field.checked;
    else if (field.name === "eligible_cost_twd") state.answers.eligible_cost_twd = field.value.trim() || null;
    else state.answers[field.name] = field.value;
    if (field.name === "application_type") { state.answers.qualification_categories = []; renderQualifications(); }
    if (field.name === "purchase_stage" || field.name === "billing_type") state.answers.prepared_documents = [];
    markChanged(); conditionalFields();
  });
  $("precheck-form").addEventListener("submit", (event) => { event.preventDefault(); evaluate(); });
  document.querySelectorAll("[data-next]").forEach((item) => item.addEventListener("click", () => showStep(Number(item.dataset.next))));
  document.querySelectorAll("[data-step]").forEach((item) => item.addEventListener("click", () => showStep(Number(item.dataset.step))));
  $("retry-catalog").addEventListener("click", bootstrap);
  $("nav-precheck").addEventListener("click", () => showStep(state.step));
  $("nav-cases").addEventListener("click", () => showCases());
  $("nav-account").addEventListener("click", () => showAccount(false));
  $("back-to-precheck").addEventListener("click", () => showStep(state.step));
  $("refresh-cases").addEventListener("click", () => showCases());
  $("email-form").addEventListener("submit", sendEmail);
  $("resend-code").addEventListener("click", sendEmail);
  $("code-form").addEventListener("submit", verifyEmail);
  $("staff-form").addEventListener("submit", staffLogin);
  $("mfa-form").addEventListener("submit", verifyStaff);
  $("login-youth").addEventListener("click", () => switchLogin("youth"));
  $("login-staff").addEventListener("click", () => switchLogin("staff"));
  $("fill-demo").addEventListener("click", () => {
    if (!state.catalog) return;
    const demo = state.catalog.demo;
    const tool = demo ? state.catalog.tools.find((entry) => entry.id === "demo-studio") : state.catalog.tools.find((entry) => entry.catalog_status === "listed_example") || state.catalog.tools[0];
    if (!tool) return;
    state.answers = { ...initialAnswers(), purchase_stage: "purchased", tool_id: tool.id, tool_name: tool.name, plan_id: demo ? "monthly-credit" : null, plan_name: demo ? "創作月訂閱（內含額度）" : "自填月訂閱方案（待查核）", billing_type: "monthly", purchase_channel: "official", purchase_url: demo ? "https://studio.example" : "", purchase_date: "2026-09-01", subscription_start: "2026-09-01", subscription_end: "2026-10-01", residency: "hsinchu", birth_date: "2000-01-01", application_type: "standard", payer: "self", payment_method: "credit_card", billing_component: "subscription", eligible_cost_twd: "4000", prior_subsidy: "none" };
    state.useMonthlyTransactions = false;
    syncForm(); markChanged(); showStep(2); status($("global-status"), "已填入合成示範資料，沒有使用真實個資。可修改後執行預檢。");
  });
  $("use-monthly-transactions").addEventListener("change", (event) => {
    state.useMonthlyTransactions = event.target.checked;
    state.answers.prepared_documents = [];
    if (state.useMonthlyTransactions && !state.answers.transactions.length) state.answers.transactions.push({ purchase_date: state.answers.purchase_date, subscription_start: state.answers.subscription_start, subscription_end: state.answers.subscription_end, eligible_cost_twd: state.answers.eligible_cost_twd });
    markChanged(); renderTransactions(); conditionalFields();
  });
  $("add-transaction").addEventListener("click", () => {
    if (state.answers.transactions.length >= 12) return;
    state.answers.transactions.push({ purchase_date: "", subscription_start: "", subscription_end: "", eligible_cost_twd: null });
    state.answers.prepared_documents = [];
    markChanged(); renderTransactions();
  });
  bootstrap();
  restoreSession();
})();
