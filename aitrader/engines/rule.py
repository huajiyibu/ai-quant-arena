"""内置双均线规则引擎：作为基线，与 AI 引擎对照。

逻辑：快线(5日均线) > 慢线(20日均线) 且空仓 → 买入；快线 < 慢线 且持仓 → 清仓。
"""
from __future__ import annotations

from ..models import Decision
from .base import DecisionContext, DecisionEngine, EngineResult


class RuleEngine(DecisionEngine):
    """双均线基线引擎"""

    name: str = "rule"

    def __init__(self, fast_window: int = 5, slow_window: int = 20) -> None:
        self.fast_window = fast_window
        self.slow_window = slow_window

    def decide(self, ctx: DecisionContext) -> EngineResult:
        decisions: list[Decision] = []
        for symbol, bars in ctx.bars.items():
            if len(bars) < self.slow_window:
                continue
            closes = [b.close for b in bars]
            ma_fast = sum(closes[-self.fast_window:]) / self.fast_window
            ma_slow = sum(closes[-self.slow_window:]) / self.slow_window
            holding = symbol in ctx.account.positions

            if holding and ma_fast < ma_slow:
                decisions.append(
                    Decision(
                        symbol=symbol,
                        action="sell",
                        reason=f"规则: 快线{ma_fast:.2f}<慢线{ma_slow:.2f}, 清仓",
                    )
                )
            elif not holding and ma_fast > ma_slow:
                decisions.append(
                    Decision(
                        symbol=symbol,
                        action="buy",
                        amount=ctx.account.cash * 0.3,
                        reason=f"规则: 快线{ma_fast:.2f}>慢线{ma_slow:.2f}, 买入",
                    )
                )
        return EngineResult(decisions=decisions)
