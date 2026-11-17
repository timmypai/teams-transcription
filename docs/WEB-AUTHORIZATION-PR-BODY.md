## Problem and result

管理頁的 Google 連接在缺少 OAuth client 時無法提供設定指引，ChatGPT 則只能顯示授權狀態。此變更新增 Google client／精確回呼網址提示、成功與拒絕結果，並以官方 Codex app-server device flow 提供 ChatGPT 登入、重新授權、取消及狀態頁。

ChatGPT 開始／取消需已登入 session、同源與 CSRF；本人明確同意、既有 grant 重新確認、同部署私有持久 home、排程暫停與會議閒置檢查保留。代碼限發起者短期記憶體顯示；完成、取消、登出及過期清除。授權期間保留入會互斥，未知清理阻擋後續操作，排程不會因授權互斥而遺失場次。Google 仍只要求 Calendar events readonly，state 綁定 session 且單次使用。

規格與操作邊界：[Web 授權](https://github.com/paulpai0412/teams-transcription/blob/feat/web-account-authorization-20261103/docs/WEB-AUTHORIZATION.md)。

## Validation

- [x] 97 項 HTTP、native stdio transport、daemon、Calendar、SQLite 相關回歸通過，0 failures／errors。
- [x] 固定本機測試映像內 3 項真 sandboxed Chromium 測試通過，0 failures／errors／skips，9.935 秒。驗證兩種授權的合成瀏覽器流程、管理登入／Origin、CSRF、跨 session 隔離、手機取消、拒絕去敏、登出與關分頁後生命週期。Google 原生 303 先核對再替換為離線合成同意頁；非真 Google 網站登入或帳號驗收。
- [x] 使用 network=none、UID10001、readonly、drop ALL、no-new-privileges、repo 原 seccomp、tmpfs，無 ports／runtime mounts；容器清理確認 `cleaned`。未關 sandbox、修改 host helper 或清除其他工作負載。
- [x] 未包含 runtime 資料、真實 OAuth、session secrets、錄音或真實會議 URL；截圖不含已填密碼、等待中的代碼或 token。真實 OAuth、模型與 Teams 音訊均未執行。

證據：`evidence/web-authorization-20261103.json`、`evidence/web-authorization-browser-20261103/`。兩路 advisory review 的修正已複核；依 repo 政策，仍需另外取得固定 head 的獨立原生 review 完成證據。

## Deployment and recovery

這是 source／測試交付，尚未 merge 或部署。瀏覽器測試映像 `sha256:c397b661c65a3c689e43b92b1a3656bfd6fa6163182d7f0e117803fdc53928d0` 僅有測試依賴，source 由 stdin 載入 tmpfs，並非正式候選映像。沒有 SQLite schema 變更。

Google Web application client、同部署可寫私有 `codex_home`、真帳號授權與後續帳號／Live RX/TX 驗證仍需由操作者在獲准環境完成。原生 review、合併與部署各依既有 gate；建立此 PR 不授權部署、修改安全設定或建立憑證。
