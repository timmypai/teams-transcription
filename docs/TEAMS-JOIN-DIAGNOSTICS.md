# Teams 入會診斷（schema_version 1）

SQLite `session_events` 的 `joining`、`connecting`、`waiting_for_admission`、
`joined` 與 `meeting_join_diagnostics` 事件附帶 `join` 快照。
`browser_open`／`meeting_join` 失敗、停止或取消也將最後快照寫入
`session_runtime.status_json` 的 `diagnostic.join`。不產生會議 JSONL／Markdown 投影。

固定 `phase`：`context_setup`、`page_create`、`page_setup`、`goto`、`page_ready`、
`starting`、`body_read`、`admission_check`、`browser_choice`、`audio_choice`、
`name_fill`、`mute_controls`、`join_control_scan`、`join_dispatch`、
`post_join_wait`、`lobby_wait`、`joined`、`unknown`。
`page_state` 只存既有判斷所得的 `unknown`、`unrecognized`、`prejoin_controls`、
`connecting`、`lobby`、`joined`、`login_required`、`connect_failed`、`ended`。
`outcome` 是 `in_progress`、`joined`、`timeout`、`stopped`、`cancelled`、`error` 或 `unknown`。

`context_ready`／`page_ready` 表示物件建立成功；`goto_started`／`goto_completed`
表示原有 navigation 開始／返回，並不代表已入會。保留歷史欄位名稱
`body_observed`：現在表示一次 semantic UI 狀態讀取完成，不再以整頁 body
文字決定 Teams lifecycle。`join_dispatch_started` 表示已呼叫 click，
`join_dispatch_completed` 表示 click 返回，兩者均不等同 Teams admission。
`joined` 須排除 lifecycle blocker，並在同一 frame 看見 Leave 與
Participants／Chat；Leave-only、不同 frame 的控制項或 connecting 都不能入會。

大廳判斷與 `post_join_ui` 的 `lobby_message` 計數共用
`browser.py` 的 `_TEAMS_LOBBY_MESSAGES`。包含
“Someone will let you in when the meeting starts.”；姓名前綴不影響判斷，
也不寫入診斷。大廳畫面仍可能保留 Join、Sign in 或 Leave；
這些控制項不代表已 admission，必須繼續等主持人准入。

`join()`、`admitted()`、`capture_lifecycle()`、prepared/capture 的檢查共用同一個新鮮 UI 判定。
Teams 只讀可見 `status`／`alert`／`dialog`；排除 log/feed 及帶有
chat/caption/transcript/conversation 標記的來源祖先與子樹。
混合 dialog 保留真正的大廳提示，不能因包含聊天就丟棄整個 dialog。
frame roster 或 page 在讀取期間變動時，不能以舊控制項宣稱 admission；
原入會 deadline、停止／取消與 prejoin frame recovery 維持不變。

入會前 `_response_preview_navigation()` 的 SDK 讀取錯誤，由呼叫端重新檢查
page／frame／roster；只有已證明 frame 快照失效、且尚未派送預覽動作時，
才沿用既有整批 scan 丟棄機制。活 frame 的 SDK 錯誤仍失敗；不重按不確定的
預覽／radio／Join，不延長原 deadline。回歸見
`test_computer_audio_frame_lifecycle.py` 與 `test_prejoin_preview_navigation.py`。

Teams 的裝置權限提示可能在電腦音訊選取或姓名填入後才出現，遮住底下的控制項。
每次選取前、選取後與姓名填入後都檢查已知裝置預覽對話框，不只在找不到 radio 時處理。
只允許唯一、可見、內容符合裝置預覽、且具有唯一 enabled
`Continue without audio or video` 按鈕的 dialog（若背後仍有 radio／Join，
該 dialog 還必須明確具有 `aria-modal=true`，且 dialog 本身不能包含 radio／Join）；
或在精確 Teams origin 上無任何 dialog／radio／textbox／switch／link／其他按鈕及終態阻塞訊息、
且具有可見標題與唯一 enabled `Continue without audio or video` 按鈕的獨立裝置前置頁。
這是關閉預覽提示，不是核准無音訊入會；成功關閉後作廢舊 audio proof，
使用新 frame roster 重新掃描、選取及讀回電腦音訊。已開始 Join 或不確定的預覽點擊
不重試；不授予實體麥克風權限、不使用強制點擊、不延長 admission deadline。
`test_prejoin_preview_navigation.py` 的實際 Chromium 案例覆蓋選取後／姓名填入後的遮擋，
並驗證提示關閉時即使 radio 被取消選取，也要重新選取成功才只送一次 Join。

Capture 另回報 `confirmed_end` 與 `meeting_removed`；短暫 connecting／unrecognized
不當成會議結束。持續失去 admission 的 capture 以 transport loss 保留部分來源，
不以正常結束掩蓋斷線。Calendar 計畫停止、操作者停止與 provider failure 的原因由 daemon 分別保存。
此判定取自 [meeting-lifecycle 的既有實作](https://github.com/paulpai0412/teams-transcription/blob/a5dd1a591ac5d84e431ccf7045762666f3f4d73c/teams_transcription/browser.py#L589-L664)，
不是整條 branch 切換；不移入其 application-final 或摘要語意。

`controls_checked` 表示至少一次 visibility 查詢返回，`controls_seen` 表示曾可見，
均為布林 latch；false 不是控制項不存在的證據。固定控制項名稱為
`browser_continue`、`audio_continue`、`computer_audio`、`name`、`mute`、`join`、`leave`。
`actions` 描述最後一次對應動作的 `not_attempted`、`inspecting`、`not_visible`、
`dispatching`、`completed` 或 `error`；`errors` 最多保留最後八個
固定 phase／action／安全 exception_type，不保存例外訊息。

Teams 失敗診斷使用已觀察到的 RAM 狀態，序列化後立即保存；不再額外查 DOM 或音訊 stats。
這些欄位不包含 URL、token、SDP、姓名、DOM 原文或音訊。
診斷失敗不取代原本入會例外；終態快照不接受後續 RX／cleanup 的修改。
既有 goto 45 秒、入會設定、輪詢、mute、admission 與模型/media 行為未放寬。
正式 supervisor 可能以取消 join task 處理 owner stop，因此 join outcome 可為
`cancelled` 而 session 終態仍為 `stopped/rx_stopped`。

離線驗證：`PYTHONPATH=tests:. python -m unittest test_teams_join_diagnostics test_browser -v`。
前者使用 Python synthetic fixture；後者啟動 sandboxed Chromium，
檢查本地合成 UI、frame、靜音與合成音軌，沒有真人 Teams 或 Live 模型。
獨立 Chromium smoke 已觀察到 Join → lobby → 模擬准入 → joined，
且聊天中的 lifecycle 字句不影響結果；這不是真人端到端證據。
此診斷不能回推 #41 的具體根因；真人驗收仍須核對本輪准入時間與客戶端狀態。
