/** Local text checks only. No household registry or identity verification is performed. */
export type IdentityFields = {
  name: string;
  idNumber: string;
  birth: string;
  address: string;
};
export type IdentitySide = {
  fileName: string;
  previewUrl: string;
  text: string;
};
export type IdentityResult = {
  fields: IdentityFields;
  front: IdentitySide;
  back: IdentitySide;
  checkedAt: string;
  corrections: Array<{
    field: keyof IdentityFields;
    before: string;
    after: string;
  }>;
};
export type IdentityApplicant = {
  name: string;
  birth: string;
  address: string;
  idNumber?: string;
};
export type IdentityCheck = {
  field: string;
  level: "error" | "warning" | "info";
  message: string;
};
export type Extraction<T> = { fields: T; notes: string[] };

export const emptyIdentityFields = (): IdentityFields => ({
  name: "",
  idNumber: "",
  birth: "",
  address: "",
});

// Mapping and weighted checksum: Ministry of Examination, 110年普通考試資訊處理第3題.
// https://wwwq.moex.gov.tw/exam/wHandExamQandA_File.ashx?c=110&code=110130&q=1&s=1005&t=Q
const LETTER_CODES: Record<string, number> = {
  A: 10,
  B: 11,
  C: 12,
  D: 13,
  E: 14,
  F: 15,
  G: 16,
  H: 17,
  I: 34,
  J: 18,
  K: 19,
  L: 20,
  M: 21,
  N: 22,
  O: 35,
  P: 23,
  Q: 24,
  R: 25,
  S: 26,
  T: 27,
  U: 28,
  V: 29,
  W: 32,
  X: 30,
  Y: 31,
  Z: 33,
};

export function normalizeTaiwanId(value: string): string {
  // Do not turn O into 0, I into 1, or otherwise guess uncertain OCR characters.
  return value.normalize("NFKC").toUpperCase().replace(/\s/g, "");
}

export function validateTaiwanId(value: string): {
  valid: boolean;
  normalized: string;
  code: string;
  message: string;
} {
  const normalized = normalizeTaiwanId(value);
  if (
    /^[A-Z][89]\d{8}$/.test(normalized) ||
    /^[A-Z]{2}\d{8}$/.test(normalized)
  ) {
    return {
      valid: false,
      normalized,
      code: "unsupported",
      message: "非本模組支援的國民身分證格式，需人工確認；這不代表證件無效。",
    };
  }
  if (!/^[A-Z][12]\d{8}$/.test(normalized)) {
    return {
      valid: false,
      normalized,
      code: "format",
      message:
        "字號應為 1 個英文字母與 9 個數字，第 2 碼為 1 或 2。請對照原圖，留意 O／0、I／1。",
    };
  }
  const digits = `${LETTER_CODES[normalized[0]]}${normalized.slice(1)}`
    .split("")
    .map(Number);
  const weights = [1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1];
  const valid =
    digits.reduce((sum, value, index) => sum + value * weights[index], 0) %
      10 ===
    0;
  return {
    valid,
    normalized,
    code: valid ? "checksum" : "checksum_mismatch",
    message: valid
      ? "字號格式與檢查碼符合；尚未查證是否核發、掛失或屬於本人。"
      : "字號檢查碼不符合，可能是辨識或輸入錯誤。請對照原圖逐碼修正。",
  };
}

export type BirthCheck = {
  valid: boolean;
  iso: string;
  message: string;
  assumedROC: boolean;
};
export function parseBirthDate(input: string, today = new Date()): BirthCheck {
  const value = input.normalize("NFKC").trim();
  const invalid = (message: string): BirthCheck => ({
    valid: false,
    iso: "",
    message,
    assumedROC: false,
  });
  if (/民國前|民前/.test(value))
    return invalid("民國前的出生日期請由人工確認與填寫西元日期。");
  const match = value.match(
    /^(?:(民國|西元|公元)\s*)?(\d{1,4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*日?$/,
  );
  if (!match)
    return invalid(
      "請填寫完整出生年月日，例如民國 90 年 5 月 16 日或 2001-05-16。",
    );
  const [, era, rawYear, rawMonth, rawDay] = match;
  const yearValue = Number(rawYear);
  const assumedROC = !era && rawYear.length <= 3;
  if ((era === "民國" || assumedROC) && yearValue < 1)
    return invalid("沒有民國 0 年，請對照出生日期。");
  if ((era === "西元" || era === "公元") && rawYear.length !== 4)
    return invalid("西元年份請填寫完整 4 碼。");
  const year = era === "民國" || assumedROC ? yearValue + 1911 : yearValue;
  const month = Number(rawMonth);
  const day = Number(rawDay);
  if (
    year < 1000 ||
    year > 9999 ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > 31
  )
    return invalid("出生年月日超出可接受的日期範圍。");
  const date = new Date(Date.UTC(year, month - 1, day));
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  ) {
    return invalid("這個日期不存在，請確認月份、日數與閏年。");
  }
  const iso = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  const todayISO = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  if (iso > todayISO) return invalid("出生日期不能晚於今天，請對照原圖修正。");
  return {
    valid: true,
    iso,
    assumedROC,
    message: assumedROC
      ? `依國民身分證出生欄位將 ${rawYear} 年暫按民國解讀為 ${iso}，請對照原圖確認。`
      : `有效日期：${iso}。`,
  };
}

const FIELD_BOUNDARY =
  /(?:身分證字號|身份證字號|國民身分證統一編號|統一編號|出生日期|出生年月日|出生年月|發證日期|領補換日期|戶籍地址|住址|父親|母親|父|母|配偶|性別)\s*[:：]?/;
function normalizedLines(text: string): string[] {
  return text
    .normalize("NFKC")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}
function compactChinese(value: string): string {
  return /[a-z]/i.test(value)
    ? value.trim().replace(/\s+/g, " ")
    : value.replace(/\s/g, "");
}
function unique(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

/** Extract labeled owner fields. Unclear or conflicting candidates remain empty for review. */
export function parseFrontOCR(
  text: string,
  today = new Date(),
): Extraction<Pick<IdentityFields, "name" | "idNumber" | "birth">> {
  const lines = normalizedLines(text);
  const names: string[] = [];
  const ids: string[] = [];
  const idObservations: string[] = [];
  const births: string[] = [];
  const birthObservations: string[] = [];
  const notes: string[] = [];
  let ambiguousName = false;
  let ambiguousId = false;
  lines.forEach((line, index) => {
    const compact = line.replace(/\s/g, "");
    // OCR can merge two fields or two documents into one line. Do not pick the first.
    if ((compact.match(/姓名/g) || []).length > 1) ambiguousName = true;
    if (
      (
        compact.match(
          /身分證字號|身份證字號|身分證統一編號|統一編號|身分證號碼|身分證號/g,
        ) || []
      ).length > 1
    )
      ambiguousId = true;
    const nameMatch = line.match(/(?:^|[:：])\s*姓\s*名\s*[:：]?\s*(.*)$/);
    if (nameMatch) {
      const candidate = (nameMatch[1] || lines[index + 1] || "")
        .split(FIELD_BOUNDARY)[0]
        .replace(/^[:：]+/, "")
        .trim();
      if (
        candidate &&
        candidate.length <= 100 &&
        !/練習資料|非正式證件|姓名/.test(candidate)
      )
        names.push(compactChinese(candidate));
    }
    const idMatch = compact.match(
      /(?:國民)?(?:身分證字號|身份證字號|身分證統一編號|統一編號|身分證號碼|身分證號)[:：]?(.*)$/,
    );
    if (idMatch) {
      const raw = normalizeTaiwanId(idMatch[1] || lines[index + 1] || "").split(
        FIELD_BOUNDARY,
      )[0];
      idObservations.push(raw || "不明:空白字號欄位");
      // Keep masked/incomplete observations too, so they cannot disappear behind a valid candidate.
      if (/^[A-Z0-9]{8,14}$/.test(raw)) ids.push(raw);
    }
    const birthMatches = [
      ...compact.matchAll(/出生(?:日期|年月日|年月)?[:：]?(.*?)(?=出生|$)/g),
    ];
    birthMatches.forEach((birthMatch) => {
      const raw = (birthMatch[1] || lines[index + 1] || "")
        .split(/發證|領補換|統一編號|身分證|性別/)[0]
        .trim();
      // Validate the whole bounded field, never a valid prefix of a longer or double date.
      const parsed = parseBirthDate(raw, today);
      birthObservations.push(parsed.valid ? parsed.iso : `不明:${raw}`);
      if (parsed.valid) births.push(parsed.iso);
      else notes.push(parsed.message);
      if (parsed.assumedROC) notes.push(parsed.message);
    });
  });
  if (!ids.length && !idObservations.length) {
    // Unlabeled fallback is deliberately restricted to an isolated complete candidate.
    const candidates = lines.flatMap((line) =>
      [
        ...line
          .toUpperCase()
          .matchAll(/(?:^|[^A-Z0-9])([A-Z][A-Z0-9]{9})(?=$|[^A-Z0-9])/g),
      ].map((match) => match[1]),
    );
    ids.push(...candidates);
  }
  const pick = (values: string[], label: string): string => {
    const candidates = unique(values);
    if (candidates.length > 1) {
      notes.push(`${label}有多個不同候選，請對照原圖手動填寫。`);
      return "";
    }
    if (!candidates.length)
      notes.push(`尚未可靠擷取${label}，請對照原圖填寫。`);
    return candidates.length === 1 ? candidates[0] : "";
  };
  let birth = pick(births, "出生日期");
  if (unique(birthObservations).length > 1) {
    birth = "";
    notes.push("出生日期有多個不同候選，請對照原圖手動填寫。");
  }
  const name = pick(names, "姓名");
  const idNumber = pick(ids, "身分證字號");
  if (ambiguousName) notes.push("同一行出現多個姓名標籤，請對照原圖手動填寫。");
  if (ambiguousId) notes.push("同一行出現多個字號標籤，請對照原圖手動填寫。");
  if (unique(idObservations).length > 1) {
    ambiguousId = true;
    notes.push("身分證字號欄位有不同或不完整候選，請對照原圖手動填寫。");
  }
  return {
    fields: {
      name: ambiguousName ? "" : name,
      idNumber: ambiguousId ? "" : idNumber,
      birth,
    },
    notes: unique(notes),
  };
}

export function parseBackOCR(
  text: string,
): Extraction<Pick<IdentityFields, "address">> {
  const lines = normalizedLines(text).map((line) => line.replace(/\s/g, ""));
  const addresses: string[] = [];
  let ambiguousAddress = false;
  lines.forEach((line, index) => {
    if ((line.match(/戶籍地址|戶籍地|住址|地址/g) || []).length > 1) {
      ambiguousAddress = true;
      return;
    }
    const match = line.match(/(?:戶籍地址|戶籍地|住址|地址)[:：]?(.*)$/);
    if (!match) return;
    let address = match[1];
    // Continue only unlabelled address-looking lines, never spouse/parents or remarks.
    for (
      let next = index + 1;
      next < Math.min(index + 4, lines.length);
      next++
    ) {
      if (
        /^(?:父親|母親|配偶|役別|備註|出生|姓名|字號|日期|地址|住址|戶籍|練習資料|非正式證件|純文字|僅用於|此文件)|^[父母][:：]/.test(
          lines[next],
        )
      )
        break;
      if (!address || /[鄉鎮市區村里鄰路街段巷弄號樓室]/.test(lines[next]))
        address += lines[next];
      else break;
    }
    address = address
      .split(/備註|父親|母親|配偶|役別|練習資料|非正式證件/)[0]
      .replace(/^[:：]+/, "")
      .trim();
    if (address) addresses.push(address);
  });
  const candidates = unique(addresses);
  const notes = ambiguousAddress
    ? ["同一行出現多個地址標籤，請對照反面原圖手動填寫。"]
    : candidates.length === 0
      ? ["尚未可靠擷取戶籍地址，請對照反面原圖填寫。"]
      : candidates.length > 1
        ? ["戶籍地址有多個不同候選，請對照原圖手動填寫。"]
        : [];
  return {
    fields: {
      address:
        !ambiguousAddress && candidates.length === 1 ? candidates[0] : "",
    },
    notes,
  };
}

export function isHsinchuCityAddress(address: string): boolean {
  const value = address
    .normalize("NFKC")
    .replace(/\s/g, "")
    .replace(/臺/g, "台");
  // A prefix in the address text only; the ID's first letter is deliberately irrelevant.
  return /^(?:\d{3}(?:\d{2,3})?)?(?:中華民國)?(?:台灣省|台灣)?新竹市/.test(
    value,
  );
}

export function checkIdentity(
  fields: IdentityFields,
  front: boolean,
  back: boolean,
  applicant?: IdentityApplicant,
): IdentityCheck[] {
  const checks: IdentityCheck[] = [];
  if (!front)
    checks.push({
      field: "front",
      level: "error",
      message: "請提供正面圖片並完成文字辨識。",
    });
  if (!back)
    checks.push({
      field: "back",
      level: "error",
      message: "請提供反面圖片並完成文字辨識。",
    });
  if (!fields.name.trim())
    checks.push({
      field: "name",
      level: "error",
      message: "姓名尚未填寫，請對照正面原圖。",
    });
  const id = validateTaiwanId(fields.idNumber);
  checks.push({
    field: "idNumber",
    level: id.valid ? "info" : "error",
    message: id.message,
  });
  const birth = parseBirthDate(fields.birth);
  checks.push({
    field: "birth",
    level: birth.valid ? "info" : "error",
    message: birth.message,
  });
  if (!fields.address.trim())
    checks.push({
      field: "address",
      level: "error",
      message: "戶籍地址尚未填寫，請對照反面原圖。",
    });
  else
    checks.push({
      field: "address",
      level: isHsinchuCityAddress(fields.address) ? "info" : "warning",
      message: isHsinchuCityAddress(fields.address)
        ? "地址文字以新竹市開頭；未查詢戶政，也未確認目前設籍或設籍期間。"
        : "地址文字尚未符合「新竹市」開頭，請核對。新竹縣與新竹市不同；請保留真實地址，由承辦確認資格。",
    });
  if (applicant) {
    const compare = (value: string) =>
      value.normalize("NFKC").replace(/\s/g, "").replace(/臺/g, "台");
    const labels = {
      name: "姓名",
      birth: "出生日期",
      address: "戶籍地址",
      idNumber: "身分證字號",
    };
    (Object.keys(labels) as Array<keyof IdentityFields>).forEach((field) => {
      const previous = applicant[field];
      if (!previous || !fields[field]) return;
      if (
        field === "idNumber" &&
        !/^[A-Za-z][0-9]{9}$/.test(normalizeTaiwanId(previous))
      )
        return;
      const left =
        field === "birth" ? parseBirthDate(previous).iso || previous : previous;
      const right =
        field === "birth" ? birth.iso || fields[field] : fields[field];
      if (compare(left).toUpperCase() !== compare(right).toUpperCase())
        checks.push({
          field,
          level: "warning",
          message: `${labels[field]}與目前申請資料不同。確認帶入後將使用您核對的內容，請先確認來源與正確性。`,
        });
    });
  }
  return checks;
}

/** Create plain text practice sheets. These pixels must pass through the real OCR engine. */
export async function createIdentityPracticeImage(
  side: "front" | "back",
): Promise<File> {
  const canvas = document.createElement("canvas");
  canvas.width = 1800;
  canvas.height = 1150;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("此瀏覽器無法建立練習圖片。");
  context.fillStyle = "#fff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "#111";
  context.font = "bold 68px sans-serif";
  context.fillText("練習資料・非正式證件", 90, 130);
  context.font = "48px sans-serif";
  context.fillText(
    `純文字欄位辨識練習：${side === "front" ? "正面" : "反面"}`,
    90,
    245,
  );
  const lines =
    side === "front"
      ? ["姓名：林小竹", "身分證字號：A123456789", "出生日期：民國90年05月16日"]
      : ["戶籍地址：新竹市東區練習路100號"];
  context.font = "58px sans-serif";
  lines.forEach((line, index) => context.fillText(line, 90, 430 + index * 160));
  context.font = "40px sans-serif";
  context.fillText("僅用於練習文字辨識，不代表任何人的真實資料。", 90, 990);
  context.fillText("此文件不能作為身分、設籍或申請資格的證明。", 90, 1060);
  const blob = await new Promise<Blob>((resolve, reject) =>
    canvas.toBlob(
      (result) =>
        result ? resolve(result) : reject(new Error("無法建立練習圖片。")),
      "image/png",
    ),
  );
  return new File(
    [blob],
    side === "front" ? "練習資料-正面.png" : "練習資料-反面.png",
    { type: "image/png" },
  );
}

export async function rotateIdentityImage(previewUrl: string): Promise<string> {
  const image = new Image();
  image.src = previewUrl;
  await image.decode();
  const canvas = document.createElement("canvas");
  canvas.width = image.naturalHeight;
  canvas.height = image.naturalWidth;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("此瀏覽器無法旋轉圖片。");
  context.translate(canvas.width / 2, canvas.height / 2);
  context.rotate(Math.PI / 2);
  context.drawImage(image, -image.naturalWidth / 2, -image.naturalHeight / 2);
  return canvas.toDataURL("image/png");
}
