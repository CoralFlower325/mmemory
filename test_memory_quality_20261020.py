"""2026-10-20 记忆可读性回归：泛词召回、项目隔离、摘要预算与写入长度。"""

import tempfile
import unittest
from pathlib import Path

import engine
import filter as memory_filter
import tagger


class MemoryQuality20261020Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mmemory-quality-20261020-')
        self.conn = engine.get_conn(str(Path(self.temp.name) / 'synthetic.db'))
        rows = [
            ('example-service', 'example-service 路由失败', '检查上游状态', '恢复路由',
             '先确认健康检查是否误报'),
            ('example-piano', 'example-piano 键位映射排错', '核对键位表', '映射恢复',
             '自动映射可能落到错误的八度'),
            ('network-fixture', '网络资产清单核对', '比对端口记录', '清单已恢复一致',
             '自动扫描可能遗漏离线设备'),
        ]
        for project, goal, approach, outcome, pitfall in rows:
            result = engine.store(goal, approach, outcome, pitfall=pitfall,
                                  project_key=project, db_conn=self.conn)
            self.assertTrue(result['stored'], result)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_generic_words_do_not_pull_unrelated_memories(self):
        self.assertEqual(engine.query_terms('实验 课程 问题 配置'), [])
        self.assertEqual(engine.recall('实验', db_conn=self.conn), [])

    def test_specific_term_and_project_scope_remove_cross_project_noise(self):
        result = engine.recall_with_explain('example-service 路由失败', db_conn=self.conn)
        self.assertEqual(result['explanation']['project_key'], 'example-service')
        self.assertEqual([m['project_key'] for m in result['memories']],
                         ['example-service'])

    def test_text_hit_suppresses_tag_only_candidates(self):
        engine.store('合成数据库迁移', '执行迁移', '迁移完成',
                     success_key='先备份再迁移', project_key='network-assets', db_conn=self.conn)
        result = engine.recall('example-service 路由失败', db_conn=self.conn)
        self.assertTrue(result)
        self.assertTrue(all('example-service' in ' '.join(
                                str(m[k] or '') for k in engine.CONTENT_FIELDS)
                            for m in result))

    def test_recall_block_has_bounded_compact_output(self):
        memories = engine.recall('example-service 路由失败', limit=5,
                                 db_conn=self.conn)
        block = engine.format_recall_block(memories, max_chars=500)
        self.assertLessEqual(len(block), 500)
        self.assertLessEqual(len(engine.format_recall_block(memories)), 1200)
        self.assertNotIn('方案：', block)
        self.assertIn('结果：', block)

    def test_overlong_and_lessonless_automatic_experience_are_rejected(self):
        fields = {'goal': '完成一项任务', 'approach': '方法', 'outcome': '结果',
                  'success_key': '', 'pitfall': ''}
        too_long = {**fields, 'outcome': '长' * (memory_filter.MAX_MEMORY_CHARS + 1)}
        self.assertFalse(memory_filter.validate_fields(
            too_long['goal'], too_long['approach'], too_long['outcome'])[0])
        messages = [{'role': 'user', 'content': '完成合成任务'},
                    {'role': 'assistant', 'content': '```text\nexecute\n```'},
                    {'role': 'user', 'content': '已经完成'}]
        ok, reason, _ = memory_filter.assess_messages(messages, fields)
        self.assertFalse(ok)
        self.assertIn('成功关键或踩坑解法', reason)
        ok, _, _ = memory_filter.assess_messages(
            messages, {**fields, 'success_key': '真实运行后通过验证'})
        self.assertTrue(ok)

    def test_known_projects_infer_one_namespace(self):
        self.assertEqual(tagger.infer_project_key('mmemory 查询优化'), 'mmemory')
        self.assertEqual(tagger.infer_project_key('example-service 配置'), 'example-service')
        self.assertEqual(tagger.infer_project_key('example-piano 键位'), 'example-piano')
        self.assertEqual(tagger.infer_project_key('普通实验课程问题'), '')


if __name__ == '__main__':
    unittest.main()
