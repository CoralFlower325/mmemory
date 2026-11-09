#!/usr/bin/env python3
"""Investigate edge cases"""
import sys, shutil, tempfile; sys.path.insert(0, '.')
import engine, pathlib

# 独立合成库探针
_tmp = pathlib.Path(tempfile.mkdtemp()) / 'edge_test.db'
_orig_db_path = engine.DB_PATH
engine.DB_PATH = _tmp
engine._schema_initialized.discard(str(_tmp))
conn = engine.get_conn()
engine.store('测试记忆系统数据库', '查询索引', '验证完成', db_conn=conn)
rows = conn.execute("SELECT id, goal, approach, outcome FROM memories WHERE status='active'").fetchall()
print(f'Total active: {len(rows)}')
for r in rows:
    has_db = '数据库' in (r['goal'] or '') or '数据库' in (r['approach'] or '') or '数据库' in (r['outcome'] or '')
    if has_db:
        print(f'  Has "数据库": id={r["id"]} goal={r["goal"][:60]}')

print()
# Check R6
mems = engine.recall('测试记忆系统', db_conn=conn)
print(f'R6 recheck: recall returned {len(mems)} results')
for m in mems:
    print(f'  id={m["id"]} goal={m["goal"][:60]} weight={m["weight"]:.3f} use_count={m["use_count"]}')

conn.close()

engine.DB_PATH = _orig_db_path
shutil.rmtree(_tmp.parent)
