# 清理收據及慢接收的因果驗證

Base `072eea2780168db9e982609818535733b6272c7c`，分支 `test/beta-audio-duration`。PR40 的 `3632ac08` 保持不變。本輪沒有模型／Teams／Oracle 操作、CI dispatch、PR、merge、部署或新安全設定；模型額度最後核實仍 28/50。新舊失敗紀錄各自保存，不以新成功覆寫。

## 1. Cleanup：可重現的交接快照競態已修，歷史分支仍未知

原 616 項的完整 log 在 `beta-voice-20261102/contracts-616-failure.log.gz`。順序為 `test_quiet_is_not_injected_during_owned_output_or_overlapping_tail` pass → `test_real_native_start_shielded_close_cannot_delay_probe_cleanup` fail → `test_rejected_clip_stops_without_sending_remaining_chunks` pass。失敗斷言原行 812：`pending_operation` true；更早斷言在 `run.result()` 後讀 operation.done() 已為 true。原 log 沒有 cleanup 分支、快照時刻或 close/reap 時序，不能事後補出首因。

以原 `run_probe`→`cleanup_probe` caller seam 建立兩個無前測依賴的同步反例；不改既有 test 的 `.03/.05/.12` 秒條件或斷言，也不新增 runtime 等待：

1. 真 cleanup algorithm 在明確過期的測試 window 產生 `pending_operation=true`。
2. 只在測試交接 seam 釋放已擁有的取消收尾，等待其完成，再將原 cleanup dict 交回 caller。
3. 舊 caller 原封不動回傳舊 `pending=true`，但 operation 已 done：精確重現同一觀測矛盾，RED 為 `True is not false`。
4. 對照例保持 operation 未完成直到收據已發出：必須仍 pending／unknown，不能假造 false；後來完成也不會修改原收據或私有 JSON。

修正僅在 **既有 cleanup await 返回後同步重讀 owned task/process**。原 window dict 保存為 `window_snapshot`，新觀測明標 `cleanup_caller_resume`；不多等、不另殺／reap、不延長 deadline。`unknown` 絕不升 `cleaned`，新發現殘留則降級。原始 protocol／deadline failure 仍在，`accepted` 門檻不變。不是將「重讀後都結束」冒稱在 deadline 內成功。

兩例從 1 fail＋1 error（缺新證據欄位）→2 pass；9 項原 cleanup 針對性回歸一次通過。已證明 **不需跨測試污染就能產生 caller-handoff 的過期收據**。未證明原 616 恰好走此分支；若 task 真在收據發出後才完成，任何誠實收據都可能仍 pending，不能承諾任意後續讀到 done 都能反推先前 pending=false。此保守 unknown 需由原 owner reconcile，不能退額度或重跑模型。

新增 QA 原始碼使 4 個舊固定 candidate tests 正確拒絕新 bytes（619 項，4 errors，log 保留）。測試現在用 `072eea` 精確 `qa_probe.py` fixture SHA `e40e2de9dc436573e93d04ea2c970ed519244c17c2ea055728deb136b8dbb862`；舊 checker／manifest／runtime pins 沒改，current source **仍須被舊 pin 拒絕**。這不是給新 runtime 授權。19 項直接 fixture tests、獨立 reviewer 的 41 項 candidate／startup／semantic 回歸通過。

### 後續快照 fixture 的獨立計時缺口

下一個 620 項 aggregate 原 cleanup 測試已過，但 `test_question_timeout_keeps_pending_media_snapshot_before_cleanup` 在原行 137 出現 `not_started != waiting_activity`。原 log 沒有 failure.timeout_seconds／framing 時序，不能反推是 phase 倒退。一次定向觀測未重現：1.671秒 pass，約1.66秒到 waiting_activity，兩次快照一致、最終 phase timeout 45秒；原620失敗仍保留。

另以受控 callback 間隔50ms、每次只提供20ms合成PCM、沿舊 fixture 的 wall-clock timestamp 規則重現：75包仍只累積0.02秒 quiet，不能達原1.5秒，RED。這證明 fixture 不能用回調排程時鐘冒充連續 capture clock，不代表已還原原620當時排程。

只改該測試 fixture：Native constructor clock DI、output clock及PCM共用連續24k虛擬 capture；quiet仍供原門檻所需的真實合成樣本數，3秒只等待真正 Native waiting_activity＋done barrier。原45／120／10秒值、private快照相等／取消／清理斷言及whole-work同步不動。兩例一次0.154秒通過，兩軸獨立review也各自重跑通過。這是 receipt契約測試，不是實際音訊／ASR／平台長時驗證。

完成上述實際修正及fixture隔離後，**最終620項 controlled offline pass／54.625秒，0 failures／errors／skips**。不是反覆未改版本跑到綠；每次 aggregate 間都有對應的已證缺口修正。616歷史失敗、619固定pin拒絕及620快照失敗與最後成功收據均保留。

## 2. 5.7 秒：本合成反例的積壓發生在 encoded fixture queue

新增 bounded scalar trace（最多 2000 rows）：每包 arrival、read、decode、刻意等待、native write 前後及 AudioContext clock；播放後才做整段樣本／時鐘對齊。PCM 留頁內清除，不外傳。保持原 receiver／pacer、no-PLC、quiet、timestamp、sandbox、network none、CPU／memory limits；同 cached amd64 Chromium 156 image，獨立 context 先 normal、後 slow。不是 Oracle／ARM 性能對照，計時仍受觀測與排程影響。

| 同 500 包／10 秒合成輸入 | normal | slow（每包寫入前刻意等 30ms） |
| --- | ---: | ---: |
| 實際輸入包／秒 | 49.996 | 50.004 |
| 實際讀出包／秒（含初始 checkpoint） | 49.448 | 31.989 |
| native write 包／秒 | 50.076 | 32.286 |
| fixture queue 在 read 取樣的最大包數 | 5 | 180 |
| 末包 arrival→read | 110.8ms | **5619.9ms** |
| 刻意等待平均 | 約 0ms | 30.276ms |
| write call→native write begin 平均 | 19.405ms | 0.0248ms |
| native writer await 平均 | 0.070ms | 0.0928ms |
| write end→render 取樣觀測平均（含 poll） | 87.633ms | 61.546ms |
| 首末包起點的 native render clock 跨度 | 9.980 秒 | 15.460 秒 |
| 加末包 20ms 的完整音訊跨度 | 10.000 秒 | 15.480 秒 |
| 精確匹配包／缺包／歧義 | 500／0／0 | 500／0／0 |
| 全段連續匹配 | true | **false** |
| 額外零洞 | 0 | **273 個，共 263040 frames＝5.48 秒** |

具體反證：若是 pacer 另加了 5.7 秒，slow 的 call→native wait 應累積而非平均僅 0.025ms；若主要是 native write 堵住，write await／write→render 應顯著成長，但主要成長已在 **arrival→read**。同圖正常組不注入 30ms 即無缺樣／插空。每 20ms 一包、每包至少處理 30ms，消費能力最多 33.3 包／秒，低於 50 包／秒；500 包理論至少 15 秒，再加初始化／解碼／排程成本，與實測吻合。這解釋該 fixture 的約 5.7 秒，不是猜硬體原因。

邊界：queue 是 timer-push `ReadableStream`，不是真 RTCRtpReceiver，不模擬 Chromium 原生 queue/drop 上限；native write return 也不是 consumer ACK。實際 Oracle 排程／丟包／遠端音質沒有新證據，不能由此排除現場也有 CPU starvation 或其他問題，更不能宣稱現場根因已修。render clock 是 native AudioContext history coordinate（有 snapshot quantization），不是遠端耳端時間。舊尾 16 包收據仍保持原 scope；本次才有全段逐包／時序。

### 吞吐不足時的具體處置設計（尚未啟用）

不加無界 buffer、不靠丟掉舊包追上、不補 PLC，也不引入另一個整場／答案時限。輸入長期超過可處理能力，保樣本、即時、有限資源無法同時保證；目前沒有可證實的 application API 能要求遠端模型降低音訊速率。native queue 內部還可能自行 drop，不能從本 fixture 推定其策略。

- 短期排程抖動沿既有 serial packet rendezvous／writer backpressure，不生成回答大小 PCM FIFO，不重寫 timestamps；source quiet／render quiet 分別保留。
- 如要在 runtime 識別長期不足，須以實際 packet timestamp、讀／寫及 native clock 的同窗變化建立去敏 degraded 診斷。不能用此次合成 queue 數冒充 native 深度，或把累計 TX 當順暢；本輪只加離線量測，未開 runtime observer。
- 明確降級為 `playback_not_realtime/continuity_unknown` 的設計不等於自動丟音。既有 owner 取消／撤權／安全失敗應 mute、撤 current output lease、關閉該段 reader/writer並標記答案已取消；保留已發生的 gaps，不假裝完整回答。不能暗中略過積壓後繼續說成成功。
- 恢復只能在 owned cleanup／current scope／原權限重查成功後，走既有 muted startup checkpoint，再允許新的 wake 題；舊回答 queue 不得流入下一題。沒有乾淨關閉證據時保持 unknown／interlock。沿用既有 B session 不代表可自動新開模型；新 session 仍需 canonical 預留額度。

本輪沒有啟用新的 auto-cancel、丟包、latency cutoff 或 queue cap。需要選定可接受延遲及 degraded 操作語意時，另給具體變更審查；不把使用者取消任意整場時限解讀為可以永久積壓。

現有可驗證邊界是 `test_b_no_plc_pacing` 的 pending ready／write／clock wait 取消、late completion 不得恢復、new helper 不沿用舊 schedule；`test_b_native_output` 保留 source/render quiet 分別重設、stop撤lease。它們支持上述取消／恢復的局部設計，**不等於已驗真實慢主機重新流暢播放或遠端 drain**。

## 3. Timeout／資源盤點

下列是既有 source 規則，這次均未刪除或放寬：

| 類別 | 位置 | 理由／限制 |
| --- | --- | --- |
| 整場 | `rx_contract.py:41,133`、`worker.py:569` | formal capture/total None；owner-immediate 沒有固定120秒；owner Stop、真正會議結束、授權／lease故障仍停止 |
| 單次回答 | `b_native.py:497,520` | 無總回答 deadline；正確 done＋source/render quiet，不用文字 final 代替 |
| 分題 | `b_question.py:38,138` | 1.5s PCM quiet＋0.6s文字穩定 heuristic；長句自然停頓可能分段，非模型限制，問題保留 |
| follow-up | `b_question.py:110`、`b_native.py:601` | 20s只管尚未開始的無新 wake 追問資格；新 wake可再問，不退出會議 |
| 輸入 queue | `b_native.py:67,378`、`b_question.py:16` | B12包／拒>1s舊PCM，framer freshness250ms；保護即時問句不是整題總時長。過期輸入可拒，不代表輸出允許默丟樣本 |
| A／legacy queue | `live.py:155`、`worker.py:90` | RPC notification32、legacy／錄音PCM256；正式A走native混音，不改成Python送音queue |
| 輸出流控 | `b_no_plc_pacing.py:53–68` | max(40ms,packet duration) advisory lead、單包<=5760frames／120ms及可取消5ms等待；非FIFO保證或答案期限 |
| 單次控制／啟動 | `rpc.py:58,155`、`b_live.py:387,394`、`assistant_config.py:28`、`b_render_history.py:127` | RPC30s、SDP35s、ICE20s、startup30s、primer500ms；故障界線不刪，不能限制已啟動回答總長 |
| watchdog | `worker.py:26,600–614` | joined消失持續10s，每約2s檢查，觸發transport_lost；不是正常靜默期限 |
| cleanup／Actions | `b_live.py:567–576`、`oracle-teams.yml:27–29` | Stop ACK＋closed3s及既有有界reap；Actions5min只管控制handoff，不持有daemon整場 |
| 平台 | `live.py` 的 `_closed`、`b_live.py:435` | 真實closed／expired事件；無證據稱固定2h本地timer，不自動重連耗新額度 |
| anti-replay容量 | `b_question_input.py:24,114`、`b_native.py:711` | input/output各4096身份，有界資源保護；耗盡B fail-closed，不承諾無限輪次 |
| 合成QA | `qa_probe.py` constants、`decoder_audio_check.js` play loop | probe45/120s及fixture16.5/20/35/45s是測試邊界，不能冒稱production截答 |

## 重現與收據

```bash
PYTHONPATH=tests:. python -m unittest \
  test_qa_probe.ProbeTests.test_cleanup_caller_refreshes_completed_operation_without_erasing_window_snapshot \
  test_qa_probe.ProbeTests.test_cleanup_caller_does_not_claim_unfinished_operation_has_completed -v

python evidence/beta-voice-causal-20261102/analyze_flow.py \
  evidence/beta-voice-causal-20261102/browser-pair.json.gz
```

browser執行命令、原始去敏 JSON、scalar report、RED/GREEN／regression及獨立審查 log 的 SHA 見 [manifest](beta-voice-causal-20261102/manifest.json)。原 Glossary／真人 reference／受控 root QA 入口仍缺，沒有新 ASR 分數，不妨礙這輪工具回歸；此結果也不代表 Beta 已發版。
