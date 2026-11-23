# Teams 會議助理

Google Calendar → Teams 自主入會 → GPT Live A 保存逐字稿 → GPT Live B 回答「小白小白」。提供登入管理頁面；SQLite 是唯一資料來源，不使用 MCP、Page 或會議 Markdown。

2026-11-23 操作者明確重新授權服務端會後 Codex 自動摘要與手動重試：正常結束且含逐字來源的場次自動排入 `summary_jobs`，由 daemon 的 `_summary_loop` 產生可引用衍生摘要，亦可在管理頁手動重試；崩潰恢復（`service_interrupted`）與 acceptance 預算場次不自動觸發摘要。逐字來源與歷史摘要／待辦維持原有 provenance、citation 與 fenced lease 安全邊界，不新增 dot 整合或替代摘要服務。

本目錄是 WSL2 來源移轉套件，不是已核准部署。先讀 [接續規則](docs/HANDOFF.md)、[原移轉記錄](TRANSFER.md) 與 [實測界線](evidence/RESULTS.md)。`TRANSFER-MANIFEST.json` 保留原始 provenance，不會因本次修改重寫；目前工作目錄不再是 pristine transfer。

## 執行與資料

- `cli.py` 的 `serve` 擁有單一 `AssistantDaemon`；管理分頁關閉不停止會議。
- Calendar 的 series／occurrence 與每次 session attempt 分開。Teams URL 只作 locator；取消、改期與授權在入會前重讀。
- A 經 sandboxed Chromium 的原生音軌／本機 WebRTC relay 送入隔離 Live peer，不走 Python PCM 送音 queue 或自訂時鐘估速。PCM 回呼只供 B／音訊觀測及明確啟用的 WAV；SQLite 分別保留 provider final、application segments 與未定稿來源。
- 核准的全域／系列人名及術語在入會時凍結，透過 A `initialItems` 提供參考；不改寫 provider 原文。
- B 有獨立 native Live 通道，每題讀本場 SQLite 與凍結的前次摘要／待辦。同一句「小白小白＋問題」不要求重問。兩個本機接收段只解碼實際收到的 Opus，不使用 PLC 延續舊音；缺樣由持續原生音軌輸出靜音，再經本機 WebRTC relay 接 owned Teams sender。沒有 Python PCM 或分塊播放器。完整來源交付後、marker dispatch 前核對授權並開通；正常完成需真實來源 quiet 與播放 quiet 都成立。缺樣靜音不是來源 quiet，遠端 Teams 的 PLC／drain 不在本機保證範圍內。
- 停止、drain、清理後封存來源；正常結束且含逐字來源時自動排入 `summary_jobs` 由 `_summary_loop` 產生 Codex 衍生摘要（管理頁亦支援手動重試）。保留既有歷史摘要／待辦可讀；省略舊待辦不代表完成。
- 重啟不自動重入中斷場次，也不對崩潰恢復（`service_interrupted`）自動觸發模型摘要。清理不明或中斷 capture 留下持久 interlock；可透過歷史頁的[人工清理審查](docs/CALENDAR-RUNTIME.md#中斷後的人工清理審查)確認舊資源停止／助理離會，不改寫原失敗，也不自動入會。

詳見 [執行契約](docs/ABC-RUNTIME.md)、[SQLite](docs/MEETING-STORE.md)、[Calendar](docs/CALENDAR-RUNTIME.md)、[詞庫](docs/GLOSSARY-RUNTIME.md)、[B](docs/B-LIVE-NATIVE.md)、[摘要](docs/SUMMARY-RUNTIME.md)。

## 本機開發

Python ≥3.11，優先 3.12；setuptools／pip，沒有 Bun 或 frontend build。Codex 固定 `0.159.0-alpha.3`；Node/npm 支援 Codex 與合成 JS fixtures。Chromium 必須開 sandbox，FFmpeg 用於合成媒體驗證。

```bash
# 此 WSL2 checkout 已有隔離 tooling；不更改全域 Codex／OAuth。
source .tools/activate
python -m teams_transcription --help

# 新的隔離環境；會下載公開依賴。
python -m pip install '.[bridge-test]' 'build==1.3.0'
python -m build --outdir /tmp/teams-assistant-dist
PYTHONPATH=tests:. python -m unittest test_assistant_store test_assistant_daemon test_calendar_client test_glossary_context test_sqlite_voice_context -v
PYTHONPATH=tests:. python -m unittest test_admin_web test_admin_browser -v
```

沒有配置 lint、formatter 或 coverage 門檻。完整 `python -m unittest discover -s tests -v` 另含實際 Chromium／Node／可選 bridge 前提；此移轉包已知缺 `experiments.probe_ws_turn_bridge` 與 `experiments.ws_turn_bridge`。必須如實報告，不能把可執行子集稱為完整 discovery 通過。

## 私有設定與管理頁

以 `runtime.example.yml` 為新設定基準。自訂 `data_root` 時，也須明確指定其內的管理密碼、session secret、Google credentials 路徑。設定優先序為 CLI → `ASSISTANT_` 環境 → YAML → defaults；未知欄位拒絕。現有 `.tools/runtime.local.yml` 屬舊配置，不能直接拿來啟動新服務；不自動改寫操作者檔案。

```bash
teams-transcription init-admin --config /absolute/private/runtime.yml
teams-transcription doctor --config /absolute/private/runtime.yml
teams-transcription serve --config /absolute/private/runtime.yml
```

`init-admin` 互動輸入密碼、保存 scrypt hash 與私有 session key，拒絕覆寫；沒有預設密碼。`doctor` 僅查本機工具與檔案 metadata，不讀 token、不開 browser、不連 provider。`serve` 才啟動背景工作；預設排程暫停。`probe-*` 不是離線自檢，不自動執行。

登入後首頁是 **A 版週行事曆**：選擇會議可預覽，或展開成會議工作區。時間使用設定的 `timezone`；七日時間格支援重疊、跨日與清晨／夜間場次，窄螢幕在格內捲動。

- 區分系列／單場、等待准入／記錄中／已停止；不以排定時間冒充實際入會。
- 前次結論、系列待辦、本次逐字稿／問答／摘要分開呈現。會議詳情每 5 秒更新，行事曆每 15 秒更新；切換會議不接收舊請求的晚到資料，更新保留閱讀位置。
- 設定獨立於 `/settings`；排程、手動場次、系列歸屬、停止／發聲、待辦、詞庫核准與清理審查仍使用原有管理操作。進階狀態移至 `/diagnostics`，不放在首頁。
- 問答保存在獨立 SQLite 日誌，不改 A 逐字來源或摘要。Native 未提供可靠問題關聯時，回覆明確標示「未對應問題」；文字不能證明遠端已聽到聲音。舊場次沒有日誌不代表沒有發生問答。

詳細資料與安全契約見 [Calendar runtime](docs/CALENDAR-RUNTIME.md)。Calendar 邀請不是錄音或發聲同意。需提供操作者持有且獲准使用的 Google OAuth client，並完成同一登入 session 的 readonly OAuth callback；不建立或抽取其他環境憑證。

登入 cookie 使用 HttpOnly／SameSite，所有變更檢查同源 Origin 及 CSRF。遠端管理必須配置 HTTPS `public_base_url` 與 secure cookie；本機 override 只綁 `127.0.0.1`。單程序、單 worker，不能用多 Uvicorn workers 複製 scheduler。

## Docker

`Dockerfile` 建置 Python、固定 Codex、Chromium sandbox、FFmpeg 與本應用。`docker-compose.yml` 要求明確 image、非 root UID/GID 與既有私人 appdata；不自動建立主機掛載目錄。容器 readonly root、drop ALL capabilities、no-new-privileges、專用 seccomp、tmpfs 與 bounded resources。

```bash
# 只有已核准的本機 build／驗證；不是部署指令。
# 目的目錄須尚未存在；只下載並驗證固定版本的公開 npm 套件，不讀 OAuth。
python tools/prefetch_codex_npm.py /tmp/teams-codex-npm
# Python 3.12、pip 與 build 的建置環境；產物只留在臨時目錄。
python -m build --wheel --outdir /tmp/teams-python-wheels
python -m pip download --only-binary=:all: --dest /tmp/teams-python-wheels \
  /tmp/teams-python-wheels/teams_transcription-*.whl 'setuptools>=68'
docker --host unix:///var/run/docker.sock build \
  --build-context codex_npm=/tmp/teams-codex-npm \
  --build-context python_wheels=/tmp/teams-python-wheels -t teams-assistant:local .
python tools/check_compose.py
# 使用上一步的 immutable local image ID；建立並清除自己的臨時 volume／container。
python tools/compose_smoke.py --image sha256:IMAGE_ID
```

`codex_npm` 與 `python_wheels` 都是必要的 build contexts，不是憑證掛載。前者僅含固定版本的公開 npm artifacts；amd64／arm64 建置會重驗 SHA512、套件 graph 與 archive，再以 offline npm 安裝。後者含本機應用 wheel、公開執行依賴與 build backend wheels；鏡像仍從目前原始碼離線建置安裝，該步驟 `--network=none`，不使用輸入的舊應用 wheel 取代原始碼。每次 dependency／build requirement 變動都須重建 wheel context；缺檔直接失敗，不偷偷上網補件。

`.dockerignore` 採精確白名單；新增 UI 資產時須同時納入 Docker context 與 wheel package data。`tools/compose_smoke.py` 會在無網路、無憑證的暫存容器內實際登入並驗證七日行事曆及樣式，不只檢查 HTML 回應。

下載與 BuildKit 安裝分開，不靠延長 timeout、跳過 TLS 或改 Codex 版本繞過環境故障。amd64 與 arm64 均改為固定 SHA512／offline npm 安裝；ARM64 context 須加 `--platform linux-arm64` 另行下載。此修改不代表已完成 native ARM64 image／browser／VM 實測；實際結果見本次交付證據，同樣須提供兩個命名 contexts。既有 Codex bundle 可用 `python tools/prefetch_codex_npm.py /tmp/teams-codex-npm --verify` 離線核實。此 WSL 既有 `.venv312` 無 pip，wheel 可用 `build --no-isolation`；依賴下載需另用帶 pip 的 Python 3.12 工具環境，不能把操作者 OAuth／data root 放進任何 build context。

本機驗證服務已於操作者核准後更新為不補音版 `teams-assistant:local-no-plc-20261029`，immutable ID=`sha256:5577e099484d6d01fb85551f571d69cc39cc596a2c4de1d198c6db71d7d64a07`；55 個 Python modules 與 2 個 UI 資產的 host／installed／image workdir 完全一致。管理頁位於 `http://127.0.0.1:18081/`，沿用既有 host network、非 root、readonly、sandbox 與同一部署 OAuth，排程仍暫停。切換前已備份 SQLite，全部 27 張表（含 SQLite sequence）內容不變；16 項隔離 Compose checks、容器內 11 項原生音訊測試及實際服務登入頁／關分頁後健康檢查通過。本輪未入會，真人聽感待驗收。[部署收據](evidence/no-plc-deployment-20261029.json)／[目前狀態及私有配置位置](docs/HANDOFF.md)。host network 仍是先前核准的本機繞過措施，不更改 repo 預設 bridge 設定，也不宣稱 WSL 底層已修復。

新合成音訊的[原生A／詞庫對照](evidence/system-completion-A-differential-20261017.json)與[30分鐘A→SQLite→實際Codex摘要→下一場系列記憶](evidence/system-completion-native-lifecycle-20261017.json)已通過指定事實；不宣稱詞庫提高辨識率。B 原先錯把每次發問當成必有獨立 Native user done／新委派；[隔離控制](evidence/native-completion-contract-isolation-20261017.json)實際收到三題合併 final 與無新委派的快取回答。依操作者選擇，已改由主程式分題、每題讀 SQLite，再用官方 `session.context.append` 回傳同一 thread，不偽造 provider final、借舊 ID 或逐題重連。[最終真 Native smoke](evidence/native-host-questions-final-20261017.json)13題皆通過，每題各讀一次資料，累計45,843bytes；涵蓋單獨喚醒後追問及短停頓，未喚醒時不查詢／輸出。這是本機合成驗證，不是無限長穩或真人 Teams 驗收；後續第18輪實測與最新修正見下段。

第18輪真人無回覆已定位為閒置PCM callback背壓，修正tap准入及continuation首段；decoder／Teams播放器不重建。[當時943項回歸與真Native控制](evidence/native-idle-output-capture-final-20261018.json)是對應版本的歷史證據，不是後續真人驗收。

第19輪確有兩次SQLite查詢並送23,069bytes，但兩次選取都漏掉本場三事實，後段另因有效音訊callback背壓停用B。[原失敗](evidence/teams-native-round19-context-failure-20261018.json)保留。第一版分批修正曾以同thread三題、125,210bytes通過指定事實，但不能據此保留任意12KB門檻；[當時證據](evidence/native-context-delivery-repair-20261018.json)仍屬歷史版本。

上一版已移除12KB整批序列化門檻，改用完整`ContextPlan.payload`及固定SDK500-byte UTF-8分塊，最後一次marker；945項回歸與當時真Native控制見[歷史證據](evidence/context-quota-removal-review-20261018.json)，不是本次完整語音品質驗收。

前一版移除任意檢索／歷史／詞庫裁切、2,000字元提問拒收、隱含回答時限與預設一小時退會；摘要改以有效進度watchdog及續租處理長工作。B改觀測A的單一原生混音，避免跨frame假gap、缺clock及重複送音；A就緒不與B clock或重複啟動混用。必要授權、來源／版本與有限媒體積壓保留。[政策與擁有層](docs/B-LIVE-NATIVE.md)／[該版證據](evidence/arbitrary-gates-removal-20261018.json)。其後服務31已執行第20輪真人測試，不能再當作idle服務；現況先查[交接](docs/HANDOFF.md)。

第20輪回報漏字、重複待辦及最新內容延遲。當時以有限FIFO修正首段准入，並改送每題完整可讀SQLite快照，無內容配額；之後真人仍回報整段斷音、重複及錯答，不能用歷史成功收據取代。**目前原始碼已將正式B輸出改為持續native track relay，移除PCM搬運／排程／准入丟包分支；完整SQLite工具機制保留。** 本機跨context下游波形驗證不等於GPT Live內容品質或真人Teams驗收；候選與部署狀態見[交接](docs/HANDOFF.md)，數據與限制見[驗證紀錄](evidence/RESULTS.md)。

實際啟用需操作者先準備 `/data/config/runtime.yml`、私人 appdata 的 UID/GID 與 Google 設定，再用 `compose.local.yml` 開啟 loopback 管理頁，透過同一配置的 `compose run --rm teams-transcription init-admin` 初始化。`compose.auth.yml` 僅 opt-in 掛載**同一部署既有** Codex home，允許正常 OAuth refresh 寫回；不複製 token、不自行建立新登入、不掛 Docker socket。本人首次裝置授權、私有持久home、重授權與軟體前置條件見 [部署與OAuth prerequisites](docs/DEPLOYMENT-PREREQUISITES.md)；新增CLI入口不代表本輪已取得grant，管理頁登入也不是ChatGPT。

管理頁 Google／ChatGPT 裝置授權入口、配置需求與驗證限制見 [Web 授權](docs/WEB-AUTHORIZATION.md)。本 target 的獨立 Web client 安裝、Bot Calendar 設定、本人 Google OAuth/MFA與實際讀取分層流程見 [Google／Calendar 安全設定指南](docs/GOOGLE-CALENDAR-SETUP.zh-TW.md)。本次 source 修改不代表已部署或完成真帳號授權。本 target 固定 Actions 控制、本人新鮮同意 binding、空私有 home 準備與明確媒體政策見 [控制與 OAuth 前置繁中指南](docs/TARGET-CONTROL-OAUTH.zh-TW.md)；工程 ready、OAuth、單次 Join 與遠端聽驗分開記錄。

容器 `/dev/shm` 必須是 tmpfs：app-server TRACE 可能包含編碼媒體，單靠 `RUST_LOG` 或「沒有 WAV」不夠。RPC 私有 runtime 退出後清除，缺 tmpfs fail closed。不用 privileged、`--no-sandbox`、host IPC 或 unconfined 繞過問題。

Chromium seccomp 依 [Playwright Docker 指南](https://playwright.dev/python/docs/docker) 與其官方 profile；瀏覽器 sandbox 是否在此主機真正可用，以實際本機頁面驗證為準。

## 證據不是端到端成功

最新已執行結果集中在 [evidence/RESULTS.md](evidence/RESULTS.md)。受控 provider／summarizer、SQLite 回歸、實際管理頁與容器本機頁面只能證明對應邊界，不能證明 Google 真帳號取會、真人 Teams 入會與轉錄、遠端可聽 B 回覆，或詞庫辨識改善。

歷史版本曾完成 admission-only 真人准入及保存來源的一般 Codex 摘要；這不是本次自主 Calendar → A／B → 摘要 → 下次記憶的同次驗收。真實驗收需要當次有效會議、錄音／發聲同意、既有 OAuth／模型權限、Google readonly 授權與可用媒體出口。沒有來源逐字稿、可引用摘要及乾淨清理，不能宣稱目標完成。

## GitHub 開發與 staging

本 target `timmypai/teams-transcription` 採 [Hermes 非作者獨立 exact-head review + GitHub Actions](docs/PR-AUTOMATION.md)；2026-11-18 決策已取代上游 Cloud-only 審查要求，無 Cloud grant／額度前提。PR CI 只跑 hosted；merge 不部署。新 `deploy.yml`／`control.yml` 僅手動、main 最新且已審 commit，走既有 devflow rootless runner；產品適配／私有初始化／精確回滾／固定 same-UID Unix 控制與驗收界線見 [GitHub 交付契約](docs/GITHUB-DELIVERY.md)。source／hosted fixture 不代表 Oracle health、使用者實際登入、OAuth 或真人 Teams 成功；歷史 paulpai0412/GHCR/staging 不可借用。未新增公開入口或變更 Unraid。
