import { useEffect, useId, useRef, useState } from 'react';
import { createDemoReceipt, prepareImage, recognizeLocally, type OcrLanguage, type OcrProgress, type PreparedImage } from '../ocr';

export type RecognizedDocument = { text: string; fileName: string; previewUrl: string };
export type OcrLabProps = {
  onText?: (text: string) => void;
  /** Invoked only when the user clicks the explicit candidate-use button. */
  onRecognized?: (document: RecognizedDocument) => void;
  /** Fires as soon as text is read, before any decision to use it. Only for
   *  checking the reading against the published exclusions; it imports nothing. */
  onScanned?: (text: string) => void;
};

export default function OcrLab({ onText, onRecognized, onScanned }: OcrLabProps) {
  const id = useId();
  const [prepared, setPrepared] = useState<PreparedImage | null>(null);
  const [fileName, setFileName] = useState('');
  const [language, setLanguage] = useState<OcrLanguage>('chi_tra+eng');
  const [phase, setPhase] = useState<'empty' | 'preparing' | 'ready' | 'recognizing' | 'done'>('empty');
  const [progress, setProgress] = useState<OcrProgress>({ stage: '', progress: 0 });
  const [text, setText] = useState('');
  const [hasResult, setHasResult] = useState(false);
  const [error, setError] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [isDemo, setIsDemo] = useState(false);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const busy = phase === 'preparing' || phase === 'recognizing';

  useEffect(() => () => {
    generation.current++;
    controller.current?.abort();
  }, []);

  async function selectFile(file: File, demo = false) {
    const job = ++generation.current;
    controller.current?.abort();
    setError(''); setConfirmation(''); setText(''); setHasResult(false);
    setPrepared(null); setFileName(file.name); setIsDemo(demo); setPhase('preparing');
    try {
      const image = await prepareImage(file);
      if (job !== generation.current) return;
      setPrepared(image); setPhase('ready');
      if (demo) setLanguage('eng');
    } catch (cause) {
      if (job !== generation.current) return;
      setError(cause instanceof Error ? cause.message : '圖片無法讀取。');
      setPhase('empty');
    }
  }

  async function loadDemo() {
    setError('');
    try { await selectFile(await createDemoReceipt(), true); }
    catch { setError('無法建立練習圖片，請重新嘗試。'); }
  }

  async function recognize() {
    if (!prepared || busy) return;
    const job = ++generation.current;
    const aborter = new AbortController();
    controller.current = aborter;
    setPhase('recognizing'); setError(''); setConfirmation(''); setText(''); setHasResult(false);
    setProgress({ stage: '準備載入文字辨識', progress: 0 });
    try {
      const result = await recognizeLocally(prepared.previewUrl, language, (value) => {
        if (job === generation.current) setProgress(value);
      }, aborter.signal);
      if (job !== generation.current) return;
      setText(result.text); setHasResult(true); setPhase('done');
      if (result.text) onScanned?.(result.text);
      if (!result.text) setError('辨識完成，但沒有找到文字。請確認方向、光線與字體大小，或換一張圖片。');
    } catch (cause) {
      if (job !== generation.current) return;
      const cancelled = cause instanceof Error && cause.name === 'AbortError';
      setError(cancelled ? '已取消辨識，圖片仍保留，可重新開始。' : `辨識未完成。${cause instanceof Error && cause.message.includes('2 分鐘') ? cause.message : '請確認辨識功能載入完成、圖片清楚，再重新嘗試；圖片不會改傳至雲端。'}`);
      setPhase('ready');
    } finally {
      if (controller.current === aborter) controller.current = null;
    }
  }

  function clear() {
    generation.current++; controller.current?.abort(); controller.current = null;
    setPrepared(null); setFileName(''); setText(''); setHasResult(false);
    setError(''); setConfirmation(''); setIsDemo(false); setPhase('empty');
    if (input.current) input.current.value = '';
  }

  function useCandidate() {
    if (!prepared || !hasResult || !text.trim() || busy) return;
    onText?.(text);
    onRecognized?.({ text, fileName, previewUrl: prepared.previewUrl });
    setConfirmation('已將這份文字放入待確認候選；請在下一步逐欄對照原圖，不代表文件已通過審查。');
  }

  return <section className="ocr-lab card" aria-labelledby={`${id}-heading`}>
    <div className="ocr-heading">
      <div><p className="muted">文字辨識（OCR）</p><h3 id={`${id}-heading`}>把圖片讀成可確認的文字</h3></div>
      <span className="badge">圖片不上傳</span>
    </div>
    <p className="muted">圖片與文字只在瀏覽器處理，不會上傳或存入本機資料庫。辨識程式與語言檔案由本站載入。</p>
    <div className="notice">辨識的是候選文字，不會驗證身分、付款真偽或補助資格。繁體中文、手寫、小字與複雜排版可能辨識錯誤，請逐項對照原圖。</div>
    <div className="ocr-actions" style={{ display: 'flex', flexWrap: 'wrap', gap: '0.75rem', marginTop: '1rem' }}>
      <button type="button" className="btn primary" disabled={busy} onClick={() => input.current?.click()}>選擇文件圖片</button>
      <button type="button" className="btn" disabled={busy} onClick={loadDemo}>載入練習收據</button>
      {prepared && <button type="button" className="btn" onClick={clear}>清除本次圖片與文字</button>}
    </div>
    <input ref={input} id={`${id}-file`} type="file" accept="image/png,image/jpeg,image/webp,.pdf" disabled={busy} hidden onChange={(event) => {
      const file = event.currentTarget.files?.[0];
      event.currentTarget.value = '';
      if (file) void selectFile(file);
    }} />
    <p className="muted">PNG／JPEG／WebP，最多 20 MiB、2,400 萬像素。PDF 請先轉成圖片再逐頁處理。</p>
    {phase === 'preparing' && <p role="status">正在本機讀取圖片與計算品質提示…</p>}
    {error && <div className="notice" role="alert">{error}</div>}
    {prepared && <>
      <div className="ocr-preview-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 260px), 1fr))', gap: '1.25rem', marginTop: '1rem' }}>
        <figure style={{ margin: 0 }}>
          <img src={prepared.previewUrl} alt={isDemo ? '練習用英文收據，非正式憑證' : '你選擇的文件圖片，僅在目前瀏覽器預覽'} style={{ maxWidth: '100%', width: '100%', maxHeight: '440px', objectFit: 'contain', background: '#edf1eb', borderRadius: '12px' }} />
          <figcaption className="muted" style={{ overflowWrap: 'anywhere' }}>{isDemo ? '練習收據 · 非正式憑證' : fileName}</figcaption>
          {(prepared.width !== prepared.processedWidth || prepared.height !== prepared.processedHeight) && <p className="muted">為節省記憶體，辨識影像已縮至 {prepared.processedWidth} × {prepared.processedHeight} px。原始檔未被修改。</p>}
        </figure>
        <div>
          <h4>圖片品質提示</h4>
          <p className="muted">依圖片尺寸、明暗與邊緣提供提醒，仍需目視確認。這些提示不代表文件有效，也未經準確率驗證。</p>
          {prepared.hints.map((hint) => <div key={hint.label} className="ocr-quality-item" style={{ padding: '0.75rem 0', borderBottom: '1px solid #e1e7df' }}>
            <strong>{hint.label}</strong><p className="muted">{hint.value} · {hint.attention ? '可留意' : '供參考'}</p><p>{hint.message}</p>
          </div>)}
        </div>
      </div>
      <div className="ocr-controls" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'end', gap: '0.75rem', marginTop: '1rem' }}>
        <label className="field" htmlFor={`${id}-language`}>辨識語言
          <select id={`${id}-language`} value={language} disabled={busy} onChange={(event) => setLanguage(event.target.value as OcrLanguage)}>
            <option value="chi_tra+eng">繁體中文＋英文</option><option value="eng">英文</option>
          </select>
        </label>
        <button type="button" className="btn primary" disabled={busy} onClick={recognize}>{hasResult ? '重新辨識圖片' : '開始文字辨識'}</button>
        {phase === 'recognizing' && <button type="button" className="btn" onClick={() => controller.current?.abort()}>取消辨識</button>}
      </div>
      {phase === 'recognizing' && <div className="ocr-progress" role="status" aria-live="polite" style={{ marginTop: '1rem' }}>
        <label htmlFor={`${id}-progress`}>{progress.stage} · 目前階段 {Math.round(progress.progress * 100)}%</label>
        <progress id={`${id}-progress`} max={1} value={progress.progress} style={{ width: '100%' }} />
        <p className="muted">各階段分別計算進度。首次載入與辨識可能需要數十秒，較慢裝置可能更久。</p>
      </div>}
      {hasResult && <div className="ocr-result" style={{ marginTop: '1.25rem' }}>
        <label className="field" htmlFor={`${id}-text`}>辨識候選文字 · 請對照原圖修改
          <textarea id={`${id}-text`} rows={10} value={text} onChange={(event) => { setText(event.target.value); setConfirmation(''); }} spellCheck={false} style={{ width: '100%', resize: 'vertical' }} />
        </label>
        <p className="muted">文字由圖片辨識產生。即使拼字或金額看起來正確，仍請逐項對照原圖確認。</p>
        {(onText || onRecognized) && <button type="button" className="btn primary" disabled={!text.trim() || busy} onClick={useCandidate}>將這份文字放入待確認候選</button>}
        {confirmation && <p role="status">{confirmation}</p>}
      </div>}
    </>}
  </section>;
}
