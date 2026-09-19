import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Bell,
  CircleHelp,
  FilePlus2,
  FileSearch,
  Home,
  Leaf,
  LogOut,
  MessageCircle,
  Route,
  ShieldCheck,
  X,
} from "lucide-react";
import { api, ApiError, errorMessage, setCsrf } from "../shared/api";
import type { Me } from "../shared/types";
import { OFFICIAL } from "../model";
import {
  closeLine,
  getLiffUrl,
  getLineIdentityToken,
  hasLiffConfiguration,
  initLine,
  loginLine,
  readLineEntry,
  type LineAction,
  type LineInitResult,
} from "../line";
import LineEntry from "../components/LineEntry";
import IdentityOCR from "../components/IdentityOCR";
import OcrLab from "../components/OcrLab";
import Safety from "../components/Safety";
import PrivacyCheck from "../components/PrivacyCheck";
import EmailLogin from "./EmailLogin";
import ApplicationForm from "./ApplicationForm";
import CaseTracking from "./CaseTracking";

type View =
  | "home"
  | "line"
  | "apply"
  | "tracking"
  | "supplement"
  | "safety"
  | "ocr";
const navigation = [
  { id: "home", title: "服務首頁", icon: Home },
  { id: "line", title: "LINE 服務入口", icon: MessageCircle },
  { id: "apply", title: "申請青年補助", icon: FilePlus2 },
  { id: "tracking", title: "申請進度", icon: Route },
  { id: "supplement", title: "補件專區", icon: FileSearch },
  { id: "ocr", title: "證件與收據辨識", icon: FileSearch },
  { id: "safety", title: "AI 安全學堂", icon: ShieldCheck },
] as const;
const unavailableLineResult = "這個 LINE 結果連結已失效或無法由目前帳號開啟。請清除連結後重新開始，或返回 LINE 重新完成預檢。";
function lineResultParameters() {
  const direct = new URLSearchParams(location.search);
  const state = direct.get("liff.state") || "";
  const question = state.indexOf("?");
  const nested = new URLSearchParams(question >= 0 ? state.slice(question + 1).split("#")[0] : "");
  return { direct, nested };
}
function readLineResultHint(): { token: string | null; invalid: boolean } {
  const { direct, nested } = lineResultParameters();
  const hints = [...direct.getAll("line_result"), ...nested.getAll("line_result")];
  if (!hints.length) return { token: null, invalid: false };
  // This opaque value is only a routing hint. The authenticated API resolves ownership.
  const valid = hints.every((hint) => /^[A-Za-z0-9_-]{32}$/.test(hint)) && new Set(hints).size === 1;
  return { token: valid ? hints[0] : null, invalid: !valid };
}
function clearLineResultUrl() {
  const url = new URL(location.href);
  url.searchParams.delete("line_result");
  const state = url.searchParams.get("liff.state");
  if (state?.includes("?")) {
    const question = state.indexOf("?");
    const hash = state.indexOf("#", question);
    const nested = new URLSearchParams(state.slice(question + 1, hash < 0 ? undefined : hash));
    if (nested.has("line_result")) {
      nested.delete("line_result");
      const query = nested.toString();
      url.searchParams.set("liff.state", state.slice(0, question) + (query ? `?${query}` : "") + (hash < 0 ? "" : state.slice(hash)));
    }
  }
  history.replaceState(history.state, "", `${url.pathname}${url.search}${url.hash}`);
}
function initialView(): View {
  const result = readLineResultHint();
  if (result.token || result.invalid) return "tracking";
  const entry = readLineEntry();
  if (entry.action) return entry.action;
  if (/^\/(cases|tasks)\//.test(location.pathname)) return "tracking";
  return "home";
}

export default function CitizenApp() {
  const [view, setView] = useState<View>(initialView);
  const [me, setMe] = useState<Me | null>(null);
  const meRef = useRef<Me | null>(null);
  // Keep the owner workspace mounted during reauthentication, only in memory.
  const [workspaceOwner, setWorkspaceOwner] = useState<Me | null>(null);
  const ownerRef = useRef<Me | null>(null);
  const sessionEpoch = useRef(0);
  const active = useRef(true);
  const refreshing = useRef(false);
  const [sessionWarning, setSessionWarning] = useState("");
  const [checkingSession, setCheckingSession] = useState(false);
  const unsavedRef = useRef(false);
  const [leaveAction, setLeaveAction] = useState<{ run: () => void } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [about, setAbout] = useState(false);
  const [generation, setGeneration] = useState(0);
  const [selectedId, setSelectedId] = useState<string>();
  const [reader, setReader] = useState<"identity" | "receipt">("identity");
  const [lineState, setLineState] = useState<LineInitResult>();
  const [lineContext, setLineContext] = useState(readLineEntry().fromLine);
  const [notifications, setNotifications] = useState<
    { id: string; text?: string; status?: string }[] | null
  >(null);
  const [lineBusy, setLineBusy] = useState(false);
  const [lineResultHint, setLineResultHint] = useState(readLineResultHint);
  const [lineResultLoading, setLineResultLoading] = useState(false);
  const [lineResultRetry, setLineResultRetry] = useState(0);
  const [lineResultRetryable, setLineResultRetryable] = useState(false);
  const [lineResultNotice, setLineResultNotice] = useState("");
  const [resolvedLineCase, setResolvedLineCase] = useState<{ accountId: string; caseId: string } | null>(null);
  function acceptSession(next: Me | null) {
    const previous = ownerRef.current;
    sessionEpoch.current += 1;
    if (
      previous?.account.id !== next?.account.id ||
      previous?.roles.join() !== next?.roles.join()
    ) {
      setGeneration((n) => n + 1);
      setSelectedId(undefined);
      setNotifications(null);
      unsavedRef.current = false;
      setLeaveAction(null);
      setResolvedLineCase(null);
      setLineResultNotice("");
    }
    ownerRef.current = next?.roles.includes("applicant") ? next : null;
    setWorkspaceOwner(ownerRef.current);
    meRef.current = next;
    setMe(next);
    setCsrf(next?.csrf_token);
    setSessionWarning("");
  }
  const expireSession = useCallback(() => {
    if (!active.current) return;
    sessionEpoch.current += 1;
    meRef.current = null;
    setMe(null);
    setCsrf(undefined);
    setNotifications(null);
    setLeaveAction(null);
    if (ownerRef.current) {
      setSessionWarning("登入已失效。尚未儲存的內容暫留在本頁記憶體，重新登入同一帳號後可繼續；重新整理或關閉本頁會清除這些內容。");
    }
  }, []);
  const refreshSession = useCallback(async () => {
    if (refreshing.current) return;
    refreshing.current = true;
    setCheckingSession(true);
    const epoch = sessionEpoch.current;
    try {
      const next = await api<Me>("/me");
      if (active.current && epoch === sessionEpoch.current) acceptSession(next);
      else setCsrf(meRef.current?.csrf_token);
    } catch (e) {
      // api announces genuine 401 separately. A network/server error is not logout.
      if (active.current && epoch === sessionEpoch.current && !(e instanceof ApiError && e.status === 401)) {
        setSessionWarning("暫時無法確認登入狀態，已保留本頁內容。請重試連線後再儲存或送件。");
      }
    } finally {
      refreshing.current = false;
      if (active.current) {
        setLoading(false);
        setCheckingSession(false);
      }
    }
  }, []);
  useEffect(() => {
    active.current = true;
    void refreshSession();
    window.addEventListener("focus", refreshSession);
    window.addEventListener("youth:session-expired", expireSession);
    return () => {
      active.current = false;
      window.removeEventListener("focus", refreshSession);
      window.removeEventListener("youth:session-expired", expireSession);
    };
  }, [refreshSession, expireSession]);
  useEffect(() => {
    if (lineContext) void initLine().then(setLineState);
  }, [lineContext]);
  const accountId = me?.account.id;
  const accountRoles = me?.roles.join() || "";
  useEffect(() => {
    if (lineResultHint.invalid) clearLineResultUrl();
    if (!lineResultHint.token || !accountId || !meRef.current?.roles.includes("applicant")) {
      setLineResultLoading(false);
      return;
    }
    const controller = new AbortController();
    const owner = accountId;
    setLineResultLoading(true);
    setLineResultRetryable(false);
    setLineResultNotice("");
    void api<{ case_id: string; snapshot_id: string }>(`/line/results/${encodeURIComponent(lineResultHint.token)}`, { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted || meRef.current?.account.id !== owner || !meRef.current.roles.includes("applicant")) return;
        if (typeof result.case_id !== "string" || !result.case_id || result.case_id.length > 128) {
          throw new ApiError(502, "INVALID_RESPONSE", "結果暫時無法讀取。");
        }
        setLineResultHint({ token: null, invalid: false });
        clearLineResultUrl();
        if (unsavedRef.current) {
          setResolvedLineCase({ accountId: owner, caseId: result.case_id });
          setLineResultNotice("已確認 LINE 指定的案件。請先儲存本頁填答，再開啟該案件。");
        } else {
          setSelectedId(result.case_id);
          setView("tracking");
          setLineResultNotice("已開啟目前登入帳號可存取的 LINE 預檢案件。");
        }
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted || meRef.current?.account.id !== owner) return;
        if (cause instanceof ApiError && cause.status === 401) return;
        if (cause instanceof ApiError && [403, 404, 410].includes(cause.status)) {
          setLineResultHint({ token: null, invalid: true });
          clearLineResultUrl();
          setLineResultNotice(unavailableLineResult);
        } else {
          setLineResultRetryable(true);
          setLineResultNotice("暫時無法讀取 LINE 結果，尚未切換案件。請重試連線。");
        }
      })
      .finally(() => { if (!controller.signal.aborted) setLineResultLoading(false); });
    return () => controller.abort();
  }, [accountId, accountRoles, lineResultHint.token, lineResultHint.invalid, lineResultRetry]);
  const reportUnsaved = useCallback((value: boolean) => { unsavedRef.current = value; }, []);
  function requestLeave(run: () => void) {
    if (unsavedRef.current) {
      setLeaveAction({ run });
      window.scrollTo({ top: 0 });
    }
    else run();
  }
  function navigate(next: View, completed = false) {
    if (next === view) return;
    const run = () => {
      unsavedRef.current = false;
      setLeaveAction(null);
      setError("");
      setView(next);
      window.scrollTo({ top: 0 });
    };
    if (completed) run();
    else requestLeave(run);
  }
  function enterLine(action: LineAction) {
    if (hasLiffConfiguration()) {
      location.assign(getLiffUrl(action));
      return;
    }
    setLineContext(true);
    history.pushState(null, "", `/?entry=line&action=${action}`);
    navigate(action);
  }
  async function logout() {
    try {
      await api("/auth/logout", { method: "POST" });
      acceptSession(null);
      navigate("home", true);
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  async function useLineAccount() {
    if (lineBusy) return;
    setLineBusy(true);
    setError("");
    try {
      const token = await getLineIdentityToken();
      if (meRef.current?.roles.includes("applicant")) {
        const challenge = await api<{ challenge_id: string }>(
          "/identity-links/line/challenges",
          { method: "POST" },
        );
        await api("/identity-links/line", {
          method: "POST",
          json: { challenge_id: challenge.challenge_id, id_token: token },
        });
        acceptSession(await api<Me>("/me"));
      } else
        acceptSession(
          await api<Me>("/auth/line/session", {
            method: "POST",
            json: { id_token: token },
          }),
        );
    } catch (e) {
      setError(
        e instanceof ApiError
          ? errorMessage(e)
          : e instanceof Error
            ? e.message
            : "LINE 登入未完成。",
      );
      if (e instanceof ApiError && e.code === "LINK_REQUIRED") setView("apply");
    } finally {
      setLineBusy(false);
    }
  }
  const requiresAccount = ["apply", "tracking", "supplement"].includes(view);
  const title = navigation.find((n) => n.id === view)?.title || "青年服務";
  const applicant = me?.roles.includes("applicant");
  return (
    <div className={`app-shell citizen-app ${lineContext ? "line-app" : ""}`}>
      <aside className="sidebar">
        <button className="brand" onClick={() => navigate("home")}>
          <span className="brand-icon">
            <Leaf size={26} />
          </span>
          <div>
            <strong>竹青通</strong>
            <small>青年 AI 補助服務</small>
          </div>
        </button>
        <nav aria-label="民眾服務">
          <a className="precheck-nav-entry" href="/precheck">
            <FileSearch size={19} /> 補助預檢
          </a>
          {navigation.map((n) => (
            <button
              key={n.id}
              className={view === n.id ? "active" : ""}
              onClick={() => navigate(n.id)}
            >
              <n.icon size={19} />
              {n.title}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-note">
            <Leaf size={23} />
            <b>申請更順，安全同行。</b>
            <p>
              把時間留給探索，
              <br />
              讓好的工具走進生活。
            </p>
          </div>
          <button className="text-btn" onClick={() => setAbout(true)}>
            <CircleHelp size={16} /> 服務說明
          </button>
        </div>
      </aside>
      <div className="workspace">
        {lineContext && (
          <div className="line-app-bar">
            <button
              onClick={() => {
                if (lineState?.inClient) closeLine();
                else {
                  setLineContext(false);
                  history.replaceState(null, "", "/");
                  navigate("line");
                }
              }}
            >
              <ArrowLeft size={16} /> 返回 LINE
            </button>
            <b>竹青通</b>
            <button aria-label="服務說明" onClick={() => setAbout(true)}>
              <CircleHelp size={18} />
            </button>
          </div>
        )}
        <header className="topbar">
          <div>
            <span>青年服務</span>
            <b>{title}</b>
          </div>
          <div className="topbar-right">
            {me ? (
              <>
                <button
                  className="notification-btn"
                  aria-label="申請通知"
                  onClick={async () => {
                    try {
                      const result = await api<{
                        items: { id: string; text?: string; status?: string }[];
                      }>("/notifications");
                      setNotifications(result.items);
                    } catch (e) {
                      setError(errorMessage(e));
                    }
                  }}
                >
                  <Bell size={19} />
                </button>
                <span className="account-email">
                  {me.account.email || "已登入"}
                </span>
                <button className="text-btn" onClick={() => requestLeave(() => { void logout(); })}>
                  <LogOut size={16} /> 登出
                </button>
              </>
            ) : (
              <button className="btn small" onClick={() => navigate("apply")}>
                登入申請
              </button>
            )}
          </div>
        </header>
        <div className="mobile-navigation">
          <a className="precheck-mobile-entry" href="/precheck">
            <FileSearch size={16} /> 補助預檢
          </a>
          {navigation
            .filter((n) =>
              ["home", "apply", "tracking", "safety"].includes(n.id),
            )
            .map((n) => (
              <button
                key={n.id}
                className={view === n.id ? "active" : ""}
                onClick={() => navigate(n.id)}
              >
                <n.icon size={16} />
                {n.title}
              </button>
            ))}
        </div>
        <main>
          {(lineResultHint.token || lineResultHint.invalid || lineResultNotice) && (
            <section className="notice" role="status" aria-label="LINE 結果連結">
              <p>{lineResultHint.invalid ? unavailableLineResult
                : lineResultLoading ? "正在確認這個 LINE 結果與目前登入帳號…"
                : lineResultNotice || "請先登入，再由服務確認這個 LINE 結果所屬的案件。連結本身不代表已登入或已完成身分查證。"}</p>
              {lineResultRetryable && applicant && <button className="btn small" disabled={lineResultLoading} onClick={() => setLineResultRetry((value) => value + 1)}>重試讀取 LINE 結果</button>}
              {resolvedLineCase && resolvedLineCase.accountId === me?.account.id && <button className="btn small" onClick={() => requestLeave(() => {
                if (meRef.current?.account.id !== resolvedLineCase.accountId) return;
                setSelectedId(resolvedLineCase.caseId);
                setView("tracking");
                setResolvedLineCase(null);
                setLineResultNotice("已開啟目前登入帳號可存取的 LINE 預檢案件。");
              })}>開啟 LINE 指定案件</button>}
              {lineResultHint.invalid && <button className="btn small" onClick={() => requestLeave(() => {
                clearLineResultUrl();
                setLineResultHint({ token: null, invalid: false });
                setLineResultNotice("");
                setLineResultRetryable(false);
                setResolvedLineCase(null);
                setSelectedId(undefined);
                setView("home");
              })}>清除連結並重新開始</button>}
            </section>
          )}
          {sessionWarning && (
            <section className="notice warning" role="status">
              <p>{sessionWarning}</p>
              <button className="btn small" disabled={checkingSession} onClick={() => void refreshSession()}>
                {checkingSession ? "正在確認…" : "重新確認登入狀態"}
              </button>
            </section>
          )}
          {leaveAction && (
            <section className="card navigation-confirmation" role="alertdialog" aria-labelledby="leave-title" aria-describedby="leave-description">
              <h2 id="leave-title">離開前，先確認尚未完成的操作</h2>
              <p id="leave-description">這一頁有尚未儲存的填答或等待確認的送件。離開會清除本頁內容；已保存的草稿與伺服器收件紀錄仍會保留。</p>
              <div className="button-row">
                <button className="btn primary" onClick={() => setLeaveAction(null)}>留在本頁</button>
                <button className="btn" onClick={() => { const next = leaveAction; setLeaveAction(null); unsavedRef.current = false; next.run(); }}>捨棄本頁未保存內容並離開</button>
              </div>
            </section>
          )}
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
          {lineContext && lineState?.state === "login_required" && (
            <div className="notice">
              <p>使用 LINE 帳號繼續，首次申請仍需完成信箱驗證與帳號連結。</p>
              <button
                className="btn"
                onClick={() => loginLine().then(setLineState)}
              >
                使用 LINE 登入
              </button>
            </div>
          )}
          {lineContext && lineState?.state === "error" && (
            <p className="notice warning">{lineState.message}</p>
          )}
          {lineContext && lineState?.state === "ready" && !me?.line_link && (
            <div className="notice">
              <div>
                <b>
                  {applicant
                    ? "連結 LINE，接收案件提醒"
                    : "使用已連結的 LINE 帳號繼續"}
                </b>
                <p>
                  {applicant
                    ? "確認後將目前 LINE 與這個申請帳號連結。"
                    : "首次申請請先驗證信箱，再連結 LINE 帳號。"}
                </p>
                <button
                  className="btn"
                  disabled={lineBusy}
                  onClick={useLineAccount}
                >
                  {lineBusy
                    ? "處理中…"
                    : applicant
                      ? "確認連結 LINE 帳號"
                      : "使用 LINE 帳號登入"}
                </button>
              </div>
            </div>
          )}
          {view === "home" && (
            <>
              <section className="citizen-hero">
                <div>
                  <p className="eyebrow">新竹青年 · AI 工具補助</p>
                  <h1>
                    申請少一點奔波，
                    <br />
                    <em>探索多一點可能。</em>
                  </h1>
                  <p>
                    從準備文件、資料核對到補件追蹤，
                    <br />
                    每一步清楚完成，讓青年與承辦都更省心。
                  </p>
                  <div className="button-row">
                    <a className="btn" href="/precheck">
                      先做補助預檢 <FileSearch size={17} />
                    </a>
                    <button
                      className="btn primary"
                      onClick={() => navigate("line")}
                    >
                      從 LINE 開始申請 <ArrowRight size={17} />
                    </button>
                    <button className="btn" onClick={() => navigate("apply")}>
                      直接線上申請
                    </button>
                  </div>
                  <p className="precheck-entry-note">
                    預檢可匿名使用，先了解工具、通路與需要準備的資料。
                    自填結果尚未核對文件，不等於正式送件或核定。
                  </p>
                </div>
                <div className="hero-journey">
                  <span className="journey-leaf">
                    <Leaf size={42} />
                  </span>
                  <p>你的申請小幫手</p>
                  <h2>
                    準備一次，
                    <br />
                    每一步都看得見。
                  </h2>
                  {[
                    "拍攝證件，核對資料",
                    "備妥收據，檢查金額",
                    "收到提醒，及時補件",
                  ].map((s, i) => (
                    <div key={s}>
                      <b>0{i + 1}</b>
                      <span>{s}</span>
                    </div>
                  ))}
                </div>
              </section>
              <section className="citizen-benefits">
                {[
                  [
                    FileSearch,
                    "先核對，再送件",
                    "證件文字辨識與文件清單，協助提早發現漏件和欄位差異。",
                  ],
                  [
                    Route,
                    "有進度，就知道",
                    "草稿、收件、補件與核定紀錄，都能回到同一個入口查看。",
                  ],
                  [
                    ShieldCheck,
                    "用 AI，也懂安全",
                    "在申請過程學會保護個資、辨識風險與核對工具來源。",
                  ],
                ].map(([Icon, name, body], i) => {
                  const I = Icon as typeof Leaf;
                  return (
                    <article className="card" key={i}>
                      <I size={26} />
                      <h3>{name as string}</h3>
                      <p>{body as string}</p>
                    </article>
                  );
                })}
              </section>
              <section className="card next-step">
                <div>
                  <p className="eyebrow">準備好，再出發</p>
                  <h2>先看看需要哪些文件</h2>
                  <p>
                    身分證正反面、官方訂閱憑證、臺幣付款證明、本人存摺封面，以及親筆簽名切結書。
                  </p>
                </div>
                <a
                  className="btn"
                  href={OFFICIAL}
                  target="_blank"
                  rel="noreferrer"
                >
                  查看市府申請須知 ↗
                </a>
              </section>
            </>
          )}
          {view === "line" && <LineEntry onSelect={enterLine} />}
          {view === "safety" && (
            <>
              <Safety />
              <PrivacyCheck />
            </>
          )}
          {view === "ocr" && (
            <>
              <div className="page-heading">
                <p className="eyebrow">文件智慧辨識</p>
                <h1>讀取資料，確認每一個細節。</h1>
                <p>影像在裝置內辨識。確認欄位後，再決定要附上的申請文件。</p>
              </div>
              <div className="reader-tabs">
                <button
                  className={reader === "identity" ? "active" : ""}
                  onClick={() => setReader("identity")}
                >
                  身分證正反面
                </button>
                <button
                  className={reader === "receipt" ? "active" : ""}
                  onClick={() => setReader("receipt")}
                >
                  收據與其他文件
                </button>
              </div>
              {reader === "identity" ? <IdentityOCR /> : <OcrLab />}
            </>
          )}
          {requiresAccount && (loading ? (
              <p role="status">正在確認登入狀態…</p>
            ) : !me ? (
              <EmailLogin onLogin={acceptSession} />
            ) : !applicant ? (
              <section className="card">
                <h2>此入口提供民眾申請</h2>
                <p>
                  目前登入的是工作帳號。請前往管理後台，或登出後以申請人帳號登入。
                </p>
                <a className="btn primary" href="/admin/">
                  前往管理後台
                </a>
              </section>
            ) : null)}
          {requiresAccount && workspaceOwner && (
            <div hidden={!applicant || me?.account.id !== workspaceOwner.account.id}>
            {view === "apply" ? (
              <ApplicationForm
                key={`${generation}-${selectedId || "new"}`}
                me={workspaceOwner}
                caseId={selectedId}
                onUnsavedChange={reportUnsaved}
                onSubmitted={(id) => {
                  setSelectedId(id);
                  navigate("tracking", true);
                }}
                onSafety={() => navigate("safety", true)}
              />
            ) : (
              <CaseTracking
                key={`${generation}-${view}`}
                selectedId={selectedId}
                supplementOnly={view === "supplement"}
                onUnsavedChange={reportUnsaved}
                onEdit={(id) => {
                  setSelectedId(id);
                  navigate("apply");
                }}
              />
            )}
            </div>
          )}
        </main>
        <footer>
          <span>竹青通 · 青年 AI 補助服務</span>
          <a href={OFFICIAL} target="_blank" rel="noreferrer">
            市府公告與申請須知 ↗
          </a>
        </footer>
      </div>
      {about && (
        <div className="modal-backdrop" onClick={() => setAbout(false)}>
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="服務說明"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="card-heading">
              <h2>服務說明</h2>
              <button
                className="icon-btn"
                aria-label="關閉服務說明"
                onClick={() => setAbout(false)}
              >
                <X />
              </button>
            </div>
            <p>
              竹青通協助青年準備 AI
              工具補助資料，並提供案件、補件與承辦核對流程。送件資料儲存於此服務後端；正式市府收件仍須完成機關系統介接與啟用。
            </p>
            <p>
              證件與收據辨識在裝置上進行，未送往外部
              AI。只有明確選擇上傳的附件會傳至服務後端；證件真偽、本人身分與補助資格仍需正式審查。
            </p>
            <p>
              {hasLiffConfiguration()
                ? "LINE 前端識別碼已設定；登入綁定與通知仍以實際後端設定為準。"
                : "LINE 選單入口已備妥，正式連接需設定 LIFF 識別碼與公開 HTTPS 網址。"}
            </p>
            <p>
              目前提供申請與審查紀錄；核銷及實際撥款需機關出納系統介接，結案不代表已匯款。
            </p>
            <button
              className="btn primary full"
              onClick={() => setAbout(false)}
            >
              了解
            </button>
          </section>
        </div>
      )}
      {notifications && (
        <div className="modal-backdrop" onClick={() => setNotifications(null)}>
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="申請通知"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="card-heading">
              <h2>申請通知</h2>
              <button
                className="icon-btn"
                aria-label="關閉通知"
                onClick={() => setNotifications(null)}
              >
                <X />
              </button>
            </div>
            {notifications.length ? (
              notifications.map((n) => (
                <article className="notice" key={n.id}>
                  <p>你的案件有新進度，請至申請進度查看。</p>
                  <button
                    className="btn small"
                    onClick={() => {
                      setNotifications(null);
                      navigate("tracking");
                    }}
                  >
                    查看案件
                  </button>
                </article>
              ))
            ) : (
              <p>目前沒有新的通知。</p>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
