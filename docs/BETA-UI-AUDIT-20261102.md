# Beta 管理網站驗收與待辦落地

本輪在 `feat/beta-meeting-workspace`，由 PR41 已獨立審查的 `c37192bd10c3d5eb568813fb1d9c3a6b7b0e9a04` 分出。PR41 不再改動。這份報告是源碼、本機實際 DOM 與合成 SQLite 資料的驗收，不是 Google、Codex 模型、Teams 或 Oracle 真 E2E，也不是 Beta 的獨立原生 code review。

本報告與其 source hash 記錄 `4805d5a` 交付時的驗收。母線後續提出的一項 P1／兩項 P2 及修正證據，另見 [獨立審查缺漏修正](BETA-REVIEW-FIX-20261102.md)，不以原測試結果代替修正版的再查核。

## 實際操作與修正

先用本輪有效 Noto 字型截圖看登入、桌面週曆、手機預覽／展開，再依使用者路徑操作。環境為 Chromium sandbox、UID10001、readonly、drop ALL、no-new-privileges、repo 原 seccomp、network=none、tmpfs，沒有 ports、OAuth 或正式資料掛載。瀏覽器只准同源 loopback；其餘 proxy／CA 設定保留。

| 發現 | 處置 | 本輪證據 |
| --- | --- | --- |
| 未連 Google 卻寫「行事曆已同步」 | 改成「已讀取已保存的會議」；保留真實未授權／連線異常提示 | 02 → 14 |
| 最新結論缺所屬會議日期，新場 pending 容易誤認已整理 | 顯示結論來源日期、最近已封存場次日期與尚待整理說明 | 14、15 |
| 變更只重複新文字，期限變化不清楚 | 用 stable ID 比前一 revision，顯示已知內容／責任人／期限／狀態前後值 | 14 的 10/30 → 11/6；19 人工責任人變更 |
| 無責任人、未承接的建議也叫待完成，且沒有操作入口 | 分「AI 建議／待確認」「明確承諾」，新增確認承接／更新表單，主狀態只有待確認、待處理、已完成等；沒有「進行中」推定 | 16–19 |
| 人工完成缺可操作證據流程 | 沿既有登入＋Origin＋CSRF；明確核實 checkbox、說明、stable ID、revision CAS、request ID；未勾選保留輸入並提示 | 17、18；HTTP／SQLite tests |
| 手機展開後 Escape 無作用 | Escape 返回行事曆並恢復查看會議按鈕焦點；Tab／Enter 提交、分頁方向鍵皆實測 | phase1 失敗後修正；phase2 重新驗證，15 可見 3px 焦點框 |
| 系列歷史預設全展開、技術 JSON 佔據歷史首頁 | 系列／舊脈絡收合；保留清理阻擋提示，僅把來源品質與執行 JSON 收在 details | 15、最終來源歷史截圖 |

沒有做 WCAG 全面合規、螢幕閱讀器或所有瀏覽器認證。最終回歸的桌面 1440×1000、手機 390×844 沒有文件層水平溢出；phase1 曾在來源頁換頁／樣式載入中量到 424px，最後手機來源頁重新驗證為 390px。七日週曆本來就是可水平捲動的局部區塊，窄螢幕用「查看會議」進閱讀區。來源／摘要分頁、歷史逐字稿、舊場與最新系列狀態分開，均由實際頁面操作。

## 實際資料與規則

| 欄位／操作 | 已實作行為 |
| --- | --- |
| 事項 ID | 沿舊 ID 更新，不因責任人、期限或再次提及重建；同文字未完工作用 id=null 會被拒絕；已結束 ID 不能重開 |
| `commitment_kind` | suggested／committed；缺明確承接的舊項預設 suggested，填責任人本身不等於承接 |
| `acceptance_evidence` | 會議來源須引用本場存在且逐字相符的 quote，含明確責任人；或經既有管理登入的人工核實，保存 actor、note、time、request_id；不是偽造模型 receipt |
| `completion_evidence` | 完成／取消／取代須來源引文或明確人工核實；沒有引文的模型終態提議留在未完清單，顯示需確認，不能直接完成 |
| `last_operator_evidence`／`task_actions` | 人工更新也有獨立、可查的依據；系列變更提供「查看人工確認依據」，不把舊逐字稿假稱為人工更新的證據 |
| owner／date | 未再次說明不清除已知值；模型留空沿用舊值。人工可明確修改／清空，沿同一 ID；換責任人或工作內容會撤銷先前承接，需要重新確認 |
| `created_at`／`last_changed_at`／`last_evidenced_at` | 分開保存；模型明確提及按來源所屬會議時間記確認，省略延續不刷新；人工操作用實際確認時間，不用每場發佈時間洗掉沉睡狀態 |
| `due_at` | 僅解析明確 YYYY-MM-DD，保留原 date 文字；無法解析顯示需確認，不猜相對日期 |
| 提醒 | 未指定／未承接、逾期、期限格式或確認時間不明、久未確認顯示待確認；`task_stale_days` 預設14、可設0–365，0停用久未更新提醒；不自動關閉／承接／新生事項或催辦第三方 |
| 人工 revision | 在同一 `series_state_revisions` 交易新增人工版本、保留原本來源與摘要身份；CAS 防舊頁覆新，精確重送 request_id 冪等，不產生新的會後完成通知 |
| B 每題快照 | 既有系列 revision fence／hash 不變；新增承諾與人工證據 metadata，無承接不能說成有人正在做 |
| 既有舊待辦 | 來源整理前只標為待核實的歷史記憶，不再用舊下拉選單無依據完成。首次有效整理延續其識別後可用新操作流程 |

引文存在性、來源版本與狀態轉移由 host 驗證；內容是否構成承諾仍是模型的語意判讀，這次沒有真 Codex 執行，不能宣稱語意品質已完成真人驗收。

## 證據索引與重跑

`evidence/ui-audit-20261102/` 皆為本輪生成與逐張查看。01–04 是有效 baseline；05–13 是修正過程；14–25 是最後源碼的完整回歸。00 缺 CJK 字型，被拒絕。08／09 在頁面尚未穩定時只截到 viewport，不作完整表單驗收；10、16／17 是完整表單。phase1 保留真正發現的 Escape 失敗與舊 harness 的 navigation 讀取 race；後者不冒充 app 錯誤。phase2 的 30 個功能斷言均通過，仍保留兩筆 Enter 已執行、後續 DOM 觀測遇到換頁的 race；harness 只對這個明確錯誤重讀畫面，不重送操作，並用 phase3 重跑受影響的鍵盤登入與完成流程：14 操作、4 斷言、0 錯誤；兩次 Enter 各只重讀畫面一次，沒有重送操作。

201 項相關（排除 fixture 繼承造成的重複收集） SQLite／HTTP／adapter／B 快照回歸通過；既有 `ci_contracts.py` 549 項離線 subset 也通過，0 fail／error／skip；有既有 asyncio timing 訊息，非真音訊／模型驗收。最終 wheel 逐一核對 package 內全部程式／靜態檔 hash 與 browser source manifest 相同。詳細結果與 screenshot hashes 見同目錄 verification-summary／source-manifest JSON。

非阻擋的後續 UX 改善：Calendar 設定仍沿既有平鋪欄位，桌面密度可再改善；本輪優先完成日常會議閱讀與待辦操作，沒有把整站重新設計或所有設定功能宣稱全面驗收。

最後源碼在容器 tmpfs 載入後核對整個 `teams_transcription` 檔案 manifest：`b451d8ff13475e9a1e7c50254b43aeb274d13a7431c5187a4ef12ca8b1878d15`。測試用的既有 sandboxed Chromium／Noto 映像為 `sha256:d923c0ed6762413ebda9440cba934432c51a231d32c83e53b67f2a58716c57a7`；這不是包含最終源碼的正式候選映像。

```bash
python tools/run_postmeeting_ui_audit.py --image sha256:d923c0ed6762413ebda9440cba934432c51a231d32c83e53b67f2a58716c57a7
```

launcher 只傳公開程式及標為合成的 driver，核對雜湊、使用暫存 SQLite／測試密碼；不抽取真 token、不連外部會議、不裝新工具。stdin 接受 JSON op（fill／select／click／key／check／viewport／shot／goto／stop），結果與截圖在容器 `/tmp/audit-shots`。結束前可用定名 container 的 `docker exec ... cat` 取回，不依賴 tmpfs 的 docker cp archive。stop 後容器及合成資料移除。

本次 Docker 的 vfs backing disk 曾被重建快取耗盡。只按已讀取且全部由本次 15:37 起建立的精確 cache ID 清理；沒有動正式資料或其他 writer。正式候選映像未完成重建／staging，沒有以此略過驗證。正式 Dockerfile、音訊路徑、lobby／政策與 Oracle 均未改動。

## 尚缺的真實交付與批准邊界

- dot 專用 Google test identity／安全授權仍 pending。現有 connector 不是 dot，Oracle OAuth 設定存在性檢查也未就緒；沒有代用家庭 Calendar、建立新 OAuth 或建真事件。真定期同步、改期 original start 穩定 ID、取消不加入、T-5、人工 lobby 與手機桌面完整實場仍未驗。
- SQLite ready intent 與受限 Codex one-shot adapter 已有，**沒有既有 dot host 的自動 completion dispatcher／回寫入口契約**。封存後自動觸發、真模型 receipt、恢復與完成通知的真閉環仍 pending，未造 timer／新 provider 或把人工命令稱自動交接。
- Beta 新 head 尚未做獨立原生 review；依母線要求此時不另開 PR 或通知協作者。PR41 的 clean review 不能套到本分支新增變更。
- 正式候選映像、Oracle staging／真人音訊、真會議整場驗收與 release 仍 pending；沒有 merge、deploy 或 VM 更動。

交由母線一次整合必要批准：固定 Beta head 的新 PR／原生 review（會通知協作者）、dot test 身份的安全授權與真測資源、外部 host 的實際執行／回寫權限契約、以及通過所有 gate 後固定候選的合併與 staging／部署。日期目標不代替這些批准或真驗證。
