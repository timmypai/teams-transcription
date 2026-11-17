# Triage labels adapter

| 上游角色 | 本 repo 擬用字串 | 意義 |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | 需求仍待評估 |
| `needs-info` | `needs-info` | 缺必要資訊／授權／前提 |
| `ready-for-agent` | `ready-for-agent` | 範圍、public seams 與驗收清楚，可執行已授權工作 |
| `ready-for-human` | `ready-for-human` | 需要操作者／真人介入 |
| `wontfix` | `wontfix` | 經決定不處理 |

這是本地 vocabulary，未查證／建立遠端 labels。真正 triage 前先查既有 labels，重用等價項目；新增／修改／貼 label 需在遠端寫入授權範圍。label 不授權 model／會議／發聲／push／部署，也不替代 staging acceptance。進度使用原生 Projects Status，避免把 `ready-for-agent` 誤當部署允許。
