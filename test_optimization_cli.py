"""新维护与会话过滤 CLI 合同，子进程只接合成库。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import engine


ROOT = Path(__file__).parent


class CliOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mmemory-optimization-cli-')
        self.path = Path(self.temp.name) / 'synthetic.db'
        self.conn = engine.get_conn(str(self.path))

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / 'cli.py'), *map(str, args)],
                                env=dict(os.environ, MMEMORY_DB=str(self.path)),
                                cwd=ROOT, text=True, capture_output=True)
        return result.returncode, json.loads(result.stdout)

    def add(self, goal='合成任务', project='synthetic'):
        return engine.store(goal, '实施方法', '已完成', project_key=project,
                            db_conn=self.conn)['memory_id']

    def transcript(self, content):
        path = Path(self.temp.name) / 'messages.json'
        path.write_text(json.dumps(content, ensure_ascii=False), encoding='utf-8')
        return path

    def test_manual_store_remains_authorized_without_transcript(self):
        rc, data = self.cli('store', '--goal', '人工独立事实', '--approach', '人工核实',
                            '--outcome', '已确认', '--project-key', 'synthetic')
        self.assertEqual(rc, 0)
        self.assertTrue(data['stored'])

    def test_transcript_pending_rejects_before_write(self):
        transcript = self.transcript({'messages': [
            {'role': 'user', 'content': '实现功能'},
            {'role': 'assistant', 'content': '```python\nprint(1)\n```'},
            {'role': 'user', 'content': '继续'}]})
        rc, data = self.cli('store', '--goal', '临时任务', '--approach', '实施中',
                            '--outcome', '进行中', '--conversation', transcript,
                            '--task-state', 'in_progress')
        self.assertNotEqual(rc, 0)
        self.assertFalse(data['stored'])
        self.assertEqual(engine.get_stats(self.conn)['total'], 0)

    def test_preference_transcript_is_quality_checked(self):
        transcript = self.transcript([
            {'role': 'user', 'content': '以后单独目录'},
            {'role': 'assistant', 'content': '按版本组织'},
            {'role': 'user', 'content': '长期保持'}])
        rc, data = self.cli('store', '--goal', '合成交付偏好', '--approach', '每版目录',
                            '--outcome', '用户明确要求', '--conversation', transcript,
                            '--memory-kind', 'preference', '--project-key', 'synthetic')
        self.assertEqual(rc, 0)
        self.assertTrue(data['stored'])

    def test_declared_quality_flags_cannot_silently_skip_context(self):
        rc, data = self.cli('store', '--goal', '临时', '--approach', '方法',
                            '--outcome', '结果', '--task-state', 'in_progress')
        self.assertNotEqual(rc, 0)
        self.assertFalse(data['stored'])

    def test_protect_unprotect_and_batch_refusal(self):
        mid = self.add()
        rc, data = self.cli('protect', mid, 999, '--reason', '明确重要')
        self.assertNotEqual(rc, 0)
        self.assertFalse(data['changed'])
        self.assertEqual(self.conn.execute('SELECT protected FROM memories WHERE id=?', (mid,)).fetchone()[0], 0)
        self.assertEqual(self.cli('protect', mid, '--reason', '明确重要')[0], 0)
        self.assertEqual(self.cli('unprotect', mid)[0], 0)

    def test_link_relations_and_cross_project_refusal(self):
        source, target, foreign = self.add('当前状态'), self.add('历史状态'), self.add('其他项目', 'other')
        rc, data = self.cli('link', source, target, '--relation', 'supersedes', '--reason', '后续明确更正')
        self.assertEqual(rc, 0)
        self.assertTrue(data['linked'])
        rc, data = self.cli('relations', target)
        self.assertEqual(rc, 0)
        self.assertEqual(data[0]['source_id'], source)
        self.assertNotEqual(self.cli('link', source, foreign, '--relation', 'supersedes', '--reason', '无效')[0], 0)

    def test_review_read_only_shows_relevant_relation(self):
        source, target = self.add('当前状态'), self.add('历史状态')
        engine.link_memories(source, target, 'supersedes', '已有证据', db_conn=self.conn)
        engine.archive_memories([target], '被当前状态取代', db_conn=self.conn)
        before = list(self.conn.iterdump())
        rc, data = self.cli('review', '--project-key', 'synthetic')
        self.assertEqual(rc, 0)
        self.assertTrue(data['read_only'])
        self.assertEqual(data['relations'][0]['target_id'], target)
        self.assertEqual(before, list(self.conn.iterdump()))


if __name__ == '__main__':
    unittest.main()
