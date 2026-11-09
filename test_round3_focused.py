#!/usr/bin/env python3
"""
Round 3 Deep Review - Focused retest with better isolation
"""
import sys, os, tempfile, sqlite3, threading
sys.path.insert(0, os.path.dirname(__file__))

import engine

errors = []
def test(name, priority, passed, detail=""):
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"  {status} [{priority}] {name}: {detail}")
    if not passed:
        errors.append(f"[{priority}] {name}: {detail}")

# Fresh DB for each test group
print("=" * 70)
print("ROUND 3 DEEP REVIEW - FOCUSED RETEST")
print("=" * 70)

# ════════════════════════════════════════════════════════════════
# TEST GROUP A: P0 - _batch_reinforce commit regression
# ════════════════════════════════════════════════════════════════
print("\n[A] P0: _batch_reinforce commit regression in recall()")

db_a = tempfile.mktemp(suffix='.db')
orig_path = engine.DB_PATH
engine.DB_PATH = type(engine.DB_PATH)(db_a)
engine._schema_initialized.discard(db_a)

conn = engine.get_conn(db_a)
engine.store(
    "unique_recall_test_alpha_beta_gamma",
    "unique approach for recall testing",
    "unique outcome for recall",
    db_conn=conn)
conn.commit()

row = conn.execute("SELECT id, weight, use_count FROM memories WHERE goal LIKE '%unique_recall_test%'").fetchone()
mem_id = row['id']
w_before = row['weight']
u_before = row['use_count']
conn.close()

print(f"  Before recall: weight={w_before:.4f}, use_count={u_before}")

# Recall with standalone connection (db_conn=None → own_conn=True)
results = engine.recall("unique_recall_test alpha beta gamma", reinforce=True)
print(f"  Recall returned {len(results)} results")

# Re-open to check persistence
conn2 = engine.get_conn(db_a)
row2 = conn2.execute("SELECT weight, use_count FROM memories WHERE id=?", (mem_id,)).fetchone()
w_after = row2['weight']
u_after = row2['use_count']
conn2.close()

print(f"  After recall (new conn): weight={w_after:.4f}, use_count={u_after}")

test("recall --reinforce: weight persists across conns",
     "P0", w_after > w_before,
     f"expected > {w_before:.4f}, got {w_after:.4f}")
test("recall --reinforce: use_count persists across conns",
     "P0", u_after > u_before,
     f"expected > {u_before}, got {u_after}")

# Also test queue_prefetch pattern
print("\n  Testing queue_prefetch pattern (background thread)...")

# Reset weight for clean test
conn_reset = engine.get_conn(db_a)
conn_reset.execute("UPDATE memories SET weight=1.0, use_count=0, last_used=datetime('now','localtime') WHERE id=?", (mem_id,))
conn_reset.commit()
w_reset = 1.0
conn_reset.close()

def prefetch_pattern():
    local_conn = engine.get_conn(db_a)
    try:
        engine.recall("unique_recall_test alpha beta gamma", db_conn=local_conn, reinforce=True)
    finally:
        local_conn.close()

t = threading.Thread(target=prefetch_pattern)
t.start()
t.join()

conn3 = engine.get_conn(db_a)
row3 = conn3.execute("SELECT weight, use_count FROM memories WHERE id=?", (mem_id,)).fetchone()
w_pf = row3['weight']
u_pf = row3['use_count']
conn3.close()

print(f"  After prefetch pattern: weight={w_pf:.4f}, use_count={u_pf}")
test("prefetch --reinforce: weight persists",
     "P0", w_pf > w_reset,
     f"expected > {w_reset:.4f}, got {w_pf:.4f}")
test("prefetch --reinforce: use_count persists",
     "P0", u_pf > 0,
     f"expected > 0, got {u_pf}")

engine.DB_PATH = orig_path
engine._schema_initialized.discard(db_a)
try: os.unlink(db_a)
except: pass
for ext in ['-wal', '-shm']:
    try: os.unlink(db_a + ext)
    except: pass

# ════════════════════════════════════════════════════════════════
# TEST GROUP B: Concurrent writes
# ════════════════════════════════════════════════════════════════
print("\n[B] P1: Concurrent store safety")

db_b = tempfile.mktemp(suffix='.db')
engine.DB_PATH = type(engine.DB_PATH)(db_b)
engine._schema_initialized.discard(db_b)

# Pre-create the DB schema
pre_conn = engine.get_conn(db_b)
pre_conn.close()

store_results = []
store_lock = threading.Lock()

def concurrent_store(i):
    try:
        conn = engine.get_conn(db_b)
        r = engine.store(
            ["排查内存泄漏", "修复时区错乱", "优化打包体积", "补齐接口鉴权", "重写日志模块"][i] + "（独立任务）",
            f"针对{['内存泄漏','时区错乱','打包体积','接口鉴权','日志模块'][i]}的独立方案",
            f"{['内存泄漏','时区错乱','打包体积','接口鉴权','日志模块'][i]}已处理",
            db_conn=conn)
        conn.close()
        with store_lock:
            store_results.append((i, r))
    except Exception as e:
        with store_lock:
            store_results.append((i, {'stored': False, 'reason': str(e)}))

threads = [threading.Thread(target=concurrent_store, args=(i,)) for i in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()

stored_count = sum(1 for _, r in store_results if r.get('stored'))
test("concurrent store: all 5 succeed",
     "P1", stored_count == 5,
     f"stored={stored_count}/5, details={[(i, r.get('reason','ok')[:40]) for i, r in store_results]}")

engine.DB_PATH = orig_path
engine._schema_initialized.discard(db_b)
try: os.unlink(db_b)
except: pass
for ext in ['-wal', '-shm']:
    try: os.unlink(db_b + ext)
    except: pass

# ════════════════════════════════════════════════════════════════
# TEST GROUP C: SQL injection safety
# ════════════════════════════════════════════════════════════════
print("\n[C] P0: SQL injection safety")

db_c = tempfile.mktemp(suffix='.db')
engine.DB_PATH = type(engine.DB_PATH)(db_c)
engine._schema_initialized.discard(db_c)

conn_c = engine.get_conn(db_c)

# Must use parameterized queries - let's check the actual SQL in store()
r = engine.store(
    "sql_injection_test'; DROP TABLE memories; --",
    "approach injection test",
    "outcome injection test",
    db_conn=conn_c)

test("SQL injection: store succeeds or fails gracefully",
     "P0", r['stored'] is True or 'stored' in r,
     f"stored={r.get('stored')}, reason={r.get('reason', 'ok')[:50]}")

# Verify table still exists
count = conn_c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
test("SQL injection: table intact after injection attempt",
     "P0", count > 0, f"count={count}")

conn_c.close()
engine.DB_PATH = orig_path
engine._schema_initialized.discard(db_c)
try: os.unlink(db_c)
except: pass
for ext in ['-wal', '-shm']:
    try: os.unlink(db_c + ext)
    except: pass

# ════════════════════════════════════════════════════════════════
# TEST GROUP D: Milestone replacement under dedup
# ════════════════════════════════════════════════════════════════
print("\n[D] P1: Milestone replacement")

db_d = tempfile.mktemp(suffix='.db')
engine.DB_PATH = type(engine.DB_PATH)(db_d)
engine._schema_initialized.discard(db_d)

conn_d = engine.get_conn(db_d)
r1 = engine.store("项目Alpha完成发布上线", "Flask搭建方案", "完成", db_conn=conn_d, project_key="alpha-proj")
test("milestone 首版 store", "P1", r1['stored'], f"reason={r1.get('reason','ok')[:50]}")

if r1['stored']:
    r2 = engine.store("项目Alpha完成发布上线并交付", "FastAPI重构方案", "完成全部通过", db_conn=conn_d, project_key="alpha-proj")
    test("milestone 后续版 store 替换首版", "P1", r2['stored'], f"reason={r2.get('reason','ok')[:50]}")
    if r2['stored']:
        v1_status = conn_d.execute("SELECT status FROM memories WHERE id=?", (r1['memory_id'],)).fetchone()
        test("milestone 首版被替换", "P1",
             v1_status is not None and v1_status['status'] == 'archived',
             f"V1 row={dict(v1_status) if v1_status else None}")

conn_d.close()
engine.DB_PATH = orig_path
engine._schema_initialized.discard(db_d)
try: os.unlink(db_d)
except: pass
for ext in ['-wal', '-shm']:
    try: os.unlink(db_d + ext)
    except: pass

# ════════════════════════════════════════════════════════════════
# TEST GROUP E: _escape_fts5 - minus sign FTS5 syntax error
# ════════════════════════════════════════════════════════════════
print("\n[E] P2: _escape_fts5 minus sign handling")

db_e = tempfile.mktemp(suffix='.db')
engine.DB_PATH = type(engine.DB_PATH)(db_e)
engine._schema_initialized.discard(db_e)

conn_e = engine.get_conn(db_e)
# Insert a memory so FTS5 table exists
engine.store("fts_minus_test", "approach minus", "outcome minus", db_conn=conn_e)
conn_e.close()

# Test FTS5 query with minus sign
try:
    results = engine._recall_by_fts(engine.get_conn(db_e), "test - query minus", 3)
    test("FTS5: minus sign in query", "P2", True, f"returned {len(results)} results")
except Exception as e:
    test("FTS5: minus sign in query", "P2", False, f"FTS5 error: {e}")

engine.DB_PATH = orig_path
engine._schema_initialized.discard(db_e)
try: os.unlink(db_e)
except: pass
for ext in ['-wal', '-shm']:
    try: os.unlink(db_e + ext)
    except: pass

# ════════════════════════════════════════════════════════════════
# TEST GROUP F: store with own connection commit behavior
# ════════════════════════════════════════════════════════════════
print("\n[F] P0: store() commit consistency")

db_f = tempfile.mktemp(suffix='.db')
engine.DB_PATH = type(engine.DB_PATH)(db_f)
engine._schema_initialized.discard(db_f)

# Store with own connection (db_conn=None)
r = engine.store("standalone_store_test", "approach", "outcome")
test("store standalone: succeeds", "P0", r['stored'], f"mem_id={r.get('memory_id')}")

# Verify it persisted
conn_f = engine.get_conn(db_f)
row = conn_f.execute("SELECT id FROM memories WHERE goal='standalone_store_test'").fetchone()
test("store standalone: persisted after close", "P0", row is not None, f"row={row}")
conn_f.close()

engine.DB_PATH = orig_path
engine._schema_initialized.discard(db_f)
try: os.unlink(db_f)
except: pass
for ext in ['-wal', '-shm']:
    try: os.unlink(db_f + ext)
    except: pass

# ════════════════════════════════════════════════════════════════
# TEST GROUP G: _escape_fts5 edge - single-char after cleaning
# ════════════════════════════════════════════════════════════════
print("\n[G] P2: _escape_fts5 single-char filtering")

q1 = engine._escape_fts5("C Python Java")
toks1 = [t.strip('"') for t in q1.split(' OR ')]
test("FTS5: single-char 'C' filtered, multi-char kept",
     "P2", 'C' not in toks1 and 'Python' in toks1 and 'Java' in toks1,
     f"q='{q1}'")

# 过滤规则是「清理后长度 <= 1 丢弃」，所以单字符 R 被丢、两字符 Go 保留。
# 断言必须按 token 比较：子串判断会让 'R' in '...React' 恒为真。
q2 = engine._escape_fts5("R Go Vue React")
toks2 = [t.strip('"') for t in q2.split(' OR ')]
test("FTS5: 'R' filtered, 'Go'/'Vue'/'React' kept",
     "P2", 'R' not in toks2 and all(t in toks2 for t in ('Go', 'Vue', 'React')),
     f"q='{q2}', toks={toks2}")

# ════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("REVIEW RESULTS")
print("=" * 70)

if errors:
    print(f"\n🔴 Found {len(errors)} issues:")
    for i, e in enumerate(errors, 1):
        print(f"  {i}. {e}")
else:
    print("\n🟢 All deep review tests passed")

sys.exit(1 if errors else 0)
