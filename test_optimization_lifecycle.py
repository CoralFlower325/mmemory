"""Protection and provenance regressions; every database is newly synthetic."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import engine


class LifecycleOptimization(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mmemory-protection-')
        self.path = Path(self.directory.name) / 'synthetic.db'
        self.conn = engine.get_conn(str(self.path))

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def store(self, goal, project='fixture'):
        return engine.store(goal, '合成方法', '合成结果', project_key=project,
                            db_conn=self.conn)['memory_id']

    def row(self, mid):
        return dict(self.conn.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone())

    def rows(self):
        return [dict(row) for row in self.conn.execute('SELECT * FROM memories ORDER BY id')]

    def test_existing_schema_upgrade_preserves_rows_and_column_order(self):
        path = Path(self.directory.name) / 'legacy.db'
        body = engine.SCHEMA_PATH.read_text().split('CREATE TABLE IF NOT EXISTS memories (', 1)[1].split('\n);', 1)[0]
        lines = [line for line in body.splitlines()
                 if not line.strip().startswith(('protected ', 'protection_reason ', '--'))]
        body = '\n'.join(lines).rstrip().rstrip(',')
        with sqlite3.connect(path) as legacy:
            legacy.execute('CREATE TABLE memories (' + body + ')')
            legacy.execute("""INSERT INTO memories(goal,approach,outcome,weight,use_count,last_used,decay_cursor_at)
                VALUES('历史合成经验','合成方法','合成结果',.41,7,'2025-01-01','2026-01-01')""")
        upgraded = engine.get_conn(str(path))
        try:
            row = dict(upgraded.execute('SELECT * FROM memories').fetchone())
            self.assertEqual(row['protected'], 0)
            self.assertEqual(row['protection_reason'], '')
            self.assertEqual(row['weight'], .41)
            self.assertEqual(row['use_count'], 7)
            self.assertEqual(row['last_used'], '2025-01-01')
            self.assertEqual(row['decay_cursor_at'], '2026-01-01')
            fresh_columns = [row[1] for row in self.conn.execute('PRAGMA table_info(memories)')]
            upgraded_columns = [row[1] for row in upgraded.execute('PRAGMA table_info(memories)')]
            self.assertEqual(fresh_columns, upgraded_columns)
            self.assertEqual(upgraded.execute('SELECT count(*) FROM memory_relations').fetchone()[0], 0)
            self.assertFalse(upgraded.in_transaction)
        finally:
            upgraded.close()

    def test_protection_preserves_content_and_use_metadata(self):
        ids = [self.store(f'重要合成经验 {index}') for index in range(2)]
        before = self.rows()
        result = engine.set_memory_protection(ids, True, '明确的长期偏好', db_conn=self.conn)
        self.assertTrue(result['changed'])
        self.assertEqual(result['memory_ids'], ids)
        for old, current in zip(before, self.rows()):
            for field in old:
                if field not in ('protected', 'protection_reason'):
                    self.assertEqual(old[field], current[field], field)
            self.assertEqual(current['protected'], 1)
            self.assertEqual(current['protection_reason'], '明确的长期偏好')
        after = self.rows()
        engine.set_memory_protection(ids, True, '明确的长期偏好', db_conn=self.conn)
        self.assertEqual(after, self.rows())

    def test_protection_scope_failure_keeps_the_whole_batch_unchanged(self):
        first = self.store('本项目偏好')
        other = self.store('其他项目偏好', project='other')
        before = self.rows()
        for ids in ([first, other], [first, 9999]):
            result = engine.set_memory_protection(ids, True, '合成理由', project_key='fixture', db_conn=self.conn)
            self.assertFalse(result['changed'])
            self.assertEqual(before, self.rows())
            self.assertFalse(self.conn.in_transaction)

    def test_enabling_protection_requires_an_explicit_reason(self):
        mid = self.store('保护理由核对')
        before = self.row(mid)
        for reason in ('', '   ', '\u200b'):
            result = engine.set_memory_protection([mid], True, reason, db_conn=self.conn)
            self.assertFalse(result['changed'])
            self.assertEqual(before, self.row(mid))
            self.assertFalse(self.conn.in_transaction)

    def test_protection_failure_preserves_caller_transaction(self):
        ids = [self.store(f'保护原子性经验 {index}') for index in range(2)]
        self.conn.execute('UPDATE memories SET outcome=? WHERE id=?', ('调用方未提交修改', ids[0]))
        self.conn.execute(f"""CREATE TEMP TRIGGER reject_protection BEFORE UPDATE OF protected ON memories
            WHEN new.id={ids[1]} BEGIN SELECT RAISE(ABORT,'blocked protection'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            engine.set_memory_protection(ids, True, '保护理由', db_conn=self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.row(ids[0])['outcome'], '调用方未提交修改')
        self.assertEqual([self.row(mid)['protected'] for mid in ids], [0, 0])
        self.conn.rollback()
        self.assertEqual(self.row(ids[0])['outcome'], '合成结果')

    def test_caller_can_roll_back_a_successful_protection(self):
        mid = self.store('可回滚保护')
        self.conn.execute('BEGIN IMMEDIATE')
        engine.set_memory_protection([mid], True, '调用方保护', db_conn=self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.row(mid)['protected'], 1)
        self.conn.rollback()
        self.assertEqual(self.row(mid)['protected'], 0)

    def test_decay_never_changes_a_protected_record(self):
        protected = self.store('重要长期偏好')
        normal = self.store('普通临时经验')
        old_cursor = (datetime.now() - timedelta(days=200)).isoformat()
        self.conn.execute('UPDATE memories SET decay_cursor_at=?', (old_cursor,))
        self.conn.commit()
        engine.set_memory_protection([protected], True, '明确长期保留', db_conn=self.conn)
        before = self.row(protected)
        self.assertEqual(engine.decay_all(db_conn=self.conn, dry_run=True), {'decayed': 0, 'archived': 1})
        self.assertEqual(engine.decay_all(db_conn=self.conn), {'decayed': 0, 'archived': 1})
        self.assertEqual(before, self.row(protected))
        self.assertEqual(self.row(normal)['status'], 'archived')

    def test_unprotect_restarts_only_previously_protected_cursors(self):
        protected = self.store('解除保护偏好')
        normal = self.store('原本未保护经验')
        self.conn.execute("UPDATE memories SET decay_cursor_at='2026-01-01',last_used='2025-01-01'")
        self.conn.commit()
        engine.set_memory_protection([protected], True, '保护理由', db_conn=self.conn)
        before = self.row(protected)
        engine.set_memory_protection([protected, normal], False, db_conn=self.conn)
        current = self.row(protected)
        self.assertNotEqual(current['decay_cursor_at'], '2026-01-01')
        self.assertEqual(current['last_used'], before['last_used'])
        self.assertEqual(current['weight'], before['weight'])
        self.assertEqual(current['use_count'], before['use_count'])
        self.assertEqual(self.row(normal)['decay_cursor_at'], '2026-01-01')
        frozen = self.rows()
        engine.set_memory_protection([protected, normal], False, db_conn=self.conn)
        self.assertEqual(frozen, self.rows())

    def test_manual_archive_and_restore_preserve_protection(self):
        mid = self.store('人工归档的重要经验')
        engine.set_memory_protection([mid], True, '原有长期保护理由', db_conn=self.conn)
        self.assertTrue(engine.archive_memories([mid], '人工确认已过期', db_conn=self.conn)['archived'])
        self.assertEqual(self.row(mid)['protected'], 1)
        self.assertTrue(engine.restore_memories([mid], db_conn=self.conn)['restored'])
        self.assertEqual(self.row(mid)['protected'], 1)
        self.assertEqual(self.row(mid)['protection_reason'], '原有长期保护理由')

    def test_protected_milestone_is_not_automatically_archived(self):
        old = self.store('完成数据库迁移')
        engine.set_memory_protection([old], True, '迁移经验必须保留', db_conn=self.conn)
        before = self.row(old)
        result = engine.store('完成数据库迁移并交付', '合成方法', '合成结果',
                              project_key='fixture', db_conn=self.conn)
        self.assertTrue(result['stored'])
        self.assertEqual(before, self.row(old))
        self.assertEqual(self.row(result['memory_id'])['status'], 'active')
        self.assertEqual(engine.list_relations(old, db_conn=self.conn), [])
        self.assertTrue(any('保护' in warning for warning in result['warnings']))

    def test_automatic_supersession_records_source_and_target_atomically(self):
        old = self.store('完成数据库迁移')
        result = engine.store('完成数据库迁移并交付', '合成方法', '合成结果',
                              project_key='fixture', db_conn=self.conn)
        new = result['memory_id']
        self.assertTrue(result['stored'])
        self.assertEqual(self.row(old)['status'], 'archived')
        relations = engine.list_relations(old, db_conn=self.conn)
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]['source_id'], new)
        self.assertEqual(relations[0]['target_id'], old)
        self.assertEqual(relations[0]['relation'], 'supersedes')
        self.assertTrue(relations[0]['reason'])

    def test_relation_failure_rolls_back_new_milestone_and_source_archive(self):
        old = self.store('完成数据库迁移')
        before = self.rows()
        self.conn.execute("CREATE TEMP TRIGGER reject_relation BEFORE INSERT ON memory_relations BEGIN SELECT RAISE(ABORT,'blocked relation'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            engine.store('完成数据库迁移并交付', '合成方法', '合成结果',
                         project_key='fixture', db_conn=self.conn)
        self.assertEqual(before, self.rows())
        self.assertEqual(engine.list_relations(old, db_conn=self.conn), [])
        self.assertFalse(self.conn.in_transaction)

    def test_explicit_relations_preserve_states_and_allow_shared_bucket(self):
        source = self.store('基于旧经验的新结论', project='')
        target = self.store('旧经验的证据记录', project='')
        engine.archive_memories([target], '保留历史证据', db_conn=self.conn)
        before = self.rows()
        result = engine.link_memories(source, target, 'derived_from', '根据旧经验原文核对',
                                      project_key='', db_conn=self.conn)
        self.assertTrue(result['linked'])
        self.assertEqual(before, self.rows())
        relations = engine.list_relations(target, db_conn=self.conn)
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations, engine.list_relations(source, db_conn=self.conn))
        self.assertEqual(relations[0]['relation'], 'derived_from')

    def test_relation_rejections_leave_no_transaction_or_metadata_changes(self):
        source = self.store('新事实')
        target = self.store('旧事实')
        other = self.store('其他项目事实', project='other')
        before = self.rows()
        arguments = [
            (source, other, 'supersedes', '不能跨项目取代', {}),
            (source, other, 'derived_from', '不能跨项目引用', {}),
            (source, target, 'supersedes', ' ', {}),
            (source, target, 'unknown', '无效关系', {}),
            (source, source, 'derived_from', '不能自引用', {}),
            (source, 9999, 'derived_from', '缺失来源', {}),
            (source, target, 'supersedes', '项目限制不匹配', {'project_key': 'other'}),
        ]
        for first, second, kind, reason, options in arguments:
            result = engine.link_memories(first, second, kind, reason, db_conn=self.conn, **options)
            self.assertFalse(result['linked'], result)
            self.assertEqual(before, self.rows())
            self.assertFalse(self.conn.in_transaction)
        self.assertEqual(engine.list_relations(source, db_conn=self.conn), [])

    def test_duplicate_relation_updates_only_reason_and_preserves_creation_time(self):
        source, target = self.store('当前结论'), self.store('原始证据')
        engine.link_memories(source, target, 'derived_from', '第一次核对理由', db_conn=self.conn)
        self.conn.execute("UPDATE memory_relations SET created_at='2026-01-01'")
        self.conn.commit()
        result = engine.link_memories(source, target, 'derived_from', '补充了明确证据', db_conn=self.conn)
        self.assertTrue(result['linked'])
        relations = engine.list_relations(source, db_conn=self.conn)
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]['reason'], '补充了明确证据')
        self.assertEqual(relations[0]['created_at'], '2026-01-01')

    def test_relation_uses_caller_transaction_and_savepoint(self):
        source, target = self.store('派生结论'), self.store('原始证据')
        self.conn.execute('BEGIN IMMEDIATE')
        engine.link_memories(source, target, 'derived_from', '合成证据', db_conn=self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(len(engine.list_relations(source, db_conn=self.conn)), 1)
        self.conn.rollback()
        self.assertEqual(engine.list_relations(source, db_conn=self.conn), [])
        self.conn.execute('UPDATE memories SET outcome=? WHERE id=?', ('调用方未提交修改', target))
        self.conn.execute("CREATE TEMP TRIGGER reject_relation BEFORE INSERT ON memory_relations BEGIN SELECT RAISE(ABORT,'blocked relation'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            engine.link_memories(source, target, 'derived_from', '合成证据', db_conn=self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.row(target)['outcome'], '调用方未提交修改')
        self.assertEqual(engine.list_relations(source, db_conn=self.conn), [])
        self.conn.rollback()


if __name__ == '__main__':
    unittest.main(verbosity=2)
