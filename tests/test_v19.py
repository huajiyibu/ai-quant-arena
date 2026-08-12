"""v0.19 测试：P0-3 回撤告警能真正触发（_maybe_notify 传全量快照而非单元素）"""
from datetime import datetime
from pathlib import Path

from aitrader.config import NotifyConfig, Settings
from aitrader.database import Database
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState
from aitrader.notify import check_alerts

D = datetime(2024, 1, 1)


def _settings(db_path):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "x", "exchange": "SH"}},
        db_path=db_path,
        notify=NotifyConfig(enabled=True, webhook_url="http://x", max_drawdown_alert=0.15),
    )


def test_check_alerts_drawdown_fires_on_full_history():
    """check_alerts 对全量快照（先涨后跌）应触发回撤告警（回撤 20% ≥ 15%）"""
    snaps = [
        {"total_assets": 100_000},
        {"total_assets": 110_000},
        {"total_assets": 88_000},
    ]
    alerts = check_alerts(True, "", snaps, 5, max_drawdown=0.15)
    assert any("回撤" in a for a in alerts)


def test_check_alerts_single_snapshot_never_fires():
    """单元素快照（旧 bug：peak==last）不应触发回撤——验证修复方向正确性"""
    snaps = [{"total_assets": 88_000}]
    assert check_alerts(True, "", snaps, 5, max_drawdown=0.15) == []


def test_maybe_notify_fires_drawdown_alert(tmp_path, monkeypatch):
    """_maybe_notify 传全量快照时，净值先涨后跌能触发 send_notify（P0-3 回归）"""
    import run as runmod

    sent = []
    import aitrader.notify as nmod
    monkeypatch.setattr(nmod, "send_notify", lambda text, url: sent.append(text))
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    acc = db.create_account("t", "ai", 100_000)
    db.add_snapshot(acc, datetime(2024, 1, 2), AccountState(100_000, 100_000))
    db.add_snapshot(acc, datetime(2024, 1, 3), AccountState(100_000, 110_000))
    db.add_snapshot(acc, datetime(2024, 1, 4), AccountState(100_000, 88_000))
    runmod._maybe_notify(
        _settings(db_path), db, {"ai": RuleEngine()}, {"ai": {"trades": 0}}, datetime(2024, 1, 4)
    )
    assert sent, "应触发告警推送"
    assert "回撤" in sent[0]
