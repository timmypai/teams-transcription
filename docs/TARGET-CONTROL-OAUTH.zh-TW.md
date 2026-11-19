# 本 target 控制、媒體政策與本人 OAuth 前置

適用 `timmypai/teams-transcription`，不是歷史 paulpai0412 部署。本文交付的是待獨立 exact-head review 的工程來源；不是新部署、本人授權、真人入會或遠端聽驗收據。AC-2 manual 與已核准私有入口不變。

## 四層結果，不能互相替代

1. **工程 source/hosted ready**：手動 workflow 的固定選單及參數接到真正 helper/socket/daemon/SQLite，runner 只用 constructor fixture；負面控制及真 main RED→候選 GREEN。這不是真會議。
2. **同部署工程 ready**：SHIP 合併已批准 exact head，僅手動 default-tip deploy，核對 source/image/container/volume，再執行本文件的空 home 準備。管理登入、健康與 metadata 仍不是 ChatGPT OAuth。
3. **本人 OAuth 與當次會議同意**：本人經同部署 `/chatgpt` 明確完成官方裝置登入/MFA；本人另就既有合法場次確認現在在場及本次加入、錄音、逐字保存、助理發聲同意。任何歷史 grant/連結/帳本/舊 DB 都不能代替。
4. **真人驗收**：Hermes 經本 target Actions 單次 Join；同場喚醒「小白小白」再問 1+1，操作人明確回報遠端聽到語音 2；然後撤權/stop/持久 cleanup。ready、request accepted、Join dispatch、文字或本機 PCM 均不是成功聽驗。

## 已核准入口

從 `devflow repo access teams-transcription` 取得私有 tunnel 與使用者網址。此 target 已核准主機 `127.0.0.1:18101` → 容器8080，對應 installed origin `http://127.0.0.1:8080`；不得為 OAuth 另開公開 port、proxy 或複製其他部署的 token。服務維持原 UID/GID10001、named volume `/data` RW、readonly rootfs 與 sandbox。

## 先準備有效空私有 Codex home（SHIP 才執行）

合併/手動部署新版本後，在 GitHub Actions **Manual same-deployment control** 選 `prepare-home`，`id`、`request_sha`、`revision` 全空。CLI 等價：

```bash
gh workflow run control.yml -R timmypai/teams-transcription --ref main -f operation=prepare-home
```

這仍是相同 main-tip/review/CI admission、host lock、固定已安裝 container 與 UID，不是通用 shell。只在本部署 paused、無 active meeting/lease/pending summary/unknown cleanup 時進行。它讀有效 runtime/env 的 `codex_home`，僅在同 data root 下以0700建立缺少的私人目錄；沒有 home 輸入欄。逐層 no-follow descriptors 驗 UID/GID/0700、可 R/W/X；symlink、錯 owner/group、公開/唯讀或 root 外路徑都拒絕，不替現有目錄 chown/chmod，也不清空或列檔。

下載該次 `devflow-evidence-control-<sha>-<run>-<attempt>`，核對 `receipt.json` 的實際 `runtime_path`、`codex_home`、UID/GID、mode（JSON 十進位448=0700、384=0600）、writable、effective hosts/deny-all、source/run，以及原 audit。這是實際執行後才有的 metadata，不預填預設 `/data/codex` 冒充觀測。`credentials_read=false`、`provider_authorization=NOT VERIFIED` 永遠不能當 grant 成功。既有設定與私有 home/憑證內容完全保留。

## 媒體出口：合法 [] 不自動放寬

`/settings` 的「媒體出口政策」顯示目前有效 `allowed_egress_hosts`。預設空白是合法 `[]`，明確拒絕全部 HTTP/WebSocket；不是程式應自行補齊的錯誤。本人確實核准所需目的地後，才在同一登入管理頁輸入每行一個精確小寫 host，勾確認並保存；不接受 URL、wildcard 或 shell。保存只改本部署固定 `config/runtime.yml` 的該欄、原子0600寫入，其他 keys/values、Codex home/帳號/密碼不變；不是改共享 kit/防火牆。無會議、paused、無授權作業/cleanup interlock 才可保存。環境 `ASSISTANT_ALLOWED_EGRESS_HOSTS` 有非空覆寫時拒絕保存，避免檔案成功但未生效；交 SHIP 核對既有受控配置，不擴充任意環境輸入。

可以明確保存空白回到 `[]`。本卡沒有為真人挑選/授予任何 hosts，也不聲稱出口已通。socket Join 必須 target host 在有效 allowlist；B 的 HTTP/WebSocket 還須原目的地檢查，官方 Live 動態媒體另需 own-start SDP proof。這不是 Teams/A/Codex 子程序的全域網路防火牆，也不是實際音訊通過保證。模型/CLI固定版本及 no_new_cost 政策不變，不加 API key/paid fallback。

## 本人 ChatGPT OAuth

工程 ready 後，本人經 `/settings` → `/chatgpt`，依畫面明確確認（已有 grant 則明確 reauthorize），在官方頁面親自完成 device flow/MFA。不將 device code、MFA、cookie、token 貼聊天/log/artifact，不移動其他環境 auth.json。不以管理密碼、Google Calendar readonly、Teams登入或來源審查帳號當 ChatGPT entitlement。

Web 授權沿用自己的 daemon reservation/cleanup，不要求把常駐 daemon 任意停止再跑 CI/TTY登入。`auth-preflight` 只有離線 metadata/版本；`auth-check` 是另外明確 readonly account/read，不是 refresh、Live RX/TX或遠端可聽證明。本 OPS 不啟動上述真 OAuth/account/model 操作。

## 當次本人同意 → Hermes 單次 Join

1. 由本人在同部署管理頁建立或選擇**既有合法、已取得 recording/speaking 授權的 pending occurrence**，其真實時間仍須處於既有 join window；不要借舊 DB、偽造在場時間或把舊 terminal 場次 clone 成新同意。本 target 不開放舊 `prepare`。
2. Hermes 先通知準備單次加入；本人確認現在在場。在 `/schedule` 對該場次勾「我現在已在此會議」與「本次加入、錄音、逐字保存及助理發聲同意」，按「建立五分鐘 Hermes 單次控制 binding（不加入）」。這不新增授權、不 Join，也不啟排程。
3. 畫面提供 `occurrence_id`、`revision`、`request_sha256`。TTL 固定300秒（工程新鮮度政策；不是會議長度）；同時綁 boot/boottime及wall clock，重啟或倒退/超時拒絕。這些 ID/hash 可給 Hermes 作固定控制，但不傳會議URL或任何登入秘密。過期就停止，重新通知/取得本人新鮮確認；沒有自動重試。
4. Hermes 手動 dispatch `control.yml`（只 main 最新已審已部署 SHA），operation=`join`，id=上述 occurrence，revision=上述數字，request_sha=上述64hex。不可猜值、抄別場或拿歷史receipt當批准。固定 helper強制 target-control binding；daemon在provider await前與真正SQLite claim同交易重查，claim寫既有policy事件保證request/occurrence只能使用一次。Calendar unchanged reread保留binding；實際更動或版本不符拒絕。
5. 正常回覆只表示 claim/starting，仍要觀測實際准入、授權媒體及本人遠端聽驗；不以HTTP/UI Join或Hermes自答代替。不要再次 dispatch Join；斷線/不確定先 view該合法occurrence及持久run，unknown留interlock。

## 撤發聲與正常停止

- `output-disable`：id=當次 session、request_sha=同次request，revision空白。只 disable，沒有 enable/grant-speaking。先在同SQLite持久撤回該occurrence speaking，再呼叫原輸出停用；Calendar refresh及monitor不能恢復此拒絕。它不撤回已錄文字，也不代表全部資源已離會。
- `stop`：同session與request，revision空白。沿用原正常stop/輸出停用，不殺外部程序或重啟服務。helper最多等待12秒**cleanup觀測**（不是會議/回答時限）；必須同binding work-finished、cleanup=cleaned、run terminal、capture lease已釋放且無capture_cleanup_unknown/service_interrupted，才 `cleanup_verified=true`。欠proof/timeout/unknown回失敗，不能以stop-return/terminal單獨當PASS。已乾淨結束的同binding重查可冪等成功；錯binding仍拒絕。
- cleanup unknown時保留原capture interlock與paused，不自動清除、重Join、改為cleaned或啟摘要。人工審查另由既有受控程序完成，不改寫原失敗。

所有 real control/OAuth/Join/遠端收音與清理驗收由既有 SHIP t_12402f67 在本卡獨立 review/合併/手動部署後接續；本文及hosted evidence不能關閉AC-4/5真人要求。
