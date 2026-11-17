# 自主會議助理 runtime

## 唯一控制入口

`teams-transcription serve --config /private/runtime.yml` 啟動 Starlette 管理頁及單一 `AssistantDaemon`。ASGI lifespan 擁有 daemon，daemon 擁有 Calendar、capture 與 summary tasks；request、分頁及 WebSocket 不持有會議生命週期。程序以 data-root flock 防止另一個 daemon 同時執行；Uvicorn workers 固定一個。

應用不提供 MCP、dot、Page、Markdown renderer 或文字模型答題 fallback。主程式不把 SQLite 寫入權交給 Live。模型只收到 host 選取且有明確 provenance 的文字；會議 A 使用原生音軌傳輸，B 的輸入觀測／明確要求的 WAV 使用有界 PCM RAM。B 的不補音輸出留在瀏覽器內，詳見 `B-LIVE-NATIVE.md`；不做模型資料檔案交換。

## Calendar → capture

1. 操作者完成管理登入、Google readonly OAuth 與 Calendar 設定，明確標示錄音／發聲授權。排程預設暫停。
2. `calendar_client.py` 分頁讀取事件，`AssistantStore.sync_occurrences()` 保存取消、改期、Teams locator 及系列／場次識別。
3. scheduler 在 join window 內選取 pending occurrence；入會前重新取得 Google event、取消／授權／時間狀態。URL 不作會議系列主鍵。
4. `claim_run()` 在同一 SQL 交易核對全域 capture slot、持久 cleanup interlock 與錄音授權，建立 session、lease，凍結前次摘要／待辦與詞庫。
5. 正式 `_admission()` 透過既有 Codex readonly `account/read` 核對 ChatGPT 路徑，不憑 plan、模型目錄或 fixture 宣稱 entitlement。
6. 真正 `worker.run_session()` 開 sandboxed Chromium。Lobby、login-required、joined、prepared、recording 分開；daemon 僅確認自身 session 的 fresh owner-ready generation。
7. A：incoming audio → 原生 WebAudio mix／owned 本機 WebRTC relay → 隔離 Live WebRTC → 增量／真正 finals → fenced SQLite。原生媒體層管理節奏，沒有 A Python PCM push、送音 queue 或短窗時鐘中止。B 的 PCM 觀測錯誤不停止健康 A；實際 source scope／原生 terminal failure 與未知清理仍失敗關閉。

Calendar HTTP transport、runner、summarizer、clock 可 constructor injection 供受控測試；沒有 YAML／env 的 fake provider 或 admission bypass。

## B 與授權

`response_runtime.py` 組合獨立 native B、scoped SQLite reader 與 owned Teams sender。B startup 得到標為歷史的前次摘要／待辦，不混成當前發言。A startup 只取得已核准詞庫，不灌完整歷史逐字稿。

B 預設靜音。當次 policy 明確允許發聲後，由 daemon 對 active controller 授權；`set_output()` 在 await 前後重查 session／lease／stop／policy。使用者手動靜音不被同次監督立即自動打開。撤權及停止關閉輸出；`stop_unknown` 不能回報為成功靜音。sender 身分、來源 clock、owned-output overlap 與 foreign sender 檢查保持 fail closed，見 `B-LIVE-NATIVE.md`。

## 終結 → 摘要 → 下次記憶

Capture 結束、drain、清理 → SQLite source seal 與唯一 summary outbox 同一交易。摘要在獨立 worker 執行，不把模型工作塞進 RX cleanup deadline。

一般 Codex 使用封存來源及其 ID，驗證四分類 JSON、引用、actual model、turn completion、cleanup、source version／SHA 後，以 summary lease 原子發布至同一 SQLite，並更新系列記憶／待辦。新會議按 occurrence chronology 取前次已發布摘要，而非最後寫入時間。摘要失敗保留來源、舊摘要與固定錯誤碼，由 UI 明確 retry。

重啟可恢復 outbox，不重播已發布模型工作，不重入 interrupted meeting。中斷 capture 或未知 cleanup 產生持久 interlock；包括 daemon 啟動後才過期的舊 lease。scheduler 暫停，DAO 也在新 claim 的同一交易拒絕，不能靠重啟或換 occurrence 迴避。

## 管理與驗證

管理頁包含設定／Calendar OAuth、排程／手動場次、停止／輸出、系列歷史／來源／摘要／待辦、詞庫核准及 audit。登入使用 private scrypt hash、server-side opaque session；變更要求同源 Origin + CSRF，OAuth state 只用一次且綁定登入 session。`/healthz` 不含私有狀態，`/api/status` 要登入。

```bash
PYTHONPATH=tests:. python -m unittest test_assistant_store test_assistant_daemon test_calendar_client test_admin_web test_sqlite_voice_context -v
```

受控 transport／provider／summarizer 驗證合約，不證明 Google 真帳號、Teams admission、Live ASR、遠端語音或一般 Codex 實際摘要。已執行的具體證據見 `../evidence/RESULTS.md`。
