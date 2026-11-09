"""Schema v2, retrieval, relation, and history regressions on synthetic data."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import dream
import engine
import obsidian_export
import review
import tagger


class RefactorV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mmemory-v2-')
        self.conn = engine.get_conn(str(Path(self.temp.name) / 'synthetic.db'))

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def store(self, goal, project='fixture', **fields):
        result = engine.store(
            goal, fields.pop('approach', '合成步骤'),
            fields.pop('outcome', '合成结果'), project_key=project,
            db_conn=self.conn, **fields)
        self.assertTrue(result['stored'], result)
        return result['memory_id']

    def row(self, memory_id):
        return dict(self.conn.execute(
            'SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone())

    def test_v2_schema_has_search_metadata_and_revision_tables(self):
        columns = {row['name'] for row in self.conn.execute(
            'PRAGMA table_info(memories)')}
        self.assertTrue({
            'revision', 'kind', 'source_ref', 'origin', 'verification',
            'valid_from', 'valid_until', 'search_text',
        }.issubset(columns))
        self.assertEqual(self.conn.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertEqual(
            [row['name'] for row in self.conn.execute(
                'PRAGMA table_info(memories_fts)')],
            ['search_text'])

    def test_chinese_fts_and_short_ascii_alias_are_reachable(self):
        memory_id = self.store('Kubernetes 集群部署要点', outcome='上线成功')
        indexed = self.conn.execute(
            'SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?',
            ('集群',)).fetchall()
        self.assertEqual([row['rowid'] for row in indexed], [memory_id])
        results = engine.recall('k8s 集群部署', project_key='fixture',
                                db_conn=self.conn)
        self.assertEqual([row['id'] for row in results], [memory_id])

    def test_traditional_and_chinese_number_variants_are_reachable(self):
        server = self.store('重启服务器后恢复', outcome='服务正常')
        self.assertEqual(
            [row['id'] for row in engine.recall(
                '重啟服務器', project_key='fixture', db_conn=self.conn)],
            [server])
        question = self.store('完成第三题校对', outcome='已通过')
        self.assertEqual(
            [row['id'] for row in engine.recall(
                '第3题', project_key='fixture', db_conn=self.conn)],
            [question])

        compact = self.store('BPS401 探活', outcome='通过')
        self.assertEqual(
            [row['id'] for row in engine.recall(
                'BPS 401', project_key='fixture', db_conn=self.conn)],
            [compact])
        spaced = self.store('BPS 403 探活', outcome='通过')
        self.assertEqual(
            [row['id'] for row in engine.recall(
                'bps403', project_key='fixture', db_conn=self.conn)],
            [spaced])
        letter_question = self.store('C题已完成', outcome='通过')
        self.assertEqual(
            [row['id'] for row in engine.recall(
                'C 题', project_key='fixture', db_conn=self.conn)],
            [letter_question])

    def test_more_chinese_numbers_and_canonical_synonyms(self):
        question = self.store('完成第三问校对', outcome='通过')
        self.assertEqual(
            [row['id'] for row in engine.recall(
                '第3问', project_key='fixture', db_conn=self.conn)],
            [question])
        large = self.store('完成第101题校对', outcome='通过')
        self.assertEqual(
            [row['id'] for row in engine.recall(
                '第一百零一题', project_key='fixture', db_conn=self.conn)],
            [large])
        thousand = self.store('完成第1001题校对', outcome='通过')
        self.assertEqual(
            [row['id'] for row in engine.recall(
                '第一千零一题', project_key='fixture', db_conn=self.conn)],
            [thousand])
        spaced = self.store('完成第3题校对', outcome='通过')
        self.assertEqual(
            [row['id'] for row in engine.recall(
                '第 3 题', project_key='fixture', db_conn=self.conn)],
            [spaced])
        deploy = self.store('生产服务部署完成', outcome='通过')
        self.assertEqual(
            [row['id'] for row in engine.recall(
                'deploy', project_key='fixture', db_conn=self.conn)],
            [deploy])
        traditional = self.store('資料庫连接恢复', outcome='通过')
        self.assertEqual(
            [row['id'] for row in engine.recall(
                '數據庫', project_key='fixture', db_conn=self.conn)],
            [traditional])

    def test_identifier_in_contrastive_pitfall_does_not_outrank_current_version(self):
        old = self.store('demo v2 部署', outcome='端口 8080')
        new = self.store('demo v3 部署', outcome='端口 9090',
                         pitfall='v3 与 v2 插件不兼容')
        results = engine.recall('demo v2 部署', project_key='fixture',
                                db_conn=self.conn)
        self.assertEqual(results[0]['id'], old)
        if len(results) > 1:
            self.assertEqual(results[1]['id'], new)
        error_401 = self.store('接口错误 401 处理', outcome='已处理')
        error_403 = self.store('接口错误 403 处理',
                               outcome='从 401 升级到 403')
        error_results = engine.recall(
            '接口错误 401', project_key='fixture', db_conn=self.conn)
        self.assertEqual(error_results[0]['id'], error_401)
        dotted_old = self.store('client v2.0 配置', outcome='旧版本')
        self.store('client v3.0 配置', outcome='从 v2.0 升级到 v3.0')
        dotted_results = engine.recall(
            'client v2.0', project_key='fixture', db_conn=self.conn)
        self.assertEqual(dotted_results[0]['id'], dotted_old)

    def test_supersedes_hides_stale_target_unless_requested(self):
        old = self.store('当前端口 8080')
        new = self.store('当前端口 9090')
        linked = engine.link_memories(
            new, old, 'supersedes', '新端口替代旧端口', db_conn=self.conn)
        self.assertTrue(linked['linked'])
        self.assertEqual(
            [row['id'] for row in engine.recall(
                '当前端口', project_key='fixture', db_conn=self.conn)],
            [new])
        included = engine.recall(
            '当前端口', project_key='fixture', db_conn=self.conn,
            include_superseded=True)
        self.assertEqual({row['id'] for row in included}, {old, new})
        self.assertEqual(
            next(row['_superseded_by'] for row in included if row['id'] == old),
            new)
        deep = engine.recall('当前端口', project_key='fixture',
                             db_conn=self.conn, deep=True)
        self.assertEqual({row['id'] for row in deep}, {old, new})

    def test_deep_recall_labels_archive_and_expiry_is_filtered(self):
        archived = self.store('需要历史回溯的部署记录')
        engine.archive_memories([archived], '已完成阶段', db_conn=self.conn)
        results = engine.recall('历史回溯', project_key='fixture',
                                db_conn=self.conn, deep=True)
        self.assertEqual([row['id'] for row in results], [archived])
        self.assertEqual(results[0]['status'], 'archived')

        expired = self.store('有效期为过去的事实', valid_until='2020-01-01')
        self.assertEqual(engine.recall('过去的事实', project_key='fixture',
                                       db_conn=self.conn), [])
        included = engine.recall('过去的事实', project_key='fixture',
                                 db_conn=self.conn, include_expired=True)
        self.assertEqual([row['id'] for row in included], [expired])
        self.assertTrue(included[0]['_expired'])

        future = self.store('未来才生效的事实', valid_from='2099-01-01')
        self.assertEqual(engine.recall('未来才生效', project_key='fixture',
                                       db_conn=self.conn), [])
        included = engine.recall('未来才生效', project_key='fixture',
                                 db_conn=self.conn, include_expired=True)
        self.assertEqual([row['id'] for row in included], [future])
        self.assertTrue(included[0]['_not_yet_valid'])

    def test_timezone_validity_and_future_successor_do_not_hide_current_fact(self):
        now = datetime.now(timezone.utc)
        current = self.store(
            '时区有效期当前事实', valid_from=(now - timedelta(hours=1)).isoformat(),
            valid_until=(now + timedelta(hours=2)).isoformat())
        self.assertEqual(
            [row['id'] for row in engine.recall(
                '时区有效期', project_key='fixture', db_conn=self.conn)],
            [current])

        old = self.store('未来切换前的当前端口', outcome='端口 8080')
        future = self.store(
            '未来切换后的端口', outcome='端口 9090',
            valid_from=(now + timedelta(days=2)).isoformat())
        engine.link_memories(future, old, 'supersedes', '未来切换',
                             db_conn=self.conn)
        current_results = engine.recall(
            '未来切换', project_key='fixture', db_conn=self.conn)
        self.assertEqual([row['id'] for row in current_results], [old])

    def test_date_only_valid_until_includes_the_full_local_day(self):
        memory_id = self.store(
            '今天仍有效的日期事实',
            valid_until=datetime.now().astimezone().date().isoformat())
        self.assertEqual(
            [row['id'] for row in engine.recall(
                '日期事实', project_key='fixture', db_conn=self.conn)],
            [memory_id])

    def test_local_naive_times_use_target_date_dst_rules(self):
        if not hasattr(time, 'tzset'):
            self.skipTest('time.tzset unavailable')
        old_tz = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'America/New_York'
            time.tzset()
            winter = engine._parse_temporal('2026-01-01T12:00:00')
            summer = engine._parse_temporal('2026-07-01T12:00:00')
        finally:
            if old_tz is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old_tz
            time.tzset()
        self.assertEqual(winter.hour, 17)
        self.assertEqual(summer.hour, 16)

    def test_update_records_snapshot_and_purge_removes_all_copies(self):
        first = self.store('可追溯事实', kind='project_fact',
                           source_ref='notes/design.md',
                           verification='verified')
        second = self.store('派生事实')
        engine.link_memories(second, first, 'derived_from', '合成来源关系',
                             db_conn=self.conn)
        updated = engine.update_memory(
            first, outcome='修订后的结果', reason='验证后修订',
            db_conn=self.conn)
        self.assertTrue(updated['updated'])
        self.assertEqual(updated['related_ids'], [second])
        history = engine.list_history(first, db_conn=self.conn)
        self.assertEqual(history['current']['revision'], 2)
        self.assertEqual(len(history['revisions']), 1)
        self.assertEqual(history['revisions'][0]['snapshot']['outcome'],
                         '合成结果')

        purged = engine.purge_memory(
            first, '清理私人测试事实', db_conn=self.conn)
        self.assertTrue(purged['purged'])
        self.assertIsNone(self.conn.execute(
            'SELECT 1 FROM memories WHERE id=?', (first,)).fetchone())
        self.assertEqual(self.conn.execute(
            'SELECT count(*) FROM memory_revisions WHERE memory_id=?',
            (first,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            'SELECT count(*) FROM memory_relations WHERE source_id=? OR target_id=?',
            (first, first)).fetchone()[0], 0)

    def test_history_and_relations_respect_project_scope(self):
        first = self.store('项目 A 事实', project='project-a')
        second = self.store('项目 B 事实', project='project-b')
        engine.link_memories(second, first, 'derived_from', '合成关系',
                             project_key='project-a', db_conn=self.conn)
        self.assertEqual(
            engine.list_relations(second, project_key='project-a',
                                  db_conn=self.conn), [])
        history = engine.list_history(second, project_key='project-a',
                                      db_conn=self.conn)
        self.assertFalse(history['found'])
        self.assertIsNone(history['current'])
        self.assertTrue(engine.list_history(
            second, project_key='project-b', db_conn=self.conn)['found'])

    def test_cross_project_relation_is_ignored_by_recall_and_reported_by_review(self):
        first = self.store('项目 A 当前事实', project='project-a')
        second = self.store('项目 B 外部注入事实', project='project-b')
        self.conn.execute("""
            INSERT INTO memory_relations(source_id,target_id,relation,reason)
            VALUES(?,?, 'supersedes', '外部合成异常')
        """, (second, first))
        self.conn.commit()
        recalled = engine.recall(
            '项目 A 当前事实', project_key='project-a', db_conn=self.conn)
        self.assertEqual([row['id'] for row in recalled], [first])
        review_result = review.review_candidates(
            project_key='project-a', db_conn=self.conn)
        self.assertTrue(any(
            item['kind'] == 'cross_project_relation'
            and set(item['memory_ids']) == {first, second}
            for item in review_result['candidates']))

    def test_purge_physically_clears_plaintext(self):
        sentinel = 'PURGE_SENTINEL_4E2A'
        path = Path(self.temp.name) / 'purge.db'
        with patch.object(engine, 'DB_PATH', path):
            result = engine.store(
                sentinel, '敏感步骤', '敏感结果', project_key='private')
            memory_id = result['memory_id']
            engine.update_memory(memory_id, outcome='修订 ' + sentinel,
                                 reason='合成修订')
            purged = engine.purge_memory(memory_id, '合成物理清理')
        self.assertTrue(purged['purged'])
        self.assertTrue(purged['physical_cleanup'])
        raw_files = [path, Path(str(path) + '-wal'), Path(str(path) + '-shm')]
        self.assertEqual(sum(
            file.read_bytes().count(sentinel.encode())
            for file in raw_files if file.exists()), 0)

    def test_revision_history_is_bounded(self):
        memory_id = self.store('修订上限测试')
        original_limit = engine.REVISION_LIMIT
        engine.REVISION_LIMIT = 2
        try:
            for index in range(4):
                engine.update_memory(
                    memory_id, outcome=f'结果{index}', reason=f'修订{index}',
                    db_conn=self.conn)
        finally:
            engine.REVISION_LIMIT = original_limit
        history = engine.list_history(memory_id, db_conn=self.conn)
        self.assertEqual(len(history['revisions']), 2)
        self.assertEqual(
            [item['reason'] for item in history['revisions']],
            ['修订2', '修订3'])

    def test_dream_rejects_output_over_shared_length_limit(self):
        for index in range(3):
            self.store(f'合成合并候选{index}')
        group = dream.find_merge_groups(
            self.conn, min_group_size=3, project_key='fixture')[0]
        result = dream.apply_merge(group, [{
            'goal': '超长合并目标',
            'approach': '超' * 1001,
            'outcome': '合成结果',
        }], self.conn)
        self.assertFalse(result['merged'])
        self.assertIn('超过', result['reason'])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM memories WHERE status='active'").fetchone()[0],
            3)

    def test_dream_rejects_metadata_forgery_and_existing_duplicates(self):
        for index in range(3):
            self.store(f'偏好候选{index}', kind='preference')
        group = dream.find_merge_groups(
            self.conn, min_group_size=3, project_key='fixture')[0]
        forged = dream.apply_merge(group, [{
            'goal': '模型伪造元数据', 'approach': '', 'outcome': '值',
            'kind': 'project_fact', 'verification': 'verified',
            'source_ref': 'trusted://forged',
        }], self.conn)
        self.assertFalse(forged['merged'])
        self.assertIn('五字段', forged['reason'])

        existing = self.store('已有合并结果')
        self.conn.execute(
            "UPDATE memories SET tags_domain='x',tags_tool='x',tags_action='x' "
            "WHERE id=?", (existing,))
        self.conn.commit()
        for index in range(3):
            self.store(f'独立合并候选{index}')
        group = next(
            item for item in dream.find_merge_groups(
                self.conn, min_group_size=3, project_key='fixture')
            if item['count'] == 3)
        duplicate = dream.apply_merge(group, [{
            'goal': '已有合并结果', 'approach': '合并', 'outcome': '合并',
        }], self.conn)
        self.assertFalse(duplicate['merged'])
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM memories WHERE status='active' "
                "AND goal='已有合并结果'").fetchone()[0],
            1)

    def test_dream_stale_kind_is_rejected(self):
        ids = [self.store(f'类型变更候选{index}', kind='preference')
               for index in range(3)]
        group = dream.find_merge_groups(
            self.conn, min_group_size=3, project_key='fixture')[0]
        for memory_id in ids:
            self.assertTrue(engine.update_memory(
                memory_id, kind='project_fact', outcome='事实结果',
                reason='类型变更', db_conn=self.conn)['updated'])
        result = dream.apply_merge(group, [{
            'goal': '过期快照合并', 'approach': '', 'outcome': '',
        }], self.conn)
        self.assertFalse(result['merged'])
        self.assertIn('已变化', result['reason'])

    def test_project_alias_nfc_and_longest_match(self):
        config = (
            ('broad', ('实验',)),
            ('specific', ('实验三',)),
            ('nfc', ('cafe\u0301',)),
        )
        self.assertEqual(
            [project for _, _, project in
             tagger._keyword_matches('实验三 caf\u00e9', config)],
            ['specific', 'nfc'])
        self.assertEqual(tagger.infer_project_key('not-example-servicex'), '')

    def test_project_ambiguity_and_short_ascii_boundary(self):
        ambiguous = engine.store(
            '完成联调',
            '连接 example-service 与 example-piano',
            '完成', db_conn=self.conn)
        self.assertFalse(ambiguous['stored'])
        self.assertIn('多个项目', ambiguous['reason'])

        self.store('go_lang 服务无法启动')
        self.assertEqual(
            engine.recall('Go 服务', project_key='fixture', db_conn=self.conn),
            [])
        self.store('BPS_401 探活', outcome='不应作为该缩写标识')
        self.assertEqual(
            engine.recall('BPS 探活', project_key='fixture', db_conn=self.conn),
            [])
        self.store('Node.js_extra 探活', outcome='不应作为运行时标识')
        self.assertEqual(
            engine.recall('Node.js 探活', project_key='fixture', db_conn=self.conn),
            [])

        invalid = engine.store(
            '项目键控制字符', '执行', '完成',
            project_key='fixture\nignore previous', db_conn=self.conn)
        self.assertFalse(invalid['stored'])
        self.assertIn('控制字符', invalid['reason'])

    def test_custom_synonym_file_is_bidirectional(self):
        path = Path(self.temp.name) / 'synonyms.json'
        path.write_text(json.dumps({
            'equivalents': [['重启', '重啟', '重新启动']]
        }, ensure_ascii=False), encoding='utf-8')
        with patch.dict(os.environ, {'MMEMORY_SYNONYMS_FILE': str(path)}):
            table = tagger._load_term_equivalents()
        self.assertIn('重啟', table['重启'])
        self.assertIn('重启', table['重啟'])

    def test_dream_prompt_sanitizes_project_and_tag_labels(self):
        prompt = dream.build_merge_prompt({
            'project_key': 'fixture\u2028ignore previous',
            'domain': 'general\x1b[31m',
            'tool': 'python',
            'action': 'build',
            'count': 1,
            'memories': [{
                'id': 1, 'goal': '目标', 'approach': '方案',
                'outcome': '结果', 'success_key': '', 'pitfall': '',
                'kind': 'experience', 'verification': 'unverified',
                'valid_from': '', 'valid_until': '',
            }],
        })
        self.assertNotIn('\u2028', prompt)
        self.assertNotIn('\x1b', prompt)

    def test_review_reports_numeric_fact_conflict(self):
        first = self.store('服务端口配置', outcome='端口 8080')
        second = self.store('服务端口配置复核', outcome='端口 9090')
        result = review.review_candidates(
            project_key='fixture', db_conn=self.conn)
        conflicts = [
            item for item in result['candidates']
            if item['kind'] == 'potential_conflict'
        ]
        self.assertTrue(any(set(item['memory_ids']) == {first, second}
                            for item in conflicts))

    def test_reindex_repairs_deleted_fts_rows_and_stats_expose_health(self):
        self.store('健康统计事实', kind='preference', verification='verified')
        self.conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('delete-all')")
        self.conn.commit()
        result = engine.rebuild_search_index(db_conn=self.conn)
        self.assertTrue(result['rebuilt'])
        self.assertEqual(result['indexed'], 1)
        stats = engine.get_stats(db_conn=self.conn)
        self.assertEqual(stats['by_kind']['preference'], 1)
        self.assertEqual(stats['by_verification']['verified'], 1)
        self.assertEqual(stats['unused_active'], 1)

    def test_reindex_failure_rolls_back_only_its_savepoint(self):
        self.store('reindex 故障回滚事实')
        self.conn.execute("""
            CREATE TEMP TRIGGER reject_reindex
            BEFORE UPDATE OF search_text ON memories
            BEGIN SELECT RAISE(ABORT, 'reindex rejected'); END
        """)
        self.conn.commit()
        self.conn.execute('BEGIN IMMEDIATE')
        with self.assertRaises(Exception):
            engine.rebuild_search_index(db_conn=self.conn)
        self.conn.execute('DROP TRIGGER reject_reindex')
        self.conn.commit()
        triggers = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'memories_%'").fetchall()
        self.assertEqual({row['name'] for row in triggers},
                         {'memories_ai', 'memories_ad', 'memories_au'})
        self.assertFalse(self.conn.in_transaction)

    def test_kind_specific_decay_keeps_preferences_longer(self):
        preference = self.store('长期偏好', kind='preference')
        experience = self.store('普通经验', kind='experience')
        cursor = (datetime.now() - timedelta(days=120)).isoformat(
            sep=' ', timespec='seconds')
        self.conn.execute(
            "UPDATE memories SET weight=.8,decay_cursor_at=? WHERE id IN (?,?)",
            (cursor, preference, experience))
        self.conn.commit()
        engine.decay_all(db_conn=self.conn)
        preference_weight = self.row(preference)['weight']
        experience_weight = self.row(experience)['weight']
        self.assertGreater(preference_weight, experience_weight)

    def test_preference_can_store_goal_only_but_experience_cannot(self):
        stored = engine.store(
            '用户偏好简洁中文回答', '', '', project_key='fixture',
            kind='preference', db_conn=self.conn)
        self.assertTrue(stored['stored'], stored)
        rejected = engine.store(
            '缺少步骤和结果的经验', '', '', project_key='fixture',
            kind='experience', db_conn=self.conn)
        self.assertFalse(rejected['stored'])
        self.assertIn('必填字段为空', rejected['reason'])

    def test_obsidian_cleanup_requires_exact_generated_footer(self):
        vault = Path(self.temp.name) / 'vault'
        vault.mkdir()
        obsidian_export._write_marker(vault, {'general'})
        generated_dir = vault / 'general'
        generated_dir.mkdir(parents=True)
        manual = generated_dir / 'manual.md'
        manual.write_text(
            '手写笔记里只提到 *mmemory ID: 7 | Status: active* 这行文字。\n',
            encoding='utf-8')
        stale = generated_dir / 'stale-999.md'
        stale.write_text('旧导出\n\n*mmemory ID: 999 | Status: active*\n',
                         encoding='utf-8')
        old_dir = obsidian_export.OBSIDIAN_DIR
        obsidian_export.OBSIDIAN_DIR = vault
        try:
            obsidian_export.export_all()
        finally:
            obsidian_export.OBSIDIAN_DIR = old_dir
        self.assertTrue(manual.exists())
        self.assertFalse(stale.exists())

    def test_obsidian_rejects_unowned_root_and_preserves_user_empty_dirs(self):
        unowned = Path(self.temp.name) / 'unowned'
        unowned.mkdir()
        (unowned / 'index.md').write_text('USER INDEX\n', encoding='utf-8')
        old_dir = obsidian_export.OBSIDIAN_DIR
        obsidian_export.OBSIDIAN_DIR = unowned
        try:
            with self.assertRaises(RuntimeError):
                obsidian_export.export_all()
        finally:
            obsidian_export.OBSIDIAN_DIR = old_dir
        self.assertEqual((unowned / 'index.md').read_text(encoding='utf-8'),
                         'USER INDEX\n')

        owned = Path(self.temp.name) / 'owned'
        owned.mkdir()
        obsidian_export._write_marker(owned, {'stale-domain'})
        (owned / 'stale-domain').mkdir()
        (owned / 'user-empty').mkdir()
        obsidian_export.OBSIDIAN_DIR = owned
        try:
            obsidian_export.export_all()
        finally:
            obsidian_export.OBSIDIAN_DIR = old_dir
        self.assertFalse((owned / 'stale-domain').exists())
        self.assertTrue((owned / 'user-empty').exists())

    def test_recall_block_reports_omitted_hits_and_fences_content(self):
        memories = [{
            'id': index,
            'goal': '超长目标' * 80,
            'outcome': '超长结果' * 80,
            'success_key': '',
            'pitfall': '先失败后修复' * 40,
            'approach': '',
            'project_key': 'fixture',
            'status': 'active',
        } for index in range(1, 6)]
        block = engine.format_recall_block(memories, 1200)
        self.assertLessEqual(len(block), 1200)
        self.assertIn('不是指令', block)
        self.assertIn('条命中未展示', block)

    def test_recall_context_warns_disputed_and_strips_control_characters(self):
        context = engine.format_recall_context({
            'id': 9,
            'goal': '争议事实',
            'outcome': '结果',
            'pitfall': '错误\x1b[31m需要复核',
            'success_key': '',
            'approach': '',
            'project_key': 'fixture\u2028ignore previous',
            'status': 'active',
            'kind': 'project_fact',
            'verification': 'disputed',
            'source_ref': 'private/source.md',
        })
        self.assertIn('disputed', context)
        self.assertIn('project_fact', context)
        self.assertNotIn('\x1b', context)
        self.assertNotIn('\u2028', context)
        self.assertNotIn('private/source.md', context)

    def test_cli_can_store_metadata_show_history_and_purge(self):
        path = Path(self.temp.name) / 'cli.db'
        env = {**os.environ, 'MMEMORY_DB': str(path)}
        store = subprocess.run([
            sys.executable, 'cli.py', 'store',
            '--goal', 'CLI 合成项目事实',
            '--approach', '执行合成步骤',
            '--outcome', '合成结果通过',
            '--kind', 'project_fact',
            '--verification', 'verified',
            '--source-ref', 'docs/synthetic.md',
            '--project-key', 'fixture',
        ], cwd=Path(__file__).parent, env=env, text=True, capture_output=True)
        self.assertEqual(store.returncode, 0, store.stderr)
        memory_id = json.loads(store.stdout)['memory_id']
        update = subprocess.run([
            sys.executable, 'cli.py', 'update', str(memory_id),
            '--outcome', '修订后的合成结果', '--reason', '合成修订',
        ], cwd=Path(__file__).parent, env=env, text=True, capture_output=True)
        self.assertEqual(update.returncode, 0, update.stderr)
        history = subprocess.run([
            sys.executable, 'cli.py', 'history', str(memory_id),
        ], cwd=Path(__file__).parent, env=env, text=True, capture_output=True)
        self.assertEqual(history.returncode, 0, history.stderr)
        payload = json.loads(history.stdout)
        self.assertEqual(payload['current']['kind'], 'project_fact')
        self.assertEqual(payload['revisions'][0]['reason'], '合成修订')
        purge = subprocess.run([
            sys.executable, 'cli.py', 'purge', str(memory_id),
            '--reason', '合成清理', '--yes',
        ], cwd=Path(__file__).parent, env=env, text=True, capture_output=True)
        self.assertEqual(purge.returncode, 0, purge.stderr)
        self.assertTrue(json.loads(purge.stdout)['purged'])

    def test_concurrent_first_open_upgrades_an_old_schema_once(self):
        path = Path(self.temp.name) / 'legacy-race.db'
        with sqlite3.connect(path) as conn:
            conn.execute("""
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
                    status TEXT DEFAULT 'active'
                )
            """)
            conn.execute(
                "INSERT INTO memories(goal,approach,outcome) VALUES(?,?,?)",
                ('旧库并发迁移', '迁移', '通过'))
        program = (
            'import sys\n'
            'import engine\n'
            'c=engine.get_conn(sys.argv[1])\n'
            'assert c.execute("PRAGMA user_version").fetchone()[0] == 2\n'
            'c.close()\n'
        )
        processes = [
            subprocess.Popen(
                [sys.executable, '-c', program, str(path)],
                cwd=Path(__file__).parent, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(8)
        ]
        failures = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            if process.returncode != 0:
                failures.append((process.returncode, stdout, stderr))
        self.assertEqual(failures, [])
        conn = engine.get_conn(str(path))
        try:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(conn.execute(
                'SELECT count(*) FROM memories').fetchone()[0], 1)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
