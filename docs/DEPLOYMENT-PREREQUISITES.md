# Docker 前置條件與本人 Codex ChatGPT 授權

Related [#1](https://github.com/paulpai0412/teams-transcription/issues/1)、[#5](https://github.com/paulpai0412/teams-transcription/issues/5)、[#7](https://github.com/paulpai0412/teams-transcription/issues/7)。本文件與 CLI onboarding 已實作；Oracle 的本人初次 grant、正常 refresh、撤銷／過期後重授權、Live RX/TX **尚未實測**。目前沒有可宣稱已在 Oracle 開放的 ChatGPT 登入網址。

新增管理頁裝置授權的 source 與操作邊界見 [Web 授權](WEB-AUTHORIZATION.md)。下列私人終端限制仍適用 CLI `auth-login`；Web 使用獨立的 session／CSRF 保護及 native app-server device flow，不重用 CLI 的 TTY 入口。source 已有入口不表示已部署、配置或取得真實 grant。

## 三層前置條件

| 層 | 必要內容 | 取得／驗證方式 |
| --- | --- | --- |
| image Python | Python ≥3.11，優先3.12；固定 base digest 的 manifest 為3.12.15；playwright1.62.0、starlette1.6.0、httpx0.28.1、uvicorn0.52.1、pydantic2.13.4、PyYAML6.0.3；Python自帶SQLite | pyproject 與 native wheels context；`auth-preflight` 讀 installed metadata，不臨時下載／安裝 |
| image CLI／browser | Codex **0.159.0-alpha.3** 與其 app-server；Node≥16（官方 launcher engines，apt實際版本另記）、npm；Chromium＋chromium-sandbox、FFmpeg、ca-certificates、tzdata | Codex tarballs固定SHA512；`--version`／FFmpeg `-version`；Chromium由套件提供system libraries，apt closure未全鎖版本；ARM64必須實跑，不靠manifest |
| image audio／WebRTC | Chromium原生WebRTC／WebAudio、WebCodecs Opus接收；FFmpeg供受控媒體檢查，SQLite保存來源 | `b_no_plc.py`／`native_media.py`；不盲裝Python audio SDK、PulseAudio、VNC或額外DB；sandbox與codec實際能力由image smoke／另行媒體probe驗證 |
| build/test only | setuptools≥68、build1.3.0、wheel工具；`python_wheels`／`codex_npm` contexts；websockets16.x僅相關bridge tests | 不當作新runtime服務；舊x64 closure不當ARM lock；contexts無私有資料 |
| host | Ubuntu24.04 ARM64、專用非root rootless Docker／Compose、user service linger、cgroup v2/systemd及CPU/memory/pids delegation；runner與root-owned controller／hook | 另一核准VM任務負責。VM不另安裝應用Node／Codex；host `/usr/bin/python3` 僅供固定控制器。runtime UID/GID10001與host runner UID1002不同 |
| runtime security | readonly rootfs、drop ALL、no-new-privileges、原seccomp、私有/data、/tmp tmpfs、**/dev/shm tmpfs**、sandbox可用，資源限制實際生效 | `auth-preflight`查metadata與mount；sandbox／實際cgroup測試由workflow/controller，preflight不假裝已啟動browser |
| runtime private data | 同一部署的runtime.yml、管理密碼／session、Google readonly OAuth、Codex home、SQLite各依用途分開路徑；staging volume `teams-staging-data` | Codex建議 `/data/codex`，0700、UID/GID10001；auth.json0600，同一可寫持久volume。既有compose.auth.yml可掛同一部署 `/auth/codex`，不從別台複製 |
| network／operator | 核准的正常TLS／CA／正確時鐘／外連；本人瀏覽器可完成ChatGPT MFA；Live另需核准的實際ICE／TURN／媒體出口 | 不開新public port、不自動改firewall或安全設定。browser allowed_egress_hosts不是Codex子程序全域防火牆；媒體可達性需獨立驗證 |

實際 image 軟體版本與架構應連同 source SHA／digest 寫入證據。apt版本受建置時倉庫影響，本分支不宣稱完全鎖定所有system libs。既有本地x64結果不能替代ARM64／Oracle收據。

## 先分清四種登入

- 助理管理頁 `/login`：本專案 scrypt 管理密碼；不是ChatGPT。
- Google connect/callback：Calendar readonly；不是ChatGPT。
- meeting `storage_state`：Teams的context設定；不是ChatGPT持久browser profile。
- **Codex ChatGPT OAuth**：CLI/app-server使用CODEX_HOME中的授權；`account/read(refreshToken=false)`只接受chatgpt，沒有API-key fallback。Chromium為headless媒體／會議用途，沒有VNC／noVNC或遠端ChatGPT browser GUI。

固定版本容器內已離線執行 `codex login --help`，確認有 `--device-auth`；未啟動登入。官方[Authentication](https://developers.openai.com/codex/auth)將device code列為headless方式；本人帳戶／workspace若未允許此功能，需本人／管理員另核准，不由程式自動開安全設定。MFA與一次性代碼由本人在自己的瀏覽器完成，不交給dot／Actions artifact／issue。

## 新增CLI與真正執行的邊界

```text
auth-preflight --config /data/config/runtime.yml
    offline metadata、套件／工具版本、UID/GID、可寫與最具體持久mount、tmpfs
auth-login --config /data/config/runtime.yml --confirm-account-authorization --confirm-private-terminal
    本人安全TTY才可啟動固定 codex login --device-auth
auth-login ... --confirm-account-authorization --confirm-private-terminal --reauthorize
    本人明確重授權既有同部署home，不自動logout／刪auth／換帳號
auth-check --config /data/config/runtime.yml
    明確readonly account/read；不啟動模型、browser或音訊
```

此處是後續已獲本人具體授權時的操作方案，**本次沒有執行login／auth-check或建立grant**。`auth-login`拒絕CI／GitHub Actions、非TTY、缺明確確認、API key／access token環境、未指定位於持久掛載的home、錯誤UID/GID／權限。已存在auth.json需`--reauthorize`；取得鎖後重查，避免並行登入覆寫新授權。既有daemon必須先由已核准方式停止，程式取得其lock才登入，不自動停止會議。home／auth symlink拒絕，既有私有設定不自動改權限繞過gate。

操作者先在**既有安全互動終端**準備相同部署的私有runtime.yml與volume，設定`codex_home: /data/codex`（不可仍用readonly image的預設home），以與服務相同UID/GID、同一volume執行一次性容器中的CLI。專用 `deploy/compose.onboarding.yml` 安裝到 `/opt/teams-staging/compose/compose.onboarding.yml`，為 standalone設定：logging driver **none**、無ports、相同named volume與非root安全條件，預設只跑offline preflight，不會啟動登入。本人確認終端未錄製且logging停用後才可加 `--confirm-private-terminal`；一般service的Docker logging會保存stdout，不能拿來交換device code。Compose `run --rm --no-deps`不加`--service-ports`；本分支不提供可輸入任意命令的Actions登入job，不把一次性device code送進GitHub logs，不新增登入endpoint／port。程式只把native CLI連到本人TTY，**不捕捉／保存其網址、代碼、token、帳號資訊**。

成功exit＋私有auth.json只回報`login_process_completed`，仍不是account或媒體通過。取消／逾時可能發生在native CLI剛寫完授權的邊界，因此標`outcome_unverified`，不自動刪除或重試；由本人核對後決定是否重新授權。timeout是600秒上限，不會自動反覆建立grant。

## refresh、重建與失效

Codex正常refresh交給固定版native實作並寫回**同一可寫home**，不自建refresh-token HTTP worker，不搬auth檔。`account/read(refreshToken=false)`不強制刷新；CLI wrapper不宣稱已驗證長期refresh。服務停止／容器重建／image升版保留原named volume，SQLite與Codex home獨立路徑不刪除；這不是把auth烤入image。Compose smoke使用**SYNTHETIC-NOT-A-REAL-GRANT**文件，驗證restart與force-recreate後私有auth metadata／內容不變，不能冒稱真實OAuth續用成功。

| 安全錯誤／狀態 | 後續處理 |
| --- | --- |
| `codex_home_explicit_persistent_path_required`／`codex_home_persistent_rw_mount_required` | 準備同部署持久可寫volume，排除最具體mount是tmpfs／ramfs／直接overlay root／ro；nested Docker CI的overlay-backed獨立named-volume bind僅於source root符合 `/volumes/<name>/_data`時接受，並以force-recreate驗證保存；不自動修配置 |
| `codex_home_private_uid_gid_required`／`codex_auth_private_uid_gid_required` | 核對rootless subordinate UID/GID與0700／0600，操作者修正，不放寬成公開 |
| `runtime_dependency_missing_or_version_mismatch`／`codex_pinned_version_required` | 重建正確image／contexts；不在VM臨時安裝或換CLI版本 |
| `chatgpt_authorization_required`／`chatgpt_auth_required` | 尚未授權或native沒有可用chatgpt帳號；本人核對失效／撤銷後再明確重授權，無自動API-key fallback |
| `codex_device_login_denied_or_failed` | native CLI在本人TTY顯示詳細原因；不擷取原始錯誤到log，拒絕不重試 |
| `codex_device_login_cancelled_outcome_unverified`／`codex_device_login_timeout_outcome_unverified` | 程序收尾後本人核對auth狀態；不冒稱沒有發出grant、不自動重登 |
| provider timeout／probe failed／readonly | 保留provider固定phase／error class與cleanup結果；檢查工具、權限、服務與核准網路，**不能直接推定唯一原因是過期或撤銷** |

`auth-check`只輸出既有provider安全allowlist，不輸出email／帳號ID／token。account已准入後仍須另行核准的Live smoke：**TX＝容器送到GPT Live；RX＝容器收到GPT Live**，驗證原生peer／datachannel、實際受控音訊交換與cleanup。SDP accepted／account/read／loopback各自不能當RX/TX成功。真人Teams遠端可聽仍由[#6](https://github.com/paulpai0412/teams-transcription/issues/6)單獨驗收；#5無憑證runtime preflight不用等#7本人grant，避免循環依賴。
