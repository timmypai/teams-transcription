# Beta 語音／詞表驗證準備，2026-11-02

後續可控重現、cleanup caller 修正、慢接收全段因果對照及完整 timeout 盤點見 [因果驗證](BETA-VOICE-CAUSAL-20261102.md)。下文的 616 項歷史失敗與其 bytes 繼續保留，並未因後續成功被抹除。

Base：PR #40 固定 `3632ac08a0c2e437f3098f14b9277e4f47349d70`。本輪使用獨立 `test/beta-audio-duration` worktree；不改 PR #40 head，不合併、不部署、不操作 Oracle、不發模型請求、不新入會。總模型帳本最後已知 28/50；本輪沒有讀主機帳本或修改額度，真正執行前仍須重新核對 canonical 值。

## 已實際執行

- `test_beta_voice_duration`：5 項虛擬時間／受控 RPC 契約。跨一小時閒置後三題沿用同一 session；回答跨 45／120／300 秒仍需 matching assistant done＋來源及 render quiet；取消後舊 turn 不可回放，新 wake 可再問；follow-up 過期後必須新 wake；平台明確 expired 不隱藏。這不是實際長時 ASR／平台持續可用證據。
- `beta_long` 實際 sandboxed Chromium 156.0.8078.47、amd64、cached image `sha256:08020a8a9da4dde8e2495cd57868e575dd2ad722de29de910c32c2a3be000076`：30 秒固定 Opus、1500 包、1,440,000 reference samples 全段連續精確匹配；missing／ambiguous packets、inter-packet gap 均空，payload 外最大絕對值 0。5 項 cleanup 全 true。這是已接線 receiver 的本機 browser 合成證據，不是新 ARM image／Oracle／Teams／自然語音聽感證據。
- 30 秒案例 encoded arrival→首個完整 render snapshot 觀測延遲 180.3–235 ms，平均 198.248 ms，**含輪詢，非耳端延遲**。觀測總 copy／overlap 617.2 ms、單次最大 7.4 ms，9 個 raced windows 被跳過後重讀；不能宣稱觀測零干擾。容器 memory peak 413,999,104 bytes／2 GiB，包含 fixture／browser，不證長期 leak-free。
- `test_glossary_asr_eval`：16 項 offline bytes/hash、NFKC/CER、詞彙對齊、普通詞誤替換、歧義、靜默、原文保留／私有檔安全反例。`test_glossary_native_pair`：17 項 DI／串行預算／同音訊／原始 revision／清理拒絕反例；未發真 ASR。
- `test_live_cleanup_snapshot`：7 項，獨立審查重現的兩個誤判已修。缺失／foreign thread RPC closed 不能確認 owned 關閉；RPC 已 reap 但資源清理失敗也不能 verified。保留 bound DC 路徑、第一失敗及原關閉行為，不改安全期限。
- 最終五個 focused suites 共 51 tests pass。先前 615 項 controlled aggregate pass；新增最後一項 pair regression 後的最終 **616 項有 1 failure**：`test_real_native_start_shielded_close_cannot_delay_probe_cleanup`，`tests/test_qa_probe.py:812` 的 `pending_operation` 為 true。兩份 receipt 及失敗 log 都保留，不反覆整套跑到綠；**最終 aggregate 不能稱 pass**。原 PR40 的 571 controlled／11 browser 既有收據保持，不以本輪重寫。此 aggregate 不是完整 original discovery，也不是原缺失 bridge experiments 或全部 browser suites。

該失敗的 QA probe／B native／RPC／測試四檔與 PR40 bytes hashes 完全相同，未呼叫本輪新 A cleanup API。測試先確認 operation 在 `run.result()` 後已 done，但存入結果的 `pending_operation` 是更早 cleanup snapshot；不能由後者已完成反推先前快照必為 false。原失敗 log 未保存分支／完整清理時間線，無法事後確證最初原因或所有當時資源狀態；保留 unknown，沒有改斷言／期限掩蓋。這不等於已證實是純環境波動，也不應當作現場模型清理已通過。

獨立 reviewer 僅做一次定向觀測：該 test pass／0.091 s，operation 45.004 ms 完成、cleanup 47.895 ms 返回，當時 live 0、pending close 0、reaped true。沒有重現原失敗；不拿此 PASS 抹除原結果。test finally 對兩個合成 child 都執行必要 kill＋wait；沒有另做 OS 殘留掃描或採集最終數值 returncode，不能冒稱獨立驗證全部歷史殘留。命令及純量證據保留在 manifest。

完整去敏 JSON／離線 log 壓縮及 SHA-256 見 [manifest](beta-voice-20261102/manifest.json)。只有合成 waveform scalar／對齊資料；沒有 PCM、私人詞表、會議原文、OAuth、token、SDP 或私人 URL。

## 時限及資源實際邊界

| 來源 | 作用／結論 |
| --- | --- |
| owner-immediate `maximum_seconds=None`、capture／worker duration None | 沒有固定整場秒數；沿既有 owner Stop、會議結束、安全故障／lease 清理路徑。本輪不加另一個整場期限 |
| production `_finish_native_speech` | 沒有 45 秒／總回答 deadline；必須正確 done 及雙 quiet，不能由 RPC final／累計 TX 取代 |
| `question_timeout=20` | bare wake 之後未開始追問的許可視窗；不是已開始長句／整場期限。新 wake 可再次准入 |
| PCM quiet 1.5 s＋文字穩定 0.6 s | 應用分題 heuristic。離線反例：2 秒自然停頓可能先交出前段，後段無新 wake 不再是一題；**未解，不能稱所有長句完整** |
| QA probe 45 s wait／120 s work | 隔離 synthetic QA 的 fixture 界線，沒有變成 meeting timer；本輪 30 秒 fixture 用 45 s 有界等待，不改 production |
| RPC 30 s、SDP 35 s、ICE 20 s、sender ICE 5 s、stop ACK 3 s及 bounded cleanup | 單次控制／故障安全等待，保留；不能因使用者取消整場上限就一併刪除 |
| 4096 input identities／output turn hashes | 有界 anti-replay 記憶保護，滿時 B fail-closed。來源路徑沒有直接把此 B error 當整場 Leave，但未做完整 worker integration；不能保證無限多輪 |
| provider `session.closed/expired` | 真實遠端事件，不是本機固定 120 秒。沒有證據支持固定兩小時上限；本輪不自動重開耗新 slots |
| native writer／browser buffering | PR40 的 40 ms clock lead 是 advisory estimate，非 native FIFO 上限／consumer ACK。既有 slow consumer 仍可有約 5.7 s 延遲及補零；**音質未全通過** |

## 詞表可執行準備與啟用缺口

工具與九類表見 [QA-GLOSSARY-AUDIO](../docs/QA-GLOSSARY-AUDIO.md)。同 SHA 音訊、人工 reference、只帶詞表的 initialItems、兩乾淨串行 session、私有原始 output／numeric revision、失敗保留、ordinary negative 和 CER 均有 source／離線驗證。

仍缺原使用者 Glossary.md 身份／hash／條目及部署 snapshot 匯入證據、人工 reference 與固定音訊。現有 root QA bridge／authority／projection 沒有 glossary pair operation；本輪不新增可繞帳本的 live CLI。實際 `DefaultLiveAdapter`／native WAV source 尚未真 browser→provider 驗證，純靜默無 final 仍 unknown。任何實測需固定 bundle 安裝與受限入口，canonical 預留兩槽、同全域鎖及 unknown interlock；先一對 pilot，不因九個類別就自動消耗 18 sessions，也不自動退款／重跑。

## 獨立審查及 Beta 尚缺驗收

獨立 Standards：兩項 cleanup P2 修正後，7 項回歸通過，無 remaining confirmed findings。獨立 Spec：最終 pair 17＋cleanup 7 通過，確認原文／revision、完整 cleanup gate、silence unknown 與未啟用聲明，無 remaining confirmed findings。這不是 GitHub 原生 review／approval，後續 PR 仍走既有流程。

Beta release 前仍須固定 PR40 正常合併／ARM image gate（需具體批准）、部署及相容 recovery、真正 Oracle 音質／延遲驗收；原使用者詞表真 ASR 對照亦未完成。可聽回覆、完整合成樣本及 job success 分別是不同證據，不能互相替代。這份交付不承諾明日必然通過全部驗收，也不把 deferred 寫成 passed。
