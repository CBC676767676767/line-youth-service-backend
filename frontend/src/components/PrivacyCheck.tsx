import { useState } from 'react';
import { ShieldCheck, ScanLine, Sparkles } from 'lucide-react';
const patterns=[
 {id:'email',name:'電子信箱',pattern:/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi},
 {id:'phone',name:'臺灣手機號碼格式',pattern:/\b09\d{2}[- ]?\d{3}[- ]?\d{3}\b/g},
 {id:'id',name:'臺灣身分證字號格式',pattern:/\b[A-Z][12]\d{8}\b/gi},
 {id:'key',name:'疑似服務金鑰字串',pattern:/\bsk-[A-Za-z0-9_-]{12,}\b/g}
];
export default function PrivacyCheck(){
 const [text,setText]=useState(''),[run,setRun]=useState(false);
 const results=patterns.map(p=>({...p,matches:[...text.matchAll(new RegExp(p.pattern.source,p.pattern.flags))]})).filter(p=>p.matches.length);
 const count=results.reduce((s,p)=>s+p.matches.length,0);
 function mask(){let next=text;for(const p of patterns)next=next.replace(new RegExp(p.pattern.source,p.pattern.flags),`[已移除${p.name}]`);setText(next);setRun(false)}
 return <section className="card privacy-check"><div className="card-heading"><div><div className="eyebrow">個資檢查</div><h2>送給 AI 前，先檢查一遍</h2><p>先找出可能含個資的字串，減少不必要的資料。</p></div><ShieldCheck size={26}/></div><label className="field"><span>輸入練習文字（請勿使用真實個資）</span><textarea rows={4} value={text} onChange={e=>{setText(e.target.value);setRun(false)}} placeholder="不確定這段內容能不能交給一般 AI？先用虛構資料練習。"/></label><div className="button-row" style={{marginTop:16}}><button className="btn" onClick={()=>{setText('以下是虛構練習資料：電子信箱 practice@example.com，手機 0900-000-000，練習金鑰 sk-practice-NOT-A-REAL-KEY-1234567890。');setRun(false)}}><Sparkles size={15}/> 載入練習資料</button><button className="btn primary" disabled={!text.trim()} onClick={()=>setRun(true)}><ScanLine size={16}/> 檢查這段文字</button>{run&&count>0&&<button className="btn" onClick={mask}>遮蔽已找到的字串</button>}</div>{run&&<div className={`notice ${count?'warning':''}`} role="status"><ShieldCheck size={19}/><div><b>{count?`找到 ${count} 處可能需要移除的資料`:'沒有命中目前這四種格式'}</b><p>{count?results.map(p=>`${p.name} ${p.matches.length} 處`).join(' · '):'沒有命中不代表內容安全；姓名、地址、機密、間接識別資訊及其他金鑰格式可能漏掉。'}</p></div></div>}<p className="small muted" style={{marginTop:16,marginBottom:0}}>文字只在此頁記憶體處理，不傳給外部 AI。格式檢查可能漏掉資料，也不驗證真假；遮蔽不等於完整匿名化，仍須人工核對。</p></section>
}
