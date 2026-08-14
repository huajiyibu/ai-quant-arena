"""v0.23 第一批修复（15:30 前必须）：
1. run.py --help 崩溃：help 字符串裸 % -> %%
2. database.py: decisions 唯一约束 + add_decision INSERT OR IGNORE（防重复决策留痕）
3. database.py: reset_account 补清 batch_runs
4. batch.py: 计息 marker 后移到 save_state 之后（防漏计窗口）
"""
from pathlib import Path

# ---- run.py ----
p = Path("run.py")
src = p.read_text(encoding="utf-8")
repls = [
    (
        'help="止损阈值（0~0.5，如 0.08=跌8%强制卖出；默认0关，PP-5）",',
        'help="止损阈值（0~0.5，如 0.08=跌8%%强制卖出；默认0关，PP-5）",',
    ),
    (
        'help="止盈阈值（0~1.0，如 0.2=涨20%强制卖出；默认0关，PP-5）",',
        'help="止盈阈值（0~1.0，如 0.2=涨20%%强制卖出；默认0关，PP-5）",',
    ),
]
for old, new in repls:
    n = src.count(old)
    assert n == 1, f"run.py: expected 1 occurrence, got {n}: {old[:40]}"
    src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("run.py: help % -> %% done")

# ---- database.py ----
p = Path("aitrader/database.py")
src = p.read_text(encoding="utf-8")
repls = [
    # 2a. decisions 唯一约束迁移（紧跟 trades 迁移块）
    (
        '''            # P0-1: trades 唯一约束（先清理重复保留最早，再建唯一索引，防 --force/崩溃重跑重复成交）
            conn.execute(
                "DELETE FROM trades WHERE id NOT IN "
                "(SELECT MIN(id) FROM trades GROUP BY account_id, date, symbol, action)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique "
                "ON trades(account_id, date, symbol, action)"
            )
''',
        '''            # P0-1: trades 唯一约束（先清理重复保留最早，再建唯一索引，防 --force/崩溃重跑重复成交）
            conn.execute(
                "DELETE FROM trades WHERE id NOT IN "
                "(SELECT MIN(id) FROM trades GROUP BY account_id, date, symbol, action)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique "
                "ON trades(account_id, date, symbol, action)"
            )
            # 体检P0: decisions 唯一约束（防 --force/崩溃重跑重复决策留痕，污染 Rank IC/归因）
            conn.execute(
                "DELETE FROM decisions WHERE id NOT IN "
                "(SELECT MIN(id) FROM decisions GROUP BY account_id, date, symbol, action)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_unique "
                "ON decisions(account_id, date, symbol, action)"
            )
''',
    ),
    # 2b. add_decision INSERT -> INSERT OR IGNORE
    (
        '''            conn.execute(
                """INSERT INTO decisions(account_id, date, engine_type, symbol, action, amount, confidence, reason,''',
        '''            conn.execute(
                """INSERT OR IGNORE INTO decisions(account_id, date, engine_type, symbol, action, amount, confidence, reason,''',
    ),
    # 3. reset_account 补清 batch_runs
    (
        '''        with self._connect() as conn:
            conn.execute("DELETE FROM trades WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM decisions WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM daily_snapshots WHERE account_id=?", (account_id,))
        self.save_state(
''',
        '''        with self._connect() as conn:
            conn.execute("DELETE FROM trades WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM decisions WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM daily_snapshots WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM batch_runs WHERE account_id=?", (account_id,))
        self.save_state(
''',
    ),
]
for old, new in repls:
    n = src.count(old)
    assert n == 1, f"database.py: expected 1 occurrence, got {n}: {old[:50]}"
    src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("database.py: decisions unique + reset_account batch_runs done")

# ---- batch.py：计息 marker 后移 ----
p = Path("aitrader/batch.py")
src = p.read_text(encoding="utf-8")
repls = [
    (
        '''        if last_int is None or last_int < today_str:
            daily_rate = self.settings.cash_interest_rate / 252
            interest_today = round(state.cash * daily_rate, 2)
            state = apply_cash_interest(state, daily_rate)
            self.db.set_last_interest_date(account_id, today_str)
''',
        '''        if last_int is None or last_int < today_str:
            daily_rate = self.settings.cash_interest_rate / 252
            interest_today = round(state.cash * daily_rate, 2)
            state = apply_cash_interest(state, daily_rate)
            # 计息 marker 后移到 save_state 之后（防"marker 已写但状态未入账"的漏计窗口）
''',
    ),
    (
        '''        self.db.save_state(account_id, new_state)
        source = "real" if date.date() == datetime.now().date() else "replay"
''',
        '''        self.db.save_state(account_id, new_state)
        # P0-1：计息标记与状态同点落库（漏计窗口修复：marker 不再早于状态提交）
        if last_int is None or last_int < today_str:
            self.db.set_last_interest_date(account_id, today_str)
        source = "real" if date.date() == datetime.now().date() else "replay"
''',
    ),
]
for old, new in repls:
    n = src.count(old)
    assert n == 1, f"batch.py: expected 1 occurrence, got {n}: {old[:50]}"
    src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("batch.py: 计息 marker 后移 done")
print("ALL OK")
