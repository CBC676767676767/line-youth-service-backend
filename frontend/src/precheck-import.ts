import type { Form } from "./model";

function validDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  if (year < 1 || month < 1 || month > 12) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day >= 1 && day <= days[month - 1];
}

/** Preserve the exact decimal value; never round it into the formal form. */
function formalAmount(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 128) return null;
  const match = /^(0|[1-9]\d{0,8})(?:\.(\d+))?$/.exec(value);
  if (!match) return null;
  const fraction = (match[2] || "").replace(/0+$/, "");
  if (fraction.length > 2) return null;
  return match[1] + (fraction ? `.${fraction}` : "");
}

/** Candidates only: the caller must let the applicant review each field.
 * Existing values never suppress a conflicting candidate or get mutated here.
 * No documents, identity proof, payment proof or eligibility result is imported.
 */
export function mapPrecheckToForm(
  inputs: Record<string, unknown>,
  _currentForm: Form,
): { updates: Partial<Form>; unresolved: string[] } {
  const updates: Partial<Form> = {};
  const unresolved: string[] = [];
  if (validDate(inputs.birth_date)) updates.birth = inputs.birth_date;
  else unresolved.push("出生日期尚未提供有效的西元年月日，請依證件填寫。");

  switch (inputs.residency) {
    case "hsinchu": updates.city = "新竹市"; break;
    case "hsinchu_county": updates.city = "新竹縣"; break;
    case "other": updates.city = "其他"; break;
    default: unresolved.push("戶籍縣市尚待確認，未替你選定新竹市。");
  }
  if (typeof inputs.tool_name === "string" && inputs.tool_name.trim()
      && inputs.tool_name.trim().length <= 200 && !/[\u0000-\u001f\u007f]/.test(inputs.tool_name)) {
    updates.tool = inputs.tool_name.trim();
  } else unresolved.push("請依實際憑證填寫 AI 工具完整名稱；工具識別碼不會轉成名稱。");

  switch (inputs.application_type) {
    case "standard": updates.special = false; break;
    case "specific":
    case "language":
      updates.special = true;
      unresolved.push("特定身分或語言資格只是預檢自述，須另附證明並由承辦確認。");
      break;
    default: unresolved.push("申請身分類型尚待確認，請自行核對是否申請特定對象補助。");
  }

  if (inputs.purchase_stage !== "purchased") {
    unresolved.push(inputs.purchase_stage === "planning"
      ? "尚未購買：購買、訂閱期間、付款及金額欄位不帶入，請購買後依實際憑證填寫。"
      : "購買情境尚待確認，未帶入購買、付款或金額欄位。");
    return { updates, unresolved };
  }

  const hasTransactions = Array.isArray(inputs.transactions) && inputs.transactions.length > 0;
  const invalidTransactions = inputs.transactions != null && !Array.isArray(inputs.transactions);
  if (hasTransactions || invalidTransactions) {
    unresolved.push("逐月交易明細需逐筆核對，不會合併為單筆購買日期、訂閱期間或金額。");
  } else {
    if (validDate(inputs.purchase_date)) updates.purchaseDate = inputs.purchase_date;
    else unresolved.push("購買日期尚待確認，請依實際憑證填寫。");
    if (validDate(inputs.subscription_end)) updates.periodEnd = inputs.subscription_end;
    else unresolved.push("訂閱結束日尚待確認，請依實際訂閱期間填寫。");
  }

  if (inputs.billing_component === "mixed") {
    unresolved.push("訂閱與其他費用混合，請先分開核對，不自動選定正式方案類型。");
  } else if (typeof inputs.billing_type === "string"
      && ["monthly", "annual", "credits"].includes(inputs.billing_type)) {
    updates.plan = inputs.billing_type;
  } else unresolved.push("訂閱方式無法直接對應正式表單，請核對月費、年費或額度方案。");

  switch (inputs.purchase_channel) {
    case "official": updates.channel = "official"; break;
    case "agent":
    case "marketplace": updates.channel = "reseller"; break;
    case "app_store":
    case "other":
    case "unsure":
      updates.channel = "unknown";
      unresolved.push("購買管道需人工確認，已列為待確認，不視為官方網站直購。");
      break;
    default: unresolved.push("購買管道尚未提供有效選項，請依實際憑證填寫。");
  }
  switch (inputs.payment_method) {
    case "credit_card": updates.paymentMethod = "card"; break;
    case "other": updates.paymentMethod = "other"; break;
    default: unresolved.push("付款方式尚待確認，請依付款證明填寫。");
  }
  if (inputs.payer === "self") updates.payer = "self";
  else if (inputs.payer === "other"
      && typeof inputs.payer_relationship === "string"
      && ["parent", "spouse", "legal_guardian"].includes(inputs.payer_relationship)) {
    updates.payer = "relative";
    unresolved.push("親屬或法定代理人代付須另附關係證明與共同切結書，適用情形由承辦確認。");
  } else unresolved.push("付款人或代付關係尚待人工核對，不會把朋友或其他關係自動改成親屬代付。");

  const amount = formalAmount(inputs.eligible_cost_twd);
  if (inputs.foreign_currency_only === true) {
    unresolved.push("只有外幣金額，不能推算或帶入臺幣實付金額。");
  } else if (inputs.multiple_tools === true) {
    unresolved.push("多項工具費用需分開核對，不會合併帶入單一工具金額。");
  } else if (hasTransactions || invalidTransactions) {
    // The transaction warning above also covers amount; never total these rows.
  } else if ((inputs.foreign_currency_only !== undefined && inputs.foreign_currency_only !== false)
      || (inputs.multiple_tools !== undefined && inputs.multiple_tools !== false)) {
    unresolved.push("外幣或多工具情境資料不完整，請先確認費用內容。");
  } else if (typeof inputs.billing_component !== "string"
      || !["subscription", "included_credits", "standalone_credits"].includes(inputs.billing_component)) {
    unresolved.push("費用內容混合或尚未確認，請先核對各項費用再填寫臺幣金額。");
  } else if (amount === null) {
    unresolved.push("臺幣費用未提供，或超過正式表單的九位整數／兩位小數範圍；未自行補零或四捨五入。");
  } else {
    updates.amount = amount;
  }
  unresolved.push("申請補助金額與憑證核對金額須另行確認填寫；預檢不代填或核定金額。");
  return { updates, unresolved };
}
