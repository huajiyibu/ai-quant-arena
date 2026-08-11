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


def attribute_trades(trades: list[dict]) -> dict[str, dict]:
    """按 reason 标签聚合已配对买入交易的盈亏。

    Args:
        trades: database.get_trades 返回的记录，含 date/symbol/action/price/volume/amount/reason

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
                pnl = (t["price"] - b["price"]) * vol
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
