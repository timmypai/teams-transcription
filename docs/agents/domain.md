# Domain sources for repo skills

本 repo 是單一 Python 會議助理，不是上游 skills monorepo。工作詞彙與安全契約以根 `AGENTS.md` 的 Architecture／Data Flow、`README.md`、`docs/HANDOFF.md` 及相應 runtime docs 為準；未有根 `GLOSSARY.md`／ADR 時，不為技能 setup 另行搬移或改名既有文件。

Calendar occurrence、series、session/run、A application segment／provider final、B question／native output、execution lease／fence、summary outbox、cleanup interlock 是不同領域概念。測試／issue／PR 延續這些用語，不以 health／build 成功代替音訊 E2E，不將 application boundary 稱 provider final。接觸新區域時讀其既有 docs；若之後新增 ADR，技能審查要納入該 ADR。
