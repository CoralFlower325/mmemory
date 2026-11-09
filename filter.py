"""
过滤系统：四层过滤
  第一层：规则过滤（零 LLM 成本）
  第二层：Agent 事实提取 + 规则判断
  第三层：字段信息量校验（防流水账）
  第四层：里程碑替换（同项目后来者替换前者）
  （去重在 engine.check_duplicate 中处理）
"""

import json
import re
import sqlite3
from typing import Optional

from tagger import fallback_tokenize, infer_project_key


# ── 第一层：规则过滤 ────────────────────────────────────────────

def rule_filter(turns: list[dict], outcome: str = '',
                has_code: bool = False, has_error: bool = False,
                tool_calls: int = 0, durable_fact: bool = False) -> tuple[bool, str]:
    if len(turns) < 3:
        return False, "对话轮次 < 3，无实际任务"
    if not durable_fact and not has_code and not has_error and tool_calls == 0:
        return False, "纯闲聊/问答，无代码/报错/工具调用"
    cancel_pattern = r'(?:取消(?:这个|本次|当前|这项)?(?:任务|操作)?|不用做了|别做了|停止(?:这个|当前)?任务|算了(?:不做了)?|cancel(?: this task)?|stop this task)'
    resume_pattern = (
        r'(?:(?:继续|恢复)(?:吧)?(?:[，,：:\s].*)?|'
        r'(?:继续|恢复)(?:这个|本次|当前|这项)?(?:任务|操作)|'
        r'(?:继续|恢复|重新|接着)(?:做|执行|完成|实现|修复|优化|处理|开始).*|'
        r'(?:resume|continue|restart)(?: this task)?)'
    )
    cancelled = False
    for turn in turns:
        if turn.get('role', 'user') != 'user':
            continue
        text = (turn.get('content', '') or '').strip(' \t\n。！!，,？?').lower()
        if re.fullmatch(cancel_pattern, text):
            cancelled = True
        elif re.fullmatch(resume_pattern, text):
            cancelled = False
    if cancelled:
        return False, "用户主动取消"
    outcome_lower = (outcome or '').lower()
    if outcome_lower.strip(' \t\n。.!') in {'cancel', 'cancelled', 'canceled', '取消', '已取消', '用户取消', '用户主动取消', '任务已取消'}:
        return False, "用户主动取消"
    first_content = (turns[0].get('content', '') or '').lower()
    query_patterns = ['是什么', '怎么解释', '什么意思', 'what is', 'explain']
    if any(p in first_content for p in query_patterns) and len(turns) <= 4:
        return False, "简单信息查询"
    return True, "通过规则过滤"


# ── 第二层：事实提取 + 规则判断 ──────────────────────────────────

def agent_judge(task_complexity: str, had_error: bool,
                code_blocks: int, tool_count: int,
                success_key: str = '', pitfall: str = '') -> tuple[bool, str]:
    if task_complexity == 'complex':
        return True, "复杂任务"
    if had_error and pitfall:
        return True, "有踩坑解法"
    if task_complexity == 'medium' and code_blocks > 0:
        return True, "中等复杂度+有代码"
    if task_complexity == 'medium' and tool_count >= 5:
        return True, "中等复杂度+多工具调用"
    if success_key and task_complexity != 'simple':
        return True, "有可复用的成功经验"
    return False, "任务过于简单或无可泛化经验"


# ── 第三层：字段信息量校验 ───────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    """jieba 分词后取关键词集合"""
    try:
        import jieba.analyse
        return set(jieba.analyse.extract_tags(text, topK=8))
    except ImportError:
        return set(fallback_tokenize(text))


def number_signature(text: str) -> tuple:
    """抽取文本里的数字序列（**有序**），用于「是否同一件事」的硬判据。

    jieba 的 extract_tags 会丢弃数字（'阶段1' 与 '阶段2' 分词后完全一致），
    于是两件不同的事相似度算成 1.0。数字在这里最有区分度（版本号、批次、题号、
    阶段号），所以单独抽出来做前置约束。

    必须保持**顺序**：用集合会把「第1章第2节」与「第2章第1节」判成同一件事
    （数字集合都是 {1,2}），而那显然是两件不同的事。
    """
    return tuple(re.findall(r'\d+', text or ''))


def _similarity(a: str, b: str) -> float:
    """关键词级 Jaccard 相似度"""
    ka, kb = _tokenize(a), _tokenize(b)
    if not ka or not kb:
        return 0.0
    intersection = len(ka & kb)
    union = len(ka | kb)
    return intersection / union if union > 0 else 0.0


MAX_MEMORY_CHARS = 1000


def validate_fields(goal: str, approach: str, outcome: str,
                    success_key: str = '', pitfall: str = '') -> tuple[bool, str, dict]:
    """
    校验必填字段，相似度仅提示；不删除可选字段中的独立信息。
    返回 (passed, reason, cleaned_fields)。字段是否有意义由作者判断。

    记忆只保留恢复入口。完整实施细节应放在项目文档中；超长自动记录会在每次
    召回时整段注入，既难以阅读，也会挤占当前任务上下文。
    """
    cleaned = {
        'success_key': success_key.strip(),
        'pitfall': pitfall.strip(),
    }

    fields_to_check = [goal, approach, outcome]

    # 空字段校验：goal/approach/outcome 不能为空
    empty_fields = [name for name, val in
                    [('goal', goal), ('approach', approach), ('outcome', outcome)]
                    if not isinstance(val, str) or is_blank(val)]
    if empty_fields:
        return False, f'必填字段为空: {", ".join(empty_fields)}', cleaned

    total_chars = sum(len(str(value or '').strip()) for value in
                      (goal, approach, outcome, success_key, pitfall))
    if total_chars > MAX_MEMORY_CHARS:
        return False, (f'五字段合计 {total_chars} 字符，超过 {MAX_MEMORY_CHARS} 字符上限；'
                       '请压缩为当前事实、关键教训和项目文档入口'), cleaned

    similar = [name for name, value in cleaned.items() if value and
               any(_similarity(value, field) > 0.8 for field in fields_to_check)]
    if cleaned['success_key'] and cleaned['pitfall'] and _similarity(cleaned['success_key'], cleaned['pitfall']) > 0.8:
        similar.append('success_key/pitfall')
    if similar:
        return True, f'相似度提示：{", ".join(similar)} 可能重复；内容已保留，请作者核对独立信息', cleaned
    return True, "字段校验通过", cleaned


# ── 第四层：里程碑替换 ─────────────────────────────────────────

# `_norm_goal` 只做 NFC + 折叠空白 + 去首尾空白，**保留全部标点**。
# 历史教训：曾把标点/空白/零宽字符当「装饰」删掉，结果「1.5 倍」与「15 倍」、
# 「A/B 测试」与「AB 测试」、「第 1 2 章」与「第12章」归一成同一串而静默丢数据。
# 代价是仅标点/空白风格不同的记录留成可见重复（`完成测试.` vs `完成测试。`），
# 这是可接受的保守方向：留可见重复 < 丢数据。
# `_norm_goal` 只做不丢失信息的操作：NFC 规范化 + 折叠连续空白 + 去首尾空白。
# 细节与踩坑记录见其 docstring —— 改这里之前务必读它。
# 折叠为单空格的「空白」集合：显式列出，而不是用 `\s`。
# Python 的 `\s` 等价于 `str.isspace()`，会把 U+001C–U+001F（FS/GS/RS/US 字段/组/
# 记录/单元分隔符）、U+0085(NEL)、U+2028/U+2029（行/段分隔符）也算进来，把它们
# 替换成空格等于**替换掉一个数据字符**——实测 `用\t分隔` 与 `用 分隔` 会被归一成
# 同一串，后者被静默判重丢弃；`a\x1cb` 与 `a b` 同样碰撞。这些都是语义载体。
_ANY_SPACE = re.compile(r'[ \t\n\r\f\v\u00a0\u1680\u2000-\u200a'
                        r'\u202f\u205f\u3000]+')


# 单独存在时不承载任何信息的字符类别：
#   Cc 控制 / Cf 格式 / Cs 代理 / Co 私用 / Cn 未分配 / Zs 空格 / Zl 行分隔 /
#   Zp 段分隔 / Mn 非间距组合标记 / Me 包围标记
# Mn/Me 必须算进来：组合标记单独出现（没有基字符）没有意义，`isprintable()`
# 对它们返回 True（U+034F/U+FE0F 都是 Mn），仅靠 isprintable 会漏。
_INVISIBLE_CATS = frozenset(
    ('Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zs', 'Zl', 'Zp',
     'Mn', 'Me',          # 非间距/包围组合标记
     'Mc', 'Sk'))         # 间距组合标记与修饰符号：单独出现时同样无可见字形
# 注意：`is_blank` 要求**全部**字符都不可见才判空，所以把 Mc/Sk 计入不会误拒
# 含正常基字符的文本（如 `café`、`かきく`、`αβγ`）。
# 少数「可打印但无可见字形」的码点：类别不在上面（So/Lo），必须显式列出。
# 谚文填充字符成对：U+115F(choseong) / U+1160(jungseong)。
_INVISIBLE_CHARS = frozenset('\u2800\u3164\u115f\u1160')


def is_blank(text: str) -> bool:
    """文本是否「实质为空」——只含空白、控制符与孤立组合标记（用于必填校验）。

    实现是「剔除不可见字符后是否为空」，而非「是否所有字符都不可见」：
    后者对 `café`(NFD) 这类含组合标记的正常文本也安全（有基字母即可打印），
    但纯组合标记/纯控制符会被判空——那正是要拒绝的垃圾输入。

    不用枚举码点的白名单：实测只能覆盖约 14% 的 Cf/Cc 码点
    （`\x01`/`\x7f`/U+034F/U+2800/U+061C/U+FFF9/U+E0020/U+FE0F 全部漏网），
    纯不可见目标因此能入库，堆成既读不懂也召不回的垃圾行。
    """
    if not text:
        return True
    import unicodedata
    for ch in text:
        if not ch.isprintable():
            continue
        if ch in _INVISIBLE_CHARS:
            continue
        if unicodedata.category(ch) in _INVISIBLE_CATS:
            continue
        # 「可打印但无可见字形」的填充符无法靠类别识别（类别是 So/Lo 等），
        # 用名字兜底：FILLER / BLANK / NOTEHEAD / NULL 这类。实测 U+FFA0
        # （HALFWIDTH HANGUL FILLER）等 33 个码点靠类别全部漏网。
        if any(mark in unicodedata.name(ch, '') for mark in
               ('FILLER', 'BLANK', 'NOTEHEAD', 'NULL')):
            continue
        return False
    return True


def _norm_goal(text: str) -> str:
    """归一化目标文本，用于「是否同一件事」的判定（去重与里程碑替换）。

    **只做不丢失信息的操作**：Unicode NFC 规范化 + 连续空白折叠为单个空格 +
    去首尾空白。不删除、不替换任何其它字符——`_ANY_SPACE` 显式列出空白码点，
    避免 `\s` 把 C0 分隔符/行分隔符当空格替换掉（见该常量的注释）。

    这是三轮连续踩坑后收敛出的结论：任何「聪明」的字符处理都会制造碰撞，让
    不同的事归一成同一串，进而在去重（静默丢弃后写的内容）或里程碑替换
    （直接 DELETE 旧行）里丢数据。实测过的碰撞：
      删标点：`1,2号` ≡ `12号`、`1:5` ≡ `15`、`init()` ≡ `init`、`1.5倍` ≡ `15倍`
      删空白：`第 1 2 章` ≡ `第12章`、`A B` ≡ `AB`（含全角 `Ａ Ｂ` ≡ `ＡＢ`）
      删零宽：把 `a\u200bb` 与 `ab` 合并，且哨兵字符自身与真实输入碰撞
    现在这些全部不再碰撞。代价是仅空白/标点风格不同的记录会留成**可见**重复
    （`完成X` 与 `完成 X`、`完成测试。` 与 `完成测试`），由 Dream 或人工合并。
    宁可留可见重复，绝不静默丢数据。
    """
    import unicodedata
    t = unicodedata.normalize('NFC', text or '')
    t = _ANY_SPACE.sub(' ', t)
    # 用显式字符集 strip，**不用** `str.strip()`：后者按 `str.isspace()` 判定，
    # 会把 U+001C–U+001F、U+0085、U+2028/U+2029 也算作首尾空白吃掉，于是
    # 「`\x1c完成卸载`」与「`完成卸载`」会被归一成同一串，去重时静默丢弃后者。
    # 这些字符是语义载体（字段/组/记录/单元分隔符、行/段分隔符），不能当空白。
    return t.strip(' ')


# 只允许这些「完成态补语」作为追加片段（归一化后整体匹配，不是包含）。
# 方向是**白名单**而不是黑名单：黑名单永远无法穷举派生任务的说法（实测用
# `与光纤布线`/`及回滚脚本`/`涉及安全加固`/`用于生产环境`/`围绕告警规则`/
# `答辩`/`恢复演练`/`for 移动端` 等 8/8 绕过），只要追加部分承载任何新工作
# 语义就是新任务，必须保留。
_UPGRADE_SUFFIXES = (
    '并交付', '并交付验收', '并验收', '并验收通过', '并全部通过',
    '并全部通过验收', '并通过验收', '并上线', '并发布', '并提交',
    '并完成', '并已交付', '并已上线', '并内部评审', '并终审',
    '并正式发布', '并结项', '并归档', '上线', '已上线', '已交付',
    '已发布', '已提交', '已验收', '已完成', '已通过', '全部通过',
    '验收通过', '交付验收',
)


def is_version_upgrade(old_goal: str, new_goal: str) -> bool:
    """新目标是否只是旧目标的**完成态升级**（同一件事的版本演进）。

    这是替换的唯一文本判据，替代了此前所有基于相似度的判据。实测相似度无法
    区分两类情形：
      真版本升级：完成论文正文排版 → 完成论文正文排版并交付（0.800）
      不同任务  ：完成政治错题本录入 → 完成英语错题本录入（0.600）
    用相似度会把不同任务判成新版而删掉旧行（不可恢复的数据丢失）。

    判据是「归一化后旧文本是新文本的前缀，且追加片段**恰好是**一个完成态补语
    （见 `_UPGRADE_SUFFIXES`）」。这里必须是白名单：

    - 仅判前缀不够：`完成数据库迁移` → `完成数据库迁移后的慢查询优化` 满足前缀，
      却是派生出的新任务（误删 4/4）。
    - 前缀 + 黑名单也不够：黑名单无法穷举下游说法，实测 `与光纤布线`、`及回滚
      脚本`、`涉及安全加固`、`用于生产环境`、`围绕告警规则`、`答辩`、`恢复演练`、
      `for 移动端` 等 8/8 全部绕过并被误删。

    保守方向：漏判只会留下**可见**的重复（可人工合并），误判会永久删掉一条
    真实记忆。
    """
    old, new = _norm_goal(old_goal), _norm_goal(new_goal)
    if not old or not new or len(new) <= len(old):
        return False
    # 旧目标太短时包含关系不可靠（如「完成」是很多目标的子串）
    if len(old) < 6:
        return False
    if not new.startswith(old):
        return False
    appended = new[len(old):].lstrip('，,、')
    return appended in _UPGRADE_SUFFIXES


def find_replaced_milestone(goal: str, tags_domain: str, tags_tool: str,
                            conn: sqlite3.Connection,
                            project_key: Optional[str] = None) -> Optional[int]:
    """
    查找应被替换的旧里程碑。

    规则（三条全部满足才替换）：
    1. 新旧目标都必须含里程碑词（完成/通过/交付/验收/提交/发布等）
    2. 同 project 下（project_key 为空即共享桶，直接禁用替换）
    3. 旧目标是新目标的扩展前身（`is_version_upgrade`），且数字序列一致

    数字序列必须一致：jieba 会丢弃数字，`阶段1`/`阶段2` 分词后完全相同，
    必须靠数字区分（详见 `number_signature`）。

    ``tags_tool`` 只保留在签名里以兼容调用方，不参与匹配；`tags_domain` 不参与
    匹配（曾作为硬门槛导致同标签漂移的版本无法替换，也曾作为放宽条件导致跨领域
    误删），列为参数仅为兼容。判据完全由文本包含关系承担——它是唯一能可靠区分
    「同一件事的版本升级」与「不同的相似任务」的信号。
    返回旧记忆 ID（应由 store 原子归档），或 None。
    """
    goal = goal or ''
    # 只有里程碑类目标才触发替换
    milestone_keywords = ['完成', '通过', '交付', '验收', '提交', '发布', '上线',
                         '放行', '修复完成', '全部通过', '可提交']
    if not any(kw in goal for kw in milestone_keywords):
        return None

    project = infer_project_key(goal) if project_key is None else str(project_key).strip()
    try:
        has_project_column = any(row[1] == 'project_key' for row in conn.execute(
            "PRAGMA table_info(memories)").fetchall())
    except sqlite3.OperationalError as e:
        # 只把「表不存在」当旧 schema。其它错误（页损坏、interrupted 等）必须上抛：
        # 若吞掉，has_project_column=False 会让下面的共享桶守卫被整体绕过，
        # 从而返回**别的项目**的历史行 ID，store 随即 DELETE 它——跨项目静默删数据。
        if 'no such table' in str(e).lower():
            has_project_column = False
        else:
            raise

    # 旧 schema 无法安全表达显式项目；不返回候选，避免跨项目替换。
    if not has_project_column and project:
        return None

    project_sql = ' AND project_key = ?' if has_project_column else ''
    params = []
    if has_project_column:
        params.append(project)

    # 没有项目身份就绝不替换：共享桶（project_key=''）与旧 schema（无 project_key
    # 列）都属此类。共享桶里装着所有未被识别的项目，把两个不同项目的目标判成
    # 版本演进（"完成iOS App上线发布" / "完成安卓App上线发布"）就是不可恢复的
    # 误删；旧 schema 更是连隔离列都没有。两条都必须拦。
    #
    # 注意这里**不能**写成 `if has_project_column and project == ''`：老 schema 下
    # has_project_column=False 会让条件整体短路，守卫失效（曾实测删掉真实里程碑）。
    if not has_project_column or project == '':
        return None

    candidates = conn.execute(f"""
        SELECT id, goal, tags_domain, tags_keywords FROM memories
        WHERE status = 'active'
          {project_sql}
        ORDER BY created_at ASC
    """, params).fetchall()

    new_numbers = number_signature(goal)

    for c in candidates:
        # 旧目标也必须是里程碑
        if not any(kw in c['goal'] for kw in milestone_keywords):
            continue

        # 数字序列不同 = 不同的事（阶段1 vs 阶段2、第1批 vs 第2批、v1 vs v2）。
        # jieba 会丢弃数字、使这两个目标文本相似度算成 1.0，所以必须前置拦截，
        # 否则新版本会去"替换"一个语义上无关的旧里程碑。
        if number_signature(c['goal']) != new_numbers:
            continue

        # 唯一文本判据：旧目标必须是新目标的扩展前身。相似度/domain/关键词重叠
        # 都不能区分「版本升级」与「不同的相似任务」，实测会把政治/英语、第一问/
        # 第二问这类不同任务误判成新版而删掉（见 is_version_upgrade）。
        if is_version_upgrade(c['goal'], goal):
            return c['id']

    return None


# ── 综合过滤 ────────────────────────────────────────────────────

_SOLUTION_PATTERN = re.compile(
    r'(?:解法|解决|修复|修正|改正|改用|改为|换成|调整|重试|恢复|替换|'
    r'补充|补齐|移除|删除|重新|处理后|通过|workaround|fix(?:ed)?|resolved?|'
    r'retry|replace)', re.IGNORECASE)
_UNRESOLVED_PATTERN = re.compile(
    r'(?:无法|未能|尚未|仍未|没有)(?:解决|修复|恢复)|仍(?:然)?失败',
    re.IGNORECASE)


def pitfall_has_resolution(text: str) -> bool:
    """踩坑字段必须留下可执行的处理信号，不只是重复错误现象。

    这里只做保守的机械门禁，不声称能证明方案正确；成功关键字段存在时仍由作者
    对语义负责。
    """
    value = (text or '').strip()
    return bool(value and _SOLUTION_PATTERN.search(value)
                and not _UNRESOLVED_PATTERN.search(value))


def should_store(conversation: dict) -> tuple[bool, str, dict]:
    """
    综合判断是否值得存储。
    返回 (worth_storing, reason, cleaned_fields)

    注意：不修改传入的 conversation dict（只读语义），
    返回 cleaned_fields 供调用方决定是否采用。
    """
    kind = conversation.get('memory_kind', 'experience')
    if kind not in ('experience', 'project_fact', 'preference'):
        return False, '记忆类型必须是 experience/project_fact/preference', {}
    if conversation.get('task_state', 'completed') != 'completed':
        return False, '仅保留已经形成的事实或完成的经验，不保留临时进度/取消任务', {}
    durable_fact = kind in ('project_fact', 'preference')

    # 第一层：规则
    ok, reason = rule_filter(
        turns=conversation.get('turns', []),
        outcome=conversation.get('outcome', ''),
        has_code=conversation.get('has_code', False),
        has_error=conversation.get('had_error', False),
        tool_calls=conversation.get('tool_calls', 0),
        durable_fact=durable_fact,
    )
    if not ok:
        return False, f"规则过滤: {reason}", {}

    # 第二层：Agent 判断
    ok, reason = (True, '明确的持久事实/偏好') if durable_fact else agent_judge(
        task_complexity=conversation.get('task_complexity', 'simple'),
        had_error=conversation.get('had_error', False),
        code_blocks=conversation.get('code_blocks', 0),
        tool_count=conversation.get('tool_calls', 0),
        success_key=conversation.get('success_key', ''),
        pitfall=conversation.get('pitfall', ''),
    )
    if not ok:
        return False, f"Agent判断: {reason}", {}

    if not durable_fact:
        success_key = str(conversation.get('success_key', '')).strip()
        pitfall = str(conversation.get('pitfall', '')).strip()
        if not success_key and not pitfall:
            return False, '自动经验缺少可复用的成功关键或踩坑解法；普通完成记录请使用项目文档', {}
        if pitfall and not success_key and not pitfall_has_resolution(pitfall):
            return False, '踩坑字段需同时包含处理方式或可复用的解决信号，不能只记录错误现象', {}

    # 第三层：字段信息量
    fields_ok, fields_reason, cleaned = validate_fields(
        goal=conversation.get('goal', ''),
        approach=conversation.get('approach', ''),
        outcome=conversation.get('outcome', ''),
        success_key=conversation.get('success_key', ''),
        pitfall=conversation.get('pitfall', ''),
    )
    # CLI filter 不提供必填正文时，只判断任务是否值得保留；store 负责拒绝空正文。
    if not fields_ok and '必填字段为空' not in fields_reason:
        return False, f"字段校验: {fields_reason}", cleaned

    reason = '通过过滤' if fields_reason == '字段校验通过' or not fields_ok else f'通过过滤；{fields_reason}'
    return True, reason, cleaned


def message_text(content) -> str:
    """提取宿主消息的文本；图片和工具参数不充当对话正文。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(block['text'] for block in content
                         if isinstance(block, dict) and
                         block.get('type') in ('text', 'input_text', 'output_text') and
                         isinstance(block.get('text'), str))
    return ''


def assess_messages(messages: list[dict], fields: dict, *,
                    memory_kind: str = 'experience',
                    task_state: str = 'completed') -> tuple[bool, str, dict]:
    """CLI 会话导入和 Hermes 共用入口，证据只从真实 transcript 提取。

    类型及完成状态是作者声明，不是系统对语义正确性的裁决。工具消息不计入
    对话轮数，retain 本身也不构成工作复杂度；直接人工 store 不调用本入口。
    """
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        return False, '缺少真实会话 messages 列表', {}
    names = ('goal', 'approach', 'outcome', 'success_key', 'pitfall')
    if any(not isinstance(fields.get(name, ''), str) for name in names):
        return False, '五字段必须是字符串', {}
    ok, reason, cleaned = validate_fields(*(fields.get(name, '') for name in names))
    if not ok:
        return False, reason, cleaned

    turns = []
    code_blocks = 0
    tool_calls = 0
    had_error = False
    work_calls = set()
    completed_calls = set()
    for message in messages:
        role = message.get('role')
        text = message_text(message.get('content'))
        if role in ('user', 'assistant') and text.strip():
            turns.append({'role': role, 'content': text})
            if role == 'assistant':
                code_blocks += len(re.findall(r'```[\s\S]*?```', text))
        if role == 'assistant':
            for call in message.get('tool_calls') or []:
                name = (call.get('function') or {}).get('name', call.get('name', ''))
                if name and name not in ('mmemory_retain', 'mmemory_update', 'mmemory_recall', 'memory') and call.get('id'):
                    work_calls.add(call['id'])
        call_id = message.get('tool_call_id')
        if role == 'tool' and call_id in work_calls and call_id not in completed_calls:
            completed_calls.add(call_id)
            tool_calls += 1
            try:
                result = json.loads(text)
            except (ValueError, TypeError):
                result = None
            if isinstance(result, dict) and (result.get('error') or
                    result.get('is_error') is True or result.get('success') is False or
                    isinstance(result.get('exit_code'), int) and result['exit_code'] != 0):
                had_error = True

    conversation = {
        **{name: fields.get(name, '') for name in names},
        'turns': turns, 'memory_kind': memory_kind, 'task_state': task_state,
        'has_code': code_blocks > 0, 'code_blocks': code_blocks,
        'had_error': had_error, 'tool_calls': tool_calls,
        'task_complexity': 'medium' if code_blocks or tool_calls >= 5 else 'simple',
    }
    return should_store(conversation)
