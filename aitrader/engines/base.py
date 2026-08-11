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
    # N-1：特征计算专用复权 K 线（估值/成交/价格展示仍用 bars 原始价）。
    # 为 None 时引擎回退用 bars（回测全链路 hfq，bars 本身即复权价，天然一致）。
    adjusted_bars: dict[str, list[Bar]] | None = None
    # PP-6：历史盈亏反馈（近 N 笔已平仓交易明细，喂模型复盘；feedback_n=0 关闭）
    recent_closed_trades: list[dict] = field(default_factory=list)
    feedback_n: int = 0


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
