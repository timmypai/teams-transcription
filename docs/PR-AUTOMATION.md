# Hermes 獨立審查與 GitHub Actions 交付政策

## 本 target 的有效決策

2026-11-18 使用者決定：「為何要用codex cloud做審核, 我只要用hermes及github action來做,跟cloud codex無關」。本政策僅適用 `timmypai/teams-transcription`，正式取代 2026-10-31 的 Cloud-only reviewer／App ID／額度前提。舊上游 `paulpai0412`、歷史 evidence 與原 CHANGELOG 保留 provenance，不能拿來擋本 target，也不能當本 target 已部署或已批准的證據。這次修正本身由非作者 Hermes reviewer 審查，不能自我批准。

- devops 是原 PR #1 的唯一 source/publisher/Actions CI owner；Hermes `reviewer` profile 是獨立 reviewer。實作者不得同次自審、偽造 Cloud review 或借 reviewer 憑證。
- reviewer 必須親讀**完整 diff（含政策與部署程式）及最終 exact head**、需求、實際測試／Actions artifacts，提交具體 APPROVED 或 CHANGES_REQUESTED，不以無 findings、CI 綠或提交審查當批准。
- reviewer 以自己既有 GitHub 登入提交原生 PR review，重新讀回 `user.login`、完整 `commit_id`、`html_url`、`state` 與具體審查結論；Kanban review transition metadata 保存這些值及 full head SHA。GitHub 真缺權限時只回報該 reviewer 的具體失敗操作與修法，不要求 Cloud grant／額度。author 不得自審。
- push 新 head 使舊 review／CI 失效；未解 findings／threads、缺 review、未知／截斷／逾時、stale head、作者自審、退件／撤銷均 fail closed。後續 APPROVED 必須解決前次 findings；新 commit 需重審。
- reviewer 逐條核對九個情境的 27 條 Given/When/Then 與 base 證據。六條 Base-unverifiable 須確認 base 確無 target adapter；三條 Already-met 須確認真實 base browser 執行。豁免不是作者說了就有效，需在審查結論逐條判斷；不得製造環境紅燈或拿 hosted fixture 當 Oracle PASS。

## 測試與證據

`ci.yml` 為非特權 `pull_request`／手動整合，僅 GitHub-hosted、readonly token、不讀真 secrets、不去 Oracle、不 publish／Join／model／grant；不得 `pull_request_target` 執行 PR code。`test` 四 shards（Python3.12/unittest/Node/sandboxed Chromium）、`check`、`verify`、`verify-base` 由 `devflow-ci` 聚合。只排除原移轉缺 source 的兩個 bridge suites；可用回歸不等於原始 full discovery，不新增 skip／刪測試／降 gate。

審查與 SHIP 必須向 GitHub 讀回 repository、workflow path/id、run id/attempt、event、完整 head SHA、結論／job，下載同 attempt artifacts 對照內容；artifact／check 名稱與作者 JSON 本身不是信任來源。base 例外需獨立判斷。branch protection `contract=no` 不代表已強制 required checks，也不授權 admin merge／升級方案。merge 僅由既有 SHIP 以 devflow 通道執行；本政策不新增 auto-merge controller。

## Readonly shadow 工具

- `policy/pr-policy.json` 固定本 target、Hermes independent GitHub adapter、敏感路徑與 `devflow-ci` 診斷；不要求 Cloud numeric bot/App ID。
- `tools/pr_policy.py` 只作候選分級、原生 review metadata 投影與 blocker 診斷。**任何輸入皆 merge_allowed=false、CLI exit1**；scope／tests provenance 未由此工具證實。arbitrary JSON、label、approval marker 不能授權。
- `tools/pr_policy_github.py` 只讀固定 GitHub API；收齊 files/checks/reviews/threads 分頁，前後核對 PR 身分，移除 patch／review prose／error bodies，僅保留 review 身分、link、SHA、state、submitted_at、body_present。每頁4MiB／30頁、每request最多20秒、90秒後不發新request；不跟外部 Link、不轉送 token、無 retry／換憑證。
- `pr-policy-shadow.yml` 僅 main 手動／`ENABLE_PR_POLICY_SHADOW=true`，hosted、3分鐘、readonly contents/pull-requests/checks，只 checkout trusted workflow commit，不下載／執行 PR source 或 artifacts。永遠 blocking 診斷，不可用結果自動合併。`docs/pr-automation/pr-ci.yml.example` 仍是歷史非啟用草案；有效 CI 是 `ci.yml`。

## 敏感邊界與手動部署

帳號、credentials、安全／存取權、網路、部署、新費用、policy／workflow、刪測試或降低驗收都需本次 target-and-action 授權及獨立 scope 審查；檔名 allowlist 不等於語義安全。本次僅授權 PR #1 必要政策、手動部署適配及固定控制鏈 source，不授權直接部署、公開網域、Cloud grant／費用或改 GPT Live OAuth／A/B 音訊。

merge **不部署**。新 `deploy.yml` 與 `control.yml` 僅 `workflow_dispatch`、固定本 target／main 最新且經獨立審查的 commit，self-hosted devflow 通道；不借歷史 staging helper／GHCR／ledger／OAuth。部署後仍需真實 health／smoke／歸屬／操作稽核／安全入口與登入；控制鏈安裝後須無 Join 實測。真 Join／媒體同意／本人 OAuth/MFA 分階段由指定卡處理。見 [GitHub delivery](GITHUB-DELIVERY.md)。

不新增 paid provider fallback／自動合併／新帳號／daemon。GitHub Actions 使用受本任務授權的既有通道；不宣稱額外費用必為零，不購買方案或擴權。
