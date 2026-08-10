"""v0.12 测试：N-2 崩溃遗留 running 可重跑、N-12 回放 source 标记、N-4 信心分桶胜率"""
from datetime import datetime
from pathlib import Path

import pytest

from aitrader.batch import BatchRunner
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.datasource import FakeDataSource
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState, Decision
from aitrader.reporter import build_daily_report

D = datetime(2024, 1, 1)


def _make_runner(tmp_path, closes, engines=None, symbols=None):
    db_path = tmp_path / "t.db"
    settings = Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols=symbols or {"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )
    db = Database(db_path)
    ds = FakeDataSource(closes)
    engines = engines or {"rule": RuleEngine()}
    return BatchRunner(settings, db, ds, engines), db


# ---------- N-2 崩溃遗留 running 卡死 ----------
def test_has_batch_run_ignores_running(tmp_path):
    """running 不算完成，崩溃遗留后应允许重跑"""
    db = Database(tmp_path / "t.db")
    acc_id = db.create_account("t", "rule", 100_000)
    db.begin_batch_run(acc_id, D)
    assert db.has_batch_run(acc_id, D) is False  # running → 未完成
    db.complete_batch_run(acc_id, D)
    assert db.has_batch_run(acc_id, D) is True  # done → 完成


def test_begin_retry_keeps_done(tmp_path):
    """同日重试 begin 不把已完成的 done 打回 running"""
    db = Database(tmp_path / "t.db")
    acc_id = db.create_account("t", "rule", 100_000)
    db.begin_batch_run(acc_id, D)
    db.complete_batch_run(acc_id, D)
    db.begin_batch_run(acc_id, D)  # 重试
    assert db.has_batch_run(acc_id, D) is True


def test_stale_running_gets_rerun(tmp_path):
    """崩溃遗留 running + 无快照 → 重跑成功并产生快照（回归：N-2 修复前会假跳过）"""
    runner, db = _make_runner(tmp_path, [1.0] * 25)
    acc_id = db.create_account("t", "rule", 100_000)
    db.begin_batch_run(acc_id, D)  # 模拟上次运行在写快照前崩溃
    assert db.get_snapshots(acc_id) == []
    runner.run(D)
    assert len(db.get_snapshots(acc_id)) == 1  # 未被 running 卡死
    assert db.has_batch_run(acc_id, D) is True  # 已补跑完成


# ---------- N-12 回放 source 标记 ----------
def test_add_snapshot_source_default_real(tmp_path):
    """真实盘快照默认 source=real"""
    db = Database(tmp_path / "t.db")
    acc_id = db.create_account("t", "rule", 100_000)
    db.add_snapshot(acc_id, D, AccountState(100_000, 100_000))
    snap = db.get_snapshots(acc_id)[0]
    assert snap["source"] == "real"


def test_snapshot_source_replay_for_history(tmp_path):
    """历史日期回放（date != 今天）→ 快照标记 source=replay，不混入真实账本"""
    runner, db = _make_runner(tmp_path, [1.0] * 25)
    runner.run(D)  # D 是历史日期
    acc_id = db.get_account_by_engine("rule")["id"]
    snap = db.get_snapshots(acc_id)[0]
    assert snap["source"] == "replay"
    assert snap["date"] == D.strftime("%Y-%m-%d")


def test_replay_and_real_share_date_keeps_first_source(tmp_path):
    """同一天先 replay 后 real：ON CONFLICT 不覆盖 source（保留首次回放标记）"""
    db = Database(tmp_path / "t.db")
    acc_id = db.create_account("t", "rule", 100_000)
    db.add_snapshot(acc_id, D, AccountState(100_000, 100_000), source="replay")
    db.add_snapshot(acc_id, D, AccountState(100_000, 100_000), source="real")
    snap = db.get_snapshots(acc_id)[0]
    assert snap["source"] == "replay"


# ---------- N-4 信心分桶胜率 ----------
def test_report_confidence_buckets(tmp_path, monkeypatch):
    """日报渲染按信心分桶的正收益占比表"""
    import aitrader.datasource as dsmod

    def _fail(self, *a, **k):
        raise RuntimeError("no net in test")

    monkeypatch.setattr(dsmod.AkShareDataSource, "fetch_daily_bars", _fail)
    db = Database(tmp_path / "t.db")
    acc_id = db.create_account("t", "ai", 100_000)
    db.add_snapshot(acc_id, D, AccountState(100_000, 100_000))  # 无快照的账户不会渲染
    rows = [
        (datetime(2024, 1, 2), 0.55, 0.01),   # <0.6  → 胜
        (datetime(2024, 1, 3), 0.65, -0.02),  # 0.6~0.7 → 负
        (datetime(2024, 1, 4), 0.75, 0.05),   # >0.7 → 胜
        (datetime(2024, 1, 5), 0.80, 0.03),   # >0.7 → 胜
    ]
    for dt, conf, fwd in rows:
        db.add_decision(
            acc_id, dt, "ai", Decision("510300", "buy", amount=1000, confidence=conf)
        )
        db.update_decision_forward_return(acc_id, dt, "510300", fwd)
    out = build_daily_report(
        db, _settings(tmp_path / "t.db"), Path("nonexist.png"), tmp_path / "r.html"
    )
    html = out.read_text(encoding="utf-8")
    assert "按信心分桶" in html
    assert "0~0.6" in html and "0.6~0.7" in html and "0.7+" in html
    # >0.7 桶 2 样本全胜
    assert "<tr><td>0.7+</td><td>2</td><td>100%</td>" in html


def _settings(db_path):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "x", "exchange": "SH"}},
        db_path=db_path,
    )
