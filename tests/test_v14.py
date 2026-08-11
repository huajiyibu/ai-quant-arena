"""v0.14 测试：N-3 成交口径标注 / N-5 归因标签 / N-8 API 记账 / N-9 catch-up / N-10 通知 / N-11 缓存兜底"""
import json
from datetime import datetime, timedelta

import pytest

from aitrader.attribution import attribute_trades, parse_tag
from aitrader.batch import BatchRunner
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.datasource import FakeDataSource
from aitrader.engines.base import DecisionContext
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState
from aitrader.notify import check_alerts, send_notify

from helpers import make_bars

D = datetime(2024, 1, 1)


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


def _settings(db_path):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "x", "exchange": "SH"}},
        db_path=db_path,
    )


def _ctx():
    bars = {"510300": make_bars([1.0] * 25)}
    return DecisionContext(D, AccountState(100_000, 100_000), bars, {"510300": "x"})


# ---------- N-8 API 调用/缓存命中记账 ----------
def test_api_cost_counting():
    eng = DeepSeekEngine(
        api_key="test", http_client=FakeHttp('{"decisions":[]}'), response_cache={}
    )
    eng.decide(_ctx())  # 未命中 → 计 1 次 API，写缓存
    eng.decide(_ctx())  # 相同 prompt → 缓存命中
    assert eng.api_calls == 1
    assert eng.cache_hits == 1


# ---------- N-3 成交口径 fill_note ----------
def test_write_last_run_has_fill_note(tmp_path, monkeypatch):
    import run as runmod

    monkeypatch.setattr(runmod, "ROOT", tmp_path)
    runmod.write_last_run({"mode": "batch"})
    payload = json.loads(
        (tmp_path / "data" / "last_run.json").read_text(encoding="utf-8")
    )
    assert "fill_note" in payload
    assert "真实盘按决策日收盘价成交" in payload["fill_note"]


# ---------- N-5 归因标签 ----------
def test_parse_tag():
    assert parse_tag("[趋势] 放量突破") == "趋势"
    assert parse_tag("回调低吸") == "其他"
    assert parse_tag("") == "其他"
    assert parse_tag(None) == "其他"


def test_attribute_trades():
    trades = [
        {"symbol": "510300", "date": "2024-01-05", "action": "buy", "price": 4.0,
         "volume": 1000, "amount": 4000, "reason": "[趋势] 突破"},
        {"symbol": "510300", "date": "2024-01-20", "action": "sell", "price": 4.2,
         "volume": 1000, "amount": 4200, "reason": "[趋势] 止盈"},
        {"symbol": "159915", "date": "2024-02-01", "action": "buy", "price": 2.0,
         "volume": 500, "amount": 1000, "reason": "无标签买入"},
        {"symbol": "159915", "date": "2024-02-10", "action": "sell", "price": 1.8,
         "volume": 500, "amount": 900, "reason": "无标签卖出"},
    ]
    attr = attribute_trades(trades)
    assert attr["趋势"]["n"] == 1
    assert attr["趋势"]["pnl"] == pytest.approx((4.2 - 4.0) * 1000)
    assert attr["趋势"]["win_rate"] == 1.0
    assert attr["其他"]["n"] == 1
    assert attr["其他"]["pnl"] == pytest.approx((1.8 - 2.0) * 500)
    assert attr["其他"]["win_rate"] == 0.0


def test_attribute_trades_unclosed_ignored():
    # 只有 buy 无 sell → 不计入
    trades = [
        {"symbol": "510300", "date": "2024-01-05", "action": "buy", "price": 4.0,
         "volume": 1000, "amount": 4000, "reason": "[趋势] 持有中"},
    ]
    assert attribute_trades(trades) == {}
    assert attribute_trades([]) == {}


# ---------- N-9 catch-up 补跑 ----------
def test_catch_up_dates(tmp_path):
    from run import _catch_up_dates

    db = Database(tmp_path / "t.db")
    acc = db.create_account("t", "rule", 100_000)
    db.add_snapshot(acc, datetime(2024, 1, 5), AccountState(100_000, 100_000))
    ds = FakeDataSource([1.0] * 40, base_date=D, trading_days={
        "2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11",
    })
    dates = _catch_up_dates(_settings(tmp_path / "t.db"), db, {"rule": RuleEngine()}, ds, datetime(2024, 1, 11))
    # 快照日次日(1/6 周六跳过) → 交易日 1/8、1/9、1/10（1/11 是"今天"，排除）
    assert [d.strftime("%Y-%m-%d") for d in dates] == ["2024-01-08", "2024-01-09", "2024-01-10"]


def test_catch_up_no_snapshot_noop(tmp_path):
    from run import _catch_up_dates

    db = Database(tmp_path / "t.db")
    db.create_account("t", "rule", 100_000)
    ds = FakeDataSource([1.0] * 40, base_date=D)
    assert _catch_up_dates(_settings(tmp_path / "t.db"), db, {"rule": RuleEngine()}, ds, datetime(2024, 1, 11)) == []


def test_catch_up_up_to_date_noop(tmp_path):
    from run import _catch_up_dates

    db = Database(tmp_path / "t.db")
    acc = db.create_account("t", "rule", 100_000)
    db.add_snapshot(acc, datetime(2024, 1, 10), AccountState(100_000, 100_000))
    ds = FakeDataSource([1.0] * 40, base_date=D, trading_days={"2024-01-10", "2024-01-11"})
    assert _catch_up_dates(_settings(tmp_path / "t.db"), db, {"rule": RuleEngine()}, ds, datetime(2024, 1, 11)) == []


def test_catch_up_integration_completes_missing_days(tmp_path):
    """连续缺失 2 个交易日后 catch-up 补齐快照且不重复成交"""
    from run import _catch_up_dates

    db_path = tmp_path / "t.db"
    db = Database(db_path)
    ds = FakeDataSource([1.0] * 40, base_date=D, trading_days={
        "2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10",
    })
    runner = BatchRunner(_settings(db_path), db, ds, {"rule": RuleEngine()})
    runner.run(datetime(2024, 1, 5))  # 只跑了一天
    acc = db.get_account_by_engine("rule")
    assert len(db.get_snapshots(acc["id"])) == 1

    dates = _catch_up_dates(_settings(db_path), db, {"rule": RuleEngine()}, ds, datetime(2024, 1, 10))
    for d in dates:
        runner.run(d)
    snaps = db.get_snapshots(acc["id"])
    assert [s["date"] for s in snaps] == ["2024-01-05", "2024-01-08", "2024-01-09"]
    # 幂等：再跑一遍不重复成交
    trades_before = len(db.get_trades(acc["id"]))
    for d in dates:
        runner.run(d)
    assert len(db.get_trades(acc["id"])) == trades_before


# ---------- N-10 告警通知 ----------
def test_check_alerts():
    assert "批处理失败" in check_alerts(False, "boom", [], 3)[0]
    assert "无成交" in check_alerts(True, "", [], 0, idle_days=5)[0]
    # 回撤触发
    snaps = [{"total_assets": 100_000}, {"total_assets": 82_000}]
    alerts = check_alerts(True, "", snaps, 3, max_drawdown=0.15)
    assert any("回撤" in a for a in alerts)
    # 正常不触发
    assert check_alerts(True, "", [{"total_assets": 100_000}], 3, idle_days=5) == []


def test_send_notify_failure_does_not_raise():
    # 无效地址 → 推送失败返回 False，且不抛异常（不阻塞主流程）
    assert send_notify("x", "") is False
    assert send_notify("x", "http://127.0.0.1:1/nope") is False


# ---------- N-11 主源失败缓存兜底 ----------
class BoomDS:
    def fetch_daily_bars(self, *a, **k):
        raise RuntimeError("source down")

    def is_trading_day(self, date):
        return True


def test_batch_falls_back_to_bars_cache(tmp_path):
    """行情源失败时用 bars 缓存兜底：缓存覆盖决策日 → 正常跑出快照，不抛错"""
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    bars = make_bars([1.0] * 25, start=D - timedelta(days=24))  # 最后一天 = D
    db.save_bars(bars)
    runner = BatchRunner(_settings(db_path), db, BoomDS(), {"rule": RuleEngine()})
    results = runner.run(D)
    assert "_warning" not in results  # 未走 bar_fetch_failed（缓存兜底成功）
    acc = db.get_account_by_engine("rule")
    assert len(db.get_snapshots(acc["id"])) == 1
