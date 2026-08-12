"""决策归因复盘（N-5）：把自由文本 reason 结构化并按标签聚合盈亏。

reason 以 [标签] 开头（趋势/回调/政策/超买/超卖/其他）；未带标签归入"其他"。
按"买入 → 该标的其后首笔卖出"配对计算该笔盈亏，按买入 reason 的标签聚合。
纯函数、零 IO，供日报/周报做"哪类理由的买入后来赚/亏"自动复盘。
"""
from __future__ import annotations

import re

TAG_PATTERN = re.compile(r"^\[([^\]\[]+)\]")


def parse_tag(reason: str | None) -> str:
    """从 reason 提取 [标签]；缺失/空/非法统一归入 其他（不崩溃）。"""
    m = TAG_PATTERN.match((reason or "").strip())
    return m.group(1).strip() if m else "其他"


def attribute_trades(trades: list[dict], commission_rate: float = 0.00025) -> dict[str, dict]:
    """按 reason 标签聚合已配对买入交易的盈亏（P1-1：净口径，扣双边佣金）。

    Args:
        trades: database.get_trades 返回的记录，含 date/symbol/action/price/volume/amount/reason
        commission_rate: 双边佣金率（默认 0.025% 与 config 一致）

    Returns:
        {标签: {"n": 笔数, "pnl": 累计盈亏, "win": 盈利笔数, "win_rate": 胜率}}
        未平仓的买入不计入；空 trades 返回 {}。
    """
    by_sym: dict[str, list] = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t)

    pairs: list[tuple[str, float]] = []  # (tag, pnl)
    for ts in by_sym.values():
        sorted_ts = sorted(ts, key=lambda x: x["date"])
        open_buys: list = []
        for t in sorted_ts:
            if t["action"] == "buy":
                open_buys.append(t)
            elif t["action"] == "sell" and open_buys:
                b = open_buys.pop(0)  # FIFO 配对
                vol = min(b["volume"], t["volume"])
                # P1-1：净口径（卖价扣佣 - 买价含佣）
                pnl = (
                    t["price"] * (1 - commission_rate)
                    - b["price"] * (1 + commission_rate)
                ) * vol
                pairs.append((parse_tag(b.get("reason")), pnl))

    agg: dict[str, dict] = {}
    for tag, pnl in pairs:
        a = agg.setdefault(tag, {"n": 0, "pnl": 0.0, "win": 0, "win_rate": 0.0})
        a["n"] += 1
        a["pnl"] += pnl
        if pnl > 0:
            a["win"] += 1
    for a in agg.values():
        a["win_rate"] = a["win"] / a["n"] if a["n"] else 0.0
    return agg


def closed_trade_pairs(trades: list, max_items: int = 5, commission_rate: float = 0.00025) -> list[dict]:
    """返回最近已平仓配对明细（PP-6 历史盈亏反馈用）。

    Args:
        trades: database.get_trades（dict）或 execute_decisions 返回（Trade 对象）
        max_items: 返回最近几笔（按 sell 日期倒序）

    Returns:
        [{"symbol", "buy_date", "buy_price", "sell_date", "sell_price", "pnl_pct", "reason"}, ...]
        只含实际成交的 buy→sell 配对；未平仓不计入。
    """

    def _f(t, key):
        return t[key] if isinstance(t, dict) else getattr(t, key, "")

    def _d(v):
        return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)

    by_sym: dict[str, list] = {}
    for t in trades:
        by_sym.setdefault(_f(t, "symbol"), []).append(t)

    pairs: list[dict] = []
    for ts in by_sym.values():
        sorted_ts = sorted(ts, key=lambda x: _f(x, "date"))
        open_buys: list = []
        for t in sorted_ts:
            if _f(t, "action") == "buy":
                open_buys.append(t)
            elif _f(t, "action") == "sell" and open_buys:
                b = open_buys.pop(0)  # FIFO 配对
                b_price = _f(b, "price")
                s_price = _f(t, "price")
                # P1-1：净口径（扣双边佣金），与 attribute_trades 一致
                net_sell = s_price * (1 - commission_rate)
                net_buy = b_price * (1 + commission_rate)
                pnl_pct = net_sell / net_buy - 1 if net_buy > 0 else 0.0
                pairs.append(
                    {
                        "symbol": _f(t, "symbol"),
                        "buy_date": _d(_f(b, "date")),
                        "buy_price": b_price,
                        "sell_date": _d(_f(t, "date")),
                        "sell_price": s_price,
                        "pnl_pct": pnl_pct,
                        "reason": _f(b, "reason"),
                    }
                )
    pairs.sort(key=lambda p: p["sell_date"], reverse=True)
    return pairs[:max_items]
