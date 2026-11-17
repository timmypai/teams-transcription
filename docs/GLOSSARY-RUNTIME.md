# SQLite 詞庫與 A 的會前參考

詞庫是辨識參考資料，不是逐字稿，也不是說話者身分證據。它與來源記錄共用 `.meeting-store.sqlite3`，不需要會議 Markdown。

## 維護與核准

`AssistantStore.save_glossary()` 維護人名 `person`、術語 `term`、縮寫 `abbreviation`。欄位包含 `canonical`、`aliases`、`pronunciation`、`definition`、`scope`、`series_id`、`status`、`revision`。

- `pending`：待核准；不傳給 A。
- `active`：操作者已核准；可加入相關會議的快照。
- `disabled`：停用；保留記錄，不傳給後續會議。
- `scope=global` 適用所有系列；`scope=series` 必須指定系列 ID。
- 更新既有記錄須提供目前 `revision`；過期更新拒絕為 `revision_conflict`。
- `list_glossary()` 回報相同／重疊名稱與別名的衝突。兩個同音姓名不應被建成同一人的別名。

```python
from teams_transcription.assistant_store import AssistantStore

store = AssistantStore('/data')
entry = store.save_glossary({
    'kind': 'term', 'canonical': 'RTO',
    'aliases': ['recovery time objective'],
    'pronunciation': 'ar tee oh',
    'definition': '服務恢復時間目標',
    'status': 'pending',
})
store.set_glossary_status(entry['id'], 'active', actor='admin')
```

此例寫入指定的本機資料庫；不是 Google／Teams／模型驗證指令。不要將真實人名或會議資料放入版控。

## 執行快照

`claim_run()` 在同一交易凍結所有已核准且適用該系列的詞庫。快照包含版本、SHA-256、記錄及覆蓋資訊；系列詞彙優先排列。沒有16KiB聚合裁切或容量省略，仍保留單欄位型別／長度驗證。舊快照的省略標記保持真實，不回填改寫舊revision。

`worker.start_provider()` 將完整凍結快照交給`LiveProvider(glossary_snapshot=...)`。`glossary_context.py`驗證型別、欄位、版本與雜湊，產生有標籤的`user`文字`initialItems`，不再設定聚合bytes門檻。會中修改只影響後續執行；不自動重連模型，不修改已凍結上下文。

來源保存維持 provider 原文。沒有 canonical 名稱替换、依與會者名單猜姓名、或把歷史摘要當成當前發言的路徑。

## 驗證與限制

```bash
PYTHONPATH=tests:. python -m unittest test_glossary_context test_assistant_store test_sqlite_only_store -v
codex app-server generate-json-schema --experimental --out /tmp/codex-protocol-schema
```

已用安裝的 Codex CLI `0.159.0-alpha.3` 匯出 schema，並驗證實際 `LiveProvider.start()` 組成的含詞庫 v3 請求。受控傳輸不啟動遠端模型。

`initialItems` 是會話文字上下文，不是已確認的 ASR hotword API。是否改善人名與術語辨識，仍須用相同音訊、人工基準逐字稿執行「有詞庫／無詞庫」真實轉錄對照；不能用接線測試聲稱準確率提升。

固定音訊、九類驗收表、私有原文及離線 CER 工具見 [A 詞表真音訊對照](QA-GLOSSARY-AUDIO.md)。該執行入口尚未接入主機 QA authority，原使用者詞表亦待確認；不是已完成真實 ASR 驗收。

2026-10-17 已用同一份離線合成中文音訊，分別執行正式原生 A 的有／無詞庫控制；代號、金額、決策與兩項待辦均匹配，收到的原始增量／final 均逐筆完整保存。兩組都通過，**沒有觀察到可據以宣稱的詞庫準確率提升**；這不是實際人名或真人會議驗收。見 `../evidence/system-completion-A-differential-20261017.json`，其中額外 PCM 對照在送音前握手逾時的負結果也保留。
