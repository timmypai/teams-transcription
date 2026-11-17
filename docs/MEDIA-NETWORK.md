# 執行主機與媒體出口

## 現行部署邊界

同一獲准主機必須能完成 Google HTTPS、Teams browser／WebRTC、Codex app-server 及官方 Live WebRTC。能開管理頁、HTTPS request 或 WebSocket，不代表 UDP／TURN／DTLS／SRTP 可用；同樣不能從舊 Cloud proxy 失敗推論目前 WSL2 失敗。

歷史 CONNECT／TURN 成對代理實驗不是正式依賴；原移轉未包含 `experiments.ws_turn_bridge`／`experiments.probe_ws_turn_bridge`。不提供假的模組、不啟用新閘道、VPN、外部主機、付費服務或特權容器來繞過前提。

## Egress 與官方媒體

`security.validate_destination` 保護既有 HTTPS／WSS allowlist：精確 host policy、無 private destination，DNS 回答都須為可接受的公網位址。顯式空 allowlist 拒絕全部 HTTP／WS；不能自動改私有配置開放網路。

官方 Live 可返回動態公網 media IP，不要求它與 HTTP endpoint DNS 相同。`RuntimeLiveGuard` 另外核對 SDP audio／data media、SHA-256 fingerprint、候選 IP／port／protocol 與 own source chain：ChatGPT admission → fresh own thread → own v3 start attempt／ACK → 同 RPC queue 的 exact-thread SDP。nonce 為 host attempt，不假稱 provider session ID。能力一次性且綁定 SDP hash，foreign、pre-start、改動／replay 均拒絕。

Browser routing 不是 OS firewall，也不保證之後 DNS／connect 被 pin；不把 allowlist 驗證宣稱為完整媒體出口或遠端音訊證據。既有 ICE host 仍須明確驗證，不輸入任意或新 provision 的 TURN credentials。

## 容器要求

Chromium sandbox 必須真的能運作；非 root、drop capabilities、no-new-privileges、明確 seccomp、私有 `/data` 與 tmpfs 不取消。使用 Playwright 官方 profile 的 user namespace syscalls 是既定容器契約，不用 privileged／unconfined／host IPC／`--no-sandbox` 掩蓋主機限制。
實際發現 Playwright 原 profile 的 `chroot` 規則依賴容器 `CAP_SYS_CHROOT`，與 drop ALL 衝突，導致 Chromium `sys_chroot("/proc/self/fdinfo/")` 失敗。專案 profile 允許這個 syscall，由 kernel 依所在 user namespace 核對能力；不增加容器 capability。修正後同一 container contract 的 sandboxed local page 成功，初始 namespace 的直接 chroot 仍回 EPERM。Chromium 的 namespace/chroot 用途見 [官方 sandbox credentials 實作](https://chromium.googlesource.com/chromium/src/+/lkgr/sandbox/linux/services/credentials.cc)。


`/dev/shm` 為 tmpfs；RPC trace 可含編碼媒體，退出後清除。實際 driver／Chromium／Codex 版本與本機 page／HTTP lifecycle 由 `tools/compose_smoke.py` 在 network=none 的 disposable container 驗證，不能當成 Google／Teams／Live 網路成功。

## 有界真實驗證順序

先用明確新配置執行 `doctor`，檢查本機工具／權限 metadata。之後僅在已授權 target/action 下使用 `probe-media --help`、`probe-live --help` 或 `probe-teams --help` 確認各入口條件，再安排當次有效真人會議。

依序核對 Teams admission、來源音訊非零、Live 接收／保存、provider finality、B scoped delegation／引用、遠端可聽與回音、停止／清理、普通 Codex summary publication／下一次記憶。每層保存固定 phase／錯誤碼及私有 receipt；不公開 URL、SDP、名單、錄音、逐字稿或 token。缺前提停在該層，不把合成結果填成成功。
