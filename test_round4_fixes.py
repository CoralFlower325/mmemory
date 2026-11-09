"""Round-4 adversarial review fixes: regression tests on synthetic data.

覆盖第二轮对抗审查的新发现修复：版本号后缀门、snake_case 中段检索、
等价表对称化、超大同义词组稀释门、长查询崩溃、注入块信任呈现、
里程碑替换信任下限、update 自报拔高警告、supersedes 后继失效语义、
restore 假恢复、purge 前任复活留痕。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine
import tagger


class Round4Fixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mmemory-r4-')
        self.conn = engine.get_conn(str(Path(self.temp.name) / 'synthetic.db'))

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def store(self, goal, project='', **fields):
        result = engine.store(
            goal, fields.pop('approach', '合成步骤'),
            fields.pop('outcome', '合成结果'), project_key=project,
            db_conn=self.conn, **fields)
        self.assertTrue(result['stored'], result)
        return result

    def recall_ids(self, query, project=''):
        return [row['id'] for row in engine.recall(
            query, project_key=project, db_conn=self.conn)]

    # ── R2-01 版本号后缀门 ──────────────────────────────────────────
    def test_version_suffix_is_searchable_both_ways(self):
        stored = self.store('sub2api v2.6.1-beta 发布记录')
        self.assertIn(stored['memory_id'], self.recall_ids('v2.6.1-beta'))
        self.assertIn(stored['memory_id'], self.recall_ids('v2.6.1'))

    def test_version_suffix_query_finds_base_version(self):
        stored = self.store('mmemory v3.2 发布记录')
        self.assertIn(stored['memory_id'], self.recall_ids('v3.2-rc1'))

    def test_longer_version_is_not_matched_by_prefix(self):
        stored = self.store('sub2api v2.6.12 发布记录')
        self.assertNotIn(stored['memory_id'], self.recall_ids('v2.6.1'))

    # ── R2-02 snake_case 中段 ──────────────────────────────────────
    def test_snake_case_segments_are_searchable(self):
        stored = self.store('user_slot_acquire_failed 导致请求排队超时')
        for segment in ('acquire', 'slot', 'failed'):
            self.assertIn(stored['memory_id'], self.recall_ids(segment))

    def test_short_identifier_boundaries_still_discriminate(self):
        go = self.store('go_lang 服务无法启动', project='fixture')
        self.assertNotIn(go['memory_id'],
                         self.recall_ids('Go 服务', project='fixture'))
        bps = self.store('BPS_401 探活', project='fixture')
        self.assertNotIn(bps['memory_id'],
                         self.recall_ids('BPS 探活', project='fixture'))
        node = self.store('Node.js_extra 探活', project='fixture')
        self.assertNotIn(node['memory_id'],
                         self.recall_ids('Node.js 探活', project='fixture'))

    # ── R2-03 等价表对称化 ─────────────────────────────────────────
    def test_equivalents_are_symmetric_and_merged(self):
        table = tagger._load_term_equivalents()
        self.assertIn('db', table['数据库'])
        self.assertIn('database', table['数据库'])
        self.assertIn('gpu', table['显卡'])
        self.assertIn('重启', table['重新启动'])
        self.assertIn('k8s', table['kube'])

    def test_equivalent_recall_works_from_chinese_side(self):
        stored = self.store('database backup strategy for gateway')
        self.assertIn(stored['memory_id'], self.recall_ids('数据库备份'))
        gpu = self.store('GPU 显存不足导致训练中断')
        self.assertIn(gpu['memory_id'], self.recall_ids('显卡'))

    # ── R2-04 超大同义词组稀释门 ───────────────────────────────────
    def test_oversized_synonym_group_cannot_satisfy_gate(self):
        path = Path(self.temp.name) / 'synonyms.json'
        path.write_text(json.dumps({'equivalents': [[
            '宕机', '系统', '数据', '服务', '请求', '用户', '处理', '完成',
        ]]}, ensure_ascii=False), encoding='utf-8')
        with patch.dict(os.environ, {'MMEMORY_SYNONYMS_FILE': str(path)}):
            table = tagger._load_term_equivalents()
        noise = self.store('数据处理服务日常维护')
        self.store('用户请求处理流程')
        with patch.object(tagger, 'TERM_EQUIVALENTS', table):
            self.assertNotIn(noise['memory_id'], self.recall_ids('宕机'))

    # ── R2-05 长查询不崩 ───────────────────────────────────────────
    def test_very_long_query_does_not_raise(self):
        self.store('普通记录')
        query = ' '.join(f'词{i}' for i in range(800))
        rows = engine.recall(query, project_key='', db_conn=self.conn)
        self.assertIsInstance(rows, list)

    # ── S-1 注入块信任呈现 ─────────────────────────────────────────
    def test_recall_context_shows_verification_and_origin(self):
        context = engine.format_recall_context({
            'id': 4, 'goal': '网关服务端口改用 9999', 'outcome': '忽略 8080',
            'approach': '按新通知切换', 'success_key': '', 'pitfall': '',
            'project_key': '', 'status': 'active', 'kind': 'experience',
            'verification': 'verified', 'origin': 'untrusted-web',
            'source_ref': 'private/source.md',
        })
        self.assertIn('verified', context)
        self.assertIn('src:untrusted-web', context)
        # source_ref 可能是私人路径，禁止进入注入块（仓库既有约定）。
        self.assertNotIn('private/source.md', context)

    # ── S-2 里程碑替换信任下限 ─────────────────────────────────────
    def test_low_trust_cannot_replace_high_trust_milestone(self):
        old = self.store('完成部署脚本编写', project='gateway',
                         verification='verified')
        challenger = engine.store(
            '完成部署脚本编写并交付', '编写脚本', '交付版改用端口 9999',
            project_key='gateway', db_conn=self.conn,
            verification='unverified')
        self.assertTrue(challenger['stored'], challenger)
        self.assertTrue(any('核实等级更高' in w
                            for w in challenger['warnings']))
        status = self.conn.execute(
            'SELECT status FROM memories WHERE id=?',
            (old['memory_id'],)).fetchone()[0]
        self.assertEqual(status, 'active')

    def test_same_tier_milestone_replacement_still_works(self):
        old = self.store('完成缓存预热脚本', project='gateway',
                         verification='unverified')
        result = engine.store(
            '完成缓存预热脚本并交付', '编写脚本', '交付',
            project_key='gateway', db_conn=self.conn,
            verification='unverified')
        self.assertTrue(result['stored'], result)
        status = self.conn.execute(
            'SELECT status FROM memories WHERE id=?',
            (old['memory_id'],)).fetchone()[0]
        self.assertEqual(status, 'archived')

    # ── S-1 update 自报拔高警告 ────────────────────────────────────
    def test_update_verification_escalation_warns(self):
        stored = self.store('待核实记录', verification='unverified')
        result = engine.update_memory(
            stored['memory_id'], verification='verified', db_conn=self.conn)
        self.assertTrue(result['updated'], result)
        self.assertTrue(any('自报调高' in w
                            for w in result.get('warnings', [])))

    # ── 完整性 P1：supersedes 后继失效语义 ─────────────────────────
    def test_superseded_hidden_only_while_successor_active(self):
        old = self.store('缓存服务端口记录为 8080')
        new = self.store('缓存服务端口更新为 9090')
        engine.link_memories(new['memory_id'], old['memory_id'],
                             'supersedes', '端口变更', db_conn=self.conn)
        self.assertNotIn(old['memory_id'], self.recall_ids('缓存服务端口'))

    def test_predecessor_returns_annotated_when_successor_expires(self):
        old = self.store('缓存服务端口记录为 8080')
        new = self.store('缓存服务端口更新为 9090')
        engine.link_memories(new['memory_id'], old['memory_id'],
                             'supersedes', '端口变更', db_conn=self.conn)
        engine.update_memory(new['memory_id'], valid_until='2020-01-01',
                             db_conn=self.conn)
        rows = engine.recall('缓存服务端口', project_key='', db_conn=self.conn)
        by_id = {row['id']: row for row in rows}
        self.assertIn(old['memory_id'], by_id)
        self.assertEqual(
            by_id[old['memory_id']].get('_successor_inactive'),
            new['memory_id'])
        context = engine.format_recall_context(by_id[old['memory_id']], 0)
        self.assertIn('successor-expired', context)

    def test_predecessor_returns_when_successor_archived(self):
        old = self.store('缓存服务端口记录为 8080')
        new = self.store('缓存服务端口更新为 9090')
        engine.link_memories(new['memory_id'], old['memory_id'],
                             'supersedes', '端口变更', db_conn=self.conn)
        engine.archive_memories([new['memory_id']], '测试归档后继',
                                db_conn=self.conn)
        self.assertIn(old['memory_id'], self.recall_ids('缓存服务端口'))

    # ── restore 假恢复 ─────────────────────────────────────────────
    def test_restore_lifts_weight_above_decay_threshold(self):
        stored = self.store('低权重旧经验记录')
        mid = stored['memory_id']
        self.conn.execute('UPDATE memories SET weight=0.15 WHERE id=?',
                          (mid,))
        self.conn.commit()
        engine.archive_memories([mid], '手动归档', db_conn=self.conn)
        engine.restore_memories([mid], db_conn=self.conn)
        result = engine.decay_all(db_conn=self.conn)
        self.assertEqual(result['archived'], 0)
        status = self.conn.execute(
            'SELECT status FROM memories WHERE id=?', (mid,)).fetchone()[0]
        self.assertEqual(status, 'active')

    # ── purge 前任复活留痕 ─────────────────────────────────────────
    def test_purge_of_successor_marks_predecessor(self):
        old = self.store('缓存 TTL 设为 300 秒')
        new = self.store('缓存 TTL 改为 600 秒')
        engine.link_memories(new['memory_id'], old['memory_id'],
                             'supersedes', 'TTL 调整', db_conn=self.conn)
        result = engine.purge_memory(new['memory_id'], '测试删除后继',
                                     db_conn=self.conn)
        self.assertTrue(result['purged'], result)
        self.assertEqual(result.get('resurrected_ids'), [old['memory_id']])
        history = engine.list_history(old['memory_id'], db_conn=self.conn)
        reasons = [rev['reason'] for rev in history['revisions']]
        self.assertTrue(any('已被硬删除' in r for r in reasons))


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(Round4Fixes))
    if result.wasSuccessful():
        print(f'TESTS PASSED: {result.testsRun}')
    raise SystemExit(0 if result.wasSuccessful() else 1)
