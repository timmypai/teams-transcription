# Issue tracker: GitHub for teams-transcription

唯一目標是 `paulpai0412/teams-transcription`，base branch `main`。GitHub Issues 保存需求／規格／拆票，PR 保存實作與 code review，Actions 保存執行證據，原生 Projects 保存工作狀態。這份 adapter 使 Matt Pocock 技能可沿用本 repo 的流程；它不代表已開遠端 issue／PR／看板，也不授權新的遠端寫入。當前任務仍只在本地工作分支準備修改。

正式追蹤票已由父任務建立，本任務讀回確認：[#1](https://github.com/paulpai0412/teams-transcription/issues/1) ARM64 image、[#2](https://github.com/paulpai0412/teams-transcription/issues/2) CI gate、[#3](https://github.com/paulpai0412/teams-transcription/issues/3) deploy／rollback、[#4](https://github.com/paulpai0412/teams-transcription/issues/4) diagnostics、[#5](https://github.com/paulpai0412/teams-transcription/issues/5) Oracle runtime、[#6](https://github.com/paulpai0412/teams-transcription/issues/6) 真人音訊。另有本人OAuth與完整前置條件票 [#7](https://github.com/paulpai0412/teams-transcription/issues/7)，本輪已讀回並承接source實作，不執行grant。本工作分支用 `Related #1` 至 `Related #4` 與 `Related #7`，對照與剩餘缺項見 [交付契約](../GITHUB-DELIVERY.md#related-正式追蹤票與待驗收範圍)。六張票讀回時皆 open，本地 commit／未來 PR merge 均不改寫其驗收狀態；不因 skills 的 publish／resolve 指令自行關票。

讀取前核對 Git root／`origin`。使用已連線 GitHub 工具且能力足夠時優先用工具；CLI fallback 的每個操作都明確帶 `--repo paulpai0412/teams-transcription`，不得因 fork、目前目錄或上游模板而把 issue／PR 寫到 `mattpocock/skills` 或其他 repo。CLI 缺登入／權限時回報缺項，不啟動登入或建立 token 繞過限制。

## 階段與完成條件

| 階段 | 技能與輸出 | GitHub 狀態／授權 |
| --- | --- | --- |
| 需求 | 未決才 `grill-with-docs`，已核准部分直接承接 | 不重問已確認規格；不自行開遠端票 |
| 規格 | `to-spec` 整合既有對話、out-of-scope、public seams、驗收證據 | 有相應授權才發布 Spec issue；否則保留本地草稿 |
| 拆票 | `to-tickets` 的可驗證垂直切片、parent／blocked-by／acceptance | 先確認粒度與依賴；已核准 breakdown 不重問；發布各 issue／依賴／labels 需在授權範圍 |
| 實作 | `tdd`／`implement`，一票或一個連貫功能的隔離分支 | 來源修改／本地 commit 不等於 push 或部署授權；初始 CI 僅手動 hosted runner；自動 PR CI 須另行核准費用與觸發政策 |
| 除錯 | `diagnosing-bugs`，能紅能綠的最小重現與回歸 | 只回傳去機密的固定碼／數值／合成證據；不貼原始會議或 provider logs |
| 審查 | `code-review` Standards／Spec 兩軸，記錄 baseline SHA | 修正發現與未決限制；不得把未跑檢查寫成通過 |
| PR | `pr` 整理 problem/result、before/after、測試、部署與恢復風險 | 只有已獲 push／PR 授權才推工作分支與開 **Draft PR**；base main；不直接寫 main、不轉 ready、不 merge |
| Actions | offline subset → native ARM64 image／sandbox smoke → optional GHCR publish | Job Summary／artifacts／source SHA／image digest 作證；手動部署，不因 PR 建立或合併自動部署 |
| staging | Oracle 固定 digest 部署、健康／sandbox／功能驗收 | 記錄實際驗收層級；真實 Teams／GPT Live／遠端音訊仍有獨立授權前提 |
| 完成 | acceptance criteria 有實測證據，且關票在授權範圍 | 發布驗收結論再明確 close；未完成真人／音訊或被阻擋的驗收票保持 open |

PR 與 Issue 使用**普通連結**，例如 `Related issue: https://github.com/paulpai0412/teams-transcription/issues/<number>`。不要在 PR body／commit message 使用 GitHub 的自動關票關鍵字，包含 `Closes #…`、`Fixes #…`、`Resolves #…`，也不要設會提前關驗收票的 linked closing relationship。PR 合併只表示 source 已進 main，**不表示 staging 或音訊驗收通過**。實作票可以在其 acceptance 已完成且有關票授權時結案；綜合驗收／parent 票留到對應證據齊備，不由子票或 PR merge 自動帶關。

## 遠端操作 adapter

「publish to tracker」在此表示提出／準備 GitHub issue；只在已有相應 target-and-action 授權時才執行 create。未授權時產生可審查的本地草稿，不因上游 skill 的 publish／close 指令自動寫入。讀取 issue／PR、比較分支、查 Actions log 等 readonly 工作可主動完成。

對應工具操作：read issue 的 body/comments/labels；read PR 的 diff、reviews、Conversation；create issue／comment／labels／dependencies；create Draft PR。遠端 issue／PR 編號先實際讀取，GitHub 共用編號空間，不猜型別。涉及 assignee／reviewer 也先解析帳號，不猜使用者名稱。

CLI fallback 範例是操作契約，不是本任務已執行的命令：

```bash
gh issue view <number> --repo paulpai0412/teams-transcription --json number,title,body,labels,comments
gh pr view <number> --repo paulpai0412/teams-transcription
gh pr diff <number> --repo paulpai0412/teams-transcription
# 以下只在該動作已獲授權後執行；body-file 先寫好供審查。
gh issue create --repo paulpai0412/teams-transcription --title '<title>' --body-file /tmp/approved-issue-body.txt
gh pr create --repo paulpai0412/teams-transcription --base main --head '<authorized-branch>' --draft --title '<title>' --body-file /tmp/approved-pr-body.txt
```

多行 body 保留真實換行，使用 structured tool argument 或 `--body-file`，不要用不安全 shell interpolation。Issue body 記錄 problem、acceptance、blocked-by、驗收環境與證據連結；PR body 依 `.github/PULL_REQUEST_TEMPLATE.md`，可使用 `pr` 技能的最小 call tree／before-after／Merge Danger 表達，但 repo 安全與權限規則優先。

parent／sub-issue 與 blocking 優先使用已支援的原生關係；工具或帳戶不支援時用 `Part of: <issue URL>`／`Blocked by: <issue URL>` 與 parent task list。不要假設 CLI 版本支援上游最新 flag，不為此自動升級。沒有新增的 Projects／labels 授權時，只提出待配置項目；使用原生 Projects，不造 dashboard。建議看板 `Todo → In progress → In review → Staging validation → Done`；PR merge 最多移至待驗收狀態，不自動把驗收票移 Done／close。

**PRs as a request surface: no.** PR 用來審查實作；外部 PR 不自動當成新需求執行，更不能把其程式送到 VM。來源 GitHub adapter 保存在 `.agents/skills/setup-matt-pocock-skills/issue-tracker-github.md` 供對照；本文件的 repo-specific 邊界優先。`implement-spec` 的 reset／merge／關票／PR-ready、wizard 的 `.env`／secrets 修改均需既有明確授權，技能本身不授權。
