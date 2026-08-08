"""v0.7 测试：特征注入（PP-2）、Rank IC 闭环（PP-4）、复权接入回测"""
import json
from datetime import datetime, timedelta

import pytest

from aitrader.backtest import Backtester, compute_forward_returns
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.datasource import FakeDataSource
from aitrader.engines.base import DecisionContext
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState, Bar

from helpers import make_bars

D = datetime(2024, 1, 1)


def _settings(db_path):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class FakeHttp:
    def __init__(self, content):
        self.resp = FakeResponse(content)
        self.kw = {}

    def post(self, url, **kwargs):
        self.kw = kwargs
        return self.resp


def _ctx():
    bars = {"510300": make_bars([1.0] * 25)}
    return DecisionContext(D, AccountState(100_000, 100_000), bars, {"510300": "x"})


# ---------- 特征注入（PP-2） ----------
def test_feature_inject_off_by_default():
    engine = DeepSeekEngine("sk", http_client=FakeHttp(json.dumps({"decisions": []})))
    assert "特征:" not in engine._build_prompt(_ctx())


def test_feature_inject_adds_features():
    engine = DeepSeekEngine(
        "sk", http_client=FakeHttp(json.dumps({"decisions": []})), feature_inject=True
    )
    prompt = engine._build_prompt(_ctx())
    assert "特征:" in prompt
    assert "rsi14=" in prompt
    assert "量比=" in prompt
    assert "vol20=" in prompt


# ---------- forward returns（PP-4 评测闭环） ----------
def _daily_bars(closes):
    base = datetime(2024, 1, 1)
    return [
        Bar("510300", base + timedelta(days=i), c, c, c, c, 1000.0)
        for i, c in enumerate(closes)
    ]


def test_compute_forward_returns():
    bars = _daily_bars([float(100 + i) for i in range(30)])
    full = {"510300": bars}
    buys = [
        (datetime(2024, 1, 3), "510300", 0.6),   # entry=102, exit=idx12 → 112
        (datetime(2024, 1, 10), "510300", 0.8),  # entry=109, exit=idx18 → 118
        (datetime(2024, 1, 25), "510300", 0.9),  # 末尾不足 horizon → 跳过
    ]
    confs, fwd = compute_forward_returns(buys, full, horizon_days=10)
    assert confs == pytest.approx([0.6, 0.8])
    assert fwd == pytest.approx([112 / 102 - 1, 119 / 109 - 1])


def test_compute_forward_returns_empty():
    confs, fwd = compute_forward_returns([], {}, horizon_days=10)
    assert confs == [] and fwd == []


# ---------- 复权透传 + Rank IC 返回 ----------
def test_backtester_passes_adjust_and_returns_rank_ic(tmp_path):
    class RecordingDS(FakeDataSource):
        def __init__(self, closes):
            super().__init__(closes, base_date=datetime(2023, 1, 1))
            self.last_adjust = None

        def fetch_daily_bars(self, symbol, days, exchange="SH", end_date=None, adjust="none"):
            self.last_adjust = adjust
            return super().fetch_daily_bars(symbol, days, exchange, end_date, adjust)

    ds = RecordingDS(list(range(100, 200)))
    db_path = tmp_path / "bt.db"
    db = Database(db_path)
    settings = _settings(db_path)
    bt = Backtester(
        settings, db, ds, RuleEngine(), "rule",
        datetime(2024, 1, 1), datetime(2024, 1, 31), adjust="hfq",
    )
    res = bt.run()
    assert ds.last_adjust == "hfq"
    assert "rank_ic" in res
    assert res["rank_ic"]["n"] >= 0
    assert isinstance(res["rank_ic"]["ic"], float)


def test_backtester_default_adjust_none(tmp_path):
    class RecordingDS(FakeDataSource):
        def __init__(self, closes):
            super().__init__(closes, base_date=datetime(2023, 1, 1))
            self.last_adjust = None

        def fetch_daily_bars(self, symbol, days, exchange="SH", end_date=None, adjust="none"):
            self.last_adjust = adjust
            return super().fetch_daily_bars(symbol, days, exchange, end_date, adjust)

    ds = RecordingDS(list(range(100, 200)))
    db_path = tmp_path / "bt.db"
    db = Database(db_path)
    settings = _settings(db_path)
    bt = Backtester(
        settings, db, ds, RuleEngine(), "rule",
        datetime(2024, 1, 1), datetime(2024, 1, 31),
    )
    bt.run()
    assert ds.last_adjust == "none"
