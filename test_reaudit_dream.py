"""Dream lifecycle regressions; all rows are newly constructed synthetic data."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dream
import engine


class DreamReaudit(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mmemory-dream-reaudit-')
        self.path = Path(self.directory.name) / 'synthetic.db'
        self.conn = engine.get_conn(str(self.path))
        self.ids = [engine.store(f'合成经验 {index}', '合成方法', '合成结果',
                                 project_key='fixture', db_conn=self.conn)['memory_id']
                    for index in range(3)]

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def rows(self, status=None):
        sql = 'SELECT * FROM memories'
        parameters = ()
        if status is not None:
            sql += ' WHERE status=?'
            parameters = (status,)
        return [dict(row) for row in self.conn.execute(sql + ' ORDER BY id', parameters)]

    def group(self):
        return dream.find_merge_groups(self.conn, project_key='fixture')[0]

    def output(self):
        return [{'goal': '合成合并结果', 'approach': '经过明确人工判断', 'outcome': '保留有效信息'}]

    def test_sources_are_archived_without_rewriting_their_history(self):
        for index, mid in enumerate(self.ids):
            self.conn.execute('''UPDATE memories SET created_at=?,last_used=?,use_count=?,weight=?,
                                 decay_cursor_at=?,pitfall=? WHERE id=?''',
                              (f'2026-01-0{index+1}', f'2026-02-0{index+1}', index+1,
                               1 + index * .1, f'2026-03-0{index+1}',
                               f'独立踩坑 {index}，使用对应方案修正', mid))
        self.conn.commit()
        before = self.rows()
        dream.apply_merge(self.group(), self.output(), self.conn)
        archived = self.rows('archived')
        self.assertEqual([row['id'] for row in archived], self.ids)
        for old, current in zip(before, archived):
            for field in old:
                if field not in ('status', 'archived_at', 'archive_reason'):
                    self.assertEqual(old[field], current[field], field)
            self.assertTrue(current['archived_at'])
            self.assertIn('Dream', current['archive_reason'])
        active = self.rows('active')
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['created_at'], '2026-01-01')
        self.assertEqual(active[0]['last_used'], '2026-02-03')
        self.assertEqual(active[0]['use_count'], 6)
        self.assertAlmostEqual(active[0]['weight'], 1.2)
        self.assertNotEqual(active[0]['decay_cursor_at'], '2026-03-03')
        self.assertFalse(self.conn.in_transaction)

    def test_each_output_gets_tags_from_its_own_five_fields(self):
        self.conn.execute("UPDATE memories SET tags_keywords='obsolete_source_keyword'")
        self.conn.commit()
        outputs = [
            {'goal': 'Selenium 网页采集', 'approach': '读取浏览器页面', 'outcome': '页面读取正常',
             'success_key': '只保留网页经验', 'pitfall': '元素等待失败，使用显式等待修正'},
            {'goal': 'pandas 工作簿统计', 'approach': '读取表格整理', 'outcome': '统计完成',
             'success_key': '只保留表格经验', 'pitfall': '数据缺项，先检查空值再计算'},
        ]
        dream.apply_merge(self.group(), outputs, self.conn)
        active = self.rows('active')
        self.assertEqual(len(active), 2)
        for row, output in zip(active, outputs):
            expected = engine.generate_tags(*(output.get(field, '') for field in engine.CONTENT_FIELDS))
            for field, value in expected.items():
                self.assertEqual(row[field], value, field)
            self.assertNotIn('obsolete_source_keyword', row['tags_keywords'])
            self.assertEqual(row['project_key'], 'fixture')

    def test_caller_can_roll_back_the_whole_merge(self):
        group = self.group()
        self.conn.execute('BEGIN IMMEDIATE')
        dream.apply_merge(group, self.output(), self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(len(self.rows('archived')), 3)
        with sqlite3.connect(self.path) as observer:
            self.assertEqual(observer.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0], 3)
        self.conn.rollback()
        self.assertEqual([row['id'] for row in self.rows('active')], self.ids)
        self.assertEqual(self.rows('archived'), [])
        self.assertEqual(len(self.rows()), 3)

    def test_failed_insert_undoes_archiving_and_preserves_caller_changes(self):
        outside = engine.store('调用方独立记录', '旧步骤', '旧结果',
                               project_key='outside', db_conn=self.conn)['memory_id']
        group = self.group()
        self.conn.execute('UPDATE memories SET outcome=? WHERE id=?', ('调用方未提交修改', outside))
        self.conn.execute("CREATE TEMP TRIGGER reject_merge BEFORE INSERT ON memories BEGIN SELECT RAISE(ABORT,'blocked merge'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            dream.apply_merge(group, self.output(), self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.rows('archived'), [])
        self.assertEqual(self.conn.execute('SELECT outcome FROM memories WHERE id=?', (outside,)).fetchone()[0], '调用方未提交修改')
        self.assertEqual(len(self.rows()), 4)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT outcome FROM memories WHERE id=?', (outside,)).fetchone()[0], '旧结果')

    def test_stale_source_returns_without_leaving_a_transaction(self):
        group = self.group()
        engine.update_memory(self.ids[0], pitfall='新经验，改用新方法修复', db_conn=self.conn)
        before = self.rows()
        dream.apply_merge(group, self.output(), self.conn)
        self.assertEqual(before, self.rows())
        self.assertFalse(self.conn.in_transaction)

    def test_group_must_match_every_source_project(self):
        group = self.group()
        group['project_key'] = 'wrong-project'
        before = self.rows()
        dream.apply_merge(group, self.output(), self.conn)
        self.assertEqual(before, self.rows())
        self.assertFalse(self.conn.in_transaction)
        group = self.group()
        self.conn.execute("UPDATE memories SET project_key='other-project' WHERE id=?", (self.ids[0],))
        self.conn.commit()
        corrected = dict(self.conn.execute('SELECT * FROM memories WHERE id=?', (self.ids[0],)).fetchone())
        group['memories'] = [corrected if row['id'] == corrected['id'] else row
                             for row in group['memories']]
        before = self.rows()
        dream.apply_merge(group, self.output(), self.conn)
        self.assertEqual(before, self.rows())
        self.assertFalse(self.conn.in_transaction)

    def test_archived_sources_cannot_be_merged_even_with_a_matching_snapshot(self):
        group = self.group()
        engine.archive_memories(self.ids, '合成归档', db_conn=self.conn)
        group['memories'] = self.rows()
        before = self.rows()
        dream.apply_merge(group, self.output(), self.conn)
        self.assertEqual(before, self.rows())
        self.assertFalse(self.conn.in_transaction)

    def test_archive_failure_leaves_no_savepoint_in_caller_transaction(self):
        group = self.group()
        self.conn.execute('BEGIN IMMEDIATE')
        with patch.object(engine, 'archive_memories', return_value={'archived': False, 'count': 0, 'memory_ids': [], 'reason': '合成失败'}):
            dream.apply_merge(group, self.output(), self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(len(self.rows('active')), 3)
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute('RELEASE mm_apply_merge')
        self.conn.rollback()

    def test_source_guard_is_covered_by_a_cross_process_write_lock(self):
        # Before the fix, SELECT's guard ran without a transaction and this writer succeeded.
        group = self.group()
        probe = []
        errors = []
        triggered = False
        program = """import json,sqlite3,sys
import engine
c=sqlite3.connect(sys.argv[1],timeout=.1)
c.row_factory=sqlite3.Row
try:
    result=engine.update_memory(int(sys.argv[2]),pitfall='另一进程新修订',db_conn=c)
except sqlite3.OperationalError as error:
    result={'locked':'locked' in str(error).lower(),'error':str(error)}
print(json.dumps(result))
c.close()
"""
        def trace(sql):
            nonlocal triggered
            if not triggered and sql.lstrip().startswith('SELECT * FROM memories WHERE id IN'):
                triggered = True
                try:
                    child = subprocess.run([sys.executable, '-c', program, str(self.path), str(self.ids[0])],
                                           cwd=Path(engine.__file__).parent, text=True,
                                           capture_output=True, timeout=10)
                    if child.returncode:
                        errors.append(child.stderr)
                    else:
                        probe.append(json.loads(child.stdout))
                except Exception as error:
                    errors.append(str(error))
        self.conn.set_trace_callback(trace)
        try:
            dream.apply_merge(group, self.output(), self.conn)
        finally:
            self.conn.set_trace_callback(None)
        self.assertTrue(triggered)
        self.assertEqual(errors, [])
        self.assertEqual(len(probe), 1)
        self.assertTrue(probe[0].get('locked'), probe)
        self.assertEqual(len(self.rows('archived')), 3)
        self.assertEqual(len(self.rows('active')), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
