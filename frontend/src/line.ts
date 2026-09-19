/**
 * LINE entry adapter. Entry hints are routing only, never authentication.
 * ID tokens are requested only on explicit service-login/link actions and sent
 * straight to our backend for verification. They are never persisted.
 */
export type LineAction = "apply" | "tracking" | "supplement" | "safety";
export type LineInitResult = {
  state: "unconfigured" | "ready" | "login_required" | "error";
  inClient: boolean;
  message: string;
};

interface LiffSdk {
  init(options: {
    liffId: string;
    withLoginOnExternalBrowser: false;
  }): Promise<void>;
  isInClient(): boolean;
  isLoggedIn(): boolean;
  login(options: { redirectUri: string }): void;
  closeWindow(): void;
  getIDToken(): string | null;
}

type LiffWindow = Window & { liff?: LiffSdk };
const SDK_URL = "https://static.line-scdn.net/liff/edge/2/sdk.js";
const ACTIONS: readonly LineAction[] = [
  "apply",
  "tracking",
  "supplement",
  "safety",
];
const SDK_TIMEOUT_MS = 15000;
const INIT_TIMEOUT_MS = 20000;

let sdkLoading: Promise<LiffSdk> | null = null;
let sdkInitialization: Promise<void> | null = null;
let initializedSdk: LiffSdk | null = null;
let activeAttempt: Promise<LineInitResult> | null = null;

function configuredId(): string {
  const env = (import.meta as ImportMeta & { env?: { VITE_LIFF_ID?: string } })
    .env;
  return typeof env?.VITE_LIFF_ID === "string" ? env.VITE_LIFF_ID.trim() : "";
}

export function hasLiffConfiguration(): boolean {
  return configuredId().length > 0;
}

/** The backend must verify this token; a decoded profile is never authentication. */
export async function getLineIdentityToken(): Promise<string> {
  const result = await initLine();
  if (result.state !== "ready") throw new Error("請先完成 LINE 登入後再試。");
  const token = sdkFromWindow()?.getIDToken();
  if (!token)
    throw new Error(
      "LINE 尚未提供登入憑證，請確認應用程式已啟用 openid 權限。",
    );
  return token;
}

function sdkFromWindow(): LiffSdk | undefined {
  return typeof window === "undefined"
    ? undefined
    : (window as LiffWindow).liff;
}

/** Reads routing hints without altering liff.* or the current URL. */
export function readLineEntry(): {
  fromLine: boolean;
  action: LineAction | null;
} {
  if (typeof window === "undefined") return { fromLine: false, action: null };
  const direct = new URLSearchParams(window.location.search);
  // URLSearchParams already decodes the outer liff.state value once.
  const state = direct.get("liff.state") || "";
  const question = state.indexOf("?");
  const nested = new URLSearchParams(
    question >= 0 ? state.slice(question + 1).split("#")[0] : "",
  );
  // During the primary redirect, the destination action is in liff.state.
  const candidate = nested.has("action")
    ? nested.get("action")
    : direct.get("action");
  return {
    fromLine: direct.get("entry") === "line" || nested.get("entry") === "line",
    action: ACTIONS.includes(candidate as LineAction)
      ? (candidate as LineAction)
      : null,
  };
}

export function getLiffUrl(action: LineAction): string {
  const query = new URLSearchParams({ entry: "line", action }).toString();
  const id = configuredId();
  return id
    ? `https://liff.line.me/${encodeURIComponent(id)}?${query}`
    : `?${query}`;
}

class LineConnectionError extends Error {}

function withinTimeout<T>(
  pending: Promise<T>,
  milliseconds: number,
  message: string,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(
      () => reject(new LineConnectionError(message)),
      milliseconds,
    );
    pending.then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        window.clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function loadSdk(): Promise<LiffSdk> {
  const existing = sdkFromWindow();
  if (existing) return Promise.resolve(existing);
  if (sdkLoading) return sdkLoading;
  const attempt = new Promise<LiffSdk>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SDK_URL;
    script.async = true;
    script.dataset.lineLiffAdapter = "true";
    const cleanup = () => {
      window.clearTimeout(timer);
      script.onload = null;
      script.onerror = null;
    };
    const fail = (message: string) => {
      cleanup();
      script.remove();
      reject(new LineConnectionError(message));
    };
    const timer = window.setTimeout(
      () => fail("LINE 連線元件載入逾時，請檢查網路後重試。網站功能仍可使用。"),
      SDK_TIMEOUT_MS,
    );
    script.onload = () => {
      const sdk = sdkFromWindow();
      if (!sdk) {
        fail("LINE 連線元件未能啟用，請重試。網站功能仍可使用。");
        return;
      }
      cleanup();
      resolve(sdk);
    };
    script.onerror = () =>
      fail("LINE 連線元件載入失敗，請檢查網路後重試。網站功能仍可使用。");
    document.head.appendChild(script);
  });
  sdkLoading = attempt;
  // Failed downloads are retryable; concurrent callers share one script.
  void attempt.catch(() => {
    if (sdkLoading === attempt) sdkLoading = null;
  });
  return attempt;
}

function connectionState(sdk: LiffSdk): LineInitResult {
  const inClient = sdk.isInClient();
  return sdk.isLoggedIn()
    ? {
        state: "ready",
        inClient,
        message: "LINE 連線已就緒；這不代表政府身分驗證或案件授權。",
      }
    : {
        state: "login_required",
        inClient,
        message: inClient
          ? "LINE 登入狀態尚未就緒，請從 LINE 重新開啟服務。"
          : "如需 LINE 連線，請按「使用 LINE 登入」。網站功能仍可使用。",
      };
}

async function connect(): Promise<LineInitResult> {
  try {
    const sdk = await loadSdk();
    if (initializedSdk !== sdk) {
      if (!sdkInitialization) {
        // Keep a pending initialization after a UI timeout: the SDK has no
        // cancellation API. A retry waits for it instead of initializing twice.
        const initialization = Promise.resolve()
          .then(() =>
            sdk.init({
              liffId: configuredId(),
              withLoginOnExternalBrowser: false,
            }),
          )
          .then(() => {
            initializedSdk = sdk;
          });
        sdkInitialization = initialization;
        void initialization.catch(() => {
          if (sdkInitialization === initialization) sdkInitialization = null;
        });
      }
      await withinTimeout(
        sdkInitialization,
        INIT_TIMEOUT_MS,
        "LINE 連線等待逾時，可以重試；若持續未完成，請重新開啟服務。網站功能仍可使用。",
      );
    }
    return connectionState(sdk);
  } catch (error) {
    // Never display/log raw SDK errors or URLs, which may include credentials.
    return {
      state: "error",
      inClient: safeInClient(),
      message:
        error instanceof LineConnectionError
          ? error.message
          : "LINE 連線暫時無法完成，請重試或繼續使用網站。設定資訊可在服務說明查看。",
    };
  }
}

/** No SDK/network work without both explicit configuration and entry=line. */
export function initLine(): Promise<LineInitResult> {
  if (typeof window === "undefined" || !readLineEntry().fromLine) {
    return Promise.resolve({
      state: "unconfigured",
      inClient: false,
      message: "目前為網站入口，未啟用 LINE 連線。",
    });
  }
  if (!hasLiffConfiguration()) {
    return Promise.resolve({
      state: "unconfigured",
      inClient: false,
      message: "LINE 連線尚未設定，仍可使用網站服務。",
    });
  }
  if (activeAttempt) return activeAttempt;
  const attempt = connect();
  activeAttempt = attempt;
  void attempt.finally(() => {
    if (activeAttempt === attempt) activeAttempt = null;
  });
  return attempt;
}

function safeInClient(): boolean {
  try {
    return sdkFromWindow()?.isInClient() === true;
  } catch {
    return false;
  }
}

/** Call only in response to the user's explicit login button. */
export async function loginLine(): Promise<LineInitResult> {
  const result = await initLine();
  if (result.state !== "login_required") return result;
  const sdk = sdkFromWindow();
  if (!sdk || result.inClient) return result;
  try {
    // Construct a clean return URL after initialization; do not copy tokens,
    // liff.* query data, a URL fragment, or arbitrary redirect parameters.
    const redirect = new URL(window.location.pathname, window.location.origin);
    redirect.searchParams.set("entry", "line");
    const action = readLineEntry().action;
    if (action) redirect.searchParams.set("action", action);
    sdk.login({ redirectUri: redirect.toString() });
    return { ...result, message: "正在開啟 LINE 登入；登入後將返回服務。" };
  } catch {
    return {
      state: "error",
      inClient: false,
      message: "無法開啟 LINE 登入，請重試或繼續使用網站。",
    };
  }
}

/** Closing a normal browser tab is intentionally a no-op. */
export function closeLine(): void {
  if (!safeInClient()) return;
  try {
    sdkFromWindow()?.closeWindow();
  } catch {
    /* Stay on the current page. */
  }
}
