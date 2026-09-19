/** Same-origin API only. Session cookies are HttpOnly; CSRF lives in memory. */
let csrf: string | undefined;
export function setCsrf(value?: string) {
  csrf = value;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public fields: { field: string; message: string }[] = [],
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 412)
      return "資料已由其他操作更新。請重新讀取案件，確認最新內容後再操作。";
    if (error.code === "REAUTH_REQUIRED")
      return "這項操作需要近期登入驗證，請重新登入後再操作。";
    return error.message;
  }
  return "連線未完成。請先重新讀取案件確認是否已成功，再以原操作重試。";
}

export type ApiOptions = RequestInit & {
  json?: unknown;
  etag?: string;
  idempotencyKey?: string;
};
export async function api<T>(
  path: string,
  options: ApiOptions = {},
): Promise<T> {
  if (!path.startsWith("/") || path.startsWith("//") || path.includes(".."))
    throw new Error("Invalid API path");
  const { json, etag, idempotencyKey, ...init } = options;
  const headers = new Headers(init.headers);
  const method = (init.method || "GET").toUpperCase();
  if (json !== undefined) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrf)
    headers.set("X-CSRF-Token", csrf);
  if (etag) headers.set("If-Match", etag);
  if (idempotencyKey) headers.set("Idempotency-Key", idempotencyKey);
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    method,
    headers,
    credentials: "same-origin",
    cache: "no-store",
    body: json !== undefined ? JSON.stringify(json) : init.body,
  });
  if (response.status === 204) return undefined as T;
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401) {
      csrf = undefined;
      if (typeof window !== "undefined")
        window.dispatchEvent(new Event("youth:session-expired"));
    }
    throw new ApiError(
      response.status,
      payload?.error?.code || "REQUEST_FAILED",
      payload?.error?.message || "服務暫時無法處理，請稍後再試。",
      payload?.error?.field_errors || [],
    );
  }
  if (!payload || typeof payload !== "object" || !("data" in payload))
    throw new ApiError(
      502,
      "INVALID_RESPONSE",
      "服務回應格式不正確，請稍後重試。",
    );
  if (typeof payload.data?.csrf_token === "string")
    csrf = payload.data.csrf_token;
  return payload.data as T;
}

export function operationKey() {
  return crypto.randomUUID();
}

export type UploadedFile = {
  file_id: string;
  file_version_id: string;
  case_id: string;
  task_id: string | null;
  document_type: string;
  file_name: string;
  scan_status: string;
  allowed_actions: string[];
};

/** The API grants one immutable upload, which is completed before it can be submitted. */
export async function uploadFile(
  caseId: string,
  documentType: string,
  file: File,
  taskId?: string,
): Promise<UploadedFile> {
  if (!["image/jpeg", "image/png", "application/pdf"].includes(file.type)) {
    throw new ApiError(
      415,
      "FILE_TYPE_NOT_ALLOWED",
      "請使用 JPEG、PNG 或 PDF 文件。",
    );
  }
  if (!file.size || file.size > 20_971_520)
    throw new ApiError(
      413,
      "FILE_TOO_LARGE",
      "每份文件需小於或等於 20 MiB，且不可為空白檔案。",
    );
  const intent = await api<{
    file_id: string;
    file_version_id: string;
    upload_url: string;
    upload_headers: Record<string, string>;
  }>("/files/upload-intents", {
    method: "POST",
    json: {
      case_id: caseId,
      task_id: taskId || null,
      document_type: documentType,
      file_name: file.name,
      size_bytes: file.size,
      content_type: file.type,
    },
  });
  // A server-returned URL must never send cookies/CSRF/file data to another origin.
  if (
    !/^\/api\/v1\/files\/[^/]+\/content$/.test(
      intent.upload_url.split("?")[0],
    ) ||
    intent.upload_url.includes("?")
  ) {
    throw new ApiError(
      502,
      "UPLOAD_URL_INVALID",
      "上傳位置不正確，請重新整理後再試。",
    );
  }
  await api(intent.upload_url.slice("/api/v1".length), {
    method: "PUT",
    body: file,
    headers: intent.upload_headers,
  });
  return api<UploadedFile>(`/files/${intent.file_id}/complete`, {
    method: "POST",
    json: { file_version_id: intent.file_version_id },
  });
}
