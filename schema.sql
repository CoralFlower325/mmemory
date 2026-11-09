-- mmemory: lightweight memory system schema
-- SQLite with FTS5

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA user_version=2;

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
    protection_reason TEXT DEFAULT '',

    -- v2 元数据。全部追加在旧列之后，兼容旧库迁移。
    revision      INTEGER NOT NULL DEFAULT 1,
    kind          TEXT NOT NULL DEFAULT 'experience',
    source_ref    TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL DEFAULT '',
    verification  TEXT NOT NULL DEFAULT 'unverified',
    valid_from    TEXT NOT NULL DEFAULT '',
    valid_until   TEXT NOT NULL DEFAULT '',
    -- 由 jieba 与规范化文本生成，供 FTS5 使用。
    search_text   TEXT NOT NULL DEFAULT ''
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

-- 修订快照：每次 update 或维护性改写前保存旧正文。
CREATE TABLE IF NOT EXISTS memory_revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id     INTEGER NOT NULL REFERENCES memories(id),
    revision      INTEGER NOT NULL,
    snapshot      TEXT NOT NULL,
    reason        TEXT NOT NULL,
    created_at    TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(memory_id, revision)
);

-- 预分词全文检索；中文正文由 jieba 写入 search_text。
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    search_text,
    content='memories',
    content_rowid='id'
);

-- FTS 同步触发器
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, search_text)
    VALUES (new.id, new.search_text);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, search_text)
    VALUES ('delete', old.id, old.search_text);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, search_text)
    VALUES ('delete', old.id, old.search_text);
    INSERT INTO memories_fts(rowid, search_text)
    VALUES (new.id, new.search_text);
END;

CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_mem_project_status ON memories(project_key, status);
CREATE INDEX IF NOT EXISTS idx_mem_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_mem_valid_until ON memories(valid_until);
CREATE INDEX IF NOT EXISTS idx_revision_memory ON memory_revisions(memory_id, revision);
