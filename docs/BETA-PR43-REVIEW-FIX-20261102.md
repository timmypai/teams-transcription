# PR43：保留較新的待辦確認時間

原生 Codex 對固定 `4f08a7ed047b8adfc2c1210301261c17b02b58d6` 的審查提出一項 P2：[discussion_r4449060552](https://github.com/paulpai0412/teams-transcription/pull/43#discussion_r4449060552)。原會議訂正稿再次發布時，無條件使用會議日期會讓較新的人工確認時間倒退，並誤觸久未確認提示。

新增公開 SQLite／來源訂正／publication／task workspace 回歸，先重現 11/20 人工確認被改回 11/1 的失敗。修正僅在合併待辦時比較有效時間點，保留既有證據時間與本場來源時間中較新者；不把重新發布時間當成新的確認。人工承接證據及舊系列 revision 不變。另測試跨時區的較晚會議證據能正常推進時間，不能按 ISO 日期字串排序。

本次沒有修改 UI template、音訊、PR41 已合併內容、外部 dispatcher、權限或部署。資料修正由真 SQLite 與讀取投影驗證，沒有重新跑 Chromium；既有截圖仍只代表其各自來源版本。新 head 必須再經原生 Codex 獨立複審，作者測試不代替審查。

216 項相關回歸及既有 549 項離線 subset 通過，0 failures／errors／skips，兩組有重疊不加總。另在修正前，已驗證 main `56f73782` 與原 Beta `4f08a7ed` 的本機合併 tree `6f50ce52`：214 項相關回歸與包含音訊 PR40 的 571 項離線 subset 通過；這是另一個 tree 的驗證，不當作本次新 head 的測試。GitHub Actions 沒有執行，不能把本機通過宣稱為 CI 通過；未呼叫真 Teams 或模型。
