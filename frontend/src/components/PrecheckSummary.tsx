import { useEffect, useId, useState } from 'react';
import { api, errorMessage } from '../shared/api';

type Check = {
  check_id?: string;
  title?: string;
  reason?: string;
  message?: string;
  next_step?: string;
  execution_status?: string;
  outcome?: string | null;
  evidence_type?: string;
  executed_at?: string;
  redacted?: boolean;
  status_counts?: Record<string, unknown> | null;
  source?: Record<string, unknown>;
};

type PrecheckSnapshot = {
  id?: string;
  previous_snapshot_id?: string | null;
  rules_version?: string;
  catalog_version?: string;
  executed_at?: string;
  history_details_redacted?: boolean;
  result?: Record<string, unknown>;
  differences?: Record<string, unknown>;
};

type PrecheckResponse = {
  latest: PrecheckSnapshot | null;
  differences?: Record<string, unknown> | null;
  current_local_application_history?: Check | null;
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function text(value: unknown, fallback = '未提供'): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function count(value: unknown): string {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? String(value) : '未提供';
}

function checks(value: unknown): Check[] {
  return Array.isArray(value)
    ? value.filter((item) => item !== null && typeof item === 'object' && !Array.isArray(item)) : [];
}

function money(value: unknown): string {
  if (typeof value !== 'string' || !/^\d{1,18}(?:\.\d{1,18})?$/.test(value)) return '待確認';
  const [whole, fraction] = value.split('.');
  return `NT$ ${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${fraction ? `.${fraction}` : ''}`;
}

function time(value: unknown): string {
  if (typeof value !== 'string' || !value) return '未提供';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '待確認'
    : date.toLocaleString('zh-TW', { timeZone: 'Asia/Taipei', hour12: false });
}

function sourceUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : undefined;
  } catch {
    return undefined;
  }
}

function status(check: Check): string {
  if (check.redacted === true) return '歷史來源已依權限隱藏，待確認';
  if (check.execution_status === 'pending') return '尚未完成';
  if (check.execution_status === 'failed') return '執行失敗，待確認';
  if (check.execution_status === 'not_applicable') return '此情境不適用';
  if (check.outcome === 'action_needed') return '需要處理';
  if (check.outcome === 'manual_review') return '需要人工確認';
  if (check.execution_status === 'completed' && check.outcome === 'no_issue') {
    if (check.evidence_type === 'server_record') return '授權紀錄檢查未見異常';
    return check.evidence_type === 'self_report' ? '自填預檢未見異常' : '已執行檢查未見異常，依據類型待確認';
  }
  return '狀態待確認';
}

function evidenceLabel(check: Check): string {
  if (check.redacted === true) return '依據：歷史紀錄來源目前不可確認，已隱藏';
  if (check.evidence_type === 'server_record') return '依據：伺服器授權紀錄';
  if (check.evidence_type === 'self_report') return '依據：使用者自述，尚未核對文件';
  return '依據類型待確認';
}

function FindingList({ items }: { items: Check[] }) {
  return <ul className="precheck-summary-findings">
    {items.map((item, index) => <li key={`${text(item.check_id, 'check')}-${index}`}>
      <strong>{text(item.title, '預檢項目')}</strong>
      <span className="precheck-summary-status">{status(item)}</span>
      <p className="precheck-summary-muted">{evidenceLabel(item)}</p>
      <p>{text(item.reason, text(item.message, '請重新預檢或洽承辦確認。'))}</p>
      {text(item.next_step, '') && <p className="precheck-summary-muted">下一步：{text(item.next_step)}</p>}
    </li>)}
  </ul>;
}

/** Read-only saved evidence. Raw answers and identity/payment details are deliberately not rendered. */
export default function PrecheckSummary({ caseId }: { caseId: string }) {
  const heading = useId();
  const currentHistoryHeading = useId();
  const [reload, setReload] = useState(0);
  const [state, setState] = useState<{
    caseId: string; loading: boolean; response: PrecheckResponse | null; error: string;
  }>({ caseId: '', loading: true, response: null, error: '' });

  useEffect(() => {
    const controller = new AbortController();
    setState({ caseId, loading: true, response: null, error: '' });
    void api<PrecheckResponse>(`/cases/${encodeURIComponent(caseId)}/precheck`, { signal: controller.signal })
      .then((response) => {
        if (!controller.signal.aborted) setState({ caseId, loading: false, response, error: '' });
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) {
          setState({ caseId, loading: false, response: null, error: errorMessage(cause) });
        }
      });
    return () => controller.abort();
  }, [caseId, reload]);

  // Hide the old case synchronously, before the effect starts the next request.
  const current = state.caseId === caseId ? state : { loading: true, response: null, error: '' };
  const latest = record(record(current.response).latest);
  const result = record(latest.result);
  const summary = record(result.summary);
  const snapshot = record(result.snapshot);
  const estimate = record(result.estimate);
  const differences = record(latest.differences ?? record(current.response).differences);
  const allChecks = checks(result.checks);
  const findings = allChecks.filter((item) => item.execution_status !== 'not_applicable'
    && (item.execution_status !== 'completed' || item.outcome !== 'no_issue'));
  const source = Array.isArray(result.sources) ? record(result.sources[0])
    : record(record(allChecks[0]).source);
  const sourceLink = sourceUrl(source.url);
  const hasSnapshot = Object.keys(latest).length > 0;
  const historyRedacted = latest.history_details_redacted === true || allChecks.some((item) => item.redacted === true);
  const currentHistory = record(record(current.response).current_local_application_history) as Check;
  const hasCurrentHistory = Object.keys(currentHistory).length > 0;
  const historyCounts = record(currentHistory.status_counts);
  const showHistoryCounts = currentHistory.execution_status === 'completed'
    && currentHistory.redacted !== true && Object.keys(historyCounts).length > 0;
  const historyGroups = [
    ['draft', '草稿'], ['submitted_or_under_review', '已送出或審查中'],
    ['withdrawn', '已撤回'], ['not_approved', '未核准'],
    ['approved_payment_unconfirmed', '已核准，付款未確認'], ['other_unconfirmed', '其他待確認'],
  ] as const;
  const changedRules = differences.rule_changed === true || differences.catalog_changed === true
    || differences.configuration_changed === true;
  const changedTransactions = differences.transaction_set_changed === true
    || differences.transaction_changes_require_review === true;
  const differenceGroups = [
    ['new_issues', '新增問題'], ['resolved_issues', '已解除問題'],
    ['still_pending', '仍待確認'], ['lost_confirmation', '原已完成、現在無法確認'],
  ] as const;

  return <section className="precheck-summary" aria-labelledby={heading} aria-busy={current.loading}>
    <style>{styles}</style>
    <div className="precheck-summary-heading">
      <div><span className="precheck-summary-muted">行政預檢 · 保存紀錄</span><h2 id={heading}>預檢摘要</h2></div>
      <button type="button" disabled={current.loading} onClick={() => setReload((value) => value + 1)}>重新讀取</button>
    </div>
    <p className="precheck-summary-notice">申請前預檢包含使用者自述；伺服器授權紀錄會另行標示。尚未核對文件，保存不等於正式送件，不保留期限或額度，也不變更案件核定與財務狀態。</p>
    {current.loading ? <p role="status">正在讀取這件案件的預檢摘要…</p>
      : current.error ? <div role="alert"><p>{current.error}</p><p className="precheck-summary-muted">可重新讀取；此操作不會修改案件。</p></div>
      : !hasSnapshot ? <p>此案件尚未保存預檢結果。可先開啟補助預檢，完成後依權限保存至草稿。</p>
      : <>
        <p className="precheck-summary-mode">{result.demo === true || result.mode === 'demo'
          ? 'DEMO 演示紀錄：合成規則，不代表政府公告。'
          : result.mode === 'public_advisory'
            ? '公開來源預檢：來源已核對不等於機關已確認系統規格，不自動核定或拒收。'
            : '已保存的自填預檢紀錄，不代表文件或資格已驗證。'}</p>
        <dl className="precheck-summary-meta">
          <div><dt>規則版本</dt><dd>{text(latest.rules_version, text(result.rules_version))}</dd></div>
          <div><dt>目錄版本</dt><dd>{text(latest.catalog_version, text(result.catalog_version))}</dd></div>
          <div><dt>來源核對日期</dt><dd>{text(snapshot.source_checked_date, '未提供；請查看規則來源')}</dd></div>
          <div><dt>執行時間（臺北）</dt><dd>{time(latest.executed_at ?? result.executed_at)}</dd></div>
        </dl>
        {sourceLink && <p><a href={sourceLink} target="_blank" rel="noopener noreferrer">查看規則來源</a></p>}
        {historyRedacted && <p className="precheck-summary-notice">部分歷史紀錄的來源權限或證據目前無法確認，已依權限隱藏。此處呈現目前可見的快照內容，原保存紀錄未被改寫；隱藏不表示沒有其他案件或已排除重複補助。</p>}
        <p><strong>{text(summary.message, '摘要資料不完整，請重新預檢或洽承辦確認。')}</strong></p>
        <dl className="precheck-summary-counts">
          <div><dt>必要檢查</dt><dd>{count(summary.required_total)}</dd></div>
          <div><dt>已完成</dt><dd>{count(summary.completed)}</dd></div>
          <div><dt>未完成</dt><dd>{count(summary.incomplete)}</dd></div>
          <div><dt>需處理／人工確認</dt><dd>{count(summary.issues)}</dd></div>
        </dl>
        {Object.keys(estimate).length > 0 && <div className="precheck-summary-estimate">
          <h3>依填答試算</h3>
          <p>{estimate.available === true ? <><strong>{money(estimate.estimated_subsidy_twd)}</strong> · 條件式試算，非核定金額</>
            : '目前無法提供試算金額；未知費用不視為 0。'}</p>
          {estimate.available === true && <p>自述臺幣費用 {money(estimate.eligible_cost_twd)}；補助上限 {money(estimate.cap_twd)}。</p>}
          {estimate.proof_pending === true && <p>身分證明待確認；不同加碼身分不疊加比例與上限。</p>}
          <p className="precheck-summary-muted">{text(estimate.reason, '費用、文件與實際核銷金額仍待確認。')}</p>
        </div>}
        <h3>需留意的項目</h3>
        {findings.length ? <>
          <FindingList items={findings.slice(0, 4)} />
          {findings.length > 4 && <details><summary>另有 {findings.length - 4} 項，展開查看</summary><FindingList items={findings.slice(4)} /></details>}
        </> : <p>這份快照沒有列出待處理項目；仍以檢查覆蓋數與摘要說明為準，尚未核對文件。</p>}
        <details className="precheck-summary-diff"><summary>與前次保存的差異</summary>
          {!latest.previous_snapshot_id ? <p>這是首份保存紀錄，尚無前次快照可比較。</p> : <>
            {differences.history_details_redacted === true && <p>{text(differences.history_redaction_message, '部分歷史紀錄差異的來源權限無法確認，已隱去，不作新增或已解除問題判定。')}</p>}
            {changedRules && <p>規則、目錄或設定已變更；結果差異可能來自版本更新，不一定是填答修改。</p>}
            {changedTransactions && <p>{text(differences.transaction_change_message, '逐月交易內容或順序已變更，請逐筆核對前後快照，不以列次認定問題已解除。')}</p>}
            {differenceGroups.map(([key, label]) => {
              const items = checks(differences[key]);
              return <p key={key}><strong>{label}：{differences.history_details_redacted === true ? '可見 ' : ''}{items.length} 項</strong>{items.length > 0
                && <> · {items.map((item) => text(item.title, '預檢項目')).join('、')}</>}</p>;
            })}
            <p className="precheck-summary-muted">問題解除僅指自填預檢結果的變化，不代表資格已確認。</p>
          </>}
        </details>
        <p className="precheck-summary-muted">資安提醒與行政預檢分開；此摘要不代表已完成防毒、文件真偽、即時經費或跨機關重複補助查核。</p>
      </>}
    {!current.loading && !current.error && <section className="precheck-current-history" aria-labelledby={currentHistoryHeading}>
      <h3 id={currentHistoryHeading}>目前授權的本機案件紀錄</h3>
      <p className="precheck-summary-muted">這是本次讀取時另行查詢的紀錄提示，只涵蓋目前帳號有權讀取的同申請人、同計畫其他案件。它不更新上方已保存快照的檢查數或執行時間。</p>
      {hasCurrentHistory ? <>
        <dl className="precheck-summary-meta">
          <div><dt>本次查詢時間（臺北）</dt><dd>{time(currentHistory.executed_at)}</dd></div>
          <div><dt>紀錄依據</dt><dd>{evidenceLabel(currentHistory)}</dd></div>
        </dl>
        <p><strong>{status(currentHistory)}</strong></p>
        <p>{text(currentHistory.reason, text(currentHistory.message, '本機紀錄查詢尚未提供可確認結果。'))}</p>
        {showHistoryCounts ? <dl className="precheck-history-counts">
          {historyGroups.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{count(historyCounts[key]) === '未提供' ? '未提供' : `${count(historyCounts[key])} 件`}</dd></div>)}
        </dl> : <p className="precheck-summary-muted">紀錄數量尚未提供或已依權限隱藏，不能視為 0 件。</p>}
        {text(currentHistory.next_step, '') && <p>下一步：{text(currentHistory.next_step)}</p>}
      </> : <p>目前沒有本次授權紀錄的查詢結果，不能據此判斷沒有其他案件。</p>}
      <p className="precheck-summary-notice">實際撥款及已領補助狀態尚未確認；核准不等於已撥款，收件不等於已領補助。沒有跨機關資料介接，不能宣稱已排除跨機關重複補助。草稿、撤回及未核准紀錄不視為已領補助。</p>
    </section>}
    <p className="precheck-summary-link"><a href="/precheck">開啟補助預檢</a><span>重新填答與查看完整備件清單</span></p>
  </section>;
}

const styles = `
.precheck-summary{padding:24px 26px;border:1px solid #e1e7d9;background:#fffef9;border-radius:13px;margin:0 0 22px;color:#284432;line-height:1.7;overflow-wrap:anywhere;font-size:14px}
.precheck-summary *{box-sizing:border-box}.precheck-summary h2{font-size:20px;margin:2px 0 0}.precheck-summary h3{font-size:15px;margin:20px 0 8px}.precheck-summary p{margin:10px 0}
.precheck-summary-heading{display:flex;align-items:center;justify-content:space-between;gap:16px}.precheck-summary button{font:inherit;border:1px solid #cfdcc7;border-radius:8px;background:#fffef9;color:#345e40;padding:8px 14px;cursor:pointer;flex-shrink:0}.precheck-summary button:disabled{opacity:.55;cursor:wait}
.precheck-summary a{color:#345e40;text-decoration:underline;text-underline-offset:3px}.precheck-summary a:focus-visible,.precheck-summary button:focus-visible,.precheck-summary summary:focus-visible{outline:3px solid #72935e;outline-offset:3px}
.precheck-summary-muted{color:#61735e;font-size:12px}.precheck-summary-notice,.precheck-summary-mode{padding:12px 14px;background:#f0f6e6;border-left:3px solid #789665}.precheck-summary-mode{background:#fcf4e2;border-color:#b29457}
.precheck-summary-meta,.precheck-summary-counts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:18px 0}.precheck-summary dt{font-size:12px;color:#61735e}.precheck-summary dd{margin:2px 0 0}.precheck-summary-counts{grid-template-columns:repeat(4,minmax(0,1fr));padding:14px 0;border-top:1px solid #e1e7d9;border-bottom:1px solid #e1e7d9}.precheck-summary-counts dd{font-size:20px;font-weight:600}
.precheck-summary-estimate{padding:0 0 12px;border-bottom:1px solid #e1e7d9}.precheck-summary-estimate h3{margin-top:0}.precheck-summary-findings{list-style:none;padding:0;margin:8px 0}.precheck-summary-findings li{padding:12px 0;border-bottom:1px solid #e1e7d9}.precheck-summary-findings p{margin:5px 0}.precheck-summary-status{display:inline-block;font-size:12px;margin-left:10px;color:#866b37}
.precheck-summary summary{cursor:pointer;padding:10px 0;font-weight:600}.precheck-summary-diff{margin-top:16px;border-top:1px solid #e1e7d9}.precheck-summary-link{display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px;padding-top:12px}.precheck-summary-link span{font-size:12px;color:#61735e}
.precheck-current-history{margin-top:24px;padding-top:16px;border-top:2px solid #d6dfce}.precheck-current-history h3{margin-top:0}.precheck-history-counts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:14px;background:#f3f6ed}.precheck-history-counts dd{font-variant-numeric:tabular-nums}
@media(max-width:600px){.precheck-summary{padding:18px 16px}.precheck-summary-heading{align-items:flex-start}.precheck-summary-meta,.precheck-summary-counts{grid-template-columns:repeat(2,minmax(0,1fr))}.precheck-summary-status{display:block;margin-left:0}}
`;
