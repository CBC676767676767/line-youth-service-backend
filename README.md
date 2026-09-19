# 竹青通｜新竹青年 AI 補助與案件服務

以 LINE 作為服務入口，協助青年申請 AI 工具補助、核對文件、補正與追蹤進度，並讓承辦以明確依據完成審查。本儲存庫包含民眾網站、獨立管理後台與既有案件 API。登入、案件、附件、補件及核定均使用伺服器紀錄；目前尚未連接市府正式收件或出納系統。

技術組合為 React／TypeScript／Vite 雙入口前端、瀏覽器內 Tesseract OCR，以及 Python 3.12、FastAPI、SQLAlchemy 2、PostgreSQL 17、Alembic 和獨立背景 worker。附件保存在私有本機目錄；容器模式使用共享 named volume。實作範圍、驗證證據及待辦詳見 [實作狀態](docs/implementation-status.md)。


## 團隊同步與兩個入口

本次網站整合分支為 `feat/hsinchu-youth-web-portals`。已有 clone 的成員可先保留自己的未提交變更，再執行：

```bash
git fetch origin
git switch feat/hsinchu-youth-web-portals
git pull --ff-only
```

- 民眾端：`http://127.0.0.1:8000/`，信箱驗證、補助申請、本機證件／收據文字辨識、附件上傳、進度及補件、安全學堂。
- 管理端：`http://127.0.0.1:8000/admin/`，獨立 HTML 與 JavaScript 入口，密碼＋TOTP 登入；無民眾／承辦角色切換。
- `admin` 是帳號管理權限；`reviewer`／`supervisor`／`auditor` 的案件存取仍依方案、指派與逐案授權。知道網址不會取得 API 權限。
- 同一瀏覽器的兩頁沿用既有同源 Cookie；若要同時操作民眾與主管，請用不同瀏覽器設定檔或無痕視窗。

完成下方後端安裝與遷移後，加入前端建置及補助方案：

```bash
cd frontend
npm ci
npm test
npm run build
cd ..
.venv/bin/python -m app.cli seed-grant
.venv/bin/python -m app.cli create-staff --email supervisor@example.org --role supervisor --scheme-id hsinchu-ai-grant-2026
.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

`create-staff` 的一次性憑證請私下保存；既有帳號不會被覆寫或自動擴權。新建補助方案也不會改寫原本 `youth-demo` 資料。

兩個入口由同一 FastAPI 服務提供，正式執行只需要 Python runtime；Docker 使用 Node 建置前端後複製產物，不新增 Node 常駐服務。開發代理、LIFF 設定與上線步驟見 [網站部署](docs/web-deployment.md)，方案欄位及文件要求見 [補助方案](docs/grant-program.md)。

## 這次交付與主題的關係

- **行政效能**：裝置內 OCR 擷取姓名、字號、生日與地址，申請人核對後帶入；文件類別與必要附件在伺服器再次檢查。OCR 不自動核定，也不代替戶政或本人驗證。
- **透明進度**：申請與補件均產生不可變收件回執；案件事件、承辦理由與通知沿用既有後端，不能由民眾網頁任意更改狀態。
- **LINE 串聯**：四個選單 URI 對應申請、進度、補件、安全學堂；已配置 LIFF 時可經既有後端驗證 ID token 登入／綁定。實際官方帳號、Rich Menu 與 HTTPS endpoint 尚需機關設定及真機驗收。
- **AI 安全素養**：申請中提供安全提醒；安全學堂包含工具風險清單、測驗、個資遮蔽練習、一頁式圖卡及事件案例。學習勾選目前不作補助自動核准條件，亦未納入政府資安認證。
- **經費核銷及撥款邊界**：已可收集付款依據與審查證據；完整核銷帳務及匯款仍待出納介接。後端 `DECIDED`／`CLOSED` 不會被顯示為已匯款。

## 已實作的核心流程

- 信箱一次性驗證碼、Cookie 工作階段、CSRF、承辦密碼與 TOTP MFA；LINE token 後端驗證及帳號綁定。
- 固定方案草稿、正式提交、不可變回執、補件任務修訂、接受／退回、主管核定及結案。
- 逐案及角色授權、ETag 版本檢查、正式提交冪等、重要操作稽核。
- 一次性附件上傳、不可覆寫物件、隔離掃描、每次下載重新驗權；掃描中仍可收件，僅 CLEAN 可閱覽或接受。
- 通知 Outbox、租約接手、固定 LINE retry key、加密請求快照；Webhook 驗簽、持久化去重與亂序處理。
- 站內通知、基本報表、非同步 CSV 匯出、帳號停用及受限制的角色管理。

本機預設掃描器是 `development-format-only-NOT-ANTIVIRUS`，只用於開發流程，沒有防毒保證。正式環境設定會拒絕開發掃描器；ClamAV 不可用時不會把文件標成 CLEAN。LINE 沒有設定時不會假裝已發送，站內通知仍可展示完整流程。

## 本機啟動：Docker 資料庫 + 原生 Python

需要 Python 3.12 與可執行 Docker Compose 的環境。以下命令皆從專案根目錄執行。現有工作環境已建好 `.venv`、私有 `.env`、資料庫與示範方案；重新接手時，直接檢查服務與遷移狀態即可。

全新環境先安裝相依套件；`requirements.lock` 鎖定執行期版本，`requirements-dev.lock` 包含開發及測試工具：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps -e .
```

只在沒有 `.env` 的新環境初始化一次。命令產生隨機憑證及權限為 0600 的檔案；已有檔案時會拒絕覆寫。

```bash
.venv/bin/python -m app.cli init
docker compose up -d db
docker compose ps db
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli seed
```

資料庫僅綁定 `127.0.0.1:55432`。第一次啟動需等 `db` 健康檢查通過再遷移；可用 `.venv/bin/alembic current` 查核，目前初始遷移為 `78a60fc3915a`。`seed` 建立 `youth-demo` 示範方案，其欄位及檢核不是任何真實補助規則。

在第一個終端啟動 API：

```bash
.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

在第二個終端執行 worker：

```bash
.venv/bin/python -m app.cli worker
```

或只執行一輪：

```bash
.venv/bin/python -m app.cli worker --once
```

開發環境提供 [互動 API 文件](http://127.0.0.1:8000/docs)、[就緒檢查](http://127.0.0.1:8000/health/ready) 與 [存活檢查](http://127.0.0.1:8000/health/live)。就緒檢查確認資料庫與結構可讀，不代表 LINE、SMTP、ClamAV 或備援演練已完成。

## 完整容器模式

此模式讓 API 與 worker 共用 `youth_data` volume，資料庫使用 `youth_db` volume。先遷移及 seed，再啟動應用程式：

```bash
docker compose up -d db
docker compose --profile app build api worker
docker compose --profile app run --rm api alembic upgrade head
docker compose --profile app run --rm api python -m app.cli seed
docker compose --profile app up -d api worker
```

原生模式的附件與郵件預設位於 `var/files`、`var/mail`；容器模式則位於 `/data/files`、`/data/mail`。同一資料庫的 API 與 worker 必須使用同一組附件及郵件儲存。切換模式前應一致搬移／掛載資料並停止原服務，不能把原生 API、容器 worker 與不同儲存目錄混用。Compose 不會自動執行遷移。

## 帳號與 API 使用方式

建立一個可操作完整審查流程的開發主管帳號：

```bash
.venv/bin/python -m app.cli create-staff --email supervisor@example.org --role supervisor
```

命令會輸出初始密碼、TOTP 設定種子與匯入 URI，請自行私下保存，不貼入 issue、聊天或版本控制。容器模式改用 `docker compose --profile app run --rm api python -m app.cli create-staff --email supervisor@example.org --role supervisor`。

青年透過 `/api/v1/auth/email/challenges` 與 `/api/v1/auth/email/challenges/verify` 登入。開發模式將驗證郵件寫入私有 spool，可在本機讀取最近信件：

```bash
.venv/bin/python -m app.cli show-mail
```

此命令會顯示驗證碼，只可用於本機開發；正式環境不可使用。承辦先呼叫 `/api/v1/auth/staff/session`，再完成 `/api/v1/auth/staff/mfa`。完整帳號契約見 [帳號模組說明](auth_notes.md)。

登入後由瀏覽器保留 Cookie。POST、PATCH、PUT、DELETE 等變更請求帶入登入回應的 `csrf_token` 作為 `X-CSRF-Token`，並使用允許的 Origin。已有資源的變更依端點要求帶入讀取回應的 `ETag` 作為 `If-Match`；正式提交及建立案件等冪等操作帶入 `Idempotency-Key`。同一提交重試必須保留原 key 與內容，不能因超時就換 key 重送。

附件依序呼叫 `/files/upload-intents`、返回的 PUT 路徑與 `upload_headers`、`/files/{id}/complete`，最後將 `file_version_id` 加入正式提交。上述路徑均有 `/api/v1` 前綴。完成上傳不等於正式收件，成功回執也不等於審查核准。

## 驗證與開發

```bash
.venv/bin/pytest
.venv/bin/ruff check app tests scripts
```

[scripts/smoke.py](scripts/smoke.py) 已在原生 API 與真實 PostgreSQL 上跑通：信箱登入、承辦 MFA、申請、冪等重送、補件、附件隔離／開發掃描、接受、核定、結案及 CSV 匯出。重新驗證時先啟動本機 API：

```bash
.venv/bin/python scripts/smoke.py
```

此腳本包含 PostgreSQL 下相同 key 的並行送件檢查，只允許 development、私有郵件 spool 與 PostgreSQL，且拒絕已配置對外 LINE 傳送的環境。它會建立可辨識的合成帳號及案件；成功結束時停用測試帳號並撤銷工作階段，保留案件作為驗證證據。一般斷言失敗或正常程序退出亦由 `atexit` 清理帳號；`kill -9`、程序崩潰或清理時資料庫不可用，仍須人工確認殘留帳號。測試涵蓋範圍及外部整合限制見 [實作狀態](docs/implementation-status.md)。

[GitHub Actions 工作流程](.github/workflows/ci.yml) 已加入儲存庫；遠端執行結果請查看 [Actions](https://github.com/CBC676767676767/line-youth-service-backend/actions)。本機驗證結果不代表遠端 CI 已通過。

## 設定、目錄與交付狀態

設定以 `YOUTH_` 為前綴，由 [app/config.py](app/config.py) 定義。`.env`、資料庫、附件、郵件、開發金鑰均不應提交 Git。正式環境另需 PostgreSQL、HTTPS／Secure Cookie、已配置的秘密與 TOTP 加密金鑰、SMTP 及 ClamAV；這些設定檢查不等於已具備正式上線條件。

| 位置 | 用途 |
| --- | --- |
| [frontend](frontend) | 民眾與管理端獨立入口，共用本機 OCR、樣式及 API client |
| [app/web.py](app/web.py) | 限定路徑的前端與靜態資源服務 |
| [app/main.py](app/main.py) | 應用程式、共用錯誤、CORS 與健康檢查 |
| [app/cases.py](app/cases.py) | 申請、任務、回執、審查與決定 |
| [app/files.py](app/files.py) | 授權上傳與下載；私有儲存見 `app/storage.py` |
| [app/notifications.py](app/notifications.py) | 通知及 LINE Webhook；排程執行見 `app/worker.py` |
| [app/admin.py](app/admin.py) | 認領受理、帳號、權限、匯出及彙整 |
| [migrations](migrations) | Alembic 資料庫遷移 |
| [tests](tests) | 自動化測試 |

程式已發布至私有儲存庫 [CBC676767676767/line-youth-service-backend](https://github.com/CBC676767676767/line-youth-service-backend)，主分支為 `main`。需使用具有儲存庫權限的 GitHub 帳號存取；執行環境憑證不隨程式發布。

LINE 行為依據：[訊息 retry key](https://developers.line.biz/en/docs/messaging-api/retrying-api-request/)、[Webhook 原文簽章驗證](https://developers.line.biz/en/docs/messaging-api/verify-webhook-signature/)。
