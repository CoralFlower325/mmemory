"""保护来源及合并追溯的原子性，均使用合成临时库。"""

import tempfile
import unittest
from pathlib import Path

import dream
import engine


class DreamProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mmemory-protected-dream-')
        self.conn = engine.get_conn(str(Path(self.temp.name) / 'synthetic.db'))
        self.ids = [engine.store(f'合成任务{i}', '实施方案', '结果已完成',
                                 project_key='synthetic', db_conn=self.conn)['memory_id']
                    for i in range(3)]
        self.group = {'project_key': 'synthetic', 'memories': [dict(row) for row in
                      self.conn.execute('SELECT * FROM memories ORDER BY id')]}
        self.output = [{'goal': '合成独立经验', 'approach': '整理来源', 'outcome': '已形成经验'}]

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_new_protection_after_candidate_blocks_merge_and_preserves_transaction(self):
        engine.set_memory_protection([self.ids[0]], True, '用户指定重要', db_conn=self.conn)
        before = list(self.conn.iterdump())
        result = dream.apply_merge(self.group, self.output, self.conn)
        self.assertFalse(result['merged'])
        self.assertEqual(before, list(self.conn.iterdump()))
        self.assertFalse(self.conn.in_transaction)

    def test_each_output_can_trace_every_source_with_originals_archived(self):
        outputs = self.output + [{'goal': '合成另一经验', 'approach': '整理另一事实', 'outcome': '完成'}]
        dream.apply_merge(self.group, outputs, self.conn)
        rows = engine.list_all(limit=None, db_conn=self.conn)
        self.assertEqual(len(rows), 2)
        for row in rows:
            relations = engine.list_relations(row['id'], db_conn=self.conn)
            self.assertEqual({r['target_id'] for r in relations}, set(self.ids))
            self.assertTrue(all(r['relation'] == 'derived_from' for r in relations))
        self.assertEqual(len(engine.list_all(status='archived', db_conn=self.conn)), 3)

    def test_relation_failure_rolls_back_sources_and_new_outputs(self):
        self.conn.execute("CREATE TRIGGER reject_relation BEFORE INSERT ON memory_relations BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        self.conn.commit()
        before = list(self.conn.iterdump())
        with self.assertRaises(Exception):
            dream.apply_merge(self.group, self.output, self.conn)
        self.assertEqual(before, list(self.conn.iterdump()))
        self.assertFalse(self.conn.in_transaction)

    def test_protected_guard_leaves_callers_transaction_open(self):
        self.conn.execute('BEGIN IMMEDIATE')
        engine.set_memory_protection([self.ids[0]], True, '明确重要', db_conn=self.conn)
        dream.apply_merge(self.group, self.output, self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT protected FROM memories WHERE id=?', (self.ids[0],)).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
