"""Schema v2, retrieval, relation, and history regressions on synthetic data."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
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

    def test_identifier_in_contrastive_pitfall_does_not_outrank_current_version(self):
        old = self.store('demo v2 部署', outcome='端口 8080')
        new = self.store('demo v3 部署', outcome='端口 9090',
                         pitfall='v3 与 v2 插件不兼容')
        results = engine.recall('demo v2 部署', project_key='fixture',
                                db_conn=self.conn)
        self.assertEqual(results[0]['id'], old)
        if len(results) > 1:
            self.assertEqual(results[1]['id'], new)

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

    def test_custom_synonym_file_is_bidirectional(self):
        path = Path(self.temp.name) / 'synonyms.json'
        path.write_text(json.dumps({
            'equivalents': [['重启', '重啟', '重新启动']]
        }, ensure_ascii=False), encoding='utf-8')
        with patch.dict(os.environ, {'MMEMORY_SYNONYMS_FILE': str(path)}):
            table = tagger._load_term_equivalents()
        self.assertIn('重啟', table['重启'])
        self.assertIn('重启', table['重啟'])

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

    def test_obsidian_cleanup_requires_exact_generated_footer(self):
        vault = Path(self.temp.name) / 'vault'
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
