# Hermes 管理畫面操作

僅執行已審核的 main 最新 commit，且 runner 上安裝的映像必須同 SHA。正式 Join 由 SHIP 負責，不由 `operate.yml` 執行。

- `devflow operate teams-transcription look --arg path=/login`：匿名登入頁，只輸出固定狀態及登入標題截圖。
- `devflow operate teams-transcription calendar-occurrences`：Bot 登入、同步並回報當次 Google 讀取狀態、attempted/succeeded/failed 與可加入場次數。零次讀取不是已連線；可加入只表示符合時間窗，不是媒體同意。
- `devflow operate teams-transcription calendar-settings`：去識別的設定與 revision。限 `calendar_ref: "primary"` 作為公開別名，可在排程暫停時提交唯讀開關；現有 Calendar 必須帶讀回的整數 revision，新增用 `0`。

GitHub 的 run inputs、標題、log、artifact 都是公開面；`args` 僅可放上述公開選項，`request` 由 devflow 產生作為公開關聯。不可填入密碼、連結、occurrence ID、binding 或 session handle。登入使用既有 `DEVFLOW_OPS_ENV` 內的 `ADMIN_PASSWORD`。已登入操作不匯出截圖、trace、HAR 或 reporter 原文。

場次原始結果與 binding 由 [同部署固定 control workflow](TARGET-CONTROL-OAUTH.zh-TW.md#當次本人同意--hermes-單次-join) 在 VM 私下選取／簽發／消費；`issue-binding` 不再是公開 operate 選項。Bot 不下載私有交接、不請本人轉貼。工程 CI 只使用合成會議及一次性帳本。
