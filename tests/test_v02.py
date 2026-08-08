"""v0.2 改进测试：引擎过滤、行情失败保护、解析容错、网络重试"""
import json
from datetime import datetime

import pytest

from aitrader.batch import BatchRunner
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.engines.rule import RuleEngine
from aitrader.util import retry_call
from run import select_daily_engines

D = datetime(2024, 1, 1)


def _engines():
    return {"rule": object(), "ai": object(), "ai_policy": object()}


# ---------------------------------------------------------------------------
# 2.1 --engine 每日批处理过滤（含 ai_policy）
# ---------------------------------------------------------------------------
def test_select_daily_both_keeps_all():
    e, w = select_daily_engines("both", _engines())
    assert set(e) == {"rule", "ai", "ai_policy"}
    assert w == []


def test_select_daily_ai():
    e, _ = select_daily_engines("ai", _engines())
    assert set(e) == {"ai"}


def test_select_daily_rule():
    e, _ = select_daily_engines("rule", _engines())
    assert set(e) == {"rule"}


def test_select_daily_ai_policy():
    e, _ = select_daily_engines("ai_policy", _engines())
    assert set(e) == {"ai_policy"}


def test_select_daily_ai_policy_missing_falls_back():
    e, w = select_daily_engines("ai_policy", {"rule": object()})
    assert set(e) == {"rule"}
    assert any("AI·政策版不可用" in x for x in w)


# ---------------------------------------------------------------------------
# 2.2 行情拉取失败时当日跳过交易（防静默坏数据）
# ---------------------------------------------------------------------------
class _EmptyDataSource:
    name = "empty"

    def fetch_daily_bars(self, symbol, days, exchange="SH", end_date=None):
        return []

    def is_trading_day(self, date):
        return True


def test_batch_skips_trading_when_bars_missing(tmp_path):
    db_path = tmp_path / "t.db"
    settings = Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )
    db = Database(db_path)
    runner = BatchRunner(settings, db, _EmptyDataSource(), {"rule": RuleEngine()})
    results = runner.run(D)
    assert "_warning" in results
    assert "bar_fetch_failed" in results["_warning"]
    # 未创建账户、无快照 → 没有虚假成交
    assert db.get_account_by_engine("rule") is None


# ---------------------------------------------------------------------------
# 2.3 DeepSeek 解析容错
# ---------------------------------------------------------------------------
def test_parse_amount_as_string():
    content = json.dumps(
        {"decisions": [{"symbol": "510300", "action": "buy", "amount": "50000", "reason": "x"}]}
    )
    ds = DeepSeekEngine("sk-test")._parse(content)
    assert ds[0].amount == 50000.0


def test_parse_bad_item_isolated_from_good():
    content = json.dumps(
        {
            "decisions": [
                {"symbol": "510300", "action": "buy", "amount": "5万", "reason": "bad"},
                {"symbol": "510300", "action": "buy", "amount": 30000, "reason": "good"},
            ]
        }
    )
    ds = DeepSeekEngine("sk-test")._parse(content)
    assert ds[0].valid is False
    assert ds[0].validation == "parse_error"
    assert ds[1].valid is True
    assert ds[1].amount == 30000.0


# ---------------------------------------------------------------------------
# 2.4 网络重试 / 退避
# ---------------------------------------------------------------------------
def test_retry_success_after_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("boom")
        return "ok"

    assert retry_call(flaky, retries=3, base_delay=0) == "ok"
    assert calls["n"] == 3


def test_retry_gives_up():
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        retry_call(always_fail, retries=2, base_delay=0)
    assert calls["n"] == 2
