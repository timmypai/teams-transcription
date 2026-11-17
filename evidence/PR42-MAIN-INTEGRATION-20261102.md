# PR42 與合併後 main 的相容驗證

PR40 已正常合併為 `7f3bbd6849b68665eafe54616ac5ca3eedf9c19d`。PR41 隨後合併的 main 為 `56f73782dfa0c6456badb26a9743cfaaee16787c`；`git merge-base --is-ancestor` 確認它包含 PR40，沒有將另一線覆蓋掉。

PR42 原 head `38e79429` 的 ordinary-word attribution P2 已修為 `ad0fec7c0e918e66a70430eef704c1e87295315c`，該 head 的原生 Codex code review 完成、無 findings。接著以正常 merge 把 main 合入本工作分支，無衝突、沒有 rebase／force push。新增的20個 main 檔案／改動均以 bytes 比較，與該 main 完全一致；保留 PR41 的來源、context、系列查詢與清理契約。

整合後 actual code 測試：

- controlled offline subset：622 tests pass，51.424秒，0 failures/errors/skips。
- postmeeting store／adapter／calendar identity／series question／host contract／admin HTTP：54 tests pass，2.142秒。
- 這次沒有執行 browser UI、Live／ASR、Teams或Oracle，也沒有 GitHub Actions dispatch。0 checks/runs 不能當成CI成功。

測試命令：`python tools/ci_contracts.py OUTPUT`；另以 `PYTHONPATH=tests:. python -m unittest test_postmeeting_store test_postmeeting_adapter test_postmeeting_calendar_identity test_postmeeting_series_question test_postmeeting_host_contract test_postmeeting_ui.PostmeetingUIHTTPTests -v`。

詳見 [integration receipt／hash manifest](pr42-main-integration-20261102/manifest.json)。測試後只新增此證據，production/test bytes不再修改。最終 merge head 仍須原生 Codex 精確重審；PR42 保持 Draft，不合併、不部署。原 Glossary與真正Oracle聽感等既有缺口沒有被這次整合宣稱完成。
