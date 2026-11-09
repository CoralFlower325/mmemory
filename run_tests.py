#!/usr/bin/env python3
"""测试运行器：在合成临时库中运行 test_*.py，并核对退出码及结果标记。

套件同时包含硬断言回归和旧式「打印 PASS/FAIL」测试。运行器不能替测试文件
补上缺失断言；新增行为应由 assert / unittest 和非零失败退出码直接判断。

这一版改为**双向核对计数**：
  - 统计输出里的通过数（PASS）与失败数（FAIL，放宽大小写与中英文措辞）；
  - 要求退出码为 0 且 **通过数 > 0**，否则视为「测试没有真正执行」；
  - 任一失败计数、非零退出码、崩溃/Traceback、超时都判失败。

完全没有通过标记的空转会判失败；部分断言是否被跳过仍取决于测试自身。

**已知局限**：无法识破「主动打印 PASS 然后 exit(0)」的测试——那是测试文件自身
在撒谎，任何基于输出的判定都看不出来。真正的护栏是测试自己用 assert / sys.exit，
本运行器是给尚未改造的旧式测试兜底。

用法：.venv/bin/python run_tests.py [可选的文件名...]
退出码：0 全部通过；1 有失败。
"""
import re
import os
import tempfile
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
PY = Path(sys.executable)

# 这两个不是断言式测试：test_edge.py 是探查脚本，run_tests.py 是自己。
SKIP = {'test_edge.py', 'run_tests.py'}

# 失败标记分强弱，避免误报：
#   强标记：本身就是失败信号，出现即判失败。`Traceback` 只在行首/独立出现时才
#          算强标记——测试打印的「数据正文」里也可能含这个词（曾误报）。
#   弱标记：中文/英文里可能是正常叙述（"PASS 错误处理分支"），只在同一行没有
#          PASS 标记时才视为失败。宁可多报噪音，也不漏判真实失败。
STRONG_FAIL_RE = re.compile(
    r'(❌|✗|✘|FAIL(?:ED|URE)?\b|AssertionError|panic|'
    r'(?<![不未无])失败|不通过|未通过)',
    re.IGNORECASE)
# Traceback 单独判定：必须出现在行首或前导空白后（真实 traceback 的形态），
# 不匹配出现在引号/正文中间的同一个词。
TRACEBACK_RE = re.compile(r'^\s*Traceback \(most recent call last\)', re.MULTILINE)
# 弱标记：中文/英文失败词后跟冒号与数字（「断言错误: 2 处」「系统异常: 1 个」
# 「ERROR: xxx」）。不要求前导边界——中文复合词前一个字符也是汉字，用 `\w`
# 做边界会永远不成立。
SOFT_FAIL_RE = re.compile(
    r'(?:ERROR|Exception|错误|异常)\s*[:：]\s*\S',
    re.IGNORECASE)
FAIL_RE = STRONG_FAIL_RE   # 供外部/兼容引用
# 通过标记：要求是「报告一项通过」的形态（行首 PASS / ✅ / `N 通过`），
# 避免把正文里的「通过」二字当通过计数。
PASS_RE = re.compile(
    r'(✅|^\s*PASS(?:ED)?\b|\d+\s*通过|通过\s*\d+|TESTS?\s+PASSED|'
    r'\d+\s*(?:tests?\s+)?pass(?:ed)?\b)',
    re.IGNORECASE | re.MULTILINE)
# 汇总中的非零失败计数必须独立判断，不能被同行 PASS/通过覆盖。
FAIL_COUNT_WORD = r'(?:失败|错误|异常|fail(?:ed|ures?|s)?\b|errors?\b|exceptions?\b)'
FAIL_COUNT_RE = re.compile(
    rf'(?<!\d)(?P<before>\d+|零)\s*(?:项|个|处|tests?)?\s*{FAIL_COUNT_WORD}'
    rf'|{FAIL_COUNT_WORD}\s*[:：=]?\s*(?P<after>\d+|零)(?!\d)',
    re.IGNORECASE)
# unittest 的结果在行尾；用例名中的 failure/error 是被测场景，不能当结果。
UNITTEST_RESULT_RE = re.compile(
    r'^\s*\S+\s+\([^\n]+\)\s+\.\.\.\s+(?P<result>ok|FAIL|ERROR)\s*$')


def find_tests(explicit):
    if explicit:
        return [Path(t) if Path(t).is_absolute() else ROOT / t for t in explicit]
    return sorted(p for p in ROOT.glob('test_*.py') if p.name not in SKIP)


def run(path):
    with tempfile.TemporaryDirectory(prefix='mmemory-suite-') as directory:
        env = dict(os.environ, MMEMORY_DB=str(Path(directory) / 'synthetic.db'))
        proc = subprocess.run(
            [str(PY), str(path)], cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=600)
    out = proc.stdout + proc.stderr
    fails = []
    unittest_passes = 0
    for ln in out.splitlines():
        unittest_result = UNITTEST_RESULT_RE.fullmatch(ln)
        if unittest_result:
            if unittest_result.group('result') == 'ok':
                unittest_passes += 1
            else:
                fails.append(ln.strip())
            continue
        counts = [match.group('before') or match.group('after')
                  for match in FAIL_COUNT_RE.finditer(ln)]
        if any(value != '零' and int(value) > 0 for value in counts):
            fails.append(ln.strip())
            continue
        # 去掉已确认的零计数，而非忽略整行；同行其它失败信号仍需保留。
        checked = FAIL_COUNT_RE.sub('', ln)
        has_pass = bool(PASS_RE.search(ln))
        # 明确报告「某项通过」的行一律不算失败行：测试标题里常含「失败」二字
        # （例如「INSERT 失败不留半成品: PASS」），那是被测场景而非测试结果。
        # 真正的失败会带 ❌/FAIL/Traceback 等强标记，或不含 PASS 计数。
        if has_pass and not re.search(r'(❌|✗|✘|\bFAIL(?:ED|URE)?\b|'
                                     r'Traceback|AssertionError)', checked, re.IGNORECASE):
            continue
        if STRONG_FAIL_RE.search(checked) or TRACEBACK_RE.search(checked):
            hit = True
        elif SOFT_FAIL_RE.search(checked) and not has_pass:
            hit = True
        else:
            hit = False
        if not hit:
            continue
        fails.append(ln.strip())
    passes = [ln for ln in out.splitlines() if PASS_RE.search(ln)]
    n_pass = len(passes) + unittest_passes
    # 汇总已经包含逐项结果，不能再按汇总行数计数或重复累加。
    summary = re.search(r'^\s*TESTS? PASSED\s*[:：=]?\s*(\d+)\s*$',
                        out, re.IGNORECASE | re.MULTILINE)
    if summary:
        n_pass = int(summary.group(1))
    unittest_count = re.search(r'^Ran (\d+) tests? in ', out, re.MULTILINE)
    if unittest_count and re.search(r'^OK\s*$', out, re.MULTILINE):
        n_pass = int(unittest_count.group(1))
    return proc.returncode, fails, n_pass


def main():
    explicit = [a for a in sys.argv[1:] if not a.startswith('-')]
    tests = find_tests(explicit)
    if not tests:
        print("没有找到测试文件")
        return 1

    failed = []
    for t in tests:
        try:
            rc, fails, n_pass = run(t)
        except subprocess.TimeoutExpired:
            print(f"FAIL  {t.name}  (超时)")
            failed.append(t.name)
            continue

        reasons = []
        if rc != 0:
            reasons.append(f"rc={rc}")
        if fails:
            reasons.append(f"{len(fails)} 处失败")
        # 核心护栏：没有任何通过计数 = 测试没真正执行（空转/跳过断言）
        if n_pass == 0:
            reasons.append("没有通过计数（测试可能被空转或跳过断言）")

        if reasons:
            failed.append(t.name)
            print(f"FAIL  {t.name}  ({', '.join(reasons)})")
            for ln in fails[:10]:
                print(f"        {ln}")
        else:
            print(f"ok    {t.name}  ({n_pass} 项通过)")

    print()
    if failed:
        print(f"{len(failed)}/{len(tests)} 个测试文件失败: {', '.join(failed)}")
        return 1
    print(f"全部 {len(tests)} 个测试文件通过")
    return 0


if __name__ == '__main__':
    sys.exit(main())
