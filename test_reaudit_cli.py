"""Public maintenance CLI regressions, with new synthetic temporary databases."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import engine


class CliReaudit(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mmemory-cli-reaudit-')
        self.path = Path(self.directory.name) / 'synthetic.db'
        self.conn = engine.get_conn(str(self.path))
        for mid, project, goal in [(101, 'fixture', '第一条归档经验'),
                                   (209, 'fixture', '第二条归档经验'),
                                   (303, 'other', '另一项目经验')]:
            self.conn.execute('''INSERT INTO memories(id,project_key,goal,approach,outcome)
                                 VALUES(?,?,?,?,?)''', (mid, project, goal, '合成方法', '合成结果'))
        self.conn.commit()
        self.environment = dict(os.environ, MMEMORY_DB=str(self.path))

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(Path(engine.__file__).with_name('cli.py')),
                               *(str(argument) for argument in arguments)],
                              cwd=Path(engine.__file__).parent, env=self.environment,
                              text=True, capture_output=True, timeout=20)

    def rows(self):
        return [dict(row) for row in self.conn.execute('SELECT * FROM memories ORDER BY id')]

    def row(self, mid):
        return dict(self.conn.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone())

    def test_bulk_archive_and_restore_report_actual_ids(self):
        before = self.rows()
        archived = self.cli('archive', 101, 209, '--reason', '旧阶段已结束，保留原始经验', '--project-key', 'fixture')
        self.assertEqual(archived.returncode, 0, archived.stderr)
        result = json.loads(archived.stdout)
        self.assertTrue(result['archived'])
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['memory_ids'], [101, 209])
        for mid in (101, 209):
            self.assertEqual(self.row(mid)['status'], 'archived')
            self.assertEqual(self.row(mid)['archive_reason'], '旧阶段已结束，保留原始经验')
        self.assertEqual(self.row(303), before[2])
        restored = self.cli('restore', 101, 209, '--project-key', 'fixture')
        self.assertEqual(restored.returncode, 0, restored.stderr)
        result = json.loads(restored.stdout)
        self.assertTrue(result['restored'])
        self.assertEqual(result['memory_ids'], [101, 209])
        for old, current in zip(before, self.rows()):
            for field in ('id', 'project_key', *engine.CONTENT_FIELDS, 'created_at', 'last_used', 'weight', 'use_count'):
                self.assertEqual(old[field], current[field], field)
            self.assertEqual(current['status'], 'active')

    def test_archive_requires_a_nonblank_reason(self):
        before = self.rows()
        missing = self.cli('archive', 101)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn('--reason', missing.stderr)
        blank = self.cli('archive', 101, '--reason', '   ')
        self.assertNotEqual(blank.returncode, 0)
        self.assertFalse(json.loads(blank.stdout)['archived'])
        self.assertEqual(before, self.rows())

    def test_scope_and_missing_id_fail_the_entire_batch(self):
        before = self.rows()
        for arguments in [
            ('archive', 101, 303, '--reason', '合成范围核对', '--project-key', 'fixture'),
            ('archive', 101, 99999, '--reason', '合成缺失 ID'),
        ]:
            failure = self.cli(*arguments)
            self.assertNotEqual(failure.returncode, 0)
            self.assertFalse(json.loads(failure.stdout)['archived'])
            self.assertEqual(before, self.rows())
        self.assertEqual(self.cli('archive', 101, '--reason', '合成归档').returncode, 0)
        before = self.rows()
        rejected = self.cli('restore', 101, '--project-key', 'other')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(json.loads(rejected.stdout)['restored'])
        self.assertEqual(before, self.rows())

    def test_restore_duplicate_conflict_returns_failure(self):
        self.assertEqual(self.cli('archive', 101, '--reason', '旧事实被替换').returncode, 0)
        inserted = self.cli('store', '--goal', '第一条归档经验', '--approach', '新合成方法',
                            '--outcome', '新合成结果', '--project-key', 'fixture')
        self.assertEqual(inserted.returncode, 0, inserted.stderr)
        before = self.rows()
        restored = self.cli('restore', 101)
        self.assertNotEqual(restored.returncode, 0)
        self.assertFalse(json.loads(restored.stdout)['restored'])
        self.assertEqual(before, self.rows())

    def test_lists_show_status_archive_reason_and_actual_ids(self):
        self.assertEqual(self.cli('archive', 101, '--reason', '该阶段结论已经过时').returncode, 0)
        active = self.cli('list', '--project-key', 'fixture')
        self.assertEqual(active.returncode, 0, active.stderr)
        self.assertIn('#209 ', active.stdout)
        self.assertNotIn('#101 ', active.stdout)
        self.assertNotIn('#303 ', active.stdout)
        archived = self.cli('list', '--status', 'archived', '--project-key', 'fixture')
        self.assertEqual(archived.returncode, 0, archived.stderr)
        self.assertIn('#101 ', archived.stdout)
        self.assertIn('状态：archived', archived.stdout)
        self.assertIn('归档原因：该阶段结论已经过时', archived.stdout)
        self.assertNotIn('方案：', archived.stdout)
        full = self.cli('list', '--status', 'archived', '--project-key', 'fixture', '--full')
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertIn('方案：', full.stdout)
        for label in ('权重：', '使用次数：', '创建时间：', '最后使用：', '衰减游标：'):
            self.assertIn(label, full.stdout)
        all_rows = self.cli('list', '--status', 'all', '--project-key', 'fixture')
        self.assertEqual(all_rows.returncode, 0, all_rows.stderr)
        self.assertIn('#101 ', all_rows.stdout)
        self.assertIn('#209 ', all_rows.stdout)
        self.assertNotIn('#303 ', all_rows.stdout)
        limited = self.cli('list', '--status', 'all', '--limit', 1)
        self.assertEqual(limited.returncode, 0, limited.stderr)
        self.assertEqual(sum(f'#{mid} ' in limited.stdout for mid in (101, 209, 303)), 1)
        invalid = self.cli('list', '--status', 'deleted')
        self.assertNotEqual(invalid.returncode, 0)

    def test_review_limit_errors_are_clean_json_without_traceback(self):
        result = self.cli('review', '--limit', 0)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('Traceback', result.stderr)
        payload = json.loads(result.stderr)
        self.assertFalse(payload['reviewed'])
        self.assertIn('limit', payload['reason'])

    def test_store_rejections_have_nonzero_exit_status(self):
        blank = self.cli('store', '--goal', '   ', '--approach', '合成方法', '--outcome', '合成结果')
        self.assertNotEqual(blank.returncode, 0)
        self.assertFalse(json.loads(blank.stdout)['stored'])
        duplicate = self.cli('store', '--goal', '第一条归档经验', '--approach', '合成方法',
                             '--outcome', '合成结果', '--project-key', 'fixture')
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertFalse(json.loads(duplicate.stdout)['stored'])
        self.assertEqual(json.loads(duplicate.stdout)['memory_id'], 101)

    def test_decay_preview_and_apply_report_archived_instead_of_deleted(self):
        self.conn.execute('UPDATE memories SET weight=.3,decay_cursor_at=? WHERE id=101',
                          ((datetime.now() - timedelta(days=60)).isoformat(),))
        self.conn.commit()
        before = self.rows()
        preview = self.cli('decay', '--project-key', 'fixture')
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn('可恢复归档', preview.stdout)
        self.assertIn('"archived": 1', preview.stdout)
        self.assertNotIn('"deleted"', preview.stdout)
        self.assertEqual(before, self.rows())
        applied = self.cli('decay', '--apply', '--project-key', 'fixture')
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout), {'decayed': 0, 'archived': 1})
        self.assertEqual(self.row(101)['status'], 'archived')
        for field in (*engine.CONTENT_FIELDS, 'id', 'last_used', 'use_count'):
            self.assertEqual(before[0][field], self.row(101)[field], field)
        self.assertLess(self.row(101)['weight'], before[0]['weight'])
        self.assertEqual(len(self.rows()), 3)
        help_text = self.cli('decay', '--help')
        self.assertEqual(help_text.returncode, 0)
        self.assertIn('可恢复归档', help_text.stdout)
        self.assertNotIn('不可恢复', help_text.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
