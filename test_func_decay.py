#!/usr/bin/env python3
"""Test: Weight Decay"""
import sys, os, tempfile, math; sys.path.insert(0, '.')
import engine, pathlib

print('=== DECAY TESTS ===')

# Use temp DB
tmp = tempfile.mktemp(suffix='.db')
engine.DB_PATH = pathlib.Path(tmp)
engine._schema_initialized.discard(tmp)

conn = engine.get_conn()

# Insert test memories with different use_counts and ages
from datetime import datetime, timedelta
now = datetime.now()

test_data = [
    # (goal, use_count, days_ago, initial_weight)
    ('高使用率近期记忆', 5, 1, 1.5),
    ('低使用率近期记忆', 1, 1, 1.0),
    ('高使用率久远记忆', 5, 30, 1.5),
    ('低使用率久远记忆', 1, 30, 1.0),   # 30 天：0.407，明显衰减但仍高于活跃阈值
    ('极低权重应归档', 0, 60, 0.3),      # 0.3*exp(-0.03*60)=0.05 < 0.2 → 归档
]

for goal, uc, days, w in test_data:
    last_used = (now - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("""
        INSERT INTO memories (goal, approach, outcome, tags_tool, tags_domain, tags_action, tags_keywords,
                              weight, use_count, last_used, decay_cursor_at)
        VALUES (?, '', '', 'general', 'general', 'general', '', ?, ?, ?, ?)
    """, (goal, w, uc, last_used, last_used))
conn.commit()

# TC-D1: Run decay
result = engine.decay_all(db_conn=conn)
print(f'D1 decay result: {result}')

# TC-D2: High-use recent should barely decay
row = conn.execute("SELECT weight FROM memories WHERE goal='高使用率近期记忆'").fetchone()
print(f'D2 high-use recent: weight={row["weight"]:.4f} (rate=0.01, ~1 day)')
assert abs(row['weight'] - 1.5 * math.exp(-0.01)) < 0.01
print('   PASS')

# TC-D3: Low-use recent should decay more
row = conn.execute("SELECT weight FROM memories WHERE goal='低使用率近期记忆'").fetchone()
print(f'D3 low-use recent: weight={row["weight"]:.4f} (rate=0.03, ~1 day)')
assert abs(row['weight'] - math.exp(-0.03)) < 0.01
print('   PASS')

# TC-D4: Low-use old should decay significantly
row = conn.execute("SELECT weight FROM memories WHERE goal='低使用率久远记忆'").fetchone()
expected_d4 = 1.0 * math.exp(-0.03 * 30)
print(f'D4 low-use old: weight={row["weight"]:.4f} (rate=0.03, ~30 days)')
print(f'   expected ~{expected_d4:.4f}')
print(f'   {"PASS" if abs(row["weight"] - expected_d4) < 0.05 else "FAIL"}')

# TC-D5: Below the active threshold the original row remains recoverable
row = conn.execute("SELECT id,status FROM memories WHERE goal='极低权重应归档'").fetchone()
print(f'D5 expired row: {"present, status=" + row["status"] if row else "deleted"}')
assert row is not None and row['status'] == 'archived'
print('   PASS')

# TC-D6: Verify decay rates are Ebbinghaus-like
# Higher use_count → slower decay
row5 = conn.execute("SELECT weight FROM memories WHERE goal='高使用率久远记忆'").fetchone()
row1 = conn.execute("SELECT weight FROM memories WHERE goal='低使用率久远记忆'").fetchone()
print(f'D6 decay ordering: high-use={row5["weight"]:.4f} vs low-use={row1["weight"]:.4f}')
# Both started at different weights so compare ratios
# high-use: 1.5*exp(-0.01*30) vs low-use: 1.0*exp(-0.03*30)
ratio_high = row5["weight"] / 1.5
ratio_low = row1["weight"] / 1.0
print(f'   high-use ratio: {ratio_high:.4f}, low-use ratio: {ratio_low:.4f}')
print(f'   {"PASS" if ratio_high > ratio_low else "FAIL: high-use should decay slower"}')

conn.close()
os.unlink(tmp)
