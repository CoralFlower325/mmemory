"""取消/恢复意图及旧式测试汇总的实行为回归；夹具均为临时脚本。"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import filter as memory_filter
import run_tests


class FilterRunnerTests(unittest.TestCase):
    def conversation(self, commands, outcome='已完成'):
        turns = [{'role': 'user', 'content': '实现合成功能'},
                 {'role': 'assistant', 'content': '开始实现'}]
        turns.extend({'role': role, 'content': text} for role, text in commands)
        return memory_filter.rule_filter(turns, outcome=outcome, has_code=True, tool_calls=5)

    def test_cancel_then_user_resume_completed(self):
        for resume in ('继续', '继续，重新完成该功能', '继续修复这个功能',
                       '恢复这个任务', '重新开始', 'continue this task'):
            with self.subTest(resume=resume):
                self.assertTrue(self.conversation([
                    ('user', '取消这个任务'), ('user', resume),
                    ('assistant', '已完成')])[0])

    def test_latest_cancel_still_rejects(self):
        self.assertFalse(self.conversation([
            ('user', '取消这个任务'), ('user', '继续'), ('user', '不用做了')])[0])

    def test_assistant_resume_does_not_override_user_cancel(self):
        self.assertFalse(self.conversation([
            ('user', '取消这个任务'), ('assistant', '继续')])[0])

    def test_feature_mentions_do_not_cancel_or_resume(self):
        self.assertTrue(self.conversation([('user', '实现取消订阅按钮')])[0])
        self.assertFalse(self.conversation([
            ('user', '取消这个任务'), ('user', '继续按钮还没实现')])[0])

    def test_final_cancelled_outcome_still_rejects(self):
        self.assertFalse(self.conversation([
            ('user', '取消这个任务'), ('user', '继续')], outcome='已取消')[0])

    def run_fixture(self, output, exit_code=0):
        with tempfile.TemporaryDirectory(prefix='mmemory-runner-regression-') as directory:
            path = Path(directory) / 'synthetic_test.py'
            path.write_text(f'print({output!r})\nraise SystemExit({exit_code})\n')
            return run_tests.run(path)

    def test_mixed_chinese_summary_cannot_hide_failure(self):
        for output in ('3通过/1失败', '3 通过 / 1 失败', '通过3，失败：1',
                       '3通过/0失败/1错误'):
            with self.subTest(output=output):
                rc, fails, passes = self.run_fixture(output)
                self.assertEqual(rc, 0)
                self.assertTrue(fails)
                self.assertGreater(passes, 0)

    def test_english_failures_cannot_hide_behind_pass(self):
        for output in ('3 passed / 1 failed', 'TESTS PASSED: 3 / FAILURES: 1',
                       'PASS\nFailures: 0; errors: 1', '3 tests passed / 1 test failed'):
            with self.subTest(output=output):
                rc, fails, passes = self.run_fixture(output)
                self.assertEqual(rc, 0)
                self.assertTrue(fails)
                self.assertGreater(passes, 0)

    def test_zero_failure_summaries_are_not_failures(self):
        for output in ('3通过/0失败', '3 passed / 0 failed',
                       'TESTS PASSED: 3 / FAILURES: 0', 'PASS\n失败：0；错误：0'):
            with self.subTest(output=output):
                rc, fails, passes = self.run_fixture(output)
                self.assertEqual(rc, 0)
                self.assertEqual(fails, [])
                self.assertGreater(passes, 0)

    def test_passed_failure_scenario_title_is_not_result_failure(self):
        rc, fails, passes = self.run_fixture('✅ PASS INSERT 失败不留半成品')
        self.assertEqual((rc, fails), (0, []))
        self.assertGreater(passes, 0)

    def test_nonzero_exit_remains_a_failure(self):
        with tempfile.TemporaryDirectory(prefix='mmemory-runner-exit-') as directory:
            path = Path(directory) / 'synthetic_test.py'
            path.write_text("print('PASS')\nraise SystemExit(2)\n")
            with patch.object(run_tests.sys, 'argv', ['run_tests.py', str(path)]):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(run_tests.main(), 1)
            self.assertIn('rc=2', output.getvalue())

    def test_standard_unittest_and_zero_execution(self):
        rc, fails, passes = self.run_fixture('Ran 2 tests in 0.001s\n\nOK')
        self.assertEqual((rc, fails), (0, []))
        self.assertEqual(passes, 2)
        _, _, zero_passes = self.run_fixture('Ran 0 tests in 0.000s\n\nOK')
        self.assertEqual(zero_passes, 0)

    def test_unittest_passed_failure_case_name_is_not_a_failure(self):
        output = (
            'test_restore_duplicate_conflict_returns_failure '
            '(synthetic.CliChecks.test_restore_duplicate_conflict_returns_failure) ... ok\n'
            'Ran 1 test in 0.001s\n\nOK'
        )
        self.assertEqual(self.run_fixture(output), (0, [], 1))

    def test_unittest_real_fail_and_error_results_still_fail(self):
        for status in ('FAIL', 'ERROR'):
            with self.subTest(status=status):
                rc, fails, passes = self.run_fixture(
                    f'test_synthetic (synthetic.Checks.test_synthetic) ... {status}')
                self.assertEqual(rc, 0)
                self.assertEqual(len(fails), 1)
                self.assertEqual(passes, 0)

    def test_explicit_pass_summary_reports_actual_count(self):
        for count in (0, 21):
            with self.subTest(count=count):
                rc, fails, passes = self.run_fixture(f'TESTS PASSED: {count}')
                self.assertEqual((rc, fails), (0, []))
                self.assertEqual(passes, count)


if __name__ == '__main__':
    unittest.main()
