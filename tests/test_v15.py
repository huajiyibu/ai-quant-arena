"""v0.15 测试：PP-6 历史盈亏反馈（已平仓配对明细 + 提示词复盘节，默认关）+ PP-8 相关性辅助"""
from datetime import datetime

import pytest

from aitrader.attribution import closed_trade_pairs
from aitrader.engines.base import DecisionContext
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.models import AccountState, Trade

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


def _ctx(feedback_n=0, closed=None):
    bars = {"510300": []}
    return DecisionContext(
        D, AccountState(100_000, 100_000), bars, {"510300": "x"},
        recent_closed_trades=closed or [], feedback_n=feedback_n,
    )


def _trade(sym, d, action, price, reason="x"):
    return Trade(
        datetime(d.year, d.month, d.day), sym, "x", action, price, 1000, price * 1000, reason
    )


# ---------- closed_trade_pairs 兼容 dict / Trade 对象 ----------
def test_closed_trade_pairs_dict():
    trades = [
        {"symbol": "510300", "date": "2024-01-05", "action": "buy", "price": 4.0,
         "volume": 1000, "amount": 4000, "reason": "[趋势] 突破"},
        {"symbol": "510300", "date": "2024-01-20", "action": "sell", "price": 4.4,
         "volume": 1000, "amount": 4400, "reason": "[趋势] 止盈"},
        # 未平仓：不计入
        {"symbol": "159915", "date": "2024-02-01", "action": "buy", "price": 2.0,
         "volume": 500, "amount": 1000, "reason": "[政策] 利好"},
    ]
    pairs = closed_trade_pairs(trades, max_items=5)
    assert len(pairs) == 1
    p = pairs[0]
    assert p["symbol"] == "510300"
    assert p["buy_date"] == "2024-01-05" and p["sell_date"] == "2024-01-20"
    assert p["pnl_pct"] == pytest.approx(4.4 / 4.0 - 1)
    assert p["reason"] == "[趋势] 突破"


def test_closed_trade_pairs_trade_objects():
    trades = [
        _trade("510300", datetime(2024, 1, 5), "buy", 4.0, reason="[趋势] 突破"),
        _trade("510300", datetime(2024, 1, 20), "sell", 4.4, reason="[趋势] 止盈"),
    ]
    pairs = closed_trade_pairs(trades, max_items=5)
    assert len(pairs) == 1
    assert pairs[0]["pnl_pct"] == pytest.approx(0.1)
    assert pairs[0]["sell_date"] == "2024-01-20"


def test_closed_trade_pairs_max_items_and_order():
    trades = [
        {"symbol": "a", "date": "2024-01-02", "action": "buy", "price": 1.0, "volume": 1, "amount": 1, "reason": "x"},
        {"symbol": "a", "date": "2024-01-03", "action": "sell", "price": 1.1, "volume": 1, "amount": 1.1, "reason": "x"},
        {"symbol": "a", "date": "2024-01-04", "action": "buy", "price": 1.0, "volume": 1, "amount": 1, "reason": "x"},
        {"symbol": "a", "date": "2024-01-05", "action": "sell", "price": 1.2, "volume": 1, "amount": 1.2, "reason": "x"},
    ]
    pairs = closed_trade_pairs(trades, max_items=1)
    assert len(pairs) == 1
    assert pairs[0]["sell_date"] == "2024-01-05"  # 最近的一笔


# ---------- 提示词复盘节（feedback_n 控制） ----------
def test_prompt_includes_feedback_section():
    closed = [
        {"symbol": "510300", "buy_date": "2024-01-05", "buy_price": 4.0,
         "sell_date": "2024-01-20", "sell_price": 4.4, "pnl_pct": 0.1,
         "reason": "[趋势] 突破"},
    ]
    eng = DeepSeekEngine(
        api_key="test", http_client=FakeHttp('{"decisions":[]}'), feedback_n=5
    )
    prompt = eng._build_prompt(_ctx(feedback_n=5, closed=closed))
    assert "近期已平仓交易" in prompt
    assert "[趋势] 突破" in prompt
    assert "盈亏+10.0%" in prompt or "盈亏+10" in prompt


def test_prompt_no_feedback_when_zero():
    closed = [
        {"symbol": "510300", "buy_date": "2024-01-05", "buy_price": 4.0,
         "sell_date": "2024-01-20", "sell_price": 4.4, "pnl_pct": 0.1, "reason": "x"},
    ]
    eng = DeepSeekEngine(
        api_key="test", http_client=FakeHttp('{"decisions":[]}'), feedback_n=0
    )
    prompt = eng._build_prompt(_ctx(feedback_n=0, closed=closed))
    assert "近期已平仓交易" not in prompt


# ---------- PP-5 止损/止盈 ----------
from aitrader.config import RiskConfig
from aitrader.models import Position
from aitrader.portfolio import apply_stop_rules


def _pos_state():
    return AccountState(
        100_000, 100_000, {"510300": Position("510300", "沪深300ETF", 1000, 4.0, 4.0)}
    )


def test_apply_stop_loss_triggers_sell():
    risk = RiskConfig(stop_loss_pct=0.08)
    forced = apply_stop_rules(_pos_state(), {"510300": 3.6}, risk)  # 4.0*0.92=3.68
    assert len(forced) == 1 and forced[0].action == "sell"
    assert "[stop_loss]" in forced[0].reason


def test_apply_take_profit_triggers_sell():
    risk = RiskConfig(take_profit_pct=0.2)
    forced = apply_stop_rules(_pos_state(), {"510300": 4.9}, risk)  # 4.0*1.2=4.8
    assert len(forced) == 1 and forced[0].action == "sell"
    assert "[take_profit]" in forced[0].reason


def test_apply_stop_no_trigger():
    risk = RiskConfig(stop_loss_pct=0.08, take_profit_pct=0.2)
    assert apply_stop_rules(_pos_state(), {"510300": 4.2}, risk) == []
    assert apply_stop_rules(_pos_state(), {"510300": 4.2}, RiskConfig()) == []


def test_apply_stop_no_position():
    assert (
        apply_stop_rules(
            AccountState(100_000, 100_000), {"510300": 3.6}, RiskConfig(stop_loss_pct=0.08)
        )
        == []
    )
