# WSL2 source transfer

> **原移轉歷史記錄，不是目前操作指示。** 以下描述 manifest 製作時的版本與排除項。後續已切換 standalone Calendar／SQLite／管理頁／summary outbox，退役 service extra、MCP、dot／Space 與會議 Markdown；目前命令及 Docker 契約見 `README.md`。原 manifest／歷史依賴閉包不回寫，此工作目錄不再是 pristine transfer。

## Reviewer 核對

解壓至新的空目錄，先執行 `python3 transfer/verify.py`。
此命令驗證全部 manifest 檔案、bytes、SHA256、額外檔案及 symlink；不執行應用、
不連網、不登入、不碰任何會議。來源修正鏈與每項所改檔案見 manifest。
正常程式、全部合成測試、Dockerfile/Compose 模板、CLI 與技術 schema 保留。
不含 .git、venv、實際 .env、token/OAuth、真實會議 URL/逐字稿/個資、runtime consent、
Codespace 私密 launcher 或操作者輸入。空白 `.env.example` 和 `runtime.example.yml`
是既有模板；tests 的 URL/consent/逐字稿只是人工負面與合成測試向量，不可拿去入會。

## 本機依賴盤點

Python 原始需求為 >=3.11；Cloud 使用 3.12.14，Node 24.19.0，Codex CLI
0.159.0-alpha.3。`pyproject.toml` 保留原約束；`transfer/requirements-cloud.txt`
固定本次已安裝的 Python runtime/測試依賴閉包，只作可重現的參考，不宣稱 Windows/
不同 Python ABI 已驗。原 Docker base digest/Codex version 見 manifest。
apt 套件沒有 lock；browser/system dependencies 需本機盤點，未替使用者安裝。
沒有打包 wheel、瀏覽器 executable、Codex executable 或下載 cache。

若本機任務完成依賴盤點並使用新的隔離 venv，可按既有 pyproject 安裝 service 和
bridge-test extras；這會下載套件，需由本機任務依現有授權執行。本 Cloud 不做安裝。
不要自動執行任何 tools probe、Chrome repair、browser/model launcher。

## 合成 focused tests

在 repo 根目錄及適當的隔離 Python 環境執行：

```bash
PYTHONPATH=tests:. python3 -m unittest test_computer_audio_frame_lifecycle test_computer_audio_observation_errors test_computer_audio_dispatch_guard test_computer_audio_wait test_response_computer_audio_ui -v
```

null-track/sender synthetic 測試需要 Node；真 local-browser fixture 為後續選擇性驗證，
不代表 Teams 或模型成功。其餘測試均保留，缺依賴或 blocked 時應如實報告。
輸出預設靜音、不擅自啟動/重啟 meeting；A 轉錄持久化、B 限本場讀取、dot 更新 Space
的介面沒有更改。manifest 的最新來源是本地候選，review 完成之前不推送或部署。

## 服務與 Compose

原 Docker/Compose 是 NAS 階段模板，保留以便審查；預設外部 tunnel network、appdata、
image 與授權參數都要另行確認，尚無可宣稱已驗的 WSL2 Compose 配置。
缺 MCP issuer/audience/JWKS 與既有合法配置時 fail_closed，不加新 Space 憑證。
WSL2 task 應先完成 loopback/transport、Chromium sandbox、音軌、檔案權限和 stop 清理盤點，
再提出具體本機驗證；本包不自動 bind port、開網路或載入帳號。
