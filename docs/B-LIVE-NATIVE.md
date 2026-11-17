# B native Live：SQLite 會議問答與原生語音

Production 組合：`worker.py` → `response_runtime.py` → `b_native.py`。A 保存原始來源；B 讀取主程式綁定的 SQLite 記錄，再由 native Live 產生回答與語音。沒有 meeting Markdown、MCP adapter、text answerer 或 `appendSpeech` production fallback。

## 固定傳輸

- Codex CLI `0.159.0-alpha.3`，Live `gpt-live-1-codex`，v3 WebRTC。
- 既有 Codex ChatGPT OAuth；沒有 API-key 或其他付費 provider fallback。
- v3 `session.context.append`；主程式管理提問與查詢，不依賴 provider delegation，不使用 public Realtime `function_call`／`response.create`。
- 明確設 `delegationAckFiller=false`，不使用 provider 預設查詢過場語；模型本身仍可能分成多個 assistant turns，不能把首句完成當整題結束。此欄位由[固定版本的官方 start schema](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/app-server-protocol/src/protocol/v2/realtime.rs#L197-L206)核實。
- 每段 quiet context `session.context.append` 最多 500 UTF-8 bytes，`channel=commentary`，綁定目前 thread 與本機 request scope；不附 delegation ID。
- 所有來源分塊成功返回後，先核對授權並開啟已就緒的原生 track，再送最後 `RESULT_COMPLETE`；marker 返回且 scope 重查成功才 commit。這避免 marker 已觸發回答、host 卻仍未接通輸出的句首窗口。marker 失敗／取消立即靜音並作廢本題，不冒稱完整交付。
- 本機 send 返回與 context cursor commit 不證明模型消費了全部資料。`model_consumption_verified=false` 保持誠實。

## Native 完成訊號與輸入生命週期

- `b_question.py` 以已准入的 24 kHz mono PCM 活動、**1.5 秒實際靜音樣本**及 **0.6 秒 ASR delta 穩定期**建立 application question。活動門檻為 PCM16 RMS 256；封包必須新鮮且連續，沒收到封包不算靜音。單次待處理提問保留全文，沒有2,000字元拒收或溢位清空；發出、取消或停止後清除該題狀態。這是應用分題 heuristic，不保證語意／說話者邊界。
- 應用問題明確標 `provider_final=false`，不升級或改寫 A 的來源。Native user `turn.done`／`delegation.created` 僅作觀察，不授權或撤銷應用問題；assistant `turn.done` 須搭配實際接收的有效音訊及 quiet，不能單憑 done 截斷原生串流。
- 固定 v3 沒有已證明可用的 client 強制 user-final／委派完成 API。`RESULT_COMPLETE` 是應用文字，不是協定 ACK；不使用 public Realtime 的 commit／response.create，不借用舊 delegation，也不以逐題重連隱藏失敗。
- B 的兩種 media profile 共用既有 `SILENT_PEER_JS` 輸入 carrier：同一輸入 track 保留零音量 source，有限輸入 PCM 結束後仍產生靜音樣本，不開實體麥克風。正式輸出使用 native mode，不安裝 PCM tap／`responseAudio` binding；通用 `LiveSpeechChannel` 的明確 PCM 探針不是 production fallback。最後 close 關閉輸入與輸出資源。
- 只看接收端 sample clock 不足以證明持續輸入：Chromium 可在停止收包後持續產生 concealed samples。驗證須核對 sender 的實際 source samples、RTP 收包及接收端 concealment，並確認下一段沿用同一 track。
- `~/apps/dot/scripts/webrtc_peer.cjs:61–73` 及 `README.md:132–141` 的既有修正也是持續靜音；`android/core/src/main/kotlin/local/dot/context/core/VoiceProtocol.kt:146–213` 以 `channel=speakable` 回傳 host 已整理的短答，不是強制完成呼叫，也不是本系統 quiet commentary 來源接線的替代品。Dot 的 `/tbo/.../voice/calls` 是不同入口；其單題歷史收據不證明本專案固定模型的連續／長時間問答。此次只唯讀研究，未執行 Dot 或讀取其憑證。
- `b_native.py` 在 echo quarantine／封包過期時會停止轉送 B PCM，但不應停止 Native 輸入音訊時鐘。carrier 只產生零音訊，不回放被拒收資料，也不影響 A。

`thread/realtime/handoffComplete` 不是本接線的替代完成方法：固定 SDK 的 `handoff_complete()` 在 `client_managed_handoffs=true` 時直接返回成功，不送完成訊息；v3 的 FramelessBidi 也映射到該函式不處理的 SessionKind V1。RPC 返回成功不能當 Native 完成證據。見[固定 SDK 實作](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/core/src/realtime_conversation.rs#L1161-L1205)；不為了取得同名 API 改用另一套委派協定。

舊接線的[隔離重現](../evidence/native-completion-contract-isolation-20261017.json)證明：第8、9題無 final，第10題才收到合併三題的 user final，並出現未重新委派的快取回答。問題是本機把「實體發問」誤當成「必有獨立 Native 完成與委派」，不是已證實的事件遺失、固定題數／時長或 32K 限制。提示調整不能保證每題查詢。操作者已選擇改由主程式管理提問與查詢。

正式分題路徑的[最終真實 Native smoke](../evidence/native-host-questions-final-20261017.json)以實際 browser capture／PCM、模型 ASR 與 temporary SQLite，在同一 thread 回答13題，每題各讀一次資料；包含修訂預算、歷史摘要、重複待辦、單獨喚醒後追問及短停頓。累計45,843bytes，未喚醒的工具與輸出均為0，正常 stop／close／reap。沒有注入完成通知或以 fixture 邊界啟動提問；[首輪11題／transport-only控制](../evidence/native-host-questions-20261017.json)另存。這不代表長時間無限連線、真人 Teams audibility 或所有停頓／雜訊情境皆通過。

## 會前與問題上下文

B 的 `initialItems` 僅包含本系列前次已發布摘要與未完成待辦，明確標示為歷史記憶；歷史不是本場發言，也不授權直接回答。新場次凍結完整內容；舊副本若已有省略標記，仍保留其真實狀態。

唯一工具是 `read_meeting_context(query)`。meeting ID 由 host 綁定；模型不能指定另一個會議、URL、SQL 或檔案路徑。

`MeetingStoreMinutesReader` 從 `MeetingStore.read()` 建立 structured records：

- 原始 provider final、application receipt parts、未定稿 provisional 保留各自 ID 與品質。
- 前次摘要標為 `sqlite_derived_summary`；待辦標為 `sqlite_task_memory`。
- 歷史列的 `text` 直接保存可引用原文，不再把整個歷史物件編碼成第二層 JSON 字串。分類、時間、負責人、日期、待辦狀態及原有欄位放在 `historical_context`，隨可讀來源送出；來源 ID、來源 session／revision 與字元引用仍由 host 保留。歷史已完成項目與系列未完成項目不合併，本場發言即使長得像 JSON，也不解析成歷史或待辦 metadata。
- `source_locators` 使用原始 `source_id`、來源 session、provider revision／receipt identity、品質與 `record_index`＋原始字元 offset。
- 只用已保存且同 capture／同 receipt／連續 part 的資訊拼接跨 chunk 搜尋，不以文字相同推定相同事件。
- 同音人名、詞庫 canonical 或 Calendar 與會者名單不會覆寫逐字稿，也不作 speaker attribution。

`b_context.MeetingQuestionContext` 每題讀取一次 SQLite，建立當下完整來源快照。
`selected_source_reading` 保留全部來源文字、`record_index`、provenance、quality
及 provider-final 標記；同一文字若來自不同來源，仍保留各自身分，不依文字去重。
完整 `source_locators`、來源 revision／SHA 及本題引用範圍留在 host `ContextPlan.result`，
模型不再負責套用 upsert／replace／extend 或維護段落快取。

同 capture、相鄰 receipt／part、相同來源修訂且無序號缺口的 application
片段，可以投影成一段完整原文。host 保留各片段的 `record_ranges`；
模型只接收可還原來源索引集合的 `record_index_range_inclusive`，不重送逐片段座標。
投影取代各個細碎顯示列，各來源文字在 wire 中只呈現一次。
這不是 provider final、同一發言或說話者證明，也不新增跨 receipt 的 literal 命中。
投影不依片段字數設定門檻，不改寫 SQLite。

`ContextPlan.payload` 是一份完整 JSON；沒有 12KB、32K、記錄數或累計傳輸配額。
快照完整不表示會議已結束或錄到全部發言，`completeness` 與歷史涵蓋狀態仍須如實說明。

回答先給本次准用來源能支持的內容，再說明涵蓋限制；partial／truncated 不代表已選取的事實不可使用，不能只回覆部分紀錄提示。要求原文時應引用准用摘錄原句，而非改寫成摘要。這是回答契約，不是模型必然遵守或遠端逐字正確的證明。

每個符合喚醒規則的 application question，包含重複、歷史及已知答案，都建立新 request ID、
重讀 SQLite，將完整快照送回同一 Native thread。所有 UTF-8 chunks 成功返回後才送
**一次** `RESULT_COMPLETE`。分塊未完整成功時不開 TX；marker dispatch 前先完成授權及接線，marker 失敗、取消或 scope 改變則立即靜音，不能 commit。
前題 marker 不授權新題。分塊依據為[固定 SDK 的 500-byte UTF-8 切片](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/codex-api/src/endpoint/realtime_websocket/methods_frameless_bidi.rs)，不是總量限制，也不證明 server 拒絕 501 bytes。

完整 payload＋marker 成功返回、scope／output 准入有效後才 commit。`committed_records`
表示最後一次完整提交的來源數，不是 cache 容量。版本倒退、同版本內容衝突、錯誤作用域
仍 fail closed。`sent_bytes` 只表示 host send-return；不冒稱模型已消費、理解或刪除舊上下文。

管理狀態的 `response_progress.tool_deliveries` 最多保留16筆實際開始執行的工具收據，每筆最多64個正整數來源索引。`question_number` 是工具執行序號，不是人類發言序號；收據包含來源版本、選取索引、bytes、phase 與 marker-return，不保存問題、來源原文或 provider ID。`delivered` 只表示完整 send-return、scope 檢查與本機 commit 成功；`model_consumption_verified` 固定為 false。不能用這些欄位宣稱模型已讀懂或遠端答案正確。

`response_progress` schema version 4 保留 `committed_records` 與工具收據，改報 native receiver 的 packets／frames／duration／energy；移除已退役的 `pcm_admission` 與 PCM 排程欠料欄位。`question_receipts` 仍只是本機提問准入。舊持久收據不改寫；診斷不保存問題、來源原文或 provider ID。

## 喚醒與輸出

- 只有「小白小白」：分題後送 `application_greeting`，原生問候實際完成才開啟20秒追問窗口；窗口內開始的一次提問可不再帶喚醒詞。
- 「小白小白，這次預算是多少？」：同一句直接建立問題與查詢，不先插入問候。
- 短於1.5秒的句中停頓不分題；尚在變動的 delta 不啟動回答。超過界線的長停頓可能分題，這是應用 heuristic 的明確限制，不是 Native 完成證明。
- 未喚醒且不在追問窗口的發言不查詢、不輸出。重複相同問題仍是新 request；重複 request sequence、其他 thread、取消、失敗或停止不能准入／重播。

正式問候不逐字比對固定文案；正常原生問候的措辭／標點差異不能讓 B 永久失效。
發聲完成觀察採本場 datachannel 的 `turn.created`／`turn.done`，核對 answer scope 內的 assistant turn ID。所有已知 turn 完成、接收及播放都出現過有效語音，且真實 Opus 解碼來源與實際播放各有0.8秒 quiet 後才靜音；缺包產生的零值只能計入播放 quiet，不能計入來源 quiet。兩條 quiet 分別追蹤，避免來源已結束但播放仍有尾音時提前停止。沒有 turn ID 的 RPC transcript/done 不能結束另一段音訊；A 的 canonical 來源保存不改。

同一已准入 request 可包含連續或重疊 assistant turns；新 turn 使舊 done／quiet 結果失效，不清空音訊、不重建 track。等待靜音操作返回後仍重查 scope／turn，必要時恢復同題輸出。沒有回答總時限；新准入活動／問題、取消、錯誤、停止或撤權會撤銷 scope。無已准入問題的 assistant turn 不能自行發聲。

B 的輸入文字優先採固定 Codex 的 canonical `item/started` →
`item/transcript/delta`：先驗同 thread／realtime session 的 `transcriptSegment`
與 user role，再接受已綁定 item 的 delta。固定官方實作會把 final-only 來源
轉為 canonical delta；因此不需把 provider done 當作提問邊界。
Canonical 先於 flat mirror，後者不能重複追加；若先收到合法 flat user delta，
只保留該 session 的明確 flat fallback，不混合兩條文字流。未知、assistant、
已完成或跨 session item 的 delta 不進喚醒器；item identity/tombstone 記憶有界，
滿額會停止 B 並保留錯誤，不用逐字相同比對刪掉重複提問。
`question_input` 僅保存16筆固定事件／結果／字數／相對時間，不記錄文字或 ID；
`question_framing` 投影本地 activity、freshness 與 quiet，不能把封包數當成語音准入。
Realtime session 必須由本次已發出 start 所收到的 `thread/realtime/started`
先行綁定，不能讓第一筆待驗的 item 自行宣告 session 身分。這個 startup 通知
可早於媒體 ready；身分綁定不會提早准入音訊或放開輸出。

這是本機完成 heuristic，不是 provider「整題永不再延續」保證。完全晚到的同題 turn 可恢復同一 relay；若音訊早於該 turn 通知、且當時已靜音，沒有 PCM FIFO 可回補。native mute 不清空瀏覽器既有媒體緩衝，不能宣稱 RTP 已按 request ID 分隔或遠端 drain 完成。完成收據固定 `drained=false`，不把接收端 quiet 寫成 Teams 已完整聽到。

B 啟動時 muted。發聲需要操作者明確的 speaking policy，並由 daemon 對本場 active lease／controller 執行授權 control；設定 boolean 本身不直接打開 TX。授權 await 前後都重新核對 scope、停止請求及 speaking policy。停止與授權撤回先關閉輸出。

`BrowserTXSink` 僅使用本場已證明擁有的 Teams WebRTC sender。`OwnedSenderDualPolicy` 保護 sender／document／frame 身分、靜音及還原。B 的回聲時間窗及不安全 PCM 封包只影響 B RX；後續合法封包可恢復，A 的原生音軌與 SQLite 來源保存不依賴此回呼。A 的本機 relay 使用原始 `NativePC`，不混入 Teams／B sender roster。

## 持續原生音軌與不補音接收

- **每題仍讀工具。** 保留歷史 `initialItems`、fresh SQLite 完整快照及最後一次 marker；不改成歷史直接回答。A 的持續來源與 B 輸入／分題不變。
- `ResponseLivePeer` 在首包前安裝 `b_no_plc.py`：實際 Opus encoded frame → WebCodecs `AudioDecoder` → `MediaStreamTrackGenerator` → 原生 WebAudio destination → 隔離頁面間持續 WebRTC relay。relay 接收段使用相同的不補音路徑，最後才接精確 owned／已協商的 Teams sender。原始 remote track 不接播放器，也不把封包交回其 NetEq 解碼路徑。
- 只解碼實際收到的封包，不呼叫 PLC、不延續舊音；generator 缺資料時由原生 WebAudio pull 輸出零值，維持傳送音軌時鐘。單用 generator 會讓下游斷包後再 PLC，因此必須保留這個持續 graph。AudioWorklet 只回報 scalar 播放觀測，不排程或搬運音訊至 Python。
- 本地背壓每次處理一個 Opus packet，完成解碼／寫入並關閉 `AudioData` 後才讀下一包。共用 `b_no_plc_pacing.py` 在既有啟播檢查後，以 `AudioContext.currentTime` 及已提交樣本估算提前量；超過 `max(40ms, 本包時長)` 時暫緩寫入。這不是 native 消費 ACK、瀏覽器 FIFO 上限或端到端延遲保證；writer 完成也不證明已播完。等待可由 owned close 取消，不改 RTP／AudioData timestamps，不加入 PCM FIFO、PLC 或 BufferSource 排程。沒有答案長度、累計 bytes 或總時限配額。單包最多120ms是 RFC 6716 格式界線，不是內容限制。不能逐包 `flush()`／重建 decoder；實測這會破壞解碼連續性。
- 只協商實際支援的 Opus48k mono／stereo，仍驗證收到的 payload codec。RTP 時間戳支援回繞，重送／倒退包不回播；不使用 NetEq 的重排、PLC 或時間伸縮。缺包或頁面執行緒停頓可能變成明顯空白，不保證更順或每字完整；既有瀏覽器緩衝中的真實音訊仍會播完。遠端 Teams 接收器仍可能自行 PLC。
- 此路徑需要 secure context、encoded streams、WebCodecs、audio `MediaStreamTrackGenerator` 與 AudioWorklet。native B 一律使用本機 fulfill 的受信任頁面；既有 egress／SDP guards 不變。缺少 API、codec 不符或 graph 失效會停止輸出，不退回原生 PLC。`jitterBufferTarget` 不控制這個繞過 NetEq 的路徑；沒有新增啟播預存門檻或公開 buffer 設定。逐包 lead 等待只減少 burst 提交，不恢復晚到包；持續慢於輸入的 consumer 仍會積壓／欠料。上游 encoded stream 的容量及丟棄行為由瀏覽器實作決定，不冒稱整條 queue 有本機可控上限。
- close 先停止入口／靜音 owned graph，取消 clock、ready、write 等待，再沿既有 cleanup 排空。writer、來源、graph 或單包格式錯誤保留首因並停止 B 輸出；不能以丟包、補造語音或重設 decoder 把錯誤改成成功。播放估算不參與來源 quiet、render quiet 或 `drained=false`，也不新增整場離會期限。全段合成對照與限制見 `../evidence/B-AUDIO-CONTINUITY-20261102.md`。
- `NativeTrackOutput` 只管理授權、每題 scope 與 scalar 媒體觀察。正常完成只 mute 同一 track；`BrowserTXSink.start()` 重用已驗證的同 document／meeting／peer／sender，不逐題 `replaceTrack`。撤權立即停用並結束 owned 輸出；最後 stop 只還原仍 owned 且存活的原始靜音 track。已 stopped 的 transceiver 不復活，ended 原 track 不重掛，外來替換不覆寫；即使外來 track 已 mute，也立即使原 lease 失效。
- source 不補音 graph 由 `ResponseLivePeer` 擁有；target graph 由 sink 擁有，撤權先停用／停止輸出，再關閉解碼與 relay。source document／peer／track 失效仍經本機 control channel 撤銷 target；target 獨立驗證會議身分、授權、receiver → decoder → rendered track → exact sender 身分鏈。
- 撤銷會立即關閉本機 owned 輸出，但遠端仍可能播放既有媒體或自行補音。測試分開驗證「沒有新 RTP」與「尾音結束」，不能用固定300ms等待或本機 mute 宣稱遠端立即無聲。
- Teams 頁的本機 relay 使用 capture hook 保存的原生 PC factory，不進入 A 的 incoming mix 或 Teams peer roster，避免 B 自己的聲音回灌 A。
- 原 production 的 PCM admission、跨 turn FIFO、0.5秒 TX queue、45秒積壓上限及追趕排程均已移除；不是改成更大的 buffer。一般 PCM adapter 僅供明確的非 production 探針，沒有 fallback。
- `NoPhysicalSink` 觀測同一不補音 graph 的來源與播放 scalar counters，停用輸出 track 並拒絕解除靜音；觀測支線始終零增益。不安裝 Python PCM callback、不寫音訊檔。
- 本版以實際 sandboxed Chromium 的不補音接收與跨 context 下游缺包測試驗證；舊版原生接收的波形／host 暫停結果不直接沿用為本版證據。測試來源、模型生成、Teams 服務及真人耳端是不同層級，詳見 `../evidence/RESULTS.md`。

### 舊 PCM 路徑的歷史驗收

第十四輪真人有聲但中途斷接；[當次收據](../evidence/teams-native-round14-audio-gap-20261016.json) 保留舊排程欠料機制。當時同題新 turn 清掉待播 PCM 的修正已被本次 native cutover 取代，不把舊波形測試當成新版真人驗收。

第十五輪操作者曾確認「已正常，可聽到完整句子」，是當時版本的遠端單次播音證據，不代表本版或長時間問答通過。見[真人收據](../evidence/teams-player-round15-continuity-20261016.json)。

第18輪入會但完全無回覆，B 在41.723秒以 `response_output_backpressure` 失敗；問題准入／工具／輸出皆0，A仍保存來源。舊閒置 PCM tap 在 Chromium 停頓後補發 callbacks，造成背壓；本次正式 native mode 已移除此路徑。見[原失敗](../evidence/teams-native-hostquestions-round18-failure-20261018.json)，不回填為真人成功。


## 停止與失敗

`MeetingBrowser.capture_lifecycle()` 區分明確結束 `confirmed_end`、被移除 `meeting_removed`、連線中及無法辨識的 UI。短暫 UI 消失不等於會議結束；持續失去 admission 的來源以 `transport_loss` 失敗封存。Calendar 計畫時間、duration、操作者 stop、provider failure 各自保留原因。

終態保存不等於 provider finality。失敗 capture 的 context completeness 是 partial；已有來源可由持久摘要工作產生有品質標記的衍生摘要。清理未知時保留 `stop_unknown`／unknown flags，不聲稱靜音成功或重新開啟 capture。

管理 API 的 B 狀態另有 `runtime_failure`：首次 runtime 錯誤的固定 `code`／`phase`、白名單 exception type 與有界 elapsed_ms；後續錯誤不覆蓋，讀取者不能修改原收據。不保存 provider message、例外原文、URL、SDP、人名或音訊。舊場次沒有此觀察時為 null，不能事後推造根因；`response_failed` 仍是整體狀態碼。這是診斷，不會把 failed instance 自動復活或重試。

啟動失敗 `startup_error.phase=native_output_attach` 可另含固定 `output_step`：
`output_scope`／`relay_scope`、`source_offer`、`target_answer`、`source_answer`、
`target_ready`、`source_check`、`sender_install`、`initial_mute`。子階段在各既有操作前記錄，
第一個失敗快照在清理前固定；清理失敗仍另報 `stop_unknown`，不能覆蓋原來源。
未知子階段為 `unknown`，不保存例外訊息或新增 browser IPC。這只定位失敗邊界，
不證明 ICE、解碼、權限或 CSP 是根因，也不改 sender／no-PLC／cleanup 契約。

`target_answer` 的 `target_stage=decoder_setup` 現在保留清理前第一個
`decoder_stage`、`decoder_exception_type` 與固定 `decoder_reason`；例如受控 CSP
反例是 `worklet_module/AbortError`，不能因為原外層 `Error` 就推斷 codec／ICE。
`target_environment` 只含第一失敗當刻的 API 可用性、secure context、meta CSP
是否存在、AudioContext 固定狀態及 sample rate，未知為 null／unknown。

`target_document_policy` 來自持有該 frame 的程序，在 navigation 前註冊的被動
document-response 觀察；只讀 `content-security-policy`，不發新請求、不讀完整
headers。回傳僅有固定 script directive／Trusted Types 旗標，沒有 nonce、URL、
cookie 或原始 CSP。它綁定 frame 與目前文件，最多4個並行讀、8個 frame、4份
policy、每份16KiB及1秒讀取界限；取消／關閉清除 owned tasks。`available=true`
只代表取得 header 快照，不能當成允許 Blob 或有效政策全貌；meta policy 只記
是否存在。未取得或 stale 文件為 `available=false`。

這些是首次失敗時的有界診斷取證，沒有一般 JS／debug port 入口。取證失敗或
取消仍須執行原 owned cleanup；狀態讀取本身不再呼叫 browser，也不重試 B。

`progress.tx` 為既有 native observation 的 scalar 快照：`available`／`connected`、累計 `packets`／`frames`、`duration`／`energy`；`progress.speech_completion` 保存本段有效活動及接收 quiet，`drained` 固定 false。狀態讀取不另做 browser IPC。沒有量測時 `available=false`，不是零丟包／完整播音證明；有界整數及有限非負浮點數經 allowlist 持久化，不含音訊、文字或 request ID。診斷數字邊界不形成內容或回答配額。這些 native 接收量不是遠端聽感、逐字完整度或模型消費 ACK。

空的例外訊息仍必須保留非空的固定失敗旗標；不能讓 `TimeoutError()` 的空字串被視為「沒有錯誤」，再被遲到的 cancellation 重設為 sleeping。原始固定首錯收據仍保留實際 exception type／phase，不用虛構 provider message 補值。

## 驗證界線

```bash
PYTHONPATH=tests:. python -m unittest test_sqlite_voice_context test_b_question test_b_native test_question_receipts test_native_incremental_context test_delta_boundary_search test_incremental_minutes_context -v
PYTHONPATH=tests:. python -m unittest test_response_runtime test_response_progress test_b_browser_tx -v
```

後者啟動 sandboxed Chromium 與本機合成音軌，使用受控 Live fixture；沒有真人會議、模型或遠端播放。另已實際操作 sandboxed 本機 lifecycle UI，及 SQLite → scoped tool → native context commit boundary。

這些結果不代表真實 Teams 端聽到 B、長時間 native Live 模型成功，或詞庫提高 ASR 準確率。實際驗收仍須操作者提供有效會議、Google 授權、既有 account/media readiness 與已授權 egress policy。

## 本機卡控處理：2026-10-18

移除的是應用自行加入的內容／總時長門檻，不宣稱provider沒有自身限制。沒有把舊數字換成更大的硬上限。

|擁有層／舊政策|目前行為|
|---|---|
|`MeetingMinutesTool`：16,000字元選取、32,000設定上界、每列32字元及4,000字元摘錄|已移除；授權來源依原始順序完整保留，來源ID、revision、query／boundary metadata不變。|
|`history_context`／store：48KiB凍結、64KiB envelope及16KiB initialItems裁切|已移除；新run凍結完整歷史與待辦。舊副本若曾省略，保留其truncated／omitted記錄，不用後來資料改寫舊revision。|
|`glossary_context`／store：16KiB詞庫及aggregate容量驗證|已移除；完整載入核准且適用範圍的快照，保留欄位驗證、revision／SHA與initialItems標籤。|
|`b_question`及下游：2,000字元拒收|已移除；每題保留全文，維持喚醒、實際PCM quiet及ASR穩定判定。|
|`b_native`／`b_audio`：隱含45秒回答、60秒lease設定上界|正式native輸出沒有回答deadline或PCM積壓容量；同題continuation不限總時長，撤權／停止／新問題仍撤銷。通用PCM探針的明確有限政策不套用正式Native。|
|`b_native`：15秒prepare總逾時、普通讀取錯誤永久停B|已移除；普通I/O／查詢失敗只作廢本題，下題重新查SQL，不自動重試。錯場、版本／來源完整性錯誤仍fail closed。|
|`assistant_config`／daemon：預設一小時、最多四小時|預設max_duration_seconds=null；明確正整數政策可提早停止，沒有四小時上界。授權occurrence end、撤權及stop不變。|
|`summary`：共用51秒generation總期限|已移除；各turn以60秒無進度watchdog辨識停滯，健康進度可持續。只認owned非空文字或recognized item的新生命週期，重複／未知事件不續命；summary lease持續heartbeat。|
|doctor／synthetic QA：空egress清單視為未設定|已修正；[]是合法deny-all，格式錯誤仍拒絕，不放寬HTTP／WebSocket政策。|
|`b_context`：cache256段，設定1–512|已退役；每題完整快照，不要求模型重建增量快取。來源版本／完整性與原子送出仍驗證，沒有來源筆數上限。|
|Native 500-byte UTF-8 chunks|保留固定SDK分塊，完整結果＋唯一RESULT_COMPLETE；沒有累計配額，也不是已證明的server拒絕門檻。|
|摘要entry20,000／group40,000bytes|保留有界分組；所有來源切片皆處理。多turn成本及跨group摘要整合限制仍存在。|
|摘要回覆512KiB、發布2MiB|保留模型回覆／儲存資源保護；超過明確失敗，原始來源不刪。|
|有限RX queue／舊PCM FIFO／BrowserTX queue|輸入RX保留背壓與資源保護；正式B輸出的PCM FIFO、TX queue及其容量已由native track relay取代，不新增無界host buffer。|
|clock／freshness／quiet、授權與作用域、SQLite fence、setup／cleanup期限及unknown interlock|保留來源與資源安全；不是內容配額，不偽造timestamp／final／成功狀態。|

### 多文件輸入的擁有層修正

正式native會議的B／明確要求WAV，觀測A隔離target page上**已混合的同一條**`__live.destination.stream`。沿用24k ScriptProcessor PCM分幀，只建立一個host校準clock及document-generation token；不再把各Teams frame的24k PCM串行送給B，也不把Python PCM推回A。新增frame／音軌自然進入同一原生混音。

這同時避免跨frame offset被誤判gap、後到frame缺clock，以及一個frame安靜而另一個仍發聲時誤判quiet。初始clock只在既有setup期限內校準；`observation_ready`與A的native `ready`分開，start回傳的ready採A原生證據。已准入的同document／token音軌不因B校準重試而重啟，避免快耗盡的setup deadline反過來誤停健康A。缺B clock不冒稱B可用；停止先關觀測入口，再在有界cleanup中清理relay。非native明確錄音仍使用其document-bound觀測路徑，不是fallback。

這不是第19輪真人失敗唯一真因的追認。真人Teams遠端可聽度仍需另次授權驗收；本機證據見`../evidence/RESULTS.md`。

### B 初始化期間的 PCM 准入與首次失敗

當 A 使用原生 track、且不要求保存 WAV，Python PCM 只有 B 這一個 consumer。worker 在此範圍註冊 host-owned `capture_ready`，native observation 在 B 初始化、停止或故障時直接丟棄這些 callback，不排入 owner／dual-policy evaluate，也不緩存或稍後回放。原生 observer 及 A track 不重建；需要 A PCM 或 WAV 的路徑保持原處理。准入在取得 callback lock、owner evaluate 與 policy evaluate 後重查，避免 await 期間停止後仍交付。

B 必須完成 native 初始化，再通過一次新的 dual-policy check，才發布 `response_ready` 並接收 PCM。既有每包檢查、每次解除靜音前的 sink 授權、預設 muted、2 秒 policy timeout、來源／document／clock fence 均保留。policy 首錯鎖定後，一般等待中及後續檢查直接拒絕；invalid-binding 的明確空綁定撤權仍執行，不把這條清理路徑當作新授權。Stop 先關閉准入，取消並等待自己擁有的初始化，再完成原有清理，不讓初始化在 Stop 後重新開啟 B。

並行 Stop 只提出一次初始化取消；等待以 shield 隔離，取消其中一個 Stop caller 不會再次取消初始化擁有的清理。這是新取消路徑的競態保護，不是現場失敗原因的判定。

native 已 ready 後的 policy 拒絕標示 `input_policy_check`；失敗後 `startup_phase` 取首次 `startup_error`，不被 native 的 ready 或清理階段覆蓋。`consumer_unavailable_packets` 只記錄 callback 未准入次數。`capture_policy_failure.elapsed_ms` 是從 policy 建立到首錯的時間，不能當作單次瀏覽器 evaluate 耗時。

2026-11-03 的 host 去敏快照確認 native ready 後被已 faulted 的 policy 阻擋；此修正移除初始化期間 B-only callback 的檢查壓力及其等待佇列。Chromium 為何逾時仍未證實，程式與離線測試通過不能取代新映像／Oracle／真人音訊驗證；本修正不自動開啟輸出或重新入會。

