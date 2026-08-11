"""PP-8 标的池相关性分析：对配置池任意两标的计算 20 日收益滚动相关性，输出矩阵。

用途：判断池内标的是否高度冗余（>0.7 视为"分散度低"），用数据决定是否需扩充。
不烧 API：只用历史行情（新浪），纯本地计算。
"""
from __future__ import annotations

import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aitrader.config import load_settings
from aitrader.datasource import AkShareDataSource


def aligned_returns(bars_map: dict[str, list]) -> dict[str, list[float]]:
    """把各标的按日期对齐（求共同日期），输出每标的日收益率序列（复权价计算）。"""
    date_to_close: dict[str, dict[str, float]] = {}
    for sym, bars in bars_map.items():
        for b in bars:
            date_to_close.setdefault(b.datetime.date(), {})[sym] = b.close
    common_dates = sorted(
        d for d, m in date_to_close.items()
        if all(sym in m for sym in bars_map)
    )
    out: dict[str, list[float]] = {s: [] for s in bars_map}
    for i in range(1, len(common_dates)):
        d_prev, d_cur = common_dates[i - 1], common_dates[i]
        for sym in bars_map:
            p_prev = date_to_close[d_prev][sym]
            p_cur = date_to_close[d_cur][sym]
            if p_prev > 0:
                out[sym].append(p_cur / p_prev - 1)
    return out


def rolling_corr(ra: list[float], rb: list[float], window: int = 20) -> list[float]:
    """逐 20 日窗口的 Pearson 相关（两序列已对齐、等长）。"""
    out: list[float] = []
    for i in range(window, len(ra) + 1):
        a, b = ra[i - window:i], rb[i - window:i]
        ma, mb = sum(a) / window, sum(b) / window
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / window
        va = sum((x - ma) ** 2 for x in a) / window
        vb = sum((y - mb) ** 2 for y in b) / window
        den = (va * vb) ** 0.5
        out.append(cov / den if den > 0 else 0.0)
    return out


def main() -> None:
    settings = load_settings()
    ds = AkShareDataSource()
    syms = list(settings.symbols)
    bars_map: dict[str, list] = {}
    for s in syms:
        bars_map[s] = ds.fetch_daily_bars(
            s, 500, settings.symbols[s].exchange, adjust="hfq"
        )
        print(f"  {s}({settings.symbols[s].name}): {len(bars_map[s])} 根")
    rets = aligned_returns(bars_map)
    n = len(rets[syms[0]]) if syms else 0
    print(f"\n共同交易日收益样本: {n}")

    print("\n20 日滚动收益相关性（平均）矩阵:")
    header = "        " + "".join(f"{s:>10}" for s in syms)
    print(header)
    for a in syms:
        row = f"{a:>8}"
        for b in syms:
            if a == b:
                row += f"{'—':>10}"
            else:
                corrs = rolling_corr(rets[a], rets[b])
                mean = statistics.mean(corrs) if corrs else float("nan")
                row += f"{mean:>10.2f}"
        print(row)

    # 结论提示：>0.7 视为高度冗余
    print("\n解读：平均滚动相关 > 0.70 → 池内高度冗余（分散度低）；<0.50 → 分散良好。")


if __name__ == "__main__":
    main()
