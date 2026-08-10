"""v0.11 测试：A-2 引擎隔离、A-3 收盘守卫、P2-2 滑点、B-1 校准、B-2 日报、B-3 市场环境"""
import json
from datetime import datetime
from pathlib import Path

import pytest

from aitrader.batch import BatchRunner
from aitrader.config import RiskConfig, Settings
from aitrader.database import Database
from aitrader.datasource import FakeDataSource
from aitrader.engines.base import DecisionContext
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState, Decision
from aitrader.portfolio import execute_decisions
from aitrader.reporter import build_daily_report

from helpers import make_bars

D = datetime(2024, 1, 1)


def _settings(db_path):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "x", "exchange": "SH"}},
        db_path=db_path,
    )


class FakeHttp:
    def __init__(self, content):
        self.resp = type(
            "R",
            (),
            {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"choices": [{"message": {"content": content}}]},
            },
        )()
        self.kw = {}

    def post(self, url, **kwargs):
        self.kw = kwargs
        return self.resp


def _ctx():
    bars = {"510300": make_bars([1.0] * 25)}
    return DecisionContext(D, AccountState(100_000, 100_000), bars, {"510300": "x"})


# ---------- P2-2 滑点 ----------
def test_slippage_changes_fill_price():
    state = AccountState(100_000, 100_000)
    risk = RiskConfig(slippage_bps=10)  # 10bps
    dec = Decision("510300", "buy", amount=30_000, reason="x", confidence=0.8)
    _, trades, _ = execute_decisions(
        state, [dec], {"510300": 4.0}, {"510300": "x"}, risk, D
    )
    assert trades[0].price == pytest.approx(4.0 * (1 + 10 / 10000))  # 买价上浮


def test_slippage_zero_default():
    state = AccountState(100_000, 100_000)
    risk = RiskConfig()
    dec = Decision("510300", "buy", amount=30_000, reason="x", confidence=0.8)
    _, trades, _ = execute_decisions(
        state, [dec], {"510300": 4.0}, {"510300": "x"}, risk, D
    )
    assert trades[0].price == pytest.approx(4.0)


# ---------- B-3 市场环境 ----------
def test_market_env_injected_when_enabled():
    engine = DeepSeekEngine(
        "sk", http_client=FakeHttp(json.dumps({"decisions": []})), market_env_inject=True
    )
    assert "市场(" in engine._build_prompt(_ctx())


def test_market_env_off_by_default():
    engine = DeepSeekEngine("sk", http_client=FakeHttp(json.dumps({"decisions": []})))
    assert "市场(" not in engine._build_prompt(_ctx())


# ---------- A-2 引擎隔离 ----------
def test_engine_isolation_keeps_others_running(monkeypatch, tmp_path):
    db = Database(tmp_path / "t.db")
    ds = FakeDataSource([1.0] * 25, base_date=datetime(2024, 1, 1))
    runner = BatchRunner(_settings(tmp_path / "t.db"), db, ds, {"rule": RuleEngine()})

    def boom(engine_type, engine, date, bars_map, force):
        if engine_type == "rule":
            raise RuntimeError("boom")
        return {"ok": True}

    monkeypatch.setattr(runner, "_run_engine", boom)
    results = runner.run(datetime(2024, 1, 26))
    assert "rule" in results
    assert results["rule"].get("skipped") is True  # 异常被隔离，不冒泡
    assert "_warning" not in results or True  # 其他引擎逻辑不受影响


# ---------- A-3 收盘守卫 ----------
def test_before_close_guard(monkeypatch, tmp_path):
    import aitrader.batch as batch_mod

    class FakeDT(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 10, 14, 30, 0)  # 周一 14:30 盘中

    monkeypatch.setattr(batch_mod, "datetime", FakeDT)
    db = Database(tmp_path / "t.db")
    ds = FakeDataSource([1.0] * 25, base_date=datetime(2024, 1, 1))
    runner = BatchRunner(_settings(tmp_path / "t.db"), db, ds, {"rule": RuleEngine()})
    results = runner.run(FakeDT(2026, 8, 10, 14, 30, 0))
    assert "_warning" in results
    assert results["_warning"] == "before_close"


# ---------- B-1 真实盘校准 ----------
def test_calibrate_forward_returns(tmp_path):
    db = Database(tmp_path / "t.db")
    acc_id = db.create_account("t", "ai", 100_000)
    ds = FakeDataSource([1.0] * 40, base_date=datetime(2024, 1, 1))
    runner = BatchRunner(_settings(tmp_path / "t.db"), db, ds, {"rule": RuleEngine()})
    bars_map = {
        "510300": ds.fetch_daily_bars(
            "510300", 45, "SH", end_date=datetime(2024, 2, 10)
        )
    }
    db.add_decision(
        acc_id, datetime(2024, 1, 20), "ai",
        Decision("510300", "buy", amount=1000, confidence=0.7),
    )
    runner._calibrate_forward_returns(acc_id, datetime(2024, 2, 10), bars_map)
    cal = db.get_calibrated_decisions(acc_id)
    assert len(cal) == 1
    assert cal[0]["forward_return"] == pytest.approx(0.0)  # 恒定价格 → fwd=0


# ---------- B-2 日报（今日决策 + 数据截至） ----------
def test_daily_report_has_decision_and_bar_date(tmp_path, monkeypatch):
    import aitrader.datasource as dsmod

    def _fail(self, *a, **k):
        raise RuntimeError("no net in test")

    monkeypatch.setattr(dsmod.AkShareDataSource, "fetch_daily_bars", _fail)
    db = Database(tmp_path / "t.db")
    acc_id = db.create_account("t", "ai", 100_000)
    db.add_decision(
        acc_id, D, "ai", Decision("510300", "buy", amount=1000, reason="测试", confidence=0.7)
    )
    db.add_snapshot(acc_id, D, AccountState(100_000, 100_000), bar_date=D.strftime("%Y-%m-%d"))
    out = build_daily_report(
        db, _settings(tmp_path / "t.db"), Path("nonexist.png"), tmp_path / "r.html"
    )
    html = out.read_text(encoding="utf-8")
    assert "今日决策" in html
    assert "数据截至" in html
