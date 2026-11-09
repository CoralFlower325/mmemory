"""项目别名与边界回归；只检查纯文本，不打开真实记忆库。"""

import json
import tempfile
import unittest
from pathlib import Path

import tagger


class ProjectSignalsTests(unittest.TestCase):
    def assert_signal(self, signal, project):
        self.assertEqual(tagger.infer_project_key(f'为 {signal} 更新连接配置'), project)

    def test_memory_system_names(self):
        for signal in ('mmemory', 'MMemory', 'hindsight'):
            with self.subTest(signal=signal):
                self.assert_signal(signal, 'mmemory')

    def test_example_service_names(self):
        for signal in ('example-service', 'Example Service'):
            with self.subTest(signal=signal):
                self.assert_signal(signal, 'example-service')

    def test_example_piano_names(self):
        for signal in ('example-piano', 'Example Piano'):
            with self.subTest(signal=signal):
                self.assert_signal(signal, 'example-piano')

    def test_generic_tools_and_subjects_do_not_assign_project(self):
        for text in ('排查网络连接', '使用 Python 训练模型', '研究机器学习',
                     '完成实验一', '配置编辑器', '实现钢琴功能'):
            with self.subTest(text=text):
                self.assertEqual(tagger.infer_project_key(text), '')

    def test_custom_config_adds_write_and_query_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'projects.json'
            path.write_text(json.dumps({
                'projects': {
                    'sample-project': {
                        'write_aliases': ['sample-project', '样本工程'],
                        'query_aliases': ['样本主题'],
                    }
                }
            }, ensure_ascii=False), encoding='utf-8')
            write, query = tagger.load_project_config(path)
        self.assertIn(('sample-project', ('sample-project', '样本工程')), write)
        self.assertIn(('sample-project', ('样本主题',)), query)


if __name__ == '__main__':
    unittest.main()
