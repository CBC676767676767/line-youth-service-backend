import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  FileCheck2,
  Save,
  ScanLine,
  ShieldCheck,
  Upload,
} from "lucide-react";
import {
  api,
  ApiError,
  errorMessage,
  operationKey,
  uploadFile,
  type UploadedFile,
} from "../shared/api";
import type { CaseData, Me, Page } from "../shared/types";
import { documentLabels, scanLabels } from "../shared/types";
import {
  checks,
  estimate,
  money,
  OFFICIAL,
  requiredDocs,
  type Doc,
  type Form,
} from "../model";
import IdentityOCR from "../components/IdentityOCR";
import OcrLab from "../components/OcrLab";
import { isHsinchuCityAddress, type IdentityResult } from "../identity";

const SCHEME = "hsinchu-ai-grant-2026";
const typeById: Record<string, string> = {
  idFront: "ID_FRONT",
  idBack: "ID_BACK",
  receipt: "RECEIPT",
  payment: "PAYMENT_PROOF",
  bank: "BANK_ACCOUNT",
  affidavit: "AFFIDAVIT",
  special: "SPECIAL_STATUS",
  relationship: "RELATIONSHIP",
};
const steps = ["申請資料", "證明文件", "安全核對", "確認送件"];
export function emptyForm(email = ""): Form {
  return {
    name: "",
    email,
    birth: "",
    city: "新竹市",
    tool: "",
    channel: "official",
    plan: "monthly",
    purchaseDate: "",
    periodEnd: "",
    amount: "",
    requested: "",
    receiptName: "",
    receiptEmail: "",
    payer: "self",
    special: false,
    bankName: "",
    receiptAmount: "",
    phone: "",
    address: "",
    identityHint: "",
    company: "",
    origin: "",
    currency: "TWD",
    originalAmount: "",
    paymentMethod: "card",
    bankType: "taiwan",
    category: "通用型",
  };
}
function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

export default function ApplicationForm({
  me,
  caseId,
  onSubmitted,
  onSafety,
}: {
  me: Me;
  caseId?: string;
  onSubmitted: (id: string) => void;
  onSafety: () => void;
}) {
  const [record, setRecord] = useState<CaseData | null>(null);
  const [form, setForm] = useState<Form>(() =>
    emptyForm(me.account.email || ""),
  );
  const [files, setFiles] = useState<Record<string, UploadedFile>>({});
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showIdentity, setShowIdentity] = useState(false);
  const [showReceipt, setShowReceipt] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [safety, setSafety] = useState([false, false, false]);
  const [dirty, setDirty] = useState(false);
  const [otherDrafts, setOtherDrafts] = useState<CaseData[]>([]);
  const createKey = useRef(operationKey());
  const pendingSubmit = useRef<{
    id: string;
    etag: string;
    key: string;
    ids: string[];
  } | null>(null);
  const mounted = useRef(true);
  const lock = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  function adopt(next: CaseData & { files?: UploadedFile[] }) {
    setRecord(next);
    setForm({
      ...emptyForm(me.account.email || ""),
      ...next.form_data,
    } as Form);
    const saved: Record<string, UploadedFile> = {};
    for (const file of next.files || [])
      if (!file.task_id) saved[file.document_type] = file;
    setFiles(saved);
    setDirty(false);
    setConfirmed(false);
    pendingSubmit.current = null;
  }
  async function reload() {
    if (!record) return;
    const latest = await api<CaseData & { files: UploadedFile[] }>(
      `/cases/${record.id}`,
    );
    adopt(latest);
    setNotice("已讀取伺服器上的最新案件。");
  }
  useEffect(() => {
    let active = true;
    async function load() {
      try {
        if (caseId) {
          const result = await api<CaseData & { files: UploadedFile[] }>(
            `/cases/${caseId}`,
          );
          if (active) adopt(result);
        } else {
          const result = await api<Page<CaseData>>(
            "/cases?status=DRAFT&limit=100",
          );
          if (active)
            setOtherDrafts(result.items.filter((c) => c.scheme_id === SCHEME));
        }
      } catch (e) {
        if (active) setError(errorMessage(e));
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [caseId]);
  async function act(work: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await work();
    } catch (e) {
      if (
        e instanceof ApiError &&
        e.status < 500 &&
        e.code !== "OPERATION_RUNNING"
      )
        pendingSubmit.current = null;
      if (mounted.current) setError(errorMessage(e));
    } finally {
      lock.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  function update<K extends keyof Form>(key: K, value: Form[K]) {
    if (pendingSubmit.current) return;
    setForm((current) => ({
      ...current,
      [key]: value,
      ...(["name", "birth", "address"].includes(key)
        ? { identityHint: "" }
        : {}),
    }));
    setDirty(true);
    setConfirmed(false);
  }
  async function save() {
    if (!record) throw new Error("Case missing");
    const next = await api<CaseData>(`/cases/${record.id}`, {
      method: "PATCH",
      etag: record.etag,
      json: { form_data: form },
    });
    if (mounted.current) {
      setRecord(next);
      setDirty(false);
      setNotice("草稿已儲存，可以稍後登入繼續。");
    }
    return next;
  }
  const locked =
    busy ||
    !!pendingSubmit.current ||
    (record != null && record.status !== "DRAFT");
  function input(key: keyof Form, type = "text", placeholder = "") {
    return (
      <input
        type={type}
        value={String(form[key])}
        placeholder={placeholder}
        disabled={locked}
        maxLength={type === "email" ? 254 : 200}
        onChange={(e) => update(key, e.target.value as never)}
      />
    );
  }
  function select(key: keyof Form, options: [string, string][]) {
    return (
      <select
        value={String(form[key])}
        disabled={locked}
        onChange={(e) => update(key, e.target.value as never)}
      >
        {options.map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
    );
  }
  function confirmIdentity(result: IdentityResult) {
    if (locked) return;
    setForm((f) => ({
      ...f,
      name: result.fields.name,
      birth: result.fields.birth,
      address: result.fields.address,
      city: isHsinchuCityAddress(result.fields.address)
        ? "新竹市"
        : /新竹縣/.test(result.fields.address)
          ? "新竹縣"
          : "其他",
      identityHint: `${result.fields.idNumber.slice(0, 2)}••••••${result.fields.idNumber.slice(-2)}`,
    }));
    setDirty(true);
    setConfirmed(false);
    setNotice("已帶入核對後的欄位。請在文件清單選擇原始證件檔案上傳。");
  }
  const docs: Doc[] = requiredDocs(form).map((d) => ({
    ...d,
    file: files[typeById[d.id]]?.file_name,
  }));
  const findings = checks(form, docs);
  const missing = docs.filter((d) => !d.file);
  if (loading) return <p role="status">正在讀取申請資料…</p>;
  if (!record)
    return (
      <section className="card start-application">
        <p className="eyebrow">青年 AI 工具補助</p>
        <h1>從一份清楚的申請開始</h1>
        <p>資料可先儲存為草稿，備齊證件與憑證後再送出。</p>
        {error && (
          <p role="alert" className="notice error">
            {error}
          </p>
        )}
        {otherDrafts.length > 0 && (
          <div className="draft-options">
            <h3>繼續既有草稿</h3>
            {otherDrafts.map((c) => (
              <button
                className="btn full"
                disabled={busy}
                key={c.id}
                onClick={() =>
                  act(async () => {
                    adopt(await api(`/cases/${c.id}`));
                  })
                }
              >
                {String(c.form_data.name || "未填姓名")} ·{" "}
                {new Date(c.last_business_update_at).toLocaleDateString(
                  "zh-TW",
                )}{" "}
                <ArrowRight size={16} />
              </button>
            ))}
          </div>
        )}
        <button
          className="btn primary"
          disabled={busy}
          onClick={() =>
            act(async () => {
              const created = await api<CaseData>("/cases", {
                method: "POST",
                json: { scheme_id: SCHEME },
                idempotencyKey: createKey.current,
              });
              if (mounted.current) adopt(created);
            })
          }
        >
          {busy ? "建立中…" : "建立新的申請"} <ArrowRight size={17} />
        </button>
      </section>
    );
  if (record.status !== "DRAFT")
    return (
      <section className="card">
        <h2>此案件已送出</h2>
        <p>原始申請與回執已保留。若需補正，請依承辦建立的補件事項處理。</p>
        <button className="btn primary" onClick={() => onSubmitted(record.id)}>
          查看案件與補件
        </button>
      </section>
    );
  return (
    <>
      <div className="page-heading">
        <p className="eyebrow">青年 AI 工具補助</p>
        <h1>讓申請，一步一步完成。</h1>
        <p>先核對、再送件。需要補充的地方，系統會提醒你。</p>
      </div>
      <ol className="application-steps">
        {steps.map((label, i) => (
          <li
            key={label}
            className={step === i ? "active" : i < step ? "done" : ""}
          >
            <button disabled={busy} onClick={() => setStep(i)}>
              <b>{i + 1}</b>
              <span>{label}</span>
            </button>
          </li>
        ))}
      </ol>
      {error && (
        <div role="alert" className="notice error">
          <p>{error}</p>
          <button
            className="btn small"
            disabled={busy}
            onClick={() => act(reload)}
          >
            重新讀取伺服器案件
          </button>
        </div>
      )}
      {notice && (
        <p role="status" className="notice success">
          {notice}
        </p>
      )}
      {pendingSubmit.current && (
        <p className="notice warning">
          正在確認送件結果。請使用原送件操作重試，或重新讀取案件；確認前不修改本次提交內容。
        </p>
      )}
      <section className="card application-card">
        {step === 0 && (
          <>
            <div className="card-heading">
              <div>
                <h2>申請人與訂閱資料</h2>
                <p>姓名、生日與戶籍地址請對照證件填寫。</p>
              </div>
              <button
                className="btn small"
                disabled={busy}
                onClick={() => {
                  setStep(1);
                  setShowIdentity(true);
                }}
              >
                <ScanLine size={17} /> 讀取身分證
              </button>
            </div>
            <div className="form-grid">
              <Field label="姓名">{input("name")}</Field>
              <Field label="電子信箱">{input("email", "email")}</Field>
              <Field label="出生日期">{input("birth", "date")}</Field>
              <Field label="聯絡電話">{input("phone", "tel")}</Field>
              <Field label="戶籍縣市">
                {select("city", [
                  ["新竹市", "新竹市"],
                  ["新竹縣", "新竹縣"],
                  ["其他", "其他縣市"],
                ])}
              </Field>
              <Field label="戶籍地址">{input("address")}</Field>
              <Field label="訂閱 AI 工具">
                {input("tool", "text", "例如 ChatGPT、Canva AI")}
              </Field>
              <Field label="工具提供公司">{input("company")}</Field>
              <Field label="購買管道">
                {select("channel", [
                  ["official", "官方網站"],
                  ["reseller", "代購或集合式平台"],
                  ["unknown", "需要承辦協助確認"],
                ])}
              </Field>
              <Field label="訂閱方式">
                {select("plan", [
                  ["monthly", "月訂閱"],
                  ["annual", "年訂閱"],
                  ["credits", "預付額度／點數"],
                ])}
              </Field>
              <Field label="購買日期">{input("purchaseDate", "date")}</Field>
              <Field label="訂閱期間結束日">{input("periodEnd", "date")}</Field>
              <Field label="臺幣實付金額">
                {input("amount", "text", "例如 680")}
              </Field>
              <Field label="申請補助金額">
                {input("requested", "text", "例如 340")}
              </Field>
              <Field label="付款方式">
                {select("paymentMethod", [
                  ["card", "信用卡"],
                  ["telecom", "電信帳單"],
                  ["wallet", "電子支付"],
                  ["other", "其他方式"],
                ])}
              </Field>
              <Field label="付款人">
                {select("payer", [
                  ["self", "申請人本人"],
                  ["relative", "親屬代付"],
                ])}
              </Field>
              <Field label="收據姓名">{input("receiptName")}</Field>
              <Field label="收據電子信箱">
                {input("receiptEmail", "email")}
              </Field>
              <Field label="收據對應臺幣金額">{input("receiptAmount")}</Field>
              <Field label="存摺戶名">{input("bankName")}</Field>
              <Field label="原始幣別">
                {select("currency", [
                  ["TWD", "新臺幣"],
                  ["USD", "美元"],
                  ["EUR", "歐元"],
                  ["JPY", "日圓"],
                  ["其他", "其他幣別"],
                ])}
              </Field>
              <Field label="原幣金額">{input("originalAmount")}</Field>
            </div>
            <label className="check-row">
              <input
                type="checkbox"
                checked={form.special}
                disabled={locked}
                onChange={(e) => update("special", e.target.checked)}
              />
              <span>申請特定身分補助比例，並將提供資格證明</span>
            </label>
            <div className="estimate-box">
              <div>
                <small>依填寫金額試算</small>
                <strong>NT$ {money(estimate(form))}</strong>
                <p>
                  依 {form.special ? "90%" : "50%"}{" "}
                  比例與上限估算，仍以承辦核定為準。
                </p>
              </div>
              <button
                className="btn"
                disabled={locked || !Number(form.amount)}
                onClick={() => update("requested", String(estimate(form)))}
              >
                帶入試算金額
              </button>
            </div>
          </>
        )}
        {step === 1 && (
          <>
            <div className="card-heading">
              <div>
                <h2>備妥證件與付款證明</h2>
                <p>
                  每類上傳一份 JPEG、PNG 或 PDF，最大 20
                  MiB。同一類有多張帳單或證明，請先合併成一份 PDF。
                </p>
              </div>
            </div>
            <div className="button-row">
              <button
                className="btn"
                onClick={() => setShowIdentity(!showIdentity)}
              >
                <ScanLine size={17} />{" "}
                {showIdentity ? "收合證件辨識" : "辨識身分證正反面"}
              </button>
              <button
                className="btn"
                onClick={() => setShowReceipt(!showReceipt)}
              >
                辨識收據文字
              </button>
            </div>
            {showIdentity && (
              <IdentityOCR
                applicant={{
                  name: form.name,
                  birth: form.birth,
                  address: form.address,
                }}
                disabled={locked}
                onConfirm={confirmIdentity}
              />
            )}
            {showReceipt && <OcrLab />}
            {form.identityHint && (
              <p className="notice">
                <CheckCircle2 size={17} /> 證件欄位已核對並帶入，字號：
                {form.identityHint}。仍需上傳原始證件，交由承辦驗核。
              </p>
            )}
            <div className="document-list">
              {requiredDocs(form).map((d) => {
                const kind = typeById[d.id],
                  file = files[kind];
                return (
                  <article className="document-upload" key={d.id}>
                    <span className="document-icon">
                      <FileCheck2 size={22} />
                    </span>
                    <div>
                      <h3>{d.title}</h3>
                      <p>{d.hint}</p>
                      {file && (
                        <p className="uploaded-name">
                          {file.file_name} ·{" "}
                          {scanLabels[file.scan_status] || "處理中"}
                        </p>
                      )}
                    </div>
                    <label className={`btn small ${locked ? "disabled" : ""}`}>
                      <Upload size={16} />
                      {file ? "更換文件" : "上傳文件"}
                      <input
                        aria-label={`上傳${d.title}`}
                        type="file"
                        hidden
                        accept="image/jpeg,image/png,application/pdf"
                        disabled={locked}
                        onChange={(e) => {
                          const chosen = e.target.files?.[0];
                          e.target.value = "";
                          if (chosen)
                            void act(async () => {
                              const result = await uploadFile(
                                record.id,
                                kind,
                                chosen,
                              );
                              if (mounted.current) {
                                setFiles((old) => ({ ...old, [kind]: result }));
                                setConfirmed(false);
                                setNotice(
                                  `${documentLabels[kind]}已上傳至案件，等待安全檢查。`,
                                );
                              }
                            });
                        }}
                      />
                    </label>
                  </article>
                );
              })}
            </div>
            <p className="small muted">
              點選上傳後，文件會儲存於此服務的私有附件區。文字辨識、檔案安全檢查與人工審查是不同步驟。
            </p>
          </>
        )}
        {step === 2 && (
          <>
            <p className="eyebrow">把安全習慣帶在身邊</p>
            <h2>使用 AI 前，先停一下、想一下。</h2>
            <p>準備申請時，也替自己的資料多做一道保護。</p>
            {[
              [
                "不把個資直接貼進 AI",
                "身分證、完整帳號、密碼與申請文件，應先移除不必要的個人資訊。",
              ],
              [
                "確認工具與付款來源",
                "優先查核官方網址、隱私政策與帳單。出現在可補助清單，不等於已取得資安認證。",
              ],
              [
                "重要結果再查證",
                "AI 可能看錯文字或生成不正確資訊，涉及金額、身分與申請條件時，請對照原始文件。",
              ],
            ].map(([title, text], i) => (
              <label className="safety-check" key={title}>
                <input
                  type="checkbox"
                  checked={safety[i]}
                  onChange={(e) =>
                    setSafety((old) =>
                      old.map((x, j) => (j === i ? e.target.checked : x)),
                    )
                  }
                />
                <span>
                  <b>{title}</b>
                  <p>{text}</p>
                </span>
              </label>
            ))}
            <button
              className="text-btn"
              onClick={() =>
                act(async () => {
                  await save();
                  onSafety();
                })
              }
            >
              <ShieldCheck size={17} /> 儲存草稿並前往 AI 安全學堂
            </button>
          </>
        )}
        {step === 3 && (
          <>
            <h2>確認內容，保留每一份依據</h2>
            <p>
              以下為文件與欄位預檢，提醒你先修正可能的差異。正式資格、核銷與核定仍由承辦確認。
            </p>
            <div className="precheck-results">
              {findings.map((f) => (
                <article key={f.id} className={`finding-row ${f.severity}`}>
                  <span>{f.severity === "pass" ? "✓" : "!"}</span>
                  <div>
                    <b>{f.title}</b>
                    <p>{f.detail}</p>
                  </div>
                </article>
              ))}
            </div>
            <div className="submission-summary">
              <b>{form.name || "尚未填姓名"}</b>
              <span>
                {form.tool || "尚未填工具"} · 申請 NT$ {money(form.requested)}
              </span>
              <span>
                {docs.filter((d) => d.file).length} / {docs.length}{" "}
                類必要文件已上傳
              </span>
            </div>
            <label className="check-row">
              <input
                type="checkbox"
                checked={confirmed}
                disabled={busy}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              <span>
                我已核對原始文件與申請內容，了解本次提交會建立不可覆寫的收件紀錄。
                <small>請備妥親筆簽名切結書；此勾選不取代書面簽署。</small>
              </span>
            </label>
            {missing.length > 0 && (
              <p className="notice warning">
                尚缺：{missing.map((d) => d.title).join("、")}。
              </p>
            )}
          </>
        )}
      </section>
      <div className="application-actions">
        <button
          className="btn"
          disabled={busy || !!pendingSubmit.current}
          onClick={() =>
            act(async () => {
              await save();
            })
          }
        >
          <Save size={16} /> 儲存草稿
        </button>
        <div className="button-row">
          {step > 0 && (
            <button
              className="btn"
              disabled={busy}
              onClick={() => setStep(step - 1)}
            >
              <ArrowLeft size={16} /> 上一步
            </button>
          )}
          {step < 3 ? (
            <button
              className="btn primary"
              disabled={busy}
              onClick={() => setStep(step + 1)}
            >
              下一步 <ArrowRight size={16} />
            </button>
          ) : (
            <button
              className="btn primary"
              disabled={busy || !confirmed || missing.length > 0}
              onClick={() =>
                act(async () => {
                  if (!pendingSubmit.current) {
                    const saved = await save();
                    pendingSubmit.current = {
                      id: saved.id,
                      etag: saved.etag,
                      key: operationKey(),
                      ids: requiredDocs(form).map(
                        (d) => files[typeById[d.id]].file_version_id,
                      ),
                    };
                  }
                  const pending = pendingSubmit.current;
                  await api(`/cases/${pending.id}/submit`, {
                    method: "POST",
                    etag: pending.etag,
                    idempotencyKey: pending.key,
                    json: { file_version_ids: pending.ids },
                  });
                  pendingSubmit.current = null;
                  setDirty(false);
                  onSubmitted(record.id);
                })
              }
            >
              {busy
                ? "送件處理中…"
                : pendingSubmit.current
                  ? "確認並重試原送件"
                  : "確認送出申請"}{" "}
              <ArrowRight size={16} />
            </button>
          )}
        </div>
      </div>
      <p className="small muted">
        資料檢核依公開公告整理；
        <a href={OFFICIAL} target="_blank" rel="noreferrer">
          完整資格與應備文件請見市府公告 ↗
        </a>
      </p>
    </>
  );
}
