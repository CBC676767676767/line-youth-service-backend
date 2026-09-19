import { useId } from 'react';
import { ArrowDown, ArrowRight, ChevronDown, ChevronLeft, ClipboardCheck, ExternalLink, FilePlus2, Files, Leaf, Menu, MessageCircle, Route, ShieldCheck, Smartphone } from 'lucide-react';
import '../line-entry.css';

export type LineEntryProps = {
  onSelect: (action: 'apply' | 'tracking' | 'supplement' | 'safety') => void;
};

const services = [
  { action: 'apply', title: '申請補助', detail: '準備資料，開始申請', Icon: FilePlus2 },
  { action: 'tracking', title: '申請進度', detail: '查看目前處理階段', Icon: Route },
  { action: 'supplement', title: '補件專區', detail: '核對原因，補齊文件', Icon: Files },
  { action: 'safety', title: 'AI 安全學堂', detail: '讓好工具用得安心', Icon: ShieldCheck },
] as const;

const steps = [
  { title: '選擇服務', description: '在本頁服務捷徑，點選你現在需要的服務。', Icon: MessageCircle },
  { title: '開啟申請頁', description: '進入對應頁面，查看準備事項與下一步。', Icon: Smartphone },
  { title: '完成資料與文件', description: '依步驟填寫、核對文件，再確認申請內容。', Icon: ClipboardCheck },
];

export default function LineEntry({ onSelect }: LineEntryProps) {
  const id = useId();
  const env = (import.meta as ImportMeta & {
    env?: { VITE_LINE_BOT_DISPLAY_NAME?: string; VITE_LINE_BOT_BASIC_ID?: string };
  }).env;
  const botDisplayName = env?.VITE_LINE_BOT_DISPLAY_NAME?.trim() || '青年補助服務';
  const configuredBasicId = env?.VITE_LINE_BOT_BASIC_ID?.trim() || '';
  const botBasicId = configuredBasicId ? `@${configuredBasicId.replace(/^@/, '')}` : '';
  const addFriendUrl = botBasicId ? `https://line.me/R/ti/p/${encodeURIComponent(botBasicId)}` : '';

  return <div className="line-entry">
    <div className="page-heading le-page-heading">
      <div><div className="eyebrow">青年服務入口</div><h1>青年補助服務，從這裡開始</h1><p>選擇需要的服務，接著準備申請、查看進度或補齊文件。</p></div>
    </div>

    <div className="le-layout">
      <section className="le-phone-stage" aria-label="網站服務捷徑">
        <div className="le-stage-caption"><MessageCircle size={15} aria-hidden="true" /><span>網站服務捷徑，點選後開啟對應頁面</span></div>
        <div className="le-phone">
          <div className="le-chat-header">
            <ChevronLeft size={24} aria-hidden="true" />
            <div><strong>{botDisplayName}</strong><span>{botBasicId || 'LINE 帳號尚未設定'}</span></div>
            <Menu size={21} aria-hidden="true" />
          </div>

          <div className="le-chat-content">
            <span className="le-chat-label">網站服務介紹</span>
            <div className="le-message">
              <span className="le-account-avatar" aria-hidden="true"><Leaf size={22} strokeWidth={1.6} /></span>
              <div className="le-message-content"><span className="le-sender">{botDisplayName}</span><div className="le-message-bubble"><p>嗨，歡迎來到青年補助服務！</p><p>準備開始申請，或想查看進度？<br />點選下方捷徑，就能接著往下走。</p></div></div>
            </div>

            <div className="le-chat-feature">
              <span className="le-feature-kicker"><Leaf size={14} aria-hidden="true" /> 青年 AI 工具補助</span>
              <strong>每一次申請，<br />都有清楚的下一步。</strong>
              <span className="le-feature-foot">準備文件 · 核對資料 · 掌握進度</span>
              <div className="le-feature-art" aria-hidden="true"><FilePlus2 size={65} strokeWidth={1.1} /><span><ClipboardCheck size={21} /></span></div>
            </div>
          </div>

          <div className="le-menu-heading"><span><Menu size={15} aria-hidden="true" /> 網站服務捷徑</span><ChevronDown size={17} aria-hidden="true" /></div>
          <nav className="le-rich-menu" aria-label="青年補助網站服務">
            {services.map(({ action, title, detail, Icon }) => <button key={action} type="button" className={`le-rich-button le-service-${action}`} aria-label={title} onClick={() => onSelect(action)}>
              <span className="le-service-icon"><Icon size={27} strokeWidth={1.6} aria-hidden="true" /></span>
              <strong>{title}</strong><span className="le-service-detail">{detail}</span><ArrowRight className="le-service-arrow" size={14} aria-hidden="true" />
            </button>)}
          </nav>
          <div className="le-phone-bottom" aria-hidden="true"><span /></div>
        </div>
        <div className="le-stage-bottom"><span /><Leaf size={14} aria-hidden="true" /><span /></div>
      </section>

      <section className="le-guide" aria-labelledby={`${id}-guide`}>
        <span className="le-guide-label">熟悉的入口，清楚的流程</span>
        <h2 id={`${id}-guide`}>不用再找入口，<br /><span>下一步就在這裡。</span></h2>
        <p className="le-guide-intro">想申請時開始準備，需要補件時接著處理。<br className="le-desktop-break" />讓每一次回到服務，都知道要做什麼。</p>

        {addFriendUrl && <a className="le-official-link" href={addFriendUrl} target="_blank" rel="noopener noreferrer"><span>加入 LINE 好友<small>{botDisplayName} · {botBasicId}</small></span><ExternalLink size={17} aria-hidden="true" /></a>}
        <p className="le-integration-note">本頁按鈕是網站服務捷徑，不代表 LINE 圖文選單已發布。{!addFriendUrl && ' LINE 帳號設定完成後，這裡會提供加入好友連結。'}</p>

        <ol className="le-steps">
          {steps.map(({ title, description, Icon }, index) => <li key={title}>
            <div className="le-step-marker"><span>{index + 1}</span>{index < steps.length - 1 && <ArrowDown size={15} aria-hidden="true" />}</div>
            <div className="le-step-copy"><h3>{title}</h3><p>{description}</p></div>
            <Icon className="le-step-icon" size={23} strokeWidth={1.5} aria-hidden="true" />
          </li>)}
        </ol>

        <div className="le-privacy-note"><ShieldCheck size={21} aria-hidden="true" /><div><strong>證件留在申請頁準備</strong><p>聊天視窗不需要貼上證件或付款資料，完整文件請依申請頁指引處理。</p></div></div>

        <a className="le-official-link" href="https://dgservice.hccg.gov.tw/serviceNotice.do?id=1323&rule=guest" target="_blank" rel="noreferrer"><span>先了解申請須知<small>新竹市數位申辦服務平台</small></span><ExternalLink size={17} aria-hidden="true" /></a>
      </section>
    </div>
  </div>;
}
