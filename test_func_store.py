#!/usr/bin/env python3
"""Test: Store (dedup / field validation / milestone replace)"""
import sys, os, tempfile; sys.path.insert(0, '.')
import engine

# Use temp DB
tmp = tempfile.mktemp(suffix='.db')
os.environ['MMEMORY_DB'] = tmp  # won't work, engine uses DB_PATH
# Patch engine.DB_PATH
import pathlib
_orig_db_path = engine.DB_PATH
engine.DB_PATH = pathlib.Path(tmp)
engine._schema_initialized.discard(tmp)

print('=== STORE TESTS ===')

# TC-S1: Normal store
conn1 = engine.get_conn()
r = engine.store('用 requests 抓取网页数据', '使用 requests.get 配合 headers 反爬', '成功抓取1000条数据',
                 success_key='设置正确的 User-Agent', pitfall='需要处理302重定向',
                 db_conn=conn1)
print(f'S1 normal store: {r}')
print(f'   {"PASS" if r["stored"] else "FAIL"}')

# TC-S2: Duplicate detection
r2 = engine.store('用 requests 爬取网页数据', 'requests.get 加 headers', '抓取成功',
                  db_conn=conn1)
print(f'S2 duplicate: stored={r2["stored"]}, reason={r2["reason"]}')
assert r2['stored'], '改写目标不应被相似度去重'
print('   PASS')

# TC-S3: 相似字段只提示，不静默清空
r3 = engine.store('完成论文排版和PDF导出', '用 LaTeX xelatex 编译', 'PDF生成成功',
                  success_key='完成论文排版和PDF导出', pitfall='字体嵌入问题',
                  db_conn=conn1)
print(f'S3 field validation: stored={r3["stored"]}')
row = conn1.execute('SELECT success_key FROM memories WHERE id=?', (r3['memory_id'],)).fetchone()
sk = row['success_key'] if row else 'N/A'
print(f'   success_key after validation: "{sk}" (must be preserved)')
assert sk == '完成论文排版和PDF导出'
assert r3['warnings']
print('   PASS')

# TC-S4: Milestone replacement (both versions must read as milestones, since
# find_replaced_milestone only supersedes an old *milestone* goal)
r4a = engine.store('完成 docker CI/CD 自动化部署流水线', 'docker-compose + GitHub Actions', '初版已上线',
                   db_conn=conn1, project_key='deploy-proj')
print(f'S4a pre-milestone: {r4a}')
r4b = engine.store('完成 docker CI/CD 自动化部署流水线，并全部通过验收', '完整流水线已通过验收', '全部通过验收可提交',
                   db_conn=conn1, project_key='deploy-proj')
print(f'S4b milestone: {r4b}')
if r4a.get('stored') and r4b.get('stored'):
    old = conn1.execute('SELECT status FROM memories WHERE id=?', (r4a['memory_id'],)).fetchone()
    print(f'   old row: {"still present, status=" + old["status"] if old else "deleted"}')
    assert old is not None and old['status'] == 'archived'
    print('   PASS')

# TC-S5: Empty optional fields
r5 = engine.store('使用 pydantic BaseModel 数据校验', 'BaseModel 定义 schema', '校验通过', db_conn=conn1)
print(f'S5 empty optionals: stored={r5["stored"]}')
print(f'   {"PASS" if r5["stored"] else "FAIL"}')

# TC-S6: Verify all tags generated correctly
row = conn1.execute('SELECT * FROM memories WHERE id=?', (r5['memory_id'],)).fetchone()
print(f'S6 tag fields: tool={row["tags_tool"]} domain={row["tags_domain"]} action={row["tags_action"]}')
print(f'   keywords={row["tags_keywords"]}')
assert row['tags_tool'] == 'pydantic'
print('   PASS')

conn1.close()
# 恢复 DB_PATH：同进程按序执行时，遗留的临时路径会让后续测试落到已删除的库上
engine.DB_PATH = _orig_db_path
for _ext in ['', '-wal', '-shm']:
    if os.path.exists(tmp + _ext):
        os.unlink(tmp + _ext)
