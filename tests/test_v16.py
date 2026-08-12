"""v0.16 测试：现金生息（货基假设）——闲置现金每日按 rate/252 计息，做全模拟"""
from datetime import datetime, timedelta

import pytest

from aitrader.batch import BatchRunner
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.datasource import FakeDataSource
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState, Position
from aitrader.portfolio import apply_cash_interest

D = datetime(2024, 1, 1)


def _settings(db_path, rate=0.017):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
        cash_interest_rate=rate,
    )


# ---------- 纯函数 ----------
def test_apply_cash_interest_increases_cash():
    st = apply_cash_interest(AccountState(100_000, 100_000), 0.017 / 252)
    assert st.cash == pytest.approx(100_000 + 100_000 * 0.017 / 252, abs=0.01)


def test_apply_cash_interest_zero_rate_no_change():
    st = apply_cash_interest(AccountState(100_000, 100_000), 0.0)
    assert st.cash == 100_000


def test_apply_cash_interest_no_cash_no_change():
    st = apply_cash_interest(
        AccountState(
            100_000, 0, {"510300": Position("510300", "沪深300ETF", 1000, 4.0, 4.0)}
        ),
        0.017 / 252,
    )
    assert st.cash == 0
    assert st.positions  # 持仓不受影响


# ---------- 批处理：空仓账户连续跑几天现金累积利息 ----------
def test_batch_accumulates_interest_over_days(tmp_path):
    days = {"2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"}
    db = Database(tmp_path / "t.db")
    # 恒定价格：rule 双均线不触发（快线==慢线）→ 一直空仓
    ds = FakeDataSource([1.0] * 40, base_date=D, trading_days=days)
    runner = BatchRunner(_settings(tmp_path / "t.db", rate=0.017), db, ds, {"rule": RuleEngine()})
    for d in [datetime(2024, 1, 2), datetime(2024, 1, 3), datetime(2024, 1, 4),
              datetime(2024, 1, 5), datetime(2024, 1, 8)]:
        runner.run(d)
    acc = db.get_account_by_engine("rule")
    snap = db.get_snapshots(acc["id"])[-1]
    # 5 个交易日空仓 → 现金应 > 10 万（累计货基利息）
    assert snap["cash"] > 100_000
    expected = 100_000 * (1 + 0.017 / 252) ** 5
    assert snap["cash"] == pytest.approx(expected, rel=0.01)


# ---------- 回测：空仓区间净值含利息增长 ----------
def test_backtest_interest_grows_idle_assets(tmp_path):
    from aitrader.backtest import Backtester

    db = Database(tmp_path / "t.db")
    ds = FakeDataSource([1.0] * 40, base_date=D)
    bt = Backtester(
        _settings(tmp_path / "t.db", rate=0.017), db, ds, RuleEngine(), "rule",
        datetime(2024, 1, 2), datetime(2024, 1, 20),
        fill_mode="close", adjust="none",
    )
    r = bt.run()
    snaps = r["snapshots"]
    assert len(snaps) >= 5
    # 空仓（恒定价不触发）→ 总资产随时间缓慢增长（利息）
    assert snaps[-1]["total_assets"] > snaps[0]["total_assets"]
    assert r["metrics"].total_return > 0
