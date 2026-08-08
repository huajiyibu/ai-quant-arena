"""风控模块测试：买入校验与金额调整"""
import pytest

from aitrader.config import RiskConfig
from aitrader.models import AccountState, Position
from aitrader.risk import LOT_SIZE, validate_buy

RISK = RiskConfig(max_position_pct=0.3, max_daily_buy_pct=0.5, commission_rate=0.00025)


def test_buy_basic_adjustment():
    state = AccountState(100_000, 100_000)
    adj = validate_buy(state, 30_000, price=4.0, risk=RISK, total_assets=100_000)
    assert adj.allowed
    assert adj.volume % LOT_SIZE == 0
    # 请求 30000，受单笔上限 100000*0.3=30000 约束 → 7500 股，含手续费成本 30007.5
    assert adj.volume == 7500
    assert adj.cost == pytest.approx(30007.50, abs=0.01)


def test_reject_when_already_holding():
    state = AccountState(
        100_000, 50_000,
        positions={"510300": Position("510300", "x", 100, 4.0, 4.0)},
    )
    adj = validate_buy(
        state, 30_000, price=4.0, risk=RISK, total_assets=100_000,
        already_holding=True,
    )
    assert not adj.allowed


def test_reject_less_than_one_lot():
    state = AccountState(100_000, 1000)  # 现金不足以买 1 手
    adj = validate_buy(state, 100_000, price=40.0, risk=RISK, total_assets=100_000)
    assert not adj.allowed


def test_daily_buy_cap():
    """单日累计买入不得超过总资产 50%"""
    state = AccountState(100_000, 100_000)
    adj = validate_buy(
        state, 100_000, price=4.0, risk=RISK,
        total_assets=100_000, already_bought_today=45_000,
    )
    assert adj.allowed
    # 剩余额度 = 50000 - 45000 = 5000 元 → 5000//400=12 手 = 1200 股
    assert adj.volume == 1200
    assert adj.cost == pytest.approx(4801.20, abs=0.01)


def test_position_cap_respects_total_assets():
    """单笔不得超过总资产 30%（即便现金充裕）"""
    state = AccountState(100_000, 100_000)
    adj = validate_buy(state, 100_000, price=4.0, risk=RISK, total_assets=100_000)
    assert adj.volume * 4.0 <= 100_000 * 0.3
