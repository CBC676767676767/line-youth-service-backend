import { useId, useState } from 'react';
import { ArrowRight, BookOpen, Check, CheckCircle2, CircleHelp, ClipboardCheck, Clock3, Download, ExternalLink, Hand, LockKeyhole, RotateCcw, ScanLine, ShieldCheck } from 'lucide-react';
import content from '../safety-content.json';

type Category = 'self' | 'automatic' | 'manual';
const categoryIcons = { self: ClipboardCheck, automatic: ScanLine, manual: Hand };

export default function Safety() {
  const id = useId();
  const [category, setCategory] = useState<Category>('self');
  const [read, setRead] = useState<Set<string>>(new Set());
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(false);
  const readableCount = content.checklist.filter(item => item.category !== 'automatic').length;
  const answeredCount = content.quiz.filter(question => answers[question.id]).length;
  const score = content.quiz.filter(question => answers[question.id] === question.correctOptionId).length;
  const activeCategory = content.checklistCategories.find(item => item.id === category)!;

  function toggleRead(itemId: string) {
    setRead(previous => {
      const next = new Set(previous);
      if (next.has(itemId)) next.delete(itemId); else next.add(itemId);
      return next;
    });
  }

  function resetQuiz() {
    setAnswers({});
    setSubmitted(false);
  }

  return <div className="safety-page">
    <style>{styles}</style>
    <div className="page-heading">
      <div><div className="eyebrow">學習 · 練習 · 保護自己</div><h1>把好工具，也用得安心。</h1><p>五分鐘，從保護資料開始。把安全習慣帶進每一次 AI 使用。</p></div>
      <a className="btn" href="/ai-safety-card.pdf" download="竹青通_AI安全使用懶人包.pdf"><Download size={17} /> 下載懶人包 PDF</a>
    </div>

    <section className="safety-intro" aria-labelledby={`${id}-intro`}>
      <div className="safety-intro-icon"><ShieldCheck size={36} strokeWidth={1.5} /></div>
      <div className="safety-intro-copy"><span className="tag green">自願學習 · 提案新增</span><h2 id={`${id}-intro`}>先保護資料，再讓 AI 幫忙。</h2><p>{content.meta.eligibilityNotice}</p></div>
      <div className="safety-intro-time"><Clock3 size={20} /><strong>5 <small>分鐘</small></strong><span>自評 · 情境題 · 小提醒</span></div>
    </section>

    <section className="card safety-section" aria-labelledby={`${id}-checklist`}>
      <div className="card-heading"><div><div className="eyebrow">01 / 使用之前</div><h2 id={`${id}-checklist`}>使用前，先確認這 8 件事</h2><p className="muted">勾選是自己的確認；比對與人工判斷有不同的界線。</p></div><span className="tag neutral">閱讀／自評 {read.size} / {readableCount}</span></div>
      <div className="safety-tabs" role="group" aria-label="選擇檢核類型">
        {content.checklistCategories.map(item => {
          const Icon = categoryIcons[item.id as Category];
          return <button type="button" key={item.id} className={`safety-tab ${category === item.id ? 'active' : ''}`} aria-pressed={category === item.id} onClick={() => setCategory(item.id as Category)}><Icon size={17} />{item.label}<span>{content.checklist.filter(check => check.category === item.id).length}</span></button>;
        })}
      </div>
      <p className="safety-category-note"><CircleHelp size={16} aria-hidden="true" /><span>{activeCategory.description}</span></p>
      <div className="safety-check-grid">
        {content.checklist.filter(item => item.category === category).map(item => <article className={`safety-check-item ${read.has(item.id) ? 'is-read' : ''}`} key={item.id}>
          <div className="safety-item-top"><h3>{item.title}</h3>{item.category === 'automatic' ? <span className="tag neutral">規劃比對</span> : item.category === 'manual' ? <span className="tag amber">仍需人工確認</span> : <span className="safety-small-label">自己確認</span>}</div>
          <p>{item.body}</p><p className="safety-action"><ArrowRight size={15} aria-hidden="true" /><span>{item.action}</span></p>
          {item.category === 'automatic' ? <div className="safety-pending"><span className="tag neutral">{item.defaultResult}</span><small>{item.limits}</small></div> : <label className="check-line safety-check-toggle"><input type="checkbox" checked={read.has(item.id)} onChange={() => toggleRead(item.id)} /><span>{item.category === 'manual' ? '我已閱讀，知道這項仍需人工確認' : item.checkedLabel}</span></label>}
        </article>)}
      </div>
      <div className="safety-check-footer"><LockKeyhole size={15} /><p>{content.meta.assessmentNotice} 本頁勾選只暫存於記憶體，離開此頁會重設。</p></div>
    </section>

    <section className="card safety-section" aria-labelledby={`${id}-quiz`}>
      <div className="card-heading"><div><div className="eyebrow">02 / 情境練習</div><h2 id={`${id}-quiz`}>遇到這種情況，你會怎麼做？</h2><p className="muted">三個日常情境，練習停一下、想一下，再操作。</p></div><span className="tag neutral">已作答 {answeredCount} / {content.quiz.length}</span></div>
      <div className="safety-questions">
        {content.quiz.map((question, index) => <fieldset className="safety-question" key={question.id}>
          <legend><span className="safety-question-number">{String(index + 1).padStart(2, '0')}</span>{question.title}</legend>
          <p>{question.question}</p>
          <div className="safety-options">
            {question.options.map(option => {
              const selected = answers[question.id] === option.id;
              const correct = submitted && option.id === question.correctOptionId;
              const wrong = submitted && selected && !correct;
              return <label key={option.id} className={`safety-option ${selected ? 'selected' : ''} ${correct ? 'correct' : ''} ${wrong ? 'needs-review' : ''}`}><input type="radio" name={`${id}-${question.id}`} value={option.id} checked={selected} onChange={() => { setAnswers(previous => ({ ...previous, [question.id]: option.id })); setSubmitted(false); }} /><span>{option.text}</span>{correct && <span className="safety-answer-label"><Check size={14} /> 正確做法</span>}{wrong && <span className="safety-answer-label">再想一下</span>}</label>;
            })}
          </div>
          {submitted && <div className="safety-explanation"><BookOpen size={18} aria-hidden="true" /><div><strong>{question.takeaway}</strong><p>{question.explanation}</p></div></div>}
        </fieldset>)}
      </div>
      <div className="safety-quiz-footer"><div><strong>{submitted ? `答對 ${score} / ${content.quiz.length} 題` : '準備好，看看你的判斷。'}</strong><p className="muted">{submitted ? '這是學習回饋，不是安全評分或補助審查。' : `${content.quiz.length - answeredCount} 題尚未作答 · 沒有及格門檻，可重新練習。`}</p></div><div className="button-row"><button type="button" className="btn" onClick={resetQuiz} disabled={answeredCount === 0 && !submitted}><RotateCcw size={15} /> 重新練習</button><button type="button" className="btn primary" disabled={answeredCount !== content.quiz.length} onClick={() => setSubmitted(true)}>{submitted ? '再次查看解說' : '查看答案與解說'}<ArrowRight size={16} /></button></div></div>
      <div className="safety-live-result" role="status" aria-live="polite">{submitted && <><CheckCircle2 size={19} /><span>{score === content.quiz.length ? content.quizFeedback.allCorrect : content.quizFeedback.someIncorrect}</span></>}</div>
    </section>

    <section className="safety-section" aria-labelledby={`${id}-incidents`}>
      <div className="card-heading"><div><div className="eyebrow">03 / 從真實事件學習</div><h2 id={`${id}-incidents`}>真實事件，提醒我們多想一步</h2><p className="muted">讀原始報告，也看清楚事件的範圍與日期。</p></div></div>
      <div className="safety-incidents">
        {content.incidents.map((incident, index) => <article className="card safety-incident" key={incident.id}><div className="safety-incident-top"><span className="safety-case-number">案例 {String(index + 1).padStart(2, '0')}</span><time dateTime={incident.disclosedOn}>{incident.disclosedOn.replace(/-/g, '.')} 揭露</time></div><h3>{incident.title}</h3><span className="safety-incident-type">{incident.type}</span><p>{incident.summary}</p><div className="safety-lesson"><BookOpen size={18} /><p>{incident.lesson}</p></div><p className="safety-boundary">{incident.boundary}</p><a href={incident.sourceUrl} target="_blank" rel="noreferrer" className="safety-source-link">{incident.sourceLabel}<ExternalLink size={14} /></a></article>)}
      </div>
      <p className="safety-footnote">{content.incidentNotice}</p>
    </section>

    <section className="safety-takeaway" aria-labelledby={`${id}-card`}>
      <div className="safety-takeaway-art" aria-hidden="true"><span>安全<br />先一步</span><ShieldCheck size={54} strokeWidth={1.2} /></div>
      <div><div className="eyebrow">收藏日常提醒</div><h2 id={`${id}-card`}>把安全四步，帶在身邊。</h2><p>資料先減量、入口先確認、權限先收斂、答案再查證。<br />開啟 A4 懶人包，收藏或列印一張日常提醒。</p><div className="button-row"><a className="btn primary" href="/ai-safety-card.html" target="_blank" rel="noreferrer"><Download size={16} /> 開啟／列印懶人包 <ExternalLink size={13} /></a><a className="safety-source-link" href="/ai-safety-card.png" download="竹青通_AI安全使用懶人包.png">下載 PNG 圖卡 <Download size={14} /></a></div></div>
    </section>

    <section className="safety-section" aria-labelledby={`${id}-activities`}>
      <div className="card-heading"><div><div className="eyebrow">一起養成安全習慣</div><h2 id={`${id}-activities`}>把安全習慣，一起練起來</h2><p className="muted">以下為宣導活動提案，尚未公告場次或開放報名。</p></div><span className="tag amber">活動規劃</span></div>
      <div className="safety-activity-grid">{content.activities.map(activity => <article className="card safety-activity" key={activity.id}><span className="safety-activity-duration"><Clock3 size={14} /> {activity.durationMinutes} 分鐘 · {activity.format}</span><h3>{activity.title}</h3><p>{activity.description}</p><div className="safety-activity-note"><LockKeyhole size={14} /><small>{activity.participationNotice}</small></div></article>)}</div>
    </section>

    <details className="card safety-sources"><summary>教材來源與使用說明 <span>{content.sources.length} 個一手來源</span></summary><p>{content.meta.privacyNotice}</p><ul>{content.sources.map(source => <li key={source.id}><a href={source.url} target="_blank" rel="noreferrer">{source.publisher} · {source.title}<ExternalLink size={12} /></a></li>)}</ul><p className="muted">資料查核：{content.meta.checkedOn}。事件依當時報告整理；產品政策與公告須再次查看原始來源。</p></details>
  </div>;
}

const styles = `
.safety-page { color: #263d35; }
.safety-page h2 { letter-spacing: -.035em; }
.safety-page .page-heading a { flex-shrink: 0; }
.safety-intro { display: flex; gap: 22px; align-items: center; padding: 28px 30px; border: 1px solid #dbe7dc; border-radius: 18px; background: #eaf1e8; margin-bottom: 28px; }
.safety-intro-icon { width: 78px; height: 78px; border-radius: 24px; background: #fffdf5; color: #3d684e; display: grid; place-items: center; flex-shrink: 0; }
.safety-intro-copy { flex: 1; }
.safety-intro-copy h2 { margin: 11px 0 8px; font-size: 24px; }
.safety-intro-copy p { margin: 0; line-height: 1.8; font-size: 13px; max-width: 630px; color: #53655b; }
.safety-intro-time { flex-shrink: 0; display: flex; align-items: center; flex-direction: column; gap: 8px; color: #43604c; border-left: 1px solid #cfddcf; padding-left: 28px; }
.safety-intro-time strong { font-size: 29px; font-weight: 600; line-height: 1; }
.safety-intro-time small { font-size: 13px; }
.safety-intro-time span { font-size: 10px; white-space: nowrap; }
.safety-section { margin: 0 0 28px; }
.safety-section > .card-heading { align-items: flex-start; gap: 18px; }
.safety-section .card-heading h2 { margin: 7px 0 8px; font-size: 23px; }
.safety-section .card-heading p { margin: 0; font-size: 13px; }
.safety-tabs { display: flex; gap: 8px; padding: 5px; border-radius: 12px; background: #f3f4ed; margin-top: 24px; }
.safety-tab { display: flex; justify-content: center; align-items: center; gap: 9px; flex: 1; border: 0; border-radius: 8px; padding: 12px 9px; background: transparent; color: #63716b; font: inherit; font-size: 13px; cursor: pointer; }
.safety-tab > span { font-size: 11px; opacity: .65; }
.safety-tab.active { color: #234b37; background: #fff; box-shadow: 0 2px 7px #304d3110; font-weight: 600; }
.safety-tab:focus-visible, .safety-page a:focus-visible, .safety-page button:focus-visible { outline: 3px solid #709876; outline-offset: 3px; }
.safety-category-note { display: flex; align-items: flex-start; gap: 8px; color: #68776d; font-size: 12px; line-height: 1.75; margin: 15px 2px 19px; }
.safety-category-note svg { flex-shrink: 0; margin-top: 3px; }
.safety-check-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 14px; }
.safety-check-item { border: 1px solid #e4e7dd; border-radius: 12px; padding: 19px; display: flex; flex-direction: column; background: #fff; }
.safety-check-item.is-read { border-color: #b8cdbb; background: #fbfdf9; }
.safety-item-top { display: flex; align-items: flex-start; gap: 9px; }
.safety-item-top h3 { flex: 1; margin: 0; font-size: 15px; line-height: 1.6; }
.safety-item-top .tag { white-space: nowrap; font-size: 10px; margin-top: 2px; }
.safety-small-label { white-space: nowrap; color: #869083; font-size: 11px; padding-top: 4px; }
.safety-check-item > p { font-size: 12px; line-height: 1.85; color: #627067; margin: 10px 0 0; }
.safety-check-item .safety-action { display: flex; gap: 7px; color: #365941; margin-bottom: 16px; }
.safety-action svg { flex-shrink: 0; margin-top: 4px; }
.safety-check-toggle { border-top: 1px solid #e8ece3; padding-top: 14px; margin-top: auto; align-items: flex-start; font-size: 12px; line-height: 1.7; cursor: pointer; }
.safety-page input[type=checkbox], .safety-page input[type=radio] { accent-color: #315d43; flex-shrink: 0; width: 17px; height: 17px; margin: 2px 0 0; }
.safety-pending { margin-top: auto; padding-top: 14px; border-top: 1px solid #e8ece3; }
.safety-pending small { display: block; color: #70766d; font-size: 11px; line-height: 1.75; margin-top: 10px; }
.safety-check-footer { display: flex; gap: 8px; align-items: flex-start; color: #788075; margin-top: 20px; }
.safety-check-footer svg { margin-top: 3px; flex-shrink: 0; }
.safety-check-footer p { font-size: 11px; line-height: 1.8; margin: 0; }
.safety-question { min-width: 0; margin: 24px 0 0; border: 0; padding: 0 0 24px; border-bottom: 1px solid #e6e9df; }
.safety-question legend { font-size: 15px; font-weight: 600; padding: 0; display: flex; align-items: center; gap: 10px; }
.safety-question-number { font-size: 11px; font-weight: 600; width: 28px; height: 28px; border-radius: 8px; background: #eff3e9; color: #5c784b; display: inline-grid; place-items: center; }
.safety-question > p { font-size: 13px; line-height: 1.8; color: #5f6b63; margin: 12px 0 15px; }
.safety-options { display: grid; gap: 8px; }
.safety-option { display: flex; align-items: flex-start; gap: 11px; padding: 13px 15px; border: 1px solid #e3e7dc; border-radius: 9px; font-size: 13px; line-height: 1.6; cursor: pointer; transition: background .15s, border-color .15s; }
.safety-option > span:first-of-type { flex: 1; }
.safety-option:hover { background: #f8faf4; }
.safety-option.selected { border-color: #89a883; background: #f2f7ec; }
.safety-option.correct { border-color: #91b19a; background: #edf5eb; }
.safety-option.needs-review { background: #fbf5e7; border-color: #dac996; }
.safety-answer-label { display: flex; gap: 4px; align-items: center; font-size: 11px; white-space: nowrap; color: #496c49; padding-top: 2px; }
.safety-option.needs-review .safety-answer-label { color: #896a26; }
.safety-explanation { display: flex; gap: 10px; padding: 15px; background: #f6f7f0; border-radius: 10px; margin-top: 13px; }
.safety-explanation svg { flex-shrink: 0; color: #5e7c51; margin-top: 1px; }
.safety-explanation strong { font-size: 12px; }
.safety-explanation p { font-size: 12px; line-height: 1.8; margin: 6px 0 0; color: #60705f; }
.safety-quiz-footer { display: flex; gap: 18px; align-items: center; justify-content: space-between; margin-top: 22px; }
.safety-quiz-footer strong { font-size: 14px; }
.safety-quiz-footer p { margin: 6px 0 0; font-size: 11px; }
.safety-page button:disabled { opacity: .42; cursor: not-allowed; }
.safety-live-result { color: #365e3a; font-size: 13px; line-height: 1.7; }
.safety-live-result:not(:empty) { display: flex; gap: 9px; align-items: flex-start; padding: 15px; margin-top: 17px; background: #edf5e9; border-radius: 10px; }
.safety-live-result svg { flex-shrink: 0; margin-top: 1px; }
.safety-incidents { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 18px; margin-top: 19px; }
.safety-incident { display: flex; flex-direction: column; }
.safety-incident-top { display: flex; justify-content: space-between; align-items: center; gap: 9px; font-size: 10px; color: #818779; }
.safety-case-number { letter-spacing: .1em; color: #5c7c4b; font-size: 11px; }
.safety-incident h3 { margin: 15px 0 8px; font-size: 19px; letter-spacing: -.03em; line-height: 1.5; }
.safety-incident-type { display: block; font-size: 10px; color: #7a8575; }
.safety-incident > p { font-size: 12px; line-height: 1.9; color: #5d6d63; margin: 15px 0; }
.safety-lesson { display: flex; align-items: flex-start; gap: 9px; padding: 14px; background: #f0f4e9; border-radius: 9px; }
.safety-lesson svg { flex-shrink: 0; color: #658053; margin-top: 3px; }
.safety-lesson p { margin: 0; font-size: 12px; line-height: 1.8; color: #49653f; }
.safety-incident .safety-boundary { font-size: 11px; color: #7e877c; }
.safety-source-link { display: inline-flex; gap: 7px; align-items: center; font-size: 12px; color: #335a43; margin-top: auto; text-decoration: none; align-self: flex-start; }
.safety-source-link:hover { text-decoration: underline; }
.safety-footnote { font-size: 11px; color: #7e877c; line-height: 1.8; margin: 12px 0 0; }
.safety-takeaway { display: flex; align-items: center; gap: 30px; border: 1px solid #dedfcf; background: #f2f0df; padding: 28px 32px; border-radius: 17px; margin-bottom: 30px; }
.safety-takeaway-art { width: 123px; min-height: 152px; padding: 18px; border-radius: 5px; background: #28533e; color: #f5f3db; box-shadow: 5px 5px 0 #d9dcc1; transform: rotate(-5deg); flex-shrink: 0; display: flex; justify-content: space-between; flex-direction: column; }
.safety-takeaway-art > span { font-size: 20px; line-height: 1.25; letter-spacing: -.05em; }
.safety-takeaway-art svg { align-self: flex-end; margin-top: 12px; }
.safety-takeaway h2 { font-size: 24px; margin: 8px 0 12px; }
.safety-takeaway p { font-size: 13px; color: #72755c; line-height: 1.8; margin: 0 0 18px; }
.safety-activity-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 16px; margin-top: 18px; }
.safety-activity { display: flex; flex-direction: column; }
.safety-activity-duration { display: flex; align-items: center; gap: 6px; font-size: 10px; color: #778771; }
.safety-activity h3 { font-size: 16px; line-height: 1.6; margin: 13px 0 10px; }
.safety-activity > p { font-size: 12px; color: #6b776d; line-height: 1.85; margin: 0 0 20px; }
.safety-activity-note { display: flex; align-items: flex-start; gap: 6px; border-top: 1px solid #e6e9dc; padding-top: 12px; margin-top: auto; }
.safety-activity-note svg { flex-shrink: 0; margin-top: 2px; color: #7b8972; }
.safety-activity-note small { font-size: 10px; line-height: 1.75; color: #81887a; }
.safety-sources summary { cursor: pointer; font-size: 13px; font-weight: 600; line-height: 1.7; }
.safety-sources summary > span { font-size: 11px; color: #87917c; font-weight: 400; margin-left: 12px; }
.safety-sources > p, .safety-sources li { font-size: 11px; line-height: 1.85; }
.safety-sources ul { padding-left: 19px; }
.safety-sources li { margin-bottom: 7px; }
.safety-sources li a { color: #4a6e46; overflow-wrap: anywhere; }
.safety-sources li a svg { margin-left: 5px; vertical-align: middle; }
@media (max-width: 900px) { .safety-intro-time { display: none; } .safety-activity-grid { grid-template-columns: 1fr; } .safety-activity > p { margin-bottom: 12px; } .safety-quiz-footer { align-items: flex-start; flex-direction: column; } }
@media (max-width: 600px) { .safety-intro { padding: 21px; gap: 15px; align-items: flex-start; } .safety-intro-icon { width: 49px; height: 49px; border-radius: 16px; } .safety-intro-icon svg { width: 29px; height: 29px; } .safety-intro-copy h2 { font-size: 20px; } .safety-check-grid, .safety-incidents { grid-template-columns: 1fr; } .safety-tab { gap: 5px; font-size: 11px; padding: 11px 6px; } .safety-tab svg { display: none; } .safety-item-top { flex-wrap: wrap; } .safety-item-top h3 { flex-basis: 100%; } .safety-option { flex-wrap: wrap; font-size: 12px; } .safety-option > span:first-of-type { flex-basis: calc(100% - 30px); } .safety-answer-label { margin-left: 28px; } .safety-quiz-footer .button-row { width: 100%; flex-wrap: wrap; } .safety-takeaway { padding: 23px; gap: 20px; } .safety-takeaway-art { display: none; } .safety-takeaway h2 { font-size: 22px; } .safety-page .card-heading > .tag { align-self: flex-start; } }
`;
