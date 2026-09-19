# 竹青通｜新竹青年 AI 補助與案件服務

以 LINE 作為服務入口，協助青年申請 AI 工具補助、核對文件、補正與追蹤進度，並讓承辦以明確依據完成審查。本儲存庫包含民眾網站、獨立管理後台與既有案件 API。登入、案件、附件、補件及核定均使用伺服器紀錄；目前尚未連接市府正式收件或出納系統。

另提供 `/precheck` 青年補助預檢精靈，以公開規則及使用者自述產生行政提示、條件式試算與備件清單，登入後可保存至本人草稿，並在民眾／承辦案件頁查看摘要。

技術組合為 React／TypeScript／Vite 雙入口前端、瀏覽器內 Tesseract OCR，以及 Python 3.12、FastAPI、SQLAlchemy 2、PostgreSQL 17、Alembic 和獨立背景 worker。附件保存在私有本機目錄；容器模式使用共享 named volume。實作範圍、驗證證據及待辦詳見 [實作狀態](docs/implementation-status.md)。


## 團隊同步與兩個入口

網站與管理後台已合併至 `main`（[PR #1](https://github.com/CBC676767676767/line-youth-service-backend/pull/1)）。已有 clone 的成員可先保留自己的未提交變更，再執行：

```bash
git fetch origin
git switch main
git pull --ff-only origin main
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

- 青年補助預檢：購買情境、工具／方案／通路、條件自查、Decimal 條件式金額試算、逐月交易及個人化備件、列印摘要；匿名輸入僅留記憶體。
- 登入後由後端重算保存不可變預檢快照；逐案授權、CSRF、ETag、冪等重試與完整重算差異，行政及資安結果分開。

- 信箱一次性驗證碼、Cookie 工作階段、CSRF、承辦密碼與 TOTP MFA；LINE token 後端驗證及帳號綁定。
- 固定方案草稿、正式提交、不可變回執、補件任務修訂、接受／退回、主管核定及結案。
- 逐案及角色授權、ETag 版本檢查、正式提交冪等、重要操作稽核。
- 一次性附件上傳、不可覆寫物件、隔離掃描、每次下載重新驗權；掃描中仍可收件，僅 CLEAN 可閱覽或接受。
- 通知 Outbox、租約接手、固定 LINE retry key、加密請求快照；Webhook 驗簽、持久化去重與亂序處理。
- 站內通知、基本報表、非同步 CSV 匯出、帳號停用及受限制的角色管理。

本機預設掃描器是 `development-format-only-NOT-ANTIVIRUS`，只用於開發流程，沒有防毒保證。正式環境設定會拒絕開發掃描器；ClamAV 不可用時不會把文件標成 CLEAN。LINE 沒有設定時不會假裝已發送，站內通知仍可展示完整流程。

## 本機啟動：Docker 資料庫 + 原生 Python

### LINE 聊天互動本機驗證

開啟 <http://127.0.0.1:8765/line-simulator>，可以實際點選六格選單及訊息按鈕。流程為購買情境、工具、計費方式、通路，再以短效不透明交接碼帶到 `/precheck`；交接碼只帶入非敏感選項，並立即從頁面 URL 移除，不能作登入或案件授權。

在 Web 頁面執行預檢後回到模擬器按「查看結果」，僅呈現後端產生的檢查數與狀態摘要。登入、保存、完整案件與待辦均沿用系統 API。側欄可測試重複事件、舊按鈕、25 分鐘過期、附件誤傳、預檢服務失敗，以及三種清楚標示的 DEMO 結果卡。

模擬器只在 development/test 且 loopback 用戶端可用；production 不開放。它以獨立合成 channel、使用者和密鑰，經原本 webhook 的驗簽／去重流程送到 fake sender。沒有對 LINE API 發送訊息、沒有新增 tunnel、沒有更動外部 Webhook、Rich Menu 或帳號回覆設定；也沒有推送 GitHub。LINE Login/LIFF 真實環境未驗收，不把本機模擬宣稱為真實 LINE 已接通。

對話、交接選項與安全摘要只在有上限的記憶體中保留；資料庫只新增事件與 fake delivery 中繼資料，不保存聊天文字、LINE userId、reply token 或匿名表單。新 migration `d76e14b290a1` 接續預檢快照 migration，不重設既有資料。每個模擬使用者的時鐘與狀態隔離；資安情境練習不改變預檢或使既有交接失效。

詳見 [本機 LINE 接線與驗證](docs/line-local-setup.md)及[離線圖文選單素材](docs/line-operations.md)。

### 預檢 MVP 快速示範（Linux／WSL）

原本私有郵件與附件 adapter 使用 POSIX 權限；Windows 請透過 WSL 執行，勿以省略權限檢查的方式繞過。首次執行需要 Python >=3.12、`python3-venv`/venv 模組及 `pip3`，可從專案根目錄啟動：

```sh
sh scripts/start-precheck-demo.sh
```

Windows PowerShell（本機已驗證 `kali-linux`）：

```powershell
wsl.exe -d kali-linux -- sh scripts/start-precheck-demo.sh
```

開啟 <http://127.0.0.1:8765/precheck>。腳本建立專屬 Linux 虛擬環境，沿用 Alembic 遷移及原本示範／整合試辦方案；SQLite、郵件與秘密保存在啟動時顯示的 `/var/tmp/youth-precheck-*` 私有目錄，重啟不重設資料。不啟動 worker、不發送真實 LINE/SMTP 訊息。Ctrl+C 停止服務。

同步網站分支後，民眾 `/` 與承辦 `/admin/` 保留原本 React／TypeScript 入口；先執行 `npm ci --prefix frontend` 及 `npm run build --prefix frontend` 即可由同一個本機服務操作。民眾入口含補助預檢連結，民眾與承辦的案件詳情均提供唯讀預檢快照摘要。沒有建置 React 時，`/precheck` 仍可獨立操作，原入口會如實提示尚未建置。

預設載入 `hsinchu_ai_2026 / public_2026_09_19` 官方公開來源快照，可直接檢查明確條件，並明示 `agency_approved=false` 與預算狀態未知。可使用「填入一組合成示範資料」快速試用：真實公告工具項目搭配虛构使用者填答，未知方案仍保留人工確認。一般類別填入 4,000 元臺幣合格費用，條件式試算為 2,000 元；改為特定類別則為 3,600 元，證明待確認。這些不是核定金額。

以 `demo-user@example.org` 走既有信箱驗證登入（`example.test` 會被既有 EmailStr 驗證拒絕）；驗證碼只寫入啟動時顯示資料目錄下 `mail/` 最新 `.eml`，不由 API 回傳。登入後建立／選擇本人草稿、保存，將購買通路改為代購重新預檢並保存第二版，在「我的案件」查看新增問題。登入與保存均不等於送件。郵件 MIME 可能為 Base64，需以郵件閱讀器或 Python email parser 開啟，勿直接把編碼文字當作驗證碼。

若要展示完全已收錄的合成工具／方案流程，停止服務後改用 `sh scripts/start-precheck-demo.sh --policy demo`。選「合成 AI 工作室 → 創作月訂閱（內含額度）」、月訂閱、官方網站及 `https://studio.example`，用生日 `2000-01-01`／新竹市自述／一般申請／購買日 `2026-09-01`、訂閱 `2026-09-01` 至 `2026-10-01`、本人付款。所有結果明示 DEMO。真實規則的[來源與歧義](docs/precheck-policy-sources.md)及[待確認清單](docs/precheck-pending-confirmations.md)分開維護。

### 規則設定

`YOUTH_PRECHECK_RULES_PATH` 指定 JSON；每次請求先通過 `PrecheckBundle` schema 驗證。預設真實公开來源種子在 `app/data/precheck-hsinchu-115.json`，合成規則在 `app/data/precheck-demo.json`。公開來源已核對和機關驗收是不同狀態；沒有可信來源的普通 draft 仍全部待確認。`YOUTH_PRECHECK_DEMO_ENABLED=false` 禁止 demo 判定；production 無論開關值都不會套用 demo。`YOUTH_PRECHECK_OFFICIAL_APPLICATION_URL` 只接受 HTTPS 且不得含帳密，優先於規則檔已配置的網址；兩者皆空時顯示未配置。修改規則及目錄須更新版本；舊快照保存原始輸入、完整設定、結果及雜湊，不會回寫舊結果。

新增路由皆在 `/api/v1`：`GET /precheck/catalog`、`POST /precheck/evaluate`、`GET/POST /cases/{case_id}/precheck`。匿名評估有 16 KiB 實際請求容量上限、字串長度驗證、既有資料庫速率限制及 `private, no-store`；只保存不可反推的來源限流識別，不保存匿名表單或結果。案件保存接受相同輸入格式，由伺服器重算，使用現有 CSRF、`If-Match` 與 `Idempotency-Key`。

文件準備勾選僅為「使用者自述已備妥」，不代表系統收到或內容核對；本輪不做文件解析、真偽辨識、政府介接、核定與額度保留。

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

程式已發布至**公開**儲存庫 [CBC676767676767/line-youth-service-backend](https://github.com/CBC676767676767/line-youth-service-backend)，主分支為 `main`；任何人都能讀取原始碼與提交歷史。執行環境憑證不隨程式發布，也絕不可提交：`.env`、`runtime.env`、資料庫密碼、session secret、TOTP 加密金鑰、LINE channel secret 與 access token、SMTP 密碼一律只存在於部署主機上的 root-only 設定檔。任何曾經推送到這個儲存庫的憑證都必須視為已外洩並立即輪替。

LINE 行為依據：[訊息 retry key](https://developers.line.biz/en/docs/messaging-api/retrying-api-request/)、[Webhook 原文簽章驗證](https://developers.line.biz/en/docs/messaging-api/verify-webhook-signature/)。
