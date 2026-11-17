# SQLite 會議資料契約

`MeetingStore(root)` 與 `AssistantStore(root)` 共用 `root/.meeting-store.sqlite3`。它是來源、runtime config／status／events／stop、Calendar／系列／場次／run、詞庫、summary revisions／outbox／待辦的唯一權威。Meeting URL 是私有 locator，不是 series 或 session ID。

## 識別與租約

- series：跨次會議記憶範圍；不同 Calendar／account 不以相同 URL 或標題自動合併。
- occurrence：Calendar recurring parent + original start 的穩定場次；改期不換身分。手動場次可明確綁定系列。
- session：32 位 hex execution attempt；不能把重啟視為同一會議重新入會許可。
- capture／summary lease 分開。每個 durable effect 在相同 SQL 交易驗證 kind、session、owner、generation、token、expiry。不能先在交易外檢查再寫入。
- lease 的有效時間使用 Linux `CLOCK_BOOTTIME`＋`/proc/sys/kernel/random/boot_id`，持久保存 `boot_id`／`expires_boottime`。同次開機跨程序共用；系統校時不改變有效期，實際 suspend 時間仍計入。換 boot、真正逾期或無法取得此時鐘時 fail closed；`expires_at` 僅留作 wall-clock 稽核，不再決定准入。

`AssistantStore.claim_run()` 同時檢查錄音授權、唯一 active capture 及所有持久 cleanup interlocks。回收 expired capture 會封存成 interrupted，不允許自動重播；即使是另一個 occurrence，也須先釐清未知資源。過期 summary 可恢復 outbox，不因此自動加入會議。

舊資料庫初始化時會加入上述兩欄，但不把舊 wall-clock deadline 換算成新有效租約。舊 active lease 保持無效，由既有 recovery 封存 capture／恢復 summary outbox；來源、摘要及 cleanup interlock 不被抹除。部署前須停止舊程序並保存一致性備份；舊映像不能直接寫入已升級 schema，回退須另行處理相符的資料庫版本。

## 來源 API

```python
from teams_transcription.storage import Session

# 真正 daemon 另外傳入當次 capture lease；此例只示意本機受控資料。
session = Session('/private/meetings').create({'provider':'mock','source':'synthetic'})
session.append_final({'event_id':'controlled-event','revision':0,
    'text':'受控來源','timestamp':'2026-10-14T01:00:00+00:00','sequence':1})
snapshot = session.store.read(session.ident)
```

Mutation methods 回傳 bounded metadata，不 materialize 整場來源。完整來源須明確 `MeetingStore.read(meeting_id)`，含 `events`、`segments`、`provisionals`、`increments`、`captures`、canonical `sources`、structured `prior_context`、`version`、`sha256`、`transcript_state`、terminal／completeness 及已發布 summary。沒有 `get_result`、file adapter、全文 cursor 或 Markdown aliases。

`Session.entries()` 是來源的統一 summary view：保留 canonical source ID、revision、文字、source state、provider final、quality、receipt／source clock 及 ordering。`Session.summary_source()` 凍結相同來源 version／SHA，供摘要 compare-and-swap。原始 event ID 與 canonical citation ID 不混為 provider 音訊身分。

Final ID + revision 相同且同文是 replay；同身分異文拒絕。新的明確 revision 取代舊可見版本但保留來源修訂。沒有 provider 身分的同文仍保存，不以字串去重。legacy provisional 無 clock 保持 null，不補零或假 source sequence。

## Runtime 與權威

`Session.config()`、`status()`、`update()`、`event()`、`stop()`／`stopped()` 使用 `session_runtime`、`session_events`、`session_stop_requests`。stop-before-create 仍有效。缺少 SQL runtime 回 `session_not_created`，不讀取／匯入 `config.json`、`status.json` 或 stop sidecar。

不產生、讀取或修復會議 `.md`／`.jsonl` 投影。外部改動舊 sidecar 不影響來源、狀態、摘要 acceptance 或 B context。既有 SQLite schema／歷史來源保留；`prior_minutes` 等歷史 SQL 欄名與 source digest key 不為美化名稱而破壞既有資料。歷史字串 context 明確標示 `historical_unverified`，不冒充已驗證結論。

完整 `read()`／`entries()` 會 materialize 整場資料，不能宣稱常數 RAM。Capture 寫入及 mutation 回傳有界；會議 A 使用原生媒體傳輸，不累積 Python PCM 送音 queue。B／明確要求的 WAV 另有有界 PCM 緩衝；摘要分組與 B wire budget 另外限制模型上下文。

## 封存與衍生資料

Terminal／application seal 不是 provider final。停止來源與 enqueue summary 在相同交易，無已存來源不排空摘要。summary publication 驗證 sealed snapshot、source version／SHA、有效 summary lease、四分類 citations、host source_quality 及真正 backend metadata；canonical JSON revision、summary job 與系列 task memory 同交易發布。

只有 SQLite summary revision 是發布結果，不寫 `summary.md` 或 `summary.json`。mock／legacy 不升格為正式 Codex acceptance；摘要成功不覆蓋 capture error 或 partial quality。詳見 `SUMMARY-RUNTIME.md`。

```bash
PYTHONPATH=tests:. python -m unittest test_meeting_store test_durable_delta test_sqlite_only_store test_source_file_isolation test_assistant_store -v
```
