import { useState, type FormEvent } from "react";
import { Mail, ShieldCheck } from "lucide-react";
import { api, errorMessage } from "../shared/api";
import type { Me } from "../shared/types";

export default function EmailLogin({ onLogin }: { onLogin: (me: Me) => void }) {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState("");
  const [resendAt, setResendAt] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (!challenge) {
        const result = await api<{
          challenge_id: string;
          resend_after: string;
        }>("/auth/email/challenges", {
          method: "POST",
          json: { email, purpose: "login" },
        });
        setChallenge(result.challenge_id);
        setResendAt(Date.parse(result.resend_after));
      } else {
        const me = await api<Me>("/auth/email/challenges/verify", {
          method: "POST",
          json: { challenge_id: challenge, code },
        });
        setCode("");
        onLogin(me);
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="card citizen-login">
      <span className="login-symbol">
        <Mail size={28} />
      </span>
      <p className="eyebrow">開始你的申請</p>
      <h2>先驗證電子信箱</h2>
      <p className="muted">
        登入後可儲存草稿、補交文件，並查看自己的案件進度。
      </p>
      <form onSubmit={submit}>
        <label className="field">
          <span>電子信箱</span>
          <input
            type="email"
            autoComplete="email"
            value={email}
            required
            maxLength={254}
            disabled={busy || !!challenge}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        {challenge && (
          <>
            <p className="notice">
              請輸入驗證信中的六位數字；驗證碼有效期限為 10 分鐘。
            </p>
            <label className="field">
              <span>信箱驗證碼</span>
              <input
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                value={code}
                maxLength={6}
                required
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              />
            </label>
          </>
        )}
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
        <button className="btn primary full" disabled={busy}>
          {busy ? "處理中…" : challenge ? "驗證並繼續" : "寄送驗證碼"}
        </button>
        {challenge && (
          <button
            type="button"
            className="text-btn"
            disabled={busy}
            onClick={() => {
              if (Date.now() < resendAt) {
                setError("請稍候一分鐘再重新寄送驗證碼。");
                return;
              }
              setChallenge("");
              setCode("");
              setError("");
            }}
          >
            更換信箱或重新寄送
          </button>
        )}
      </form>
      <p className="small muted">
        <ShieldCheck size={14} />{" "}
        信箱驗證確認帳號使用權；證件與補助資格由承辦另行核對。
      </p>
    </section>
  );
}
