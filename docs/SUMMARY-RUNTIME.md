# 持久會後摘要與系列記憶

**目前服務已停用自動摘要（使用者取消需求，2026-10-31 修正）。** 正常完成、停止與 crash recovery 都不再建立摘要 job；daemon 啟動不消費歷史 outbox，管理頁重試回 `summary_service_disabled`。封存逐字來源、歷史已發布摘要與待辦不刪除，也不新增替代服務。下文保留既有歷史資料格式與低階發布／lease 規則，不能當成自動服務仍啟用的操作說明。

摘要是 SQLite 的衍生資料，不是 provider-final 逐字稿。Capture cleanup 不執行模型摘要；`AssistantDaemon` 的獨立 summary worker 消耗持久 outbox，使用一般 Codex turn。GPT Live 不負責直接寫 SQL 或自行啟動任意程式。

## 原子邊界

- `MeetingStore.finalize()` 或 `AssistantStore.finish_run()` 在來源封存交易內插入摘要工作。
- 同一 session／source SHA-256 只有一個工作；沒有已保存文字就不排空白摘要。
- 捕捉與摘要各有獨立租約。來源寫入驗證 capture lease，摘要發布驗證 summary lease，均在相同 SQL 交易檢查 session、owner、generation、token 與期限。
- 模型執行前讀取封存 snapshot；發布時核對 source version 與 SHA-256。過期租約、不同 session 或變動的 snapshot 拒絕發布。
- 發布 canonical JSON summary revision、更新 summary job 狀態與 rolling task memory 同一交易。不產生 `summary.md`、`summary.json` 或逐字稿投影。
- 模型／網路工作不持有 SQLite 鎖。

Production 摘要保持 Codex CLI `0.159.0-alpha.3` 及既有 ChatGPT OAuth。省略 `summary_model` 時沿用操作者的一般 Codex 設定；不使用 Live／realtime 模型名稱，不新增 API-key 或 paid-provider fallback。模型與 provider acceptance metadata 由 host 的實際完成結果保存，不接受模型自稱。

模型generation沒有整份工作共用的總期限。各turn以60秒無有效進度判定停滯；只有本thread／turn的非空文字增量或已識別item的新生命週期能續期，重複／未知／外場事件不能續期。版本檢查、RPC啟動／ACK與cleanup仍有界。daemon每個summary lease週期的三分之一續租，涵蓋generation及cleanup；失去lease即取消工作，發布交易仍驗證fence。

## 摘要內容與品質

`summary.py` 驗證「重點、決策、待辦、未決問題」結構。每項引用必須來自本次封存 snapshot 的來源 ID。輸入過長時有界分組，來源文字／ID／provenance 仍保留；不可用語意相似或文字相同合併原始發言。

`source_quality` 是 host 聚合；逐段 provider final、application segment、provisional、來源 clock 與 ordering 均有區別。保存文字、會議終態或摘要完成不代表 provider 已定稿。歷史記憶另攜帶 capture status、錯誤碼與停止原因，不能用摘要成功覆蓋 capture 失敗。

## 重新啟動與失敗

中斷摘要租約回到 pending。已完成發布但尚未完成租約清理的工作，只補完 outbox，不再次呼叫模型。失敗工作保留固定錯誤碼，由操作者明確 retry；來源與已發布的前次摘要不刪除。重新啟動不重播舊入會。

Capture 清理未知會暫停後續入會，摘要恢復仍可處理已封存來源。未知清理不能被錯誤重設、重啟或已產出摘要視為成功。

## 系列記憶與待辦

前次摘要依 **occurrence chronology** 選取：較早會議的晚到摘要不取代較新的前次會議。新run凍結完整該版本摘要與系列待辦，歷史initialItems不再另作容量裁切。舊凍結副本若曾省略，保留其真實truncated／omitted資訊，不以更新資料改寫；原始摘要一直保存在SQLite。

待辦有穩定 ID、來源 session／source refs 及操作者狀態。相同文字、負責人與日期可延續既有衍生 task；不是原始逐字稿去重，也不是模糊語意合併。新摘要沒提到舊待辦，不代表完成；已完成待辦也不被新一輪相同文字自動重開。

`update_task(task_id, status)` 允許 `open`、`completed`、`deferred`，保存 audit。傳給 B 的歷史待辦會標示目前 `task_id`／`task_status`；未完成清單不包含已完成項。歷史文字不能蓋過操作者最新狀態。

## 驗證

```bash
PYTHONPATH=tests:. python -m unittest test_assistant_store test_assistant_daemon test_sqlite_voice_context -v
```

已跑過隔離 source → outbox → summary publication boundary，及跨 restart 的已發布工作收尾、不重入／不重播模型、stale lease、封存與排程 rollback、晚到摘要 chronology、任務延續與已完成狀態覆蓋舊待辦。

受控 summarizer 的結果只證明儲存與 orchestration 合約。不能當成此輪真實 Codex 模型或 Teams／Live 端到端成功證據。

另已沿本機既有 ChatGPT OAuth，對固定合成 application segment 執行正式 `summary.generate()`：實際 `gpt-6.1-sol`、14.408 秒，通過四分類 schema／引用／來源 fence／cleanup，發布 SQLite `json-v1` revision 1，來源 provider_final_count 仍為 0，沒有 Markdown／JSONL。收據見 `../evidence/standalone-codex-summary-20261014.json`。這證明一般 Codex 與 SQL publication 接線，不是 Live 轉錄、真人會議或下一場記憶的同次驗收。
