import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import {
  ArrowLeft,
  ArrowRight,
  Check,
  ClipboardCheck,
  Download,
  FileText,
  FolderOpen,
  Leaf,
  LockKeyhole,
  LogOut,
  Plus,
  RefreshCw,
  ShieldCheck,
  Users,
  X,
} from 'lucide-react';
import { api, ApiError, errorMessage, setCsrf } from '../shared/api';
import {
  caseLabels,
  documentLabels,
  scanLabels,
  taskLabels,
  type CaseData,
  type Me,
  type Page,
  type Scheme,
  type TaskData,
} from '../shared/types';

const staffRoles = ['reviewer', 'supervisor', 'auditor', 'admin'];
const caseRoles = ['reviewer', 'supervisor', 'auditor'];
const roleLabels: Record<string, string> = {
  reviewer: '承辦人員',
  supervisor: '業務主管',
  auditor: '稽核人員',
  admin: '帳號管理員',
  applicant: '申請人',
};
const resultLabels: Record<string, string> = {
  PENDING: '待確認',
  QUESTION: '有疑義',
  PASS: '符合',
  FAIL: '不符合',
};
const criterionLabels: Record<string, string> = {
  eligibility: '申請資格',
  documents: '文件完整性',
  expense: '購買項目與申請金額',
  identity: '本人、付款與收款資料',
  safety: '重複補助與其他人工查核',
};
type Evidence = {
  rule_version_id: string;
  case_revision_id?: string | null;
  file_version_id?: string | null;
  replaces_file_version_id?: string | null;
  page_no?: number | null;
  field_path?: string | null;
  note?: string | null;
};
type Review = {
  id: string;
  criterion_code: string;
  result: string;
  version: number;
  etag: string;
  evidence_refs: Evidence[];
  internal_note?: string | null;
  public_reason?: string | null;
};
type FileInfo = {
  file_id: string;
  file_version_id: string;
  file_name: string;
  document_type: string;
  scan_status: string;
  task_id: string | null;
  size_bytes: number;
  allowed_actions?: string[];
};
type Revision = {
  id: string;
  revision_no: number;
  form_snapshot: Record<string, unknown>;
  submitted_at: string;
};
type Submission = {
  id: string;
  task_id: string | null;
  task_revision: number | null;
  submitted_at: string;
  file_version_ids: string[];
  statement?: string | null;
};
type Decision = {
  id: string;
  outcome: string;
  reason: string;
  decided_at: string;
  supersedes_id?: string | null;
};
type Detail = CaseData & {
  files?: FileInfo[];
  revisions: Revision[];
  submissions: Submission[];
  decisions: Decision[];
};
type SchemeInfo = Scheme & { criterion_labels?: Record<string, string> };
type FormSchema = { form_schema: { properties?: Record<string, { title?: string }> } };
type Bundle = { detail: Detail; scheme: SchemeInfo; schema: FormSchema; reviews: Review[] };
type AccountInfo = {
  id: string;
  email: string;
  status: string;
  etag: string;
  grants: {
    id: string;
    role: string;
    scope_type: string;
    scope_id: string | null;
    expires_at: string | null;
  }[];
};
type Run = (operation: () => Promise<unknown>, success: string) => Promise<boolean>;

function date(value?: string | null) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-TW', { hour12: false });
}
function textValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '未填寫';
  if (typeof value === 'boolean') return value ? '是' : '否';
  return typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
}
function futureDate() {
  const d = new Date(Date.now() + 7 * 86400000);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
function Badge({ children, tone = '' }: { children: ReactNode; tone?: string }) {
  return <span className={`ad-badge ${tone}`}>{children}</span>;
}
function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="ad-field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
function Notice({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return (
    <div className={`ad-notice ${error ? 'ad-error' : ''}`} role={error ? 'alert' : 'status'}>
      {children}
    </div>
  );
}
function useStableKey() {
  const previous = useRef({ body: '', key: '' });
  return (body: unknown) => {
    const serialized = JSON.stringify(body);
    if (serialized !== previous.current.body || !previous.current.key) {
      previous.current = { body: serialized, key: crypto.randomUUID() };
    }
    return previous.current.key;
  };
}

export default function AdminApp() {
  const [me, setMe] = useState<Me | null>(null);
  const [ready, setReady] = useState(false);
  const [message, setMessage] = useState('');
  const [checking, setChecking] = useState(false);
  const current = useRef<Me | null>(null);
  const generation = useRef(0);

  const identify = useCallback((identity: Me) => {
    generation.current++;
    current.current = identity;
    setCsrf(identity.csrf_token);
    setMe(identity);
    setMessage('');
    setReady(true);
  }, []);

  useEffect(() => {
    let active = true;
    const refreshIdentity = async () => {
      const turn = ++generation.current;
      setChecking(true);
      try {
        const identity = await api<Me>('/me');
        if (active && turn === generation.current) identify(identity);
      } catch (error) {
        if (active && !(error instanceof ApiError && error.status === 401))
          setMessage(errorMessage(error));
      } finally {
        if (active) {
          setReady(true);
          setChecking(false);
        }
      }
    };
    const expired = () => {
      generation.current++;
      if (current.current) setMessage('登入已失效，請重新登入。先前案件資料已清除。');
      current.current = null;
      setCsrf();
      setMe(null);
      setReady(true);
    };
    const focus = () => {
      if (current.current) void refreshIdentity();
    };
    window.addEventListener('youth:session-expired', expired);
    window.addEventListener('focus', focus);
    void refreshIdentity();
    return () => {
      active = false;
      window.removeEventListener('youth:session-expired', expired);
      window.removeEventListener('focus', focus);
    };
  }, [identify]);

  async function logout() {
    try {
      await api('/auth/logout', { method: 'POST' });
      generation.current++;
      current.current = null;
      setCsrf();
      setMe(null);
      setMessage('已登出，工作資料已清除。');
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }

  if (!ready)
    return (
      <div className="ad-root ad-auth-shell">
        <p role="status">正在確認登入狀態…</p>
      </div>
    );
  if (!me)
    return (
      <div className="ad-root">
        <Login onLogin={identify} message={message} />
      </div>
    );
  if (!me.roles.some((role) => staffRoles.includes(role))) {
    return (
      <div className="ad-root ad-auth-shell">
        <section className="ad-login-card">
          <ShieldCheck size={35} />
          <h1>此入口限工作帳號使用</h1>
          <p>目前登入沒有承辦、主管、稽核或帳號管理權限。</p>
          {message && <Notice error>{message}</Notice>}
          <button className="ad-btn ad-primary" onClick={() => void logout()}>
            登出並使用工作帳號
          </button>
          <a href="/" className="ad-text-link">
            返回民眾服務
          </a>
        </section>
      </div>
    );
  }
  const identityKey = JSON.stringify([me.account.id, me.roles.slice().sort(), me.permissions]);
  return (
    <div className="ad-root">
      <Workspace key={identityKey} me={me} checking={checking} onLogout={logout} />
    </div>
  );
}

function Login({ onLogin, message }: { onLogin: (me: Me) => void; message: string }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [challenge, setChallenge] = useState<{
    mfa_challenge_id: string;
    expires_at: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      if (!challenge) {
        const result = await api<{ mfa_challenge_id: string; expires_at: string }>(
          '/auth/staff/session',
          {
            method: 'POST',
            json: { identifier: email, password },
          },
        );
        setPassword('');
        setCode('');
        setChallenge(result);
      } else {
        const identity = await api<Me>('/auth/staff/mfa', {
          method: 'POST',
          json: { mfa_challenge_id: challenge.mfa_challenge_id, code },
        });
        setPassword('');
        setCode('');
        onLogin(identity);
      }
    } catch (cause) {
      setError(errorMessage(cause));
      setPassword('');
      setCode('');
      if (cause instanceof ApiError && cause.code === 'MFA_EXPIRED') setChallenge(null);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="ad-login-layout">
      <section className="ad-login-story">
        <a className="ad-brand" href="/">
          <span>
            <Leaf size={25} />
          </span>
          <strong>
            竹青通<small>承辦作業平台</small>
          </strong>
        </a>
        <div>
          <span className="ad-eyebrow">把服務接好，讓青年走得更順</span>
          <h1>
            每一份申請，
            <br />
            都有清楚的處理依據。
          </h1>
          <p>
            核對文件、追蹤補件、留下人工審查紀錄。
            <br />
            以授權的工作帳號，接續下一步。
          </p>
          <div className="ad-login-points">
            <span>
              <ClipboardCheck size={18} /> 可追溯的審查
            </span>
            <span>
              <ShieldCheck size={18} /> 依職務授權
            </span>
          </div>
        </div>
        <a href="/" className="ad-login-back">
          <ArrowLeft size={16} /> 民眾申請請由服務入口進入
        </a>
      </section>
      <section className="ad-login-side">
        <form className="ad-login-card" onSubmit={submit}>
          <span className="ad-login-icon">
            <LockKeyhole size={27} />
          </span>
          <span className="ad-eyebrow">工作帳號登入</span>
          <h2>{challenge ? '完成第二步驗證' : '歡迎回到工作台'}</h2>
          <p>
            {challenge
              ? '請輸入驗證器目前顯示的六碼驗證碼。'
              : '請使用由管理人員開通的帳號與密碼。'}
          </p>
          {message && <Notice>{message}</Notice>}
          {error && <Notice error>{error}</Notice>}
          <fieldset disabled={busy}>
            {!challenge ? (
              <>
                <Field label="工作帳號信箱">
                  <input
                    type="email"
                    autoComplete="username"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                  />
                </Field>
                <Field label="密碼">
                  <input
                    type="password"
                    autoComplete="current-password"
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </Field>
              </>
            ) : (
              <>
                <Field label="六碼驗證碼" hint={`本次登入驗證期限：${date(challenge.expires_at)}`}>
                  <input
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    pattern="[0-9]{6}"
                    maxLength={6}
                    required
                    value={code}
                    onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                    autoFocus
                  />
                </Field>
                <button
                  type="button"
                  className="ad-text-link"
                  onClick={() => {
                    setChallenge(null);
                    setCode('');
                    setError('');
                  }}
                >
                  重新輸入帳號與密碼
                </button>
              </>
            )}
            <button className="ad-btn ad-primary ad-full" type="submit">
              {busy ? '正在驗證…' : challenge ? '驗證並登入' : '繼續第二步驗證'}
              <ArrowRight size={17} />
            </button>
          </fieldset>
          <p className="ad-login-help">
            無法登入或遺失驗證器，請聯絡帳號管理人員。請勿將密碼或驗證碼提供給他人。
          </p>
        </form>
      </section>
    </div>
  );
}

function Workspace({
  me,
  checking,
  onLogout,
}: {
  me: Me;
  checking: boolean;
  onLogout: () => Promise<void>;
}) {
  const canReadCases = me.roles.some((role) => caseRoles.includes(role));
  const canManageAccounts = me.roles.includes('admin');
  const [view, setView] = useState<'cases' | 'accounts'>(canReadCases ? 'cases' : 'accounts');
  const [items, setItems] = useState<CaseData[]>([]);
  const [status, setStatus] = useState('RECEIVED');
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [caseTab, setCaseTab] = useState('documents');
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const requestSequence = useRef(0);
  const listSequence = useRef(0);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      requestSequence.current++;
      listSequence.current++;
    };
  }, []);

  const loadCases = useCallback(
    async (cursor?: string) => {
      if (!canReadCases) return;
      const turn = ++listSequence.current;
      setLoading(true);
      setError('');
      try {
        const query = new URLSearchParams({ limit: '30' });
        if (status) query.set('status', status);
        if (cursor) query.set('cursor', cursor);
        const page = await api<Page<CaseData>>(`/staff/cases?${query}`);
        if (alive.current && turn === listSequence.current) {
          setItems((previous) => (cursor ? [...previous, ...page.items] : page.items));
          setNextCursor(page.next_cursor || null);
        }
      } catch (cause) {
        if (alive.current && turn === listSequence.current) setError(errorMessage(cause));
      } finally {
        if (alive.current && turn === listSequence.current) setLoading(false);
      }
    },
    [canReadCases, status],
  );
  useEffect(() => {
    if (view === 'cases' && !selectedId) void loadCases();
  }, [view, selectedId, loadCases]);

  async function loadCase(id: string) {
    const turn = ++requestSequence.current;
    if (id !== selectedId) setCaseTab('documents');
    setSelectedId(id);
    setBundle(null);
    setLoading(true);
    setError('');
    try {
      const detail = await api<Detail>(`/staff/cases/${encodeURIComponent(id)}`);
      const [scheme, schema, reviews] = await Promise.all([
        api<SchemeInfo>(`/schemes/${encodeURIComponent(detail.scheme_id)}`),
        api<FormSchema>(`/schemes/${encodeURIComponent(detail.scheme_id)}/form-schema`),
        api<{ items: Review[] }>(`/staff/cases/${encodeURIComponent(id)}/review-items`),
      ]);
      if (alive.current && turn === requestSequence.current)
        setBundle({ detail, scheme, schema, reviews: reviews.items });
    } catch (cause) {
      if (alive.current && turn === requestSequence.current) setError(errorMessage(cause));
    } finally {
      if (alive.current && turn === requestSequence.current) setLoading(false);
    }
  }
  const run: Run = async (operation, success) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await operation();
      if (!alive.current) return false;
      setNotice(success);
      if (selectedId) await loadCase(selectedId);
      return true;
    } catch (cause) {
      if (alive.current) setError(errorMessage(cause));
      return false;
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  function closeDetail() {
    requestSequence.current++;
    setSelectedId(null);
    setBundle(null);
    setCaseTab('documents');
    setError('');
    setNotice('');
  }
  return (
    <div className="ad-shell">
      <aside className="ad-sidebar">
        <a className="ad-brand" href="/admin/">
          <span>
            <Leaf size={25} />
          </span>
          <strong>
            竹青通<small>承辦作業平台</small>
          </strong>
        </a>
        <div className="ad-side-caption">你的工作範圍</div>
        <nav aria-label="後台功能">
          {canReadCases && (
            <button
              className={view === 'cases' ? 'active' : ''}
              onClick={() => {
                setView('cases');
                closeDetail();
              }}
            >
              <FolderOpen size={19} /> 案件工作台
            </button>
          )}
          {canManageAccounts && (
            <button
              className={view === 'accounts' ? 'active' : ''}
              onClick={() => {
                setView('accounts');
                closeDetail();
              }}
            >
              <Users size={19} /> 帳號管理
            </button>
          )}
        </nav>
        <div className="ad-side-note">
          <ShieldCheck size={22} />
          <p>
            權限依工作帳號核定。
            <br />
            案件操作由伺服器再次確認。
          </p>
        </div>
      </aside>
      <div className="ad-workspace">
        <header className="ad-topbar">
          <span>新竹青年服務 · 後台</span>
          <div>
            <span className="ad-user-email">{me.account.email}</span>
            <button className="ad-btn ad-quiet" disabled={busy} onClick={() => void onLogout()}>
              <LogOut size={15} /> 登出
            </button>
          </div>
        </header>
        <main className="ad-main">
          <div className="ad-role-row">
            {me.roles
              .filter((role) => staffRoles.includes(role))
              .map((role) => (
                <Badge key={role}>{roleLabels[role]}</Badge>
              ))}
            {checking && <span role="status">正在確認登入…</span>}
          </div>
          {error && (
            <Notice error>
              {error}
              <button
                className="ad-text-link"
                onClick={() => (selectedId ? void loadCase(selectedId) : void loadCases())}
              >
                重新讀取
              </button>
            </Notice>
          )}
          {notice && <Notice>{notice}</Notice>}
          <fieldset className="ad-work-area" disabled={busy || checking}>
            {view === 'accounts' && canManageAccounts ? (
              <Accounts me={me} />
            ) : selectedId ? (
              <>
                <div className="ad-detail-toolbar">
                  <button className="ad-text-link" onClick={closeDetail}>
                    <ArrowLeft size={16} /> 返回案件列表
                  </button>
                  <button
                    className="ad-btn"
                    onClick={() => void loadCase(selectedId)}
                    disabled={loading}
                  >
                    <RefreshCw size={15} /> 更新案件
                  </button>
                </div>
                {loading ? (
                  <div className="ad-empty" role="status">
                    正在讀取案件與審查資料…
                  </div>
                ) : (
                  bundle && (
                    <CaseWorkspace
                      bundle={bundle}
                      me={me}
                      run={run}
                      tab={caseTab}
                      onTab={setCaseTab}
                    />
                  )
                )}
              </>
            ) : (
              <>
                <div className="ad-page-heading">
                  <div>
                    <span className="ad-eyebrow">接續每一份申請</span>
                    <h1>案件工作台</h1>
                    <p>只顯示你有權存取的案件，開啟後可核對文件與處理補件。</p>
                  </div>
                  <button className="ad-btn" onClick={() => void loadCases()} disabled={loading}>
                    <RefreshCw size={16} /> 更新列表
                  </button>
                </div>
                <section className="ad-card">
                  <div className="ad-list-toolbar">
                    <Field label="案件狀態">
                      <select
                        value={status}
                        onChange={(e) => {
                          setItems([]);
                          setStatus(e.target.value);
                        }}
                      >
                        <option value="">全部狀態</option>
                        {Object.entries(caseLabels).map(([key, label]) => (
                          <option key={key} value={key}>
                            {label}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <span className="ad-muted">目前已載入 {items.length} 件</span>
                  </div>
                  <div className="ad-case-list">
                    {items.map((item) => (
                      <button
                        key={item.id}
                        className="ad-case-row"
                        onClick={() => void loadCase(item.id)}
                      >
                        <span className="ad-case-icon">
                          <FileText size={23} />
                        </span>
                        <span className="ad-case-copy">
                          <strong>{textValue(item.form_data.name)}</strong>
                          <span>{item.case_no}</span>
                          <small>最近更新：{date(item.last_business_update_at)}</small>
                        </span>
                        <Badge tone={item.status === 'RECEIVED' ? 'amber' : ''}>
                          {caseLabels[item.status] || item.status}
                        </Badge>
                        <ArrowRight size={18} />
                      </button>
                    ))}
                  </div>
                  {!items.length && !loading && (
                    <div className="ad-empty">
                      <FolderOpen size={32} />
                      <h2>目前沒有此狀態的案件</h2>
                      <p>可切換篩選條件。承辦帳號也須先取得案件分派或存取授權。</p>
                    </div>
                  )}
                  {loading && (
                    <p role="status" className="ad-muted">
                      正在讀取案件…
                    </p>
                  )}
                  {nextCursor && (
                    <button
                      className="ad-btn ad-full"
                      disabled={loading}
                      onClick={() => void loadCases(nextCursor)}
                    >
                      載入更多案件
                    </button>
                  )}
                </section>
              </>
            )}
          </fieldset>
        </main>
      </div>
    </div>
  );
}

function CaseWorkspace({
  bundle,
  me,
  run,
  tab,
  onTab,
}: {
  bundle: Bundle;
  me: Me;
  run: Run;
  tab: string;
  onTab: (tab: string) => void;
}) {
  const { detail, scheme, schema, reviews } = bundle;
  const files = detail.files || [];
  const tabs = [
    ['documents', '申請資料與文件'],
    ['tasks', '補件處理'],
    ['reviews', '人工檢核'],
    ['decision', '核定與結案'],
  ];
  return (
    <>
      <div className="ad-page-heading">
        <div>
          <span className="ad-eyebrow">{scheme.name}</span>
          <h1>{textValue(detail.form_data.name)}的申請</h1>
          <p className="ad-case-number">{detail.case_no}</p>
        </div>
        <Badge>{caseLabels[detail.status] || detail.status}</Badge>
      </div>
      <div className="ad-case-summary">
        <span>
          正式表單<strong>第 {detail.current_revision_no} 版</strong>
        </span>
        <span>
          補件任務<strong>{detail.tasks?.length || 0} 項</strong>
        </span>
        <span>
          最近業務更新<strong>{date(detail.last_business_update_at)}</strong>
        </span>
      </div>
      {detail.allowed_actions.includes('start_review') && (
        <section className="ad-start-review">
          <div>
            <h2>文件已收件，準備開始審查</h2>
            <p>開始後建立方案必要檢核項目，並可提出補件要求。</p>
          </div>
          <button
            className="ad-btn ad-primary"
            onClick={() =>
              void run(
                () =>
                  api(`/staff/cases/${detail.id}/start-review`, {
                    method: 'POST',
                    etag: detail.etag,
                  }),
                '已開始審查。',
              )
            }
          >
            <ClipboardCheck size={17} /> 開始審查
          </button>
        </section>
      )}
      <nav className="ad-tabs" aria-label="案件資料頁籤">
        {tabs.map(([key, title]) => (
          <button
            key={key}
            aria-current={tab === key ? 'page' : undefined}
            className={tab === key ? 'active' : ''}
            onClick={() => onTab(key)}
          >
            {title}
          </button>
        ))}
      </nav>
      {tab === 'documents' && (
        <>
          <section className="ad-card">
            <div className="ad-section-heading">
              <h2>申請人填寫資料</h2>
              <Badge tone="neutral">唯讀</Badge>
            </div>
            <dl className="ad-data-grid">
              {Object.entries(detail.form_data).map(([key, value]) => (
                <div key={key}>
                  <dt>
                    {schema.form_schema.properties?.[key]?.title ||
                      (
                        {
                          name: '姓名',
                          email: '信箱',
                          phone: '電話',
                          subject: '申請主旨',
                          description: '申請說明',
                        } as Record<string, string>
                      )[key] ||
                      key}
                  </dt>
                  <dd>{textValue(value)}</dd>
                </div>
              ))}
            </dl>
            {!Object.keys(detail.form_data).length && <p className="ad-muted">尚未填寫資料。</p>}
          </section>
          <section className="ad-card">
            <div className="ad-section-heading">
              <h2>正式提交的文件</h2>
              <span className="ad-muted">{files.length} 個版本</span>
            </div>
            <p className="ad-muted">安全檢查通過後可下載。文件內容、身分與付款真偽仍須人工查核。</p>
            <DocumentList files={files} />
          </section>
          <section className="ad-card">
            <h2>送件紀錄</h2>
            <div className="ad-timeline">
              {detail.submissions.map((submission) => (
                <div key={submission.id}>
                  <span className="ad-timeline-dot" />
                  <strong>{submission.task_id ? '補件提交' : '申請送件'}</strong>
                  <small>
                    {date(submission.submitted_at)} · {submission.file_version_ids.length} 份文件
                  </small>
                  {submission.statement && <p>{submission.statement}</p>}
                </div>
              ))}
            </div>
            {!detail.submissions.length && <p className="ad-muted">尚無正式提交紀錄。</p>}
          </section>
        </>
      )}
      {tab === 'tasks' && (
        <>
          {detail.allowed_actions.includes('create_task') && (
            <TaskCreate caseId={detail.id} run={run} />
          )}
          {detail.tasks?.map((task) => (
            <TaskCard
              key={`${task.id}:${task.version}`}
              task={task}
              submissions={detail.submissions.filter((s) => s.task_id === task.id)}
              files={files}
              run={run}
            />
          ))}
          {!detail.tasks?.length && (
            <section className="ad-card ad-empty">
              <h2>目前沒有補件要求</h2>
              <p>審查時如發現缺件，請提供具體要求、驗收標準及期限。</p>
            </section>
          )}
        </>
      )}
      {tab === 'reviews' && (
        <>
          <Notice>
            檢核結論由承辦人工作成。符合或不符合都須附正式表單或文件依據；有疑義時請說明原因。
          </Notice>
          {!scheme.rule_version_id && (
            <Notice error>
              方案尚未提供已發布的規則版本，暫時無法儲存檢核結論，請聯絡系統管理人員。
            </Notice>
          )}
          {reviews.map((item) => (
            <ReviewEditor
              key={`${item.id}:${item.version}`}
              item={item}
              detail={detail}
              scheme={scheme}
              run={run}
              schema={schema}
            />
          ))}
          {!reviews.length && (
            <section className="ad-card ad-empty">
              <h2>尚未建立檢核項目</h2>
              <p>案件開始審查後，系統會帶入方案必要檢核。</p>
            </section>
          )}
        </>
      )}
      {tab === 'decision' && (
        <DecisionPanel
          detail={detail}
          scheme={scheme}
          reviews={reviews}
          me={me}
          run={run}
          schema={schema}
        />
      )}
    </>
  );
}

function DocumentList({ files }: { files: FileInfo[] }) {
  const transfer = useRef<AbortController | null>(null);
  useEffect(() => () => transfer.current?.abort(), []);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  async function download(file: FileInfo) {
    setBusy(file.file_version_id);
    setError('');
    const controller = new AbortController();
    transfer.current = controller;
    try {
      const path = `/api/v1/files/${encodeURIComponent(file.file_id)}/download?file_version_id=${encodeURIComponent(file.file_version_id)}`;
      const response = await fetch(path, {
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      });
      if (!response.ok) {
        if (response.status === 401) window.dispatchEvent(new Event('youth:session-expired'));
        const payload = await response.json().catch(() => null);
        throw new ApiError(
          response.status,
          payload?.error?.code || 'DOWNLOAD_FAILED',
          payload?.error?.message || '文件下載失敗，請重新讀取案件。',
        );
      }
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = file.file_name;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy('');
    }
  }
  return (
    <>
      {error && <Notice error>{error}</Notice>}
      <div className="ad-files">
        {files.map((file) => (
          <div className="ad-file-row" key={file.file_version_id}>
            <span className="ad-case-icon">
              <FileText size={21} />
            </span>
            <div>
              <strong>{documentLabels[file.document_type] || file.document_type}</strong>
              <p>{file.file_name}</p>
              <small>
                {file.task_id ? '補件附件' : '初次申請附件'} · {Math.ceil(file.size_bytes / 1024)}{' '}
                KB
              </small>
            </div>
            <Badge tone={file.scan_status === 'CLEAN' ? '' : 'amber'}>
              {scanLabels[file.scan_status] || '暫不可閱覽'}
            </Badge>
            <button
              className="ad-btn"
              disabled={file.scan_status !== 'CLEAN' || Boolean(busy)}
              onClick={() => void download(file)}
              aria-label={`下載 ${file.file_name}`}
            >
              <Download size={15} /> {busy === file.file_version_id ? '下載中' : '下載'}
            </button>
          </div>
        ))}
      </div>
      {!files.length && <p className="ad-muted">尚無可顯示的正式附件。</p>}
    </>
  );
}

function TaskCreate({ caseId, run }: { caseId: string; run: Run }) {
  const [title, setTitle] = useState('');
  const [requirement, setRequirement] = useState('');
  const [criteria, setCriteria] = useState('');
  const [due, setDue] = useState(futureDate);
  const keyFor = useStableKey();
  async function submit(event: FormEvent) {
    event.preventDefault();
    const body = {
      title,
      requirement,
      acceptance_criteria: criteria,
      due_at: new Date(due).toISOString(),
    };
    await run(
      () =>
        api(`/staff/cases/${caseId}/tasks`, {
          method: 'POST',
          json: body,
          idempotencyKey: keyFor(body),
        }),
      '補件要求已建立，通知將依伺服器排程處理。',
    );
  }
  return (
    <details className="ad-card ad-create-task">
      <summary>
        <Plus size={18} /> 建立補件要求
      </summary>
      <form onSubmit={submit}>
        <Field label="補件標題">
          <input
            required
            maxLength={200}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="例如：補上清晰的身分證正面"
          />
        </Field>
        <Field label="需要補正的內容">
          <textarea
            required
            maxLength={2000}
            rows={3}
            value={requirement}
            onChange={(e) => setRequirement(e.target.value)}
          />
        </Field>
        <Field label="驗收標準">
          <textarea
            required
            maxLength={2000}
            rows={2}
            value={criteria}
            onChange={(e) => setCriteria(e.target.value)}
            placeholder="請說明怎樣的文件才符合要求。"
          />
        </Field>
        <Field label="補件期限">
          <input
            type="datetime-local"
            required
            value={due}
            onChange={(e) => setDue(e.target.value)}
          />
        </Field>
        <button className="ad-btn ad-primary" type="submit">
          建立補件要求
        </button>
      </form>
    </details>
  );
}

function TaskCard({
  task,
  submissions,
  files,
  run,
}: {
  task: TaskData;
  submissions: Submission[];
  files: FileInfo[];
  run: Run;
}) {
  const [action, setAction] = useState<'accept' | 'reopen' | 'revise' | 'cancel' | ''>('');
  const [note, setNote] = useState('');
  const [due, setDue] = useState(futureDate);
  const current = submissions.filter((s) => s.task_revision === task.task_revision).slice(-1)[0];
  const submittedFiles = current
    ? files.filter((file) => current.file_version_ids.includes(file.file_version_id))
    : [];
  const clean =
    Boolean(current) &&
    current!.file_version_ids.every((id) =>
      submittedFiles.some((f) => f.file_version_id === id && f.scan_status === 'CLEAN'),
    );
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!action || (['accept', 'reopen'].includes(action) && !current)) return;
    const body =
      action === 'accept'
        ? { submission_id: current!.id, review_note: note }
        : action === 'reopen'
          ? { submission_id: current!.id, public_reason: note, due_at: new Date(due).toISOString() }
          : action === 'revise'
            ? { reason: note, due_at: new Date(due).toISOString() }
            : { reason: note };
    const completed = {
      accept: '已接受這次補件。',
      reopen: '已要求重新補正，新的期限與原因已記錄。',
      revise: '補件期限已更新，新要求版本與變更原因已記錄。',
      cancel: '補件任務已取消，取消原因已留存。',
    };
    await run(
      () =>
        api(`/staff/tasks/${task.id}/${action}`, { method: 'POST', json: body, etag: task.etag }),
      completed[action],
    );
  }
  return (
    <section className="ad-card">
      <div className="ad-section-heading">
        <h2>{task.title}</h2>
        <Badge tone={task.status === 'SUBMITTED' ? 'amber' : ''}>
          {taskLabels[task.status] || task.status}
        </Badge>
      </div>
      <p className="ad-preserve">{task.requirement}</p>
      <div className="ad-task-criteria">
        <strong>驗收標準</strong>
        <p>{task.acceptance_criteria}</p>
        <small>
          期限：{date(task.due_at)}
          {task.is_overdue ? ' · 已逾期' : ''} · 第 {task.task_revision} 版要求
        </small>
      </div>
      {current && (
        <div className="ad-submission">
          <h3>本次補件內容</h3>
          <p className="ad-muted">送出時間：{date(current.submitted_at)}</p>
          {current.statement && <p className="ad-preserve">{current.statement}</p>}
          <DocumentList files={submittedFiles} />
        </div>
      )}
      <div className="ad-actions">
        {task.allowed_actions.includes('accept_submission') && (
          <button
            className="ad-btn ad-primary"
            disabled={!clean}
            onClick={() => {
              setAction('accept');
              setNote('');
            }}
          >
            接受補件
          </button>
        )}
        {task.allowed_actions.includes('reopen_task') && (
          <button
            className="ad-btn"
            disabled={!current}
            onClick={() => {
              setAction('reopen');
              setNote('');
            }}
          >
            要求重新補正
          </button>
        )}
        {task.allowed_actions.includes('revise_task') && (
          <button
            className="ad-btn"
            onClick={() => {
              setAction('revise');
              setNote('');
            }}
          >
            調整補件期限
          </button>
        )}
        {task.allowed_actions.includes('cancel_task') && (
          <button
            className="ad-btn"
            onClick={() => {
              setAction('cancel');
              setNote('');
            }}
          >
            取消補件任務
          </button>
        )}
      </div>
      {task.status === 'SUBMITTED' && !clean && (
        <p className="ad-muted">需先取得本次補件內容，且所有附件通過安全檢查，才能接受補件。</p>
      )}
      {action && (
        <form className="ad-inline-form" onSubmit={submit}>
          <Field
            label={
              {
                accept: '驗收紀錄',
                reopen: '提供申請人的補正原因',
                revise: '調整期限理由',
                cancel: '取消任務理由',
              }[action]
            }
          >
            <textarea
              required
              maxLength={action === 'revise' || action === 'cancel' ? 1000 : 2000}
              rows={3}
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </Field>
          {(action === 'reopen' || action === 'revise') && (
            <Field label="新的補件期限">
              <input
                type="datetime-local"
                required
                value={due}
                onChange={(e) => setDue(e.target.value)}
              />
            </Field>
          )}
          <div className="ad-actions">
            <button className="ad-btn ad-primary" type="submit">
              {
                {
                  accept: '確認接受補件',
                  reopen: '送出重新補正要求',
                  revise: '儲存新的補件期限',
                  cancel: '確認取消補件任務',
                }[action]
              }
            </button>
            <button className="ad-btn" type="button" onClick={() => setAction('')}>
              取消操作
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

function EvidenceEditor({
  value,
  onChange,
  detail,
  ruleId,
  disabled,
  schema,
}: {
  value: Evidence[];
  onChange: (value: Evidence[]) => void;
  detail: Detail;
  ruleId: string;
  disabled?: boolean;
  schema: FormSchema;
}) {
  const files = detail.files || [];
  const update = (index: number, next: Evidence) =>
    onChange(value.map((item, i) => (i === index ? next : item)));
  return (
    <div className="ad-evidence">
      <div className="ad-section-heading">
        <h3>人工核對的依據</h3>
        {!disabled && (
          <button
            className="ad-btn ad-small"
            type="button"
            disabled={!ruleId || value.length >= 50}
            onClick={() => onChange([...value, { rule_version_id: ruleId }])}
          >
            <Plus size={14} /> 新增依據
          </button>
        )}
      </div>
      <p className="ad-muted">引用正式表單版本或已通過安全檢查的文件，保留欄位、頁次及核對說明。</p>
      {value.map((entry, index) => {
        const selectedFile = files.find((file) => file.file_version_id === entry.file_version_id);
        const selectedRevision = detail.revisions.find(
          (revision) => revision.id === entry.case_revision_id,
        );
        const selected = entry.file_version_id
          ? `file:${entry.file_version_id}`
          : entry.case_revision_id
            ? `form:${entry.case_revision_id}`
            : '';
        return (
          <fieldset disabled={disabled} key={index} className="ad-evidence-row">
            <div className="ad-evidence-top">
              <strong>依據 {index + 1}</strong>
              {!disabled && (
                <button
                  type="button"
                  className="ad-icon-btn"
                  aria-label={`移除依據 ${index + 1}`}
                  onClick={() => onChange(value.filter((_, i) => i !== index))}
                >
                  <X size={16} />
                </button>
              )}
            </div>
            <Field label="依據來源">
              <select
                required
                value={selected}
                onChange={(e) => {
                  const [kind, id] = e.target.value.split(':');
                  update(index, {
                    rule_version_id: ruleId,
                    ...(kind === 'form'
                      ? { case_revision_id: id }
                      : kind === 'file'
                        ? { file_version_id: id }
                        : {}),
                  });
                }}
              >
                <option value="">請選擇已核對的資料</option>
                <optgroup label="正式申請表">
                  {detail.revisions.map((revision) => (
                    <option key={revision.id} value={`form:${revision.id}`}>
                      申請表第 {revision.revision_no} 版 · {date(revision.submitted_at)}
                    </option>
                  ))}
                </optgroup>
                <optgroup label="正式提交文件">
                  {files.map((file) => (
                    <option
                      key={file.file_version_id}
                      value={`file:${file.file_version_id}`}
                      disabled={file.scan_status !== 'CLEAN'}
                    >
                      {file.file_name} · {scanLabels[file.scan_status] || file.scan_status}
                    </option>
                  ))}
                </optgroup>
              </select>
            </Field>
            <div className="ad-form-grid">
              {entry.case_revision_id && (
                <Field label="核對欄位（選填）">
                  <select
                    value={entry.field_path || ''}
                    onChange={(e) =>
                      update(index, { ...entry, field_path: e.target.value || null })
                    }
                  >
                    <option value="">整份表單</option>
                    {Object.keys(selectedRevision?.form_snapshot || {}).map((field) => (
                      <option key={field} value={field}>
                        {schema.form_schema.properties?.[field]?.title || field}
                      </option>
                    ))}
                  </select>
                </Field>
              )}
              {entry.file_version_id && (
                <Field label="核對頁次（選填）">
                  <input
                    type="number"
                    min={1}
                    max={100000}
                    step={1}
                    value={entry.page_no || ''}
                    onChange={(e) =>
                      update(index, {
                        ...entry,
                        page_no: e.target.value ? Number(e.target.value) : null,
                      })
                    }
                  />
                </Field>
              )}
              {entry.file_version_id && (
                <Field
                  label="取代的舊附件（選填）"
                  hint="僅適用於已接受的同類補件；需填寫替代理由。"
                >
                  <select
                    value={entry.replaces_file_version_id || ''}
                    onChange={(e) =>
                      update(index, { ...entry, replaces_file_version_id: e.target.value || null })
                    }
                  >
                    <option value="">無替代關係</option>
                    {files
                      .filter(
                        (file) =>
                          file.file_version_id !== entry.file_version_id &&
                          file.document_type === selectedFile?.document_type,
                      )
                      .map((file) => (
                        <option key={file.file_version_id} value={file.file_version_id}>
                          {file.file_name} · {scanLabels[file.scan_status] || file.scan_status}
                        </option>
                      ))}
                  </select>
                </Field>
              )}
            </div>
            <Field label={entry.replaces_file_version_id ? '替代理由（必填）' : '核對說明（選填）'}>
              <textarea
                rows={2}
                maxLength={1000}
                required={Boolean(entry.replaces_file_version_id)}
                value={entry.note || ''}
                onChange={(e) => update(index, { ...entry, note: e.target.value || null })}
              />
            </Field>
          </fieldset>
        );
      })}
      {!value.length && <p className="ad-empty-inline">尚未引用依據。</p>}
    </div>
  );
}

function ReviewEditor({
  item,
  detail,
  scheme,
  run,
  schema,
}: {
  item: Review;
  detail: Detail;
  scheme: SchemeInfo;
  run: Run;
  schema: FormSchema;
}) {
  const [result, setResult] = useState(item.result);
  const [internal, setInternal] = useState(item.internal_note || '');
  const [publicReason, setPublicReason] = useState(item.public_reason || '');
  const [evidence, setEvidence] = useState<Evidence[]>(item.evidence_refs || []);
  const canEdit = detail.allowed_actions.includes('review_case') && Boolean(scheme.rule_version_id);
  const needEvidence = result === 'PASS' || result === 'FAIL';
  const missingReason =
    (result === 'FAIL' || result === 'QUESTION') && !internal.trim() && !publicReason.trim();
  async function save(event: FormEvent) {
    event.preventDefault();
    await run(
      () =>
        api(`/staff/cases/${detail.id}/review-items/${item.id}`, {
          method: 'PATCH',
          etag: item.etag,
          json: {
            result,
            internal_note: internal || null,
            public_reason: publicReason || null,
            evidence_refs: evidence,
          },
        }),
      '人工檢核與依據已儲存。',
    );
  }
  return (
    <form className="ad-card" onSubmit={save}>
      <div className="ad-section-heading">
        <h2>
          {scheme.criterion_labels?.[item.criterion_code] ||
            criterionLabels[item.criterion_code] ||
            item.criterion_code}
        </h2>
        <Badge tone={item.result === 'FAIL' ? 'red' : item.result === 'PASS' ? '' : 'amber'}>
          已存：{resultLabels[item.result] || item.result}
        </Badge>
      </div>
      <fieldset disabled={!canEdit}>
        <Field label="人工檢核結果">
          <select value={result} onChange={(e) => setResult(e.target.value)}>
            {Object.entries(resultLabels).map(([key, label]) => (
              <option value={key} key={key}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        <div className="ad-form-grid">
          <Field label="內部審查紀錄">
            <textarea
              rows={3}
              maxLength={2000}
              value={internal}
              onChange={(e) => setInternal(e.target.value)}
            />
          </Field>
          <Field label="提供申請人的說明">
            <textarea
              rows={3}
              maxLength={2000}
              value={publicReason}
              onChange={(e) => setPublicReason(e.target.value)}
            />
          </Field>
        </div>
        <EvidenceEditor
          value={evidence}
          onChange={setEvidence}
          detail={detail}
          ruleId={scheme.rule_version_id || ''}
          disabled={!canEdit}
          schema={schema}
        />
        {canEdit && (
          <>
            <p className="ad-muted">
              {needEvidence && !evidence.length
                ? '請至少加入一項可追溯依據。'
                : missingReason
                  ? '不符合或有疑義時，請填寫內部紀錄或提供申請人的說明。'
                  : '儲存只更新此檢核項目，不會直接核定案件。'}
            </p>
            <button
              className="ad-btn ad-primary"
              disabled={(needEvidence && !evidence.length) || missingReason}
              type="submit"
            >
              <Check size={16} /> 儲存檢核
            </button>
          </>
        )}
      </fieldset>
    </form>
  );
}

function DecisionPanel({
  detail,
  scheme,
  reviews,
  me,
  run,
  schema,
}: {
  detail: Detail;
  scheme: SchemeInfo;
  reviews: Review[];
  me: Me;
  run: Run;
  schema: FormSchema;
}) {
  const [outcome, setOutcome] = useState('APPROVED');
  const [reason, setReason] = useState('');
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [completion, setCompletion] = useState('');
  const keyFor = useStableKey();
  const canDecide =
    me.roles.includes('supervisor') && detail.allowed_actions.includes('create_decision');
  const canClose = me.roles.includes('supervisor') && detail.allowed_actions.includes('close_case');
  const criteria = scheme.required_criteria || [];
  const required = reviews.filter((item) => criteria.includes(item.criterion_code));
  const reviewComplete =
    criteria.length > 0 &&
    required.length === criteria.length &&
    required.every((item) => ['PASS', 'FAIL'].includes(item.result));
  const outcomeMatches =
    outcome === 'APPROVED'
      ? required.every((item) => item.result === 'PASS')
      : required.some((item) => item.result === 'FAIL');
  const tasksComplete = (detail.tasks || []).every((task) =>
    ['ACCEPTED', 'CANCELLED'].includes(task.status),
  );
  function takeReviewEvidence() {
    const unique = new Map(
      reviews.flatMap((item) => item.evidence_refs).map((ref) => [JSON.stringify(ref), { ...ref }]),
    );
    setEvidence([...unique.values()].slice(0, 50));
    setConfirmed(false);
  }
  async function decide(event: FormEvent) {
    event.preventDefault();
    const body = {
      outcome,
      reason,
      rule_version_id: scheme.rule_version_id,
      evidence_refs: evidence,
    };
    await run(
      () =>
        api(`/staff/cases/${detail.id}/decisions`, {
          method: 'POST',
          json: body,
          etag: detail.etag,
          idempotencyKey: keyFor(body),
        }),
      '主管決定已記錄。核定與結案不代表已撥款。',
    );
  }
  return (
    <>
      <Notice>核定由主管依人工檢核與證據作成。此頁不執行付款，結案也不代表款項已入帳。</Notice>
      {detail.decisions.map((decision) => (
        <section className="ad-card" key={decision.id}>
          <div className="ad-section-heading">
            <h2>{decision.outcome === 'APPROVED' ? '核准紀錄' : '駁回紀錄'}</h2>
            <Badge tone="neutral">{date(decision.decided_at)}</Badge>
          </div>
          <p className="ad-preserve">{decision.reason}</p>
          {decision.supersedes_id && <p className="ad-muted">此筆為更正決定。</p>}
        </section>
      ))}
      {canDecide && (
        <form className="ad-card" onSubmit={decide}>
          <h2>主管作成決定</h2>
          <div className="ad-readiness">
            <span>
              <Badge tone={reviewComplete ? '' : 'amber'}>
                {reviewComplete ? '必要檢核已完成' : '必要檢核尚未完成'}
              </Badge>
            </span>
            <span>
              <Badge tone={tasksComplete ? '' : 'amber'}>
                {tasksComplete ? '補件任務已處理' : '仍有未完成補件'}
              </Badge>
            </span>
          </div>
          <Field label="核定結果">
            <select
              value={outcome}
              onChange={(e) => {
                setOutcome(e.target.value);
                setConfirmed(false);
              }}
            >
              <option value="APPROVED">核准</option>
              <option value="REJECTED">駁回</option>
            </select>
          </Field>
          <Field label="核定理由">
            <textarea
              required
              rows={3}
              maxLength={1000}
              value={reason}
              onChange={(e) => {
                setReason(e.target.value);
                setConfirmed(false);
              }}
            />
          </Field>
          <button className="ad-btn" type="button" onClick={takeReviewEvidence}>
            帶入已儲存的檢核依據
          </button>
          <EvidenceEditor
            value={evidence}
            onChange={(value) => {
              setEvidence(value);
              setConfirmed(false);
            }}
            detail={detail}
            ruleId={scheme.rule_version_id || ''}
            schema={schema}
          />
          <label className="ad-check">
            <input
              type="checkbox"
              required
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            <span>我已核對檢核結果、補件與證據，確認作成此決定。</span>
          </label>
          <p className="ad-muted">
            文件安全檢查與替代關係仍由伺服器確認；有未解決項目時會阻擋核定。
          </p>
          <button
            className="ad-btn ad-primary"
            type="submit"
            disabled={
              !confirmed ||
              !evidence.length ||
              !scheme.rule_version_id ||
              !reviewComplete ||
              !tasksComplete ||
              !outcomeMatches
            }
          >
            確認送出主管決定
          </button>
          {reviewComplete && !outcomeMatches && (
            <p className="ad-error-text">核准須全部必要項目符合；駁回須至少一項不符合並附依據。</p>
          )}
        </form>
      )}
      {canClose && (
        <form
          className="ad-card"
          onSubmit={(event) => {
            event.preventDefault();
            void run(
              () =>
                api(`/staff/cases/${detail.id}/close`, {
                  method: 'POST',
                  etag: detail.etag,
                  json: { completion_note: completion },
                }),
              '案件已結案，完成紀錄已保存。',
            );
          }}
        >
          <h2>完成案件結案</h2>
          <Field label="結案紀錄" hint="記錄實際完成的行政處理，不以結案宣告付款完成。">
            <textarea
              required
              maxLength={2000}
              rows={3}
              value={completion}
              onChange={(e) => setCompletion(e.target.value)}
            />
          </Field>
          <button className="ad-btn ad-primary" type="submit">
            確認結案
          </button>
        </form>
      )}
      {!canDecide && !canClose && !detail.decisions.length && (
        <section className="ad-card ad-empty">
          <h2>目前沒有可執行的核定操作</h2>
          <p>主管可在完成檢核與補件後作成決定，其他角色可查看已完成紀錄。</p>
        </section>
      )}
    </>
  );
}

function Accounts({ me }: { me: Me }) {
  const [items, setItems] = useState<AccountInfo[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [target, setTarget] = useState<AccountInfo | null>(null);
  const [reason, setReason] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const alive = useRef(true);
  async function load(next = 0) {
    setBusy(true);
    setError('');
    try {
      const result = await api<{ items: AccountInfo[] }>(`/admin/accounts?limit=30&offset=${next}`);
      if (alive.current) {
        setItems(result.items);
        setOffset(next);
        setHasMore(result.items.length === 30);
      }
    } catch (cause) {
      if (alive.current) setError(errorMessage(cause));
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  useEffect(() => {
    alive.current = true;
    void load();
    return () => {
      alive.current = false;
    };
  }, []);
  async function changeStatus(event: FormEvent) {
    event.preventDefault();
    if (!target) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await api(`/admin/accounts/${target.id}/status`, {
        method: 'PATCH',
        etag: target.etag,
        json: { status: target.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE', reason },
      });
      if (!alive.current) return;
      setTarget(null);
      setReason('');
      setConfirmed(false);
      setNotice('帳號狀態已更新，該帳號既有登入已撤銷。');
      await load(offset);
    } catch (cause) {
      if (alive.current) setError(errorMessage(cause));
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  return (
    <>
      <div className="ad-page-heading">
        <div>
          <span className="ad-eyebrow">帳號管理權限</span>
          <h1>帳號與授權資料</h1>
          <p>查看目前授權，依核定程序啟用或停用帳號。</p>
        </div>
        <button className="ad-btn" disabled={busy} onClick={() => void load(offset)}>
          <RefreshCw size={16} /> 更新帳號
        </button>
      </div>
      <Notice>
        帳號管理權限不自動取得案件內容。新增或擴大高權限須完成獨立核准，本頁不提供授權操作。
      </Notice>
      {error && <Notice error>{error}</Notice>}
      {notice && <Notice>{notice}</Notice>}
      {target && (
        <form className="ad-card ad-account-action" onSubmit={changeStatus}>
          <h2>{target.status === 'ACTIVE' ? '停用' : '啟用'}帳號</h2>
          <p>{target.email}</p>
          <fieldset disabled={busy}>
            <Field label="變更理由">
              <textarea
                required
                minLength={5}
                maxLength={500}
                rows={3}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </Field>
            <label className="ad-check">
              <input
                type="checkbox"
                required
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              <span>已核對帳號與核准依據，了解既有登入將失效。</span>
            </label>
            <div className="ad-actions">
              <button className="ad-btn ad-primary" type="submit" disabled={!confirmed}>
                確認{target.status === 'ACTIVE' ? '停用' : '啟用'}
              </button>
              <button type="button" className="ad-btn" onClick={() => setTarget(null)}>
                取消操作
              </button>
            </div>
          </fieldset>
        </form>
      )}
      <section className="ad-card">
        <div className="ad-account-list">
          {items.map((account) => (
            <article key={account.id} className="ad-account-row">
              <div className="ad-section-heading">
                <h2>{account.email}</h2>
                <Badge tone={account.status === 'ACTIVE' ? '' : 'neutral'}>
                  {account.status === 'ACTIVE' ? '啟用中' : '已停用'}
                </Badge>
              </div>
              <div className="ad-grants">
                {account.grants.map((grant) => (
                  <div key={grant.id}>
                    <Badge tone="neutral">{roleLabels[grant.role] || grant.role}</Badge>
                    <span>
                      {grant.scope_type === 'GLOBAL'
                        ? '全域範圍'
                        : grant.scope_type === 'SCHEME'
                          ? '方案範圍'
                          : '案件範圍'}
                      {grant.scope_id ? `：${grant.scope_id}` : ''}
                      {grant.expires_at ? ` · 到期 ${date(grant.expires_at)}` : ''}
                    </span>
                  </div>
                ))}
                {!account.grants.length && <span className="ad-muted">目前無有效授權。</span>}
              </div>
              <div className="ad-actions">
                <span className="ad-muted ad-account-id">帳號識別：{account.id}</span>
                {account.id !== me.account.id ? (
                  <button
                    className="ad-btn ad-small"
                    disabled={busy}
                    onClick={() => {
                      setTarget(account);
                      setReason('');
                      setConfirmed(false);
                    }}
                  >
                    {account.status === 'ACTIVE' ? '停用帳號' : '啟用帳號'}
                  </button>
                ) : (
                  <span className="ad-muted">目前登入帳號</span>
                )}
              </div>
            </article>
          ))}
        </div>
        {busy && <p role="status">正在處理帳號資料…</p>}
        <div className="ad-pagination">
          <button
            className="ad-btn"
            disabled={busy || offset === 0}
            onClick={() => void load(Math.max(0, offset - 30))}
          >
            上一頁
          </button>
          <span>第 {Math.floor(offset / 30) + 1} 頁</span>
          <button
            className="ad-btn"
            disabled={busy || !hasMore}
            onClick={() => void load(offset + 30)}
          >
            下一頁
          </button>
        </div>
      </section>
    </>
  );
}
