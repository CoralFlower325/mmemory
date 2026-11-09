#!/usr/bin/env python3
"""
obsidian_export.py — mmemory → Obsidian 单向导出
每条记忆生成一个 markdown 文件，按 domain 分子目录。
"""

import os
import sys
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent))
import engine

# 可通过环境变量覆盖路径
OBSIDIAN_DIR = Path(os.environ.get(
    "MMEMORY_OBSIDIAN_DIR",
    Path(__file__).resolve().parent / "obsidian-export",
))


def _is_mmemory_file(filepath: Path) -> bool:
    """只把包含完整生成页脚的 Markdown 视为 mmemory 生成物。"""
    try:
        content = filepath.read_text(encoding='utf-8')
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            return False
        matches = [re.fullmatch(
            r'\*mmemory ID: \d+ \| Status: (?:active|archived)\*',
            line) for line in lines[-3:]]
        return any(matches)
    except Exception:
        return False


def sanitize_filename(text: str, max_len: int = 60) -> str:
    """生成安全的文件名"""
    text = re.sub(r'[\\/:*?"<>|\n\r]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text or "untitled"


def filename_for(m: dict) -> str:
    """一条记忆对应一个稳定且唯一的文件名。

    带上记忆 id：goal 截断到 60 字符后，两条只在 60 字符之后才有区别的记忆会
    算出同名文件，后者会把前者的文件覆盖掉（两条只落下一个 md），index.md 里
    却仍写两条链接形成死链。id 保证唯一，也让「同一条记忆」的文件名稳定不变，
    增量更新才真正成立。
    """
    return f"{sanitize_filename(m.get('goal') or '')}-{m['id']}.md"


def memory_to_markdown(m: dict) -> str:
    """将一条记忆转为 markdown"""
    lines = [
        f"# {(m.get('goal') or '')[:80]}",
        "",
        f"- **领域**: {m.get('tags_domain') or 'general'}",
        f"- **工具**: {m.get('tags_tool') or 'general'}",
        f"- **操作**: {m.get('tags_action') or 'general'}",
        f"- **关键词**: {m.get('tags_keywords') or ''}",
        f"- **权重**: {(m.get('weight') if m.get('weight') is not None else 1.0):.2f}"
        f"  |  **使用次数**: {m.get('use_count') or 0}",
        f"- **创建**: {m.get('created_at') or ''}  |  **最后使用**: {m.get('last_used') or ''}",
        "",
        "## 目标",
        m.get('goal') or '',
        "",
        "## 方案",
        m.get('approach') or '',
        "",
        "## 结果",
        m.get('outcome') or '',
    ]

    if m.get('success_key'):
        lines.extend(["", "## 成功关键", m['success_key']])
    if m.get('pitfall'):
        lines.extend(["", "## 踩坑+解法", m['pitfall']])

    lines.extend([
        "",
        "---",
        f"*mmemory ID: {m['id']} | Status: {m.get('status') or ''}*",
    ])

    return '\n'.join(lines)


def export_all():
    """导出所有 active 记忆到 Obsidian"""
    OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)

    conn = engine.get_conn()
    try:
        # limit=None 表示不加限制：绝不能传一个大数字（曾被夹到 500 而静默漏导），
        # 导出必须覆盖全部记忆，否则超出的部分不仅不写文件，还会被清理阶段当成
        # 「已删除」而删掉既有 md。
        memories = engine.list_all(status='active', limit=None, db_conn=conn)
    finally:
        conn.close()

    # 空库不能直接 return：否则会跳过下面的清理阶段与 index 重写，库里记忆被
    # 清空后旧的 md 与失效的 index 链接会一直残留。这里让空库也走完整流程
    # （expected_files 为空 ⇒ 清理阶段会删掉全部 mmemory 生成的 md）。
    # 按 domain 分组
    groups: dict[str, list] = {}
    for m in memories:
        # `or 'general'` 而非 get 默认值：键存在但值为 NULL 时 get 返回 None，
        # 会让 OBSIDIAN_DIR / None 抛 TypeError 并中断整个导出。
        # 还要经过 sanitize_filename：domain 里若含 `/` 会抛 FileNotFoundError、
        # 含 `..` 或绝对路径会**在 vault 之外建目录写文件**（脏数据可来自外部迁移）。
        domain = sanitize_filename(m.get('tags_domain') or 'general')
        if domain in ('', '.', '..'):
            domain = 'general'
        groups.setdefault(domain, []).append(m)

    # 增量更新：收集当前应存在的文件路径
    expected_files: set[Path] = set()

    updated = 0
    created = 0
    for domain, mems in sorted(groups.items()):
        domain_dir = OBSIDIAN_DIR / domain
        domain_dir.mkdir(exist_ok=True)

        for m in mems:
            filepath = domain_dir / filename_for(m)
            new_content = memory_to_markdown(m)

            if filepath.exists():
                old_content = filepath.read_text(encoding='utf-8')
                if old_content != new_content:
                    filepath.write_text(new_content, encoding='utf-8')
                    updated += 1
                # else: 内容未变，跳过
            else:
                filepath.write_text(new_content, encoding='utf-8')
                created += 1

            expected_files.add(filepath)

    # 清理已删除记忆对应的文件
    removed = 0
    for domain_dir in OBSIDIAN_DIR.iterdir():
        if domain_dir.is_dir():
            for f in domain_dir.glob("*.md"):
                # 只删除含 mmemory 标记的文件，保护用户手建文件
                if f not in expected_files and _is_mmemory_file(f):
                    f.unlink()
                    removed += 1

    # 清理空目录
    for domain_dir in list(OBSIDIAN_DIR.iterdir()):
        if domain_dir.is_dir() and not any(domain_dir.iterdir()):
            domain_dir.rmdir()

    # 生成索引文件
    index_lines = [
        "# mmemory 记忆索引",
        "",
        f"*导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}*  ",
        f"*总记忆数: {len(memories)}*",
        "",
    ]
    for domain, mems in sorted(groups.items()):
        index_lines.append(f"## {domain} ({len(mems)} 条)")
        index_lines.append("")
        for m in mems:
            fname = filename_for(m)
            # #、方括号等在正常技术目标中常见；Wikilink 会把 # 当标题分隔。
            # 文件命名保持原样，用编码后的 Markdown 相对路径链接到真实文件。
            relative_path = quote((Path(domain) / fname).as_posix(), safe='/')
            label = re.sub(r'\s+', ' ', (m.get('goal') or '')[:60])
            label = label.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
            index_lines.append(
                f"- [{label}]({relative_path})")
        index_lines.append("")

    (OBSIDIAN_DIR / "index.md").write_text('\n'.join(index_lines), encoding='utf-8')

    print(f"导出完成: 新建 {created} / 更新 {updated} / 删除 {removed} → {len(groups)} 个领域目录")
    for domain, mems in sorted(groups.items()):
        print(f"  {domain}: {len(mems)} 条")


if __name__ == "__main__":
    export_all()
