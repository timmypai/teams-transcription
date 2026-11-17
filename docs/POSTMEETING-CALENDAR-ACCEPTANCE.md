# Calendar → 系列最新狀態驗收協議

本文件保存 2026-11-02 新需求的 Calendar 真測協議，適用既有
`paulpai0412/teams-transcription`；不新建產品、第二個輪詢器或部署。
主線擁有真實身分確認與測試活動建立／改期；此分支只準備可審閱契約與合成 fixtures。

## 本次證據邊界

- **已完成離線測試**：`tests/test_postmeeting_calendar_identity.py` 以 invented Google
  payload 經 `normalize_event` → `AssistantStore` 公開方法驗證下表；沒有真實 Google、
  Teams、模型或通知呼叫。
- **真 Google 測試 pending**：主線發現目前 Calendar connector 的 profile 是使用者家庭帳戶，
  並非 dot 自有測試身分。不能以該帳戶、私人 Calendar、任意 `account_id` 文字，或歷史
  10/28 fixture／browser 紀錄替代本次 dot 身分證明。
- 未建立、改期或刪除任何真實活動，未新增 OAuth／token、搬憑證、改 VM／Oracle 或掃描全部
  Calendar。身分不符時停止這條真測線；可繼續離線、adapter 與 UI 驗證。

## 身分與資源前置協議

1. 主線以已連接、已授權介面唯讀確認 dot 自有身分；保留觀察時間與對應 opaque account
   identity。`get_profile` 顯示家庭／私人帳戶時，結果標為 `identity_mismatch`，不建立資源。
   不能自造新的 OAuth grant、權限或改用他人憑證解決。
2. 明確選定 dot 自有的**一個專用測試 Calendar**，核對其正式 Calendar ID 與擁有者；不得使用
   `primary` alias 或 UI 名稱充作正式 ID，也不列舉使用者全部 Calendar。
3. 將已核身的 account identity＋正式 Calendar ID 綁到唯一既有設定列。只有此列 enabled；
   其他列不因驗收被更改。`account_id` 目前是分組欄位，不能單靠其文字證明 OAuth 身分。
   正式身分 tuple 不可在有既存 occurrence 的設定列上被重指到另一帳戶／Calendar。
4. 主線在這個 Calendar 建立一個可清理的 recurring parent，至少兩個 instances，不邀任何
   第三人。使用當次核准且有效的測試 Teams locator；沒有時先驗收同步／改期／取消，
   `missing` locator 需顯示待確認，不能用合成 URL 冒充可入會活動。
5. 測試 token／session、真實 locator、活動內容及完整 account／Calendar／event ID 留在私有
   runtime；source 只保存 synthetic fixtures 及去識別的 hash、版本、時間與結果。

## 沿用的簡單排程

沿用同一 `AssistantDaemon` Calendar poll，預設 60 秒；scheduler 持續使用原有 owner、
租約與互斥。指定測試 Calendar 的 `join_early_seconds=300`，其他錄音／發聲 gate 仍需有效。
T-5 表示依**最新 start**開始準備／准入窗口，不是整場 deadline；正常結束取授權 occurrence
end、明確 stop 或撤權。背景輪詢不依附管理網頁生命週期。

真 Google 通知延遲、poll 邊界與 API 延遲會影響觀察時刻；驗收應保存實際時間，不能只用
單一固定 sleep 宣稱精確 T-5。入會前必須透過既有 `get_event` 重讀，晚到的改期／取消仍須
阻擋舊窗口的 Join。排程暫停、未知清理 interlock 或未授權時維持阻擋，不為測試放寬。

## 執行步驟與獨立判定

下表所有「真測」列目前均為 **pending**，需要主線完成上述 dot 身分前置。

| 步驟 | 操作與必查結果 | 必留證據 |
| --- | --- | --- |
| 真同步 | 同一正式 Calendar 建立 recurring parent＋兩次 occurrence；透過既有 poll 讀回，不在 SQLite 手造會議 | 當次 API 觀察時間、核身／Calendar hash、parent hash、兩個原始 start、各自 occurrence ID hash |
| 系列範圍 | 同 parent 的兩次屬同系列、不同 occurrence；不同正式 Calendar／account／parent 即使標題／URL 同也不合併 | 正式 identity tuple hash 與 series／occurrence mapping；跨 scope 部分可只用合成 fixture，勿為覆蓋新增其他真帳戶 |
| 改期 | 在 T-5 前將第一次 start 移後；原始 start 保持，event／occurrence／series 身分不換，最新 start、end、etag／updated 被保存 | 改前／改後 API 與 SQLite projection 的版本、原始 start、最新 start／end；舊窗口無 run |
| T-5 | 接近**改後** start 的五分鐘前；觀察 existing scheduler 準備／准入，入會前最新 `get_event` 成功 | Poll、fresh read、claim、準備狀態的 UTC 時間；相對 latest start 偏移；唯一 session ID hash |
| 取消 | 第二次在入會前取消，含最小 cancelled response；不重建活動、不 Join | 同 occurrence、cancelled revision、due 為空、無 run／join receipt；API deleted／cancelled 觀察時間 |
| 入會競態 | 在已到窗口、fresh read 前改期或取消；既有入會重查拒絕，不依賴舊 poll snapshot | latest API revision、拒絕 code、未新增 run；受控 fixture 不等於真 Google race |
| 唯一與恢復 | 重複 poll、重開管理頁、重啟 daemon；同 occurrence 不新增第二 run，不自動重入 interrupted run | 前後 occurrence／run count、原 session、lease generation／state、interlock；不持久化 token |
| 會中與結束 | 僅在當次有效測試會議與既有准入授權下進行；確認超過五分鐘仍持續到授權 end／stop，來源品質誠實保存 | actual admission／來源／停止／清理 receipt，provider final 與 application segment 分開 |
| 外部會後整理 | 終態來源立即形成 durable handoff；本場＋上一有效系列狀態交給已核實入口，保留失敗與重試狀態 | session＋source SHA＋pipeline version key、source revision／品質、lease／heartbeat、真入口執行 receipt；不得用 stub 當成功 |
| 原子系列發布 | 產本場摘要、決策、stable task ID、系列最新與本次變更；舊工作晚回不能覆蓋新 revision | 同一發布 transaction 的 revision、前版／本版 SHA、來源引用、任務變更；故障注入後無部分發布 |
| 通知與恢復 | 使用既有已授權目的地與機制；重試或 crash 不重複通知／待辦 | publication 對應唯一 notification key、真 delivery／ack receipt；未有目的地或 authority 時保持 pending |
| 查詢 | 預設只答最新有效結論、未完成任務與必要變更；歷史明問才查；一題鎖一個系列 revision，下題刷新 | 題目開始／結果 revision、引用 occurrence＋source revision；在題間發布新版的受控結果 |
| 網站 | 桌面、手機登入查看真同步日曆、改期／取消、歷史逐字稿、本場摘要、系列最新與 task 狀態；關頁後背景持續 | 當次候選 SHA、viewport、操作／截圖、無 JS error、關頁後觀察；10/28 截圖只能作歷史參考 |

完成／取消／被取代的 task 必須帶來源證據；未再提及的 open task 原 ID 延續，不重複新增；
已完成 task 不自動重開。沒有來源、外部入口失敗、模型未完成或清理未知都不能標 published。
晚補逐字稿產生新的 correction snapshot，保存原文與先前修訂，不覆蓋原封存來源。

## 離線 fixture 入口

```bash
PYTHONPATH=tests:. python -m unittest test_postmeeting_calendar_identity -v
PYTHONPATH=tests:. python -m unittest test_calendar_client test_assistant_store test_assistant_daemon -v
```

新增 suite 驗證 account／Calendar／parent 隔離、等價 UTC offset 的 original start、
latest-start T-5、最小取消、重送／重啟與 stale Google update。它只驗證資料與排程公開
seams；不能證明 OAuth 與資料的真身分綁定、真實時序、Teams 准入、真人遠端音訊、實際 Codex
整理或通知成功。

## 私有執行紀錄格式

每次真測以當次候選 commit SHA、UTC 開始／結束、執行 owner、核身結果與單一 Calendar hash
建檔。逐步保存 `observed / passed / failed / blocked / pending`，以及對應 API／UI／runtime
receipt 的私有位置和去敏摘要；不可將 `pending`、timeout 或未知 outcome 轉為成功。
建立／改期與清理只由主線資源 owner 執行。清理前重讀目標 ID、確認是本輪建立且仍屬 dot
專用 Calendar；不更改他人活動，不取消正在使用的會議，不以刪除資料掩蓋失敗。
