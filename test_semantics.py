#!/usr/bin/env python3
"""Test: 隔离与替换语义（项目命名空间 / 去重边界 / 里程碑数据安全）

这些行为此前完全没有回归护栏，而它们的失效方式都是「静默删掉或丢弃一条真实
记忆」，所以单独成文件。全部在临时库上跑，不触碰生产库。
"""
import sys, os, tempfile, shutil, pathlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine

errors = []


def test(name, passed, detail=""):
    print(f'  {"PASS" if passed else "FAIL"} {name}: {detail}')
    if not passed:
        errors.append(f"{name}: {detail}")


def fresh(tag):
    p = pathlib.Path(tempfile.mkdtemp()) / f'{tag}.db'
    engine.DB_PATH = p
    engine._schema_initialized.discard(str(p))
    return engine.get_conn()


def rows(conn):
    return sorted((r[0], r[1], r[2]) for r in
                  conn.execute("SELECT id, goal, project_key FROM memories"))


print('=== 隔离与替换语义 ===')

# ── 1. 项目命名空间是硬隔离 ─────────────────────────────────────
print('\n[1] project_key 硬隔离')
c = fresh('iso')
engine.store("完成 example-service 路由修复", "方案", "结果", db_conn=c,
             project_key='example-service')
engine.store("完成 example-piano 键位修复", "方案", "结果", db_conn=c,
             project_key='example-piano')
only_service = engine.list_all(status='active', db_conn=c, project_key='example-service')
test("list 只返回指定项目",
     len(only_service) == 1 and only_service[0]['project_key'] == 'example-service',
     f"got={[(m['project_key'], m['goal'][:12]) for m in only_service]}")
test("list 省略 key 返回全部", len(engine.list_all(status='active', db_conn=c)) == 2,
     f"total={len(engine.list_all(status='active', db_conn=c))}")
test("recall 不跨项目泄漏",
     all(m['project_key'] == 'example-service'
         for m in engine.recall('example-service 路由', db_conn=c,
                                project_key='example-service')),
     "example-service 查询只回同项目")

# 跨项目不互相去重
r = engine.store("完成 example-service 路由修复", "方案", "结果", db_conn=c,
                 project_key='example-piano')
test("跨项目同文本不互相去重", r['stored'] is True, f"reason={r['reason']}")

# ── 2. stats 带 project_key 不再崩溃 ────────────────────────────
print('\n[2] stats --project-key')
try:
    s = engine.get_stats(db_conn=c, project_key='example-service')
    test("stats(project_key) 可用", s['active'] == 1 and s['total'] == 1,
         f"{s}")
except Exception as e:
    test("stats(project_key) 可用", False, f"raised {type(e).__name__}: {e}")

# ── 3. 去重边界：同一件事 vs 不同的事 ───────────────────────────
print('\n[3] 去重边界')
c = fresh('dedup')
engine.store("抓取网页数据平台", "用 selenium 驱动 chrome 抓取动态渲染页面", "成功", db_conn=c)
r = engine.store("抓取网页数据平台", "用 playwright 驱动 chromium 抓取动态渲染页面", "成功", db_conn=c)
test("tool 漂移仍去重", r['stored'] is False and
     c.execute("SELECT count(*) FROM memories").fetchone()[0] == 1,
     f"reason={r['reason']}")

c = fresh('dedup2')
engine.store("完成 2022C 玻璃成分聚类求解", "用 KMeans 方案甲", "结果甲", db_conn=c)
r = engine.store("完成 2022C 论文附录排版导出", "用 LaTeX 方案乙", "结果乙", db_conn=c)
test("不同任务不被误吞", r['stored'] is True and
     c.execute("SELECT count(*) FROM memories").fetchone()[0] == 2,
     f"reason={r['reason']}, rows={c.execute('SELECT count(*) FROM memories').fetchone()[0]}")

# ── 4. 已知项目内：里程碑替换 ───────────────────────────────────
print('\n[4] 已知项目内里程碑替换')
c = fresh('ms')
engine.store("完成示例建模题论文正文排版", "方案甲", "完成", db_conn=c,
             project_key='example-service')
engine.store("完成示例建模题论文正文排版并全部通过验收", "方案乙", "全部通过验收",
             db_conn=c, project_key='example-service')
rs = c.execute("SELECT id,goal,project_key FROM memories WHERE status='active'").fetchall()
test("旧版被替换为新版", len(rs) == 1 and '全部通过验收' in rs[0][1], f"rows={rs}")
test("旧版原文可归档恢复", c.execute("SELECT count(*) FROM memories WHERE status='archived'").fetchone()[0] == 1)

# ── 5. 共享桶：绝不自动删除 ─────────────────────────────────────
print('\n[5] 共享桶保护（不同项目不得互删）')
c = fresh('shared')
engine.store("完成 iOS App 上线发布", "Xcode 归档", "已上线", db_conn=c)
engine.store("完成 安卓 App 上线发布", "Gradle 打包", "已上线", db_conn=c)
rs = rows(c)
test("不同项目两条都保留", len(rs) == 2, f"rows={[(x[1]) for x in rs]}")

c = fresh('shared2')
engine.store("完成 Delta 项目部署验收", "统一方案", "统一结果", db_conn=c)
# 用**白名单内**的后缀（`并交付`）：这样在命名空间内本会触发替换，
# 共享桶必须拦下。此前用的 `并交付上线` 不在白名单，is_version_upgrade
# 本就是 False，测不到共享桶守卫（实测去掉守卫仍全绿）。
engine.store("完成 Delta 项目部署验收并交付", "统一方案", "统一结果", db_conn=c)
test("共享桶内版本升级也不删除", c.execute("SELECT count(*) FROM memories").fetchone()[0] == 2,
     f"rows={c.execute('SELECT count(*) FROM memories').fetchone()[0]}")

c = fresh('shared3')
engine.store("完成论文排版导出PDF", "方案A", "结果A", db_conn=c)
r = engine.store("完成论文排版导出PDF", "方案A", "结果A", db_conn=c)
test("共享桶内逐字重复仍去重", r['stored'] is False and
     c.execute("SELECT count(*) FROM memories").fetchone()[0] == 1, f"reason={r['reason']}")

# ── 6. 数据丢失回归：删 A 却命中 B 的行 ─────────────────────────
print('\n[6] 里程碑与去重交互不丢数据')
c = fresh('loss')
for g in ("完成 Alpha 项目部署验收", "Alpha 项目部署与验收要点"):
    c.execute("INSERT INTO memories (project_key,goal,approach,outcome,tags_tool,"
              "tags_domain,tags_action,tags_keywords) VALUES "
              "('mos',?,'统一方案','统一结果','general','devops','build',"
              "'部署,验收,Alpha,项目,方案')", (g,))
c.commit()
before = {r[1] for r in rows(c)}
r = engine.store("完成 Alpha 项目部署验收", "统一方案", "统一结果",
                 db_conn=c, project_key='mos')
after = {r[1] for r in rows(c)}
# 关键不变量：store 返回未存储时，不能有行凭空消失
test("未存储时不得删除既有行", r['stored'] is True or after == before,
     f"stored={r['stored']}, 前后差={before - after}")

# ── 7. 衰减不因脏数据整体崩溃 ───────────────────────────────────
print('\n[7] 脏 last_used 不炸衰减')
c = fresh('dirty')
c.execute("INSERT INTO memories (goal,approach,outcome,tags_tool,tags_domain,"
          "tags_action,tags_keywords,last_used) VALUES "
          "('脏数据行','a','b','general','general','general','','not-a-date')")
c.execute("INSERT INTO memories (goal,approach,outcome,tags_tool,tags_domain,"
          "tags_action,tags_keywords,last_used) VALUES "
          "('好数据行','a','b','general','general','general','',"
          "datetime('now','localtime'))")
c.commit()
try:
    res = engine.decay_all(db_conn=c)
    test("decay_all 跳过脏数据", isinstance(res, dict), f"{res}")
except Exception as e:
    test("decay_all 跳过脏数据", False, f"raised {type(e).__name__}: {e}")

# ── 8. limit 边界 ───────────────────────────────────────────────
print('\n[8] limit 边界')
c = fresh('limit')
for i in range(3):
    engine.store(f"limit 边界测试条目第{i}号完全不同主题", "方案", "结果", db_conn=c)
test("limit=-1 不等于全库", len(engine.recall("边界测试条目", db_conn=c, limit=-1)) >= 1,
     f"got={len(engine.recall('边界测试条目', db_conn=c, limit=-1))}")
test("limit=0 不返回全库", len(engine.recall("边界测试条目", db_conn=c, limit=0)) <= 1,
     f"got={len(engine.recall('边界测试条目', db_conn=c, limit=0))}")
test("list_all(-1) 不是全库",
     len(engine.list_all(limit=-1, db_conn=c)) < 3,
     f"total=3, got={len(engine.list_all(limit=-1, db_conn=c))}")
test("list_all(None) 不抛异常",
     isinstance(engine.list_all(limit=None, db_conn=c), list),
     "None → 默认值")
test("recall(None) 不抛异常",
     isinstance(engine.recall("边界测试条目", db_conn=c, limit=None), list),
     "None → 默认值")

# ── 9. 数字序列是硬判据（jieba 会丢弃数字）─────────────────────
print('\n[9] 数字边界：阶段1 vs 阶段2 是不同的事')
import filter as _f
test("阶段1/阶段2 相似度被识别为高",
     _f._similarity("完成 MOS 阶段1 部署", "完成 MOS 阶段2 部署") == 1.0,
     "分词丢弃数字，相似度算成 1.0（所以必须另加数字约束）")
c = fresh('num')
engine.store("完成 MOS 阶段1 部署", "阶段1方案", "阶段1完成", db_conn=c, project_key='p')
r = engine.store("完成 MOS 阶段2 部署", "阶段2方案", "阶段2完成", db_conn=c, project_key='p')
goals = sorted(g for (g,) in c.execute("SELECT goal FROM memories"))
test("阶段1/阶段2 都保留（不静默丢失）",
     r['stored'] is True and len(goals) == 2,
     f"stored={r['stored']}, goals={goals}")
r = engine.store("完成 MOS 阶段2 部署", "阶段2方案", "阶段2完成", db_conn=c, project_key='p')
# 里程碑目标重复存储会走「替换」而非「去重」，两者都保证不新增行。
test("相同数字与文本不新增重复行",
     c.execute("SELECT count(*) FROM memories").fetchone()[0] == 2,
     f"rows={c.execute('SELECT count(*) FROM memories').fetchone()[0]}, reason={r['reason']}")

c = fresh('num2')
engine.store("完成第1批数据处理", "方案", "完成", db_conn=c, project_key='p')
engine.store("完成第2批数据处理", "方案", "完成", db_conn=c, project_key='p')
test("第1批/第2批 都保留",
     c.execute("SELECT count(*) FROM memories").fetchone()[0] == 2,
     f"rows={c.execute('SELECT count(*) FROM memories').fetchone()[0]}")

# ── 10. apply_merge 不得因空串字段删数据 ────────────────────────
print('\n[10] apply_merge 空串字段保护')
import dream as _dream
c = fresh('merge')
for g in ("分析 A 项目数据集特征分布", "拆解 A 项目数据集字段含义", "梳理 A 项目数据集来源口径"):
    engine.store(g, "方案", "结果", db_conn=c, project_key='p')
before = sorted(g for (g,) in c.execute("SELECT goal FROM memories"))
grp = _dream.find_merge_groups(c, min_group_size=2, project_key='p')
_dream.apply_merge(grp[0], [{"goal": "", "approach": "", "outcome": ""}], c)
after = sorted(g for (g,) in c.execute("SELECT goal FROM memories"))
test("空串字段不删旧数据", before == after, f"before={len(before)}, after={len(after)}")
_dream.apply_merge(grp[0], [{"goal": "合并目标", "approach": "合并方案", "outcome": "合并结果"}], c)
merged = [g for (g,) in c.execute("SELECT goal FROM memories WHERE status='active'")]
test("合法合并仍可用", merged == ["合并目标"], f"rows={merged}")
test("合并来源全部保留归档", c.execute("SELECT count(*) FROM memories WHERE status='archived'").fetchone()[0] == 3)

# ── 11. 脏 last_used / NULL 值不炸衰减 ──────────────────────────
print('\n[11] 脏数据不炸衰减')
c = fresh('dirty2')
c.execute("INSERT INTO memories (goal,approach,outcome,last_used) "
          "VALUES ('时区型','a','b','2020-01-01T00:00:00+08:00')")
c.execute("INSERT INTO memories (goal,approach,outcome,last_used) "
          "VALUES ('Z结尾','a','b','2020-01-01T00:00:00Z')")
c.execute("INSERT INTO memories (goal,approach,outcome,weight,last_used) "
          "VALUES ('weightNULL','a','b',NULL,datetime('now'))")
c.execute("INSERT INTO memories (goal,approach,outcome,use_count,last_used) "
          "VALUES ('ucNULL','a','b',NULL,datetime('now'))")
c.execute("INSERT INTO memories (goal,approach,outcome,last_used) "
          "VALUES ('lastUsedNULL','a','b',NULL)")
c.commit()
try:
    res = engine.decay_all(db_conn=c)
    test("带时区/NULL 脏数据不崩溃", isinstance(res, dict), f"{res}")
    res2 = engine.decay_all(db_conn=c, dry_run=True)
    test("dry_run 同样不崩溃", isinstance(res2, dict), f"{res2}")
except Exception as e:
    test("带时区/NULL 脏数据不崩溃", False, f"raised {type(e).__name__}: {e}")

# ── 12. domain/tool 漂移不再漏去重 ──────────────────────────────
print('\n[12] 标签漂移不漏去重')
c = fresh('drift')
g = "优化这个处理流程的效率"
engine.store(g, "用 pandas 做数据处理和清洗", "完成", db_conn=c, project_key='p')
r = engine.store(g, "用 React 写前端页面组件", "完成", db_conn=c, project_key='p')
test("同 goal 不同 domain 仍去重",
     r['stored'] is False and c.execute("SELECT count(*) FROM memories").fetchone()[0] == 1,
     f"reason={r['reason']}")

# ── 13. 中文自然语言召回（FTS/LIKE 分词）────────────────────────
print('\n[13] 中文查询可召回')
c = fresh('zh')
engine.store("实现记忆系统的权重衰减算法优化", "用指数曲线", "完成", db_conn=c, project_key='p')
hits = engine.recall("记忆系统权重衰减算法优化", db_conn=c, project_key='p')
test("中文自然语言查询能命中", len(hits) >= 1, f"got={len(hits)}")
test("query_terms 对中文切词", len(engine.query_terms("记忆系统权重衰减")) >= 2,
     f"{engine.query_terms('记忆系统权重衰减')}")

# ── 14. 并发写入不产生重复行 ────────────────────────────────────
print('\n[14] 并发写入去重')
import threading
c = fresh('concurrent')
c.close()
errs = []
lock = threading.Lock()


def _concurrent_store():
    try:
        cc = engine.get_conn()
        engine.store("并发同一条目：修复数据库连接池泄漏问题",
                     "统一方案内容", "统一结果内容", db_conn=cc, project_key='p')
        cc.close()
    except Exception as e:                                  # noqa: BLE001
        with lock:
            errs.append(f"{type(e).__name__}: {e}")


ts = [threading.Thread(target=_concurrent_store) for _ in range(20)]
for t in ts:
    t.start()
for t in ts:
    t.join()
c = engine.get_conn()
rows = c.execute("SELECT count(*) FROM memories").fetchone()[0]
test("20 线程存同一条目只落 1 行", rows == 1, f"rows={rows}")
test("并发写入无异常", not errs, f"errs={errs[:2]}")

# 不同条目并发必须全部写入（锁不能把正常并发写吞掉）
c = fresh('concurrent2')
c.close()
errs2 = []


def _distinct_store(i):
    try:
        cc = engine.get_conn()
        engine.store(f"并发不同任务{i}号：独立主题内容{i}", "方案", "结果",
                     db_conn=cc, project_key='p')
        cc.close()
    except Exception as e:                                  # noqa: BLE001
        with lock:
            errs2.append(f"{type(e).__name__}: {e}")


ts = [threading.Thread(target=_distinct_store, args=(i,)) for i in range(10)]
for t in ts:
    t.start()
for t in ts:
    t.join()
c = engine.get_conn()
rows2 = c.execute("SELECT count(*) FROM memories").fetchone()[0]
test("10 线程存不同条目全部落库", rows2 == 10, f"rows={rows2}, errs={errs2[:2]}")


# ── 15. 不同任务绝不被静默删除/丢弃（第四轮回归）─────────────────
print('\n[15] 相似但不同的任务必须都保留')
_differing = [
    ("完成 ExampleWiki 政治错题本录入", "完成 ExampleWiki 英语错题本录入"),
    ("通过示例建模题第一问", "通过示例建模题第二问"),
    ("完成示例歌单核验", "完成示例歌单整改"),
    ("完成示例计时器修复", "完成示例计时器全部通过"),
    ("完成 ExampleNovel 第一章审查", "完成 ExampleNovel 第二章审查"),
]
for a, b in _differing:
    c = fresh('distinct')
    engine.store(a, "方案甲", "结果甲", db_conn=c, project_key='p')
    engine.store(b, "方案乙", "结果乙", db_conn=c, project_key='p')
    goals = [g for (g,) in c.execute("SELECT goal FROM memories")]
    test(f"两条都保留: {a[:18]}",
         a in goals and b in goals, f"rows={[g[:16] for g in goals]}")

# 逐字 / 同 goal 重复仍去重
for a, b in [("完成示例论文附录排版导出", "完成示例论文附录排版导出"),
             ("修复数据库连接池泄漏问题", "修复数据库连接池泄漏问题")]:
    c = fresh('verbatim')
    engine.store(a, "x1", "y1", db_conn=c, project_key='p')
    r = engine.store(b, "x2", "y2", db_conn=c, project_key='p')
    n = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    test(f"逐字重复仍去重: {a[:16]}", n == 1 and r['stored'] is False,
         f"rows={n}, {r['reason'][:20]}")

# ── 16. 事务所有权：store 不得提交/回滚调用方的事务 ─────────────
print('\n[16] 事务所有权')
c = fresh('txown')
c.execute("INSERT INTO memories (goal,approach,outcome) VALUES ('调用方未提交行','a','b')")
engine.store("store 写入的行", "方案", "结果", db_conn=c)
c.rollback()
rows = [g for (g,) in c.execute("SELECT goal FROM memories")]
test("store 不提交调用方事务", rows == [], f"rollback 后残留={rows}")
c.close()

# 异常不得把连接留在持有写锁的事务里
c = fresh('txlock')
_orig = _f.find_replaced_milestone


def _boom(*a, **k):
    raise RuntimeError("boom")


_f.find_replaced_milestone = _boom
try:
    engine.store("异常条目", "方案", "结果", db_conn=c)
except RuntimeError:
    pass
_f.find_replaced_milestone = _orig
test("异常不泄漏事务/写锁", not c.in_transaction, f"in_transaction={c.in_transaction}")
c.close()

# ── 17. FTS 保留字不再让该层失效 ────────────────────────────────
print('\n[17] FTS 保留字')
c = fresh('ftsword')
engine.store("部署发布流水线", "a", "b", db_conn=c, project_key='p')
for q in ("部署 OR 发布", "更新 AND 部署", "完成 100% 部署"):
    fts = engine._escape_fts5(q)
    test(f"保留字不成非法表达式: {q}",
         'OR OR' not in fts and 'AND OR' not in fts, f"fts={fts!r}")


# ── 18. 去重判据：只有归一化后逐字相同才算重复（第五轮回归）──────
print('\n[18] 不同任务绝不因相似而被判重')
_num_ordered = [
    ("完成第1章第2节修订", "完成第2章第1节修订"),
    ("完成第1批第2阶段处理", "完成第2批第1阶段处理"),
    ("完成 v1.2 迁移到 v2.0", "完成 v2.0 迁移到 v1.2"),
    ("完成第3章第7节校对", "完成第7章第3节校对"),
    ("为A项目添加单元测试", "为B项目添加单元测试"),
    ("部署测试环境到服务器A", "部署测试环境到服务器B"),
]
for a, b in _num_ordered:
    c = fresh('numord')
    engine.store(a, "甲方案", "甲结果", db_conn=c, project_key='p')
    engine.store(b, "乙方案", "乙结果", db_conn=c, project_key='p')
    goals = [g for (g,) in c.execute("SELECT goal FROM memories")]
    test(f"数字顺序/字母不同都保留: {a[:18]}",
         a in goals and b in goals, f"rows={[g[:14] for g in goals]}")

# 归一化后逐字相同 → 仍去重
c = fresh('verb2')
engine.store("修复数据库连接池泄漏问题", "HikariCP 配置", "解决", db_conn=c, project_key='p')
r = engine.store("修复数据库连接池泄漏问题", "Druid 配置", "解决", db_conn=c, project_key='p')
test("同 goal 不同方案仍去重",
     r['stored'] is False and c.execute("SELECT count(*) FROM memories").fetchone()[0] == 1,
     f"reason={r['reason'][:22]}")

# ── 19. 权重上限 WEIGHT_CAP（此前无任何测试）───────────────────
print('\n[19] WEIGHT_CAP 上限')
c = fresh('cap')
engine.store("权重上限测试条目内容", "方案", "结果", db_conn=c, project_key='p')
mid = c.execute("SELECT id FROM memories").fetchone()[0]
for _ in range(40):
    engine._batch_reinforce(c, [mid])
c.commit()
w = c.execute("SELECT weight, use_count FROM memories WHERE id=?", (mid,)).fetchone()
test("权重不超过 WEIGHT_CAP=3.0", w['weight'] <= 3.0 and w['weight'] == 3.0,
     f"weight={w['weight']}, use_count={w['use_count']}")

# ── 20. FTS 与 LIKE 两层各自可用（此前无测试，层失效会全绿）─────
print('\n[20] FTS / LIKE 单层可用')
c = fresh('layers')
engine.store("zzunique 特殊词条内容", "包含 unique_keyword 的方案", "结果", db_conn=c, project_key='p')
fts_hits = engine._recall_by_fts(c, "zzunique", 5, project_key='p')
test("FTS 层能独立命中", len(fts_hits) >= 1, f"hits={len(fts_hits)}")
like_hits = engine._recall_by_like(c, "unique_keyword", 5, project_key='p')
test("LIKE 层能独立命中", len(like_hits) >= 1, f"hits={len(like_hits)}")

# ── 21. apply_merge 正向不变量 ─────────────────────────────────
print('\n[21] apply_merge 不变量')
c = fresh('mergeinv')
for g, a in (("分析甲数据集特征", "方案甲"), ("拆解乙数据集字段", "方案乙"), ("梳理丙数据集口径", "方案丙")):
    engine.store(g, a, "结果", db_conn=c, project_key='p')
# 夹具必须非退化：权重/用量全为默认值时 max 与 sum 前后都一样，断言恒真、
# 抓不到「权重清零」「用量不守恒」这类变异。显式设置不同值。
c.execute("UPDATE memories SET weight=2.5, use_count=7 WHERE goal LIKE '分析%'")
c.execute("UPDATE memories SET weight=1.2, use_count=3 WHERE goal LIKE '拆解%'")
c.execute("UPDATE memories SET weight=0.8, use_count=2 WHERE goal LIKE '梳理%'")
c.commit()
before = c.execute("SELECT weight, use_count FROM memories").fetchall()
sum_before = sum(x['use_count'] for x in before)
max_before = max(x['weight'] for x in before)
grp = _dream.find_merge_groups(c, min_group_size=2, project_key='p')
if grp:
    _dream.apply_merge(grp[0], [{"goal": "合并后目标", "approach": "合并方案", "outcome": "合并结果"}], c)
    after = c.execute("SELECT weight, use_count FROM memories WHERE status='active'").fetchall()
    test("合并后权重取最大值",
         max(x['weight'] for x in after) == max_before,
         f"{[x['weight'] for x in before]} → {[x['weight'] for x in after]}")
    test("合并后 use_count 总量守恒",
         sum(x['use_count'] for x in after) == sum_before,
         f"{sum_before} → {sum(x['use_count'] for x in after)}")
    # 注意：不能查 `count(*) FROM memories_fts` —— 它是 content='memories' 的
    # 外部内容表，count 恒等于主表行数，断言永远为真（曾是完全无信息量的伪断言）。
    n_idx = c.execute("SELECT count(*) FROM memories_fts_docsize").fetchone()[0]
    n_main = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    test("合并后 FTS 索引与主表同步", n_idx == n_main, f"docsize={n_idx} main={n_main}")

# ── 22. recall(reinforce=True) 的事务所有权 ────────────────────
print('\n[22] recall 事务所有权')
c = fresh('rctx')
engine.store("recall 事务探针条目内容", "方案", "结果", db_conn=c, project_key='p')
c.execute("INSERT INTO memories (goal,approach,outcome) VALUES ('调用方未提交行','a','b')")
# 必须传同一个 project_key，否则查询落在共享桶、命中 0 条，
# `_batch_reinforce` 永不执行，这条断言就成了空转（曾实测如此）。
_hits = engine.recall("recall 事务探针条目内容", db_conn=c, reinforce=True,
                      project_key='p')
test("recall 探针真的命中（避免空转）", len(_hits) >= 1, f"hits={len(_hits)}")
c.rollback()
rows = [g for (g,) in c.execute("SELECT goal FROM memories")]
# 调用方未提交的行必须随 rollback 一起消失（若 recall 越权 commit，它会残留）
test("recall(reinforce) 不提交调用方事务",
     '调用方未提交行' not in rows, f"rollback 后残留={rows}")
c.close()

# ── 23. 数字守卫是替换的决定性判据 ─────────────────────────────
print('\n[23] 数字守卫对替换的决定性')
c = fresh('numguard')
engine.store("完成 2022C 论文正文排版", "方案甲", "完成", db_conn=c, project_key='p')
# 追加式扩展 + 数字序列不同 → 数字守卫必须拦下（否则会删掉旧行）
engine.store("完成 2022C 论文正文排版第3章补充", "方案乙", "完成", db_conn=c, project_key='p')
rows = [g for (g,) in c.execute("SELECT goal FROM memories")]
test("数字序列不同的扩展不替换",
     len(rows) == 2, f"rows={[g[:20] for g in rows]}")
c.close()

# ── 24. 后续任务不被前缀包含误删 ───────────────────────────────
print('\n[24] 后续任务不误删')
for a, b in [("完成数据库迁移", "完成数据库迁移后的慢查询优化"),
             ("完成论文正文排版", "完成论文正文排版的格式问题排查"),
             ("完成接口联调", "完成接口联调发现的超时问题整改"),
             ("修复交付复现问题", "修复交付复现问题后的性能优化")]:
    c = fresh('followup')
    engine.store(a, "甲的重要经验", "甲", db_conn=c, project_key='p')
    engine.store(b, "乙的全新内容", "乙", db_conn=c, project_key='p')
    goals = [g for (g,) in c.execute("SELECT goal FROM memories")]
    test(f"后续任务保留: {a[:16]}", a in goals and b in goals,
         f"rows={[g[:16] for g in goals]}")
    c.close()

# ── 25. 语义载体（. - / _）不被归一化吞掉 ──────────────────────
print('\n[25] 语义载体保留')
for a, b in [("完成 1.5 倍加速", "完成 15 倍加速"),
             ("完成 A/B 测试", "完成 AB 测试"),
             ("完成 test_1 修复", "完成 test1 修复")]:
    c = fresh('semchar')
    engine.store(a, "甲", "甲", db_conn=c, project_key='p')
    engine.store(b, "乙", "乙", db_conn=c, project_key='p')
    n = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    test(f"语义载体保留: {a[:16]}", n == 2, f"rows={n}")
    c.close()


# ── 26. 共享连接多线程并发（第七轮回归）────────────────────────
print('\n[26] 共享连接并发')
import threading as _th
c = fresh('shared_conn')
c.close()
_conn = engine.get_conn()
_errs = []


def _same_goal():
    try:
        engine.store("共享连接并发同一条目测试内容", "方案", "结果", db_conn=_conn)
    except Exception as e:                                  # noqa: BLE001
        _errs.append(f"{type(e).__name__}: {e}")


ts = [_th.Thread(target=_same_goal) for _ in range(8)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=60)
_n = _conn.execute("SELECT count(*) FROM memories").fetchone()[0]
test("共享连接 8 线程同条目只落 1 行", _n == 1, f"rows={_n}, errs={_errs[:2]}")
test("共享连接并发后无未提交事务", not _conn.in_transaction,
     f"in_transaction={_conn.in_transaction}")
_conn.close()

# 派生任务绝不替换（黑名单时代 8/8 误删）
print('\n[27] 派生任务绝不替换')
for a, b in [("完成服务器扩容", "完成服务器扩容与光纤布线"),
             ("完成数据库迁移", "完成数据库迁移及回滚脚本编写"),
             ("完成支付接口开发", "完成支付接口开发涉及安全加固"),
             ("完成接口开发", "完成接口开发用于生产环境"),
             ("完成监控搭建", "完成监控搭建围绕告警规则"),
             ("完成课程设计", "完成课程设计答辩"),
             ("完成数据库备份", "完成数据库备份恢复演练")]:
    test(f"派生任务不替换: {a[:14]}", not _f.is_version_upgrade(a, b),
         f"appended={_f._norm_goal(b)[len(_f._norm_goal(a)):]}")

# 中文序数分隔不被归一化吞掉
print('\n[28] 中文序数分隔保留')
for a, b in [("完成第1，2章校对", "完成第12章校对"),
             ("完成 A、B 方案对比", "完成 AB 方案对比"),
             ("完成 A|B 模块联调", "完成 AB 模块联调")]:
    c = fresh('cnsep')
    engine.store(a, "甲内容", "甲", db_conn=c, project_key='p')
    engine.store(b, "乙内容", "乙", db_conn=c, project_key='p')
    n = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    test(f"序数分隔不被吞: {a[:14]}", n == 2, f"rows={n}")
    c.close()

print()

# ── 29. 短文本守卫 / 事务所有权 / dry_run 不写入（第八轮补覆盖）────
print('\n[29] 短目标不替换')
c = fresh('shortgoal')
engine.store("完成部署", "甲方案", "甲", db_conn=c, project_key='p')
engine.store("完成部署并上线", "乙方案", "乙", db_conn=c, project_key='p')
rows = [g for (g,) in c.execute("SELECT goal FROM memories")]
test("短目标(<6字)不替换", len(rows) == 2, f"rows={rows}")
c.close()

print('\n[30] decay / apply_merge 事务所有权')
c = fresh('dctx')
engine.store("衰减事务探针条目内容测试", "方案", "结果", db_conn=c, project_key='p')
c.execute("INSERT INTO memories (goal,approach,outcome) VALUES ('调用方未提交行','a','b')")
engine.decay_all(db_conn=c)
c.rollback()
rows = [g for (g,) in c.execute("SELECT goal FROM memories")]
test("decay_all 不提交调用方事务", '调用方未提交行' not in rows, f"残留={rows}")
c.close()

c = fresh('mctx')
for g in ("分析甲数据集特征内容", "拆解乙数据集字段内容", "梳理丙数据集口径内容"):
    engine.store(g, "方案", "结果", db_conn=c, project_key='p')
c.execute("INSERT INTO memories (goal,approach,outcome) VALUES ('调用方未提交行','a','b')")
grp = _dream.find_merge_groups(c, min_group_size=2, project_key='p')
if grp:
    _dream.apply_merge(grp[0], [{"goal": "合并结果目标", "approach": "合并方案",
                                 "outcome": "合并结果"}], c)
    c.rollback()
    rows = [g for (g,) in c.execute("SELECT goal FROM memories")]
    test("apply_merge 不提交调用方事务", '调用方未提交行' not in rows, f"残留={rows}")
c.close()

print('\n[31] dry_run 不写库')
c = fresh('dry')
c.execute("INSERT INTO memories (goal,approach,outcome,weight,last_used) VALUES "
          "('过期条目待删除测试','a','b',0.05,'2020-01-01 00:00:00')")
c.commit()
before = c.execute("SELECT count(*) FROM memories").fetchone()[0]
engine.decay_all(db_conn=c, dry_run=True)
after = c.execute("SELECT count(*) FROM memories").fetchone()[0]
test("dry_run 不删除任何行", before == after, f"{before} → {after}")
engine.decay_all(db_conn=c)
real_after = c.execute("SELECT count(*) FROM memories").fetchone()[0]
archived = c.execute("SELECT count(*) FROM memories WHERE status='archived'").fetchone()[0]
test("真实衰减保留过期原文并归档", real_after == before and archived == 1, f"total={real_after}, archived={archived}")
c.close()

print('\n[32] 归一化后为空的目标不无限堆积')
c = fresh('emptygoal')
engine.store("！！！", "甲", "甲", db_conn=c, project_key='p')
r2 = engine.store("！！！", "乙", "乙", db_conn=c, project_key='p')
n = c.execute("SELECT count(*) FROM memories").fetchone()[0]
test("纯标点目标第二次被去重", r2['stored'] is False, f"rows={n}")
c.close()

print()


# ── 33. FTS 表缺失后补建必须回填（第九轮回归）──────────────────
print('\n[33] FTS 表缺失后自动补建并回填')
import sqlite3 as _sqlite3
_d = pathlib.Path(tempfile.mkdtemp())
_fp = _d / 'ftsold.db'
_raw = _sqlite3.connect(str(_fp))
_raw.executescript((pathlib.Path(__file__).parent / 'schema.sql').read_text())
_raw.execute("INSERT INTO memories (goal,approach,outcome) VALUES ('已有条目甲','a','b')")
_raw.commit()
_raw.execute("DROP TABLE memories_fts")
_raw.commit()
_raw.close()
engine.DB_PATH = _fp
engine._schema_initialized.discard(str(_fp))
_c = engine.get_conn()
test("补建后索引已回填",
     _c.execute("SELECT count(*) FROM memories_fts_docsize").fetchone()[0] == 1,
     f"docsize={_c.execute('SELECT count(*) FROM memories_fts_docsize').fetchone()[0]}")
try:
    engine.store("已有条目甲", "a2", "b2", db_conn=_c)
    engine.decay_all(db_conn=_c)
    test("补建后既有行可强化/剪枝（不再 malformed）", True, "store+decay OK")
except Exception as e:                                  # noqa: BLE001
    test("补建后既有行可强化/剪枝（不再 malformed）", False,
         f"{type(e).__name__}: {e}")
_c.close()

# ── 34. 空白/零宽/标点归一化边界（第九轮回归）──────────────────
print('\n[34] 归一化边界')
# 归一化只折叠**空白本身**（多空格/全角空格→单空格、NFC 规范化），
# 不删除任何字符。因此「有无空格」「标点差异」都是**可见**重复（留重复 < 丢数据）。
_norm_same = [("完成  数据库连接池泄漏问题", "完成 数据库连接池泄漏问题"),
              ("完成 论文排版导出 ", " 完成 论文排版导出")]
_norm_diff = [("完成第 1 2 章校对", "完成第12章校对"),
              ("完成 A B 模块联调", "完成 AB 模块联调"),
              ("完成 A,B 模块联调", "完成 AB 模块联调"),
              ("完成 1:5 比例调整", "完成 15 比例调整"),
              ("完成 1.5 倍加速", "完成 15 倍加速"),
              ("完成 init() 重构", "完成 init 重构")]
for a, b in _norm_same:
    test(f"同义差异归一: {a[:14]}",
         _f._norm_goal(a) == _f._norm_goal(b), "应视为同一件事")
for a, b in _norm_diff:
    c = fresh('normedge')
    engine.store(a, "甲内容", "甲", db_conn=c, project_key='p')
    engine.store(b, "乙内容", "乙", db_conn=c, project_key='p')
    n = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    test(f"语义分隔保留: {a[:14]}", n == 2, f"rows={n}")
    c.close()

# ── 35. 零宽字符目标被拒（第九轮回归）──────────────────────────
print('\n[35] 纯零宽目标被拒')
c = fresh('zwsp')
for i in range(3):
    engine.store(goal='\u200b', approach=f'方案{i}', outcome=f'结果{i}',
                 db_conn=c, project_key='p')
test("纯零宽目标不入库",
     c.execute("SELECT count(*) FROM memories").fetchone()[0] == 0,
     f"rows={c.execute('SELECT count(*) FROM memories').fetchone()[0]}")
c.close()

# ── 36. apply_merge 陈旧 group 不翻倍（第九轮回归）─────────────
print('\n[36] apply_merge 陈旧 group')
c = fresh('stale')
for g in ("分析甲数据集特征内容", "拆解乙数据集字段内容", "梳理丙数据集口径内容"):
    engine.store(g, "方案", "结果", db_conn=c, project_key='p')
_grp = _dream.find_merge_groups(c, min_group_size=2, project_key='p')
if _grp:
    _dream.apply_merge(_grp[0], [{"goal": "合并目标X", "approach": "合并方案",
                                  "outcome": "合并结果"}], c)
    _n1 = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    _dream.apply_merge(_grp[0], [{"goal": "合并目标X", "approach": "合并方案",
                                  "outcome": "合并结果"}], c)
    _n2 = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    test("陈旧 group 重复应用不翻倍", _n1 == _n2, f"{_n1} → {_n2}")
c.close()

# ── 37. 归一化不丢信息（第十轮回归）────────────────────────────
print('\n[37] 归一化不丢信息：覆盖各类分隔/不可见字符')
# 这些全部是「不同的事」，绝不能归一成同一串（否则后者被静默丢弃）
_must_differ = [
    ("完成第 1 2 章校对", "完成第12章校对"),
    ("完成 A B 模块联调", "完成 AB 模块联调"),
    ("完成第１ ２章校对", "完成第１２章校对"),        # 全角数字
    ("完成Ａ Ｂ模块联调", "完成ＡＢ模块联调"),        # 全角字母
    ("完成 A,B 模块联调", "完成 AB 模块联调"),
    ("完成 1:5 比例调整", "完成 15 比例调整"),
    ("完成 1.5 倍加速", "完成 15 倍加速"),
    ("完成 init() 重构", "完成 init 重构"),
    ("完成数据\u200b清洗", "完成数据清洗"),           # 零宽字符
    ("完成A" + chr(1) + "B部署", "完成A B部署"),       # 控制字符 vs 空格
]
for a, b in _must_differ:
    c = fresh('ninfo')
    engine.store(a, "甲内容", "甲", db_conn=c, project_key='p')
    engine.store(b, "乙内容", "乙", db_conn=c, project_key='p')
    n = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    test(f"不归一: {a[:16]!r}", n == 2, f"rows={n}")
    c.close()

# 纯不可见/空白目标必须被拒（不得进库）
c = fresh('blank')
for i, ch in enumerate(['\u2060', '\u180e', '\u3164', '\u200e', '\u200b',
                        '   ', '\u3000']):
    engine.store(goal=ch, approach=f'方案{i}', outcome=f'结果{i}',
                 db_conn=c, project_key='p')
test("纯不可见/空白目标全部被拒",
     c.execute("SELECT count(*) FROM memories").fetchone()[0] == 0,
     f"rows={c.execute('SELECT count(*) FROM memories').fetchone()[0]}")
c.close()

# ── 38. 老 schema（无 project_key 列）不做里程碑替换（第十轮回归）──
print('\n[38] 老 schema 共享桶守卫')
import sqlite3 as _sq
_d = pathlib.Path(tempfile.mkdtemp())
_op = _d / 'oldschema.db'
_r = _sq.connect(str(_op))
_r.executescript("""CREATE TABLE memories(id INTEGER PRIMARY KEY, goal TEXT,
    approach TEXT, outcome TEXT, success_key TEXT DEFAULT '', pitfall TEXT DEFAULT '',
    tags_tool TEXT, tags_domain TEXT, tags_action TEXT, tags_keywords TEXT,
    weight REAL DEFAULT 1.0, use_count INTEGER DEFAULT 0, created_at TEXT,
    last_used TEXT, status TEXT DEFAULT 'active');""")
_r.execute("INSERT INTO memories (goal,approach,outcome) VALUES "
           "('完成 Delta 项目部署验收','甲方案','甲')")
_r.commit()
_oc = _r  # 裸连接（不经 get_conn 补列）
_oc.row_factory = _sq.Row
_rid = _f.find_replaced_milestone("完成 Delta 项目部署验收并交付", "devops",
                                  "general", _oc, project_key='')
test("老 schema 共享桶不替换", _rid is None, f"returned={_rid}")
_oc.close()

# ── 39. _ensure_healthy 独立补列/补触发器（第十轮回归）──────────
print('\n[39] 结构自愈：局部缺失')
# (a) 表在 + FTS 在 + project_key 列缺
_d2 = pathlib.Path(tempfile.mkdtemp())
_p2 = _d2 / 'nocol.db'
_r2 = _sq.connect(str(_p2))
_r2.executescript("""CREATE TABLE memories(id INTEGER PRIMARY KEY, goal TEXT,
    approach TEXT, outcome TEXT, success_key TEXT DEFAULT '', pitfall TEXT DEFAULT '',
    tags_tool TEXT, tags_domain TEXT, tags_action TEXT, tags_keywords TEXT,
    weight REAL DEFAULT 1.0, use_count INTEGER DEFAULT 0, created_at TEXT,
    last_used TEXT, status TEXT DEFAULT 'active');""")
_r2.commit(); _r2.close()
engine.DB_PATH = _p2
engine._schema_initialized.discard(str(_p2))
_c2 = engine.get_conn()
test("缺 project_key 列被补上",
     any(r[1] == 'project_key' for r in _c2.execute("PRAGMA table_info(memories)")),
     "列已补")
_r3 = engine.store("显式项目写入测试条目内容", "方案", "结果",
                   db_conn=_c2, project_key='projX')
test("补列后显式项目可写入", _r3['stored'] is True, f"reason={_r3['reason']}")
# (b) 触发器被删
_c2.execute("DROP TRIGGER memories_ad")
_c2.commit()
engine._schema_initialized.discard(str(_p2))
_c3 = engine.get_conn()
trg = {r[0] for r in _c3.execute(
    "SELECT name FROM sqlite_master WHERE type='trigger'")}
test("缺失触发器被补建", 'memories_ad' in trg, f"triggers={sorted(trg)}")
_c3.close()

# ── 40. 不可见字符全覆盖（第十一轮回归）────────────────────────
print('\n[40] 不可见/孤标记目标全部被拒')
_cps = [0x01, 0x7f, 0x034f, 0x2800, 0x061c, 0xfff9, 0x200b, 0x200c, 0x200e,
        0x2060, 0x180e, 0x3164, 0xfe0f, 0x00ad, 0xfeff, 0x202a, 0x115f]
_miss = [hex(c) for c in _cps if not _f.is_blank(chr(c))]
test("不可见字符全部判空", not _miss, f"漏网={_miss}")
c = fresh('inv')
for i, cp in enumerate(_cps):
    engine.store(goal=chr(cp), approach=f'方案{i}', outcome=f'结果{i}',
                 db_conn=c, project_key='p')
test("不可见目标全部被拒入库",
     c.execute("SELECT count(*) FROM memories").fetchone()[0] == 0,
     f"rows={c.execute('SELECT count(*) FROM memories').fetchone()[0]}")
c.close()
# 正常目标不得被误拒
for t in ("完成论文排版", "café", "方案A", "emoji测试\U0001F389", "a", "1"):
    test(f"正常目标不被误拒: {t[:10]}", not _f.is_blank(t), "is_blank=False")

# ── 41. apply_merge 拒绝不可见内容 + 数值脏数据（第十一轮回归）──
print('\n[41] apply_merge 非法输入保护')
c = fresh('mergeblank')
for g in ("分析甲数据集特征内容", "拆解乙数据集字段内容", "梳理丙数据集口径内容"):
    engine.store(g, "方案", "结果", db_conn=c, project_key='p')
_grp = _dream.find_merge_groups(c, min_group_size=2, project_key='p')
if _grp:
    _dream.apply_merge(_grp[0], [{"goal": "\u200b", "approach": "\u200b",
                                  "outcome": "\u200b"}], c)
    test("不可见内容不删真记忆",
         c.execute("SELECT count(*) FROM memories").fetchone()[0] == 3,
         f"rows={c.execute('SELECT count(*) FROM memories').fetchone()[0]}")
    # 非字符串类型也不得留下半成品
    _dream.apply_merge(_grp[0], [{"goal": ["列表"], "approach": "a",
                                  "outcome": "b"}], c)
    test("非字符串类型不删真记忆",
         c.execute("SELECT count(*) FROM memories").fetchone()[0] == 3,
         f"rows={c.execute('SELECT count(*) FROM memories').fetchone()[0]}")
c.close()

# ── 42. 数值列脏数据不拖垮维护任务（第十一轮回归）───────────────
print('\n[42] 数值列脏数据')
c = fresh('numdirty')
c.execute("INSERT INTO memories (goal,approach,outcome,weight,use_count,"
          "last_used,project_key) VALUES ('脏数值行测试','a','b','一点五',"
          "'不少',datetime('now'),'p')")
c.execute("INSERT INTO memories (goal,approach,outcome,weight,use_count,"
          "last_used,project_key) VALUES ('NULL数值行测试','a','b',NULL,NULL,"
          "datetime('now'),'p')")
c.commit()
try:
    _r = engine.decay_all(db_conn=c)
    test("decay 不因数值脏数据崩溃", isinstance(_r, dict), f"{_r}")
except Exception as e:                                  # noqa: BLE001
    test("decay 不因数值脏数据崩溃", False, f"{type(e).__name__}: {e}")
c.close()

# ── 43. 填充符类不可见字符（第十二轮回归）──────────────────────
print('\n[43] 无字形填充符全部被拒')
_fill = [0xFFA0, 0x1D159, 0x2422, 0x1144E, 0x11945, 0x11C44, 0x3164, 0x2800,
         0x115F, 0x1160]
_fmiss = [hex(c) for c in _fill if not _f.is_blank(chr(c))]
test("填充符全部判空", not _fmiss, f"漏网={_fmiss}")
c = fresh('fill')
for i, cp in enumerate(_fill):
    engine.store(goal=chr(cp), approach=f'方案{i}', outcome=f'结果{i}',
                 db_conn=c, project_key='p')
test("填充符目标全部被拒入库",
     c.execute("SELECT count(*) FROM memories").fetchone()[0] == 0,
     f"rows={c.execute('SELECT count(*) FROM memories').fetchone()[0]}")
c.close()
# 多语言正常文本不误判
for t in ("日本語テスト", "한국어", "Ω", "αβγ", "café"):
    test(f"正常多语言不误判: {t}", not _f.is_blank(t), "is_blank=False")

# ── 44. 强化对脏数值 weight 的兜底（第十二轮回归）───────────────
print('\n[44] 强化路径的脏权重兜底')
c = fresh('dirtyw')
c.execute("INSERT INTO memories (goal,approach,outcome,weight,use_count,"
          "last_used,project_key) VALUES ('脏权重强化测试','a','b','一点五',"
          "0,datetime('now'),'p')")
c.commit()
_mid = c.execute("SELECT id FROM memories").fetchone()[0]
engine._reinforce_inline(c, _mid)
c.commit()
_w = c.execute("SELECT weight FROM memories WHERE id=?", (_mid,)).fetchone()[0]
test("脏权重被强化后不坍缩", _w is not None and float(_w) > 0.2,
     f"weight={_w!r}")
_r = engine.decay_all(db_conn=c)
_alive = c.execute("SELECT count(*) FROM memories WHERE id=?",
                   (_mid,)).fetchone()[0]
test("强化后不被 decay 立即删除", _alive == 1, f"alive={_alive}, decay={_r}")
c.close()

# ── 45. FTS docsize 漂移自愈（第十二轮回归）─────────────────────
print('\n[45] FTS docsize 漂移自愈')
_d = pathlib.Path(tempfile.mkdtemp())
_fp = _d / 'drift.db'
engine.DB_PATH = _fp
engine._schema_initialized.discard(str(_fp))
_c = engine.get_conn()
for i in range(3):
    engine.store(f"漂移自愈测试条目{i}号内容不同", "方案", "结果", db_conn=_c,
                 project_key='p')
# 人为造漂移：绕过触发器直接删 FTS 索引行
_c.execute("INSERT INTO memories_fts(memories_fts) VALUES('delete-all')")
_c.commit()
_n_before = _c.execute("SELECT count(*) FROM memories_fts_docsize").fetchone()[0]
_c.close()
engine._schema_initialized.discard(str(_fp))
_c2 = engine.get_conn()
_n_after = _c2.execute("SELECT count(*) FROM memories_fts_docsize").fetchone()[0]
test("docsize 漂移被自愈", _n_before == 0 and _n_after == 3,
     f"before={_n_before} after={_n_after}")
_c2.close()

# ── 46. 非空白类分隔符不得被当空格（第十三轮回归）───────────────
print('\n[46] C0/行分隔符不被当空格折叠')
_sep_pairs = [
    ("用" + chr(0x09) + "分隔符解析TSV日志", "用 分隔符解析TSV日志"),   # TAB 属空白，折叠
    ("用" + chr(0x1c) + "分隔符解析日志", "用 分隔符解析日志"),         # FS 是数据字符
    ("日志" + chr(0x1e) + "解析与导出", "日志 解析与导出"),             # RS
    ("数据" + chr(0x85) + "清洗流程", "数据 清洗流程"),                 # NEL
    ("报告" + chr(0x2028) + "排版导出", "报告 排版导出"),               # 行分隔符
]
# 前三项[1:] 是「数据字符 vs 空格」必须区分；第 0 项是空白互等（可折叠）
for a, b in _sep_pairs[1:]:
    c = fresh('c0sep')
    engine.store(a, "甲内容", "甲", db_conn=c, project_key='p')
    engine.store(b, "乙内容", "乙", db_conn=c, project_key='p')
    n = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    test(f"分隔符不被当空格: {a[:12]!r}", n == 2, f"rows={n}")
    c.close()
# 空白互等（含全角/多空格）应折叠去重
for a, b in [("完成  数据库迁移任务", "完成 数据库迁移任务"),
             ("完成数据库迁移任务\u3000", "完成数据库迁移任务")]:
    c = fresh('wscollapse')
    engine.store(a, "x1", "y1", db_conn=c, project_key='p')
    r = engine.store(b, "x2", "y2", db_conn=c, project_key='p')
    test(f"空白折叠仍去重: {a[:12]!r}", r['stored'] is False, f"reason={r['reason'][:18]}")
    c.close()

# ── 47. 首尾 C0/行分隔符不得被 strip 吃掉（第十四轮回归）─────────
print('\n[47] 首尾非空白分隔符不被吃掉')
for cp in (0x1c, 0x1d, 0x1e, 0x1f, 0x85, 0x2028, 0x2029):
    c = fresh('stripsep')
    engine.store(chr(cp) + "完成卸载流程梳理", "甲", "甲", db_conn=c, project_key='p')
    engine.store("完成卸载流程梳理", "乙", "乙", db_conn=c, project_key='p')
    n = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    test(f"首尾 U+{cp:04X} 不碰撞", n == 2, f"rows={n}")
    c.close()
# 普通首尾空白仍应被去掉（同一条）
c = fresh('striptrim')
engine.store("\t\n 完成日志清理与归档", "x1", "y1", db_conn=c, project_key='p')
r = engine.store("完成日志清理与归档", "x2", "y2", db_conn=c, project_key='p')
test("普通首尾空白仍被 strip", r['stored'] is False, f"reason={r['reason'][:16]}")
c.close()

# ── 48. own_tx=False 时删旧+插新必须原子（第十四轮回归）─────────
print('\n[48] 调用方事务内写入异常不留半成品')
c = fresh('savepoint')
engine.store("完成服务器扩容与验收", "甲方案", "甲", db_conn=c, project_key='p')
_before = c.execute("SELECT count(*) FROM memories").fetchone()[0]
c.execute("CREATE TRIGGER mm_test_fail BEFORE INSERT ON memories "
          "WHEN new.approach = '触发失败' BEGIN SELECT RAISE(ABORT,'boom'); END")
c.commit()
c.execute("BEGIN IMMEDIATE")
try:
    engine.store("完成服务器扩容与验收并交付", "触发失败", "乙",
                 db_conn=c, project_key='p')
except Exception:                                       # noqa: BLE001
    pass
c.commit()          # 调用方无从知道失败，按契约提交
_after = c.execute("SELECT count(*) FROM memories").fetchone()[0]
test("写入异常不留 DELETE 半成品", _after == _before,
     f"{_before} → {_after}")
c.execute("DROP TRIGGER mm_test_fail")
c.commit()
c.close()

# ── 49. apply_merge 同场景（第十四轮回归）───────────────────────
print('\n[49] apply_merge 在调用方事务内异常不留半成品')
c = fresh('savepoint2')
for g in ("分析甲数据集特征内容", "拆解乙数据集字段内容", "梳理丙数据集口径内容"):
    engine.store(g, "方案", "结果", db_conn=c, project_key='p')
_grp = _dream.find_merge_groups(c, min_group_size=2, project_key='p')
_before = c.execute("SELECT count(*) FROM memories").fetchone()[0]
if _grp and _before == 3:
    c.execute("CREATE TRIGGER mm_test_fail2 BEFORE INSERT ON memories "
              "WHEN new.goal = '触发失败的合并目标' BEGIN SELECT RAISE(ABORT,'boom'); END")
    c.commit()
    c.execute("BEGIN IMMEDIATE")
    try:
        _dream.apply_merge(_grp[0], [{"goal": "触发失败的合并目标",
                                      "approach": "a", "outcome": "b"}], c)
    except Exception:                                   # noqa: BLE001
        pass
    c.commit()
    _after = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    test("apply_merge 异常不留 DELETE 半成品", _after == _before,
         f"{_before} → {_after}")
    c.execute("DROP TRIGGER mm_test_fail2")
    c.commit()
c.close()

# ── 50. 旧 schema 显式项目早退不留 savepoint（第十五轮回归）──────
print('\n[50] 旧 schema 显式项目早退无副作用')
import sqlite3 as _sq2
_d = pathlib.Path(tempfile.mkdtemp())
_op = _d / 'oldschema2.db'
_r0 = _sq2.connect(str(_op))
_r0.executescript("""CREATE TABLE memories(id INTEGER PRIMARY KEY, goal TEXT,
    approach TEXT, outcome TEXT, success_key TEXT DEFAULT '', pitfall TEXT DEFAULT '',
    tags_tool TEXT, tags_domain TEXT, tags_action TEXT, tags_keywords TEXT,
    weight REAL DEFAULT 1.0, use_count INTEGER DEFAULT 0, created_at TEXT,
    last_used TEXT, status TEXT DEFAULT 'active');""")
_r0.commit(); _r0.close()
_oc = _sq2.connect(str(_op))
_oc.row_factory = _sq2.Row
_oc.execute("BEGIN IMMEDIATE")
for i in range(20):
    _rr = engine.store(f"显式项目目标第{i}项", "a", "b", db_conn=_oc,
                       project_key='projX')
    assert _rr['stored'] is False, _rr
_leaked = False
try:
    _oc.execute("RELEASE mm_store_replace")
    _leaked = True
except _sq2.OperationalError:
    pass
test("旧 schema 早退不泄漏 savepoint", not _leaked, f"leaked={_leaked}")
_rr = engine.store("旧schema共享桶写入测试", "a", "b", db_conn=_oc)
test("旧 schema 共享桶仍可写", _rr['stored'] is True, f"reason={_rr['reason'][:20]}")
_oc.commit()
_oc.close()

# ── 51. 旧 schema 显式项目：own_tx=True 不残留事务（第十六轮回归）──
print('\n[51] 旧 schema 显式项目不残留写事务')
import sqlite3 as _sq3
_d = pathlib.Path(tempfile.mkdtemp())
_op = _d / 'oldschema3.db'
_r0 = _sq3.connect(str(_op))
_r0.executescript("""CREATE TABLE memories(id INTEGER PRIMARY KEY, goal TEXT,
    approach TEXT, outcome TEXT, success_key TEXT DEFAULT '', pitfall TEXT DEFAULT '',
    tags_tool TEXT, tags_domain TEXT, tags_action TEXT, tags_keywords TEXT,
    weight REAL DEFAULT 1.0, use_count INTEGER DEFAULT 0, created_at TEXT,
    last_used TEXT, status TEXT DEFAULT 'active');""")
_r0.commit(); _r0.close()
# 用裸连接（own_tx=True）：必须不残留写事务
_oc = _sq3.connect(str(_op))
_oc.row_factory = _sq3.Row
_rr = engine.store("旧schema显式项目目标", "a", "b", db_conn=_oc,
                   project_key='projX')
test("旧 schema 显式项目被拒", _rr['stored'] is False, f"reason={_rr['reason'][:20]}")
test("拒绝后无残留写事务", not _oc.in_transaction,
     f"in_transaction={_oc.in_transaction}")
# 同连接后续写入必须真正落盘（若残留事务，会报成功却永不提交）
_rr2 = engine.store("旧schema共享桶落盘测试", "a", "b", db_conn=_oc)
test("后续写入真正落盘", _rr2['stored'] is True and
     _oc.execute("SELECT count(*) FROM memories").fetchone()[0] == 1,
     f"stored={_rr2['stored']}, rows={_oc.execute('SELECT count(*) FROM memories').fetchone()[0]}")
_oc.close()

# ── 52. 同名 savepoint 不被越权回滚（第十六轮回归）───────────────
print('\n[52] 调用方同名 savepoint 不被越权回滚')
c = fresh('spname')
engine.store("调用方数据行测试内容", "a", "b", db_conn=c, project_key='p')
c.commit()
c.execute("SAVEPOINT mm_store_replace")
_before = c.execute("SELECT count(*) FROM memories").fetchone()[0]
try:
    engine.store(None, "a", "b", db_conn=c)      # 必填校验前抛错
except Exception:                                # noqa: BLE001
    pass
_after = c.execute("SELECT count(*) FROM memories").fetchone()[0]
test("同名 savepoint 下调用方数据不受影响", _before == _after,
     f"{_before} → {_after}")
_sp_alive = True
try:
    c.execute("RELEASE mm_store_replace")
except _sq3.OperationalError:
    _sp_alive = False
test("调用方 savepoint 未被销毁", _sp_alive, f"alive={_sp_alive}")
c.rollback()
c.close()

# ── 53. get_conn 返回的连接绝无未提交事务（第十七轮回归）────────
print('\n[53] get_conn 返回连接的事务不变量')
_c = fresh('conninv')
for i in range(3):
    engine.store(f"连接不变量条目{i}号内容不同", "a", "b", db_conn=_c, project_key='p')
# 制造索引漂移，走 drift 自愈分支
_c.execute("INSERT INTO memories_fts(memories_fts) VALUES('delete-all')")
_c.commit()
_path = engine.DB_PATH
_c.close()
engine._schema_initialized.discard(str(_path))
_c2 = engine.get_conn()
test("drift 自愈后无残留事务", not _c2.in_transaction,
     f"in_transaction={_c2.in_transaction}")
# 该连接上的写入必须真正落盘
_r = engine.store("连接不变量验证目标行", "a", "b", db_conn=_c2)
_c2.close()
_chk = engine.get_conn()
_n = _chk.execute("SELECT count(*) FROM memories WHERE goal LIKE '连接不变量验证%'").fetchone()[0]
test("该连接写入真正落盘", _r['stored'] is True and _n == 1,
     f"stored={_r['stored']}, rows={_n}")
_chk.close()

print()
if errors:
    print(f"=== {len(errors)} 项失败 ===")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print("=== 隔离与替换语义：全部通过 ===")
