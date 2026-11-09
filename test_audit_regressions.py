"""Audited behavior regressions. Every database and export is synthetic."""
import math
import re
import sqlite3
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import engine
import dream
import filter as memory_filter


class AuditRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mmemory-regression-')
        self.db = Path(self.directory.name) / 'memory.db'
        self.conn = engine.get_conn(str(self.db))

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def store(self, goal, **fields):
        return engine.store(goal, fields.pop('approach', '运行合成步骤'),
                            fields.pop('outcome', '完成合成任务'), db_conn=self.conn,
                            project_key=fields.pop('project_key', 'fixture'), **fields)['memory_id']

    def row(self, mid):
        return dict(self.conn.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone())

    def clock(self, moment):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment
        return patch.object(engine, 'datetime', Frozen)

    def test_decay_does_not_charge_same_interval_twice(self):
        mid = self.store('衰减游标回归')
        start = datetime(2026, 1, 1)
        self.conn.execute('UPDATE memories SET last_used=?, decay_cursor_at=? WHERE id=?',
                          (start.isoformat(), start.isoformat(), mid))
        self.conn.commit()
        with self.clock(start + timedelta(days=30)):
            engine.decay_all(db_conn=self.conn)
            first = self.row(mid)
            self.assertAlmostEqual(first['weight'], math.exp(-.9))
            self.assertEqual(engine.decay_all(db_conn=self.conn), {'decayed': 0, 'archived': 0})
            self.assertEqual(first, self.row(mid))
            self.assertEqual(first['last_used'], start.isoformat())

    def test_daily_and_one_shot_decay_agree(self):
        one, daily = self.store('一次衰减'), self.store('按日衰减')
        start = datetime(2026, 1, 1)
        self.conn.execute('UPDATE memories SET decay_cursor_at=?', (start.isoformat(),))
        self.conn.commit()
        for day in range(1, 31):
            # Isolate rows with distinct project scopes while retaining identical age.
            self.conn.execute("UPDATE memories SET project_key='daily' WHERE id=?", (daily,))
            self.conn.commit()
            with self.clock(start + timedelta(days=day)):
                engine.decay_all(db_conn=self.conn, project_key='daily')
        with self.clock(start + timedelta(days=30)):
            engine.decay_all(db_conn=self.conn, project_key='fixture')
        self.assertAlmostEqual(self.row(one)['weight'], self.row(daily)['weight'])

    def test_decay_preview_keeps_cursor_and_caller_transaction(self):
        mid = self.store('预览游标')
        self.conn.execute("UPDATE memories SET decay_cursor_at='2026-01-01' WHERE id=?", (mid,))
        before = self.row(mid)
        with self.clock(datetime(2026, 1, 20)):
            engine.decay_all(db_conn=self.conn, dry_run=True)
            self.assertEqual(before, self.row(mid))
            engine.decay_all(db_conn=self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.row(mid)['weight'], 1)

    def old_database(self):
        db = Path(self.directory.name) / 'old.db'
        # 固定最小旧 schema，避免测试依赖当前 schema.sql 的追加列位置。
        schema = """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal TEXT NOT NULL,
            approach TEXT NOT NULL,
            outcome TEXT NOT NULL,
            success_key TEXT DEFAULT '',
            pitfall TEXT DEFAULT '',
            tags_tool TEXT DEFAULT 'general',
            tags_domain TEXT DEFAULT 'general',
            tags_action TEXT DEFAULT 'general',
            tags_keywords TEXT DEFAULT '',
            weight REAL DEFAULT 1.0,
            use_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            last_used TEXT DEFAULT (datetime('now', 'localtime')),
            status TEXT DEFAULT 'active',
            project_key TEXT NOT NULL DEFAULT ''
        );
        """
        with sqlite3.connect(db) as c:
            c.executescript(schema)
            c.execute("INSERT INTO memories(goal,approach,outcome,weight,last_used) VALUES('历史记录','步骤','结果',.41,'2025-01-01')")
        return db

    def test_migration_baselines_existing_weight_and_new_rows(self):
        db = self.old_database()
        c = engine.get_conn(str(db))
        try:
            row = c.execute('SELECT * FROM memories').fetchone()
            self.assertEqual(row['weight'], .41)
            self.assertIsNotNone(row['decay_cursor_at'])
            self.assertEqual(engine.decay_all(db_conn=c), {'decayed': 0, 'archived': 0})
            mid = engine.store('旧库上的新记录', '步骤', '结果', db_conn=c)['memory_id']
            self.assertIsNotNone(c.execute('SELECT decay_cursor_at FROM memories WHERE id=?', (mid,)).fetchone()[0])
        finally:
            c.close()

    def test_optional_numeric_difference_is_preserved(self):
        mid = self.store('并发配置', approach='并发线程数量设为2', success_key='并发线程数量设为8',
                         pitfall='同步屏障遗漏会死锁，补齐同步屏障后恢复')
        self.assertEqual(self.row(mid)['success_key'], '并发线程数量设为8')
        self.assertIn('同步屏障', self.row(mid)['pitfall'])

    def test_text_match_beats_high_weight_coarse_tag(self):
        wrong = self.store('Excel 人事资料管理', approach='Excel 管理员工档案')
        target = self.store('修复 pivotcache', approach='清除工作簿缓存')
        self.conn.execute('UPDATE memories SET weight=3 WHERE id=?', (wrong,))
        self.conn.commit()
        results = engine.recall('Excel pivotcache', limit=1, db_conn=self.conn, project_key='fixture')
        self.assertEqual([r['id'] for r in results], [target])

    def test_long_query_keeps_tail_term(self):
        target = self.store('月桂酸实验条件', approach='记录溶液处理方法')
        result = engine.recall('请帮我查找一下以前关于相关实验处理方法和条件的记忆，现在需要了解月桂酸',
                               db_conn=self.conn, project_key='fixture')
        self.assertIn(target, [r['id'] for r in result])
        self.assertIn(engine.query_terms('月桂酸')[0], engine.query_terms('请帮我查找相关实验条件关于月桂酸'))

    def test_old_optional_chinese_field_is_reachable_without_retag(self):
        mid = self.store('历史条目', pitfall='同步屏障遗漏会死锁，补齐同步屏障后恢复')
        self.conn.execute("UPDATE memories SET tags_keywords='' WHERE id=?", (mid,))
        self.conn.commit()
        self.assertEqual(engine.recall('同步屏障', db_conn=self.conn, project_key='fixture')[0]['id'], mid)

    def test_explain_matches_returned_ids_and_preserves_isolation(self):
        target = self.store('pivotcache 查询')
        other = self.store('pivotcache 隔离条目', project_key='other')
        before = self.row(target)
        result = engine.recall_with_explain('pivotcache', db_conn=self.conn, project_key='fixture')
        self.assertEqual([r['id'] for r in result['memories']], [target])
        self.assertEqual([r['id'] for r in result['explanation']['results']], [target])
        self.assertNotIn(other, [r['id'] for r in result['memories']])
        self.assertEqual(self.row(target), before)
        engine.recall('pivotcache', db_conn=self.conn, project_key='fixture', reinforce=True)
        self.assertEqual(self.row(target)['use_count'], before['use_count'] + 1)
        self.assertEqual(self.row(other)['use_count'], 0)

    def test_cancel_task_is_distinct_from_cancel_feature(self):
        def conv(text, role='user', outcome='完成'):
            return {'turns': [{'role': role, 'content': text}, {'role': 'assistant', 'content': '执行'},
                               {'role': 'user', 'content': '好了'}], 'outcome': outcome,
                    'has_code': True, 'tool_calls': 2}
        self.assertTrue(memory_filter.rule_filter(**conv('实现取消订阅按钮'))[0])
        self.assertTrue(memory_filter.rule_filter(**conv('取消这个任务', 'assistant'))[0])
        self.assertFalse(memory_filter.rule_filter(**conv('不用做了'))[0])
        self.assertFalse(memory_filter.rule_filter(**conv('取消这个任务'))[0])
        self.assertTrue(memory_filter.rule_filter(**conv('实现功能', outcome='取消订阅功能已完成'))[0])
        self.assertFalse(memory_filter.rule_filter(**conv('实现功能', outcome='已取消'))[0])

    def test_update_preserves_metadata_and_updates_fts(self):
        mid = self.store('原目标')
        before = self.row(mid)
        result = engine.update_memory(mid, goal='mmemory 新目标 pivotcache', pitfall='新屏障问题通过同步解决',
                                      db_conn=self.conn, project_key='fixture')
        self.assertTrue(result['updated'])
        after = self.row(mid)
        for key in ('project_key', 'weight', 'use_count', 'created_at', 'last_used', 'decay_cursor_at'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(engine._recall_by_fts(self.conn, 'pivotcache', 5, 'fixture')[0]['id'], mid)
        self.assertFalse(engine.update_memory(mid, outcome='不可改', project_key='wrong', db_conn=self.conn)['updated'])
        self.assertFalse(engine.update_memory(mid, goal=' ', db_conn=self.conn)['updated'])

    def test_update_duplicate_rejects_without_reinforcement(self):
        a, b = self.store('已有目标'), self.store('另一个目标')
        before = [self.row(a), self.row(b)]
        self.assertFalse(engine.update_memory(b, goal='已有目标', db_conn=self.conn)['updated'])
        self.assertEqual([self.row(a), self.row(b)], before)

    def test_update_caller_transaction_and_failed_trigger(self):
        mid = self.store('更新事务')
        self.conn.execute('BEGIN')
        self.conn.execute("UPDATE memories SET outcome='调用方数据' WHERE id=?", (mid,))
        self.conn.execute("CREATE TEMP TRIGGER fail_update BEFORE UPDATE OF goal ON memories BEGIN SELECT RAISE(ABORT,'blocked'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            engine.update_memory(mid, goal='新目标', db_conn=self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.row(mid)['outcome'], '调用方数据')
        self.conn.rollback()
        self.assertEqual(self.row(mid)['outcome'], '完成合成任务')

    def test_backup_includes_wal_and_refuses_overwrite(self):
        self.store('WAL 中已提交的记录')
        path = Path(self.directory.name) / 'backup.db'
        engine.backup_database(str(path), str(self.db))
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM memories').fetchone()[0], 1)
        with self.assertRaises(FileExistsError):
            engine.backup_database(str(path), str(self.db))

    def test_backup_does_not_migrate_source(self):
        db = self.old_database()
        engine.backup_database(str(Path(self.directory.name) / 'old-backup.db'), str(db))
        with sqlite3.connect(db) as c:
            self.assertNotIn('decay_cursor_at', [row[1] for row in c.execute('PRAGMA table_info(memories)')])

    def test_merge_keeps_history_and_resets_decay_baseline(self):
        for i in range(3):
            mid = self.store(f'独立任务{i}')
            self.conn.execute('UPDATE memories SET created_at=?,last_used=?,use_count=? WHERE id=?',
                              (f'2026-01-0{i+1}', f'2026-02-0{i+1}', i+1, mid))
        self.conn.commit()
        group = dream.find_merge_groups(self.conn)[0]
        prompt = dream.build_merge_prompt(group)
        self.assertIn('独立任务必须保留', prompt)
        self.assertIn('low_specificity', group)
        dream.apply_merge(group, [{'goal':'经人工判断合并', 'approach':'保留信息', 'outcome':'完成'}], self.conn)
        rows = self.conn.execute("SELECT * FROM memories WHERE status='active'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM memories WHERE status='archived'").fetchone()[0], 3)
        self.assertEqual(rows[0]['created_at'], '2026-01-01')
        self.assertEqual(rows[0]['last_used'], '2026-02-03')
        self.assertEqual(rows[0]['use_count'], 6)
        self.assertIsNotNone(rows[0]['decay_cursor_at'])

    def test_cli_commands_use_supplied_database_and_exit_status(self):
        env = dict(os.environ, MMEMORY_DB=str(self.db))
        def cli(*arguments):
            return subprocess.run([sys.executable, 'cli.py', *arguments], env=env,
                                  text=True, capture_output=True)
        inserted = cli('store', '--goal', 'CLI 合成目标', '--approach', '合成步骤',
                       '--outcome', '合成结果', '--project-key', 'fixture')
        self.assertEqual(inserted.returncode, 0, inserted.stderr)
        mid = json.loads(inserted.stdout)['memory_id']
        updated = cli('update', str(mid), '--outcome', '已修订')
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertEqual(self.row(mid)['outcome'], '已修订')
        rejected = cli('update', str(mid), '--outcome', '错误项目', '--project-key', 'wrong')
        self.assertEqual(rejected.returncode, 1)
        explained = cli('recall', 'CLI', '--project-key', 'fixture', '--explain')
        self.assertEqual(explained.returncode, 0, explained.stderr)
        self.assertIn('query_terms', explained.stdout)
        backup = cli('backup', '--output', str(Path(self.directory.name) / 'cli-backup.db'))
        self.assertEqual(backup.returncode, 0, backup.stderr)
        before = self.row(mid)
        preview = cli('decay')
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(self.row(mid), before)

    def test_stale_merge_rejects_explicit_content_revision(self):
        for i in range(3):
            self.store(f'合并候选{i}')
        group = dream.find_merge_groups(self.conn)[0]
        mid = group['memories'][0]['id']
        self.assertTrue(engine.update_memory(mid, pitfall='新经验通过新解法恢复', db_conn=self.conn)['updated'])
        before = [dict(row) for row in self.conn.execute('SELECT * FROM memories ORDER BY id')]
        dream.apply_merge(group, [{'goal':'旧快照结果','approach':'旧步骤','outcome':'旧结果'}], self.conn)
        self.assertEqual([dict(row) for row in self.conn.execute('SELECT * FROM memories ORDER BY id')], before)

    def test_stale_merge_rejects_project_correction_in_caller_transaction(self):
        for i in range(3):
            self.store(f'项目归属候选{i}')
        group = dream.find_merge_groups(self.conn)[0]
        mid = group['memories'][0]['id']
        self.conn.execute("UPDATE memories SET project_key='corrected' WHERE id=?", (mid,))
        before = [dict(row) for row in self.conn.execute('SELECT * FROM memories ORDER BY id')]
        dream.apply_merge(group, [{'goal':'旧归属结果','approach':'旧步骤','outcome':'旧结果'}], self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual([dict(row) for row in self.conn.execute('SELECT * FROM memories ORDER BY id')], before)
        self.conn.rollback()
        self.assertEqual(self.row(mid)['project_key'], 'fixture')

    def test_merge_inherits_live_usage_without_invalidating_snapshot(self):
        mids = [self.store(f'使用元数据候选{i}') for i in range(3)]
        group = dream.find_merge_groups(self.conn)[0]
        engine._batch_reinforce(self.conn, mids[:2])
        self.conn.execute("UPDATE memories SET tags_keywords='live_keyword' WHERE id=?", (mids[0],))
        self.conn.commit()
        dream.apply_merge(group, [{'goal':'维护合并','approach':'当前步骤','outcome':'当前结果'}], self.conn)
        rows = self.conn.execute("SELECT * FROM memories WHERE status='active'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['use_count'], 2)
        self.assertEqual(rows[0]['weight'], 1.1)
        self.assertNotIn('live_keyword', rows[0]['tags_keywords'])
        self.assertEqual(rows[0]['tags_keywords'], engine.generate_tags('维护合并', '当前步骤', '当前结果')['tags_keywords'])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM memories WHERE status='archived'").fetchone()[0], 3)

    def test_project_signals_and_explicit_shared_bucket(self):
        self.assertEqual(engine.resolve_project_key('mmemory 查询优化', None), 'mmemory')
        self.assertEqual(engine.resolve_project_key('example-service 配置', None),
                         'example-service')
        self.assertEqual(engine.resolve_project_key('mmemory 查询优化', ''), '')


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AuditRegressions))
    if result.wasSuccessful():
        print(f'TESTS PASSED: {result.testsRun}')
    raise SystemExit(0 if result.wasSuccessful() else 1)
