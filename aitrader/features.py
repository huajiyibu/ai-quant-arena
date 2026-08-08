"""确定性技术特征计算：纯函数，无 IO。

用途：喂给提示词前由代码精确计算 MA/RSI/波动率等，避免 LLM 对数字串"心算"漂移
（见 docs/PREDICTION_IMPROVEMENTS.md PP-2）。

⚠️ 前置依赖：行情未复权时，除权日附近特征会跳变失真。在接入前复权数据之前，
本模块仅作为库函数与评测基础，尚未接入提示词注入（feature_inject 默认关闭）。
"""
from __future__ import annotations

from .models import Bar


def _ma(closes: list[float], n: int) -> float | None:
    """最近 n 根收盘价简单均线；不足 n 根返回 None"""
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _rsi(closes: list[float], n: int = 14) -> float:
    """RSI（简单平均口径，非 Wilder 平滑）。只用最近 n 个涨跌幅。"""
    if len(closes) < n + 1:
        return 100.0
    changes = [closes[i] - closes[i - 1] for i in range(len(closes) - n, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_g = sum(gains) / n
    avg_l = sum(losses) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def compute_features(bars: list[Bar], lookback: int = 20) -> dict:
    """基于传入 K 线计算特征字典（各指标只依赖最近 N 根，与传入总长度无关）。

    lookback 为兼容参数（预留特征窗口上限）；指标按自身需求取最后几根，
    因此更早的历史 / 多余的未来根都不影响结果——回测中调用方传入"截至当日的切片"即天然无前视。

    Returns:
        dict，含（数据不足的键省略）：
        ma5/ma10/ma20, ret_1d/ret_5d/ret_20d, vol_20d（日收益波动率）,
        rsi14, pct_from_high20/pct_from_low20, volume_ratio（5日均量/20日均量）
    """
    if not bars:
        return {}
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    feats: dict = {}

    for n, key in ((5, "ma5"), (10, "ma10"), (20, "ma20")):
        m = _ma(closes, n)
        if m is not None:
            feats[key] = round(m, 4)

    if len(closes) >= 2:
        feats["ret_1d"] = round(closes[-1] / closes[-2] - 1, 6)
    if len(closes) >= 6:
        feats["ret_5d"] = round(closes[-1] / closes[-6] - 1, 6)
    if len(closes) >= 21:
        feats["ret_20d"] = round(closes[-1] / closes[-21] - 1, 6)

    if len(closes) >= 21:
        rets = [
            closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 20, len(closes))
        ]
        mean = sum(rets) / 20
        var = sum((r - mean) ** 2 for r in rets) / 20
        feats["vol_20d"] = round(var ** 0.5, 6)

    if len(closes) >= 15:
        feats["rsi14"] = round(_rsi(closes, 14), 2)

    if len(closes) >= 20:
        last20 = closes[-20:]
        hi, lo = max(last20), min(last20)
        last = closes[-1]
        feats["pct_from_high20"] = round(last / hi - 1, 6)
        feats["pct_from_low20"] = round(last / lo - 1, 6)

    if len(vols) >= 20:
        v5 = sum(vols[-5:]) / 5
        v20 = sum(vols[-20:]) / 20
        if v20 > 0:
            feats["volume_ratio"] = round(v5 / v20, 4)

    return feats
