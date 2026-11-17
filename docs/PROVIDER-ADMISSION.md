# Codex／Live admission

## 正式路徑

`AssistantDaemon._admission()` 在真人 capture 前使用 `CodexProviderState`，透過固定 app-server 的 readonly `account/read(refreshToken=false)` 查同一部署既有 ChatGPT 帳號。不直接讀 token、不登入、不建立 API key、不啟動模型查額度，也不以 model/list 或 planType 推論 Live entitlement。

成功需 account.type=chatgpt、相同 settings，且固定 `codex_live / gpt-live-1-codex / no_new_cost`。狀態僅 RAM、TTL 300 秒；失效需重新查核。API-key 帳號、過期成功、readonly failure、未知 cleanup 一律不通過。daemon 正式 runner 不接受 fixture config；constructor 注入 runner 僅供受控測試。

`admissible_unverified` 只代表可沿既有獲准路徑嘗試，`model_access=checked_on_start`。真正模型可用性需實際 start／推理，不能承諾免費或已取得模型權限。[Codex app-server account 整合說明](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)。

Calendar 錄音／發聲 policy、admin session、ChatGPT admission、Live source-chain／media guard 是不同 gate。Google OAuth 或受邀參加會議不能替代模型、錄音或輸出授權。

## 有界探測與資源

initialize 30 秒、account/read 15 秒、cleanup 8 秒分開計算，不共用過短的單一 timeout。任何階段失敗都清理部分建立的 RPC；重複取消不重置 cleanup deadline。只有帳號與清理都成功才更新 RAM admission。

RPC close 共用 owned cleanup task，TERM 等待 3 秒、必要時 KILL 再等 2 秒、reader 最多再等 1 秒；實際未回收不能標為 reaped。app-server 的媒體／TRACE runtime 要確認在 `/dev/shm` tmpfs，退出後清除；沒有磁碟 fallback。既有 Codex home／OAuth 不改寫或複製。

`auth_probe` 只含固定 phase、status、error class 與 elapsed，保存 initialize／account-read 首因，cleanup 另列。不含帳號 response、email、ID、token、URL、stderr、任意例外本文或 runtime 路徑。管理 `/api/status` 要登入；`doctor` 只看檔案 metadata 與工具版本，不做 account RPC。

## 驗證

```bash
PYTHONPATH=tests:. python -m unittest test_provider_state test_provider_probe test_runtime_guard test_assistant_daemon -v
```

受控 JSON child 驗證 slow initialize／account budgets、部分初始化、取消、TERM→KILL、首因／隱私、TTL、wrong account 與 clean-only admission。這不是既有 OAuth、GPT Live、Teams 或媒體出口的新實測成功。歷史 Cloud 代理／只讀錯誤不能直接當作 WSL2 現況。
