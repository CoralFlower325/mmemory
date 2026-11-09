#!/usr/bin/env python3
"""
mmemory CLI — 本地记忆维护入口
用法:
  python cli.py store --goal "..." --approach "..." --outcome "..."
  python cli.py recall "查询消息"
  python cli.py list
  python cli.py archive 123 456 --reason "已过期，移出日常召回"
  python cli.py restore 123 456
  python cli.py stats
  python cli.py decay
  python cli.py dream
  python cli.py tag "测试文本"
"""

import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import engine
import tagger
import filter
import dream
import review


def cmd_store(args):
    if args.conversation:
        try:
            transcript = json.loads(Path(args.conversation).read_text(encoding='utf-8'))
            messages = transcript.get('messages') if isinstance(transcript, dict) else transcript
            fields = {name: getattr(args, name) for name in engine.CONTENT_FIELDS}
            passed, reason, _ = filter.assess_messages(
                messages, fields, memory_kind=args.memory_kind or 'experience',
                task_state=args.task_state or 'completed')
        except (OSError, ValueError) as exc:
            passed, reason = False, f'会话读取失败: {exc}'
        if not passed:
            print(json.dumps({'stored': False, 'memory_id': None, 'reason': reason},
                             ensure_ascii=False, indent=2))
            sys.exit(1)
    elif args.task_state:
        print(json.dumps({'stored': False, 'memory_id': None,
                          'reason': 'task-state 质量标注需要 --conversation 提供真实消息'},
                         ensure_ascii=False, indent=2))
        sys.exit(1)
    result = engine.store(
        goal=args.goal,
        approach=args.approach,
        outcome=args.outcome,
        success_key=args.success_key or '',
        pitfall=args.pitfall or '',
        project_key=args.project_key,
        kind=args.memory_kind or 'experience',
        source_ref=args.source_ref,
        origin=args.origin,
        verification=args.verification,
        valid_from=args.valid_from,
        valid_until=args.valid_until,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['stored']:
        sys.exit(1)


def cmd_recall(args):
    # 召回默认只读不写：每个任务开头都会强制 recall，若此时加分，分数就
    # 只反映「查询有没有撞上」而不是「经验有没有用」。只有确认这条经验真
    # 被采用时才显式传 --reinforce。
    result = engine.recall_with_explain(args.message, limit=args.limit,
                             reinforce=args.reinforce,
                             project_key=args.project_key,
                             deep=args.deep,
                             include_superseded=args.include_superseded,
                             include_expired=args.include_expired)
    memories = result['memories']
    if args.explain:
        print(json.dumps(result['explanation'], ensure_ascii=False, indent=2))
    if not memories:
        print("未找到相关记忆")
        return
    print(engine.format_recall_block(memories))


def cmd_update(args):
    result = engine.update_memory(args.memory_id, goal=args.goal, approach=args.approach,
                                  outcome=args.outcome, success_key=args.success_key,
                                  pitfall=args.pitfall,
                                  kind=args.memory_kind,
                                  source_ref=args.source_ref,
                                  origin=args.origin,
                                  verification=args.verification,
                                  valid_from=args.valid_from,
                                  valid_until=args.valid_until,
                                  reason=args.reason,
                                  project_key=args.project_key)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['updated']:
        sys.exit(1)


def cmd_backup(args):
    print(json.dumps({'backup': engine.backup_database(args.output)}, ensure_ascii=False))


def cmd_archive(args):
    result = engine.archive_memories(args.memory_ids, args.reason,
                                     project_key=args.project_key)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['archived']:
        sys.exit(1)


def cmd_restore(args):
    result = engine.restore_memories(args.memory_ids, project_key=args.project_key)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['restored']:
        sys.exit(1)


def cmd_protect(args):
    result = engine.set_memory_protection(
        args.memory_ids, args.command == 'protect', args.reason,
        project_key=args.project_key)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['changed']:
        sys.exit(1)


def cmd_link(args):
    result = engine.link_memories(args.source_id, args.target_id,
                                  args.relation, args.reason,
                                  project_key=args.project_key)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['linked']:
        sys.exit(1)


def cmd_relations(args):
    print(json.dumps(engine.list_relations(args.memory_id), ensure_ascii=False, indent=2))


def cmd_history(args):
    print(json.dumps(engine.list_history(args.memory_id), ensure_ascii=False, indent=2))


def cmd_purge(args):
    if not args.yes:
        print(json.dumps({
            'purged': False,
            'memory_id': args.memory_id,
            'reason': '硬删除不可恢复；确认后加 --yes',
        }, ensure_ascii=False, indent=2))
        sys.exit(1)
    result = engine.purge_memory(
        args.memory_id, args.reason, project_key=args.project_key)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['purged']:
        sys.exit(1)


def cmd_review(args):
    try:
        result = review.review_candidates(project_key=args.project_key,
                                          status=args.status, limit=args.limit)
    except ValueError as exc:
        print(json.dumps({'reviewed': False, 'reason': str(exc)},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_list(args):
    memories = engine.list_all(status=args.status, limit=args.limit,
                               project_key=args.project_key)
    if not memories:
        print(f"无 {args.status} 记忆")
        return
    for i, m in enumerate(memories, 1):
        if not args.full:
            print(engine.format_memory_summary(m, i))
            print()
            continue
        print(engine.format_memory(m, i))
        print(f"   状态：{m['status']}")
        print(f"   权重：{m.get('weight')}")
        print(f"   使用次数：{m.get('use_count')}")
        print(f"   创建时间：{m.get('created_at') or ''}")
        print(f"   最后使用：{m.get('last_used') or ''}")
        print(f"   衰减游标：{m.get('decay_cursor_at') or ''}")
        print(f"   类型：{m.get('kind') or 'experience'}")
        print(f"   验证状态：{m.get('verification') or 'unverified'}")
        print(f"   来源标识：{m.get('source_ref') or ''}")
        print(f"   来源说明：{m.get('origin') or ''}")
        print(f"   有效区间：{m.get('valid_from') or '不限'} -> {m.get('valid_until') or '不限'}")
        print(f"   修订号：{m.get('revision') or 1}")
        if m.get('protected'):
            print(f"   保护原因：{m.get('protection_reason') or ''}")
        if m['status'] == 'archived':
            print(f"   归档时间：{m.get('archived_at') or ''}")
            print(f"   归档原因：{m.get('archive_reason') or ''}")
        print()


def cmd_stats(args):
    stats = engine.get_stats(project_key=args.project_key)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def cmd_reindex(args):
    print(json.dumps(engine.rebuild_search_index(), ensure_ascii=False, indent=2))


def cmd_decay(args):
    # 默认预览；显式 --apply 才改变权重并把低阈值记录移入可恢复归档。
    result = engine.decay_all(project_key=args.project_key,
                              dry_run=not args.apply)
    if not args.apply:
        print("（预览：不改记忆正文、状态、权重或衰减游标；连接可能维护 schema/索引。--apply 执行衰减及可恢复归档）")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_dream(args):
    result = dream.run_dry_dream(project_key=args.project_key)
    print(f"合并候选组（相同标签不代表相同任务）: {len(result['merge_groups'])}")
    for i, (g, p) in enumerate(zip(result['merge_groups'], result['merge_prompts'])):
        project = g.get('project_key') or '未指定项目'
        action = g.get('action', 'general')
        print(f"\n--- 组 {i+1}: [{project}/{g['domain']}/{g['tool']}/{action}] ({g['count']} 条) ---")
        if args.verbose:
            print(p[:500] + '...' if len(p) > 500 else p)
    print(f"\n衰减预览（未写入）: {result['decay_result']}")


def cmd_tag(args):
    tags = tagger.extract_fixed_tags(args.text)
    keywords = tagger.extract_keywords(args.text)
    print(f"tool:     {tags['tool']}")
    print(f"domain:   {tags['domain']}")
    print(f"action:   {tags['action']}")
    print(f"keywords: {keywords}")


def cmd_filter(args):
    """模拟过滤测试"""
    conversation = {
        'turns': [{'role': 'user', 'content': args.text}],
        'outcome': '完成',
        'has_code': args.has_code,
        'had_error': args.has_error,
        'tool_calls': args.tool_calls,
        'task_complexity': args.complexity,
        'code_blocks': args.code_blocks,
        'success_key': args.success_key or '',
        'pitfall': args.pitfall or '',
    }
    # 模拟多轮
    conversation['turns'] = [
        {'role': 'user', 'content': args.text},
        {'role': 'assistant', 'content': '开始执行...'},
        {'role': 'user', 'content': '好的'},
    ]
    ok, reason, _ = filter.should_store(conversation)
    print(f"{'✓ 存储' if ok else '✗ 跳过'}: {reason}")


def main():
    parser = argparse.ArgumentParser(description='mmemory CLI')
    sub = parser.add_subparsers(dest='command')

    # store
    p = sub.add_parser('store', help='存储记忆')
    p.add_argument('--goal', required=True)
    p.add_argument('--approach', default='')
    p.add_argument('--outcome', default='')
    p.add_argument('--success-key', default='')
    p.add_argument('--pitfall', default='')
    p.add_argument('--project-key', default=None,
                   help='项目命名空间；省略时按高信号项目名推断')
    p.add_argument('--conversation', help='真实消息 JSON 列表或 {"messages": [...]}；启用会话质量过滤')
    p.add_argument('--memory-kind', '--kind', dest='memory_kind',
                   choices=['experience', 'project_fact', 'preference'], default=None)
    p.add_argument('--task-state', choices=['completed', 'in_progress', 'cancelled'], default=None)
    p.add_argument('--source-ref', default='', help='稳定来源标识，如文件、URL 或记忆 ID')
    p.add_argument('--origin', default='', help='来源类型或宿主说明')
    p.add_argument('--verification', default='unverified',
                   choices=['unverified', 'verified', 'disputed'])
    p.add_argument('--valid-from', default='', help='ISO-8601 生效时间')
    p.add_argument('--valid-until', default='', help='ISO-8601 失效时间')

    # recall
    p = sub.add_parser('recall', help='召回记忆')
    p.add_argument('message')
    p.add_argument('--limit', type=int, default=5)
    p.add_argument('--reinforce', action='store_true', help='显式使用后强化命中记忆')
    p.add_argument('--explain', action='store_true', help='解释项目、查询词、候选来源和排序')
    p.add_argument('--deep', action='store_true', help='活跃命中不足时包含归档线索')
    p.add_argument('--include-superseded', action='store_true',
                   help='显示已被 supersedes 关系取代的旧记录并标注')
    p.add_argument('--include-expired', action='store_true',
                   help='显示有效区间之外的历史或未来记录并标注')
    p.add_argument('--project-key', default=None,
                   help='项目命名空间；省略时按查询文本推断')

    p = sub.add_parser('update', help='按 ID 修订正文，保留项目和使用元数据')
    p.add_argument('memory_id', type=int)
    for field in ('goal', 'approach', 'outcome', 'success-key', 'pitfall'):
        p.add_argument('--' + field, default=None)
    p.add_argument('--memory-kind', '--kind', dest='memory_kind', default=None,
                   choices=['experience', 'project_fact', 'preference'])
    p.add_argument('--source-ref', default=None)
    p.add_argument('--origin', default=None)
    p.add_argument('--verification', default=None,
                   choices=['unverified', 'verified', 'disputed'])
    p.add_argument('--valid-from', default=None)
    p.add_argument('--valid-until', default=None)
    p.add_argument('--reason', default='手动修订')
    p.add_argument('--project-key', default=None, help='仅允许更新此项目中的 ID')

    p = sub.add_parser('backup', help='生成一致性 SQLite 备份，不覆盖已有文件')
    p.add_argument('--output', default=None)

    p = sub.add_parser('archive', help='按实际 ID 归档，保留原文和使用历史',
                       description='按实际记忆 ID 整批归档；退出默认召回，仍可恢复。')
    p.add_argument('memory_ids', type=int, nargs='+', help='实际记忆 ID，可指定多个')
    p.add_argument('--reason', required=True, help='归档原因，必填')
    p.add_argument('--project-key', default=None, help='仅允许归档此项目中的 ID')

    p = sub.add_parser('restore', help='按实际 ID 恢复归档记忆，精确冲突时拒绝',
                       description='按原 ID 整批恢复；目标与现役记忆精确冲突时整批拒绝。')
    p.add_argument('memory_ids', type=int, nargs='+', help='已归档的实际记忆 ID，可指定多个')
    p.add_argument('--project-key', default=None, help='仅允许恢复此项目中的 ID')

    p = sub.add_parser('review', help='只读列出潜在重复、冲突和混合项目线索，不自动裁决')
    p.add_argument('--project-key', default=None, help='省略时分项目总览；空串仅共享桶')
    p.add_argument('--status', default='active', choices=['active', 'archived', 'all'])
    p.add_argument('--limit', type=int, default=50, help='最多展示的候选数，不限制参与复核的记录数')

    p = sub.add_parser('link', help='按具体 ID 记录证据关系，不自动归档')
    p.add_argument('source_id', type=int, help='当前结论或派生记录 ID')
    p.add_argument('target_id', type=int, help='被取代记录或原始来源 ID')
    p.add_argument('--relation', required=True, choices=['supersedes', 'derived_from'])
    p.add_argument('--reason', required=True)
    p.add_argument('--project-key', default=None, help='限定两个 ID 的项目')

    p = sub.add_parser('relations', help='查看某个实际 ID 的双向引用关系')
    p.add_argument('memory_id', type=int)

    p = sub.add_parser('history', help='查看某个实际 ID 的当前记录和修订快照')
    p.add_argument('memory_id', type=int)

    p = sub.add_parser('purge', help='不可恢复地删除正文、关系和修订快照')
    p.add_argument('memory_id', type=int)
    p.add_argument('--reason', required=True)
    p.add_argument('--project-key', default=None, help='限定实际 ID 的项目')
    p.add_argument('--yes', action='store_true', help='确认不可恢复删除')

    for command in ('protect', 'unprotect'):
        p = sub.add_parser(command, help='保护重要记录免于自动维护' if command == 'protect' else '解除保护，从当前时刻重新计入衰减')
        p.add_argument('memory_ids', type=int, nargs='+')
        p.add_argument('--reason', required=command == 'protect', default='')
        p.add_argument('--project-key', default=None, help='限定实际 ID 的项目')

    # list
    p = sub.add_parser('list', help='列出记忆')
    p.add_argument('--status', default='active', choices=['active', 'archived', 'all'],
                   help='active 为现役，archived 为归档，all 包含两者（默认 active）')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--project-key', default=None)
    p.add_argument('--full', action='store_true', help='显示完整五字段和维护元数据')

    # stats
    p = sub.add_parser('stats', help='统计信息')
    p.add_argument('--project-key', default=None)

    p = sub.add_parser('reindex', help='重建中文检索索引与关键词列')

    # decay
    p = sub.add_parser('decay', help='权重衰减（默认预览；--apply 执行可恢复归档）',
                       description='预览或执行权重衰减；低于阈值的记录可恢复归档，返回 decayed 和 archived 数量。')
    p.add_argument('--project-key', default=None)
    p.add_argument('--apply', action='store_true',
                   help='执行衰减及可恢复归档（默认只预览）')

    # dream
    p = sub.add_parser('dream', help='Dream 模式 (dry run)')
    p.add_argument('--verbose', '-v', action='store_true')
    p.add_argument('--project-key', default=None)

    # tag
    p = sub.add_parser('tag', help='测试 Tag 生成')
    p.add_argument('text')

    # filter
    p = sub.add_parser('filter', help='测试过滤逻辑')
    p.add_argument('text')
    p.add_argument('--has-code', action='store_true')
    p.add_argument('--has-error', action='store_true')
    p.add_argument('--tool-calls', type=int, default=0)
    p.add_argument('--complexity', default='simple', choices=['simple', 'medium', 'complex'])
    p.add_argument('--code-blocks', type=int, default=0)
    p.add_argument('--success-key', default='')
    p.add_argument('--pitfall', default='')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    cmds = {
        'store': cmd_store, 'recall': cmd_recall, 'list': cmd_list,
        'stats': cmd_stats, 'reindex': cmd_reindex,
        'decay': cmd_decay, 'dream': cmd_dream,
        'tag': cmd_tag, 'filter': cmd_filter,
        'update': cmd_update, 'backup': cmd_backup,
        'archive': cmd_archive, 'restore': cmd_restore,
        'protect': cmd_protect, 'unprotect': cmd_protect,
        'link': cmd_link, 'relations': cmd_relations, 'review': cmd_review,
        'history': cmd_history, 'purge': cmd_purge,
    }
    cmds[args.command](args)


if __name__ == '__main__':
    main()
