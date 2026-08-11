"""v0.13 测试：N-1 真实盘特征复权（双 bars）——特征用复权价、估值/成交/价格展示用原始价"""
from datetime import datetime

import pytest

from aitrader.adjfactor import compute_adjusted_bars, parse_dividends
from aitrader.batch import BatchRunner
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.datasource import FakeDataSource
from aitrader.engines.base import DecisionContext, DecisionEngine, EngineResult
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState, Decision
from aitrader.features import compute_features

from helpers import make_bars

D = datetime(2024, 1, 1)


class _DummyHttp:
    """_build_prompt 不调 API，但构造器需要 http_client 提供 post 方法"""

    def post(self, *a, **k):
        raise AssertionError("不应触发网络调用")


def _settings(db_path):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )


def _engine(feature_inject=True):
    return DeepSeekEngine(
        api_key="test", http_client=_DummyHttp(), feature_inject=feature_inject
    )


# ---------- _feature_bars 优先级 ----------
def test_feature_bars_prefers_adjusted():
    raw = make_bars([4.0] * 25)
    adj = make_bars([4.1] * 25)  # 模拟复权（全部 +0.1）
    ctx = DecisionContext(
        D, AccountState(100_000, 100_000), {"510300": raw}, {"510300": "x"},
        adjusted_bars={"510300": adj},
    )
    eng = _engine()
    assert eng._feature_bars(ctx, "510300") is adj


def test_feature_bars_falls_back_when_missing():
    raw = make_bars([4.0] * 25)
    # adjusted_bars 为 None → 回退原始（回测路径）
    ctx0 = DecisionContext(D, AccountState(100_000, 100_000), {"510300": raw}, {"510300": "x"})
    eng = _engine()
    assert eng._feature_bars(ctx0, "510300") == raw
    # adjusted_bars 无该 symbol / 该 symbol 为空列表 → 回退原始
    ctx1 = DecisionContext(
        D, AccountState(100_000, 100_000), {"510300": raw}, {"510300": "x"},
        adjusted_bars={"999999": []},
    )
    assert eng._feature_bars(ctx1, "510300") == raw
    ctx2 = DecisionContext(
        D, AccountState(100_000, 100_000), {"510300": raw}, {"510300": "x"},
        adjusted_bars={"510300": []},
    )
    assert eng._feature_bars(ctx2, "510300") == raw


# ---------- 核心：prompt 特征用复权价、价格展示用原始价 ----------
def test_prompt_features_use_adjusted_while_price_shows_raw():
    """除权跳空场景：原始价最后一天跳空 3.9，复权价抹平为 4.0。
    断言特征行 ret5 基于复权（≈0% 不跳空），价格行仍显示原始 3.900。"""
    # 前 24 根 4.0，最后一根跳空到 3.9（模拟除权）；Bar 是 frozen dataclass，用 replace 重建
    from dataclasses import replace

    raw = make_bars([4.0] * 25)
    last = raw[-1]
    raw = raw[:-1] + [
        replace(last, open=3.9, high=3.9 * 1.01, low=3.9 * 0.99, close=3.9)
    ]
    # 除权日累计分红 0.1 → 复权后最后一根 = 4.0
    last_date = raw[-1].datetime.date()
    adj = compute_adjusted_bars(raw, parse_dividends([(last_date, 0.1)]))

    # 前置校验：确实造出了"原始跳空、复权连续"
    assert raw[-1].close == pytest.approx(3.9)
    assert adj[-1].close == pytest.approx(4.0)
    assert compute_features(raw)["ret_5d"] == pytest.approx(-0.025)
    assert compute_features(adj)["ret_5d"] == pytest.approx(0.0)

    ctx = DecisionContext(
        D, AccountState(100_000, 100_000), {"510300": raw}, {"510300": "沪深300ETF"},
        adjusted_bars={"510300": adj},
    )
    prompt = _engine()._build_prompt(ctx)
    # 特征行用复权（ret5 不跳空）；价格展示行用原始（保留 3.900 真实可成交价）
    assert "ret5=+0.00%" in prompt
    assert "3.900" in prompt


# ---------- batch 集成：双 bars 传递 + 估值用原始 ----------
class _CaptureEngine(DecisionEngine):
    name = "cap"

    def __init__(self):
        self.ctx = None

    def decide(self, ctx):
        self.ctx = ctx
        return EngineResult(decisions=[Decision("", "hold", reason="")])


def test_batch_passes_adjusted_bars_to_ctx(tmp_path, monkeypatch):
    import aitrader.adjfactor as adjmod

    # 分红：整个区间每份累计 0.1 → 复权价 = 原始价 + 0.1
    monkeypatch.setattr(adjmod, "fetch_dividends", lambda sym: [(D.date(), 0.1)])
    cap = _CaptureEngine()
    db_path = tmp_path / "t.db"
    settings = _settings(db_path)
    db = Database(db_path)
    ds = FakeDataSource([4.0] * 25, base_date=D)
    runner = BatchRunner(settings, db, ds, {"cap": cap})
    runner.run(D)

    assert cap.ctx is not None
    raw_last = cap.ctx.bars["510300"][-1].close
    adj_last = cap.ctx.adjusted_bars["510300"][-1].close
    assert raw_last == pytest.approx(4.0)      # ctx.bars 保持原始价
    assert adj_last == pytest.approx(4.1)      # ctx.adjusted_bars 为复权价
    # 引擎内特征源 = 复权，价格源 = 原始
    assert cap.ctx.adjusted_bars["510300"][-1] is not cap.ctx.bars["510300"][-1]


def test_batch_fallback_when_dividend_fails(tmp_path, monkeypatch):
    import aitrader.adjfactor as adjmod

    def _boom(sym):
        raise RuntimeError("no net")

    monkeypatch.setattr(adjmod, "fetch_dividends", _boom)
    cap = _CaptureEngine()
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    runner = BatchRunner(_settings(db_path), db, FakeDataSource([4.0] * 25, base_date=D), {"cap": cap})
    runner.run(D)
    # 复权失败降级：adjusted_bars 与 bars 相同，流程不中断
    assert cap.ctx.adjusted_bars["510300"][-1].close == pytest.approx(4.0)


# ---------- B-1 forward return 用复权价（含分红口径，与回测一致） ----------
def test_forward_return_uses_adjusted_bars(tmp_path):
    """决策日分红未生效（4.0），20 交易日后分红生效（复权 4.1）：
    用复权价回填的 forward return 应含分红（≈2.5%），而非原始价 0%。"""
    bars = make_bars([4.0] * 40, start=D)
    adjusted = compute_adjusted_bars(
        bars, parse_dividends([("2024-01-20", 0.1)])
    )
    db = Database(tmp_path / "t.db")
    acc_id = db.create_account("t", "ai", 100_000)
    db.add_decision(
        acc_id, datetime(2024, 1, 5), "ai",
        Decision("510300", "buy", amount=1000, confidence=0.7),
    )
    runner = BatchRunner(
        _settings(tmp_path / "t.db"), db, FakeDataSource([4.0] * 40, base_date=D),
        {"ai": RuleEngine()},
    )
    # batch 现传 adjusted_bars_map → 回填用复权
    runner._calibrate_forward_returns(
        acc_id, datetime(2024, 2, 10), {"510300": adjusted}
    )
    cal = db.get_calibrated_decisions(acc_id)
    assert len(cal) == 1
    assert cal[0]["forward_return"] == pytest.approx(4.1 / 4.0 - 1)

    # 对照：若误用原始价则漏掉分红，fwd=0
    db2 = Database(tmp_path / "t2.db")
    acc2 = db2.create_account("t", "ai", 100_000)
    db2.add_decision(
        acc2, datetime(2024, 1, 5), "ai",
        Decision("510300", "buy", amount=1000, confidence=0.7),
    )
    runner2 = BatchRunner(
        _settings(tmp_path / "t2.db"), db2, FakeDataSource([4.0] * 40, base_date=D),
        {"ai": RuleEngine()},
    )
    runner2._calibrate_forward_returns(acc2, datetime(2024, 2, 10), {"510300": bars})
    cal2 = db2.get_calibrated_decisions(acc2)
    assert cal2[0]["forward_return"] == pytest.approx(0.0)
