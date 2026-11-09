"""
记忆引擎：SQLite CRUD + 多级召回 + 权重衰减
"""

import sqlite3
import math
import threading
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from tagger import (
    generate_tags,
    extract_tags_from_message,
    infer_project_key,
    infer_query_project_keys,
    project_keys_in_text,
)

DB_PATH = Path(os.environ.get('MMEMORY_DB') or Path(__file__).parent / 'memory.db')
SCHEMA_PATH = Path(__file__).parent / 'schema.sql'

# 集中定义 magic numbers
REINFORCE_INCREMENT = 0.1
WEIGHT_CAP = 3.0
DELETE_THRESHOLD = 0.2
DECAY_THRESHOLD = 0.01

# 遗留属性：get_conn 已改为每次连接都校验结构（不再依赖缓存），这个集合已无实际
# 作用，仅保留供既有测试调用 `.discard()`，避免改测试。
_schema_initialized: set[str] = set()

# 进程内全局写锁：把「读 in_transaction → BEGIN IMMEDIATE → 查重 → 插入 →
# commit」这一整段串行化。
#
# 为什么必须：Hermes 插件在会话线程建一条长连接，随后在工具线程池（最多 8 并发）
# 里反复复用它写入。`check_same_thread=False` 只解除了 sqlite3 的线程断言，并不能
# 让上述整段原子——多线程交错会互相提交/回滚对方的事务，实测 8 线程存同一条目落
# 4 行并抛 "cannot start a transaction within a transaction"，还可能把连接留在
# 未提交状态（之后写入报成功却不落盘，进程退出即静默丢失）。
#
# 用一把模块级 RLock 而不是「每连接一把」：sqlite3.Connection 不支持 weakref，
# 无法安全地按连接身份挂锁（id() 会被复用，强引用会泄漏连接）。本系统是单用户、
# 单进程、几十行的规模，写操作本就该串行，全局锁的代价可忽略且实现无生命周期
# 陷阱。跨进程并发仍由 SQLite 的 WAL + busy_timeout 保证。
_write_lock = threading.RLock()


def conn_lock(conn: sqlite3.Connection) -> threading.RLock:
    """取得进程内写锁（保留 conn 参数以便调用点不变）。"""
    return _write_lock
# 项目命名空间约定：空字符串是未指定项目的兼容命名空间。
# 这样旧调用仍能读写旧记录，同时带 project_key 的记录不会被旧调用
# 意外参与去重、里程碑替换或召回。
LEGACY_PROJECT_KEY = ''


def normalize_project_key(project_key: Optional[str]) -> str:
    """归一化项目键；None/空白值表示旧调用的兼容命名空间。"""
    if project_key is None:
        return LEGACY_PROJECT_KEY
    return str(project_key).strip()


def resolve_project_key(text: str, project_key: Optional[str] = None) -> str:
    """显式键优先；未传键时仅按高信号项目名推断。"""
    if project_key is not None:
        return normalize_project_key(project_key)
    return infer_project_key(text)


def resolve_recall_project_keys(text: str, project_key=None) -> list[str]:
    """解析召回作用域；显式字符串严格单项目，空串严格共享桶。"""
    if isinstance(project_key, str):
        return [normalize_project_key(project_key)]
    if project_key is not None:
        values = [normalize_project_key(value) for value in project_key]
        return list(dict.fromkeys(values))
    inferred = infer_query_project_keys(text)
    return inferred or [LEGACY_PROJECT_KEY]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """检查列是否存在，兼容尚未迁移的旧数据库。

    只把「表不存在」当作 False；其它数据库错误（SQLITE_CORRUPT、文件被换等）
    必须向上抛——以前 `except sqlite3.Error: return False` 会把真实故障伪装成
    「旧 schema」，让 store 返回一个平静的 stored=False，错误被长期掩盖。
    """
    try:
        return any(row[1] == column for row in conn.execute(
            f"PRAGMA table_info({table})").fetchall())
    except sqlite3.OperationalError as e:
        if 'no such table' in str(e).lower():
            return False
        raise


def _project_clause(conn: sqlite3.Connection, project_key,
                    alias: str = '', table: str = 'memories') -> tuple[str, list]:
    """返回项目过滤 SQL 和参数；召回可传多个精确项目作用域。

    新 schema 按精确 project_key 过滤；旧 schema 无法安全表达非空项目，
    因此对显式项目返回永不匹配条件，避免静默跨项目访问。
    """
    if project_key is None:
        keys = [LEGACY_PROJECT_KEY]
    elif isinstance(project_key, str):
        keys = [normalize_project_key(project_key)]
    else:
        keys = list(dict.fromkeys(normalize_project_key(value) for value in project_key))
    prefix = f'{alias}.' if alias else ''
    if not keys:
        return ' AND 1 = 0', []
    if not _has_column(conn, table, 'project_key'):
        if keys == [LEGACY_PROJECT_KEY]:
            return '', []
        return ' AND 1 = 0', []
    # 共享桶要把 NULL 也算进去：schema 是 NOT NULL DEFAULT ''，但外部裸连接或
    # 迁移脚本可能留下 project_key IS NULL 的行，而 `NULL = ''` 不成立。
    parts, params = [], []
    for key in keys:
        if key == LEGACY_PROJECT_KEY:
            parts.append(f'({prefix}project_key = ? OR {prefix}project_key IS NULL)')
        else:
            parts.append(f'{prefix}project_key = ?')
        params.append(key)
    return ' AND (' + ' OR '.join(parts) + ')', params


def get_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """获取数据库连接，首次调用时自动建表"""
    # path 统一解析成绝对字符串：避免同一文件因传入 Path/相对路径被当成两个缓存键
    path = str(Path(db_path or str(DB_PATH)).expanduser().resolve())
    # timeout：拿不到写锁时等待而不是立刻抛 "database is locked"。store() 用
    # BEGIN IMMEDIATE 串行化写入，并发写者需要这个等待窗口才能排队而非报错。
    # check_same_thread=False：连接可能被跨线程复用（Hermes 插件在会话线程建连、
    # 在工作线程里调用 store/recall），默认 True 会直接抛 ProgrammingError 并被
    # 上层吞成「存储失败」，静默丢写。共享连接的并发互斥由模块级 _write_lock 保证。
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    # 从建立连接到结构校验结束，任何失败都必须关闭连接再抛，否则 fd 会随每次
    # 失败调用累积泄漏（PRAGMA 也会在非数据库文件上抛错，所以 try 必须从这里开始）。
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        _ensure_healthy(conn)
    except Exception:
        conn.close()
        raise

    return conn


def _ensure_healthy(conn: sqlite3.Connection):
    """确保 memories 表、project_key 列、memories_fts、同步触发器齐备（四项）。

    各项**独立**检查、缺失即补——不要用 elif 串联：实测「表在、FTS 在、
    project_key 列缺」或「表在、FTS 表在、触发器被删」这类局部缺失会被串联逻辑
    漏掉，后果分别是显式项目写入被静默拒绝、FTS 索引永久陈旧不告警。
    """
    with _write_lock:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'memories' not in tables:
            conn.executescript(SCHEMA_PATH.read_text())
            conn.commit()
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        # 1) project_key 列（无条件检查，与 FTS 分支解耦）
        if 'memories' in tables and not _has_column(
                conn, 'memories', 'project_key'):
            conn.execute("ALTER TABLE memories ADD COLUMN project_key "
                         "TEXT NOT NULL DEFAULT ''")
            conn.commit()

        # 旧 weight 可能已经衰减过，无法从 last_used 推断已扣除的区间。
        # 升级时保留现有权重，以现在为新基线，不追扣未知历史。
        if not _has_column(conn, 'memories', 'decay_cursor_at'):
            conn.execute('ALTER TABLE memories ADD COLUMN decay_cursor_at TEXT')
            conn.execute("UPDATE memories SET decay_cursor_at = datetime('now', 'localtime')")
            conn.commit()

        for column, definition in [('archived_at', 'TEXT'),
                                   ('archive_reason', "TEXT DEFAULT ''"),
                                   ('protected', 'INTEGER NOT NULL DEFAULT 0'),
                                   ('protection_reason', "TEXT DEFAULT ''")]:
            if not _has_column(conn, 'memories', column):
                conn.execute(f'ALTER TABLE memories ADD COLUMN {column} {definition}')
                conn.commit()

        if 'memory_relations' not in tables:
            conn.execute(_RELATIONS_TABLE_DDL)
            conn.commit()

        # 2) FTS 虚表：缺失则补建并立刻回填（空索引会让之后对既有行的
        #    UPDATE/DELETE 报 `database disk image is malformed`）
        if 'memories_fts' not in tables:
            conn.execute(_FTS_TABLE_DDL)
            conn.commit()
            conn.execute(
                "INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            conn.commit()

        # 3) 三个同步触发器：缺失则补建（否则索引在计数上"健康"但内容永久陈旧）
        triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
        missing = [d for name, d in _FTS_TRIGGER_DDL if name not in triggers]
        if missing:
            for ddl in missing:
                conn.execute(ddl)
            conn.commit()
            # 触发器是索引正确性的来源，补建后重建一次索引以对齐
            conn.execute(
                "INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            conn.commit()

        # 4) 索引漂移自愈：用 docsize（索引真实文档数）而非 `count(*) FROM
        #    memories_fts` —— 后者是 content='memories' 的外部内容表，返回的就是
        #    主表行数，比较恒等、永不触发 rebuild。
        try:
            n_indexed = conn.execute(
                "SELECT count(*) FROM memories_fts_docsize").fetchone()[0]
            n_main = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
            if n_indexed != n_main:
                conn.execute(
                    "INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
                conn.commit()
        except sqlite3.OperationalError:
            # 必须 rollback 再吞：rebuild 失败（如另一写者持锁超时）时 python-
            # sqlite3 已隐式 BEGIN，异常后事务仍开着。若把这条连接返回给调用方，
            # 调用方的 store 会因 own_tx=False 而永不提交——报 stored=True 却
            # 永久丢失，同时残留写锁阻塞其它进程。
            try:
                conn.rollback()
            except sqlite3.Error:
                pass

        # 不变量：get_conn 返回的连接绝不能带着未提交事务。调用方据此决定
        # own_tx；残留事务会让它们的写入静默不落盘。
        if conn.in_transaction:
            conn.rollback()


_FTS_TABLE_DDL = """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        goal, approach, outcome, success_key, pitfall, tags_keywords,
        content='memories', content_rowid='id')
"""

_RELATIONS_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS memory_relations (
        source_id INTEGER NOT NULL REFERENCES memories(id),
        target_id INTEGER NOT NULL REFERENCES memories(id),
        relation TEXT NOT NULL CHECK (relation IN ('supersedes', 'derived_from')),
        reason TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        PRIMARY KEY (source_id, target_id, relation))
"""

_FTS_TRIGGER_DDL = (
    ('memories_ai', """
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, goal, approach, outcome,
                success_key, pitfall, tags_keywords)
            VALUES (new.id, new.goal, new.approach, new.outcome,
                new.success_key, new.pitfall, new.tags_keywords);
        END
    """),
    ('memories_ad', """
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, goal, approach,
                outcome, success_key, pitfall, tags_keywords)
            VALUES ('delete', old.id, old.goal, old.approach, old.outcome,
                old.success_key, old.pitfall, old.tags_keywords);
        END
    """),
    ('memories_au', """
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, goal, approach,
                outcome, success_key, pitfall, tags_keywords)
            VALUES ('delete', old.id, old.goal, old.approach, old.outcome,
                old.success_key, old.pitfall, old.tags_keywords);
            INSERT INTO memories_fts(rowid, goal, approach, outcome,
                success_key, pitfall, tags_keywords)
            VALUES (new.id, new.goal, new.approach, new.outcome,
                new.success_key, new.pitfall, new.tags_keywords);
        END
    """),
)


# ── 写入 ────────────────────────────────────────────────────────

def store(goal: str, approach: str, outcome: str,
          success_key: str = '', pitfall: str = '',
          db_conn: Optional[sqlite3.Connection] = None,
          project_key: Optional[str] = None) -> dict:
    """
    存储一条记忆。
    返回 {'stored': bool, 'reason': str, 'memory_id': int|None}
    """
    conn = db_conn or get_conn()
    own_conn = db_conn is None
    # 整段写入临界区（读 in_transaction → BEGIN IMMEDIATE → 查重 → 插入 → commit）
    # 必须在连接锁内，否则共享连接的多线程会交错提交对方的事务。
    with conn_lock(conn):
        return _store_locked(conn, own_conn, goal, approach, outcome,
                             success_key, pitfall, project_key)


def _store_locked(conn, own_conn, goal, approach, outcome,
                  success_key, pitfall, project_key):
    import filter as mmemory_filter

    # 事务所有权：只有当调用方没有活跃事务时，store 才自己开事务并负责提交/回滚。
    # 调用方已有未提交事务（例如想批量原子写入）时，store 只借用该连接、不提交也
    # 不回滚，把事务边界留给调用方——否则 store 的 commit 会把调用方的半成品工作
    # 一起落盘，rollback 则会静默丢掉调用方的数据。
    own_tx = not conn.in_transaction
    # 记录「本函数是否自己开了 SAVEPOINT」。异常处理只在确实开过时才去
    # ROLLBACK TO —— SQLite 按名字解析 savepoint，若调用方恰好有同名 savepoint，
    # 无条件 ROLLBACK TO 会**越权回滚掉调用方的帧**（实测丢调用方未提交数据）。
    sp_open = False

    try:
        # 空字段校验：goal/approach/outcome 不能为空。
        # 用 `filter.is_blank` 判空而非 `str.strip()`：后者不会剥掉零宽/格式
        # 字符（`'\u200b'.strip()` 仍是 `'\u200b'`），纯不可见目标能通过校验，
        # 而 `_norm_goal` 又不删这类字符 ⇒ 归一化非空 ⇒ 正常入库，
        # 堆成读不懂也召不回的垃圾行。is_blank 覆盖全部 Unicode 格式化字符。
        empty_fields = [name for name, val in
                        [('goal', goal), ('approach', approach), ('outcome', outcome)]
                        if mmemory_filter.is_blank(val)]
        if empty_fields:
            return {'stored': False,
                    'reason': f'必填字段为空: {", ".join(empty_fields)}',
                    'memory_id': None}

        # 目标明确涉及多个项目时不能静默取词表里的第一个；调用方必须选择
        # 一个 project_key。其他四字段只用于目标没有项目信号时的兜底。
        if project_key is None:
            goal_projects = project_keys_in_text(goal)
            if len(goal_projects) > 1:
                return {'stored': False,
                        'reason': '目标涉及多个项目，请显式指定 project_key',
                        'memory_id': None}
            project_key = ((goal_projects[0] if goal_projects else '')
                           or infer_project_key(f"{approach} {outcome} {success_key} {pitfall}"))
        project_key = normalize_project_key(project_key)

        # 长度与字段合同对直接 API、CLI 和自动消费者都必须一致。
        validation_ok, validation_reason, cleaned = mmemory_filter.validate_fields(
            goal, approach, outcome, success_key, pitfall)
        if not validation_ok:
            return {'stored': False, 'reason': validation_reason, 'memory_id': None}
        success_key = cleaned['success_key']
        pitfall = cleaned['pitfall']
        tags = generate_tags(goal, approach, outcome, success_key, pitfall)
        warnings = [] if validation_reason == '字段校验通过' else [validation_reason]

        # 旧数据库（无 project_key 列）无法安全表达显式项目：必须在**开启事务
        # 之前**就判定并返回。放在 BEGIN 之后会被下面任何早退路径跳过提交/回滚，
        # 把写事务永久留在连接上（实测：in_transaction=True 残留、其它连接
        # `database is locked`、同连接后续 store 报成功却永不落盘）。
        has_project_col = _has_column(conn, 'memories', 'project_key')
        if not has_project_col and normalize_project_key(project_key):
            return {'stored': False,
                    'reason': '数据库 schema 尚未支持 project_key，未写入显式项目记忆',
                    'memory_id': None}

        # 「查重 + 插入」必须串行化：两者分开执行时，多个写者会同时通过去重检查、
        # 然后各自插入，产生重复行（实测 20 线程存同一条目会落 20 行）。
        # BEGIN IMMEDIATE 先拿写锁，让并发写者排队，使检查与插入对其它写者原子。
        if own_tx:
            conn.execute("BEGIN IMMEDIATE")

        replaced_id = mmemory_filter.find_replaced_milestone(
            goal, tags['tags_domain'], tags['tags_tool'], conn,
            project_key=project_key)
        # 保护是明确的人工作用：完成状态扩展不能自动移出受保护来源。
        if replaced_id is not None and _has_column(conn, 'memories', 'protected'):
            protected_row = conn.execute('SELECT protected FROM memories WHERE id=?',
                                         (replaced_id,)).fetchone()
            if protected_row['protected']:
                warnings.append(f'记忆 #{replaced_id} 已受保护，旧版本保持活跃；新记录独立保存')
                replaced_id = None

        # 去重检查（排除即将被替换的行）
        dup_id = check_duplicate(conn, tags, goal, project_key=project_key,
                                 exclude_id=replaced_id)
        if dup_id is not None:
            existing = conn.execute(
                'SELECT approach,outcome,success_key,pitfall FROM memories WHERE id=?',
                (dup_id,)).fetchone()
            changed = [field for field, value in
                       [('approach', approach), ('outcome', outcome),
                        ('success_key', success_key), ('pitfall', pitfall)]
                       if value != (existing[field] or '')]
            if changed:
                warnings.append(f'正文未融合：{", ".join(changed)} 与原记录不同；新增经验或更正结论请使用 update {dup_id}')
            _reinforce_inline(conn, dup_id)
            if own_tx:
                conn.commit()
            return {'stored': False,
                    'reason': f'与记忆 #{dup_id} 重复，已强化',
                    'memory_id': dup_id, 'warnings': warnings}

        # 「归档旧版本 + 插新」用 SAVEPOINT 包住，无论是否自有事务都原子。
        # 只用 `if own_tx: rollback()` 不够：调用方已开事务（文档鼓励的批量原子
        # 写入用法）时，若 INSERT 失败而不局部回滚，旧里程碑会退出召回而新行不存在。
        # SAVEPOINT 只回滚我们自己这一段，不触碰调用方事务。
        conn.execute("SAVEPOINT mm_store_replace")
        sp_open = True
        if replaced_id is not None:
            conn.execute("""UPDATE memories SET status='archived',
                archived_at=datetime('now','localtime'), archive_reason='被完成状态升级的里程碑取代'
                WHERE id=? AND status='active'""", (replaced_id,))

        project = normalize_project_key(project_key)
        if has_project_col:
            cur = conn.execute("""
                INSERT INTO memories
                    (project_key, goal, approach, outcome, success_key, pitfall,
                     tags_tool, tags_domain, tags_action, tags_keywords)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (project, goal, approach, outcome, success_key, pitfall,
                  tags['tags_tool'], tags['tags_domain'], tags['tags_action'],
                  tags['tags_keywords']))
        else:
            # 旧 schema（无 project_key 列）：显式项目已在上面拦下，这里只可能是
            # 共享桶写入，保留旧调用能力。
            cur = conn.execute("""
                INSERT INTO memories
                    (goal, approach, outcome, success_key, pitfall,
                     tags_tool, tags_domain, tags_action, tags_keywords)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (goal, approach, outcome, success_key, pitfall,
                  tags['tags_tool'], tags['tags_domain'], tags['tags_action'],
                  tags['tags_keywords']))

        if _has_column(conn, "memories", "decay_cursor_at"):
            conn.execute("UPDATE memories SET decay_cursor_at = datetime('now', 'localtime') WHERE id = ?", (cur.lastrowid,))

        if replaced_id is not None:
            linked = link_memories(cur.lastrowid, replaced_id, 'supersedes',
                                   '同项目完成状态扩展，新里程碑取代旧里程碑', db_conn=conn)
            if not linked['linked']:
                raise ValueError(linked['reason'])

        conn.execute("RELEASE mm_store_replace")
        sp_open = False
        if own_tx:
            conn.commit()
        mem_id = cur.lastrowid
        reason = '新记忆已存储'
        if replaced_id is not None:
            reason += f'（替换旧记忆 #{replaced_id}）'
        return {'stored': True, 'reason': reason, 'memory_id': mem_id, 'warnings': warnings}
    except Exception:
        # 只有确实开过 SAVEPOINT 才回滚它，恢复旧版本的活跃状态。
        # 无条件回滚会越权销毁调用方的同名 savepoint。
        if sp_open:
            try:
                conn.execute("ROLLBACK TO mm_store_replace")
                conn.execute("RELEASE mm_store_replace")
            except sqlite3.Error:
                pass
        if own_tx:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise
    finally:
        if own_conn:
            conn.close()


def update_memory(memory_id: int, *, goal: Optional[str] = None,
                  approach: Optional[str] = None, outcome: Optional[str] = None,
                  success_key: Optional[str] = None, pitfall: Optional[str] = None,
                  project_key: Optional[str] = None,
                  db_conn: Optional[sqlite3.Connection] = None) -> dict:
    """显式修订活跃正文，保留项目和使用元数据，不触发强化或里程碑替换。"""
    import filter as mmemory_filter
    conn = db_conn or get_conn()
    own_conn = db_conn is None
    with conn_lock(conn):
        own_tx = not conn.in_transaction
        sp_open = False
        try:
            if own_tx:
                conn.execute('BEGIN IMMEDIATE')
            clause, params = ('', []) if project_key is None else _project_clause(conn, project_key)
            row = conn.execute(f'SELECT * FROM memories WHERE id = ? {clause}',
                               (memory_id, *params)).fetchone()
            if row is None:
                if own_tx:
                    conn.rollback()
                return {'updated': False, 'reason': '未找到指定项目中的记忆', 'memory_id': memory_id}
            if row['status'] != 'active':
                if own_tx:
                    conn.rollback()
                return {'updated': False, 'reason': '归档原文不可直接修订，请先恢复', 'memory_id': memory_id}
            values = dict(row)
            changes = dict(goal=goal, approach=approach, outcome=outcome,
                           success_key=success_key, pitfall=pitfall)
            values.update({key: value.strip() for key, value in changes.items() if value is not None})
            if any(mmemory_filter.is_blank(values[key]) for key in ('goal', 'approach', 'outcome')):
                if own_tx:
                    conn.rollback()
                return {'updated': False, 'reason': '必填字段不能为空', 'memory_id': memory_id}
            validation_ok, validation_reason, cleaned = mmemory_filter.validate_fields(
                *(values[key] for key in CONTENT_FIELDS))
            if not validation_ok:
                if own_tx:
                    conn.rollback()
                return {'updated': False, 'reason': validation_reason, 'memory_id': memory_id}
            values['success_key'] = cleaned['success_key']
            values['pitfall'] = cleaned['pitfall']
            tags = generate_tags(*(values[key] for key in CONTENT_FIELDS))
            duplicate = check_duplicate(conn, tags, values['goal'],
                                        project_key=values['project_key'], exclude_id=memory_id)
            if duplicate is not None:
                if own_tx:
                    conn.rollback()
                return {'updated': False, 'reason': f'目标与记忆 #{duplicate} 精确重复', 'memory_id': memory_id}
            conn.execute('SAVEPOINT mm_update')
            sp_open = True
            conn.execute('''UPDATE memories SET goal=?, approach=?, outcome=?, success_key=?, pitfall=?,
                            tags_tool=?, tags_domain=?, tags_action=?, tags_keywords=? WHERE id=?''',
                         (*(values[key] for key in CONTENT_FIELDS), tags['tags_tool'],
                          tags['tags_domain'], tags['tags_action'], tags['tags_keywords'], memory_id))
            conn.execute('RELEASE mm_update')
            sp_open = False
            if own_tx:
                conn.commit()
            return {'updated': True, 'reason': '正文及标签已更新，使用记录保持原值', 'memory_id': memory_id}
        except Exception:
            if sp_open:
                conn.execute('ROLLBACK TO mm_update')
                conn.execute('RELEASE mm_update')
            if own_tx:
                conn.rollback()
            raise
        finally:
            if own_conn:
                conn.close()


def archive_memories(memory_ids: list[int], reason: str, *,
                     project_key: Optional[str] = None,
                     db_conn: Optional[sqlite3.Connection] = None) -> dict:
    """按实际 ID 原子归档；完整保留正文、项目及使用元数据。"""
    return _transition_memories(memory_ids, 'archived', reason,
                                project_key=project_key, db_conn=db_conn)


def restore_memories(memory_ids: list[int], *, project_key: Optional[str] = None,
                     db_conn: Optional[sqlite3.Connection] = None) -> dict:
    """恢复原 ID；拒绝同项目精确目标冲突，归档期间不计入后续衰减。"""
    return _transition_memories(memory_ids, 'active', '',
                                project_key=project_key, db_conn=db_conn)


def _transition_memories(memory_ids, target, reason, *, project_key, db_conn):
    import filter as mmemory_filter
    ids = list(dict.fromkeys(int(mid) for mid in memory_ids))
    operation = 'archived' if target == 'archived' else 'restored'
    result = {operation: False, 'count': 0, 'memory_ids': ids, 'reason': ''}
    if not ids or any(mid <= 0 for mid in ids):
        return {**result, 'reason': '须提供有效的实际记忆 ID'}
    if target == 'archived' and mmemory_filter.is_blank(reason):
        return {**result, 'reason': '归档必须填写原因'}
    conn = db_conn or get_conn()
    own_conn = db_conn is None
    with conn_lock(conn):
        own_tx = not conn.in_transaction
        sp_open = False
        try:
            if own_tx:
                conn.execute('BEGIN IMMEDIATE')
            clause, params = ('', []) if project_key is None else _project_clause(conn, project_key)
            placeholders = ','.join('?' for _ in ids)
            rows = conn.execute(f'SELECT * FROM memories WHERE id IN ({placeholders}) {clause}',
                                (*ids, *params)).fetchall()
            expected = 'active' if target == 'archived' else 'archived'
            if len(rows) != len(ids) or any(row['status'] != expected for row in rows):
                if own_tx:
                    conn.rollback()
                return {**result, 'reason': f'整批未修改：ID不存在、项目不符或并非{expected}状态'}
            if target == 'active':
                seen = set()
                for row in rows:
                    key = (row['project_key'], mmemory_filter._norm_goal(row['goal']))
                    duplicate = check_duplicate(conn, {}, row['goal'], project_key=row['project_key'])
                    if key in seen or duplicate is not None:
                        if own_tx:
                            conn.rollback()
                        return {**result, 'reason': f'整批未恢复：记忆 #{row["id"]} 与活跃记录或本批目标精确冲突'}
                    seen.add(key)
            conn.execute('SAVEPOINT mm_transition')
            sp_open = True
            if target == 'archived':
                conn.executemany("""UPDATE memories SET status='archived',
                    archived_at=datetime('now','localtime'), archive_reason=? WHERE id=?""",
                                 [(reason, mid) for mid in ids])
            else:
                conn.executemany("""UPDATE memories SET status='active', archived_at=NULL,
                    archive_reason='', decay_cursor_at=datetime('now','localtime') WHERE id=?""",
                                 [(mid,) for mid in ids])
            conn.execute('RELEASE mm_transition')
            sp_open = False
            if own_tx:
                conn.commit()
            return {operation: True, 'count': len(ids), 'memory_ids': ids,
                    'reason': '已归档，原文和使用元数据保留' if target == 'archived' else '已恢复，归档时长不参与衰减'}
        except Exception:
            if sp_open:
                conn.execute('ROLLBACK TO mm_transition')
                conn.execute('RELEASE mm_transition')
            if own_tx:
                conn.rollback()
            raise
        finally:
            if own_conn:
                conn.close()


def set_memory_protection(memory_ids: list[int], protected: bool, reason: str = '', *,
                          project_key: Optional[str] = None,
                          db_conn: Optional[sqlite3.Connection] = None) -> dict:
    """按实际 ID 整批设置保护；阻止自动维护，不限制人工归档。"""
    import filter as mmemory_filter
    ids = list(dict.fromkeys(int(mid) for mid in memory_ids))
    result = {'changed': False, 'memory_ids': ids, 'protected': bool(protected), 'reason': ''}
    if not ids or any(mid <= 0 for mid in ids):
        return {**result, 'reason': '须提供有效的实际记忆 ID'}
    if protected not in (True, False):
        return {**result, 'reason': 'protected 必须是 True 或 False'}
    if protected and mmemory_filter.is_blank(reason):
        return {**result, 'reason': '保护必须填写理由'}
    conn = db_conn or get_conn()
    own_conn = db_conn is None
    with conn_lock(conn):
        own_tx = not conn.in_transaction
        sp_open = False
        try:
            if own_tx:
                conn.execute('BEGIN IMMEDIATE')
            clause, params = ('', []) if project_key is None else _project_clause(conn, project_key)
            placeholders = ','.join('?' for _ in ids)
            rows = conn.execute(f'SELECT id FROM memories WHERE id IN ({placeholders}) {clause}',
                                (*ids, *params)).fetchall()
            if len(rows) != len(ids):
                if own_tx:
                    conn.rollback()
                return {**result, 'reason': '整批未修改：ID 不存在或项目不符'}
            conn.execute('SAVEPOINT mm_protection')
            sp_open = True
            if protected:
                conn.executemany('UPDATE memories SET protected=1, protection_reason=? WHERE id=?',
                                 [(reason, mid) for mid in ids])
            else:
                # 仅真正解除保护时恢复衰减计时，不重复重置原本未保护的记录。
                conn.executemany("""UPDATE memories SET
                    decay_cursor_at=CASE WHEN protected=1 THEN datetime('now','localtime')
                                         ELSE decay_cursor_at END,
                    protected=0, protection_reason=? WHERE id=?""",
                                 [(reason, mid) for mid in ids])
            conn.execute('RELEASE mm_protection')
            sp_open = False
            if own_tx:
                conn.commit()
            message = ('保护已设置，正文和全部使用元数据保持原值' if protected else
                       '已解除保护；原受保护记录恢复衰减计时，权重、次数和真实使用时间保持原值')
            return {**result, 'changed': True, 'reason': message}
        except Exception:
            if sp_open:
                conn.execute('ROLLBACK TO mm_protection')
                conn.execute('RELEASE mm_protection')
            if own_tx:
                conn.rollback()
            raise
        finally:
            if own_conn:
                conn.close()


def link_memories(source_id: int, target_id: int, relation: str, reason: str, *,
                  project_key: Optional[str] = None,
                  db_conn: Optional[sqlite3.Connection] = None) -> dict:
    """记录同项目的显式引用；source 取代或源于 target，不自动归档任何记忆。"""
    import filter as mmemory_filter
    source_id, target_id = int(source_id), int(target_id)
    result = {'linked': False, 'source_id': source_id, 'target_id': target_id,
              'relation': relation, 'reason': ''}
    if source_id <= 0 or target_id <= 0 or source_id == target_id:
        return {**result, 'reason': '须提供两个不同的实际记忆 ID'}
    if relation not in ('supersedes', 'derived_from'):
        return {**result, 'reason': 'relation 仅支持 supersedes 或 derived_from'}
    if mmemory_filter.is_blank(reason):
        return {**result, 'reason': '引用关系必须填写理由'}
    conn = db_conn or get_conn()
    own_conn = db_conn is None
    with conn_lock(conn):
        own_tx = not conn.in_transaction
        sp_open = False
        try:
            if own_tx:
                conn.execute('BEGIN IMMEDIATE')
            clause, params = ('', []) if project_key is None else _project_clause(conn, project_key)
            rows = conn.execute(f'SELECT id,project_key FROM memories WHERE id IN (?,?) {clause}',
                                (source_id, target_id, *params)).fetchall()
            if len(rows) != 2:
                if own_tx:
                    conn.rollback()
                return {**result, 'reason': '未建立关系：ID 不存在或项目不符'}
            if normalize_project_key(rows[0]['project_key']) != normalize_project_key(rows[1]['project_key']):
                if own_tx:
                    conn.rollback()
                return {**result, 'reason': '未建立关系：不能跨项目引用或取代'}
            conn.execute('SAVEPOINT mm_link')
            sp_open = True
            conn.execute("""INSERT INTO memory_relations(source_id,target_id,relation,reason)
                VALUES(?,?,?,?) ON CONFLICT(source_id,target_id,relation)
                DO UPDATE SET reason=excluded.reason""", (source_id, target_id, relation, reason))
            conn.execute('RELEASE mm_link')
            sp_open = False
            if own_tx:
                conn.commit()
            return {**result, 'linked': True, 'reason': reason}
        except Exception:
            if sp_open:
                conn.execute('ROLLBACK TO mm_link')
                conn.execute('RELEASE mm_link')
            if own_tx:
                conn.rollback()
            raise
        finally:
            if own_conn:
                conn.close()


def list_relations(memory_id: int, db_conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """列出指定 ID 的出向和入向引用，包括归档来源。"""
    conn = db_conn or get_conn()
    own_conn = db_conn is None
    try:
        rows = conn.execute('''SELECT * FROM memory_relations WHERE source_id=? OR target_id=?
                               ORDER BY created_at,source_id,target_id,relation''',
                            (memory_id, memory_id)).fetchall()
        return [dict(row) for row in rows]
    finally:
        if own_conn:
            conn.close()


def backup_database(output: Optional[str] = None, db_path: Optional[str] = None) -> str:
    """只读源连接生成 SQLite 一致快照；不迁移源库，不覆盖已有文件。"""
    source_path = Path(db_path or DB_PATH).resolve()
    destination = Path(output) if output else source_path.parent / 'backups' / (
        'memory-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.db')
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # x 模式给同一用户的误操作一个明确错误，避免覆盖已有备份。
    with destination.open('xb'):
        pass
    try:
        from contextlib import closing
        with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
    except Exception:
        destination.unlink()
        raise
    return str(destination)


def check_duplicate(conn: sqlite3.Connection, tags: dict, goal: str,
                    project_key: Optional[str] = None,
                    exclude_id: Optional[int] = None) -> Optional[int]:
    """去重：同 project_key 且**归一化目标文本完全相同**才判定重复。

    判据刻意只有这一条，因为任何更宽松的度量都被实测证明会误判并静默丢数据：

    1. 文本相似度区分不了「同义改写」与「相似的不同任务」，区间真重叠：
         同义改写（应合并）：用requests抓取网页数据 / 用requests爬取网页数据 = 0.600
         不同任务（应保留）：完成政治错题本录入 / 完成英语错题本录入          = 0.600
       更细的差别（政治/英语、第一问/第二问、不同项目字母）都在 0.6–0.75，与
       同义改写同区间，调阈值无解。
    2. 关键词集合也分不开：「第一问/第二问」「第一章/第二章」的关键词完全相同。
    3. 数字序列不够：jieba 会丢弃数字，且单纯比较数字集合**无序**，`第1章第2节`
       与 `第2章第1节` 的数字集合都是 {1,2}、相似度 1.0；单字母（A 项目/B 项目）
       也会被 jieba 整个丢弃。

    所以最终只保留最强的判据：NFC、折叠连续空白后逐字相同（保留标点）。这足以覆盖
    「同一件事存了两遍」（主要场景：同 goal 不同 approach/outcome），而把同义
    改写留成**可见**的重复行交由 Dream/人工合并。宁可留可见重复，绝不静默丢数据。

    候选范围不要求 `tags_tool`/`tags_domain` 相等（二者由文本偶然派生，同一任务
    两次记录常落不同标签）。`exclude_id` 用于排除即将被新里程碑取代的旧行。
    """
    import filter as mmemory_filter

    project_key = resolve_project_key(goal, project_key)
    project_sql, project_params = _project_clause(conn, project_key)
    candidates = conn.execute(f"""
        SELECT id, goal, tags_keywords, tags_domain, tags_tool FROM memories
        WHERE status = 'active'
          {project_sql}
        ORDER BY weight DESC
    """, tuple(project_params)).fetchall()

    if not candidates:
        return None

    new_norm = mmemory_filter._norm_goal(goal)
    if not new_norm:
        return None

    for c in candidates:
        if exclude_id is not None and c['id'] == exclude_id:
            continue
        if mmemory_filter._norm_goal(c['goal'] or '') == new_norm:
            return c['id']
    return None


def _reinforce_inline(conn: sqlite3.Connection, memory_id: int):
    """在事务内强化记忆（不 commit，由调用方决定何时提交）"""
    # 脏数值兜底：weight 为 NULL 或非数值 TEXT 时，`weight + ?` 会得到 NULL，
    # 或被 SQLite 弱类型强转成小数（'一点五' + 0.1 → 0.1，低于
    # DELETE_THRESHOLD，下一次 decay 立即归档）。COALESCE 只处理 NULL，
    # 所以用 typeof 判类别，非 real/integer 一律按 1.0 起算。
    cursor_sql = ", decay_cursor_at = datetime('now', 'localtime')" if _has_column(conn, 'memories', 'decay_cursor_at') else ''
    conn.execute(f"""
        UPDATE memories
        SET use_count = CASE WHEN typeof(use_count) IN ('integer','real')
                             THEN use_count ELSE 0 END + 1,
            last_used = datetime('now', 'localtime'),
            weight = MIN(
                (CASE WHEN typeof(weight) IN ('integer','real')
                      THEN weight ELSE 1.0 END) + ?, ?) {cursor_sql}
        WHERE id = ? AND status = 'active'
    """, (REINFORCE_INCREMENT, WEIGHT_CAP, memory_id))


# ── 召回 ────────────────────────────────────────────────────────

CONTENT_FIELDS = ('goal', 'approach', 'outcome', 'success_key', 'pitfall')


def recall(message: str, limit: int = 5,
           db_conn: Optional[sqlite3.Connection] = None,
           reinforce: bool = False,
           project_key: Optional[str] = None) -> list[dict]:
    """融合同项目的标签、全文及五字段候选；默认不强化。"""
    return recall_with_explain(message, limit, db_conn, reinforce, project_key)['memories']


def recall_with_explain(message: str, limit: int = 5,
                        db_conn: Optional[sqlite3.Connection] = None,
                        reinforce: bool = False,
                        project_key: Optional[str] = None) -> dict:
    """与普通召回共享实现，解释仅包含实际返回记录的 ID、来源和排序。"""
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 5
    conn = db_conn or get_conn()
    with conn_lock(conn):
        return _recall_locked(conn, db_conn is None, message, limit, reinforce, project_key)


def _recall_locked(conn, own_conn, message, limit, reinforce, project_key):
    own_tx = not conn.in_transaction
    try:
        project = resolve_recall_project_keys(message, project_key)
        tags = extract_tags_from_message(message, db_conn=conn)
        required_terms = query_terms(message)
        ranking_terms = _ranking_terms(message)
        # 小型本地记忆库先融合同项目候选再统一裁剪，不能按最终 limit 提前
        # 淘汰真正相关的文本命中，造成 limit=1 与 limit=20 的首条不同。
        clause, params = _project_clause(conn, project)
        candidate_limit = max(50, conn.execute(
            f"SELECT count(*) FROM memories WHERE status='active' {clause}", params).fetchone()[0])
        candidates, sources, text_rank = {}, {}, {}
        # 只有查询里存在具体词时才检索。泛词（如“实验”“课程”“问题”）不应
        # 借用标签把整个领域的历史任务全部拉进上下文。
        paths = (
            ('tag', _recall_by_tags(conn, tags, candidate_limit, project_key=project)),
            ('fts', _recall_by_fts(conn, message, candidate_limit, project_key=project)),
            ('like', _recall_by_like(conn, message, candidate_limit, project_key=project)),
        ) if required_terms else ()
        for source, rows in paths:
            for rank, row in enumerate(rows):
                mid = row['id']
                candidates[mid] = row
                sources.setdefault(mid, []).append(source)
                if source != 'tag':
                    text_rank[mid] = min(text_rank.get(mid, candidate_limit), rank)
        required_hits, hits, matched_chars = {}, {}, {}
        for mid, row in candidates.items():
            body = ' '.join(str(row.get(field) or '') for field in CONTENT_FIELDS)
            required_hits[mid] = sum(_term_in_text(term, body) for term in required_terms)
            hits[mid] = sum(_term_in_text(term, body) for term in ranking_terms)
            matched_chars[mid] = sum(len(term) for term in ranking_terms
                                     if _term_in_text(term, body))
        def order(row):
            mid = row['id']
            return (-required_hits[mid], -hits[mid], -matched_chars[mid],
                    text_rank.get(mid, candidate_limit), -int('tag' in sources[mid]),
                    -int(bool(row.get('protected'))), -float(row.get('weight') or 0), -mid)
        # 标签、动作词和宽泛词只能提升排序。两个以上非泛词时，至少命中两个，
        # 避免查询“Go 路由”只因共享“路由”而拉入 Django 记录。
        short_terms = [term for term in required_terms
                       if term.isascii() and len(term) <= 3]
        relevant = []
        for row in candidates.values():
            body = ' '.join(str(row.get(field) or '') for field in CONTENT_FIELDS)
            if required_hits[row['id']] < 1:
                continue
            # 短标识（如 Go、API、401）必须全部命中，避免共享中文词把
            # Django/Google 或相邻版本带进来；长词仍可单独召回。
            if any(not _term_in_text(term, body) for term in short_terms):
                continue
            relevant.append(row)
        results = sorted(relevant, key=order)[:limit]
        explanation = {
            'project_key': project[0] if len(project) == 1 else project,
            'project_keys': project,
            'query_terms': ranking_terms,
            'required_terms': required_terms,
            'required_short_terms': short_terms,
            'tags': tags,
            'order': 'required_hits DESC, text_hits DESC, matched_term_chars DESC, text_rank ASC, tag_hit DESC, protected DESC, weight DESC, id DESC',
            'results': [{'id': r['id'], 'sources': sources[r['id']],
                         'required_hits': required_hits[r['id']],
                         'text_hits': hits[r['id']],
                         'matched_term_chars': matched_chars[r['id']],
                         'text_rank': text_rank.get(r['id'])} for r in results],
        }
        if reinforce and results:
            _batch_reinforce(conn, [r['id'] for r in results])
            if own_tx:
                conn.commit()
        return {'memories': results, 'explanation': explanation}
    except Exception:
        if own_tx:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def _recall_by_tags(conn: sqlite3.Connection, tags: dict, limit: int,
                    project_key: Optional[str] = None) -> list[dict]:
    """Tag 精确匹配 — 要求至少一个维度不是 general"""
    domain = tags['tags_domain']
    tool = tags['tags_tool']
    action = tags['tags_action']
    project_sql, project_params = _project_clause(conn, project_key)

    # 如果两个核心维度都是 general，跳过精确匹配（避免召回全部）
    if domain == 'general' and tool == 'general':
        # 只用 action
        if action != 'general':
            rows = conn.execute(f"""
                SELECT * FROM memories
                WHERE status = 'active' AND tags_action = ?
                  {project_sql}
                ORDER BY weight DESC, last_used DESC
                LIMIT ?
            """, (action, *project_params, limit)).fetchall()
            return [dict(r) for r in rows] if rows else []
        return []

    # domain + tool 都非 general：AND 匹配
    if domain != 'general' and tool != 'general':
        rows = conn.execute(f"""
            SELECT * FROM memories
            WHERE status = 'active'
              AND tags_domain = ? AND tags_tool = ?
              {project_sql}
            ORDER BY weight DESC, last_used DESC
            LIMIT ?
        """, (domain, tool, *project_params, limit)).fetchall()
        if rows:
            return [dict(r) for r in rows]

    # 只有一个具体维度时，只按该维度匹配。以前的
    # ``domain = ? OR tool = ?`` 会把 ``general`` 当成通配符，导致
    # 一个项目的查询召回几乎所有其他项目。
    if domain != 'general':
        rows = conn.execute(f"""
            SELECT * FROM memories
            WHERE status = 'active' AND tags_domain = ?
              {project_sql}
            ORDER BY weight DESC, last_used DESC
            LIMIT ?
        """, (domain, *project_params, limit)).fetchall()
    elif tool != 'general':
        rows = conn.execute(f"""
            SELECT * FROM memories
            WHERE status = 'active' AND tags_tool = ?
              {project_sql}
            ORDER BY weight DESC, last_used DESC
            LIMIT ?
        """, (tool, *project_params, limit)).fetchall()
    else:
        rows = []

    if rows:
        return [dict(r) for r in rows]

    # 退一步：只匹配 action
    if action != 'general':
        rows = conn.execute(f"""
            SELECT * FROM memories
            WHERE status = 'active' AND tags_action = ?
              {project_sql}
            ORDER BY weight DESC, last_used DESC
            LIMIT ?
        """, (action, *project_params, limit)).fetchall()

    return [dict(r) for r in rows] if rows else []


_QUERY_FILLERS = {
    '请帮', '帮我', '请问', '一下', '现在', '需要', '查找', '寻找',
    '关于', '相关', '记忆', '之前', '以前', '如何', '怎么', '是否',
    '有没有', '告诉', '帮忙', '请', '想要', '想起', '用于', '我们',
    '这个', '那个', '进行', '可以', '什么', '解决', '一下子',
}

# 这些词可帮助同一项目内排序，但不能单独决定“有记忆命中”。
_QUERY_GENERIC_TERMS = {
    '问题', '实验', '课程', '项目', '系统', '报告', '文件', '配置', '修复',
    '完成', '实现', '优化', '测试', '验证', '审查', '复核', '交付',
    '版本', '当前', '状态', '设置', '历史', '经验', '内容', '操作',
    '方法', '任务', '错误', '报错', '失败', '原因', '使用', '结果',
    '代码', '功能',
}


def _clean_query_token(token: str) -> str:
    # 去掉外围问号/句号等标点，但保留 Node.js、C++、C# 这类内部或尾随有效符号。
    return (token or '').strip().strip('?!.,;:，。；：、')


def _query_terms(query: str, *, include_generic: bool = False,
                 max_terms: Optional[int] = None) -> list[str]:
    import jieba
    terms, seen = [], set()

    def add(token: str):
        token = _clean_query_token(token)
        if not token:
            return
        folded = token.casefold()
        if folded in _QUERY_FILLERS:
            return
        if not include_generic and folded in _QUERY_GENERIC_TERMS:
            return
        meaningful = len(token) > 1 or (token.isascii() and token.isalnum())
        if token.isascii() and len(token) == 1:
            meaningful = False
        if meaningful and folded not in seen:
            seen.add(folded)
            terms.append(token)

    # 章节编号与单字母题号是有意义标识，必须在 jieba 把 A题 拆成 A/题 前提取。
    for token in re.findall(
            r'(?:第[0-9一二三四五六七八九十百]+[章节问题])+|(?:[A-Za-z][题卷章节])',
            query or ''):
        add(token)
    for chunk in re.findall(r'[\u4e00-\u9fff]+|[^\s\u4e00-\u9fff]+', query or ''):
        tokens = jieba.cut(chunk) if re.search(r'[\u4e00-\u9fff]', chunk) else [chunk]
        for token in tokens:
            add(token)
            cleaned = _clean_query_token(token)
            # API401、V33 等紧凑标识参与 FTS 的同时拆出字母与数字，使正文里
            # “API 401”也能命中；普通单字母仍被 add() 丢弃。
            if (cleaned.isascii() and re.search(r'[A-Z]', cleaned)
                    and re.fullmatch(r'[A-Za-z]+\d+|\d+[A-Za-z]+', cleaned)):
                for part in re.findall(r'[A-Za-z]+|\d+', cleaned):
                    add(part)
    if max_terms is not None:
        return sorted(terms, key=lambda term: -len(term))[:max_terms]
    return terms


def query_terms(query: str, max_terms: Optional[int] = None) -> list[str]:
    """返回足以独立证明相关性的查询词；泛词不单独触发召回。"""
    return _query_terms(query, include_generic=False, max_terms=max_terms)


def _ranking_terms(query: str) -> list[str]:
    """返回可用于同项目排序的查询词，包含动作和状态等泛词。"""
    return _query_terms(query, include_generic=True)


def _search_terms(query: str) -> list[str]:
    """候选检索词；允许 A题 拆成 A/题 先取候选，正文门槛仍要求完整题号。"""
    terms = list(_ranking_terms(query))
    seen = {term.casefold() for term in terms}
    for term in terms:
        if re.fullmatch(r'[A-Za-z][题卷章节]', term):
            for part in (term[0], term[1]):
                if part.casefold() not in seen:
                    seen.add(part.casefold())
                    terms.append(part)
    return terms


def _term_in_text(term: str, text: str) -> bool:
    """正文命中检查；ASCII 标识按词边界，避免 Go 命中 Django/Google。"""
    folded_term, folded_text = term.casefold(), text.casefold()
    if folded_term.isascii():
        pattern = rf'(?<![a-z0-9]){re.escape(folded_term)}(?![a-z0-9])'
        return re.search(pattern, folded_text) is not None
    if re.fullmatch(r'[a-z][题卷章节]', folded_term):
        pattern = rf'(?<![a-z0-9]){re.escape(folded_term[0])}\s*{re.escape(folded_term[1])}'
        return re.search(pattern, folded_text) is not None
    return folded_term in folded_text


def _escape_fts5(query: str) -> str:
    """构造 FTS5 查询串，转义特殊字符防止语法错误/注入。

    用 OR 而非 AND 连接：全文候选与标签、LIKE 候选共同参与召回，自然语言往往只有部分词能
    对上（例如「记忆系统的召回逻辑是否需要改进」里只有"记忆系统""召回"命中）。
    用 AND 要求每个词都出现，实测这类查询 FTS 恒返回 0，整层形同废弃；OR 配合
    `ORDER BY rank` 让 BM25 把同时命中更多词的记录排到前面，兼顾召回与排序。
    """
    import re
    # FTS5 的布尔关键字必须剔除：它们会与拼接用的 OR 组合成非法表达式
    # （'部署 OR 发布' → '部署 OR OR OR 发布'），被下面的 OperationalError
    # 吞掉后整层静默失效。
    reserved = {'or', 'and', 'not', 'near'}
    words = _search_terms(query)
    safe_words = []
    for w in words:
        cleaned = re.sub(r'[-*"()+:\^%]', '', w)     # 去掉 FTS5/通配特殊字符
        cleaned = cleaned.strip()
        if len(cleaned) > 1 and cleaned.lower() not in reserved:
            safe_words.append(f'"{cleaned}"')        # 加引号：即使撞上保留字也只当字面词
    return ' OR '.join(safe_words) if safe_words else ''


def _recall_by_fts(conn: sqlite3.Connection, query: str, limit: int,
                   project_key: Optional[str] = None) -> list[dict]:
    """FTS5 全文检索（已转义）"""
    fts_query = _escape_fts5(query)
    if not fts_query:
        return []
    project_sql, project_params = _project_clause(conn, project_key, alias='m')
    try:
        rows = conn.execute(f"""
            SELECT m.* FROM memories_fts f
            JOIN memories m ON m.id = f.rowid
            WHERE memories_fts MATCH ? AND m.status = 'active'
              {project_sql}
            ORDER BY rank
            LIMIT ?
        """, (fts_query, *project_params, limit)).fetchall()
        return [dict(r) for r in rows] if rows else []
    except sqlite3.OperationalError as e:
        # 只吞 MATCH 语法/分词类错误（退到 LIKE 兜底即可）。除此之外——
        # 尤其是 `no such table: memories_fts`（FTS 表缺失，整层会永久静默失效）
        # 或 SQLITE_CORRUPT——必须向上抛，不能伪装成「没找到」。
        msg = str(e).lower()
        if msg.startswith('fts5: syntax error') or msg.startswith('unterminated string'):
            return []
        raise


def _recall_by_like(conn: sqlite3.Connection, query: str, limit: int,
                    project_key: Optional[str] = None) -> list[dict]:
    """五字段 LIKE，旧记录也无需重写关键词即可召回。"""
    keywords = _search_terms(query)
    if not keywords:
        return []
    term_conditions = []
    params = []
    for kw in keywords:
        safe = kw.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        condition = '(' + ' OR '.join(f"{field} LIKE ? ESCAPE '\\'" for field in CONTENT_FIELDS) + ')'
        term_conditions.append(condition)
        params.extend([f'%{safe}%'] * len(CONTENT_FIELDS))
    conditions = ' OR '.join(term_conditions)
    hit_count = ' + '.join(f'CASE WHEN {condition} THEN 1 ELSE 0 END' for condition in term_conditions)
    project_sql, project_params = _project_clause(conn, project_key)
    rows = conn.execute(f"""
        SELECT * FROM memories
        WHERE status = 'active' AND ({conditions}) {project_sql}
        ORDER BY ({hit_count}) DESC, weight DESC, id DESC LIMIT ?
    """, (*params, *project_params, *params, limit)).fetchall()
    return [dict(row) for row in rows]


def _batch_reinforce(conn: sqlite3.Connection, ids: list[int]):
    """批量强化（单条 SQL，避免 N+1）"""
    if not ids:
        return
    placeholders = ','.join('?' * len(ids))
    cursor_sql = ", decay_cursor_at = datetime('now', 'localtime')" if _has_column(conn, 'memories', 'decay_cursor_at') else ''
    conn.execute(f"""
        UPDATE memories
        SET use_count = CASE WHEN typeof(use_count) IN ('integer','real')
                             THEN use_count ELSE 0 END + 1,
            last_used = datetime('now', 'localtime'),
            weight = MIN(
                (CASE WHEN typeof(weight) IN ('integer','real')
                      THEN weight ELSE 1.0 END) + ?, ?)
            {cursor_sql}
        WHERE id IN ({placeholders}) AND status = 'active'
    """, (REINFORCE_INCREMENT, WEIGHT_CAP, *ids))


# ── 权重衰减 ────────────────────────────────────────────────────

def decay_all(db_conn: Optional[sqlite3.Connection] = None,
              delete_threshold: float = DELETE_THRESHOLD,
              dry_run: bool = False,
              project_key: Optional[str] = None) -> dict:
    """
    调用时按 decay_cursor_at 以来的新增时长衰减；使用艾宾浩斯曲线变体。
    dry_run=True 不改正文、权重或游标；打开连接仍可能维护 schema/索引。
    返回 {'decayed': int, 'archived': int}；低权重退出活跃集，原文可恢复。
    """
    conn = db_conn or get_conn()
    own_conn = db_conn is None
    with conn_lock(conn):
        return _decay_all_locked(conn, own_conn, delete_threshold, dry_run,
                                 project_key)


def _decay_all_locked(conn, own_conn, delete_threshold, dry_run, project_key):
    # 与 store 相同的事务所有权规则：调用方已开事务时不提交、不回滚。
    own_tx = not conn.in_transaction
    sp_open = False

    try:
        # 必须在读取前取得写锁，不能用旧快照覆盖另一进程刚强化的权重/游标。
        if not dry_run:
            if own_tx:
                conn.execute('BEGIN IMMEDIATE')
            conn.execute('SAVEPOINT mm_decay')
            sp_open = True
        if project_key is None:
            project_sql, project_params = '', []
        else:
            project_sql, project_params = _project_clause(conn, project_key)
        protected_sql = ' AND protected = 0' if _has_column(conn, 'memories', 'protected') else ''
        rows = conn.execute(f"""
            SELECT id, weight, use_count, last_used, decay_cursor_at FROM memories
            WHERE status = 'active' {project_sql} {protected_sql}
        """, project_params).fetchall()

        now = datetime.now()
        to_update = []   # (new_weight, id)
        to_archive = []  # (new_weight, id)

        for r in rows:
            # 脏数据（NULL / 空串 / 非法字符串 / 带时区）不能让整个衰减任务崩溃：
            # decay 也是 Dream 流程的一部分，一行坏数据会拖垮全部记忆的维护。
            # 这里把整段解析与减法都包住——`fromisoformat` 能解析带时区的
            # ISO-8601（如 '...+08:00'、'...Z'），返回的是 aware datetime，
            # 与 naive 的 now 相减会抛 TypeError，所以减法也必须在保护内。
            try:
                last = datetime.fromisoformat(r['decay_cursor_at'] or r['last_used'])
                if last.tzinfo is not None:
                    last = last.replace(tzinfo=None)
                # 数值列强制转换：NULL 用默认值，非数值（脏数据）同样跳过该行，
                # 否则 str 与 int/float 比较、字符串乘浮点都会抛 TypeError 拖垮整批。
                weight = float(r['weight']) if r['weight'] is not None else 1.0
                use_count = int(r['use_count']) if r['use_count'] is not None else 0
                days_idle = max((now - last).total_seconds() / 86400, 0)
            except (TypeError, ValueError):
                continue

            # use_count 现在只在显式 reinforce 或 store 去重命中时增长，召回
            # 本身不再计分，所以绝大多数记录的计数是 0。原先 0 次对应 0.15
            # 的衰减率，约 11 天不用就退出活跃集——对长期项目记忆过于激进，等于
            # 把「最近没人提起」当成「这条经验没价值」。这里整体放宽，最激进
            # 的一档也要 ~54 天，且长期项目记忆仍比一次性任务衰减更慢。
            if use_count >= 5:
                rate = 0.01     # ~161 天
            elif use_count >= 2:
                rate = 0.02     # ~80 天
            else:
                rate = 0.03     # ~54 天

            new_weight = weight * math.exp(-rate * days_idle)

            if new_weight < delete_threshold:
                to_archive.append((new_weight, r['id']))
            elif abs(new_weight - weight) > DECAY_THRESHOLD:
                to_update.append((new_weight, now.isoformat(sep=' ', timespec='microseconds'), r['id']))

        if not dry_run:
            # 批量 UPDATE（单条 SQL）
            if to_update:
                conn.executemany(
                    "UPDATE memories SET weight = ?, decay_cursor_at = ? WHERE id = ?", to_update)
            if to_archive:
                # 低权重只表示低频使用，不证明经验错误；退出活跃集但保留原文。
                conn.executemany(
                    """UPDATE memories SET status='archived', archived_at=datetime('now','localtime'),
                       archive_reason='衰减低于活跃阈值' WHERE id=? AND status='active'""",
                    [(mid,) for _, mid in to_archive])
            conn.execute('RELEASE mm_decay')
            sp_open = False
            if own_tx:
                conn.commit()

        return {'decayed': len(to_update), 'archived': len(to_archive)}
    except Exception:
        if sp_open:
            conn.execute('ROLLBACK TO mm_decay')
            conn.execute('RELEASE mm_decay')
        # 只回滚自有事务，并保证异常不把连接留在持写锁的打开事务里。
        if own_tx:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise
    finally:
        if own_conn:
            conn.close()


# ── 工具方法 ────────────────────────────────────────────────────

def list_all(status: str = 'active', limit: Optional[int] = 50,
             db_conn: Optional[sqlite3.Connection] = None,
             project_key: Optional[str] = None) -> list[dict]:
    """列出所有记忆。

    `limit=None` 表示**不限制**，用于需要全量数据的内建调用（如 Obsidian 导出
    必须导出全部）。数字 limit 会被夹到 [1, 500]——SQLite 的 LIMIT -1 等于
    「无上限」、负数会意外拉出全库，None/非数字则会抛异常。
    """
    if limit is None:
        limit_sql, limit_params = '', []
    else:
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 50
        limit_sql, limit_params = ' LIMIT ?', [limit]
    conn = db_conn or get_conn()
    own_conn = db_conn is None
    try:
        if project_key is None:
            project_sql, project_params = '', []
        else:
            project_sql, project_params = _project_clause(conn, project_key)
        status_sql, status_params = ('1=1', []) if status == 'all' else ('status = ?', [status])
        rows = conn.execute(f"""
            SELECT * FROM memories
            WHERE {status_sql} {project_sql}
            ORDER BY weight DESC{limit_sql}
        """, (*status_params, *project_params, *limit_params)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own_conn:
            conn.close()


def get_stats(db_conn: Optional[sqlite3.Connection] = None,
              project_key: Optional[str] = None) -> dict:
    """统计信息"""
    conn = db_conn or get_conn()
    own_conn = db_conn is None
    try:
        if project_key is None:
            project_sql, project_params = '', []
        else:
            project_sql, project_params = _project_clause(conn, project_key)
        active = conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE status = 'active' {project_sql}",
            project_params).fetchone()[0]
        # _project_clause 返回的是以 " AND ..." 开头的片段，所以必须自带 WHERE
        # 占位，否则拼出 "FROM memories AND project_key = ?" 直接语法错误。
        total = conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE 1=1 {project_sql}",
            project_params).fetchone()[0]
        archived = conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE status='archived' {project_sql}",
            project_params).fetchone()[0]
        projects = conn.execute(
            f"SELECT COUNT(DISTINCT NULLIF(project_key, '')) FROM memories "
            f"WHERE 1=1 {project_sql}",
            project_params).fetchone()[0] if _has_column(
                conn, 'memories', 'project_key') else 0
        return {
            'active': active,
            'archived': archived,
            'total': total,
            'projects': projects,
        }
    finally:
        if own_conn:
            conn.close()


def format_memory_summary(m: dict, index: int = 0) -> str:
    """列表默认摘要：只显示身份、目标、结果和必要状态。"""
    project = str(m.get('project_key') or '').strip()
    scope = f'[{project}] ' if project else ''
    flags = []
    if m.get('status') == 'archived':
        flags.append('archived')
    if m.get('protected'):
        flags.append('protected')
    suffix = f" [{' '.join(flags)}]" if flags else ''
    lines = [f"{index}. #{m['id']} {scope}{_clip_text(m.get('goal'), 120)}{suffix}"]
    if m.get('outcome'):
        lines.append(f"   结果：{_clip_text(m.get('outcome'), 160)}")
    if m.get('status') == 'archived':
        lines.append('   状态：archived')
        if m.get('archive_reason'):
            lines.append(f"   归档原因：{_clip_text(m.get('archive_reason'), 120)}")
    return '\n'.join(lines)


def format_memory(m: dict, index: int = 0) -> str:
    """格式化单条记忆用于注入上下文"""
    project = m.get('project_key', '')
    scope = f"{project}|" if project else ''
    prefix = f"[{scope}{m['tags_domain']}/{m['tags_tool']}/{m['tags_action']}]"
    lines = [f"{index}. #{m['id']} {prefix} 目标：{m['goal']}"]
    lines.append(f"   方案：{m['approach']}")
    lines.append(f"   结果：{m['outcome']}")
    if m.get('success_key'):
        lines.append(f"   成功关键：{m['success_key']}")
    if m.get('pitfall'):
        lines.append(f"   踩坑+解法：{m['pitfall']}")
    return '\n'.join(lines)


def _clip_text(text, limit: int) -> str:
    """压缩成单行摘要；完整正文仍可通过 list 查看。"""
    value = ' '.join(str(text or '').split())
    if len(value) <= limit:
        return value
    return value[:max(1, limit - 1)].rstrip() + '…'


def format_recall_context(m: dict, index: int = 0) -> str:
    """召回上下文条目：保留目标、结果和一条可复用教训，限制单条长度。"""
    project = str(m.get('project_key') or '').strip()
    scope = f'[{project}] ' if project else ''
    lines = [f"{index}. #{m['id']} {scope}{_clip_text(m.get('goal'), 120)}"]
    if m.get('outcome'):
        lines.append(f"   结果：{_clip_text(m.get('outcome'), 180)}")
    lesson = m.get('pitfall') or m.get('success_key') or m.get('approach')
    if lesson:
        lines.append(f"   要点：{_clip_text(lesson, 180)}")
    return '\n'.join(lines)


def format_recall_block(memories: list[dict], max_chars: int = 1200) -> str:
    """格式化召回结果，并保证返回值不超过调用方给定的总字符预算。"""
    budget = int(max_chars)
    if not memories or budget <= 0:
        return ''
    header = '[记忆系统] 以下是相关的历史线索，使用时请结合当前现场核实：\n'
    if len(header) > budget:
        return header[:budget]
    items = []
    used = len(header)
    for index, memory in enumerate(memories, 1):
        item = format_recall_context(memory, index)
        separator = '\n\n' if items else ''
        if used + len(separator) + len(item) > budget:
            break
        items.append(item)
        used += len(separator) + len(item)
    return header + '\n\n'.join(items)
