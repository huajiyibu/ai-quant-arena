"""账本与交易执行测试"""
from datetime import datetime

import pytest

from aitrader.config import RiskConfig
from aitrader.models import AccountState, Decision, Position, Trade
from aitrader.portfolio import apply_trade, execute_decisions, refresh_prices

RISK = RiskConfig(max_position_pct=0.3, max_daily_buy_pct=0.5, commission_rate=0.00025)
D = datetime(2024, 1, 1)


def test_buy_updates_state_immutably():
    state = AccountState(100_000, 100_000)
    trade = Trade(D, "510300", "沪深300ETF", "buy", 4.0, 100, 400.10, "测试")
    new = apply_trade(state, trade)
    assert new.cash == pytest.approx(99_599.90)
    assert "510300" in new.positions
    # 原状态不变（不可变）
    assert state.cash == 100_000
    assert "510300" not in state.positions


def test_sell_clears_position():
    state = AccountState(
        100_000, 99_000,
        positions={"510300": Position("510300", "沪深300ETF", 100, 4.0, 4.0)},
    )
    trade = Trade(D, "510300", "沪深300ETF", "sell", 4.1, 100, 409.90, "测试")
    new = apply_trade(state, trade)
    assert new.cash == pytest.approx(99_409.90)
    assert "510300" not in new.positions


def test_duplicate_buy_raises():
    state = AccountState(
        100_000, 50_000,
        positions={"510300": Position("510300", "x", 100, 4.0, 4.0)},
    )
    trade = Trade(D, "510300", "x", "buy", 4.0, 100, 400.0, "")
    with pytest.raises(ValueError):
        apply_trade(state, trade)


def test_sell_missing_raises():
    state = AccountState(100_000, 100_000)
    trade = Trade(D, "510300", "x", "sell", 4.0, 100, 400.0, "")
    with pytest.raises(ValueError):
        apply_trade(state, trade)


def test_refresh_prices():
    state = AccountState(
        100_000, 99_000,
        positions={"510300": Position("510300", "x", 100, 4.0, 4.0)},
    )
    new = refresh_prices(state, {"510300": 4.5})
    assert new.positions["510300"].last_price == 4.5
    assert new.positions["510300"].unrealized_pnl == pytest.approx(50.0)


def test_execute_decisions_buy_and_sell_flow():
    state = AccountState(100_000, 100_000)
    decisions = [
        Decision("510300", "buy", amount=30_000, reason="买"),
    ]
    names = {"510300": "沪深300ETF"}
    new, trades = execute_decisions(
        state, decisions, {"510300": 4.0}, names, RISK, D
    )
    assert len(trades) == 1
    assert trades[0].action == "buy"
    assert "510300" in new.positions

    # 再跑一天：死叉卖出
    decisions2 = [Decision("510300", "sell", reason="卖")]
    new2, trades2 = execute_decisions(
        new, decisions2, {"510300": 3.8}, names, RISK, D
    )
    assert len(trades2) == 1
    assert trades2[0].action == "sell"
    assert "510300" not in new2.positions
