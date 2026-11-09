"""Archive, recall and decay regressions using only newly constructed data."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import dream
import engine


class ArchiveReaudit(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mmemory-archive-test-')
        self.path = Path(self.directory.name) / 'synthetic.db'
        self.conn = engine.get_conn(str(self.path))

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def store(self, goal='合成独立经验', project='fixture', **fields):
        return engine.store(goal, fields.pop('approach', '合成步骤'),
                            fields.pop('outcome', '合成结果'), project_key=project,
                            db_conn=self.conn, **fields)['memory_id']

    def row(self, mid):
        return dict(self.conn.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone())

    def test_manual_archive_preserves_history_and_excludes_default_consumers(self):
        mids = [self.store(f'合成经验{i}', pitfall=f'独立问题{i}，对应方案修复') for i in range(3)]
        self.conn.execute("UPDATE memories SET weight=1.6,use_count=7,last_used='2026-01-02',decay_cursor_at='2026-01-03'")
        self.conn.commit()
        before = {mid:self.row(mid) for mid in mids}
        result = engine.archive_memories(mids, '明确过时', project_key='fixture', db_conn=self.conn)
        self.assertTrue(result['archived'])
        self.assertFalse(self.conn.in_transaction)
        for mid in mids:
            current = self.row(mid)
            self.assertEqual(current['status'], 'archived')
            self.assertEqual(current['archive_reason'], '明确过时')
            self.assertTrue(current['archived_at'])
            for key in before[mid]:
                if key not in ('status', 'archive_reason', 'archived_at', 'revision'):
                    self.assertEqual(current[key], before[mid][key], key)
        self.assertEqual(engine.recall('合成经验', db_conn=self.conn, project_key='fixture'), [])
        self.assertEqual(engine.list_all(db_conn=self.conn, project_key='fixture'), [])
        self.assertEqual(len(engine.list_all(status='all', db_conn=self.conn)), 3)
        self.assertEqual(dream.find_merge_groups(self.conn, project_key='fixture'), [])
        stats = engine.get_stats(self.conn)
        self.assertEqual({key: stats[key] for key in ('active', 'archived', 'total', 'projects')},
                         {'active':0,'archived':3,'total':3,'projects':1})
        self.assertFalse(engine.update_memory(mids[0], outcome='覆盖历史', db_conn=self.conn)['updated'])

    def test_restore_keeps_original_id_and_excludes_archived_time_from_decay(self):
        mid = self.store()
        self.conn.execute("UPDATE memories SET weight=.9,use_count=3,last_used='2025-01-01',decay_cursor_at='2025-01-01' WHERE id=?", (mid,))
        self.conn.commit()
        original = self.row(mid)
        engine.archive_memories([mid], '合成历史', db_conn=self.conn)
        self.assertTrue(engine.restore_memories([mid], db_conn=self.conn)['restored'])
        restored = self.row(mid)
        for field in original:
            if field not in ('decay_cursor_at', 'archived_at', 'archive_reason', 'revision'):
                self.assertEqual(original[field], restored[field], field)
        self.assertNotEqual(restored['decay_cursor_at'], '2025-01-01')
        self.assertEqual(engine.decay_all(db_conn=self.conn), {'decayed':0,'archived':0})
        self.assertEqual(self.row(mid)['weight'], .9)

    def test_invalid_id_or_cross_project_batch_changes_nothing(self):
        first, second = self.store(), self.store('别项目记录', project='other')
        before = [self.row(first), self.row(second)]
        for ids, project in [([first, second], 'fixture'), ([first, 9999], None)]:
            self.assertFalse(engine.archive_memories(ids, '合成理由', project_key=project, db_conn=self.conn)['archived'])
            self.assertEqual([self.row(first), self.row(second)], before)
            self.assertFalse(self.conn.in_transaction)
        self.assertFalse(engine.archive_memories([first], '', db_conn=self.conn)['archived'])

    def test_restore_conflict_rejects_the_whole_batch(self):
        first, second = self.store('重复目标'), self.store('其他历史')
        engine.archive_memories([first, second], '历史', db_conn=self.conn)
        self.store('重复目标')
        self.assertFalse(engine.restore_memories([first, second], db_conn=self.conn)['restored'])
        self.assertEqual(self.row(second)['status'], 'archived')
        self.assertFalse(self.conn.in_transaction)

    def test_same_goal_archived_batch_cannot_restore_two_active_copies(self):
        first = self.store('重复目标')
        engine.archive_memories([first], '旧版本', db_conn=self.conn)
        second = self.store('重复目标')
        engine.archive_memories([second], '另一版本', db_conn=self.conn)
        self.assertFalse(engine.restore_memories([first, second], db_conn=self.conn)['restored'])
        self.assertEqual(engine.list_all(db_conn=self.conn), [])

    def test_caller_transaction_remains_owned_by_caller(self):
        mid = self.store()
        before = self.row(mid)
        self.conn.execute('BEGIN IMMEDIATE')
        engine.archive_memories([mid], '调用方批量操作', db_conn=self.conn)
        self.assertTrue(self.conn.in_transaction)
        with sqlite3.connect(self.path) as observer:
            self.assertEqual(observer.execute('SELECT status FROM memories WHERE id=?', (mid,)).fetchone()[0], 'active')
        self.conn.rollback()
        self.assertEqual(self.row(mid), before)

    def test_archive_failure_rolls_back_only_its_own_changes(self):
        first, second, outside = self.store('第一个'), self.store('第二个'), self.store('调用方')
        self.conn.execute("CREATE TEMP TRIGGER reject_archive BEFORE UPDATE OF status ON memories WHEN new.id=%d BEGIN SELECT RAISE(ABORT,'archive rejected'); END" % second)
        self.conn.execute('UPDATE memories SET outcome=? WHERE id=?', ('未提交的调用方修改', outside))
        with self.assertRaises(sqlite3.IntegrityError):
            engine.archive_memories([first, second], '合成失败', db_conn=self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.row(first)['status'], 'active')
        self.assertEqual(self.row(outside)['outcome'], '未提交的调用方修改')
        self.conn.rollback()

    def test_completion_upgrade_archives_independent_old_experience(self):
        old = self.store('完成 mmemory 召回修订', pitfall='独立锁问题，写前加锁解决')
        original = self.row(old)
        new = self.store('完成 mmemory 召回修订并交付')
        self.assertEqual(self.row(old)['status'], 'archived')
        self.assertEqual(self.row(old)['pitfall'], original['pitfall'])
        self.assertEqual(self.row(new)['pitfall'], '')
        self.assertEqual([r['id'] for r in engine.list_all(db_conn=self.conn)], [new])

    def test_archived_rows_cannot_be_reinforced_by_stale_recall_ids(self):
        mid = self.store()
        engine.archive_memories([mid], '合成归档', db_conn=self.conn)
        before = self.row(mid)
        engine._batch_reinforce(self.conn, [mid])
        engine._reinforce_inline(self.conn, mid)
        self.conn.commit()
        self.assertEqual(self.row(mid), before)

    def test_decay_archives_original_fields_and_own_failure_cleans_transaction(self):
        mid = self.store(pitfall='必须保留的问题与解决方法')
        self.conn.execute("UPDATE memories SET weight=.3,last_used='2025-01-01',decay_cursor_at='2025-01-01' WHERE id=?", (mid,))
        self.conn.commit()
        before = self.row(mid)
        self.conn.execute("CREATE TEMP TRIGGER reject_decay BEFORE UPDATE OF status ON memories BEGIN SELECT RAISE(ABORT,'decay rejected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            engine.decay_all(db_conn=self.conn)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(before, self.row(mid))
        self.conn.execute('DROP TRIGGER reject_decay')
        self.assertEqual(engine.decay_all(db_conn=self.conn), {'decayed':0,'archived':1})
        for field in before:
            if field not in ('status','archived_at','archive_reason','weight',
                             'decay_cursor_at','revision'):
                self.assertEqual(before[field], self.row(mid)[field], field)
        self.assertLess(self.row(mid)['weight'], before['weight'])

    def test_decay_lock_covers_read_before_cross_process_reinforcement(self):
        mid = self.store()
        self.conn.execute("UPDATE memories SET decay_cursor_at='2025-01-01' WHERE id=?", (mid,))
        self.conn.commit()
        observations, errors = [], []
        program = '''import sqlite3,sys,json
import engine
c=sqlite3.connect(sys.argv[1],timeout=.1)
try:
 engine._reinforce_inline(c,int(sys.argv[2]));c.commit()
 print(json.dumps({'locked':False}))
except sqlite3.OperationalError as e:
 print(json.dumps({'locked':'locked' in str(e).lower()}))
c.close()
'''
        def trace(sql):
            if ('SELECT id, weight, use_count, last_used, decay_cursor_at' in sql
                    and 'FROM memories' in sql):
                child = subprocess.run([sys.executable,'-c',program,str(self.path),str(mid)],
                                       text=True,capture_output=True,timeout=10)
                if child.returncode:
                    errors.append(child.stderr)
                else:
                    observations.append(json.loads(child.stdout))
        self.conn.set_trace_callback(trace)
        try:
            engine.decay_all(db_conn=self.conn)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(errors, [])
        self.assertEqual(observations, [{'locked':True}])
        self.assertEqual(self.row(mid)['status'], 'archived')

    def test_numeric_chapters_preserve_order_in_recall(self):
        correct = self.store('完成第1章第2节校对')
        self.store('完成第2章第1节校对')
        for query in ('第1章第2节', '完成第1章第2节校对'):
            self.assertEqual(engine.recall(query,limit=1,project_key='fixture',db_conn=self.conn)[0]['id'],correct)

    def test_candidate_fusion_has_same_first_result_for_different_limits(self):
        correct = self.store('mmemory pivot 经验', project='mmemory', pitfall='pivotcache 参数失败，刷新缓存解决')
        for index in range(60):
            self.store(f'mmemory 参数 {index}', project='mmemory')
        self.conn.execute('UPDATE memories SET weight=3,tags_keywords=? WHERE id<>?', ('参数',correct))
        self.conn.execute("UPDATE memories SET tags_keywords='' WHERE id=?", (correct,))
        self.conn.commit()
        for limit in (1,20):
            self.assertEqual(engine.recall('mmemory pivotcache 参数',limit=limit,db_conn=self.conn)[0]['id'],correct)

    def test_goal_owns_namespace_even_when_approach_mentions_another_project(self):
        result = engine.store('mmemory 审查命名空间', '检查错题本隔离', '已完成',db_conn=self.conn)
        self.assertEqual(self.row(result['memory_id'])['project_key'], 'mmemory')
        self.assertTrue(engine.recall('mmemory 审查命名空间',db_conn=self.conn))

    def test_fts_database_failures_are_not_hidden_as_empty_results(self):
        class Broken:
            def execute(self, *args):
                raise sqlite3.OperationalError('database disk image is malformed')
        with patch.object(engine, '_project_clause', return_value=('', [])):
            with self.assertRaises(sqlite3.OperationalError):
                engine._recall_by_fts(Broken(),'合成查询',5)

    def test_duplicate_with_new_content_explicitly_warns_it_was_not_merged(self):
        mid = self.store('同一持久事实', outcome='旧结论')
        result = engine.store('同一持久事实','合成步骤','相反的新结论',
                              project_key='fixture',db_conn=self.conn)
        self.assertFalse(result['stored'])
        self.assertEqual(result['memory_id'], mid)
        self.assertEqual(self.row(mid)['outcome'], '旧结论')
        self.assertTrue(any('outcome' in warning and f'update {mid}' in warning
                            for warning in result['warnings']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
