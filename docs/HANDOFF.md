# WSL2 接續規則

## 目前目標

依使用者核定的 standalone 設計實作及驗證：Google Calendar → Teams、SQLite 交換中心、A 詞庫 `initialItems`、B native 語音問答、會後一般 Codex 摘要、系列記憶與 Docker 登入管理頁。dot／MCP／Page／會議 Markdown 路徑已退役，不恢復相容 facade。

`README.md`、`AGENTS.md` 與目前 source 是新執行方式；`TRANSFER.md`、`TRANSFER-MANIFEST.json` 與 `transfer/requirements-cloud.txt` 記錄原移轉基準。不得重寫 manifest 認證本機修改。原 WSL2 package 位於無 remote 的父工作區 Git root，不提交至父 repository；GitHub 發布使用 `paulpai0412/teams-transcription` 的獨立 checkout。

## 授權與私有資料

使用者已要求安裝、修正與實測，並於 2026-10-29 再次明確要求 commit／push 至 GitHub。發布目標維持 `paulpai0412/teams-transcription` 的 `fix/standalone-native-questions-20261017`；使用獨立 `/home/timmypai/teams-transcription-publish-u9_kpkzj` checkout，接續原分支 `8c9074e`，不提交到父工作區。只發布公開 source、測試、文件與去識別數值證據；本機 tooling、runtime、OAuth、session 及媒體不入版控。不 merge／force-push／另行部署或 dispatch CI。

2026-10-15 另經操作者明確核准，在指定 Google 專案啟用 Calendar API、建立測試模式 OAuth app／專用 client，並完成 Calendar events readonly 同意。本機 loopback 管理服務用於此授權驗證；這不授權發布 Google 正式應用、任意新增活動或正式部署。

操作者已提供當次Teams連結及「現在可測」，另核准最多50次代建活動測試，並確認本次音訊擷取、文字逐字稿保存及助理發聲同意。目前本機場次20筆、真人嘗試19輪，沒有批次建立50筆；第19輪只送一次Join，之後已正常停止，沒有新Google活動寫入。這不是整個主日曆或其他會議的錄音授權。最新操作要求：**每輪送出加入前先通知，等操作者回覆已在會議內再加入；不得無聲重試。**

操作者最新要求：**先完成功能收尾與可自行執行的整合驗證，再集中安排真人測試；不要每個小修補都邀請重入會。** 現在不送 Join、不新增測試活動、不開啟排程或媒體授權。既定 Calendar／SQLite／A／B／摘要／詞庫／管理頁／Docker 模組已有接線，但本版跨場閉環、A 內容與詞庫實效對照、B 完整內容及重複問答、長穩與標準冷建置仍不可宣稱完成。先自行完成可做的部分，再通知操作者集中驗收；真正 Join 前的新鮮在場確認要求不變。

- 真實會議需當次有效連結及錄音／發聲同意；歷史聊天連結不作目前有效會議假設。
- Google Calendar 需操作者提供獲准使用的 client 設定及 readonly OAuth；不能以假資料冒充已連上 Google。
- Codex 只用同一已授權部署的既有 OAuth；不抽取其他環境 token、不加 API key 或 paid fallback。
- 私有 `.tools/runtime.local.yml` 屬舊設定且拒絕全部 egress，保持原檔；新程序用明確的新配置。不要暗改設定解除 gate。
- browser sandbox、owned sender、source provenance、lease fence、`stop_unknown` 與 capture cleanup interlock 不放寬。

## 最新工作優先序：不補音 B 已部署本機，待真人驗收

- 操作者已恢復音訊工作，明確要求「修正不要補音」，並核准「更新本機docker」。B 的 Live 與本機 relay 接收段已切至 `b_no_plc.py` 的實際 Opus 解碼／持續原生零值輸出，不以放大 jitter buffer 冒充關閉 PLC。A、SQLite、提問與發聲授權不變。2026-10-29 已部署 `teams-assistant:local-no-plc-20261029`／`sha256:5577e099484d6d01fb85551f571d69cc39cc596a2c4de1d198c6db71d7d64a07` 至同一本機服務，container=`dbeb12f50408a1e2b8075bd2bc8462fc7c4d0c3c5cc7bb4a4dc0b45489f9cb14`；入口仍為 `http://127.0.0.1:18081/`，未重新入會。
- 隔離真 GPT Live 三題的輸出音軌均先於首段語音開通；首秒 Live 接收端在首次／閒置後回答觀察到約 44／87 毫秒的延展樣本，接續回答為零。這是待釐清線索，不是當次 Teams 聽感的唯一根因證明。該輪量測未修改正式程式或部署，臨時程序已清理；合成量測收據保留於 `/tmp/native-provider-startup-zvj9r6pl/result.json`，不可當成永久歸檔。
- 本機實際 Chromium 的缺包 smoke 已證明：沒有新來源樣本時播放靜音，不因該靜音提前結束回答；恢復後同一音軌能播放不同聲音並正常收尾。容器內 Chromium 155.0.8059.79／已安裝程式的 11 項媒體測試亦通過，無憑證、無 source overlay；16 項隔離 Compose checks 通過。最初 internal 網路中 ICE 未連線／source packets=0；沿用實際部署 host network 對照通過，沒有改正式網路、延長 timeout 或放寬 sandbox。這不是真人聽感或遠端零 PLC 保證。新路徑與 quiet 契約見 `B-LIVE-NATIVE.md`。
- 本次切換前無進行中場次／租約／摘要，排程暫停；舊容器 SIGTERM／exit143、非 OOM。私有備份為 `backups/pre-no-plc-20261029T054141Z.sqlite3`，0600、integrity=ok；切換後全部 27 張表（含 SQLite sequence）內容相同。僅改私有 env 的 image pin，掛載、授權、host network、安全與資源設定不變；55 modules＋2 UI assets 與受測 source 相同。實際服務 healthy、登入頁正常、未登入 API 401、關分頁後 healthcheck 成功，僅監聽 loopback。沒有讀取密碼／OAuth 內容、新登入、Join、模型呼叫或啟用排程；真人測試仍須新鮮在場確認。收據：`../evidence/no-plc-deployment-20261029.json`。
- 2026-10-29 發布前以空 `CODEX_HOME` 跑完 103 個可用 suites：**1021 tests／355.539 秒，0 failures／errors／skips**；仍排除兩個移轉缺來源的 bridge suites，不稱完整原始 discovery。擴大回歸先揭露舊 QA 假接收端缺 source counters、非安全 worker fixture 與固定時點／短 duration 競態；僅修正測試契約、secure 本機文件與事件驅動停止／音調觀測，未改 production modules 或再次部署。獨立 checkout 的 CLI help 亦通過。收據：`../evidence/publication-regression-20261029.json`。
- 操作者已選 **A：週行事曆＋可展開會議工作區**。正式 UI 已接上 SQLite、前次結論／最新待辦、持續更新逐字稿與獨立問答日誌；設定分頁、原有管理控制及安全檢查保留。新增 `calendar_view.py`、`admin_ui.py`、`qa_activity.py` 及 packaged CSS／JS，契約見 `CALENDAR-RUNTIME.md`。
- 已執行既有可用回歸 **1001 tests／308.992 秒，全通過、skips=0**；兩個原移轉缺來源的 bridge suites 仍明確排除。實際 Chromium 操作登入、系列／單場隔離、歷史深連結、待辦保存、手動場次、持續來源／問答、過期請求、手機版、關分頁後背景持續、停止及受控摘要。wheel 建置、解包 CLI 與資產 HTTP routes 通過；詳見 `../evidence/calendar-ui-a-20261028.json`。
- 前次 A 版 UI 部署歷史：`teams-assistant:local-calendar-ui-20261028`／`sha256:13f6df3d2e76e220b792c737833ad58ecada546161e1f5cb21519e5b47ebab7f` 已由上述不補音版取代。下列 UI 切換與遷移紀錄保留為歷史證據，不代表目前 image；當次 B 音訊及完成判定未變。
- 三版 HTML 與離線 ZIP 已歸檔至 `docs/ui-prototype/`，不打包進應用程式；入口為 `index.html?variant=A|B|C`。舊預覽與隔離 smoke 服務已停止。原型是虛構資料，既有原型驗證不冒充正式接線、Claude／Grok 評審或真人驗收。
- 切換前確認沒有 running run、active lease 或待處理摘要，舊容器正常 SIGTERM／exit143、非 OOM。私有備份為 data root 的 `backups/pre-calendar-ui-20261028T153800Z.sqlite3`，0600／integrity=ok。新版只新增空 `question_activity` 表；啟動後、登入前原有 25 張表完整一致，登入後只新增 1 筆 admin audit。Compose 僅 image 與既有 image env 值變更，掛載、網路及安全／資源限制不變。
- 新版 16 項隔離 Compose checks 通過，含實際 sandboxed Chromium 登入及 CSS／JS 行事曆；修正 Docker context 原先漏掉靜態資產的白名單，沒有放寬 CSP。已在實際部署登入、查看歷史結論／待辦／逐字稿及獨立設定頁，無 JS 錯誤。最後 started=true、active=null、interlock=false、last_error=null、背景工作3、排程暫停；Google 狀態 authorized=true（不是本輪新取會成功證明）。關閉管理分頁後 healthcheck exit0、僅監聽127.0.0.1:18081。收據見 `../evidence/calendar-ui-deployment-20261028.json`。
- 舊段落中「保持運作」的 session `2174edb9dc08452c80d42cbb19afced0` 現已確認於 `2026-10-28T13:01:01.417956+00:00` stopped／scheduled_end／error=null，早於本次部署；本次沒有停止活動會議。其他歷史 interrupted 狀態不改寫，舊場次沒有問答日誌不回填推測內容。
- 本機再建置可重用公開 bundle `/tmp/calendar-ui-codex-npm-20261028`（已再次通過 `--verify`，暫存路徑可能被清理）及 `/home/timmypai/.cache/teams-build-20261020-9KSv7Q/python_wheels`。prefetch 工具只接受 `/tmp` 或 `/workspace`，不要搬到其他位置再繞過檢查；任何 build context 都不包含 data root 或 OAuth。

## 接續閱讀與驗收

核心契約：`ABC-RUNTIME.md`、`MEETING-STORE.md`、`DURABLE-DELTA-TRANSCRIPT.md`、`BOUNDED-RX.md`、`PROVIDER-ADMISSION.md`、`B-LIVE-NATIVE.md`、`CALENDAR-RUNTIME.md`、`GLOSSARY-RUNTIME.md`、`SUMMARY-RUNTIME.md`。

新證據只記錄實際完成的層級，見 `../evidence/RESULTS.md`。實際 UI 及 CLI 已以無模型／無會議配置操作；capture／summary 的受控 fixture 不代表 Live、真人 Teams 或遠端語音成功。完整 discovery 的兩個實驗 bridge 模組未隨原移轉提供，不能隱藏成全綠。

完成標準是同次真實 Calendar 場次、Teams admission、A 逐字來源、B 正確引用且遠端可聽、停止／清理、普通 Codex 摘要持久發布、下次同系列讀到正確記憶；詞庫效果另需固定音訊與人工基準的有／無詞庫對照。缺人為前提時明確列出，不將受控替身當作驗收。

2026-10-15 已完成真實 Google readonly API／Calendar 場次；第三輪經操作者准入後，正式 A 的 `gpt-live-1-codex` 連線、RX ready 及非零 PCM 皆已觀察到。SQLite 保存 16 筆 user application segments，provider final=0，不能冒充定稿；停止／清理後，正式一般 Codex `gpt-6.1-sol` 已發布實際來源摘要 revision 1，job=published。B 在 sender_select 失敗：active 音訊 transceiver 為 recvonly/null sender，未建立可發聲通道。

第四輪在 audio_choice 失敗，尚未 Join；其 prior_json 已綁到第三輪摘要 revision 1。後續非入會探針直接重現預覽讀取 `Frame was detached`：補齊 caller read guard，僅在尚未派送預覽動作且已證明 frame 失效時丟棄整批 scan，相關 60 tests 及真實非入會 smoke 通過。第五輪依先通知／再確認規則實際入會，A recording／RX ready，保存 17 段未定稿、provider final=0；正常停止且清理完成，一般 Codex 已發布該輪摘要 revision 1。指定測試句關鍵詞未找到，不能宣稱辨識正確。

第五輪 B 仍在 sender_select 失敗。真實非入會探針證明 Teams 已選 owned device，但原 adapter 對 WebAudio track 套用關閉 DSP 的 capture-device constraints，拋出 OverconstrainedError，未返回任何 track。已在 owned-source 邊界處理固定關閉的 gain／echo／noise processing，拒絕不支援的 exact 要求，其他 constraints 仍交 native。15 項回歸通過；相同真實 Teams 請求現可取得 live／disabled owned track，實體麥克風權限仍 denied，未 Join。沒有放寬 sender／SDP／permissions，也沒有開實體麥克風。

第六輪已真正入會，A RX ready，B response_ready／eligible sender=1／已授權發聲；操作者已說但沒聽到回答，這是失敗結果。A 保存 25 段未定稿，含 wake 但無完整指定句；不能推定操作者未說或以 A final=0 推定 B 無 done。正常停止／清理後，普通 Codex 已發布該輪摘要 revision 1。舊狀態缺少 B 執行中證據，不猜改 wake／partial／done 邊界。

已補有界 B progress：管理 API／總覽即時查，SQLite／歷史保留停止前快照及觀測時間。可區分缺 clock、fanout 接受／拒絕、B RX／peer enqueue、user／assistant delta／done、handoff、tool call 及 speech 完成；不記原文、URL、SDP 或音訊。59 項相關回歸及實際無憑證 UI 操作通過；受控 UI launcher 的外層 Ctrl+C exit=1 已明記，不冒充 production CLI 錯誤，scaffold 已移除。

另完成真實 B 合成中文控制組：本機離線 zh-TW TTS → 正式 Native B（不開 synthetic gate）→ SQLite 工具 → 原生回答；user／assistant done 各 1、handoff admitted=1、tool_calls=1，答案含來源 Teams 項目，生成有效 PCM，正常停止／清理。此組直接送 peer，輸出保持 muted；只排除該合成情境的 B 協定問題，不涵蓋 worker clock／fanout、真人 ASR、Teams TX 或遠端可聽。詳見 `../evidence/RESULTS.md`。

第七輪在發言前確認 B 確實接受／排入 PCM、缺 clock=0；操作者已確認「有叫小白小白有回應，沒帶就無回應，這是我要的」。因此真人遠端可聽與該輪門控已通過，規則不改。隨後 B 曾 response_failed；停止前快照已保留，85 段未定稿／final=0、普通 Codex 摘要 revision 1 已發布，清理完成。未確認回答內容或歷史理解，也未能從第六輪缺失狀態追溯其唯一真因。

後續離線中文 bare-wake 真模型探針重現：自然問候措辭不同即觸發硬編碼 greeting_text_mismatch，且 error 後仍被誤標 listening。已移除正式文案比對，保留完整喚醒、靜音、作用域、授權及音訊完成 gate；保留第一個錯誤、不在 error 後發布 idle，每次 speech 清空舊 drain。新回歸 before fail／after pass；空 CODEX_HOME 的 69 tests 通過。同 PCM 真模型 after 可正常 greeting → listening、speech closed／error=null、stop／cleanup 正常，未入會／未發聲。詳見 evidence；不能冒充修正版本真人持續驗收。

第八輪操作者回報都有回答，但僅看到部分記錄。本輪已主動停止／清理、撤回發聲；停止後再叫沒有回應符合無 active run 的狀態，不是待命。保存 62 段 user application segments、provider finals=2，普通 Codex summary job=published／error=null。停止前 B 快照另有 response_failed／native_error，精確首因未留存，不回填成已確定的停止競態或預算耗盡。

直接檢查本輪 SQLite 及 wire 證明：前次摘要與 1 項待辦存在，但按 record_index 重排與單一詞彙排序使本場來源擠掉歷史。修正為保留 literal／跨 part 命中，其餘依待辦、本場、前次摘要輪流涵蓋並保留各自相關度，次序延續到 wire 裁切；不增加預算、不改 locator／品質。68 tests 通過。同一實際保存來源的正式 Native B、離線中文問題、輸出 muted before／after：before 僅本場 9 筆、答案未含待辦；after 待辦 1／前次摘要 3／本場 4，答案包含保存的待辦全文、仍誠實提示部分來源，正常停止／清理。此組不是修正版 Teams 遠端語意驗收。

第九輪在新的在場確認後建立第 10 筆活動並入會，A recording／B ready；操作者聽到完成狀態後回報後續無聲。停止前直接觀察 B failed、output=false、admitted=4／tool_calls=3，非主動停止造成。相同保存來源連讀前三次累計 32,381／32,768 bytes、第四次拋出本機累計 budget exhaustion，與計數一致；舊 live 精確首因未投影，不回填成已取得原始例外。

操作者明確要求取消本機 32K 限制；已移除累計 gate／參數／quota ledger，撤掉未採用的 thread 輪替。單次 payload、500-byte chunks、RAM cache／音訊佇列及來源／授權保護不變；sent_bytes 只觀測，total_limit_bytes=null。145 tests 通過；正式 GPT Live 靜音 smoke 同一 thread 連續五題，累計 57,144 bytes、每題均含保存待辦原文，第五題後純喚醒也成功，正常 stop／cleanup。這不是 Teams 遠端驗收，不代表模型容量無限。詳見 evidence 新節。

第九輪已在明確通知後正常 stop，run=stopped／operator_stop、capture／B cleanup=cleaned、summary job=published／error=null；這不代表後來中斷的第十輪也清理成功。

第十輪在新的在場確認後使用第 11 筆活動／原系列，只 Join 一次。live 曾觀察 5 次 tool calls、56,508 bytes、error=null；操作者仍表示不知上次記錄。服務之後無退出碼退出，最後持久快照為 7 次 tool calls／79,992 bytes／error=null。原庫 hot rollback journal 使 readonly open 失敗；先在 tmpfs 副本診斷，再走正式 daemon expired-lease recovery 恢復原庫、標成 interrupted／service_interrupted、封存來源。實際普通 Codex `gpt-6.1-sol` 已發布恢復摘要、requirement_met=true，沒有重入會；不能把這當成外部清理成功。

已確認 B 啟動紀錄有歷史記憶標記及保存待辦，載入的是凍結前次摘要／系列待辦而非完整前次逐字稿。本輪 prior 有重點 1／未決 3／決策 0／摘要待辦 0、系列待辦 1。新增 history_status 區分摘要存在、空分類及查詢可引用內容，避免將 query available=false 當成沒有前次摘要。156 tests 通過；真實 Native 靜音控制可正確說有空摘要但無決策，單句原文問題可念出實際保存系列待辦。泛問未逐字命中的控制不算內容驗收，原現場否認歷史的唯一根因仍未確定。詳見 evidence。

本機Docker驗證服務已經操作者明確核准使用host network，並於2026-10-27核准更新至B原生音軌候選；Compose project=`teams-validation`、container=`teams-validation-teams-transcription-1`，目前container ID=`1e4ab453e209481300c7281f6e13e6e94f664dc1bd772af12474506cc59a11f1`，image=`sha256:a22d154c6dc187f099dae6ac61333dd2b138de7fc1a57a330eb2aea363073252`。沿用僅`127.0.0.1:18081`的管理listener；非root、readonly、drop ALL、no-new-privileges、seccomp及既有資源額度保留。host network仍是本機bridge媒體丟包的核准繞過措施，不是WSL底層修復或通用正式部署建議。

2026-10-20真人場次`e25838703fdb4075b84711ccb4757f02`於07:21:00Z joined，但操作者兩次已發言皆未獲回答。第二次新增114,817個非零樣本，A／B仍各僅1筆ASR delta，問題／工具／發聲0；不能將ready當語音成功。隔離合成語音對照發現Docker bridge→Live上行嚴重丟包；同映像／Chromium／原生relay改host network後，同連線兩次完整辨識且遠端回報上行丟包0。本場其後透過正常stop於08:02Z封存，state=stopped／cleanup=cleaned／error=rx_stopped，finals2／increments1，summary job=published；沒有改寫失敗。服務切換後已實測管理登入、idle／active=null／interlock=false、healthcheck exit0，關閉管理頁仍運作。2026-10-22管理頁Google=`google_authorization_required`；此通用碼不證明token過期唯一原因，也不能以last_poll_at冒稱Google讀取成功。詳見[evidence最新紀錄](../evidence/RESULTS.md)。

操作者再次要求先完成功能及可自行執行的整合驗證，再集中一次真人測試；目前不Join、不新增活動、不啟用排程。原第20輪`50e0663084714a789cb4c5bfa9dec184`的cleanup unknown及人工審查不改寫。私有配置目錄為`/home/timmypai/.local/share/teams-assistant/local-validation/config/`；現行使用`runtime.docker-host.yml`、`docker-validation-host.env`、`compose.validation-host.yml`，Compose依序為repo的`docker-compose.yml`、`compose.auth.yml`與此host override，**不要再加compose.local.yml或使用舊bridge env**。原bridge三個檔案保留但不是現行部署。data root→`/data`、同一既有Codex home→`/auth/codex`不變，沒有複製token；沒有改WSL／防火牆、資源額度、全域授權或合法deny-all `[]`，未commit／push。真人遠端播音、三項問題及跨場閉環尚未驗收；完成自行驗證後才通知集中測試，真正Join仍須先通知及新鮮在場確認。

2026-10-22原始碼已修正兩項可重現問題：TX destination空檔沒有靜音時鐘，改由零值ConstantSource持續驅動；歷史原文被編碼成第二層JSON字串，改為直接可讀`text`＋完整`historical_context`。發聲授權、來源身分／引用、待辦狀態、每題新查SQLite與B原生輸入不變。當時部署的`teams-assistant:local-readable-history-20261022`包含兩項修正；已安裝三個變更模組SHA與受測source一致，CLI及9項SQLite consumer驗證通過。當時實際管理登入／畫面、healthcheck通過，關閉管理頁後服務仍healthy；排程暫停、active=null、interlock=false，未Join或新增活動。此為前輪狀態，已由下述新版部署及最新interlock狀態取代。

修正後正式A／B接線的合成閉環連續五題通過：目前事實、更新後首次回答、兩次各念一次歷史待辦、再次回答最新預算；接收端五段波形及兩題機器語音讀回通過，正常停止、真實Codex摘要發布與下場記憶亦通過。既有Python3.12可用全回歸982項，errors／failures／skips均0；缺來源的兩個歷史suite仍排除。先前Native錯答、重述、少音及datachannel timeout保留為負例，不宣稱這一輪成功已修復所有網路／模型問題。當時自行驗證及核准本機更新已完成；Google仍為`google_authorization_required`，Calendar自動抓會需操作者重新授權，手動真人驗收不以Google授權為前提。不得自動Join或啟用排程。詳見[evidence](../evidence/RESULTS.md)。

2026-10-27操作者以原連結及新鮮在場確認啟動手動測試，occurrence=`38898f9fd9584c50967213220166a169`、series=`a2c893c5d6af476188ced00417345f27`、session=`a0934f4217364282b25a72f411b4640f`，只送一次Join。操作者聽到回答但開頭不連續，後續無回覆，**本輪真人驗收失敗**。來源保存138個application segments／0個provider final，B有8題准入及8次工具交付；07:47:03Z因`stale_execution_lease`封存為interrupted／service_interrupted。主機同boot journal觀察wall-clock正跳34.642246秒，舊wall-clock租約因此被判逾期；Docker未restart／OOM，媒體子程序已退出。最近狀態active=null、capture_interlock=true；摘要發布不改寫來源失敗或清理結果。排程仍暫停，不自動重入。Google重新授權只阻擋Calendar自動抓會，不阻擋既有手動入會流程。

本輪修正已完成原始碼、991項可用回歸及隔離候選實跑：持久租約改`CLOCK_BOOTTIME`＋boot identity，保留真正過期／換boot fencing；TX僅首包加headroom，欠料後不再多加160ms；新增PCM准入／TX數值觀測，不新增內容、次數或回答時限。`teams-assistant:local-lease-tx-20261027`已於操作者明確核准後部署至上述本機服務。已安裝五個變更模組SHA與受測source一致；無OAuth／source overlay、network none的跨程序租約及實際Chromium AudioWorklet smoke通過。真人每次句首缺字的唯一根因仍欠當場逐包資料，不能把本機排程修正當成完整真人驗收。

切換前已確認無活動租約／場次／摘要工作，停止舊容器（SIGTERM／exit143，非OOM），以SQLite backup保存data root內`backups/pre-lease-boottime-20261027T103519Z.sqlite3`，目錄0700／檔案0600、完整性通過。正式庫已加入租約兩欄；遷移後、登入前逐表比對，24張表既有欄位完全相同，原audit完整保留且只新增1筆維持暫停的`settings_changed`。不要讓舊映像直接寫已升級庫。新版已實際登入管理頁，API為started=true／active=null／背景工作3／capture_interlock=true／last_error=`capture_cleanup_review_required`；關閉分頁後`healthcheck --port 18081`仍exit0。沒有解除interlock、啟用排程或Join；下一輪須取得清理審查所需的離會確認及新鮮在場確認，入會前先通知。Google仍需重新授權自動抓會，但不阻擋既有manual流程。詳見[evidence](../evidence/RESULTS.md)。

新一輪操作者確認在原會議且舊助理已離會後，完成cleanup review並只送一次manual Join。occurrence=`74ef3c637a6b4dc98437203f5df81281`、session=`85393098a70c414ab1ec7b53daedecab`，沿用原series；2026-10-27T12:26:46Z開始recording。其後操作者回報整段斷音、重複及錯答，**本輪真人驗收失敗**。當場觀察3題工具完整交付，但沒有模型消費ACK；144包PCM在`awaiting_new_assistant_turn`遭丟棄，TX欠料計數0。這證明host准入丟音存在，不證明所有斷音或錯答只有此因。本次native cutover未停止／替換此既有服務、未再Join；即時活動狀態須另查，不以初始ready當成功。

前次A原生化已將會議音軌經`native_media.py`本機RTC relay送入隔離Live peer，移除`_SOURCE_CLOCK_JS`與A Python PCM queue。B仍每題讀工具，不安全輸入PCM只拒收／計數，後續可恢復；本次只另改B輸出，不改A保存。新增frame接入、暫停恢復、空白UI frame及停止／未知清理有當時的波形證據；第17輪guard原失敗保留，不回填真人成功。

既有本機候選 `teams-assistant:local-native-completion-20261017`，immutable ID=`sha256:731ed95e512986a33df2543ec0b5c45ba073ea5924a93ed228889ec3cc28cff4`，**尚未包含 host-question 修正**。標準 Dockerfile 以已驗證 contexts 刷新15.13秒，當時49模組 host／installed／workdir SHA一致；實際 CLI、固定 Codex binary、sandboxed B RTC 靜音與同 track 恢復通過。首次 internal-network fixture 逾時保留；既定 routed fixture 通過，未延長 timeout／關 sandbox／加 caps，自有容器及 network 已清除。[映像收據](../evidence/native-completion-image-20261017.json)。前輪 Compose／媒體／管理頁仍是歷史版本證據，不代表本次原始碼已部署。


### 最新：歷史／長篇測試已再次入會，A／B就緒

- 正式B已改為native incoming track→跨隔離context本機RTC→owned Teams sender；移除Python輸出PCM、跨turn FIFO及分塊排程。每題fresh SQLite／完整快照保留，完整chunks後、marker dispatch前開通授權輸出；marker-return及scope重查後才commit。
- 原生decoder以零增益圖保持本機無聲；本機relay不進A capture／roster。sender ownership、stopped／ended資源及foreign替換的清理有實際browser驗證。Native quiet不等於遠端drain；完全晚到turn前的靜音音訊不能回補。
- 最終可用全回歸974項通過、0skip；缺來源的兩個歷史suite仍排除。已安裝候選8模組SHA吻合，sandboxed跨context下游兩輪、host650ms停頓、撤權與sender還原smoke通過。另保留間歇低RMS窗與一次尾段tone分類的負例；尚未有匹配證據可指認其原因，不宣稱所有斷音／重複已消失。
- 操作者「繼續，我在會議等你」後，已將本機服務切換至`teams-assistant:local-native-track-20261027`／`sha256:a22d154c6dc187f099dae6ac61333dd2b138de7fc1a57a330eb2aea363073252`。停機前無活動場次／租約／摘要工作，capture interlock=false；停止舊容器後完成私有SQLite backup，integrity=ok。resolved Compose僅image及其既有env傳遞值不同，未更動網路／授權／資源／排程。新容器identity見上文，實際管理登入、畫面及healthcheck通過。
- 2026-10-27T15:36Z先通知後，以原連結／原series建立一次manual場次；occurrence=`d50f24d729414205b49f6de55874e6e2`、session=`55b167917684470ca3462d6dbf91da40`。Join dispatch completed，但120秒後`meeting_join_failed`／`timeout`／`post_join_wait`，本輪未入會、未啟動A／B。最後UI有lobby_message=1及sign_in／prejoin控制，leave=false；多類訊號不能唯一判定未准入或辨識問題。cleanup=`cleaned`，daemon active=null／interlock=false／last_error=null。已通知操作者本次失敗並詢問是否看見准入請求；**不重送，等新的在場／准入確認後先通知再重試一次**。尚未取得新版真人播音驗收。
- 後續已獲准使用既有本機部署登入，完成host source的**真GPT Live隔離驗證：兩個session、各連續三題**。短答預算十萬→十二萬、多句三項待辦週五→週四，更新後首答及再次詢問均正確；每題各一次fresh SQLite／tool、版本2→3→3、內容各出現一次、同track且正常回sleeping。六題均在owned sender後的獨立receiver收到音訊，兩個session stop／cleanup乾淨；沒有production改碼或新增限制。Live端仍有concealment，未逐字聽驗下游音訊，不能宣稱真人Teams無斷音／全部舊問題已解決。詳見evidence最後一節；私有合成收據保留，臨時程式及資料庫已清除。
- 2026-10-28原連結／原series的session=`63037b2da04d43aeb4078507f0be9d08`、occurrence=`b1ce89ee902a4a8689e4825d33e62781`已實際入會，操作者回報「聽起來可以，我已結束會議」。查詢時系統仍recording、尚無摘要工作；透過正式管理頁對此session正常stop，11:15:40Z stopped／operator_stop、cleanup=cleaned。11:16:15Z摘要job=published／attempts1／error=null，實際一般Codex `gpt-6.1-sol`、json-v1 revision1／provider_requirement_met=true。151筆來源含10 provider finals與141 application segments；摘要為重點4／決策3／待辦3／未決4，3筆新待辦已保存open。實際歷史頁可見摘要，daemon active=null／interlock=false／last_error=null；沒有重入或重啟。
- 真人「聽起來可以」是本輪正面聽感回報，不等於所有過去斷音／重複／內容問題皆已驗收。本輪會後摘要是在人工正常stop之後觸發；未證明Teams會議結束自動偵測，不能把operator_stop寫成自動離會。下次需新的入會授權；本次已不在會議。
- 後續操作者要求再入會測前次內容／待辦及三分鐘朗讀。session=`1372c499daaf4a9cab8e887f1f0e6160`在Join dispatch後post_join_wait逾時；操作者另要求「再重新登入」後，只新增一次session=`9fc72a7c6c2844be956c59c45eb1fb8d`（occurrence=`419b45e479e848fda9d74e3222295133`），這次在audio_choice逾時，browser_continue=error、computer_audio=error、join_dispatch_completed=false，尚未送出Teams Join。兩輪cleanup=cleaned，最後daemon active=null／interlock=false／last_error=null；沒有重部署或音訊程式改動。
- 第二輪已凍結前次`63037…`摘要revision1、available=true／truncated=false，系列open tasks=8；其中前次新增3項，不能把所有系列待辦當成前次新增。尚未有這兩項真人問答驗收。朗讀稿與五題檢查位於`local://meeting-three-minute-reading.md`，約770中文字，僅給操作者朗讀，未預載SQLite或模型上下文。
- 非入會診斷：工具瀏覽器停在launcher；獨立sandboxed Playwright兩次到`/light-meetings/launch`，畫面是音訊／視訊權限提示及預覽，radio_count=0、Computer audio不可見。此診斷沒有owned input注入、未授權實體麥克風，不能把提示直接判定為production根因；它與production零radio的觀察一致，但尚未證明唯一原因或修復。沒有按Join、呼叫模型或用無音訊入會繞過。臨時診斷瀏覽器及程式已清理；接續須先處理／確認此預覽路徑，再取得新鮮在場確認，不再盲目重送。
- 操作者再要求「先再登入一次」後，已先通知並只新增一次session=`2174edb9dc08452c80d42cbb19afced0`、occurrence=`cee9994a552d4d07967efca5d7168be9`。本次未改程式／設定，已通過預覽與實際准入；最新recording／rx_ready=true、B sleeping／ready、error=null。凍結的前次session仍為`63037b2da04d43aeb4078507f0be9d08`、revision1／available=true／truncated=false。已請操作者先問前次內容與當次待辦，再朗讀`local://meeting-three-minute-reading.md`並提問；稿件沒有預載到會議或模型來源。**本輪保持運作，只關閉管理分頁；接續查這筆session，不得重建或重啟，兩項問答內容及人耳驗收尚待回報。**

### 前次：首段PCM音訊與完整可讀問答快照（歷史版本）

- 已重現首段capture太晚的遺失窗口。`b_live.py`將capture generation與最後marker送出放在同一個瀏覽器操作；`b_native.py`以既有有限FIFO保存早到PCM，等待完整工具交付、sender就緒及scope重查才放行，舊turn／取消／失敗不重播。持續decoder／播放器不重建。
- 第20輪4次SQL查詢均完整選取，最新事實已在來源內。受控Native仍能在marker返回後答舊資料或重複；只調整問題順序未解決。改由`MeetingQuestionContext`每題傳完整可讀快照，刪除模型端增量cache協定；逐片段座標留在host，wire以完整原文及連續來源索引範圍表示，無內容或筆數配額。這是接線修正，不是發現GPT Live的32K限制。
- 正式程式的實際喚醒／ASR、同thread三題provider-revision控制通過首答事實、無舊值與不重複檢查；582→596→617片段的三題控制也回答正確內容，但literal checker因「ORBIT五一／RIVER七二」保留失敗。沒有將先前誤答、ASR改義或填充語負例刪掉。本機Chromium送達12,288frames且首尾波形可見；不等於遠端可聽或所有漏字真因均已排除。
- `response_progress` schema3改報`committed_records`，不再使用已退役的cache計數。詳見[問答契約](INCREMENTAL-QA-CONTEXT.md)及[最新驗證](../evidence/RESULTS.md)。服務32已由修正版source啟動，操作者確認會議關閉並授權代辦後已完成正式清理審查，現在可待命；Docker未更新。新版真人驗收仍待重新開會及新鮮在場ACK。

### 前次：移除不必要卡控與單一混音觀測

- 完整保留授權來源、凍結歷史與核准詞庫；移除16K／4K選取、48KiB／64KiB envelope、16KiB initialItems及2K提問拒收，不用更大的任意數字替換。舊凍結省略標記仍真實保留。
- 回答預設deadline=None；移除45秒總期限／60秒設定上界及15秒prepare wrapper。普通查詢／傳送I/O失敗只作廢本題；下題fresh SQL，不重試。錯場、版本、文件／媒體錯誤仍fail closed。
- 會議預設沒有額外duration，但授權occurrence end／撤權／stop有效。摘要移除共用51秒總期限，以owned有效進度及持續lease heartbeat運作；重複／未知item不能續命。
- 正式B／WAV只觀測A target既有的單一24k原生混音。後到frame自然接入，不建立第二條B時間線；clock不可用不假稱B ready，也不停止健康A。target scope await之後才重查錄音授權，已以真browser反例確認撤權後不交付待處理PCM。
- 最後整合另修正兩個就緒生命週期問題：native start回傳ready必須採A的原生證據，不沿用觀測路徑的false；已准入的同document／token音軌只啟動一次，B校準重試不能拿快耗盡的setup deadline重啟A。缺B clock的實際worker及雙音軌波形控制已通過，未延長期限。
- 本機雙音軌／後到frame、真Native中文ASR→fresh SQL→完整內容／有效PCM、真Codex長工作發布均已跑。Native指定待辦可讀出，但先前控制仍有無資料誤答後補答案，最新控制也有多餘前言／填充語；完整語音品質及真人遠端不算通過。數據與前提見`../evidence/RESULTS.md`。

### 前次：只移除12KB與其他卡控審查（歷史版本）

- 不以另一個數字替換12KB。`max_delta_bytes`、`context_batch`及`context_delta_budget_too_small`已從active code／tests移除；單一完整JSON透過固定SDK分塊送完，再一次marker及atomic commit。
- 945項可用回歸通過；大型task引用fixture修改前被舊門檻拒絕，修改後成功。真Native中文PCM／ASR重新讀SQLite一次，17,418bytes、1,000來源引用完整保留，正常ACK／closed／reaped。准入後正確待辦前仍有異常字元，不宣稱完整語意／遠端語音品質。
- 本次其餘限制只review、未一概刪除。`B-LIVE-NATIVE.md`按Spec／Standards列出16K／4K檢索、48KiB歷史凍結、2K提問、45秒回答、15秒prepare、預設一小時場次、摘要時限，以及必須保留的資源／授權保護。
- 靜態發現late-frame缺時鐘校準、worker跨document共用frame counter、初次慢校準後仍可ready；尚未修改或重現，不回填為舊真人失敗唯一真因。


### 第19輪內容缺失與有效音訊背壓：2026-10-18

- A已保存三項測試事實；B兩次真的查詢並送出23,069bytes，卻兩次都未選入這些事實。舊程式把單批12KB序列化上限當作邏輯選取容量，較早的事實被後續問題片段／metadata擠掉；不能用tool次數或sent bytes宣稱內容正確。
- `ContextPlan.payloads`分批送齊選取範圍，長文保持原始offset／revision，所有批次後一次marker及commit。host cache淘汰不再刪掉本題資料；`receipt_readings`只作精確來源映射的閱讀投影，不新增provider final或跨receipt literal命中。
- 有效播音另改為單一in-flight binding、45秒PCM範圍的有界FIFO及絕對音訊時間節流，BrowserTX原0.5秒佇列與answer絕對期限不變。真browser的650ms停頓、真正慢消費溢位與generation撤銷均有回歸；不是宣稱現場真的停頓650ms。
- 保存來源v30／v52重播分別3／5批，三事實均在wire。正式Native中文PCM／ASR同thread三題，各一次新SQLite讀取；重複三事實與更新金額通過，累計125,210bytes，第一題包含650ms有效音訊期間停頓，正常ACK／closed／reaped。不是Teams遠端驗收。
- 初版僅觀察首個closed segment、日期答錯及第二題未產生新delta的控制另留存。最後probe補上0.5秒實際quiet後通過；不據此改正式分題規則、宣稱唯一provider根因或隱藏負例。詳見最終收據與`B-LIVE-NATIVE.md`。

### 第18輪無回覆修正：2026-10-18

- B 在41.723秒以 `response_output_backpressure/audio_callback` 失敗，44筆user delta但問題准入／工具／播放皆0；A與主服務仍存活，已有4筆provider finals／37段application來源。這不是32K或答案播到一半；[失敗收據](../evidence/teams-native-hostquestions-round18-failure-20261018.json)不回填為成功。
- 已用真Chromium重現「無speech lease仍回傳閒置音訊，頁面停頓後callback補發達八筆pending」；Python callback最高0.027ms仍可失敗。修正PCM tap的scope准入、關閉與generation隔離，decoder及Teams播放器不重建，真正有效音訊的背壓保護保留。合法continuation先開tap，既有FIFO保留owned sender啟動期間的首段。
- 最終943項可用回歸通過（仍排除兩個原移轉缺實驗source的bridge suites）；最終真Native先閒置50.654秒並注入650ms頁面停頓，再連問3題，每題1次SQLite讀取、修訂金額正確、有效語音／完成／drain與STOP ACK／closed／reaped皆通過。這不是修正後真人遠端驗收；[完整證據](../evidence/native-idle-output-capture-final-20261018.json)。


### Native 輸入分題修正：2026-10-17

- **根因已定位於本機契約假設：** 完整輸入第8、9題未收到 final；第10題 Native 才一次完成三題，並用快取回答、未新委派。本機沒有漏掉原始完成事件，但不能要求每次實體發問必有獨立 Native turn／delegation。提示控制曾通過10題後再失敗；不是固定八題／32K上限，也不能推定 provider 內部原因。見[隔離證據](../evidence/native-completion-contract-isolation-20261017.json)。
- **操作者已選擇主程式管理提問與查詢。** `b_question.py` 以實際 PCM quiet 與穩定 ASR 分題，明確為 application／非 provider final；`b_native.py` 每題新 request、新 SQLite 查詢，以 `session.context.append`＋`RESULT_COMPLETE` 回同一 thread。刪除 user-final／provider-handoff 的准入依賴，不借舊 ID、不逐題重連；保留喚醒、A provenance、授權、持續播放器與 assistant 完成／drain。
- **最終正式分題真 Native smoke：13/13題，每題1次 reader，45,843bytes，同一 thread。** 真 browser capture／PCM／ASR，不注入完成或 fixture 提問邊界；本場修訂預算、前次摘要、重複待辦、單獨喚醒後追問及短停頓均通過。未喚醒工具／輸出0，無 runtime error，stop ACK／closed／reaped。前一 transport-only prototype 及首輪11題另列，不混算成同一 thread。[最終收據](../evidence/native-host-questions-final-20261017.json)、[首輪收據](../evidence/native-host-questions-20261017.json)。
- 分題採1.5秒實際靜音、0.6秒文字穩定及單題2,000字元界線；沒有封包不算靜音，長停頓／雜訊仍有 heuristic 風險。累計 context 無本機上限，送出成功仍不等於模型消費 ACK。見[目前契約](B-LIVE-NATIVE.md#native-完成訊號與輸入生命週期)。
- 最終941項可用回歸通過，failures／errors／skips皆0；仍排除原移轉缺實驗source的兩個 bridge suites。[回歸收據](../evidence/native-host-questions-regression-20261017.json)。清理 await 期間的新 PCM 會增加 input revision，舊題因此不能恢復准入；取消舊題不永久撤銷有效的 session 發聲授權。
- 本輪未重入 Teams、未刷新既有程序或容器映像，不冒充真人遠端／長時間整體驗收。A 30分鐘、實際 Codex 摘要／重啟／系列待辦及前輪真 Native 撤權證據保留，但不是本輪重跑。歷史負例不回填成功；下一次 Join 仍需新鮮在場確認。

第十一輪操作者「有聽到，但沒說待辦、只說部分內容」為失敗，不回填為內容通過。已重現首段完成後清 handoff／關閉 output，後續同問題語音被丟的缺陷；改以 raw assistant turn ID 完成，保留有界同問題 continuation，新 user／喚醒、取消、錯誤、停止及原45秒絕對期限皆撤權。marker send 返回前不開TX；原始 user final 遲到不再誤當新喚醒。最終824可用回歸通過（131.682秒），兩個移轉缺少 bridge suites 仍排除。真模型有逐字命中控制與連續三題 cached refs／無 observed closed-gate loud callback，但最新三題逐字比對均未過，不宣稱真人完整內容驗收。既有待辦有「未明確」註記；不能編造完整清單。詞庫 entries=0，詞庫效果尚未對照。下一輪須先通知並取得新在場確認及有效會議；尚未完成遠端內容／持續穩定、自主排程與同版最終驗收。詳見 evidence 及 [Native 完成契約](B-LIVE-NATIVE.md#喚醒與輸出)。

### 第十二輪准入失敗與待續條件

操作者新鮮確認「仍在，可以再加入」後建立活動 13、對新 occurrence 送出一次 Join。2026-10-16 05:20:33 UTC 派送完成，05:22:23 UTC `meeting_join_failed/timeout`，操作者回報「允許了，但沒入會」。Teams 最後分類為 prejoin＋可選登入入口，不能以登入按鈕斷言需要登入。A／B 尚未啟動、音訊 0、來源 unavailable／failed；正常清理且媒體政策已撤回。第十一輪已發布摘要與原待辦保留，不將本輪空來源冒充內容驗收。

兩次不送 Join 的真實預覽檢查，一次音訊預覽準備成功、另一次逾時；觀察到的 request-refused／aborted 類別不能唯一歸因。原瀏覽器已清理，缺少操作者准入當下的轉換／連線證據，**第十二輪根因未證明，該次診斷未修改正式程式**。收據：[teams-admission-round12-20261016.json](../evidence/teams-admission-round12-20261016.json)。

活動 14／第十三輪曾正常入會並啟動 A／B，詳見 [ready 收據](../evidence/teams-admission-round13-ready-20261016.json)，但後續真人回報完全無回應，因此 ready 不當成內容驗收。本次已用正式 v3 允許的「先 handoff、後完成喚醒逐字」順序重現本機 scoped reader 未呼叫；修正後實際 SQLite → Native 邊界只讀一次並最後送完整 marker。後段 PCM 另修正 callback 回壓、實際 BrowserTX 0.5 秒佇列溢位、到期啟動與 pacing 換 turn 競態；都有失敗前／成功後直接證據，仍不冒充第十三輪唯一 fatal root cause。

正式 Native／原喚醒規則／legacy profile 的真模型靜音控制，使用同一既有 OAuth 與本場綁定 SQLite：未喚醒前 tool=0；喚醒後 handoff=1／admitted=1／tool=1，發出有效 PCM、無 runtime error、正常 stop／cleaned。沒有 Teams 或物理輸出，且保存待辦原句比對仍 false，不能宣稱真人內容已過。收據：[native-wake-order-control-20261016.json](../evidence/native-wake-order-control-20261016.json)。仍需新的在場確認後才能以原句做真人重測，不得無聲重入。

原第十三輪首個fatal與第十六輪generic Error均不可唯一回溯；相關失敗及第十四輪斷音回報保留。第十五輪完整句子播音通過，不替代歷史待辦全文、長穩及完整E2E驗收；目前服務22待命且媒體撤回，下一輪仍需新在場ACK。撤權須實查checkbox取消：合併form overrides會重加原checked欄位，送空值仍可能視為勾選，正常同源CSRF POST必須省略兩個媒體key。不得回寫原始TRANSFER-MANIFEST認證本次修改。


