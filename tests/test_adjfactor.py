"""复权因子（adjfactor.py）测试：解析/累计分红/后复权调整"""
from datetime import date, datetime

import pytest

from aitrader.adjfactor import (
    compute_adjusted_bars,
    cumulative_dividend_at,
    parse_dividends,
)
from aitrader.models import Bar


def _bar(d, o, h, l, c):
    return Bar("510300", datetime(d.year, d.month, d.day), o, h, l, c, 1000.0)


def test_parse_dividends_sorted_and_float():
    rows = parse_dividends([("2025-01-10", "0.12"), ("2024-01-10", 0.05)])
    assert rows == [(date(2024, 1, 10), 0.05), (date(2025, 1, 10), 0.12)]


def test_cumulative_dividend_at_boundaries():
    divs = parse_dividends([("2024-01-10", 0.05), ("2025-01-10", 0.12)])
    assert cumulative_dividend_at(divs, date(2023, 12, 31)) == 0.0
    assert cumulative_dividend_at(divs, date(2024, 1, 10)) == pytest.approx(0.05)
    assert cumulative_dividend_at(divs, date(2024, 6, 1)) == pytest.approx(0.05)
    assert cumulative_dividend_at(divs, date(2025, 1, 10)) == pytest.approx(0.12)
    assert cumulative_dividend_at([], date(2024, 1, 1)) == 0.0


def test_adjusted_bars_removes_ex_dividend_gap():
    """除权日累计分红 0.1：除权日及之后价格 +0.1，除权前不变 → 跳空消除"""
    divs = parse_dividends([("2024-06-20", 0.1)])
    bars = [
        _bar(date(2024, 6, 19), 4.0, 4.1, 3.9, 4.0),   # 除权前
        _bar(date(2024, 6, 20), 3.9, 4.0, 3.85, 3.9),  # 除权日（跳空）
        _bar(date(2024, 6, 21), 3.95, 4.0, 3.9, 3.95),  # 除权后
    ]
    adj = compute_adjusted_bars(bars, divs)
    assert adj[0].close == pytest.approx(4.0)            # 除权前不加
    assert adj[1].close == pytest.approx(4.0)            # 3.9 + 0.1
    assert adj[2].close == pytest.approx(4.05)           # 3.95 + 0.1
    # 除权日开盘也被调整 → 前后连续无跳空
    assert adj[1].open == pytest.approx(4.0)
    assert adj[1].high == pytest.approx(4.1)
    assert adj[1].low == pytest.approx(3.95)
    # 原列表不被修改
    assert bars[1].close == pytest.approx(3.9)


def test_adjusted_bars_no_dividend_returns_same_objects():
    bars = [_bar(date(2024, 6, 19), 4.0, 4.1, 3.9, 4.0)]
    adj = compute_adjusted_bars(bars, [])
    assert adj == bars
