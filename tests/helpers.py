"""测试工具：构造 Bar 序列"""
from datetime import datetime, timedelta

from aitrader.models import Bar


def make_bars(closes: list[float], symbol: str = "510300", start=None) -> list[Bar]:
    """按收盘价序列生成 Bar 列表"""
    start = start or datetime(2024, 1, 1)
    bars: list[Bar] = []
    for i, c in enumerate(closes):
        d = start + timedelta(days=i)
        bars.append(
            Bar(
                symbol=symbol,
                datetime=d,
                open=float(c),
                high=float(c) * 1.01,
                low=float(c) * 0.99,
                close=float(c),
                volume=1000.0,
            )
        )
    return bars
