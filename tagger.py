"""
Tag 生成系统：三层
  第一层：固定维度（tool/domain/action），规则匹配
  第二层：动态关键词，jieba TF-IDF
  第三层：同义词归一化
"""

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Optional

# ── 第一层：规则库 ──────────────────────────────────────────────

TOOL_PATTERNS: dict[str, str] = {
    # 注意：python 的 pattern 不能包含裸的 `import `——它在字典里排第一，会把
    # `import pandas` / `import torch` 这类具体库全部抢先判成 python，令后面
    # 所有库的 pattern 失效。具体库各自由自己的 pattern 命中。
    'python':     r'python|\.py\b',
    'selenium':   r'selenium|webdriver|chromedriver|undetected',
    'playwright': r'playwright',
    'requests':   r'import requests|requests\.(get|post|put|delete)',
    'httpx':      r'import httpx|httpx\.',
    'pandas':     r'import pandas|pd\.|DataFrame|read_csv|read_excel',
    'numpy':      r'import numpy|np\.',
    'matplotlib': r'import matplotlib|plt\.',
    'flask':      r'from flask|import flask|Flask\(__name__\)',
    'fastapi':    r'from fastapi|import fastapi|FastAPI\(\)',
    'sqlalchemy': r'from sqlalchemy|create_engine',
    'redis':      r'import redis|redis\.Redis',
    'celery':     r'from celery|Celery\(',
    'docker':     r'docker (build|run|compose)|Dockerfile|docker-compose',
    'git':        r'git (commit|push|pull|merge|rebase|checkout|clone)',
    'ffmpeg':     r'ffmpeg|ffprobe',
    'pytest':     r'import pytest|pytest\.|def test_',
    'jieba':      r'import jieba|jieba\.',
    'pydantic':   r'from pydantic|BaseModel',
    'nginx':      r'nginx|proxy_pass',
    'latex':      r'\\documentclass|\\begin\{document\}|xelatex|pdflatex',
    'fpga':       r'Verilog|VHDL|vivado|quartus|FPGA',
    'sqlite':     r'sqlite3?\.connect|import sqlite3',
    'mysql':      r'pymysql|mysql\.connector|MySQLdb',
    'postgres':   r'psycopg2|asyncpg|postgresql',
    'hermes':     r'hermes|Hermes Agent',
    'onnx':       r'onnx|onnxruntime',
    'torch':      r'import torch|PyTorch|torch\.nn',
    'transformers': r'from transformers|AutoModel|AutoTokenizer',
}

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    'web-scraping':    ['爬虫','抓取','scrape','crawl','反爬','动态渲染','爬取','spider'],
    'data-processing': ['数据处理','清洗','ETL','csv','excel','数据分析','合并','数据集'],
    'automation':      ['自动化','脚本','批处理','定时','cron','workflow','流水线'],
    'doc-writing':     ['论文','报告','LaTeX','排版','文档','写作','word','docx','PPT'],
    'math-modeling':   ['数学建模','优化','算法','概率','统计','MATLAB','运筹'],
    'electronics':     ['电路','PCB','信号','示波器','频谱','模电','数电','焊接'],
    'ml':              ['训练','模型','推理','loss','epoch','微调','finetune'],
    'devops':          ['部署','CI/CD','docker','nginx','服务器','运维','监控'],
    'fpga':            ['FPGA','Verilog','VHDL','时序','综合','仿真','约束','约束文件'],
    'web-dev':         ['前端','后端','API','接口','页面','路由','组件','React','Vue'],
    'linux':           ['Linux','shell','bash','系统','内核','权限','systemd'],
    'memory-system':   ['记忆系统','记忆','recall','retain','memory','Hindsight','mmemory'],
    'music-curation':  ['Apple Music','My Playlist','歌单','播放列表','曲目','歌曲核验','playlist'],
}

# 项目/场景词优先于通用词，避免“论文”“数据”“模型”等宽泛词抢占领域。
PRIORITY_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    'exam-learning':      ['考试复习', '错题本', '学习计划', '学习目标'],
    'research':           ['文献综述', '论文调研', '研究计划'],
    'game-development':   ['游戏开发', '游戏设计', '关卡设计', '游戏'],
    'fiction-writing':    ['小说写作', '长篇写作', '章节写作'],
    'education-planning': ['升学规划', '择校', '复习计划'],
    'math-modeling':      ['数学建模竞赛', '数学建模', '建模竞赛'],
}

# 记忆属于哪个项目不是领域标签的同义词。默认只放通用示例，真实项目名应写入
# 本地忽略文件 projects.json，或用 MMEMORY_PROJECTS_FILE 指向另一份配置。
DEFAULT_PROJECT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('mmemory', ('mmemory', 'hindsight', '记忆系统')),
    ('example-service', ('example-service', 'example service')),
    ('example-piano', ('example-piano', 'example piano')),
)

DEFAULT_QUERY_PROJECT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _project_config_path() -> Optional[Path]:
    configured = os.environ.get('MMEMORY_PROJECTS_FILE')
    if configured:
        return Path(configured).expanduser()
    local = Path(__file__).with_name('projects.json')
    return local if local.exists() else None


def _alias_tuple(value, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f'{label} 必须是非空字符串数组')
    aliases = []
    for alias in value:
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError(f'{label} 必须是非空字符串数组')
        aliases.append(unicodedata.normalize('NFC', alias).strip())
    return tuple(aliases)


def load_project_config(path: Optional[Path] = None) -> tuple[
        tuple[tuple[str, tuple[str, ...]], ...],
        tuple[tuple[str, tuple[str, ...]], ...]]:
    """加载项目别名配置，并返回写入与查询两份有序词表。

    配置格式：
    ``{"projects": {"key": ["alias", ...]}}``

    每项也可写成
    ``{"write_aliases": [...], "query_aliases": [...]}``。未提供配置文件时
    使用内置通用示例；配置文件与默认表同名键会覆盖默认值，其他键追加。
    """
    write_order = [key for key, _ in DEFAULT_PROJECT_KEYWORDS]
    write_map = {key: aliases for key, aliases in DEFAULT_PROJECT_KEYWORDS}
    query_order = [key for key, _ in DEFAULT_QUERY_PROJECT_KEYWORDS]
    query_map = {key: aliases for key, aliases in DEFAULT_QUERY_PROJECT_KEYWORDS}

    if path is None:
        path = _project_config_path()
    if path is not None:
        path = Path(path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f'项目配置不存在：{path}')
        data = json.loads(path.read_text(encoding='utf-8'))
        projects = data.get('projects') if isinstance(data, dict) else None
        if not isinstance(projects, dict):
            raise ValueError('项目配置必须包含 projects 对象')
        for project, spec in projects.items():
            if not isinstance(project, str) or not project.strip():
                raise ValueError('项目键必须是非空字符串')
            project = project.strip()
            if isinstance(spec, list):
                write_aliases = _alias_tuple(spec, f'{project}.aliases')
                query_aliases = write_aliases
            elif isinstance(spec, dict):
                write_aliases = _alias_tuple(
                    spec.get('write_aliases'), f'{project}.write_aliases')
                query_aliases = _alias_tuple(
                    spec.get('query_aliases', list(write_aliases)),
                    f'{project}.query_aliases')
            else:
                raise ValueError(f'{project} 的别名配置必须是数组或对象')

            if project not in write_map:
                write_order.append(project)
            write_map[project] = write_aliases
            if project not in query_map:
                query_order.append(project)
            query_map[project] = query_aliases

    _reject_duplicate_aliases(write_map, '写入')
    return (
        tuple((key, write_map[key]) for key in write_order),
        tuple((key, query_map[key]) for key in query_order),
    )


def _reject_duplicate_aliases(project_map: dict[str, tuple[str, ...]],
                              kind: str):
    seen = {}
    for project, aliases in project_map.items():
        for alias in aliases:
            folded = unicodedata.normalize('NFC', alias).casefold()
            owner = seen.get(folded)
            if owner is not None and owner != project:
                raise ValueError(
                    f'{kind}别名 {alias!r} 同时属于 {owner} 和 {project}')
            seen[folded] = project


PROJECT_KEYWORDS, QUERY_PROJECT_KEYWORDS = load_project_config()


def _alias_in_text(alias: str, text: str) -> bool:
    lowered = unicodedata.normalize('NFC', text or '').casefold()
    alias = unicodedata.normalize('NFC', alias).casefold()
    if alias.isascii() and alias.isalnum():
        return re.search(rf'(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])', lowered) is not None
    return alias in lowered


def _keyword_matches(text: str, keywords) -> list[tuple[int, int, str]]:
    normalized = unicodedata.normalize('NFC', text or '').casefold()
    matches = []
    for project, aliases in keywords:
        for alias in aliases:
            needle = unicodedata.normalize('NFC', alias).casefold()
            if needle.isascii() and needle.isalnum():
                pattern = rf'(?<![a-z0-9_]){re.escape(needle)}(?![a-z0-9_])'
                spans = (m.span() for m in re.finditer(pattern, normalized))
            else:
                spans = (m.span() for m in re.finditer(re.escape(needle), normalized))
            for start, end in spans:
                matches.append((start, end, project))
    # 同一段文本同时命中“实验”和“实验三”时，只保留更具体的别名。
    filtered = []
    for start, end, project in matches:
        length = end - start
        contained = any(
            other_start <= start and end <= other_end and other_end - other_start > length
            for other_start, other_end, _ in matches
        )
        if not contained:
            filtered.append((start, end, project))
    order = {project: index for index, (project, _) in enumerate(keywords)}
    return sorted(filtered, key=lambda item: (order.get(item[2], 10**9), item[0], item[1]))


def project_keys_in_text(text: str) -> list[str]:
    """返回文本中所有明确的现有项目键，保持词表顺序并去重。"""
    return list(dict.fromkeys(project for _, _, project
                              in _keyword_matches(text, PROJECT_KEYWORDS)))


def infer_query_project_keys(text: str) -> list[str]:
    """为召回解析查询范围；精确项目优先，未见精确名时才用宽泛主题词。

    这个函数不用于写入归属。查询允许“深度学习”同时搜索实验一和实验二，
    但“使用 MATLAB 训练模型”不会因此改变新记忆的 project_key。
    """
    exact = project_keys_in_text(text)
    if exact:
        return exact
    matched = {project for _, _, project
               in _keyword_matches(text, QUERY_PROJECT_KEYWORDS)}
    order = [project for project, _ in PROJECT_KEYWORDS]
    return [project for project in order if project in matched]


def infer_project_key(text: str) -> str:
    """从高信号项目名推断命名空间；不确定时返回 ``''``。

    多个项目词同时出现时保持配置顺序，由调用方在写入前检查歧义。
    """
    matches = project_keys_in_text(text)
    return matches[0] if matches else ''

ACTION_KEYWORDS: dict[str, list[str]] = {
    'debug':     ['bug','报错','error','失败','异常','排查','调试','出错','崩溃','traceback','修复'],
    'build':     ['搭建','构建','安装','配置','初始化','setup','新建','创建','install','完成','产出','交付'],
    'migrate':   ['迁移','升级','替换','切换','迁到','改成','换用','重构'],
    'deploy':    ['部署','上线','发布','docker','CI/CD','release'],
    'optimize':  ['优化','加速','减少','降低','提升','压缩','改进','性能','精修','重写'],
    'integrate': ['集成','对接','接入','API','联调','整合','桥接'],
    'extract':   ['提取','抽取','解析','导出','转换'],
    'design':    ['设计','方案','架构','规划','选型','评估'],
    'test':      ['测试','验证','检查','校验','benchmark','压测','审查','审阅','复核'],
    'manage':    ['整改','管理','编辑','替换','保留','删除','调整','核验','拉回'],
}


# ── 第一层：固定维度提取 ────────────────────────────────────────

def extract_fixed_tags(text: str) -> dict[str, str]:
    """从文本中提取 tool/domain/action 三个固定维度"""

    # tool
    tool = 'general'
    for name, pat in TOOL_PATTERNS.items():
        if re.search(pat, text, re.IGNORECASE):
            tool = name
            break

    # domain
    domain = 'general'
    lowered = text.lower()
    for d, keywords in PRIORITY_DOMAIN_KEYWORDS.items():
        if any(kw.lower() in lowered for kw in keywords):
            domain = d
            break
    if domain == 'general':
        # 统一小写比较：词表里写的是 MATLAB / Linux / ETL 这类大写词，裸 `in`
        # 会让 "matlab" 这种小写写法漏掉匹配。
        for d, keywords in DOMAIN_KEYWORDS.items():
            if any(kw.lower() in lowered for kw in keywords):
                domain = d
                break

    # action
    action = 'general'
    for a, keywords in ACTION_KEYWORDS.items():
        if any(kw.lower() in lowered for kw in keywords):
            action = a
            break

    return {'tool': tool, 'domain': domain, 'action': action}


# ── 第二层：动态关键词 ──────────────────────────────────────────

def extract_keywords(text: str, topk: int = 5) -> list[str]:
    """jieba TF-IDF 提取关键词"""
    try:
        import jieba.analyse
        return jieba.analyse.extract_tags(
            text, topK=topk,
            allowPOS=('n', 'nr', 'ns', 'nt', 'nz', 'vn', 'eng')
        )
    except ImportError:
        tokens = fallback_tokenize(text)
        tokens.sort(key=len, reverse=True)
        return tokens[:topk]


# ── 公共分词回退（jieba 不可用时） ──────────────────────────────

def fallback_tokenize(text: str) -> list[str]:
    """jieba 不可用时的回退分词：按 Unicode 范围切分中文/英文词"""
    return re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z][a-zA-Z0-9_]+', text)


DEFAULT_TERM_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    'k8s': ('kubernetes', 'kube'),
    'kubernetes': ('k8s', 'kube'),
    'db': ('database', '数据库'),
    'database': ('db', '数据库'),
    '数据库': ('db', 'database'),
    'config': ('configuration', '配置'),
    'configuration': ('config', '配置'),
    '配置': ('config', 'configuration'),
    'repo': ('repository', '代码库'),
    'repository': ('repo', '代码库'),
    '代码库': ('repo', 'repository'),
    'vm': ('virtual machine', '虚拟机'),
    'virtual machine': ('vm', '虚拟机'),
    '虚拟机': ('vm', 'virtual machine'),
    'llm': ('large language model', '大语言模型'),
    'large language model': ('llm', '大语言模型'),
    '大语言模型': ('llm', 'large language model'),
    'ai': ('artificial intelligence', '人工智能'),
    'artificial intelligence': ('ai', '人工智能'),
    '人工智能': ('ai', 'artificial intelligence'),
    'gpu': ('graphics processing unit', '显卡'),
    'graphics processing unit': ('gpu', '显卡'),
    'cpu': ('central processing unit', '处理器'),
    'central processing unit': ('cpu', '处理器'),
    'api': ('application programming interface', '接口'),
    'application programming interface': ('api', '接口'),
    'cli': ('command line interface', '命令行'),
    'command line interface': ('cli', '命令行'),
    '命令行': ('cli', 'command line interface'),
    'ci': ('continuous integration', '持续集成'),
    'continuous integration': ('ci', '持续集成'),
    '持续集成': ('ci', 'continuous integration'),
    'auth': ('authentication', 'authorization', '认证', '授权'),
    'authentication': ('auth', '认证'),
    'authorization': ('auth', '授权'),
    '认证': ('auth', 'authentication'),
    '授权': ('auth', 'authorization'),
    '重启': ('重啟', '重新启动', '重新啟動'),
    '重啟': ('重启', '重新启动', '重新啟動'),
    '服务器': ('服務器',),
    '服務器': ('服务器',),
    '网络': ('網絡',),
    '網絡': ('网络',),
    '记忆': ('記憶',),
    '記憶': ('记忆',),
    '系统': ('系統',),
    '系統': ('系统',),
    '项目': ('項目',),
    '項目': ('项目',),
    '测试': ('測試',),
    '測試': ('测试',),
    '失败': ('失敗', '报错', '報錯'),
    '失敗': ('失败', '报错', '報錯'),
    '修复': ('修復',),
    '修復': ('修复',),
    '验证': ('驗證',),
    '驗證': ('验证',),
    '数据库': ('數據庫', '資料庫'),
    '數據庫': ('数据库',),
    '資料庫': ('数据库',),
}


def _load_term_equivalents() -> dict[str, tuple[str, ...]]:
    table = {
        term.casefold(): list(values)
        for term, values in DEFAULT_TERM_EQUIVALENTS.items()
    }
    configured = os.environ.get('MMEMORY_SYNONYMS_FILE')
    path = (Path(configured).expanduser() if configured
            else Path(__file__).with_name('synonyms.json'))
    if configured and not path.exists():
        raise FileNotFoundError(f'同义词配置不存在：{path}')
    if not path.exists():
        return {term: tuple(dict.fromkeys(values))
                for term, values in table.items()}
    data = json.loads(path.read_text(encoding='utf-8'))
    groups = data.get('equivalents') if isinstance(data, dict) else data
    if not isinstance(groups, list):
        raise ValueError('同义词配置必须是数组或包含 equivalents 数组的对象')
    for group in groups:
        if not isinstance(group, list) or len(group) < 2 or any(
                not isinstance(term, str) or not term.strip() for term in group):
            raise ValueError('每个同义词组至少包含两个非空字符串')
        normalized = [unicodedata.normalize('NFC', term).strip() for term in group]
        for term in normalized:
            key = term.casefold()
            table.setdefault(key, [])
            table[key].extend(other for other in normalized if other != term)
    return {
        term: tuple(dict.fromkeys(values))
        for term, values in table.items()
    }


TERM_EQUIVALENTS = _load_term_equivalents()


_CN_DIGITS = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3,
              '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}


def _parse_small_chinese_number(text: str) -> Optional[int]:
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if '百' in text:
        left, _, right = text.partition('百')
        hundreds = _CN_DIGITS.get(left, 1) if left else 1
        rest = _parse_small_chinese_number(right) if right else 0
        return hundreds * 100 + (rest or 0) if rest is not None else None
    if '十' in text:
        left, _, right = text.partition('十')
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    return None


def _format_small_chinese_number(value: int) -> str:
    digits = '零一二三四五六七八九'
    if value < 10:
        return digits[value]
    if value < 20:
        return '十' + (digits[value % 10] if value % 10 else '')
    if value < 100:
        return digits[value // 10] + '十' + (
            digits[value % 10] if value % 10 else '')
    if value < 1000:
        rest = value % 100
        head = digits[value // 100] + '百'
        if rest == 0:
            return head
        if rest < 10:
            return head + '零' + digits[rest]
        return head + _format_small_chinese_number(rest)
    return str(value)


def _numeric_equivalents(term: str) -> tuple[str, ...]:
    match = re.fullmatch(r'第(\d+)([题卷章节])', term)
    if match:
        value = int(match.group(1))
        return (f'第{_format_small_chinese_number(value)}{match.group(2)}',)
    match = re.fullmatch(r'第([零〇一二两三四五六七八九十百]+)([题卷章节])', term)
    if match:
        value = _parse_small_chinese_number(match.group(1))
        if value is not None:
            return (f'第{value}{match.group(2)}',)
    return ()


def search_term_equivalents(term: str) -> tuple[str, ...]:
    """返回查询词的可选等价写法；原词由调用方保留。"""
    folded = (term or '').casefold()
    values = list(TERM_EQUIVALENTS.get(folded, ()))
    synonym_map = load_synonym_map()
    canonical = synonym_map.get(folded)
    if canonical:
        values.extend(key for key, value in synonym_map.items()
                      if value == canonical and key != folded)
    values.extend(_numeric_equivalents(term))
    return tuple(dict.fromkeys(values))


def build_search_text(*parts: str) -> str:
    """把五字段和标签转换成 FTS5 可直接索引的分词文本。

    默认 unicode61 不会切分连续中文；这里先用 jieba 切词并用空格连接。
    只保留去重后的稳定 token，避免正文与搜索索引双份无序膨胀。
    """
    text = unicodedata.normalize('NFC', ' '.join(str(part or '') for part in parts))
    if not text.strip():
        return ''
    try:
        import jieba
        tokens = []
        for chunk in re.findall(r'[\u4e00-\u9fff]+|[^\s\u4e00-\u9fff]+', text):
            if re.search(r'[\u4e00-\u9fff]', chunk):
                tokens.extend(jieba.cut_for_search(chunk))
            else:
                tokens.extend(re.findall(r'[a-zA-Z0-9_+#.-]+', chunk))
    except ImportError:
        tokens = fallback_tokenize(text)
    cleaned = []
    seen = set()
    for token in tokens:
        token = (token or '').strip().casefold()
        if not token or token in seen:
            continue
        if re.fullmatch(r'[a-z0-9_+#.-]+', token) or len(token) > 1:
            seen.add(token)
            cleaned.append(token)
    return ' '.join(cleaned)


# ── 第三层：同义词归一化 ─────────────────────────────────────────

def load_synonym_map() -> dict[str, str]:
    """返回内置同义词映射。

    以前这里还会读数据库的 synonyms 表，但 Dream 补全出的词表全是
    「83→testing」「锁→devops」这类噪声，且唯一调用方从没传连接进来，
    所以整条链路是死的。现在只保留这份人工维护的内置表。
    """
    return {
        '爬虫': 'scraping', '爬取': 'scraping', '抓取': 'scraping',
        'selenium': 'selenium',
        '部署': 'deploy', '上线': 'deploy', '发布': 'deploy',
        '报错': 'error', '异常': 'error', '失败': 'error', '出错': 'error',
        '安装': 'install', '配置': 'config',
        '优化': 'optimize', '加速': 'optimize', '提升': 'optimize',
        '迁移': 'migrate', '升级': 'migrate', '替换': 'migrate',
        '论文': 'doc', '报告': 'doc', '文档': 'doc',
        '训练': 'training', '推理': 'inference', '微调': 'finetune',
    }


def normalize_keywords(keywords: list[str], synonym_map: Optional[dict] = None) -> set[str]:
    """归一化：同义词映射 + 小写化"""
    if synonym_map is None:
        synonym_map = load_synonym_map()
    return {synonym_map.get(kw, kw.lower()) for kw in keywords}


# ── 组装 ────────────────────────────────────────────────────────

def generate_tags(goal: str, approach: str, outcome: str,
                  success_key: str = '', pitfall: str = '') -> dict[str, str]:
    """五字段 Tag 生成；兼容原有三参数调用。"""
    full_text = f"{goal} {approach} {outcome} {success_key} {pitfall}"
    fixed = extract_fixed_tags(full_text)
    keywords = extract_keywords(full_text)

    return {
        'tags_tool': fixed['tool'],
        'tags_domain': fixed['domain'],
        'tags_action': fixed['action'],
        'tags_keywords': ','.join(keywords),  # 确保返回 str，不是 list
    }


def extract_tags_from_message(message: str, db_conn=None) -> dict[str, str]:
    """从用户消息（非结构化文本）提取 Tag，用于召回时的查询"""
    fixed = extract_fixed_tags(message)
    keywords = extract_keywords(message, topk=3)
    return {
        'tags_tool': fixed['tool'],
        'tags_domain': fixed['domain'],
        'tags_action': fixed['action'],
        'tags_keywords': ','.join(keywords),  # 修复：返回 str 而非 list
        'project_key': infer_project_key(message),
    }
