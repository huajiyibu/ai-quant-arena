"""walk-forward 回测模块：用历史行情逐日回放单引擎，输出绩效指标与基准对比。

设计要点：
- 独立数据库跑回测（默认 data/backtest.db），不污染每日仿真账本
- 每次回测前重置账户，保证结果可重复（AI 引擎由响应缓存避免重复计费）
- 逐日只喂"截至当日"的行情（配合 P0 修复，无前视偏差）
- 指标与基准计算均为纯函数，可单测
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .config import Settings
from .database import Database
from .datasource import DataSource
from .engines.base import DecisionContext, DecisionEngine, EngineResult
from .models import AccountState, Bar, Decision
from .portfolio import execute_decisions, refresh_prices

logger = logging.getLogger(__name__)


@dataclass
class BacktestMetrics:
    """回测绩效指标"""

    initial_capital: float
    final_assets: float
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float
    profit_factor: float
    turnover: float
    trade_count: int
    buy_count: int
    sell_count: int


def compute_metrics(
    snapshots: list[dict], trades: list[dict], initial_capital: float
) -> BacktestMetrics:
    """从每日净值快照与成交流水计算绩效指标（纯函数，可单测）。

    - 年化：按交易日数折算（252 个交易日/年）
    - 夏普：日收益均值 / 标准差 × sqrt(252)
    - 胜率/盈亏比：按 (symbol, buy→sell) 配对计算已实现盈亏
    """
    n = len(snapshots)
    if n == 0:
        return BacktestMetrics(
            initial_capital=initial_capital,
            final_assets=initial_capital,
            total_return=0.0,
            annual_return=0.0,
            max_drawdown=0.0,
            sharpe=0.0,
            win_rate=0.0,
            profit_factor=0.0,
            turnover=0.0,
            trade_count=0,
            buy_count=0,
            sell_count=0,
        )

    final_assets = snapshots[-1]["total_assets"]
    total_return = final_assets / initial_capital - 1 if initial_capital else 0.0
    annual_return = (
        (final_assets / initial_capital) ** (252 / n) - 1
        if initial_capital > 0 and final_assets > 0
        else 0.0
    )

    # 最大回撤
    peak = float("-inf")
    max_drawdown = 0.0
    for s in snapshots:
        v = s["total_assets"]
        if v > peak:
            peak = v
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - v) / peak)

    # 夏普（日收益）
    rets: list[float] = []
    prev = None
    for s in snapshots:
        v = s["total_assets"]
        if prev is not None and prev > 0:
            rets.append(v / prev - 1)
        prev = v
    sharpe = 0.0
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        std = var**0.5
        if std > 0:
            sharpe = mean / std * (252**0.5)

    # 胜率 / 盈亏比（按 symbol 配对 buy→sell）
    buys: dict[str, float] = {}
    wins = losses = 0
    gross_profit = gross_loss = 0.0
    for t in trades:
        if t["action"] == "buy":
            buys[t["symbol"]] = t["amount"]
        elif t["action"] == "sell":
            pnl = t["amount"] - buys.get(t["symbol"], 0.0)
            if pnl >= 0:
                wins += 1
                gross_profit += pnl
            else:
                losses += 1
                gross_loss += abs(pnl)
    decided = wins + losses
    win_rate = wins / decided if decided else 0.0
    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else (gross_profit if gross_profit > 0 else 0.0)
    )

    # 换手率（双边口径）= 买卖成交额合计 ÷ 平均总资产
    avg_assets = sum(s["total_assets"] for s in snapshots) / n
    turnover = sum(t["amount"] for t in trades) / avg_assets if avg_assets else 0.0

    buy_count = sum(1 for t in trades if t["action"] == "buy")
    sell_count = sum(1 for t in trades if t["action"] == "sell")

    return BacktestMetrics(
        initial_capital=initial_capital,
        final_assets=final_assets,
        total_return=total_return,
        annual_return=annual_return,
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        win_rate=win_rate,
        profit_factor=profit_factor,
        turnover=turnover,
        trade_count=len(trades),
        buy_count=buy_count,
        sell_count=sell_count,
    )


def compute_benchmark(
    bars: list[Bar], initial_capital: float, commission_rate: float = 0.0
) -> list[dict]:
    """买入持有基准：以区间首日收盘价全额买入（扣单边买入佣金），逐日按收盘价折算资产。

    Returns:
        [{"date": "YYYY-MM-DD", "assets": float}, ...]
    """
    if not bars:
        return []
    first_close = bars[0].close
    if first_close <= 0:
        return []
    factor = 1.0 - commission_rate  # 买入一次成本（PP-1：基准与策略同口径）
    return [
        {
            "date": b.datetime.strftime("%Y-%m-%d"),
            "assets": initial_capital * factor * (b.close / first_close),
        }
        for b in bars
    ]


class Backtester:
    """walk-forward 回测器：用历史行情逐日回放单个引擎，输出快照/成交/指标。

    使用独立数据库（不污染每日仿真账本）；每次运行前重置账户，保证可重复。
    回测区间内的宏观政策不注入（避免用当下新闻评价历史决策）。
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        data_source: DataSource,
        engine: DecisionEngine,
        engine_type: str,
        start_date: datetime,
        end_date: datetime,
        record_decisions: bool = False,
        fill_mode: str = "close",
    ) -> None:
        self.settings = settings
        self.db = db
        self.data_source = data_source
        self.engine = engine
        self.engine_type = engine_type
        self.start_date = start_date
        self.end_date = end_date
        self.record_decisions = record_decisions
        self.fill_mode = fill_mode

    def run(self) -> dict:
        """逐日回放，返回 {account_id, snapshots, trades, metrics}"""
        lookback = self.settings.lookback_days
        window = lookback + 5

        # 取数窗口：按区间动态计算（覆盖 start 前的 lookback 预热 + 区间内交易日），
        # 避免硬编码 600 根导致长区间（>2.5 年）起点数据不足
        span_days = (self.end_date - self.start_date).days
        fetch_days = min(max(int(span_days * 1.5) + lookback + 20, lookback + 20), 5000)

        # 1. 拉取截至 end_date 的完整历史（每标的）
        full: dict[str, list[Bar]] = {}
        for symbol, cfg in self.settings.symbols.items():
            try:
                full[symbol] = self.data_source.fetch_daily_bars(
                    symbol, fetch_days, cfg.exchange, end_date=self.end_date
                )
            except Exception:
                logger.exception("回测行情获取失败: %s", symbol)
                full[symbol] = []

        # 交易日 = 各标的有行情的日期（升序去重）
        dates = sorted({b.datetime.date() for bars in full.values() for b in bars})
        dates = [d for d in dates if self.start_date.date() <= d <= self.end_date.date()]
        if not dates:
            logger.warning(
                "回测区间 %s~%s 无行情", self.start_date.date(), self.end_date.date()
            )
            return {"account_id": None, "snapshots": [], "trades": [], "metrics": None}

        # 2. 建账户并重置（每次回测从初始资金重新开始）
        account = self.db.get_account_by_engine(self.engine_type)
        if account is None:
            account_id = self.db.create_account(
                f"{self.engine.name}引擎·回测",
                self.engine_type,
                self.settings.initial_capital,
            )
        else:
            account_id = account["id"]
        self.db.reset_account(account_id)
        state = self.db.load_state(account_id)
        assert state is not None, "回测账本状态缺失"

        names = self.settings.symbol_names

        import bisect

        # 预建每标的日期索引，把每交易日过滤从 O(N) 降到 O(log N)（修复长区间回测性能）
        bar_dates: dict[str, list] = {
            sym: [b.datetime.date() for b in bars] for sym, bars in full.items()
        }

        # 3. 逐日回放
        for d in dates:
            day = datetime.combine(d, datetime.min.time())
            # 只取截至当日的行情（配合 P0-1 修复，杜绝前视偏差）
            bars_map: dict[str, list[Bar]] = {}
            fill_prices: dict[str, float] = {}
            for sym, bars in full.items():
                idx = bisect.bisect_right(bar_dates[sym], d)
                if idx:
                    bars_map[sym] = bars[max(0, idx - window):idx]
                # PP-1 next_open 成交假设：下一根 K 线（下一可交易日）的开盘价作成交价
                if self.fill_mode == "next_open" and idx < len(bars):
                    fill_prices[sym] = bars[idx].open

            prices = {sym: bars[-1].close for sym, bars in bars_map.items()}
            state = refresh_prices(state, prices)

            ctx = DecisionContext(
                date=day,
                account=state,
                bars=bars_map,
                symbol_names=names,
                lookback=lookback,
                policy_text="",  # 回测不注入当下政策，保证评价一致性
            )
            try:
                result = self.engine.decide(ctx)
            except Exception as exc:
                logger.warning("回测引擎 %s 第 %s 日决策失败: %s", self.engine.name, d, exc)
                result = EngineResult(
                    decisions=[
                        Decision(
                            symbol="",
                            action="hold",
                            reason=f"回测引擎异常: {exc}",
                            fallback=True,
                        )
                    ]
                )

            new_state, trades, _ = execute_decisions(
                state,
                result.decisions,
                prices,
                names,
                self.settings.risk,
                day,
                fill_prices=fill_prices or None,
            )
            for t in trades:
                self.db.add_trade(account_id, t)
            if self.record_decisions:
                for dec in result.decisions:
                    self.db.add_decision(
                        account_id,
                        day,
                        self.engine_type,
                        dec,
                        prompt=result.prompt,
                        raw_output=result.raw_output,
                    )

            state = new_state
            self.db.save_state(account_id, state)
            self.db.add_snapshot(account_id, day, state)

        snapshots = self.db.get_snapshots(account_id)
        trades = self.db.get_trades(account_id)
        metrics = compute_metrics(snapshots, trades, self.settings.initial_capital)
        return {
            "account_id": account_id,
            "snapshots": snapshots,
            "trades": trades,
            "metrics": metrics,
        }
