#!/usr/bin/env python3
"""
第三轮深度审查测试：
1) _batch_reinforce commit 回归检测
2) 空字符串/超长/特殊字符边界
3) _recall_by_like SQL LIKE 通配符注入
4) check_duplicate 空 keywords 处理
5) FTS5 转义完整性
6) store/reinforce 事务一致性
"""
import sys, os, tempfile, sqlite3, math, threading, time
sys.path.insert(0, os.path.dirname(__file__))

errors = []

def test(name, priority, passed, detail=""):
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"  {status} [{priority}] {name}: {detail}")
    if not passed:
        errors.append(f"[{priority}] {name}: {detail}")

# ── SETUP ──────────────────────────────────────────────────────
import engine
import tagger
import filter as mm_filter

# Use temp DB
tmp_db = tempfile.mktemp(suffix='.db')
engine.DB_PATH = type(engine.DB_PATH)(tmp_db)
engine._schema_initialized.discard(tmp_db)

print("=" * 70)
print("第三轮深度审查测试")
print("=" * 70)

# ── 1. sqlite3 close() 行为验证 ──────────────────────────────
print("\n[1] sqlite3 事务行为验证")

tmp_check = tempfile.mktemp(suffix='.db')
conn_check = sqlite3.connect(tmp_check)
conn_check.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)')
conn_check.execute("INSERT INTO t (val) VALUES ('hello')")
conn_check.close()

conn_check2 = sqlite3.connect(tmp_check)
try:
    row = conn_check2.execute('SELECT val FROM t').fetchone()
    close_behavior = "auto_commit" if row else "auto_rollback"
except:
    close_behavior = "auto_rollback"
conn_check2.close()
os.unlink(tmp_check)
print(f"  INFO: sqlite3 close() behavior = {close_behavior}")

# ── 2. _batch_reinforce commit 回归检测 ──────────────────────
print("\n[2] _batch_reinforce commit 回归检测")

# Test 2a: recall(reinforce=True) with own connection — reinforcement must persist
conn_setup = engine.get_conn(tmp_db)
r = engine.store("recall_commit_test_goal", "test_approach", "test_outcome", db_conn=conn_setup)
conn_setup.commit()
mem_id = r['memory_id']

# Get initial state
row_before = conn_setup.execute(
    "SELECT weight, use_count FROM memories WHERE id=?", (mem_id,)).fetchone()
initial_weight = row_before['weight']
initial_use = row_before['use_count']
conn_setup.close()

# Recall with own connection (db_conn=None)
engine.recall("recall_commit_test_goal", reinforce=True)

# Check if reinforcement persisted
conn_check = engine.get_conn(tmp_db)
row_after = conn_check.execute(
    "SELECT weight, use_count FROM memories WHERE id=?", (mem_id,)).fetchone()
new_weight = row_after['weight']
new_use = row_after['use_count']
conn_check.close()

weight_changed = abs(new_weight - initial_weight) > 0.001
use_changed = new_use > initial_use
test("_batch_reinforce: recall standalone 权重持久化",
     "P0", weight_changed,
     f"weight: {initial_weight:.4f} → {new_weight:.4f}")
test("_batch_reinforce: recall standalone use_count 持久化",
     "P0", use_changed,
     f"use_count: {initial_use} → {new_use}")

# Test 2b: recall(reinforce=True) with external db_conn — reinforcement must be
# committed by recall itself, not left for the caller (a caller that closes
# without commit would silently lose the score).
conn_ext = engine.get_conn(tmp_db)
row_before2 = conn_ext.execute(
    "SELECT weight, use_count FROM memories WHERE id=?", (mem_id,)).fetchone()
w_before2 = row_before2['weight']
u_before2 = row_before2['use_count']
engine.recall("recall_commit_test_goal", db_conn=conn_ext, reinforce=True)
row_after2 = conn_ext.execute(
    "SELECT weight, use_count FROM memories WHERE id=?", (mem_id,)).fetchone()
w_after2 = row_after2['weight']
test("_batch_reinforce: recall with external conn 已提交",
     "P0", w_after2 > w_before2,
     f"weight: {w_before2:.4f} → {w_after2:.4f}")
conn_ext.close()

# 换一个新连接读，确认不是只在原连接事务里可见
conn_ext2 = engine.get_conn(tmp_db)
row_persist = conn_ext2.execute(
    "SELECT weight FROM memories WHERE id=?", (mem_id,)).fetchone()
test("_batch_reinforce: 强化跨连接持久化",
     "P0", row_persist['weight'] > w_before2,
     f"weight after reopen: {row_persist['weight']:.4f}")
conn_ext2.close()

# Test 2c: queue_prefetch pattern — background thread recall
print("\n[2c] queue_prefetch 背景线程 recall 强化持久性")
prefetch_weight_before = new_weight  # from test 2a result

def prefetch_sim():
    local_conn = engine.get_conn(tmp_db)
    try:
        engine.recall("recall_commit_test_goal", db_conn=local_conn, reinforce=True)
    finally:
        local_conn.close()

t = threading.Thread(target=prefetch_sim)
t.start()
t.join()

conn_check3 = engine.get_conn(tmp_db)
row_pf = conn_check3.execute(
    "SELECT weight, use_count FROM memories WHERE id=?", (mem_id,)).fetchone()
pf_weight = row_pf['weight']
pf_use = row_pf['use_count']
conn_check3.close()

test("queue_prefetch: 背景线程 close() 后强化是否持久化",
     "P0", pf_weight > prefetch_weight_before or pf_use > new_use,
     f"weight: {prefetch_weight_before:.4f} → {pf_weight:.4f}, use: {new_use} → {pf_use}")

# ── 3. 空字符串边界 ──────────────────────────────────────────
print("\n[3] 空字符串/空白边界")

# 3a: 空 goal
r = engine.store("", "approach", "outcome")
test("store: 空 goal 被拒绝", "P0", not r['stored'], f"reason={r['reason']}")

# 3b: 纯空格 goal
r = engine.store("   ", "approach", "outcome")
test("store: 纯空格 goal 被拒绝", "P0", not r['stored'], f"reason={r['reason']}")

# 3c: 空 approach
r = engine.store("goal", "", "outcome")
test("store: 空 approach 被拒绝", "P0", not r['stored'], f"reason={r['reason']}")

# 3d: 空 outcome
r = engine.store("goal", "approach", "")
test("store: 空 outcome 被拒绝", "P0", not r['stored'], f"reason={r['reason']}")

# 3e: 空查询 recall
results = engine.recall("")
test("recall: 空字符串查询不崩溃", "P1", isinstance(results, list), f"results={len(results)}")

# 3f: 纯空格查询 recall
results = engine.recall("   ")
test("recall: 纯空格查询不崩溃", "P1", isinstance(results, list), f"results={len(results)}")

# 3g: validate_fields 全空
passed, reason, cleaned = mm_filter.validate_fields("", "", "", "", "")
test("validate_fields: 全空返回 False", "P1", not passed, f"reason={reason}")

# 3h: validate_fields optional 全空
passed, reason, cleaned = mm_filter.validate_fields("goal", "approach", "outcome", "", "")
test("validate_fields: optional 全空返回 True", "P1", passed and cleaned['success_key'] == '' and cleaned['pitfall'] == '')

# ── 4. check_duplicate 空 keywords ────────────────────────────
print("\n[4] check_duplicate 边界")

conn_dup = engine.get_conn(tmp_db)

# 4a: 插入一条 keywords 为空的记忆
conn_dup.execute("""
    INSERT INTO memories (goal, approach, outcome, tags_tool, tags_domain, tags_action, tags_keywords)
    VALUES ('empty_kw_test', '', '', 'general', 'general', 'general', '')
""")
conn_dup.commit()

# 4b: keywords 为空时，判据改由目标文本承担——goal 完全相同就是真重复
empty_tags = {'tags_tool': 'general', 'tags_domain': 'general', 'tags_action': 'general', 'tags_keywords': ''}
dup_id = engine.check_duplicate(conn_dup, empty_tags, "empty_kw_test")
test("check_duplicate: 空 keywords 但 goal 相同判重复", "P1", dup_id is not None, f"dup_id={dup_id}")

# 4b2: keywords 为空且 goal 不同 → 不应误判
dup_id_diff = engine.check_duplicate(conn_dup, empty_tags, "totally_different_goal_xyz")
test("check_duplicate: 空 keywords + 不同 goal 不误判", "P1", dup_id_diff is None, f"dup_id={dup_id_diff}")

# 4c: tags_keywords 恰好为 '',''
empty_tags2 = {'tags_tool': 'general', 'tags_domain': 'general', 'tags_action': 'general', 'tags_keywords': ''}
dup_id2 = engine.check_duplicate(conn_dup, empty_tags2, "empty_kw_test")
test("check_duplicate: 多次空 keywords 查询一致性", "P2", dup_id2 == dup_id, f"dup_id={dup_id2}")

conn_dup.close()

# ── 5. FTS5 转义 ──────────────────────────────────────────────
print("\n[5] FTS5 转义完整性")

# 5a: 含 * 的查询
q = engine._escape_fts5("test * query")
test("FTS5: 移除 *", "P1", '*' not in q, f"q='{q}'")

# 5b: 含 " 的查询
q = engine._escape_fts5('test " query')
# 词被包成 "..."（让保留字只当字面词），所以要求的是引号不残留**在词内**
test("FTS5: 词内引号被剥离", "P1",
     all('"' not in t.strip('"') for t in q.split(' OR ')), f"q='{q}'")

# 5c: 含 ( ) 的查询
q = engine._escape_fts5("test (query) here")
test("FTS5: 移除括号", "P1", '(' not in q and ')' not in q, f"q='{q}'")

# 5d: 含 - 的查询 (FTS5 NOT operator)
q = engine._escape_fts5("test - query")
# '-' between words could be FTS5 NOT, but our function doesn't strip '-'
# This is actually a potential FTS5 syntax issue
test("FTS5: - 字符被剥离且查询可执行", "P2",
     '-' not in q and isinstance(engine._recall_by_fts(engine.get_conn(tmp_db), "test - query", 3), list),
     f"q='{q}'")

# 5e: 纯特殊字符查询
q = engine._escape_fts5("*\"()-+:^")
test("FTS5: 纯特殊字符返回空", "P1", q == '', f"q='{q}'")

# 5f: 超短词 (单字符)
q = engine._escape_fts5("a b c test")
test("FTS5: 单字符词被过滤", "P2", 'a' not in q and 'b' not in q and 'test' in q, f"q='{q}'")

# 5g: FTS5 实际查询不会崩溃
fts_results = engine._recall_by_fts(engine.get_conn(tmp_db), 'test * " ( ) query', 3)
test("FTS5: 特殊字符查询不崩溃", "P0", isinstance(fts_results, list))

# ── 6. LIKE 通配符边界 ────────────────────────────────────────
print("\n[6] LIKE 通配符边界")

conn_like = engine.get_conn(tmp_db)

# 6a: 查询含 % (SQL LIKE 通配符)
try:
    results = engine._recall_by_like(conn_like, "100% 完成", 3)
    test("LIKE: % 通配符查询不崩溃", "P1", isinstance(results, list), f"results={len(results)}")
except Exception as e:
    test("LIKE: % 通配符查询不崩溃", "P1", False, f"error={e}")

# 6b: 查询含 _ (SQL LIKE 通配符)
try:
    results = engine._recall_by_like(conn_like, "test_value 查询", 3)
    test("LIKE: _ 通配符查询不崩溃", "P1", isinstance(results, list), f"results={len(results)}")
except Exception as e:
    test("LIKE: _ 通配符查询不崩溃", "P1", False, f"error={e}")

conn_like.close()

# ── 7. 超长输入 ────────────────────────────────────────────────
print("\n[7] 超长输入边界")

# 7a: 超长 goal
long_goal = "A" * 10000
r = engine.store(long_goal, "approach", "outcome")
test("store: 超长 goal 有界拒绝", "P1", r['stored'] is False, f"len(goal)={len(long_goal)}, reason={r.get('reason')}")

# 7b: 超长查询
long_query = "测试 " * 500
try:
    results = engine.recall(long_query, limit=3)
    test("recall: 超长查询不崩溃", "P1", isinstance(results, list), f"query_len={len(long_query)}")
except Exception as e:
    test("recall: 超长查询不崩溃", "P1", False, f"error={e}")

# 7c: sanitize_filename 超长
fn = mm_filter  # wrong module, use obsidian
import obsidian_export
fn = obsidian_export.sanitize_filename("测" * 200)
test("sanitize_filename: 超长截断", "P2", len(fn) <= 60, f"len={len(fn)}")

# ── 8. 特殊字符输入 ────────────────────────────────────────────
print("\n[8] 特殊字符输入")

# 8a: SQL 注入尝试 (should be safe with parameterized queries)
r = engine.store("'; DROP TABLE memories; --", "approach", "outcome")
test("store: SQL 注入尝试安全", "P0", r['stored'] is True, f"mem_id={r.get('memory_id')}")

# Verify table still exists
conn_safe = engine.get_conn(tmp_db)
count = conn_safe.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
test("store: SQL 注入后表完好", "P0", count > 0, f"count={count}")
conn_safe.close()

# 8b: Unicode 边界字符
r = engine.store("测试 emoji 🎉🔥💻 和特殊字符 ™®©", "方案包含 → ← ↑ ↓ 箭头", "结果含 日本語 한국어 العربية")
test("store: Unicode/Emoji 不崩溃", "P1", r['stored'] is True, f"mem_id={r.get('memory_id')}")

# 8c: 含换行符的输入
r = engine.store("goal\nwith\nnewlines", "approach\r\nwith\r\nCRLF", "outcome\twith\ttabs")
test("store: 换行符/制表符不崩溃", "P2", r['stored'] is True)

# ── 9. 并发安全 ────────────────────────────────────────────────
print("\n[9] 并发安全")

# 9a: 并发写入
results_lock = threading.Lock()
store_results = []

def concurrent_store(i):
    conn_t = engine.get_conn(tmp_db)
    try:
        topics = ["分布式锁", "连接池泄漏", "缓存穿透", "慢查询索引", "主从延迟"]
        r = engine.store(f"排查生产事故：{topics[i]}的定位与修复", f"针对{topics[i]}采用独立方案{i}", f"{topics[i]}已解决", db_conn=conn_t)
        with results_lock:
            store_results.append(r)
    finally:
        conn_t.close()

threads = [threading.Thread(target=concurrent_store, args=(i,)) for i in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()

all_stored = all(r['stored'] for r in store_results)
test("并发: 5线程同时 store", "P1", all_stored and len(store_results) == 5,
     f"stored={sum(1 for r in store_results if r['stored'])}/{len(store_results)}")

# 9b: 并发读取 (recall)
recall_results = []

def concurrent_recall(i):
    results = engine.recall(f"concurrent_test_{i}", limit=3)
    with results_lock:
        recall_results.append(len(results))

threads = [threading.Thread(target=concurrent_recall, args=(i,)) for i in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()

test("并发: 5线程同时 recall", "P1", len(recall_results) == 5,
     f"all returned: {recall_results}")

# ── 10. 事务一致性：store 中 dedup + reinforce ────────────────
print("\n[10] store 事务一致性")

conn_tx = engine.get_conn(tmp_db)

# 10a: store 一条新记忆
r1 = engine.store("tx_test_goal", "tx_approach", "tx_outcome", db_conn=conn_tx)
test("事务: 首次 store 成功", "P0", r1['stored'] is True, f"id={r1['memory_id']}")

# 10b: store 重复 → reinforce
r2 = engine.store("tx_test_goal", "tx_approach", "tx_outcome", db_conn=conn_tx)
test("事务: 重复 store 触发 reinforce", "P0", not r2['stored'] and '重复' in r2['reason'])

# 10c: 强化效果已 commit (因为 store 自带 commit)
row = conn_tx.execute("SELECT use_count FROM memories WHERE id=?", (r1['memory_id'],)).fetchone()
test("事务: store dedup 后强化已持久化", "P0", row['use_count'] >= 1, f"use_count={row['use_count']}")

conn_tx.close()

# ── 11. store 里程碑替换 + dedup 竞争 ─────────────────────────
print("\n[11] 里程碑替换 + dedup 交互")

conn_ms = engine.get_conn(tmp_db)

# 11a: 存一条里程碑
r_v1 = engine.store("里程碑测试项目完成发布", "方案甲", "完成", db_conn=conn_ms, project_key="ms-proj")
test("里程碑: 首版存储成功", "P1", r_v1['stored'], f"id={r_v1.get('memory_id')}")

# 11b: 存新里程碑，应替换 V1
if r_v1['stored']:
    r_v2 = engine.store("里程碑测试项目完成发布并全部通过", "方案乙", "完成全部通过", db_conn=conn_ms, project_key="ms-proj")
    test("里程碑: 后续版存储成功", "P1", r_v2['stored'], f"id={r_v2.get('memory_id')}")
    if r_v2['stored']:
        old = conn_ms.execute("SELECT status FROM memories WHERE id=?", (r_v1['memory_id'],)).fetchone()
        test("里程碑: 首版被后续版替换归档", "P1", old is not None and old['status'] == 'archived', f"row={dict(old) if old else None}")

conn_ms.close()

# ── 12. fallback_tokenize 边界 ────────────────────────────────
print("\n[12] fallback_tokenize 边界")

# 12a: 空字符串
result = tagger.fallback_tokenize("")
test("fallback_tokenize: 空字符串", "P2", result == [], f"result={result}")

# 12b: 纯标点
result = tagger.fallback_tokenize("...,,,!!!???")
test("fallback_tokenize: 纯标点", "P2", result == [], f"result={result}")

# 12c: 纯数字
result = tagger.fallback_tokenize("12345 67890")
# Numbers: pattern is [a-zA-Z][a-zA-Z0-9_]+ so pure numbers won't match
# Chinese pattern is [\u4e00-\u9fff]+ so no match either
test("fallback_tokenize: 纯数字", "P2", result == [], f"result={result}")

# 12d: 单字符英文 (should not match [a-zA-Z][a-zA-Z0-9_]+)
result = tagger.fallback_tokenize("I am a b c")
# 'I' won't match ([a-zA-Z][a-zA-Z0-9_]+ requires 2+ chars), 'am' will match
test("fallback_tokenize: 单字符英文不匹配", "P2", 'I' not in result and 'am' in result, f"result={result}")

# 12e: 中英文混合
result = tagger.fallback_tokenize("使用Python3处理数据")
test("fallback_tokenize: 中英混合", "P1", 'Python' in result or 'Python3' in result, f"result={result}")

# ── 13. extract_fixed_tags 首匹配 ──────────────────────────────
print("\n[13] extract_fixed_tags 首匹配行为")

# 13a: 多个 tool 匹配 — 取第一个 dict 中出现的
tags = tagger.extract_fixed_tags("使用 selenium 和 playwright 爬虫")
# selenium 出现在 TOOL_PATTERNS 顺序中先于 playwright
test("tool 首匹配: selenium 先于 playwright", "P2", tags['tool'] == 'selenium',
     f"tool={tags['tool']} (selenium appears first in TOOL_PATTERNS)")

# 13b: 关键词在注释/字符串中也匹配
tags = tagger.extract_fixed_tags("这个文件包含了 import pytest 的语句")
test("tool: 关键词在描述中也匹配", "P2", tags['tool'] == 'pytest', f"tool={tags['tool']}")

# ── 14. _escape_fts5 长查询保留 ────────────────────────────────
print("\n[14] _escape_fts5 长查询保留")

# 全部有效词参与查询，不截掉句尾
long_q = "word1 word2 word3 word4 word5 word6 word7 word8"
q = engine._escape_fts5(long_q)
word_count = len(q.split(" OR ")) if q else 0
test("FTS5: 完整有效查询词", "P1", word_count == 8, f"word_count={word_count}, q='{q[:80]}'")

# ── 15. obsidian_export 文件名安全 ─────────────────────────────
print("\n[15] obsidian_export 文件名安全")

# 15a: 含所有不安全字符
fn = obsidian_export.sanitize_filename('\\/:*?"<>|\n\r\t')
test("sanitize: 所有不安全字符被移除", "P1",
     all(c not in fn for c in '\\/:*?"<>|\n\r\t'),
     f"fn='{fn}'")

# 15b: 只有不安全字符
fn = obsidian_export.sanitize_filename('\\/:*?"<>|')
test("sanitize: 全不安全字符→untitled", "P1", fn == "untitled", f"fn='{fn}'")

# 15c: 正常中文
fn = obsidian_export.sanitize_filename("用Python爬虫抓取数据")
test("sanitize: 正常中文保留", "P1", fn == "用Python爬虫抓取数据", f"fn='{fn}'")

# ── 16. validate_fields 相似度边界 ──────────────────────────────
print("\n[16] validate_fields 相似度边界")

# 16a: success_key 完全等于 goal
passed, reason, cleaned = mm_filter.validate_fields(
    "完全相同的文本", "approach", "outcome",
    success_key="完全相同的文本")
test("validate: 相同 success_key 保留并提示", "P1",
     passed and cleaned['success_key'] == '完全相同的文本' and '提示' in reason, f"success_key='{cleaned['success_key']}'")

# 16b: pitfall 完全等于 success_key
passed, reason, cleaned = mm_filter.validate_fields(
    "goal", "approach", "outcome",
    success_key="相同的文本内容", pitfall="相同的文本内容")
test("validate: 相同可选字段保留", "P1",
     passed and cleaned['success_key'] == '相同的文本内容' and cleaned['pitfall'] == '相同的文本内容',
     f"success_key='{cleaned['success_key']}', pitfall='{cleaned['pitfall']}'")

# 16c: 完全不同的文本
passed, reason, cleaned = mm_filter.validate_fields(
    "用Flask搭建Web服务器", "配置路由和中间件", "服务器启动成功",
    success_key="使用了蓝图模式", pitfall="端口冲突导致启动失败")
test("validate: 不同文本保留所有字段", "P1",
     cleaned['success_key'] != '' and cleaned['pitfall'] != '',
     f"success_key='{cleaned['success_key'][:30]}'")

# ── 17. rule_filter 边界 ──────────────────────────────────────
print("\n[17] rule_filter 边界")

# 17a: turns < 3
ok, reason = mm_filter.rule_filter(
    turns=[{'content': 'hi'}, {'content': 'hello'}],
    has_code=True)
test("rule_filter: < 3 turns 拒绝", "P1", not ok, f"reason={reason}")

# 17b: 空 content
ok, reason = mm_filter.rule_filter(
    turns=[{'content': ''}, {'content': ''}, {'content': ''}],
    has_code=True)
test("rule_filter: 空 content 但 has_code 通过", "P1", ok)

# 17c: turns 中 content 为 None
ok, reason = mm_filter.rule_filter(
    turns=[{'content': None}, {'content': None}, {'content': None}],
    has_code=True)
test("rule_filter: None content 不崩溃", "P1", ok, f"reason={reason}")

# 17d: turns 缺少 content 键
ok, reason = mm_filter.rule_filter(
    turns=[{'role': 'user'}, {'role': 'assistant'}, {'role': 'user'}],
    has_code=True)
test("rule_filter: 缺少 content 键不崩溃", "P1", ok)

# 17e: 用户取消 (前3轮)
ok, reason = mm_filter.rule_filter(
    turns=[{'content': '帮我写代码'}, {'content': '好的'}, {'content': '取消'}],
    has_code=True)
test("rule_filter: 第3轮取消被拒绝", "P1", not ok, f"reason={reason}")

# ── 18. dream.apply_merge 空结果保护 ──────────────────────────
print("\n[18] dream.apply_merge 边界")

import dream

# 18a: 空 merged_memories
conn_dream = engine.get_conn(tmp_db)
try:
    _before = conn_dream.execute("SELECT count(*) FROM memories").fetchone()[0]
    dream.apply_merge(
        {'domain': 'test', 'tool': 'test', 'memories': [{'id': 99999}]},
        [],
        conn_dream)
    _after = conn_dream.execute("SELECT count(*) FROM memories").fetchone()[0]
    test("apply_merge: 空结果不崩溃不修改", "P1", _before == _after,
         f"rows {_before} -> {_after}")
except Exception as e:
    test("apply_merge: 空结果不崩溃不修改", "P1", False, f"error={e}")
conn_dream.close()

# ── 19. store 与 validate_fields 交互 ──────────────────────────
print("\n[19] store 内部 validate_fields 交互")

# 19a: store 时 success_key 与 goal 相同
conn_vf = engine.get_conn(tmp_db)
r = engine.store(
    "验证字段清理功能", "验证方案", "验证成功",
    success_key="验证字段清理功能", pitfall="",
    db_conn=conn_vf)
if r['stored']:
    row = conn_vf.execute("SELECT success_key FROM memories WHERE id=?",
                          (r['memory_id'],)).fetchone()
    test("store: 重复 success_key 保留", "P1",
         row and row['success_key'] == '验证字段清理功能',
         f"success_key='{row['success_key'] if row else 'N/A'}'")
else:
    test("store: 重复 success_key 保留", "P1", False, f"store failed: {r['reason']}")
conn_vf.close()

# ── 20. recall limit 类型安全 ─────────────────────────────────
print("\n[20] recall limit 边界（0/负数/None）")

# Simulate the plugin's recall with bad limit type
try:
    result = engine.recall("test", limit=0)
    test("recall: limit=0 不崩溃", "P1", isinstance(result, list), f"results={len(result)}")
except Exception as e:
    test("recall: limit=0 不崩溃", "P1", False, f"error={e}")

try:
    result = engine.recall("test", limit=-1)
    test("recall: limit=-1 不崩溃", "P2", isinstance(result, list), f"results={len(result)}")
except Exception as e:
    test("recall: limit=-1 不崩溃", "P2", False, f"error={e}")

# ── CLEANUP ────────────────────────────────────────────────────
import shutil
try:
    os.unlink(tmp_db)
except:
    pass
for ext in ['-wal', '-shm']:
    try:
        os.unlink(tmp_db + ext)
    except:
        pass

# ── SUMMARY ────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("审查结果汇总")
print("=" * 70)

if errors:
    print(f"\n🔴 发现 {len(errors)} 个问题:")
    for i, e in enumerate(errors, 1):
        print(f"  {i}. {e}")
    sys.exit(1)
else:
    print("\n🟢 所有深度测试通过，未发现新问题")
    sys.exit(0)
