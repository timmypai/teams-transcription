# GitHub PR 分級與 Codex Cloud review 試行

狀態：**僅 shadow / blocking 實作，未啟用自動 review、PR CI 或合併**。固定起點為 `main@c9d54b7f72be1c94318386ce7dab7a61df4396e3`。這個 PR 改政策／工作流，本身列人工批准；merge 不部署。原 PR22 的 ARM build、Oracle、模型／hosted 帳本不由本工作修改。

## 已選第三檔與工作分工

| 變更 | 最終目標 | 本版行為 |
| --- | --- | --- |
| 一般修正、文件與新增回歸測試 | 必要測試與獨立 Codex Cloud review 明確完成後，由 GitHub 依批准規則合併 | 僅候選分級，阻擋合併 |
| 核心程式／音訊修正 | 同上，另需完整適用回歸、sandboxed browser／media 測試及需求驗收證據 | 核心候選需 `pr-contracts` 與 `pr-full-regression`；名稱只是待驗證契約，沒有合格來源證明仍阻擋 |
| 帳號、credentials、安全／存取權限、網路、部署、新費用、政策／workflow 自身、降低驗收門檻 | 明確人工批准 | 人工，無作者 label override |
| 無法分類、證據未知／截斷／逾時 | fail closed | 阻擋 |

Codex Cloud 是獨立 code reviewer；主助理負責需求、協調修正、執行驗收及改進流程，不再增加一輪逐行手動 code review。寫碼者不得是同次獨立 review 執行者。使用者已允許本 repo 的改進意見、discussion、rule 自主迭代；每次修改以 Git diff／commit／PR 與政策 hash 留稽核。這不授權取消既有 gate、擴權、改費用、變更 OAuth／token 或越過平台必要確認。正常新增回歸測試可跟修正走；刪測試、既有測試有刪行、改 acceptance、review policy 本身保守列人工。若要放寬此篩選，也屬政策變更。

路徑 allowlist 只能篩選候選；同一音訊或文件檔也能新增權限、網路、成本或偷偷改驗收。`semantic_scope_unverified` 永久阻擋到獨立 reviewer 的完整語義分類可以驗證。不能只靠檔名宣告安全。

## 交付與執行邊界

- `policy/pr-policy.json`：固定 repo、候選規則、必要 check 名稱及未驗證 App 身分。與腳本一律由可信 main 讀取。
- `tools/pr_policy.py`：純 stdlib 判定／CLI，輸出最新完整 head／base SHA、政策 SHA256、候選分級與固定 blocker codes。任何輸入皆 `merge_allowed=false`，CLI exit 1；沒有 merge／auto-merge／approval override 程式或 enforce 開關。
- `tools/pr_policy_github.py`：只讀固定 GitHub API，收齊 changed files、全部 check runs／reviews 與 review threads 分頁，前後重讀 PR head／base／狀態。每頁最多 4 MiB、30 頁／集合、每 request 使用最多 20 秒的 socket timeout、90 秒後不再開始新 request（不是整段下載的硬時限；workflow 另限 3 分鐘）；超限、403、redirect、GraphQL errors、file count 不符皆阻擋。headers 裏的外部 Link 不會被跟隨，也不轉送 token。沒有 retry／換憑證。
- `.github/workflows/pr-policy-shadow.yml`：僅 main 的手動 dispatch，`ENABLE_PR_POLICY_SHADOW=true` 才能跑；hosted x64、3 分鐘 timeout、唯讀 `contents/pull-requests/checks`。只 checkout `github.workflow_sha`，不抓 PR source／artifacts、不跑 PR 程式。只留去除 patch、review body、API error body 的小型 JSON／summary，7 天保存。沒有 merge 權限、模型或部署呼叫。工作必定紅燈；變數造成 skip 也不是通過。
- [PR CI 非啟用草案](pr-automation/pr-ci.yml.example)：檔案刻意置於 workflows 目錄外。非特權 `pull_request`、hosted runner、唯讀 token、不帶 secrets、不用 `pull_request_target` 或 Oracle runner。完整 discovery 不替缺的 prerequisites 製造 skips；尚不是可啟用的合格 full test profile。

本分支不會從 PR payload／作者 label／留言命令注入可信政策。不下載或執行 PR artifacts；不將 PR 送入 self-hosted runner。重新 push 或 base 更新即需重新收集／重判；前後 SHA 一致只證明觀測期間 PR metadata 未改，不是跨 API 的原子證明。未來任何有寫入權的 controller 還必須重新核實 base、所有證據與 head，以 SHA compare-and-swap 合併；不能直接使用這份 shadow JSON。

目前 `required_check_not_successful` 只檢查觀測到的同 SHA 唯一成功 check；中立、skip、pending、舊 SHA、重複名稱皆不算成功。它**沒有**證明 check 的 workflow／job／attempt／App／測試內容來源，故另有不可取消的 `required_test_provenance_unverified`。任何綠燈、偽冒名稱或 artifact 均不授權 merge。

## 官方 review 接線與尚缺契約

[官方 GitHub Codex](https://learn.chatgpt.com/docs/third-party/github) 支援 `@codex review`、Automatic review 與 `AGENTS.md` review rules；目前文件描述僅標 P0／P1，沒有承諾可機讀的完整 diff coverage、PASS 或 GitHub `APPROVED`。眼睛反應、沒有留言、CI 綠燈或作者提供的 JSON 都不是 review pass。[官方 review rules](https://developers.openai.com/blog/custom-code-review-rules-for-codex) 也不替代 tests／protection／required approvals。

本版尚未驗證 App installation ID／bot user ID，不能憑 login 或 `type=Bot` 信任結果。一般 `APPROVED` 也未實作為 Codex 原生完成證據。`native_review_contract_unverified`、`codex_identity_unverified` 持續阻擋；任一未解 thread 保守阻擋，包含舊 head 的 thread。comments／reactions 沒有當作 success adapter。`CHANGES_REQUESTED`、pending、dismissed、作者自審、錯誤 SHA 都另列 blocker。

啟用安全自動合併前必須用另一位獨立 Cloud review 執行者及專用合成 PR 實際驗證：

1. 固定 GitHub App／installation／bot numeric identity，確認 repo grant 範圍；不新增 grant 或擴權來猜測。
2. 官方可驗證的明確完成事件、完整 head SHA、獨立執行者、完整 diff coverage 與明確結論；若原生產品未提供，保持 shadow，不自造 PASS schema。
3. 完整適用 tests／驗收 profile、可信 App／workflow／run／job／attempt 來源，最新 head／base，沒有 unresolved P0／P1、pending／dismissed review、未知結果或截斷。
4. 從可信 main 取得政策，敏感範圍的批准由 owner 身分與該 SHA 綁定；作者 label 不能替代批准。新增 commit 失效舊批准。
5. 可靠的平台合併保護與最後一刻 SHA／base／checks fence。這份程式不補作未批准的 write-token merger，不繞 protection 的 403。

上述正例／反例驗證未完成之前，不能把「可以開啟 Automatic review」描述成「可安全自動合併」。目前沒有向 PR 發送 review 指令或自行重做 code review。

## 費用與啟用 blocker

| 項目 | 運算上限／試行估算 | 額度／費用現況 |
| --- | --- | --- |
| shadow 手動報告 | 每次一個 hosted Linux x64 job，最多 3 分鐘；若 owner 日後批准 20 次，保守至多 60 runner 分鐘 | 未 dispatch；included minutes／remaining／spending limit 未核實。20 次是待批准提案，並非已實作月硬限 |
| 未啟用 PR CI 草案 | 每 head 10＋20＝最多 30 runner 分鐘；20 個 head 至多 600 分鐘；同 PR 新 run 取消舊 run | 自動事件尚未安裝；push 次數沒有總月上限，不可視作零費用或無限批准 |
| Codex native review | 手動與自動 review 均使用帳號 review 額度 | [官方價格文件](https://learn.chatgpt.com/docs/pricing) 描述 Plus／Pro cloud review；實際方案、剩餘額度與額外用量上限未知，不能猜。API-key-only 不涵蓋此路徑，不新增 API key |
| artifacts | shadow 小型 metadata JSON，7 天；草案尚無 artifacts | storage 可用額度與實際帳費未核實 |

新增實際美元費用目前**無法估計**，因帳戶可用額度與計價條件未知；不是 $0 保證。本輪未 dispatch Actions、未呼叫模型。啟用任何自動事件前，owner 必須先提供 billing usage／spending controls 與可批准的數值額度，並選擇能強制執行的停止策略；沒有月硬限的自動 CI 不算成本受控。不得為此升級方案、公開私人 repo、加新付費服務或 secret。

已知限制（由父任務提供，沒有重試或繞道）：

| 操作／目標 | 結果／原因 | 現況 |
| --- | --- | --- |
| rulesets，私人個人 repo | HTTP 403：Upgrade to GitHub Pro or make repository public | denied；不得升級或改公開 |
| main branch protection | HTTP 403：Resource not accessible by integration | denied；已停止，不換憑證繞道 |
| Actions permissions endpoint | HTTP 400：connector not allowed | unknown；不從 admin metadata 推定權限 |
| native Automatic review／App scopes／完成契約 | 產品支援 review；帳戶設定及機讀 PASS 尚未驗證 | pending；沒有啟用成功證據 |
| auto merge | `allow_auto_merge=false`；平台 protection 能力不足 | pending／blocked；沒有更改設定或合併此政策 PR |

## owner 最小網頁步驟（待相應 scope／費用批准）

1. 在 ChatGPT／Codex 的 GitHub code review 設定確認既有連接只包含此 repo，記錄實際 plan／remaining review usage／停止上限。若須 Connect、新 OAuth grant 或擴權，停下另行批准。
2. 先保留 Automatic review 關閉；核實準備好的獨立 reviewer 身分、費用，對固定合成 PR／完整 SHA 做一輪有界 review，保存實際完成／不通過／逾時／新 push 的訊號。只有使用者明確批准該次 review 的耗額度行為後執行。驗證原生輸出是否能滿足上述契約；不能就停在 shadow。
3. 人工審查並批准本政策 PR。這是必要的政策本身 gate，不可讓它自我批准。若日後批准報告 job 的範圍：只允許 main，新增唯讀 `contents/pull-requests/checks`，`ENABLE_PR_POLICY_SHADOW=true`，每次需 owner 手動 dispatch（最多 3 分鐘／run），輸出永遠不合併。先核實原已遇 403 的設定，不能假設有權寫。
4. 自動 PR CI、Auto review recurring 消耗與任何寫入型合併 controller 各自需具體新 scope／成本及實際驗證。現在 private 個人方案的 rulesets／protection 已被拒絕；若無平台可用且已批准的安全 enforcement，維持人工 merge，不用替代 token／假 required check 繞過。
5. 只有以上能力、SHA 契約、測試／驗收及預算都驗證，才提請精確啟用變更。合併後 build／publish／deploy 仍沿用各自手動 gate，不能加入 push-to-main 部署。

這是完成實作後的精確前提清單，不是要求使用者重新選方案。機制尚未啟用；下一步由 owner 驗證產品與帳號支援，主助理持續協調修正與需求驗收。
