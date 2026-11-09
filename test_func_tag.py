#!/usr/bin/env python3
"""Test: Tag Generation"""
import sys; sys.path.insert(0, '.')
import tagger

print('=== TAG GENERATION ===')

# TC-T1: English text with known tool
r = tagger.extract_fixed_tags('Using selenium to scrape web pages')
print(f'T1 tool=selenium: tool={r["tool"]} domain={r["domain"]} action={r["action"]}')
print(f'   PASS' if r['tool'] == 'selenium' else f'   FAIL: tool={r["tool"]}')

# TC-T2: Chinese text with tool + domain + action
r = tagger.extract_fixed_tags('用 playwright 爬取动态渲染的网页数据，解决反爬问题')
print(f'T2 Chinese+tool: tool={r["tool"]} domain={r["domain"]} action={r["action"]}')
ok = r['tool'] == 'playwright' and r['domain'] == 'web-scraping'
print(f'   {"PASS" if ok else "FAIL"}')

# TC-T3: No known tool/domain → all general
r = tagger.extract_fixed_tags('今天天气不错，适合出去玩')
print(f'T3 all-general: tool={r["tool"]} domain={r["domain"]} action={r["action"]}')
ok = all(v == 'general' for v in r.values())
print(f'   {"PASS" if ok else "FAIL"}')

# TC-T4: Multiple tools (first regex match wins)
r = tagger.extract_fixed_tags('从 selenium 迁移到 playwright，用 pandas 处理数据')
print(f'T4 multi-tool first-wins: tool={r["tool"]}')
print(f'   NOTE: first match={r["tool"]} (dict order dependent)')

# TC-T5: Keyword extraction (jieba)
kw = tagger.extract_keywords('用 jieba 分词提取关键词进行文本分析')
print(f'T5 keywords: {kw}')
print(f'   {"PASS" if len(kw) > 0 else "FAIL: no keywords"}')

# TC-T6: Synonym normalization
norm = tagger.normalize_keywords(['爬虫','报错','部署','优化'])
print(f'T6 synonyms: {norm}')
ok = 'scraping' in norm and 'error' in norm and 'deploy' in norm and 'optimize' in norm
print(f'   {"PASS" if ok else "FAIL"}')

# TC-T7: extract_tags_from_message (for recall)
r = tagger.extract_tags_from_message('帮我用 flask 部署一个 API 接口')
print(f'T7 msg tags: tool={r["tags_tool"]} domain={r["tags_domain"]} action={r["tags_action"]}')
print(f'   tags_keywords type={type(r["tags_keywords"]).__name__}')
print(f'   {"PASS" if isinstance(r["tags_keywords"], str) else "FAIL: keywords not str"}')
