-- mmemory: lightweight memory system schema
-- SQLite with FTS5

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 主记忆表
-- project_key 和 decay_cursor_at 按旧库 ALTER TABLE 的追加顺序排列。
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    goal          TEXT NOT NULL,
    approach      TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    success_key   TEXT DEFAULT '',
    pitfall       TEXT DEFAULT '',

    -- Tag 固定维度
    tags_tool     TEXT DEFAULT 'general',
    tags_domain   TEXT DEFAULT 'general',
    tags_action   TEXT DEFAULT 'general',
    -- Tag 动态关键词（逗号分隔）
    tags_keywords TEXT DEFAULT '',

    -- 权重与衰减
    weight        REAL DEFAULT 1.0,
    use_count     INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now', 'localtime')),
    last_used     TEXT DEFAULT (datetime('now', 'localtime')),

    -- 状态
    status        TEXT DEFAULT 'active',  -- active 参与日常召回；archived 保留历史、可恢复
    -- 逻辑项目命名空间。空字符串代表未识别项目（共享桶）。
    project_key   TEXT NOT NULL DEFAULT '',
    -- 上次已计入 weight 的时间，与真实使用时间 last_used 分开。
    decay_cursor_at TEXT DEFAULT (datetime('now', 'localtime')),
    archived_at   TEXT,
    archive_reason TEXT DEFAULT '',
    -- 显式保护避免自动维护；人工指定 ID 的归档仍然允许。
    protected     INTEGER NOT NULL DEFAULT 0,
    protection_reason TEXT DEFAULT ''
);

-- 显式引用：source supersedes/derived_from target；记录关系不改变记忆状态。
CREATE TABLE IF NOT EXISTS memory_relations (
    source_id     INTEGER NOT NULL REFERENCES memories(id),
    target_id     INTEGER NOT NULL REFERENCES memories(id),
    relation      TEXT NOT NULL CHECK (relation IN ('supersedes', 'derived_from')),
    reason        TEXT NOT NULL,
    created_at    TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (source_id, target_id, relation)
);

-- 全文检索（兜底召回）
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    goal, approach, outcome, success_key, pitfall, tags_keywords,
    content='memories',
    content_rowid='id'
);

-- FTS 同步触发器
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, goal, approach, outcome, success_key, pitfall, tags_keywords)
    VALUES (new.id, new.goal, new.approach, new.outcome, new.success_key, new.pitfall, new.tags_keywords);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, goal, approach, outcome, success_key, pitfall, tags_keywords)
    VALUES ('delete', old.id, old.goal, old.approach, old.outcome, old.success_key, old.pitfall, old.tags_keywords);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, goal, approach, outcome, success_key, pitfall, tags_keywords)
    VALUES ('delete', old.id, old.goal, old.approach, old.outcome, old.success_key, old.pitfall, old.tags_keywords);
    INSERT INTO memories_fts(rowid, goal, approach, outcome, success_key, pitfall, tags_keywords)
    VALUES (new.id, new.goal, new.approach, new.outcome, new.success_key, new.pitfall, new.tags_keywords);
END;

-- 查询索引：唯一的过滤维度是 status 与 project_key。工作集仅几十行，这两个
-- 索引收益有限但成本极低，保留以与线上库结构一致（线上库存在这两个索引）。
CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_mem_project_status ON memories(project_key, status);
