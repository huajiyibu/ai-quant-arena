"""v0.17 测试：评审 P0-1 计息幂等 / P0-3 数据截至 / P0-5 IC 只收成交 / P1-1 归因净口径 / P1-2 已持仓 / P1-3 实时重试 / P1-6 last_run 保留"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aitrader.batch import BatchRunner
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.datasource import AkShareDataSource, FakeDataSource
from aitrader.engines.base import DecisionContext
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState, Decision, Position
from aitrader.reporter import build_daily_report

from helpers import make_bars

D = datetime(2024, 1, 1)


def _settings(db_path, rate=0.017):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
        cash_interest_rate=rate,
    )


class FakeHttp:
    def __init__(self, content):
        self.resp = type(
            "R", (), {"raise_for_status": lambda self: None,
                       "json": lambda self: {"choices": [{"message": {"content": content}}]}}
        )()
        self.kw = {}

    def post(self, url, **kwargs):
        self.kw = kwargs
        return self.resp


# ---------- P0-1 计息幂等 ----------
def test_batch_interest_idempotent_on_force(tmp_path):
    """同日 force 重跑不双计息（last_interest_date 幂等）"""
    days = {"2024-01-02"}
    db = Database(tmp_path / "t.db")
    ds = FakeDataSource([1.0] * 40, base_date=D, trading_days=days)
    runner = BatchRunner(_settings(tmp_path / "t.db", rate=0.017), db, ds, {"rule": RuleEngine()})
    d = datetime(2024, 1, 2)
    runner.run(d)
    acc = db.get_account_by_engine("rule")
    cash1 = db.get_snapshots(acc["id"])[-1]["cash"]
    runner.run(d, force=True)  # force 重跑同日
    cash2 = db.get_snapshots(acc["id"])[-1]["cash"]
    assert cash2 == pytest.approx(cash1, abs=0.01)


# ---------- P0-3 日报数据截至陈旧告警 ----------
def test_report_data_until_stale_warning(tmp_path, monkeypatch):
    import aitrader.datasource as dsmod

    def _fail(self, *a, **k):
        raise RuntimeError("no net")

    monkeypatch.setattr(dsmod.AkShareDataSource, "fetch_daily_bars", _fail)
    db = Database(tmp_path / "t.db")
    acc = db.create_account("t", "rule", 100_000)
    stale = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    db.add_snapshot(acc, datetime.now(), AccountState(100_000, 100_000), bar_date=stale)
    out = build_daily_report(
        db, _settings(tmp_path / "t.db"), Path("nonexist.png"), tmp_path / "r.html"
    )
    html = out.read_text(encoding="utf-8")
    assert "陈旧" in html


# ---------- P0-5 校准只回填已成交 buy ----------
def test_get_uncalibrated_buys_excludes_rejected(tmp_path):
    db = Database(tmp_path / "t.db")
    acc = db.create_account("t", "ai", 100_000)
    for _ in range(2):
        db.add_decision(
            acc, D, "ai", Decision("510300", "buy", amount=1000, confidence=0.7)
        )
    # 把其中一笔标为被风控拒绝
    with db._connect() as conn:
        conn.execute(
            "UPDATE decisions SET execution_result='risk_rejected:low_confidence' "
            "WHERE id=(SELECT MIN(id) FROM decisions WHERE account_id=?)",
            (acc,),
        )
    uncal = db.get_uncalibrated_buys(acc)
    assert len(uncal) == 1  # 被拒那笔不进校准


# ---------- P1-2 已持仓再 buy 标记 ----------
def test_validate_marks_already_holding():
    bars = {"510300": make_bars([1.0] * 25)}
    pos = {"510300": Position("510300", "沪深300ETF", 1000, 1.0, 1.0)}
    ctx = DecisionContext(
        D, AccountState(100_000, 99_000, pos), bars, {"510300": "沪深300ETF"}
    )
    eng = DeepSeekEngine(api_key="test", http_client=FakeHttp('{"decisions":[]}'))
    out = eng._validate([Decision("510300", "buy", amount=50_000, confidence=0.8)], ctx)
    assert out[0].valid is False
    assert out[0].validation == "already_holding"


# ---------- P1-3 实时行情接入重试 ----------
def test_fetch_realtime_retries(monkeypatch):
    parts = ["沪深300ETF", "4.80", "4.79", "4.81", "4.82", "4.78", "", "", "123456"] \
        + [""] * 21 + ["2024-01-02", "15:00:00"]
    text = 'var hq_str_sh510300="' + ",".join(parts) + '";'
    calls = []

    def fake_get(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("boom")
        return type("R", (), {"encoding": "gbk", "text": text})()

    monkeypatch.setattr("requests.get", fake_get)
    ds = AkShareDataSource()
    bar = ds._fetch_realtime("510300", "SH")
    assert len(calls) >= 2  # 首次失败后重试
    assert bar is not None and bar.close == pytest.approx(4.81)


# ---------- P1-6 last_run 失败保留上次 ----------
def test_write_last_run_keeps_prev_on_error(tmp_path, monkeypatch):
    import run as runmod

    monkeypatch.setattr(runmod, "ROOT", tmp_path)
    runmod.write_last_run(
        {"mode": "batch", "date": "2026-08-11", "engine_results": {"ai": {"trades": 1}}, "ok": True}
    )
    runmod.write_last_run({"mode": "error", "ok": False, "error": "boom", "engine_results": {}})
    payload = json.loads(
        (tmp_path / "data" / "last_run.json").read_text(encoding="utf-8")
    )
    assert payload["engine_results"]["ai"]["trades"] == 1  # 保留上次
    assert payload["prev_ok"] is True
