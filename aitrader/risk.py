"""风控模块：纯函数，无 IO（对应 HLD §2 risk，NFR1 可测试性核心）。

所有买入在成交前都必须经过 validate_buy 校验与金额调整。
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import RiskConfig
from .models import AccountState

LOT_SIZE: int = 100  # A股 1 手 = 100 股


@dataclass
class BuyAdjustment:
    """买入风控结果"""

    allowed: bool
    volume: int = 0
    cost: float = 0.0
    reason: str = ""


def validate_buy(
    state: AccountState,
    requested_amount: float,
    price: float,
    risk: RiskConfig,
    total_assets: float,
    already_bought_today: float = 0.0,
    already_holding: bool = False,
) -> BuyAdjustment:
    """校验并调整一笔买入请求。

    约束（SRS FR6）：
    1. 已持仓则拒绝（不重复加仓）
    2. 单笔买入 ≤ 总资产 × max_position_pct
    3. 单日累计买入 ≤ 总资产 × max_daily_buy_pct
    4. 不超过可用现金（含手续费）
    5. 成交量按 100 股整数向下取整，不足 1 手则拒绝
    """
    if already_holding:
        return BuyAdjustment(allowed=False, reason="已持仓，不重复买入")

    cap_cash = state.cash / (1 + risk.commission_rate)
    amount = min(
        requested_amount,
        total_assets * risk.max_position_pct,
        total_assets * risk.max_daily_buy_pct - already_bought_today,
        cap_cash,
    )
    if amount < price * LOT_SIZE:
        return BuyAdjustment(
            allowed=False,
            reason=f"资金不足 1 手（约 {price * LOT_SIZE:,.0f} 元）",
        )

    volume = int(amount // (price * LOT_SIZE)) * LOT_SIZE
    cost = round(volume * price * (1 + risk.commission_rate), 2)
    return BuyAdjustment(allowed=True, volume=volume, cost=cost, reason="通过")
