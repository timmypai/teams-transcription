# B 第二版 review 反例修復

本增量接在 `b34830facc30c86126effae785c329b2e9a7a8d6`。只修改 B Browser／dual policy／Live／native 模組與相關測試、證據；A RX-only／持久化、C、共同 RPC 不改。所有驗證為離線、合成 sender、假 RPC，無真會議或放聲。

## 正常停止不能中斷 RX

原 `owned.disposed` 分支會將正在正常 `replaceTrack(original)` 的 lease 判為違規並永久 fault。現在 stop 僅在原 lease 身份仍有效時標記 `stopping`。dual policy 在同 document／meeting／peer／sender／owner、generated 和 original 都 disabled、排程已清空的恢復過渡期間允許 RX 檢查通過，沒有授權任何 TX。未進入正常 stopping 的失效 lease、陌生 enabled audio／video，以及恢復期間被外部 enabled 的 original sender，仍拒絕並靜音。

`test_normal_restore_await_keeps_rx_and_future_speech_alive` 精確 gate `replaceTrack(original)`：apply 前与已 apply 未 reply 兩個 await 都有 RX packet 繼續，before／during／after=True，faulted=False，下一段 speech 可重新 authorize。另有過渡期 foreign video／original 外部 enable 的拒絕反例。

## close 取消後仍完成 owned cleanup

close 改為唯一 shielded shutdown task。並行 caller 共用一次 stop；caller 在 stop RPC／closed wait 期間被取消或再次取消，都只在 shutdown task 完成後收到 CancelledError。native 的 RX／tool／speech／timeout children 清理也在該 task 內，清空 PCM queue，再執行共同本地清理。

receiver 保持運作直到限定時間等 stop ACK 與 own-thread closed。status 新增 `stop_ack_received`、`stop_closed_received`、`stop_error`，未確認保持 timeout／unknown。`cleanup_status`／`cleanup_errors` 專門記本地清理結果：peer cleanup 失敗仍嘗試 RPC close，回 unknown cleanup 並拋出錯誤，不能假成功。已清理的重複 close 回原證據，不重新 stop。

`test_cancel_pending_stop_rpc_waits_timeout_then_owned_cleanup` 精確使用 entered Event＋永遠等待的 stop request，cancel 後 peer／RPC 各 close 一次、receiver 結束、本地 cleanup 完成，而 ACK／closed 為 False、stop_status=timeout。另驗證 ACK 後等待 closed 的重複取消、並行 close、native queue／children、peer cleanup 失敗仍收 RPC。

整合仍須採用原有 A guard callbacks／explicit dual-mode 每包檢查與 C 操作介面；原生 Live provider 回傳與真人 Teams 驗收仍待白天，不由此回歸推定通過。
