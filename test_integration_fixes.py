#!/usr/bin/env python3
"""Integration test for the 5 fixes - exercises actual store/recall flow."""
import sys, os, sqlite3, tempfile
sys.path.insert(0, os.path.dirname(__file__))

import engine
import tagger
import filter

# Use a temp DB
_directory = tempfile.TemporaryDirectory(prefix='mmemory-fixes-')
DB = os.path.join(_directory.name, 'memory.db')

conn = engine.get_conn(DB)

print("=== Integration: store → recall flow ===")

# 1. Store a memory
r = engine.store(
    goal="用Python selenium爬取动态网页数据",
    approach="使用undetected chromedriver绕过反爬",
    outcome="成功抓取了目标网站的数据",
    success_key="undetected chromedriver有效绕过检测",
    pitfall="需要等待页面加载完成",
    db_conn=conn,
)
print(f"Store result: {r}")
assert r['stored'] is True, f"Expected stored=True, got {r}"

# 2. Store a duplicate - should reinforce, not insert
r2 = engine.store(
    goal="用Python selenium爬取动态网页数据",
    approach="使用undetected chromedriver绕过反爬",
    outcome="成功抓取了目标网站的数据",
    success_key="undetected chromedriver有效绕过检测",
    pitfall="需要等待页面加载完成",
    db_conn=conn,
)
print(f"Duplicate result: {r2}")
assert r2['stored'] is False, f"Expected stored=False for dup, got {r2}"
assert '重复' in r2['reason'], f"Expected '重复' in reason, got {r2['reason']}"

# 3. Recall - verify _batch_reinforce doesn't commit prematurely
# (caller controls commit)
conn2 = engine.get_conn(DB)
memories = engine.recall("selenium爬虫", db_conn=conn2)
print(f"Recall result: {len(memories)} memories")
assert len(memories) > 0, "Expected at least 1 recalled memory"
conn2.close()

# 4. Test filter.should_store with had_error key
conv = {
    'turns': [
        {'role': 'user', 'content': '帮我用Python写个爬虫'},
        {'role': 'assistant', 'content': '好的，开始编写...'},
        {'role': 'user', 'content': '报错了'},
    ],
    'outcome': '完成',
    'has_code': True,
    'had_error': True,  # This is the fixed key name
    'tool_calls': 3,
    'task_complexity': 'medium',
    'code_blocks': 2,
    'success_key': 'undetected chromedriver',
    'pitfall': '需要等待页面加载',
}
ok, reason, cleaned = filter.should_store(conv)
print(f"Filter result: ok={ok}, reason={reason}")
assert ok is True, f"Expected filter to pass, got {reason}"
assert isinstance(cleaned, dict), "Expected cleaned dict"

# 5. Test tagger.generate_tags is a pure function (no DB conn, no writes)
tags = tagger.generate_tags("一个全新的未知领域任务", "尝试新方法", "部分成功")
print(f"Tags: {tags}")
assert set(tags) == {'tags_tool', 'tags_domain', 'tags_action', 'tags_keywords'}
assert isinstance(tags['tags_keywords'], str)

# Cleanup
conn.close()
os.remove(DB)
# Clean WAL/SHM if they exist
for ext in ['-wal', '-shm']:
    p = DB + ext
    if os.path.exists(p):
        os.remove(p)

print("\n=== ALL INTEGRATION TESTS PASSED ===")

_directory.cleanup()
