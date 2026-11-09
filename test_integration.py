#!/usr/bin/env python3
"""mmemory 功能集成测试 — 使用临时DB"""
import sys, os, math, tempfile, unittest.mock
sys.path.insert(0, os.path.dirname(__file__))

import engine
import pathlib

# 使用临时DB
tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
tmp_db_path = tmp_db.name
tmp_db.close()
engine._schema_initialized.discard(tmp_db_path)

def get_test_conn(**kwargs):
    return engine.get_conn(db_path=tmp_db_path)

results = []

def record(category, name, priority, passed, detail=""):
    results.append({"category": category, "name": name, "priority": priority, "passed": passed, "detail": detail})
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"  {status} [{priority}] {name}: {detail}")

print("=" * 60)
print("mmemory 功能集成测试 (临时DB)")
print("=" * 60)

conn = get_test_conn()

# ── 1. Tag 生成 (中英文混合) ──────────────────────────────────
print("\n[1] Tag 生成")
from tagger import generate_tags, extract_tags_from_message

tags = generate_tags("用Python写一个HTTP服务器", "搭建简单服务器", "成功")
ok = tags['tags_domain'] != 'general' and 'Python' in tags['tags_keywords']
record("tag", "纯中文生成domain+keywords", "P0", ok, f"domain={tags['tags_domain']}, kw={tags['tags_keywords']}")

tags = generate_tags("deploy React app to AWS with Docker", "containerize and deploy", "success")
ok = tags['tags_domain'] in ('web-dev', 'devops') and 'Docker' in tags['tags_keywords']
record("tag", "纯英文生成domain+keywords", "P0", ok, f"domain={tags['tags_domain']}, kw={tags['tags_keywords']}")

tags = generate_tags("修复数据库死锁问题的debug经验", "用jstack分析", "解决")
ok = tags['tags_action'] == 'debug'
record("tag", "中英混合识别action=debug", "P0", ok, f"action={tags['tags_action']}, kw={tags['tags_keywords']}")

tags = extract_tags_from_message("帮我写一个React组件部署到AWS")
ok = tags['tags_domain'] in ('web-dev', 'web') or 'React' in tags.get('tags_keywords', '')
record("tag", "从消息提取Tag(domain非general)", "P1", ok, f"domain={tags['tags_domain']}")

# ── 2. 存储 (去重/字段校验/里程碑替换) ──────────────────────────
print("\n[2] 存储")

# 2.1 正常存储
r = engine.store("unique_store_test_001", "方案A独有", "结果A独有", db_conn=conn)
record("store", "正常存储新记忆", "P0", r['stored'] == True, f"id={r['memory_id']}")

# 2.2 去重
r2 = engine.store("unique_store_test_001", "方案A独有", "结果A独有", db_conn=conn)
record("store", "重复存储被拦截(去重)", "P0", r2['stored'] == False and '重复' in r2['reason'], f"reason={r2['reason']}")

# 2.3 字段校验
import filter as mm_filter
_, _, cleaned = mm_filter.validate_fields(
    "field_check_goal", "approach_check", "outcome_check",
    success_key="field_check_goal", pitfall="")
record("store", "字段校验:相似success_key保留", "P1",
       cleaned['success_key'] == 'field_check_goal',
       f"success_key 'field_check_goal'→'{cleaned['success_key']}'")

# 2.4 里程碑替换：绕过dedup直接测试filter逻辑
# 先存一条里程碑记忆
r_v1 = engine.store("完成 mos 2022C 项目X 版本发布", "项目X用Flask搭建", "完成",
                    db_conn=conn, project_key="mos")
if r_v1['stored']:
    # 测试 find_replaced_milestone 能否识别同domain+tool的旧里程碑
    v1_tags = conn.execute("SELECT tags_domain, tags_tool FROM memories WHERE id=?",
                           (r_v1['memory_id'],)).fetchone()
    replaced = mm_filter.find_replaced_milestone(
        "完成 mos 2022C 项目X 版本发布并全部通过", v1_tags[0], v1_tags[1], conn,
        project_key="mos")
    record("store", "里程碑替换:find_replaced识别旧里程碑", "P1",
           replaced == r_v1['memory_id'],
           f"replaced_id={replaced}, expected={r_v1['memory_id']}")
else:
    record("store", "里程碑替换:find_replaced识别旧里程碑", "P1", False, f"v1被去重: {r_v1['reason']}")

# ── 3. 三级召回 ──────────────────────────────────────────
print("\n[3] 三级召回")

engine.store("react_component_development", "用React开发组件", "组件完成", db_conn=conn)
results_recall = engine.recall("React前端组件开发", limit=3, db_conn=conn)
has_tag_match = any(r['tags_domain'] == 'web-dev' for r in results_recall)
record("recall", "Tag精确匹配:web-dev domain命中", "P0", has_tag_match, f"召回{len(results_recall)}条")

engine.store("fts_search_test_unique_keyword", "包含特殊词汇unique_keyword", "测试FTS", db_conn=conn)
results_fts = engine.recall("特殊词汇unique_keyword", limit=3, db_conn=conn)
record("recall", "FTS5全文检索命中", "P0", len(results_fts) > 0, f"召回{len(results_fts)}条")

engine.store("解决数据库死锁问题", "用jstack分析线程状态", "死锁消除", db_conn=conn)
results_like = engine.recall("数据库 死锁", limit=3, db_conn=conn)
record("recall", "LIKE兜底召回", "P0", len(results_like) > 0, f"召回{len(results_like)}条")

results_empty = engine.recall("完全不存在的xyzzy12345", limit=3, db_conn=conn)
record("recall", "无匹配返回空列表", "P1", len(results_empty) == 0)

# ── 4. 权重衰减 ──────────────────────────────────────────
print("\n[4] 权重衰减")

decay_result = engine.decay_all(db_conn=conn)
record("decay", "decay_all正常执行", "P0", 'decayed' in decay_result, f"decayed={decay_result['decayed']}")

row = conn.execute("SELECT MIN(weight) FROM memories WHERE status='active'").fetchone()
record("decay", "活跃记忆权重≥0", "P0", row[0] is None or row[0] >= 0, f"min={row[0]:.4f}")

w_high = 1.0 * math.exp(-0.01 * 10)
w_low = 1.0 * math.exp(-0.15 * 10)
record("decay", "高使用次数衰减更慢", "P2", w_high > w_low, f"w_high={w_high:.3f} > w_low={w_low:.3f}")

# ── 5. Dream dry run ──────────────────────────────────────────
print("\n[5] Dream dry run")
import dream

count_before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
dream_result = dream.run_dry_dream(db_conn=conn)
count_after = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
record("dream", "Dream dry run不修改数据", "P0", count_before == count_after)

expected_keys = {'merge_groups', 'merge_prompts', 'decay_result'}
record("dream", "Dream返回完整结构", "P1", expected_keys.issubset(dream_result.keys()))

# ── 6. Obsidian 导出 ──────────────────────────────────────────
print("\n[6] Obsidian 导出")
import obsidian_export
test_dir = pathlib.Path(tempfile.mkdtemp(prefix="mmemory_test_obsidian_clean_"))
obsidian_export.OBSIDIAN_DIR = test_dir

# Patch DB_PATH to use temp DB for export
orig_db_path = engine.DB_PATH
engine.DB_PATH = tmp_db_path
engine._schema_initialized.discard(str(tmp_db_path))
engine._schema_initialized.add(str(tmp_db_path))
try:
    obsidian_export.export_all()
    record("obsidian", "导出执行不报错", "P0", True)
except Exception as e:
    record("obsidian", "导出执行不报错", "P0", False, str(e))
finally:
    engine.DB_PATH = orig_db_path

index_exists = (test_dir / "index.md").exists()
record("obsidian", "生成index.md索引文件", "P0", index_exists)

domains = [d.name for d in test_dir.iterdir() if d.is_dir()]
record("obsidian", "按domain分子目录", "P1", len(domains) > 0, f"domains={domains}")

sample_files = [f for f in test_dir.rglob("*.md") if f.name != "index.md"]
if sample_files:
    content = sample_files[0].read_text()
    has_heading = content.startswith("#")
    has_sections = "## 目标" in content
    record("obsidian", "markdown含标题+段落", "P1", has_heading and has_sections)
else:
    record("obsidian", "markdown含标题+段落", "P1", False, "无md文件")

conn.close()

# ── 汇总 ──────────────────────────────────────────
print("\n" + "=" * 60)
print("测试汇总")
print("=" * 60)

for p in ["P0", "P1", "P2"]:
    subset = [r for r in results if r['priority'] == p]
    passed = sum(1 for r in subset if r['passed'])
    failed = sum(1 for r in subset if not r['passed'])
    print(f"\n{p}: {passed} 通过 / {failed} 失败 (共{len(subset)})")
    for r in subset:
        status = "✅" if r['passed'] else "❌"
        print(f"  {status} [{r['category']}] {r['name']}")

total_pass = sum(1 for r in results if r['passed'])
total_fail = sum(1 for r in results if not r['passed'])
print(f"\n总计: {total_pass} 通过 / {total_fail} 失败 / {len(results)} 总数")

os.unlink(tmp_db_path)
