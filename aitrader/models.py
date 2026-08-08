"""领域模型：行情 Bar、持仓 Position、账本状态 AccountState、决策 Decision、成交 Trade。

设计要点（对应 HLD §1）：
- 账本状态 AccountState 为不可变对象，账本变更通过 portfolio.apply_trade 返回新状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Action = Literal["buy", "sell", "hold"]
EngineType = Literal["ai", "rule", "ai_policy"]


@dataclass(frozen=True)
class Bar:
    """一根 K 线"""

    symbol: str
    datetime: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Position:
    """一个持仓"""

    symbol: str
    name: str
    volume: int
    cost_price: float
    last_price: float

    @property
    def market_value(self) -> float:
        return self.last_price * self.volume

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.cost_price) * self.volume


@dataclass
class AccountState:
    """不可变账本状态"""

    initial_capital: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def total_assets(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    @property
    def total_pnl(self) -> float:
        return self.total_assets - self.initial_capital


@dataclass
class Decision:
    """引擎输出的操作指令"""

    symbol: str
    action: Action
    amount: float = 0.0
    reason: str = ""
    confidence: float = 0.5  # 模型对该信号带来正收益的信心（0~1，PP-4）
    fallback: bool = False
    valid: bool = True      # 语义校验是否通过（false 时不执行，仅留痕）
    validation: str = ""   # 校验结果："ok" 或具体原因


@dataclass
class Trade:
    """一笔已执行的成交（审计流水）"""

    date: datetime
    symbol: str
    name: str
    action: Literal["buy", "sell"]
    price: float
    volume: int
    amount: float
    reason: str
