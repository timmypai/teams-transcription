# A 詞表真音訊對照（準備完成，尚未執行模型）

原使用者 `Glossary.md` 的位置、bytes hash、條目及已部署 SQLite 匯入狀態仍未確認。不能用下列合成測試名稱冒充原詞表。PR39／PR40 tree 沒有該原檔；現有 `glossary_context` 是 A 的 `initialItems` 參考，不是已確認的 ASR hotword API，也不會修改 provider 原文。

## 固定材料與驗收表

每一個 clip 先固定私有 24 kHz、mono、PCM16 WAV、WAV SHA-256、PCM SHA-256及人工複核的 reference SHA。兩組必須送同一 WAV；不把 reference、完整測試句或預期答案放入 `initialItems`。有詞組只帶已核准詞表 snapshot，記錄 physical JSON file SHA 與 `snapshot.sha256`（兩者意義不同）、版本及條目 revision。provider／model及部署／CLI版本另綁當次執行證據，不能從本地 checkout 推定 Oracle 版本。

| 類別 | 人工 reference／必要對照 | 記錄與判定 |
| --- | --- | --- |
| `term` | 原詞表的特殊術語 | exact target hit／漏詞／錯詞；保留原文 |
| `abbreviation` | 字母縮寫及其實際讀法 | 不自動改大小寫、展開縮寫來算命中 |
| `mixed_language` | 同句中文、英文或代號 | 逐詞及整句 CER；不事後補 canonical |
| `person` | 已核准人名及人工確認讀音 | 不能把同音名字當同一人，不能猜 speaker |
| `homophone_disambiguated` | 音訊本身有足夠消歧上下文 | `context_reviewed=true` 才給 lexical 分數 |
| `homophone_ambiguous` | 音訊沒有足夠消歧線索 | ambiguity／unknown，不要求選中單一姓名；該 clip CER 不計 |
| `ordinary_negative` | 普通詞與詞表近音，未實際說出術語 | 註記 forbidden replacements，計普通詞誤替換；邊界不確定則 unknown |
| `self_correction` | 同一 clip 明確先說錯再更正 | reference 保留更正過程，不把舊說法刪掉再評分 |
| `silence` | 空 reference、確定性無詞靜默 | 記幻覺字數；目前原生 runner 無 final 時只能 unknown，不能將沒收到事件當 pass |

先做一組可判讀的小型 pilot，而非一次消耗所有額度。每組兩個乾淨、串行的 A session；第二組只在第一組完整 owned cleanup 驗證後啟動。先保留 canonical 兩 slots，不退款未用／失敗／未知 slot，不自動重試。這份 source 不會建立、重置或結算主機帳本。

## 工具及尚缺的執行入口

`tools/glossary_native_pair.py` 的 `run_pair(...)` 提供 trusted constructor DI 和實際 `DefaultLiveAdapter`，重用 `LiveProvider`、原生 WebAudio→owned Live WebRTC input；不呼叫實體麥克風、不入 Teams、不走 Python PCM push，也不接實體輸出。reference 僅交離線 scorer，不傳給 provider factory。原始同 thread RPC final／修訂及允許的事件欄位保留在私有檔；保留空白，不用 `on_final` 已 trim 的文字冒充原文。missing／conflicting final 會是 unverified。

這不是可直接 dispatch 的新 Actions operation：現有 root QA bridge／固定 candidate／projection／authority **尚未接入 glossary pair**。沒有不受帳本控制的 `--live` CLI。實際啟用需將固定 source 納入受限 QA 入口：同全域鎖、canonical 50 次帳本預留兩槽、durable thread/realtime stage claim、unknown interlock、同 attempt 結算及唯白名單摘要 artifact。需另列固定安裝 payload 與 operation 授權，不能因工具存在就認為已啟用；不需新增 API key 或取得原管理密碼。

有詞／無詞 provider 檔、事件 history及模型失敗均保留，不能以失敗重跑後的新檔覆寫。輸出 mode 0600／私有目錄 0700；不得將人名、reference、原詞表、WAV或 provider 原文提交 Git 或上傳一般 artifact。`delivery_verified` 的實際 adapter 依本機 native buffer 播放結束及 RTP counter 增量；**不是模型消費 ACK**。其他 importer 的 delivery／human review／provider-original flags 是操作者 attestation，scorer 不會自行證明其真實性。

## 只讀離線評分

runner 產生私有 `scoring-manifest.json`；也可匯入既有真音訊對照，只要依 `tools/glossary_asr_eval.py` 的 v1 schema 綁所有 bytes hashes、同音訊與同 provider/model。人為勾選 metadata 不是實測證據。

```bash
python tools/glossary_asr_eval.py \
  --manifest /private/glossary-pilot/scoring-manifest.json \
  --output /private/glossary-pilot/report.json
```

報告只含 hash、固定類別、數字、unknown 原因；原檔不變。`nfkc-v1` 僅 Unicode NFKC，回報 Unicode database 版本；保留大小寫、空白、標點、繁簡及同音差異。以原 reference codepoint spans 註記，再作相同正規化。CER 是 lexical edit distance，不是語意判官；edit tie 或詞邊界不確定時不能假裝知道原因。exit 0 只是完成計分，不代表辨識率達標或詞表造成改善；exit 2 代表未知／歧義。普通詞誤替換必須與目標詞命中一起報，不可只選改善案例。

普通詞若沒有完全匹配的外圍文字，不能只因單一 deterministic edit path 剛好對到 forbidden word 就算誤替換。工具沿同一有界 distance matrix 檢查所有最佳路徑：span 內任一字可對到不同輸出位置或刪除，就保持 attribution unknown；該位置有 insertion 也保守 unknown。其他 span 的 tie 不會直接否定本 span 的確定對應。CER 距離不變，這仍不構成語意或因果辨識證明。

人工標註、原檔身份及實測資料未備妥前，本輪單元測試只證明 scorer／串行准入／資料隔離契約，**沒有真實 ASR 分數**。2026-10-17 兩組同音訊五事實均命中的紀錄沒有逐詞／人名分數且另有 PCM handshake failure，不能替代此驗收。
