"""规则引擎（双均线）测试"""
from datetime import datetime

from aitrader.engines.base import DecisionContext
from aitrader.engines.rule import RuleEngine
from aitrader.models import AccountState, Position

from helpers import make_bars

NAMES = {"510300": "沪深300ETF"}
D = datetime(2024, 1, 1)


def test_buy_on_uptrend():
    bars = {"510300": make_bars(list(range(100, 125)))}  # 持续上涨
    ctx = DecisionContext(D, AccountState(100_000, 100_000), bars, NAMES)
    decisions = RuleEngine().decide(ctx).decisions
    assert any(d.action == "buy" for d in decisions)


def test_sell_on_downtrend():
    bars = {"510300": make_bars(list(range(125, 100, -1)))}  # 持续下跌
    state = AccountState(
        100_000, 0,
        positions={"510300": Position("510300", "沪深300ETF", 100, 4.0, 4.0)},
    )
    ctx = DecisionContext(D, state, bars, NAMES)
    decisions = RuleEngine().decide(ctx).decisions
    assert any(d.action == "sell" for d in decisions)


def test_no_action_when_flat():
    bars = {"510300": make_bars([5.0] * 25)}  # 横盘
    ctx = DecisionContext(D, AccountState(100_000, 100_000), bars, NAMES)
    decisions = RuleEngine().decide(ctx).decisions
    assert decisions == []


def test_ignores_insufficient_bars():
    bars = {"510300": make_bars([1.0] * 5)}  # 不足 20 根
    ctx = DecisionContext(D, AccountState(100_000, 100_000), bars, NAMES)
    decisions = RuleEngine().decide(ctx).decisions
    assert decisions == []
