"""账本与交易执行：纯函数（对应 HLD §2 portfolio）。

AccountState 不可变，apply_trade / refresh_prices 返回新状态，便于测试与审计。
"""
from __future__ import annotations

from .models import AccountState, Position, Trade


def apply_trade(state: AccountState, trade: Trade) -> AccountState:
    """执行一笔成交，返回新账本状态（不修改入参）。

    Raises:
        ValueError: 重复买入 / 卖出不存在的持仓 / 未知操作
    """
    if trade.action == "buy":
        if trade.symbol in state.positions:
            raise ValueError(f"重复买入: {trade.symbol}")
        positions = dict(state.positions)
        positions[trade.symbol] = Position(
            symbol=trade.symbol,
            name=trade.name,
            volume=trade.volume,
            cost_price=round(trade.price, 4),
            last_price=round(trade.price, 4),
        )
        return AccountState(
            initial_capital=state.initial_capital,
            cash=round(state.cash - trade.amount, 2),
            positions=positions,
        )

    if trade.action == "sell":
        pos = state.positions.get(trade.symbol)
        if pos is None:
            raise ValueError(f"卖出不存在的持仓: {trade.symbol}")
        if trade.volume != pos.volume:
            raise ValueError("当前仅支持整仓卖出")
        positions = dict(state.positions)
        del positions[trade.symbol]
        return AccountState(
            initial_capital=state.initial_capital,
            cash=round(state.cash + trade.amount, 2),
            positions=positions,
        )

    raise ValueError(f"未知操作: {trade.action}")


def refresh_prices(state: AccountState, prices: dict[str, float]) -> AccountState:
    """用最新收盘价刷新持仓现价，返回新状态"""
    positions = {
        sym: Position(
            symbol=p.symbol,
            name=p.name,
            volume=p.volume,
            cost_price=p.cost_price,
            last_price=prices.get(sym, p.last_price),
        )
        for sym, p in state.positions.items()
    }
    return AccountState(
        initial_capital=state.initial_capital,
        cash=state.cash,
        positions=positions,
    )


def execute_decisions(
    state: AccountState,
    decisions: list[Decision],
    prices: dict[str, float],
    names: dict[str, str],
    risk: RiskConfig,
    trade_date: datetime,
) -> tuple[AccountState, list[Trade]]:
    """依次执行决策（含风控校验），返回 (新状态, 成交流水)。纯函数，可单测。

    - buy 经 validate_buy 校验与金额调整；被拒则跳过
    - sell 仅支持整仓卖出
    """
    from .risk import validate_buy

    trades: list[Trade] = []
    already_bought_today: float = 0.0

    for d in decisions:
        if not d.valid:
            continue  # 语义校验未通过：跳过执行（已在决策表留痕）
        price = prices.get(d.symbol)
        if price is None or d.symbol not in names:
            continue
        name = names[d.symbol]

        if d.action == "buy":
            adj = validate_buy(
                state,
                requested_amount=d.amount,
                price=price,
                risk=risk,
                total_assets=state.total_assets,
                already_bought_today=already_bought_today,
                already_holding=d.symbol in state.positions,
            )
            if not adj.allowed:
                continue
            trade = Trade(trade_date, d.symbol, name, "buy", price, adj.volume, adj.cost, d.reason)
            state = apply_trade(state, trade)
            already_bought_today += adj.cost
            trades.append(trade)

        elif d.action == "sell":
            pos = state.positions.get(d.symbol)
            if pos is None:
                continue
            proceeds = round(pos.volume * price * (1 - risk.commission_rate), 2)
            trade = Trade(trade_date, d.symbol, name, "sell", price, pos.volume, proceeds, d.reason)
            state = apply_trade(state, trade)
            trades.append(trade)

    return state, trades
