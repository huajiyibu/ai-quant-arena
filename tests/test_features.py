"""features.py 确定性特征计算测试（PP-2 数据层，纯函数）"""
from datetime import datetime, timedelta

import pytest

from aitrader.features import compute_features
from aitrader.models import Bar


def _bars(closes):
    base = datetime(2024, 1, 1)
    return [
        Bar("510300", base + timedelta(days=i), c, c, c, c, 1000.0)
        for i, c in enumerate(closes)
    ]


def test_features_basic_uptrend():
    closes = list(range(100, 125))  # 100..124，25 根
    feats = compute_features(_bars(closes))
    assert feats["ma5"] == pytest.approx(122.0)  # 120..124
    assert feats["ma10"] == pytest.approx(119.5)  # 115..124
    assert feats["ma20"] == pytest.approx(114.5)  # 105..124
    assert feats["ret_1d"] == pytest.approx(round(124 / 123 - 1, 6))
    assert feats["ret_5d"] == pytest.approx(round(124 / 119 - 1, 6))
    assert feats["ret_20d"] == pytest.approx(round(124 / 104 - 1, 6))
    assert feats["rsi14"] == 100.0  # 全涨 → 无亏损 → RSI 顶
    assert feats["pct_from_high20"] == pytest.approx(0.0)  # 当前即 20 日高点
    assert feats["pct_from_low20"] == pytest.approx(round(124 / 105 - 1, 6))
    assert feats["volume_ratio"] == pytest.approx(1.0)  # 量恒定
    assert feats["vol_20d"] > 0


def test_features_insufficient_data_omits_keys():
    feats = compute_features(_bars(list(range(5))))  # 5 根
    assert "ma5" in feats
    assert "ma10" not in feats
    assert "ret_1d" in feats
    assert "ret_5d" not in feats
    assert "ret_20d" not in feats
    assert "rsi14" not in feats
    assert "volume_ratio" not in feats


def test_features_only_depend_on_recent_history():
    """特征只取决于最近 N 根，更早的历史不影响 → 天然无前视"""
    a = compute_features(_bars(list(range(100, 125))))  # 100..124（25 根）
    b = compute_features(_bars(list(range(0, 125))))    # 0..124（更早历史更长，后 20 根相同）
    assert a["ma5"] == b["ma5"]
    assert a["ma20"] == b["ma20"]
    assert a["ret_20d"] == b["ret_20d"]
    assert a["rsi14"] == b["rsi14"]
    assert a["volume_ratio"] == b["volume_ratio"]
    # 加一根未来 bar 会进入窗口 → 特征应改变（这是预期的：调用方负责只传截至当日）
    future = compute_features(_bars(list(range(100, 125)) + [999.0]))
    assert future["ma5"] != a["ma5"]


def test_features_empty_and_flat():
    assert compute_features([]) == {}
    # 交替 100/101 → 涨跌各半 → RSI≈50
    closes = [100, 101] * 10
    feats = compute_features(_bars(closes))
    assert feats["rsi14"] == pytest.approx(50.0)
