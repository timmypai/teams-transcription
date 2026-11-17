# 管理頁的 Google／ChatGPT 授權

本次範圍：依使用者「在 web 管理網頁增加此 2 個登入授權機制」，在固定基底 `294c0f4` 增加安全的授權引導。程式、文件與離線驗證不代表部署或真實帳號驗收；已獲准推送原專案工作分支，PR 建立仍待母線取得批准。本次不建立／匯入憑證、不發出真實 OAuth grant、不改 Oracle／Google／ChatGPT 安全設定、不 merge／部署。

## 操作

登入管理頁後開啟「設定」。兩種帳號授權均與管理密碼、Teams 登入、錄音及發聲同意分開。

- Google：沿用 `POST /google/connect` → 官方同意頁 → `/google/callback`，只要求 Calendar events readonly、offline access。未配置 client 時顯示 `google_client_config_file` 的要求與精確回呼網址，無效按鈕不顯示。部署管理者須另行準備私有 Web application client JSON，將相同 URI 登記於 Google Cloud，非 loopback 使用 HTTPS／安全 cookie。網頁不收 client secret、不建立 Google 專案或 client。拒絕授權返回設定頁，既有憑證不變更；錯誤／重播 state 拒絕，token exchange 只做一次。
- ChatGPT：開啟 `/chatgpt`，先暫停排程、結束會議並完成清理確認，再由本人勾選同意並開始。已有 `auth.json` 另需明確勾選重新授權。以固定版 Codex app-server 的 `account/login/start`、`type=chatgptDeviceCode` 取得官方網址與短效代碼；使用者自行在 OpenAI 網站登入、MFA、輸入代碼。本人帳戶／workspace 必須允許裝置授權；程式不代改該設定。

ChatGPT 必須有明確 `codex_home`，位於**同部署**的持久可寫掛載，目錄 0700／auth.json 0600、執行 UID/GID 正確，並通過既有 `auth-preflight`。網頁不建立 home、不搬移或讀取 token、不接受密碼／token 輸入。缺設定時顯示部署需求；不以其他環境的憑證或 API key 補足。

代碼只在發起者的有效管理 session 顯示。重新整理頁面查看結果；關閉分頁不會把登入程序留給 request 管理，應用生命週期負責收尾。取消、登出、session 過期、10 分鐘等待上限或服務停止會取消並收回子程序。過程中保留登入檔鎖、阻擋新的入會；未知清理則繼續阻擋。作業不自動重試、不啟用排程、不開啟發聲。完成只表示 native 登入完成且私有授權檔存在；帳號准入、正常 refresh、Live RX/TX、真人 Teams 音訊仍須另行驗證。

## 保護與驗證邊界

所有啟動／取消使用已登入 session、同源 Origin／Referer 與 CSRF。Google state 綁定 session、5 分鐘過期且單次使用。管理頁保持 no-store、框架拒絕與現有 CSP；代碼不放在網址、cookie、localStorage、一般狀態 API 或 audit。native 使用 stdio，不開 loopback callback listener／TCP port；stderr 丟棄，RPC 僅保留固定錯誤碼與必要完成欄位，TRACE／SQLite/logs 限既有 verified tmpfs 並於子程序收尾刪除。token 由 native 直接保存至同一私有 home。

部署前仍須確認反向代理／APM 不保存授權頁 response body 或 `/google/callback` query；應用的 uvicorn access log 原已停用。本次沒有修改實際 proxy、容器 logging、安全設定或 credentials。私人終端 `auth-login` 原有 TTY／明確確認／daemon 停止等限制保留；新增的是獨立 Web device 流程，並非讓原終端登入在 CI 執行。

測試公開邊界為已授權的管理 HTTP 流程、native stdio adapter 與 daemon 入會排他。provider fixtures 都是合成資料；不得稱為真實 Google／ChatGPT 授權通過。部署／真帳號測試另需當次授權。

## 官方依據

- [Codex app-server：managed account login／cancel／completed](https://developers.openai.com/codex/app-server)。本次另以已安裝 `codex-cli 0.159.0-alpha.3` 的 `generate-json-schema` 離線核對 `chatgptDeviceCode`、`verificationUrl`、`userCode`、`loginId` 與 completion schema，未啟動 login。
- [Google OAuth Web server flow：redirect URI／state／offline access](https://developers.google.com/identity/protocols/oauth2/web-server)。

獨立 review 須依 `AGENTS.md`／`docs/PR-AUTOMATION.md` 取得原生完成證據；本地測試或一般子代理意見不能代替該 gate。本次不寫遠端 PR／review，不宣稱 gate 通過。

## 本次驗證

97 項 HTTP／native transport／daemon／Calendar／SQLite 相關回歸通過。另在固定本機測試映像 `sha256:c397b661c65a3c689e43b92b1a3656bfd6fa6163182d7f0e117803fdc53928d0` 執行真 Chromium 156.0.8078.73／Python 3.12.15／Noto Sans CJK TC，共 3 項 browser tests 通過，0 failure／error／skip，9.935 秒。執行 UID10001、network=none、readonly、drop ALL、no-new-privileges、repo 原 seccomp、tmpfs；無 ports、runtime mounts 或正式憑證。保留其餘 proxy，僅對 loopback 加入既有 audit 使用的 bypass；沒有修改 host helper 或關閉 sandbox。

實際操作涵蓋管理登入／Origin、CSRF 拒絕、未勾本人同意、跨 session 不見代碼且無法取消、關分頁後繼續、手機取消、去敏拒絕、登出取消，以及 Google 成功／拒絕／重播。Google 原生表單 POST 與 server 303 的官方 URL／state／scope／offline 參數先核對，再以離線同源合成同意頁替換該 303；token exchange 只走 MockTransport。這不是 Google 網站登入、真帳號或跨站 provider E2E。

桌機登入／設定與手機取消截圖已逐張查看；中文正常，手機取消頁無文件層水平溢出。截圖不含已填密碼、等待中的代碼或 token。容器收尾確認不存在，`cleanup_outcome=cleaned`。主機原 helper 錯誤、proxy 與合成測試修正過程均保留於 [browser 證據](../evidence/web-authorization-browser-20261103/result.json)／[各輪結果](../evidence/web-authorization-browser-20261103/attempts.json)。新測試映像不含 production source，也不是 Oracle 部署候選；建置 recipe／宣告依賴隨證據保存，Debian／轉接依賴版本不宣稱為 portable lock。

重跑：準備已建好的同規格固定映像，再執行 `python tools/run_web_authorization_browser.py --image sha256:<image-id> --output /tmp/<new-evidence-directory>`。launcher 只傳指定 source／合成測試，核對 manifest，不 pull／安裝或掛任何資料；每輪唯一容器名稱、有界清理並驗證不存在，unknown 會回傳非零。原生 review 與真帳號／部署驗收仍待各自批准。
