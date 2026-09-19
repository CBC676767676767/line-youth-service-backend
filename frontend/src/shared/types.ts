export type Me = {
  account: { id: string; email: string | null; status: string };
  roles: string[];
  csrf_token?: string;
  permissions: { role: string; scope_type: string; scope_id: string | null }[];
  line_link: {
    id: string;
    binding_version: number;
    version: number;
    etag: string;
    follow_status: string;
  } | null;
};
export type Page<T> = { items: T[]; next_cursor?: string | null };
export type Scheme = {
  id: string;
  name: string;
  description: string;
  active: boolean;
  schema_version: number;
  document_types: string[];
  rule_version_id?: string;
  required_criteria?: string[];
};
export type TaskData = {
  id: string;
  case_id: string;
  title: string;
  status: string;
  requirement: string;
  acceptance_criteria: string;
  due_at: string;
  task_revision: number;
  is_overdue: boolean;
  version: number;
  etag: string;
  allowed_actions: string[];
  accepted_submission_id?: string | null;
};
export type CaseData = {
  id: string;
  case_no: string;
  scheme_id: string;
  status: string;
  form_data: Record<string, unknown>;
  schema_version: number;
  current_revision_no: number;
  last_business_update_at: string;
  version: number;
  etag: string;
  allowed_actions: string[];
  tasks?: TaskData[];
  decision?: {
    id: string;
    outcome: string;
    reason: string;
    decided_at: string;
  } | null;
  assigned_to?: string | null;
};
export type ReviewItem = {
  id: string;
  criterion_code: string;
  result: string;
  version: number;
  etag: string;
  evidence_refs: Record<string, unknown>[];
  internal_note?: string | null;
  public_reason?: string | null;
};
export const caseLabels: Record<string, string> = {
  DRAFT: "草稿準備中",
  RECEIVED: "已收件",
  UNDER_REVIEW: "承辦審查中",
  DECIDED: "已完成核定",
  CLOSED: "已結案",
  WITHDRAWN: "已撤回",
};
export const taskLabels: Record<string, string> = {
  OPEN: "待補件",
  REOPENED: "請再次補正",
  SUBMITTED: "補件已送出",
  ACCEPTED: "補件已接受",
  CANCELLED: "已取消",
};
export const scanLabels: Record<string, string> = {
  PENDING_UPLOAD: "上傳未完成",
  PENDING_SCAN: "等待安全檢查",
  SCANNING: "安全檢查中",
  CLEAN: "可供閱覽",
  SCAN_FAILED: "安全檢查待重試",
  REJECTED: "文件未通過檢查",
  INFECTED: "文件未通過檢查",
};
export const documentLabels: Record<string, string> = {
  ID_FRONT: "身分證正面",
  ID_BACK: "身分證反面",
  RECEIPT: "訂閱收據／憑證",
  PAYMENT_PROOF: "臺幣付款證明",
  BANK_ACCOUNT: "本人存摺封面",
  AFFIDAVIT: "親筆簽名切結書",
  SPECIAL_STATUS: "特定身分證明",
  RELATIONSHIP: "代付關係與共同切結",
  APPLICATION: "申請文件",
  OTHER: "其他證明文件",
};
