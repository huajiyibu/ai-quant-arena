"""v0.3 改进测试：回测报表过滤、数据新鲜度守卫、JSON 异常留痕、政策拉取时机"""
from datetime import datetime

import pytest

from aitrader.batch import BatchRunner
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState
from aitrader.reporter import build_backtest_report

D = datetime(2024, 1, 1)


def _settings(db_path):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# P0-1 回测报表只渲染指定引擎（过滤历史残留账户）
# ---------------------------------------------------------------------------
def test_backtest_report_filters_engine_types(tmp_path):
    db = Database(tmp_path / "t.db")
    settings = _settings(tmp_path / "t.db")
    r_id = db.create_account("rule引擎·回测", "rule", 100_000)
    a_id = db.create_account("ai引擎·回测", "ai", 100_000)
    db.add_snapshot(r_id, D, AccountState(100_000, 100_000))
    db.add_snapshot(a_id, D, AccountState(100_000, 100_000))

    out = build_backtest_report(
        db, settings, tmp_path / "no.png", tmp_path / "r.html", engine_types={"rule"}
    )
    text = out.read_text(encoding="utf-8")
    assert "rule引擎·回测" in text
    assert "ai引擎·回测" not in text


# ---------------------------------------------------------------------------
# P0-2 数据新鲜度守卫：严重陈旧行情当日跳过，新鲜则放行
# ---------------------------------------------------------------------------
def test_batch_skips_when_bars_stale(tmp_path):
    from aitrader.datasource import FakeDataSource

    class StaleDataSource:
        """返回固定旧行情（最后一天远早于目标日），模拟数据源滞后/断更"""

        name = "stale"

        def __init__(self):
            self._inner = FakeDataSource([1.0] * 25)

        def fetch_daily_bars(self, symbol, days, exchange="SH", end_date=None):
            return self._inner.fetch_daily_bars(symbol, days, exchange, end_date=None)

        def is_trading_day(self, date):
            return True

    db = Database(tmp_path / "t.db")
    runner = BatchRunner(_settings(tmp_path / "t.db"), db, StaleDataSource(), {"rule": RuleEngine()})
    results = runner.run(datetime(2024, 3, 1))  # 目标日远超 bars 最新(2024-01-25)
    assert "_warning" in results
    assert "stale_bars" in results["_warning"]


def test_batch_runs_when_bars_fresh(tmp_path):
    from aitrader.datasource import FakeDataSource

    db = Database(tmp_path / "t.db")
    ds = FakeDataSource([1.0] * 25)
    runner = BatchRunner(_settings(tmp_path / "t.db"), db, ds, {"rule": RuleEngine()})
    results = runner.run(datetime(2024, 1, 26))  # 与 bars 最新(01-25)差 1 天 → 放行
    assert "_warning" not in results
    assert "rule" in results


# ---------------------------------------------------------------------------
# P2-11 resp.json() 抛 ValueError（JSONDecodeError）时异常含原始响应文本
# ---------------------------------------------------------------------------
def test_call_non_json_response_raises_with_body():
    class BadHttp:
        def post(self, url, **kw):
            class Resp:
                text = "not json at all"

                def raise_for_status(self):
                    pass

                def json(self):
                    raise ValueError("Expecting value")

            return Resp()

    engine = DeepSeekEngine("sk-test", http_client=BadHttp())
    with pytest.raises(Exception) as ei:
        engine._call("prompt")
    assert "not json at all" in str(ei.value)


# ---------------------------------------------------------------------------
# P2-13 无 include_policy 引擎时不联网拉政策
# ---------------------------------------------------------------------------
def test_policy_not_fetched_without_policy_engine(tmp_path):
    from aitrader.datasource import FakeDataSource

    class FakePolicySrc:
        name = "fp"

        def __init__(self):
            self.called = False

        def fetch_macro_news(self, keywords, max_items):
            self.called = True
            return ["央行降息"]

    db = Database(tmp_path / "t.db")
    policy_src = FakePolicySrc()
    runner = BatchRunner(
        _settings(tmp_path / "t.db"), db, FakeDataSource([1.0] * 25),
        {"rule": RuleEngine()}, policy_source=policy_src,
    )
    runner.run(datetime(2024, 1, 26))
    assert policy_src.called is False
