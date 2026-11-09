#!/usr/bin/env python3
"""Test: Obsidian Export"""
import sys, os, tempfile; sys.path.insert(0, '.')
import engine, obsidian_export, pathlib

print('=== OBSIDIAN EXPORT TESTS ===')

# Use temp dir for export
tmpdir = tempfile.mkdtemp(prefix='obsidian_test_')
_orig_export_dir = obsidian_export.OBSIDIAN_DIR
obsidian_export.OBSIDIAN_DIR = pathlib.Path(tmpdir)

# 使用独立合成数据库
import shutil
_db_copy = pathlib.Path(tempfile.mkdtemp()) / 'obsidian_test.db'
_orig_db_path = engine.DB_PATH
engine.DB_PATH = _db_copy
engine._schema_initialized.discard(str(_db_copy))

engine.store('导出合成测试目标', '运行导出', '生成笔记')

# TC-O1: sanitize_filename
fn = obsidian_export.sanitize_filename('用 selenium 爬取网页:数据处理/分析')
print(f'O1 sanitize: "{fn}"')
assert '/' not in fn and ':' not in fn and '\\\\' not in fn
print(f'   PASS')

# TC-O2: sanitize long filename
long = '这是一个非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常长的文件名用于测试截断'
fn2 = obsidian_export.sanitize_filename(long)
print(f'O2 long sanitize: {len(fn2)} chars (max=60)')
print(f'   {"PASS" if len(fn2) <= 60 else "FAIL"}')

# TC-O3: sanitize empty
fn3 = obsidian_export.sanitize_filename('')
print(f'O3 empty: "{fn3}"')
print(f'   {"PASS" if fn3 == "untitled" else "FAIL"}')

# TC-O4: memory_to_markdown
sample_mem = {
    'id': 1, 'goal': '测试目标', 'approach': '测试方案', 'outcome': '测试结果',
    'success_key': '关键点', 'pitfall': '踩坑记录',
    'tags_domain': 'web-dev', 'tags_tool': 'flask', 'tags_action': 'build',
    'tags_keywords': 'flask,api,部署', 'weight': 1.23, 'use_count': 3,
    'created_at': '2025-01-01 10:00:00', 'last_used': '2025-01-02 10:00:00',
    'status': 'active',
}
md = obsidian_export.memory_to_markdown(sample_mem)
print(f'O4 markdown: {len(md)} chars')
print(f'   has heading: {"# 测试目标" in md}')
print(f'   has success_key: {"关键点" in md}')
print(f'   has pitfall: {"踩坑记录" in md}')
print(f'   {"PASS" if all([x in md for x in ["# 测试目标", "关键点", "踩坑记录", "flask"]]) else "FAIL"}')

# TC-O5: export_all
try:
    obsidian_export.export_all()
    # Check output files exist
    index = pathlib.Path(tmpdir) / 'index.md'
    print(f'O5 export: index.md exists={index.exists()}')
    if index.exists():
        content = index.read_text()
        print(f'   index content length: {len(content)}')
        print(f'   has links: {"[[" in content}')

    # Count domain dirs and md files
    md_files = list(pathlib.Path(tmpdir).rglob('*.md'))
    print(f'   total .md files: {len(md_files)}')
    print(f'   {"PASS" if len(md_files) > 0 else "FAIL: no files exported"}')
except Exception as e:
    print(f'O5 export ERROR: {e}')

# Cleanup
import shutil
shutil.rmtree(tmpdir, ignore_errors=True)

engine.DB_PATH = _orig_db_path
obsidian_export.OBSIDIAN_DIR = _orig_export_dir
shutil.rmtree(_db_copy.parent)
