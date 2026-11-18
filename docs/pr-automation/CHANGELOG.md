# PR 流程變更稽核

| 日期 | 決策／變更 | 授權與限制 | 驗證狀態 |
| --- | --- | --- | --- |
| 2026-11-18 | timmypai/teams-transcription 改為 Hermes 非作者 reviewer + GitHub Actions；取代下列 Cloud-only／App ID／額度前提 | 使用者明確決策，僅正式政策／必要手動部署控制 source；不免審、不自審、不 admin merge、不直接部署／grant／新費用 | 最終 exact head、具體原生 GitHub review 及逐條 base 例外交獨立 reviewer；shadow 永不授權合併 |
| 2026-10-31 | 使用者選第三檔：一般與核心修正需完整適用測試及獨立 Codex Cloud review 後交 GitHub 合併；敏感／未知範圍人工 | 本 repo 試行，merge 不部署；不開新 token／grant／權限／成本 | 平台保護及原生 PASS 契約未齊，尚未啟用 |
| 2026-10-31 | 主助理改為需求、協調修正、驗收；Codex Cloud 獨立 review；不另疊逐行手動 review | 使用者允許意見／discussion／rule 自主迭代；保留安全 gate 與必要確認 | 寫入 AGENTS；本實作不以自審冒充完成 |
| 2026-10-31 | 新增只讀 shadow 分級、同 SHA 證據診斷、敏感路徑／測試刪行人工篩選 | 本政策 PR 人工批准；任何結果都禁止 merge；既有五個 workflow 不加事件或部署副作用 | synthetic tests；實際 App／Actions 接線未驗證 |
| 2026-10-31 | workflows 事件驗證從五個固定檔案改六個精確檔名 | 唯一新增為 main-only 手動 read-only shadow；所有實際 workflow 仍只接受 workflow_dispatch | 不放寬自動觸發／費用 gate；inactive CI 草案在 docs |

本表記錄需求與行為；每個實際修改仍以 Git commit／PR diff 作完整稽核。報告的 `policy_sha256` 識別該次可信政策內容，不是簽章或可移植 merge 授權。
