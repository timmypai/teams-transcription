# 會後整理與系列最新狀態（2026-11-02）

本輪授權：既有 teams-transcription 的獨立工作分支；不建立產品、不改音訊、Oracle、VM、lobby 或組織政策，不 merge/deploy。來源為本輪委派及其 trusted transcript。Google identity 尚非 dot，真測 pending；不可用家庭帳戶、新 OAuth 或舊驗收代替。

## 需求與逐項驗收

- [ ] 正式 account + calendar ID + recurring parent 識別系列；originalStart 識別 occurrence，改期保 ID，Teams URL 不合併系列。
- [ ] 只同步指定測試 Calendar，沿既有 60 秒 scheduler poll；最新 start 的 T-5 準備，取消不 join。T-5 不限制會議長度，人工 lobby 允許保留。
- [ ] 每場 SQLite 來源 revision、SHA、時間、品質、原文與歷史保留；終態 application segments 可交接，不等待不存在的 provider final。
- [ ] seal 交易建立 durable 外部整理 ready intent；不恢復 10/31 停用的舊 summary jobs/daemon loop。
- [ ] session_id + sourceSHA + pipelineVersion 冪等 key；lease、heartbeat、失敗重領、舊 writer fence，重啟不重建會議。
- [ ] 外部整理只用既有 Codex RPC 真入口；綁定 thread/turn 完成證據及實際 model；禁工具、SQL、任意命令、API key/provider fallback。沒有自主 dot 派工入口時明示 authority blocker，不自造 timer。
- [ ] 當場摘要、決策、stable ID 待辦，引用 occurrence + immutable source snapshot revision。
- [ ] 原子發布系列最新 state + changes；CAS 前版 revision，舊場結果不能覆新場。
- [ ] 未提及未完項保留；完成/取消/被取代須本場引用證據；完成項不可自動重開，重送不重複待辦。
- [ ] 預設僅最新有效結論、未完項與必要變更；history 明問另讀，不累加所有舊摘要。
- [x] 每題鎖同一系列 revision，下題刷新；已經主線取得三處共享面邊界，程式／合成驗證完成，真 Live 仍待驗收。
- [ ] durable 本機完成通知，重送不重複；不向未指定 Page、email 或外部渠道發布。
- [ ] 管理網站顯示真同步狀態、逐場歷史逐字稿、摘要、系列最新/tasks，桌面與手機實際 browser 驗收。
- [ ] 新增 public store/adapter/API seams 測試、相關回歸；獨立原生 Codex Cloud review；draft PR，來源不含秘密/私人資料。

## 檔案 ownership 與可審閱邊界

主 writer：assistant_store.py 的初始化、seal hook、Calendar binding；新增 postmeeting_store.py。獨立 UI writer：calendar_view.py、admin_ui.py、static/calendar.js/css 及新 UI tests。獨立 Calendar writer：新驗收協議及新 identity fixtures/tests。不碰 b_response.py、daemon.py、音訊、runner 或部署。前述 public store、外部 adapter、Calendar normalization→store、管理 API/browser 是本輪已核定 seams。

新外部交接先落 SQLite ready intent。沒有已配置、已授權、可驗證的 dot host trigger 時保持 ready，顯示等待外部執行；只提供可由既有 host 明確呼叫的一次性 adapter，不啟動常駐程式。通知是 SQLite 管理 UI inbox，同交易唯一 key；外部通知 delivery 需另行具體目的地授權。

既有 meeting_store 原文 seal 不修改；晚補稿提供 append-only correction snapshot，新 SHA/key，包含 base snapshot 及補稿 provenance，原 snapshot 不覆寫。host adapter 結果不能自稱 Codex provider，必須經現有 restricted app-server turn 收到實際完成才發布。合成測試不是真 Google、Teams、Codex 或遠端可聽驗收。

## 管理 UI 驗證（2026-11-02）

UI writer 經主 writer 協調另持有 `admin_web.py`，補上歷史逐場整理、來源補稿錨點、系列最新狀態與本機完成通知已讀操作。沒有新增外部派工或模型執行 HTTP endpoint。最新系列待辦為依來源發布的唯讀投影，避免舊 `memory_tasks` 操作造成兩套狀態分歧。

`PYTHONPATH=tests:. python -m unittest test_calendar_view test_admin_web test_postmeeting_ui.PostmeetingUIHTTPTests -v`：31 項通過，3.139 秒。新增的 3 項 HTTP／SQLite 整合測試驗證逐場歷史與最新系列版本分離、補稿引用保留原文及唯一錨點、完成通知的 Origin／CSRF 及冪等已讀。`node --check teams_transcription/static/calendar.js`、Python compile 與 `git diff --check` 通過。

實際桌面／手機測試尚未完成，狀態為 **blocked，未被權限審查拒絕**。執行 `PYTHONPATH=tests:. python -m unittest test_postmeeting_ui -v` 時，既有 `Browser()` 嘗試啟動 `/usr/bin/chromium`，在建立任何頁面之前退出；Chromium 報告 SUID sandbox helper 必須由 root 擁有且為 4755。唯讀診斷確認 `/usr/lib/chromium/chrome-sandbox` 為 `nobody:4755`，沒有其他已安裝的可用 Chromium executable；同時有 crashpad database 的啟動錯誤。未調整權限、安裝 browser、加入 `--no-sandbox` 或放寬 sandbox；沒有桌面／手機 screenshot，不能宣稱渲染驗收已通過。Browser plugin 未列於本 session，沿用既有 Playwright／sandboxed Chromium seam。回歸測試已保存，待可用的保留 sandbox 執行環境重跑。

## 候選實作與驗收狀態

| 項目 | 本分支可核實結果 | 尚需驗收 |
| --- | --- | --- |
| Calendar key／改期／T-5 | account_id + 正式 calendar_id + parent；舊 key 原地遷移保 public IDs；Calendar 身份欄不可換綁；新 Calendar 預設 300 秒，既有已保存設定不暗改；沿原 60 秒 poll | account_id 仍是設定標籤，尚非 Google 身份證據；現有 OAuth 是單身份；dot 專用 Calendar 真同步及入會 pending |
| Source／handoff | seal 同交易建立來源 snapshot + ready intent；不呼叫舊 summary source／queue／loop；empty source 不造成功摘要 | 已部署 host 是否能收到 completion event 尚未核實 |
| 外部執行 | pinned CLI `0.159.0-alpha.3` 真版本檢查；一次性 restricted RPC adapter；完整輸入 SHA、thread/turn 完成、實際 model、cleanup gate | 未執行真 model／account read；沒有自造 dot metadata 或自主 trigger；authority pending |
| 系列發布 | append-only 來源／state revisions；stable IDs；省略延續，終態不重開；引用檢查；原子 summary + state + changes + inbox；lease／heartbeat／冪等及舊場防覆新 | 模型語義品質需真來源驗收；引用存在性檢查不等於語義真實性證明 |
| 失敗／補稿 | 可重領已知失敗；cleanup unknown 設 blocked，不能自動重領；correction snapshot 保原文，新 key；舊場僅發布歷史而不改 latest | unknown cleanup 需既有 host owner 查證，沒有自動繞過／解鎖 |
| 查詢 | `series_question_context`／`read_question_snapshot` 同鎖固定來源與系列 revision；B 每題 reader、獨立版本／hash 與 scope fence 已在隔離整合分支接線 | 9 項新測試與 native 合成驗證通過；真 Live／Teams 尚待驗收 |
| 通知／UI | 管理歷史頁 durable inbox + CSRF 已讀；最新系列與逐場歷史分開；補稿引用可追溯 | 沒有外部發送；desktop/mobile browser blocked（如上），真 Google E2E 未過 |

主線於 2026-11-02 回報真 Google 前置：現有 connector 僅家庭 primary／假日日曆，沒有 dot identity/switch；Oracle 13:12:28 僅做存在性檢查，OAuth client／credentials 檔均不存在。此分支沒有讀秘密、操作 Oracle、登入／grant 或以家庭帳戶代用。正常 `/settings` → `/google/connect` 需操作者安全設定；現有 `calendar.events.readonly` scope 不被擴權。這是 **pending 配置／身份**，不是已成功真同步，也不是本輪 auto-review denied。

回歸：首次 108 項相關測試通過；`python tools/ci_contracts.py /tmp/postmeeting-offline-contracts` 的既有離線 subset **549 tests，0 failures/errors/skips**，無 Teams/model 呼叫。執行末段有既有 async pending-task 警告，未藏為驗收成功；這不是全量 browser／原始 discovery。新增的 public store／adapter／Calendar／HTTP 合成驗證另列候選提交收據。

候選收據（2026-11-02，獨立本機 checkout）：`PYTHONPATH=tests:. python -m unittest test_postmeeting_store test_postmeeting_adapter test_postmeeting_calendar_identity test_postmeeting_ui.PostmeetingUIHTTPTests test_assistant_store test_assistant_daemon test_calendar_client test_summary test_calendar_view test_admin_web -q`：**115 tests／8.629 秒，0 failures/errors/skips**。CLI help、JS syntax、diff whitespace checks 通過。`python -m build --no-isolation` 未執行成功（環境無 build 模組）；未下載安裝。改用既有 setuptools 的 `python -m pip wheel --no-build-isolation --no-deps --wheel-dir /tmp/postmeeting-dist .` 成功，wheel 包含新 store／adapter 與 UI assets；無新依賴。

## 後續獨立準備（2026-11-02）

真 host 入口逐項核對與最低執行／重送契約已補入 `POSTMEETING-HOST-ADAPTER.md`。核實結論：現有 `_execute → finish_run` 是本機 SQLite commit 接點；沒有既有 dot completion callback／dispatcher。`session_events` 只是 journal，local Teams control 僅固定入會／停止操作，現有 workflow 也不是 meeting-completion subscription。不能把手動一次性函式或新 timer 稱為會後自動。需要真實外部 host principal、同部署既有 auth/data-root、commit 後 durable wake-up 及 startup replay 的契約；新自主派工入口／外部目的地另有 authority 邊界。

新增 `test_postmeeting_host_contract`：actual SQLite store + one-shot adapter orchestration，僅 RPC 外部邊界合成。**2 tests 通過**，無真 auth/model／Calendar/Teams 呼叫，證明重送不重複發布／通知，以及 cleanup unknown 阻擋重領；不是自動 E2E。

安全 Docker QA 評估：依 cloud runtime Docker 指引，明確清除 endpoint/context/TLS selectors 並只查 `unix:///var/run/docker.sock`。local daemon **28.4.0 linux x86_64 可用**；`image ls` 及 `ps` 均為空，沒有歷史可用 production/test image 或容器。現有 repo 的 `compose_smoke.py` 可在 network=none、nonroot、readonly、drop ALL、no-new-privileges、原 seccomp、tmpfs 下測實際 sandboxed Chromium，但要求已建好 immutable image。此環境沒有現成 codex_npm/python_wheels build contexts；沒有匯出目前 runtime（避免混入秘密）、借用 Oracle image、pull 新未核實 image、安裝套件、chmod helper、修改 Docker host/網路或弱化 sandbox。狀態 **pending image/build prerequisites**，非 Docker daemon 不可用，也非權限拒絕。提供已核實 image/contexts 後才可走原 smoke 路徑；桌面／手機驗收仍未過。

Draft PR 建立在 `b9f3eed` 時遭 automatic approval review 拒絕：認定缺少可信使用者明確開 PR 授權及外部分享風險。回傳明確 rejected、未建立；未改用 CLI／其他工具重試。原生 PR review 因沒有 PR 而未請求。已提出明確授權問題；未收到新 evidence 前保持 pending，獨立程式／測試／文件工作繼續。

B 窄幅接線新增的待協調依賴：`MeetingStoreMinutesReader` 現由 `response_runtime._AsyncStore` 只提供 `MeetingStore.read`；`MeetingQuestionContext` 使用逐字來源 `version` 與 records hash 做同版衝突檢查。如果只刷新 series records 而不處理獨立系列 revision，A source version 未變時下一題將誤觸 `context_same_version_conflict`。因此需 owner 協調 `b_response.py` reader、`b_context.py` 版本身份，以及 `_AsyncStore` 的一致 snapshot 交付窄幅變更；不碰音訊、分題／播放或預算 gate。先列衝突，未寫共享檔案。

本次後續收據：117 項相同相關回歸（含新增兩個 host orchestration tests）通過，8.884 秒，0 failures/errors/skips；wheel 以原離線方式重新建置。`origin_session_id` 為 host 保存的最初來源 session，待辦跨場延續與更新不換 origin，供後續 B reader 接線使用。當前仍沒有真 Codex／Google／桌面手機 E2E 或 native review 完成證據。

## B 共享面已協調的隔離整合（2026-11-02）

主線轉達音質 owner 明確不修改 `b_response.py`、`b_context.py`、`response_runtime._AsyncStore`，並授權窄幅接線。本次由 `406d818` 建立獨立 `feat/postmeeting-series-query`，沒有操作音質分支／原音訊檔案。

- `response_runtime.py` 僅改 import 與 `_AsyncStore`：使用 `AssistantStore.read_question_snapshot`，在一把 SQLite lock 擷取本場 source 與綁定 occurrence 的系列 head。模型沒有 series/session 選擇參數，standalone 無 Calendar run 的來源保留原 legacy fallback。
- `postmeeting_store.py` 新增同鎖 read seam；不改 raw `MeetingStore.read`／原文、來源 version 或入會 prior。沒有新派工或 timer。
- `b_response.py` 的 reader 對已發布系列只投影有效結論、未完 tasks 及已退出有效集合的必要狀態變更。舊 admission summary 不再與新狀態疊加。host locators 保留 stable item ID、來源 occurrence/source SHA/snapshot revision 等 refs；品質標 derived，不升級成 provider final。
- `b_context.py` 分別檢查 A version 與 `series_snapshot={series_id,revision,sha256}`。hash 包含本題 records 與系列身份；A 版本不變但 series revision 升版可以刷新。相同系列 revision 改 state hash、版本倒退、跨系列或身份消失皆 fail closed；同 A/series 版本改 records 仍是衝突。每題只取一次 snapshot，head 在送出中升版不改本題 payload，下題才刷新。原 request/thread scope、完整交付、marker／commit fence 及音訊 gate 均不動。

新增 `test_postmeeting_series_question` 9 tests 通過，包含真 temporary SQLite 與既有 native responder 的合成 RPC/media boundary；無真模型或發聲。先前 65 項 context/SQLite/native/B-reader 回歸通過。首次擴大命令誤列不存在的 `test_b_context`，已改正；同次 `test_response_runtime` 的 7 failures + 1 error 都源於 Chromium 在 browser_open 先退出（含依賴其 cleanup_status 的 assertion），另有該不存在 module 的 import error。未削弱測試或 sandbox；這個實際 browser suite 仍 blocked，不與 credential-free context checks 混稱全通過。

現況更新：本分支已具備 B 每題最新狀態接線的 source／合成驗證，先前「B 接線待 owner」的項目由此段取代。dot 自動 trigger、真 Google 身份與 desktop/mobile／真 voice E2E、native independent review 仍未完成。明確歷史查閱仍透過已有逐場管理頁／來源記錄；本次預設問答不自動加入全部舊逐字稿。


隔離整合候選驗證收據：`test_postmeeting_series_question`（9 新 tests）加 postmeeting／Calendar／SQLite／context／native responder／管理 HTTP 等相關 suites 合計 **184 tests／9.789 秒，0 failures/errors/skips**。原有 `tools/ci_contracts.py` 離線 subset 再跑 **549 tests，0 failures/errors/skips，teams_or_model_called=false**，stderr 仍有先前已揭露的 async task／執行時間警告；不宣稱所有 browser suites 通過。無新依賴的 wheel 再建置成功，72 個 package files 與候選源碼逐位元組一致；JS syntax／diff checks 通過。PR 建立拒絕仍未重試、未 merge/deploy。

## 原生 PR 審查與修正（2026-11-02）

使用者明確批准固定 `9d15eb5b51857a8cfe9a75c68d063c47138b57a2` 的 draft PR 與 Codex review（不 merge/deploy／憑證或安全變更）後，先查同分支 PR 不存在、核對遠端 head，再建立 [draft PR #41](https://github.com/paulpai0412/teams-transcription/pull/41)，只發起一次 `@codex review`。此為先前已知未執行之拒絕後的新授權；沒有繞過拒絕、擴權或更換憑證。

原生 Codex 在固定 head 的 review 於 14:53 UTC 完成並提出兩項 P1，不能稱為通過：

1. [取消／heartbeat 失敗時遺失 cleanup unknown](https://github.com/paulpai0412/teams-transcription/pull/41#discussion_r4443825311)。新增 actual SQLite + synthetic RPC 回歸先重現 `ready/postmeeting_cancelled` 的錯誤，修正後仍向 caller 丟出原 cancellation／heartbeat failure，另將 settled generation 的 cleanup unknown 持久化為 blocked。已完成的 generation 也會檢查，不因取消競態略過。沒有自動解除 blocked。
2. [後次補稿遺失先前補稿](https://github.com/paulpai0412/teams-transcription/pull/41#discussion_r4443825322)。新增回歸先重現 A→B 補稿最新 input 缺 A。修正會依 snapshot submission history 累積 correction IDs（不按文字去重），加入 `supersedes`／`active_correction_ids` 明示應用補稿取代關係；原 snapshots／原文不改寫。獨立 correction request SHA 保存 exact replay identity，較晚修訂之後重送舊 request 仍回原 snapshot、不重建 intent。新的 cumulative source SHA 包含完整來源／補稿歷史；不是 provider-final 或推測的 provider 時序。受限 adapter prompt 明示只以有效補稿解讀當前含義，歷史版本不變成重複承諾。

四項新增回歸涵蓋 cancellation + unknown cleanup、heartbeat failure + unknown cleanup、A/B 分次補稿最終可同時引用、同來源新補稿明確取代並保留另一來源及舊 request 冪等。相關測試 **188 tests／10.066 秒，0 failures/errors/skips**；既有離線 subset **549 tests，0 failures/errors/skips，無真 model/Teams 呼叫**；既有 async timing/task warnings 仍明記。離線 wheel 成功且 72 個 package files 與修正源碼一致。

審查只對 `9d15eb5b...` 完成並有 findings。修正後的新 head 並未因本地測試自動成為 reviewed/approved；不代替 reviewer resolve threads、不自造 PASS、不將 review 或無 CI 視為 Google/Oracle/Teams／desktop/mobile 真驗收。既有 CI/check runs/statuses 在審查時為零，workflows 均 manual-only；本任務未 dispatch 新 workflow。

### 精確 head 再審：cleanup interlock 範圍

依主線批准的同範圍修正／再審閉環，對 `dbd39414475eb9e6a708faa408e8a334a9712f50` 發起精確 head review。原生 reviewer 於 15:09 UTC 提出新增 [P1：補稿可繞過 unknown-cleanup interlock](https://github.com/paulpai0412/teams-transcription/pull/41#discussion_r4444007729)：只排除 running，原 snapshot blocked 後的新 correction ready intent 仍可被領取。

新增 public SQLite 測試先重現 correction claim 錯誤成功。修正將 blocked provider cleanup 的准入阻擋作用於同一 SQLite execution root，不能靠新 snapshot、session 或 series 啟動另一個 provider。來源補稿／新場來源仍可保存為 ready intent，沒有抹除來源、重新入會、改音訊或自動解除 interlock。既有 host owner 未建立可驗證 cleanup 前保持阻擋；沒有新安全權限或 SQL 解鎖捷徑。

驗證：相關 **189 tests／10.147 秒，0 failures/errors/skips**；原離線 subset **549 tests，0 failures/errors/skips**；wheel 72 個 package files 逐位元組符合候選源碼，無新依賴。這個新候選仍需原生獨立再審，不以自測標 threads resolved，也不作 merge/deploy 或真實環境驗收。

### 過期嘗試的 cleanup 安全回報

`ffb7762b42a03882d512b156b4fe8241f16c46d0` 的精確 head 再審另指出 [P1：租約真正過期時 unknown cleanup 無法持久保存](https://github.com/paulpai0412/teams-transcription/pull/41#discussion_r4444115988)。新增 real SQLite + synthetic RPC 測試先重現 settled cleanup unknown 後仍為 `running/error_code=None`。

修正加入私有、按 job/generation 綁定的 attempt ledger，保存 owner 與 token SHA（不輸出 raw token）。cleanup-unknown 安全回報以既有 attempt 身份驗證，不要求尚有執行時間權限；只能記錄風險。過期 owner 的普通更新／結果發布仍依原 lease fence 拒絕，也不能改寫新 generation 的 job state。若 takeover 已發生，舊 attempt 的風險獨立保存，並阻擋新 provider 的 heartbeat／發布及 root 後續 claims；新 owner 的普通失敗收尾或程序重啟不會抹掉舊風險。偽造身份、已確認 cleanup 完成的嘗試不得新增阻擋；沒有安全解鎖權限或自動清除 unknown。

新增三項測試涵蓋真正 expiry、takeover 後安全回報與 ordinary stale-writer rejection、偽造／已清理嘗試拒絕。相關 **192 tests／10.577 秒，0 failures/errors/skips**；原離線 subset **549 tests，0 failures/errors/skips**；wheel 72 個 package files 與源碼一致。既有 warnings 仍保留；source 原文、音訊、Oracle、權限與憑證未變。修正候選再經獨立 review 才可認定閉環，不以自測 resolve threads。

### 主因與 cleanup outcome 分離；新週期承諾身份

`ac03fe96094118a5b820807954edc01e368998ec` 的原生再審指出 [P1：generation 自己的 finally 仍可能以 cleanup unknown 覆蓋 primary error](https://github.com/paulpai0412/teams-transcription/pull/41#discussion_r4444303421)，以及 [P2：後續場次明確新交辦的同文字／責任人／空期限工作無法與 closed task 區分](https://github.com/paulpai0412/teams-transcription/pull/41#discussion_r4444303436)。

前者以 synthetic `account=apiKey + cleanup_error` 重現 caller 只收到 cleanup unknown；修正保留原始 auth／turn／timeout／cancellation，cleanup outcome 另由 host lifecycle 傳遞，避免 CancelledError 在 task settlement 後遺失附帶資訊。結果只在 cleanup 確認後回傳；orchestrator 仍持久化安全 block，但 caller 看得到真正需修復的 primary error。新增 direct auth/turn/timeout 組合及 actual SQLite orchestration 回歸，均沒有真 grant/model 呼叫。

後者以三個 occurrence 的完成後重新交辦情境重現。新建立項目的 deterministic ID 納入 origin occurrence；延續／更新一律保留既有 stable ID。相同未完項仍須帶舊 ID，不因新 occurrence 重新建立；同來源場次的舊 closed 項不自動重生；只有後續 occurrence 明確的新承諾才可建立新週期 ID。原已完成項保持終態與來源，舊 callback 仍不可重播。受限 prompt 明示再次提及／補稿不算新承諾。

相關 **195 tests／10.525 秒，0 failures/errors/skips**；原離線 subset **549 tests，0 failures/errors/skips**；無新 provider、第三方、權限或部署。新 UI／任務承接語意的 Beta 擴充另分支規劃，不混入此 review 修正 head。
