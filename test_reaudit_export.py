"""Synthetic export regression: ordinary technical goals produce real links."""
import contextlib
import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import engine
import obsidian_export


class ExportLinkRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mmemory-export-links-')
        self.root = Path(self.directory.name)
        self.db = self.root / 'synthetic.db'
        self.vault = self.root / 'vault'
        self.conn = engine.get_conn(str(self.db))

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def store(self, goal):
        result = engine.store(goal, '运行合成维护步骤', '合成任务完成',
                              project_key='fixture', db_conn=self.conn)
        self.assertTrue(result['stored'], result)
        return dict(self.conn.execute('SELECT * FROM memories WHERE id=?',
                                       (result['memory_id'],)).fetchone())

    def export(self):
        with patch.object(engine, 'DB_PATH', self.db), \
             patch.object(obsidian_export, 'OBSIDIAN_DIR', self.vault), \
             contextlib.redirect_stdout(io.StringIO()):
            obsidian_export.export_all()

    def links(self):
        lines = (self.vault / 'index.md').read_text(encoding='utf-8').splitlines()
        result = []
        for line in lines:
            if not line.startswith('- '):
                continue
            # Parse the link shape rather than counting Markdown files: a file
            # can exist while a # fragment or ] delimiter points somewhere else.
            match = re.fullmatch(r'- \[((?:\\.|[^\\\]])*)\]\(([^)]*)\)', line)
            self.assertIsNotNone(match, line)
            label, destination = match.groups()
            url = urlsplit(destination)
            self.assertEqual((url.scheme, url.netloc, url.query, url.fragment), ('', '', '', ''))
            self.assertNotIn(' ', destination)
            filepath = self.vault / unquote(url.path)
            self.assertTrue(filepath.is_file(), destination)
            result.append((label, destination, filepath))
        return result

    def test_normal_technical_goals_link_to_each_actual_memory(self):
        goals = [
            '修复 C# 调用与 items[0] 错误',
            '检查 [[旧笔记]] 与 A|B 参数',
            r'修复 C:\路径\数组[1] 的分隔',
            '完成 1:5 比例和功能 (preview)',
            '目标第一行\n第二行 [数组] | 标签',
            '修复 foo#bar ](其他路径) 引用',
        ]
        rows = [self.store(goal) for goal in goals]
        self.export()
        links = self.links()
        self.assertEqual(len(links), len(rows))
        by_filename = {filepath.name: (label, destination, filepath)
                       for label, destination, filepath in links}
        for row in rows:
            filename = obsidian_export.filename_for(row)
            label, destination, filepath = by_filename[filename]
            text_label = re.sub(r'\\([\\\[\]])', r'\1', label)
            self.assertEqual(text_label, re.sub(r'\s+', ' ', row['goal'][:60]))
            self.assertIn(f"*mmemory ID: {row['id']} |", filepath.read_text(encoding='utf-8'))
            self.assertTrue(destination.endswith('.md'))
        self.assertTrue(any('%23' in destination for _, destination, _ in links))
        self.assertTrue(any('%5B' in destination and '%5D' in destination
                            for _, destination, _ in links))

    def test_generated_file_names_are_kept_and_handwritten_notes_survive_cleanup(self):
        row = self.store('修复 AGENTS.md 的 C# 和 array[0]')
        self.export()
        links = self.links()
        generated = links[0][2]
        self.assertEqual(generated.name, obsidian_export.filename_for(row))
        handwritten = generated.parent / '我的手写记录.md'
        handwritten.write_text('这条笔记由用户维护。', encoding='utf-8')
        stale = generated.parent / '已经过期的生成文件.md'
        stale.write_text('*mmemory ID: 999999 | Status: active*', encoding='utf-8')
        self.export()
        self.assertTrue(generated.exists())
        self.assertEqual(handwritten.read_text(encoding='utf-8'), '这条笔记由用户维护。')
        self.assertFalse(stale.exists())
        self.assertEqual(len(self.links()), 1)

    def test_empty_active_set_cleans_generated_files_and_index(self):
        self.store('导出 C# [缓存] 的历史记录')
        self.export()
        generated = self.links()[0][2]
        self.conn.execute('DELETE FROM memories')
        self.conn.commit()
        self.export()
        self.assertFalse(generated.exists())
        self.assertEqual(self.links(), [])
        self.assertIn('*总记忆数: 0*', (self.vault / 'index.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ExportLinkRegressions))
    if result.wasSuccessful():
        print(f'TESTS PASSED: {result.testsRun}')
    raise SystemExit(0 if result.wasSuccessful() else 1)
