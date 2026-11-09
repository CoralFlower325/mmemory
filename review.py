"""只读记忆复核线索；相同文本、版本差异和项目引用都不是自动裁决。"""

import re
import sqlite3
from itertools import combinations
from pathlib import Path

import jieba

import engine
import filter as memory_filter
import tagger


_GOAL_FILLERS = {
    '完成', '修订', '更新', '配置', '修复', '排查', '当前', '现役', '最新',
    '已经', '任务', '项目', '记录', '进行', '工作', '交付', '验收', '通过',
}
_VERSION_RE = re.compile(
    r'(?<![A-Za-z0-9_])(?P<entity>jdk|java|python|node(?:\.js)?|cuda|pytorch|torch|mysql|postgresql|sqlite)'
    r'\s*[:=]?\s*v?(?P<value>\d+(?:\.\d+)*)(?![A-Za-z0-9_])', re.IGNORECASE)
_ENTITY_ALIASES = {'jdk': 'java', 'node.js': 'node', 'torch': 'pytorch'}
_STATE_RE = re.compile(
    r'(?P<entity>[A-Za-z][A-Za-z0-9_.-]*|[\u4e00-\u9fff]{2,12}?)'
    r'\s*(?:通道|服务|接口|方案|功能|账号|账户|模型)?\s*[:：]?\s*'
    r'(?P<value>不可用|不能使用|无法使用|不支持|未启用|已关闭|已失效|失效|无效|'
    r'可用|可以使用|支持|已启用|已开启|有效)')
_NEGATIVE_STATES = {'不可用', '不能使用', '无法使用', '不支持', '未启用',
                    '已关闭', '已失效', '失效', '无效'}


def _matches(text, keyword):
    """拉丁项目名按词边界匹配，中文项目名按原文片段匹配。"""
    pattern = re.escape(keyword)
    if keyword[0].isascii() and keyword[0].isalnum():
        pattern = r'(?<![A-Za-z0-9_])' + pattern
    if keyword[-1].isascii() and keyword[-1].isalnum():
        pattern += r'(?![A-Za-z0-9_])'
    return list(re.finditer(pattern, text, re.IGNORECASE))


def _goal_terms(goal):
    text = memory_filter._norm_goal(goal).casefold()
    for _, aliases in tagger.PROJECT_KEYWORDS:
        for alias in aliases:
            for match in reversed(_matches(text, alias.casefold())):
                text = text[:match.start()] + ' ' + text[match.end():]
    text = _VERSION_RE.sub(lambda m: _ENTITY_ALIASES.get(
        m.group('entity').casefold(), m.group('entity').casefold()), text)
    synonyms = tagger.load_synonym_map()
    return frozenset(synonyms.get(token, token) for token in jieba.cut(text)
                     if token not in _GOAL_FILLERS and
                     (len(token) > 1 and re.fullmatch(r'[\w\u4e00-\u9fff]+', token)
                      or re.fullmatch(r'[a-z]', token)))


def _claims(memory, fields):
    claims = []
    for field in fields:
        text = str(memory.get(field) or '')
        for pattern, kind in ((_VERSION_RE, 'version'), (_STATE_RE, 'state')):
            for match in pattern.finditer(text):
                entity = match.group('entity').casefold()
                value = match.group('value')
                claims.append({
                    'memory_id': memory['id'], 'field': field, 'entity':
                    _ENTITY_ALIASES.get(entity, entity), 'claim_type': kind,
                    'value': value, 'polarity': None if kind == 'version' else
                    ('negative' if value in _NEGATIVE_STATES else 'positive'),
                    'matched_text': match.group(0), 'text': text,
                })
    return claims


def _contrasts(left, right):
    clues = []
    for a, b in ((a, b) for a in left for b in right):
        if a['entity'] != b['entity'] or a['claim_type'] != b['claim_type']:
            continue
        differs = (a['value'] != b['value']) if a['claim_type'] == 'version' else (
            a['polarity'] != b['polarity'])
        if differs:
            clue = {'type': 'explicit_' + a['claim_type'] + '_difference',
                    'entity': a['entity'], 'evidence': [a, b],
                    'note': '版本或状态变化可能是合法升级、历史说明或不同条件，须按原文裁决'}
            if clue not in clues:
                clues.append(clue)
    return clues


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def review_candidates(*, project_key=None, status='active', limit=50,
                      db_conn=None, relations=None):
    """返回带具体 ID、五字段原文和显式关系的复核候选，不写入或维护数据库。

    None 分项目总览，空键严格限定共享桶。limit 只裁剪展示，不截断参与比较的记录。
    默认 mode=ro，刻意不调用会自动迁移/修索引的 engine.get_conn。
    """
    if status not in {'active', 'archived', 'all'}:
        raise ValueError('status 仅支持 active、archived、all')
    limit = int(limit)
    if not 1 <= limit <= 500:
        raise ValueError('limit 须为 1 到 500')
    project = None if project_key is None else str(project_key).strip()
    own_conn = db_conn is None
    conn = db_conn if db_conn is not None else sqlite3.connect(
        Path(engine.DB_PATH).expanduser().resolve().as_uri() + '?mode=ro', uri=True)
    try:
        with engine.conn_lock(conn):
            if own_conn:
                conn.execute('BEGIN')
            clause = '' if project is None else ' WHERE COALESCE(project_key,\'\') = ?'
            all_memories = _rows(conn, 'SELECT * FROM memories' + clause + ' ORDER BY id',
                                 () if project is None else (project,))
            memories = [row for row in all_memories if status == 'all' or row['status'] == status]
            by_id = {row['id']: row for row in all_memories}
            selected_ids = {row['id'] for row in memories}
            if relations is None:
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                      "AND name='memory_relations'").fetchone()
                relations = _rows(conn, 'SELECT * FROM memory_relations ORDER BY '
                                  'created_at,source_id,target_id,relation') if exists else []
            relevant_relations = []
            for relation in relations:
                source = by_id.get(relation['source_id'])
                target = by_id.get(relation['target_id'])
                if source is None or target is None or (source.get('project_key') or '') != (
                        target.get('project_key') or ''):
                    continue
                if not selected_ids.intersection((source['id'], target['id'])):
                    continue
                relevant_relations.append({**relation, 'source': {
                    key: source.get(key) for key in ('id', 'goal', 'status', 'project_key')},
                    'target': {key: target.get(key) for key in ('id', 'goal', 'status', 'project_key')}})

            candidates = []

            # 关系两端必须同项目。正常写入会拦截跨项目引用；这里仍显式检查
            # 直接迁移或外部写入留下的违约数据，避免 review 静默把它过滤掉。
            for relation in relations:
                source = by_id.get(relation['source_id'])
                target = by_id.get(relation['target_id'])
                if source is None or target is None:
                    continue
                if not selected_ids.intersection((source['id'], target['id'])):
                    continue
                if (source.get('project_key') or '') == (target.get('project_key') or ''):
                    continue
                candidates.append({
                    'kind': 'cross_project_relation',
                    'memory_ids': [source['id'], target['id']],
                    'project_key': source.get('project_key') or '',
                    'requires_judgment': True,
                    'memories': [source, target],
                    'clues': [{'type': 'cross_project_relation',
                               'relation': relation['relation'], 'reason': relation['reason'],
                               'note': '关系两端项目不同；应核实迁移遗漏或外部写入'}],
                    'relations': [relation],
                })

            def add(kind, records, clues):
                ids = [row['id'] for row in records]
                candidates.append({'kind': kind, 'memory_ids': ids,
                    'project_key': records[0].get('project_key') or '',
                    'requires_judgment': True, 'memories': records, 'clues': clues,
                    'relations': [relation for relation in relevant_relations if
                                  set(ids).intersection((relation['source_id'], relation['target_id']))]})

            terms = {row['id']: _goal_terms(row['goal']) for row in memories}
            for memory in memories:
                hits = []
                for name, aliases in tagger.PROJECT_KEYWORDS:
                    for field in engine.CONTENT_FIELDS:
                        text = str(memory.get(field) or '')
                        matched = sorted({match.group(0) for alias in aliases
                                          for match in _matches(text, alias)})
                        if matched:
                            hits.append({'project_key': name, 'field': field,
                                         'matched_text': matched, 'text': text})
                mentioned = {hit['project_key'] for hit in hits}
                own_project = memory.get('project_key') or ''
                other_projects = mentioned - ({own_project} if own_project in mentioned else set())
                # 一个项目正文提到它使用的工具项目很常见，不能当冲突。只有归属项目外
                # 还出现至少两个不同项目，才值得人工判断是否混入了独立事实。
                if len(other_projects) >= 2:
                    add('mixed_projects', [memory], [{'type': 'multiple_project_references',
                        'evidence': hits, 'note': '多项目引用可能完全合法；仅提示核对是否混入独立事实'}])
                internal = _contrasts(_claims(memory, ('outcome',)),
                                      _claims(memory, ('success_key',)))
                if internal:
                    add('potential_conflict', [memory], internal)

            for left, right in combinations(memories, 2):
                if (left.get('project_key') or '') != (right.get('project_key') or ''):
                    continue
                exact = memory_filter._norm_goal(left['goal']) == memory_filter._norm_goal(right['goal'])
                same_terms = len(terms[left['id']]) >= 2 and terms[left['id']] == terms[right['id']]
                if not exact and not same_terms:
                    continue
                same_numbers = memory_filter.number_signature(left['goal']) == (
                    memory_filter.number_signature(right['goal']))
                if exact or same_numbers:
                    add('potential_duplicate', [left, right], [{
                        'type': 'normalized_goal_equal' if exact else 'goal_terms_equal',
                        'goal_texts': [left['goal'], right['goal']],
                        'shared_terms': sorted(terms[left['id']]),
                        'different_fields': [field for field in engine.CONTENT_FIELDS if
                                             left.get(field) != right.get(field)],
                        'note': '文字接近不证明同一事实；数字顺序一致仍不能替代人工判断'}])
                contrasts = _contrasts(_claims(left, ('outcome',)), _claims(right, ('outcome',)))
                if contrasts:
                    add('potential_conflict', [left, right], contrasts)
            return {'read_only': True, 'scope': {'project_key': project, 'status': status},
                    'scanned_count': len(memories), 'candidate_count': len(candidates),
                    'truncated': len(candidates) > limit, 'candidates': candidates[:limit],
                    'relations': relevant_relations,
                    'notice': '仅为待复核线索，未判断事实真假、合并、归档、修订或强化任何记忆'}
    finally:
        if own_conn:
            conn.close()
