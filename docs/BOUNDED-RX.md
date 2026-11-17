# Owner-confirmed RX 與有界清理

## 正式 standalone 路徑

`AssistantDaemon` 建立帶 capture lease 的 `Session`，呼叫同一 `worker.run_session()`；沒有舊 JobService／QueueWorker／detached CLI 或 ready 相容入口。正式 config 固定 `rx_session_contract=True`、當次 daemon owner 與凍結 context。

入會頁面、大廳、joined、prepared、owner_confirmed、recording、draining、terminal 分開。只有 fresh own prepared generation 的 owner confirmation 才可開始 capture／Live；重送、錯誤 owner、過期 generation、stop、lease 失效都拒絕。daemon 自動確認自身當次 prepared，不由外部任意 session 或 browser 分頁授權。

Capture 前／關鍵 await 後重讀 admission、停止、Calendar 同意與有效 lease。只有 Leave 可見不代表 joined；必須 semantic lifecycle 佐證，見 `TEAMS-JOIN-DIAGNOSTICS.md`。

## 時間及停止

- join、audio readiness、drain與shutdown維持有限期限。會議本身預設沒有額外duration硬上限，仍於授權occurrence end停止；明確設定的正duration政策可提早停止。
- Calendar 取消／撤銷錄音授權、管理 stop、scheduled end、service shutdown 都走同一持久 stop 路徑，保留 fixed end reason。
- SIGTERM 由 CLI／ASGI lifespan 傳給 daemon，停止背景 loop、請求 capture stop、清理 owned tasks；不把 summary model turn 放入 capture cleanup。
- 同一 cleanup deadline 涵蓋 drain、B close、peer／RPC、pending source writes 與自有子程序；取消不重置 deadline，shielded cleanup 仍保留真實 pending／reaped 狀態。
- STOP ACK、closed、子程序已退出、provider final 與實際離開 Teams 是不同證據。`stop_unknown`／cleanup unknown 不得回報成功。
- 正常停止先關閉 capture ingress 及原生 source gate，再關閉 owned relay，之後才送 provider drain／close。明確要求 WAV／record-only 時，在同一 cleanup deadline 內排空已接受 PCM；file／tone 維持真正 PCM adapter。supervisor 不重複取消進行中的清理。原生 A 沒有 PCM 傳送 sample ledger，不把觀測到的 PCM 當成已送達模型；pending WAV／PCM 或清理未確認仍保持 unknown。run=`failed`／cleanup=`cleaned` 不代表來源完整。
- capture／provider 的第一個非良性失敗保持不變；後續 caller cancellation、sender-close timeout 或 provider error 另記清理結果，不能把來源失敗改成 stopped。drain 或最後 stats await 期間才出現的真實 provider error，同樣不能發布 completed。

來源終結與 outbox 同一 SQL 交易。daemon 啟動後才到期的舊 capture lease，同樣產生持久 interlock 並暫停 scheduler；`claim_run()` 也在交易內拒絕所有新 capture，不可換場次繞過。summary outbox 的恢復不解除 capture interlock。

## A 原生媒體傳輸

`LiveProvider(..., media_source=meeting)` 是會議 A 的唯一接線：capture frame 的原生 WebAudio mix → owned 本機 WebRTC relay → 隔離 `LivePeer` 的固定 destination／官方模型 WebRTC sender。多個實際音訊 frame 混入同一 A；新增音軌與音訊 frame 接入原生 graph，空白 UI iframe 不影響 capture。relay 不進入 Teams sender roster，也不使用實體麥克風或外部 ICE。

`start_capture()` 在 owner／授權確認後打開 source gate；停止先關閉 gate、source track 及 owned relay。已擁有的 source／target document 失效、原生 terminal failure 與未知清理明確失敗。暫時 suspended／disconnected、單次 `ready=False` 或 optional stats 逾時不是整場中止理由。模型握手與 capture readiness 各驗證一次，不把第二次診斷快照當作額外啟動 gate。

會議 A 不呼叫 `LivePeer.push()`，不使用 AudioBuffer 逐包排程、自訂 playback-rate、時鐘 slope 門檻或 Python 送音 queue。file／tone 與 B 保留真正的 PCM adapter；這不是會議 A 的 fallback。B 的不安全 PCM／時戳封包只拒收並計數，後續合法封包可恢復；明確要求保存 WAV 時，PCM 完整性仍是必須履行的錄音契約。

正式native的B／WAV只觀測A隔離target上的單一原生混音track，不串行疊加每個Teams frame的PCM。觀測使用host校準clock與document-generation token；`observation_ready`獨立於A的native `ready`，缺B clock不停止健康A。新增frame加入既有混音，停止先關觀測入口。非native錄音的continuity依host驗證document分開追蹤，JS提供的ID不能自行建立權威。

`RxAudioSnapshot` schema 2 明確記錄 `input_mode`。native 模式的 `live_push`、排程 frames／queue 指標為 `None`；`native_source` 記錄有界狀態及真實 relay RTP 計數，cleanup 為 `idle/pending/cleaned/unknown`。未觀察到不等於零，RTP 計數不等於模型聽懂或 provider final。未保存 WAV 時 `audio_seconds=None`；`pcm_audio_seconds` 不冒充原生傳輸時長。首次真實失敗保留，後續 drain／cleanup 不覆寫來源位置。

## 僅測試的有界合約

`rx_test_contract` 與 constructor-injected fixtures 供合成測試縮短限制；與正式 session contract 的選擇互斥。它不是 runtime YAML／env 的假 provider 或安全 bypass。測試以受控 browser／Live、真 worker、SQLite 與必要的實際本機子程序驗證。

```bash
python -m unittest discover -s tests -p test_rx_contract.py -v
PYTHONPATH=tests:. python -m unittest test_rx_ready_ttl test_incremental_qa_cleanup test_assistant_daemon -v
```

實測 prepared/owner gate、TTL、stop-before-start、deadline、來源 drain、反覆取消、owned cleanup、未知狀態保留與不重播。合成成功不能證明真實 Teams admission、遠端 Live、RX 品質、B audibility 或完整端到端摘要。
