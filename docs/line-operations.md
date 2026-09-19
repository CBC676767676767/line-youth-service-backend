# LINE 圖文選單與對話操作

儲存庫包含六格圖文選單素材、離線驗證工具，以及可明確啟用的 LINE live reply adapter。`scripts/line_menu.py` 仍只處理本機素材，沒有發布、上傳、設定預設圖文選單或修改 Webhook 的功能，也不讀取 LINE 憑證或私人 profile。本輪尚未發布真實 Rich Menu，原帳號設定維持原狀。

`app/data/line-rich-menu.json` 是 LINE Rich Menu payload，版本名稱為 `youth-service-v1`；PNG 為 2500 × 1686 RGB。由左到右、由上到下依序為「補助預檢」`youth:start`、「補助規則」`youth:rules`、「備件清單」`youth:documents`、「申請進度」`youth:tracking`、「安全提醒」`youth:safety`、「服務選單」`youth:menu`。

「補助規則」會讀取後端規則版本，顯示公開日期、通路條件及官方來源；與「協助」的操作說明、聯絡入口分開。購買情境、工具、計費方式與通路四步同時提供手機 quickReply 及桌機 Flex 按鈕，使用相同簽章選項。工具翻頁、其他工具及舊按鈕檢查同樣適用。一般網頁備用入口不帶個人填答；改用網頁時會明示需重新填答。

文字指令為「選單、預檢、文件、進度、補正、安全、協助」。結果只顯示檢查數、狀態及版本。尚未保存會導回預檢；登入保存成功後才提供經登入驗權的本次結果入口。聊天點選不代表送件、補正受理或補助核定；安全練習不影響行政結果，也不假裝有真人接手。

Flex 卡片提供 altText。圖文選單替代文字為「青年補助服務六格選單：補助預檢、補助規則、備件清單、申請進度、安全提醒、服務選單。」；離線驗證結果的 `imageAlt` 也提供同一內容。測試介面使用具名稱的按鈕，不以單張圖片代替全部操作。

本輪另向 LINE 官方 `validate/reply` 端點驗證 20 組合成訊息，全部回應 HTTP 200，涵蓋指令、四步選項、交接、錯誤及結果卡。這只驗證訊息格式；沒有呼叫 reply／push 發送訊息，也不表示 Webhook 或手機端顯示已驗收。

## 離線素材驗證

從已安裝依賴的 repository 執行；省略子指令也只驗證本機素材。

```sh
python scripts/line_menu.py validate
python -m pytest tests/test_line_menu.py
```

驗證包含 JSON schema、版本、六個 postback 與標籤、區域範圍／重疊／空隙、PNG 格式與尺寸，以及不超過 1,000,000 bytes 的檔案大小。成功輸出 `mode: offline`、`imageAlt` 與涵蓋 JSON／PNG 的 `contentHash`，不進行外部 API 呼叫。

調整圖片時可使用本機已有的繁體中文字型；字型不隨專案交付。PowerShell 範例：

```powershell
.venv\Scripts\python.exe scripts/line_menu.py render --font C:\Windows\Fonts\msjhbd.ttc
```

WSL 對應字型路徑為 `/mnt/c/Windows/Fonts/msjhbd.ttc`。`render` 會重建本機 PNG 並驗證；仍需目視確認文字未截斷、圖片區域與按鈕一致。

素材規格參考 [LINE 圖文選單指南](https://developers.line.biz/en/docs/messaging-api/using-rich-menus/)與 [Messaging API 參考](https://developers.line.biz/en/reference/messaging-api/nojs/)。離線通過不代表已在 LINE 帳號啟用，也不代替真機驗收。live sender 與預設關閉的測試模擬器說明見 [LINE 設定](line-local-setup.md)。
