# 隔離的瀏覽器與 API 整合測試

`scripts/web_smoke.py` 建立一次性的 SQLite 測試服務，使用既有 FastAPI、Email OTP、密碼／TOTP、Cookie、CSRF、文件 API 與案件狀態機。它沒有登入捷徑，也不替換 API 回應。

資料庫、附件、合成圖片、郵件 spool 與隨機秘密全部位於私有暫存目錄；帳號只使用合成的 `example.org` 信箱。只監聽 `127.0.0.1`，SMTP／LINE 設定明確留空，掃描只呼叫 `process_scans`，不執行通知 worker。停止服務後刪除本次資料與憑證檔。這個工具不可作為正式環境啟動方式。

## 自動驗證完整 API 流程

先依 README 安裝 Python 開發依賴，並完成前端 `npm ci`、`npm test`、`npm run build`。在儲存庫根目錄執行：

```bash
.venv/bin/python scripts/web_smoke.py smoke --require-web
```

此命令使用另一個本機埠 `8012`，自動建資料、啟動真 HTTP 服務、測試並清理。可用 `--port` 指定未使用的本機埠。尚未建置前端但想先測 API 時，可省略 `--require-web`。

測試以三個獨立 Cookie client 操作民眾、方案主管與系統管理員，涵蓋：

1. 真 Email OTP 寄到私有 spool、讀取郵件驗證碼後登入；承辦另走密碼及 TOTP MFA。
2. 建立 `hsinchu-ai-grant-2026` 草稿、儲存並重新讀取，依 API 上傳六種有效 PNG 位元組，完成檔案上傳。
3. 掃描中仍可正式提交並取得不可變回執；掃描前下載遭拒。
4. 主管讀案件、手動開始審查；`QUESTION` 不會自動建立補件任務，需另呼叫建立任務。
5. 民眾讀取任務、上傳新付款文件並正式補件；未通過掃描時承辦不能接受。
6. 執行檔案格式掃描，驗證 `CLEAN` 只改檔案狀態；任務仍須明確接受才會成為 `ACCEPTED`。
7. 未完成審查不能決定；測試角色逐項呼叫審查 API 填入結論與具體版本依據，全部 `PASS` 仍維持 `UNDER_REVIEW`，另作決定後才成為 `DECIDED`。

SQLite 流程用來驗證契約及整合，不取代既有 PostgreSQL 遷移、並行送件與資料庫行為測試。`CLEAN` 的掃描引擎明確是 `development-format-only-NOT-ANTIVIRUS`，不代表已完成防毒、文件真偽、身分或資格驗證。自動 HTTP 測試也不等於瀏覽器操作測試。

## 提供 Playwright 的持續測試服務

第一個終端執行：

```bash
web_smoke_dir="$(mktemp -d)"
.venv/bin/python scripts/web_smoke.py serve --port 8011 --state-file "$web_smoke_dir/state.json"
```

`state.json` 以 `0600` 權限建立，不可放在儲存庫內，且不會覆寫既有檔案。測試程式透過本機檔案讀取，不要 `cat`、`console.log`、附加為 CI artifact 或放入截圖。其結構包含：

| 欄位 | 用途 |
| --- | --- |
| `base_url` | 測試服務網址 |
| `applicant.email`、`form` | 合成申請信箱及完整表單 |
| `staff.supervisor`、`staff.admin` | 各自的信箱、密碼及 TOTP seed，僅供測試程式使用 |
| `documents[類別].path` | 真 PNG 圖片；六種基本文件及條件文件均可用 |
| `mail_spool_dir` | 測試 OTP 郵件，只允許本機測試程式讀取 |
| `codes_file` | 更新後的私有 OTP／TOTP JSON 檔路徑 |

民眾、主管與管理員請使用不同 Playwright browser context，避免同源登入 Cookie 互相覆蓋。`serve` 不會預先替民眾建立案件，讓瀏覽器自行完成建立、存檔與提交；Email 帳號也由真正的 OTP 驗證流程建立。

測試程式按下「寄送驗證碼」後，可呼叫以下命令更新 `codes_file`，再由測試程式直接讀取 `applicant_otp`、`supervisor_totp` 或 `admin_totp`，不經工具輸出顯示：

```bash
.venv/bin/python scripts/web_smoke.py codes --state-file /同一私有暫存目錄/state.json
```

`codes` 不會印出實際碼值，只寫私有檔案。TOTP 仍遵守後端的防重放規則；同一帳號在同一時段重複登入時，要等待下一個有效 TOTP 時段，不可重設伺服器的已用碼紀錄來繞過檢查。

預設每秒執行一次安全的開發格式掃描。若需要精確測試「掃描中不可接受」邊界，以 `--scan-interval 0` 啟動，再由測試程式於指定步驟執行：

```bash
.venv/bin/python scripts/web_smoke.py scan --state-file /同一私有暫存目錄/state.json
```

這個命令只掃描本次 fixture 的文件，不發送 LINE／SMTP。測試程式可用 `/__smoke__/identity` 比對 `fixture_id` 確認連到自己的測試服務；此路由只在 fixture 中註冊，不在正式 app 開啟。

測試完成後以 Ctrl-C／正常終止訊號停止服務並清理。`kill -9` 或機器突然關閉無法執行清理，需自行刪除該次私有暫存目錄，勿保留測試憑證作為交付物。
