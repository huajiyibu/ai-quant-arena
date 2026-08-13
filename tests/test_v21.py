"""v0.21 测试：公平对比归一化——真实累计盈亏与归一化对比口径分离，符号不再反转。"""
from datetime import datetime

from aitrader.config import Settings
from aitrader.database import Database
from aitrader.models import AccountState
from aitrader.reporter import FAIR_START_DATE, _fair_pnl, _fair_snapshots, _true_pnl, build_summary

D = datetime(2024, 1, 1)


def _state(total: float, cash: float | None = None) -> AccountState:
    """构造指定总资产的账户状态（pnl 由 total - initial 推出）。"""
    initial = 100_000.0
    return AccountState(initial_capital=initial, cash=cash if cash is not None else total)


def _db_with_accounts(tmp_path) -> Database:
    db = Database(tmp_path / "t.db")
    acc = db.create_account("AI引擎", "ai", 100_000)
    # 提前建仓日：bar_date 为 None（v0.21 之前的历史快照）且已盈利
    db.add_snapshot(acc, datetime(2024, 1, 2), _state(101_000), bar_date="", source="real")
    # 公平起点日：bar_date 非空，总资产已含提前建仓涨幅
    db.add_snapshot(acc, datetime(2024, 1, 3), _state(101_200), bar_date="2024-01-03", source="real")
    db.add_snapshot(acc, datetime(2024, 1, 4), _state(100_800), bar_date="2024-01-04", source="real")
    return db


def test_true_pnl_is_relative_to_initial_capital():
    """真实累计盈亏必须相对初始资金：总资产 100,800 → +800（不能因归一化变成负数）。"""
    amt, pct = _true_pnl({"pnl": 800.0}, 100_000)
    assert amt == 800.0
    assert abs(pct - 0.8) < 1e-9


def test_fair_pnl_does_not_flip_sign_of_early_gains():
    """公平对比自 FAIR_START_DATE 起算，但真实盈亏口径不应受其影响。"""
    snaps = [
        {"date": "2026-08-07", "bar_date": None, "total_assets": 1_000_000},
        {"date": "2026-08-10", "bar_date": "2026-08-10", "total_assets": 1_001_195},
        {"date": "2026-08-11", "bar_date": "2026-08-11", "total_assets": 1_000_555},
    ]
    fair = _fair_snapshots(snaps)
    assert fair[0]["date"] == FAIR_START_DATE  # 显式公平起点，而非 bar_date 残留
    f_amt, f_pct, f_date = _fair_pnl(snaps)
    assert f_date == "2026-08-10"
    assert f_amt == 1_000_555 - 1_001_195  # -640，公平对比口径允许为负
    assert f_pct < 0


def test_summary_shows_true_pnl_and_fair_note(tmp_path):
    """CLI 汇总：主口径为真实累计盈亏（符号正确），对比口径作为副注。"""
    db = _db_with_accounts(tmp_path)
    settings = Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "x", "exchange": "SH"}},
    )
    summary = build_summary(db, settings)
    assert "累计盈亏 +800.00" in summary  # 100,800 - 100,000，符号为正
    assert "对比(自" in summary and "归一)" in summary


def test_fair_snapshots_fallback_when_no_bar_date(tmp_path):
    """无任何 bar_date 时回退到首个快照（保持 v0.21 曲线兼容，不崩溃）。"""
    db = Database(tmp_path / "t.db")
    acc = db.create_account("rule", "rule", 100_000)
    db.add_snapshot(acc, datetime(2024, 1, 2), _state(100_000), bar_date="")
    db.add_snapshot(acc, datetime(2024, 1, 3), _state(100_500), bar_date="")
    snaps = db.get_snapshots(acc)
    assert len(_fair_snapshots(snaps)) == 2
    f_amt, f_pct, f_date = _fair_pnl(snaps)
    assert f_date == "2024-01-02"
    assert f_amt == 500.0


def test_fair_pnl_single_snapshot_returns_zero():
    """公平对比快照不足 2 个时不除零、不崩溃，金额为 0。"""
    snaps = [{"date": "2026-08-10", "bar_date": "2026-08-10", "total_assets": 1_000_000}]
    f_amt, f_pct, f_date = _fair_pnl(snaps)
    assert f_amt == 0.0
    assert f_pct == 0.0
    assert f_date is None
