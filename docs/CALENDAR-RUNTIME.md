# Google Calendar 與自主排程

`calendar_client.py` 使用 Google OAuth 的離線授權及 Calendar events 唯讀 API。`daemon.py` 擁有背景輪詢、入會、停止與摘要工作；HTTP 請求或管理瀏覽器不是工作生命週期的擁有者。

## 授權與私有設定

目前是一份 Google OAuth 憑證供應所設定的 Calendar。`account_id` 是資料分組範圍，不是另一份 OAuth 登入；不要把不同 Google 帳號當成可同時使用的已授權登入。

- OAuth client 檔使用 Google 下載的 `web` JSON，包含 `client_id`、`client_secret`、`redirect_uris`。
- 回呼必須逐字匹配已註冊 URI；非 loopback 使用 HTTPS。
- 只要求 `https://www.googleapis.com/auth/calendar.events.readonly`。
- client／token 檔必須是目前 UID 擁有的普通私有檔案，權限 `0600`，不可經由 symlink；token 目錄必須私有。
- access token 自動更新，refresh token 以私有原子寫入保存。`invalid_grant` 會持久標示需重新授權，不繼續讀取。
- 連接 Calendar 不代表已取得加入、錄音或發聲授權。設定每個 Calendar 的 `enabled`、`recording_authorized`、`speaking_authorized`。

Google client、操作者授權與有效測試會議仍需實際提供。受控 HTTP fixtures 不代表已連接真實 Google 帳號。

## 排程與識別

預設 `scheduler_paused=true`。輪詢使用固定时间窗、`singleEvents=true`、`showDeleted=true`，消耗所有頁面，包括空白但含下一頁 token 的頁面。重複 page token、超大回應與過大的時間窗結果拒絕為錯誤；不回傳假完整結果，也不混用 `syncToken` 與時間過濾。

- **Series**：帳號範圍＋設定的 Calendar＋recurring parent；單次活動預設獨立系列。
- **Occurrence**：recurring parent＋原始開始時間，或單次 event ID。
- **Run**：獨立 session ID、來源記錄與租約。
- 改期與改 Teams URL 不更換既有 occurrence。不同系列使用同一 URL，不會自動合併。
- 人工分組是明確操作；Calendar 記錄不可合併到另一帳號範圍。

連結優先取 `conferenceData` video URI，再取描述／地點的 HTML 與文字。僅接受標準 HTTPS Teams 入會 host/path。多個不同 Teams 連結、無效連結、全天活動或無明確 UTC offset 的時間不自動加入。

## 行事曆與會議工作區

`admin_web.py` 保留登入及控制路由；`admin_ui.py` 與 `static/calendar.css`／`calendar.js` 提供 A 版週時間格及可展開工作區。靜態資產隨 wheel 打包，不需 frontend build、CDN 或新增套件。設定位於 `/settings`，進階狀態位於 `/diagnostics`。

- `GET /api/calendar?start=YYYY-MM-DD&end=YYYY-MM-DD`：設定時區的日期區間，起點包含、終點不含；返回所有重疊場次，不載入逐字全文。沒有日期的待確認場次另計數，回到排程處理，不捏造日期。
- `GET /api/meetings/{occurrence_id}`：同場來源、問答、摘要、控制狀態與同系列記憶。未啟動場次選取時間上較早的最新已發佈摘要；已有 run 則使用入會時凍結的前次快照。系列待辦每次重讀目前狀態。
- `calendar_view.py` 使用 SQLite 身分，不用標題／Teams URL 猜系列。只有有效 capture 租約且實際 `recording` 才顯示進行中；加入中仍能停止，但不冒稱已入會。後端每次操作仍重查授權、租約及 interlock。
- 詳情每 5 秒、週曆每 15 秒讀取。隱藏分頁暫停輪詢；失敗保留資料並標示可能過期。切換場次取消舊請求、拒絕晚到回應；不重建未變的閱讀節點或強制捲到底。
- API 未登入回傳 JSON／401；HTML 頁面導向登入。所有 mutation 沿用 `POST /actions/*` 的同源 Origin 與 CSRF。API／頁面不快取；瀏覽器腳本與請求僅允許同源。
- 人工場次表單使用設定時區的本地時間。DST 不存在或重複的時刻拒絕提交，不自行猜 offset；成功後導向新場次。

### 問答日誌的證據邊界

`qa_activity.py` 保存 B 已准入的 application question 與實際 assistant transcript 通知。正式 `response_runtime.py` 注入帶 capture lease 的 `Session.store`；`meeting_store.py` 在寫入交易內檢查 lease／sealed 狀態。日誌獨立於 A 的 source version、SHA、摘要與系列記憶，不能回灌成逐字來源。

目前固定 Native RPC 未提供可靠的 application request 對應。回答保持 `request_id=null`；有 provider item／turn ID 才能將該項 delta 更新成 final。匿名 delta 只是暫定顯示群組，匿名 final 另存，不依文字去重或硬配最近提問。日誌中的 final 也不是 Teams 播放完成證明。

保存工作由單一背景 writer 處理，最多等待 64 個操作，**沒有文字長度配額，也不限制問題或回答內容**。磁碟失敗或待寫操作飽和時停止接受新的日誌操作、明確顯示記錄不可用；不阻塞 A／B 音訊。已接受工作持續受 lease fence 約束。停止只使用既有清理時間窗的餘額；未完成的寫入保持追蹤並回報 `question_activity_cleanup_incomplete`，不能冒稱保存成功。舊會議缺少日誌時顯示沒有可用記錄，不推論沒有人提問。


## 執行限制

每次入會前重新讀取活動。取消、已過期、移到未來、無錄音授權或不在允許時間窗內的場次拒絕加入。已在執行的活動取消時，程式要求停止；設定撤銷錄音授權也會停止。

`max_duration_seconds`預設為`null`，沒有額外的一小時退會或四小時設定上界。操作者明確設定的正整數仍可提早停止。授權occurrence end永遠生效；無額外時長上限不代表可超過場次授權、撤權或管理stop。

全域僅有一個 capture 租約。同一資料根目錄僅允許一個 daemon。會議中斷不自動重入；未知清理狀態會阻擋新的 capture 並暫停排程，不能把重新啟動或人工取消錯誤當成清理成功。

來源封存與摘要 outbox 同一交易；摘要由獨立工作執行。重新啟動可恢復未完成摘要，不重播舊入會。沒有來源時不生成空白成功摘要。

### 中斷後的人工清理審查

登入管理頁後，從「歷史」開啟受阻場次。只有 run 已終止且來源已封存、沒有任何有效 capture 租約，並能以 PID／程序啟動簽章確認舊 worker 不在時，才可提交清理審查。尚未封存、程序仍存活、程序身分缺漏或無法查證，一律保持阻擋。

操作者必須親自確認兩件事，並逐一勾選未預選的欄位：

1. 舊瀏覽器、容器及相關程序均已停止。
2. 助理已離開會議。

`POST /actions/review-capture-cleanup` 使用登入 session、同源 Origin、CSRF，以及綁定當下 run／runtime 的 stamp。在同一 SQLite 寫入交易重查終態、租約、stamp 與 worker，新增 `capture_cleanup_reviews` 和 `capture_cleanup_reviewed` audit。頁面開啟後狀態改變，舊確認不得生效；後續 runtime 改變也會使舊審查失效。每個阻擋場次各自審查。

這是人工資源清理確認，**不是 provider STOP receipt 或逐字稿 final 證明**。原始 interrupted／failed、cleanup unknown、來源及摘要不改寫；只在所有阻擋都已有效審查時解除 capture interlock。排程仍暫停，不會自動入會或啟用發聲。不要用重啟、直接改 SQL、改 data root 或代替操作者勾選繞過確認。


## 本機合約驗證

```bash
PYTHONPATH=tests:. python -m unittest test_calendar_client test_assistant_daemon test_assistant_store test_calendar_view test_question_activity test_capture_cleanup_review test_admin_web test_admin_browser -v
```

受控驗證覆蓋 OAuth 更新、私有檔案、分頁、DST offset、原始 recurring 身分、最小取消回應、入會前改期／取消、背景排程與停止建立競態。尚未以此證明真實 Teams admission、GPT Live 音訊傳输或遠端可聽見 B。

## Provider 依據

- [Google Calendar events.list：時間窗、分頁與 syncToken 限制](https://developers.google.com/workspace/calendar/api/v3/reference/events/list)
- [Google OAuth web-server flow：離線授權、回呼與更新 token](https://developers.google.com/identity/protocols/oauth2/web-server)
