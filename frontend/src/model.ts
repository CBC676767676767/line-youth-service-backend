export const OFFICIAL =
  "https://dgservice.hccg.gov.tw/serviceNotice.do?id=1323&rule=guest";
export type View =
  | "line"
  | "home"
  | "apply"
  | "ocr"
  | "tracking"
  | "safety"
  | "review";
export type Status =
  | "DRAFT"
  | "RECEIVED"
  | "UNDER_REVIEW"
  | "SUPPLEMENT"
  | "RESUBMITTED"
  | "APPROVED"
  | "REIMBURSED"
  | "PAID"
  | "REJECTED";
export type Doc = {
  id: string;
  title: string;
  hint: string;
  file?: string;
  quality?: "clear" | "blur";
  preview?: string;
  synthetic?: boolean;
  attachments?: { file: string; preview: string }[];
};
export type Form = {
  name: string;
  email: string;
  birth: string;
  city: string;
  tool: string;
  channel: string;
  plan: string;
  purchaseDate: string;
  periodEnd: string;
  amount: string;
  requested: string;
  receiptName: string;
  receiptEmail: string;
  payer: string;
  special: boolean;
  bankName: string;
  receiptAmount: string;
  phone: string;
  address: string;
  identityHint: string;
  company: string;
  origin: string;
  currency: string;
  originalAmount: string;
  paymentMethod: string;
  bankType: string;
  category: string;
};
export type Finding = {
  id: string;
  severity: "error" | "warning" | "pass";
  title: string;
  detail: string;
  source: string;
};
export const names: Record<Status, string> = {
  DRAFT: "草稿準備中",
  RECEIVED: "已收件",
  UNDER_REVIEW: "承辦審查中",
  SUPPLEMENT: "待補正",
  RESUBMITTED: "補件已送出",
  APPROVED: "已核定",
  REIMBURSED: "核銷完成",
  PAID: "撥款完成",
  REJECTED: "不予補助",
};
export const baseDocs: Doc[] = [
  {
    id: "idFront",
    title: "身分證正面",
    hint: "姓名、出生年月日及身分證字號清晰可辨。",
  },
  { id: "idBack", title: "身分證反面", hint: "需能辨識設籍新竹市的戶籍地址。" },
  {
    id: "receipt",
    title: "官方訂閱收據／憑證",
    hint: "姓名或電子信箱、AI完整名稱、公司、訂閱日期與期間、原始費用、付款方式。",
  },
  {
    id: "payment",
    title: "臺幣帳單與繳款證明",
    hint: "依付款方式備妥臺幣出帳／交易明細及本人付款帳戶證明。",
  },
  {
    id: "bank",
    title: "本人存摺封面",
    hint: "限申請人本人帳戶；戶名、銀行、帳號完整清楚。",
  },
  {
    id: "affidavit",
    title: "親筆簽名切結書",
    hint: "使用官方附件三，親筆簽名後上傳；未成年者留意完整計畫中的法定代理人簽名欄。代付者的聲明填法請先向承辦確認。",
  },
];
export const goodForm: Form = {
  name: "林小竹",
  email: "demo@example.com",
  birth: "2001-05-16",
  city: "新竹市",
  tool: "ChatGPT",
  channel: "official",
  plan: "monthly",
  purchaseDate: "2026-09-05",
  periodEnd: "2026-10-05",
  amount: "680",
  requested: "340",
  receiptName: "林小竹",
  receiptEmail: "demo@example.com",
  payer: "self",
  special: false,
  bankName: "林小竹",
  receiptAmount: "680",
  phone: "0900-000-000",
  address: "新竹市東區",
  identityHint: "",
  company: "OpenAI",
  origin: "美國",
  currency: "USD",
  originalAmount: "20",
  paymentMethod: "card",
  bankType: "taiwan",
  category: "通用型",
};
export const paymentHints: Record<string, string> = {
  card: "信用卡：卡片姓名、簽名、末4碼照片；帳單姓名、末4碼、品名及臺幣金額。其他卡號可遮蔽，不提供安全碼。",
  telecom: "電信帳單：繳款人、電話末3碼、品名及臺幣金額。",
  wallet:
    "電子支付：本人支付帳戶證明，以及付款日期、金額、品名的交易明細；臺幣金額須有出帳依據。",
  other:
    "其他方式：本人支付帳戶證明，以及付款日期、金額、品名的付款明細；臺幣金額須有出帳依據。",
};
export function requiredDocs(f: Form) {
  return [
    ...baseDocs.map((d) =>
      d.id === "payment"
        ? { ...d, hint: paymentHints[f.paymentMethod] || paymentHints.other }
        : d,
    ),
    ...(f.special
      ? [
          {
            id: "special",
            title: "特定身分／語言證照",
            hint: "申請90%補助者，提供符合官方資格的證明。",
          },
        ]
      : []),
    ...(f.payer === "relative"
      ? [
          {
            id: "relationship",
            title: "代付關係證明與共同切結書",
            hint: "戶口名簿、戶籍謄本等關係證明及附件四；適用關係交由承辦確認。",
          },
        ]
      : []),
  ];
}
export function seedDocs(f: Form, bad = false): Doc[] {
  return requiredDocs(f).map((d) => ({
    ...d,
    file: `範例_${d.title}.png`,
    quality: bad && d.id === "idFront" ? "blur" : "clear",
    synthetic: true,
  }));
}
function moneyCents(value: string): bigint | null {
  if (!/^(?:0|[1-9]\d{0,8})(?:\.\d{1,2})?$/.test(value)) return null;
  const [whole, fraction = ""] = value.split(".");
  return BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0"));
}
function estimateUnits(f: Form): bigint | null {
  const cents = moneyCents(f.amount);
  if (cents === null) return null;
  // Four decimal places retain the exact percentage, including fractions of a
  // cent. An agency rounding rule has not been supplied, so none is invented.
  const calculated = cents * (f.special ? 90n : 50n);
  const cap = (f.special ? 6000n : 3000n) * 10_000n;
  return calculated < cap ? calculated : cap;
}
export function estimate(f: Form): string | null {
  const units = estimateUnits(f);
  if (units === null) return null;
  const fraction = (units % 10_000n).toString().padStart(4, "0").replace(/0+$/, "");
  return (units / 10_000n).toString() + (fraction ? `.${fraction}` : "");
}
export function money(value: number | string | null | undefined): string {
  if (value === null || value === undefined) return "待確認";
  const text = String(value);
  if (!/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(text)) return "待確認";
  const [whole, fraction] = text.split(".");
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (fraction ? `.${fraction}` : "");
}
export function checks(f: Form, docs: Doc[]): Finding[] {
  const found: Finding[] = [];
  const add = (
    id: string,
    severity: Finding["severity"],
    title: string,
    detail: string,
    source: string,
  ) => found.push({ id, severity, title, detail, source });
  const missing = requiredDocs(f).filter(
    (d) => !docs.some((x) => x.id === d.id && x.file),
  );
  add(
    "docs",
    missing.length ? "error" : "pass",
    missing.length ? "應備文件還沒齊全" : "應備文件已備齊",
    missing.length
      ? `請補上：${missing.map((d) => d.title).join("、")}`
      : "已檢查檔案是否存在；內容真實性與簽名仍由承辦確認。",
    "官方「申請證明文件、應備文件」",
  );
  const blurry = docs.filter((d) => d.quality === "blur");
  const unknownQuality = docs.some((d) => d.file && !d.synthetic);
  const noImages = !docs.some((d) => d.file);
  add(
    "quality",
    blurry.length ? "error" : unknownQuality || noImages ? "warning" : "pass",
    blurry.length
      ? "身分證影像需要重拍"
      : noImages
        ? "尚未加入文件供檢查"
        : unknownQuality
          ? "上傳文件的清晰度仍需人工確認"
          : "範例文件已就緒",
    blurry.length
      ? "照片的姓名區域模糊。請平放文件、避開反光，拍攝完整邊界。"
      : noImages
        ? "加入文件後，再檢查照片是否完整、清晰、沒有反光。"
        : unknownQuality
          ? "已上傳不代表可辨識。本機影像品質提示僅供參考，請逐份檢視原圖。"
          : "範例文件已有品質標記；使用自己的文件時，仍需重新辨識與確認。",
    "影像品質提示，非身分驗證",
  );
  const excluded = [
    "CapCut",
    "Kling",
    "Meitu",
    "Wink",
    "WHEE",
    "SenseAvatar",
    "Manus",
  ];
  const known = [
    "ChatGPT",
    "Google AI",
    "Grok",
    "Claude",
    "Perplexity",
    "Canva AI",
    "Adobe Firefly",
    "Midjourney",
    "Figma AI",
    "Microsoft Copilot",
    "copy.ai",
    "Notion AI",
    "Jasper",
    "Grammarly",
    "Speak",
    "Elicit",
    "Cursor",
  ];
  const banned = excluded.some(
    (x) => x.toLowerCase() === f.tool.trim().toLowerCase(),
  );
  const listed = known.some(
    (x) => x.toLowerCase() === f.tool.trim().toLowerCase(),
  );
  add(
    "tool",
    banned ? "error" : listed ? "pass" : "warning",
    banned
      ? "此工具列於不予補助清單"
      : listed
        ? "工具出現在公告列舉項目"
        : "工具需進一步確認",
    banned
      ? "依目前公告，此工具不予補助；有疑義可洽青年發展中心，系統不代替正式處分。"
      : listed
        ? "仍需核對實際方案、購買管道與憑證。"
        : "公告工具清單並非完整白名單；未列出不等於不符合，交由承辦確認。",
    "官方「可補助AI軟體／不予補助」",
  );
  add(
    "channel",
    f.plan === "credits" || f.channel === "reseller"
      ? "error"
      : f.channel === "unknown"
        ? "warning"
        : "pass",
    f.plan === "credits" || f.channel === "reseller"
      ? "購買方式不符公告範圍"
      : f.channel === "unknown"
        ? "購買管道尚待確認"
        : "購買方式初步符合",
    f.plan === "credits" || f.channel === "reseller"
      ? "代購／集合式平台及API額度、點數、Token預付儲值不在本計畫補助範圍。"
      : f.channel === "unknown"
        ? "請承辦核對實際購買平台；不確定不等於不符資格。"
        : "申報為官方網站直接購買訂閱，須核對收據。",
    "官方「不予補助」第2、3款",
  );
  const name = f.name.trim();
  const receiptName = f.receiptName.trim();
  const email = f.email.trim().toLowerCase();
  const receiptEmail = f.receiptEmail.trim().toLowerCase();
  const identity = Boolean(
    (name && receiptName && name === receiptName) ||
    (email && receiptEmail && email === receiptEmail),
  );
  add(
    "identity",
    f.payer === "relative" ? "warning" : identity ? "pass" : "warning",
    f.payer === "relative"
      ? "代付款項交由承辦確認"
      : identity
        ? "購買人資訊可對應申請人"
        : "購買人資訊對不上",
    f.payer === "relative"
      ? "請附關係證明及共同切結書。公告對代付關係的文字不完全一致，需人工確認。"
      : identity
        ? "姓名或電子信箱相符；這是欄位比對，不代表已驗證本人身分。"
        : `申請人：${f.name}／${f.email}；憑證：${f.receiptName}／${f.receiptEmail}。先確認辨識結果；如有其他可辨識購買人資訊或代付例外，由承辦核對，不能只憑姓名不同駁回。`,
    "官方「審核機制」購買人資訊",
  );
  const amount = moneyCents(f.amount);
  const requested = moneyCents(f.requested);
  const receiptAmount = moneyCents(f.receiptAmount);
  const expected = estimateUnits(f);
  const needsRoundingRule = expected !== null && expected % 100n !== 0n;
  const amountValid = amount !== null && amount > 0n && requested !== null && requested > 0n;
  const amountMatch =
    amountValid && !needsRoundingRule && requested * 100n === expected && receiptAmount === amount;
  add(
    "amount",
    amountMatch
      ? "pass"
      : !amountValid ||
          (!needsRoundingRule && expected !== null && requested !== null && requested * 100n > expected) ||
          (receiptAmount !== null && amount !== null && receiptAmount !== amount)
        ? "error"
        : "warning",
    amountMatch ? "金額計算一致" : "申請金額需要確認",
    expected === null
      ? "臺幣費用尚未提供有效金額，無法試算；未知費用不視為 0。請依付款與憑證填寫。"
      : `臺幣付款 ${money(f.amount)} 元 × ${f.special ? "90" : "50"}%，依上限條件式試算 ${money(estimate(f))} 元；目前填寫 ${money(f.requested)} 元。${needsRoundingRule ? "精確結果含不足一分的尾數，須由承辦確認處理規則；未自行捨去或四捨五入。" : "試算不代表核定金額，仍須核對費用與資格證明。"}`,
    "官方「補助金額、臺幣帳單」",
  );
  const bankName = f.bankName.trim();
  const bankKnown = Boolean(bankName && name);
  const bankMatches = bankKnown && bankName === name;
  add(
    "bank",
    !bankKnown ? "warning" : bankMatches ? "pass" : "error",
    !bankKnown ? "申請人姓名或存摺戶名尚待填寫" : bankMatches
      ? "存摺戶名一致"
      : "存摺戶名與申請人不同",
    "公告要求申請人本人帳戶。帳號正確性與實際帳戶歸屬仍需確認。",
    "官方「存摺封面影本」",
  );
  const eligible =
    f.city === "新竹市" && f.birth >= "1985-04-03" && f.birth <= "2010-04-02";
  add(
    "eligibility",
    eligible ? "pass" : "error",
    eligible ? "設籍與出生日期初步符合" : "基本資格需要確認",
    "依公告所列出生日期範圍1985/04/03–2010/04/02及新竹市設籍條件比對，仍須核對證件。",
    "2026/08/14起官方資格版本",
  );
  const inRange =
    f.purchaseDate >= "2026-04-02" && f.purchaseDate <= "2026-10-31";
  add(
    "date",
    inRange ? "warning" : "error",
    inRange ? "購買日期在範圍內；申請期限待確認" : "購買日期不在公告範圍",
    "月費制購買後1個月、年費制2個月內申請，最晚2026/11/30或經費用罄止；多月累計與曆月邊界交由承辦核對。",
    "官方「購買日期、申請方式」",
  );
  add(
    "duplicate",
    "warning",
    "重複補助需由機關查核",
    "每年以一次、一項AI工具為限。跨局處補助紀錄須由機關查核，不能僅憑本頁資料判定。",
    "官方「補助次數、重複補助」",
  );
  return found;
}
