# PR41 原生審查修正閉環

- PR：[draft #41](https://github.com/paulpai0412/teams-transcription/pull/41)，未 merge／deploy。
- 固定已審 head：`c37192bd10c3d5eb568813fb1d9c3a6b7b0e9a04`。
- [原生再審請求](https://github.com/paulpai0412/teams-transcription/pull/41#issuecomment-6581462356) 明列本輪六項 findings。
- [獨立原生收據](https://github.com/paulpai0412/teams-transcription/pull/41#issuecomment-6581530842) 對應同 head，回覆未發現重大問題；[原生狀態](https://github.com/paulpai0412/teams-transcription/pull/41#issuecomment-6580132496) 為 completed、has_any_findings=false、has_unresolved_findings=false。
- 六個 threads 在這份獨立結果之後才由作者 resolve，非只憑自測；[閉環說明](https://github.com/paulpai0412/teams-transcription/pull/41#issuecomment-6581682967)。

原兩項 P1：

1. 取消／heartbeat 失敗吞掉 settled cleanup-unknown，將工作錯放回 ready。修復保留 primary error，以獨立 host lifecycle + authenticated attempt ledger 持久保存 unknown cleanup；即使租約過期／被 takeover，也只能記錄安全風險，不恢復舊 writer 的發布權。root admission／heartbeat／publication 遵守 interlock，新補稿不能繞過。新增 cancellation、heartbeat、真正 expiry、takeover、偽造／已清理 attempt 等回歸。
2. 每次補稿只以首份 snapshot 為基底，後次補稿遺失先前補稿。修復累積 immutable correction history，保存 explicit supersedes／active correction IDs 與獨立 request identity。分次 A→B→A2 修訂保留全部來源、正確取代關係與舊 request 冪等，不重寫原文。

後續 review 還補齊 generation 自己的 auth/turn/timeout + cleanup 雙重失敗主因保留，與後一 occurrence 明確新週期工作的新 ID（已完成舊項保持終態，未完延續需既有 ID）。

最終本機相關 195 tests 與既有離線 549 tests 通過；後者為既有 subset，無真 model/Teams 呼叫。wheel 建置通過。既有 timing/task warnings 與 browser 前置限制均有保留，不稱全量真實 E2E。GitHub status／workflow run 均為零，未 dispatch 新 CI。

以上不是 GitHub APPROVED／自動 merge gate 的產品契約，不是 Google 身份、外部 dot trigger、Oracle、Teams、真模型整理或 UI 真 Google E2E 驗收。Beta UI 的後續程式／audit 以獨立分支進行，保留上述 PR head。
