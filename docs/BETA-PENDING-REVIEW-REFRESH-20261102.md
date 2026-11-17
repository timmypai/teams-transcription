# a683fe4：連續未解決訂正的最新依據

母線複核指出：S1 承接被 C1 撤回後，`acceptance_evidence` 已移除；C2 再訂正時，舊 helper 提早返回，`acceptance_review` 停在 C1。已在原 `a683fe4c379595787fcd0882a35d8cd76a2bd436` 真 SQLite 與 ASGI HTTP 重現，非僅函式 probe。

本次只修改 `task_workflow.py` 的後端 metadata 行為，沒有修改 UI template、B 音訊、PR41、權限、外部 dispatcher 或部署。

- 待確認的 `source_corrected` review 即使沒有 acceptance evidence，仍追蹤原相關 session／source 的後續快照。
- 每次相關新快照更新目前 SHA、snapshot revision、active correction IDs 及相關 replacement sources；host 以同一交易推進系列 revision，讓 HTTP／projection／B 的下一次讀取拿到同一份最新依據。
- `initial_invalidation` 固定保留第一次撤回的快照及引文，原 `invalidated_source_ref/evidence` 不改；既有 series revision 與來源快照仍保留歷史。只存首次撤回與目前依據，不累積所有中間版本到預設回答。
- 相同／較舊快照不回退；重送 C1 仍驗證最新來源，保持 state／revision 不變。不重新承接、不完成、不重開、不建立任務或通知。無關 session 與獨立人工確認保留。
- metadata 修補的 ledger ID 帶 series revision，允許修補舊版已部分寫入的同一 source snapshot，避免舊 key 碰撞；無變更的 replay 不寫入。

驗證包括：C1→C2→C3 都未重新承接；舊 C1 replay；舊 revision 稽核不變；closed 仍 closed；無關 session 不動；HTTP 只呈現 C2 最新訂正引文及連結；Calendar projection 與 B metadata 均與最新 review 相同。另用 a683 公開 API 建出 Alice 停 C1、Bob 已到 C2 的舊 SQLite，再由新版 replay 修補為兩者皆 C2，第二次 replay 完全不變，沒有任意 SQL 匯入結果。

214 項相關回歸及 549 項既有離線 subset 通過（兩組有重疊，不加總）。本次未另跑 Chromium；新的實際 ASGI／SQLite／B projection 測試覆蓋此次資料更新，既有截圖只作歷史證據。沒有真 Google／Codex／Teams／Oracle E2E，原身分、dispatcher 及正式候選驗證 blocker 不變。修正仍待母線獨立複核。

結果與源碼 hash：[evidence/beta-pending-review-refresh-20261102](../evidence/beta-pending-review-refresh-20261102/verification-summary.json)。
