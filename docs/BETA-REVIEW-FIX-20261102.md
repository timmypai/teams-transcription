# Beta 4805d5a 獨立審查缺漏修正

母線針對 `4805d5ae65b0a5301fc183f4c46569aceddfac17` 提供一項 P1、兩項 P2。本次沿 `feat/beta-meeting-workspace` 修正，不改 PR41 已審 `c37192bd`，不另開 PR、發通知、合併、部署或操作 Oracle。以下是作者的重現／回歸證據，修正版仍待母線再次獨立查核。

本報告記錄 a683fe4 的修正。後續複核發現連續未解決訂正的 review 仍可能停在 C1，另見 [最新依據刷新修正](BETA-PENDING-REVIEW-REFRESH-20261102.md)；不把 a683 的驗證當作後續版本驗證。

## P1：來源訂正後繼承失效承接

在 detached `4805d5a` worktree 執行新公開 SQLite 回歸，已重現同 task／text／owner、`acceptance_evidence=null` 或整項 task 省略時仍保留 committed。原本 `source_evidence()` 能拒絕明傳的 superseded quote，但繼承 metadata 未經同樣驗證。

- 每次 merge 先依 session＋source ID／active correction chain 重驗所有繼承的 meeting-source 承接，含省略的任務。失效證據轉為 suggested／待確認，保留失效引用、目前替代來源文字、SHA、來源版本與快照 revision；不能只留一個無法追查的布林值。
- 寫入 correction snapshot 的同一交易便撤銷受影響證據，新增明確 `source_correction` 衍生 state revision，不等外部 Codex。這不是模型摘要／完成 receipt，也不新增通知或執行器。
- 舊場次晚補稿只改受影響的承接 metadata，保留最近會議／摘要身份、其他 session 以及同 session 其他來源的有效證據。closed task 狀態不自動重開。
- 獨立人工核實是另一個 authority：同一工作內容與責任人下，原會議被訂正不會撤銷人工承接，也不讓後續模型 quote 覆蓋人工確認。人工 evidence 記錄 basis series revision 與 source refs；更換責任人／內容仍需重新承接。
- 舊 correction request 重送保留原 snapshot ID，但驗證目前 metadata 使用最新來源快照，不能用舊 C1 去否定後續 C2。精確重送不新增 revision／任務／通知。
- Calendar task projection 與 B 每題 snapshot 均帶修正後承接狀態及替代來源。B 的既有 source／series fence 保留；沒有宣稱真模型口頭回答已驗證。

回歸涵蓋 S1→C1 撤回、delta 省略、C2 重新有效承接→C3 再撤回、重送 C1、同／跨 session 無關來源保留、晚到舊場次修正、人工優先、closed 不重開、UI 與 B metadata。UI 可直接查看訂正引文及其來源版本。

## P2：系列與表單跨 revision 讀取

原版實際 ASGI 路由已重現：在 `series_state()` 回傳與下一次 task projection 之間完成任務，heading 留在舊版，open／closed article 都沒有該任務；表單則能出現新責任人卻帶舊 CAS revision。

新增 `task_workspace_snapshot()`，在單一 store lock 讀取 series heading、完整 state、open／closed projection、legacy tasks 與操作歷史。兩個 handler 使用此同一快照，表單 expected_revision 亦從該 state 取得，不再各自 await 重讀。

回歸在舊讀取邊界、以及新完整 snapshot 交出後立即推進 revision：舊快照會完整顯示舊版；下一次 GET 才完整顯示新版，不消失／重複，不出現新欄位配舊 CAS。表單已開啟後發生訂正再提交，仍會得到明確 409、保留輸入，要求操作者自行重載。

## P2：合法多行說明被拒絕

在原版真 ASGI app 提交 LF 完成說明得到 400（預期 303）。沒有修改通用 `assistant_store.text()`。

- 專用 note validator 將 CRLF 規範成 LF，允許 LF；拒絕其餘 Unicode Cc（含 tab、NUL、單獨 CR、DEL／C1），規範化後上限 2000 字元，空白說明另有 required 錯誤。
- CRLF／LF 精確重送使用同一 normalized request digest，仍是冪等操作。
- required、超長、控制字元與類型錯誤各有明確 code／提示，不再誤報多行說明為空白。錯誤頁保留合法輸入，無效控制字元以替代符號標示，避免原樣輸出控制碼。
- note 保存原始文字語意，HTML escaped；歷史以 pre-wrap 呈現換行。HTTP accept／update／complete／cancel 均驗證多行、錯誤恢復、HTML escaping、重送與長度邊界。

## 驗證與範圍

`evidence/beta-review-fix-20261102/` 保存去敏重現摘要、source manifest、結果與本輪截圖。舊版失敗輸出含暫存測試 CSRF，因此只存測試名稱及結果摘要，不上傳完整 HTML traceback。

R01／R02 是修正中的並發表單與手機撤回畫面；R03–R07 涵蓋訂正來源、獨立人工承接、控制字元提示、多行完成與系列 closed state。一次 `.task-action-note` selector 選中兩段歷史造成工具 strict-mode 失敗，改選最後一段後重驗通過；不是產品執行錯誤，紀錄保留。最後源碼的額外重跑與精確 hashes 見 verification JSON。

最終源碼 manifest 為 `e7cd7fecf9aa0467c4e0b55a686e3fc3d672d96b3686d1eae1f6f5c374a04a30`。F1／F2 與 `fixed-source-ui-interactions.jsonl` 使用此版本：34 操作、9 斷言、0 錯誤／JS error／文件層水平溢出，重新走過訂正、舊表單 409、獨立人工承接、再次訂正與多行完成。wheel 內全部 73 個 package 檔案與此 manifest 相同。舊版／修正中／最終版本的證據分開保留，沒有把先前截圖冒充最後版本。

定向＋既有相關回歸 212 項通過，既有離線 subset 549 項通過，兩組有重疊，不加總。仍有既有 asyncio timing 訊息。瀏覽器為本輪實際 sandboxed Chromium／真 admin server／SQLite／合成資料，沒有 Google、Teams、Codex 模型或 Oracle 真 E2E。沒有重新啟用舊 summary worker／timer。原本 dot Google 身份、安全授權、外部自動 dispatcher 契約與候選映像／正式環境驗證等 blocker 不變。
