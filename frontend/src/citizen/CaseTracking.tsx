import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  Download,
  FileClock,
  RefreshCw,
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
import {
  caseLabels,
  documentLabels,
  scanLabels,
  taskLabels,
  type CaseData,
  type Page,
  type Scheme,
  type TaskData,
} from "../shared/types";
import PrecheckSummary from "../components/PrecheckSummary";

type Receipt = {
  receipt_id: string;
  submitted_at: string;
  task_revision?: number | null;
  file_version_ids: string[];
};
type Detail = CaseData & { files: UploadedFile[] };
const events: Record<string, string> = {
  CASE_CREATED: "建立申請草稿",
  DRAFT_SAVED: "更新申請草稿",
  CASE_DRAFT_SAVED: "儲存申請草稿",
  CASE_SUBMITTED: "申請已收件",
  REVIEW_STARTED: "承辦開始審查",
  CASE_REVIEW_STARTED: "承辦開始審查",
  TASK_CREATED: "新增補件事項",
  TASK_REVISED: "補件要求已更新",
  TASK_SUBMITTED: "補件已送出",
  TASK_ACCEPTED: "補件已接受",
  TASK_REOPENED: "請再次補正",
  TASK_CANCELLED: "補件事項已取消",
  DECISION_CREATED: "案件已完成核定",
  CASE_DECIDED: "案件已完成核定",
  CASE_CLOSED: "案件已結案",
  CASE_WITHDRAWN: "申請已撤回",
};
function date(value: string) {
  return new Date(value).toLocaleString("zh-TW", { hour12: false });
}

export default function CaseTracking({
  selectedId,
  supplementOnly,
  onEdit,
  onUnsavedChange,
}: {
  selectedId?: string;
  supplementOnly: boolean;
  onEdit: (id: string) => void;
  onUnsavedChange: (value: boolean) => void;
}) {
  const [cases, setCases] = useState<CaseData[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [record, setRecord] = useState<Detail | null>(null);
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [timeline, setTimeline] = useState<
    { id: string; event_type: string; occurred_at: string }[]
  >([]);
  const [scheme, setScheme] = useState<Scheme | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const detailRequest = useRef(0);
  const unfinishedTasks = useRef(new Set<string>());
  const reportTaskWork = useCallback((id: string, unfinished: boolean) => {
    if (unfinished) unfinishedTasks.current.add(id);
    else unfinishedTasks.current.delete(id);
    onUnsavedChange(unfinishedTasks.current.size > 0);
  }, [onUnsavedChange]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (unfinishedTasks.current.size) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => {
      window.removeEventListener("beforeunload", warn);
      onUnsavedChange(false);
    };
  }, [onUnsavedChange]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function open(id: string) {
    const request = ++detailRequest.current;
    setError("");
    setBusy(true);
    try {
      const detail = await api<Detail>(`/cases/${encodeURIComponent(id)}`);
      const [history, receiptPage, program] = await Promise.all([
        api<Page<{ id: string; event_type: string; occurred_at: string }>>(
          `/cases/${detail.id}/timeline?limit=100`,
        ),
        api<Page<Receipt>>(`/cases/${detail.id}/receipts?limit=100`),
        api<Scheme>(`/schemes/${detail.scheme_id}`),
      ]);
      if (mounted.current && request === detailRequest.current) {
        setRecord(detail);
        setTimeline(history.items);
        setReceipts(receiptPage.items);
        setScheme(program);
      }
    } catch (e) {
      if (mounted.current && request === detailRequest.current) {
        setRecord(null);
        setError(errorMessage(e));
      }
    } finally {
      if (mounted.current && request === detailRequest.current) setBusy(false);
    }
  }
  async function loadCases(more = false) {
    setBusy(true);
    setError("");
    try {
      const result = await api<Page<CaseData>>(
        `/cases?limit=30${more && cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      );
      if (mounted.current) {
        setCases((old) => (more ? [...old, ...result.items] : result.items));
        setCursor(result.next_cursor || null);
      }
    } catch (e) {
      if (mounted.current) setError(errorMessage(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  useEffect(() => {
    void loadCases();
    const match = location.pathname.match(/^\/(cases|tasks)\/([^/]+)$/);
    if (selectedId) void open(selectedId);
    else if (match?.[1] === "cases") void open(match[2]);
    else if (match?.[1] === "tasks")
      void api<TaskData>(`/tasks/${encodeURIComponent(match[2])}`)
        .then((t) => open(t.case_id))
        .catch((e) => {
          if (mounted.current) setError(errorMessage(e));
        });
  }, [selectedId]);
  return (
    <>
      <div className="page-heading">
        <p className="eyebrow">每一步，都有紀錄</p>
        <h1>
          {supplementOnly ? "補件清楚，申請更順。" : "你的申請，進度看得見。"}
        </h1>
        <p>收件與核定是不同階段，請依承辦列出的要求準備補件。</p>
      </div>
      {error && (
        <p role="alert" className="notice error">
          {error}
        </p>
      )}
      {record ? (
        <>
          <div className="button-row">
            <button
              className="text-btn"
              disabled={busy}
              onClick={() => {
                detailRequest.current += 1;
                setRecord(null);
                void loadCases();
              }}
            >
              <ArrowLeft size={16} /> 所有案件
            </button>
            <button
              className="btn small"
              disabled={busy}
              onClick={() => open(record.id)}
            >
              <RefreshCw size={16} /> 更新進度
            </button>
          </div>
          <section className="card case-overview">
            <div>
              <span className="badge">
                {caseLabels[record.status] || "處理中"}
              </span>
              <h2>{String(record.form_data.tool || "青年服務申請")}</h2>
              <p>
                {String(record.form_data.name || "")} · {scheme?.name}
              </p>
              <small className="muted">案件編號：{record.case_no}</small>
            </div>
            {record.allowed_actions.includes("save_draft") && record.scheme_id === "hsinchu-ai-grant-2026" && (
              <button className="btn primary" onClick={() => onEdit(record.id)}>
                繼續填寫 <ArrowRight size={16} />
              </button>
            )}
          </section>
          {record.status === "DRAFT" && record.scheme_id !== "hsinchu-ai-grant-2026" && (
            <p className="notice">此草稿屬於其他方案，無法使用青年 AI 工具補助表單續填。原資料仍保留；可於下方查看預檢摘要，或<a href="/precheck">開啟補助預檢</a>。</p>
          )}
          <PrecheckSummary
            key={`${record.id}-${record.version}`}
            caseId={record.id}
          />
          {record.decision && (
            <section className="card">
              <p className="eyebrow">核定結果</p>
              <h2>
                {record.decision.outcome === "APPROVED"
                  ? "核定通過"
                  : "不予補助"}
              </h2>
              <p>{record.decision.reason}</p>
              <small>{date(record.decision.decided_at)}</small>
              <p className="small muted">
                核定及結案紀錄不代表已完成撥款；實際匯款請依機關出納通知。
              </p>
            </section>
          )}
          <section className="case-tasks">
            <h2>補件與待辦事項</h2>
            {record.tasks?.length ? (
              record.tasks.map((task) => (
                <TaskCard
                  key={`${task.id}-${task.version}`}
                  task={task}
                  files={record.files.filter((f) => f.task_id === task.id)}
                  documentTypes={scheme?.document_types || []}
                  onDone={() => open(record.id)}
                  onUnsavedChange={reportTaskWork}
                />
              ))
            ) : (
              <div className="card empty-panel">
                <CheckCircle2 size={27} />
                <p>目前沒有需要補正的事項。</p>
              </div>
            )}
          </section>
          <section className="card">
            <h2>已附文件</h2>
            <p className="small muted">
              檔案通過安全檢查後才能下載；這不代表內容或資格審查通過。
            </p>
            <div className="case-file-list">
              {record.files.length ? (
                record.files.map((f) => (
                  <article key={f.file_version_id}>
                    <div>
                      <b>{documentLabels[f.document_type] || "證明文件"}</b>
                      <p>{f.file_name}</p>
                      <small>{scanLabels[f.scan_status] || "處理中"}</small>
                    </div>
                    {f.allowed_actions.includes("download_file") && (
                      <a
                        className="btn small"
                        href={`/api/v1/files/${f.file_id}/download?file_version_id=${f.file_version_id}`}
                      >
                        <Download size={15} /> 下載
                      </a>
                    )}
                  </article>
                ))
              ) : (
                <p>尚未附上文件。</p>
              )}
            </div>
          </section>
          <div className="tracking-columns">
            <section className="card">
              <h2>收件回執</h2>
              {receipts.length ? (
                receipts.map((r) => (
                  <article className="receipt-record" key={r.receipt_id}>
                    <FileClock size={20} />
                    <div>
                      <b>
                        {r.task_revision
                          ? `第 ${r.task_revision} 版補件收件`
                          : "申請收件"}
                      </b>
                      <p>{date(r.submitted_at)}</p>
                      <small className="muted">回執：{r.receipt_id}</small>
                      <p>
                        {r.file_version_ids.length} 份文件 · 原始收件紀錄已保留
                      </p>
                    </div>
                  </article>
                ))
              ) : (
                <p>正式提交後會產生收件回執。</p>
              )}
            </section>
            <section className="card">
              <h2>案件歷程</h2>
              <ol className="case-timeline">
                {timeline.map((t) => (
                  <li key={t.id}>
                    <b>{events[t.event_type] || "案件紀錄更新"}</b>
                    <small>{date(t.occurred_at)}</small>
                  </li>
                ))}
              </ol>
            </section>
          </div>
        </>
      ) : (
        <>
          <div className="card-heading">
            <h2>我的案件</h2>
            <button
              className="btn small"
              disabled={busy}
              onClick={() => loadCases()}
            >
              <RefreshCw size={16} /> 更新
            </button>
          </div>
          {busy && <p role="status">正在讀取案件…</p>}
          {cases.length ? (
            <div className="case-list">
              {cases.map((c) => (
                <button
                  className="card case-list-item"
                  key={c.id}
                  onClick={() => open(c.id)}
                >
                  <div>
                    <span className="badge">
                      {caseLabels[c.status] || "處理中"}
                    </span>
                    <h3>
                      {String(
                        c.form_data.tool ||
                          c.form_data.subject ||
                          "青年補助申請",
                      )}
                    </h3>
                    <p>{String(c.form_data.name || "尚未填寫姓名")}</p>
                    <small>{date(c.last_business_update_at)}</small>
                  </div>
                  <ArrowRight size={21} />
                </button>
              ))}
            </div>
          ) : (
            !busy && (
              <section className="card empty-panel">
                <FileClock size={32} />
                <h2>目前還沒有申請案件</h2>
                <p>完成申請後，可在這裡查看收件與補件紀錄。</p>
              </section>
            )
          )}
          {cursor && (
            <button
              className="btn full"
              disabled={busy}
              onClick={() => loadCases(true)}
            >
              載入更多案件
            </button>
          )}
        </>
      )}
    </>
  );
}

function TaskCard({
  task,
  files,
  documentTypes,
  onDone,
  onUnsavedChange,
}: {
  task: TaskData;
  files: UploadedFile[];
  documentTypes: string[];
  onDone: () => void;
  onUnsavedChange: (id: string, value: boolean) => void;
}) {
  const [kind, setKind] = useState(documentTypes[0] || "OTHER");
  const [selected, setSelected] = useState<string[]>([]);
  const [uploaded, setUploaded] = useState<UploadedFile[]>(files);
  const [statement, setStatement] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const unfinished = busy || pending || !!statement.trim() || selected.length > 0;
  useEffect(() => {
    onUnsavedChange(task.id, unfinished);
    return () => onUnsavedChange(task.id, false);
  }, [task.id, unfinished, onUnsavedChange]);
  const lock = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const serverFiles = JSON.stringify(files);
  useEffect(() => {
    setUploaded(files);
    const available = new Set(files.map((file) => file.file_version_id));
    setSelected((old) => old.filter((id) => available.has(id)));
  }, [serverFiles]);
  const operation = useRef<{
    key: string;
    ids: string[];
    statement: string;
  } | null>(null);
  const canSubmit = task.allowed_actions.includes("submit_task");
  async function act(work: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (e) {
      if (
        e instanceof ApiError &&
        e.status < 500 &&
        e.status !== 401 &&
        e.code !== "OPERATION_RUNNING"
      ) {
        operation.current = null;
        setPending(false);
      }
      if (mounted.current) setError(errorMessage(e));
    } finally {
      lock.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <article className="card task-card">
      <div className="card-heading">
        <div>
          <span className="badge">{taskLabels[task.status] || "處理中"}</span>
          <h3>{task.title}</h3>
        </div>
        <small>
          {task.is_overdue ? "已超過期限" : "補件期限"}
          <br />
          {date(task.due_at)}
        </small>
      </div>
      <dl>
        <dt>需要補充的內容</dt>
        <dd>{task.requirement}</dd>
        <dt>如何確認完成</dt>
        <dd>{task.acceptance_criteria}</dd>
      </dl>
      {canSubmit && (
        <>
          <label className="field">
            <span>補充說明</span>
            <textarea
              value={statement}
              disabled={busy || pending}
              maxLength={2000}
              onChange={(e) => setStatement(e.target.value)}
              placeholder="請說明修正內容，承辦會連同原申請一起核對。"
            />
          </label>
          {task.allowed_actions.includes("upload_file") && (
            <div className="task-upload">
              <label className="field">
                <span>補件文件類別</span>
                <select
                  value={kind}
                  disabled={busy || pending}
                  onChange={(e) => setKind(e.target.value)}
                >
                  {documentTypes.map((t) => (
                    <option key={t} value={t}>
                      {documentLabels[t] || "證明文件"}
                    </option>
                  ))}
                </select>
              </label>
              <label className="btn">
                <Upload size={16} /> 上傳補件文件
                <input
                  type="file"
                  aria-label={`上傳補件文件：${task.title}`}
                  accept="image/jpeg,image/png,application/pdf"
                  hidden
                  disabled={busy || pending}
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    e.target.value = "";
                    if (file)
                      void act(async () => {
                        const result = await uploadFile(
                          task.case_id,
                          kind,
                          file,
                          task.id,
                        );
                        if (mounted.current) {
                          setUploaded((old) => [...old, result]);
                          setSelected((old) => [...old, result.file_version_id]);
                        }
                      });
                  }}
                />
              </label>
            </div>
          )}
          {uploaded.map((f) => (
            <label className="check-row" key={f.file_version_id}>
              <input
                type="checkbox"
                disabled={busy || pending}
                checked={selected.includes(f.file_version_id)}
                onChange={(e) =>
                  setSelected((old) =>
                    e.target.checked
                      ? [...old, f.file_version_id]
                      : old.filter((id) => id !== f.file_version_id),
                  )
                }
              />
              <span>
                {f.file_name}
                <small>
                  {documentLabels[f.document_type] || "證明文件"} ·{" "}
                  {scanLabels[f.scan_status] || "處理中"}
                </small>
              </span>
            </label>
          ))}
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
          {pending && (
            <p className="notice warning">
              若連線中斷，請使用此按鈕以原內容確認送件結果，或更新案件進度。
            </p>
          )}
          <button
            className="btn primary"
            disabled={
              busy ||
              selected.length > 10 ||
              (!selected.length && !statement.trim())
            }
            onClick={() =>
              act(async () => {
                if (!operation.current)
                  operation.current = {
                    key: operationKey(),
                    ids: [...selected],
                    statement,
                  };
                setPending(true);
                const current = operation.current;
                await api(`/tasks/${task.id}/submissions`, {
                  method: "POST",
                  etag: task.etag,
                  idempotencyKey: current.key,
                  json: {
                    task_revision: task.task_revision,
                    file_version_ids: current.ids,
                    statement: current.statement,
                  },
                });
                operation.current = null;
                setPending(false);
                if (mounted.current) onDone();
              })
            }
          >
            {busy ? "送出中…" : pending ? "重試原補件送出" : "確認送出補件"}
          </button>
          <p className="small muted">
            送出後會產生補件回執，原申請紀錄不會被覆寫。
          </p>
        </>
      )}
    </article>
  );
}
