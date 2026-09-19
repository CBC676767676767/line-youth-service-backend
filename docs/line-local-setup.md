# LINE 設定與驗證狀態

Messaging API 與 LINE Login 使用不同 Channel。`YOUTH_LINE_MESSAGING_CHANNEL_ID` 屬於 Messaging API；`YOUTH_LINE_CHANNEL_ID` 必須是 LINE Login Channel ID，不能互相代填。LINE 登入只證明帳號控制，不取代戶籍、身分或補助資格查證。

## 已實作的 live reply 接線

| 設定 | 用途 |
| --- | --- |
| `YOUTH_LINE_BOT_ENABLED` | 預設 `false`；明確開啟聊天回覆 |
| `YOUTH_LINE_REPLY_MODE` | 支援 `disabled`、`fake`、`live`；預設 `disabled` |
| Messaging Channel ID、secret、access token、destination | live 模式需要完整有效設定；只放私人執行環境，不能進入 Git 或 `VITE_*` |
| `YOUTH_LINE_PUBLIC_PRECHECK_URL` | live 模式要求不含帳密的 HTTPS 預檢網址 |
| `YOUTH_LINE_SIMULATOR_ENABLED` | 預設 `false`，只供明確啟用的 development/test 離線測試 |

只有 bot 開啟且 mode 為 live 時，API 生命週期才啟動專用 LINE reply worker。它與 Webhook 共用有上限的記憶體佇列，只處理經簽章與 destination 檢查的入站事件；不會順便啟動 SMTP、附件掃描、匯出或主動推播 worker。

live adapter 呼叫固定的 LINE reply API，不追蹤轉址，也不把送出失敗改成 push。每次只嘗試一次；網路或伺服器回應不確定時標為 `UNKNOWN`，不假稱送達或自動重送。HTTP 200 只表示 `API_ACCEPTED`，不表示送達或已讀。離線 fake sender 仍明確標示 `FAKE_SENT`，兩者不互相替代。

本專案 Webhook 路徑是 `/api/v1/webhooks/line`。本輪已完成本機驗簽、去重、live adapter 的 HTTP 模擬與 worker 生命週期測試，並以合成模板完成官方 `validate/reply` 格式驗證，20／20 回應 HTTP 200。格式驗證沒有發送訊息；本輪尚未更改外部 Webhook、發布 Rich Menu 或對真實 LINE 使用者發訊息。不能把這些測試稱為真實 LINE 已接通。

## 預檢與保存結果

購買情境、工具、計費及通路用按鈕選擇後，以短效不透明 token 帶入 `/precheck`；這些選項不代表登入或案件授權。完整填答在網頁進行，匿名填答不持久化；聊天不收生日、證件、帳號、金額或附件。

後端評估只回存安全摘要。尚未保存的聊天卡明示狀態，導回完整預檢；只有登入後保存交易成功才顯示「已保存預檢（未正式送件）」。已保存的連結以 `line_result` 不透明 token 銜接，`GET /api/v1/line/results/{token}` 仍要求目前登入者、相同擁有人及案件權限。聊天或網址不攜帶案件 ID；token 不能取代驗權。

對話及交接綁定使用者、規則 hash、session、revision 與 20 分鐘期限。舊按鈕不能改寫新選項；重新開始、到期、規則更新或新評估會使舊結果無法冒充目前結果。資安練習保留預檢狀態。程序停止會清除暫存對話及結果連結；已保存快照仍依原案件權限取得。

`line_reply_jobs` 只記錄事件、通道、時間與處理狀態中繼資料，不保存聊天本文、LINE userId、reply token 或匿名表單。reply token 只留在有期限的程序記憶體中。敏感輸入、LINE 回應本文及憑證不寫入一般錯誤日誌。

## 本機啟動與後續部署

`sh scripts/start-precheck-demo.sh` 提供本機 `/precheck`，明確停用 bot、live sender 與模擬器。`--line-profile /private/path/line.json` 僅載入受限私人設定，不會啟動 sender；profile 必須由目前 Linux 使用者持有、權限為 0600，且不能是 symlink。不要把私人 profile 路徑、內容或驗證郵件放進交付文件。

模擬器只作離線回歸用途，預設沒有 UI 入口；只有 development/test 明確開啟後才可從 loopback 使用。最新本機服務已確認模擬器頁面與 JavaScript 皆回應 404，正常入口、預檢與 readiness 回應 200，未登入查詢結果 token 回應 401。合成 Channel、時鐘與假發送紀錄不代表真實 LINE 狀態。

目前 GCP 已完成認證，確認 billing 已啟用且 Compute API 啟用成功；尚未部署 VM。部署檔案位於 `deploy/gcp/`，仍在整理與驗證。公開回呼尚缺網域及 HTTPS，正式信箱登入尚需 SMTP；完整 LINE 登入／綁定另需同一 Provider 下的 LINE Login Channel、Provider ID 與啟用 openid 的 LIFF ID。前端 `VITE_LIFF_ID` 設定後須重新建置；首次綁定沿用既有登入與明確綁定流程。

本輪沒有更改 LINE 帳號外部設定、建立 Git commit 或執行 GitHub push。完成部署後仍需真實 LINE Webhook、Login／LIFF 回跳、Rich Menu 及外部投遞驗證。測試結果與部署限制集中記錄於 [實作狀態](implementation-status.md)，操作與素材見 [圖文選單](line-operations.md)。
