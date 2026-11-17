# GitHub 開發與 Oracle staging 交付契約

## timmypai hosted-only 接入（本輪有效範圍）

本輪 target 僅 `timmypai/teams-transcription`；來源為 `main@c792cf9f313dc1ba2fafe8eb1c811b8e862adc66`。下方 `paulpai0412` 的歷史部署資源、授權、runner、GHCR 與 `/opt/teams-staging` **不屬於本次可用資源**，不得借用。

- 新 `ci.yml` 只在非特權 `pull_request`／手動 `workflow_dispatch` 跑 GitHub-hosted Ubuntu，唯讀 token、不讀真 secrets、不 publish、不上 Oracle、不 Join／grant。checkout exact head，test/check/verify/verify-base 由 `devflow-ci` 聚合。merge 不部署。
- 公開 npm 固定 SHA512 的 `codex_npm` 與當次 Python3.12 公開 wheels 兩個命名 contexts 均實際建置；映像保留原 non-root、readonly、drop ALL、seccomp、tmpfs。回歸在 network-none 的同版映像跑所有可用 suites；只明列排除原移轉缺來源的兩個 bridge suites，不宣稱原始 full discovery。Node、sandboxed Chromium 與固定 Codex 均由映像提供。
- `.devflow/hosted_verify.py` 僅 disposable hosted harness：隨機 `teams-transcription-ci-*` 容器、專用 volume、無網路 app、無 published port、無 Docker socket 掛載。建立自己的 foreign sentinel／檔案／SQLite，核對前後 hash、身分、啟動時間及狀態；所有 controller mutations 都經 exact tuple allowlist，混合 foreign plan 在第一個操作前整批拒絕。證據的 `outside_mutations=0` 僅表示該 harness 稽核範圍，**不是 Oracle 或全主機任意檔案未寫入的證明**。
- AC-1～3 的九個 Given/When/Then 從 EPIC `t_a3f4f673` 定稿逐字納入 `.devflow/scenarios.json`。PR 證據標明 disposable hosted scope，與部署證據分離；負例不在實際部署執行。UI 使用真正 sandboxed Chromium／已安裝管理頁；只用隨機一次性測試密碼，沒有真憑證、provider 或會議。trace/video/HAR 關閉，密碼欄清空後才截圖。
- base job 真正建置 PR base 的應用映像，用相同 UI fixtures 核對既有登入行為；CLI adapter 在 base 缺席時回報 INVALID／`Base-unverifiable`，不製造假紅燈。UI 既有行為用 `Already-met`；PR 上的豁免仍需獨立 reviewer 核對，CI 綠不等於豁免已批准或 native review 完成。
- `deploy/isolated_deployment.py oracle-preflight` 固定 exit 1，輸出 `enabled=false`、空 VM allowlist；沒有 enable 旗標、self-hosted 新入口或虛構 socket/UID。專屬 VM 安裝由別卡處理，可信工作准入、ownership、安全入口、原生獨立 review 與明確手動部署契約由 SHIP 核實後另行接線；此 PR 不啟用 Oracle workflow。
- AGENTS／PR-AUTOMATION 的獨立 Codex Cloud 原生完成 gate 未改，仍 `unverified`。普通 approval、無 findings、作者自審、綠 CI 都不能授權 merge；本輪不自行發出耗用未知額度的 review 指令。實際部署、使用者登入、Teams 雙向語音均未由 source 或 hosted 證據宣稱通過。

以下保留原來源歷史契約，非本次操作授權。


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
