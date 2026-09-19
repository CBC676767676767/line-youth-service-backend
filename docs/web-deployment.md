# 民眾與承辦前端的建置、入口與部署

新版前端放在 `frontend/`，使用一份 React／Vite 專案建置兩份入口 HTML。Python API 與 worker 的部署方式維持原有架構；執行時由 FastAPI 提供前端靜態檔案，容器不需要 Node.js 或另一個網頁伺服器。

## 入口與權限

| 網址 | 回傳內容 |
| --- | --- |
| `/` | 民眾入口 `frontend/dist/index.html` |
| `/admin/` | 承辦入口 `frontend/dist/admin/index.html` |
| `/admin` | 308 轉向 `/admin/`，保留查詢參數 |
| `/cases/{id}`、`/tasks/{id}` | 民眾入口，保留通知連結的原始網址；案件與任務資料仍須經 API 授權讀取 |
| `/api/v1/*` | 既有 API；不會套用網頁 fallback |
| `/assets/*` | Vite 建置的 JavaScript、CSS 等資源 |
| `/ocr/*` | 同源 OCR worker、WASM、語言模型及授權文件 |
| `/ai-safety-card.html`、`.pdf`、`.png` | 指定的安全教材 |

承辦 HTML 是可公開載入的登入外殼。網址分開不代表授權完成；Cookie 工作階段、承辦密碼與 MFA、CSRF、角色及逐案權限仍以後端 API 為準。未列出的頁面、資源與 API 路徑回傳 404，不會以通用 SPA 頁面掩蓋錯誤。識別碼只接受 1–128 字元的英數字、底線或連字號，首字元必須是英數字。

同源部署沿用現有 Cookie 與 Origin 檢查。民眾和承辦共用同一 Cookie 名稱與網域，在同一瀏覽器切換登入會切換工作階段；測試兩種角色時可使用不同瀏覽器設定檔。前端入口拆分不會另建一套身分認證。

此部署接線只保證兩個入口與資源可由後端提供，不能單憑頁面可開啟就判定每個畫面已與案件 API、通知或真實付款串接。實際功能範圍以 README 與各模組說明為準；通知網址載入前端也不等於案件已通過登入與授權。

## 本機建置

使用 Node.js 22 與專案鎖檔。在儲存庫根目錄執行：

```bash
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Python 相依套件、資料庫、遷移與 seed 的初始化仍依 README 執行。成功後開啟 `http://127.0.0.1:8000/` 或 `http://127.0.0.1:8000/admin/`。尚未建置時，這兩個網頁入口會回傳 503 與建置提示；API 和健康檢查可繼續使用。

預設建置目錄由 `app/config.py` 相對於儲存庫定位，與執行命令的工作目錄無關。需要其他位置時，設定：

```bash
export YOUTH_FRONTEND_DIST=/absolute/path/to/frontend/dist
```

Vite `base` 使用 `/`，所有共用下載與 OCR 資源使用絕對根路徑，讓 `/admin/`、`/cases/{id}` 與 `/tasks/{id}` 也能找到相同檔案。

建議先使用以上同源 `8000` 建置模式。若改用 `http://127.0.0.1:5173` 的 Vite 開發伺服器，需在後端的 `YOUTH_ALLOWED_ORIGINS` 明確加入 `http://127.0.0.1:5173`；只有確實使用 `localhost:5173` 才另外加入該來源。代理 `/api` 到 FastAPI 的 `changeOrigin` 主要處理 Host，不保證將瀏覽器的 Origin 改成 `8000`，因此仍會接受後端的 Origin／CSRF 檢查。不要允許任意來源。

```bash
YOUTH_ALLOWED_ORIGINS='["http://127.0.0.1:8000","http://localhost:8000","http://127.0.0.1:5173"]' \
  .venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

## Docker 與 Compose

Dockerfile 第一階段以 `node:22-alpine` 執行 `npm ci` 與建置，再將 `dist` 複製到 Python 3.12 runtime。最終映像只包含 Python 執行環境、後端與前端產物，不包含 Node.js、`node_modules` 或前端來源。

```bash
docker compose --profile app build api worker
docker compose --profile app run --rm api alembic upgrade head
docker compose --profile app up -d api worker
```

第一次啟動仍需先建立 `.env`、啟動資料庫及 seed，詳見 README。Compose 維持 `127.0.0.1:8000` 綁定，沒有新增對外公開的埠；正式 HTTPS 由部署環境提供，正式環境的 SMTP、ClamAV 及其他設定檢查未移除。

若需要 LIFF，建置時指定公開的 LIFF ID：

```bash
VITE_LIFF_ID=your-public-liff-id docker compose --profile app build api worker
```

亦可使用 `docker build --build-arg VITE_LIFF_ID=your-public-liff-id -t youth-service .`。`VITE_LIFF_ID` 會出現在前端程式中，修改後必須重新建置。不要把 channel secret、access token 或其他秘密放入任何 `VITE_*` 變數；後端秘密繼續以 runtime 環境變數注入。未設定 LIFF ID 不代表已完成 LINE 真實環境驗收。

LIFF 應啟用 `openid` scope，LINE Login 的 channel ID 與後端 `YOUTH_LINE_CHANNEL_ID` 必須一致；Login／Messaging channel 必須屬於同一 provider，並設定對應的 `YOUTH_LINE_PROVIDER_ID`。首次使用先以 Email OTP 建立服務帳號，再於已登入狀態明確選擇綁定 LINE。前端僅在使用者按下登入或綁定時取得 ID token，透過既有 `/api/v1/auth/line/session` 或綁定 challenge／確認 API 送交後端查驗；不將 token 寫入 localStorage、sessionStorage 或匯出報告。LINE 登入不等於政府實名驗證，亦不代替案件授權。本次尚未使用真實 LINE 帳號、LIFF channel 完成外部驗收。

## 快取與瀏覽器相容性

- 民眾與承辦 HTML 使用 `Cache-Control: no-store`，避免部署後保留舊的入口。
- Vite 帶雜湊的資源可長期快取；沒有雜湊的檔案會重新驗證。
- OCR worker、core 與語言模型檔名固定，使用 ETag／Last-Modified 重新驗證，避免版本更換後把舊模型與新引擎混用。WASM 使用 `application/wasm`，模型使用二進位 MIME。
- 沿用 `nosniff`、`no-referrer` 與 `X-Frame-Options: DENY`。沒有加入會阻擋 LIFF SDK、瀏覽器 worker 或 WASM 的新 CSP／跨來源隔離要求。
- 只公開設定的建置目錄與指定資源類型路徑；不公開 `var`、附件儲存區、`.env` 或程式碼。跨目錄符號連結和路徑穿越遭拒絕。

若反向代理另加 CSP，須以實機 LINE 與本機 OCR 驗收需要的 script、connect、worker、image 與 WASM 來源；不要直接套入會阻斷功能的預設政策。

## 持續整合與驗證

GitHub Actions 先使用 Node 22 執行鎖檔安裝、前端測試與雙入口建置，再執行既有 Python 靜態檢查與回歸測試、隔離 SQLite 補助方案 HTTP 流程、PostgreSQL 遷移及真實 HTTP 工作流程；同時檢查兩個網頁入口，最後建置完整 Docker 映像。

`tests/test_web.py` 驗證不同 HTML、通知深層網址、未知路徑 404、原有 API 授權、靜態資源 MIME、快取重新驗證、HEAD／Range、私有路徑及符號連結界線。瀏覽器的 OCR、LIFF、照相選圖及真實外部服務仍須另外驗收，不能以靜態檔回傳 200 取代。

可重複的真 API 與瀏覽器測試環境見 [隔離整合測試](web-smoke.md)。
