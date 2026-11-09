#!/usr/bin/env python3
"""Test: Dream Mode"""
import sys, shutil, tempfile; sys.path.insert(0, '.')
import engine, dream, pathlib

print('=== DREAM MODE TESTS ===')

# 全部夹具在临时库构造
_tmp = pathlib.Path(tempfile.mkdtemp()) / 'dream_test.db'
_orig_db_path = engine.DB_PATH
engine.DB_PATH = _tmp
engine._schema_initialized.discard(str(_tmp))
conn = engine.get_conn()

for i in range(3):
    engine.store(f'合成维护任务{i}', '运行脚本', '完成操作', project_key='fixture', db_conn=conn)

# TC-DM1: find_merge_groups
groups = dream.find_merge_groups(conn, min_group_size=2)
print(f'DM1 merge groups (min=2): {len(groups)} groups')
for g in groups:
    print(f'   [{g["domain"]}/{g["tool"]}] count={g["count"]}')
print(f'   {"PASS" if isinstance(groups, list) and len(groups) >= 1 else "FAIL: 无合并组"}')

# TC-DM2: build_merge_prompt
if groups:
    prompt = dream.build_merge_prompt(groups[0])
    print(f'DM2 merge prompt: {len(prompt)} chars')
    print(f'   starts: {prompt[:80]}...')
    print(f'   contains "JSON": {"JSON" in prompt}')
    print(f'   {"PASS" if len(prompt) > 100 else "FAIL"}')
else:
    print('DM2 SKIP: no groups with min=2')
    # Try min=1
    groups = dream.find_merge_groups(conn, min_group_size=1)
    if groups:
        prompt = dream.build_merge_prompt(groups[0])
        print(f'DM2 merge prompt (min=1): {len(prompt)} chars')
        print(f'   {"PASS" if len(prompt) > 100 else "FAIL"}')

# TC-DM3: run_dry_dream (full pipeline)
result = dream.run_dry_dream(db_conn=conn)
print(f'DM3 dry dream: merge_groups={len(result["merge_groups"])}')
print(f'   decay_result={result["decay_result"]}')
print(f'   merge_prompts count={len(result["merge_prompts"])}')
print(f'   {"PASS" if "decay_result" in result else "FAIL"}')

conn.close()

engine.DB_PATH = _orig_db_path
shutil.rmtree(_tmp.parent)
