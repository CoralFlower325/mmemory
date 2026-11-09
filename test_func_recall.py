#!/usr/bin/env python3
"""Test: Three-level Recall (Tag / FTS5 / LIKE)"""
import sys, os, shutil, tempfile; sys.path.insert(0, '.')
import engine, pathlib

# Run against a throwaway copy so the reinforcement test can never mutate the
# real memory store.
tmp = tempfile.mktemp(suffix='.db')
_orig_db_path = engine.DB_PATH

engine.DB_PATH = pathlib.Path(tmp)
engine._schema_initialized.discard(tmp)

for goal, project in [('部署 docker 服务器运维', ''), ('MOS 数学建模竞赛论文求解', 'mos'),
                      ('mmemory 记忆系统权重衰减算法优化', 'mmemory'), ('数据库索引查询', '')]:
    engine.store(goal, '使用合成数据验证', '验证完成', project_key=project)

print('=== RECALL TESTS (synthetic DB) ===')

# TC-R1: Tag recall - known tool+domain (devops/general cluster)
mems = engine.recall('部署 docker 服务器运维')
print(f'R1 tag recall (devops): {len(mems)} results')
if mems:
    print(f'   top: [{mems[0]["tags_domain"]}/{mems[0]["tags_tool"]}] {mems[0]["goal"][:50]}')
print(f'   {"PASS" if len(mems) > 0 else "FAIL"}')

# TC-R2: Tag recall - math-modeling domain
mems = engine.recall('数学建模竞赛论文求解', project_key='mos')
print(f'R2 tag recall (math-modeling): {len(mems)} results')
assert mems, '合成夹具中的目标必须召回'
print('   PASS')
if mems:
    print(f'   top: [{mems[0]["tags_domain"]}/{mems[0]["tags_tool"]}] {mems[0]["goal"][:50]}')

# TC-R3: FTS5 recall - no tag match, use fulltext
mems = engine.recall('记忆系统权重衰减算法优化', project_key='mmemory')
print(f'R3 fts recall: {len(mems)} results')
assert mems, '合成夹具中的目标必须召回'
print('   PASS')
if mems:
    print(f'   top: {mems[0]["goal"][:60]}')

# TC-R4: LIKE recall fallback - very generic
mems = engine.recall('数据库')
print(f'R4 like recall: {len(mems)} results')
assert mems, '合成夹具中的目标必须召回'
print('   PASS')
if mems:
    print(f'   top: {mems[0]["goal"][:60]}')

# TC-R5: No match at all
mems = engine.recall('量子计算宇宙飞船外星人')
print(f'R5 no match: {len(mems)} results')
assert not mems
print('   PASS')

# TC-R6: Recall must NOT change weight by default (query-only inspection)
conn = engine.get_conn()
probe = '部署 docker 服务器运维'
hit = engine.recall(probe, db_conn=conn)
target_id = hit[0]['id']
target = conn.execute('SELECT weight,use_count FROM memories WHERE id=?',
                      (target_id,)).fetchone()
before = (target['weight'], target['use_count'])
engine.recall(probe, db_conn=conn)
after = conn.execute('SELECT weight,use_count FROM memories WHERE id=?',
                     (target_id,)).fetchone()
print(f'R6 no-reinforce: weight {before[0]:.3f}→{after["weight"]:.3f}, '
      f'count {before[1]}→{after["use_count"]}')
print(f'   {"PASS" if (after["weight"], after["use_count"]) == before else "FAIL: recall mutated weight"}')

# TC-R7: Explicit reinforcement is opt-in
engine.recall(probe, db_conn=conn, reinforce=True)
bumped = conn.execute('SELECT weight,use_count FROM memories WHERE id=?',
                      (target_id,)).fetchone()
print(f'R7 reinforce=True: count {before[1]}→{bumped["use_count"]}')
print(f'   {"PASS" if bumped["use_count"] > before[1] else "FAIL: reinforce had no effect"}')
conn.close()

# TC-R8: Format recall block
mems = engine.recall('MOS 数学建模')
if mems:
    block = engine.format_recall_block(mems[:2])
    print(f'R8 format block: {len(block)} chars, starts with: {block[:60]}...')
    print(f'   {"PASS" if "[记忆系统]" in block else "FAIL"}')

# 恢复 DB_PATH：单进程跑多个测试文件时，残留的临时路径会让后续测试
# 落到一个已被删除的库上，结果随执行顺序变化。
engine.DB_PATH = _orig_db_path
for ext in ['', '-wal', '-shm']:
    p = tmp + ext
    if os.path.exists(p):
        os.remove(p)
