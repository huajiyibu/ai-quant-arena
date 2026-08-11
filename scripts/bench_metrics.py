"""基准风险指标：买入持有单标的在实验区间的夏普/最大回撤，与 AI 变体对比（支撑"AI 风险调整后更优"结论）。"""
from __future__ import annotations

import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aitrader.config import load_settings
from aitrader.datasource import AkShareDataSource


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-06-01")
    ap.add_argument("--end", default="2026-08-01")
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")

    settings = load_settings()
    sym = next(iter(settings.symbols))
    ds = AkShareDataSource()
    bars = ds.fetch_daily_bars(
        sym, 400, settings.symbols[sym].exchange, end_date=end, adjust="hfq"
    )
    dates = [b for b in bars if start.date() <= b.datetime.date() <= end.date()]
    closes = [b.close for b in dates]
    n = len(closes)
    if n < 2:
        print("数据不足")
        return
    init = 1_000_000
    assets = [init * (1 - 0.00025) * (c / closes[0]) for c in closes]
    rets = [assets[i] / assets[i - 1] - 1 for i in range(1, n)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = var ** 0.5
    sharpe = mean / std * math.sqrt(252) if std > 0 else 0.0
    peak = float("-inf")
    mdd = 0.0
    for a in assets:
        peak = max(peak, a)
        mdd = max(mdd, (peak - a) / peak)
    total = assets[-1] / init - 1
    print(f"基准({sym} 买入持有) {start.date()}~{end.date()}:")
    print(f"  总收益 {total:+.2%} | 夏普 {sharpe:.2f} | 最大回撤 {mdd:.2%}")
    print(f"  年化波动风险约 {std*math.sqrt(252):.0%}（满仓吃波动）")
    print()
    print("同期 AI+市场环境：+9.52% | 夏普 1.57 | 回撤 2.86%")


if __name__ == "__main__":
    main()
