# GitHub 開發與 Oracle staging 交付契約

## 本 target 控制/OAuth 工程增量（2026-11-19）

[繁中操作指南與證據界線](TARGET-CONTROL-OAUTH.zh-TW.md) 定義本次增量：原 `control.yml` 固定選單擴為 view/join/output-disable/stop/prepare-home，不是舊 target 的 root authority/ledger/prepare；本人在既有 authenticated Origin/CSRF 管理 action 對合法 pending occurrence 確認當次在場及媒體同意，五分鐘 boot/time/revision/request binding 在真正 SQLite claim 同交易消費。未變 Calendar reread 保留 binding，來源更動拒絕；output-disable 持久否決該 occurrence speaking，不能被 Calendar refresh/monitor 重授。stop 必須 work-finished/cleaned/terminal/released lease，不把未知清理當成功。Actions 不提供授權發聲、任意 shell/path/URL 或 seed。

有效 Codex home 現在由新初始化與固定 prepare-home 工程動作建立/驗證；既有部署須部署新已審來源後執行該動作，不能假設舊收據中的 missing home 已修。逐層 no-follow/UID-GID/0700/RWX；不讀列搬token、不grant、不model、不改現有owner/config。metadata現在明列實際有效路徑及egress/deny-all，舊metadata「不輸出路徑」描述不再適用。本人可由 `/settings` 的明確媒體政策表單原子保存固定私有runtime的精確hosts（或空白 []）；不自動放寬，不增加通用Actions設定輸入。下方 view/stop-only、home不建立的描述保留為先前source範圍，不代表新增量的能力。

本卡僅source/hosted：真main08a5c0a helper Join拒絕與home缺失的兩項assertion RED（errors=0）→候選GREEN，另有真CLI/helper/socket/daemon/SQLite控制整鏈、正常及負面控制與真sandboxed owner UI。runner為constructor fixture，不是Teams/model。獨立review→既有t_12402f67 SHIP負責手動default-tip部署/工程readiness/本人OAuth/真人聽驗；merge不部署，AC-2 manual/核准私有tunnel不變，未宣稱真人AC-4/5通過。Lesson: L-0013。

## timmypai 手動交付接入（本輪有效範圍）

本輪 target 僅 `timmypai/teams-transcription`；來源為 `main@c792cf9f313dc1ba2fafe8eb1c811b8e862adc66`。下方 `paulpai0412` 的歷史部署資源、授權、runner、GHCR 與 `/opt/teams-staging` **不屬於本次可用資源**，不得借用。

- 新 `ci.yml` 只在非特權 `pull_request`／手動 `workflow_dispatch` 跑 GitHub-hosted Ubuntu，唯讀 token、不讀真 secrets、不 publish、不上 Oracle、不 Join／grant。checkout exact head，test/check/verify/verify-base 由 `devflow-ci` 聚合。merge 不部署。
- 公開 npm 固定 SHA512 的 `codex_npm` 與當次 Python3.12 公開 wheels 兩個命名 contexts 均實際建置；映像保留原 non-root、readonly、drop ALL、seccomp、tmpfs。回歸在獨有 Docker bridge（提供本機 ICE 所需正常路由，無 published ports、無憑證）的同版 nonroot/readonly/seccomp 映像執行；僅測試 `/tmp` 加 `exec` 以執行測試產生的 fake subprocess，產品 Compose 不變。先前 network-none／internal bridge 的真實 RTC 失敗保留，不以此推論產品音訊根因；只用合成本機 peers，禁止真 Teams／model／grant。只明列排除原移轉缺來源的兩個 bridge suites，不宣稱原始 full discovery。`test_staging_compose` 不跳過：改由 hosted `check` job 實際使用 Docker/Compose CLI 做無 daemon 的 config 檢查，另存 host-compose.json；image shards 明列 routed_to_host_check，聚合必須兩邊成功。Node、sandboxed Chromium 與固定 Codex 均由映像提供；Node Playwright runner 使用固定 Python Playwright 套件內建的相容 Node，不以 Debian Node18 啟動需20以上的 runner。
- `.devflow/hosted_verify.py` 僅 disposable hosted harness：隨機 `teams-transcription-ci-*` 容器、專用 volume、無網路 app、無 published port、無 Docker socket 掛載。建立自己的 foreign sentinel／檔案／SQLite，核對前後 hash、身分、啟動時間及狀態；所有 controller mutations 都經 exact tuple allowlist，混合 foreign plan 在第一個操作前整批拒絕。證據的 `outside_mutations=0` 僅表示該 harness 稽核範圍，**不是 Oracle 或全主機任意檔案未寫入的證明**。
- AC-1～3 的九個 Given/When/Then 從 EPIC `t_a3f4f673` 定稿逐字納入 `.devflow/scenarios.json`。PR 證據標明 disposable hosted scope，與部署證據分離；負例不在實際部署執行。UI 使用真正 sandboxed Chromium／已安裝管理頁；只用隨機一次性測試密碼，沒有真憑證、provider 或會議。trace/video/HAR 關閉，密碼欄清空後才截圖。
- base job 真正建置 PR base 的應用映像，用相同 UI fixtures 核對既有登入行為；CLI adapter 在 base 缺席時回報 INVALID／`Base-unverifiable`，不製造假紅燈。UI 既有行為用 `Already-met`；PR 上的豁免仍需獨立 reviewer 核對，CI 綠不等於豁免已批准或 native review 完成。
- `deploy/isolated_deployment.py oracle-preflight` 仍只屬 hosted harness，exit1／`target_manual_workflow_required`，不能 enable 或拿來部署。真實 source 入口是手動 `deploy.yml` → `.devflow/deploy.sh` → `deploy/target_deploy.py`。共用 devflow user/rootless daemon 信任邊界已接受，不要求專用帳號／獨立 daemon／pre-job gate；namespace 不等於權限隔離。
- 本 PR 正式提供本 target 手動部署／產品 build／private runtime／SQLite fence／精確 release rollback 與固定控制 source；僅 source 與 hosted disposable 證據，不宣稱 Oracle 成功。原 SHIP t_3b14e30d 在獨立 review／merge 後擁有 Oracle 安裝、實際歸屬／18101／health／稽核／安全入口與使用者登入驗收；本 OPS 不 dispatch 部署或執行 VM。
- 固定控制鏈 source：Hermes 已授權 Actions → 本 target `control.yml`／helper → `docker exec` 以同部署 UID10001 呼叫 `/data/.teams-control/control.sock` → daemon。只允許既有已授權 occurrence 的 `view` 與既有 acceptance binding 的 `stop`，沒有 shell/path/URL、prepare、Join 或授權輸入。親讀 same-UID／0700 parent／0600 socket；stop-return 後須同 binding 的持久 terminal/cleanup receipt，不明即失敗。原生 view 需要真實已授權 occurrence；SHIP 在可運行階段無 Join 探測，不虛構 occurrence／借 DB 或 ledger。真 Join／媒體同意／本人 OAuth/MFA 留 t_12402f67。
- 使用者 2026-11-18 決策正式取代 Cloud-only：非作者 Hermes reviewer 審完整 diff／政策／最終 exact head，自己既有 GitHub 登入原生 review、讀回身分／commit_id／連結／具體結論並逐條判斷 base 例外；CI 綠、無 findings、提交審查不是批准。只讀 shadow 永不授權自動合併；不要求 Cloud grant／額度／App ID。詳見 [有效政策](PR-AUTOMATION.md)。

### 本 target 的手動 source 契約

`deploy.yml`／`control.yml`／`readiness.yml` 只有 workflow_dispatch；main 最新 commit 才執行。readonly contents/actions/pull-requests/checks token，checkout exact SHA、不保留 git credential。`target_admission.py` 在秘密與私有寫入之前向 GitHub 核對最新 main、merged PR、指定非作者 Hermes reviewer（`login=paulpai0412` 且 immutable numeric `user.id=273007156` 且 `type=User`）原生 APPROVED 的最終 head、merge tree=reviewed tree、零 unresolved threads 及該 head 最新 `ci.yml` PR run 的 repository/workflow id/path/run/attempt/event/SHA/success。長 build 後再讀一次。它不會合併，不接受作者 JSON/marker；full diff/scope/base 判斷屬 reviewer，而非文字 parse 假裝自動批准。新 head 必須重 CI/review；若 main 期間有新合併，停止並重派最新版，不重跑舊部署降版。

沿 devflow 範本的 build→up→health→readonly smoke→確切前 release 回滾順序，因產品有兩個 public-only contexts／SQLite／sandbox 改以固定 Python adapter 實作，未更動共享 kit。`prepare-contexts.sh` 只在自己 disposable build container 下載固定 SHA512 Codex npm／公開 Python3.12 wheels；本機 app wheel只供依賴解析，產品仍在 Dockerfile `--network=none` 從當次 source 安裝。支援原生 amd64/arm64；無 GHCR/packages-write／host pip 安裝，不帶 data/OAuth。image ID、OCI source/revision、GitHub source/run/attempt 寫入私有 release receipt；不是 registry digest 簽章。

固定資源清單：`devflow-teams-transcription-app`、volume `devflow-teams-transcription-data`、network `devflow-teams-transcription`、`io.devflow.owner=timmypai/teams-transcription`、僅127.0.0.1:18101→8080、該次命名 helper/build container、自有 image、`~/.devflow-live/timmypai-teams-transcription`（0700）內 state／release／public build contexts／audit。主機 rootless socket 由同 UID `/run/user/<uid>/docker.sock` 讀取，不借舊 staging socket。先比對 manifest/label/實際 container ID/image/port；外來同名 volume/network/container、18101碰撞、遺留helper、unknown switching 均拒絕，不領養／刪除。每個 adapter mutator 有 exact tuple allowlist／attempted/succeeded/failed 記錄；外來 plan 整批拒絕。快照只讀非本專案 Docker service 的 ID/image/start/status/health，前後需相等；不掛／操作外來資料。`outside_mutations=0` 是這個固定執行器操作範圍，**不是 syscall／全主機任意寫入證明**，共用 UID/daemon 仍是受信任邊界。

`DEVFLOW_APP_ENV` secret 在首次初始化僅接受一行 `DEVFLOW_ADMIN_PASSWORD=<16..1024字元密碼>`，不 echo／放 argv／artifact；不接受 arbitrary dotenv 來改 image/mount/command。沒有預設密碼。以產品 UID10001 在自己 named volume 初始化0700/0600 runtime、scrypt密碼/session及SQLite；排程暫停、allowed_egress_hosts=[]、持久`/data/codex`空白，不碰模型／OAuth／現有帳號。既有配置/密碼絕不覆寫，部分初始化需查核，不從舊環境拷貝。rootless volume 採映像`/data` UID10001 copy-up，不用錯誤的host UID bind推測。

更新前以實際 `.meeting-store.lock` 與 claim fence 檢查 paused／無active meeting/lease/pending summary／unknown cleanup interlock，再持久 `.deployment-maintenance` 擋新claim、停止自己service、重查並SQLite backup API做0600私有備份。未通過不得停止舊服務。up/health/smoke失敗先存 bounded inspect state（不raw logs/env），停止候選，僅在 schema/runtime config hash相同且 quiescent 時，用前 healthy **immutable image ID + exact compose/config.env/seccomp** 回滾；不使用live tag、不移除其他服務、不restore資料。成功後最後才解除maintenance、原子更新healthy state。build失敗不碰執行中服務；首次失敗無前版則保留switching/maintenance待查。SIGKILL、stop或maintenance解除不確定時 fail closed，不把未知清理改成成功／無限重試。重跑已healthy同SHA只讀health，不重建或重啟；不同run/attempt/nonce不覆寫舊release。release/public contexts/私有備份不自動prune，磁碟保留由SHIP查核，不動共享儲存。

產品保持nonroot/readonly/drop ALL/no-new-privileges/seccomp/tmpfs、4GB/2CPU/512pids、取消摘要及lease/cleanup gate。smoke只GET `/healthz`／匿名`/login`，不登入、不seed、不Join、不model。hosted adapter probe實際跑public-context build、同名volume/port拒絕、private init/health、壞candidate→exact rollback、資料保留及foreign service/file/SQLite sentinel；這不是Oracle rootless/cgroup/真登入驗收。Unix+daemon synthetic regression另驗peer UID、授權、wrong binding、stop-return≠cleanup及精確terminal proof，沒有provider。

使用者 2026-11-19 已核准標準私有 SSH tunnel → VM `127.0.0.1:18101`，並接受 host-loopback smoke 等同該入口畫面；不再要求另一個入口／HTTPS proxy。對應 installed origin 固定為 `http://127.0.0.1:8080`，部署 consumer 核對同一 container/image/StartedAt 及精確 `127.0.0.1:18101:8080` 映射，不接受 caller URL／任意 approved boolean。實際用browser看到login、完成管理登入、看到管理頁、關分頁後health仍分層驗證；本輪 source/hosted 結果不是 Oracle 實證。若沿用非loopback HTTPS 則仍必須是既有 public_base_url/secure cookie 設定；不自行暴露或改操作者 runtime。管理密碼登入與`/chatgpt`本人OAuth/MFA、Google、Teams授權分開。初始空Codex home/deny-all只代表可安全啟動管理，不是Live已可用。

### 本 target 的安全失敗證據（schema 1）

既有 `deploy.yml` → `.devflow/deploy.sh` → `target_deploy.py` 的固定入口不變；沒有新 workflow、host shell/URL/path/args、權限、帳號或自動部署。失敗在同一 `evidence-deploy-<run>-<attempt>/failure.json` 與 stdout 只輸出 `schema_version=1`、`repository/source_sha/run_id/run_attempt/action`、`primary_failure`、最多8筆 `secondary_failures`、`cleanup`。不能取得或驗證的身分欄為 null。primary/secondary 只有固定 `phase/code`、有界 `exit_status`（-255..255，未知/timeout/有界中止為 null）與 public output 數值投影。phase enum：`prepare_contexts/image_build/audit/cleanup/unknown`；code enum：`command_failed/command_timeout/output_limit/command_start_failed/audit_failed/snapshot_failed/cleanup_failed/evidence_write_failed/unknown`，另保留既有 `target_runtime_rejected:<SAFE_FAILURE_CODES>` 精確碼。未知永遠為 unknown，不解析例外文字或猜測 ARM、wheel、buildx、OOM 根因。schema 只診斷操作邊界；Oracle run 44320770971 的真正 build 根因仍未知。

公共 contexts/image build 的 stdout/stderr 也不可信：二進位 pipe + selector 每次最多8192 bytes，兩流共用8388608 bytes讀取預算，超過即有界中止；最多多讀一個8192-byte chunk判斷 overflow，不保存整行或raw檔。line計數飽和1024、單行長度飽和2048 bytes、事件上限0（**所有文字直接丟棄，不做raw、遮字fallback、tail或事件文字解析**）；行計數/長度截斷不阻止持續drain，總byte/時間界限仍生效。非UTF8、控制碼、秘密/PII/URL均不解碼、不持久化。prepare/image各維持原900/1800秒deadline，有界中止殺本機process group並最多等2秒reap；不能以Docker CLI退出當作daemon builder已清理，失敗時 `cleanup=unknown`，不盲retry/prune/remove。成功public命令仍繼續原immutable image/revision驗證，未知文字不會被誤判成build失敗。不新增private runtime/browser/OAuth/log/env採集，既有runtime安全碼及固定JSON consumer維持。

原始失敗先記錄；snapshot/audit或candidate capture/stop/rollback的第二次失敗不能覆蓋它。candidate採證失敗仍嘗試固定owned candidate stop；stop未知就不回滾、不解除maintenance。只有原有schema/config/quiescent fence、前release immutable image/compose/seccomp及health完整通過才可記 `cleanup=rollback_verified`；其他失敗保留unknown（無需清理的前置拒絕為not_required），失敗run仍nonzero、不輸出本次healthy receipt。原audit scope文字及限制不變，沒有擴稱全host稽核。

`python .devflow/diagnostics_proof.py` 在hosted check實跑同一CLI/seam測試：immutable failed `da251b1f4fb95620271191f0bdc9d9f71b96bde3` 六項真正When/Then assertion RED（非import error）→head GREEN；真disposable subprocess失敗/timeout/超量、二次audit失敗與完整payload/stdout/stderr負例。僅寫固定數值的 `diagnostics-red-green.json`，與九個原EPIC情境/27步及base豁免分層，不能當Oracle AC通過。Lesson: L-0006。獨立新head reviewer仍須逐條判斷Already-met/Base-unverifiable，不借舊PR批准；Oracle診斷取證/重試路由仍由原SHIP在審核合併後決定。

### 已審 main 才能執行的無 Join 驗證入口

`readiness.yml` 是無任何 caller inputs 的手動入口；與 deploy/control 共用 concurrency 與 host lock。先執行相同 `target_admission.py` main-tip/exact-head 原生 review/CI/tree/snapshot 核對，再向固定 deployed container 的已安裝 helper 執行 `verify`。不從 kit 臨時拷 code、不執行 PR branch、不部署／重啟服務；只在已核對 source SHA/image/container ID/UID10001/StartAt 的服務內啟動短命 sandboxed Chromium。loopback 登入→七日週行事曆→設定→關閉頁面/context/browser→再次health；不讀或提交任何設定表單，不改 public_base_url/cookie_secure 繞 gate。固定同源路由 allowlist 禁其他網頁與非登入 mutation。沒有 trace/video/HAR/storage_state、raw DOM/console/exception、截圖或私人 log artifact；只輸出 boolean、來源/container/image/run 收據。登入本身會產生正常的管理 audit/session，不是零資料寫入宣稱；run/claim/authorization/排程資料需前後相同。

無 Join readiness 在已有 socket 對固定 `{}`（無 operation、occurrence/session id）傳送，檢查 parent精確0700/socket0600、目前UID ownership、SO_PEERCRED same UID 與該 kernel peer 的固定已安裝 daemon argv/executable/UID／init-child lineage（不把 Compose `init:true` 的 PID1 init 當成 daemon）；只接受既有 wire 的精確 `teams_local_control_rejected` 回覆。連線錯誤、EOF、timeout、未知／成功回覆都失敗，不能當預期拒絕成功。以 mode=ro/query_only 一致讀取 paused／無active工作，逐一hash固定控制權威表（settings/calendars/series/occurrences/runs/leases，值不出記憶體），前後相同；另從既有登入API核對daemon started/idle、不closing。只標 `transport-and-rejection-readiness`；正向 authorized view 與 bound stop/cleanup 仍為 **NOT EXECUTED**，沒有真 occurrence 不虛構／seed／prepare/join/stop/grant，也不碰 model/OAuth/ledger。

管理密碼由操作者在 repository **Settings → Secrets and variables → Actions → New repository secret** 建立 `DEVFLOW_APP_ENV`，內容僅 `DEVFLOW_ADMIN_PASSWORD=<16..1024字元本人保管密碼>` 一行。SHIP 是 secret metadata 查核 owner；不要將值交給聊天、source、卡片、截圖、log或artifact。固定consumer只由 Actions env→host記憶體→`docker exec -i` stdin→短命consumer讀取，不放argv或app持久env；缺值／未知key／多行／不合長度拒絕，既有config/管理密碼絕不覆寫。source/hosted驗證不等待操作者secret；hosted使用一次性synthetic密碼，實跑真正daemon/socket/browser與缺secret/錯密碼負例，不是Oracle收據。一般 loopback 自動登入不單獨證明使用者可達；本 target 使用者 2026-11-19 已接受上述固定私有 tunnel／host-loopback 畫面等價，原SHIP依該決定核對固定映射及同部署，不再要求另一個安全入口，不能新開公網／共享proxy。管理登入不是ChatGPT授權，OAuth/MFA/真Join仍留真人lane。

- `target_smoke.py` 於同一正式 deploy invocation／原 host lock 內執行原 `AC-1.S3`、`AC-3.S3`，保存當次 audit 後執行 `AC-2.S3`；只用 `.devflow/acceptance.py` 的原事件／report 格式，步驟逐字讀原 `scenarios.json`，不改 checker、不回填舊 run、不在 Oracle 執行 hosted verifier／S2。當次 GitHub repo/workflow/run/attempt/main/SHA 由 API 核對；健康步驟另核對當下 state、container/image/OCI source/revision 與前後 StartedAt。artifact 附 `acceptance.json`、每步固定數值／boolean log、`smoke-identity.json` 及真正 consumer source；映像仍是 local immutable ID，不冒稱 registry digest。
- `AC-3.S3` 檢查**既有操作者私有 runtime**：非loopback HTTPS 仍要求 secure cookie；唯一私有例外是預設／精確固定 installed HTTP origin、port8080/listener0.0.0.0、cookie_secure=false，對應上述已核准 tunnel。部署 Given/Then 均重查 installed identity 與 host-loopback 映射；未知模式／錯port／公開HTTP／來源變動仍fail closed。不建立或改入口，不會因 `b'password'` 或內部 readiness 通過而轉 PASS。短命匿名 sandboxed browser 只 GET 該 origin 的 `/login`、固定 `/assets/calendar.css`、未登入 `/api/status`，檢查真登入控件／401／無管理頁。cookie 的 Secure 依實際模式檢查，兩種模式都保留 HttpOnly、SameSite=Lax、精確domain/path；cookie只留記憶體，另向固定 installed loopback `/login` 核對同一 in-memory session reuse，拒絕foreign/stale session。不讀 session key、不輸出 cookie、不登入、不修改授權。當次 evidence 以 `access_mode=approved-private-ssh-tunnel` 或 `configured-https` 如實記錄，不以HTTPS名稱冒充私有HTTP；SSH tunnel本身不由consumer建立／探測。Oracle 真跑與使用者登入仍由原SHIP負責；不再問已決定的入口、不要求新建公網／proxy。歷史FAIL／not-run不改寫。
- 健康切換／maintenance 已完成但入口前提未驗，保留原 healthy state 並讓 acceptance fail，不對健康服務盲目重啟或回滾；原 up／health 失敗的精確 release rollback 完全保留。重跑同 SHA 仍不 rebuild/restart，但重新執行當次真 smoke，不能以舊成功 receipt 代替。
- `readiness` 另從同一已安裝 helper 取固定 runtime/codex-home 存在／UID-GID/mode／effective write access／位於 `/data` 的 metadata，並沿用既有 container inspect 的 named-volume RW 旗標；不列檔、不讀 OAuth/token、不搬家／初始化 Codex home。home 不存在為未驗，不推定登入；provider authorization 始終 `NOT VERIFIED`。這些 metadata 不替代本人 OAuth 或 authorized view/bound stop-cleanup。
- hosted `repair_proof.py` 對固定失敗 main 執行既有 seam：六個 consumer assertion RED→GREEN，及受控前置阻塞造成 sdp_apply 未進入的原測試 RED→事件同步 fixture GREEN；另保留無注入的整體 deadline 前置逾時控制。不改產品 40ms／production deadline，不冒稱已唯一重建舊 runner 排程。Lesson: L-0007、L-0008。
- 同一 hosted proof 另跑 `private_smoke_proof.py`：從修正前 main `51e7ee1a06e455f114581df6d5ce68da442f6ddc` 讀取真正 runtime/consumer，核准模式的 entrypoint／transport evidence 兩項 assertion RED（errors=0）→head GREEN；新增固定HTTP真browser、同部署session與錯origin/port/cookie/越界GET負控制。報告 `private-smoke-red-green.json` 與 EPIC acceptance 分層；不是 Oracle tunnel/OAuth/真人證據。Lesson: L-0012；L-0011 明確區分HTTPS與核准私有模式。

- 撤權測試保留原 sender ended/disabled、RTP 不再增加、全段尾音 <1000ms 與末200ms RMS <.001 的所有門檻。hosted run44352585609 的真 550Hz-only/no-marker 控制取得零交越1250/950Hz假命中；固定去敏scalar fixture綁 artifact14450285408/digest，原失敗版 predicate 重播 RED，而同窗1100Hz coherent RMS只 .01554/.01001。fixture改用同一480-sample窗的標記音component，保留 .06 RMS 門檻，另真下游1100Hz正控制／550Hz從未送marker的多相位負控制；沒有改production音訊或時限，不推定真人遠端洩漏，也不稱歷史根因已唯一重建。Lesson: L-0009。

以下保留原來源歷史契約，**已被上面本target決策取代，非本次操作授權或Cloud-only前提**。


本次核准規格：`paulpai0412/teams-transcription`，從 `main@bf0a108237608e6ee5cea3e93e08e9b3105f38ad` 建立隔離分支；GitHub Actions build Docker → 私有 GHCR → Oracle VM pull 固定 digest。Oracle Ubuntu 24.04 ARM64 A1（2 OCPU／12 GB／200 GB）是 staging；production 保留原 Unraid 方向，本次不部署、不變更 Unraid。Cloud Codex 不直接 SSH Oracle、不建立 Tailscale、不開公網 port。VM bootstrap／帳號／runner 註冊由另一項已核准工作處理，此分支只提供待審查 source。

## Related 正式追蹤票與待驗收範圍

2026-10-29 透過 GitHub connector 讀回下列七張票；當時皆為 open。本分支的實作對應 **Related #1、Related #2、Related #3、Related #4、Related #7** 與 repo-local skills adapter，沒有修改遠端 issue／PR／Projects 或關票。以下「已有」只描述本地 source 與本地測試，不代表 GitHub Actions 或 Oracle 已實跑。

| 正式票 | 本分支對應 | 尚缺的驗收證據／功能 |
| --- | --- | --- |
| [Related #1：ARM64 image](https://github.com/paulpai0412/teams-transcription/issues/1) | native ARM64 build workflow、固定 base／Codex artifacts、Python wheel contexts、同一 image 測試後 optional publish、digest／SHA artifact | image 內容與metadata敏感模式檢查已實作並在本地x64驗證；原生ARM64 Actions／runtime與實際發布尚未執行，x64不能代替 |
| [Related #2：offline CI／release gate](https://github.com/paulpai0412/teams-transcription/issues/2) | 保留原 build／CLI／contracts、受控 offline subset、初始僅手動 hosted runner、固定 action SHA、timeout、summary／artifacts；image smoke 失敗時不進入 publish step | 實際 Actions 的 passed／failed gate 收據與同一 SHA／digest 關聯待跑；已實作成功Actions run／repo／workflow／source SHA／artifact SHA256／published digest的機器驗證gate，pull後再比對label SHA；合成成功／失敗／fork／竄改反例已驗證，實際GitHub API與ARM64 run仍待執行 |
| [Related #3：pull deploy／rollback](https://github.com/paulpai0412/teams-transcription/issues/3) | 專用 repo rootless runner／job token 契約、固定 digest、concurrency、maintenance claims、health／sandbox／cgroup preflight、SQLite backup／schema fence、保留 healthy rollback 目標 | workflow dispatch、registry pull、實際切換／健康檢查／回滾／私有資料持久性收據未跑；runner 已註冊或 Docker 已安裝不等於已部署 |
| [Related #4：bounded diagnostics](https://github.com/paulpai0412/teams-transcription/issues/4) | 固定staging metadata allowlist、唯讀SQLite flags、15分鐘／100行／64KiB近期日誌安全事件投影、numeric資源摘要與source／build／deploy／diagnostic run關聯；完整報告合成secret／PII測試 | source及合成敏感樣本測試已完成，未知行整行丟棄，只保留固定事件code與timestamp；實際Oracle日誌／權限／資源／source關聯讀回仍未執行，不依賴GitHub masking |
| [#5：Oracle runtime 隔離驗收](https://github.com/paulpai0412/teams-transcription/issues/5) | 本分支提供待執行工具與配置契約 | 另一任務核實 VM／runner 後，仍須在 Oracle 真實環境驗證 image／UI／sandbox／持久性／資源限制；本地 x64 證據不構成此票完成。本人首次Codex ChatGPT grant仍是#7的實際執行阻塞；admin／Google登入不能替代 |
| [#6：真人 Teams 音訊驗收](https://github.com/paulpai0412/teams-transcription/issues/6) | 保留原產品與當次授權／在場規則，明列 synthetic vs real E2E 邊界 | 本次未入會、未呼叫模型、未真人發聲驗收。只有取得新鮮授權、實際收據並經 review 才能結案 |
| [Related #7：部署前置與本人OAuth](https://github.com/paulpai0412/teams-transcription/issues/7) | 單一prerequisites文件、auth-preflight／受限device-login／readonly auth-check、無日誌無ports的onboarding Compose、合成auth persistence | 本人新grant、refresh／撤銷／過期、實際Live RX/TX尚未執行；沒有Oracle可用登入URL；#5無憑證preflight可獨立先做 |

父任務另回報 VM 已核實 rootless Docker `29.9.2`／Compose `5.6.1`、`teams-staging` UID `1002` 與 `/run/user/1002/docker.sock`；本 Cloud task 沒有 SSH 或自行重驗，runner 註冊仍進行中。這是另一任務的前置資訊，不是部署／#5 驗收證據。控制器依執行 UID 導出 socket，不把主機 UID 1002 誤當成容器 UID 10001。

PR／交付描述使用普通 `Related` 連結，不能設自動關票關係。**PR merge 不完成 #1–7 的驗收，也不自動關閉 #5／#6。** 後續只在相應驗收證據齊備與關票動作已獲授權時，明確記錄結果再結案。

## 流程與證據

| 流程 | 觸發／runner | 權限與結果 |
| --- | --- | --- |
| `meeting-functional-validation.yml` | 僅手動；GitHub x64 | `contents:read`；建 wheel、CLI、controlled subset，JSON／log artifact 與 Job Summary |
| `arm64-image.yml` | 僅 main 手動；GitHub `ubuntu-24.04-arm` | `contents:read, packages:write`；native ARM64 build、架構、Codex、Compose、sandbox／登入 UI／SQLite synthetic smoke；`publish=false` 預設不推送 |
| `oracle-staging.yml` | 僅 main 手動 deploy／rollback；專用 VM | `packages:read, actions:read`；固定 host controller；digest、health、維護／SQLite preflight、備份、allowlist report |
| `oracle-diagnostics.yml` | 僅 main 手動；專用 VM | 無 repo／package 權限；固定 controller；只讀 bounded safe-event／resource／source-run artifact |

最初四個 workflow 均僅接受 `workflow_dispatch`，沒有 push／pull_request 或其他自動觸發；push／開 Draft PR 不啟動 runner。未來新增自動 CI 觸發前另行確認費用與核准。repo variable `ENABLE_HOSTED_CI=true` 才啟用 hosted CI／build，`STAGING_READY=true` 才啟用 VM jobs。這兩個變數啟用前應保持未設定／false；本次讀取 variables 遭 HTTP 403，未重試或變更 token，不能宣稱已核實其值。手動觸發政策不依賴其值；先核實 private repo 的 included minutes、storage、ARM runner 可用性與 spending controls，不授權額外費用。沒有執行 Actions，不表示 CI 已通過；gate skip 也不算 validation success。

ARM64 的 Codex launcher＋binary 都從官方 npm registry 使用正常 TLS 下載，SHA512 固定、graph／archive 驗證，然後 `--network=none` offline npm 安裝。`--platform linux-arm64` 不能誤用 x64 bundle。Python wheels 必須從同一 native ARM64／Python 3.12 工作環境重新取得，不能把歷史 x64 closure 當 ARM lock。base digest 已包含 ARM64 manifest，但套件可用性、image build、Chromium sandbox、rootless cgroup 與音訊需分層實測；apt 未鎖完整 closure。

build 成功後先對 immutable local image ID 跑 `compose_smoke.py`，另跑 `image_content_check.py` 檢查應用路徑、metadata與root auth檔案（root-only metadata用無網路／無mount／drop ALL的單獨owner探針；app保持非root），全部成功才可將**同一個 local image**推至 GHCR（不重新建置），tag 包含 run id／attempt。Job Summary／`repo-digest.txt` 記錄 registry digest 與 source SHA。部署輸入只接受 `sha256:` 加 64 個小寫 hex，registry／package 固定在控制器，不接受 tag、任意 registry、shell、路徑或額外 Docker 參數。source／revision／maintenance labels 只是一致性檢查；新增 `release_gate.py` 透過固定GitHub API核對 `.github/workflows/arm64-image.yml` 的成功main手動run、repo與head repo、source SHA、run attempt、artifact ZIP SHA256與`published-image.json`內digest／四項validation。missing／expired／ambiguous／tampered artifact皆拒絕，storage redirect不帶GitHub Authorization，只允許固定HTTPS artifact storage hosts。deploy輸入另需40hex source_sha與成功build_run_id；不是只信labels。package寫入與受保護main workflow仍是信任邊界，這不是OIDC簽章attestation。

artifact只留7天；已驗過且曾healthy的rollback receipt保存在私有state，rollback重新核對其原成功run attempt，不因artifact過期重新接受任意image。run不存在／不成功仍拒絕。這依賴受信任的host history，不是從過期artifact重新取得證明；summary 的 job result 及 JSON counts 應連同 artifact 一起判讀。失敗前尚未產生的報告會明確缺少，不冒稱完成。真正 Teams admission、Google 現場授權、GPT Live 內容、B 遠端可聽／音訊 E2E 不由 build／health／fixture 成功推論。真實測試仍需當次有效授權與在場確認。

## VM 任務的最小配置契約

| 項目 | 約定 |
| --- | --- |
| 帳號 | `teams-staging`，非 root，無 sudo、不加入 root Docker group；只擁有 staging rootless daemon 與其工作資料 |
| labels | `self-hosted`, `Linux`, `ARM64`, `teams-transcription-staging`；只註冊此私人 repo |
| rootless socket | `/run/user/<teams-staging UID>/docker.sock`，由同 UID 擁有；`docker info` 必須含 `name=rootless` |
| runtime | `/usr/bin/python3`（Ubuntu Python ≥3.11）、`/usr/bin/docker`、Compose v2（支援 `up --wait`）；不在 VM 裝 build 工具或跑 PR tests |
| host controller | 將本分支 `deploy/stagingctl.py` 審查後安裝成 `/opt/teams-staging/bin/stagingctl`（root:root 0755）；不從 runner checkout 執行；`release_gate.py`／`diagnostic_projection.py`同時安裝於該bin目錄，root:root、不可group／other write |
| 固定設定 | `deploy/compose.staging.yml` → `/opt/teams-staging/compose/compose.yml`；現有 `docker/chromium-seccomp.json` → 同目錄；`runtime.env` 為 private deployment 的非 secret allowlisted環境設定；三個檔案 root 擁有、不可 group／other write |
| 目錄 | `/opt`、`/opt/teams-staging`、其 `bin`／`compose` 均 root 擁有、不可讓 runner 替換；`/srv/teams-staging/state` 為 runner 擁有 0700，父目錄管理員擁有；`deployment.json`／`image.env` 由控制器原子寫入 |
| 私有資料 | external rootless named volume `teams-staging-data` → `/data`；容器 UID/GID `10001:10001`。勿直接用 runner-owned 0700 bind directory，rootless subordinate UID 不等於主機 UID |
| 啟動前置 | 管理員在該**同一 rootless daemon**明確建立 volume、用受測 image 初始化私有 runtime.yml／管理密碼／session key；排程暫停。不得放預設密碼或從其他部署拷 OAuth。若有 Codex OAuth，只能同一部署授權的私有 `/data` 路徑，不是 build context |
| 管理入口 | 僅 `127.0.0.1:18081`，不新增公網 port／網路／tunnel。遠端 HTTPS 管理若另有需要須獨立安排；本次不配置 |
| resources | app 4 GB RAM／1 GB shm／2 CPU／512 pids；rootless systemd user service 必須有 cgroup v2 delegation；控制器核對 daemon 的 v2／systemd，sandbox 容器實讀 cpu.max／memory.max／pids.max 驗證 2 CPU／4 GB／512 pids 實際生效，不能默默忽略 limits |

映像的非 root、readonly rootfs、drop ALL、no-new-privileges、原 Chromium seccomp、私有 `/data`、tmpfs 不放寬。部署在停止舊服務前，先用無網路、無私有 volume 的一次性容器驗證 VM 的實際 sandbox、Codex 版本與 ARM64 執行。若 Ubuntu userns／AppArmor／rootless／Chromium 限制失敗，停止調查，不加 privileged、CAP_SYS_CHROOT、`--no-sandbox`、host IPC 或 unconfined。

## Runner 與 GitHub 保護

labels 與 workflow 的 `if` 只是路由／防誤操作，不能阻止有寫入權的人另建 workflow。管理員另將 `deploy/runner-job-started.sh` 安裝為 `/opt/teams-staging/bin/runner-job-started.sh`（root:root 0755），設定 runner service 的 `ACTIONS_RUNNER_HOOK_JOB_STARTED` 指向該路徑並重啟 runner。hook 僅接受此 repo 的 `workflow_dispatch`、`refs/heads/main` 與上述兩個 VM workflow 的精確 `GITHUB_WORKFLOW_REF`，其餘在執行步驟前失敗；不要由 repo checkout 指定 hook。初次 enable 前，以非 main／PR 的**受控拒絕測試**驗證 hook 確實生效，不僅靠 labels。

此 hook 不是對已遭入侵的同 UID workflow 的隔離沙箱。獲准 main workflow 仍可操作此 rootless daemon 與 staging 私有資料；私有 repo、最少 write collaborators、受保護 main／workflow review，以及未受信任 PR 不進 VM，是必要信任邊界。若帳戶方案支援 runner group workflow allowlist，再加嚴限制，不為此升級方案或新增費用。

GitHub Environments：`image-build`、`staging` 限 main；可用時設 required reviewer／prevent self-review。private repo 的 required reviewers 與 protection rule 能力依方案，必須實際查證；若需要的核准保護不可用，維持 `STAGING_READY=false` 並回報，不宣稱已有強制核准。production 可先記錄規格，這次不建立可部署 production job。

GHCR publish 使用 build job 的 `GITHUB_TOKEN packages:write`，不是 PAT；確保 package 維持 private 並連結 repo。deploy／rollback 使用各自 job 的 `GITHUB_TOKEN packages:read, actions:read`，package 設定必須授予此 repo Actions read access。token 由 stdin 交給控制器，讀取上限 16 KiB，寫入該 job 的 0700 `/srv/teams-staging/state/registry/<GITHUB_RUN_ID>-<GITHUB_RUN_ATTEMPT>` 作 `DOCKER_CONFIG`；Python context 與 SIGTERM／SIGINT handling 清除，workflow 另以 always step 清除此 job 的固定目錄；不傳進 app、不寫 artifact／CLI args、不保存長期 PAT。diagnostics 不需要 GHCR token，使用已有本地 image。SIGKILL／VM crash 不能由程序 trap 保證清理；重新啟用 runner 前需檢查並清除已中止 job 的 credential 目錄，不重用遺留目錄，job token 也由 GitHub 的工作生命週期限制。

## 部署、回滾與診斷

1. 手動 build／validate；需要 publish 時明確選 `publish=true`，保留成功 run 與 digest。
2. 操作者在 staging 管理頁暫停排程、確認沒有活動會議／capture 或 summary lease／待摘要工作，完成必要 cleanup review。deploy 不會自動停止真實會議、代操作授權或清空 interlock。
3. 手動 deploy digest＋source_sha＋build_run_id，先驗成功Actions證據鏈才login／pull；GitHub concurrency 與 host 非阻塞 flock 共同避免重疊。先 pull／驗 labels／執行 sandbox，再在與 app claims 共用的 `.meeting-store.lock` 內先核實 SQLite quiescence，成立才設定 `.deployment-maintenance` marker（若已忙碌，不新增 marker、不停止舊服務）；app 的 `claim_run`／`claim_summary` 在同一 lock 下拒絕新工作，避免檢查與 stop 之間的 Join 競態。
4. 再停舊服務、重查狀態、用 SQLite backup API 建立 0600 私有 `deployment-backups/<UUID>.sqlite3` 並 integrity_check；不輸出內容。原子記錄切換狀態／current 與 previous digest，`up --wait` 健康檢查，在同一 store lock 內重查 paused／idle 才清 maintenance marker；釋放成功後才持久記錄並回報 healthy。
5. 失敗保留原始固定錯誤碼；健康驗證失敗會嘗試停止候選，若停止也失敗則 `cleanup=stop_uncertain`，不可當已清理。helper timeout 會只清除此 invocation 的隨機命名容器，無法確認已移除則另記 `helper_cleanup_uncertain`，保留原始失敗碼。最後釋放 maintenance 的 transport outcome 若不明則記 `maintenance_release_uncertain`、保留 switching 狀態供人工核對；此時可能已重新准入新工作，因此不盲目停止服務。maintenance marker 持久保留，防止未完成部署重新接受工作。workflow 不自動反覆重試／部署或恢復資料庫。
6. 手動 rollback 不接受新 digest，只取記錄的前版 digest；同樣要 quiescence、驗 image／sandbox、備份。失敗 candidate 不升格為 previous；rollback 若失敗，重試仍指向同一個已知 healthy 前版，成功才更新 history。schema hash 必須與前次切換前相同；若已遷移 schema，拒絕切回，需另行審查 migration／restore。**保留 image 不代表任意 DB 降版安全；控制器絕不自動還原 DB 或丟棄部署後資料。** 初次部署無 previous image；切換中斷或首次初始化失敗的人工 recovery 不由任意 shell workflow 取代。

備份留在 staging 私有 volume，不上傳 GitHub；由 VM 操作者安排保留／磁碟空間監控與異機備份。控制器不自動刪歷史備份。rootless daemon 與持久 runner 若重啟，需驗證 user service linger、socket、cgroup、labels、job hook；不得讓未完成部署因 VM 重啟變成成功。

diagnostics沒有shell／path／grep／log行數輸入，只讀固定staging service。Docker logs固定`--since 15m --tail 100 --timestamps`，輸出先寫私有匿名TemporaryFile，命令限時15秒，再最多讀64KiB並丟棄截斷片段；64KiB不是暫存檔落盤上限，超長單行仍可能在限時內寫入更多資料。staging Compose設定json-file rotation max-size=10m、max-file=3；真正VM尚須核實effective logging設定與磁碟空間，rotation及timeout不能保證每次capture的64KiB硬上限。只輸出完整匹配的startup／shutdown／loopback health事件固定code與正規化timestamp。未知行、OAuth、URL／姓名／逐字稿／SDP／audio或任意exception原文整行丟棄，只計omitted_lines；沒有原文或遮字fallback，synthetic測試含尾端注入、非UTF8／超量及完整報告未知欄位。這是有界安全日誌投影，不是任意原始log的保證。

資源只輸出configured memory／CPU／pids限制及numeric CPU%／memory bytes／pids；container只保留status／health／OOM／exit code與固定package digest。SQLite用mode=ro／query_only，只傳固定booleans／schema hash，不dump、不匯入application constructor或migration。source SHA／成功build run連結來自已驗證receipt，deploy／diagnostic run ID必須有界正整數；不輸出inspect env、私人配置、帳號或token。helper／commands有timeout，artifact保留7天；不是restart／redeploy／自動修復。

## 原生 Projects 與技能

GitHub Issues／PR 是工作與 code review 記錄；原生 GitHub Projects 作看板：`Todo → In progress → In review → Staging validation → Done`，可加 Environment、source SHA、image digest、validation level、Actions run URL 欄位。Done 依票的 acceptance evidence；僅 health 成功不把音訊 E2E ticket 標完成。本分支加入 issue／PR templates；Project 建立、欄位、保護規則、變數、secrets、runner registration 與遠端 tickets 仍由有相應授權的父任務操作，本次 source 變更不代表已建立看板。

`.agents/skills` vendor `mattpocock/skills@59db609eb1e18aaf59b63284d0e52255aff0c63c` 的 engineering 20＋productivity 7＝27 個完整技能與 MIT LICENSE。`UPSTREAM.json` 記錄每檔 hashes；唯一 vendor 修改為各 `agents/openai.yaml` 增加 `policy.allow_implicit_invocation:false`。保留 assets／references／其餘附檔，不複製上游根 AGENTS／CI／package。隨附程式只有 `wizard/template.sh`（會寫 .env、GitHub secrets／variables、開 browser）及 `diagnosing-bugs/scripts/hitl-loop.template.sh`（互動並回印 observations）；均只靜態讀取，不執行。這是完整檔案盤點與敏感動作辨識，**不是完整安全 audit**。GitHub issue／PR adapter 見 [issue-tracker](agents/issue-tracker.md)，含 Draft PR、普通 issue 關聯與驗收後明確關票；PR merge 不自動完成驗收票。階段路由與權限覆蓋規則見根 AGENTS；下個 run 再核實 discovery，本輪明確讀檔使用，不宣稱熱載入成功。

## 官方參考

- [GitHub hosted runner 與 private usage](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [GitHub Actions security／self-hosted runner 風險](https://docs.github.com/en/actions/reference/security/secure-use)
- [Runner job hooks 與非零 exit 阻擋 job](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts)
- [Docker rootless 資源限制與 delegation](https://docs.docker.com/engine/security/rootless/tips/#limiting-resources)
- [Docker named build contexts](https://docs.docker.com/build/ci/github-actions/named-contexts/)
- [GitHub Actions artifact API](https://docs.github.com/en/rest/actions/artifacts?apiVersion=2022-11-28)
- [GitHub workflow run／attempt API](https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2022-11-28)
- [Matt Pocock 官方 source](https://github.com/mattpocock/skills/tree/59db609eb1e18aaf59b63284d0e52255aff0c63c)


完整image／host／private volume前置與#7本人裝置授權方案見 [DEPLOYMENT-PREREQUISITES](DEPLOYMENT-PREREQUISITES.md)；27技能完整清單與assets／references核對見 [SKILLS-INVENTORY](agents/SKILLS-INVENTORY.md)。本輪不執行新OAuth grant。
