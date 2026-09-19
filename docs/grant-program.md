# 2026 新竹市青年 AI 工具補助方案契約

這個整合試辦依 2026 年公開資料建立，**未經機關驗收、未串接市府正式收件**。資料送入此服務會產生本系統案件與收件紀錄，不代表已向市府完成申請。資格、補助額、代付例外與最終核定均由有權限的承辦人工確認。

## 建立方案與人員

先套用既有 migrations，再執行：

```sh
python -m app.cli seed-grant
python -m app.cli create-staff --email reviewer@example.org --role reviewer --scheme-id hsinchu-ai-grant-2026
python -m app.cli create-staff --email supervisor@example.org --role supervisor --scheme-id hsinchu-ai-grant-2026
```

固定方案 ID 為 `hsinchu-ai-grant-2026`，表單版本為 1。`seed-grant` 不建立或覆寫 `youth-demo`；重跑不覆寫現存方案或已發布規則。舊 `seed` 與 `create-staff` 預設仍使用 `youth-demo`。非 admin 的方案必須先存在。既有帳號不可用此命令覆寫、換角色或擴充範圍。

`reviewer` 可開始審查、建立／接受補件與填寫檢核；`supervisor` 才能作成決定及結案。`admin` 仍依既有設計為全域帳號管理角色，`--scheme-id` 不會讓 admin 取得案件核定權限。每次建立人員回傳的一次性密碼及 TOTP 設定資料應私下保存。

## 公開方案與表單

`GET /api/v1/schemes/{id}` 與需登入的 `/form-schema` 都提供：

- `rule_version_id`: 此方案目前有效、已發布的 RULE 版本 ID；未發布、錯誤類型或不屬本方案時為 `null`，此時不能核定。
- `required_criteria`: `eligibility`, `documents`, `expense`, `identity`, `safety`。
- `criterion_labels`: 對應「申請資格與期限」「文件完整性與清晰度」「購買項目與申請金額」「本人、付款與收款資料」「重複補助與其他人工查核」。
- `required_document_types` 與 `conditional_document_requirements`: 下述文件條件；條件物件格式為 `{field, equals, document_types}`。

舊 `youth-demo` 也提供上述契約，檢核項目與必附文件仍依原設定。規則在本系統 `PUBLISHED` 只表示版本可作為審查依據，並非官方驗收證明。

`form_data` 固定接受目前前端的 27 個 key，額外欄位拒絕；每個 schema property 都有中文 `title`：

| 欄位 | 格式與必要性 |
|---|---|
| `name`, `email`, `birth`, `city`, `tool`, `purchaseDate`, `amount`, `requested` | 送件必填。email 檢查基本格式；日期為真實合法的 `YYYY-MM-DD`。city 與 tool 不設資格白名單。 |
| `channel`, `plan`, `payer`, `paymentMethod`, `special` | 送件必填，避免未填付款／身分類型繞過條件文件；special 為 boolean。 |
| `periodEnd`, `receiptName`, `receiptEmail`, `bankName`, `receiptAmount`, `phone`, `address`, `identityHint`, `company`, `origin`, `currency`, `originalAmount`, `bankType`, `category` | 可不填或為空字串；非空日期、email、金額仍檢查格式，相關文件內容由人工查核。 |

金額 `amount/requested/receiptAmount/originalAmount` 使用字串，最多 9 位整數與 2 位小數；不接受負數、千分號、科學記號或 JSON number。這是技術輸入格式，不是補助上限或核定規則。草稿可保存空字串，送件才要求上述必填欄位；非空且格式錯誤的草稿仍會回報 422。

列舉值與前端一致：

- `channel`: `official/reseller/unknown`；`plan`: `monthly/annual/credits`。
- `payer`: `self/relative`；`paymentMethod`: `card/telecom/wallet/other`。
- `bankType`: `taiwan/other`；`currency`: `USD/TWD/EUR/JPY/其他`。
- `category`: `通用型/影像類/辦公類/學習類/其他類`；最後三種選填列舉欄位也接受空字串。

不以姓名差異、縣市、生日範圍、軟體名稱、購買日期範圍、付款額或申請額直接自動駁回；也不把 OCR 結果當成身分驗證。`identityHint` 僅用於前端遮罩提示，不是已驗證身分資料。

## 文件與送件

初次送件固定要求 `ID_FRONT`, `ID_BACK`, `RECEIPT`, `PAYMENT_PROOF`, `BANK_ACCOUNT`, `AFFIDAVIT` 六種類別。`special === true` 時另要求 `SPECIAL_STATUS`；`payer === "relative"` 時另要求 `RELATIONSHIP`。後端以此次 submit 的 `file_version_ids` 查實際 File.document_type，不能僅靠前端勾選或把檔案上傳但未提交。缺少任一類別會回 422 `REQUIRED_DOCUMENT_MISSING`，field_errors 會列出缺少類別，案件保留草稿。

文件類別存在不代表內容完整。代付切結書及關係證明可放同類別的多個附件或合併 PDF；信用卡、臺幣帳單、繳款等多份證明可同屬 `PAYMENT_PROOF`，仍由承辦逐份查核。現有 API 一次提交最多 10 個版本，是本系統產品限制，並非官方文件數上限。上傳仍沿用 PDF/JPEG/PNG、容量、私人儲存、掃描與存取控制。

申請者 `GET /cases/{id}` 的 `files` 可取回本案已完成儲存的附件，包含 `task_id`，供重新整理後恢復草稿及補件附件選擇。承辦 `GET /staff/cases/{id}` 的 `files` 僅包含本案 Submission 正式提交過的版本，不列未提交的草稿附件。兩者都先檢查案件權限，再回傳 file/version ID、檔名、類別、掃描狀態等 metadata；不回傳儲存位置或上傳 token。下載與預覽仍須經現有授權 API；未 CLEAN 不能下載。

案件仍依 `DRAFT → RECEIVED → UNDER_REVIEW → DECIDED → CLOSED`，補件 `OPEN/REOPENED → SUBMITTED → ACCEPTED` 是獨立狀態；收到文件不等於補件已接受或案件通過。開始審查會建立五個 PENDING 檢核項目，不會自動 PASS。人工核定須完成項目、處理全部補件、引用正式提交且通過掃描的證據，以及有效規則版本。已送件表單不可直接覆寫，更正透過補件說明與附件保留紀錄。

本版沒有核銷／撥款登錄功能；`CLOSED` 只代表案件結案，不能顯示為已撥款。

## 官方來源與未決事項

依 [新竹市數位申辦平台 115 年度 AI 工具補助申辦須知](https://dgservice.hccg.gov.tw/serviceNotice.do?id=1323&rule=guest)及其 115/8/14 修正版完整計畫建立；核對日 2026-09-19。計畫第 1–4 頁是對象、期限、金額、審核及核銷；第 5–6 頁附件一列申請欄位及付款證明；第 7 頁存摺，第 8 頁切結書，第 9 頁代付共同切結書。可由上述官方頁下載現行附件。

需要機關確認而未寫成自動資格規則的項目：代付人範圍在本文與附件的表述不同、月繳累積／跨月與購買後申請期限、金額尾數處理、10 個政府工作天補正期限的起算。官方列舉的補助工具不是封閉白名單；姓名不同可能涉及付款例外。未找到另提第二次核銷申請或另送收據正本紙本的明文，也不能保證日後免查驗正本或固定撥款日期。

驗收至少包含：完整及空白表單、非法日期／金額／列舉、六種固定文件、兩種條件文件的四種組合、未發布規則不外露、跨案存取拒絕、承辦不列未提交附件，以及舊示範方案與全套既有測試。
