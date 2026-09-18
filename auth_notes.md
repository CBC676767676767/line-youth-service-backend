# 帳號模組串接與維運

本模組位於 `app/auth.py`，所有路徑由主程式加上 `/api/v1`。信箱驗證只證明信箱控制；LINE 驗證只證明指定 LINE 帳號控制。兩者都不授予既有敏感案件所有權。承辦角色必須使用密碼與 TOTP 完成登入，信箱或 LINE 登入的工作階段只會取得 applicant 授權。

## 工作階段及 CSRF

登入成功後，HTTP 回應設定兩個 Cookie：`youth_session` 是 HttpOnly 不透明工作階段識別碼；`youth_session_csrf` 是可供前端讀取的 CSRF 值。Cookie 名稱會隨 `YOUTH_SESSION_COOKIE_NAME` 改變。兩者皆設 SameSite=Lax，正式環境要求 Secure。資料庫只保存用途分離的 HMAC 摘要。

前端對登入後的 POST、PATCH、PUT、DELETE 請求傳入 `X-CSRF-Token`；值可使用登入回應的 `data.csrf_token`。重新整理頁面後，可先呼叫 `GET /me` 從 CSRF Cookie 還原同一值；此回應不提供 HttpOnly 工作階段識別碼。若 CSRF Cookie 遺失，重新登入即可輪替工作階段。Cookie 及 API 必須使用同源或符合部署 Cookie 政策的網域，允許來源明確配置於 `YOUTH_ALLOWED_ORIGINS`。

`current_principal` 每次讀取目前帳號狀態、角色版本及未撤銷／未過期的授權。修改操作同時驗證 CSRF 與 Origin；不可依使用者傳入的角色判斷權限。最後存取時間每 30 秒才以獨立短交易更新，避免提交呼叫者的案件交易，也不增加 session.version。停用帳號、撤銷 session 或更新 role_version 後，舊工作階段立即不能發起新的授權請求。

## 信箱及帳號 API

| 路徑 | 使用方式 |
|---|---|
| `POST /auth/email/challenges` | `{email, purpose: "login"}`；回傳 challenge_id、expires_at、resend_after、code_length，不回傳驗證碼 |
| `POST /auth/email/challenges/verify` | `{challenge_id, code}`；登入成功後設定 Cookie 並回傳帳號與 CSRF |
| `POST /auth/email/challenges` | 已登入時使用 `{email, purpose: "reauth"}`，信箱必須是目前帳號信箱；須 CSRF，驗證成功只更新原工作階段近期驗證時間 |
| `GET /account/sessions` | 只列出自己的未撤銷且未到絕對期限的工作階段；附每筆 ETag |
| `DELETE /account/sessions/{id}` | 帶 CSRF 與該工作階段 `If-Match`；不能撤銷他人工作階段 |
| `POST /account/sessions/revoke-all` | 需要近期驗證，撤銷所有工作階段並清除 Cookie |
| `POST /account/email/change-challenges` | 需要近期驗證與 CSRF，`{email}` 為新信箱，驗證碼寄送到新信箱 |
| `POST /account/email/change` | `{challenge_id, code}`；挑戰須屬於目前帳號與 session。成功後輪替 session，舊 session 全部撤銷；不合併帳號 |
| `POST /auth/logout` | 撤銷目前工作階段；需要 CSRF |

OTP 預設 10 分鐘有效、最多 5 次錯誤、重送最少 60 秒。驗證碼用完即失效，重送建立新挑戰並使舊挑戰失效，不能延長舊碼時間。`auth_rate_limits` 使用 PostgreSQL／SQLite 原子 upsert，對信箱、來源及全域共享限流；不信任外來 `X-Forwarded-For` 作為來源。正式反向代理若需還原來源，應在受信任的伺服器代理配置層完成。過期的限流資料可由維運依 `expires_at` 清理，不可刪除尚有效的限流資料。

開發環境 `YOUTH_MAIL_BACKEND=spool` 將完整驗證信寫至 `YOUTH_MAIL_SPOOL_DIR`，目錄權限 0700、信件權限 0600，不提供任何 HTTP 讀取入口。正式環境必須設定 SMTP；`YOUTH_SMTP_USE_TLS=true` 使用隱式 TLS，否則使用 `YOUTH_SMTP_STARTTLS=true` 升級 TLS，禁止明文 SMTP。寄信失敗回 503 並使該次挑戰失效，不假裝寄送成功。SMTP 密碼與 TOTP 加密金鑰不得寫入版本控制。

## 承辦登入及 LINE

`POST /auth/staff/session` 使用 `{identifier, password}`，只建立 5 分鐘有效的 `mfa_challenge_id`，不授予業務 Cookie。`POST /auth/staff/mfa` 使用 `{mfa_challenge_id, code}`，檢查 TOTP、帳號狀態及現有角色後才建立工作階段。TOTP 接受目前時間步及前後一個時間步，已成功使用的時間步不可重放。密碼使用 Argon2，驗證器種子使用 Fernet 加密，請保存並備援 `YOUTH_TOTP_ENCRYPTION_KEY`；任意換金鑰會導致既有 MFA 無法解密。

LINE 須配置 `YOUTH_LINE_CHANNEL_ID` 與 `YOUTH_LINE_PROVIDER_ID`。後端固定向 LINE 官方 `https://api.line.me/oauth2/v2.1/verify` 驗證原始 ID token，再檢查 issuer、audience、期限及 subject；不採用前端解碼的 user ID。測試替換 HTTP 呼叫不構成正式環境的驗證略過機制。

服務帳號近期驗證後，先呼叫 `POST /identity-links/line/challenges`，再以 `{challenge_id, id_token}` 呼叫 `POST /identity-links/line`。資料庫的有效連結唯一索引阻止同一 LINE 連結多個帳號或同一帳號被覆蓋綁定。`POST /auth/line/session` 只允許既有連結登入；它不構成近期服務帳號驗證，解除連結或變更信箱前仍須信箱重新驗證。

`DELETE /identity-links/line` 須近期服務帳號驗證、CSRF 及連結 `If-Match`。成功後保留帳號與案件、撤銷所有現有工作階段。通知工作在實際寄送前須重新檢查連結未撤銷及 binding_version，不得因訊息已排入佇列就繼續傳送。

## 驗證範圍

`tests/test_auth.py` 驗證真實 SQLite 儲存、Cookie、私有郵件 spool、OTP 重放／過期／次數限制、CSRF、MFA、LINE 錯誤 channel／綁定衝突、解除綁定、帳號停用與角色更新、信箱變更及平行 session touch。整體 [HTTP 煙霧流程](scripts/smoke.py) 已使用真實 PostgreSQL 完成青年信箱登入、承辦 MFA 及案件至結案／匯出的操作。LINE HTTP 呼叫在自動化測試中以固定官方回應替換；實際 LINE Channel 與 SMTP 投遞仍須另行整合驗證。完整範圍見 [實作狀態](docs/implementation-status.md)。
