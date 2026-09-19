import { useEffect, useId, useRef, useState } from 'react';
import { prepareImage, recognizeLocally, type OcrProgress, type QualityHint } from '../ocr';
import {
  checkIdentity, createIdentityPracticeImage, emptyIdentityFields, normalizeTaiwanId,
  parseBackOCR, parseBirthDate, parseFrontOCR, rotateIdentityImage,
  type IdentityApplicant, type IdentityFields, type IdentityResult, type IdentitySide,
} from '../identity';
import '../identity.css';

export type IdentityOCRProps = {
  applicant?: IdentityApplicant;
  disabled?: boolean;
  onConfirm?: (result: IdentityResult) => void;
};
type SideKey = 'front' | 'back';
type SideState = IdentitySide & { recognized: boolean; hints: QualityHint[]; notes: string[] };
const emptySide = (): SideState => ({ fileName: '', previewUrl: '', text: '', recognized: false, hints: [], notes: [] });
const labels: Record<keyof IdentityFields, string> = { name: '姓名', idNumber: '身分證字號', birth: '出生日期', address: '戶籍地址' };

export default function IdentityOCR({ applicant, disabled = false, onConfirm }: IdentityOCRProps) {
  const uid = useId();
  const [sides, setSides] = useState<Record<SideKey, SideState>>({ front: emptySide(), back: emptySide() });
  const [fields, setFields] = useState<IdentityFields>(emptyIdentityFields);
  const [extracted, setExtracted] = useState<IdentityFields>(emptyIdentityFields);
  const [busy, setBusy] = useState<{ side: SideKey; label: string } | null>(null);
  const [progress, setProgress] = useState<OcrProgress | null>(null);
  const [error, setError] = useState('');
  const [reviewed, setReviewed] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const operation = useRef(0);
  const locked = useRef(false);
  const abort = useRef<AbortController | null>(null);
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;

  useEffect(() => () => { operation.current++; abort.current?.abort(); }, []);
  useEffect(() => {
    if (disabled) {
      operation.current++; abort.current?.abort(); abort.current = null;
      locked.current = false; setBusy(null); setProgress(null);
    }
  }, [disabled]);
  useEffect(() => { setReviewed(false); }, [applicant?.name, applicant?.birth, applicant?.address, applicant?.idNumber]);

  const checks = checkIdentity(fields, sides.front.recognized, sides.back.recognized, applicant);
  const hasErrors = checks.some(check => check.level === 'error');
  const canConfirm = !disabled && !busy && !hasErrors && reviewed;
  const hasImage = Boolean(sides.front.previewUrl || sides.back.previewUrl);
  const immutable = disabled || Boolean(busy);

  function invalidate() { setReviewed(false); setConfirmed(false); }
  function resetFields(side: SideKey) {
    const clear = side === 'front' ? { name: '', idNumber: '', birth: '' } : { address: '' };
    setFields(previous => ({ ...previous, ...clear }));
    setExtracted(previous => ({ ...previous, ...clear }));
    invalidate();
  }
  function begin(side: SideKey, label: string) {
    if (locked.current || disabledRef.current) return null;
    locked.current = true; setBusy({ side, label }); setError(''); setProgress(null);
    invalidate(); return ++operation.current;
  }
  function finish(token: number) {
    if (token !== operation.current) return;
    locked.current = false; setBusy(null); setProgress(null); abort.current = null;
  }
  function showError(cause: unknown, token: number) {
    if (token !== operation.current) return;
    if (cause instanceof Error && cause.name === 'AbortError') setError('已取消辨識，您可以重新開始。');
    else setError(cause instanceof Error ? cause.message : '圖片處理未完成，請重新選擇或再試一次。');
  }
  async function loadImage(side: SideKey, file?: File, practice = false) {
    if (!file && !practice) return;
    const token = begin(side, '讀取圖片'); if (token === null) return;
    try {
      const source = file ?? await createIdentityPracticeImage(side);
      const prepared = await prepareImage(source);
      if (token !== operation.current || disabledRef.current) return;
      setSides(previous => ({ ...previous, [side]: { fileName: source.name, previewUrl: prepared.previewUrl, text: '', recognized: false, hints: prepared.hints, notes: [] } }));
      resetFields(side);
    } catch (cause) { showError(cause, token); }
    finally { finish(token); }
  }
  async function rotate(side: SideKey) {
    const source = sides[side].previewUrl;
    if (!source) return;
    const token = begin(side, '旋轉圖片'); if (token === null) return;
    try {
      const previewUrl = await rotateIdentityImage(source);
      if (token !== operation.current || disabledRef.current) return;
      setSides(previous => ({ ...previous, [side]: { ...previous[side], previewUrl, text: '', recognized: false, notes: [] } }));
      resetFields(side);
    } catch (cause) { showError(cause, token); }
    finally { finish(token); }
  }
  async function recognize(side: SideKey) {
    const source = sides[side].previewUrl;
    if (!source) return;
    const token = begin(side, '辨識圖片文字'); if (token === null) return;
    const controller = new AbortController(); abort.current = controller;
    // A new run invalidates its old candidate even when the run later fails.
    setSides(previous => ({ ...previous, [side]: { ...previous[side], recognized: false, text: '', notes: [] } }));
    resetFields(side);
    try {
      const result = await recognizeLocally(source, 'chi_tra+eng', value => {
        if (token === operation.current) setProgress(value);
      }, controller.signal);
      if (token !== operation.current || disabledRef.current) return;
      const parsed = side === 'front' ? parseFrontOCR(result.text) : parseBackOCR(result.text);
      setSides(previous => ({ ...previous, [side]: { ...previous[side], text: result.text, recognized: true, notes: parsed.notes } }));
      setFields(previous => ({ ...previous, ...parsed.fields }));
      setExtracted(previous => ({ ...previous, ...parsed.fields }));
    } catch (cause) { showError(cause, token); }
    finally { finish(token); }
  }
  function remove(side: SideKey) {
    if (immutable) return;
    setSides(previous => ({ ...previous, [side]: emptySide() })); resetFields(side); setError('');
  }
  function confirm() {
    if (!canConfirm) return;
    const finalFields = { name: fields.name.trim(), idNumber: normalizeTaiwanId(fields.idNumber), birth: parseBirthDate(fields.birth).iso, address: fields.address.trim() };
    const corrections = (Object.keys(labels) as Array<keyof IdentityFields>)
      .filter(field => extracted[field] !== finalFields[field])
      .map(field => ({ field, before: extracted[field], after: finalFields[field] }));
    const keep = (side: SideState): IdentitySide => ({ fileName: side.fileName, previewUrl: side.previewUrl, text: side.text });
    onConfirm?.({ fields: finalFields, front: keep(sides.front), back: keep(sides.back), checkedAt: new Date().toISOString(), corrections });
    setFields(finalFields); setConfirmed(true);
  }

  return <section className="identity-ocr" aria-labelledby={`${uid}-title`}>
    <div className="identity-heading">
      <div><p className="identity-eyebrow">證件資料核對</p><h3 id={`${uid}-title`}>身分證正反面，先看清楚再帶入</h3></div>
      <span className="identity-badge">資料與格式檢查</span>
    </div>
    <p className="identity-boundary"><strong>照片只在你的裝置內辨識。</strong> 核對姓名、字號、生日與戶籍地址；證件真偽與本人身分仍需正式驗證。</p>
    <ol className="identity-steps"><li>選擇兩面圖片</li><li>分別辨識與核對</li><li>確認後帶入資料</li></ol>
    <p className="identity-help">請讓證件完整入鏡、文字朝上，避開反光。可選 PNG、JPEG、WebP，每張上限 20 MiB；PDF 請先將所需頁面轉成圖片。請勿在他人或公共裝置留下證件資料。</p>
    <div className="identity-sides">
      {(['front', 'back'] as const).map(side => {
        const item = sides[side]; const label = side === 'front' ? '正面' : '反面';
        return <section key={side} className="identity-side" aria-labelledby={`${uid}-${side}`}>
          <div className="identity-side-heading"><h4 id={`${uid}-${side}`}>{label}<small>{side === 'front' ? '姓名、字號、出生日期' : '戶籍地址'}</small></h4><span className={`identity-state ${item.recognized ? 'ready' : ''}`}>{item.recognized ? '已辨識・待核對' : item.previewUrl ? '待辨識' : '尚未選圖'}</span></div>
          <div className="identity-file-actions">
            <label className={`identity-file-button ${immutable ? 'is-disabled' : ''}`}>選擇{label}圖片<input type="file" accept="image/png,image/jpeg,image/webp" aria-label={`選擇${label}圖片`} disabled={immutable} onChange={event => { void loadImage(side, event.currentTarget.files?.[0]); event.currentTarget.value = ''; }} /></label>
            <label className={`identity-file-button ${immutable ? 'is-disabled' : ''}`}>拍攝{label}<input type="file" accept="image/*" capture="environment" aria-label={`拍攝${label}`} disabled={immutable} onChange={event => { void loadImage(side, event.currentTarget.files?.[0]); event.currentTarget.value = ''; }} /></label>
          </div>
          <button type="button" className="identity-text-button" disabled={immutable} onClick={() => void loadImage(side, undefined, true)}>載入{label}練習資料</button>
          {item.previewUrl ? <>
            <a className="identity-preview" href={item.previewUrl} target="_blank" rel="noreferrer" aria-label={`開啟${label}圖片查看細節`}><img src={item.previewUrl} alt={`${label}圖片：${item.fileName}`} /></a>
            <p className="identity-filename">{item.fileName} · 點圖片可放大查看</p>
            <div className="identity-buttons">
              <button type="button" className="btn primary" disabled={immutable} onClick={() => void recognize(side)}>{item.recognized ? `重新辨識${label}` : `辨識${label}文字`}</button>
              <button type="button" className="btn" disabled={immutable} onClick={() => void rotate(side)}>順時針旋轉 90°</button>
              <button type="button" className="identity-text-button" disabled={immutable} onClick={() => remove(side)}>移除{label}</button>
            </div>
            <details className="identity-detail"><summary>圖片品質提示（不代表證件有效性）</summary><ul>{item.hints.map(hint => <li key={hint.label}><strong>{hint.label}：{hint.value}</strong><br />{hint.message}</li>)}</ul></details>
          </> : <div className="identity-placeholder">{label === '正面' ? '請選擇姓名、字號與出生日期所在的一面' : '請選擇戶籍地址所在的一面'}<small>練習資料是純文字圖片，非正式證件。</small></div>}
          {busy?.side === side && <div className="identity-progress" role="status" aria-live="polite"><span>{progress?.stage || busy.label}</span>{progress && <progress max={1} value={progress.progress} />}<small>繁體中文可能辨識不完整，完成後請逐欄核對。</small>{abort.current && <button type="button" className="identity-text-button" onClick={() => abort.current?.abort()}>取消辨識</button>}</div>}
          {item.recognized && <>
            {item.notes.length > 0 && <ul className="identity-extraction-notes">{item.notes.map(note => <li key={note}>{note}</li>)}</ul>}
            <details className="identity-detail"><summary>查看{label}原始辨識文字</summary><textarea aria-label={`${label}原始辨識文字`} readOnly value={item.text} rows={5} /><p>保留辨識器原始輸出；下方欄位修正不會改寫這段文字。{!item.text && ' 未辨識出文字，請對照原圖填寫，或重新拍攝。'}</p></details>
          </>}
        </section>;
      })}
    </div>
    {error && <p role="alert" className="identity-error">{error}</p>}
    {hasImage && <div className="identity-review">
      <h4>核對候選資料</h4><p>辨識文字可能缺漏或誤讀。請對照上方圖片逐欄修正；下方內容還未帶入申請。</p>
      <div className="identity-fields">
        {(Object.keys(labels) as Array<keyof IdentityFields>).map(field => <label className={`field identity-field ${field === 'address' ? 'wide' : ''}`} key={field}><span>{labels[field]}</span><input
          value={fields[field]} disabled={immutable} autoComplete="off" spellCheck={false}
          type="text"
          placeholder={field === 'birth' ? '2001-05-16 或民國 90 年 5 月 16 日' : field === 'address' ? '請對照反面完整戶籍地址' : '請先辨識或對照原圖填寫'}
          maxLength={field === 'address' ? 200 : field === 'idNumber' ? 20 : 100}
          onChange={event => { const value = event.currentTarget.value; setFields(previous => ({ ...previous, [field]: value })); invalidate(); }}
        />{field === 'birth' && <small>民國年換算西元；不會以發證日期當作生日。</small>}{field === 'idNumber' && <small>不會自動把 O 改成 0、I 改成 1；也不會用字首推測戶籍地。</small>}</label>)}
      </div>
      <ul className="identity-checks" aria-label="資料檢查結果">{checks.map((check, index) => <li key={`${check.field}-${index}`} className={`identity-check ${check.level}`}><span>{check.level === 'error' ? '待修正' : check.level === 'warning' ? '請核對' : '格式提示'}</span>{check.message}</li>)}</ul>
      <label className="identity-confirm-check"><input type="checkbox" checked={reviewed} disabled={immutable || hasErrors} onChange={event => { setReviewed(event.currentTarget.checked); setConfirmed(false); }} /><span>我已對照正反面圖片，確認姓名、字號、出生日期與地址，並確認與申請資料不同的欄位。</span></label>
      <button type="button" className="btn primary" disabled={!canConfirm} onClick={confirm}>確認並帶入申請資料</button>
      {hasErrors && <p className="identity-help">請完成兩面辨識並修正上方「待修正」項目，才能勾選確認。其他證件格式請洽人工處理。</p>}
      {confirmed && <p role="status" className="identity-success">已確認並帶入核對後的資料。原始文字與修正紀錄仍保留供對照。</p>}
    </div>}
  </section>;
}
