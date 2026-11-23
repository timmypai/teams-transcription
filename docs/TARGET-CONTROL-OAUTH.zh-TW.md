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

合併/手動部署新版本後，在 GitHub Actions **Manual same-deployment control** 選 `prepare-home`。CLI 等價：

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

1. Bot 先完成[本 target 的 Google／Calendar 前置](GOOGLE-CALENDAR-SETUP.zh-TW.md)，用固定 `control.yml` 的 `operation=preview` 在 VM 私下選擇唯一符合入會時間窗的既有 pending 場次。零場、多場或無法確定唯一場次就是本人要測的場次時，不發 Join。預覽不是授權。不可借舊 DB、clone terminal 場次或請本人抄 ID。
2. Hermes 通知準備加入，在同一已準備的操作中確認本人現在就在該場、且明確同意單次加入、錄音、逐字保存與助理發聲，才由同一 GitHub actor dispatch `operation=join-confirmed-present-and-media`。這個固定選項是 Hermes 對上述人類確認的聲明，不是任意 input 或自動推定。只送 `operation`；不帶會議或授權資訊，不 block/resume、不重跑 Actions attempt。
3. Runner 驗證預覽／dispatch 的 300 秒新鮮度與同部署身分，經原管理表單重查同一場次、revision 及起訖，再簽發一次；私下立即透過已安裝 helper 的 stdin 交接。既有 binding TTL、boot fence、SQLite 單次 claim、Calendar 設定版本、暫停狀態與清理 interlock 保持不變。300 秒不是會議時限，不必為單場打開整個 Calendar 的媒體旗標。
4. 去敏 receipt 的 `dispatch_at` 是 helper 寫入 Unix socket 前量得的時間，`issue_to_dispatch_ms` 是同次簽發到送出的耗時。它不是實際入會或遠端聽驗收據。缺值或結果不明時不猜、不重試 Join；僅使用同一控制槽的 `view` 查證。無法證明已清理時保留 interlock，走既有人工清理審查。

## 撤發聲與正常停止

- `view`／`output-disable`／`stop` 均只帶固定 `operation`。Runner 私下核對同 actor、同部署與同一 run 的 binding；不輸出 session handle。`output-disable` 先持久撤回該場發聲，再停用輸出。Calendar refresh 及 monitor 不能恢復此拒絕。
- `stop` 沿用原正常停止，不強殺或重啟。最多等待 12 秒觀測同一 binding 的 durable cleanup：work-finished、cleaned、terminal、capture lease 已釋放、沒有 unknown/interrupted。確認後清除 runner 槽內的會議識別與 binding，只保留結果；重查可冪等成功。沒有證據不算清理成功。
- cleanup unknown時保留原capture interlock與paused，不自動清除、重Join、改為cleaned或啟摘要。人工審查另由既有受控程序完成，不改寫原失敗。

所有 real control/OAuth/Join/遠端收音與清理驗收由既有 SHIP t_12402f67 在本卡獨立 review/合併/手動部署後接續；本文及hosted evidence不能關閉AC-4/5真人要求。
