"""只读复核线索回归；所有正文均为新建合成数据。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine
import review


class ReadOnlyReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mmemory-review-')
        self.path = Path(self.directory.name) / 'synthetic.db'
        self.conn = engine.get_conn(str(self.path))

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def store(self, goal, outcome='合成结果', project='fixture', **fields):
        result = engine.store(goal, fields.pop('approach', '合成步骤'), outcome,
                              db_conn=self.conn, project_key=project, **fields)
        self.assertTrue(result['stored'], result)
        return result['memory_id']

    def candidates(self, **options):
        return review.review_candidates(db_conn=self.conn, **options)

    def test_reworded_goal_with_same_numbers_exposes_full_evidence(self):
        left = self.store('配置数据库连接池', outcome='池容量为8', pitfall='等待超时，调整容量后恢复')
        right = self.store('更新数据库连接池', outcome='池容量为12')
        result = self.candidates()
        duplicate = [c for c in result['candidates'] if c['kind'] == 'potential_duplicate']
        self.assertEqual([c['memory_ids'] for c in duplicate], [[left, right]])
        self.assertEqual(duplicate[0]['memories'][0]['pitfall'], '等待超时，调整容量后恢复')
        self.assertTrue(duplicate[0]['requires_judgment'])
        self.assertIn('outcome', duplicate[0]['clues'][0]['different_fields'])

    def test_same_labels_and_different_goals_never_make_a_candidate(self):
        self.store('完成政治错题本录入')
        self.store('完成英语错题本录入')
        self.conn.execute("UPDATE memories SET tags_tool='python',tags_domain='general'")
        self.conn.commit()
        self.assertEqual(self.candidates()['candidate_count'], 0)

    def test_chapter_order_and_single_letter_identifiers_remain_distinct(self):
        for goal in ('完成第1章第2节校对', '完成第2章第1节校对',
                     '完成 A 模块配置', '完成 B 模块配置'):
            self.store(goal)
        self.assertEqual(self.candidates()['candidate_count'], 0)

    def test_pair_comparisons_never_cross_namespace(self):
        self.store('配置 Java 运行环境', 'JDK11 可用', project='first')
        self.store('配置 Java 运行环境', 'JDK17 可用', project='second')
        self.store('配置 Java 运行环境', 'JDK21 可用', project='')
        result = self.candidates()
        self.assertEqual(result['scanned_count'], 3)
        self.assertEqual(result['candidate_count'], 0)
        shared = self.candidates(project_key='')
        self.assertEqual(shared['scanned_count'], 1)
        self.assertEqual(shared['scope']['project_key'], '')

    def test_explicit_versions_are_only_conflict_clues(self):
        old = self.store('example-service Java 运行环境', '当前使用JDK11运行服务',
                         project='example-service')
        new = self.store('example-service Java 运行环境修订',
                         '当前使用Java17运行服务', project='example-service')
        clues = [c for c in self.candidates()['candidates'] if c['kind'] == 'potential_conflict']
        self.assertEqual([c['memory_ids'] for c in clues], [[old, new]])
        self.assertEqual(clues[0]['clues'][0]['entity'], 'java')
        self.assertEqual({e['value'] for e in clues[0]['clues'][0]['evidence']}, {'11', '17'})
        self.assertIn('合法升级', clues[0]['clues'][0]['note'])

    def test_internal_positive_negative_state_exposes_both_fields(self):
        mid = self.store('修订认证通道结论', '认证通道已失效', success_key='认证通道可用')
        clues = [c for c in self.candidates()['candidates'] if c['kind'] == 'potential_conflict']
        self.assertEqual([c['memory_ids'] for c in clues], [[mid]])
        evidence = clues[0]['clues'][0]['evidence']
        self.assertEqual({e['field'] for e in evidence}, {'outcome', 'success_key'})
        self.assertEqual({e['polarity'] for e in evidence}, {'negative', 'positive'})

    def test_resolved_past_pitfall_is_not_internal_contradiction(self):
        self.store('更新认证通道方案', '认证通道可用', success_key='认证通道有效',
                   pitfall='认证通道失效，替换正确凭据后恢复')
        self.assertEqual(self.candidates()['candidate_count'], 0)

    def test_single_related_tool_project_reference_is_not_a_candidate(self):
        self.store('Python 当前接入配置', approach='通过本地服务调用 MCP',
                   project='tool-fixture')
        mixed = [c for c in self.candidates()['candidates'] if c['kind'] == 'mixed_projects']
        self.assertEqual(mixed, [])

    def test_multi_project_reference_is_a_clue_not_a_rejection(self):
        mid = self.store('mmemory 项目隔离检查',
                         approach='对比 example-service 与 example-piano 记忆',
                         project='mmemory')
        result = self.candidates(project_key='mmemory')
        mixed = [c for c in result['candidates'] if c['kind'] == 'mixed_projects']
        self.assertEqual([c['memory_ids'] for c in mixed], [[mid]])
        evidence = mixed[0]['clues'][0]['evidence']
        self.assertEqual({e['project_key'] for e in evidence},
                         {'mmemory', 'example-service', 'example-piano'})
        self.assertIn('合法', mixed[0]['clues'][0]['note'])

    def test_default_excludes_archive_but_shows_supersession_relation(self):
        old = self.store('配置 Java 运行环境', 'JDK11 可用')
        self.assertTrue(engine.archive_memories([old], '被明确新环境替代', db_conn=self.conn)['archived'])
        new = self.store('配置 Java 运行环境', 'JDK17 可用')
        self.assertTrue(engine.link_memories(new, old, 'supersedes', '环境已升级',
                                             db_conn=self.conn)['linked'])
        result = self.candidates()
        self.assertEqual(result['scanned_count'], 1)
        self.assertEqual(result['candidate_count'], 0)
        self.assertEqual(result['relations'][0]['target']['status'], 'archived')
        self.assertEqual(result['relations'][0]['source_id'], new)
        both = self.candidates(status='all')
        self.assertEqual(both['scanned_count'], 2)
        self.assertTrue(any(c['relations'] for c in both['candidates']))

    def test_external_cross_project_relation_is_reported_as_integrity_clue(self):
        left = self.store('环境 A', project='first')
        right = self.store('环境 B', project='second')
        result = self.candidates(relations=[{'source_id': left, 'target_id': right,
                                            'relation': 'derived_from', 'reason': '合成非法关系'}])
        self.assertEqual(result['relations'], [])
        integrity = [c for c in result['candidates'] if c['kind'] == 'cross_project_relation']
        self.assertEqual(len(integrity), 1)
        self.assertEqual(integrity[0]['memory_ids'], [left, right])
        self.assertEqual(integrity[0]['clues'][0]['reason'], '合成非法关系')

    def test_read_only_preserves_data_and_borrowed_transaction(self):
        self.store('更新数据库连接池')
        self.store('配置数据库连接池')
        self.conn.execute("UPDATE memories SET outcome='未提交合成修订' WHERE id=1")
        before = list(self.conn.iterdump())
        total_changes = self.conn.total_changes
        self.candidates()
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.total_changes, total_changes)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.conn.rollback()

    def test_default_connection_never_initializes_or_creates_database(self):
        self.store('更新数据库连接池')
        self.store('配置数据库连接池')
        before = list(self.conn.iterdump())
        with patch.object(engine, 'DB_PATH', self.path), patch.object(engine, 'get_conn',
                                                                    side_effect=AssertionError('不得迁移')):
            result = review.review_candidates()
        self.assertTrue(result['read_only'])
        self.assertEqual(result['scanned_count'], 2)
        self.assertEqual(list(self.conn.iterdump()), before)
        missing = Path(self.directory.name) / 'missing.db'
        with patch.object(engine, 'DB_PATH', missing):
            with self.assertRaises(sqlite3.OperationalError):
                review.review_candidates()
        self.assertFalse(missing.exists())

    def test_missing_relation_table_is_not_created_by_review(self):
        self.store('环境资料')
        self.conn.execute('DROP TABLE memory_relations')
        self.conn.commit()
        self.assertEqual(self.candidates()['relations'], [])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE "
                                           "name='memory_relations'").fetchone())

    def test_candidate_limit_does_not_cut_scanned_memories(self):
        for goal in ('配置数据库连接池', '更新数据库连接池', '修订数据库连接池'):
            self.store(goal)
        result = self.candidates(limit=1)
        self.assertEqual(result['scanned_count'], 3)
        self.assertEqual(result['candidate_count'], 3)
        self.assertEqual(len(result['candidates']), 1)
        self.assertTrue(result['truncated'])

    def test_invalid_scope_parameters_are_explicit_errors(self):
        with self.assertRaises(ValueError):
            self.candidates(status='deleted')
        for limit in (0, 501):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    self.candidates(limit=limit)


if __name__ == '__main__':
    unittest.main(verbosity=2)
