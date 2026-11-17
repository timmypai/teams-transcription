# 完整增量保存與來源品質

## 固定協定來源

Codex CLI 固定 `0.159.0-alpha.3`，官方 tree `3b01b36fa5eb96ba82a776bd3c2fc57f8969181f`。

- [RPC realtime schema](https://github.com/openai/codex/blob/rust-v0.159.0-alpha.3/codex-rs/app-server-protocol/src/protocol/v2/realtime.rs#L430)：delta 為 `{threadId, role, delta}`，done 為 `{threadId, role, text}`，沒有可依賴的增量 ID／revision／來源音訊 clock。
- [frameless parser](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/codex-api/src/endpoint/realtime_websocket/protocol_frameless_bidi.rs#L15-L96)：真正 `turn.done` user transcript 才形成 provider final；`turn.created`／`turn.delta`、canonical item completed、local stop／seal 均不可代替。
- 固定協定沒有核實的 audio-commit／final-flush 保證。`session.close` 已送或收到 closed 不是定稿證據，不套用另一個 public Realtime API 的 VAD 名稱。

## 持久保存

`rpc.py` 解析時記本機 UTC 接收時間，不捏造 provider 欄位。`LiveProvider` 僅接受 own thread 的 user 來源；foreign、assistant、unbound 通知不入來源。

正式 worker 在 bounded queue32 前，將整筆增量以 UTF-8 最多 8192 bytes 分段，一次 callback／SQLite transaction 保存全部 parts。沒有 64 KiB 後停止保存的路徑；語意段落與 provider finals 分開。done 同樣先保存再進 consumer，不能重複寫入。

`transcript_increments` 保存 capture、receipt_sequence、part_index、原文、received_at、late 與可空 source identity／revision／sequence／timestamp。`increments` 保留修訂歷史；`segments` 呈現同一明確身分的最新 revision。相同身分異文拒絕；沒有身分的同文保存，不能猜重送。

SQL synchronous／跨程序 lock／lease fence 保護 durable effects。來源已提交即可讀，不等待 provider done；錯誤、cancel、late delta、restart 或下游 B 不會覆寫原文。全部文字只在 SQLite，不生成 Markdown／JSONL，也沒有投影修復或 MCP paging。

## 品質與排序

| Source state | Finality | Ordering／clock |
| --- | --- | --- |
| `provider_final` | 真正 provider done | 明確 provider sequence 優先，其後 receipt；clock 未知可為 null |
| `application_segment` | 未定稿保存 | 本機 receipt order；本機 receipt 不冒充 provider sequence |
| `legacy_provisional` | 未定稿／可能歷史截斷 | `grouped_source_streams`；缺少來源 clock 不補零 |

`transcript_state` 分開 saved、unfinalized counts、provider-final counts、capture seal 與 source gaps。terminal／可讀／summary published 都不升格為 provider final；來源部分可讀仍是 partial，不是假 unavailable。

`Session.entries()`、summary publication 及 B source locator 使用一致品質與 ordering。完整來源 SHA 不依 Markdown 排版；mutation 回傳 bounded metadata，明確完整 read 仍可能配置整場 RAM。

## 收尾與媒體留存

停止 capture 後先關閉原生 source gate／owned relay；file／tone 的 PCM adapter 另等待已排程 WebAudio 尾端。沿用同一 deadline，最多送一次原生 [`session.close`](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/codex-api/src/endpoint/realtime_websocket/protocol.rs)，保留剩餘通知窗口，再關 peer／RPC 並 drain 已接收通知。不得延長 cleanup deadline 或把 cleanup unknown 藏成成功。

固定版本的真實 datachannel 已觀察到 `session.closed`／`reason=client_request`，不是先前只接受的 `requested/stopped`。只有本機已進入 closing 且已要求本場 session close，才將這個精確原生 reason 當作正常 acknowledgement；單設 closing、未要求關閉、RPC／foreign scope、expired、transport error 與未知 reason 均不因此變成成功。ack 可在 send await 返回前抵達；既存或後到的真實 provider failure 仍保留。這不增加 provider final／flush 保證。

[app-server SQLite log filter](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/state/src/log_db.rs#L66) 與 `RUST_LOG` stderr filter 不同，TRACE 可含編碼媒體。RPC 的 sqlite／log 私有目錄必須在確認的 `/dev/shm` tmpfs，子程序退出後清除；缺少 tmpfs 回 `codex_runtime_requires_tmpfs`，不退回磁碟。既有 OAuth home 不移動或複製。沒有 WAV 不足以宣稱沒有任何媒體 payload 留存。

## 下游與驗證

B 讀結構化來源，附 canonical source ID／revision／quality／`record_index` 與原文 character ranges。超過 query budget 時取命中附近的 bounded excerpt；同 receipt 的連續 parts 可跨界搜尋，但不能拼接不同 capture、revision、foreign source。詳見 `INCREMENTAL-QA-CONTEXT.md`。

```bash
PYTHONPATH=tests:. python -m unittest test_durable_delta test_delta_final_identity test_delta_boundary_search test_delta_long_context test_delta_clock_quality test_delta_summary_context -v
```

歷史原生語音曾有真正 final，但本次不能用歷史收據宣稱真人 Calendar／Teams／Live 全流程完成。新驗證結果見 `../evidence/RESULTS.md`。
