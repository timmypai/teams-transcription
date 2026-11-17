# 第一版 Beta：管理網站與可執行待辦

目標日期：2026-11-03（Asia/Taipei）。日期不授權略過驗收、合併、部署、登入／安全變更。此工作由 PR41 已審 head `c37192bd10c3d5eb568813fb1d9c3a6b7b0e9a04` 分出；PR41 保留不再混入 UI 擴充。PR41 原生精確 head 再審無 findings，六個 addressed threads 在獨立確認後 resolved，仍是 draft／未 merge。

## 可交付最小範圍與現有差距

以下表格保留開工基準；已落地的欄位、規則、截圖與真實缺口見 [本輪驗收報告](BETA-UI-AUDIT-20261102.md)。

| 面向 | 現有程式 | 最小處置 |
| --- | --- | --- |
| 最新結論 | 已有系列單一 head、有效 conclusions 與 changes，非所有摘要疊加 | 顯示最近一場日期與最新已整理來源日期；清楚標示新場次尚待整理，主要區只放有效結論／必要變更／待確認 |
| 前場差異 | changes 有 item ID、新值與 previous_status；完整前版 state 在 SQLite | 依 stable ID 比較前一 revision，可呈現文字、責任人或期限的已知差異，不用文字猜配，不重送全部舊摘要 |
| 任務身份 | `id/text/owner/date/status/source_refs/origin_session_id`，新週期 ID 已納入 occurrence | 更新必須帶既有 ID；同未完工作不可重建。owner/date 改變而 id=null 仍是身份模糊風險，應列待確認，不自動配對或建立 |
| 建議／承諾 | open 不證明有人承接；owner 非空也不等於接受 | 加 `commitment_kind=suggested/committed` 與明確 acceptance evidence；舊項缺證據預設待確認，不能稱進行中 |
| 時間 | 只有自由文字 date；state.published_at 不是每項更新時間 | 加 `created_at/last_changed_at/last_evidenced_at` 與可解析 `due_at`；原日期文字保留，無法解析不猜日期 |
| 完成 | 模型目前須引用本場來源，終態不能自動重開；缺獨立人工作業證據欄位 | completion evidence 明確區分人工確認或可信來源及引用；不因未提及／最後期限已過而完成 |
| 需要更新 | 未提及可延續，但無 per-task stale age | 逾期、14 天未有更新、未承接、更新時間／期限不明列「需確認」，只在讀取時計算，不自動新生任務、關閉或催辦 |
| 易懂狀態 | 內部 open/deferred/completed/cancelled/superseded | 主要呈現「待確認／待處理／已完成」；取消／被取代保留歷史，不列有效待辦。不得把待處理稱作正在處理 |

### 最小後端方案（先明列，與 UI 微調分開）

- 在衍生 state 的 task item 增加 commitment、時間與 evidence 欄位，不改原始逐字稿。未提供 acceptance 的既有 open task 一律視為待確認。模型只能提供引用與候選分類；host 驗證引文存在，人工確認與模型推測分開。
- 如加入人工承接／完成操作，沿現有登入、Origin、CSRF，綁定 stable task ID＋expected series revision，保存獨立 human-task-action evidence／actor／時間及冪等 request ID。這是人工作業紀錄，不能偽裝 Codex 產物或本場發言；completion 必須明確確認與證據／說明。
- owner、期限或內容更新沿舊 ID；同文字卻無舊 ID、或來源對應不清時拒絕自動新增，交由待確認。新的 recurring commitment 必須有不同 occurrence 的明確新一輪證據，已完成舊項不自動重開。
- `last_changed_at` 只在實際欄位／狀態變更時更新；未提及延續不刷新。`last_evidenced_at` 只在新明確來源確認時更新。不得用每場發布時間把沉睡任務假裝剛更新。
- 需確認提醒是本機投影，沒有新 timer、第三方催辦、聯絡人同步、權限或外部費用。新增資料格式須獨立測試／審閱，不暗改已審 PR41 head。

## UI 驗證清單

- [x] 本輪實際截圖並逐張檢視；視覺判斷只用本輪 evidence。
- [x] 桌面 1440×1000／手機 390×844：登入、週曆、選場、逐字稿、摘要／待辦、系列歷史。
- [x] 鍵盤 Tab／Enter／Escape、focus 可見、主要控制可達、錯誤可恢復。
- [x] 主操作、資訊密度、欄位分組、標籤與手機 reflow 已檢查；沒有宣稱全面無障礙／文字縮放認證。
- [x] 未連 Google 的真實本機錯誤狀態；無來源／尚未開始與外部整理 pending 空狀態。
- [x] 最新有效結論與待整理新場不混淆；舊歷史需展開；來源可追。
- [x] 任務不把無 owner／無承接當進行，不自動完成或重開；逾期／久未更新只提示。
- [x] 低影響修正前後同樣場景回歸；不改音訊、lobby、Oracle 或憑證。

## 證據邊界與必要阻礙

本輪已依 Product Design audit/index/user-context 執行 preflight（無 saved context），讀取 audit framework。共享 critical-overrides reference 在 cloud skill resource 中無法取得；Browser skill／cloud Browser tool 也未提供，因此使用本 repo 已有 Playwright sandboxed Chromium seam。不是另建產品或部署替代站。

主機 Chromium helper ownership 不正確，未 chmod 或關 sandbox。依既有 Dockerfile 與固定 SHA512 npm artifacts／既有宣告 Python wheels 成功建置私有本機測試 image。容器 nonroot、readonly、drop ALL、no-new-privileges、原 seccomp、tmpfs、network=none、無 ports／OAuth／真資料掛載。原 compose smoke 在 Browser 的全域 proxy 讓 loopback 導向不可用 proxy 時失敗；audit harness 僅明列 localhost proxy bypass，保留其餘 proxy，且 route 限同源、kernel network=none，未擴大任何外部 egress。

baseline image source 為 `ac03fe96094118a5b820807954edc01e368998ec`，UI／admin／calendar-view 檔案與已審 `c37192bd...` 完全相同。audit 資料明列「合成驗收／非真會議」，使用真管理 server／SQLite／DOM 與本輪截圖；不得把 seed／mock-summary 叫作真 Google、Codex 或 Teams E2E。Google dot 身份／安全 OAuth 配置與真實外部 completion dispatcher 仍未建立，真整場 Beta runtime 交付需要這些前置，不得以日期目標代替。
