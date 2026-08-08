"""v0.4 改进测试：逐标的陈旧剔除、日历保守跳过、写账标记、执行结果回填、日期校验"""
from datetime import datetime

import pytest

from aitrader.batch import BatchRunner
from aitrader.config import RiskConfig, Settings
from aitrader.database import Database
from aitrader.datasource import FakeDataSource
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState, Decision
from aitrader.portfolio import execute_decisions
from run import _date_type

D = datetime(2024, 1, 1)


def _settings(db_path, symbols=None):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols=symbols or {"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# P0-3 单标的陈旧被剔除，其余正常交易（不整日跳过）
# ---------------------------------------------------------------------------
def test_single_stale_symbol_excluded_not_whole_day(tmp_path):
    class MixedDataSource:
        name = "mixed"
        calendar_ok = True

        def __init__(self):
            self._inner = FakeDataSource([1.0] * 25)

        def fetch_daily_bars(self, symbol, days, exchange="SH", end_date=None):
            if symbol == "510300":
                return self._inner.fetch_daily_bars(symbol, days, exchange, end_date)
            return self._inner.fetch_daily_bars(symbol, days, exchange, end_date=None)

        def is_trading_day(self, date):
            return True

    db = Database(tmp_path / "t.db")
    settings = _settings(
        tmp_path / "t.db",
        {"510300": {"name": "x", "exchange": "SH"}, "588000": {"name": "y", "exchange": "SH"}},
    )
    runner = BatchRunner(settings, db, MixedDataSource(), {"rule": RuleEngine()})
    results = runner.run(datetime(2024, 3, 1))  # 588000 陈旧(35天)，510300 新鲜
    assert "_warning" not in results  # 部分陈旧 → 不整日跳过
    assert "rule" in results
    acc = db.get_account_by_engine("rule")
    assert db.get_snapshot(acc["id"], datetime(2024, 3, 1)) is not None


# ---------------------------------------------------------------------------
# P0-4 交易日历不可用 + 工作日 → 保守跳过
# ---------------------------------------------------------------------------
def test_calendar_unavailable_skips_weekday(tmp_path):
    class NoCalendarDataSource:
        name = "nocal"
        calendar_ok = False

        def __init__(self):
            self._inner = FakeDataSource([1.0] * 25)

        def fetch_daily_bars(self, symbol, days, exchange="SH", end_date=None):
            return self._inner.fetch_daily_bars(symbol, days, exchange, end_date)

        def is_trading_day(self, date):
            return True  # 工作日（可能是节假日）

    db = Database(tmp_path / "t.db")
    runner = BatchRunner(_settings(tmp_path / "t.db"), db, NoCalendarDataSource(), {"rule": RuleEngine()})
    results = runner.run(datetime(2024, 1, 26))
    assert "_warning" in results
    assert "calendar_unavailable" in results["_warning"]


# ---------------------------------------------------------------------------
# P1-7 batch_runs 标记：无快照但有标记也拦截，防崩溃重跑重复成交
# ---------------------------------------------------------------------------
def test_batch_run_marker_blocks_repeat(tmp_path):
    db = Database(tmp_path / "t.db")
    ds = FakeDataSource(list(range(100, 125)))
    runner = BatchRunner(_settings(tmp_path / "t.db"), db, ds, {"rule": RuleEngine()})
    runner.run(datetime(2024, 1, 26))
    acc = db.get_account_by_engine("rule")
    assert db.has_batch_run(acc["id"], datetime(2024, 1, 26))
    # 模拟"崩溃未完成"：只 begin 一个新日期，无快照 → 重跑应被标记拦截
    db.begin_batch_run(acc["id"], datetime(2024, 1, 29))
    res = runner.run(datetime(2024, 1, 29))
    assert res["rule"]["skipped"] is True


# ---------------------------------------------------------------------------
# P1-8 + P2-14 决策执行结果回填（含风控截断留痕）
# ---------------------------------------------------------------------------
def test_execution_results_captured():
    state = AccountState(100_000, 100_000)
    decisions = [
        Decision("510300", "buy", amount=30_000, reason="x"),
        Decision("999999", "buy", amount=1_000, reason="幻"),
    ]
    new_state, trades, results = execute_decisions(
        state, decisions, {"510300": 4.0}, {"510300": "x"}, RiskConfig(), D
    )
    # buy 实际成交，含"请求→实际"截断信息
    assert results["510300"].startswith("executed:buy")
    assert "请求" in results["510300"] and "→实际" in results["510300"]
    # 未知 symbol 无价格
    assert results["999999"] == "no_price"


# ---------------------------------------------------------------------------
# P2-16 日期参数校验
# ---------------------------------------------------------------------------
def test_date_type_valid():
    assert _date_type("2026-08-01") == datetime(2026, 8, 1)


def test_date_type_invalid():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _date_type("garbage")
