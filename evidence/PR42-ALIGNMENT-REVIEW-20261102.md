# PR42 原生審查的普通詞歧義修正

原 head `38e79429b505440a208bd78b1bbaa4fd0ab9225c` 的 [官方 P2](https://github.com/paulpai0412/teams-transcription/pull/42#discussion_r4448987085)：reference `甲普普`、hypothesis `星普`、普通詞 span `[1,2)`，單一 deterministic path 把 `星` 算為確定誤替換；另一等成本路徑卻可保留這個 `普`，因此歸屬不確定。

公開 `evaluate_manifest` seam 新回歸先 RED：誤替換計數 1，預期 0。修正沿既有有界 distance matrix 反向追蹤所有最佳邊；用兩列 reachability，對每個 reference 位置標記不同 hypothesis 位置／刪除或 insertion 的保守歧義。沒有列舉所有 paths，沿用 4,000,000-cell 上限，額外記憶體 O(m+n)。普通詞缺 exact outer context 且 span 有歧義時為 unknown，不能計 definite false substitution；不修改 CER、normalization 或原文。

另一對照 `甲普乙丙丙`→`甲星乙丙`：tie 在其他位置，不影響本 span 的確定誤替換，因此仍計 1。原長詞 exact outer context 反例及跨類別測試保持通過，不因修復把所有普通詞一概當 unknown。

- RED：2 tests，1 fail；原碼精確重現官方反例。
- GREEN：evaluator／pair／glossary 39 tests pass；evaluator 共18項。
- 最終 controlled offline aggregate：622 tests pass／52.270秒，0 failures/errors/skips。不是全部 browser／原始 discovery 或新 ARM／Oracle 驗證。
- 獨立 Standards reviewer：18項 evaluator 通過，無 confirmed findings。
- 獨立 Spec reviewer：18項通過；另以961組小型字串對獨立完整 DAG 遍歷核對，無 confirmed findings。這項不是原生 Codex 複審，最終 head 仍須另請其核實。

收據／原始 RED 及 SHA 見 [manifest](pr42-review-20261102/manifest.json)。原620及其他失敗紀錄不改寫。此修正沒有發起 ASR／Live、Teams入會、Oracle或安全設定變更，也沒有啟用尚缺的 root QA glossary operation。PR42 保持 Draft、不得由這份紀錄視為已核准合併。
