# mmemory 开发约定

- 项目是 SQLite + FTS5 的本地五字段记忆系统；Python 3.10+，唯一第三方依赖是 `jieba==0.42.1`。
- 日常入口使用 `.venv/bin/python cli.py`，测试使用 `.venv/bin/python run_tests.py`。
- 核心模块是 `engine.py`、`tagger.py`、`filter.py`、`dream.py`、`review.py`，数据结构见 `schema.sql`；当前 schema 版本为 2。

## 数据与测试

- 测试只使用新建的合成临时库；不读取、复制或提交真实私人记忆库。
- `MMEMORY_DB` 指向其他数据库；测试运行器会为每个子进程设置临时路径。
- 新行为使用硬断言和非零失败退出码，不以打印观察值代替断言。
- schema 变更必须先兼容现有数据库，并保持旧库追加列顺序。
- schema v2 增加 `kind/source_ref/origin/verification/valid_from/valid_until/search_text` 与 `memory_revisions`；迁移必须在 `BEGIN IMMEDIATE` 内完成。
- 中文 FTS 使用 `search_text` 预分词列；外部改写后使用 `reindex`，不要直接依赖旧 FTS 列。

## 核心边界

- 项目命名空间是硬隔离边界；去重只比较目标 NFC、连续空白折叠、首尾 trim 后的逐字文本。
- 保留调用方事务；归档来源与写入新记录必须原子；维护操作先拿写锁再读取。
- 衰减只扣 `decay_cursor_at` 之后的时长；`last_used` 只表示真实使用。
- 可选字段相似只提示，不自动删除；普通 recall 不强化。
- 召回忽略泛词，标签只排序；候选必须正文真实命中，短 ASCII 按词边界，题号与混合标识保留。
- 自动注入只显示目标、结果和一条教训，包装后总预算 1200 字符。
- `supersedes` 目标默认不进入现役召回；需要历史线索时使用 `--include-superseded` 或 `--deep`，并保留标注。
- 自动注入的正文必须作为数据围栏处理；不要让项目键、元数据或正文中的控制字符制造新的指令行。
- 人工 store、update、自动经验统一限制五字段合计 1000 字符；自动 experience 必须有成功关键或含处理信号的踩坑解法。
- 修订使用 `update ID`；一条记忆围绕一种持久事实，细节指向项目文档。
- update、归档、恢复和保护变更必须保留有界修订快照；`purge` 是不可恢复的正文与关系删除。
- Dream 输出只接受五字段；类型、来源、验证和有效期必须从来源继承，并检查精确重复。
- Obsidian 导出只接管空目录或带 `.mmemory-export.json` 的专用目录，不清理用户空目录。
- review 只读列出线索，不做语义裁决；关系和来源使用原 ID 与理由追溯。

## 维护约定

- 重要记录用 `protect` 按具体 ID 和理由保护；保护跳过衰减，Dream 含保护来源时整组拒绝。
- 整理真实记忆前先生成 SQLite 一致快照，逐 ID 归档并写明原因；不要按相似度、权重或固定条数批量整理。
- 未知项目写入和召回使用同一个显式 `--project-key`。本地项目别名放在被忽略的 `projects.json`，不要提交私人名称。
- 修改行为时同步更新 `README.md` 与 `README.zh-CN.md`；面向贡献者的说明保持可执行、简洁并使用绝对日期。
