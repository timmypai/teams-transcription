# Hermes 管理畫面操作

操作只由已審核的 main 最新 commit 經 VM runner 執行；部署映像 SHA 必須與該 commit 相同。
不改排程開關、不建立會議、不啟動 OAuth、不入會。真人 AC-4 仍須由 SHIP 實際驗收。

## 前置設定

本人在 GitHub repository `timmypai/teams-transcription` 的 Actions secret 設定
`DEVFLOW_OPS_ENV`，內容為單行 `ADMIN_PASSWORD=管理密碼`（不加引號）。
不要在聊天、卡片或操作參數提供密碼。缺少時回報 `missing_ADMIN_PASSWORD`，操作失敗。
Google Calendar 的本人 OAuth 與排會議另行完成，管理登入不能代替 Google 或 GPT Live 授權。

## 唯讀登入頁

```bash
devflow operate teams-transcription look --arg path=/login
```

不需要秘密；只允許 `/login`，回傳狀態、固定頁名與登入標題截圖，不匯出頁面全文。

## 同步與選場次

```bash
devflow operate teams-transcription calendar-occurrences
```

登入後在設定按「更新行事曆」，再開 `/schedule`。同步失敗即拒絕回傳成功；
只回傳尚未結束且可供選擇的 pending 場次，包含 `occurrence_id`、整數 `revision`、`start_at`、`end_at`、`joinable` 及頁面 `timezone`。`joinable` 只代表目前在入會時間窗內，不代表已有媒體同意。
起訖為帶 UTC offset 的 ISO 時間；結果不含標題、姓名、Google Calendar ID 或 Teams URL。
沒有未來場次時回傳空陣列，不自行建立。截圖遮蔽標題與操作欄。

## 簽發短效 binding

先通知本人即將加入、確認本人在場及本次加入／錄音／逐字保存／助理發聲同意，
才能以 JSON 布林旗標傳入該次同意（登入及既有日曆授權均不能推定本人在場）。

```bash
devflow operate teams-transcription issue-binding --args "$BINDING_ARGS"
```

由 Bot 用當次選定場次的 `occurrence_id`、整數 `revision`、原樣 `start_at`／`end_at`，加上布林值 `present: true`、`media: true` 組成 `BINDING_ARGS` JSON。`--arg` 一律傳字串，不能代替這裡的整數與布林值。

透過 `/schedule` 現有表單勾選並送出，不呼叫 Join。結果包含
`occurrence_id`、`revision`、`request_sha256`、`issued_at`、`expires_at`、`expires_in_seconds`。
時間為伺服器 Unix 秒數，效期維持 300 秒；`request_sha256` 是本次控制 binding，
依原有控制入口對應到 Join 的 `request_sha`，不是 OAuth token。
過期、太早、版本／授權失效時操作失敗，結果保留管理頁顯示的固定原因。
這份 binding 只對選定的單場生效；唯讀 Calendar 不必啟用整個日曆的錄音或發聲。一般排程和其他場次不受影響。
截圖只有 binding 頁面標題，不拍 binding 本文；不錄 trace、video、HAR 或自動失敗截圖。

SHIP 取得結果後立即 dispatch 原有 `control.yml` Join，不經使用者轉貼、不走 block/resume，
也不延長有效期或自動重試。操作與 control/deploy 共用既有序列化群組。
SHIP 在正式執行時記錄 `Join dispatch 時間 - issued_at`；本開發卡未入會，沒有實測該耗時。

## 工程驗證範圍

CI 的 `operations.spec.ts` 用安裝後應用、一次性 SQLite 與既有 Calendar constructor DI
跑同一套登入／同步／表單操作，驗證有效簽發及過期／太早拒絕；不開 Google、Teams 或模型。
`ops/` 不參與驗收測試收集。既有九個 EPIC 情境保持不變；新增工程測試不冒稱人工 AC-4 通過。
base 保留原九個情境；新的 schedule metadata 另由 `operate_proof.py` 在固定 base 上驗證兩項斷言紅燈。
