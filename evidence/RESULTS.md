# 此可攜來源包的驗證界線

## 原移轉歷史證據

來源候選的 focused UI/observer/stop/frame 測試：35 項通過，1.323 秒。
這是合成 Python locator/frame 測試，沒有真人 Teams、模型、原音保存或部署。
frame 版本只在 action 尚未送出且已有明確 detached/removed/roster change 證據時，
丟棄整批 scan；原 deadline、radio checked 與 Join guard、唯一候選規則均保留。
實際 trial56 根因未知，本包沒有原 trial 資料，亦不回填舊診斷 flags。

既有 source 的 protected JS、RX/TX/security/owner/TTL 模組核對未被 frame 修正改動。
WSL2 Chromium/WebRTC、既有 provider runtime 可用性及真實 TX/RX 尚待本機與白天驗收。
此包不是全套 regression、Docker build 或真人成功證據。
原歷史 evidence/experiments 都不帶入；它們的失敗收據仍留在 Cloud 原位置，不改寫。

## Standalone 本機驗證：2026-10-14

目前實作為 Calendar／SQLite／雙 Live／一般 Codex summary outbox／登入管理頁；dot、MCP、Page 與會議 Markdown 已退役。以下是本次直接觀察，**不是完整真人端到端成功**。

- **可執行回歸：773 tests，120.266 秒，0 failures／errors／skips。** 使用全新空 `CODEX_HOME`，沒有使用 host OAuth。先排除原移轉缺少的 `test_bridge_integration.py` 與 `test_media_bridge.py`，其依賴分別是未提供的 `experiments.probe_ws_turn_bridge`／`experiments.ws_turn_bridge`；不是 full discovery 通過。
- **實際管理 UI：** sandboxed Chromium 登入、核准詞庫、啟動受控 capture、關閉分頁後背景繼續、重新開啟停止、受控摘要發布、待辦完成後登出／登入仍保留。capture／summarizer 為 constructor-injected fixture，非 Teams／模型。實際 browser 回歸亦納入上述 773 項。
- **實際 CLI：** stdin 初始化私人管理密碼 → default `serve` → loopback health／login → SIGTERM；無 Calendar 設定、無會議／模型。沒有生成會議 Markdown。
- **實際 API boundary：** temporary SQLite 的 legacy provisional＋application segment → 封存 → synthetic summary publication → B scoped context，可讀尾端來源且維持 partial／provider_final_count=0；沒有普通模型呼叫或 Markdown／JSONL。
- **真實 Codex 摘要：** 沿本機既有 ChatGPT OAuth，以一段固定合成 application segment 執行正式 `summary.generate()`，實際模型 `gpt-6.1-sol`；14.408 秒完成 schema／引用／source fence／cleanup 驗證並發布 SQLite revision 1、`provider_requirement_met=true`。完整 read 與生成結果一致，來源 provider_final_count 仍為 0，不產生 Markdown／JSONL。見 [真模型收據](standalone-codex-summary-20261014.json)。這不是 Live ASR 或真人會議的同次摘要驗收。
- **Compose config：16 checks 全通過。** fake inputs、無 service／registry／model 操作；驗證 private mount、不自動建立主機路徑、loopback override、UID/GID、readonly／drop caps、資源限制、health port、auth opt-in／正常 refresh 與設定優先序。
- **正式 Dockerfile／wheel 建置及實際容器：** 最終 immutable image `sha256:1591401055f591b4e6c0aac264143833773b27de78e9645d85de9521dfa011c4`，本機 tag `teams-assistant:standalone-local-26fb`。實際 runner 的 13 checks 全通過；詳見 [機器收據](standalone-compose-final-20261014.json)。network=none、不掛憑證、不發佈 port；真 CLI／daemon、登入／Origin、sandboxed Chromium local page、SQLite restart、Codex 版本及停止路徑均有執行。SIGTERM 0.473 秒，自己的臨時 container／volume 由 runner 清除。這不是部署、Google 或 Teams 媒體驗證。
- **詞庫 wire：** 曾由本機固定 Codex CLI 匯出 experimental schema，驗證實際 `LiveProvider.start()` 的 frozen glossary `initialItems`。這只證明 host wire／schema，不證明模型採用或 ASR 改善。

### 本次修正的已重現原因

1. 管理表單原 `Referrer-Policy:no-referrer` 令實際 Chromium POST 的 Origin 成為 null，被同源 gate 正確拒絕。改 `strict-origin`，不允許 null／foreign Origin，不移除 CSRF；實際登入通過且 referrer 不包含 query。參考 [MDN Origin](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Origin)。
2. expired capture 留下 persisted interlock，DAO 卻可 claim 不同 occurrence。before smoke 為 interlock=true／admitted=true；修正同交易檢查後 admitted=false／`capture_cleanup_review_required`。daemon 也刷新啟動後才出現的 interlock 並暫停排程。
3. `Session.entries()` 對 legacy provisional 的 ordering 與 summary publisher 不一致，導致 `invalid_result_summary`。保留 `grouped_source_streams` 後，真 SQLite → synthetic summary → B boundary 成功；永久長來源回歸通過。
4. 容器 drop ALL 與原 seccomp 的 conditional `CAP_SYS_CHROOT` 規則衝突，Chromium chroot 失敗。允許 user-namespace sandbox 所需 syscall，不授予容器 capability。修正後實際 sandboxed page 成功；同時直接非特權 chroot 仍被 kernel 以 EPERM 拒絕。沒有使用 privileged、unconfined、host IPC 或 `--no-sandbox`。

### 重跑已知可執行子集

從 project root 啟用既有 tooling。此命令明確排除上述兩個缺少原始實驗模組的 suites，其他 import／assertion 仍照常失敗；不是自動 skip 或 full-suite 宣告。

```bash
source .tools/activate
PYTHONPATH=tests:. python - <<'PY'
import os, tempfile, unittest
excluded = {'test_bridge_integration.py', 'test_media_bridge.py'}
class AvailableLoader(unittest.TestLoader):
    def _match_path(self, path, full_path, pattern):
        return path not in excluded and super()._match_path(path, full_path, pattern)
with tempfile.TemporaryDirectory() as home:
    os.environ['CODEX_HOME'] = home
    print('Excluded missing transfer prerequisites:', sorted(excluded))
    result = unittest.TextTestRunner(verbosity=2).run(AvailableLoader().discover('tests'))
    raise SystemExit(0 if result.wasSuccessful() else 1)
PY
```

### 尚未完成的真實驗收

本次受控環境沒有配置可用 Google OAuth client／readonly 授權及真實 Calendar 場次，未重新使用歷史聊天中的 Teams URL。沒有在本次證明真帳號 Calendar 輪詢、自主真人入會／A ASR、B 模型理解與遠端可聽、真人來源的會後摘要／下一次會議記憶，或固定音訊「有詞庫／無詞庫」準確率對照。一般 Codex 對合成 SQL 來源的真實摘要與持久發布已另行通過，不混稱真人端到端完成。

需操作者在**私有設定／管理頁**提供同一獲准部署的 Google client 與 Calendar 設定、完成 readonly OAuth，以及明確 media egress 配置、當次有效會議與錄音／發聲同意。本機既有 Codex OAuth 與一般模型已實測；若改以 Docker 驗收，仍須明確指定同一部署的既有 OAuth 掛載，不複製 token。不要求將 token／client secret 貼進聊天，不新增 API key／付費 fallback，不自動啟用正式服務或部署。缺前提時保留未驗證狀態，不把 fixture 填成成功。

## 真實 Google Calendar 唯讀驗證：2026-10-15

- 操作者已核准指定 Google 專案的 Calendar API、測試模式 OAuth app／專用 web client 與測試帳號；未發布正式應用或增加日曆寫入權限。
- 本機 `127.0.0.1:18081` 的真實管理登入 → Google 帳戶／同意頁 → `/google/callback` → `/settings` 已完成。舊流程曾停在「操作未完成」且未保存憑證；沒有重放舊授權碼，重新產生 state 並由操作者完成同範圍同意後成功。舊回呼的確切拒絕碼未留存，不把推測當成已定位的程式缺陷。
- client 與授權檔保存在原始碼外的私有資料根目錄；授權檔為目前 UID 擁有、非 symlink 的普通 `0600` 檔，含 access／refresh token。本機保存的 scope 為 `https://www.googleapis.com/auth/calendar.events.readonly`。未輸出任何 token／client secret，下載暫存副本已刪除。
- 經實際管理表單設定 `primary` 日曆並執行正式 daemon 的立即輪詢；Google 狀態為已授權、`calendar_errors={}`、`last_error=null`、無 active session。
- **獨立新建的正式 `GoogleCalendarClient`，從私有檔載入授權並實際讀取 API：** `2026-10-15T07:27:26.808054+00:00`，`GET www.googleapis.com/calendar/v3/calendars/primary/events` 回傳 **HTTP 200**；lookback 300 秒、lookahead 48 小時，完成分頁的活動數 **0**、有效 Teams 活動數 **0**。觀察 hook 僅記錄 method／host／path／status，不記錄 headers、query 或活動內容；client 正常關閉。這不是 fixture，也不代表已實測 token 到期更新。
- 日曆讀取已啟用，但排程仍暫停，加入／錄音與發聲授權均未勾選；SQLite 當時 `calendar_occurrences=0`、`assistant_runs=0`。本次沒有會議、Live／麥克風或語音輸出。
- 新私有配置的實際 CLI `doctor` 已執行：私有檔案、既有 Codex OAuth、固定版本、Chromium、FFmpeg 與非 root 等 checks 均通過；唯一 false check 是 `egress_policy_configured`，因此 `local_ready=false`／exit 1，符合尚未配置媒體出口的狀態。offline doctor 的外部 gates 仍標示 unverified，不取代上述實際 Google HTTP 200 證據。
- 隨後操作者明確核准：在剛授權帳號的主日曆透過網頁新增**一筆**本次測試活動，並確認此場音訊擷取、文字逐字稿保存及助理發聲同意。尚未提供當次 Teams 連結／時段，因此沒有新增活動、啟用全日曆錄音政策或入會；不能把這項單場同意擴成其他活動的同意。
- **真實 readonly Codex admission：** `2026-10-15T07:38:15.103921+00:00`，正式 `CodexProviderState.refresh()` 經固定 app-server 的 `account/read(refreshToken=false)` 成功，1.754 秒；initialize／account read／cleanup 皆 succeeded，tmpfs 暫存子項剩餘 0。結果只為 `admissible_unverified`，未呼叫模型、未取出／複製 token，不能宣稱已驗證 Live entitlement、媒體或中文辨識。正式入會時仍須重新取得 fresh admission。
- **WSL2 實際 sandboxed Chromium：** 使用既有 `.tools/bin/chromium`、Playwright `chromium_sandbox=True` 開啟本機 HTML，核對標題後查看 `chrome://sandbox`；Layer 1=Namespace、PID／Network namespaces=Yes、Seccomp-BPF=Yes，頁面顯示 adequately sandboxed，browser 正常關閉。無會議、麥克風、模型或外部頁面；不使用 `--no-sandbox`。這不代表已驗證 Teams WebRTC 音訊。
- **仍未完成：** 當次有效 Calendar／Teams 測試場次、明確 media egress、A 真實逐字稿、B 遠端可聽回覆、真人來源摘要／下次系列記憶，以及詞庫有／無對照。Google OAuth／真實唯讀 API 邊界已補驗，單場媒體同意已由操作者確認，不把這些擴稱完整端到端成功。

## 真人 Teams／A／摘要與 B 修正：2026-10-15

操作者提供當次有效連結、表示現在可測，核准最多 50 次代建活動測試。已透過 Google Calendar 網頁建立 11 筆，OAuth API 仍維持 events.readonly。只以已選定的 Calendar occurrence 手動加入，排程持續暫停；**尚未以此證明自動排程入會**。第四筆在等待操作者期間過期，未入會並在本機跳過；第四至第十次實際嘗試分別使用第五至第十一筆活動，不繞過活動時間窗。

| 輪次 | 直接觀察 | 驗證界線 |
| --- | --- | --- |
| 1 | Join dispatch completed，120 秒內未確認 admission，清理完成 | 操作者事後表示當時尚未入會等候；不能僅以這次 timeout 歸因程式 |
| 2 | audio_choice 發生 Playwright Error，尚未送出 Join，清理完成 | 該輪原始 SDK 訊息未保存，不能以後續非入會重現結果回填 |
| 3 | 操作者確認已准入；正式 A 為 recording／RX ready，Live connected，收到非零 PCM | SQLite 保存 16 筆 user application segments，provider final=0，品質為 unfinalized；不是 16 筆定稿 |
| 4 | 操作者確認已在會議內後開始；audio_choice 發生 Playwright Error，尚未送出 Join，清理完成 | prior_json 已凍結第三輪的真實摘要 revision 1；只證明 host 記憶快照綁定，不代表 B 已理解 |
| 5 | 先通知、操作者確認後入會；A recording／RX ready，17 段 user application segments，provider final=0 | B 仍 sender_select／eligible=0／recvonly；指定測試句關鍵詞未找到，不宣稱辨識正確或遠端可聽 |
| 6 | 操作者確認已在場後入會；A RX ready，B response_ready／eligible sender=1，發聲已授權 | 操作者已說但沒聽到回答；A 保存 25 段未定稿，含喚醒詞但無完整指定句，不能推定操作者未說或 B 的接收／委派結果 |
| 7 | A RX ready；B 缺 clock=0、實際接受及 peer enqueue 封包持續增加。操作者回報有聽到，並確認「有叫小白小白有回應，沒帶就無回應，這是我要的」 | 真人遠端可聽與該輪喚醒門控已確認；後端多次回應後仍進入 response_failed，不能宣稱持續穩定或回答內容／歷史記憶均已驗收 |
| 8 | 操作者確認後入會；問候及兩題後曾觀察 B sleeping／error=null、handoff admitted=2、tool_calls=2、speech closed／drained=true。操作者回報都有回答，但表示只看到部分記錄 | 有回答不等於正確讀到歷史；停止前保存快照另為 response_failed／native_error，沒有精確首因，不將其回填為單純停止競態或 context budget 耗盡 |

第三輪經原管理 stop 控制停止，狀態 stopped／cleanup=cleaned。正式 summary outbox job 已 published、error=null；一般 Codex 實際 model=`gpt-6.1-sol`，SQLite 摘要 revision 1、format=json-v1、source_count=16、provider_requirement_met=true。摘要保留未定稿來源品質及引用，不宣稱完整定稿或 B 已聽見；實際來源與摘要可由登入歷史頁核對。

### 本輪已定位及修正

- **空政策誤判：** `ProductionResponseController.start()` 把顯式 `allowed_egress_hosts=[]` 當成缺少政策，與底層 guard 的 deny-all 定義矛盾。改為拒絕缺少 key，仍由原 guard 驗證型別／內容、拒絕 HTTP／WebSocket 與未證明媒體；未加入任意 host。新增實際 worker／Chromium 回歸，before 無法進入 B ready，after B 先靜音且真實 browser request 被 blocked-by-client 拒絕。
- **B 無輸出路徑：** 第三輪 response_unavailable 的首因為 sender_select；兩個舊 peer closed，唯一 active 音訊 transceiver 的 sender.track=null、direction/currentDirection=recvonly，eligible_count=0。B 模型尚未啟動，不能把這個失敗當成模型語音能力不足。
- **owned 音訊輸入：** 新增 `OWNED_AUDIO_INPUT_JS`，只在 response mode、精確 Teams origins 於 document init 提供真正 WebAudio 音訊 track；預設 disabled、每次取得獨立 clone，保留 audiooutput 列舉，拒絕 camera／不符的 exact device。沒有實體麥克風 permission、Chrome fake-device flags、PulseAudio、SDP 修改或 sender guard 放寬。實際 Teams 入會前 smoke 回報 owned input available／track live／disabled、video tracks=0、physical permission=false，且**沒有呼叫 Join**。
- 本機正常 offer／answer 已產生可被原 `response_sender()` 選取的 sending sender；獨立唯讀 review 未見 adapter blocking issue。**真實 Teams 是否協商 sending、B 模型是否啟動及遠端是否可聽，仍未通過。**

相關 60 項測試首輪 59 通過，1 個舊 guard_construct fixture 將 `[]` 錯當缺省；改為真正缺少 key 後，該 5 項 suite 全通過。另 91 項 capture／lease／frame／deadline／worker／post-join 隱私回歸全部通過，15.430 秒。這不是重新執行完整 discovery。暫時 SDK debug hook 已移除。

操作者要求：**每輪先通知，確認已在會議內後才送出加入，不無聲重試。** 第五輪已透過原管理控制停止，state=stopped／cleanup=cleaned；普通 Codex `gpt-6.1-sol` 已發布該輪摘要 revision 1，job=published／error=null。該輪啟動時 prior_json available=true／revision=1／series 匹配；這是前次記憶 host 快照，不代表 B 理解。等待時排程暫停、無 active run／capture interlock，Calendar 錄音／發聲 flags 已撤回。2026-10-14 Docker 映像尚未包含本次修正，不擴稱當前最終映像或完整端到端完成。

### 入會前 frame 重建缺口：直接重現及修正

- 非入會探針使用正式 `MeetingBrowser`／沙箱 Chromium 與當次連結，硬性攔截 Join。捕捉到 `_response_preview_navigation()` 的 locator count 拋出 Playwright `Frame was detached`；當時舊 child frame 已 detached，Join 未派送。只記錄固定錯誤分類／自有 source 位置，不保存 SDK 原文或會議頁面內容。這直接定位同一預覽路徑的缺口，不能回填第二／四輪未保存的原始例外。
- 原主要 radio scan 已可重驗 frame 快照；預覽掃描的 SDK 讀取錯誤卻直接逸出。修正為：預覽動作未派送時重新執行原 read guard，僅對已證明失效的 frame／roster 丟棄整批 scan；活 frame 的錯誤仍失敗，所有既有 deadline、停止、唯一 radio、readback 與不重按 Join 保護保留。
- 新回歸 before 在 frame 替換情境失敗，after 通過；同時驗證未替換 frame 的 SDK 錯誤仍不送出 radio／Join。相關 frame／preview／dispatch／observation／wait／UI／join 診斷 **60 tests 全通過，7.580 秒**，包含不確定預覽 click 不重試。
- 修正後再次執行正式真實 Teams 入會前 smoke：`prejoinCompleted=true`、`joinDispatched=false`，可見 3 個 audio radios；context／browser 正常關閉。這不是新一輪實際入會，也不證明 B 遠端可聽。

### Owned input 與 Teams DSP 約束：直接重現及修正

- 第五輪實際 B 的 startup_error phase=sender_select，eligible_count=0。後續不入會探針確認：Teams 的 microphone switch 為原生 checked=true（不是 aria-checked），已選到 owned device；audio-only getUserMedia 呼叫一次，返回 track=0，發生 OverconstrainedError。原先「強制關麥」假說未被此探針支持。
- Teams 要求 autoGainControl／echoCancellation／noiseSuppression=false，另含關閉的 legacy goog processing preferences；不是要求實體裝置或 camera。將同一請求帶入本機沙箱測試，before 直接得到 `OverconstrainedError: Cannot satisfy constraints`。根因為將 capture-device processing constraints 套在 WebAudio track，而非缺少虛擬裝置。
- 修正 `OWNED_AUDIO_INPUT_JS` 的 getUserMedia 邊界：來源本來就無麥克風 DSP，settings／capabilities 明確反映三項固定關閉；在此驗證 processing 要求，拒絕 exact=true，非強制偏好不冒充已啟用。其他 format／processing 約束仍經 native API；不吞原生錯誤、不 grant permission、不改 SDP／sender guard。
- **15 tests 全通過，17.970 秒**：7 項 owned-input 實際沙箱媒體測試與 8 項 response runtime 測試；包含原請求成功、exact=false／unsupported exact=true、optional preferences、獨立 track 停止、正常 offer/answer、預設靜音、TX 還原及失敗隔離。
- 修正後真實 Teams **非入會**同一路徑：getUserMedia calls=1／returned=1／owned=1／live=1／disabled=1、errors=[]，3 秒後 track 仍 live／enabled=false，microphone permission=denied，Join 未派送。browser/context 已關閉，臨時 screenshot 已移除。
- 更新服務 `calendar-live-audio6` 已啟動並經正常管理登入確認 started=true、無 active run／capture interlock，錄音／發聲 flags=false。下一輪實際 Teams sending／B 模型與遠端可聽仍待操作者確認後驗證；此非入會結果不能替代它。

### 第六輪無回覆與有界診斷：2026-10-15

- 第六輪已由管理控制停止，state=stopped／end_reason=operator_stop／cleanup=cleaned；一般 Codex `gpt-6.1-sol` 已發布 SQLite 摘要 revision 1，job=published／error=null。入會、A 保存及摘要不是 B 遠端可聽成功。
- 舊狀態只保存 B startup／授權，未提供執行中 RX／wake／handoff／speech 快照。兩個 own app-server 的 RAM TRACE 可見持續 input/output audio 事件，但只保留近期重複資料；不能用當時無 handoff 字樣回填之前從未發生委派，也不能以 A provider final=0 推定 B 無 done。
- 補上 `response_progress_diagnostic()`、原生 RX／scoped transcript 計數、worker 缺 clock／fanout 接受與拒絕計數；正常管理 API／總覽提供即時 B status。既有 snapshot cadence 與首次停止前保存 `response_status`／`response_snapshot_at`，歷史頁可查；診斷不改錄音、wake、委派、發聲授權或 provider-final 邊界。
- **59 tests 全通過，18.590 秒**，涵蓋 privacy／native wake與context／handoff／worker／daemon／管理授權。新增測試驗證部分輸入不喚醒、失敗 handoff 可區分、敏感文字不進狀態，以及惡意型別／超大計數不洩漏或冒充完成。
- **實際受控 UI smoke：** 空 CODEX_HOME、無 Calendar／模型／會議／硬體；正常 browser 登入 → API navigation → 即時 B 面板 → 停止 → 已保存歷史快照。缺 clock 與 handoff rejection 可見，受控 private-text marker 不可見；browser 已釋放。這是 constructor-injected capture，不是真人數據。頁面 CSP 阻擋自訂 fetch，改正常同源導覽，未放寬 CSP。臨時 launcher 在 Ctrl+C 後 app shutdown complete，但最外層未捕捉 KeyboardInterrupt、exit=1；不是 production CLI 退出失敗，scaffold／資料／自有 RAM 目錄已刪除。

### 真實 Native B 合成中文控制組

- 使用已安裝的離線 `Microsoft Hanhan Desktop` zh-TW，合成公開固定問題「小白小白，我們剛才決定先完成哪項測試？」；4.983 秒、24 kHz mono PCM。沒有實體麥克風、Teams 入會或遠端發聲，Windows WAV 與 RAM 音訊／SQLite 探針資料已移除。
- 同一既有 Codex OAuth、固定 CLI、正式 `NativeLiveResponder`／RuntimeLiveGuard deny-all＋own-start SDP proof；關閉 synthetic text gate，沒有 appendText、手動 question-stage admission、text-answerer 或付費 fallback。SQLite 只有明確標示的受控決策資料；音訊走既有 direct peer push，**刻意不涵蓋 worker clock/fanout 或 Teams TX**。
- 實際 user delta=14／done=1、assistant delta=13／done=1、handoff received=1／admitted=1／rejected=0，tool_calls=1；原生回答包含僅在受控 SQLite 資料提供的 Teams 項目。speech phase=closed／done=true／error=null、activity_frames=145408；output disabled，因此不宣稱實際可聽或 sink drained。peer connected／data channel open；正常 stop=stopped／cleanup=cleaned。
- 此控制組證明本次合成情境下，B 能完成中文喚醒、SQLite 工具讀取及原生語音生成；**沒有修復或通過第六輪真人無聲問題**。下一輪先核對真實 fanout／B RX，再要求操作者說測試句，依新快照定位接收、委派或 Teams TX，不猜改 partial/done 規則。
- 新服務 `calendar-live-diagnostic7` 已載入診斷；真實管理登入確認 started=true、response status 欄位存在、無 active run／interlock，排程暫停、Calendar 錄音／發聲 flags=false。仍須先通知並取得新一輪在場確認，不能自行重入會。

### 第七輪真人語音／門控與問候後失效修正

- 新診斷在要求操作者發言前確認 B accepted=162／peer enqueue=162、缺 clock=0、B dropped=0；此為本機接收／排入，不冒充 provider consumption。後續有 user／assistant done、handoff admission 與 SQLite 工具讀取，操作者直接確認 Teams 端可聽，且無喚醒時無回應。未把這個成功回填為第六輪根因已知或已通過。
- 本輪已正常停止／cleanup=cleaned。SQLite 保存 **85 段未定稿 user application segments、provider final=0**；一般 Codex `gpt-6.1-sol` 已發布 revision 1，summary job=published／error=null。停止前快照為 B failed、tool_calls=3、handoff received=8／admitted=3／rejected=5、缺 clock=0；錯誤在舊快照只有 native_error，不能回填精確首因。
- **獨立重現後續失效：** 同一離線 zh-TW「小白小白」PCM，走正式 Native B、輸出 muted。before user done=1／assistant done=1，但 first failure=`greeting_text_mismatch`；模型已自然問候，host 卻因文案不逐字相同而拒絕。還觀察到 error 存在時狀態被錯誤發布為 listening。這直接定位可重現缺陷，不宣稱舊真人快照已保存相同錯誤訊息。
- 移除正式問候的固定文案比對；不改 complete wake、未喚醒靜音、own-thread、handoff／source scope 或發聲授權。正常完成仍需 assistant done＋有效音訊／quiet tail／drain。錯誤不再重開 idle，第一個失敗不被後續收尾錯誤覆蓋；每段新 speech 清除上一段 drain，避免把先前成功誤報為本次完成。明確的 synthetic muted text gate 不變。
- 新行為回歸 before 失敗，after 通過：自然問候後仍能讀取及回答下一題；另涵蓋首因保存、error 不重開 listening、上一段 drain 不得污染新回答。**69 tests 全通過，20.672 秒**，使用空 CODEX_HOME；包含原生、schema、muted probe、context／lease／輸出及隱私邊界，不是完整 discovery。
- **同 PCM 真模型 after：** user done=1／assistant done=1，sleeping → greeting → listening，speech closed／error=null、activity_frames=24576；正常 stop=stopped／cleanup=cleaned。全程未入會、不向實體裝置發聲，不替代修正版本的真人持續問答驗收。WAV、RAM store／音訊與空測試 home 已清除。
- 服務已切至 `calendar-live-greeting8` 並正常登入確認 started=true、無 active run／interlock，排程暫停、錄音／發聲 flags=false。操作者已確認的喚醒規則保持不變；尚需修正版本持續問答、回答內容／歷史記憶、詞庫對照與最終 Docker 映像驗證。

### 第八輪歷史記憶預算裁切：實際來源與真模型 before／after

- 本輪由管理 stop 正常停止，state=stopped／end_reason=operator_stop／cleanup=cleaned；已撤回 Calendar 錄音及發聲 flags，排程暫停。SQLite 保存 **62 段 user application segments、provider finals=2**，不將兩類相加或稱所有段落已定稿。普通 Codex summary job=published／error=null／attempts=1。
- 操作者停止後再次呼叫沒有回應時，已確認無 active run；此時不是待命狀態，不自行重入會。此前保存的 B failed 快照仍保留，未將無精確原因的失敗標成正常。
- 前次凍結記憶 available=true、1 項未完成待辦、未截斷。實際讀取包含 62 筆本場來源、3 筆前次摘要與 1 筆待辦。原工具選到待辦，但按 record_index 恢復時間序，增量 wire 從尾端裁切後只剩本場來源；歷史待辦不在送出 payload。
- 移除重新按來源位置排序後，原問題的 wire 已保留待辦；但另一句自然問法的真模型控制組仍只選到 9 筆本場來源、未包含待辦，回答也未包含待辦原文。這證明只移除排序仍不足，不能以單一問題通過稱為修復。
- 最終選取保留明確 literal／跨 part 命中，其他記錄依待辦、本場來源、前次摘要輪流涵蓋，各自保留相關度；原始 locator／record_index 不變，優先序延續到 wire 裁切。不提高 byte budget，不抹除 truncated／partial。
- 新回歸以大量重複問題逐字稿擠壓較晚的歷史待辦／決策，before 缺失待辦而失敗；after 保留待辦、決策及本場來源並符合 wire 上限。**68 tests 全通過，0.337 秒**：incremental minutes、B tool、boundary／long context、SQLite voice context、native incremental context；不是完整 discovery。
- **同一真實 SQLite 場次的真模型 after：** 使用離線 zh-TW 合成問題「小白小白，上次會議未完成待辦的原文是什麼？請念出那項待辦。」（7.827 秒），正式 Native B／預設 gate、同一既有 OAuth，物理輸出 disabled。實際 payload 包含待辦 1、前次摘要 3、本場來源 4；assistant done=1、handoff admitted=1、tool_calls=1、speech closed／error=null、activity_frames=497664。僅在記憶體比對答案，確認包含保存的完整待辦原文；答案仍提示部分資料，未假稱完整。正常 stop=stopped／cleanup=cleaned。
- 此控制組直接 peer push，**不涵蓋真人 ASR、worker fanout、Teams TX 或修正版遠端內容驗收**。未輸出待辦／逐字稿原文；Windows WAV 與自有 RAM 暫存已移除。服務已載入記憶修正為 `calendar-live-memory9`；真人持續問答／歷史語意、詞庫對照、自主排程與最新 Docker 映像仍未完成。

### 第九輪後續失效與取消本機累計上限

- 操作者確認能聽到完成狀態，但後續喚醒無回應、Teams 仍在線。**停止前**管理 API 證明同一 active run 的 B failed／response_failed／output_enabled=false，handoff admitted=4、tool_calls=3、clock_missing=0，speech closed／drained=true。A／Teams 在線不能作 B 正常待命的證明；當時未主動停止，不能再以 operator stop 解釋此次無回應。舊投影仍只存 native_error，無精確首因。
- 將同場 SQLite snapshot 凍結後，以正式 context 連續讀取直接重現：前三次送出 11,615／10,915／9,851 bytes，累計 32,381／32,768；第四次拋出 context_budget_exhausted_rebuild_thread_required。這定位可重現且符合該輪計數的本機容量缺陷，不冒充已從舊 live memory 取回原始例外。
- 已先告知操作者 B 失效及即將停止，再透過管理 stop 收尾；run=stopped／end_reason=operator_stop／error=null、capture／B cleanup=cleaned、summary job=published／error=null。已撤回該場錄音／發聲政策，排程暫停。
- 操作者明確要求取消 32K 限制。移除 IncrementalMinutesContext 的累計容量參數／gate、保守 quota charging、耗盡分支與對應舊測試；未採用的換 thread 實作／測試已撤除。保留單次 payload／原生 500-byte chunks、RAM cache／音訊佇列、來源隔離、commit／版本與靜音保護。sent_bytes 只觀察成功返回量，total_limit_bytes=null，不再以本機累計量停用 B。
- 同 thread 連續五題的 native 回歸 before 在舊門檻進入 failed，after 通過；保留過大單次 metadata 仍失敗的測試。**145 tests 通過，25.739 秒，空 CODEX_HOME**，涵蓋 native/live、incremental／SQLite／boundary／long context、回覆 runtime／startup／sender、daemon、QA／muted gate 及診斷隱私；不是完整 discovery。
- **真實 GPT Live 靜音 smoke：** 同一既有 OAuth、正式 gate、同場實際保存來源、同一 physical thread；五題均完成且答案均包含保存待辦原文，累計 send-return bytes 依序 11,989／22,941／34,286／45,921／57,144。第五題後再以純「小白小白」喚醒成功進入 listening；tool_calls=5、context_failure=null、native_error=null，正常 stop／cleanup=stopped／cleaned，程序 exit=0。沒有改大上限、沒有重設／更換 thread、沒有入 Teams 或物理播放；直接 peer push，不涵蓋真人 ASR／worker fanout／Teams TX。取消本機上限不代表供應商容量無限。
- 離線 TTS 的 Windows WAV、自有 RAM runtime 與 retained-kernel PCM／控制腳本已清除。服務更新為 `calendar-live-unlimited10`，正常管理登入／API 確認 started=true、無 active run／interlock、last_error=null，所有 Calendar 錄音／發聲 flags=false、排程暫停。修正版真人連續回答仍需先通知並取得新的在場確認；不自行重入會，未新建 Docker 映像。

### 第十輪持久結果與中斷恢復：2026-10-16

第十輪使用第 11 筆活動及原系列，只送出一次 Join。後續 live API 曾觀察 B listening／error=null、handoff admitted=5／rejected=0、tool_calls=5、累計 56,508 bytes、speech closed／drained=true。操作者仍回報不知道上次記錄，因此不能把傳輸成功當成歷史語意驗收。再續查前服務已退出且無退出碼；最後持久快照為 B sleeping／error=null、tool_calls=7、累計 79,992 bytes、clock_missing=0。這直接證明本場已跨舊門檻，不回填成所有回答內容／遠端持續穩定均已通過。

- 原庫有 hot rollback journal，readonly open 失敗。先持 shared store lock，確認檔案 metadata 穩定，僅複製 SQLite＋journal 至私有 tmpfs 做恢復診斷；原庫當時未改。恢復副本顯示 run 仍 running、capture recording、沒有 summary job，不能說已正常停止。
- 經正式 daemon entrypoint 啟動，SQLite 正常恢復原庫；expired lease recovery 將 run 標成 interrupted／service_interrupted、封存來源並建立 outbox。實際一般 Codex `gpt-6.1-sol` 已發布恢復摘要，provider_requirement_met=true；沒有重新入會，也沒有將未知清理改成 cleaned。
- 正常管理登入確認 no active run、capture_interlock=true、last_error=capture_cleanup_review_required；撤回過期 Calendar 錄音／發聲 flags、排程暫停。原 journal 已消失，普通 readonly 查詢恢復。RAM 副本已刪除。服務退出的直接原因沒有退出碼／堆疊，仍未知。

### 歷史已載入但空分類／查詢不足的語意區分

- B 的 RAM 啟動紀錄已找到 historical_meeting_memory 標記、喚醒規則與保存待辦；主程式 `live_start_params()` 明確把歷史放入 initialItems。這不是「模型消費全部資料」的證明。官方 [startup-context toggle](https://github.com/openai/codex/commit/d5b4b9837017c0fde8c4738968ae2471723989f6) 說明 includeStartupContext=false 排除 Codex 自動產生的 startup context；沒有為修正歷史而打開 workspace context。
- 本輪凍結的是較早 occurrence 的最新合格摘要：重點 1、決策 0、摘要待辦 0、未決問題 3，另有系列未完成待辦 1，未截斷；不是完整前次逐字稿。空分類不能推論沒有摘要，系列延續待辦也不能冒充上次新增。
- 新增 history_status，獨立於查詢層 available：包含摘要存在、分類數、系列待辦數、截斷與是否包含前次逐字稿。通過 tool 及增量 wire，Native 指令要求區分「無資料可引用」與「無前次摘要」。沒有提高或恢復本機累計容量門檻，也沒有擴大到其他系列／任意會議。
- 新回歸 before 因無 history_status 失敗；after 在無 selected refs 時仍能區分不存在的歷史與已保存的空摘要。**156 tests 通過，24.368 秒，空 CODEX_HOME**，含 scoped SQLite／native／incremental、版本／來源隔離、喚醒／靜音、worker、daemon、摘要儲存及 QA；不是完整 discovery。
- 真實 Native 靜音控制：受控空摘要 wire 為 summary_available=true／四分類皆 0／selected facts=false，模型完整回答「有保存摘要，但沒有記錄決策／重點／待辦／未決問題」，正常 stop／cleanup。最初控制曾只觀察到文字片段，不據此判定整句；另以有界 notification 時序追蹤觀察完整 assistant done。
- 實際第十輪保存資料的泛問控制未逐字命中待辦，不列為內容驗收；另以單句詢問系列待辦原文，確認 admitted query 含原文、wire 含保存待辦、answering 階段輸出含完整待辦原文，speech 完成／error=null、正常 stop／cleanup。全程同一已授權 OAuth、正式 gate、離線 zh-TW TTS、輸出 disabled、未入會；不代表真人 ASR／Teams TX 或原現場否認歷史的唯一根因已被確定。未輸出私人待辦／回答原文，自有 WAV／RAM／PCM／控制腳本已清除。

### 目前來源的 Docker 候選映像

本機 Unix Docker 29.6.2 建置 `teams-assistant:local-history-status-20261016`，immutable image=`sha256:de0671de83d2bb65bf7876e323789852bf27f81bce7f97c4cfe3f132156fc95c`。嚴格 build-context allowlist 排除私有設定與憑證；沒有 push／部署。

實際 Compose smoke **15 checks 全通過**：network=none、無 host ports／OAuth／Docker socket 掛載；正常 daemon／管理登入及同源保護、MCP 路徑不存在、空歷史摘要語意、同綁定 context 超過舊 32K、非 root／readonly／drop caps、初始 namespace chroot 被 kernel 拒絕、sandboxed Chromium 本機頁、固定 Codex、SQLite 重啟持久性及無會議 Markdown／WAV。SIGTERM 0.432 秒，自己的 volume／container 已清除。受控 context send receipts 不代表實際模型消費；此容器沒有會議或模型。

收據：[standalone-compose-current-history-20261016.json](standalone-compose-current-history-20261016.json)。此映像代表當時歷史狀態／無累計上限修正，不含後續清理審查；最新候選見下節。真人歷史理解／端到端、詞庫有無對照及自主排程仍未全數驗收；不是正式部署或最終完成宣告。

### 可稽核的中斷清理審查：2026-10-16

- 新增受控管理頁／store／daemon 接線。只有終態且來源已封存、無 capture 租約、PID／程序啟動簽章證明 worker 不在，並由操作者明確確認資源停止及助理離會，才能在同一 SQLite 交易保存審查與 audit。stamp 過期、runtime 後改、存活／未知 worker、缺少確認、其他未審查場次均保持 fail closed。人工確認不改寫原失敗、cleanup unknown、來源或摘要；不恢復排程、不入會、不啟用輸出。
- 獨立 backend 安全審查未報告阻斷問題；獨立 route/UI 審查發現非終態且 worker 不在時仍出現可提交表單。新增 consumer-visible regression，修正前失敗；改用共用 TERMINAL 與 ended_at gate 後通過，非終態只顯示等待封存。
- 第一輪可用回歸 808 tests 有 19 errors：新 fixture 未傳入 claim_run 已凍結的 prior_context，觸發 meeting_exists_conflict；修正 fixture，沒有放寬 production 衝突檢查。另 1 failure 是既有超時測試綁定私有 RPC next_id；刪除該 incidental assertion，保留 timeout=unknown、失敗終態、子程序回收、來源保留與遲到寫入被 fence 阻擋等行為斷言。
- focused cleanup／admin／provisional suites **56 tests 通過，5.643 秒**。首次重跑的自製 runner 選錯 Codex PATH，造成 12 個 native contract errors；未修改 production gate。改用既有 `.tools/activate`、空 CODEX_HOME 後，**809 tests 全通過，129.657 秒、skips=0**。原移轉缺少的 `test_bridge_integration.py`、`test_media_bridge.py` 仍明確排除；不宣稱完整原始套件通過。
- 實際 sandboxed Chromium 操作獨立 tmpfs fixture 管理頁：登入、查看 interrupted 歷史、觀察兩個 checkbox 均未預選，僅在自有 fixture 勾選並送出。確認表單消失、顯示審查時間／admin、audit 新增 capture_cleanup_reviewed、capture_interlock=false、active_session_id=null、scheduler 仍暫停；原 interrupted／service_interrupted 及來源 unavailable 保留。無 Google、模型或 Teams 呼叫，fixture server／browser 已停止。
- 真實本機服務更新為 `calendar-live-cleanup-review13`，正常管理登入確認 started=true、無 active run、capture_interlock=true／capture_cleanup_review_required。真實歷史頁已有清理確認入口，**未勾選、未提交**；排程仍暫停，Calendar 錄音／發聲未勾選。操作者尚未親自確認舊資源停止／助理離會，不能代簽或自行重入會。

清理審查版本另已建置 `teams-assistant:local-cleanup-review-20261016`，immutable image=`sha256:e0da0a4840c1135494da5a61f065d3f3d6771597cfe37a608c441bf20cce38a1`。同一本機 Unix daemon 的 **15-check Compose smoke 全通過**，SIGTERM 0.476 秒；network=none、無憑證／host ports／Docker socket，自己的 volume／container 已清除。此 smoke 不呼叫 Google／模型／會議；清理審查的實際表單行為另由上列 tmpfs Chromium smoke 證明，不混稱容器真人驗收。收據：[standalone-compose-current-cleanup-20261016.json](standalone-compose-current-cleanup-20261016.json)。

### 操作者確認離會後的實際清理審查

操作者回覆「已退出，你可確認」後，正式服務仍為 started=true、無 active run、capture_interlock=true。第十輪歷史頁確認舊 worker 不在；本機程序盤點未找到舊 Playwright capture browser 或綁定此 runtime 的 FFmpeg／app-server。由實際 Chromium 登入管理頁，確認兩欄原先未勾選，依此次授權勾選並提交一次清理審查；未呼叫 Join、未恢復排程或授權媒體。

提交後獨立正常 HTTP/API 讀回：capture_interlock=false、active_session_id=null、last_error=null；歷史頁確認表單消失、顯示人工清理時間，audit 有 capture_cleanup_reviewed；原 interrupted／service_interrupted 保留。排程仍暫停，Calendar 錄音／發聲勾選數為 0。瀏覽器 hidden-selector wait 雖錯誤逾時，沒有重播 POST；以實際 form count=0、API 與 audit 確認效果，並回報工具問題。自有管理瀏覽器已關閉。此結果僅是清理確認完成，不是下一輪真人入會或歷史問答驗收。

### 第十一輪重新入會與歷史快照

操作者在前輪清理完成後回覆「原連結，我已入會」。舊 Calendar CDP session 已關閉；僅重開原本已授權的 Windows Edge 專用 profile，沒有複製 cookies／token 或建立新登入。透過 Google Calendar 實際網頁建立第 12 筆活動，使用原 Teams 連結及當下 Asia/Taipei 一小時時段。正常管理表單暫時開啟本次測試媒體政策、立即輪詢、明確指派原系列，再對新 occurrence 提交一次 Join；排程始終暫停，沒有無聲重試。

正式 API／SQLite 觀察 capture=recording、phase=rx_capture；B startup=ready、state=sleeping、error=null，accepted=752／peer_push=752／dropped=0，尚無 handoff 或 speech，輸出待命為 muted。本輪 host 凍結前次摘要 available=true、同系列、重點 3／未決問題 5／決策 0／摘要待辦 0、系列未完成待辦 1，truncated=false；詞庫 entries=0。這證明場次綁定與實際 RX，不證明模型已理解歷史或 Teams 遠端已聽到回答。

已請操作者說「小白小白，請念出系列未完成待辦的原文。」待核對本輪工具調用、實際原文命中、語音完成及操作者遠端確認。第十一輪仍在執行；不得把第十輪的人工清理確認／中斷摘要當成本輪收尾結果，也不得聲稱本輪已完成詞庫效果驗收。

### 第十一輪「只聽到部分紀錄」：完成邊界重現與修正

- 操作者實際回報「有聽到，但沒說待辦內容，只說只有部分內容」，本輪內容驗收失敗。當時同一 active run 的 B sleeping／error=null、user done=1／assistant done=1、handoff admitted=1、tool_calls=1、host sent_bytes=11,351、speech closed／drained=true。這些狀態沒有證明回答內容正確或後半段已播出。
- 已通知後透過正常 stop 收尾；run=stopped／operator_stop／error=null、cleanup=cleaned，撤回 Calendar 錄音／發聲。普通 Codex `gpt-6.1-sol` 已發布 revision 1，provider_requirement_met=true，來源品質仍 partial。沒有重新入會。
- 相同凍結 SQLite／zh-TW 合成問題的正式 Native 靜音重播，確認待辦可在選取／wire 中，也有實際逐字命中的成功控制；但另有首段僅 9 或 4 字就被本機標成 sleeping，之後仍收到至少 32 個明顯語音封包，當時已無 active output lease。舊短觀察窗沒有等完後續 turn，因此不把短首段當成模型完整答案。這直接重現「首段完成 → 關閉閘門 → 後段被拒絕」缺陷，不能唯一回填為原現場對話的完整逐字因果。
- 首個永久 consumer regression 在修正前為 sink writes=1／expected=2，修正後可交付後段代表 PCM。完成改為本場 raw `turn.created`／`turn.done` 的 assistant turn ID；plain RPC assistant transcript/done 沒有 turn ID，不再拿來完成另一段媒體。A 的來源保存與 canonical delegation 讀取不改。
- 同一 admitted handoff 的連續 assistant turns 可在原回答範圍內重開受控 lease；新 user turn／新喚醒、取消、錯誤、停止及原 45 秒絕對期限都撤銷能力。後續段不重設期限；沒有問題授權或只收到任意 PCM 不會開輸出。新 turn 會使舊 done 失效，包含 quiet wait、drain lock 等待，以及 matching done 比新 lease 早到的交錯。
- 官方固定版本 [start schema](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/app-server-protocol/src/protocol/v2/realtime.rs#L197-L206) 與本機匯出 schema 均確認 `delegationAckFiller` 為可選 boolean；正式明確設 false。單獨關此開關仍有模型自行產生過場語的失敗，未將它冒充完整修復。回答契約另要求先回答有來源內容、再說明限制，要求原文時不改寫。
- 兩位 fresh read-only reviewers 找到 drain 等待 silent-write lock、取消 startup 卡在 answering、期限跨越 startup 後漏清理等邊界。三個獨立 consumer regression 均先失敗，再修正通過（0.233 秒）；focused 重讀均無剩餘具體 P1/P2。新原始 user final 遲到也曾誤當新喚醒、撤銷自己的後段；已以失敗前／通過後 regression 修正，真正新發言仍撤權。
- 完整資料及 `RESULT_COMPLETE` 的 send 返回之前保持 TX 關閉。新 consumer test 在修正前觀察到 marker 未返回、sink 已 start=1；修正後不會提早授權。刪除舊 cancellation test 對此不安全中間時序的 incidental assertion，保留取消後不 commit、輸出撤權及靜音驗證；沒有放寬 production gate。
- 一次 completion 修正後的正式 Native 靜音 smoke，完成回答含保存待辦全文，無 native error、無觀察到關閉後的大音量封包，正常 stop／cleanup。其後同 thread 三題仍能持續調用工具並使用 cached task refs；較早控制的第 2／3 題逐字命中，但首題有改寫，沒有把三題全部算過。最後來源控制的三題逐字比對均未通過，仍無 native error 或觀察到未交付的大音量尾段。這是程式後段交付修正與逐字內容品質的區別；待辦本身亦含「未明確」註記，不能冒充完整可執行清單。
- 822 tests（132.105 秒）及 marker 修正後 823 tests（136.786 秒）的可用離線回歸均通過，兩個原移轉缺少的 bridge suites 仍排除。後續 original-user-final regression 與最終來源的全量結果另記，不沿用較早版本宣稱全數完成。LSP 有既有 synthetic greeting optional／literal return／counter literal 型別診斷，未宣稱 type-clean，也未新增 lint/typecheck 工具。

最新真人遠端歷史理解／完整內容、詞庫同音訊人工基準對照及自主排程仍未驗收；修正版不得無聲重入。所有模型重播均使用同一既有 OAuth、正式 Native、物理輸出 disabled、未進 Teams；不保存真實錄音，不輸出私人任務／人名原文。

最終 source 的 **824 tests 通過，131.682 秒、skips=0**（仍明確排除兩個移轉缺少 bridge suites）。`calendar-live-native-turns14` 已載入修正，正常 API 確認 started=true／無 active run／capture_interlock=false／last_error=null；排程仍暫停、媒體授權未勾選。摘要與前輪失敗紀錄保留，沒有自動入會。自己的 replay scripts、schema、合成 WAV／PCM 與私有回答 buffers 已清除。結構化、有未通過項目的收據見 [native-turn-completion-20261016.json](native-turn-completion-20261016.json)。

### Native 完成修正版的本機候選映像

標準 Dockerfile 本輪在公開 pip 依賴下載達 600 秒命令期限而取消；Playwright 報告 47.7 MB、84.9 kB/s，並非程式測試通過。沒有重試相同慢下載或沿用舊 app code 宣稱成功。改用已驗證本機 dependency image `sha256:e0da0a4840c1135494da5a61f065d3f3d6771597cfe37a608c441bf20cce38a1`，確認 Requires-Dist 與目前 wheel 完全相同；在隔離無憑證 context 以目前 source 建 wheel，network=none／no-index／no-deps 強制重裝，並覆蓋完整目前 source，保留非 root／entrypoint／runtime defaults。

新候選 `teams-assistant:local-native-turns-20261016`，immutable ID=`sha256:3081a86eef9e28a067c64a565fb4104b2da8187c0c5dfda40f54cedab58f4cb9`。Host、installed package 與 workdir 的 `b_native.py` SHA 均為 `b6159f7910bbb8e4942f41027ff1a06429ba4ee4585d9f92e8d51389b5290a6a`，不是舊碼。離線候選建置 4.22 秒；[build 收據](native-turns-candidate-build-20261016.json) 保留標準建置失敗及 recovery 方法，不改 production Dockerfile 或 provenance manifest。

此新 immutable candidate 的 **15-check Compose smoke 全通過**，SIGTERM 0.477 秒。network=none、無 credentials／host ports／Docker socket；實際預設 daemon、管理登入／Origin gate、sandboxed Chromium 本機頁、pinned Codex、SQLite 重啟及安全限制均經觀察。自己的 container／volume 已清除。收據：[standalone-compose-current-native-turns-20261016.json](standalone-compose-current-native-turns-20261016.json)。這不是標準冷建置成功、模型／真人會議或正式部署驗收。

### 第十二輪：操作者已允許，但未入會

- 新鮮在場確認後，透過 Calendar UI 建立活動 13、明確綁原系列，正常管理 Join 只送一次。05:20:33 UTC 派送完成，05:22:23 UTC 入會逾時；操作者確認已允許卻未入會，未當成未核准。
- failure=`meeting_join_failed/timeout`，phase=`meeting_join`，cleanup=`cleaned`。A／B 尚未啟動，音訊 0、source finalized／failed／unavailable；未呼叫會後摘要。最終 fresh UI 有 prejoin＋Sign in 分類，沒有已辨識到的 Leave／lobby／明確錯誤。這不證明 mandatory login，也不足以判定 ICE、裝置或漏判根因。
- 不送 Join 的實際預覽檢查，一次完成音訊準備、一次逾時；request-refused／aborted 分類不足以唯一歸因。未擷取音訊、啟動模型或再次入會。已清除這兩次探測／本機 smoke 的暫存 script。
- 為下一輪準備僅固定狀態碼的暫存觀察接線：原 CLI／正常 daemon，既有 Join 與 deadline／取消／媒體 gate 不變；UI 每三秒最多四十筆，ICE snapshot 最多 0.25 秒且不延長原期限。sandboxed Chromium 本機合成 Join→lobby→joined 已通過、只點一次、私人 sentinel 不洩漏；不是實際 Teams 入會證據。服務 `calendar-live-admission-observer15` 已實際啟動、登入 API smoke 正常；active=null、interlock=false、排程暫停、錄音／發聲撤回。
- **沒有定位唯一根因或修改正式程式，也沒有宣稱 Native 內容驗收通過。** 需新鮮在場確認後同步觀察准入前後轉換。收據：[teams-admission-round12-20261016.json](teams-admission-round12-20261016.json)。暫存 bootstrap 位於 tmpfs，檔案 mode 0700；完成觀察即停止與移除，不當成正式修正或新 Docker 驗收。


### 第十三輪：已入會，喚醒後完全無回應

- 活動 14／實際第十三輪依新的在場確認只送一次 Join。05:43:58 UTC joined、05:44:05 receiving_ready、05:44:09 B ready／output authorized，操作者確認已入會；不是停在等候室。
- 操作者說指定待辦原文問題後回報完全無回應。05:48:31 UTC API 顯示 B failed／response_failed、輸出關閉、speech not_started／native_error；累計 handoff 10／admitted 0／rejected 10、tool 0／context bytes 0。這些是累計數，不能將十筆全部歸為該一句問題。A 當時仍 recording／rx_capture。
- 先通知後於 06:01:13 UTC 正常 stop，A 與 B cleanup=cleaned、無 interlock，媒體政策已撤回。來源 finalized／partial，ordinary `gpt-6.1-sol` 摘要 revision 1／source_version 50、provider requirement met 已發布；來源公開版本為 51。原始首個 fatal cause 沒有取回：RAM trace 未保留完整 exchange，bounded debugger attach 的 transport ended，沒有留下 debug session。不猜測為登入、音量或待辦缺失。

### 可重現邊界修正與最終驗證

- 固定版本 v3 將 delegation.created 與 turn.done 分開處理，handoff 文字不保證帶喚醒詞。已實際重現：合法同 turn handoff 先到、完成喚醒＋問題後到，舊碼停在 listening／reader 0／tool 0／rejected 1；修正為最多一筆有界候選，同 turn／相符完成問題才准入一次。真正臨時 SQLite → Native → complete marker smoke 已只讀一次，沒有模型或物理輸出；不採未知 turn／意譯猜測。
- 等 predecessor drain 的後段 PCM 原先可卡住八筆 browser callbacks。改為綁原 45 秒期限的 RAM FIFO；新隊列又經真正 Chromium／BrowserTXSink 的二十包 consumer test 抓到 0.5 秒播放佇列溢位，修正 pacing，不放寬 sink bound。另重現並修正到期啟動與 pacing 期間換 assistant turn 清空 queue 的競態；後者 red 為 `pop from an empty deque`，green 保留新 turn 音訊並排除舊 packet。
- 新增首次 runtime failure 的固定 code／phase／exception type／有界 elapsed_ms，經 controller allowlist 才輸出；後續失敗不覆蓋，不洩漏 raw exception／URL／SDP／姓名。空 TimeoutError 訊息曾使失敗旗標變成 false，再被 late cancellation 復活；新增失敗前／成功後回歸，保留固定非空失敗旗標。不把此本機缺陷當成第十三輪唯一根因。
- 刪除 mock-only 喚醒轉送測試及私有 greeting 呼叫斷言，將部分喚醒的無早期 TX 行為移到真正 Native notification／PCM 邊界；刪除要求兩次不同時點的 diagnostic age 必須完全相等之偶發斷言，仍保留取消收據與清理驗證，未以新數字重釘。
- **最終可用回歸 856 tests 全通過，144.160 秒、skips=0**；明確排除原移轉缺少的 `test_bridge_integration`／`test_media_bridge`，不是完整原始 discovery。正式 Native／原喚醒規則／production legacy media profile 的單次真模型靜音控制，未喚醒前 tool=0；喚醒後 admitted=1／tool=1／context bytes=11,947、有效 PCM、無 error，正常 stop／cleaned。沒有真 Teams 或物理輸出；保存待辦逐字比對仍 false。控制早於最後空訊息旗標修正，該最後改動另有失敗／取消回歸及全量證據。收據：[真模型控制](native-wake-order-control-20261016.json)、[原失敗與修正](teams-native-silent-round13-20261016.json)。
- 正常服務 `calendar-live-native-wake17` 已載入最終 source，登入 API 確認 started=true、無 active／interlock／last_error；paused、media flags=false，未自動入會。新候選 `teams-assistant:local-native-wake-order-20261016`／`sha256:e4c628d2e98f2b36af9a15dccd3492c404755e2969718533c8491474381bbde8` 的 installed／workdir 四模組 SHA 與 host 相同，15-check Compose smoke 全通過、SIGTERM 0.423 秒。依賴相同的本機基底＋目前 wheel 離線重裝；首次 FROM image-ID 的 metadata 失敗與修正保留，不宣稱 stock cold build／真人回覆或部署完成。收據：[build](native-wake-order-candidate-build-20261016.json)、[Compose](standalone-compose-native-wake-order-20261016.json)。


### 第十四輪：遠端已聽到，但中途斷音後接回

- 操作者回報「有聽到，但中間斷掉又接回，最後說以上是待辦的原文」。遠端有聲成立；連續性失敗，全文正確仍未核對，不能只憑結尾判定原文完整。
- 最近一次現場觀察 07:19:31 UTC：active、A recording、B sleeping、error_code／runtime_failure=null、handoff admitted／tool calls 各4、context bytes 46,802、speech closed／drained=true／truncated=false。這些是累計與尾端排空狀態，沒有中途播放連續性的證明；本節沒有另做 live poll。
- 無憑證、網路請求中止的實際 sandboxed Chromium，使用正式 BrowserTXSink／本機合成 sender／20包恒定PCM。按PCM時長送包並在中間注入120ms延遲，實際 source 排程量到最大120ms空隙、總141.333ms，drain仍成功且truncated=false，2.55秒完成。未使用Teams、模型或實體麥克風，未量測遠端錄音。證明逐包排程遇遲到資料會插空且現有drain不會揭露，不證明真人現場唯一根因或丟字。
- 已核定的簡化設計記於 `docs/B-LIVE-NATIVE.md`：歷史直接用initialItems回答；需要最新本場資料才查SQLite；音訊避免同題反覆重建，保留授權／scope／撤權與有界緩衝。這次只完成設計與診斷，沒有更動執行碼、重啟服務或送新Join。收據：[teams-native-round14-audio-gap-20261016.json](teams-native-round14-audio-gap-20261016.json)。

### 持續播放器／同題尾段修正：每題工具機制不變

- 操作者明確取消歷史直接回答變更，保留每題`read_meeting_context`及`RESULT_COMPLETE`。只修改Native音訊FIFO、Gated輸出生命週期、Browser／Pulse adapter及NoPhysicalSink介面，不改A、歷史檢索或喚醒規則。
- 直接Native通知／PCM重現：前段drain被暫停，排入3包尾段，再收到同題新assistant turn；修正前預期5包只送出2包，修正後5包依序交付、tool_calls仍1。同題queue改綁answer scope，不因assistant turn更換清除或跳過；真正取消／scope撤銷／停止／到期仍清除。
- 真正sandboxed Chromium的生成MediaStream track經WebAudio觀察到所有尾段合成訊號，正常drain後同一sender track保留、無runtime error。固定PCM markers只是合成訊號，沒有錄音／模型／Teams；樣本觀察不是逐frame遠端完整性證明。另有實際Browser tests驗證pause後舊訊號不重播、close最終還原靜音sender、120ms受控抖動下連續排程與0.5秒預計隊列界限。
- 首次212項focused checks有2fail：刪除與新規格相反且重複的同題丟尾段斷言；將抖動fixture改成絕對媒體時序，不累加sleep與IPC時間。其後860可用回歸通過。兩位fresh只讀review中，Native無具體問題；adapter發現舊drain可能取到下一generation成功收據，實際新回歸先失敗，再改成generation mismatch固定interrupted結果；21項browser identity通過，reviewer確認finding已處理。
- **最終861可用tests通過、146.062秒、skips0**；原移轉缺少的`test_bridge_integration`／`test_media_bridge`仍排除，不稱完整原始discovery。最後版本另重跑Native tail smoke與實際Browser輸出樣本，皆通過。
- 同版候選`teams-assistant:local-persistent-player-20261016`／`sha256:337206e3b1d184cc069f965a4fabda844ad87c6d57e079334370f25f3c12fff8`離線重建3.138秒，4個變更模組host／installed／workdir SHA一致，15項network-none／無憑證Compose smoke通過，SIGTERM1.080秒。不是標準冷建置、發布或部署。
- 07:58檢查正式服務已無active／無interlock，僅撤回該測試Calendar殘留錄音／發聲政策，沒有代替操作者停止會議。idle舊服務17在本次SIGTERM後supervisor報exit1；中間18以SIGINT正常exit0，最終`calendar-live-player19`（PID1748798）正常啟動。08:14:59 API確認started、無active／interlock／last_error，媒體勾選0；沒有建立活動、入會或真模型探針。新版遠端連續性與全文仍須新的在場確認。
- 收據：[播放器修正與界線](persistent-player-verification-20261016.json)、[離線candidate build](persistent-player-candidate-build-20261016.json)、[Compose smoke](standalone-compose-persistent-player-20261016.json)。原始TRANSFER-MANIFEST未回寫。

### 第十五輪：操作者確認已正常、可聽到完整句子

- 新的在場確認後建立活動16，綁原系列，08:22:25 UTC只送一次Join。服務`calendar-live-player19`使用上述最終播放器source；A recording／RX ready、B ready／發聲已授權，請原句測試。沒有再次修改執行碼或重啟。
- 操作者回覆「已正常，可聽到完整句子」。**本輪遠端完整句子播音通過**，接受此直接觀察，不重測確認。這不代表待辦逐字內容已核對、每次問題皆正確或整體端到端完成。
- 08:28:02 UTC API補充狀態：run active、A recording、B sleeping、output enabled／muted、error_code與runtime_failure=null、speech closed／drained=true／truncated=false。累計handoff admitted／tool calls各3、context bytes34,388、total_limit_bytes=null；不能據累計數宣稱真人問了3題，或據drain宣稱遠端無聲音問題。
- 保持本輪在線，不主動停止、重啟或重入；錄音／發聲僅本輪授權、排程未恢復。待操作者表示測試結束後，通知並正常停止，接續核對來源／清理／摘要發布。收據：[ready](teams-player-round15-ready-20261016.json)、[真人播音結果](teams-player-round15-continuity-20261016.json)。

### 第十五輪新資訊問答失敗與後續A擷取逾時

- 操作者已在會議內說明非正式決議的合成測試資料「藍天／12000」。A的SQLite v39保存相應未定稿片段、provider final=0；獨立唯讀的正式reader/tool投影所選record order可讀出兩事實。這不是正式B該題實際傳送或模型消費證據，不能以本機probe代替真人答案。
- 真人隨後回報「本場會議無足夠資料，無法回答這個問題」，本場新資訊問答失敗，不要求重問。Native工具累計提問前3、回報後6；handoff累計received7／admitted6／rejected1、last rejection=deadline_expired；context sent69,301bytes、known28、pending=false／failure_code=null。確有期間工具執行，但沒有逐題payload／精確問題對應，不宣稱那題已拿到或已完整送入兩事實。
- 08:41:01仍active／B answering；08:42:22該run因A `rx_capture_failed`、phase=`rx_capture`、exception=`TimeoutError`自動failed／capture_error。非assistant送stop／restart；A與B cleanup均cleaned，後續API無active／interlock，媒體已撤回。這個較晚capture timeout不直接解釋前面資料不足；provider RAM trace隨清理已消失，沒有復原精確工具payload或逾時唯一根因。
- ordinary `gpt-6.1-sol`於08:42:39發布summary revision1／source_version77／provider requirement met；保存來源仍partial，不宣稱轉錄或全流程成功。原播放器完整句子真人通過保留，內容測試失敗與capture失敗也保留。沒有重入或source修改。收據：[新資訊問題與自動終態](teams-round15-insufficient-context-20261016.json)。

### 第十五輪問題修正與同版驗證：2026-10-16

- **A 的第一個錯誤不是 timeout。** SQLite events 保存08:42:17.755 UTC `QueueFull`／`rx_capture`，後續08:42:22.055 sender-close `TimeoutError` 曾覆蓋首錯。收到13573包、入列13572、消費13316、push返回13315；queue容量256／overflow1。設定duration3600秒、drain5秒，沒有持久stop；不是20分鐘硬性上限或本次assistant送stop。
- **B 可重現的漏資料發生在 incremental wire。** 使用不可變 `changed_version<=39` 重建38個本場片段＋2筆凍結歷史；舊選取／重複metadata使wire漏掉兩個測試事實。保留明確命中優先、同相關度新片段優先；選完才依record order呈現，並移除重複context／固定identity欄位，只在wire省略未知None locator。三種問法×冷／近似暖cache共6情境皆保留兩事實，含marker為11706–11787bytes，完整host provenance不變。原現場精確query／cache／payload已不可恢復；這是實際來源的有界重建，不是唯一歷史因果證明。
- A 正常停止先關入口、排空已接受PCM，再drain／close，沿用同一deadline；持久stop不重複取消進行中的cleanup。已接受但未返回的samples即使process已reaped也保留unknown／failure。第一個非良性來源失敗不被後續取消、provider error或close timeout改成stopped。
- A-only實測AudioContext／performance時鐘對應，修正慢／快sender的實際wall-duration及queue節流；B原graph／nominal rate不變。合成0.98時鐘舊碼在第13369包overflow，新碼0.98／1.02各16000包、32768000 input frames完整排程且無overflow；真來源gap不拿來猜速率。正式A offer的實際本機RTC排程239616／239616frames、context running；短測clock近1.0，**沒有重現或證明原現場底層時鐘漂移**。未放大queue、pacing bound或deadline，未補造PCM。
- 新增最多16筆／每筆64個來源索引的實際工具執行收據，記錄source version、選取、bytes、phase與marker-return；不保存query／原文／provider IDs，模型消費驗證固定false。送出計數仍不等於模型理解或遠端答案正確。
- 正式Native／原prompt／原喚醒規則、無synthetic text gate的真模型靜音控制，只把合成事實存SQLite，語音只問問題；tool1／admitted1／marker-return後答出藍天與12000。source v39、14refs、wire11960bytes、無runtime error、STOP ACK／closed、owned cleanup cleaned／live0。控制在文字命中後、quiet tail尚未結束時停止，不當成完整播音／A逐字／歷史待辦全文／Teams驗收；先前未完成控制與harness報告錯誤也保留於[控制收據](native-context-wire-control-20261016.json)。
- 兩位fresh只讀review：B無具體finding；A抓到drain後final stats await才到的provider error會因close-entry exemption被吞掉。實際worker回歸先回0而非failure1；修正close在owned cleanup後始終傳遞保留錯誤，最後4項專測通過，reviewer確認已處理。保留durable tail／provenance／非provider-final斷言。
- **最終887項可用回歸全通過，186.820秒，failures0／errors0／skips0。** 仍明確排除移轉缺少的`test_bridge_integration`／`test_media_bridge`，不稱完整原始discovery。新候選`sha256:c8232259378b51ca012a80a125ff35ea8652436d5fc59ea872c51fdebd1929d0`離線build3.832秒、全部48個Python模組host／installed／workdir一致；15-check無憑證network-none Compose通過，SIGTERM0.576秒。
- 舊服務19確認idle後SIGINT正常exit0；新`calendar-live-context20`／PID2235670於11:16:50 UTC API實查started、無active／interlock／last_error、排程暫停、錄音／發聲勾選0。未新增Calendar活動／Join；6個臨時scripts與2個replay artifacts均已清除，原TRANSFER-MANIFEST未重寫。新版本尚待先通知與新的在場確認後真人重測，不回填原失敗為成功。
- 收據：[修正與界線](round15-context-capture-repair-20261016.json)、[同版build](context-capture-candidate-build-20261016.json)、[Compose smoke](standalone-compose-context-capture-20261016.json)。不是標準Dockerfile冷建置、發布或正式部署。


### 第十六輪早退與安全首錯診斷：2026-10-16

- 操作者新在場確認後，復用同一已授權Google瀏覽器建立第17筆測試活動、明確綁原系列。Calendar readonly讀回確認只一筆、原Teams locator相同且時間／媒體政策有效；14:06:22 UTC只送一次Join。14:07:43.772 recording／RXready，14:07:49.035 Bready，14:07:53.355 failed／capture_error；未請操作者說資料，未取得本輪真人看見／語意／遠端播音驗收。
- 實際98包／200704frames均為零，push嘗試98／返回97，queue depth0／overflow0／invalid0；不是上一輪QueueFull。diagnostic僅`operation_failed`／`Error`／`rx_capture`／worker378（drain重新await consumer）。原訊息與RAMtrace已清，不能事後斷言是哪個clock guard；來源與provider final均0，A／B cleanup=cleaned，無active／interlock。
- 重現固定clock碼跨JS/Python遺失及原始push位置被後續task await改寫；先red再修正明確`.code`橋接與首次consumer failure snapshot。三個allowlist code保持，不解析或保存exception body，未知code／惡意文字維持generic。consumer已確定失敗且資源釋放可為failed／cleaned，不是來源成功；未返回的pending push仍fail closed。沒有改sampler容許值、queue、deadline、B／播放器或每題工具機制。
- 真實A＋B的Python絕對節流控制，以及第三頁正式`CAPTURE_JS`／owned合成track回呼控制，各260包全部返回、Bready，owned3／live0／cleanupcleaned。後者260筆clock樣本無guard failure，playback rate約0.998846–1.002562。**兩組均未重現原早退，不把非Teams控制冒充唯一現場因果或修復證明。**
- 實際sandboxed Chromium以suspend後呼叫公開push，得到`source_audio_context_not_running`、失敗包排程frames0、ownedclose返回；沒有模型／Teams／實體輸入輸出。39項專測通過16.999秒；最終**890可用tests通過182.183秒，failures0／errors0／skips0**，仍排除移轉缺少的兩個bridge suites。
- 本機診斷候選`sha256:e2f2b1ee2716b53dedf09e4e1826693351fbeb70c21c0060230421b6dac323a9`離線build3.237秒；48個Python模組host／installed／workdir一致。15-check無憑證network-none Compose通過，SIGTERM0.561秒。不是標準Dockerfile冷建置、發布或部署。
- 撤權的早期helper合併操作未真正取消checkbox，空值也仍有欄位；後改為正常同源CSRF POST明確省略兩個key，14:38:15 UTC實查recording／speaking checked=0。期間排程一直暫停，沒有第二次Join或其他capture。舊服務20確認idle後SIGINT正常exit0；新`calendar-live-source-diag21`／PID2652871於14:39:26 API實查started、無active／interlock／last_error、paused／media0。
- 原第16輪唯一原因仍待新版安全診斷下的實際情境；沒有重入，下一次仍須先通知並取得新鮮在場ACK。4個臨時scripts／2個replay artifacts已清除，TRANSFER-MANIFEST未修改。
- 收據：[真實失敗](teams-round16-early-capture-failure-20261016.json)、[兩組控制與限制](round16-dual-live-controls-20261016.json)、[安全診斷修正](source-clock-diagnostics-verification-20261016.json)、[同源build](source-clock-diagnostics-candidate-build-20261016.json)、[Compose](standalone-compose-source-clock-diagnostics-20261016.json)。


### 第十七輪：確認是新增A時鐘guard中止

- 新在場確認後，第18筆Calendar活動讀回唯一存在、綁原系列；14:49:09 UTC只送一次Join。14:49:46.700 recording，14:49:58.171 failed／capture_error；sample audio10.325秒、121包嘗試／120返回，queueoverflow0／invalid0／depth0，來源與providerfinal均0。沒有內容或遠端播音驗收。
- 新首錯診斷明確保存`SourceClockError/source_audio_clock_unstable/rx_capture/browser.py576`。可確認新增`_SOURCE_CLOCK_JS` guard主動終止，而非上一輪QueueFull；同碼含多個分支，未保存當次量測值，不宣稱確定是哪條短窗條件或第16輪完全同因。
- 已向操作者明確說明新增A時鐘策略是本輪退場條件，建議撤回該策略並保留B資料／播放器／首錯修正；**尚未修改或撤回程式**，也不將建議當成原QueueFull已修復。策略涉及恢復舊傳送但保留原長時間積壓風險的取捨，待操作者回應。
- A／B cleanupcleaned，API無active／interlock；本輪以明確省略checkbox的同源CSRF POST撤回媒體，實查勾選0，排程暫停。沒有第二次Join；所有下一輪仍先通知並取得新在場ACK。服務21及e2f候選未變，不重跑不相干回歸。
- 收據：[第17輪明確guard失敗](teams-round17-clock-guard-failure-20261016.json)。原失敗與尚未完成端到端驗收保持不變。

### A 原生媒體乾淨切換與正常關閉修正：2026-10-16

- **移除失敗來源，不調高門檻。** 會議A使用原生WebAudio／owned本機RTC relay進入隔離Live sender，不再走Python PCM送音queue、逐包AudioBuffer排程或自訂時鐘slope。file／tone、B及明確WAV為真正的獨立PCM用途，不是A fallback；B每題工具及持續播放器不變。
- 實際本機接收端波形驗證：PCM observer阻塞22.5秒仍收到正確音調；capture前後gate、多frame／新增track、scope失效、取消及未知清理皆涵蓋。停止後瞬間全頻RMS不必為零，未將其當新來源洩漏；改以來源端新1900Hz正向訊號及接收端未穿透、已停止track／closed PC證明，不改production尾音時序。
- fresh只讀review找到暫停中新增來源被當fatal、等待lock時新document世代遺失、第二次optional startup snapshot被當fatal及B共用JS marker破壞；均修正並由reviewer確認無剩餘具體finding。前兩個實際RTC情境以第一版421行snapshot重播：before兩案error，after兩案通過；不以mock echo代替波形。
- 真實A＋B的原生傳送60秒沒有capture／model執行錯誤，卻在drain出現`live_session_closed`。後續有界probe確認datachannel正常`session.closed/reason=client_request`、own finish requested／sent皆true，並非缺reason。程式先前只認`requested/stopped`；現在只對own closing＋finish request的精確native ack放行。4項回歸before為2fail／1error，修正後46項專測通過6.584秒；unknown／null、expired、transport及先前／後到真錯仍失敗，沒有通用closing吞錯。
- **最終真模型內容控制仍false。** 修正後原生傳送60.097秒、SQLite來源21筆（provider final2／application segment19）；金額匹配、代碼詞未匹配。A failure=null、A／B／source／browser close皆返回，自有程序3／live0／reaped，B未喚醒tool0且STOP ACK／closed皆true。這證明音訊到模型、來源保存及正常清理，不證明全文ASR、原現場唯一根因、歷史待辦或Teams遠端播音。早期失敗控制與最終未過的雙事實判準均保留，不重跑到綠或改寫來源字詞。
- 最終**915可用回歸通過，227.005秒、failures0／errors0／skips0**；仍排除原移轉缺少`test_bridge_integration`／`test_media_bridge`。刪除舊PCM時長預設及私有scope counter的 incidental assertions，保留真正join無capture／B恢復行為，未放寬bounded-contract授權gate。
- 同源候選`sha256:eca77e558d075e3a581e108a88e90de8166a112ce8d227eb50374c9d92f10c05`，離線build4.751秒、49模組三處SHA一致、7條Requires-Dist與base相同。先前helper因venv無pip及BuildKit把裸image ID當registry名稱而失敗；改用現成build／setuptools、驗證既有base tag對應immutable ID，未加裝／登入／pull或更改依賴。不是標準冷建置、發布或部署。
- **容器native測試最初16 errors的真因是fixture網路。** 同image／硬化條件下，internal無default route時Chromium155 ICE candidates=0；routed介面則candidate1／mDNS1且pair succeeded。未放寬seccomp／caps／sandbox；改以routed fixture、頁面HTTP全阻擋、無外部ICE／憑證／模型／會議。17案通過76.201秒，另15-check network-none Compose全通過、SIGTERM0.505秒。兩種network條件不混稱。
- 舊服務21確認idle後pidfd SIGINT正常exit0；新`calendar-native-media22`／PID3437177已透過新admin session實查started、無active／interlock／last_error、paused／media checked0。沒有Calendar寫入或Join，仍18筆活動／17次嘗試；下一次必須先通知並取得新在場ACK。
- 收據：[整體與缺口](native-meeting-media-verification-20261016.json)、[真模型最終控制](native-meeting-media-real-live-final-control-20261016.json)、[review red/green](native-meeting-media-review-redgreen-20261016.json)、[同源build](native-meeting-media-candidate-build-20261016.json)、[ICE因果對照](native-meeting-media-container-ice-comparison-20261016.json)、[native容器](native-meeting-media-container-smoke-routed-20261016.json)、[Compose](standalone-compose-native-meeting-media-20261016.json)。
- 六個自有臨時scripts及source review diff已刪除；模型TRACE／控制SQLite由tmpfs暫存生命週期清理。正式來源、歷史失敗收據及原TRANSFER-MANIFEST保留，沒有發布或push。


### 系統自行驗證與剩餘 B 長穩缺口：2026-10-17

- 本輪依操作者要求先完成自行驗證；沒有 Teams Join、Google 活動寫入或啟用媒體政策，仍為18筆活動／17次真人嘗試。沒有 push、發布、部署、替換 provider／模型／固定 Codex 版本，原 TRANSFER-MANIFEST 保留。
- **B 摘錄裁切修正：** 單一已選來源再次受 wire budget 縮短時，舊碼只留前半段而裁掉尾端 literal；先以實際 reader/context 邊界重現失敗，再共用原查詢詞與 excerpt 邏輯圍繞命中縮短，維持原始 char offsets。相關127項回歸通過。[red/green](system-completion-fixes-redgreen-20261017.json)。
- **B 每題新鮮度修正：** 原正式 Native 控制第一題讀 SQLite，後六題雖產生答案卻沒有新 delegation/read；預算更新仍沿用舊金額，主機正確未授權新題發聲。明確補足重複／歷史／已知答案也須本題重新查詢、舊 marker 不授權新題的提示，沒有新增協定事件、換 thread、歷史直接回答或改播放器。[原失敗](system-completion-B-before-freshness-20261017.json)、[兩題實際更新金額](system-completion-B-freshness-short-20261017.json)、[實際 capture→B 兩題](system-completion-B-capture-short-20261017.json)。這是提示層修正，不是伺服器強制工具保證。
- **長穩尚未通過：** direct PCM 控制前8題皆讀工具並答對本場、上次決策、兩條待辦原文／負責人／日期；第9題僅有部分 user transcript，沒有 final/handoff。另以實際 capture→timestamped B queue 的持續來源重現：前7題通過，第8題 source packets／sender frames／outbound RTP 都前進，收到19個 user deltas、pending 含喚醒與問題，但沒有 user final 或 delegation；沒有 reader 拒絕、native error 或停止來源。不能把它當成本機32K gate、確定的八題上限或已證明服務端根因。[direct 負例](system-completion-B-direct-input-negative-20261017.json)、[production fanout 負例](system-completion-B-production-capture-negative-20261017.json)。raw native／canonical 事件邊界仍在定位；本輪不請操作者入會補測。
- **撤權與停止：** 真 Native 模型經正式 gate 送至僅計數／rolling hash、無裝置的 sink。撤權控制期間活躍解碼包4→98，sink bytes維持290816，output disabled／inactive、sink muted、無 native error；STOP ACK／closed／程序reaped。[實際撤權](system-completion-B-revocation-20261017.json)。不宣稱 Teams owned sender 或遠端 audibility 已驗收；首個缺乏精確錯誤位置的控制負例保留，不回填原因。
- **A 原生與詞庫：** 新英文 native 音訊命中預先指定代碼詞及金額；同一中文 WAV 無詞庫／4筆核准合成詞庫兩組均命中五事實，來源回呼與 SQLite 精確相符。兩組均通過不代表辨識率改善，也不回填前一輪不同 WAV 的代碼詞失敗；另保留補充 PCM 握手逾時、尚未取得來源的負例。[A對照](system-completion-A-differential-20261017.json)。
- **A→摘要→下場真模型閉環：** 同版 native A 1829.194秒，0／900／1800秒三段語音各命中五事實，來源以真 lease／heartbeat 保存，停止後 seal+outbox。兩次普通 `gpt-6.1-sol` 實際發布來源 ID／version／SHA 符合的摘要；發布後重啟恢復拒絕舊 generation publisher，同系列最新記憶與任務狀態延續、相同 locator 不同系列隔離。第二場沒有新增任務，舊完成／延後不因摘要省略而重開或消失。第二段專用事實判準在執行前已固定；泛用首段 matcher 對不同句子的 false 原值保留，另記實際第二段金額／交付日／無新增待辦三事實。[native lifecycle](system-completion-native-lifecycle-20261017.json)。來源仍保留 provider final／未定稿品質，不宣稱真人全文 ASR。
- **Compose 環境污染修正：** 原 checker 的 disposable `--env-file` 仍被 inherited shell 的模板變數覆寫，會指向操作者 image／config／掛載。render 前僅清除模板白名單同名環境，舊函式在衝突環境實際失敗，新 CLI 17項通過。[原函式負例](system-completion-compose-red-control-20261017.json)、[修正檢查](system-completion-compose-config-20261017.json)。
- **標準 Docker 建置：** online npm timeout/reset 與大型 Playwright wheel timeout 都有原始失敗；不只 BuildKit 出現，因此不斷言唯一基礎網路原因。採公開固定版本 artifacts 與官方 SHA 驗證，分離下載及離線安裝；`codex_npm`、`python_wheels` 必要 contexts 不含 OAuth／data root。amd64 npm重驗固定SHA512後offline安裝，Python從當前源碼以 `--network=none --no-index` 安裝；不換版本、延長timeout、停TLS或放寬sandbox。[標準冷建置及最終source刷新](system-completion-standard-build-20261017.json)、[官方wheel驗證](system-completion-python-wheel-inputs-20261017.json)。
- 最終候選 `sha256:2430a6234a113267022658bb6ce2e31a069b6d1fa992bf27a5dd79c0fb27c11f` 的49個Python模組host／installed／workdir SHA完全相同，實際import47個非入口模組成功，source workdir僅含49個`.py`。Python3.12.15、Chromium155、Codex0.159.0-alpha.3。[image identity](system-completion-image-identity-20261017.json)。非amd64及正式部署未驗證。
- **最終916項可用回歸通過，242.266秒，failures0／errors0／skips0。** 仍明確排除移轉未附實驗source的 `test_bridge_integration`／`test_media_bridge`，不稱完整原始 discovery。[回歸收據](system-completion-final-regression-20261017.json)。兩位fresh只讀review對B／Docker修正無具體剩餘finding；review不代替執行證據。
- 同一新映像[15項network-none無憑證Compose](system-completion-compose-smoke-20261017.json)通過，SIGTERM0.487秒；[17項routed實際本機RTC／波形](system-completion-image-native-media-20261017.json)通過62.578秒，未關sandbox／加caps／外部ICE。第一個忘接Docker stdin、未執行任何案例的harness負例另存，不把exit0當通過。
- [14項實際browser管理頁檢查](system-completion-image-admin-browser-20261017.json)通過：登入、詞庫pending→active及revision、真合成native來源／Codex摘要及品質標記、系列任務修改、未授權且不立即加入的未來手動場次；關分頁後daemon健康，容器重啟後重新登入確認詞庫／任務／排程持久。只複製已完成的合成SQLite作UI fixture，沒有實際Google／會議／憑證。自有UI容器已正常停止並移除。

- 額外以同一硬化映像、network-none、空白私有 Codex home 啟動真正 app-server，完成 stdio initialize／initialized 並確認程序回收；沒有模型或憑證。[實際app-server啟動](system-completion-image-app-server-20261017.json)。第一個 fixture 未先建立明確 CODEX_HOME 路徑，Rust 正確拒絕，修正的是測試前提，不是放寬配置檢查。


- **原始事件對照補充：** 同一Native thread前10題全部查工具並答對，累計34194bytes，並非固定八題或32K上限。第11題合成source context在1855秒wall time只到1598秒，55秒間僅新增34816frames；該輪沒有證明完整問題已送入，不能用它歸因SDK。[raw／clock負例](system-completion-B-raw-clock-negative-20261017.json)。
- **最終完整刺激仍未通過長穩：** 補齊正式拓撲的本機A native RTC消費端，並等待AudioBufferSource.onended而非假定wall duration。第7題108495frames／4.520625秒音訊在5.249秒完整播放；capture5212包全接受、無時鐘拒絕／drop，A native ready，B sender與RTP持續前進。原始wire有新的user turn.created及9筆input_transcript.added，但整個播放後55秒觀察窗內沒有user turn.done、delegation.created或assistant答案；canonical也沒有完成。前六題read／marker／原文事實均通過，第七題工具0、原生error=false，正常停止／回收。[完整刺激與協定邊界阻擋](system-completion-B-qualified-native-blocker-20261017.json)。
- 固定版parser只把合法`turn.done`／`delegation.created`轉為完成／委派，未發現以相同文字抑制已解析完成事件的程式；v3沒有已確認的client強制user完成或新委派介面，`RESULT_COMPLETE`也不是server完成API。可確定缺失在原始Native完成／委派邊界，不能確定服務內部VAD／資源／其他原因。這不是播放器修正，也不靠改模型、取消授權、借舊ID或每題換thread冒充完成。**整個系統尚未達到集中真人驗收條件；目前不請操作者入會。**
- 舊待命22再次實查idle／paused／media0後pidfd SIGINT，supervisor確認exit0；新`calendar-system-completion23`／PID1295372已重新登入確認started、無active／interlock／last_error、paused與media0，配置未改，沒有Calendar寫入或Join。[待命接線](system-completion-standby-20261017.json)。

### Native 完成支援與輸入生命週期：2026-10-17

- 依操作者要求唯讀查核 `~/apps/dot` 的 Android Native 接線、WebRTC probe 與歷史收據；未啟動 Dot、讀取其憑證或接回 Dot／MCP。Dot 等待真實 `turn.done`／`delegation.created`，沒有找到 client 強制完成 API；單題歷史成功不能證明連續／長時支援。固定 Codex 的 `handoff_complete()` 在 client-managed 模式直接成功返回、不送完成訊息；不能拿 RPC ACK 當完成。契約與官方 source 連結見 [B Native 文件](../docs/B-LIVE-NATIVE.md#native-完成訊號與輸入生命週期)。
- **已修正本機輸入缺陷：** B 的 legacy／trusted-none 共用既有靜音 carrier，有限 PCM 或 echo quarantine 不再使輸入音軌沒有 source；不改 Teams 持續播放器、模型、喚醒、每題工具或授權。實際本機 RTC 對照：舊 graph 兩秒沒有新 RTP、concealed samples 持續增加；新 graph 收到99包真實靜音、無新增 concealment，下一段仍沿用同 track。[媒體對照與來源 SHA](native-completion-media-20261017.json)。
- 新的實際 B peer／RTC 行為回歸，在原有限來源 graph 下失敗於 source duration 增量0秒；修正後連同916項可用回歸全部通過（224.342秒，failures0／errors0／skips0）。刪除只複製 legacy JavaScript 選擇的 incidental test，改驗證實際樣本／收包／concealment／下一段連續性；兩個原移轉缺少的 bridge suites 仍明列排除。
- **首輪 Native 負例保留：** 前兩題均有真實 user done、新委派／SQLite／marker與 assistant done，第二題事實正確後卻出現 `response_output_backpressure`；不能宣稱 carrier 已解決長穩。正常 stop ACK／closed／程序回收，沒有實體播音或 Teams。[首輪收據](native-completion-first-attempt-20261017.json)。
- **補上耗時觀察仍未解決 Native 缺訊號：** 同 thread 前七題新工具／marker／原文及更正事實通過；第八題191418frames／7.97575秒語音在8.716秒wall完整播放，5961包全接受，A ready、B持續RTP，原始19筆delta但沒有user done／新delegation。11980次PCM callback最高0.778ms、host loop lag最高3.435ms、browser interval最高101.8ms／目標100ms、pending最高2、dropped0、runtime error=false。當時24396bytes、沒有本機累計限制；不能歸因32K，也不能斷言 provider 固定八題上限。正常ACK／closed／reaped。[完整刺激與 callback 對照](native-completion-carrier-qualified-negative-20261017.json)。
- **界線：** 這次修正證明實際靜音輸入連續性，不等於強制 Native 完成、模型已消費所有工具資料或 B 長穩通過；尚未達到集中真人驗收，不請操作者入會。沒有增加 Calendar 活動、Join、媒體授權、push、發布或部署；仍為18筆活動／17次真人嘗試。
- **real-render 對照未解決：** 另以既有 `trusted-none` 路徑維持真實 render、禁止實體播音；同 thread 前七題仍通過，第八題完整7.97575秒音訊／8.717秒wall，5956包全接受，但只有21筆delta與user created、沒有done／新委派。callback最高0.704ms、host lag最高2.675ms、pending最高1、dropped0，正常ACK／closed／reaped。沒有把這個未證明有效的 renderer 切成正式預設。[對照負例](native-completion-render-negative-20261017.json)。

- **官方 final channel 也不是強制完成：** 只將工具最後 marker 改送支援的 speakable channel，前七題工具／事實通過且沒有念出 wire marker，第八題完整刺激後仍無user done／新委派。這是有界 prototype，沒有切換正式 commentary 機制、偽造完成或放寬 gate。[路由負例](native-completion-speakable-negative-20261017.json)。
- **長 idle 控制無效，usage 不能冒稱原因：** 第480秒首次提問的合成來源未在既有180秒 probe 界線內回報 onended，未完成任何合格問題；不能拿它證明 Native idle 失效。44筆 usage 的 status／reset 都為 null、backend model usage 為空，音訊時長到659200ms只證明該計數，不證明 quota 耗盡或無限制。正常停止及程序回收。[無效刺激與用量觀察](native-completion-age-invalid-stimulus-20261017.json)。
- **目前映像已驗證修正路徑：** 標準Dockerfile15.13秒，候選`teams-assistant:local-native-completion-20261017`，immutable ID=`sha256:731ed95e512986a33df2543ec0b5c45ba073ea5924a93ed228889ec3cc28cff4`。49個Python模組host／installed／workdir一致，實際CLI／固定Codex binary及本機B RTC真實靜音／同track恢復通過；無模型或憑證掛載。internal-network fixture ICE逾時的負例保留，改回既定routed fixture後通過，未延長timeout或關sandbox。自己的容器及network已清除。[映像](native-completion-image-20261017.json)、[fixture負例](native-completion-image-fixture-negative-20261017.json)。
- 舊`calendar-system-completion23`核對PID／command／config／listener及idle／paused／media0後，以pidfd SIGINT正常exit0；新`calendar-system-completion24`／PID2241997已重新登入，確認started、無active／interlock／last_error、排程暫停、四項媒體授權未勾。原私有配置未改、沒有活動寫入／Join；仍18筆活動／17次真人嘗試。[待命收據](native-completion-standby-20261017.json)。
- **結論仍為阻擋：** 已交付經實際RTC與回歸驗證的本機持續輸入修正，但沒有找到通過連續實測的Native強制完成／新委派方法。固定SDK／模型的服務端缺訊號原因尚未確定；不把本次研究寫成完整功能完成，不請操作者再入會。保留`/dev/shm/assistant-native-blocker-i41u0xbn/{repro.py,input.json}`兩個私有synthetic最小重現檔（目錄0700／檔案0600），沒有憑證或真實會議資料；其餘完成的probe／媒體／SQLite／建置暫存與host build／egg-info已清除。歷史收據及TRANSFER-MANIFEST未重寫。見[本輪完整匯總](native-completion-verification-20261017.json)。


### Native 主程式分題與每題查詢：2026-10-17

- **根因不是本機漏解析完成事件。** 六個真實 Native 隔離控制包含原題首問、快序列、重複題、結構化提示及失敗後繼續輸入。第8、9題沒有 final；第10題收到合併三題的 user final，並出現沒有新 delegation 的快取回答。舊程式錯把每次實體發問當成必有獨立 Native 完成／委派；無法據此推定 provider 內部 VAD、quota、固定題數或時長。所有控制正常停止／回收；歷史負例保留。[隔離收據](native-completion-contract-isolation-20261017.json)。
- **操作者選定「改由主程式管理提問與查詢」。** `b_question.py` 以已准入 PCM activity／1.5秒實際 quiet／0.6秒文字穩定分題；沒封包不算靜音，單題2,000字元溢位整題捨棄，不是累計 context 限制。`b_native.py` 每題建立新的 application request、重讀 SQLite，使用固定 v3 官方 `session.context.append` commentary 分塊回同一 thread，完整 marker 返回才允許輸出。明確 `provider_final=false`，不合成 user done、不借舊 delegation、不逐題重連；A 來源與 Teams 持續播放器不變。
- **正式分題真 Native 首輪11/11題通過。** 真 browser capture → timestamped PCM → 真模型 ASR → application framer → scoped SQLite；沒有 fixture 提問邊界。每題 reader=1，包含本場更正金額、歷史決策與重複待辦原文／負責人／日期；同一 thread 累計39,057bytes，工具收據11次完整返回，未喚醒工具／輸出0。274.527秒連線，無 runtime error，正常 STOP ACK／closed／process reaped。另一次11題 transport-only prototype 使用 fixture 邊界，單獨列明，不冒充分題證據。[修正與實測收據](native-host-questions-20261017.json)。
- 回歸首次940項有12個失敗：舊 greeting fixture 仍用 user final 啟動、取消測試仍要求永久撤銷授權，以及 fake clock 修正後顯現的既有 echo-tail 斷言。已遷移為 application request 與實際清理／新題可恢復的行為檢查，不恢復已移除的協定依賴。另實際重現「await 清理期間新 PCM 到達，舊題仍准入」競態，以 input revision 在 await 後拒絕過期題；聚焦133項通過（7.970秒）。來源 budget 包含 request metadata，超額不前推未送 cache。
- `response_progress` schema 2 使用 `question_receipts`，不將本機准入冒稱 provider handoff；持久收據不含問題／來源原文或 provider ID。舊歷史收據未改寫。
- 本輪無 Teams Join、Calendar 寫入或實體收發音；既有待命程序／容器映像未刷新。這不是無限長穩、真人遠端 audibility 或整個系統同版 E2E 驗收；長停頓／雜訊分題仍有明示限制。固定模型、同一既有 OAuth、媒體授權與隔離契約不變。
- **最終擴充真 Native 13/13題通過，309.584秒。** 同一 thread 每題 reader=1、工具累計45,843bytes；另有1次單獨喚醒問候，完成實際 assistant done／有效音訊／drain 後開啟追問，不帶喚醒詞的第12題仍讀取更新後金額。第13題在喚醒詞與問題之間插入0.5秒 quiet，只建立1題、不插入問候。這兩段新增刺激使用既有離線 zh-TW voice，不讀實體麥克風；全程輸出 muted、無 runtime error，所有資源正常 stop／close／reap。[最終真模型收據](native-host-questions-final-20261017.json)。啟動後僅刪除未引用的 `_session_events` deque；行為及 wire 不變，最終回歸驗證清理後原始碼。
- **最終941項可用回歸通過**，test runner 227.814秒、整體228.146秒，failures0／errors0／skips0；包含分題、同題多段播放、取消／停止／撤權、marker／cache 提交、診斷隔離及真本機 Chromium 邊界。仍明確排除原移轉未附實驗source的 `test_bridge_integration`／`test_media_bridge`，不稱完整原始 discovery。[回歸與原始碼 SHA](native-host-questions-regression-20261017.json)。
- 已清除本輪私有 tmpfs probes、合成 PCM／SQLite／原始觀察與過時的前輪最小 reproducer；保留有界證據及永久行為回歸。沒有修改原 TRANSFER-MANIFEST、憑證、媒體政策或其他服務。


### 第18輪真人無回覆與閒置 PCM callback 修正：2026-10-18

- 操作者確認已在會議內及沿用原連結後，先恢復已驗證來源的服務25、核對 active=null／interlock=false／排程暫停，使用正常管理表單建立本場、沿用既有明確系列並只送一次 Join。A recording／native media connected、B ready／sleeping／owned sender 已選取；[ready 收據](teams-native-hostquestions-round18-ready-20261018.json)不是語音驗收。
- **本輪失敗：操作者沒有聽到回覆。** 首錯為 `response_output_backpressure/audio_callback`，elapsed 41,723ms；B 有44筆 user delta，但 question receipts、tool calls、context bytes、輸出活動皆0。主服務與A仍存活，當時已保存4筆 provider finals／37個 application parts。這不是32K、查詢拒絕或播放答案中斷；[原始有界失敗收據](teams-native-hostquestions-round18-failure-20261018.json)保留，不回填成功。隨後正常停止本場，確認 active=null／interlock=false，來源封存與摘要狀態 completed，沒有重啟或重入掩蓋該錯。
- **擁有層缺陷已重現：** 舊 `CAPTURE_OUTPUT_JS` 在沒有任何 speech lease 時仍逐包序列化解碼音訊並送回Python；真 Chromium 頁面主執行緒停頓650ms後，callback補發即可達八筆pending而永久停用B。對照Python callback最高0.027ms、sink writes=0，仍報8次output_backpressure；不是已證明Python處理過慢。現場實際停頓的觸發來源／長度未記錄，650ms是本機控制，不冒充現場量測。先前941項／13題合成驗證未覆蓋此交錯。
- **修正：** legacy／trusted-none PCM tap預設不准入，只在logical speech lease期間開啟；結束、取消、失敗及關閉撤回，capture generation拒收遲到舊callback。原生decoder、Live track及Teams持續播放器不重建；已准入有效音訊的背壓界線保留，不提高buffer或直接吞錯。普通Live speech與Native共用同一生命週期。
- 原650ms重現修正後errors=0、pending=0、dropped=0、idle host PCM callbacks=0。永久真browser回歸先紅後綠，並驗證後續有效波形、靜音gate、tap關閉與跨capture generation拒收；聚焦112項通過。首輪整合揭露未啟動peer的清理判斷與測試DI契約缺漏，已在擁有層及fixture修正，不加入production假provider或fallback。
- **真Native修正驗證：** 同一thread先閒置50.657秒（含650ms頁面停頓），state=sleeping、runtime failure=null、tool=0、tap pending／dropped／frames=0。隨後3/3題各讀SQLite一次，原金額與更正金額事實通過，皆有有效語音與完成／drain；每題結束tap回到關閉，正常STOP ACK／closed／process reaped。只用同一既有OAuth與固定模型，沒有Teams或實體音訊。[red-green及真模型證據](native-idle-output-capture-repair-20261018.json)。
- 額外用真browser固定`.125`首段、owned sender延遲啟動、後續`.25`尾段，重現初版tap gating可能丟continuation首段；改在已准入同題scope先開tap，再等待sender，以原有FIFO保留首段，await後重查scope。這是本次修正自己的回歸修正，不冒稱第18輪另有這個實測症狀；先紅後綠，連同49項Native／browser測試通過。
- **最終943項可用回歸通過**，220.490秒、failures0／errors0／skips0，仍排除原移轉缺少實驗source的兩個bridge suites。最終來源另重跑真Native：50.654秒閒置＋650ms停頓後3/3題、新查詢與修訂事實通過，正常ACK／closed／reaped；第一組50.657秒控制另存，不混算成同一次模型連線。[最終驗證與來源SHA](native-idle-output-capture-final-20261018.json)。
- 已核對服務25的PID／command／config／listener與idle／paused／media0，再以pidfd SIGINT正常exit0；服務26（PID278938）載入修正、重新登入確認started／active=null／interlock=false／last_error=null，排程暫停、四項媒體授權未勾。正常登出並關閉自有管理browser後，實際CLI `healthcheck --port 18081` 仍exit0。沒有再次Join，仍本機場次19／真人嘗試18；私有配置未變，GitHub及Docker尚未刷新。
- 已清除本輪私有tmpfs合成音訊、SQLite、probe scripts與模型暫存，只保留有界證據及永久回歸。第18輪遠端無回覆仍是失敗；修正後的真人audibility／完整E2E尚未驗收，下次Join需先通知及新的在場確認。


### 第19輪來源缺漏與有效音訊背壓：2026-10-18

- **確有查詢，但送錯範圍。** A保存三事實；B兩次讀SQLite並送23,069bytes，實際選取索引在v30／v52都漏掉三事實。舊流程先將文字選取降到wire bytes的四分之一，再刪選取末端以塞進單批12KB；較早事實被問題片段／metadata擠掉。B稍後另在有效音訊callback以`response_output_backpressure`失效，A仍保存。[原失敗收據](teams-native-round19-context-failure-20261018.json)不回填成功。
- **修正。** `ContextPlan.payloads`分批送完工具選取，不因序列化或host cache容量刪掉本題資料。保留request、原始offset／revision；全批次後一次marker、scope重查及commit。`receipt_readings`只提供有映射的原文投影，不改A、造final或跨缺口增加literal命中。
- **音訊。** legacy／trusted-none共用單一in-flight binding、PCM FIFO及絕對音訊時間節流。48k mono PCM16容量4,320,000bytes（約4.12MiB，含in-flight），依原45秒answer lease計算；真正溢位仍失敗，不放寬BrowserTX原0.5秒或answer絕對期限。真Chromium測試含650ms停頓、immediate／held consumer、實際BrowserTX波形FIFO、generation撤銷及慢消費溢位；650ms是控制，不冒充現場量測。
- **來源重播。** 原庫readonly，只在in-memory備份按changed_version重建來源；v30／v52分別33／55筆、3／5批，三事實均進入wire。不是重跑原真人提問或模型消費證明，不匯出會議原文。
- **負例保留。** [首個harness](native-context-batches-first-control-20261018.json)僅觀察done且cleanup key錯誤；[batch語意控制](native-context-batches-semantic-negative-20261018.json)曾編出錯誤日期；[首段控制](native-context-projection-first-segment-negative-20261018.json)只有短回應。[實際ASR首輪](native-context-asr-first-control-20261018.json)及[重複控制](native-context-asr-repeat-negative-20261018.json)首題三事實通過，但第二題沒有新delta／application request。沒有把本機push當遠端收包，或將此歸因模型容量、去重、播放器。
- **最後真Native三題通過。** 既有Hanhan zh-TW TTS → 真PCM／ASR → 正式application framer，同thread三題各重新讀SQLite一次；前兩題三事實、第三題更正金額皆正確，累計125,210bytes，無本機累計上限。第一題含650ms有效播音期間停頓，無runtime failure，正常ACK／closed／reaped。問題間補送0.5秒實際quiet，不把無封包當靜音，也未改production等待規則或宣稱唯一因果。沒有Teams或實體音訊。
- **parser核對。** [固定版官方parser](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/codex-api/src/endpoint/realtime_websocket/protocol_frameless_bidi.rs#L16-L66)逐事件將`input_transcript.added/item.text`轉為delta，此函式沒有依相同文字抑制新事件；不據此推定provider內部行為。不新增final依賴、借舊ID或逐題重連。
- **最終953項可用回歸通過**，236.570秒、failures／errors／skips皆0；仍排除原移轉缺少source的兩個bridge suites。刪除只檢查文字複製次數的舊測試，保留消費者行為／作用域／批次失敗及來源範圍回歸。
- 第19輪正常stop，run=`stopped/operator_stop`、summary=`published/error=null`。服務27核對idle／paused／media0後SIGINT exit0；服務28（PID353783）已載入source並實際登入確認started、active=null／interlock=false／last_error=null、paused／四項media flags=false。沒有新增活動／Join、改配置、commit／push或刷新Docker。[最終回歸與來源SHA](native-context-delivery-repair-20261018.json)。真人遠端及完整E2E仍待先通知及新鮮在場確認。


### 移除12KB整批門檻與必要性審查：2026-10-18

- 操作者明確要求移除、不換大數字，並review其他本機卡控。刪除`max_delta_bytes`、人工JSON分頁／二次切片、`context_batch`及容量拒絕路徑；`ContextPlan.payload`為單一完整JSON。固定SDK500-byte UTF-8分塊、最後一次marker、scope重查與atomic commit保留。source／tests已無舊參數、欄位或容量錯誤引用。
- 真實consumer回歸：1,000筆來源引用的待辦，修改前拋`context_delta_budget_too_small`，修改後保留完整引用及待辦。134項聚焦測試通過；最終**945項可用回歸通過**，237.256秒、failures／errors／skips皆0，仍排除原移轉缺source的兩個bridge suites。移除只要求人工分頁／容量拒絕的測試，保留失敗／取消／scope／marker／來源範圍保護。
- 真Native中文PCM／ASR → 本題SQLite讀取1次 → 17,418bytes完整JSON、1,000引用、一次marker、17,433bytes send-return；引用首尾及全集保留，正常ACK／closed／reaped。沒有注入application question／provider final、沒有initial history shortcut，也沒有Teams或實體輸出。
- 第一個probe只收集整段assistant transcript，含無關文字，不能當完整語意通過；後續按marker-return及answering作用域觀察，讀出正確待辦，但仍有異常開頭字元。這證明本次傳送／引用及指定內容路徑，不證明乾淨逐字播音、遠端audibility或整體語音品質；原始合成觀察另存，不把前綴問題隱藏。
- 兩位fresh reviewer分別查Spec內容限制與Standards媒體／生命週期。其他16K／4K檢索、48KiB歷史凍結、16KiB startup／詞庫、2K問題、45秒回答、15秒prepare、預設一小時場次與摘要deadline等**未在本次移除**；必要性、代價及建議見`../docs/B-LIVE-NATIVE.md`。官方500-byte依據是固定SDK自己的切片實作，不等於證明server拒絕501bytes。
- 三個靜態發現：late frame缺B時鐘校準、跨document共用frame continuity counter、初次慢校準後仍可能ready。未修改／重現，不能回填為舊真人失敗的唯一真因。授權、租約、來源／版本、sender／document隔離、有界音訊與unknown cleanup保護保留。
- 服務28核對idle／paused／media0後正常SIGINT exit0；`calendar-no-context-quota29`（PID536689）載入修正版，實際管理登入／API確認started、active=null、interlock=false、last_error=null、paused／四項media flags=false。未Join、改私有配置、push或重建Docker。[本次完整收據](context-quota-removal-review-20261018.json)。

### 不必要卡控移除與單一混音觀測：2026-10-18

- 移除16K選取／32K設定上界／4K摘錄、48KiB歷史凍結／64KiB envelope／16KiB歷史initialItems、詞庫聚合容量、2K提問拒收、45秒回答／60秒設定上界、15秒prepare wrapper、預設一小時退會／四小時上界及51秒摘要總generation期限。未換成更大的任意數字。舊凍結省略metadata仍真實保留，必要授權、scope、來源／版本／SHA、有限媒體積壓與cleanup保護不變。
- 最終**980項可用回歸通過**，253.033秒，failures／errors／skips均0；仍明確排除原移轉缺source的`test_bridge_integration.py`與`test_media_bridge.py`，不是完整原始discovery。曾出現的4項測試失配、重複摘要事件續命、await撤權後仍交付PCM、ready回傳不一致及B重試近deadline重啟A，均保留觀察並在擁有層修正；沒有以放寬時鐘、延長setup或改成假成功解決。
- 真實本機Chromium／RTC：兩個source documents（含後到frame）只產生一個host觀測generation；47包／96,256frames，gap=0，穩態23,881.45frames/s而非兩倍送音。兩頻率皆在同一PCM中；只靜音一源不誤分題，兩源quiet後才發出一題；A沒有Python PCM push，停止後入口關閉。
- 真Native中文PCM／ASR：本題fresh SQL一次，3個合成來源共17,320字元，55,733bytes完整JSON、全部來源保留；500-byte最大分塊、唯一marker、186,368有效活動frames，觀察到active deadline=None。指定待辦與週三可讀出；STOP ACK／closed／reaped均確認。這是muted synthetic邊界，並非A真實轉錄或Teams遠端可聽證明。
- 首次Native probe因把observer掛在constructor之後的`native._pcm`而沒有觀測實際`peer.on_pcm`，計數斷言失敗；已修正probe接線，沒有據此改production音量／完成規則。該次canonical文字仍曾先說無資料再補正確內容，負面觀察保留；最新控制也有前言及多次「嗯」。指定內容／傳送通過**不等於完整語音品質通過**，不把重跑結果冒稱模型語意真因已修復。
- 真Codex摘要`gpt-6.1-sol`：在注入95秒前置等待後正常生成及原子發布revision1，總104.665秒；原90秒summary lease經4次heartbeat維持有效，發布時已超過原expiry，source SHA／version保持一致。不是模型工作本身耗時104秒；版本／啟動／RPC／cleanup期限未繞過。
- 受控Node圖8508項斷言、5,761,024frames，跨120秒合成audio-time仍維持原有限backlog；不是120秒真人／模型長穩驗收。doctor現接受合法deny-all `[]`，本機metadata檢查local_ready=true，不等於Google／入會／遠端audibility已驗證。
- 服務29／30正常SIGINT exit0；最終`calendar-no-arbitrary-gates31`（PID882051）已載入source，實際登入／API確認started、active=null／interlock=false／last_error=null、paused／四項media flags=false，關閉分頁後healthcheck exit0。未新增真實場次／Join、未改私有配置、未commit／push或刷新Docker。下一輪真人仍先通知並取得新鮮在場ACK。

完整有界收據及20個source SHA：[`arbitrary-gates-removal-20261018.json`](arbitrary-gates-removal-20261018.json)。原第19輪失敗與前輪限制審查維持歷史證據，不回填為真人成功。

### 首段音訊與完整問答快照修正：2026-10-19

- 第20輪真人回報漏字、反覆待辦及最新資料延遲。唯讀診斷確認4次fresh SQL及完整工具結果，新事實已保存且選入；不是沒有讀工具。19筆RPC assistant-done不能當成19次獨立回答，現場沒有足夠逐turn文字證據，不能宣稱每個症狀的唯一原因均已證明。
- 已重現最後marker返回至sender啟動之間capture仍關閉的首段漏音窗口。`ResponseLivePeer.send_event(capture_output=True)`在同一瀏覽器操作更新generation並送marker；`NativeLiveResponder`使用原有限FIFO保存早到PCM，完整工具交付、sender及scope准入後才放行。保留舊turn隔離、撤權、取消、失敗與有限背壓；不重建decoder／播放器。
- 實際本機Chromium波形smoke送達12,288frames，觀察到11,743活動samples，首段440Hz及尾段880Hz皆存在，drained=true／truncated=false。首個probe曾因逐packet重設合成正弦相位而使頻率量測抵銷；修正的是probe相位，不是production音量。這不證明所有現場中間漏字都同因，也不等於遠端可聽。
- 真Native原增量協定控制在完整資料／marker返回後仍誤答、重複或用舊事實；只把問題移至最後未解決。改為`MeetingQuestionContext`每題完整可讀快照，不再要求模型維護upsert／replace／extend cache。完整source locator、revision／SHA與逐片段座標留在host；不依文字去重、不裁切來源，也不新增容量限制。

同一組實際喚醒／ASR、同thread三題、582→596→617筆application receipt的對照：

|模型wire表示|第1題bytes|觀察|
|---|---:|---|
|完整原文＋逐片段座標|32,477|否認已有歷史待辦；下一題兩次否認新事實。|
|完整原文＋連續來源索引範圍|4,057|三題皆回答正確待辦／新值；未觀察到重複完整句，第三題未念舊值。|

- 上表第二組的literal checker仍為false：模型以`ORBIT五一`／`RIVER七二`表示合成代碼，原檢查期待ASCII數字。保留原始失敗及合成答案觀察，不改判定器冒稱全綠，也不將文字觀察當成遠端音質證明。之前其他控制的誤答、ASR把問題改成「代碼凍結日期」、額外填充語與未准入第三題等負例均保留。
- 最終正式程式另跑99筆provider-final／歷史來源及同identity修訂，使用真喚醒詞、Native ASR及PCM quiet分題，不使用synthetic question-stage准入。三題首答皆通過指定事實、無舊值及不重複檢查：歷史待辦、新代碼／日期、修訂後結論／代碼／日期。每題fresh SQL，未省略來源；所有已完成控制均確認STOP ACK、closed、reaped、live_processes=0及pending_close_tasks=0。模型及CLI維持`gpt-live-1-codex`／`0.159.0-alpha.3`。
- 可用回歸首輪978項、256.656秒，977項通過，1項Browser continuation fixture等待drain逾時。修正fixture：等早到PCM實際形成2,048活動frames後才推進fake quiet clock，不延長timeout、不改production完成規則。整個`test_b_browser_tx`兩項重跑通過（2.665秒）；其餘977項沿用未受該test-only修改影響的結果。不是聲稱再跑過一輪全綠discovery。原移轉缺source的`test_bridge_integration.py`與`test_media_bridge.py`仍明確排除，skips=0。
- 約03:00 UTC唯讀確認舊版場次仍recording、B sleeping／muted、error=null、工具查詢4次、無stop request。本輪未新增活動／Join、重啟服務、改私有配置、push或刷新Docker。服務31尚未載入修正；待active場次安全結束及取得新鮮在場ACK，才安排新版真人Teams驗收。`response_progress`更新為schema3／`committed_records`，舊持久收據不回填。

完整有界正反例、來源SHA與驗證界線：[`native-snapshot-audio-repair-20261019.json`](native-snapshot-audio-repair-20261019.json)。

### 授權切換至修正版服務：2026-10-19

- 操作者要求關閉舊版並重新入會；已先通知將使用原連結。透過管理頁正常停止第20輪場次後，A回報`rx_cleanup_unknown`，B為`cleaned`；紀錄保持failed／cleanup unknown，沒有回填成功。
- 舊服務31正常SIGINT exit0；停止前無其子程序，之後確認原PID882051不存在。metadata-only doctor所有本機檢查通過，不冒稱新的Teams／Native驗收。
- 修正版`calendar-readable-snapshot32`（PID3109628）在18081啟動，實際管理頁登入及API確認started=true、active=null、capture_interlock=true、last_error=`capture_cleanup_review_required`。清理審查表單可用，兩項人工確認均未勾選；待操作者確認程序已停止及助理已離會後親自提交。不繞過interlock、不改SQL／私有配置，尚未建立新場次或Join；Docker／GitHub未變動。

### 操作者確認後完成清理審查：2026-10-19

- 操作者明確表示「你自己做，會議已關閉了」。重新確認舊PID882051不存在後，依這項確認與代辦授權，透過已登入管理頁勾選兩項並提交正式清理審查；未直接改SQL、換data root或偽造provider STOP收據。
- 提交後實際API確認started=true、closing=false、active=null、capture_interlock=false、last_error=null；管理頁顯示排程「暫停」。SQLite原場次仍為failed／cleanup unknown／response cleaned／operator_stop／rx_cleanup_unknown，原始失敗證據未改寫。
- 服務32已可待命。因操作者已關閉會議，本輪未建立新場次、未Join，也未宣稱新版真人語音驗收完成。

### 延遲裝置預覽遮擋入會：2026-10-20

- 操作者提供新的在場確認後，服務32已不在運行且18081無listener，沒有據此宣稱正常退出。啟動服務33後，兩次已通知的正式嘗試（`c6865aa56caf4e009ef70dbe8a6c39f3`、`7de68ad00f0940659b7a4d1a922351ff`）分別在mute／computer-audio階段TimeoutError；均尚未派送Join，最後failed／cleanup cleaned。A／B未就緒，不算真人驗收。
- 真實Teams預加入診斷觀察到延遲出現的裝置預覽modal：`Continue without audio or video`覆蓋已顯示的音訊與Join控制項。Playwright的Join trial click確認pointer-events被攔截，未實際Join。這證明存在遮擋路徑；不將前述每個逾時都回填為同一已證明原因。
- `_response_preview_navigation`改為在電腦音訊選取前後及姓名填入後處理唯一已知modal；背景仍有控制項時另要求`aria-modal=true`，dialog內不得含radio／Join。關閉提示後作廢audio proof，重新選取及讀回；不開實體麥克風、不force click、不重按不確定動作、不延長deadline。
- 新增實際sandboxed Chromium、本機route-fulfilled的兩個回歸：modal在audio click／name fill後出現，關閉時故意取消radio；均須重新選取後只Join一次。修正前實際Join click被overlay攔截而失敗。第一版修正把preview讀取放在frame-recovery之外，造成既有frame-loss測試失敗；已移回原guarded scan，不放寬SDK錯誤、撤權或uncertain-dispatch保護。最終11個相關suite **84項通過，10.668秒**。
- 早期真實站點probe只驗證audio readback，未驗證Join actionability，不能當成入會成功。補上trial click後才取得上述遮擋證據。修正後一次真實預加入probe在45秒內呈白畫面、無控制項而逾時；未Join、browser已關閉，不能以本機回歸掩蓋這次失敗。暫存probe及私有截圖已刪除。
- 核對服務33 active=null／interlock=false後，正常SIGINT exit0；`calendar-late-preview34`（PID123597）從新source啟動於18081。重新通知後，透過管理頁只提交一次原連結／原系列的正式人工場次`7f440526a3e04c91ae2df9f2a65f4ab1`。全域排程、私有配置及授權政策未改，Docker與GitHub未更新。
- 服務34場次最後仍為failed／meeting_join_failed／cleanup cleaned：event376顯示120秒內radio=0、Join=0、dialog=0、document complete、frame rescan=0，preview及Join均未派送。這是新的頁面載入阻擋，尚不能聲稱其根因已定位；未啟動A／B真人測試。
- 後續不派送Join的網路診斷，原設定觀察到7個CDN script的`ERR_QUIC_PROTOCOL_ERROR`，body文字／button／radio均0。僅在拋棄式probe停用QUIC的對照仍失敗，出現35個CDN script的`ERR_NETWORK_CHANGED`；因此**未將停用QUIC寫入正式啟動政策，也未把它宣稱為修復**。未放寬TLS、sandbox或音訊准入。
- WSL `wslinfo --networking-mode`實際回傳`mirrored`。60秒控制中觀察到46行netlink事件；另一次10秒唯讀監測有52行事件，其中12次eth0路由刪除，並有IPv4／IPv6位址刪除及重建，非僅猜測DNS。未保存位址／MAC。Windows當下Wi-Fi為Up，過去15分鐘WLAN／NetworkProfile事件查詢為空；這不證明Windows網路完全無問題，也未定位更底層的mirroring變動來源。未調整VPN／WSL設定、未停止其他專案。
- 以既有本機映像`sha256:731ed95e512986a33df2543ec0b5c45ba073ea5924a93ed228889ec3cc28cff4`作依賴底座，唯讀掛載目前`teams_transcription`及暫存probe，`PYTHONPATH=/work`載入新source。使用local Docker daemon、非root、readonly rootfs、drop ALL capabilities、no-new-privileges、既有seccomp、private shm／tmpfs、bridge網路；沒有掛載SQLite、OAuth或data root，連結只經stdin提供，沒有發布port或啟動管理服務。
- 該隔離控制實際回報`prejoin_ready=true`、`audio_verified=true`：正式入會程式走到Join控制項，Playwright trial click確認可操作後中止，**實際Join=0**。仍有telemetry拒絕、auth／fetch abort及兩筆page-error，未驗證其入會後影響；不宣稱零錯誤或真人音質通過。主機是Chrome for Testing153.0.8010.12，容器是Chromium155.0.8059.79，故同時改變了瀏覽器版本與network namespace，不能將成功唯一歸因於隔離網路。主機`/usr/bin`與`/usr/local/bin`未找到其他Chromium executable供同版本對照。
- 控制browser正常關閉，容器exit0／stderr空，`docker ps --all`確認該具名暫時容器已移除。服務34在管理分頁關閉後仍回應HTTP200、started=true、active=null、interlock=false、last_error=null。沒有助理留在本輪會議內。下一步需要明確核准本機Docker驗證接線，或先穩定WSL網路；未把任何外部阻擋回填為成功。
- 暫存網路probe已刪除；沒有留下HTTP/2對照程式、追加依賴或修改正式Chromium啟動旗標。

### 核准切換本機Docker驗證服務：2026-10-20

- 操作者明確選擇「切換本機Docker驗證」：僅切換此助理，沿用原私有資料及同一既有OAuth，不複製token、不動其他專案，實際入會前另通知。原主機runtime.yml未覆寫；新增private runtime.docker.yml及Compose env／command override，保留原授權、排程與合法deny-all `[]`。
- 原暫存build contexts已不存在。依標準程序重新取得兩個公開固定Codex npm artifacts並驗證SHA512；Python wheels沿用既有映像的runtime版本，backend使用已安裝的setuptools84.0.0，沒有升級runtime依賴。標準Dockerfile建置21.23秒；npm仍offline安裝，Python從目前source以`--network=none`／`--no-index`安裝。build context沒有data root／OAuth。
- 新image=`sha256:79c602c0c65d32ea3f9166c7afbc6a2f3b33a8b93de9390d87caec7f220d6bc8`，tag=`teams-assistant:local-snapshot-preview-20261020`。實際比對50個Python modules，host／site-packages／image workdir SHA全相同；browser.py SHA=`336d690830d09960e0979ab064b32c390d72f0e4bfdada337c89f0ae63024a96`，B的context／native／live保持前輪修正版SHA。實際CLI help及`codex-cli 0.159.0-alpha.3`通過。
- 以實際容器程序確認UID1000、effective capabilities=0、NoNewPrivs=1、seccomp filter啟用、root filesystem readonly；初始namespace的chroot仍被PermissionError拒絕。實際Chromium使用既有sandbox啟動策略，沒有新增Chrome旗標、CAP_SYS_CHROOT、privileged或host IPC。
- 新映像中4項實際Chromium音訊／RTC案例通過：TX mute／owned sender還原、有限continuation積壓、同track持續真靜音及恢復、sender延遲期間保留首段PCM。另2項延遲modal案例初次因測試容器PYTHONPATH少了`/verify`而import error；只修正該次測試命令後，兩項於3.436秒通過。其餘4項沿用9.298秒首輪的成功結果，沒有聲稱首輪6項全綠，也沒有改production或test來消除import錯誤。所有案例無OAuth／model／Teams實際Join。
- 使用核准的真實掛載執行metadata-only doctor，11項本機檢查皆true、local_ready=true；各Google／Teams／Live／audibility gate仍明列unverified。配置render確認只有原data root→`/data`及原`/home/timmypai/.codex`→`/auth/codex`兩個bind mount，OAuth允許正常refresh寫回同一目錄，沒有複製auth.json。
- 重新核對主機版active=null／interlock=false後，服務34正常SIGINT exit0。Compose project=`teams-validation`，container=`teams-validation-teams-transcription-1`（ID=`7d48d4d5e2b6b7c11af8a15594572bb375b82bdc1082bcfe8b64e0cae40b886c`）啟動並healthy。實際inspect確認新image、UID1000:1000、readonly、drop ALL、no-new-privileges／seccomp、獨立bridge，僅`127.0.0.1:18081→8080`。
- 實際登入新容器管理頁，畫面顯示排程暫停、Google已授權及既有published摘要；API確認started=true、closing=false、active=null、interlock=false、last_error=null。Google畫面狀態不冒稱新的真實入會／轉錄／摘要驗收。原資料庫直接沿用，沒有建立新人工場次。
- **部署後反例保留：**在運行容器中，用正式open／join路徑但以trial click攔住Join的原連結probe，120秒後仍逾時。最後快照origin=`microsoft_identity`、textbox1／heading1、radio0、Teams Join0，preview及Join均未派送，browser正常關閉。這與先前一次隔離Docker預加入成功分開記錄；目前不能宣稱入會已修復或三項語音問題已通過。待操作者新鮮在場確認，再釐清訪客入會／Microsoft登入導向；不自動使用其他Teams帳號或繞過登入政策。
- 本機wheel build的`build/`與`teams_transcription.egg-info/`已移除。公開建置材料保留於`/tmp/teams-codex-20261020-9KSv7Q`及`/home/timmypai/.cache/teams-build-20261020-9KSv7Q`；兩處均不含OAuth或會議資料。
- 關閉管理分頁後，再讀API仍為HTTP200、started=true、closing=false、active=null、interlock=false、last_error=null、background tasks=3；已啟動容器內的實際`teams-transcription healthcheck --port 8080`亦exit0。後台不依附瀏覽器分頁，沒有為了驗證而啟用排程或建立新場次。

### Docker版實際入會並等待真人測試：2026-10-20

- 操作者新鮮確認「仍在，可以加入」後，已先通知，以原連結／原系列透過管理頁只提交一次人工入會，session=`e25838703fdb4075b84711ccb4757f02`；保留排程暫停，全域政策未改。
- 實際事件：07:19:47Z joining、07:20:14Z connecting、07:21:00Z joined。其後持久狀態依序owner_confirmed→recording，rx_ready=true，B starting→sleeping；API active對應本場、interlock=false、last_error=null，B startup ready／error=null。
- B選中唯一owned sender，正常協商audio sendrecv；output enabled但喚醒前muted。一次快照accepted／peer-push皆574 packets、dropped0，工具呼叫及ASR事件皆0。這只證明媒體接線就緒，不等於使用者已說話或遠端已聽到回答。
- 該時點SQLite本場finals／transcript_increments／provisionals皆0。已請操作者先提供三個新事實，再喚醒詢問一次；待實際發言後核對來源保存、fresh tool及回答。未宣稱三項語音問題驗收通過；之前主機／預加入的負例保留，不因本次joined而改寫。

### 已確認發言但無回答：媒體路徑對照及核准本機切換

- 2026-10-20本場`e25838703fdb4075b84711ccb4757f02`：操作者回報沒聽到回答，依單次短句再確認已說。07:30:04Z→07:32:10Z增加3,020,800 frames／114,817非零samples／1,475 packets，但A／B user delta仍各1，本場application increment只有5字；問題、工具及輸出皆0。非零sample不證明可辨語音，ready不代表端到端成功。
- 使用既有離線Microsoft Hanhan Desktop合成8.388秒中文句，無實體播放、無Teams Join或Google寫入；同一既有Codex OAuth／固定模型，正式`LiveProvider`及sandboxed Chromium，臨時資料、沒有注入ASR或完成事件。
- 首次在活動容器內加開診斷browser時，RPC啟動遇到`BlockingIOError`，cleanup另回報`RuntimeError`；沒有將這個新增probe的失敗當成原場次根因或提高PID額度。之後改用獨立一次性容器，正常清理。
- bridge原生路徑先觀察到無辨識或只剩後半句；直接輸入曾兩次完整，**不能因此推定relay故障**。後續同一Live連線native→direct→native對照，本機relay packetsLost始終0，Live遠端上行累計lost依序612／1,197／1,757，近期fractionLost為0.609375／0.5／0.546875；direct亦受影響。初步relay假設被此反例推翻。
- 交叉控制：主機Chromium153的兩句完整，上行累計lost0；Docker Chromium155 direct兩句仍有缺漏，累計sent859／lost380；把同一套主機Chromium153 readonly掛入Docker，第一句漏問句，兩句後累計lost193。不能只歸咎Chromium版本。8秒當時route monitor為0 events；不據此否定之前route churn，也不將上游WSL issue當成此機已證明的根因。
- 操作者明確核准一次性host-network診斷後，同image／Chromium155／原生capture+relay／固定Live，連續兩句皆完整；Live上行sent1,329→2,356、遠端lost0→0，本機relay lost0。Live下行仍有2個丟包，不能把「上行0」寫成全鏈路零丟包。所有一次性診斷容器已確認移除。
- 操作者另核准切換本機驗證服務。首次stop表單因管理session過期導回登入，未生效；重新登入並核對同一active ID後才提交stop。08:02Z原場次正常封存，state=stopped、cleanup=cleaned、error=rx_stopped，finals2／increments1，summary job=published／attempt1／error=null。停止不把先前無回答改為成功。
- 新增私有mode600的`runtime.docker-host.yml`、`docker-validation-host.env`、`compose.validation-host.yml`；base+auth+host override，不加入local port mapping。Compose render與實際inspect確認host network、無published ports、同immutable image、UID1000:1000、readonly root、drop ALL、既有seccomp／no-new-privileges，PID512等資源額度不變。原bridge設定保留，未改WSL、防火牆、其他服務或模型。
- 正式服務替換成功且healthy，container=`0bc52a557ca5739892b537893fd5c68e904736c070b2ea911f2a372ec59bf761`；實際`ss`僅見`127.0.0.1:18081`，容器`healthcheck --port 18081` exit0。登入管理頁及關閉分頁後API200／started=true／active=null／interlock=false／last_error=null，背景不依附分頁。
- 2026-10-22接續UI核對：排程暫停，Google=`google_authorization_required`。這是外部授權待恢復，未讀token、未猜測過期唯一原因；last_poll_at不是成功讀取證據。操作者再次要求先完成可自行驗證的整個功能，再集中真人驗收。目前沒有重新入會，也沒有宣稱三項真人問題或跨場閉環完成。

### 隔離整合與 TX 靜音時鐘修正：2026-10-22

- 依操作者要求，不再入會或請真人逐句重測。以臨時 SQLite、合成中文語音、正式 worker／A／B／一般 Codex，以及本機協商的 WebRTC sender／receiver 驗證；只有會議准入是本機 fixture，沒有注入 ASR、答案、工具回覆或完成通知，也沒有寫入正式會議資料。
- 同一場連續五題各重新讀一次 SQLite：原始四項事實、更新後預算／交付日／負責人、兩次歷史待辦及再次詢問最新預算。更新後第一次提問即回答新值；兩次待辦各引用一次。未喚醒的工具及輸出為0。正常停止後，真實一般 Codex 發布帶來源的摘要；下一場凍結最新摘要並保留兩項未完成待辦。這些是合成閉環，不是 Google／Teams 真人驗收。
- 舊 TX 在整合中排程 gap=0，送入播放器與實際 destination 波形吻合，但接收端會因 NetEq 補償而拉長／縮短。隔離接收器到另一容器後仍重現，不能歸咎同程序量測。單純移除靜音 HTML audio player 則收包但不解碼，未採用。
- 不呼叫模型的對照定位：開啟 owned output 後立即送 PCM，或先等待30秒再開啟並送 PCM，均正常；**開啟後等1.5秒才送首包**則三次重現，5.77秒輸入在接收端變為6.19／6.28／6.27秒，包絡相關係數0.647–0.667、RTP lost=0。原因是空 destination 沒有持續提供靜音樣本，不是 buffer 排程缺口或內容配額。
- 修正 `b_browser_tx.py`：從播放器建立起，以零值 ConstantSource 維持 destination；pause 留下靜音時鐘，最終 stop／pagehide 停止並斷開。既有 mute、owned sender、授權重查、撤權丟棄、0.5秒 queue及還原規則不變。
- 新增實際 sandboxed Chromium／本機 RTP 回歸 `test_waiting_for_pcm_and_repeated_speech_preserve_received_audio`：修正前兩段長度差0.65／0.35秒而失敗；修正後兩段均符合長度差小於0.15秒、包絡相關係數大於0.9。連同 TX identity、dual policy、leases、null sender 共45項通過；未掛載 OAuth，也沒有實體播放。
- 尚未把波形吻合當成完整語音成功：一次 Native 整合的文字答案完整，但首題實際送出的有效語音只有約1.54秒。另一次未喚醒、一次 Live 啟動等待 media/datachannel timeout，以及加開第二 Chromium 的啟動失敗均保留為負例；正在區分 provider 提前輸出、capture／turn 准入及已送出語音的實際內容。
- 後續 host-network 整合也觀察到 B remote-inbound lost16及3035；最初兩句上行0不能推廣為所有通道零丟包，也不能單憑累計 lost 宣稱每段發言丟失。沒有因此改模型、擴大資源額度或放寬安全 gate。
- 可用全回歸的兩次容器執行未完成：首次測試 runner 漏了正式 Compose 已有的 init，累積361個 zombie 並觸及 PID 上限；補回 `--init` 後，假子程序測試因 `/tmp` 的 `noexec` 而失敗，後續等待掛起。已停止兩個測試容器，未提高 PID／記憶體額度或放寬容器 mount 權限；轉用專案既有 `.venv312`，清空繼承環境並指定空白 HOME／CODEX_HOME。僅排除已知缺來源的兩個歷史 suite，不能稱原始完整 discovery。
- 固定版 Codex 的 [voice-host 契約](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/voice-host/README.md)同樣要求 ordered `oai-events` 實際 OPEN 才算 transport ready；ICE／peer connected 不足以替代。本次兩個連線負例尚未證明 SCTP／DCEP 根因，未更改 channel ID、協商模式或既有等待期限。
- 既有本機開發環境完成可用回歸：981項、284.156秒、errors0／failures0／skips0，包含實際 Chromium／Node 邊界及來源、摘要、排程、管理頁測試。仍排除 `test_bridge_integration.py`、`test_media_bridge.py`，不是原始完整 discovery。
- 加入有界連線摘要後，A／B 分別在1.211／4.468秒開啟 channel；兩端 application m-line均接受、SCTP／DTLS connected、ordered channel ID0。未改協商設定即恢復，前兩次 timeout 的根因仍未證明。此輪首題8.47秒有效 PCM 在接收端仍為8.47秒，包絡相關係數0.986、未觀察到 closed capture gate 丟棄有效音訊；但內容驗收失敗：A已將合成專案／預算辨識錯誤，B回答資料不足。工具有送出39筆來源、2,894 bytes完整payload；B remote-inbound累計lost1,127，不能把這輪當成功，或把所有問題歸因TX。
- 再一輪完成五題：每題工具1次、每題marker1次；五段TX波形均過既定門檻，首題及更新題的接收音訊再送回A辨識，四項／三項事實皆讀回。這是同一A上下文的機器讀回，不是獨立辨識或真人聽感證明。歷史待辦兩次各引用一次、摘要及下一場記憶通過；**整輪仍失敗**，因首題B文字為「小玲」，當題SQLite及送出快照是「小林」。最後預算題另觀察到重述兩次，只有一次工具與marker，中間有額外Native user／assistant turn；不能以再加一次查詢、文字去重或切掉後段音訊當修復。
- 該輪五題未觀察到有效音訊在closed capture gate被丟棄；這不追認之前1.54秒首題。A／B remote-inbound累計lost40／41，Linux UDP InErrors、RcvbufErrors、SndbufErrors、InCsumErrors前後均0；只能排除這些計數器所涵蓋的本機丟棄，不能斷言整條網路或provider無問題。
- TX候選映像 `teams-assistant:local-tx-clock-20261022` 已建成，immutable ID=`sha256:92fd8489e097635bdaa6046c53171451b701e82bb0a91dc1d2612553397dbd61`，**未部署到待命服務**。先前把整個build設為network none造成apt層cache miss而失敗；恢復README的標準建置方式後沿用既有工具層，Python仍由目前source在network none／no-index下離線建置安裝，未改Dockerfile或依賴。
- 新映像無source overlay、無OAuth的實際RTP smoke：等待30秒，再連續三次開啟發聲後等1.5秒才送PCM；接收長度誤差最多0.01秒、包絡相關係數至少0.989、RTP lost0、三次drain成功。CLI help可執行；Codex版本為`0.159.0-alpha.3`（空白暫存HOME仍有PATH alias警告，未抑制）。已安裝`b_browser_tx.py` SHA256=`27d2320c4fbd99a7b15430f059e4cb84ec1ca1013757452846a3b97c7e35af57`，與本次source一致。純合成smoke中的Python3.12 audioop棄用警告保留，沒有更換runtime掩蓋。

### 歷史可讀契約與完整合成閉環：2026-10-22

- 額外控制僅存在於臨時 harness：將B收到的PCM改為零，以明確合成的application question測試context-only能力；這不驗證正式喚醒，也未採用為產品改線。Native可以回覆本場資料，但兩次歷史問答仍錯稱沒有資料。實際送出payload含完整歷史摘要及兩項系列待辦，問題不是沒有讀SQLite。
- 歷史列原本將整個歷史物件編碼進`text`，造成第二層JSON字串。另一控制只把歷史原文與metadata分開，兩次歷史問答皆正確；該輪A卻有辨識錯誤及高上行lost，仍判整輪失敗。這支持改善可讀契約，不證明所有錯答或重述只有同一原因。
- 正式修正`MeetingStoreMinutesReader`／`MeetingQuestionContext`：歷史`text`直接為原文，其餘原有欄位移至`historical_context`並完整送出。來源ID生成、來源session／revision、品質、凍結歷史、待辦狀態及引用保持；字元範圍對應原文。沒有解析任意會議JSON發言、依文字去重、配額、改B輸入或以歷史initialItems跳過工具。
- 新增回歸先在舊格式失敗，再通過：歷史完成／未完成狀態分離、特殊字元原文及引用、JSON形狀的會議發言不能變成歷史metadata；既有大篇幅歷史／跨場隔離亦通過。五組相關suite共108項通過。
- 正式接線合成驗證（非上述控制）：真實A／B Native、語音喚醒、每題新查SQLite與模型回答，沒有注入問題、ASR、答案或完成事件。連續五題全部通過：本場四項事實、更新後第一次提問即回答新值、歷史待辦兩次各念一次、再次正確回答最新預算。五段接收音訊長度差最多0.07秒、包絡相關係數至少0.947；兩題接收語音機器讀回亦含全部指定事實，closed capture gate有效音訊丟棄觀測為0。
- 正常停止及response清理完成，真實一般Codex發布帶來源摘要；下一場引用這份摘要，保留原兩項未完成待辦。當輪新增的交付待辦仍標明來源為未定稿片段及交付內容未提供，沒有將來源品質改成provider final。
- 修正後可用全回歸：既有Python3.12隔離環境共982項、288.588秒，failures／errors／skips均0；仍在discovery前排除缺原始來源的`test_bridge_integration.py`、`test_media_bridge.py`。不是完整原始套件discovery，也沒有為通過而刪測試、增加正式資源額度或放寬sandbox。
- 最終候選`teams-assistant:local-readable-history-20261022`，immutable ID=`sha256:ab17fa0c8252375520db1deca9d255f7e4c380e5671c93783b2c469fae059f27`，包含TX與歷史契約修正。標準Dockerfile／既有公開cache建置25.78秒；安裝來源與前述合成受測來源的三個變更模組SHA一致。無source overlay、無OAuth、network none的已安裝SQLite consumer九項驗證通過，實際CLI help通過；沒有以host來源遮蓋舊映像。
- 操作者明確選擇「更新本機待命服務」後，僅修改私有host env的固定image ID，沿用三份現行Compose配置，以`--no-build --pull never --wait`完成本機切換。新container=`d04d612ca3d795a2851d6502c9d597347683ba11e341167a0c0d771d1f68a472`，實際image為ab17fa候選；資料與同一Codex授權掛載、UID1000、readonly、drop ALL、no-new-privileges、seccomp、pids512／4GiB／2CPU／1GiB shm均保留。三份私有配置仍為0600，listener實測僅127.0.0.1:18081。
- 更新後實際瀏覽器重新登入及畫面驗證通過；排程暫停、active=null、capture_interlock=false、last_error=null、背景工作3。關閉分頁後實際CLI healthcheck exit0，容器running／healthy。Google仍為authorization_required，沒有啟動重新授權、入會、新增活動或啟用排程。臨時整合／接收／音訊harness與合成音訊fixture已清除，保留永久回歸及本文件證據。
- 這是一輪通過的合成閉環，不是正式Google活動、真實Teams准入、真人聽感、模型必然採用來源或網路根因修復。之前的少音、錯答、重述及datachannel timeout不改寫為成功；仍需集中真人驗收。

### 校時租約與晚到音訊修正：2026-10-27

- 真人負例：操作者聽到回答但開頭斷音，持續提問後無回應。當場保存138個application segments／0個provider final，8題准入／8次工具交付；最後由`stale_execution_lease`中斷並封存，非內容配額或問題次數上限。Docker未restart／OOM，媒體子程序已退出；保留cleanup interlock及partial來源品質，不以後續摘要發布當成整場成功。
- 同boot journal相鄰記錄的wall差76.632328秒、monotonic差41.990082秒，差值為正跳34.642246秒；resolved亦記錄Clock change detected。最後心跳到該記錄的wall age約34.998488秒，扣除正跳約0.356242秒，隨後0.225905秒封存。這證明牆鐘變動及舊租約判定的關聯，不指認宿主校時元件，也不把monotonic差值當成所有suspend情境的實際經過時間。
- 先以臨時SQLite重現：只增加wall-clock 34.642246秒，舊碼即拒絕來源寫入／續租並回收場次。修正capture／summary共同計時為Linux `CLOCK_BOOTTIME`＋boot identity，wall timestamp只供稽核；未延長租期或取消交易內fence。回歸涵蓋向前／向後校時、真正經過期限、換boot、summary發布及舊schema來源保全。舊active lease不換算復活，仍依既有recovery封存／重排outbox。
- TX舊碼在PCM欠料後會重加首包headroom；修正僅在每段首包保留headroom，後包晚到立即接播。以下舊值為實際Chromium排程觀察；新版另由AudioWorklet取得render-thread樣本，兩輪每段均完整保留960 frames、重用同一track，實際空白與排程值相符：

  |觀察|PCM晚到 ms|追加headroom ms|排程空白 ms|
  |---|---:|---:|---:|
  |舊碼重現|26.667|160|186.667|
  |新版候選，第1輪|30.667|0|30.667|
  |新版候選，第2輪|30.667|0|30.667|

- 新增`pcm_admission`與`tx`固定計數，保留PCM原准入條件及首個錯誤。實際sink量測經diagnostic allowlist寫入SQLite再讀回一致；無效型別、負值、巨大值及私有內容由回歸驗證不會混入持久診斷。`direct`不是audibility，`available=false`不是零欠料；計數有界不形成新的停答門檻。原0.5秒待播積壓與所有授權／owned sender／cleanup保護均保留。
- 相關142項先通過；Python3.12可用全回歸991項、278.228秒，failures／errors／skips均0。仍在discovery前排除缺來源的`test_bridge_integration.py`及`test_media_bridge.py`，不稱完整原始discovery。將新增波形測量由ScriptProcessor改為render-thread AudioWorklet、要求完整960 frames後，4項實際Chromium TX回歸再次通過，包含本機RTP包絡與重複播音。
- 候選`teams-assistant:local-lease-tx-20261027`，immutable ID=`sha256:652619912b114b2265de4e3a05d0b5494f5f5740f82935eb81a061901fa9d8fa`；標準Dockerfile／既有公開cache建置15.02秒，Python仍由目前source以network none／no-index離線安裝。已安裝`assistant_store.py`、`meeting_store.py`、`b_browser_tx.py`、`b_native.py`、`diagnostics.py`五個SHA均與受測source一致，實際CLI help通過。
- 無OAuth／正式data／source overlay、network none、非root／readonly／drop ALL／no-new-privileges的候選smoke通過：兩個新Python程序分別模擬wall正跳及回撥後仍可續租／保存來源；實際等待到期後capture寫入遭fence，interlock保留，summary可由新generation接手。另以既有seccomp、sandboxed Chromium及AudioWorklet重現上述兩輪晚到音訊，數值觀測可持久化；沒有關閉sandbox、增加正式資源額度、呼叫model或Join Teams。
- 本輪**未部署候選、未遷移正式資料庫、未解除interlock、未啟用排程或重入會**。正式服務維持ab17fa版本。本機修正不能回溯證明真人每次缺字的唯一原因；當場沒有逐包TX、首段拒收或接收端波形，後續仍需具備新觀測的授權真人驗收。
- 本輪臨時回歸runner、租約smoke及音訊smoke三個harness已刪除；永久回歸保留。完成前確認既有服務仍為ab17fa、running、restarts=0，未以候選容器代替正式服務狀態。

### 核准的校時／TX本機服務更新：2026-10-27

- 完成上述自行驗證後，操作者明確選擇「更新本機待命服務」，並另行允許使用既有本機管理密碼完成登入驗證。範圍僅本機`teams-validation`，不入會、不啟用排程、不解除interlock；沒有變更NAS、全域網路、OAuth或模型配置，也沒有push。
- 停機前readonly查詢確認scheduler_paused=true、active leases／running runs／pending或running summary jobs均0。舊容器收到SIGTERM後exited143，非OOM；不把143寫成正常exit0或真人清理成功。停機SQLite及備份的integrity_check均ok，25張表筆數一致；一致性備份為data root內`backups/pre-lease-boottime-20261027T103519Z.sqlite3`，目錄0700／檔案0600，SHA256=`b5d1cdee58d95979e2361f1211ce1a59e75c0d3c230ec32b083661c56ba5970b`。
- 僅更新私有host env的固定image pin及其原有env_file傳遞值，其餘resolved service配置相同；三份私有配置仍為0600。沿用現行三份Compose，以`--no-build --pull never --wait`切換至`sha256:652619912b114b2265de4e3a05d0b5494f5f5740f82935eb81a061901fa9d8fa`。新container=`4b7ff7d87c539b1a245155b5f9430912fdea507477ea9037d394a35b47e7bfbc`，實際image吻合且healthy。
- 正式庫已加入`boot_id`／`expires_boottime`，integrity_check通過。遷移後、登入前以備份雙向EXCEPT核對既有欄位：24張表完全相同，原assistant_audit所有列保留，僅新增1筆`settings_changed`；這對應既有daemon啟動時因interlock維持暫停的路徑。沒有活動租約被復活，沒有修改來源或摘要。
- 實際容器的UID1000、host network、readonly、drop ALL、no-new-privileges、seccomp、init、pids512／4GiB／2CPU／1GiB shm及資料／既有授權掛載均與切換前相同。listener再實測僅127.0.0.1:18081。
- 實際瀏覽器登入及畫面驗證通過；管理API為started=true／closing=false／active_session_id=null／background_task_count=3／capture_interlock=true／last_error=`capture_cleanup_review_required`。排程暫停、Google仍為authorization_required；未將最後輪詢時間當Google讀取成功。僅使用經核准的管理密碼填入本機登入表單，沒有顯示、另存或更換密碼。
- 關閉管理分頁後，已部署程式的`teams-transcription healthcheck --port 18081`仍exit0。清理保護鎖維持；尚未執行新版真人Teams驗收，不把部署健康或本機合成波形當成真人句首／連續提問通過。

### B 原生音軌輸出切換：2026-10-27

- 最新真人仍回報整段斷音、重複及錯答，驗收失敗。當場3題工具完整send-return，但沒有模型消費ACK；144包PCM在`awaiting_new_assistant_turn`被host丟棄、TX欠料計數0。這是移除host輸出PCM准入／排程的依據，不是全部問題唯一根因或模型內容正確的證明。
- 正式輸出改為`ResponseLivePeer` native receiver → 跨隔離context的持續本機WebRTC relay → 原owned／已協商Teams sender。`NativeTrackOutput`不搬運PCM、不設FIFO／headroom／回答總時限；每題fresh SQLite及完整內容交付保留。資料chunks返回後先核對授權並接通，才dispatch最後marker；失敗取消立即mute，commit仍需marker-return及scope重查。
- 實際Chromium第一版原生接線曾無outbound RTP；直接將incoming track交給sender不足以啟動decoder。加入muted element後雖有RTP，native接收energy卻為0；獨立WebAudio觀察證明訊號存在。最後採未muted element→MediaElementAudioSource→零增益destination維持原生解碼，source／target／下游均取得非零native energy。本機不發聲，不在production加入worklet meter或PCM callback。
- 下游在不同context及PC量測**經owned sender後**的合成波形，不是讀source排程圖。校準時gate關閉仍有接收活動，耳端fixture為靜音；等待實際接收quiet後才測授權波形。已驗證同track兩次回答、撤權後靜音、A capture仍0條回灌／1個原meeting peer、最終還原原靜音sender及NoPhysicalSink拒絕解除靜音。
- 初始測試來源在decoder接上前就送RTP，曾觀察source receiver原生加速移除15,781samples，造成首輪tone縮短；不是Python新增丟包。fixture改成先協商空sender、明確tone／pattern時才送音，同時涵蓋「第一題前沒有RTP」啟動。修正後source acceleration為960samples；保留原生jitter會調整節奏、mute不flush遠端buffer的界線。
- 下列通過的controlled runs在每輪中段刻意停止Python事件迴圈650ms，下游仍依330→550→880Hz順序收到訊號，沒有重複tone序列。10ms量測窗的實際結果：

  |輪次|首段辨識 ms|中段辨識 ms|尾段辨識 ms|內部未分類／無訊號窗最大 ms|
  |---|---:|---:|---:|---:|
  |host source，第1輪|350|1980|340|10|
  |host source，第2輪|350|2000|350|0|
  |installed candidate，第1輪|340|2000|350|0|
  |installed candidate，第2輪|350|2000|340|0|

- 第1輪10ms窗為音調切換附近的混合頻率，RMS仍非零，不當成10ms無聲證據；native編解碼／jitter不是逐sample無損複製。有效quiet與所有已知assistant done僅是本機完成heuristic，`drained=false`；完全晚到turn通知前的已靜音音訊沒有FIFO可回補，不能宣稱無限continuation／遠端全文通過。
- 初次可用回歸971項有6個failure／1個error：包含停止transceiver後錯誤replaceTrack、muted外來track替換的policy窗口、receiver concealment尾音與固定等待的錯誤假設、兩個舊PCM QA fixtures，以及0.35秒capture提早取消原生setup。原始失敗保留；partial-start測試已改等待真正B啟動失敗，再證明A仍能保存revision2，最後明確stop，單項實際Chromium驗證通過。
- 修正sender cleanup／policy後，stopped transceiver不復活、ended original不重掛，muted foreign replacement也立即fault。兩個實際撤銷／navigation控制均在outbound packet24停止，之後下游received packets增量0；接收端另產生55,553／56,033個concealed samples，尾音460ms後結束，最後200ms RMS為0。未加production等待／重設或buffer，沒有把遠端補音當持續越權送音。
- 修正上述安全項目的focused browser run為20/21通過，Node sender6/6通過；中段每個10ms窗的嚴格波形檢查曾間歇失敗。後續診斷執行走到正常stop，但其外加teardown probe讀取已刪除的`__bTX`報錯，不能計為乾淨通過。保留原斷音assertion並加入低RMS窗的精確位置／頻率診斷，未用放寬門檻換綠燈，尚無matching stats可指認該次原因。
- 第二次整合974項只剩foreign-owner cleanup失敗：不能因全域便利指標換成外來物件，就留下自己仍持有的ended sender track。清理改依owner map＋精確sender／track身分，保留外來物件及外來替換track；stopped／ended限制不放寬。
- 最終Python3.12可用全回歸**974項、299.963秒，failures／errors／skips均0**，包含原嚴格中段波形、caller／marker交錯、fresh SQLite、A持續保存、sender身分與清理、Native scalar QA。依既有前提在discovery前排除缺來源的`test_bridge_integration.py`／`test_media_bridge.py`，不稱完整原始discovery。成功回歸不抹除上述間歇波形負例，也不是已找到該次唯一根因。
- 標準Dockerfile候選為`teams-assistant:local-native-track-20261027`，immutable ID=`sha256:a22d154c6dc187f099dae6ac61333dd2b138de7fc1a57a330eb2aea363073252`。首次全域`--network=none`改變RUN cache key，apt步驟無法下載而失敗；改回既有network mode、以不可達loopback HTTP／HTTPS proxy禁止依賴下載後，apt及Codex步驟均實際CACHED，12.02秒完成。Python仍用既有公開wheel context、`RUN --network=none`／`--no-index`從當前source安裝，未更換依賴／模型／TLS或sandbox。
- 已安裝候選在`/tmp`工作目錄、沒有應用source overlay，8個變更模組`b_native`／`b_native_output`／`b_browser_tx`／`b_dual_policy`／`b_live`／`browser`／`diagnostics`／`qa_probe`的site-packages SHA與受測source逐一一致。實際CLI help及`codex-cli 0.159.0-alpha.3`通過；無OAuth／正式資料掛載。Codex version因刻意不存在的隔離CODEX_HOME提示無法建立PATH alias，仍exit0；沒有讀取既有憑證。
- installed smoke使用uid10001、readonly、drop ALL、no-new-privileges、既有seccomp、512pids／4GiB／2CPU／1GiB shm及tmpfs。為本機RTC使用既定routed bridge，不冒稱network-none；所有頁面由fixture本機攔截，沒有model或Teams呼叫。只掛載合成fixture與臨時smoke harness，runtime modules來自已安裝package。
- 第一次installed smoke第1輪通過，第2輪零交越分類為`330→550→880→550`而失敗；當次未保存各run長度，不能判定末尾是短暫編解碼轉態還是重複音訊。新增每個tone run長度及位置的診斷，沒有修改production、門檻或順序assert；後一次installed smoke兩輪全部通過上述波形、撤權靜音、同track、A無回灌、NoPhysicalSink及原sender還原。原失敗保留，不能宣稱所有音訊品質負例已解決。
- 本輪未呼叫真實provider、未部署候選、未遷移正式庫、未啟用排程或Join。完成前現行容器仍為`4b7ff7d87c539b1a245155b5f9430912fdea507477ea9037d394a35b47e7bfbc`／image`652619…`，running／healthy／restart0；這不代表該服務無活動場次。兩個disposable smoke容器均自動移除。
- 本輪四個臨時source／relay probe、native smoke及regression runner已移除；永久consumer-visible回歸與合成fixture保留。README、B契約、HANDOFF及根目錄AGENTS已更新，不保留舊PCM輸出為正式fallback。

### 核准的真 GPT Live 隔離驗證（原生音軌候選）

- 操作者允許繼續真provider驗證。使用既有部署的本機Codex登入，沒有讀取／複製token、Teams入會、服務切換或正式資料操作；實際執行host source，不冒稱installed candidate驗證。Codex為`0.159.0-alpha.3`，正式start要求`gpt-live-1-codex`／v3／audio／WebRTC；此model名稱是request值，不是獨立的server-reported model證據。
- 輸入為Windows本機SAPI `Microsoft Hanhan Desktop`產生的合成國語，記憶體轉成24k mono PCM，經正常喚醒／ASR／application question流程；沒有繞過分題或注入文字答案。來源為隔離temporary SQLite；每題由正式`MeetingStoreMinutesReader`讀取。正式`NativeLiveResponder`→`NativeTrackOutput`→`BrowserTXSink`→合成owned meeting sender→另一context的WebRTC receiver；本機實體輸出維持靜音。
- 第一個真Live session在同一thread連續問三次「本場會議最新核定的預算是多少？請只說金額」，第二題前修訂SQLite來源：

  |輪次|provider完成文字|SQLite／交付版本|SQLite讀取／tool次數|下游RMS > .01秒數|
  |---|---|---|---|---:|
  |1|新臺幣十萬元。|2／2|1／1|1.27|
  |2|新臺幣十二萬元。|3／3|1／1|1.56|
  |3|新臺幣十二萬元。|3／3|1／1|1.55|

- 三題金額各出現一次、各一個assistant turn.created及transcript.done；同一output track，完整工具marker send-return後均正常回到sleeping，無runtime error或truncation。`model_consumption_verified=false`、`drained=false`保留；更新後的首次回答正確，不以send-return冒稱模型消費ACK。
- 原生音訊不是無損承諾：三次snapshot的Live receiver累計concealed samples為21,960／51,213／51,213，本機relay均0，下游均2,076；packetsLost均0。這些計數不足以定位單字斷音；非零音訊及正確provider文字不等於已逐字聽驗下游聲音。
- 第一session耗時51.252秒；正常stop ACK／closed皆收到，native cleanup=`cleaned`、owned processes=2／live=0／reaped=true、pending close=0。私有合成收據：`/tmp/teams-native-real-yxpg_7e0/result.json`。本次短答通過不抹除前述間歇波形負例。
- 第二個真Live session補測三項多句待辦的完整原文，每題仍走正常語音喚醒。第一題為「週三前完成青松專案測試報告。週五前確認白樺專案驗收清單。下週一整理銀河專案的風險紀錄。」第二題前只把SQLite第二項的週五改成週四，第三題重問：

  |輪次|完成文字檢查|SQLite／交付版本|SQLite讀取／tool次數|下游RMS > .01秒數|
  |---|---|---|---|---:|
  |1|三項全文各出現一次，第二項週五|2／2|1／1|7.71|
  |2|三項全文各出現一次，第二項已改週四、無週五|3／3|1／1|7.66|
  |3|三項全文各出現一次，維持週四、無週五|3／3|1／1|7.77|

- 多句三題均僅一個assistant turn、同output track、正常回sleeping、無error／truncation；assistant turn從created到done約11秒。比對忽略標點／空白及「周／週」字形，沒有放寬內容或重複次數。provider完成文字與三項原文相符，但沒有把下游音訊重新辨識或真人聽驗；不能據此保證每個字都已可聽。
- 多句run三個snapshot的Live receiver累計concealed samples=21,314／41,782／60,785，累計packetsLost=1／1／1；relay concealed=1,320／1,320／1,320，下游=6,192／6,192／6,192，兩者packetsLost均0。第二、三題本機relay／下游補音計數未增加；Live端補音仍增加，不能稱為零斷音或直接判定舊問題唯一根因。
- 第二session耗時71.983秒，正常stop ACK／closed與native cleanup皆成功；owned processes=2／live=0／reaped=true、pending close=0。私有合成收據：`/tmp/teams-native-real-gvldjfdj/result.json`。
- 兩次共6題完成指定內容與實際下游音訊檢查。本輪未修改production code、未加內容／時長卡控；臨時harness與兩組SQLite／runtime已清除，保留私有JSON收據。最後確認現行容器仍為`4b7ff7d87c539b1a245155b5f9430912fdea507477ea9037d394a35b47e7bfbc`、image `sha256:652619912b114b2265de4e3a05d0b5494f5f5740f82935eb81a061901fa9d8fa`、running／healthy／restart0。候選尚未部署；仍待明確服務更新授權與真人Teams驗收，不宣稱歷史全部斷音問題已修復。

### 原生音軌候選部署與真人入會嘗試：2026-10-27

- 操作者表示「繼續，我在會議等你」，承接前述本機候選切換及原連結真人測試。操作前已通知；未啟用排程或修改Google授權，沒有NAS／其他部署操作。
- 停機前管理API active=null、capture_interlock=false；readonly SQLite確認execution leases僅completed41／expired2，runs僅failed12／interrupted2／stopped12，summary jobs全部published17。舊容器正常走Docker stop；停止後SQLite backup為私有data root的`backups/pre-native-track-20261027T153539Z.sqlite3`，0600、integrity_check=ok。
- 僅更新私有`docker-validation-host.env`的固定image；resolved Compose排除image及既有`TRANSCRIPTION_IMAGE`環境傳遞後完全相同。以原三份Compose、明確本機daemon及`--no-build --pull never --wait`切換完成；container=`1e4ab453e209481300c7281f6e13e6e94f664dc1bd772af12474506cc59a11f1`，image=`sha256:a22d154c6dc187f099dae6ac61333dd2b138de7fc1a57a330eb2aea363073252`，running／healthy／restart0、UID1000:1000、host network、readonly=true。
- 新版實際登入管理頁並操作manual表單；沿用原series、錄音及發聲授權，只建立一次場次並送一次Join。occurrence=`d50f24d729414205b49f6de55874e6e2`、session=`55b167917684470ca3462d6dbf91da40`。15:36:54Z進入joining、15:37:02Z connecting，15:38:54Z入會等待逾時。
- 結果為**未成功入會**：`meeting_join_failed`／`timeout`、phase=`post_join_wait`，Join dispatch已完成；最後觀察距失敗370ms、總觀察192次，category=`multiple_known_categories`（admission_lobby／login_prompt／prejoin）。lobby_message=1、sign_in_button=1、join_button=1、computer_audio_radio=1，未看到leave控制。這證明有等候室訊號，不能單獨斷言操作者未准入或判定唯一UI原因；不調大timeout或停用准入檢查掩蓋問題。
- Capture state=failed／end_reason=preparation_error／cleanup_status=cleaned／rx_ready=false；沒有本場來源或B回答可驗收。最後daemon started=true、active_session_id=null、capture_interlock=false、last_error=null、response=null；管理畫面及實際容器healthcheck exit0。已關閉管理分頁，不是停止服務。已告知操作者失敗並等候新的在場／准入確認，沒有自動重試；尚未進行原生候選真人聽感驗收。

### 原生音軌真人重試：2026-10-28（已入會，待語音驗收）

- 操作者回覆「繼續」後，先通知再以原連結／原series送出一次manual Join。現行container仍為`1e4ab453e209481300c7281f6e13e6e94f664dc1bd772af12474506cc59a11f1`／原生音軌image `a22d154…`，running／healthy／restart0，沒有重新部署。入會前API active=null／interlock=false／last_error=null。
- 本輪session=`63037b2da04d43aeb4078507f0be9d08`、occurrence=`b1ce89ee902a4a8689e4825d33e62781`。11:08:45Z joining，11:08:53Z connecting、join_dispatch_completed=true；等待時即時請操作者准入，未追加Join。11:09:48Z實際live_connected／rx_ready／receiving_ready，11:09:54Z response_ready及response_output_authorized；capture進入recording，B startup_phase=ready、state=sleeping／error_code=null。
- 11:10:49Z快照仍recording／rx_ready=true，SQLite已保存1筆transcript increment；這是application來源，不冒稱provider final或文字內容正確。B tool_calls=0、question admitted=0、assistant transcript counters=0，尚未完成真人問答驗收；待喚醒時output_enabled=true／output_muted=true符合設計。已請操作者先說測試事實，再以「小白小白」提問，確認完整播音及答案；服務與本輪保持運作，僅關閉管理分頁，沒有stop／重部署。

### 原生音軌真人回饋與會後摘要：2026-10-28

- 操作者回報「聽起來可以，我已結束會議」，詢問摘要是否生成。針對同session `63037b2da04d43aeb4078507f0be9d08`查詢時，capture仍recording、run仍running、summary_jobs及summary_revisions均無本場資料；沒有把使用者結束會議等同已完成host封存。
- 通知後透過正式管理頁、核對精確session的`/actions/stop`正常停止；11:15:40Z run=stopped／operator_stop、run error_code=null，capture cleanup_status=cleaned，capture的`rx_stopped`保留。封存後summary job=pending，沒有直接改庫／重啟／重送Join／手動偽造摘要。
- 11:16:15.846789Z summary job=published、attempts=1、error_code=null。SQLite summary revision1，format=json-v1、generation_mode=codex、實際model=`gpt-6.1-sol`、provider_requirement_met=true；source_version=154，source SHA=`b233ccb081f16ca687091cf9581cda8027db0d8609eb299ed4d42c12a1489fb7`，摘要 SHA=`625bc3b42ccf94937540742bc3d113f60ba65e0745eb9e3731382be41b205017`。
- 保存151筆來源：provider final10、application segment141；不是全部provider定稿。衍生摘要重點4／決策3／待辦3／未決問題4；memory_tasks中本場3筆待辦均為open。只記計數及收據，不將實際會議文字加入source changes。
- 實際管理歷史頁確認摘要模型與四分類可見；最後daemon active_session_id=null、capture_interlock=false、last_error=null，管理分頁已關閉。操作者正面聽感回報保留，但未擴張成所有歷史音訊問題已解決；本次需要operator_stop才觸發摘要，不宣稱自動偵測會議結束已通過。

### 前次記憶與三分鐘口述驗證準備：2026-10-28

- 操作者要求以原連結測前次內容／待辦，並朗讀新增約三分鐘稿後問答。第一輪session=`1372c499daaf4a9cab8e887f1f0e6160`、occurrence=`f1210dd2748f42d68e559ca54a674139`：Join dispatch完成，但post_join_wait／timeout，最後只有登入／預覽控制訊號，cleanup=cleaned。沒有A／B或內容驗收。
- 操作者另要求「再重新登入」後先通知，只送出一次新的manual入會工作，session=`9fc72a7c6c2844be956c59c45eb1fb8d`、occurrence=`419b45e479e848fda9d74e3222295133`。這次失敗在audio_choice／timeout，browser_continue=error／computer_audio=error；join_dispatch_completed=false、Join未嘗試，不能歸因操作者沒允許准入。最後cleanup=cleaned，daemon active=null／interlock=false／last_error=null。
- 本輪prior_json確認綁定剛才已發布的session `63037b2da04d43aeb4078507f0be9d08`、revision1，available=true／truncated=false；系列未完成8項、前次新增3項。此為host凍結證據，不是模型已讀或已答對。
- 準備771個中文字的虛構青禾客服試辦稿，包含前中後段事實、十八萬改二十一萬的修訂、分工、期限及兩種風險處理；每分鐘約250～270字約三分鐘，實際依語速。稿件及問題僅提供操作者：`local://meeting-three-minute-reading.md`；未寫入會議SQLite／initialItems或工具來源，避免預載答案污染聽取驗證。
- 非入會檢查未啟用實體麥克風、未呼叫模型。工具瀏覽器停在launcher，工具click報disabled/inert但DOM標記為enabled；已回報工具問題，不把工具層症狀直接套用到正式adapter。獨立sandboxed Playwright兩次到`teams.microsoft.com/light-meetings/launch`，未找到舊launcher的joinOnWeb，畫面證明已是裝置權限提示＋預覽，radio_count=0／Computer audio不可見。探針沒有正式owned input注入，因此權限提示不能視為正式失敗唯一根因；沒有按無音訊入會或跳過安全檢查。兩次probe均closed，管理及檢查分頁已關閉，沒有永久程式修改。兩項真人測試仍未完成。
- 操作者另明確要求「先再登入一次」後，未改程式／設定，以原連結／原series只新增session=`2174edb9dc08452c80d42cbb19afced0`。入會前active=null／interlock=false；本輪實際join_dispatch_completed=true、connecting／error=null，已通過前一輪卡住的預覽階段，但尚不能稱已入會。已即時通知操作者允許准入，不追加重試。
- 上述單次重試隨後實際准入成功：session `2174edb9dc08452c80d42cbb19afced0`／occurrence `cee9994a552d4d07967efca5d7168be9`，recording／rx_ready=true、B sleeping／startup_phase=ready／error=null。prior_json為前次`63037…`摘要revision1、available=true／truncated=false。沒有修碼或繞過音訊檢查；這次成功不證明前一輪預覽失敗原因已解決。已提供兩項測試順序與朗讀稿，本輪保持運作，待操作者實際問答及聽感回報；只關閉管理分頁。

### A 版行事曆正式接線與隔離驗證：2026-10-28

- 依操作者選版實作正式週時間格、會議預覽／展開工作區與獨立設定頁。資料來自 SQLite；新增日曆／詳情 API 與獨立 B 問答日誌，保留原有控制、Origin／CSRF、來源品質及 lease fence。沒有修改音訊傳送、分題或完成判定。
- **可用回歸 1001 tests／308.992 秒，0 failures／errors／skips。** 使用既有 Python 3.12／工具與空 `CODEX_HOME`。明確排除缺歷史實驗來源的 `test_bridge_integration`、`test_media_bridge`，不是完整原始 discovery。
- **實際 Chromium／正式 app／新暫存 SQLite：** 登入；週時間軸、重疊／跨日／清晨／夜間場次；系列前次摘要與待辦狀態保存；單場隔離；舊詳情請求延遲後不覆蓋新選擇；空日期與歷史深連結；本地時間人工場次表單及返回新場次；獨立設定頁。
- **實際持續更新與生命週期：** 透過原 Join 控制啟動 constructor-injected runner，顯示記錄中、逐字來源及未對應問題的回答。三次新來源／問答更新不換掉既有節點、不搶 focus，保留非零閱讀捲動；HTML 標記保持文字。1440px／390px 無整頁橫向溢出。關閉管理分頁後 runner 繼續；重新開啟後正常 stop、來源封存、受控摘要 published，保存 4 筆來源／8 筆問答，停止／輸出控制消失。登出後 API 401。
- **打包：** `python -m build --wheel --no-isolation` 成功，CSS／JS 與來源 bytes 一致，wheel 不含原型／fixture。啟用的 Python 沒有 pip，未安裝新工具；改用已建置 wheel 解包執行 CLI `--help`、CSS／JS HTTP routes 及未登入 API 拒絕檢查，均通過，不冒稱 pip 安裝成功。
- 初次隔離 launcher 用空 prior context，被既有 `meeting_exists_conflict` 正確拒絕；只修正 fixture 使用 claim 時凍結的快照，未放寬正式 guard。其他 browser wait 依實際空狀態與摘要 tab 校正，沒有因 wait timeout 重送 mutation。
- [機器收據與受測來源 SHA](calendar-ui-a-20261028.json)。隔離程序／瀏覽器已停止；本輪沒有部署、碰既有資料庫、入真實會議、使用 OAuth 或呼叫模型。Native 語音開頭不順仍 **Pending**；本次不宣稱真人音訊或內容驗收通過。

### A 版 UI 本機 Docker 更新：2026-10-28

- 操作者明確核准更新本機服務。建置前以實際 Docker context 重現 `calendar.css`／`calendar.js` 均缺失；根因是既有 `.dockerignore` 只允許 Python。補上兩個資產的精確白名單後，context bytes 與來源相同，原型仍排除。
- 正式 Dockerfile 使用固定 SHA512 驗證的公開 Codex artifacts 及既有 Python wheels；來源安裝 `--network=none`。15.25 秒建置 `teams-assistant:local-calendar-ui-20261028`，image=`sha256:13f6df3d2e76e220b792c737833ad58ecada546161e1f5cb21519e5b47ebab7f`。54 Python modules＋2 UI assets 的 host／installed／workdir SHA 完全一致。
- **16 項隔離 Compose checks 通過**，network=none、不掛 OAuth／正式資料、沒有 host port。涵蓋真實 daemon／登入／Origin、七日行事曆與 CSS、sandboxed Chromium、無特權 chroot 拒絕、固定 Codex、SQLite 重啟持久性、超過舊 32K 的完整來源修訂及正常 SIGTERM（0.401 秒）。[隔離收據](calendar-ui-compose-20261028.json)。
- 舊驗證腳本仍引用已退役的 `IncrementalMinutesContext`，改用現有 `MeetingQuestionContext`；舊「20 次短 payload 累計應超過 32K」是偶然大小斷言，刪除並改驗證兩次約45KB來源修訂的完整原文。首次加長 fixture 超過既有單筆 provider event 65536 bytes，被正確拒絕；只調整合成 fixture。瀏覽器檢查另改用 CSP-safe locator／CSS assertions，且不將時間軸算成第八天。這些負例與修正沒有改動正式媒體、CSP 或內容政策。
- 停機前 running runs／active leases／未完成摘要均0，排程暫停。舊容器 exit143／非OOM；SQLite backup `backups/pre-calendar-ui-20261028T153800Z.sqlite3` 為0600，integrity=ok，與來源25表指紋一致。無網路、無OAuth的新版 schema 初始化只新增空問答表；原有25表及欄位／內容不變。
- 僅更新私有 Compose 的 image pin；解析後除 image 與既有 `TRANSCRIPTION_IMAGE` 值外完全一致。實際容器的掛載、UID/GID、host network、readonly、drop caps、seccomp、資源限制不變；mount list 順序變動已按 destination 核對，不誤判為配置差異。
- **實際部署驗證：** 管理登入、七日時格、展開歷史工作區與設定分頁正常，JS errors=0；代表歷史場次顯示138筆逐字來源、21筆系列待辦及摘要，原 interrupted 狀態保留。舊問答日誌為空，不冒稱沒有問答。頁面檢視截圖僅在暫存瀏覽器模糊私人文字，不修改資料。
- 登入前原有25表全同；登入後只增加1筆 admin audit。最後 started=true／active=null／interlock=false／last_error=null，排程仍暫停；Google authorized 狀態不等於新一次真實取會驗收。關閉管理分頁後 healthcheck exit0，僅監聽127.0.0.1:18081。[部署收據](calendar-ui-deployment-20261028.json)。本次未入會、未啟用發聲、未要求模型呼叫；音訊 issue 仍 Pending。

### B 不補音接收端切換與本機驗證：2026-10-29

- 依操作者「不要補音」要求，新增 `b_no_plc.py`，切換 B provider 接收與本機 relay 接收兩處；只解碼實際收到的 Opus，原生接收器的 encoded writable 不接收封包，raw NetEq track 不送入輸出。連續 AudioDecoder → generator → 原生 WebAudio destination 在缺料時輸出零，不延續舊語音；跨問題保留音軌。不修改 A 收音、SQLite context、Teams 正常協商與 owned sender 授權。
- **實際 Chromium 153.0.8010.12：** 單接收端缺包區間 RMS=0；恢復後辨識出不同頻率且沿用同一音軌。正式 `ResponseLivePeer`／`BrowserTXSink`／`OwnedSenderDualPolicy`／`NativeTrackOutput` 整合 smoke 使用合成來源、真正 WebRTC relay 與獨立下游接收器：缺包時 source frames 增量0、render frames 增量54656，下游峰值 RMS=`3.4120730848373197e-06`，未提早完成；恢復不同頻率、同一音軌後正常 quiet completion，`drained=false`、cleanup=`closed`。下游有極低 Opus 殘值，不能冒稱端到端每個 sample 都為零。
- **21 個相關 suites：243 tests／138.893 秒，0 failures／errors／skips。** 涵蓋缺包不算來源靜音、來源／render 尾端分開收斂、新 turn 清除舊 quiet、RTP wrap／重複／晚到封包、非 Opus／缺 API 拒絕、來源變更、撤權、清理、持續輸入、同 thread 多題及完整 SQLite context；不是全 repository discovery。
- 驗證中修正 raw／generated track identity 接線；移除會破壞連續解碼的逐包 flush。初次 suites 因 insecure fixture 缺 API 且在 `ontrack` 的 SDP 協商中立即 close 而逾時；改為先驗 API，target 的 `setRemoteDescription` 完成後、發布 answer 前建立 decoder。新增缺 API 必須快速失敗且清理完成的 browser regression；未放寬 guard 或延長測試期限掩蓋問題。
- 一次僅准入一個 Opus packet 的 decode／write，不新增回答總時限、文字或累計 bytes 配額；120ms 是 RFC6716 單包格式上界。實際來源 quiet 與 render quiet 分別觀測，缺包產生的零不能替代真實來源 quiet。既有 `jitterBufferTarget` 不控制這條 bypass 路徑，未新增自訂預緩衝參數。
- [數值與指令收據](b-no-plc-receiver-20261029.json)。一次性 scripts 已移除，保留純數值 receipt。本輪未呼叫 GPT Live、入 Teams、輸出至實體裝置、保存音訊或部署 Docker；現行服務仍是前節 UI image。遠端 Teams 的 PLC 不在此修改控制範圍；晚包、缺包與主執行緒停頓可能變成靜音，不宣稱真人聽感或完整字句已通過。

### 不補音版本本機 Docker 更新：2026-10-29

- 操作者明確核准更新本機 Docker。沿用本機 daemon、既有固定 Codex 公開 bundle 及 Python wheels；bundle 重驗通過，應用從目前 source 離線安裝。20.34 秒建置 `teams-assistant:local-no-plc-20261029`，image=`sha256:5577e099484d6d01fb85551f571d69cc39cc596a2c4de1d198c6db71d7d64a07`；55 Python modules＋2 UI assets 的 host／installed／image workdir SHA 相同。
- **16 項隔離 Compose checks 通過**，network=none、無授權與正式資料掛載；包含實際登入行事曆、sandbox、固定 Codex、完整 context、SQLite 重啟持久性與正常 SIGTERM。[收據](no-plc-compose-20261029.json)。
- **容器內 Chromium 155.0.8059.79／已安裝程式：11 tests／44.888 秒，0 failures／errors／skips。** 合成缺包時 RMS=0、source frames 增量0，恢復不同頻率並沿用同一音軌；涵蓋兩段 relay、不中途完成、撤權、A 隔離與清理。僅掛 readonly test fixtures，沒有 source overlay、OAuth、模型或 Teams。首次 internal-network 檢查逾時；診斷顯示無 default route、ICE state=new、source／outbound packets=0、沒有 remote candidate，API 可用。相同 image 在現行部署的 host network 對照可連線並解碼，所有媒體測試通過。未改正式網路、放寬 sandbox 或延長測試期限；自有容器及網路已移除。
- 切換前排程暫停、非終態 runs／leases／未發布摘要均0。舊容器正常 SIGTERM／exit143、非 OOM；SQLite backup=`backups/pre-no-plc-20261029T054141Z.sqlite3`，0600／integrity=ok。切換後全部 27 張表（含 SQLite sequence）的內容指紋相同，沒有 schema 或資料遷移。
- 只更新私有 Compose image pin 與其既有環境值，完整解析後其餘設定相同；實際掛載、UID/GID、host network、readonly、capabilities、seccomp、資源額度不變。新版 container=`dbeb12f50408a1e2b8075bd2bc8462fc7c4d0c3c5cc7bb4a4dc0b45489f9cb14`，started=`2026-10-29T05:41:59.234181835Z`，healthy。
- 實際管理登入頁瀏覽／視覺確認正常、JS errors=0，未登入 API 401、health 200；關閉分頁後 CLI healthcheck 成功，僅監聽 `127.0.0.1:18081`。本輪沒有讀取密碼或 OAuth 內容、沒有重新登入正式管理頁、Join、模型呼叫或啟用排程；真人音訊聽感仍待驗收。[完整部署收據](no-plc-deployment-20261029.json)。

### GitHub 發布前可用回歸與測試接線修正：2026-10-29

- 操作者要求 commit／push。原 source 仍屬無 remote 的父工作區，改用既有獨立 checkout；目標 `paulpai0412/teams-transcription`／`fix/standalone-native-questions-20261017`，原 HEAD 與遠端均為 `8c9074e`。不提交父專案、不 merge、不 force-push、不 dispatch workflow 或再次部署。
- 初次擴大回歸 **1021 tests／347.831 秒，4 failures／1 error**。三個 QA 假接收端漏補 source counters，無法到達預期問答／timeout；worker 合成頁不是 secure context；固定 200ms 波形取樣與 sub-second capture duration 存在觀測競態。只修測試：保留未發聲的真實零來源觀測、精確本機 HTTPS fulfill／其他網路 abort、以已收到 PCM／TX restore 事件決定停止，以及接收端實際音調觀測。沒有放寬 production guard 或修改 Docker 映像。
- 修正後針對性 **23 tests／31.947 秒通過**；最終空 `CODEX_HOME`、暫存 SQLite、實際 sandboxed Chromium／Node fixtures 的 **103 suites、1021 tests／355.539 秒，0 failures／errors／skips**。明確排除移轉包缺歷史實驗 source 的 `test_bridge_integration`／`test_media_bridge`，不是完整原始 discovery。
- 獨立 checkout 的 `python -m teams_transcription --help` 正常。依公開路徑 allowlist 排除 runtime／tooling／OAuth／媒體；新增或變更 receipt 未含結構化私有 payload，secret／真實 Teams link 掃描唯一命中為測試的 `synthetic-fixture` URL，原型 ZIP 亦無敏感匹配。空 Codex 暫存目錄已移除；沒有使用正式憑證、入會或模型呼叫。[回歸指令、前後結果與修正收據](publication-regression-20261029.json)。


### GitHub 開發流程與 Oracle staging source 準備：2026-10-29

- 從遠端 main `bf0a108` 建立本地隔離分支；完成 hosted offline CI、原生 ARM64 image validation／optional GHCR publish、手動固定 digest staging deploy／rollback、bounded allowlisted diagnostics 的 source，含 Job Summary／7-day artifacts。沒有 push、dispatch、publish 或操作 VM／Unraid。配置契約見 [GitHub delivery](../docs/GITHUB-DELIVERY.md)。
- 本次 controlled offline subset **153 tests，0 failures／errors／skips**；不是完整原始 discovery。actionlint v1.7.7、原 Compose 17 項檢查及 staging Compose render 通過。maintenance／rollback 的審查缺陷已先以回歸重現紅燈再修正；含 SIGTERM credential cleanup、helper cleanup uncertainty、cleanup review parity。
- x64 冷建置先受 build container 的 proxy／CA 前提阻擋；沿用本環境既有 proxy／公開 CA mount 後完成。未停 TLS、換 Codex 版本或放寬 sandbox。實際無網路、無憑證 x64 容器 **17 項 smoke 通過**：daemon health、登入／Origin guard／行事曆 UI、sandbox／chroot 拒絕、實際 cgroup 限制、SQLite persistence、正常停止，臨時容器／volume 已清除。
- ARM64 僅完成 base manifest、固定 SHA512 npm／archive、Codex ELF aarch64、Python 3.12 ARM binary wheels 與 buildx Dockerfile check。**未執行 native ARM64 image、Oracle rootless VM、真實 Teams／GPT Live 或音訊 E2E**，不能用 x64／health 成功代替。
- 固定官方 Matt Pocock engineering＋productivity 27 個技能／79 個檔案及 MIT LICENSE，僅修改 openai.yaml implicit policy 為 false；完整來源 hashes 在 `.agents/skills/UPSTREAM.json`。附帶兩個 script 只靜態讀取，未執行；不是完整安全 audit，也未驗證下輪 discovery。GitHub adapter 保留 Draft PR、授權範圍與 staging 驗收後才關票，merge 不自動關驗收 Issue。
- [完整收據](github-actions-preparation-20261029.json) 保留本次精確範圍、測試與剩餘前提；沒有重寫原 transfer manifest。


### Provenance／診斷缺口與本人 Codex onboarding：2026-10-29

- Related #1–4／#7，隔離分支本地實作，沒有push／dispatch／publish／Oracle／Unraid變更。成功Actions run／固定repo／main workflow／SHA／attempt／artifact ZIP SHA256／published digest的機器gate已實作；pull後再核對label source。成功與各失敗／fork／竄改／redirect反例以受控fixture驗證，不冒稱真實GitHub gate已執行。
- 診斷有固定15分鐘／100行／64KiB安全事件投影、numeric資源摘要與source／build／deploy／diagnostic run關聯；未知行整行丟棄。合成token／private key／email／中文人名／meeting URL／逐字稿／SDP／audio／malformed／oversized案例及完整報告隱私測試通過。
- 新增auth-preflight／本人TTY且雙重明確確認的auth-login／獨立readonly auth-check；專用onboarding Compose預設offline、logging:none、無ports。固定CLI login --help已離線核對device-auth；**未執行真正login／account RPC／grant／refresh／Live RX/TX**。軟體與host/private-volume分層及本人流程見 [prerequisites](../docs/DEPLOYMENT-PREREQUISITES.md)。
- 最終 controlled aggregate **185 tests，0 failures／errors／skips**；不是完整原始discovery。最終x64 image `sha256:08020a8a9da4dde8e2495cd57868e575dd2ad722de29de910c32c2a3be000076`，source=`8fc15cf`：**21項無網路／無真憑證Compose smoke＋7項image內容檢查通過**，包含actual software preflight、fake auth restart／force-recreate保存、SQLite、UI、sandbox／kernel chroot拒絕、實際cgroup限制。合成auth不是有效授權，也不證明真正refresh。
- 審查及實測揭露並修正FFmpeg -version、deepest mount與授權競態；nested Docker的overlay-backed named-volume bind以固定形態辨識並以實際force-recreate保存驗證，不宣稱host毀損後耐久。中途local VFS disk滿已僅清task disposable cache/images恢復，最終重建及驗證完成。
- 27 skills完整名字／79檔／25附檔／0 upstream assets檔、MIT、hash及AGENTS路由核對通過；兩支上游shell未執行，下輪discovery未驗證。清單見 [SKILLS-INVENTORY](../docs/agents/SKILLS-INVENTORY.md)。兩軸獨立review的重大發現均已修正。
- [最終完整收據](github-delivery-onboarding-completion-20261029.json)。ARM64／GitHub／Oracle、本人新grant與refresh／失效、Live RX/TX／真人Teams仍待各自授權與實際驗收；未更動原transfer manifest，PR merge不關驗收票。

### 初始手動觸發政策與 Draft PR 交付準備：2026-10-29

- 父任務已放行本分支 push／Draft PR；本輪移除 offline CI 的 pull_request／push 事件，四個 workflow 全為 workflow_dispatch。核對遠端 main `bf0a108`，原有唯一 workflow 也只接受手動觸發；push／開 PR 不依賴讀不到的 variables 防止自動 runner。
- variables API 先前 HTTP403，沒有重試／換 token，值仍未知。保留手動 job gates，費用、保護與環境 readiness 仍需核實；未 dispatch／merge／publish／deploy。
- 觸發回歸先因兩個自動事件失敗，修正後通過。重跑 controlled offline aggregate **186 tests，0 failures／errors／skips**；actionlint v1.7.7、27 skills hashes／routes通過。未重建image；先前x64 21 smoke＋7內容檢查仍依原source收據判讀，非ARM或真OAuth證據。
- 補充診斷限制：64KiB為讀取／投影上限，非TemporaryFile落盤硬上限。15秒timeout及Compose 10m×3 log rotation不能替代真VM logging與磁碟核實。
- 詳細事件清單、main SHA、credential scan、tests及未跑gate見 [本輪收據](github-manual-trigger-policy-20261029.json)。

### B 初始化准入及取消清理修正：2026-11-03

- 以已部署 #44 的 `45caaed4` 為基底，實作來源 `aafd3953`。B-only PCM 在初始化完成及新一次 policy check 前丟棄、不排隊或回放；faulted 後一般檢查拒絕，明確撤權保留。Stop 關閉准入、只取消初始化一次，隔離等待者取消；A native／WAV、預設 muted、2 秒 policy bound、來源與授權 fence 不變。
- 定向紅測試重現 native ready 被錯標 native_start、faulted 排隊檢查、初始化 fanout、缺少 fresh barrier、Stop 後恢復及重複取消；修正後 **149 項純程式測試通過**，18.371 秒，0 failures／errors／skips。兩軸獨立本地 advisory review 無重大發現，並非原生 PR review approval。
- cached tooling image＋目前 tracked source overlay，非新 ARM 映像：**15 項 sandboxed Chromium／runtime 合成測試通過**，runner 44.902 秒，0 failures／errors／skips。UID10001、readonly、drop ALL、no-new-privileges、原 seccomp 保留；沒有憑證、外部 ICE 或模型。owned routed Docker bridge 保留設定的 proxy、不用 host network、不改 managed network policy；容器及網路已清理。
- network-none／internal-only 的負例及收據保留。兩項先前失敗案例在 base／current 的 internal bridge 均失敗，沿用舊收據的 routed bridge 配置後 base／current 均通過，再完成 current 15 項測試；沒有改 timeout、刪測試或關 sandbox。
- 現場證據與本機重現支持 policy timeout 鎖定後阻擋 native readiness，**Oracle 的 frame.evaluate 逾時原因及真人遠端聲音恢復仍未驗證**。本輪沒有操作 Oracle、Join／Stop、憑證、模型、遠端 branch／PR、build 或 deployment；新原生 PR review 及 release／真人驗收仍待對應授權。
- [詳細收據](b-native-start-fanout-20261103.json)、[驗證 hashes](b-native-start-verification-manifest-20261103.json)、[瀏覽器及純程式原始輸出](b-native-start-browser-20261103/)。
