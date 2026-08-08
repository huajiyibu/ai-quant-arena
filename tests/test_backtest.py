"""回测模块测试：指标计算、基准、walk-forward 回放、AI 响应缓存"""
from datetime import datetime

import pytest

from aitrader.backtest import Backtester, compute_benchmark, compute_metrics
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.datasource import FakeDataSource
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.engines.rule import RuleEngine
from aitrader.models import Bar


def _settings(db_path, capital=100_000):
    return Settings(
        initial_capital=capital,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )


def test_compute_metrics_basic():
    snapshots = [
        {"date": "2024-01-01", "cash": 100000, "total_assets": 100000, "pnl": 0},
        {"date": "2024-01-02", "cash": 50000, "total_assets": 110000, "pnl": 10000},
        {"date": "2024-01-03", "cash": 50000, "total_assets": 105000, "pnl": 5000},
    ]
    trades = [
        {"symbol": "510300", "action": "buy", "amount": 50000},
        {"symbol": "510300", "action": "sell", "amount": 60000},
    ]
    m = compute_metrics(snapshots, trades, 100000)
    assert m.total_return == pytest.approx(0.05)
    assert m.max_drawdown == pytest.approx(5000 / 110000)
    assert m.win_rate == pytest.approx(1.0)
    # 无亏损时盈亏比退化为毛利（10000），不除零
    assert m.profit_factor == pytest.approx(10000.0)
    assert m.buy_count == 1 and m.sell_count == 1
    assert m.trade_count == 2


def test_compute_metrics_empty():
    m = compute_metrics([], [], 100000)
    assert m.total_return == 0
    assert m.max_drawdown == 0
    assert m.sharpe == 0
    assert m.win_rate == 0
    assert m.trade_count == 0


def test_compute_metrics_negative_trade_affects_win_rate():
    snapshots = [{"date": "2024-01-01", "total_assets": 100000}]
    trades = [
        {"symbol": "510300", "action": "buy", "amount": 50000},
        {"symbol": "510300", "action": "sell", "amount": 40000},
    ]
    m = compute_metrics(snapshots, trades, 100000)
    assert m.win_rate == 0.0
    assert m.profit_factor == 0.0  # 纯亏损无盈利


def test_benchmark_buy_and_hold():
    bars = [
        Bar("510300", datetime(2024, 1, 1), 10, 10, 10, 10, 1000),
        Bar("510300", datetime(2024, 1, 2), 10, 10, 10, 11, 1000),
        Bar("510300", datetime(2024, 1, 3), 11, 11, 11, 12, 1000),
    ]
    curve = compute_benchmark(bars, 100000)
    assert len(curve) == 3
    assert curve[0]["assets"] == 100000
    assert curve[-1]["assets"] == 120000


def test_benchmark_empty():
    assert compute_benchmark([], 100000) == []


def test_backtester_replays_rule_engine(tmp_path):
    """walk-forward 回放：规则引擎在上升趋势中产生成交，指标可计算"""
    closes = list(range(100, 200))
    ds = FakeDataSource(closes, base_date=datetime(2023, 1, 1))
    db_path = tmp_path / "bt.db"
    db = Database(db_path)
    settings = _settings(db_path)
    bt = Backtester(
        settings, db, ds, RuleEngine(), "rule",
        datetime(2024, 1, 1), datetime(2024, 1, 31),
    )
    res = bt.run()
    assert res["metrics"] is not None
    assert len(res["snapshots"]) >= 10
    assert len(res["trades"]) >= 1
    assert res["metrics"].trade_count == len(res["trades"])
    # 快照与成交流水都落在回测库
    acc = db.get_account_by_engine("rule")
    assert acc is not None


def test_backtester_reset_gives_fresh_start(tmp_path):
    """重复回测从初始资金重新开始，成交不累计"""
    closes = list(range(100, 200))
    ds = FakeDataSource(closes, base_date=datetime(2023, 1, 1))
    db_path = tmp_path / "bt.db"
    db = Database(db_path)
    settings = _settings(db_path)
    start, end = datetime(2024, 1, 1), datetime(2024, 1, 31)

    bt1 = Backtester(settings, db, ds, RuleEngine(), "rule", start, end)
    r1 = bt1.run()
    bt2 = Backtester(settings, db, ds, RuleEngine(), "rule", start, end)
    r2 = bt2.run()

    assert r1["metrics"].trade_count == r2["metrics"].trade_count
    acc = db.get_account_by_engine("rule")
    assert len(db.get_trades(acc["id"])) == r2["metrics"].trade_count


def test_backtester_no_bars_returns_none(tmp_path):
    """区间内无行情时返回 metrics=None"""

    class EmptyDataSource:
        name = "empty"

        def fetch_daily_bars(self, symbol, days, exchange="SH", end_date=None):
            return []

        def is_trading_day(self, date):
            return True

    db_path = tmp_path / "bt.db"
    db = Database(db_path)
    settings = _settings(db_path)
    bt = Backtester(
        settings, db, EmptyDataSource(), RuleEngine(), "rule",
        datetime(2024, 1, 1), datetime(2024, 1, 31),
    )
    res = bt.run()
    assert res["metrics"] is None


def test_deepseek_response_cache(tmp_path):
    """DeepSeek 响应缓存：相同 prompt 第二次不重复调用 API"""
    calls = {"n": 0}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"decisions":[]}'}}]}

    class FakeHttp:
        def post(self, url, **kwargs):
            calls["n"] += 1
            return FakeResp()

    http = FakeHttp()
    cache: dict = {}
    engine = DeepSeekEngine(api_key="x", http_client=http, response_cache=cache)
    engine._call("prompt-test")
    engine._call("prompt-test")
    assert calls["n"] == 1  # 第二次命中缓存
    assert len(cache) == 1


def test_deepseek_cache_shared_across_policy_flags(tmp_path):
    """缓存键不含 include_policy：ai 与 ai_policy 提示词相同时共享缓存，不重复计费"""
    calls = {"n": 0}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"decisions":[]}'}}]}

    class FakeHttp:
        def post(self, url, **kwargs):
            calls["n"] += 1
            return FakeResp()

    http = FakeHttp()
    cache: dict = {}
    # 回测中 ai 与 ai_policy 的 policy_text 均为空，prompt 一致 → 应共享缓存
    DeepSeekEngine(api_key="x", http_client=http, response_cache=cache)._call("same-prompt")
    DeepSeekEngine(
        api_key="x", http_client=http, include_policy=True, response_cache=cache
    )._call("same-prompt")
    assert calls["n"] == 1


def test_select_backtest_engines_default_skips_policy():
    """回测默认跳过政策版，避免重复结果误导"""
    from run import select_backtest_engines

    engines = {"rule": RuleEngine(), "ai": RuleEngine(), "ai_policy": RuleEngine()}
    sel, warns = select_backtest_engines("both", engines)
    assert set(sel) == {"rule", "ai"}
    assert warns


def test_select_backtest_engines_explicit_policy():
    """显式 --engine ai_policy 才回测政策版，并提示退化为纯价格版"""
    from run import select_backtest_engines

    engines = {"rule": RuleEngine(), "ai": RuleEngine(), "ai_policy": RuleEngine()}
    sel, warns = select_backtest_engines("ai_policy", engines)
    assert set(sel) == {"ai_policy"}
    assert any("退化" in w for w in warns)
