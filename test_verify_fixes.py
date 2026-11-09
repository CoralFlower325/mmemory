#!/usr/bin/env python3
"""Verify all 5 code-level fixes."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

errors = []

# 1. Test cli.py key name fix: had_error (not has_error)
print("=== Test 1: cli.py key name 'had_error' ===")
with open(os.path.join(os.path.dirname(__file__), 'cli.py')) as f:
    cli_src = f.read()
if "'has_error'" in cli_src and "'had_error'" not in cli_src:
    errors.append("FAIL: cli.py still uses 'has_error' key")
elif "'had_error'" in cli_src:
    print("  PASS: cli.py uses 'had_error' key")
else:
    errors.append("FAIL: cli.py key not found")

# 2. Test cli.py unpacking: ok, reason, _ = ...
print("=== Test 2: cli.py unpacks 3 values from should_store ===")
if "ok, reason, _ = filter.should_store" in cli_src:
    print("  PASS: cli.py unpacks 3 values")
elif "ok, reason = filter.should_store" in cli_src:
    errors.append("FAIL: cli.py still unpacks only 2 values")
else:
    errors.append("FAIL: cli.py should_store call not found")

# 3. Test engine.py _batch_reinforce has no commit
print("=== Test 3: engine.py _batch_reinforce no commit ===")
with open(os.path.join(os.path.dirname(__file__), 'engine.py')) as f:
    engine_src = f.read()
# Extract _batch_reinforce function
import re
batch_fn = re.search(r'def _batch_reinforce\(.*?\n(?=def |\Z)', engine_src, re.DOTALL)
if batch_fn:
    fn_text = batch_fn.group()
    if 'conn.commit()' in fn_text:
        errors.append("FAIL: _batch_reinforce still has conn.commit()")
    else:
        print("  PASS: _batch_reinforce has no commit")
else:
    errors.append("FAIL: _batch_reinforce function not found")

# 独立的 reinforce() 已移除：显式强化走 recall(reinforce=True)，它在内部
# 自行提交，由 test_round3_deep.py 的 2a/2b/2c 覆盖。
assert 'def reinforce(' not in engine_src, "reinforce() 应已删除（无调用方）"
print("  PASS: reinforce() 已移除，显式强化由 recall(reinforce=True) 负责")

# 4. Test tagger.py generate_tags has no commit
print("=== Test 4: tagger.py generate_tags no commit ===")
with open(os.path.join(os.path.dirname(__file__), 'tagger.py')) as f:
    tagger_src = f.read()
gen_fn = re.search(r'def generate_tags\(.*?\n(?=def |\Z)', tagger_src, re.DOTALL)
if gen_fn:
    fn_text = gen_fn.group()
    if 'db_conn.commit()' in fn_text or '.commit()' in fn_text:
        errors.append("FAIL: generate_tags still has commit()")
    else:
        print("  PASS: generate_tags has no commit")
else:
    errors.append("FAIL: generate_tags function not found")

# 5. Test fallback_tokenize exists in tagger.py and is imported by filter.py
print("=== Test 5: fallback_tokenize shared function ===")
if 'def fallback_tokenize' in tagger_src:
    print("  PASS: fallback_tokenize defined in tagger.py")
else:
    errors.append("FAIL: fallback_tokenize not in tagger.py")

with open(os.path.join(os.path.dirname(__file__), 'filter.py')) as f:
    filter_src = f.read()
if 'from tagger import fallback_tokenize' in filter_src:
    print("  PASS: filter.py imports fallback_tokenize from tagger")
else:
    errors.append("FAIL: filter.py doesn't import fallback_tokenize")

if 'fallback_tokenize' in filter_src and 're.findall' not in filter_src.split('def _tokenize')[1].split('def ')[0]:
    print("  PASS: filter.py _tokenize uses fallback_tokenize (no duplicate regex)")
else:
    # Check more carefully
    tokenize_section = filter_src[filter_src.index('def _tokenize'):]
    tokenize_section = tokenize_section[:tokenize_section.index('\ndef ')]
    if 'fallback_tokenize' in tokenize_section and 're.findall' not in tokenize_section:
        print("  PASS: filter.py _tokenize uses fallback_tokenize")
    else:
        errors.append("FAIL: filter.py _tokenize still has duplicate regex")

# 6. Functional import test
print("\n=== Test 6: Import chain works ===")
try:
    import tagger
    import filter
    import engine
    print("  PASS: All modules import successfully")
except Exception as e:
    errors.append(f"FAIL: Import error: {e}")

# 7. Functional test: fallback_tokenize
print("=== Test 7: fallback_tokenize works ===")
from tagger import fallback_tokenize
result = fallback_tokenize("使用Python爬虫抓取数据")
assert isinstance(result, list), "Should return list"
assert len(result) > 0, "Should return non-empty list"
print(f"  PASS: fallback_tokenize returned {result}")

# 8. Functional test: filter._tokenize uses fallback
print("=== Test 8: filter._tokenize uses fallback_tokenize ===")
from filter import _tokenize
result = _tokenize("使用Python爬虫抓取数据")
assert isinstance(result, set), "Should return set"
print(f"  PASS: _tokenize returned {result}")

# Summary
print("\n" + "=" * 50)
if errors:
    print(f"FAILURES: {len(errors)}")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
    sys.exit(0)
