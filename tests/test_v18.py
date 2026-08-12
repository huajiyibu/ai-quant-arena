"""v0.18 测试：快照记当日货基利息 + 日报展示累计利息"""
from datetime import datetime
from pathlib import Path

import pytest

from aitrader.batch import BatchRunner
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.datasource import FakeDataSource
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState
from aitrader.reporter import build_daily_report

D = datetime(2024, 1, 1)


def _settings(db_path, rate=0.017):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
        cash_interest_rate=rate,
    )


def test_add_snapshot_records_interest(tmp_path):
    db = Database(tmp_path / "t.db")
    acc = db.create_account("t", "rule", 100_000)
    db.add_snapshot(acc, D, AccountState(100_000, 100_000), interest=6.75)
    snap = db.get_snapshots(acc)[0]
    assert snap["interest"] == pytest.approx(6.75)


def test_batch_accumulates_interest_in_snapshots(tmp_path):
    days = {"2024-01-02", "2024-01-03", "2024-01-04"}
    db = Database(tmp_path / "t.db")
    ds = FakeDataSource([1.0] * 40, base_date=D, trading_days=days)
    runner = BatchRunner(_settings(tmp_path / "t.db", rate=0.017), db, ds, {"rule": RuleEngine()})
    for d in (datetime(2024, 1, 2), datetime(2024, 1, 3), datetime(2024, 1, 4)):
        runner.run(d)
    acc = db.get_account_by_engine("rule")
    snaps = db.get_snapshots(acc["id"])
    assert len(snaps) == 3
    total = sum(s["interest"] for s in snaps)
    # 3 天空仓累计利息 ≈ 100000*((1+r)^3 - 1) 的近似；每笔 > 0
    assert all(s["interest"] > 0 for s in snaps)
    expected_total = 100_000 * ((1 + 0.017 / 252) ** 3 - 1)
    assert total == pytest.approx(expected_total, rel=0.05)


def test_report_shows_interest_total(tmp_path, monkeypatch):
    import aitrader.datasource as dsmod

    def _fail(self, *a, **k):
        raise RuntimeError("no net")

    monkeypatch.setattr(dsmod.AkShareDataSource, "fetch_daily_bars", _fail)
    db = Database(tmp_path / "t.db")
    acc = db.create_account("t", "rule", 100_000)
    db.add_snapshot(acc, D, AccountState(100_000, 100_000), interest=6.75)
    out = build_daily_report(
        db, _settings(tmp_path / "t.db"), Path("nonexist.png"), tmp_path / "r.html"
    )
    html = out.read_text(encoding="utf-8")
    assert "货基利息累计" in html
    assert "+6.75" in html
