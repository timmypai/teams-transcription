# 本 target Google client 與 Calendar 安全設定

適用 `timmypai/teams-transcription`，交付鏈維持 OPS t_0fb257d3 → 本卡獨立 reviewer → 原 SHIP t_12402f67。**本文是工程入口指南，不是已安裝真 client、OAuth 成功、Google GET 或真人語音收據。** 不使用舊 target 的 client/token/DB，也不建立 Google client。

## 四個階段分開確認

|階段|由誰處理|成功只代表|
|---|---|---|
|安裝既有 Web client|本人安全提供檔案；Bot 走固定 Actions|本部署 private client 檔存在且 redirect 合格；沒有 OAuth／Google GET|
|本人 Google OAuth/MFA|本人經同部署管理頁完成|同一登入 session 的 readonly callback 保存授權；不是讀取成功|
|Calendar 讀取設定|Bot 經正式 `operate calendar-settings`|指定本地設定的讀取開關保存／讀回；不取得錄音或發聲同意|
|實際讀取|Bot 經 `operate calendar-occurrences`|本次啟用 Calendar 的 attempted/succeeded/failed 計數；不等於 Join／遠端聽驗|

ChatGPT/GPT Live `/chatgpt`、管理密碼、Google OAuth、Teams 登入仍是不同授權。Google scope 保持 `calendar.events.readonly`，不為列出帳號所有 Calendar 增加 scope。沒有授權時，不觸發重新授權、不以 Join 作探測。

## 1. SHIP 先交付受審入口

原 SHIP 核對非作者 native exact-head APPROVE、CI、merge tree後，合併本 PR，再手動部署 main 最新已審 SHA；merge 不部署。部署/同 SHA 整合/health/readiness 通過後才進入下面設定。不在 PR head 上執行 self-hosted workflow、不用 runner 任意 shell、不要求本人代做 VM 工作。

## 2. client 的唯一秘密輸入與固定安裝

只有確實缺 client 時，請本人把**本 target 獲准使用的既有 Google Web application client JSON 完整內容**放在此 repo：GitHub → Settings → Secrets and variables → Actions → **Repository secret `TEAMS_GOOGLE_CLIENT_JSON`**。不要貼聊天、PR、run inputs、issue、artifact；不要讀出 secret 值確認。Bot 僅以 `gh secret list -R timmypai/teams-transcription` 檢查名稱。

- `DEVFLOW_APP_ENV` 仍只處理原 `DEVFLOW_ADMIN_PASSWORD`；`DEVFLOW_OPS_ENV` 仍只放原 `ADMIN_PASSWORD`，不得塞 client、Calendar ID 或 token；不要覆蓋已有密碼 secret。
- 只收 JSON 的 `web` 類型。必須有非空 `client_id`、`client_secret`、`redirect_uris`；installed client、token 格式、重複 key、無效 JSON／超限、未登記的有效回呼 URI均失敗。只保存正式 client 實際使用的三個欄位；Google endpoint 不由輸入覆寫。
- 有效 callback 取自本部署 runtime，當前 installed origin 為 `http://127.0.0.1:8080`，對應 **`http://127.0.0.1:8080/google/callback`**。不要把 host `18101` 自動當成 registered callback，也不要改寫 runtime 或放寬 redirect 來使安裝通過。瀏覽器必須透過既有核准 tunnel 使用與 callback 相同的 origin；本機轉接埠若不一致，先由 SHIP 核對既有 tunnel，不新開公開 listener/proxy。
- client 位置固定為 named volume `devflow-teams-transcription-data` 內 `/data/config/google-client.json`。只容許既有有效 `google_client_config_file` 正好指向該位置；不能輸入任意路徑。root/config 必須同執行 UID/GID、0700，client 必須10001:10001／0600。錯 owner/group/mode、symlink、非普通檔案都拒絕，不自動修權限或清除。
- 由已部署 app 內同 UID helper 從 stdin 接收，不放在 argv、環境傳入子程序或暫存 artifact；經 no-follow directory fd、應用 claim lock、paused/idle 檢查、私有暫檔/fsync及不覆蓋發布。沿原 volume 持久保存，不重啟 daemon。
- 相同內容重跑只驗證／讀回既有檔；不同 client 拒絕覆蓋（`google_client_conflict`），不搬移或銷毀既有 OAuth。需要換 client 的行為另行核准，不用本入口暗中輪換。

本人完成 secret 後，Bot 才 dispatch 無輸入的固定 workflow：

```bash
gh workflow run google-client.yml -R timmypai/teams-transcription --ref main
```

`google-client.yml` 先執行 reviewed main-tip admission，之後才提供獨立 secret。adapter 再驗 main/review、部署 SHA、container/image、10001:10001、固定 RW volume、安全限制及健康，才呼叫已安裝的 `install-google-client`。共用原 manual workflow concurrency/host lock。沒有路徑、command、URL 或 target 輸入。

讀回本次 `devflow-evidence-google-client-<sha>-<run>-<attempt>` 的 `receipt.json`／失敗碼。成功結果只有 installed/existing/private/redirect 布林值，**`oauth_verified=false`、`google_read_verified=false` 永遠不代表真 Google 已驗**。失敗不輸出 client／原始 exception／完整程序 stdout/stderr。artifact 不上傳秘密或 `/data`。缺輸入、格式／權限或安裝失敗不應邀本人盲重 OAuth。

## 3. 本人 OAuth/MFA（不是 Bot 的設定操作）

安裝成功後，SHIP 先確認 `/settings` 不再阻擋 client／callback。**本人**經同部署私有入口登入，在「Google 行事曆」按連接並於官方 Google 頁選正確帳號、完成 MFA／原 readonly 同意；同一登入 session 回到 `/google/callback`。本人只回報完成狀態，不轉貼 URL query、code、state、cookie 或 token。Bot 不代登入、不開始 grant、不擴 scope，不借其他部署憑證。

callback 沿既有 private credential 機制寫 `/data/config/google-credentials.json`。UI「授權已保存」是保存結果，不是 Google GET。若 client/app 不屬於本人獲准的 Google 專案，或帳號不被現有測試模式允許，僅本人能處理該專案權限；此卡不建立 client、不發布 OAuth app。

## 4. Bot 讀回後精確設定 Calendar

先以既有 `DEVFLOW_OPS_ENV` 登入，不要求本人重設密碼或操作管理頁：

```bash
devflow operate teams-transcription calendar-settings
```

去敏結果只含 `scheduler_paused` 與各設定的 revision、primary、enabled、既有錄音／發聲旗標及時間窗。不匯出 Calendar ref、帳號、名稱、cookie、CSRF 或登入後截圖。

Bot 依**剛讀回**的主日曆 revision，只調整 Calendar 讀取開關：

```bash
# 本 target 本人帳號的主日曆尚未配置：固定 primary，revision 必須為0。
devflow operate teams-transcription calendar-settings \
  --args '{"calendar_ref":"primary","revision":0,"enabled":true}'
# 已配置者：仍用 primary 公開別名，revision 帶上一次安全讀回的整數。
devflow operate teams-transcription calendar-settings \
  --args "$CALENDAR_SETTINGS_ARGS"
```

`CALENDAR_SETTINGS_ARGS` 由 Bot 以當次讀回的資料組成 JSON：`calendar_ref` 固定為 `"primary"`、`revision` 是整數、`enabled` 是布林值。`--arg` 一律傳字串，不用於整數或布林參數。

`primary` 明確指本人授權帳號的主日曆。公開入口不讀出或接收 Calendar ID／local ref，不使用 CalendarList scope 枚舉日曆。若需要變更非主日曆，交原 orchestrator 補已審私有入口，不索取 ID、不請本人代操作或把值塞進密碼 secret。

操作只走 `/settings` 的同源、已登入、CSRF 表單 `/actions/calendar-readonly`。server 在 daemon Join lock 與 store claim lock下重查 paused/active lease、精確revision；變更期間已啟排程／有會議／授權作業／stale revision 均拒絕。只保存 enabled，既有 identity、錄音／發聲授權及時間窗不變；新 primary 為錄音=false／發聲=false。保存後再次 GET 設定，核對改動及其餘設定不變；未通過不得回報成功。沒有 POST poll、OAuth、Join、binding 或 scheduler，也不自動選會議。相同 ref/revision/讀取值可直接回覆未改動；stale重放不猜新revision。

## 5. 實際 Google 讀取另驗

```bash
devflow operate teams-transcription calendar-occurrences
```

核對當次 run 的 Google configured/authorized/error_code、enabled_calendar_count、read.attempted/succeeded/failed/event_count/completed、ready。`poll_id` 只用於私下驗證讀取新鮮度。零Calendar/零attempted仍是 **未讀取**；client installed或UI ok也不是 Google成功。真 Google成功至少須本部署真授權、啟用Calendar>0、本次全部讀取完成且成功；空 events可以成功，但不能憑空製造會議。hosted fixture 的 mock GET僅測工程投影。

只有實際取回合法場次、同部署前置條件齊全後，才集中取得本人當次新鮮在場、加入、錄音、逐字保存與發聲同意。**主日曆的錄音與發聲旗標維持 false**；Bot 用[單次控制流程](TARGET-CONTROL-OAUTH.zh-TW.md)為選定場次簽發 binding。唯讀 OAuth 不是媒體同意，單次 binding 也不授權整個 Calendar、系列或一般排程。

**Bot**操作日曆與300秒binding，不恢復「本人操作管理頁／轉貼occurrence_id或binding」舊流程。當次通知、單次Join、遠端聽到2與cleanup仍由原SHIP驗，不能因本卡PR/CI完成而宣稱 AC-4/5真人PASS。

## 缺項只問可完成的動作

- 缺 client：本人將既有 Web client JSON放 **`TEAMS_GOOGLE_CLIENT_JSON`**，完成後回覆「完成」；其餘安裝／驗證由Bot做。
- 缺 Google account grant/MFA：本人在同部署 Google 官方流程完成，僅回報完成；Bot不收秘密。
- 缺Calendar啟用：由Bot先讀後設定，不問本人代做；主日曆不需帳號或敏感ID。非主日曆未配置則遵守上面的工程範圍界線。
- 缺有效會議／新鮮同意或人耳回報：只問本人真正持有的時段、在場／媒體同意或聽感；Bot完成其餘正式操作。

本卡測試只用合成client/canary、temporary SQLite、真installed CLI/form及host exporter；Docker/GitHub與Google外部邊界受控。固定 main17074a7 的同入口斷言 RED→GREEN 存 `google-setup-red-green.json`；不以 import error當RED。不讀真秘密、不執行正式安裝/OAuth/Google/Join；所有正式結果由SHIP另留同SHA、run/attempt、去敏收據。
