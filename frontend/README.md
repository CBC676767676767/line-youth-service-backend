# 竹青通雙入口前端

民眾與管理後台是 Vite multi-page 的兩個獨立 HTML / React entry，共用元件、文字辨識及同源 API client。

| 目錄 | 職責 |
| --- | --- |
| `src/citizen/` | 民眾首頁、信箱登入、申請草稿、附件、補件與進度 |
| `src/admin/` | 工作帳號密碼＋TOTP、案件／補件／證據審查、主管決定、帳號管理 |
| `src/shared/` | API、CSRF、版本及冪等請求、三階段私有附件上傳、型別及中文狀態 |
| `src/components/` | LINE 選單、裝置內 OCR、AI 安全教材及個資遮蔽練習 |
| `public/ocr/` | 同源 OCR 模型與引擎，附來源、SHA-256 及授權 |
| `tests/` | 證件解析與 API 邊界的 Node 測試 |

```bash
npm ci
npm test
npm run build
```

先完成根目錄 README 的 Python／DB 初始化、遷移、`seed-grant`，再開 FastAPI，從同源 `/` 和 `/admin/` 操作。`dist/` 是建置產物，不提交到 Git；Docker 和 CI 都從鎖檔重新建置。

開發時 `npm run dev` 將 `/api` 代理至 `127.0.0.1:8000`；後端需顯式允許 `http://127.0.0.1:5173` Origin。正式環境走同源 HTTPS 與 Secure Cookie。

LIFF 的公開識別碼放 `.env.local` 中的 `VITE_LIFF_ID`，變更後重新建置；不放任何密鑰。設定方式見 [部署說明](../docs/web-deployment.md)。只在明確按下登入／連結時取得 ID token，直接送同源後端驗證，不信任前端 profile、不存 web storage。

申請資料、附件、角色與審核決定均以 API 為準；沒有前端角色切換或自造收件紀錄。OCR 只是候選欄位，人工確認後才帶入；原始圖片仍需明確選擇上傳。完整證號和 OCR 原始文字不作草稿欄位。每一文件類別一份檔案，有多頁時先合併 PDF。

LINE 真機、正式 SMTP／防毒／機關及出納串接另行驗收。資料庫結案不代表匯款，欄位檢查碼也不代表身分證真偽或本人驗證。
