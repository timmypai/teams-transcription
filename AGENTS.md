# Repository Guidelines

## Repo-local Skills & Standard Delivery

- `.agents/skills/` 保留官方 `mattpocock/skills` 27 技能參考（`policy.allow_implicit_invocation: false`，見 [.agents/skills/UPSTREAM.json](.agents/skills/UPSTREAM.json) 與 [docs/agents/SKILLS-INVENTORY.md](docs/agents/SKILLS-INVENTORY.md)）。只在實際需要時按階段選用：`grill-with-docs`、`to-spec`、`to-tickets`、`tdd`、`implement`、`diagnosing-bugs`、`code-review`、`pr`；嚴禁在無授權下執行外部寫入／安裝腳本／自動關票（參考 [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md)，Draft PR 與驗收票分離規則不變）。
- **審查與標準交付（2026-11-26 更新）：** 本 repo（`timmypai/teams-transcription`）採用 **Hermes 獨立 reviewer + GitHub Actions** 與 **devflow 公版部署流程**：`.github/workflows/deploy.yml` 依 devflow 公版啟用自動部署（`push` 至 `main`/`master` 自動觸發 default-tip 部署並保留 `workflow_dispatch`，`.devflow/config.env` 設 `DEVFLOW_DEPLOY_MANUAL_ONLY=false`）；`.devflow/deploy.sh`、`.devflow/capture.sh`、`.devflow/redact.py` 與公版一致，映像離線建置放在 `.devflow/deploy-build.sh`，組態參數放在 `.devflow/config.env` 與 `deploy/compose.yml`。`control.yml`、`operate.yml`、`oracle-teams.yml` 等其餘控制與驗證 workflow 維持手動 default-tip `workflow_dispatch`-only。
- 本人 OAuth onboarding（Related #7）：`auth-preflight` 僅查離線 metadata；`auth-login` 限本人 TTY 且 daemon 已停；`auth-check` 才做 readonly account/read。管理／Google／Teams 登入皆不能替代 ChatGPT OAuth（見 [docs/DEPLOYMENT-PREREQUISITES.md](docs/DEPLOYMENT-PREREQUISITES.md)）。

## Project Overview

Python 會議助理：Google Calendar 找到 Teams 場次，自主入會；A 使用 `gpt-live-1-codex` 保存逐字來源，B 以獨立 native Live 通道回答「小白小白」問題。2026-11-23 操作者明確重新授權服務端會後 Codex 自動摘要與手動重試（`_enqueue_summary`、`_summary_loop`、`retry-summary`），以 SQLite 封存來源產生衍生摘要並維持原本 provenance／citation／fenced lease 安全邊界。Docker 提供登入管理頁面。SQLite 是唯一會議資料權威；不使用 MCP 或會議 Markdown 交換／輸出。

工作前讀 `docs/HANDOFF.md`、`README.md`、`TRANSFER.md`、`evidence/RESULTS.md`。來源起於 WSL2 移轉套件；本 target 是 `timmypai/teams-transcription`，`paulpai0412` 僅歷史上游而非部署／審查身分。提交前核對 Git root／remote，不得提交至包含其他專案的父工作區。來源提交不代表核准部署；既有實測記錄不代表本次真實入會成功。

## Architecture & Data Flow

- **入口與控制：** `cli.py` 的 `serve` 啟動單一 `AssistantDaemon` 和 `admin_web.py`；HTTP request／瀏覽器分頁不持有背景工作。管理頁使用私有 scrypt 密碼、伺服器端 session、同源 Origin 與 CSRF 檢查。
- **排程與識別：** `calendar_client.py` 處理 Google readonly OAuth、分頁與時間；`assistant_store.py` 保存 Calendar、series、occurrence、run。重複場次以 recurring event + original start 識別；Teams URL 只是 locator，不能拿來合併系列。入會前重讀取消／改期與授權。
- **A：** `browser.py` 接收會議音軌，`native_media.py` 以原生 WebAudio／本機 WebRTC relay 混入隔離 Live peer，再經 `live.py`／`rpc.py` → `incremental.py`／`meeting_store.py` SQLite。會議 A 不走 Python PCM push／送音 queue／自訂時鐘估速；PCM 回呼只供 B 及明確要求的 WAV。provider final、application segment、legacy provisional 分別保留 provenance，不依文字去重或猜測說話者。
- **詞庫：** `glossary_context.py` 驗證經核准、適用範圍的 people／terms 快照；入會凍結版本及 SHA，僅放入 A `initialItems`。這不是 ASR hotword API，不能據此宣稱辨識率提升。
- **B：** `response_runtime.py` 組合 `b_native.py`、`b_native_output.py` 與 `b_browser_tx.py`。`b_question.py` 由已准入 PCM quiet＋穩定 ASR 建立 application question，不等待 provider user final／委派。每題重讀綁定會議的 SQLite，由 `MeetingQuestionContext` 交付完整可讀快照，經 `session.context.append`＋`RESULT_COMPLETE` 回傳同一 thread；沒有內容或累計 bytes 配額。完整交付後、marker dispatch 前核對授權並開通，marker 返回且 scope 重查後才 commit。B 的兩個接收段由 `b_no_plc.py` 解碼實收 Opus；持續原生 WebAudio 在缺樣時輸出零值，不走 NetEq PLC。跨頁 WebRTC relay → owned Teams sender 保留，不經 Python PCM／應用 FIFO／BufferSource 排程；真實來源 quiet 與播放 quiet 分開判定。relay 不進 A capture hook／peer roster，B 不改寫 A。
- **Teams 音訊輸入：** `browser.py` 的 `OWNED_AUDIO_INPUT_JS` 僅於 response mode／精確 Teams origins 提供程式持有的靜音 WebAudio input；不開實體麥克風或偽造 permissions。需由 Teams 正常協商音訊傳送，不可把 recvonly／未協商 sender 硬改成可發聲。
- **會後：** 停止／drain／清理 → SQLite seal，保留逐字來源及 provenance。正常結束且含逐字來源時自動排入 `summary_jobs` 由 `_summary_loop` 產生 Codex 衍生摘要（亦支援介面手動重試）；歷史摘要／系列記憶／待辦保留既有資料，省略舊待辦不代表完成。重啟不可自動重入中斷會議，也不對崩潰恢復的 `service_interrupted` 場次自動觸發模型摘要；清理不明確時保留 interlock。

## Key Directories

| Path | Purpose |
| --- | --- |
| `teams_transcription/` | daemon、登入 UI、Calendar、SQLite、capture／Live／語音控制 |
| `tests/`, `tests/fixtures/` | `unittest`、合成 RPC／DOM／media fixtures、實際本機 Chromium 邊界 |
| `docs/` | 執行、詞庫、摘要、來源品質與安全契約 |
| `docker/`, `tools/` | 容器入口、Chromium seccomp、建置與本機驗證工具；先讀副作用 |
| `transfer/`, `evidence/` | 原始移轉驗證／歷史依賴參考與有界證據，不是正式環境設定 |

真實會議連結、人名、錄音、逐字稿、OAuth 與 session secrets 不進 source changes。

## Review guidelines

以下供獨立 Hermes reviewer 使用，以自己既有登入提交並讀回原生 GitHub exact-head review（身分／完整 SHA／連結／具體結論），不製造 Cloud 結果或自訂可信 PASS／approval marker：

- 核心音訊修改檢查 A／B 隔離、真實來源 quiet 與 render quiet、撤權／停止後發聲、SQLite context 完整交付、lease／來源 fence 與未知清理 interlock。合成結果不能推論真人遠端聽感或完整驗收。
- 特別檢查修改是否新增帳號、credentials、安全／存取權限、網路、部署或費用，及是否刪測試、降低 acceptance／修改 review policy。這些需要人工批准；一般路徑或作者 label 不代表批准。
- PR 自動化必須從可信 main 讀政策；不能在特權 `pull_request_target` 執行 PR code、在 Oracle runner 跑 PR、信任 PR artifact／留言／label 作批准。任何 stale SHA、未知／截斷／逾時 review／check 都應 fail closed。主分支合併後的部署依 `.devflow/config.env`（`DEVFLOW_DEPLOY_MANUAL_ONLY=false`）與 `.github/workflows/deploy.yml`（default-tip only）自動執行；`control.yml`、`operate.yml`、`oracle-teams.yml` 等其餘控制與驗證 workflow 維持手動觸發。
- 優先報告實際 P0／P1 問題與可重現依據，不因需求文字要求就宣稱完整 coverage 或 machine-readable PASS。官方原生審查輸出不等於本文定義的自動合併授權；沒有 findings 也不能由 controller 自動視為通過。

## Development Commands

從 project root 使用隔離 Python。依賴下載／系統與瀏覽器安裝需對應授權；本機已備妥的 `.tools/activate` 選用 `.venv312`、Node、Codex、Chromium、FFmpeg。

```bash
python -m pip install '.[bridge-test]' 'build==1.3.0'
python -m build --outdir /tmp/teams-transcription-dist
python -m teams_transcription --help

PYTHONPATH=tests:. python -m unittest test_assistant_store test_assistant_daemon test_calendar_client -v
PYTHONPATH=tests:. python -m unittest test_glossary_context test_sqlite_voice_context test_summary -v
PYTHONPATH=tests:. python -m unittest test_admin_web test_admin_browser -v
python -m unittest discover -s tests -v
```

`build` 的隔離建置可能下載依賴。沒有設定 lint、formatter、type-check 或 coverage 命令；不要順手引入新工具。

有核准設定與執行授權後：

```bash
teams-transcription init-admin --config /absolute/private/runtime.yml
teams-transcription doctor --config /absolute/private/runtime.yml
teams-transcription serve --config /absolute/private/runtime.yml
```

`doctor` 只查本機工具／檔案 metadata；不啟動瀏覽器、不讀取 token 內容、不呼叫 model。`probe-live`、`probe-media`、`probe-teams` 是需另行授權的明確探測，不是可自動執行的離線檢查。

## Code Conventions & Common Patterns

- Python 四空格、`snake_case`、`PascalCase`；tests 為 `test_*.py`／`test_*`，沿用周邊引號及排版。
- **模型原生主導，提問流程例外（操作者已核准）：** 回答內容與原生語音交由 GPT Live；主程式管理喚醒、分題、每題新查詢與發聲安全，不等待 Native 每題完成／委派。不得把 application boundary 冒稱 provider final，或修改 A 的 provenance。除已明定契約外不另加內容／中止規則；短視窗時鐘波動不直接升級為整場退出。媒體節奏／背壓使用原生媒體與 WebRTC，資源邊界須透明，不冒稱模型限制，也不用無界緩衝掩蓋吞吐問題。
- Async resource 明確 `start`／`close`，追蹤 tasks、bounded queues／waits；正常停止先關入口，在同一 cleanup deadline 排空已接受 PCM 與 pending writes。保留 shielded cleanup、取消安全及第一個來源失敗；排空途中不重複取消或延長 deadline。阻塞 SQLite 操作用 `asyncio.to_thread`。
- 使用既有 constructor DI：daemon 的 store／calendar／runner／summarizer／clock，Calendar HTTP transport，provider RPC factory／clock，B reader／guard／resolver／sink。測試注入不能變成環境變數或公開的假 provider 開關。
- `MeetingStore._locked(mutating=True, kind=..., meeting_id=...)` 在來源交易內驗證 lease 的 owner、generation、token、expiry、kind 與目標 meeting。不得把 fence 移到交易外；await 後重查控制授權及停止狀態。
- `storage.Session` 的 config／status／events／stop request 都在 SQLite。mutations 回傳 bounded metadata；需要完整來源時明確 `read`。不恢復 `.md`、`.jsonl`、檔案 fallback、舊 facade 或 cursor aliases。
- 錯誤使用固定 code 與 `diagnostics.py` allowlist；保留原始失敗、另記 cleanup 結果。不能將 exception body、URL、SDP、DOM、姓名或音訊寫入 diagnostics。`rpc.safe_error` 不是持久化 allowlist 的替代品。
- fail closed：失效授權、錯誤 meeting/thread、部分 context delivery、stale lease、`stop_unknown`／unknown cleanup 都不能變成成功。terminal 不等於 provider final；工具 send-return 不等於模型消費，本機 PCM bytes 不等於遠端聽得到。
- B context 的 `allowed_egress_hosts=[]` 是合法 HTTP／WebSocket deny-all，不能誤判為缺少政策或用任意 host 繞過。官方 Live 動態媒體另需 own-start SDP proof；這個 context guard 不是 Teams／A／Codex 子程序的全域網路防火牆。
- 修改 B 音訊、分題或完成判定前讀 `docs/B-LIVE-NATIVE.md`：B 觀測 A 的單一原生混音；1.5秒實際 PCM quiet＋0.6秒文字穩定是應用 heuristic。保留單題全文、完整授權內容及無回答總時限。Native assistant done 須搭配真實解碼來源 quiet 與播放 quiet；缺樣輸出的零值不能冒充來源 quiet。新 turn 重設兩條 quiet，但完全晚到通知之前的靜音音訊不能回補。native mute 不 flush 瀏覽器媒體緩衝；本機 quiet 不是遠端 drain，schema4 不得回報 `drained=true`。撤權、停止、來源 fence 與輸入資源保護不變。
- 設定優先序：CLI → 非空且白名單內的 `ASSISTANT_` 環境 → YAML → defaults。管理密碼、session secret、Google token 路徑須在 data root；client config、Codex home、Teams storage state 使用明確絕對路徑及私有權限。寫入 atomic；不得自動改操作者配置繞過 gate。

## Important Files

- Entry／settings：`pyproject.toml`、`teams_transcription/__main__.py`、`cli.py`、`assistant_config.py`、`runtime.example.yml`、`.env.example`。
- Orchestration／UI：`daemon.py`、`assistant_store.py`、`admin_web.py`、`calendar_client.py`、`docs/CALENDAR-RUNTIME.md`。
- Source／summary：`storage.py`、`meeting_store.py`、`incremental.py`、`source_contract.py`、`summary.py`、`docs/SUMMARY-RUNTIME.md`、`docs/MEETING-STORE.md`。
- Live／詞庫：`live.py`、`rpc.py`、`glossary_context.py`、`history_context.py`、`docs/GLOSSARY-RUNTIME.md`。
- Native speech／安全：`response_runtime.py`、`b_native.py`、`b_native_output.py`、`b_browser_tx.py`、`b_no_plc.py`、`b_question.py`、`b_live.py`、`b_media_profile.py`、`b_context.py`、`runtime_guard.py`、`provider_state.py`、`docs/B-LIVE-NATIVE.md`、`docs/PROVIDER-ADMISSION.md`。
- Capture／Docker：`browser.py`、`native_media.py`、`worker.py`、`rx_contract.py`、`rx_audio_stats.py`、`docs/BOUNDED-RX.md`、`docs/TEAMS-JOIN-DIAGNOSTICS.md`、`Dockerfile`、`docker-compose.yml`、`docker/chromium-seccomp.json`。

未帶目錄的 source module 都位於 `teams_transcription/`。

## Runtime/Tooling Preferences

- **全雲端調度（使用者 2026-10-30 明確要求）：** 部署、實測調度與去敏結果取回預設用 GitHub Actions＋Oracle repo runner；不得要求小米或其他個人電腦持續在線。Cloud Codex 不直接 SSH Oracle。現有部署／診斷 workflow 已有歷史成功證據，不代表已有 Live QA workflow；新測試入口仍須固定候選 SHA、同一全域 50 次帳本、host 互斥／未知清理 interlock 與去敏 artifacts。詳見 [雲端 QA 交接](docs/QA-PASSIVE-AUDIO.md#cloud-actions-handoff)。不得因本規則擴權、略過 hook 或自動批准 Live／OAuth／部署。
- **長期並行工程規格（不限本次 A/B）：** 主動拆解依賴，讓獨立開發、review、離線測試與交付準備並行；一線受阻不停止其他獨立線。相同 branch／VM 配置／模型 session／全域帳本交由單一 owner 或共用鎖串行，避免同時寫入；不得為並行混合實驗變因。每次交付列出可並行項、真正依賴與受阻線；回報 checkpoint 不代表等待下一步指示。這是執行方法，不是新增工具權限或 blanket approval；仍遵守本輪具體確認與多代理工具限制。

- Python ≥3.11，開發／容器優先 Python 3.12；setuptools／pip。`transfer/requirements-cloud.txt` 是歷史 Python 3.12/Linux closure，不是新版本 portable lock。
- Node/npm 供 Codex 與合成 JS fixtures；不是 Bun 或 frontend build。Codex CLI 必須 **`0.159.0-alpha.3`**。Chromium 明確選用現有 executable，sandbox 不能關；FFmpeg 供本機 WAV／合成驗證。
- Codex app-server TRACE 可能包含 encoded media，`RUST_LOG` 不足以阻止。`rpc.py` 要求 `/dev/shm` 為 tmpfs，子程序退出後清除；不可加入 persistent fallback。
- 只用同一已授權部署的既有 Codex／ChatGPT OAuth；不抽取、複製其他環境 token，不加 API key／付費 provider fallback。Google Calendar readonly OAuth 是另一個需操作者完成的授權。
- Docker 非 root、readonly root filesystem、drop capabilities、no-new-privileges、明確 seccomp、私有 `/data`、tmpfs。Chromium 新 user namespace 需要 `clone/setns/unshare/chroot` syscall；不授予容器 `CAP_SYS_CHROOT`，並實測初始 namespace 的 chroot 仍被 kernel 拒絕。OAuth bind mount 為 opt-in，正常 refresh 寫回同一私有目錄；不用 privileged／`--no-sandbox`／host IPC 繞過失敗。
- Docker build 依 `README.md` 準備 `codex_npm`（公開固定 SHA512 artifacts）與 `python_wheels`（本機應用／公開依賴 wheels）兩個命名 contexts；不可傳 data root／OAuth。amd64 重驗後 offline npm 安裝，Python 從當前源碼以 `--network=none` 離線安裝；依賴變動先重建 context，不以換版本、停用 TLS 或放寬 sandbox 解決下載失敗。
- 真實 Google／會議／model／發聲、push／CI／merge／部署需相應 target-and-action 授權。保留未提交修改；不要把此套件提交至父 workspace 或自動發布。Docker 本機檢查明確指定 local daemon，不能沿用指向 NAS 的環境。

## Testing & QA

- stdlib `unittest`／`IsolatedAsyncioTestCase`；temporary SQLite、`AsyncMock`／patch、受控 RPC／browser／media。指定 sibling suites 時使用 `PYTHONPATH=tests:.`。
- 全量不是 browser-free：Node fixtures 直接執行；部分 suites 需要 sandboxed Chromium，Codex 會啟用離線 schema checks。此移轉包未附兩個舊實驗的 source，`test_bridge_integration`／`test_media_bridge` 會 import error；排除後的可用回歸不可稱完整原始 discovery。報告前提、排除項與 skips，不為了綠燈刪測試或安裝無關工具。
- 驗證 consumer-visible invariants：取消改期、同場穩定識別、跨場隔離、詞庫凍結／核准、來源修訂／重送、stop-before-create、await 後撤權、stale lease、未知清理 interlock、會後 outbox／重啟、待辦狀態與 muted output。沒有 coverage 百分比要求。
- UI 要實際操作 browser；背景工作要關閉分頁再確認。合成 fixtures 不代表真實 Teams admission、GPT Live 正確逐字稿、詞庫辨識效果、native 遠端 audibility 或實際 Codex 摘要成功。
- `python3 transfer/verify.py` 僅驗證 pristine 套件對 `TRANSFER-MANIFEST.json`，不驗證程式行為。修改／額外檔案／symlink 會使快照不同；不要重寫 provenance manifest 認證本次修改。
