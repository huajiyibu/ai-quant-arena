"""决策引擎抽象层。

设计要点（对应 HLD §2 engines/base）：
- 统一接口 DecisionEngine.decide(ctx) -> EngineResult
- EngineResult 携带 prompt 与 raw_output，供上层完整留痕（SRS NFR2）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..models import AccountState, Bar, Decision


@dataclass
class DecisionContext:
    """喂给引擎的决策上下文"""

    date: datetime
    account: AccountState
    bars: dict[str, list[Bar]]
    symbol_names: dict[str, str]
    lookback: int = 20
    policy_text: str = ""


@dataclass
class EngineResult:
    """引擎输出：决策列表 + 完整输入输出留痕"""

    decisions: list[Decision] = field(default_factory=list)
    prompt: str = ""
    raw_output: str = ""


class DecisionEngine:
    """决策引擎基类（新增引擎继承此类即可接入系统）"""

    name: str = "base"

    def decide(self, ctx: DecisionContext) -> EngineResult:
        raise NotImplementedError
