<div align="center">

# mmemory

**面向 AI 协作工作的本地五字段记忆层。**

用 SQLite 保存可复用的经验，用中文友好的 FTS5 找回它们，并让每个项目保持隔离。

[English](README.md) · [简体中文](README.zh-CN.md)

[![CI](https://github.com/CoralFlower325/mmemory/actions/workflows/ci.yml/badge.svg)](https://github.com/CoralFlower325/mmemory/actions/workflows/ci.yml) ![Python](https://img.shields.io/badge/Python-%3E%3D3.10-3776ab) ![SQLite](https://img.shields.io/badge/SQLite-FTS5-003b57) [![MIT](https://img.shields.io/badge/license-MIT-0969da)](LICENSE)

[快速开始](#快速开始) · [召回机制](#召回机制) · [命令行](#命令行) · [隐私边界](#数据与隐私) · [参与贡献](CONTRIBUTING.md)

</div>

## 它解决什么

mmemory 是给跨聊天、编码会话、文档和实验工作使用的小型命令行记忆系统。
一条记忆由五个短字段组成：

| 字段 | 用途 |
| --- | --- |
| `goal` | 要记住的任务或事实 |
| `approach` | 尝试过的做法 |
| `outcome` | 当前结果 |
| `success_key` | 成功的关键条件 |
| `pitfall` | 失败信号与处理方式 |

写入、标签生成和普通召回不调用 LLM，也不启动后台服务。Python 规则与
`jieba` 提取固定标签和关键词，SQLite FTS5 与有边界的正文匹配只在明确的
项目命名空间内寻找候选。

| 保存 | 查找 | 维护 |
| --- | --- | --- |
| 带类型、来源、验证和有效期的五字段记录 | 项目内中文友好 FTS5、别名、标签和字面文本召回 | 修订、保护、衰减、归档、深层召回、硬删除与合并流程 |
| 精确去重与里程碑更新 | 召回解释和有长度上限的上下文块 | 只读复核线索与 SQLite 一致备份 |
| 面向 Obsidian 的 Markdown 导出 | 只有真实使用后才可选强化 | 每个测试都使用合成临时库 |

## 快速开始

要求：

- Python 3.10 或更新版本
- SQLite 编译时启用 FTS5
- `jieba==0.42.1`

```bash
git clone https://github.com/CoralFlower325/mmemory.git
cd mmemory
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python cli.py --help
```

默认数据库是项目目录内的 `memory.db`，请只保存在本机。可通过 `MMEMORY_DB`
切换到其他文件：

```bash
MMEMORY_DB=/absolute/path/to/private-memory.db .venv/bin/python cli.py stats
```

### 保存并召回

```bash
.venv/bin/python cli.py store \
  --goal "mmemory 召回回归" \
  --approach "先比较字面命中，再按标签排序" \
  --outcome "泛化动作词不会再拉入无关记忆" \
  --success-key "候选必须正文真实命中" \
  --pitfall "只看排序会让仅命中标签的候选进入结果；改为先过滤正文命中" \
  --kind experience \
  --verification verified \
  --source-ref docs/recall-notes.md \
  --project-key mmemory

.venv/bin/python cli.py recall "mmemory 召回回归" --explain
.venv/bin/python cli.py list --project-key mmemory
```

`goal`、`approach`、`outcome` 必填。五字段合计上限为 1000 字符，因此完整
操作细节应放在项目文档中，记忆只保留当前事实、关键教训和文档入口。

## 项目命名空间

项目隔离是硬边界。除查询明确解析到多个项目，或调用方有意省略项目范围外，
召回不会自动扩展到其他项目。

mmemory 内置少量中性示例。复制 [`projects.example.json`](projects.example.json)
为 `projects.json` 后，可加入自己的写入和查询别名：

```json
{
  "projects": {
    "my-project": {
      "write_aliases": ["my-project", "项目代号"],
      "query_aliases": ["my-project", "项目主题"]
    }
  }
}
```

`projects.json` 已被 Git 忽略。配置放在仓库外时，可通过
`MMEMORY_PROJECTS_FILE` 指定路径。

领域同义词可复制 [`synonyms.example.json`](synonyms.example.json) 为
`synonyms.json`，或设置 `MMEMORY_SYNONYMS_FILE`。每个数组都是双向别名组。

无法可靠识别的项目应在写入和召回中使用同一个显式 `--project-key`。
`--project-key ""` 表示共享桶。

## 召回机制

召回在解析后的项目范围内合并四层来源：

1. 由 jieba 预分词、FTS5 索引的 `search_text`。
2. 五字段上的字面命中。
3. 根据正文生成的标签关键词。
4. 查询别名、保留 ASCII 词边界和混合标识的查询词。

标签以及“修复、配置、状态”等泛化动作词只影响排序，不能单独把候选拉入
结果。候选必须存在真实的正文命中。

`--explain` 会展示解析项目、必须命中的词、每个 ID 的来源以及排序理由。
默认注入摘要只包含目标、结果和一条教训，包装文案与召回内容合计不超过
1200 字符。

关系会参与召回：`supersedes` 的目标默认隐藏；`--include-superseded` 会恢复
并明确标注旧记录。`--deep` 在当前命中不足时加入归档记录，
`--include-expired` 显示超过有效期的事实。记忆正文始终作为数据，不作为指令。

普通召回不会改变使用次数或权重。只有记忆确实被采用后才使用
`--reinforce`。新结论替换旧事实时，应使用 `update ID`，不要用不同措辞
反复追加同一事实。

## 元数据与历史

记忆类型、来源、验证状态和有效期都会落库：

```bash
.venv/bin/python cli.py store \
  --goal "生产 API 使用 9090 端口" \
  --approach "检查部署配置" \
  --outcome "端口已从 8080 改为 9090" \
  --kind project_fact \
  --verification verified \
  --source-ref deploy/config.yaml \
  --valid-from "2026-11-01" \
  --project-key api-service

.venv/bin/python cli.py update 42 --outcome "端口为 9090" --reason "部署验证"
.venv/bin/python cli.py history 42
```

`kind` 支持 `experience`、`project_fact` 和 `preference`；验证状态支持
`unverified`、`verified` 和 `disputed`。update、归档、恢复、保护和合并会在
`memory_revisions` 中保留快照。

## 命令行

| 命令 | 作用 |
| --- | --- |
| `store` | 写入五字段记忆或校验导入会话 |
| `recall` | 在单个项目内召回，支持解释、深层、有效期、取代关系和显式强化 |
| `update ID` | 修订正文并保留项目和真实使用元数据 |
| `history ID` | 查看当前记录和历史快照 |
| `list` | 查看活跃、归档或全部记忆 |
| `protect` / `unprotect` | 按具体 ID 与理由保护或恢复衰减 |
| `archive` / `restore` | 切换状态并保留原 ID |
| `review` | 只读列出重复、冲突和混合项目线索 |
| `link` / `relations` | 记录和查看 `supersedes` / `derived_from` 证据 |
| `decay` | 预览或应用经过时长的衰减与低权重归档 |
| `dream` | 生成手动合并候选，不自动修改正文 |
| `reindex` | 重建 jieba 检索词和 FTS5 索引 |
| `purge` | 不可恢复地删除正文、关系和修订快照 |
| `backup` | 创建不覆盖已有文件的一致 SQLite 快照 |
| `stats` | 查看数量、类型、验证状态、过期、未使用和取代关系健康度 |

每个命令都可用 `--help` 查看准确参数。

## 维护模型

mmemory 区分三个时间概念：

- `last_used` 表示一次真实使用。
- `decay_cursor_at` 表示权重已经计入的时间边界。
- `archived_at` 表示记录离开活跃集的时间。

重复 dry run 不会重复扣减同一段时长。受保护记录跳过衰减。归档与恢复保留
原 ID，恢复后不追扣归档期间的时长。

旧 schema v1 数据库会在首次打开时于写锁事务中升级到 v2，保留原 ID、补充
元数据和修订表并重建派生索引。外部 SQL 修改正文后使用 `reindex`。

`review` 提供证据，不进行语义裁决。`dream` 始终是手动 dry run。合并写入
在一个事务中完成，拒绝包含受保护来源的组，并记录来源关系，而不是静默
删除历史。敏感正文需要使用单独的不可恢复 `purge` 路径。

维护真实数据库前先生成一致快照：

```bash
.venv/bin/python cli.py backup --output backups/before-maintenance.db
```

备份包含私人记忆正文，已被 Git 忽略。

## Obsidian 导出

将活跃记忆导出为 Markdown：

```bash
.venv/bin/python obsidian_export.py
```

默认目录是 `./obsidian-export`，可用 `MMEMORY_OBSIDIAN_DIR` 覆盖。导出为
单向流程：mmemory 在生成文件中写入 ID，只清理带自身标记的旧文件。

## 数据与隐私

这是给单个可信操作者在自己的电脑上使用的本地工具，不是多用户服务，也不
执行网络同步。

- `memory.db`、SQLite WAL/SHM、备份、导出和本地项目配置不应进入版本控制。
- 测试只创建合成临时数据库，不复制生产正文。
- 公开仓库刻意排除私人运行数据、历史维护记录和本机集成。
- 分享日志或截图前先复核；记忆正文可能包含路径、名称、账号细节和项目上下文。

## 开发

运行完整公开测试：

```bash
.venv/bin/python run_tests.py
```

运行器为每个测试子进程提供临时 `MMEMORY_DB`，检查退出码，并拒绝空转或相互
矛盾的输出。存储、事务、召回和维护契约见 [`AGENTS.md`](AGENTS.md)。

仓库结构：

| 路径 | 职责 |
| --- | --- |
| `engine.py` | 数据库、写入、召回、生命周期与备份 |
| `tagger.py` | 固定标签、检索词、项目别名和可配置同义词 |
| `filter.py` | 字段校验、质量判断与里程碑替换 |
| `dream.py` | 手动合并候选与原子应用 |
| `review.py` | 只读复核线索 |
| `obsidian_export.py` | 确定性 Markdown 导出 |
| `cli.py` | 命令行入口 |
| `schema.sql` | schema v2 表、修订历史、FTS5 索引和触发器 |

贡献应保持公开测试合成、聚焦且可复现。提交 Pull Request 或分享诊断前，
请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE)。
