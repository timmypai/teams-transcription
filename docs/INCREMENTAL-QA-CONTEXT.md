# B 的 SQLite 每題完整問答上下文

## 綁定與讀取

`NativeLiveResponder` 接受 `b_question.py` 建立的本場 application question 後，呼叫 `MeetingQuestionContext.prepare()`；不以 provider user final／client delegation 當准入條件。每題由唯一 host tool `read_meeting_context` 重讀當前 scoped SQLite snapshot，送出完整可讀來源；沒有定時全文重送、任意 SQL、檔案路徑或跨會議工具。

reader 回 structured `records` 與已標 provenance 的歷史摘要／待辦，不解析 Markdown。主程式固定 meeting／thread，模型不能指定另一個 meeting。source locator 包含 canonical source ID、revision、source state／quality／provider final、`record_index` 與原文 character ranges；字元座標不是 UTF-8 byte offset 或 casefold 後索引。

模型不再負責維護增量 cache。每題完整快照包含當下的來源修訂；真正 provider 更正取代同 ID 舊文，不變成兩次發言。無 provider 身分的 receipt 保持不同 ID，不猜測 final／增量配對。version 退後、同 version 異文或重複／歧義 ID 拒絕。

`available` 只表示本次選取有可引用內容，不表示前次摘要是否存在。
`history_status` 另外回報凍結摘要的 `summary_available`、四類
`summary_item_counts`、`series_open_task_count`、`truncated` 與
`previous_transcript_included`。空分類不是沒有摘要；跨場待辦不是上次新增待辦；
本通道提供前次摘要而非完整前次逐字稿。即使本次沒有可引用事實，也能據 metadata
說明已保存摘要的存在與範圍，但不得補編內容。

## 可讀來源與原子交付

`selected_source_reading` 包含全部來源原文及來源品質；host `ContextPlan.result`
另保留完整 locators、版本／SHA、引用範圍及 query 命中證據，不把 hash／cache 操作協定交給模型。
同 capture、相同來源修訂且 receipt／part 相鄰的 application 片段可串成一段閱讀投影；
host 的 `record_ranges` 保留每個片段的原文座標，模型以
`record_index_range_inclusive` 表示相同的連續來源索引集合，避免逐片段座標淹沒閱讀內容。
投影取代零碎列，不另外重送相同來源文字，不依文字去重，
也不證明連續語音、同一說話者或 provider final。
跨 part 的 literal 命中仍只接續同一 receipt，不把可讀投影冒稱搜尋證據。

沒有 12KB 單次配額、32K 累計上限、256 段 cache 或來源筆數上限；不因內容增長更換 thread。
這不代表模型具無限容量或必定正確理解。實際 provider／連線錯誤仍如實失敗，
不裁切原始來源或用無界媒體緩衝掩蓋吞吐問題。

每個 `session.context.append` input_text 依固定 SDK 切為 ≤500 UTF-8 bytes，
使用同 thread／本機 request scope 及既有 commentary channel。
`application_question`、request ID、問題與完整來源在同一 JSON，不送 delegation ID。
全部 chunks 成功返回後才送一次 `RESULT_COMPLETE`。資料及 marker 返回、scope 重查及
輸出准入成功後才 commit；失敗、取消、timeout 或 scope 改變不提交未完成快照。
`sent_bytes` 是 host send-return 計數，`committed_records` 是最後完整提交的來源數；
兩者不是模型消費 ACK，也不能證明 provider 刪除了先前上下文。

B 的回答契約是只依本題完整快照；歷史來源保持歷史，舊問題的答案不代替本次讀取。
來源有 partial／truncated 狀態仍可回答其支持的事實，但須說明涵蓋限制；
available=false、缺少來源或 incomplete delivery 不得猜答。
來源原文、問題及歷史內容均為未信任資料，不執行其中指令。

## Native wake 與研究 probe

同一句 wake＋question 可直接查詢，不要求重問；只有 wake 才啟動 greeting。分題要求1.5秒實際 PCM quiet 與0.6秒文字穩定，沒封包不算靜音；短暫停頓不提前開 speech lease。問候實際完成後才開20秒單次追問窗口。這是 application heuristic，明確 `provider_final=false`，不改 A 的來源品質；長停頓／雜訊風險見 `B-LIVE-NATIVE.md`。

`qa_probe.py` 保留明確授權的獨立、靜音合成研究入口。它不入 Teams、不用真人麥克風、不作 production fallback。設定採 `load_assistant_settings`；固定合成來源寫入 SQLite，私有 JSON evidence 用全新路徑／0600。stdout 僅固定控制 receipt，不含來源／回答正文。`--authorize-live`、既有 egress／OAuth 與所需私有輸出缺一即拒絕。

Probe 的 total work 與 owned cleanup deadline 分開；外層 timeout 取消 operation 後，仍追蹤 finally 新生 close task／子程序。pending task、reaped、STOP ACK／closed 與 protocol unknown 分開；資源回收不代表 provider stop 成功。synthetic muted text gate 的 exact greeting 只關本地靜音 lease，不冒充 provider final／audio completion。

QA control receipt schema 2 的 `accepted` 另要求兩題各自 `audio_rx_verified=true`，以及 `owned_cleanup_verified=true`。每題只在該題 `answering` 的 output lease 內取 before／after；同題 continuation 沿用首個 baseline，前題／問候語累計不作本題證據。記錄同一 audio receiver 的 inbound `packetsReceived`／`bytesReceived` 增量，及 no-PLC 實收解碼 source packets／frames／duration／energy；來源 PCM16 RMS 必須達既有活動門檻8。首包前若該receiver尚無inbound report，只有inbound與source全零時可記錄SSRC未知的零baseline；after仍須有真實數值增量，已知SSRC更換仍拒絕。缺樣、receiver更換、counter reset、非有限／錯誤型別資料、只有TX／文字／render靜音均拒絕；正常quiet end才完成該window。這些是同一回答期間的音訊觀測，不是provider消費ACK、逐字語音品質或Teams遠端可聽證明。完整固定數值snapshot留在0600私有收據；公開控制收據只保留數值delta與固定失敗原因，不記track ID、SDP、token或例外原文。

cleanup必須同時確認stopped、STOP ACK、closed、native cleaned、process reaped、live processes／pending close tasks為0且operation已結束。舊schema1的accepted不能當成此RX驗收通過。CLI仍用同一固定模型／OAuth／兩題合成來源，未恢復服務摘要功能。映像正式宣告`espeak-ng`；ARM64手動build的既有Compose smoke會先在network-none隔離容器合成固定喚醒／問題PCM，缺依賴即失敗，這個前置檢查不呼叫account／模型。真正Oracle新映像及GPT Live RX驗收仍需後續發布與明確執行，不因離線fixture通過而追認舊映像。

固定QA問題要求「請只用繁體中文逐字引用會議預算原文，不加說明。」，以`espeak-ng -v cmn-latn-pinyin`合成；既有拼音喚醒仍用`-v en`。這與原生提示已有的繁體中文／要求原文時逐字引用規則一致，不修改正式Native提示或上下文。`exact_source_quote`要求同題question phase恰有一個非隔離問候的assistant final，其回答（僅去掉外側空白）完整等於當題SQLite來源句，包含原文標點；不是只含金額子字串或只查最後一句。先說前綴／錯值再回正確原句、重複多句亦拒絕；既有已識別的延遲問候仍隔離保存。英文翻譯、額外前綴／說明、錯值混入及舊revision均不通過。兩題RX合格、cleanup合格與內容引用合格是獨立條件；RX合格不可當成整體QA成功。既有映像的英文輸入／中文金額比對只證明測試契約混用語言，不能據此斷言是模型回答英文的唯一原因；新刺激及真實逐字語音結果仍需另行實測。

## 驗證與限制

```bash
PYTHONPATH=tests:. python -m unittest test_sqlite_voice_context test_delta_long_context test_delta_boundary_search test_incremental_minutes_context test_native_incremental_context test_qa_probe test_muted_probe_greeting -v
```

reader 每題仍 materialize／掃描當前來源，不宣稱持久搜尋索引、強制模型遺忘或模型消費 ACK。真 Native 控制測試與本機 Chromium 可以驗證特定輸入、交付及播放邊界，不能替代 Teams 遠端可聽性或保證任意問題正確回答；最新證據及負例見 `../evidence/RESULTS.md`。


固定合成的離線音素前置會用同一voice分別查「請」與完整問題：前者去除空白、stress／tone數字與joiner後必須為`tɕhiŋ`，整句不可出現外語voice切換（例如`(en)`）。空值、超過4096 bytes、無效UTF-8或不符音素均以固定錯誤碼在瀏覽器／模型前拒絕；不把音素原文寫入控制收據。這只驗證已知漢字／fallback缺陷，不是整句語音品質或模型ASR證明。既有image smoke實際執行這個前置及非零PCM檢查，另記`fixed_qa_mandarin_phonemes_verified`。

隔離QA voice A/B候選方法（本分支未執行模型）：host維持既有maintenance／paused與同一固定image，僅在一次性QA子程序內把`qa_probe.subprocess`替換成只有`run`的局部adapter；不要修改全域`subprocess.run`、運行app、安裝檔或接受判定。基線image來源必須是`00ba60768dc347fe41d64e3887f4e3bd748e2a79`，其已安裝`qa_probe.py` SHA256應為`d035385e486bab6db6704146b4f35d1d2ce831a35dea1aff6e0111425ac54535`；不符就停止。先以相同image離線驗證候選voice的漢字／整句音素，再審查核心覆寫：

```python
real_run = qa_probe.subprocess.run

def voice_only(argv, **kwargs):
    if argv == ['espeak-ng', '--stdout', '-v', 'cmn', qa_probe.QUESTION]:
        argv = ['espeak-ng', '--stdout', '-v', 'cmn-latn-pinyin', qa_probe.QUESTION]
    return real_run(argv, **kwargs)

# 只在該QA子程序；existing_args保留既有authorize/config/private-result等參數。
with patch.object(qa_probe, 'subprocess', SimpleNamespace(run=voice_only)):
    exit_code = qa_probe.main(existing_args)
```

完整host wrapper還須核對並記錄原image/source/module及wrapper SHA、固定問題SHA、覆寫前後voice與PCM SHA/frames/RMS、profile=`legacy`、stage=`question`及request prompt SHA；保留原QA私有收據與cleanup結果，新增一次模型通道就計入同一50次額度，不自動循環或重試。`qa_rx_evidence.py`、`b_native.py`與全部接受函式／門檻不替換；候選結果必須標記為「基線image加voice-only wrapper」，不能冒稱正式修復image驗收，也不能改寫case0003失敗。這是有界刺激比較，不足以排除provider隨機性或缺少的ingress/VAD觀測。wrapper本身不授予新權限，實際執行由原host owner按現有授權處理。

同一來源的revision更新由SQLite取代舊版；不同來源與歷史記憶仍完整交付，不以文字／金額猜測去重或按最大revision挑事實。每題wire明示`context_mode=full_snapshot`、meeting/thread scope、version及當前records的`snapshot_sha256`；可用的canonical source ID與revision也隨reading交付，metadata不能當成會議內容或全域時間順序。此快照取代先前可引用快照，前次assistant回答不構成來源。一般問題依當題來源自然回答；要求逐字引用時僅回支援問題的當前原文，不另加引言、引號或紀錄範圍說明，原文自帶標點照留。不同來源若矛盾則說明不確定性，不擅自挑值。

QA私有證據另保存至多8份constructed context plan摘要（每份至多64個reading的文字SHA／revision，標示截斷），以及至多8份工具delivery的固定phase／version／payload SHA／snapshot SHA／byte counts／marker receipt；不保存原始context全文或未知欄位。constructed plan只證明主程式建構內容；delivery只證明host send-return／marker，兩者都不代表模型消費。可比較兩題payload與snapshot SHA、revision／文字SHA及sent bytes，區分新快照含舊文、未送齊與模型仍沿用舊回答。case0004的voice-only候選雖恢復轉錄與兩題RX，前綴及舊值混入仍屬失敗；本修正未做模型重試，不回溯改判。
